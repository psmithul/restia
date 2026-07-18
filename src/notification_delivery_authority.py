"""Canonical SQL authority for reminder claims and browser notifications.

All production callers use this module through the compatibility wrappers in
``reminder_delivery_claims`` and ``browser_notification_outbox``.  The
database row owner is always immutable ``Account.id``; usernames are accepted
only as authenticated compatibility aliases and resolved before any query.

Every cancel/claim/enqueue mutation locks the owner's Account row first.  That
small owner-scoped fence closes cancel-before-enqueue races on PostgreSQL while
SQLite retains its ordinary single-writer guarantee.  External side effects
remain outside these transactions and are protected by opaque claim tokens.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from core.database import (
    Account,
    BrowserNotification,
    ReminderCancellation,
    ReminderDeliveryClaim,
    SessionLocal,
    utcnow_naive,
)
from src.identity import ensure_account, find_account
from src.secret_storage import private_digest


_ERROR_CODE_RE = re.compile(r"[^a-z0-9_]+")
_SAFE_ERROR_CODES = frozenset({
    "delivery_failed",
    "required_channel_not_delivered",
    "telegram_recipient_delivery_failed",
})


@dataclass(frozen=True)
class SqlReminderClaim:
    acquired: bool
    token: str = ""
    reason: str = ""


def _naive_utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current
    return current.astimezone(timezone.utc).replace(tzinfo=None)


def _error_code(value: object) -> str:
    raw = str(value or "delivery_failed").strip().lower()
    code = _ERROR_CODE_RE.sub("_", raw).strip("_")[:64]
    return code if code in _SAFE_ERROR_CODES else "delivery_failed"


def _normalized_owner(value: object) -> str:
    return str(value or "").strip().lower()


def _normalized_note(value: object) -> str:
    return str(value or "").strip()[:255]


def _normalized_occurrence(value: object) -> str:
    return str(value or "").strip()[:255]


def _normalized_channel(value: object) -> str:
    return (str(value or "browser").strip().lower() or "browser")[:64]


def _account(db, owner: object, *, create: bool) -> Account | None:
    raw = _normalized_owner(owner)
    if not raw:
        return None
    account = db.query(Account).filter(
        Account.id == raw,
        Account.status == "active",
    ).one_or_none()
    if account is None:
        account = ensure_account(db, raw) if create else find_account(db, raw)
    if account is None:
        return None
    # Every claim/enqueue/cancel path calls this helper with create=True.  The
    # row lock serializes those owner-scoped barriers on PostgreSQL.
    return db.query(Account).filter(
        Account.id == account.id,
        Account.status == "active",
    ).with_for_update().one()


def _cancellation_query(db, *, owner_id: str, note_id: str, occurrence: str):
    return db.query(ReminderCancellation).filter(
        ReminderCancellation.owner_id == owner_id,
        ReminderCancellation.note_id == note_id,
        (
            (ReminderCancellation.scope == "all")
            | (
                (ReminderCancellation.scope == "occurrence")
                & (ReminderCancellation.occurrence == occurrence)
            )
        ),
    )


def _upsert_cancellation(
    db,
    *,
    owner_id: str,
    note_id: str,
    occurrence: str | None,
    now: datetime,
) -> ReminderCancellation:
    scope = "all" if occurrence is None else "occurrence"
    normalized_occurrence = "" if occurrence is None else _normalized_occurrence(occurrence)
    row = db.query(ReminderCancellation).filter(
        ReminderCancellation.owner_id == owner_id,
        ReminderCancellation.note_id == note_id,
        ReminderCancellation.scope == scope,
        ReminderCancellation.occurrence == normalized_occurrence,
    ).with_for_update().one_or_none()
    if row is None:
        row = ReminderCancellation(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            note_id=note_id,
            scope=scope,
            occurrence=normalized_occurrence,
            cancelled_at=now,
        )
        db.add(row)
    else:
        row.cancelled_at = now
    db.flush()
    return row


def claim_reminder(
    *,
    owner: str,
    note_id: str,
    occurrence: str,
    channel: str,
    dedupe_seconds: int,
    lease_seconds: int,
    now: datetime | None,
) -> SqlReminderClaim:
    normalized_note = _normalized_note(note_id)
    if not normalized_note:
        return SqlReminderClaim(True, token="")
    normalized_occurrence = _normalized_occurrence(occurrence)
    normalized_channel = _normalized_channel(channel)
    current = _naive_utc(now)
    db = SessionLocal()
    try:
        account = _account(db, owner, create=True)
        if account is None:
            raise ValueError("reminder delivery owner is required")
        if _cancellation_query(
            db,
            owner_id=account.id,
            note_id=normalized_note,
            occurrence=normalized_occurrence,
        ).first() is not None:
            db.commit()
            return SqlReminderClaim(False, reason="cancelled")

        row = db.query(ReminderDeliveryClaim).filter(
            ReminderDeliveryClaim.owner_id == account.id,
            ReminderDeliveryClaim.note_id == normalized_note,
            ReminderDeliveryClaim.occurrence == normalized_occurrence,
            ReminderDeliveryClaim.channel == normalized_channel,
        ).with_for_update().one_or_none()
        if row is not None:
            if row.status == "delivered" and row.delivered_at is not None:
                permanent = bool(normalized_occurrence)
                recent = row.delivered_at >= current - timedelta(
                    seconds=max(1, int(dedupe_seconds)),
                )
                if permanent or recent:
                    db.commit()
                    return SqlReminderClaim(False, reason="delivered")
            if row.status == "claimed" and row.claimed_at is not None:
                if row.claimed_at >= current - timedelta(
                    seconds=max(1, int(lease_seconds)),
                ):
                    db.commit()
                    return SqlReminderClaim(False, reason="in_flight")
            if row.status == "awaiting_browser_ack":
                db.commit()
                return SqlReminderClaim(False, reason="browser_ack_pending")
            if (
                row.status == "failed"
                and row.retry_after is not None
                and row.retry_after > current
            ):
                db.commit()
                return SqlReminderClaim(False, reason="retry_backoff")

        token = uuid.uuid4().hex
        digest = private_digest("reminder-delivery-claim", token)
        if row is None:
            row = ReminderDeliveryClaim(
                id=str(uuid.uuid4()),
                owner_id=account.id,
                note_id=normalized_note,
                occurrence=normalized_occurrence,
                channel=normalized_channel,
                version=1,
            )
            db.add(row)
        else:
            row.version = int(row.version or 1) + 1
        row.status = "claimed"
        row.claim_token_digest = digest
        row.claimed_at = current
        row.retry_after = None
        row.delivered_at = None
        row.last_error_code = None
        db.commit()
        return SqlReminderClaim(True, token=token)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _claimed_row(
    db,
    *,
    owner: str,
    note_id: str,
    occurrence: str,
    channel: str,
    token: str,
    allowed_statuses: Iterable[str],
) -> ReminderDeliveryClaim | None:
    account = _account(db, owner, create=False)
    if account is None:
        return None
    digest = private_digest("reminder-delivery-claim", token)
    return db.query(ReminderDeliveryClaim).filter(
        ReminderDeliveryClaim.owner_id == account.id,
        ReminderDeliveryClaim.note_id == _normalized_note(note_id),
        ReminderDeliveryClaim.occurrence == _normalized_occurrence(occurrence),
        ReminderDeliveryClaim.channel == _normalized_channel(channel),
        ReminderDeliveryClaim.status.in_(tuple(allowed_statuses)),
        ReminderDeliveryClaim.claim_token_digest == digest,
    ).with_for_update().one_or_none()


def acknowledge_reminder(
    *, owner: str, note_id: str, occurrence: str, channel: str,
    token: str, now: datetime | None,
) -> bool:
    if not _normalized_note(note_id) or not str(token or "").strip():
        return False
    db = SessionLocal()
    try:
        row = _claimed_row(
            db,
            owner=owner,
            note_id=note_id,
            occurrence=occurrence,
            channel=channel,
            token=token,
            allowed_statuses=("claimed", "awaiting_browser_ack"),
        )
        if row is not None:
            row.status = "delivered"
            row.delivered_at = _naive_utc(now)
            row.retry_after = None
            row.last_error_code = None
            row.version = int(row.version or 1) + 1
            db.commit()
            return True
        account = _account(db, owner, create=False)
        existing = None if account is None else db.query(ReminderDeliveryClaim).filter(
            ReminderDeliveryClaim.owner_id == account.id,
            ReminderDeliveryClaim.note_id == _normalized_note(note_id),
            ReminderDeliveryClaim.occurrence == _normalized_occurrence(occurrence),
            ReminderDeliveryClaim.channel == _normalized_channel(channel),
            ReminderDeliveryClaim.status == "delivered",
        ).one_or_none()
        db.commit()
        return existing is not None
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def reminder_claim_active(
    *, owner: str, note_id: str, occurrence: str, channel: str, token: str,
) -> bool:
    if not _normalized_note(note_id) or not str(token or "").strip():
        return False
    db = SessionLocal()
    try:
        row = _claimed_row(
            db,
            owner=owner,
            note_id=note_id,
            occurrence=occurrence,
            channel=channel,
            token=token,
            allowed_statuses=("claimed",),
        )
        db.rollback()
        return row is not None
    finally:
        db.close()


def await_reminder_browser_ack(
    *, owner: str, note_id: str, occurrence: str, channel: str, token: str,
) -> bool:
    if not _normalized_note(note_id) or not str(token or "").strip():
        return False
    db = SessionLocal()
    try:
        row = _claimed_row(
            db,
            owner=owner,
            note_id=note_id,
            occurrence=occurrence,
            channel=channel,
            token=token,
            allowed_statuses=("claimed",),
        )
        if row is None:
            db.rollback()
            return False
        row.status = "awaiting_browser_ack"
        row.retry_after = None
        row.last_error_code = None
        row.version = int(row.version or 1) + 1
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def fail_reminder(
    *, owner: str, note_id: str, occurrence: str, channel: str, token: str,
    error: str, retry_seconds: int, now: datetime | None,
) -> bool:
    if not _normalized_note(note_id) or not str(token or "").strip():
        return False
    current = _naive_utc(now)
    db = SessionLocal()
    try:
        row = _claimed_row(
            db,
            owner=owner,
            note_id=note_id,
            occurrence=occurrence,
            channel=channel,
            token=token,
            allowed_statuses=("claimed",),
        )
        if row is None:
            db.rollback()
            return False
        row.status = "failed"
        row.retry_after = current + timedelta(seconds=max(1, int(retry_seconds)))
        row.last_error_code = _error_code(error)
        row.version = int(row.version or 1) + 1
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def cancel_reminders(
    *, owner: str, note_id: str, occurrence: str | None,
) -> int:
    normalized_note = _normalized_note(note_id)
    if not normalized_note:
        return 0
    db = SessionLocal()
    try:
        account = _account(db, owner, create=True)
        if account is None:
            raise ValueError("reminder delivery owner is required")
        _upsert_cancellation(
            db,
            owner_id=account.id,
            note_id=normalized_note,
            occurrence=occurrence,
            now=utcnow_naive(),
        )
        query = db.query(ReminderDeliveryClaim).filter(
            ReminderDeliveryClaim.owner_id == account.id,
            ReminderDeliveryClaim.note_id == normalized_note,
            ReminderDeliveryClaim.status.in_((
                "claimed", "awaiting_browser_ack", "failed",
            )),
        )
        if occurrence is not None:
            query = query.filter(
                ReminderDeliveryClaim.occurrence == _normalized_occurrence(occurrence),
            )
        rows = query.with_for_update().all()
        for row in rows:
            row.status = "cancelled"
            row.claim_token_digest = None
            row.retry_after = None
            row.last_error_code = "note_changed_or_removed"
            row.version = int(row.version or 1) + 1
        db.commit()
        return len(rows)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _clear_cancellations(
    db,
    *,
    owner_id: str,
    note_id: str,
    occurrence: str | None,
) -> int:
    query = db.query(ReminderCancellation).filter(
        ReminderCancellation.owner_id == owner_id,
        ReminderCancellation.note_id == note_id,
    )
    if occurrence is not None:
        normalized = _normalized_occurrence(occurrence)
        query = query.filter(
            (ReminderCancellation.scope == "all")
            | (
                (ReminderCancellation.scope == "occurrence")
                & (ReminderCancellation.occurrence == normalized)
            ),
        )
    rows = query.with_for_update().all()
    for row in rows:
        db.delete(row)
    return len(rows)


def rearm_reminders(
    *, owner: str, note_id: str, occurrence: str | None,
) -> int:
    normalized_note = _normalized_note(note_id)
    if not normalized_note:
        return 0
    db = SessionLocal()
    try:
        account = _account(db, owner, create=True)
        if account is None:
            raise ValueError("reminder delivery owner is required")
        _clear_cancellations(
            db,
            owner_id=account.id,
            note_id=normalized_note,
            occurrence=occurrence,
        )
        query = db.query(ReminderDeliveryClaim).filter(
            ReminderDeliveryClaim.owner_id == account.id,
            ReminderDeliveryClaim.note_id == normalized_note,
            ReminderDeliveryClaim.status.in_((
                "claimed", "awaiting_browser_ack", "failed", "cancelled",
            )),
        )
        if occurrence is not None:
            query = query.filter(
                ReminderDeliveryClaim.occurrence == _normalized_occurrence(occurrence),
            )
        rows = query.with_for_update().all()
        for row in rows:
            db.delete(row)
        db.commit()
        return len(rows)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def enqueue_browser(
    owner: str,
    payload: dict,
    *,
    dedupe_key: str,
    reminder_claim: dict | None,
) -> dict:
    normalized_owner = _normalized_owner(owner)
    if not normalized_owner:
        raise ValueError("browser notification owner is required")
    item = dict(payload or {})
    item["id"] = str(item.get("id") or uuid.uuid4())
    item["owner"] = normalized_owner
    item.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    normalized_dedupe = str(dedupe_key or "").strip()[:500]
    dedupe_digest = (
        private_digest("browser-notification-dedupe", normalized_dedupe)
        if normalized_dedupe else None
    )
    claim = reminder_claim if isinstance(reminder_claim, dict) else {}
    claim_note_id = _normalized_note(claim.get("note_id"))
    claim_occurrence = _normalized_occurrence(claim.get("occurrence"))
    claim_channel = _normalized_channel(claim.get("channel")) if claim_note_id else ""
    claim_token = str(claim.get("token") or "").strip()
    claim_owner = _normalized_owner(claim.get("owner"))

    db = SessionLocal()
    try:
        account = _account(db, normalized_owner, create=True)
        if account is None:
            raise ValueError("browser notification owner is required")
        claim_account = None
        if claim_note_id:
            claim_account = _account(db, claim_owner or normalized_owner, create=False)
            if claim_account is None or claim_account.id != account.id:
                raise ValueError("browser notification claim owner must match owner")
            if _cancellation_query(
                db,
                owner_id=account.id,
                note_id=claim_note_id,
                occurrence=claim_occurrence,
            ).first() is not None:
                db.commit()
                return {
                    **item,
                    "_outbox_created": False,
                    "_outbox_pending": False,
                    "_outbox_cancelled": True,
                }

        existing = None
        if dedupe_digest:
            existing = db.query(BrowserNotification).filter(
                BrowserNotification.owner_id == account.id,
                BrowserNotification.dedupe_key_digest == dedupe_digest,
            ).with_for_update().one_or_none()
        if existing is None:
            row = BrowserNotification(
                id=item["id"],
                owner_id=account.id,
                payload=item,
                dedupe_key_digest=dedupe_digest,
                claim_owner_id=claim_account.id if claim_account is not None else None,
                claim_note_id=claim_note_id,
                claim_occurrence=claim_occurrence,
                claim_channel=claim_channel,
                claim_token=claim_token or None,
            )
            db.add(row)
            created = True
            pending = True
        else:
            existing_payload = existing.payload
            if isinstance(existing_payload, dict):
                item = dict(existing_payload)
            created = False
            pending = existing.acknowledged_at is None
            if pending and claim_token:
                existing.claim_owner_id = claim_account.id if claim_account else None
                existing.claim_note_id = claim_note_id
                existing.claim_occurrence = claim_occurrence
                existing.claim_channel = claim_channel
                existing.claim_token = claim_token

        cutoff = utcnow_naive() - timedelta(days=7)
        db.query(BrowserNotification).filter(
            BrowserNotification.acknowledged_at.isnot(None),
            BrowserNotification.acknowledged_at < cutoff,
        ).delete(synchronize_session=False)
        db.commit()
        return {**item, "_outbox_created": created, "_outbox_pending": pending}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def pending_browser(owner: str, *, limit: int) -> list[dict]:
    db = SessionLocal()
    try:
        account = _account(db, owner, create=False)
        if account is None:
            db.rollback()
            return []
        rows = db.query(BrowserNotification).filter(
            BrowserNotification.owner_id == account.id,
            BrowserNotification.acknowledged_at.is_(None),
        ).order_by(
            BrowserNotification.created_at.asc(), BrowserNotification.id.asc(),
        ).limit(max(1, min(int(limit), 1000))).all()
        result = [dict(row.payload) for row in rows if isinstance(row.payload, dict)]
        db.rollback()
        return result
    finally:
        db.close()


def cancel_browser(
    owner: str,
    note_id: str,
    *,
    occurrence: str | None,
) -> int:
    normalized_note = _normalized_note(note_id)
    if not _normalized_owner(owner) or not normalized_note:
        return 0
    db = SessionLocal()
    try:
        account = _account(db, owner, create=True)
        if account is None:
            raise ValueError("browser notification owner is required")
        _upsert_cancellation(
            db,
            owner_id=account.id,
            note_id=normalized_note,
            occurrence=occurrence,
            now=utcnow_naive(),
        )
        query = db.query(BrowserNotification).filter(
            BrowserNotification.owner_id == account.id,
            BrowserNotification.acknowledged_at.is_(None),
        )
        rows = query.with_for_update().all()
        normalized_occurrence = (
            None if occurrence is None else _normalized_occurrence(occurrence)
        )
        expected_task_id = f"reminder-{normalized_note}"
        matched = []
        for row in rows:
            direct = row.claim_note_id == normalized_note and (
                normalized_occurrence is None
                or row.claim_occurrence == normalized_occurrence
            )
            if not direct and normalized_occurrence is None:
                payload = row.payload
                direct = (
                    isinstance(payload, dict)
                    and str(payload.get("task_id") or "") == expected_task_id
                )
            if direct:
                matched.append(row)
        for row in matched:
            db.delete(row)
        db.commit()
        return len(matched)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def rearm_browser(
    owner: str,
    note_id: str,
    *,
    occurrence: str | None,
) -> int:
    normalized_note = _normalized_note(note_id)
    if not _normalized_owner(owner) or not normalized_note:
        return 0
    db = SessionLocal()
    try:
        account = _account(db, owner, create=True)
        if account is None:
            raise ValueError("browser notification owner is required")
        count = _clear_cancellations(
            db,
            owner_id=account.id,
            note_id=normalized_note,
            occurrence=occurrence,
        )
        db.commit()
        return count
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def browser_ack_candidates(owner: str, ids: list[str]) -> list[dict]:
    normalized_ids = list(dict.fromkeys(
        str(value or "").strip() for value in ids if str(value or "").strip()
    ))[:1000]
    if not _normalized_owner(owner) or not normalized_ids:
        return []
    db = SessionLocal()
    try:
        account = _account(db, owner, create=False)
        if account is None:
            db.rollback()
            return []
        rows = db.query(BrowserNotification).filter(
            BrowserNotification.owner_id == account.id,
            BrowserNotification.acknowledged_at.is_(None),
            BrowserNotification.id.in_(normalized_ids),
        ).all()
        result = [{
            "id": row.id,
            "claim_owner": row.claim_owner_id or "",
            "claim_note_id": row.claim_note_id or "",
            "claim_occurrence": row.claim_occurrence or "",
            "claim_channel": row.claim_channel or "",
            "claim_token": row.claim_token or "",
        } for row in rows]
        db.rollback()
        return result
    finally:
        db.close()


def acknowledge_browser(owner: str, ids: list[str]) -> int:
    normalized_ids = list(dict.fromkeys(
        str(value or "").strip() for value in ids if str(value or "").strip()
    ))[:1000]
    if not _normalized_owner(owner) or not normalized_ids:
        return 0
    db = SessionLocal()
    try:
        account = _account(db, owner, create=False)
        if account is None:
            db.rollback()
            return 0
        rows = db.query(BrowserNotification).filter(
            BrowserNotification.owner_id == account.id,
            BrowserNotification.acknowledged_at.is_(None),
            BrowserNotification.id.in_(normalized_ids),
        ).with_for_update().all()
        now = utcnow_naive()
        for row in rows:
            row.acknowledged_at = now
        db.commit()
        return len(rows)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
