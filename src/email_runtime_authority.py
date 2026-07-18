"""Canonical SQL authority for mutable email runtime state.

The local email SQLite file is a rebuildable connector/LLM cache only.  This
module owns durable tags, automation configuration and idempotency, and manual
scheduled delivery in the configured SQLAlchemy database.  Every private value
is inside an encrypted ORM payload; searchable columns are keyed digests.

Claims commit before IMAP/SMTP/LLM/calendar work begins.  Completion and retry
updates are fenced by both an opaque claim digest and an optimistic version,
and database time—not process time—decides lease expiry.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from core.database import (
    Account,
    EmailAccount,
    EmailAutomationRule,
    EmailAutomationRun,
    EmailRuntimeImportRun,
    EmailScheduledDelivery,
    EmailTagState,
    SessionLocal,
)
from src.identity import ensure_account, find_account
from src.secret_storage import private_digest


AUTOMATION_RULE_KEYS = (
    "email_auto_summarize",
    "email_auto_reply",
    "email_auto_tag",
    "email_auto_spam",
    "email_auto_calendar",
)
AUTOMATION_OPERATIONS = frozenset({
    "summary", "reply", "classify", "calendar", "email_received",
})
SAFE_AUTOMATION_ERRORS = frozenset({"operation_failed", "payload_invalid"})
SAFE_SCHEDULE_ERRORS = frozenset({
    "smtp_failed", "payload_invalid", "email_account_unavailable",
})
DEFAULT_LEASE_SECONDS = 5 * 60
MAX_ATTEMPTS = 5
MAX_IMPORT_BATCH = 500
MAX_TAG_ROWS = 20_000
MAX_LEGACY_DATABASE_BYTES = 256 * 1024 * 1024
_HEX_64 = re.compile(r"[0-9a-f]{64}")


class EmailRuntimeAuthorityError(RuntimeError):
    """Canonical email runtime state could not be accessed safely."""


@dataclass(frozen=True)
class AutomationClaim:
    row_id: str
    owner_id: str
    owner_username: str
    account_key: str
    operation: str
    message_digest: str
    claim_token_digest: str
    version: int
    attempts: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class ScheduledDeliveryClaim:
    row_id: str
    owner_id: str
    owner_username: str
    email_account_id: str
    claim_token_digest: str
    version: int
    attempts: int
    payload: dict[str, Any]


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def _normalize_db_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is not None:
        result = result.astimezone(timezone.utc).replace(tzinfo=None)
    return result


def _db_now(db) -> datetime:
    return _normalize_db_time(
        db.execute(select(func.current_timestamp())).scalar_one()
    )


def _begin_write(db) -> None:
    if db.get_bind().dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _resolve_account(db, owner: object, *, create: bool = True) -> Account:
    raw = str(owner or "").strip()
    if not raw:
        raise EmailRuntimeAuthorityError("Email runtime state requires an owner")
    account = db.query(Account).filter(
        Account.id == raw,
        Account.status == "active",
    ).one_or_none()
    if account is None:
        account = ensure_account(db, raw) if create else find_account(db, raw)
    if account is None or account.status != "active":
        raise EmailRuntimeAuthorityError("Email owner is not an active account")
    return account


def _account_key(value: object) -> str:
    normalized = str(value or "default").strip() or "default"
    if len(normalized) > 255:
        raise EmailRuntimeAuthorityError("Email account key is too long")
    return normalized


def _resolve_email_account(
    db,
    *,
    owner: Account,
    email_account_id: object = None,
) -> EmailAccount:
    requested = str(email_account_id or "").strip()
    query = db.query(EmailAccount)
    if requested:
        row = query.filter(EmailAccount.id == requested).one_or_none()
    else:
        row = query.filter(
            EmailAccount.owner.in_((owner.username, owner.id)),
            EmailAccount.enabled.is_(True),
        ).order_by(
            EmailAccount.is_default.desc(), EmailAccount.created_at.asc(),
        ).first()
    if row is None:
        raise EmailRuntimeAuthorityError("Email account is unavailable")
    stored_owner = str(row.owner or "").strip().lower()
    if stored_owner not in {owner.username.lower(), owner.id.lower()}:
        raise EmailRuntimeAuthorityError("Email account does not belong to owner")
    return row


def _message_digest(owner_id: str, account_key: str, message_id: object) -> str:
    value = str(message_id or "").strip()
    if not value:
        raise EmailRuntimeAuthorityError("Email message identity is required")
    return private_digest(
        f"email-message-v1:{owner_id}:{account_key}", value,
    )


def _location_digest(
    owner_id: str,
    account_key: str,
    folder: object,
    uid: object,
    *,
    message_id: object = None,
) -> str:
    uid_value = str(uid or "").strip()
    if not uid_value:
        message_value = str(message_id or "").strip()
        if not message_value:
            raise EmailRuntimeAuthorityError("Email location identity is required")
        uid_value = f"message:{message_value}"
    material = f"{str(folder or 'INBOX').strip()}\0{uid_value}"
    return private_digest(
        f"email-location-v1:{owner_id}:{account_key}", material,
    )


def _retry_at(clock: datetime, attempts: int) -> datetime:
    seconds = min(6 * 60 * 60, 30 * (2 ** max(0, attempts - 1)))
    return clock + timedelta(seconds=seconds)


def _claim_digest(kind: str, row_id: str, token: str) -> str:
    return private_digest(f"{kind}-claim-v1:{row_id}", token)


def get_email_automation_rules(
    owner: object,
    *,
    legacy_settings: Mapping[str, Any] | None = None,
) -> dict[str, bool]:
    """Read owner-scoped automation flags, importing one legacy snapshot once."""

    db = SessionLocal()
    try:
        _begin_write(db)
        account = _resolve_account(db, owner)
        row = db.query(EmailAutomationRule).filter(
            EmailAutomationRule.owner_id == account.id,
            EmailAutomationRule.account_key == "*",
        ).one_or_none()
        if row is None:
            values = {
                key: bool((legacy_settings or {}).get(key, False))
                for key in AUTOMATION_RULE_KEYS
            }
            row = EmailAutomationRule(
                id=str(uuid.uuid4()), owner_id=account.id,
                account_key="*", rules=values, version=1,
            )
            db.add(row)
            marker_payload = {
                "keys": list(AUTOMATION_RULE_KEYS),
                "source": "legacy_settings_snapshot",
            }
            source_sha256 = private_digest(
                f"email-runtime-import-v1:{account.id}",
                _canonical_json(values),
            )
            db.add(EmailRuntimeImportRun(
                id=str(uuid.uuid4()), owner_id=account.id,
                source_kind="settings_email_automation",
                source_sha256=source_sha256, state="completed",
                details=marker_payload, completed_at=_db_now(db),
            ))
        db.commit()
        stored = row.rules if isinstance(row.rules, dict) else {}
        return {key: bool(stored.get(key, False)) for key in AUTOMATION_RULE_KEYS}
    except IntegrityError:
        db.rollback()
        return get_email_automation_rules(owner, legacy_settings=None)
    finally:
        db.close()


def set_email_automation_rules(
    owner: object,
    updates: Mapping[str, Any],
    *,
    legacy_settings: Mapping[str, Any] | None = None,
) -> dict[str, bool]:
    unknown = set(updates) - set(AUTOMATION_RULE_KEYS)
    if unknown:
        raise EmailRuntimeAuthorityError("Unsupported email automation rule")
    get_email_automation_rules(owner, legacy_settings=legacy_settings)
    db = SessionLocal()
    try:
        _begin_write(db)
        account = _resolve_account(db, owner)
        row = db.query(EmailAutomationRule).filter(
            EmailAutomationRule.owner_id == account.id,
            EmailAutomationRule.account_key == "*",
        ).with_for_update().one()
        values = {
            key: bool((row.rules or {}).get(key, False))
            for key in AUTOMATION_RULE_KEYS
        }
        values.update({key: bool(value) for key, value in updates.items()})
        row.rules = values
        row.version += 1
        db.commit()
        return values
    finally:
        db.close()


def upsert_email_tag_state(
    *,
    owner: object,
    account_id: object,
    message_id: object,
    uid: object,
    folder: object,
    tags: Iterable[object] | None = None,
    spam_verdict: bool | None = None,
    spam_reason: object = None,
    moved_to: object = None,
    model_used: object = None,
    subject: object = None,
    sender: object = None,
    merge_tags: bool = False,
) -> dict[str, Any]:
    """Create/update one canonical tag row without exposing private indexes."""

    db = SessionLocal()
    try:
        _begin_write(db)
        account = _resolve_account(db, owner)
        key = _account_key(account_id)
        msg_digest = _message_digest(account.id, key, message_id)
        loc_digest = _location_digest(
            account.id, key, folder, uid, message_id=message_id,
        )
        rows = db.query(EmailTagState).filter(
            EmailTagState.owner_id == account.id,
            EmailTagState.account_key == key,
            or_(
                EmailTagState.message_digest == msg_digest,
                EmailTagState.location_digest == loc_digest,
            ),
        ).with_for_update().all()
        message_row = next(
            (value for value in rows if value.message_digest == msg_digest),
            None,
        )
        location_row = next(
            (value for value in rows if value.location_digest == loc_digest),
            None,
        )
        row = message_row or location_row
        # A message can move while an IMAP UID is later reused. Consolidate
        # the stale location occupant before updating either unique identity.
        if message_row is not None and location_row not in (None, message_row):
            db.delete(location_row)
            db.flush()
        if row is None:
            row = EmailTagState(
                id=str(uuid.uuid4()), owner_id=account.id, account_key=key,
                message_digest=msg_digest, location_digest=loc_digest,
                payload={}, version=1,
            )
            db.add(row)
        current = dict(row.payload or {})
        normalized_tags = [
            str(tag).strip().lower().replace("_", "-")
            for tag in (tags or []) if str(tag or "").strip()
        ]
        normalized_tags = list(dict.fromkeys(normalized_tags))
        if merge_tags:
            normalized_tags = list(dict.fromkeys([
                *[str(tag) for tag in current.get("tags", [])],
                *normalized_tags,
            ]))
        payload = {
            **current,
            "message_id": str(message_id or "").strip(),
            "uid": str(uid or "").strip(),
            "folder": str(folder or "INBOX").strip() or "INBOX",
            "tags": normalized_tags,
            "spam_verdict": (
                bool(spam_verdict) if spam_verdict is not None
                else bool(current.get("spam_verdict", False))
            ),
            "spam_reason": (
                str(spam_reason or "")[:200] if spam_reason is not None
                else str(current.get("spam_reason") or "")[:200]
            ),
            "moved_to": (
                str(moved_to or "")[:255] if moved_to is not None
                else str(current.get("moved_to") or "")[:255]
            ),
            "model_used": (
                str(model_used or "")[:255] if model_used is not None
                else str(current.get("model_used") or "")[:255]
            ),
            "subject": (
                str(subject or "") if subject is not None
                else str(current.get("subject") or "")
            ),
            "sender": (
                str(sender or "") if sender is not None
                else str(current.get("sender") or "")
            ),
        }
        row.message_digest = msg_digest
        row.location_digest = loc_digest
        row.payload = payload
        if rows:
            row.version += 1
        db.commit()
        return payload
    finally:
        db.close()


def list_email_tag_states(
    *,
    owner: object,
    account_id: object = None,
    folder: object = None,
    uids: Iterable[object] | None = None,
    message_ids: Iterable[object] | None = None,
) -> list[dict[str, Any]]:
    db = SessionLocal()
    try:
        account = _resolve_account(db, owner)
        query = db.query(EmailTagState).filter(
            EmailTagState.owner_id == account.id,
        )
        if account_id not in (None, ""):
            query = query.filter(
                EmailTagState.account_key == _account_key(account_id),
            )
        uid_set = {str(value or "").strip() for value in (uids or [])}
        mid_set = {str(value or "").strip() for value in (message_ids or [])}
        folder_value = str(folder or "").strip()
        result: list[dict[str, Any]] = []
        for row in query.order_by(EmailTagState.updated_at.desc()).limit(
            MAX_TAG_ROWS
        ):
            payload = dict(row.payload or {})
            if folder_value and str(payload.get("folder") or "") != folder_value:
                continue
            if uid_set or mid_set:
                if (
                    str(payload.get("uid") or "") not in uid_set
                    and str(payload.get("message_id") or "").strip() not in mid_set
                ):
                    continue
            payload["account_id"] = row.account_key
            payload["version"] = row.version
            result.append(payload)
        return result
    finally:
        db.close()


def clear_done_email_tags(
    *, owner: object, account_id: object, folder: object, uid: object,
    done_tags: Iterable[str],
) -> None:
    rows = list_email_tag_states(
        owner=owner, account_id=account_id, folder=folder, uids=[uid],
    )
    done = {str(tag).strip().lower().replace("_", "-") for tag in done_tags}
    for row in rows:
        kept = [tag for tag in row.get("tags", []) if str(tag) not in done]
        if kept != row.get("tags", []):
            upsert_email_tag_state(
                owner=owner, account_id=row.get("account_id") or account_id,
                message_id=row.get("message_id"), uid=row.get("uid"),
                folder=row.get("folder"), tags=kept,
                spam_verdict=bool(row.get("spam_verdict")),
                spam_reason=row.get("spam_reason"), moved_to=row.get("moved_to"),
                model_used=row.get("model_used"), subject=row.get("subject"),
                sender=row.get("sender"),
            )


def unflag_email_spam(
    *, owner: object, uid: object, account_id: object = None,
    folder: object = None,
) -> int:
    rows = list_email_tag_states(
        owner=owner, account_id=account_id, folder=folder, uids=[uid],
    )
    for row in rows:
        upsert_email_tag_state(
            owner=owner, account_id=row.get("account_id"),
            message_id=row.get("message_id"), uid=row.get("uid"),
            folder=row.get("folder"), tags=row.get("tags", []),
            spam_verdict=False, spam_reason="", moved_to=row.get("moved_to"),
            model_used=row.get("model_used"), subject=row.get("subject"),
            sender=row.get("sender"),
        )
    return len(rows)


def clear_email_runtime_results(
    *,
    owner: object,
    operations: Iterable[str] = (),
    clear_tags: bool = False,
) -> dict[str, int]:
    """Clear user-requested derived results without touching active claims."""

    requested = {str(value) for value in operations}
    if requested - AUTOMATION_OPERATIONS:
        raise EmailRuntimeAuthorityError("Unsupported email automation operation")
    db = SessionLocal()
    try:
        _begin_write(db)
        account = _resolve_account(db, owner)
        result = {"tags": 0, "automation": 0}
        if clear_tags:
            result["tags"] = db.query(EmailTagState).filter(
                EmailTagState.owner_id == account.id,
            ).delete(synchronize_session=False)
        if requested:
            result["automation"] = db.query(EmailAutomationRun).filter(
                EmailAutomationRun.owner_id == account.id,
                EmailAutomationRun.operation.in_(tuple(requested)),
                EmailAutomationRun.state != "claimed",
            ).delete(synchronize_session=False)
        db.commit()
        return {key: int(value or 0) for key, value in result.items()}
    finally:
        db.close()


def _payload_digest(owner_id: str, payload: Mapping[str, Any]) -> str:
    return private_digest(
        f"email-scheduled-payload-v1:{owner_id}", _canonical_json(payload),
    )


def create_scheduled_delivery(
    *,
    owner: object,
    email_account_id: object,
    scheduled_for: datetime,
    payload: Mapping[str, Any],
    delivery_id: object = None,
    idempotency_key: object = None,
    initial_state: str = "queued",
) -> EmailScheduledDelivery:
    if initial_state not in {"queued", "retry", "delivered", "failed", "cancelled"}:
        raise EmailRuntimeAuthorityError("Invalid scheduled delivery state")
    scheduled = _normalize_db_time(scheduled_for)
    content = dict(payload)
    db = SessionLocal()
    try:
        _begin_write(db)
        account = _resolve_account(db, owner)
        email_account = _resolve_email_account(
            db, owner=account, email_account_id=email_account_id,
        )
        row_id = str(delivery_id or uuid.uuid4())
        idem = str(idempotency_key or f"manual:{row_id}")[:128]
        existing = db.query(EmailScheduledDelivery).filter(
            EmailScheduledDelivery.owner_id == account.id,
            EmailScheduledDelivery.idempotency_key == idem,
        ).one_or_none()
        if existing is not None:
            if existing.payload_sha256 != _payload_digest(account.id, content):
                raise EmailRuntimeAuthorityError(
                    "Scheduled delivery idempotency key changed payload"
                )
            return existing
        clock = _db_now(db)
        row = EmailScheduledDelivery(
            id=row_id, owner_id=account.id,
            email_account_id=email_account.id, idempotency_key=idem,
            payload=content, payload_sha256=_payload_digest(account.id, content),
            scheduled_for=scheduled, state=initial_state, attempts=0, version=1,
            completed_at=(clock if initial_state == "delivered" else None),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def list_scheduled_deliveries(owner: object) -> list[dict[str, Any]]:
    db = SessionLocal()
    try:
        account = _resolve_account(db, owner)
        rows = db.query(EmailScheduledDelivery).filter(
            EmailScheduledDelivery.owner_id == account.id,
            EmailScheduledDelivery.state.in_(("queued", "retry", "failed")),
        ).order_by(EmailScheduledDelivery.scheduled_for.asc()).all()
        result = []
        for row in rows:
            payload = dict(row.payload or {})
            result.append({
                "id": row.id,
                "to": payload.get("to", ""),
                "cc": payload.get("cc"),
                "subject": payload.get("subject", ""),
                "send_at": row.scheduled_for.isoformat(),
                "created_at": row.created_at.isoformat(),
                "status": {
                    "queued": "pending", "retry": "pending",
                }.get(row.state, row.state),
                "error": row.last_error_code,
            })
        return result
    finally:
        db.close()


def cancel_scheduled_delivery(*, owner: object, delivery_id: object) -> bool:
    db = SessionLocal()
    try:
        _begin_write(db)
        account = _resolve_account(db, owner)
        row = db.query(EmailScheduledDelivery).filter(
            EmailScheduledDelivery.id == str(delivery_id or ""),
            EmailScheduledDelivery.owner_id == account.id,
        ).with_for_update().one_or_none()
        if row is None or row.state not in {"queued", "retry"}:
            db.rollback()
            return False
        row.state = "cancelled"
        row.next_attempt_at = None
        row.version += 1
        db.commit()
        return True
    finally:
        db.close()


def claim_due_scheduled_delivery(
    *, lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> ScheduledDeliveryClaim | None:
    db = SessionLocal()
    try:
        _begin_write(db)
        clock = _db_now(db)
        row = db.query(EmailScheduledDelivery).filter(
            EmailScheduledDelivery.scheduled_for <= clock,
            or_(
                and_(
                    EmailScheduledDelivery.state.in_(("queued", "retry")),
                    or_(
                        EmailScheduledDelivery.next_attempt_at.is_(None),
                        EmailScheduledDelivery.next_attempt_at <= clock,
                    ),
                ),
                and_(
                    EmailScheduledDelivery.state == "claimed",
                    EmailScheduledDelivery.lease_expires_at <= clock,
                ),
            ),
        ).order_by(
            EmailScheduledDelivery.scheduled_for.asc(),
            EmailScheduledDelivery.created_at.asc(),
            EmailScheduledDelivery.id.asc(),
        ).with_for_update(skip_locked=True).first()
        if row is None:
            db.rollback()
            return None
        token = uuid.uuid4().hex
        digest = _claim_digest("email-scheduled", row.id, token)
        row.state = "claimed"
        row.claim_token_digest = digest
        row.claimed_at = clock
        row.lease_expires_at = clock + timedelta(seconds=max(30, lease_seconds))
        row.next_attempt_at = None
        row.attempts += 1
        row.version += 1
        version = row.version
        payload = dict(row.payload or {})
        owner = db.get(Account, row.owner_id)
        db.commit()
        return ScheduledDeliveryClaim(
            row_id=row.id, owner_id=row.owner_id,
            owner_username=owner.username if owner else "",
            email_account_id=row.email_account_id,
            claim_token_digest=digest, version=version,
            attempts=row.attempts, payload=payload,
        )
    finally:
        db.close()


def complete_scheduled_delivery(
    claim: ScheduledDeliveryClaim,
    *,
    provider_message_id: object = None,
) -> bool:
    db = SessionLocal()
    try:
        _begin_write(db)
        clock = _db_now(db)
        updated = db.query(EmailScheduledDelivery).filter(
            EmailScheduledDelivery.id == claim.row_id,
            EmailScheduledDelivery.owner_id == claim.owner_id,
            EmailScheduledDelivery.state == "claimed",
            EmailScheduledDelivery.claim_token_digest == claim.claim_token_digest,
            EmailScheduledDelivery.version == claim.version,
            EmailScheduledDelivery.lease_expires_at > clock,
        ).update({
            EmailScheduledDelivery.state: "delivered",
            EmailScheduledDelivery.claim_token_digest: None,
            EmailScheduledDelivery.claimed_at: None,
            EmailScheduledDelivery.lease_expires_at: None,
            EmailScheduledDelivery.completed_at: clock,
            EmailScheduledDelivery.last_error_code: None,
            EmailScheduledDelivery.provider_message_id: (
                str(provider_message_id) if provider_message_id else None
            ),
            EmailScheduledDelivery.version: EmailScheduledDelivery.version + 1,
        }, synchronize_session=False)
        db.commit()
        return updated == 1
    finally:
        db.close()


def fail_scheduled_delivery(
    claim: ScheduledDeliveryClaim,
    *,
    error_code: str = "smtp_failed",
) -> bool:
    if error_code not in SAFE_SCHEDULE_ERRORS:
        error_code = "smtp_failed"
    db = SessionLocal()
    try:
        _begin_write(db)
        clock = _db_now(db)
        terminal = claim.attempts >= MAX_ATTEMPTS
        updated = db.query(EmailScheduledDelivery).filter(
            EmailScheduledDelivery.id == claim.row_id,
            EmailScheduledDelivery.owner_id == claim.owner_id,
            EmailScheduledDelivery.state == "claimed",
            EmailScheduledDelivery.claim_token_digest == claim.claim_token_digest,
            EmailScheduledDelivery.version == claim.version,
        ).update({
            EmailScheduledDelivery.state: "failed" if terminal else "retry",
            EmailScheduledDelivery.claim_token_digest: None,
            EmailScheduledDelivery.claimed_at: None,
            EmailScheduledDelivery.lease_expires_at: None,
            EmailScheduledDelivery.next_attempt_at: (
                None if terminal else _retry_at(clock, claim.attempts)
            ),
            EmailScheduledDelivery.last_error_code: error_code,
            EmailScheduledDelivery.version: EmailScheduledDelivery.version + 1,
        }, synchronize_session=False)
        db.commit()
        return updated == 1
    finally:
        db.close()


def claim_email_automation(
    *,
    owner: object,
    account_id: object,
    operation: str,
    message_id: object,
    payload: Mapping[str, Any] | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> AutomationClaim | None:
    if operation not in AUTOMATION_OPERATIONS:
        raise EmailRuntimeAuthorityError("Unsupported email automation operation")
    db = SessionLocal()
    try:
        _begin_write(db)
        account = _resolve_account(db, owner)
        key = _account_key(account_id)
        digest = _message_digest(account.id, key, message_id)
        row = db.query(EmailAutomationRun).filter(
            EmailAutomationRun.owner_id == account.id,
            EmailAutomationRun.account_key == key,
            EmailAutomationRun.operation == operation,
            EmailAutomationRun.message_digest == digest,
        ).with_for_update().one_or_none()
        clock = _db_now(db)
        if row is None:
            content = dict(payload or {})
            content.setdefault("message_id", str(message_id or "").strip())
            row = EmailAutomationRun(
                id=str(uuid.uuid4()), owner_id=account.id, account_key=key,
                operation=operation, message_digest=digest, payload=content,
                state="pending", attempts=0, version=1,
            )
            db.add(row)
            db.flush()
        elif row.state in {"completed", "failed"}:
            db.rollback()
            return None
        elif row.state == "claimed" and (
            row.lease_expires_at is None or row.lease_expires_at > clock
        ):
            db.rollback()
            return None
        elif row.next_attempt_at is not None and row.next_attempt_at > clock:
            db.rollback()
            return None
        token = uuid.uuid4().hex
        claim_digest = _claim_digest("email-automation", row.id, token)
        row.state = "claimed"
        row.claim_token_digest = claim_digest
        row.claimed_at = clock
        row.lease_expires_at = clock + timedelta(seconds=max(30, lease_seconds))
        row.next_attempt_at = None
        row.attempts += 1
        row.version += 1
        version = row.version
        attempts = row.attempts
        content = dict(row.payload or {})
        db.commit()
        return AutomationClaim(
            row_id=row.id, owner_id=account.id, owner_username=account.username,
            account_key=key, operation=operation, message_digest=digest,
            claim_token_digest=claim_digest, version=version,
            attempts=attempts, payload=content,
        )
    except IntegrityError:
        db.rollback()
        return None
    finally:
        db.close()


def complete_email_automation(
    claim: AutomationClaim,
    *,
    result: Mapping[str, Any] | None = None,
) -> bool:
    db = SessionLocal()
    try:
        _begin_write(db)
        clock = _db_now(db)
        row = db.query(EmailAutomationRun).filter(
            EmailAutomationRun.id == claim.row_id,
            EmailAutomationRun.owner_id == claim.owner_id,
            EmailAutomationRun.state == "claimed",
            EmailAutomationRun.claim_token_digest == claim.claim_token_digest,
            EmailAutomationRun.version == claim.version,
            EmailAutomationRun.lease_expires_at > clock,
        ).with_for_update().one_or_none()
        if row is None:
            db.rollback()
            return False
        payload = dict(row.payload or {})
        if result:
            payload["result"] = dict(result)
        row.payload = payload
        row.state = "completed"
        row.claim_token_digest = None
        row.claimed_at = None
        row.lease_expires_at = None
        row.next_attempt_at = None
        row.completed_at = clock
        row.last_error_code = None
        row.version += 1
        db.commit()
        return True
    finally:
        db.close()


def fail_email_automation(
    claim: AutomationClaim,
    *,
    error_code: str = "operation_failed",
) -> bool:
    if error_code not in SAFE_AUTOMATION_ERRORS:
        error_code = "operation_failed"
    db = SessionLocal()
    try:
        _begin_write(db)
        clock = _db_now(db)
        terminal = claim.attempts >= MAX_ATTEMPTS
        updated = db.query(EmailAutomationRun).filter(
            EmailAutomationRun.id == claim.row_id,
            EmailAutomationRun.owner_id == claim.owner_id,
            EmailAutomationRun.state == "claimed",
            EmailAutomationRun.claim_token_digest == claim.claim_token_digest,
            EmailAutomationRun.version == claim.version,
        ).update({
            EmailAutomationRun.state: "failed" if terminal else "retry",
            EmailAutomationRun.claim_token_digest: None,
            EmailAutomationRun.claimed_at: None,
            EmailAutomationRun.lease_expires_at: None,
            EmailAutomationRun.next_attempt_at: (
                None if terminal else _retry_at(clock, claim.attempts)
            ),
            EmailAutomationRun.last_error_code: error_code,
            EmailAutomationRun.version: EmailAutomationRun.version + 1,
        }, synchronize_session=False)
        db.commit()
        return updated == 1
    finally:
        db.close()


def completed_email_automation_results(
    *,
    owner: object,
    account_id: object = None,
    operation: str,
    message_ids: Iterable[object] | None = None,
) -> dict[str, dict[str, Any]]:
    if operation not in AUTOMATION_OPERATIONS:
        raise EmailRuntimeAuthorityError("Unsupported email automation operation")
    wanted = {str(value or "").strip() for value in (message_ids or [])}
    db = SessionLocal()
    try:
        account = _resolve_account(db, owner)
        query = db.query(EmailAutomationRun).filter(
            EmailAutomationRun.owner_id == account.id,
            EmailAutomationRun.operation == operation,
            EmailAutomationRun.state == "completed",
        )
        if account_id not in (None, ""):
            query = query.filter(
                EmailAutomationRun.account_key == _account_key(account_id),
            )
        result: dict[str, dict[str, Any]] = {}
        for row in query.order_by(EmailAutomationRun.updated_at.desc()).limit(
            MAX_TAG_ROWS
        ):
            payload = dict(row.payload or {})
            message_id = str(payload.get("message_id") or "").strip()
            if not message_id or (wanted and message_id not in wanted):
                continue
            result[message_id] = dict(payload.get("result") or {})
        return result
    finally:
        db.close()


def has_email_automation_history(
    *, owner: object, account_id: object, operation: str,
) -> bool:
    db = SessionLocal()
    try:
        account = _resolve_account(db, owner)
        return db.query(EmailAutomationRun.id).filter(
            EmailAutomationRun.owner_id == account.id,
            EmailAutomationRun.account_key == _account_key(account_id),
            EmailAutomationRun.operation == operation,
            EmailAutomationRun.state == "completed",
        ).first() is not None
    finally:
        db.close()


def _legacy_connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro", uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _source_digest(owner_id: str, kind: str, row: Mapping[str, Any]) -> str:
    return private_digest(
        f"email-runtime-import-v1:{owner_id}:{kind}", _canonical_json(row),
    )


def _mark_import(
    db,
    *,
    owner_id: str,
    source_kind: str,
    source_sha256: str,
    details: Mapping[str, Any],
) -> None:
    if db.query(EmailRuntimeImportRun.id).filter(
        EmailRuntimeImportRun.owner_id == owner_id,
        EmailRuntimeImportRun.source_kind == source_kind,
        EmailRuntimeImportRun.source_sha256 == source_sha256,
    ).first() is None:
        db.add(EmailRuntimeImportRun(
            id=str(uuid.uuid4()), owner_id=owner_id, source_kind=source_kind,
            source_sha256=source_sha256, state="completed",
            details=dict(details), completed_at=_db_now(db),
        ))


def _import_offset(db, *, owner_id: str, source_kind: str) -> int:
    return int(db.query(func.count(EmailRuntimeImportRun.id)).filter(
        EmailRuntimeImportRun.owner_id == owner_id,
        EmailRuntimeImportRun.source_kind == source_kind,
    ).scalar() or 0)


def import_legacy_email_runtime(
    *,
    owner: object,
    sidecar_path: str | Path,
    limit: int = MAX_IMPORT_BATCH,
) -> dict[str, int]:
    """Bounded, read-only, replay-safe import of non-cache sidecar state."""

    path = Path(sidecar_path)
    result = {"tags": 0, "schedules": 0, "automation": 0}
    try:
        info = path.lstat()
    except FileNotFoundError:
        return result
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > MAX_LEGACY_DATABASE_BYTES
    ):
        raise EmailRuntimeAuthorityError("Legacy email sidecar is unsafe")
    limit = max(1, min(int(limit), MAX_IMPORT_BATCH))
    db = SessionLocal()
    source = None
    try:
        _begin_write(db)
        account = _resolve_account(db, owner)
        source = _legacy_connect(path)
        tables = {
            str(row[0]) for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        owner_aliases = {account.username, account.id}
        email_accounts = db.query(EmailAccount).filter(
            EmailAccount.owner.in_(tuple(owner_aliases)),
        ).all()
        email_account_ids = {str(row.id) for row in email_accounts}
        default_email = next((row for row in email_accounts if row.is_default), None)
        if default_email is None and email_accounts:
            default_email = email_accounts[0]
        sole_active_owner = int(db.query(func.count(Account.id)).filter(
            Account.status == "active",
        ).scalar() or 0) == 1

        if "email_tags" in tables:
            tag_columns = {
                str(row[1]) for row in source.execute(
                    "PRAGMA table_info(email_tags)"
                ).fetchall()
            }
            tag_owner_values = tuple(sorted(owner_aliases))
            tag_account_values = tuple(sorted(email_account_ids))
            tag_predicates = []
            tag_params: list[Any] = []
            if "owner" in tag_columns and tag_owner_values:
                tag_predicates.append(
                    "owner IN (" + ",".join("?" * len(tag_owner_values)) + ")"
                )
                tag_params.extend(tag_owner_values)
                if sole_active_owner:
                    tag_predicates.append("owner = '' OR owner IS NULL")
            if "account_id" in tag_columns and tag_account_values:
                tag_predicates.append(
                    "account_id IN (" + ",".join("?" * len(tag_account_values)) + ")"
                )
                tag_params.extend(tag_account_values)
                if sole_active_owner:
                    tag_predicates.append("account_id = '' OR account_id IS NULL")
            tag_where = " OR ".join(tag_predicates) or (
                "1=1" if sole_active_owner else "0=1"
            )
            selected_tag_columns = [
                (
                    name if name in tag_columns
                    else f"{default} AS {name}"
                )
                for name, default in (
                    ("message_id", "''"), ("owner", "''"),
                    ("account_id", "''"), ("uid", "''"),
                    ("folder", "'INBOX'"), ("subject", "''"),
                    ("sender", "''"), ("tags", "'[]'"),
                    ("spam_verdict", "0"), ("spam_reason", "''"),
                    ("moved_to", "''"), ("model_used", "''"),
                )
            ]
            tag_offset = _import_offset(
                db, owner_id=account.id, source_kind="email_tags",
            )
            rows = source.execute(
                f"SELECT {', '.join(selected_tag_columns)} "
                f"FROM email_tags WHERE {tag_where} ORDER BY rowid LIMIT ? OFFSET ?",
                (*tag_params, limit, tag_offset),
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                row_owner = str(row.get("owner") or "").strip()
                row_account = str(row.get("account_id") or "").strip()
                if (
                    row_owner not in owner_aliases
                    and row_account not in email_account_ids
                    and not (sole_active_owner and not row_owner and not row_account)
                ):
                    continue
                key = row_account or (str(default_email.id) if default_email else "default")
                digest = _source_digest(account.id, "email_tags", row)
                if db.query(EmailRuntimeImportRun.id).filter(
                    EmailRuntimeImportRun.owner_id == account.id,
                    EmailRuntimeImportRun.source_kind == "email_tags",
                    EmailRuntimeImportRun.source_sha256 == digest,
                ).first():
                    continue
                try:
                    tags = json.loads(str(row.get("tags") or "[]"))
                except json.JSONDecodeError:
                    tags = []
                payload = {
                    "message_id": str(row.get("message_id") or "").strip(),
                    "uid": str(row.get("uid") or "").strip(),
                    "folder": str(row.get("folder") or "INBOX"),
                    "subject": str(row.get("subject") or ""),
                    "sender": str(row.get("sender") or ""),
                    "tags": tags if isinstance(tags, list) else [],
                    "spam_verdict": bool(row.get("spam_verdict")),
                    "spam_reason": str(row.get("spam_reason") or "")[:200],
                    "moved_to": str(row.get("moved_to") or "")[:255],
                    "model_used": str(row.get("model_used") or "")[:255],
                }
                if not payload["message_id"]:
                    _mark_import(
                        db, owner_id=account.id, source_kind="email_tags",
                        source_sha256=digest,
                        details={"imported": False, "reason": "missing_message_id"},
                    )
                    continue
                msg_digest = _message_digest(account.id, key, payload["message_id"])
                loc_digest = _location_digest(
                    account.id, key, payload["folder"], payload["uid"],
                    message_id=payload["message_id"],
                )
                existing = db.query(EmailTagState).filter(
                    EmailTagState.owner_id == account.id,
                    EmailTagState.account_key == key,
                    or_(
                        EmailTagState.message_digest == msg_digest,
                        EmailTagState.location_digest == loc_digest,
                    ),
                ).one_or_none()
                if existing is None:
                    db.add(EmailTagState(
                        id=str(uuid.uuid4()), owner_id=account.id,
                        account_key=key, message_digest=msg_digest,
                        location_digest=loc_digest, payload=payload, version=1,
                    ))
                    result["tags"] += 1
                automation_existing = db.query(EmailAutomationRun).filter(
                    EmailAutomationRun.owner_id == account.id,
                    EmailAutomationRun.account_key == key,
                    EmailAutomationRun.operation == "classify",
                    EmailAutomationRun.message_digest == msg_digest,
                ).one_or_none()
                if automation_existing is None:
                    db.add(EmailAutomationRun(
                        id=str(uuid.uuid4()), owner_id=account.id,
                        account_key=key, operation="classify",
                        message_digest=msg_digest,
                        payload={
                            "message_id": payload["message_id"],
                            "folder": payload["folder"],
                            "uid": payload["uid"],
                            "result": {"imported": True},
                        },
                        state="completed", attempts=1,
                        completed_at=_db_now(db), version=1,
                    ))
                    result["automation"] += 1
                _mark_import(
                    db, owner_id=account.id, source_kind="email_tags",
                    source_sha256=digest,
                    details={"account_key": key, "imported": existing is None},
                )

        for table, operation in (
            ("email_summaries", "summary"),
            ("email_ai_replies", "reply"),
        ):
            if table not in tables:
                continue
            cache_columns = {
                str(row[1]) for row in source.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            selected_cache_columns = [
                name if name in cache_columns else f"{default} AS {name}"
                for name, default in (
                    ("message_id", "''"), ("owner", "''"),
                    ("uid", "''"), ("folder", "'INBOX'"),
                )
            ]
            source_offset = _import_offset(
                db, owner_id=account.id, source_kind=table,
            )
            if "owner" in cache_columns:
                placeholders = ",".join("?" * len(owner_aliases))
                cache_owner_predicate = f"owner IN ({placeholders})"
                if sole_active_owner:
                    cache_owner_predicate += " OR owner = '' OR owner IS NULL"
                cache_owner_params = tuple(sorted(owner_aliases))
            else:
                cache_owner_predicate = "1=1" if sole_active_owner else "0=1"
                cache_owner_params = ()
            rows = source.execute(
                f"SELECT {', '.join(selected_cache_columns)} FROM {table} "
                f"WHERE {cache_owner_predicate} ORDER BY rowid LIMIT ? OFFSET ?",
                (*cache_owner_params, limit, source_offset),
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                row_owner = str(row.get("owner") or "").strip()
                if row_owner not in owner_aliases and not (
                    sole_active_owner and not row_owner
                ):
                    continue
                message_id = str(row.get("message_id") or "").strip()
                if not message_id:
                    invalid_digest = _source_digest(account.id, table, row)
                    _mark_import(
                        db, owner_id=account.id, source_kind=table,
                        source_sha256=invalid_digest,
                        details={"imported": False, "reason": "missing_message_id"},
                    )
                    continue
                key = str(default_email.id) if default_email else "default"
                source_digest = _source_digest(account.id, table, row)
                if db.query(EmailRuntimeImportRun.id).filter(
                    EmailRuntimeImportRun.owner_id == account.id,
                    EmailRuntimeImportRun.source_kind == table,
                    EmailRuntimeImportRun.source_sha256 == source_digest,
                ).first():
                    continue
                message_digest = _message_digest(account.id, key, message_id)
                existing = db.query(EmailAutomationRun).filter(
                    EmailAutomationRun.owner_id == account.id,
                    EmailAutomationRun.account_key == key,
                    EmailAutomationRun.operation == operation,
                    EmailAutomationRun.message_digest == message_digest,
                ).one_or_none()
                if existing is None:
                    db.add(EmailAutomationRun(
                        id=str(uuid.uuid4()), owner_id=account.id,
                        account_key=key, operation=operation,
                        message_digest=message_digest,
                        payload={
                            "message_id": message_id,
                            "folder": str(row.get("folder") or "INBOX"),
                            "uid": str(row.get("uid") or ""),
                            "result": {"imported": True},
                        },
                        state="completed", attempts=1,
                        completed_at=_db_now(db), version=1,
                    ))
                    result["automation"] += 1
                _mark_import(
                    db, owner_id=account.id, source_kind=table,
                    source_sha256=source_digest,
                    details={"imported": existing is None, "operation": operation},
                )

        if "scheduled_emails" in tables and default_email is not None:
            columns = {
                str(row[1]) for row in source.execute(
                    "PRAGMA table_info(scheduled_emails)"
                ).fetchall()
            }
            selected = [
                name if name in columns else f"{default} AS {name}"
                for name, default in (
                    ("id", "''"), ("to_addr", "''"), ("cc", "''"),
                    ("bcc", "''"), ("subject", "''"), ("body", "''"),
                    ("in_reply_to", "''"), ("references_hdr", "''"),
                    ("attachments", "'[]'"), ("send_at", "''"),
                    ("created_at", "''"), ("status", "'pending'"),
                    ("error", "''"), ("owner", "''"),
                    ("account_id", "''"),
                    ("odysseus_kind", "'scheduled'"),
                )
            ]
            schedule_predicates = []
            schedule_params: list[Any] = []
            if "owner" in columns:
                values = tuple(sorted(owner_aliases))
                schedule_predicates.append(
                    "owner IN (" + ",".join("?" * len(values)) + ")"
                )
                schedule_params.extend(values)
                if sole_active_owner:
                    schedule_predicates.append("owner = '' OR owner IS NULL")
            if "account_id" in columns and email_account_ids:
                values = tuple(sorted(email_account_ids))
                schedule_predicates.append(
                    "account_id IN (" + ",".join("?" * len(values)) + ")"
                )
                schedule_params.extend(values)
                if sole_active_owner:
                    schedule_predicates.append(
                        "account_id = '' OR account_id IS NULL"
                    )
            schedule_where = " OR ".join(schedule_predicates) or (
                "1=1" if sole_active_owner else "0=1"
            )
            schedule_offset = _import_offset(
                db, owner_id=account.id, source_kind="scheduled_emails",
            )
            rows = source.execute(
                f"SELECT {', '.join(selected)} FROM scheduled_emails "
                f"WHERE {schedule_where} ORDER BY rowid LIMIT ? OFFSET ?",
                (*schedule_params, limit, schedule_offset),
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                row_owner = str(row.get("owner") or "").strip()
                row_account = str(row.get("account_id") or "").strip()
                if (
                    row_owner not in owner_aliases
                    and row_account not in email_account_ids
                    and not (sole_active_owner and not row_owner and not row_account)
                ):
                    continue
                if (
                    str(row.get("odysseus_kind") or "scheduled") == "agent_draft"
                    or str(row.get("status") or "") == "agent_draft"
                ):
                    skipped_digest = _source_digest(
                        account.id, "scheduled_emails", row,
                    )
                    _mark_import(
                        db, owner_id=account.id,
                        source_kind="scheduled_emails",
                        source_sha256=skipped_digest,
                        details={
                            "imported": False,
                            "reason": "level5_agent_draft_authority",
                        },
                    )
                    continue
                digest = _source_digest(account.id, "scheduled_emails", row)
                if db.query(EmailRuntimeImportRun.id).filter(
                    EmailRuntimeImportRun.owner_id == account.id,
                    EmailRuntimeImportRun.source_kind == "scheduled_emails",
                    EmailRuntimeImportRun.source_sha256 == digest,
                ).first():
                    continue
                email_account = next(
                    (value for value in email_accounts if str(value.id) == row_account),
                    default_email,
                )
                try:
                    scheduled_for = _normalize_db_time(row.get("send_at"))
                except (TypeError, ValueError):
                    _mark_import(
                        db, owner_id=account.id, source_kind="scheduled_emails",
                        source_sha256=digest,
                        details={"imported": False, "reason": "invalid_schedule"},
                    )
                    continue
                try:
                    attachments = json.loads(str(row.get("attachments") or "[]"))
                except json.JSONDecodeError:
                    attachments = []
                payload = {
                    "to": str(row.get("to_addr") or ""),
                    "cc": str(row.get("cc") or ""),
                    "bcc": str(row.get("bcc") or ""),
                    "subject": str(row.get("subject") or ""),
                    "body": str(row.get("body") or ""),
                    "in_reply_to": str(row.get("in_reply_to") or ""),
                    "references": str(row.get("references_hdr") or ""),
                    "attachments": attachments if isinstance(attachments, list) else [],
                    "odysseus_kind": str(row.get("odysseus_kind") or "scheduled"),
                }
                state = {
                    "pending": "queued", "sent": "delivered",
                    "failed": "failed", "cancelled": "cancelled",
                }.get(str(row.get("status") or "pending"), "cancelled")
                sid = str(row.get("id") or uuid.uuid4())
                existing = db.query(EmailScheduledDelivery).filter(
                    EmailScheduledDelivery.owner_id == account.id,
                    EmailScheduledDelivery.idempotency_key == f"legacy:{sid}"[:128],
                ).one_or_none()
                if existing is None:
                    clock = _db_now(db)
                    db.add(EmailScheduledDelivery(
                        id=sid, owner_id=account.id,
                        email_account_id=str(email_account.id),
                        idempotency_key=f"legacy:{sid}"[:128], payload=payload,
                        payload_sha256=_payload_digest(account.id, payload),
                        scheduled_for=scheduled_for, state=state, attempts=0,
                        completed_at=(clock if state == "delivered" else None),
                        last_error_code=("smtp_failed" if state == "failed" else None),
                        version=1,
                    ))
                    result["schedules"] += 1
                _mark_import(
                    db, owner_id=account.id, source_kind="scheduled_emails",
                    source_sha256=digest,
                    details={"imported": existing is None, "state": state},
                )

        for table, operation, message_column in (
            ("email_calendar_extractions", "calendar", "message_id"),
            ("email_event_seen", "email_received", "message_key"),
        ):
            if table not in tables:
                continue
            columns = {
                str(row[1]) for row in source.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }
            if "owner" not in columns and not sole_active_owner:
                continue
            automation_offset = _import_offset(
                db, owner_id=account.id, source_kind=table,
            )
            placeholders = ",".join("?" * len(owner_aliases))
            owner_predicate = (
                f"owner IN ({placeholders})"
                + (" OR owner = '' OR owner IS NULL" if sole_active_owner else "")
                if "owner" in columns else "1=1"
            )
            owner_params = tuple(sorted(owner_aliases)) if "owner" in columns else ()
            rows = source.execute(
                f"SELECT * FROM {table} WHERE {owner_predicate} "
                "ORDER BY rowid LIMIT ? OFFSET ?",
                (*owner_params, limit, automation_offset),
            ).fetchall()
            for raw in rows:
                row = dict(raw)
                row_owner = str(row.get("owner") or "").strip()
                if row_owner not in owner_aliases and not (
                    sole_active_owner and not row_owner
                ):
                    continue
                message_id = str(row.get(message_column) or "").strip()
                if not message_id:
                    invalid_digest = _source_digest(account.id, table, row)
                    _mark_import(
                        db, owner_id=account.id, source_kind=table,
                        source_sha256=invalid_digest,
                        details={"imported": False, "reason": "missing_message_id"},
                    )
                    continue
                key = _account_key(
                    row.get("account_key") or row.get("account_id") or
                    (str(default_email.id) if default_email else "default")
                )
                digest = _source_digest(account.id, table, row)
                if db.query(EmailRuntimeImportRun.id).filter(
                    EmailRuntimeImportRun.owner_id == account.id,
                    EmailRuntimeImportRun.source_kind == table,
                    EmailRuntimeImportRun.source_sha256 == digest,
                ).first():
                    continue
                message_digest = _message_digest(account.id, key, message_id)
                existing = db.query(EmailAutomationRun).filter(
                    EmailAutomationRun.owner_id == account.id,
                    EmailAutomationRun.account_key == key,
                    EmailAutomationRun.operation == operation,
                    EmailAutomationRun.message_digest == message_digest,
                ).one_or_none()
                if existing is None:
                    event_uids: list[str] = []
                    if operation == "calendar":
                        try:
                            parsed = json.loads(str(row.get("event_uids") or "[]"))
                            if isinstance(parsed, list):
                                event_uids = [str(value) for value in parsed]
                        except json.JSONDecodeError:
                            pass
                    result_payload: dict[str, Any] = {
                        "event_uids": event_uids,
                    }
                    if operation == "calendar" and "events_created" in columns:
                        try:
                            result_payload["events_created"] = int(
                                row.get("events_created") or 0
                            )
                        except (TypeError, ValueError):
                            result_payload["events_created"] = 0
                    db.add(EmailAutomationRun(
                        id=str(uuid.uuid4()), owner_id=account.id,
                        account_key=key, operation=operation,
                        message_digest=message_digest,
                        payload={
                            "message_id": message_id,
                            "folder": str(row.get("folder") or "INBOX"),
                            "result": result_payload,
                        },
                        state="completed", attempts=1,
                        completed_at=_db_now(db), version=1,
                    ))
                    result["automation"] += 1
                _mark_import(
                    db, owner_id=account.id, source_kind=table,
                    source_sha256=digest,
                    details={"imported": existing is None, "operation": operation},
                )
        db.commit()
        return result
    except (sqlite3.DatabaseError, OSError) as exc:
        db.rollback()
        raise EmailRuntimeAuthorityError(
            "Legacy email sidecar could not be imported safely"
        ) from exc
    finally:
        if source is not None:
            source.close()
        db.close()


def digest_is_valid(value: object) -> bool:
    return isinstance(value, str) and _HEX_64.fullmatch(value) is not None
