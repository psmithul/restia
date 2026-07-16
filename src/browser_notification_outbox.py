"""Durable, owner-scoped browser notification outbox."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS browser_notification_outbox (
            id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            dedupe_key TEXT NOT NULL DEFAULT '',
            claim_owner TEXT NOT NULL DEFAULT '',
            claim_note_id TEXT NOT NULL DEFAULT '',
            claim_occurrence TEXT NOT NULL DEFAULT '',
            claim_channel TEXT NOT NULL DEFAULT '',
            claim_token TEXT NOT NULL DEFAULT '',
            acknowledged_at TEXT
        )
        """
    )
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(browser_notification_outbox)")}
    if "dedupe_key" not in columns:
        conn.execute("ALTER TABLE browser_notification_outbox ADD COLUMN dedupe_key TEXT NOT NULL DEFAULT ''")
    for name in ("claim_owner", "claim_note_id", "claim_occurrence", "claim_channel", "claim_token"):
        if name not in columns:
            conn.execute(f"ALTER TABLE browser_notification_outbox ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_browser_notification_pending
        ON browser_notification_outbox(owner, acknowledged_at, created_at)
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_browser_notification_dedupe
        ON browser_notification_outbox(owner, dedupe_key)
        WHERE dedupe_key <> ''
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS browser_reminder_cancellations (
            owner TEXT NOT NULL,
            note_id TEXT NOT NULL,
            scope TEXT NOT NULL,
            occurrence TEXT NOT NULL DEFAULT '',
            cancelled_at TEXT NOT NULL,
            PRIMARY KEY (owner, note_id, scope, occurrence)
        )
        """
    )
    return conn


def enqueue_browser_notification(
    path: str | Path,
    owner: str,
    payload: dict,
    *,
    dedupe_key: str = "",
    reminder_claim: dict | None = None,
) -> dict:
    """Persist a notification before it is reported as browser-deliverable."""
    normalized_owner = str(owner or "").strip().lower()
    if not normalized_owner:
        raise ValueError("browser notification owner is required")
    item = dict(payload or {})
    item["id"] = str(item.get("id") or uuid.uuid4())
    item["owner"] = normalized_owner
    item.setdefault("timestamp", _utcnow_iso())
    normalized_dedupe = str(dedupe_key or "").strip()[:500]
    claim = reminder_claim if isinstance(reminder_claim, dict) else {}
    claim_owner = str(claim.get("owner") or "").strip().lower()
    claim_note_id = str(claim.get("note_id") or "").strip()
    claim_occurrence = str(claim.get("occurrence") or "").strip()
    claim_channel = str(claim.get("channel") or "").strip().lower()
    claim_token = str(claim.get("token") or "").strip()
    conn = _connect(path)
    try:
        # The cancellation tombstone check and enqueue are one write
        # transaction. This closes the cancel-before-enqueue barrier race.
        conn.execute("BEGIN IMMEDIATE")
        if claim_note_id:
            cancelled = conn.execute(
                """
                SELECT 1 FROM browser_reminder_cancellations
                WHERE owner = ? AND note_id = ?
                  AND (scope = 'all' OR (scope = 'occurrence' AND occurrence = ?))
                LIMIT 1
                """,
                (normalized_owner, claim_note_id, claim_occurrence),
            ).fetchone()
            if cancelled is not None:
                conn.commit()
                return {
                    **item,
                    "_outbox_created": False,
                    "_outbox_pending": False,
                    "_outbox_cancelled": True,
                }
        try:
            conn.execute(
                """
                INSERT INTO browser_notification_outbox(
                    id, owner, payload, created_at, dedupe_key,
                    claim_owner, claim_note_id, claim_occurrence, claim_channel, claim_token
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["id"],
                    normalized_owner,
                    json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                    item["timestamp"],
                    normalized_dedupe,
                    claim_owner,
                    claim_note_id,
                    claim_occurrence,
                    claim_channel,
                    claim_token,
                ),
            )
            created = True
            pending = True
        except sqlite3.IntegrityError:
            if not normalized_dedupe:
                raise
            row = conn.execute(
                """
                SELECT payload, acknowledged_at FROM browser_notification_outbox
                WHERE owner = ? AND dedupe_key = ?
                """,
                (normalized_owner, normalized_dedupe),
            ).fetchone()
            if row is None:
                raise
            existing = json.loads(row["payload"])
            if isinstance(existing, dict):
                item = existing
            created = False
            pending = row["acknowledged_at"] is None
            if pending and claim_token:
                conn.execute(
                    """
                    UPDATE browser_notification_outbox
                    SET claim_owner = ?, claim_note_id = ?, claim_occurrence = ?,
                        claim_channel = ?, claim_token = ?
                    WHERE owner = ? AND dedupe_key = ? AND acknowledged_at IS NULL
                    """,
                    (
                        claim_owner,
                        claim_note_id,
                        claim_occurrence,
                        claim_channel,
                        claim_token,
                        normalized_owner,
                        normalized_dedupe,
                    ),
                )
        # Acknowledged history is useful briefly for diagnostics but does not
        # need to grow forever. Unacknowledged rows are never evicted by a cap.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        conn.execute(
            "DELETE FROM browser_notification_outbox WHERE acknowledged_at IS NOT NULL AND acknowledged_at < ?",
            (cutoff,),
        )
        conn.commit()
        return {**item, "_outbox_created": created, "_outbox_pending": pending}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def pending_browser_notifications(path: str | Path, owner: str, limit: int = 200) -> list[dict]:
    """Read without draining; only an explicit owner-scoped ack removes it."""
    normalized_owner = str(owner or "").strip().lower()
    if not normalized_owner:
        return []
    conn = _connect(path)
    try:
        rows = conn.execute(
            """
            SELECT payload FROM browser_notification_outbox
            WHERE owner = ? AND acknowledged_at IS NULL
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (normalized_owner, max(1, min(int(limit), 1000))),
        ).fetchall()
    finally:
        conn.close()
    items: list[dict] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            items.append(payload)
    return items


def cancel_browser_notifications_for_reminder(
    path: str | Path,
    owner: str,
    note_id: str,
    *,
    occurrence: str | None = None,
) -> int:
    """Tombstone pending browser rows for a rescheduled/archived note.

    Both primary browser deliveries and browser mirrors share the task id, so
    this catches rows created before claim linkage was added as well as V2 rows.
    A write transaction serializes cancellation with a concurrent client ACK.
    """

    normalized_owner = str(owner or "").strip().lower()
    normalized_note = str(note_id or "").strip()
    if not normalized_owner or not normalized_note:
        return 0
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        scope = "all" if occurrence is None else "occurrence"
        normalized_occurrence = "" if occurrence is None else str(occurrence or "").strip()
        conn.execute(
            """
            INSERT INTO browser_reminder_cancellations(
                owner, note_id, scope, occurrence, cancelled_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(owner, note_id, scope, occurrence) DO UPDATE SET
                cancelled_at = excluded.cancelled_at
            """,
            (
                normalized_owner,
                normalized_note,
                scope,
                normalized_occurrence,
                _utcnow_iso(),
            ),
        )
        rows = conn.execute(
            """
            SELECT id, payload, dedupe_key, claim_note_id, claim_occurrence
            FROM browser_notification_outbox
            WHERE owner = ? AND acknowledged_at IS NULL
            """,
            (normalized_owner,),
        ).fetchall()
        ids: list[str] = []
        expected_task_id = f"reminder-{normalized_note}"
        normalized_occurrence = None if occurrence is None else str(occurrence or "").strip()
        for row in rows:
            linked_note = str(row["claim_note_id"] or "") == normalized_note
            linked_occurrence = str(row["claim_occurrence"] or "")
            matches = linked_note and (
                normalized_occurrence is None
                or linked_occurrence == normalized_occurrence
            )
            if not matches and normalized_occurrence is not None:
                # Compatibility for rows created before explicit linkage.
                old_key = f"reminder:{normalized_note}:{normalized_occurrence}"
                dedupe_key = str(row["dedupe_key"] or "")
                matches = dedupe_key == old_key or dedupe_key.startswith(old_key + ":")
            if not matches and normalized_occurrence is None:
                try:
                    payload = json.loads(row["payload"])
                    matches = isinstance(payload, dict) and str(payload.get("task_id") or "") == expected_task_id
                except (TypeError, json.JSONDecodeError):
                    matches = False
            if matches:
                ids.append(str(row["id"]))
        if ids:
            placeholders = ",".join("?" for _ in ids)
            cursor = conn.execute(
                f"""
                DELETE FROM browser_notification_outbox
                WHERE owner = ? AND acknowledged_at IS NULL AND id IN ({placeholders})
                """,
                (normalized_owner, *ids),
            )
            count = cursor.rowcount
        else:
            count = 0
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rearm_browser_notifications_for_reminder(
    path: str | Path,
    owner: str,
    note_id: str,
    *,
    occurrence: str | None = None,
) -> int:
    """Clear cancellation state when a user intentionally restores a reminder."""

    normalized_owner = str(owner or "").strip().lower()
    normalized_note = str(note_id or "").strip()
    if not normalized_owner or not normalized_note:
        return 0
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if occurrence is None:
            cursor = conn.execute(
                "DELETE FROM browser_reminder_cancellations WHERE owner = ? AND note_id = ?",
                (normalized_owner, normalized_note),
            )
        else:
            cursor = conn.execute(
                """
                DELETE FROM browser_reminder_cancellations
                WHERE owner = ? AND note_id = ?
                  AND (scope = 'all' OR (scope = 'occurrence' AND occurrence = ?))
                """,
                (normalized_owner, normalized_note, str(occurrence or "").strip()),
            )
        conn.commit()
        return cursor.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def acknowledge_browser_notifications(path: str | Path, owner: str, ids: list[str]) -> int:
    """Acknowledge only rows owned by the authenticated profile."""
    normalized_owner = str(owner or "").strip().lower()
    normalized_ids = list(dict.fromkeys(str(value or "").strip() for value in ids if str(value or "").strip()))
    if not normalized_owner or not normalized_ids:
        return 0
    normalized_ids = normalized_ids[:1000]
    placeholders = ",".join("?" for _ in normalized_ids)
    conn = _connect(path)
    try:
        cursor = conn.execute(
            f"""
            UPDATE browser_notification_outbox
            SET acknowledged_at = ?
            WHERE owner = ? AND id IN ({placeholders})
            """,
            (_utcnow_iso(), normalized_owner, *normalized_ids),
        )
        return cursor.rowcount
    finally:
        conn.close()


def browser_notification_ack_candidates(path: str | Path, owner: str, ids: list[str]) -> list[dict]:
    """Return owner-scoped pending rows and their internal claim linkage."""
    normalized_owner = str(owner or "").strip().lower()
    normalized_ids = list(dict.fromkeys(str(value or "").strip() for value in ids if str(value or "").strip()))
    if not normalized_owner or not normalized_ids:
        return []
    normalized_ids = normalized_ids[:1000]
    placeholders = ",".join("?" for _ in normalized_ids)
    conn = _connect(path)
    try:
        rows = conn.execute(
            f"""
            SELECT id, claim_owner, claim_note_id, claim_occurrence, claim_channel, claim_token
            FROM browser_notification_outbox
            WHERE owner = ? AND acknowledged_at IS NULL AND id IN ({placeholders})
            """,
            (normalized_owner, *normalized_ids),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()
