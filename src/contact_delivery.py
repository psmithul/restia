"""Durable CardDAV mutation delivery for owner-scoped contacts.

Contact rows are the local authority.  A CardDAV mutation is first committed
with an encrypted outbox row, then this module delivers exactly one operation
per database transaction.  HTTP preconditions make a replay after a crash
idempotent, while per-record ordering prevents later edits overtaking an
earlier create/update/delete.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, exists, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from core.database import (
    ContactDelivery,
    ContactRecord,
    ContactSource,
    SessionLocal,
    utcnow_naive,
)
from src import carddav_contacts as carddav
from src.audit_context import bind_service_audit_context
from src.life_core import append_action_audit
from src.secret_storage import private_digest


CLAIM_LEASE_SECONDS = 120
MAX_DELIVERY_ATTEMPTS = 20
logger = logging.getLogger(__name__)


def _eligible_delivery_filter(now):
    stale_before = now - timedelta(seconds=CLAIM_LEASE_SECONDS)
    return or_(
        ContactDelivery.state == "pending",
        and_(
            ContactDelivery.state == "retry",
            or_(
                ContactDelivery.next_attempt_at.is_(None),
                ContactDelivery.next_attempt_at <= now,
            ),
        ),
        and_(
            ContactDelivery.state == "processing",
            or_(
                ContactDelivery.claimed_at.is_(None),
                ContactDelivery.claimed_at <= stale_before,
            ),
        ),
    )


def _has_earlier_open_delivery():
    predecessor = aliased(ContactDelivery)
    return exists().where(and_(
        predecessor.owner_id == ContactDelivery.owner_id,
        predecessor.record_id == ContactDelivery.record_id,
        predecessor.state != "completed",
        predecessor.id != ContactDelivery.id,
        or_(
            predecessor.created_at < ContactDelivery.created_at,
            and_(
                predecessor.created_at == ContactDelivery.created_at,
                predecessor.id < ContactDelivery.id,
            ),
        ),
    ))


def _source_config(source: ContactSource) -> dict[str, str]:
    return {
        "url": str(source.base_url or ""),
        "username": str(source.username or ""),
        "password": str(source.password or ""),
    }


def enqueue_contact_delivery(
    db,
    *,
    owner_id: str,
    source: ContactSource,
    record: ContactRecord,
    operation: str,
    raw_vcard: str = "",
) -> ContactDelivery:
    """Add one immutable desired connector operation in the caller transaction."""

    normalized = str(operation or "").strip().lower()
    if source.kind != "carddav" or normalized not in {"create", "update", "delete"}:
        raise ValueError("A valid CardDAV contact delivery is required")
    source_config_version = int(source.config_version or 1)
    content_hash = hashlib.sha256(str(raw_vcard or "").encode("utf-8")).hexdigest()
    key = private_digest(
        "contact-delivery-v1",
        (
            f"{record.id}:{int(record.version or 1)}:"
            f"{source_config_version}:{normalized}:{content_hash}"
        ),
    )
    existing = db.query(ContactDelivery).filter(
        ContactDelivery.owner_id == owner_id,
        ContactDelivery.idempotency_key == key,
    ).first()
    if existing is not None:
        return existing
    delivery = ContactDelivery(
        id=str(uuid.uuid4()),
        owner_id=owner_id,
        source_id=source.id,
        record_id=record.id,
        operation=normalized,
        idempotency_key=key,
        payload={
            "uid": str(record.remote_uid or ""),
            "raw_vcard": str(raw_vcard or ""),
            "record_version": int(record.version or 1),
            "source_config_version": source_config_version,
        },
        state="pending",
        attempts=0,
        version=1,
    )
    try:
        with db.begin_nested():
            db.add(delivery)
            db.flush()
    except IntegrityError:
        delivery = db.query(ContactDelivery).filter(
            ContactDelivery.owner_id == owner_id,
            ContactDelivery.idempotency_key == key,
        ).one()
    return delivery


def open_delivery_record_ids(
    db, *, owner_id: str, source_id: str,
) -> set[str]:
    return {
        str(value)
        for (value,) in db.query(ContactDelivery.record_id).filter(
            ContactDelivery.owner_id == owner_id,
            ContactDelivery.source_id == source_id,
            ContactDelivery.state != "completed",
        ).all()
    }


def _claim_one(db, *, owner_id: str, source_id: str | None = None):
    now = utcnow_naive()
    eligible = _eligible_delivery_filter(now)
    has_earlier_open_delivery = _has_earlier_open_delivery()
    query = db.query(ContactDelivery).filter(
        ContactDelivery.owner_id == owner_id,
        eligible,
        ~has_earlier_open_delivery,
    )
    if source_id:
        query = query.filter(ContactDelivery.source_id == source_id)
    candidates = query.order_by(
        ContactDelivery.created_at.asc(), ContactDelivery.id.asc(),
    ).limit(100).all()
    for candidate in candidates:
        token = str(uuid.uuid4())
        updated = db.query(ContactDelivery).filter(
            ContactDelivery.id == candidate.id,
            ContactDelivery.owner_id == owner_id,
            ContactDelivery.version == int(candidate.version or 1),
            eligible,
        ).update(
            {
                ContactDelivery.state: "processing",
                ContactDelivery.claim_token: token,
                ContactDelivery.claimed_at: now,
                ContactDelivery.next_attempt_at: None,
                ContactDelivery.last_error_code: None,
                ContactDelivery.attempts: int(candidate.attempts or 0) + 1,
                ContactDelivery.version: int(candidate.version or 1) + 1,
                ContactDelivery.updated_at: now,
            },
            synchronize_session=False,
        )
        if updated == 1:
            db.flush()
            # The bulk CAS deliberately bypasses identity-map synchronization.
            # Expire the candidate so attempts/version/token reflect the row
            # that was actually claimed, not the pre-claim object.
            db.expire(candidate)
            return db.query(ContactDelivery).filter(
                ContactDelivery.id == candidate.id,
                ContactDelivery.claim_token == token,
            ).one()
    return None


def _load_delivery_snapshot(db, delivery: ContactDelivery) -> dict[str, Any]:
    """Read the exact connector attempt, returning only detached primitives."""

    record = db.query(ContactRecord).filter(
        ContactRecord.id == delivery.record_id,
        ContactRecord.owner_id == delivery.owner_id,
    ).one_or_none()
    source = db.query(ContactSource).filter(
        ContactSource.id == delivery.source_id,
        ContactSource.owner_id == delivery.owner_id,
    ).one_or_none()
    if record is None or source is None:
        raise RuntimeError("Contact delivery authority row is missing")
    payload = dict(delivery.payload or {})
    operation = str(delivery.operation)
    if int(payload.get("source_config_version") or 0) != int(
        source.config_version or 1
    ):
        raise carddav.CardDAVConflict(
            "CardDAV configuration changed before delivery"
        )
    return {
        "operation": operation,
        "config": _source_config(source),
        "uid": str(payload.get("uid") or record.remote_uid or ""),
        "raw_vcard": str(payload.get("raw_vcard") or ""),
        "href": str(record.remote_href or "") or None,
        "etag": str(record.remote_etag or "") or None,
    }


def _execute_delivery(snapshot: dict[str, Any]) -> dict[str, str | None]:
    """Perform one CardDAV operation without any open database session."""

    operation = str(snapshot.get("operation") or "")
    if operation in {"create", "update"}:
        delivered_href, delivered_etag = carddav.put_contact(
            dict(snapshot.get("config") or {}),
            uid=str(snapshot.get("uid") or ""),
            raw_vcard=str(snapshot.get("raw_vcard") or ""),
            href=str(snapshot.get("href") or "") or None,
            etag=str(snapshot.get("etag") or "") or None,
        )
        return {"href": delivered_href, "etag": delivered_etag}
    if operation == "delete":
        carddav.delete_contact(
            dict(snapshot.get("config") or {}),
            uid=str(snapshot.get("uid") or ""),
            href=str(snapshot.get("href") or "") or None,
            etag=str(snapshot.get("etag") or "") or None,
        )
        return {"href": None, "etag": None}
    raise RuntimeError("Contact delivery operation is invalid")


def _complete_delivery(
    db,
    delivery: ContactDelivery,
    *,
    delivered_href: str | None,
    delivered_etag: str | None,
) -> None:
    """Fence and commit the result of an already-finished network operation."""

    record = db.query(ContactRecord).filter(
        ContactRecord.id == delivery.record_id,
        ContactRecord.owner_id == delivery.owner_id,
    ).one_or_none()
    if record is None:
        raise RuntimeError("Contact delivery authority row is missing")
    payload = dict(delivery.payload or {})
    operation = str(delivery.operation)
    now = utcnow_naive()
    # A configuration edit may commit while the network request is in flight.
    # Fence finalization so an old address-book response can never seed href or
    # ETag metadata into the newly configured source generation.
    source_updated = db.query(ContactSource).filter(
        ContactSource.id == delivery.source_id,
        ContactSource.owner_id == delivery.owner_id,
        ContactSource.config_version == int(
            payload.get("source_config_version") or 0
        ),
    ).update(
        {
            ContactSource.version: ContactSource.version + 1,
            ContactSource.updated_at: now,
        },
        synchronize_session=False,
    )
    if source_updated != 1:
        raise carddav.CardDAVConflict(
            "CardDAV configuration changed during delivery"
        )
    if operation in {"create", "update"}:
        record.remote_href = delivered_href
        record.remote_etag = delivered_etag
    delivery.state = "completed"
    delivery.payload = {}
    delivery.claim_token = None
    delivery.claimed_at = None
    delivery.next_attempt_at = None
    delivery.last_error_code = None
    delivery.completed_at = now
    delivery.version = int(delivery.version or 1) + 1
    db.flush()

    # One successful contact must not erase another record's durable conflict.
    # Derive the source summary from the remaining queue while the config row
    # lock acquired above is still held.
    remaining_problem = db.query(ContactDelivery.id).filter(
        ContactDelivery.owner_id == delivery.owner_id,
        ContactDelivery.source_id == delivery.source_id,
        ContactDelivery.state.in_(("retry", "conflict")),
    ).first() is not None
    remaining_open = db.query(ContactDelivery.id).filter(
        ContactDelivery.owner_id == delivery.owner_id,
        ContactDelivery.source_id == delivery.source_id,
        ContactDelivery.state != "completed",
    ).first() is not None
    source_values: dict[Any, Any] = {
        ContactSource.last_sync_at: now,
        ContactSource.sync_state: (
            "error" if remaining_problem else ("idle" if remaining_open else "ready")
        ),
        ContactSource.updated_at: now,
    }
    if remaining_problem:
        source_values[ContactSource.last_error] = (
            "CardDAV contact delivery requires attention"
        )
    else:
        source_values[ContactSource.last_error] = None
    db.query(ContactSource).filter(
        ContactSource.id == delivery.source_id,
        ContactSource.owner_id == delivery.owner_id,
    ).update(source_values, synchronize_session=False)
    append_action_audit(
        db,
        owner_id=delivery.owner_id,
        action="contacts.delivery.completed",
        entity_type="contact_record",
        entity_id=record.id,
        reason="Queued CardDAV contact mutation completed",
        after_state={
            "operation": operation,
            "delivery_state": "completed",
            "record_version": int(record.version or 1),
        },
        details={"attempts": int(delivery.attempts or 0)},
        idempotency_ref=delivery.idempotency_key,
    )
    db.flush()


def _claimed_delivery(
    db, *, owner_id: str, delivery_id: str, claim_token: str,
) -> ContactDelivery | None:
    return db.query(ContactDelivery).filter(
        ContactDelivery.id == delivery_id,
        ContactDelivery.owner_id == owner_id,
        ContactDelivery.state == "processing",
        ContactDelivery.claim_token == claim_token,
    ).one_or_none()


def _bind_delivery_audit(db, *, owner_id: str) -> None:
    bind_service_audit_context(
        db,
        account_id=owner_id,
        interface="domain_service",
        actor_type="connector",
        credential_type="carddav",
    )


def _record_delivery_failure(
    session_factory,
    *,
    owner_id: str,
    delivery_id: str,
    claim_token: str,
    exc: Exception,
) -> str | None:
    db = session_factory()
    try:
        _bind_delivery_audit(db, owner_id=owner_id)
        delivery = _claimed_delivery(
            db,
            owner_id=owner_id,
            delivery_id=delivery_id,
            claim_token=claim_token,
        )
        if delivery is None:
            db.rollback()
            return None
        state = _fail_delivery(db, delivery, exc)
        db.commit()
        return state
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _fail_delivery(db, delivery: ContactDelivery, exc: Exception) -> str:
    source_exists = db.query(ContactSource.id).filter(
        ContactSource.id == delivery.source_id,
        ContactSource.owner_id == delivery.owner_id,
    ).first() is not None
    conflict = isinstance(exc, carddav.CardDAVConflict)
    exhausted = int(delivery.attempts or 0) >= MAX_DELIVERY_ATTEMPTS
    state = "conflict" if conflict or exhausted else "retry"
    code = "remote_conflict" if conflict else (
        "attempts_exhausted" if exhausted else "transport_failure"
    )
    now = utcnow_naive()
    delivery.state = state
    delivery.claim_token = None
    delivery.claimed_at = None
    delivery.last_error_code = code
    delivery.next_attempt_at = None if state == "conflict" else (
        now + timedelta(seconds=min(300, 2 ** min(int(delivery.attempts or 1), 8)))
    )
    delivery.version = int(delivery.version or 1) + 1
    if source_exists:
        source_error = (
            "CardDAV contact changed remotely"
            if conflict
            else (
                "CardDAV contact delivery stopped after repeated failures"
                if exhausted
                else "CardDAV contact delivery will retry"
            )
        )
        db.query(ContactSource).filter(
            ContactSource.id == delivery.source_id,
            ContactSource.owner_id == delivery.owner_id,
        ).update(
            {
                ContactSource.sync_state: "error",
                ContactSource.last_error: source_error,
                ContactSource.version: ContactSource.version + 1,
                ContactSource.updated_at: now,
            },
            synchronize_session=False,
        )
    append_action_audit(
        db,
        owner_id=delivery.owner_id,
        action="contacts.delivery.failed",
        entity_type="contact_record",
        entity_id=delivery.record_id,
        reason="Queued CardDAV contact mutation did not complete",
        after_state={
            "operation": delivery.operation,
            "delivery_state": state,
        },
        details={"error_code": code, "attempts": int(delivery.attempts or 0)},
        idempotency_ref=delivery.idempotency_key,
        outcome="failure",
    )
    db.flush()
    return state


def drain_contact_deliveries(
    session_factory=SessionLocal,
    *,
    owner_id: str,
    source_id: str | None = None,
    limit: int = 100,
) -> dict[str, int]:
    """Deliver committed work, committing each network operation separately."""

    result = {"completed": 0, "retried": 0, "conflicts": 0}
    for _ in range(max(1, min(int(limit), 500))):
        claim_db = session_factory()
        try:
            _bind_delivery_audit(claim_db, owner_id=owner_id)
            delivery = _claim_one(
                claim_db, owner_id=owner_id, source_id=source_id,
            )
            if delivery is None:
                claim_db.rollback()
                break
            delivery_id = str(delivery.id)
            claim_token = str(delivery.claim_token or "")
            if not claim_token:
                raise RuntimeError("Contact delivery claim token is missing")
            # Persist the lease before any network I/O.  A crash therefore
            # leaves recoverable processing work instead of an invisible,
            # transaction-held claim, and SQLite's global writer lock is not
            # held across CardDAV timeouts.
            claim_db.commit()
        except Exception:
            claim_db.rollback()
            raise
        finally:
            claim_db.close()

        snapshot_db = session_factory()
        try:
            _bind_delivery_audit(snapshot_db, owner_id=owner_id)
            delivery = _claimed_delivery(
                snapshot_db,
                owner_id=owner_id,
                delivery_id=delivery_id,
                claim_token=claim_token,
            )
            if delivery is None:
                snapshot_db.rollback()
                continue
            snapshot = _load_delivery_snapshot(snapshot_db, delivery)
            # End every DB transaction before socket resolution or HTTP I/O.
            snapshot_db.rollback()
        except (carddav.CardDAVError, RuntimeError) as exc:
            snapshot_db.rollback()
            state = _record_delivery_failure(
                session_factory,
                owner_id=owner_id,
                delivery_id=delivery_id,
                claim_token=claim_token,
                exc=exc,
            )
            if state is not None:
                result["conflicts" if state == "conflict" else "retried"] += 1
            continue
        except Exception:
            snapshot_db.rollback()
            raise
        finally:
            snapshot_db.close()

        try:
            delivered = _execute_delivery(snapshot)
        except (carddav.CardDAVError, RuntimeError) as exc:
            state = _record_delivery_failure(
                session_factory,
                owner_id=owner_id,
                delivery_id=delivery_id,
                claim_token=claim_token,
                exc=exc,
            )
            if state is not None:
                result["conflicts" if state == "conflict" else "retried"] += 1
            continue
        finally:
            # Drop the detached credential/vCard snapshot promptly.  Exceptions
            # and audit rows contain only structural error codes.
            snapshot.clear()

        db = session_factory()
        try:
            _bind_delivery_audit(db, owner_id=owner_id)
            delivery = _claimed_delivery(
                db,
                owner_id=owner_id,
                delivery_id=delivery_id,
                claim_token=claim_token,
            )
            if delivery is None:
                db.rollback()
                continue
            try:
                _complete_delivery(
                    db,
                    delivery,
                    delivered_href=delivered.get("href"),
                    delivered_etag=delivered.get("etag"),
                )
                outcome = "completed"
            except (carddav.CardDAVError, RuntimeError) as exc:
                state = _fail_delivery(db, delivery, exc)
                outcome = "conflicts" if state == "conflict" else "retried"
            db.commit()
            result[outcome] += 1
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    return result


def inprocess_contact_delivery_enabled() -> bool:
    """Return whether the lifespan-owned connector worker should run.

    This gate is intentionally independent from both Tasks and the email or
    Telegram pollers.  Deployments with an external delivery worker can disable
    only this loop.
    """

    return os.getenv(
        "RESTIA_INPROCESS_CONTACT_DELIVERY", "1"
    ).strip().lower() not in {"0", "false", "no", "off", ""}


def contact_delivery_worker_enabled(*, cutover_error: object = None) -> bool:
    """Fail closed while legacy contact adoption requires recovery."""

    return cutover_error is None and inprocess_contact_delivery_enabled()


def pending_contact_delivery_owner_ids(
    session_factory=SessionLocal, *, limit: int = 100,
) -> list[str]:
    """Return a bounded, non-identifying work queue of due owner IDs."""

    db = session_factory()
    try:
        now = utcnow_naive()
        oldest_due = func.min(ContactDelivery.created_at).label("oldest_due")
        rows = db.query(ContactDelivery.owner_id, oldest_due).filter(
            _eligible_delivery_filter(now),
            ~_has_earlier_open_delivery(),
        ).group_by(ContactDelivery.owner_id).order_by(
            oldest_due.asc(), ContactDelivery.owner_id.asc()
        ).limit(
            max(1, min(int(limit), 500))
        ).all()
        return [str(owner_id) for owner_id, _oldest_due in rows if owner_id]
    finally:
        db.rollback()
        db.close()


async def drain_contact_deliveries_once(
    session_factory=SessionLocal,
    *,
    owner_limit: int = 100,
    batch_size: int = 5,
) -> dict[str, int]:
    """Run one bounded, TaskScheduler-independent background delivery pass."""

    totals = {"owners": 0, "completed": 0, "retried": 0, "conflicts": 0}
    owners = await asyncio.to_thread(
        pending_contact_delivery_owner_ids,
        session_factory,
        limit=owner_limit,
    )
    for owner_id in owners:
        try:
            result = await asyncio.to_thread(
                drain_contact_deliveries,
                session_factory,
                owner_id=owner_id,
                limit=max(1, min(int(batch_size), 25)),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never log owner IDs, URLs, credentials, vCards, or connector
            # exception text. The durable row and ActionAudit hold redacted
            # structural status for later recovery.
            logger.warning(
                "Contact delivery background pass failed: %s",
                type(exc).__name__,
            )
            continue
        totals["owners"] += 1
        for key in ("completed", "retried", "conflicts"):
            totals[key] += int(result.get(key, 0))
        await asyncio.sleep(0)
    return totals


async def contact_delivery_loop(
    session_factory=SessionLocal,
    *,
    idle_seconds: float | None = None,
    batch_size: int | None = None,
) -> None:
    """Continuously drain CardDAV outbox work under FastAPI lifespan ownership."""

    if idle_seconds is None:
        try:
            idle_seconds = float(
                os.getenv("RESTIA_CONTACT_DELIVERY_INTERVAL_SECONDS", "2")
            )
        except (TypeError, ValueError):
            idle_seconds = 2.0
    interval = max(0.25, min(float(idle_seconds), 300.0))
    if batch_size is None:
        try:
            batch_size = int(os.getenv("RESTIA_CONTACT_DELIVERY_BATCH_SIZE", "5"))
        except (TypeError, ValueError):
            batch_size = 5
    bounded_batch = max(1, min(int(batch_size), 25))

    while True:
        try:
            totals = await drain_contact_deliveries_once(
                session_factory,
                owner_limit=100,
                batch_size=bounded_batch,
            )
            await asyncio.sleep(0.1 if totals["owners"] else interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Contact delivery worker iteration failed: %s",
                type(exc).__name__,
            )
            await asyncio.sleep(interval)


__all__ = [
    "contact_delivery_loop",
    "contact_delivery_worker_enabled",
    "drain_contact_deliveries",
    "drain_contact_deliveries_once",
    "enqueue_contact_delivery",
    "inprocess_contact_delivery_enabled",
    "open_delivery_record_ids",
    "pending_contact_delivery_owner_ids",
]
