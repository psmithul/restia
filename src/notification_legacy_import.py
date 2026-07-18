"""Bounded, idempotent adoption of V2 reminder/browser SQLite sidecars.

The source databases are opened read-only and deliberately retained.  A
content-digest marker records each attempted snapshot in the configured main
database, so a crash can retry without fabricating completion or deleting the
only legacy copy.  Canonical rows always win over later legacy snapshots.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from core.database import (
    Account,
    BrowserNotification,
    NotificationRuntimeImportRun,
    ReminderCancellation,
    ReminderDeliveryClaim,
    SessionLocal,
    utcnow_naive,
)
from src.auth_helpers import resolved_runtime_owner
from src.constants import DATA_DIR
from src.identity import ensure_account
from src.secret_storage import private_digest


MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_SOURCE_ROWS = 50_000
REMINDER_SOURCE_KIND = "reminder_delivery_sidecar_v1"
BROWSER_SOURCE_KIND = "browser_notification_sidecar_v1"
_CLAIM_STATUSES = frozenset({
    "claimed", "awaiting_browser_ack", "delivered", "failed", "cancelled",
})


class NotificationRuntimeImportError(RuntimeError):
    """A legacy notification snapshot could not be safely adopted."""


@dataclass(frozen=True)
class NotificationRuntimeImportResult:
    source_kind: str
    source_sha256: str = ""
    imported: int = 0
    skipped: int = 0
    already_completed: bool = False
    source_present: bool = False


def _source_digest(path: Path) -> str:
    if path.is_symlink():
        raise NotificationRuntimeImportError(
            "Legacy notification source must not be a symbolic link"
        )
    try:
        candidates = [path]
        wal_path = Path(str(path) + "-wal")
        if wal_path.exists():
            if wal_path.is_symlink():
                raise NotificationRuntimeImportError(
                    "Legacy notification WAL must not be a symbolic link"
                )
            candidates.append(wal_path)
        size = sum(candidate.stat().st_size for candidate in candidates)
    except OSError as exc:
        raise NotificationRuntimeImportError(
            "Legacy notification source cannot be inspected"
        ) from exc
    if size > MAX_SOURCE_BYTES:
        raise NotificationRuntimeImportError(
            "Legacy notification source exceeds the migration safety limit"
        )
    digest = hashlib.sha256()
    try:
        for candidate in candidates:
            digest.update(candidate.name.encode("utf-8"))
            digest.update(b"\0")
            with candidate.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    except OSError as exc:
        raise NotificationRuntimeImportError(
            "Legacy notification source cannot be read"
        ) from exc
    return digest.hexdigest()


def _readonly_connection(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5,
        )
    except sqlite3.Error as exc:
        raise NotificationRuntimeImportError(
            "Legacy notification source is not a readable SQLite database"
        ) from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _snapshot_digest(snapshot: dict[str, list[dict[str, Any]]]) -> str:
    """Hash logical rows so WAL checkpoint/cleanup cannot forge a new run."""

    normalized: dict[str, list[str]] = {}
    for table, rows in sorted(snapshot.items()):
        normalized[table] = sorted(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            for row in rows
        )
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if exists is None:
        return set()
    return {
        str(row[1])
        for row in conn.execute(f'SELECT * FROM pragma_table_info("{table}")')
    }


def _bounded_rows(
    conn: sqlite3.Connection,
    table: str,
    *,
    required: Iterable[str],
) -> list[dict[str, Any]]:
    columns = _table_columns(conn, table)
    if not columns:
        return []
    missing = sorted(set(required) - columns)
    if missing:
        raise NotificationRuntimeImportError(
            f"Legacy {table} schema is missing required columns"
        )
    count = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    if count > MAX_SOURCE_ROWS:
        raise NotificationRuntimeImportError(
            f"Legacy {table} exceeds the migration row safety limit"
        )
    return [
        dict(row)
        for row in conn.execute(f'SELECT * FROM "{table}"').fetchall()
    ]


def _parse_datetime(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _safe_text(value: object, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _owner_account(db, value: object) -> Account:
    username = resolved_runtime_owner(_safe_text(value, 160) or None)
    return ensure_account(db, username)


def _marker(
    db,
    *,
    source_kind: str,
    source_sha256: str,
) -> NotificationRuntimeImportRun | None:
    return db.query(NotificationRuntimeImportRun).filter(
        NotificationRuntimeImportRun.source_kind == source_kind,
        NotificationRuntimeImportRun.source_sha256 == source_sha256,
    ).one_or_none()


def _record_failed_marker(
    *, source_kind: str, source_sha256: str, error: BaseException,
) -> None:
    db = SessionLocal()
    try:
        row = _marker(
            db,
            source_kind=source_kind,
            source_sha256=source_sha256,
        )
        if row is None:
            row = NotificationRuntimeImportRun(
                id=str(uuid.uuid4()),
                source_kind=source_kind,
                source_sha256=source_sha256,
            )
            db.add(row)
        row.state = "failed"
        row.details = {
            "error_code": error.__class__.__name__.lower()[:64],
            "source_retained": True,
        }
        row.completed_at = None
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _snapshot_reminders(path: Path) -> dict[str, list[dict[str, Any]]]:
    conn = _readonly_connection(path)
    try:
        claims = _bounded_rows(
            conn,
            "reminder_delivery_claims",
            required={
                "owner", "note_id", "occurrence", "channel", "status",
                "claim_token", "claimed_at", "retry_after", "delivered_at",
            },
        )
        cancellations = _bounded_rows(
            conn,
            "reminder_delivery_cancellations",
            required={
                "owner", "note_id", "scope", "occurrence", "cancelled_at",
            },
        )
        return {"claims": claims, "cancellations": cancellations}
    except sqlite3.Error as exc:
        raise NotificationRuntimeImportError(
            "Legacy reminder source could not be queried"
        ) from exc
    finally:
        conn.close()


def _snapshot_browser(path: Path) -> dict[str, list[dict[str, Any]]]:
    conn = _readonly_connection(path)
    try:
        outbox = _bounded_rows(
            conn,
            "browser_notification_outbox",
            required={"id", "owner", "payload", "created_at", "acknowledged_at"},
        )
        cancellations = _bounded_rows(
            conn,
            "browser_reminder_cancellations",
            required={
                "owner", "note_id", "scope", "occurrence", "cancelled_at",
            },
        )
        return {"outbox": outbox, "cancellations": cancellations}
    except sqlite3.Error as exc:
        raise NotificationRuntimeImportError(
            "Legacy browser source could not be queried"
        ) from exc
    finally:
        conn.close()


def _import_cancellations(
    db,
    rows: Iterable[dict[str, Any]],
) -> tuple[int, int]:
    imported = skipped = 0
    for source in rows:
        note_id = _safe_text(source.get("note_id"), 255)
        scope = _safe_text(source.get("scope"), 24)
        occurrence = _safe_text(source.get("occurrence"), 255)
        if not note_id or scope not in {"all", "occurrence"}:
            skipped += 1
            continue
        account = _owner_account(db, source.get("owner"))
        existing = db.query(ReminderCancellation).filter(
            ReminderCancellation.owner_id == account.id,
            ReminderCancellation.note_id == note_id,
            ReminderCancellation.scope == scope,
            ReminderCancellation.occurrence == occurrence,
        ).one_or_none()
        if existing is not None:
            skipped += 1
            continue
        db.add(ReminderCancellation(
            id=str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"restia:notification-cancel:{account.id}:{note_id}:{scope}:{occurrence}",
            )),
            owner_id=account.id,
            note_id=note_id,
            scope=scope,
            occurrence=occurrence,
            cancelled_at=_parse_datetime(source.get("cancelled_at")) or utcnow_naive(),
        ))
        imported += 1
    return imported, skipped


def _import_reminder_snapshot(
    snapshot: dict[str, list[dict[str, Any]]],
    *,
    source_sha256: str,
) -> NotificationRuntimeImportResult:
    db = SessionLocal()
    try:
        existing_marker = _marker(
            db,
            source_kind=REMINDER_SOURCE_KIND,
            source_sha256=source_sha256,
        )
        if existing_marker is not None and existing_marker.state == "completed":
            db.rollback()
            return NotificationRuntimeImportResult(
                source_kind=REMINDER_SOURCE_KIND,
                source_sha256=source_sha256,
                already_completed=True,
                source_present=True,
            )
        imported = skipped = 0
        for source in snapshot["claims"]:
            note_id = _safe_text(source.get("note_id"), 255)
            occurrence = _safe_text(source.get("occurrence"), 255)
            channel = _safe_text(source.get("channel"), 64).lower() or "browser"
            status = _safe_text(source.get("status"), 32).lower()
            if not note_id or status not in _CLAIM_STATUSES:
                skipped += 1
                continue
            account = _owner_account(db, source.get("owner"))
            existing = db.query(ReminderDeliveryClaim).filter(
                ReminderDeliveryClaim.owner_id == account.id,
                ReminderDeliveryClaim.note_id == note_id,
                ReminderDeliveryClaim.occurrence == occurrence,
                ReminderDeliveryClaim.channel == channel,
            ).one_or_none()
            if existing is not None:
                skipped += 1
                continue
            token = _safe_text(source.get("claim_token"), 500)
            db.add(ReminderDeliveryClaim(
                id=str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"restia:reminder-claim:{account.id}:{note_id}:{occurrence}:{channel}",
                )),
                owner_id=account.id,
                note_id=note_id,
                occurrence=occurrence,
                channel=channel,
                status=status,
                claim_token_digest=(
                    private_digest("reminder-delivery-claim", token)
                    if token else None
                ),
                claimed_at=_parse_datetime(source.get("claimed_at")),
                retry_after=_parse_datetime(source.get("retry_after")),
                delivered_at=_parse_datetime(source.get("delivered_at")),
                last_error_code=(
                    "legacy_delivery_failed" if status == "failed" else None
                ),
                version=1,
            ))
            imported += 1
        cancel_imported, cancel_skipped = _import_cancellations(
            db, snapshot["cancellations"],
        )
        imported += cancel_imported
        skipped += cancel_skipped
        marker = existing_marker or NotificationRuntimeImportRun(
            id=str(uuid.uuid4()),
            source_kind=REMINDER_SOURCE_KIND,
            source_sha256=source_sha256,
        )
        if existing_marker is None:
            db.add(marker)
        marker.state = "completed"
        marker.details = {
            "imported": imported,
            "skipped": skipped,
            "source_retained": True,
        }
        marker.completed_at = utcnow_naive()
        db.commit()
        return NotificationRuntimeImportResult(
            source_kind=REMINDER_SOURCE_KIND,
            source_sha256=source_sha256,
            imported=imported,
            skipped=skipped,
            source_present=True,
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _browser_row_id(db, *, owner_id: str, source_id: object) -> str:
    raw = _safe_text(source_id, 255)
    candidate = raw if 0 < len(raw) <= 36 else ""
    if candidate and db.query(BrowserNotification.id).filter(
        BrowserNotification.id == candidate,
    ).first() is None:
        return candidate
    return str(uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"restia:browser-notification:{owner_id}:{raw}",
    ))


def _import_browser_snapshot(
    snapshot: dict[str, list[dict[str, Any]]],
    *,
    source_sha256: str,
) -> NotificationRuntimeImportResult:
    db = SessionLocal()
    try:
        existing_marker = _marker(
            db,
            source_kind=BROWSER_SOURCE_KIND,
            source_sha256=source_sha256,
        )
        if existing_marker is not None and existing_marker.state == "completed":
            db.rollback()
            return NotificationRuntimeImportResult(
                source_kind=BROWSER_SOURCE_KIND,
                source_sha256=source_sha256,
                already_completed=True,
                source_present=True,
            )
        imported = skipped = 0
        for source in snapshot["outbox"]:
            try:
                payload = json.loads(str(source.get("payload") or ""))
            except (TypeError, ValueError):
                skipped += 1
                continue
            if not isinstance(payload, dict):
                skipped += 1
                continue
            account = _owner_account(db, source.get("owner"))
            dedupe_key = _safe_text(source.get("dedupe_key"), 500)
            dedupe_digest = (
                private_digest("browser-notification-dedupe", dedupe_key)
                if dedupe_key else None
            )
            if dedupe_digest and db.query(BrowserNotification.id).filter(
                BrowserNotification.owner_id == account.id,
                BrowserNotification.dedupe_key_digest == dedupe_digest,
            ).first() is not None:
                skipped += 1
                continue
            row_id = _browser_row_id(
                db,
                owner_id=account.id,
                source_id=source.get("id"),
            )
            if db.query(BrowserNotification.id).filter(
                BrowserNotification.id == row_id,
            ).first() is not None:
                skipped += 1
                continue
            claim_note_id = _safe_text(source.get("claim_note_id"), 255)
            if not claim_note_id:
                task_id = _safe_text(payload.get("task_id"), 255)
                if task_id.startswith("reminder-"):
                    claim_note_id = task_id[len("reminder-"):]
            claim_occurrence = _safe_text(source.get("claim_occurrence"), 255)
            claim_channel = _safe_text(source.get("claim_channel"), 64).lower()
            claim_token = _safe_text(source.get("claim_token"), 500)
            claim_account = None
            if claim_note_id and claim_token:
                claim_account = _owner_account(
                    db, source.get("claim_owner") or source.get("owner"),
                )
                if claim_account.id != account.id:
                    claim_account = None
                    claim_token = ""
            db.add(BrowserNotification(
                id=row_id,
                owner_id=account.id,
                payload=payload,
                dedupe_key_digest=dedupe_digest,
                claim_owner_id=claim_account.id if claim_account else None,
                claim_note_id=claim_note_id,
                claim_occurrence=claim_occurrence,
                claim_channel=claim_channel,
                claim_token=claim_token or None,
                acknowledged_at=_parse_datetime(source.get("acknowledged_at")),
                created_at=_parse_datetime(source.get("created_at")) or utcnow_naive(),
            ))
            imported += 1
        cancel_imported, cancel_skipped = _import_cancellations(
            db, snapshot["cancellations"],
        )
        imported += cancel_imported
        skipped += cancel_skipped
        marker = existing_marker or NotificationRuntimeImportRun(
            id=str(uuid.uuid4()),
            source_kind=BROWSER_SOURCE_KIND,
            source_sha256=source_sha256,
        )
        if existing_marker is None:
            db.add(marker)
        marker.state = "completed"
        marker.details = {
            "imported": imported,
            "skipped": skipped,
            "source_retained": True,
        }
        marker.completed_at = utcnow_naive()
        db.commit()
        return NotificationRuntimeImportResult(
            source_kind=BROWSER_SOURCE_KIND,
            source_sha256=source_sha256,
            imported=imported,
            skipped=skipped,
            source_present=True,
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _import_one(path: Path, *, source_kind: str) -> NotificationRuntimeImportResult:
    if not path.exists():
        return NotificationRuntimeImportResult(source_kind=source_kind)
    source_sha256 = ""
    try:
        # The raw digest is used only if schema/snapshot validation fails. A
        # successful marker uses the canonical logical snapshot digest below,
        # which is stable whether SQLite has already checkpointed its WAL.
        source_sha256 = _source_digest(path)
        if source_kind == REMINDER_SOURCE_KIND:
            snapshot = _snapshot_reminders(path)
            source_sha256 = _snapshot_digest(snapshot)
            return _import_reminder_snapshot(
                snapshot,
                source_sha256=source_sha256,
            )
        snapshot = _snapshot_browser(path)
        source_sha256 = _snapshot_digest(snapshot)
        return _import_browser_snapshot(
            snapshot,
            source_sha256=source_sha256,
        )
    except Exception as exc:
        if source_sha256:
            _record_failed_marker(
                source_kind=source_kind,
                source_sha256=source_sha256,
                error=exc,
            )
        if isinstance(exc, NotificationRuntimeImportError):
            raise
        raise NotificationRuntimeImportError(
            "Legacy notification state could not be imported"
        ) from exc


def import_legacy_notification_runtime(
    *,
    reminder_path: str | Path | None = None,
    browser_path: str | Path | None = None,
) -> dict[str, NotificationRuntimeImportResult]:
    """Adopt both sidecars without modifying or deleting either source."""

    base = Path(DATA_DIR)
    reminder = Path(reminder_path) if reminder_path is not None else (
        base / "reminder_delivery_claims.sqlite3"
    )
    browser = Path(browser_path) if browser_path is not None else (
        base / "browser_notification_outbox.sqlite3"
    )
    return {
        "reminder": _import_one(reminder, source_kind=REMINDER_SOURCE_KIND),
        "browser": _import_one(browser, source_kind=BROWSER_SOURCE_KIND),
    }


__all__ = [
    "BROWSER_SOURCE_KIND",
    "NotificationRuntimeImportError",
    "NotificationRuntimeImportResult",
    "REMINDER_SOURCE_KIND",
    "import_legacy_notification_runtime",
]
