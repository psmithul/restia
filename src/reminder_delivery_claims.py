"""Durable claims for reminder side effects.

The browser reminder endpoint and the background scanner both call the same
dispatcher.  These SQLite claims make that dispatcher the single authority for
whether a reminder occurrence may perform an external side effect.  SQLite's
``BEGIN IMMEDIATE`` transaction serializes claims across threads and app
processes; a short lease makes an interrupted attempt retryable.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class ReminderClaim:
    acquired: bool
    token: str = ""
    reason: str = ""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminder_delivery_claims (
            owner TEXT NOT NULL,
            note_id TEXT NOT NULL,
            occurrence TEXT NOT NULL,
            channel TEXT NOT NULL,
            status TEXT NOT NULL,
            claim_token TEXT NOT NULL DEFAULT '',
            claimed_at TEXT,
            retry_after TEXT,
            delivered_at TEXT,
            last_error TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (owner, note_id, occurrence, channel)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reminder_delivery_cancellations (
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


def claim_reminder_delivery(
    path: str | Path | None,
    *,
    owner: str,
    note_id: str,
    occurrence: str,
    channel: str,
    dedupe_seconds: int = 25 * 60,
    lease_seconds: int = 5 * 60,
    now: datetime | None = None,
) -> ReminderClaim:
    """Atomically claim one reminder occurrence.

    An explicit occurrence (normally the note due timestamp) is permanently
    deduplicated after acknowledgement.  Callers without an occurrence retain
    the legacy rolling 25-minute dedupe window.
    """

    if path is None:
        from src.notification_delivery_authority import claim_reminder

        result = claim_reminder(
            owner=owner,
            note_id=note_id,
            occurrence=occurrence,
            channel=channel,
            dedupe_seconds=dedupe_seconds,
            lease_seconds=lease_seconds,
            now=now,
        )
        return ReminderClaim(
            acquired=result.acquired,
            token=result.token,
            reason=result.reason,
        )

    current = (now or _utcnow()).astimezone(timezone.utc)
    normalized_owner = str(owner or "").strip().lower()
    normalized_note = str(note_id or "").strip()
    normalized_occurrence = str(occurrence or "").strip()
    normalized_channel = str(channel or "browser").strip().lower() or "browser"
    if not normalized_note:
        return ReminderClaim(True, token="")

    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cancelled = conn.execute(
            """
            SELECT 1 FROM reminder_delivery_cancellations
            WHERE owner = ? AND note_id = ?
              AND (scope = 'all' OR (scope = 'occurrence' AND occurrence = ?))
            LIMIT 1
            """,
            (normalized_owner, normalized_note, normalized_occurrence),
        ).fetchone()
        if cancelled is not None:
            conn.commit()
            return ReminderClaim(False, reason="cancelled")
        row = conn.execute(
            """
            SELECT status, claimed_at, retry_after, delivered_at
            FROM reminder_delivery_claims
            WHERE owner = ? AND note_id = ? AND occurrence = ? AND channel = ?
            """,
            (normalized_owner, normalized_note, normalized_occurrence, normalized_channel),
        ).fetchone()

        if row is not None:
            delivered_at = _parse_utc(row["delivered_at"])
            if row["status"] == "delivered" and delivered_at is not None:
                permanent = bool(normalized_occurrence)
                recent = delivered_at >= current - timedelta(seconds=max(1, dedupe_seconds))
                if permanent or recent:
                    conn.commit()
                    return ReminderClaim(False, reason="delivered")

            claimed_at = _parse_utc(row["claimed_at"])
            if row["status"] == "claimed" and claimed_at is not None:
                if claimed_at >= current - timedelta(seconds=max(1, lease_seconds)):
                    conn.commit()
                    return ReminderClaim(False, reason="in_flight")
            if row["status"] == "awaiting_browser_ack":
                conn.commit()
                return ReminderClaim(False, reason="browser_ack_pending")

            retry_after = _parse_utc(row["retry_after"])
            if row["status"] == "failed" and retry_after is not None and retry_after > current:
                conn.commit()
                return ReminderClaim(False, reason="retry_backoff")

        token = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO reminder_delivery_claims (
                owner, note_id, occurrence, channel, status, claim_token,
                claimed_at, retry_after, delivered_at, last_error
            ) VALUES (?, ?, ?, ?, 'claimed', ?, ?, NULL, NULL, '')
            ON CONFLICT(owner, note_id, occurrence, channel) DO UPDATE SET
                status = 'claimed',
                claim_token = excluded.claim_token,
                claimed_at = excluded.claimed_at,
                retry_after = NULL,
                delivered_at = NULL,
                last_error = ''
            """,
            (
                normalized_owner,
                normalized_note,
                normalized_occurrence,
                normalized_channel,
                token,
                current.isoformat(),
            ),
        )
        conn.commit()
        return ReminderClaim(True, token=token)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def acknowledge_reminder_delivery(
    path: str | Path | None,
    *,
    owner: str,
    note_id: str,
    occurrence: str,
    channel: str,
    token: str,
    now: datetime | None = None,
) -> bool:
    """Mark a claimed delivery successful if the caller still owns it."""

    if path is None:
        from src.notification_delivery_authority import acknowledge_reminder

        return acknowledge_reminder(
            owner=owner,
            note_id=note_id,
            occurrence=occurrence,
            channel=channel,
            token=token,
            now=now,
        )

    if not note_id or not token:
        return False
    current = (now or _utcnow()).astimezone(timezone.utc)
    conn = _connect(path)
    try:
        cursor = conn.execute(
            """
            UPDATE reminder_delivery_claims
            SET status = 'delivered', delivered_at = ?, retry_after = NULL,
                last_error = ''
            WHERE owner = ? AND note_id = ? AND occurrence = ? AND channel = ?
              AND status IN ('claimed', 'awaiting_browser_ack') AND claim_token = ?
            """,
            (
                current.isoformat(),
                str(owner or "").strip().lower(),
                str(note_id or "").strip(),
                str(occurrence or "").strip(),
                str(channel or "browser").strip().lower() or "browser",
                token,
            ),
        )
        if cursor.rowcount == 1:
            return True
        row = conn.execute(
            """
            SELECT status FROM reminder_delivery_claims
            WHERE owner = ? AND note_id = ? AND occurrence = ? AND channel = ?
            """,
            (
                str(owner or "").strip().lower(),
                str(note_id or "").strip(),
                str(occurrence or "").strip(),
                str(channel or "browser").strip().lower() or "browser",
            ),
        ).fetchone()
        return bool(row is not None and row["status"] == "delivered")
    finally:
        conn.close()


def reminder_claim_is_active(
    path: str | Path | None,
    *,
    owner: str,
    note_id: str,
    occurrence: str,
    channel: str,
    token: str,
) -> bool:
    """Revalidate claim ownership immediately before an irreversible send."""

    if path is None:
        from src.notification_delivery_authority import reminder_claim_active

        return reminder_claim_active(
            owner=owner,
            note_id=note_id,
            occurrence=occurrence,
            channel=channel,
            token=token,
        )

    if not note_id or not token:
        return False
    conn = _connect(path)
    try:
        row = conn.execute(
            """
            SELECT 1 FROM reminder_delivery_claims
            WHERE owner = ? AND note_id = ? AND occurrence = ? AND channel = ?
              AND status = 'claimed' AND claim_token = ?
            LIMIT 1
            """,
            (
                str(owner or "").strip().lower(),
                str(note_id or "").strip(),
                str(occurrence or "").strip(),
                str(channel or "browser").strip().lower() or "browser",
                str(token or "").strip(),
            ),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def await_browser_ack(
    path: str | Path | None,
    *,
    owner: str,
    note_id: str,
    occurrence: str,
    channel: str,
    token: str,
) -> bool:
    """Keep a browser-primary claim pending until its outbox row is acked."""
    if path is None:
        from src.notification_delivery_authority import await_reminder_browser_ack

        return await_reminder_browser_ack(
            owner=owner,
            note_id=note_id,
            occurrence=occurrence,
            channel=channel,
            token=token,
        )
    if not note_id or not token:
        return False
    conn = _connect(path)
    try:
        cursor = conn.execute(
            """
            UPDATE reminder_delivery_claims
            SET status = 'awaiting_browser_ack', retry_after = NULL,
                last_error = ''
            WHERE owner = ? AND note_id = ? AND occurrence = ? AND channel = ?
              AND status = 'claimed' AND claim_token = ?
            """,
            (
                str(owner or "").strip().lower(),
                str(note_id or "").strip(),
                str(occurrence or "").strip(),
                str(channel or "browser").strip().lower() or "browser",
                token,
            ),
        )
        return cursor.rowcount == 1
    finally:
        conn.close()


def fail_reminder_delivery(
    path: str | Path | None,
    *,
    owner: str,
    note_id: str,
    occurrence: str,
    channel: str,
    token: str,
    error: str = "delivery_failed",
    retry_seconds: int = 5 * 60,
    now: datetime | None = None,
) -> bool:
    """Release a claim into a durable retry-backoff state."""

    if path is None:
        from src.notification_delivery_authority import fail_reminder

        return fail_reminder(
            owner=owner,
            note_id=note_id,
            occurrence=occurrence,
            channel=channel,
            token=token,
            error=error,
            retry_seconds=retry_seconds,
            now=now,
        )

    if not note_id or not token:
        return False
    current = (now or _utcnow()).astimezone(timezone.utc)
    retry_after = current + timedelta(seconds=max(1, retry_seconds))
    conn = _connect(path)
    try:
        cursor = conn.execute(
            """
            UPDATE reminder_delivery_claims
            SET status = 'failed', retry_after = ?, last_error = ?
            WHERE owner = ? AND note_id = ? AND occurrence = ? AND channel = ?
              AND status = 'claimed' AND claim_token = ?
            """,
            (
                retry_after.isoformat(),
                str(error or "delivery_failed")[:240],
                str(owner or "").strip().lower(),
                str(note_id or "").strip(),
                str(occurrence or "").strip(),
                str(channel or "browser").strip().lower() or "browser",
                token,
            ),
        )
        return cursor.rowcount == 1
    finally:
        conn.close()


def cancel_reminder_deliveries(
    path: str | Path | None,
    *,
    owner: str,
    note_id: str,
    occurrence: str | None = None,
) -> int:
    """Cancel unfinished claims after a note is changed, archived, or deleted."""

    if path is None:
        from src.notification_delivery_authority import cancel_reminders

        return cancel_reminders(
            owner=owner,
            note_id=note_id,
            occurrence=occurrence,
        )

    normalized_owner = str(owner or "").strip().lower()
    normalized_note = str(note_id or "").strip()
    if not normalized_note:
        return 0
    current = _utcnow()
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        scope = "all" if occurrence is None else "occurrence"
        normalized_occurrence = "" if occurrence is None else str(occurrence or "").strip()
        conn.execute(
            """
            INSERT INTO reminder_delivery_cancellations(
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
                current.isoformat(),
            ),
        )
        params: list[str] = [normalized_owner, normalized_note]
        occurrence_clause = ""
        if occurrence is not None:
            occurrence_clause = " AND occurrence = ?"
            params.append(str(occurrence or "").strip())
        cursor = conn.execute(
            f"""
            UPDATE reminder_delivery_claims
            SET status = 'cancelled', claim_token = '', retry_after = NULL,
                last_error = 'note_changed_or_removed'
            WHERE owner = ? AND note_id = ?{occurrence_clause}
              AND status IN ('claimed', 'awaiting_browser_ack', 'failed')
            """,
            tuple(params),
        )
        conn.commit()
        return cursor.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rearm_reminder_deliveries(
    path: str | Path | None,
    *,
    owner: str,
    note_id: str,
    occurrence: str | None = None,
) -> int:
    """Clear unfinished delivery state for an intentionally restored reminder."""

    if path is None:
        from src.notification_delivery_authority import rearm_reminders

        return rearm_reminders(
            owner=owner,
            note_id=note_id,
            occurrence=occurrence,
        )

    normalized_owner = str(owner or "").strip().lower()
    normalized_note = str(note_id or "").strip()
    if not normalized_note:
        return 0
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if occurrence is None:
            conn.execute(
                "DELETE FROM reminder_delivery_cancellations WHERE owner = ? AND note_id = ?",
                (normalized_owner, normalized_note),
            )
        else:
            conn.execute(
                """
                DELETE FROM reminder_delivery_cancellations
                WHERE owner = ? AND note_id = ?
                  AND (scope = 'all' OR (scope = 'occurrence' AND occurrence = ?))
                """,
                (normalized_owner, normalized_note, str(occurrence or "").strip()),
            )
        params: list[str] = [normalized_owner, normalized_note]
        occurrence_clause = ""
        if occurrence is not None:
            occurrence_clause = " AND occurrence = ?"
            params.append(str(occurrence or "").strip())
        cursor = conn.execute(
            f"""
            DELETE FROM reminder_delivery_claims
            WHERE owner = ? AND note_id = ?{occurrence_clause}
              AND status IN ('claimed', 'awaiting_browser_ack', 'failed', 'cancelled')
            """,
            tuple(params),
        )
        conn.commit()
        return cursor.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
