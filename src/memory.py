import json
import logging
import os
import time
import uuid
import re
from typing import List, Dict, Tuple
from datetime import datetime

logger = logging.getLogger(__name__)

def tokenize(text: str) -> List[str]:
    return [word.strip('.,!?";') for word in text.split()]

def get_text_similarity(text1: str, text2: str) -> float:
    if not text1 or not text2: return 0.0
    t1, t2 = set(tokenize(text1.lower())), set(tokenize(text2.lower()))
    if not t1 and not t2: return 1.0
    if not t1 or not t2: return 0.0
    return len(t1 & t2) / len(t1 | t2)

class MemoryManager:
    """Mnemosyne-backed implementation of MemoryManager."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.mnemo = None
        self._init_mnemosyne()
        self._migrate_legacy_json()

    def _init_mnemosyne(self):
        try:
            # Mnemosyne initializes a default SQLite database while its module
            # is imported, before this instance can pass ``db_path``. Keep that
            # import-time database inside Restia's reviewed data directory
            # unless the operator explicitly configured a different location.
            os.makedirs(self.data_dir, exist_ok=True)
            os.environ.setdefault("MNEMOSYNE_DATA_DIR", self.data_dir)
            from mnemosyne import Mnemosyne
            db_path = os.path.join(self.data_dir, "mnemosyne.db")
            self.mnemo = Mnemosyne(db_path=db_path)
            logger.info("Mnemosyne memory initialized at %s", db_path)
        except ImportError:
            logger.error("Failed to import mnemosyne. Is it installed?")
            raise

    def _migrate_legacy_json(self):
        legacy_path = os.path.join(self.data_dir, "memory.json")
        if not os.path.exists(legacy_path):
            return

        try:
            with open(legacy_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            if data:
                logger.info("Migrating %d legacy memories to Mnemosyne...", len(data))
                migrated = 0
                for entry in data:
                    if not isinstance(entry, dict) or not entry.get("text"):
                        continue

                    meta = dict(entry.get("metadata") or {})
                    meta.update({
                        "category": entry.get("category", "fact"),
                        "uses": entry.get("uses", 0),
                        "legacy_id": entry.get("id")
                    })
                    owner_val = entry.get("owner")
                    if owner_val:
                        meta["owner"] = owner_val

                    self._store_memory_row(
                        content=entry["text"],
                        source=entry.get("source", "user"),
                        metadata=meta,
                        owner=owner_val,
                        memory_id=entry.get("id"),
                        session_id=entry.get("session_id"),
                        timestamp=entry.get("timestamp"),
                        trust_tier="IMPORTED",
                    )
                    migrated += 1

                logger.info("Migrated %d memories.", migrated)

            backup_path = legacy_path + ".bak"
            os.rename(legacy_path, backup_path)
        except Exception as e:
            logger.error("Error migrating legacy memory.json: %s", e)

    @staticmethod
    def _iso_timestamp(value=None) -> str:
        if value:
            try:
                if isinstance(value, (int, float)) or str(value).isdigit():
                    return datetime.fromtimestamp(float(value)).isoformat()
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
            except Exception:
                pass
        return datetime.now().isoformat()

    def _store_memory_row(
        self,
        *,
        content: str,
        source: str = "user",
        metadata: Dict = None,
        owner: str = None,
        memory_id: str = None,
        session_id: str = None,
        timestamp=None,
        importance: float = 0.5,
        trust_tier: str = "STATED",
    ) -> str:
        """Write native memory rows without invoking Mnemosyne BEAM extraction."""
        if not self.mnemo:
            raise RuntimeError("Mnemosyne is not initialized")

        memory_id = str(memory_id or uuid.uuid4())
        meta = dict(metadata or {})
        if owner:
            meta["owner"] = owner
        if session_id:
            meta["session_id"] = session_id
        timestamp_iso = self._iso_timestamp(timestamp)
        session = session_id or getattr(self.mnemo, "session_id", "default")
        category = meta.get("category", "fact")
        cursor = self.mnemo.conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO working_memory
            (id, content, source, timestamp, session_id, importance, metadata_json,
             author_id, veracity, memory_type, trust_tier, scope)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                content,
                source,
                timestamp_iso,
                session,
                importance,
                json.dumps(meta),
                owner,
                "unknown",
                category,
                trust_tier,
                "global",
            ),
        )
        cursor.execute(
            """
            INSERT OR REPLACE INTO memories
            (id, content, source, timestamp, session_id, importance, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                content,
                source,
                timestamp_iso,
                session,
                importance,
                json.dumps(meta),
            ),
        )
        self.mnemo.conn.commit()
        return memory_id

    def extract_memory_from_chat(self, chat_history: List[Dict], session_id: str = None) -> List[Dict]:
        memories = []
        for msg in chat_history:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            content = str(msg.get("content", ""))
            for line in content.split("\n"):
                line = line.strip()
                text_match = re.match(r"^(?:[-*•]|\d+\.)\s*(.*)", line)
                if text_match:
                    text = text_match.group(1).strip()
                    if text:
                        memories.append({
                            "text": text,
                            "timestamp": int(datetime.now().timestamp()),
                            "session_id": session_id
                        })
        return memories

    def _validate_entries(self, entries: List[Dict]) -> List[Dict]:
        valid = []
        for entry in entries or []:
            if not isinstance(entry, dict):
                continue
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            cleaned = dict(entry)
            cleaned.setdefault("id", str(uuid.uuid4()))
            cleaned["text"] = text
            cleaned.setdefault("timestamp", int(time.time()))
            cleaned.setdefault("source", "unknown")
            cleaned.setdefault("category", "fact")
            cleaned.setdefault("uses", 0)
            valid.append(cleaned)
        return valid

    def process_inline_memory_command(self, message: str) -> Tuple[bool, str]:
        pattern = r"^(?:remember|memorize|save|note|store)[:\-]?\s+(.+)$"
        match = re.match(pattern, message.strip(), re.IGNORECASE)
        return (True, match.group(1).strip()) if match else (False, "")

    def _to_odysseus(self, m: dict) -> dict:
        meta_json = m.get("metadata_json") or "{}"
        try:
            meta = json.loads(meta_json)
        except:
            meta = {}

        ts_str = m.get("timestamp") or ""
        ts = 0
        if ts_str:
            try:
                ts = int(datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp())
            except:
                pass

        return {
            "id": m.get("id"),
            "text": m.get("content", ""),
            "timestamp": ts,
            "source": m.get("source", "user"),
            "category": meta.get("category", "fact"),
            "uses": meta.get("uses", 0),
            "owner": m.get("author_id") or meta.get("owner"),
            "session_id": m.get("session_id") or meta.get("session_id"),
            "metadata": meta,
        }

    def load_all(self) -> List[Dict]:
        if not self.mnemo: return []

        cursor = self.mnemo.conn.cursor()
        cursor.execute("SELECT * FROM working_memory")
        rows = cursor.fetchall()

        entries = []
        for row in rows:
            m = dict(row)
            entries.append(self._to_odysseus(m))
        return entries

    def load(self, owner: str = None) -> List[Dict]:
        entries = self.load_all()
        if owner is None:
            return entries
        return [e for e in entries if e.get("owner") == owner]

    def claim_ownerless(self, owner: str):
        if not self.mnemo: return
        cursor = self.mnemo.conn.cursor()
        cursor.execute(
            "UPDATE working_memory SET author_id = ? WHERE author_id IS NULL OR author_id = ''",
            (owner,)
        )
        cursor.execute(
            "UPDATE memories SET metadata_json = json_insert(COALESCE(metadata_json, '{}'), '$.owner', ?) WHERE id IN (SELECT id FROM working_memory WHERE author_id = ?)",
            (owner, owner)
        )
        self.mnemo.conn.commit()

    def save(self, entries: List[Dict]):
        if not self.mnemo: return
        entries = self._validate_entries(entries)
        cursor = self.mnemo.conn.cursor()
        entry_ids = {e.get("id") for e in entries if e.get("id")}
        if entry_ids:
            placeholders = ",".join("?" for _ in entry_ids)
            cursor.execute(f"DELETE FROM working_memory WHERE id NOT IN ({placeholders})", tuple(entry_ids))
            cursor.execute(f"DELETE FROM memories WHERE id NOT IN ({placeholders})", tuple(entry_ids))
        else:
            cursor.execute("DELETE FROM working_memory")
            cursor.execute("DELETE FROM memories")

        for e in entries:
            eid = e.get("id")
            if not eid: continue

            meta = dict(e.get("metadata") or {})
            meta.update({
                "category": e.get("category", meta.get("category", "fact")),
                "uses": e.get("uses", meta.get("uses", 0)),
                "owner": e.get("owner", meta.get("owner")),
                "session_id": e.get("session_id", meta.get("session_id")),
            })
            self._store_memory_row(
                content=e.get("text"),
                source=e.get("source") or "user",
                metadata=meta,
                owner=e.get("owner"),
                memory_id=eid,
                session_id=e.get("session_id"),
                timestamp=e.get("timestamp"),
            )
        self.mnemo.conn.commit()

    def add_entry(self, text: str, source: str = "user", category: str = "fact", owner: str = None) -> Dict:
        if not text.strip():
            raise ValueError("Memory text cannot be empty")

        meta = {"category": category, "uses": 0}
        if owner:
            meta["owner"] = owner

        mid = self._store_memory_row(
            content=text.strip(),
            source=source,
            metadata=meta,
            owner=owner,
        )

        return {
            "id": mid,
            "text": text.strip(),
            "timestamp": int(time.time()),
            "source": source,
            "category": category,
            "uses": 0,
            "owner": owner,
            "metadata": meta,
        }

    def increment_uses(self, ids: List[str]) -> None:
        if not ids or not self.mnemo: return

        cursor = self.mnemo.conn.cursor()
        for mid in set(ids):
            cursor.execute(
                "UPDATE working_memory SET metadata_json = json_set(COALESCE(metadata_json, '{}'), '$.uses', COALESCE(json_extract(metadata_json, '$.uses'), 0) + 1) WHERE id = ?",
                (mid,)
            )
            cursor.execute(
                "UPDATE memories SET metadata_json = json_set(COALESCE(metadata_json, '{}'), '$.uses', COALESCE(json_extract(metadata_json, '$.uses'), 0) + 1) WHERE id = ?",
                (mid,)
            )
        self.mnemo.conn.commit()

    def find_duplicates(self, text: str, entries: List[Dict] = None) -> List[Dict]:
        if entries is None:
            entries = self.load()
        text_lower = text.strip().lower()
        return [e for e in entries if e["text"].lower() == text_lower]

    def categorize_memory_by_relevance(self, message: str, memories: list):
        categories = {"contacts": [], "preferences": [], "facts": [], "tasks": []}
        msg_lower = message.lower()

        for mem in memories:
            text_lower = mem["text"].lower()
            if any(w in text_lower for w in ["phone", "email", "address", "lives", "works"]):
                if any(w in msg_lower for w in ["contact", "phone", "address", "email"]):
                    categories["contacts"].append(mem)
            elif any(w in text_lower for w in ["likes", "dislikes", "prefers", "favorite"]):
                if any(w in msg_lower for w in ["like", "prefer", "favorite", "want"]):
                    categories["preferences"].append(mem)
            elif any(w in text_lower for w in ["todo", "task", "remind", "meeting"]):
                if any(w in msg_lower for w in ["todo", "task", "schedule", "remind"]):
                    categories["tasks"].append(mem)
            else:
                if get_text_similarity(message, mem["text"]) > 0.4:
                    categories["facts"].append(mem)
        return categories

    def get_relevant_memories(self, query: str, memories: list, threshold: float = 0.05, max_items: int = 8):
        if not self.mnemo or not query.strip(): return []

        owner = None
        if memories and memories[0].get("owner"):
            owner = memories[0].get("owner")

        try:
            results = self.mnemo.recall(query, top_k=max_items, author_id=owner)
        except Exception:
            logger.debug("Mnemosyne recall failed; using text fallback", exc_info=True)
            results = []

        odysseus_results = []
        for r in results:
            odysseus_results.append(self._to_odysseus(r))

        if odysseus_results:
            return odysseus_results

        query_lower = query.strip().lower()
        scored = []
        for memory in memories or []:
            if not isinstance(memory, dict):
                continue
            text = str(memory.get("text") or "")
            if not text:
                continue
            text_lower = text.lower()
            score = get_text_similarity(query_lower, text_lower)
            if query_lower in text_lower:
                score = max(score, 1.0)
            if score >= threshold:
                scored.append((score, memory))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [memory for _score, memory in scored[:max_items]]
