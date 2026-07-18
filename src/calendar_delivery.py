"""Durable, replay-safe CalDAV delivery for owner-scoped calendar events.

Calendar mutations and their encrypted ``CalendarDelivery`` rows commit in one
authority transaction.  This worker claims only committed rows, releases every
database transaction before DNS/HTTP, then finalizes through claim, event, and
configuration compare-and-swap fences.  Per-event FIFO plus conditional WebDAV
requests makes stale-lease recovery safe after a process crash.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, exists, func, or_
from sqlalchemy.orm import aliased

from core.database import (
    Account,
    ActionAudit,
    CalendarCal,
    CalendarDelivery,
    CalendarEvent,
    SessionLocal,
    utcnow_naive,
)
from src import caldav_writeback as caldav
from src.audit_context import bind_service_audit_context, build_action_audit_details
from src.secret_storage import private_digest


CLAIM_LEASE_SECONDS = 120
MAX_DELIVERY_ATTEMPTS = 12
logger = logging.getLogger(__name__)


class CalendarConfigChanged(caldav.CalDAVConflict):
    """The connector binding changed before a result could be finalized."""


class CalendarEventChanged(caldav.CalDAVConflict):
    """The local event generation is no longer durably represented."""


class CalendarSnapshotInvalid(caldav.CalDAVConflict):
    """An encrypted outbox snapshot failed its structural fence."""


def _append_action_audit(
    db,
    *,
    owner_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    reason: str,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
    idempotency_ref: object | None = None,
    outcome: str = "success",
) -> ActionAudit:
    """Append a structural audit without importing higher-level domain cycles."""

    audit = ActionAudit(
        id=str(uuid.uuid4()),
        owner_id=owner_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_state=dict(before_state or {}),
        after_state=dict(after_state or {}),
        details=build_action_audit_details(
            db,
            owner_id=owner_id,
            reason=reason,
            outcome=outcome,
            idempotency_ref=idempotency_ref,
            domain_details=details,
        ),
    )
    db.add(audit)
    db.flush()
    return audit


def _eligible_delivery_filter(now):
    stale_before = now - timedelta(seconds=CLAIM_LEASE_SECONDS)
    return or_(
        CalendarDelivery.state == "pending",
        and_(
            CalendarDelivery.state == "retry",
            or_(
                CalendarDelivery.next_attempt_at.is_(None),
                CalendarDelivery.next_attempt_at <= now,
            ),
        ),
        and_(
            CalendarDelivery.state == "processing",
            or_(
                CalendarDelivery.lease_expires_at.is_(None),
                CalendarDelivery.lease_expires_at <= now,
                and_(
                    CalendarDelivery.claimed_at.isnot(None),
                    CalendarDelivery.claimed_at <= stale_before,
                ),
            ),
        ),
    )


def _has_earlier_open_delivery():
    predecessor = aliased(CalendarDelivery)
    return exists().where(and_(
        predecessor.owner_id == CalendarDelivery.owner_id,
        predecessor.event_uid == CalendarDelivery.event_uid,
        predecessor.state.notin_(("completed", "cancelled")),
        predecessor.id != CalendarDelivery.id,
        or_(
            predecessor.created_at < CalendarDelivery.created_at,
            and_(
                predecessor.created_at == CalendarDelivery.created_at,
                predecessor.id < CalendarDelivery.id,
            ),
        ),
    ))


def _claim_one(db, *, owner_id: str, calendar_id: str | None = None):
    """Lease one FIFO-eligible row using a versioned compare-and-swap."""

    now = utcnow_naive()
    eligible = _eligible_delivery_filter(now)
    query = db.query(CalendarDelivery).filter(
        CalendarDelivery.owner_id == owner_id,
        eligible,
        ~_has_earlier_open_delivery(),
    )
    if calendar_id:
        query = query.filter(CalendarDelivery.calendar_id == calendar_id)
    candidates = query.order_by(
        CalendarDelivery.created_at.asc(), CalendarDelivery.id.asc(),
    ).limit(100).all()
    for candidate in candidates:
        token = str(uuid.uuid4())
        expected_version = int(candidate.version or 1)
        values = {
            CalendarDelivery.state: "processing",
            CalendarDelivery.claim_token: token,
            CalendarDelivery.claimed_at: now,
            CalendarDelivery.lease_expires_at: now + timedelta(
                seconds=CLAIM_LEASE_SECONDS
            ),
            CalendarDelivery.next_attempt_at: None,
            CalendarDelivery.last_error_code: None,
            CalendarDelivery.attempts: int(candidate.attempts or 0) + 1,
            CalendarDelivery.version: expected_version + 1,
            CalendarDelivery.updated_at: now,
        }
        updated = db.query(CalendarDelivery).filter(
            CalendarDelivery.id == candidate.id,
            CalendarDelivery.owner_id == owner_id,
            CalendarDelivery.version == expected_version,
            eligible,
        ).update(values, synchronize_session=False)
        if updated == 1:
            db.flush()
            db.expire(candidate)
            return db.query(CalendarDelivery).filter(
                CalendarDelivery.id == candidate.id,
                CalendarDelivery.claim_token == token,
            ).one()
    return None


def _claimed_delivery(
    db, *, owner_id: str, delivery_id: str, claim_token: str,
) -> CalendarDelivery | None:
    return db.query(CalendarDelivery).filter(
        CalendarDelivery.id == delivery_id,
        CalendarDelivery.owner_id == owner_id,
        CalendarDelivery.state == "processing",
        CalendarDelivery.claim_token == claim_token,
    ).one_or_none()


def _has_successor_for_version(
    db, delivery: CalendarDelivery, *, event_version: int,
) -> bool:
    """Prove a newer local edit has its own committed FIFO operation."""

    return db.query(CalendarDelivery.id).filter(
        CalendarDelivery.owner_id == delivery.owner_id,
        CalendarDelivery.event_uid == delivery.event_uid,
        CalendarDelivery.id != delivery.id,
        CalendarDelivery.state.notin_(("completed", "cancelled")),
        CalendarDelivery.expected_event_version == int(event_version),
        or_(
            CalendarDelivery.created_at > delivery.created_at,
            and_(
                CalendarDelivery.created_at == delivery.created_at,
                CalendarDelivery.id > delivery.id,
            ),
        ),
    ).first() is not None


def _event_generation_is_covered(
    db, delivery: CalendarDelivery, *, event_version: int,
) -> bool:
    expected = int(delivery.expected_event_version or 0)
    current = int(event_version or 0)
    return current == expected or (
        current > expected
        and _has_successor_for_version(
            db, delivery, event_version=current,
        )
    )


def _parse_snapshot_datetime(value: object) -> datetime:
    raw = str(value or "").strip()
    if raw.endswith("Z"):
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00")
        return parsed.replace(tzinfo=None)
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _snapshot_ical(payload: dict[str, Any], *, uid: str) -> str:
    event = payload.get("event")
    if not isinstance(event, dict):
        raise CalendarSnapshotInvalid("Calendar delivery snapshot is missing")
    if str(event.get("uid") or "") != uid:
        raise CalendarSnapshotInvalid("Calendar delivery snapshot UID changed")
    try:
        recurrence_exdates = event.get("recurrence_exdates") or []
        if isinstance(recurrence_exdates, str):
            recurrence_exdates = json.loads(recurrence_exdates or "[]")
        if not isinstance(recurrence_exdates, list):
            raise TypeError("recurrence_exdates must be a list")
        event_version = int(event.get("event_version") or 0)
        raw = {
            "uid": uid,
            "summary": str(event.get("summary") or ""),
            "description": str(event.get("description") or ""),
            "location": str(event.get("location") or ""),
            "dtstart": _parse_snapshot_datetime(event.get("dtstart")),
            "dtend": _parse_snapshot_datetime(event.get("dtend")),
            "all_day": bool(event.get("all_day")),
            "is_utc": bool(event.get("is_utc")),
            "rrule": str(event.get("rrule") or ""),
            "recurrence_exdates": list(recurrence_exdates),
        }
    except (TypeError, ValueError) as exc:
        raise CalendarSnapshotInvalid(
            "Calendar delivery snapshot is invalid"
        ) from exc
    if event_version < 1:
        raise CalendarSnapshotInvalid("Calendar delivery snapshot is invalid")
    try:
        return caldav.build_event_ical(raw)
    except Exception as exc:
        raise CalendarSnapshotInvalid(
            "Calendar delivery snapshot is invalid"
        ) from exc


def _load_delivery_snapshot(db, delivery: CalendarDelivery) -> dict[str, Any]:
    """Return only detached primitives for the exact claimed generation."""

    calendar = db.query(CalendarCal).filter(
        CalendarCal.id == delivery.calendar_id,
        CalendarCal.owner_id == delivery.owner_id,
    ).one_or_none()
    event = db.query(CalendarEvent).filter(
        CalendarEvent.uid == delivery.event_uid,
        CalendarEvent.owner_id == delivery.owner_id,
        CalendarEvent.calendar_id == delivery.calendar_id,
    ).one_or_none()
    account = db.query(Account).filter(
        Account.id == delivery.owner_id,
        Account.status == "active",
    ).one_or_none()
    if calendar is None or event is None or account is None:
        raise CalendarConfigChanged("Calendar delivery authority is missing")
    if str(calendar.source or "").lower() != "caldav":
        raise CalendarConfigChanged("Calendar is no longer CalDAV-backed")
    if int(calendar.config_version or 0) != int(
        delivery.expected_config_version or 0
    ):
        raise CalendarConfigChanged("CalDAV configuration changed")
    payload = dict(delivery.payload or {})
    payload_event = payload.get("event")
    try:
        payload_version = int(
            payload_event.get("event_version") or 0
        ) if isinstance(payload_event, dict) else 0
    except (TypeError, ValueError) as exc:
        raise CalendarSnapshotInvalid(
            "Calendar delivery generation changed"
        ) from exc
    if not isinstance(payload_event, dict) or payload_version != int(
        delivery.expected_event_version or 0
    ):
        raise CalendarSnapshotInvalid("Calendar delivery generation changed")
    if not _event_generation_is_covered(
        db, delivery, event_version=int(event.version or 1),
    ):
        raise CalendarEventChanged("Calendar event changed without delivery")

    operation = str(delivery.operation or "").lower()
    if operation not in {"create", "update", "delete"}:
        raise CalendarSnapshotInvalid("Calendar delivery operation is invalid")
    raw_ical = "" if operation == "delete" else _snapshot_ical(
        payload, uid=str(delivery.event_uid),
    )
    return {
        "owner_username": str(account.username),
        "owner_id": str(delivery.owner_id),
        "delivery_id": str(delivery.id),
        "delivery_version": int(delivery.version or 1),
        "calendar_id": str(calendar.id),
        "calendar_account_id": str(calendar.account_id or ""),
        "calendar_base_url": str(calendar.caldav_base_url or ""),
        "config_version": int(calendar.config_version or 1),
        "event_uid": str(delivery.event_uid),
        "observed_event_version": int(event.version or 1),
        "operation": operation,
        "raw_ical": raw_ical,
        # Prefer the authority row's most recently delivered metadata. This
        # lets a queued create->update or delete->restore chain advance safely.
        "href": str(event.remote_href or ""),
        "etag": str(event.remote_etag or ""),
    }


def _account_marker(account: dict[str, Any]) -> str:
    """Opaque marker for rebind-sensitive config, excluding rotated access tokens."""

    material = json.dumps({
        "id": str(account.get("id") or ""),
        "url": str(account.get("url") or "").strip(),
        "username": str(account.get("username") or "").strip(),
        "password": str(account.get("password") or ""),
        "oauth_provider": str(account.get("oauth_provider") or ""),
        "oauth_refresh_token": str(account.get("oauth_refresh_token") or ""),
    }, sort_keys=True, separators=(",", ":"))
    return private_digest("calendar-config-fence-v1", material)


def _select_account(owner_username: str, account_id: str) -> dict[str, Any]:
    from src.caldav_sync import _load_caldav_accounts

    accounts = list(_load_caldav_accounts(owner_username) or [])
    if account_id:
        matches = [
            account for account in accounts
            if str(account.get("id") or "") == account_id
        ]
    else:
        # Legacy calendars are safe only when ownership is unambiguous.
        matches = accounts if len(accounts) == 1 else []
    if len(matches) != 1:
        raise CalendarConfigChanged("CalDAV account binding is unavailable")
    return dict(matches[0])


def _resolve_account_config(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Resolve/decrypt one account after the DB transaction has ended."""

    from src.caldav_sync import validate_caldav_url
    from src.secret_storage import decrypt

    account = _select_account(
        str(snapshot["owner_username"]),
        str(snapshot.get("calendar_account_id") or ""),
    )
    try:
        account_url = validate_caldav_url(str(account.get("url") or ""))
        collection_url = validate_caldav_url(
            str(snapshot.get("calendar_base_url") or account_url)
        )
    except ValueError as exc:
        raise CalendarConfigChanged(
            "CalDAV destination is no longer valid"
        ) from exc
    # Stored collection hrefs may vary in path, but never in credential origin.
    caldav._same_origin_resource(account_url, collection_url)
    password = decrypt(str(account.get("password") or ""))
    access_token = ""
    if account.get("oauth_provider") == "google":
        from src.caldav_sync import _ensure_google_calendar_token

        access_token = str(
            _ensure_google_calendar_token(account, str(snapshot["owner_username"]))
            or ""
        )
    username = str(account.get("username") or "").strip()
    if not account_url or not username or not (password or access_token):
        raise caldav.CalDAVAuthError("CalDAV credentials are incomplete")
    return {
        "url": account_url,
        "collection_url": collection_url,
        "username": username,
        "password": password,
        "access_token": access_token,
        "config_marker": _account_marker(account),
    }


def _current_account_marker(snapshot: dict[str, Any]) -> str:
    account = _select_account(
        str(snapshot["owner_username"]),
        str(snapshot.get("calendar_account_id") or ""),
    )
    return _account_marker(account)


def _execute_delivery(snapshot: dict[str, Any]) -> dict[str, str | None]:
    """Perform one remote operation without a live database session."""

    config = _resolve_account_config(snapshot)
    snapshot["config_marker"] = str(config.pop("config_marker"))
    collection_url = str(config.pop("collection_url"))
    operation = str(snapshot["operation"])
    href = str(snapshot.get("href") or "") or None
    etag = str(snapshot.get("etag") or "") or None
    if operation == "delete":
        # A prior FIFO delete or a pull may already have proved absence.
        if href is None:
            return {"href": None, "etag": None}
        caldav.delete_calendar_event(
            config,
            collection_url=collection_url,
            href=href,
            etag=etag,
        )
        return {"href": None, "etag": None}

    # Queued operations intentionally adapt to metadata produced by an earlier
    # FIFO item: create->update uses the newly learned ETag, while a restore
    # after delete becomes a deterministic conditional create.
    effective_operation = "update" if href and etag else "create"
    delivered_href, delivered_etag = caldav.put_calendar_event(
        config,
        collection_url=collection_url,
        uid=str(snapshot["event_uid"]),
        raw_ical=str(snapshot["raw_ical"]),
        operation=effective_operation,
        href=href,
        etag=etag,
    )
    return {"href": delivered_href, "etag": delivered_etag}


def _calendar_config_filter(query, snapshot: dict[str, Any]):
    query = query.filter(
        CalendarCal.config_version == int(snapshot["config_version"]),
    )
    account_id = str(snapshot.get("calendar_account_id") or "")
    query = query.filter(
        CalendarCal.account_id == account_id
        if account_id else CalendarCal.account_id.is_(None)
    )
    base_url = str(snapshot.get("calendar_base_url") or "")
    return query.filter(
        CalendarCal.caldav_base_url == base_url
        if base_url else CalendarCal.caldav_base_url.is_(None)
    )


def _complete_delivery(
    db,
    delivery: CalendarDelivery,
    *,
    snapshot: dict[str, Any],
    delivered_href: str | None,
    delivered_etag: str | None,
) -> None:
    """Finalize only if claim, delivery, event, and config generations match."""

    if str(snapshot.get("config_marker") or "") != _current_account_marker(snapshot):
        raise CalendarConfigChanged("CalDAV account binding changed")
    now = utcnow_naive()
    calendar_updated = _calendar_config_filter(
        db.query(CalendarCal).filter(
            CalendarCal.id == delivery.calendar_id,
            CalendarCal.owner_id == delivery.owner_id,
            CalendarCal.source == "caldav",
        ),
        snapshot,
    ).update(
        {CalendarCal.updated_at: CalendarCal.updated_at},
        synchronize_session=False,
    )
    if calendar_updated != 1:
        raise CalendarConfigChanged("CalDAV configuration changed")

    event = db.query(CalendarEvent).filter(
        CalendarEvent.uid == delivery.event_uid,
        CalendarEvent.owner_id == delivery.owner_id,
        CalendarEvent.calendar_id == delivery.calendar_id,
    ).one_or_none()
    if event is None or not _event_generation_is_covered(
        db, delivery, event_version=int(event.version or 1),
    ):
        raise CalendarEventChanged("Calendar event changed without delivery")
    event_version = int(event.version or 1)
    event_values: dict[Any, Any] = {
        CalendarEvent.remote_href: delivered_href,
        CalendarEvent.remote_etag: delivered_etag,
        CalendarEvent.caldav_sync_pending: None,
        CalendarEvent.updated_at: CalendarEvent.updated_at,
    }
    event_updated = db.query(CalendarEvent).filter(
        CalendarEvent.uid == delivery.event_uid,
        CalendarEvent.owner_id == delivery.owner_id,
        CalendarEvent.calendar_id == delivery.calendar_id,
        CalendarEvent.version == event_version,
    ).update(event_values, synchronize_session=False)
    if event_updated != 1:
        raise CalendarEventChanged("Calendar event changed during delivery")

    claim_version = int(snapshot["delivery_version"])
    delivery_updated = db.query(CalendarDelivery).filter(
        CalendarDelivery.id == delivery.id,
        CalendarDelivery.owner_id == delivery.owner_id,
        CalendarDelivery.state == "processing",
        CalendarDelivery.claim_token == delivery.claim_token,
        CalendarDelivery.version == claim_version,
    ).update({
        CalendarDelivery.state: "completed",
        CalendarDelivery.payload: {},
        CalendarDelivery.claim_token: None,
        CalendarDelivery.claimed_at: None,
        CalendarDelivery.lease_expires_at: None,
        CalendarDelivery.next_attempt_at: None,
        CalendarDelivery.last_error_code: None,
        CalendarDelivery.completed_at: now,
        CalendarDelivery.version: claim_version + 1,
        CalendarDelivery.updated_at: now,
    }, synchronize_session=False)
    if delivery_updated != 1:
        raise caldav.CalDAVConflict("Calendar delivery claim changed")
    _append_action_audit(
        db,
        owner_id=delivery.owner_id,
        action="calendar.delivery.completed",
        entity_type="calendar_event",
        entity_id=delivery.event_uid,
        reason="Queued CalDAV calendar mutation completed",
        after_state={
            "operation": str(delivery.operation),
            "delivery_state": "completed",
            "event_version": event_version,
        },
        details={"attempts": int(delivery.attempts or 0)},
        idempotency_ref=delivery.idempotency_key,
    )
    db.flush()


def _failure_code(exc: Exception, *, exhausted: bool) -> tuple[str, bool]:
    if isinstance(exc, CalendarConfigChanged):
        return "config_changed", True
    if isinstance(exc, CalendarEventChanged):
        return "event_changed", True
    if isinstance(exc, CalendarSnapshotInvalid):
        return "invalid_snapshot", True
    if isinstance(exc, caldav.CalDAVNotFound):
        return "remote_missing", True
    if isinstance(exc, caldav.CalDAVConflict):
        return "remote_conflict", True
    if exhausted:
        return "attempts_exhausted", True
    if isinstance(exc, caldav.CalDAVAuthError):
        return "authentication_failed", False
    if isinstance(exc, caldav.CalDAVDiscoveryError):
        return "discovery_failed", False
    return "transport_failure", False


def _fail_delivery(db, delivery: CalendarDelivery, exc: Exception) -> str:
    exhausted = int(delivery.attempts or 0) >= MAX_DELIVERY_ATTEMPTS
    code, terminal = _failure_code(exc, exhausted=exhausted)
    state = "conflict" if terminal else "retry"
    now = utcnow_naive()
    delivery.state = state
    delivery.claim_token = None
    delivery.claimed_at = None
    delivery.lease_expires_at = None
    delivery.last_error_code = code
    delivery.next_attempt_at = None if terminal else now + timedelta(
        seconds=min(900, 2 ** min(int(delivery.attempts or 1), 9))
    )
    delivery.version = int(delivery.version or 1) + 1
    _append_action_audit(
        db,
        owner_id=delivery.owner_id,
        action="calendar.delivery.failed",
        entity_type="calendar_event",
        entity_id=delivery.event_uid,
        reason="Queued CalDAV calendar mutation did not complete",
        after_state={
            "operation": str(delivery.operation),
            "delivery_state": state,
        },
        details={
            "error_code": code,
            "attempts": int(delivery.attempts or 0),
        },
        idempotency_ref=delivery.idempotency_key,
        outcome="failure",
    )
    db.flush()
    return state


def _bind_delivery_audit(db, *, owner_id: str) -> None:
    bind_service_audit_context(
        db,
        account_id=owner_id,
        interface="domain_service",
        actor_type="connector",
        credential_type="caldav",
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


def drain_calendar_deliveries(
    session_factory=SessionLocal,
    *,
    owner_id: str,
    calendar_id: str | None = None,
    limit: int = 100,
) -> dict[str, int]:
    """Deliver committed rows, with no database session during network I/O."""

    result = {"completed": 0, "retried": 0, "conflicts": 0}
    for _ in range(max(1, min(int(limit), 500))):
        claim_db = session_factory()
        try:
            _bind_delivery_audit(claim_db, owner_id=owner_id)
            delivery = _claim_one(
                claim_db, owner_id=owner_id, calendar_id=calendar_id,
            )
            if delivery is None:
                claim_db.rollback()
                break
            delivery_id = str(delivery.id)
            claim_token = str(delivery.claim_token or "")
            if not claim_token:
                raise RuntimeError("Calendar delivery claim token is missing")
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
            snapshot_db.rollback()
        except (caldav.CalDAVError, RuntimeError) as exc:
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
        except (caldav.CalDAVError, RuntimeError) as exc:
            try:
                state = _record_delivery_failure(
                    session_factory,
                    owner_id=owner_id,
                    delivery_id=delivery_id,
                    claim_token=claim_token,
                    exc=exc,
                )
            finally:
                snapshot.clear()
            if state is not None:
                result["conflicts" if state == "conflict" else "retried"] += 1
            continue
        except Exception:
            # Unexpected process failures deliberately leave the committed
            # lease for stale recovery, but private detached content should not
            # stay reachable from this worker frame.
            snapshot.clear()
            raise

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
                    snapshot=snapshot,
                    delivered_href=delivered.get("href"),
                    delivered_etag=delivered.get("etag"),
                )
                outcome = "completed"
            except (caldav.CalDAVError, RuntimeError) as exc:
                state = _fail_delivery(db, delivery, exc)
                outcome = "conflicts" if state == "conflict" else "retried"
            db.commit()
            result[outcome] += 1
        except Exception:
            db.rollback()
            raise
        finally:
            snapshot.clear()
            db.close()
    return result


def inprocess_calendar_delivery_enabled() -> bool:
    """Independent from Tasks, email, Telegram, and contact delivery loops."""

    return os.getenv(
        "RESTIA_INPROCESS_CALENDAR_DELIVERY", "1"
    ).strip().lower() not in {"0", "false", "no", "off", ""}


def pending_calendar_delivery_owner_ids(
    session_factory=SessionLocal, *, limit: int = 100,
) -> list[str]:
    db = session_factory()
    try:
        now = utcnow_naive()
        oldest_due = func.min(CalendarDelivery.created_at).label("oldest_due")
        rows = db.query(
            CalendarDelivery.owner_id, oldest_due,
        ).filter(
            _eligible_delivery_filter(now),
            ~_has_earlier_open_delivery(),
        ).group_by(CalendarDelivery.owner_id).order_by(
            oldest_due.asc(), CalendarDelivery.owner_id.asc(),
        ).limit(max(1, min(int(limit), 500))).all()
        return [str(owner_id) for owner_id, _ in rows if owner_id]
    finally:
        db.rollback()
        db.close()


async def drain_calendar_deliveries_once(
    session_factory=SessionLocal,
    *,
    owner_limit: int = 100,
    batch_size: int = 5,
) -> dict[str, int]:
    totals = {"owners": 0, "completed": 0, "retried": 0, "conflicts": 0}
    owners = await asyncio.to_thread(
        pending_calendar_delivery_owner_ids,
        session_factory,
        limit=owner_limit,
    )
    for owner_id in owners:
        try:
            result = await asyncio.to_thread(
                drain_calendar_deliveries,
                session_factory,
                owner_id=owner_id,
                limit=max(1, min(int(batch_size), 25)),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Calendar delivery background pass failed: %s",
                type(exc).__name__,
            )
            continue
        totals["owners"] += 1
        for key in ("completed", "retried", "conflicts"):
            totals[key] += int(result.get(key, 0))
        await asyncio.sleep(0)
    return totals


async def calendar_delivery_loop(
    session_factory=SessionLocal,
    *,
    idle_seconds: float | None = None,
    batch_size: int | None = None,
) -> None:
    if idle_seconds is None:
        try:
            idle_seconds = float(os.getenv(
                "RESTIA_CALENDAR_DELIVERY_INTERVAL_SECONDS", "2"
            ))
        except (TypeError, ValueError):
            idle_seconds = 2.0
    interval = max(0.25, min(float(idle_seconds), 300.0))
    if batch_size is None:
        try:
            batch_size = int(os.getenv(
                "RESTIA_CALENDAR_DELIVERY_BATCH_SIZE", "5"
            ))
        except (TypeError, ValueError):
            batch_size = 5
    bounded_batch = max(1, min(int(batch_size), 25))
    while True:
        try:
            totals = await drain_calendar_deliveries_once(
                session_factory,
                owner_limit=100,
                batch_size=bounded_batch,
            )
            await asyncio.sleep(0.1 if totals["owners"] else interval)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Calendar delivery worker iteration failed: %s",
                type(exc).__name__,
            )
            await asyncio.sleep(interval)


__all__ = [
    "calendar_delivery_loop",
    "drain_calendar_deliveries",
    "drain_calendar_deliveries_once",
    "inprocess_calendar_delivery_enabled",
    "pending_calendar_delivery_owner_ids",
]
