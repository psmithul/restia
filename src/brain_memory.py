"""Optional Restia Brain-backed memory adapter.

This keeps Restia's existing memory manager contract intact while allowing a
local Restia Brain checkout to act as the external recall/write layer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.memory import MemoryManager

logger = logging.getLogger(__name__)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BrainMemoryConfig:
    root: Path
    vault_path: Path
    db_path: Path
    owner: str
    sync_writes: bool = True
    list_limit: int = 1000

    @classmethod
    def from_env(cls) -> Optional["BrainMemoryConfig"]:
        if not _truthy(os.getenv("ODYSSEUS_BRAIN_MEMORY_ENABLED")):
            return None

        root_value = (
            os.getenv("ODYSSEUS_BRAIN_ROOT")
            or os.getenv("RESTIA_BRAIN_ROOT")
            or "/app/brain"
        )
        root = Path(root_value).expanduser().resolve()
        vault_path = Path(os.getenv("RESTIA_BRAIN_VAULT") or root / "vault").expanduser().resolve()
        db_path = Path(os.getenv("RESTIA_BRAIN_DB") or root / "data" / "brain.sqlite").expanduser().resolve()
        owner = (os.getenv("ODYSSEUS_BRAIN_OWNER") or "admin").strip()
        sync_writes = os.getenv("ODYSSEUS_BRAIN_SYNC_WRITES", "true").strip().lower() != "false"
        try:
            list_limit = int(os.getenv("ODYSSEUS_BRAIN_LIST_LIMIT", "1000"))
        except ValueError:
            list_limit = 1000
        return cls(
            root=root,
            vault_path=vault_path,
            db_path=db_path,
            owner=owner,
            sync_writes=sync_writes,
            list_limit=max(1, list_limit),
        )


class BrainAugmentedMemoryManager(MemoryManager):
    """MemoryManager-compatible adapter with Restia Brain recall/write support.

    Native ``memory.json`` remains as a compatibility cache for existing UI,
    edit/delete, and vector-index behavior. Brain search is queried on demand
    for chat recall, and newly saved native memories are mirrored into the brain
    when write sync is enabled.
    """

    def __init__(self, data_dir: str, config: BrainMemoryConfig, brain_service: Any):
        super().__init__(data_dir)
        self.brain_config = config
        self.brain_service = brain_service
        self._syncing_writes = False

    def load(self, owner: str = None) -> List[Dict]:
        """Load native cache rows plus read-only Restia Brain rows.

        ``load_all()`` intentionally remains native-only so mutation paths that
        edit/delete by id do not treat external Brain notes as JSON-backed rows.
        """
        native = super().load(owner=owner)
        external = self.list_external_memories(owner=owner, max_items=self.brain_config.list_limit)
        return self._merge_memory_rows(native, external)

    def get_external_relevant_memories(
        self,
        query: str,
        *,
        owner: Optional[str] = None,
        max_items: int = 3,
    ) -> List[Dict[str, Any]]:
        if not query.strip() or not self._owner_allowed(owner):
            return []
        try:
            results = self.brain_service.search(query, limit=max_items)
        except Exception as exc:
            logger.warning("Restia Brain recall failed: %s", exc)
            return []

        memories: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for result in results:
            memory = self._brain_result_to_memory(result, owner=owner)
            if not memory:
                continue
            key = memory["id"]
            if key in seen:
                continue
            seen.add(key)
            memories.append(memory)
        return memories[:max_items]

    def list_external_memories(
        self,
        *,
        owner: Optional[str] = None,
        max_items: int = 100,
    ) -> List[Dict[str, Any]]:
        """Return Restia Brain notes as read-only memory rows for the UI list."""
        if not self._owner_allowed(owner):
            return []
        try:
            now = time.time()
            if now - getattr(self, "_last_read_sync", 0.0) > 60.0 and hasattr(self.brain_service, "sync"):
                self.brain_service.sync()
                self._last_read_sync = now
            store = getattr(self.brain_service, "store", None)
            if store is None:
                return []
            with store.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT id, path, title, type, frontmatter, content,
                           created_at, updated_at, indexed_at
                    FROM documents
                    WHERE TRIM(COALESCE(content, '')) != ''
                    ORDER BY COALESCE(updated_at, indexed_at, created_at) DESC, path ASC
                    LIMIT ?
                    """,
                    (max(1, int(max_items)),),
                ).fetchall()
        except Exception as exc:
            logger.warning("Restia Brain list failed: %s", exc)
            return []

        memories: List[Dict[str, Any]] = []
        for row in rows:
            memory = self._brain_document_to_memory(row, owner=owner)
            if memory:
                memories.append(memory)
        return memories

    def save(self, entries: List[Dict]):
        persistable_entries = [
            entry for entry in entries
            if not self._is_external_memory_entry(entry)
        ]
        if self.brain_config.sync_writes and not self._syncing_writes:
            self._sync_unsynced_entries(persistable_entries)
        return super().save(persistable_entries)

    def find_duplicates(self, text: str, entries: List[Dict] = None) -> List[Dict]:
        if entries is None:
            entries = self.load()
        return super().find_duplicates(text, entries)

    def _sync_unsynced_entries(self, entries: List[Dict]) -> None:
        self._syncing_writes = True
        try:
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                text = str(entry.get("text") or "").strip()
                if not text or not self._owner_allowed(entry.get("owner")):
                    continue
                metadata = entry.setdefault("metadata", {})
                if not isinstance(metadata, dict):
                    metadata = {}
                    entry["metadata"] = metadata
                if metadata.get("brain_path") or entry.get("source") == "restia_brain":
                    continue

                source = str(entry.get("source") or "odysseus")
                owner = str(entry.get("owner") or self.brain_config.owner or "local")
                try:
                    path = self.brain_service.capture(
                        text,
                        source=f"odysseus:{source}:{owner}",
                        approved=True,
                    )
                except Exception as exc:
                    logger.warning("Restia Brain write sync failed: %s", exc)
                    continue
                metadata["brain_path"] = path
                metadata["brain_synced_at"] = int(time.time())
        finally:
            self._syncing_writes = False

    def _owner_allowed(self, owner: Optional[str]) -> bool:
        configured = self.brain_config.owner
        if not configured:
            return True
        return owner in (None, configured)

    @staticmethod
    def _is_external_memory_entry(entry: Any) -> bool:
        if not isinstance(entry, dict):
            return False
        return bool(
            entry.get("readonly")
            or entry.get("source") == "restia_brain"
            or str(entry.get("id") or "").startswith("brain:")
        )

    @classmethod
    def _merge_memory_rows(cls, native: List[Dict], external: List[Dict]) -> List[Dict]:
        merged = list(native)
        seen = {entry.get("id") for entry in merged if isinstance(entry, dict)}
        for entry in external:
            if not isinstance(entry, dict):
                continue
            memory_id = entry.get("id")
            if memory_id in seen:
                continue
            seen.add(memory_id)
            merged.append(entry)
        return merged

    def _brain_result_to_memory(self, result: Any, *, owner: Optional[str]) -> Optional[Dict[str, Any]]:
        title = str(getattr(result, "title", "") or "")
        path = str(getattr(result, "path", "") or "")
        chunk = str(getattr(result, "chunk", "") or "").strip()
        citation = str(getattr(result, "citation", "") or path or title)
        if not chunk and not title:
            return None

        text = self._compact_brain_text(chunk or title)
        digest = hashlib.sha1(
            f"{citation}\n{text}".encode("utf-8"), usedforsecurity=False
        ).hexdigest()[:24]
        memory: Dict[str, Any] = {
            "id": f"brain:{digest}",
            "text": text,
            "timestamp": 0,
            "source": "restia_brain",
            "category": "brain",
            "uses": 0,
            "metadata": {
                "brain_path": path,
                "citation": citation,
                "reason": str(getattr(result, "reason", "") or ""),
                "score": getattr(result, "score", None),
                "title": title,
            },
        }
        if owner:
            memory["owner"] = owner
        return memory

    def _brain_document_to_memory(self, row: Any, *, owner: Optional[str]) -> Optional[Dict[str, Any]]:
        path = str(row["path"] or "")
        title = str(row["title"] or path or "Brain note").strip()
        body = str(row["content"] or "").strip()
        if not body and not title:
            return None

        text = self._compact_brain_text(body or title)
        title_prefix = title.lower()
        if title and title_prefix not in text[: max(80, len(title))].lower():
            text = f"{title}: {text}"

        metadata: Dict[str, Any] = {
            "brain_path": path,
            "citation": path,
            "title": title,
            "note_type": str(row["type"] or "brain"),
        }
        try:
            frontmatter = json.loads(str(row["frontmatter"] or "{}"))
            if isinstance(frontmatter, dict):
                metadata["frontmatter"] = frontmatter
        except json.JSONDecodeError:
            pass

        memory: Dict[str, Any] = {
            "id": f"brain:{hashlib.sha1(path.encode('utf-8'), usedforsecurity=False).hexdigest()[:24]}",
            "text": text,
            "timestamp": self._timestamp_from_iso(
                row["updated_at"] or row["indexed_at"] or row["created_at"]
            ),
            "source": "restia_brain",
            "category": str(row["type"] or "brain"),
            "uses": 0,
            "readonly": True,
            "metadata": metadata,
        }
        if owner:
            memory["owner"] = owner
        return memory

    @staticmethod
    def _compact_brain_text(text: str, limit: int = 900) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        compact = " ".join(lines) if lines else text.strip()
        if len(compact) <= limit:
            return compact
        return compact[: limit - 1].rstrip() + "..."

    @staticmethod
    def _timestamp_from_iso(value: Any) -> int:
        if not value:
            return 0
        try:
            return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
        except (TypeError, ValueError, OSError):
            return 0


def build_brain_memory_manager(data_dir: str) -> Optional[BrainAugmentedMemoryManager]:
    config = BrainMemoryConfig.from_env()
    if config is None:
        return None
    if not config.root.exists():
        logger.warning("Restia Brain disabled: root does not exist: %s", config.root)
        return None

    root_str = str(config.root)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)

    try:
        from restia_brain.config import BrainConfig
        from restia_brain.runtime import create_service
    except Exception as exc:
        logger.warning("Restia Brain disabled: import failed from %s: %s", config.root, exc)
        return None

    try:
        brain_config = BrainConfig(
            vault_path=config.vault_path,
            db_path=config.db_path,
        )
        service = create_service(brain_config)
        service.initialize()
        service.sync()
    except Exception as exc:
        logger.warning("Restia Brain disabled: initialization failed: %s", exc)
        return None

    logger.info("Restia Brain memory enabled: vault=%s db=%s", config.vault_path, config.db_path)
    return BrainAugmentedMemoryManager(data_dir, config, service)
