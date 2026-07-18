"""Canonical, owner-scoped calendar mutations for Restia V3.

This module is deliberately transport-free.  Callers own the SQLAlchemy
transaction; a calendar mutation, its Life Graph projection, its structural
audit rows, and any encrypted CalDAV delivery are flushed together and can be
rolled back together.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from dateutil.rrule import rrulestr
from sqlalchemy.exc import IntegrityError

from core.database import (
    Account,
    CalendarCal,
    CalendarDelivery,
    CalendarEvent,
    EntityLink,
    LifeEntity,
    utcnow_naive,
)
from src.life_core import append_action_audit
from src.life_graph import (
    LifeGraphError,
    create_entity_link,
    create_life_entity,
    update_life_entity,
)
from src.secret_storage import private_digest


MAX_UID_CHARS = 255
MAX_SUMMARY_CHARS = 1_000
MAX_DESCRIPTION_CHARS = 100_000
MAX_LOCATION_CHARS = 4_000
MAX_RRULE_CHARS = 512
MAX_LINKS = 100
SNAPSHOT_TYPE = "restia.calendar_event"
SNAPSHOT_VERSION = 1

_DEFAULT_CALENDAR_NAMESPACE = uuid.UUID("20bf7a57-fce6-48c6-95bd-f31359a12ef9")
_IDEMPOTENT_EVENT_NAMESPACE = uuid.UUID("2e4e51f1-e62f-4495-824d-9bf6ff340ab1")
_UNSET = object()
_TIMED_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?"
    r"(?:Z|[+-]\d{2}:\d{2})?$"
)
_DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RRULE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*$")
_RRULE_VALUE_RE = re.compile(r"^[A-Z0-9,+-]+$")
_RRULE_KEYS = frozenset({
    "FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTH",
    "BYMONTHDAY", "BYHOUR", "BYMINUTE", "BYSETPOS", "WKST",
})
_RRULE_FREQS = frozenset({"DAILY", "WEEKLY", "MONTHLY", "YEARLY"})
_IMPORTANCE = frozenset({"low", "normal", "high", "critical"})
_EVENT_TYPES = frozenset({
    "meeting", "class", "work", "personal", "travel", "deadline",
    "routine", "focus", "rest", "reminder", "health", "meal", "social",
    "admin", "other",
})
_LINK_TARGET_TYPES = frozenset({
    "life_area", "goal", "project", "milestone", "task", "action",
    "person", "note", "file", "decision", "event", "deadline",
})
_SNAPSHOT_KEYS = frozenset({
    "snapshot_type", "snapshot_version", "uid", "calendar_id",
    "event_version", "summary", "description", "location", "dtstart",
    "dtend", "all_day", "is_utc", "rrule", "recurrence_exdates", "color",
    "status", "importance", "event_type",
})


class CalendarServiceError(ValueError):
    """Base class for safe calendar-domain failures."""


class CalendarNotFound(CalendarServiceError):
    pass


class CalendarConflict(CalendarServiceError):
    pass


class CalendarRemoteWritePending(CalendarConflict):
    """A committed local generation must reach CalDAV before pull may win."""


@dataclass(frozen=True)
class CalendarMutationResult:
    event: CalendarEvent
    graph_entity: LifeEntity
    delivery: CalendarDelivery | None
    links_created: tuple[EntityLink, ...]
    created: bool
    event_version: int
    graph_version: int
    delivery_version: int | None
    before_snapshot: dict[str, Any] | None
    after_snapshot: dict[str, Any]


def _owner_id(account: Account) -> str:
    owner_id = str(getattr(account, "id", "") or "").strip()
    if not owner_id:
        raise CalendarServiceError("A concrete calendar owner is required")
    return owner_id


def _base_uid(value: object) -> str:
    uid = str(value or "").strip()
    if not uid or len(uid) > MAX_UID_CHARS:
        raise CalendarServiceError("Calendar event UID is invalid")
    if "::" in uid:
        raise CalendarServiceError(
            "Calendar mutations require an exact base event UID, not an occurrence ID"
        )
    return uid


def _expected_version(value: object) -> int:
    if isinstance(value, bool):
        raise CalendarConflict("Calendar event version is invalid")
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise CalendarConflict("Calendar event version is invalid") from exc
    if version < 1:
        raise CalendarConflict("Calendar event version is invalid")
    return version


def _text(
    value: object,
    *,
    field: str,
    limit: int,
    required: bool = False,
    collapse: bool = False,
) -> str:
    normalized = str(value or "").strip()
    if collapse:
        normalized = " ".join(normalized.split())
    if required and not normalized:
        raise CalendarServiceError(f"{field} is required")
    if len(normalized) > limit:
        raise CalendarServiceError(f"{field} is too long")
    return normalized


def _optional_text(value: object | None, *, field: str, limit: int) -> str | None:
    if value is None:
        return None
    normalized = _text(value, field=field, limit=limit)
    return normalized or None


def _snapshot_string(
    value: object,
    *,
    field: str,
    limit: int,
    required: bool = False,
) -> str:
    """Validate a captured string without changing a single character."""

    if not isinstance(value, str):
        raise CalendarServiceError(f"Calendar event snapshot {field} must be a string")
    if required and not value:
        raise CalendarServiceError(f"Calendar event snapshot {field} is required")
    if len(value) > limit:
        raise CalendarServiceError(f"Calendar event snapshot {field} is too long")
    return value


def _importance(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in _IMPORTANCE:
        raise CalendarServiceError("importance must be low, normal, high, or critical")
    return normalized


def _event_type(value: object | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip().lower()
    if normalized not in _EVENT_TYPES:
        raise CalendarServiceError("Unknown calendar event type")
    return normalized


def _parse_datetime(value: object, *, field: str, all_day: bool) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        if not all_day:
            raise CalendarServiceError(f"{field} must include a time")
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw or len(raw) > 64:
            raise CalendarServiceError(f"{field} is not a valid ISO datetime")
        if not all_day and _DATE_ISO_RE.fullmatch(raw):
            raise CalendarServiceError(f"{field} must include a time")
        if all_day and _DATE_ISO_RE.fullmatch(raw):
            try:
                parsed = datetime.combine(date.fromisoformat(raw), time.min)
            except ValueError as exc:
                raise CalendarServiceError(
                    f"{field} is not a valid ISO date"
                ) from exc
        else:
            if not _TIMED_ISO_RE.fullmatch(raw):
                raise CalendarServiceError(
                    f"{field} must be an unambiguous ISO datetime"
                )
            try:
                parsed = datetime.fromisoformat(
                    raw[:-1] + "+00:00" if raw.endswith("Z") else raw
                )
            except ValueError as exc:
                raise CalendarServiceError(
                    f"{field} is not a valid ISO datetime"
                ) from exc
    else:
        raise CalendarServiceError(f"{field} must be an ISO datetime")

    aware = parsed.tzinfo is not None and parsed.utcoffset() is not None
    if all_day:
        if aware:
            raise CalendarServiceError("All-day event dates must not include a timezone")
        if parsed.time() != time.min:
            raise CalendarServiceError("All-day event dates must start at midnight")
        return parsed.replace(tzinfo=None), False
    if aware:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None), True
    return parsed.replace(tzinfo=None), False


def _datetime_range(
    dtstart: object,
    dtend: object | None,
    *,
    all_day: bool,
) -> tuple[datetime, datetime, bool]:
    if not isinstance(all_day, bool):
        raise CalendarServiceError("all_day must be a boolean")
    start, start_utc = _parse_datetime(dtstart, field="dtstart", all_day=all_day)
    if dtend is None:
        end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
        end_utc = start_utc
    else:
        end, end_utc = _parse_datetime(dtend, field="dtend", all_day=all_day)
    if start_utc != end_utc:
        raise CalendarServiceError(
            "dtstart and dtend must use the same timezone representation"
        )
    if end <= start:
        raise CalendarServiceError("dtend must be later than dtstart")
    return start, end, start_utc


def _normalize_rrule(value: object | None, *, dtstart: datetime, is_utc: bool) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if len(raw) > MAX_RRULE_CHARS or "\n" in raw or "\r" in raw:
        raise CalendarServiceError("Recurrence rule is invalid")
    if raw.upper().startswith("RRULE:"):
        raise CalendarServiceError("Store recurrence rules without the RRULE: prefix")

    parts: dict[str, str] = {}
    for token in raw.split(";"):
        if token.count("=") != 1:
            raise CalendarServiceError("Recurrence rule is invalid")
        key, value_text = (part.strip().upper() for part in token.split("=", 1))
        if (
            not _RRULE_KEY_RE.fullmatch(key)
            or key not in _RRULE_KEYS
            or key in parts
            or not value_text
            or not _RRULE_VALUE_RE.fullmatch(value_text)
        ):
            raise CalendarServiceError("Recurrence rule contains an unsupported value")
        parts[key] = value_text
    if parts.get("FREQ") not in _RRULE_FREQS:
        raise CalendarServiceError("Recurrence frequency is unsupported")
    if "COUNT" in parts and "UNTIL" in parts:
        raise CalendarServiceError("Recurrence rule cannot combine COUNT and UNTIL")
    for key, maximum in (("INTERVAL", 1_000), ("COUNT", 10_000)):
        if key in parts:
            try:
                number = int(parts[key])
            except ValueError as exc:
                raise CalendarServiceError(f"Recurrence {key} must be an integer") from exc
            if number < 1 or number > maximum:
                raise CalendarServiceError(f"Recurrence {key} is out of range")

    ordered = ["FREQ"] + sorted(key for key in parts if key != "FREQ")
    normalized = ";".join(f"{key}={parts[key]}" for key in ordered)
    rule_start = dtstart.replace(tzinfo=timezone.utc) if is_utc else dtstart
    try:
        rule = rrulestr(normalized, dtstart=rule_start)
        rule.after(rule_start, inc=True)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CalendarServiceError("Recurrence rule is invalid") from exc
    return normalized


def _normalize_exdates(value: object) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise CalendarServiceError("recurrence_exdates must be a JSON list") from exc
    else:
        decoded = value
    if not isinstance(decoded, list) or len(decoded) > 10_000:
        raise CalendarServiceError("recurrence_exdates must be a bounded list")
    normalized: list[str] = []
    for item in decoded:
        if not isinstance(item, str) or not item.strip() or len(item) > 64:
            raise CalendarServiceError("recurrence_exdates contains an invalid value")
        normalized.append(item.strip())
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _event_datetime_iso(value: datetime, *, is_utc: bool) -> str:
    if is_utc:
        return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    return value.isoformat()


def snapshot_calendar_event(event: CalendarEvent) -> dict[str, Any]:
    """Return the typed private snapshot used by encrypted action undo rows."""

    return {
        "snapshot_type": SNAPSHOT_TYPE,
        "snapshot_version": SNAPSHOT_VERSION,
        "uid": str(event.uid),
        "calendar_id": str(event.calendar_id),
        "event_version": int(event.version or 1),
        "summary": str(event.summary or ""),
        "description": str(event.description or ""),
        "location": str(event.location or ""),
        "dtstart": _event_datetime_iso(event.dtstart, is_utc=bool(event.is_utc)),
        "dtend": _event_datetime_iso(event.dtend, is_utc=bool(event.is_utc)),
        "all_day": bool(event.all_day),
        "is_utc": bool(event.is_utc),
        "rrule": str(event.rrule or ""),
        # Preserve the authority's exact JSON text.  Snapshot undo is a byte-
        # stable replay of bounded mutable strings, not an import normalizer.
        "recurrence_exdates": str(event.recurrence_exdates or ""),
        "color": event.color,
        "status": str(event.status or "confirmed"),
        "importance": str(event.importance or "normal"),
        "event_type": event.event_type,
    }


def _validate_snapshot(value: Mapping[str, Any], *, uid: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CalendarServiceError("Calendar event snapshot must be an object")
    if set(value) != _SNAPSHOT_KEYS:
        raise CalendarServiceError("Calendar event snapshot has an invalid shape")
    if (
        value.get("snapshot_type") != SNAPSHOT_TYPE
        or type(value.get("snapshot_version")) is not int
        or value.get("snapshot_version") != 1
    ):
        raise CalendarServiceError("Calendar event snapshot version is unsupported")
    if _base_uid(value.get("uid")) != uid:
        raise CalendarServiceError("Calendar event snapshot UID does not match")
    calendar_id = _text(
        value.get("calendar_id"), field="calendar_id", limit=255, required=True
    )
    if type(value.get("event_version")) is not int:
        raise CalendarServiceError("Calendar event snapshot version is invalid")
    _expected_version(value.get("event_version"))
    all_day = value.get("all_day")
    if not isinstance(all_day, bool) or not isinstance(value.get("is_utc"), bool):
        raise CalendarServiceError("Calendar event snapshot has invalid date flags")
    start, end, is_utc = _datetime_range(
        value.get("dtstart"), value.get("dtend"), all_day=all_day
    )
    if is_utc != value.get("is_utc"):
        raise CalendarServiceError("Calendar event snapshot timezone flag is inconsistent")
    status = str(value.get("status") or "").strip().lower()
    if status not in {"confirmed", "cancelled"}:
        raise CalendarServiceError("Calendar event snapshot status is invalid")
    summary = _snapshot_string(
        value.get("summary"), field="summary", limit=MAX_SUMMARY_CHARS
    )
    description = _snapshot_string(
        value.get("description"),
        field="description",
        limit=MAX_DESCRIPTION_CHARS,
    )
    location = _snapshot_string(
        value.get("location"), field="location", limit=MAX_LOCATION_CHARS
    )
    rrule = _snapshot_string(
        value.get("rrule"), field="rrule", limit=MAX_RRULE_CHARS
    )
    _normalize_rrule(rrule, dtstart=start, is_utc=is_utc)
    exdates = _snapshot_string(
        value.get("recurrence_exdates"),
        field="recurrence_exdates",
        limit=640_000,
    )
    _normalize_exdates(exdates)
    color = value.get("color")
    if color is not None:
        color = _snapshot_string(color, field="color", limit=64)
    normalized = {
        "calendar_id": calendar_id,
        "summary": summary,
        "description": description,
        "location": location,
        "dtstart": start,
        "dtend": end,
        "all_day": all_day,
        "is_utc": is_utc,
        "rrule": rrule,
        "recurrence_exdates": exdates,
        "color": color,
        "status": status,
        "importance": _importance(value.get("importance")),
        "event_type": _event_type(value.get("event_type")),
    }
    return normalized


def _structural_event_state(event: CalendarEvent) -> dict[str, Any]:
    return {
        "status": str(event.status or "confirmed"),
        "all_day": bool(event.all_day),
        "recurring": bool(str(event.rrule or "").strip()),
        "is_utc": bool(event.is_utc),
        "version": int(event.version or 1),
    }


def _owned_calendar(db, *, owner_id: str, calendar_id: object) -> CalendarCal:
    normalized_id = _text(
        calendar_id, field="calendar_id", limit=255, required=True
    )
    calendar = db.query(CalendarCal).filter(
        CalendarCal.id == normalized_id,
        CalendarCal.owner_id == owner_id,
    ).first()
    if calendar is None:
        raise CalendarNotFound("Calendar not found")
    return calendar


def upsert_caldav_calendar(
    db,
    *,
    account: Account,
    calendar_id: object,
    name: object,
    connector_account_id: object | None,
    remote_url: object,
) -> tuple[CalendarCal, bool]:
    """Create or refresh one immutable-owner CalDAV collection binding.

    ``CalendarCal.account_id`` is the connector UUID stored in preferences;
    ``owner_id`` is always Restia's immutable ``Account.id``.  A binding
    change advances ``config_version`` so already-claimed outbox work cannot
    be finalized against different credentials or a different collection.
    """

    owner_id = _owner_id(account)
    normalized_id = _text(
        calendar_id, field="calendar_id", limit=255, required=True,
    )
    normalized_name = _text(
        name, field="calendar name", limit=255, required=True, collapse=True,
    )
    normalized_remote_url = _text(
        remote_url, field="CalDAV collection URL", limit=4_000, required=True,
    )
    connector_id = _optional_text(
        connector_account_id, field="CalDAV connector account ID", limit=255,
    )
    calendar = db.query(CalendarCal).filter(
        CalendarCal.id == normalized_id,
        CalendarCal.owner_id == owner_id,
    ).first()
    if calendar is None:
        calendar = CalendarCal(
            id=normalized_id,
            owner_id=owner_id,
            owner=str(getattr(account, "username", "") or "") or None,
            name=normalized_name,
            color="#5b8abf",
            source="caldav",
            account_id=connector_id,
            caldav_base_url=normalized_remote_url,
            config_version=1,
        )
        try:
            with db.begin_nested():
                db.add(calendar)
                db.flush()
        except IntegrityError as exc:
            calendar = db.query(CalendarCal).filter(
                CalendarCal.id == normalized_id,
                CalendarCal.owner_id == owner_id,
            ).first()
            if calendar is None:
                raise CalendarConflict("CalDAV calendar binding already exists") from exc
        else:
            append_action_audit(
                db,
                owner_id=owner_id,
                action="calendar.caldav.discovered",
                entity_type="calendar",
                entity_id=calendar.id,
                reason="CalDAV calendar discovered",
                after_state={"source": "caldav", "config_version": 1},
                details={"connector_bound": bool(connector_id)},
            )
            return calendar, True

    if str(calendar.source or "").lower() != "caldav":
        raise CalendarConflict("Calendar ID belongs to a non-CalDAV calendar")
    expected_config = int(calendar.config_version or 1)
    binding_changed = (
        (calendar.account_id or None) != connector_id
        or str(calendar.caldav_base_url or "") != normalized_remote_url
    )
    values: dict[Any, Any] = {}
    if calendar.name != normalized_name:
        values[CalendarCal.name] = normalized_name
    username = str(getattr(account, "username", "") or "") or None
    if calendar.owner != username:
        values[CalendarCal.owner] = username
    if binding_changed:
        values.update({
            CalendarCal.account_id: connector_id,
            CalendarCal.caldav_base_url: normalized_remote_url,
            CalendarCal.config_version: expected_config + 1,
        })
    if not values:
        return calendar, False

    values[CalendarCal.updated_at] = utcnow_naive()
    changed = db.query(CalendarCal).filter(
        CalendarCal.id == calendar.id,
        CalendarCal.owner_id == owner_id,
        CalendarCal.config_version == expected_config,
    ).update(values, synchronize_session=False)
    if changed != 1:
        db.expire_all()
        raise CalendarConflict("CalDAV calendar binding changed; retry sync")
    db.flush()
    db.expire(calendar)
    db.refresh(calendar)
    append_action_audit(
        db,
        owner_id=owner_id,
        action="calendar.caldav.refreshed",
        entity_type="calendar",
        entity_id=calendar.id,
        reason="CalDAV calendar metadata refreshed",
        before_state={"source": "caldav", "config_version": expected_config},
        after_state={
            "source": "caldav",
            "config_version": int(calendar.config_version or 1),
        },
        details={"binding_changed": binding_changed},
    )
    return calendar, False


def ensure_default_calendar(db, *, account: Account) -> CalendarCal:
    """Return the deterministic first owned local calendar, creating one if needed."""

    owner_id = _owner_id(account)
    existing = db.query(CalendarCal).filter(
        CalendarCal.owner_id == owner_id,
        CalendarCal.source == "local",
    ).order_by(CalendarCal.created_at.asc(), CalendarCal.id.asc()).first()
    if existing is not None:
        return existing

    calendar_id = str(uuid.uuid5(_DEFAULT_CALENDAR_NAMESPACE, owner_id))
    calendar = CalendarCal(
        id=calendar_id,
        owner_id=owner_id,
        owner=str(getattr(account, "username", "") or "") or None,
        name="Personal",
        color="#5b8abf",
        source="local",
        config_version=1,
    )
    try:
        with db.begin_nested():
            db.add(calendar)
            db.flush()
    except IntegrityError:
        calendar = db.query(CalendarCal).filter(
            CalendarCal.id == calendar_id,
            CalendarCal.owner_id == owner_id,
        ).first()
        if calendar is None:
            raise CalendarConflict("Default calendar could not be established")
        return calendar

    append_action_audit(
        db,
        owner_id=owner_id,
        action="calendar.created",
        entity_type="calendar",
        entity_id=calendar.id,
        reason="Default calendar created",
        after_state={"source": "local", "config_version": 1},
        details={"default": True},
    )
    return calendar


def create_local_calendar(
    db,
    *,
    account: Account,
    name: object,
    color: object = "#5b8abf",
    calendar_id: object | None = None,
    source: object = "local",
) -> CalendarCal:
    """Create one audited local/import collection for an immutable owner."""

    owner_id = _owner_id(account)
    normalized_id = (
        _text(calendar_id, field="calendar_id", limit=255, required=True)
        if calendar_id is not None
        else str(uuid.uuid4())
    )
    normalized_name = _text(
        name, field="calendar name", limit=255, required=True, collapse=True
    )
    normalized_color = _text(color, field="calendar color", limit=255) or "#5b8abf"
    normalized_source = _text(
        source, field="calendar source", limit=32, required=True, collapse=True
    ).lower()
    if normalized_source not in {"local", "import"}:
        raise CalendarServiceError("Local calendar source is invalid")
    calendar = CalendarCal(
        id=normalized_id,
        owner_id=owner_id,
        owner=str(getattr(account, "username", "") or "") or None,
        name=normalized_name,
        color=normalized_color,
        source=normalized_source,
        config_version=1,
    )
    try:
        with db.begin_nested():
            db.add(calendar)
            db.flush()
    except IntegrityError as exc:
        raise CalendarConflict("Calendar already exists") from exc
    append_action_audit(
        db,
        owner_id=owner_id,
        action="calendar.created",
        entity_type="calendar",
        entity_id=calendar.id,
        reason="Calendar collection created",
        after_state={"source": normalized_source, "config_version": 1},
        details={"default": False},
    )
    return calendar


def update_calendar_collection(
    db,
    *,
    account: Account,
    calendar_id: object,
    expected_version: object,
    name: object | None = None,
    color: object | None = None,
) -> CalendarCal:
    """CAS-update display metadata and invalidate stale collection clients."""

    owner_id = _owner_id(account)
    calendar = _owned_calendar(db, owner_id=owner_id, calendar_id=calendar_id)
    try:
        expected = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise CalendarConflict("Calendar configuration version is invalid") from exc
    if isinstance(expected_version, bool) or expected < 1:
        raise CalendarConflict("Calendar configuration version is invalid")
    if int(calendar.config_version or 1) != expected:
        raise CalendarConflict("Calendar changed; reload it before saving")
    values: dict[Any, Any] = {}
    if name is not None:
        normalized_name = _text(
            name, field="calendar name", limit=255, required=True, collapse=True
        )
        if normalized_name != calendar.name:
            values[CalendarCal.name] = normalized_name
    if color is not None:
        normalized_color = _text(
            color, field="calendar color", limit=255, required=True
        )
        if normalized_color != calendar.color:
            values[CalendarCal.color] = normalized_color
    if not values:
        return calendar
    values.update({
        CalendarCal.config_version: expected + 1,
        CalendarCal.updated_at: utcnow_naive(),
    })
    changed = db.query(CalendarCal).filter(
        CalendarCal.id == calendar.id,
        CalendarCal.owner_id == owner_id,
        CalendarCal.config_version == expected,
    ).update(values, synchronize_session=False)
    if changed != 1:
        db.expire_all()
        raise CalendarConflict("Calendar changed; reload it before saving")
    db.flush()
    db.expire(calendar)
    db.refresh(calendar)
    append_action_audit(
        db,
        owner_id=owner_id,
        action="calendar.updated",
        entity_type="calendar",
        entity_id=calendar.id,
        reason="Calendar collection metadata updated",
        before_state={"config_version": expected},
        after_state={"config_version": expected + 1},
        details={
            "name_changed": CalendarCal.name in values,
            "color_changed": CalendarCal.color in values,
        },
    )
    return calendar


def delete_empty_calendar_collection(
    db,
    *,
    account: Account,
    calendar_id: object,
    expected_version: object,
) -> None:
    """Delete only an empty, exact collection generation with an audit row."""

    owner_id = _owner_id(account)
    normalized_id = _text(
        calendar_id, field="calendar_id", limit=255, required=True
    )
    try:
        expected = int(expected_version)
    except (TypeError, ValueError) as exc:
        raise CalendarConflict("Calendar configuration version is invalid") from exc
    if isinstance(expected_version, bool) or expected < 1:
        raise CalendarConflict("Calendar configuration version is invalid")
    calendar = db.query(CalendarCal).filter(
        CalendarCal.id == normalized_id,
        CalendarCal.owner_id == owner_id,
    ).with_for_update().first()
    if calendar is None:
        raise CalendarNotFound("Calendar not found")
    if int(calendar.config_version or 1) != expected:
        raise CalendarConflict("Calendar changed; reload it before deleting")
    if db.query(CalendarEvent.uid).filter(
        CalendarEvent.calendar_id == calendar.id,
        CalendarEvent.owner_id == owner_id,
    ).first() is not None:
        raise CalendarConflict("Calendars with event history cannot be deleted yet")
    append_action_audit(
        db,
        owner_id=owner_id,
        action="calendar.deleted",
        entity_type="calendar",
        entity_id=calendar.id,
        reason="Empty calendar collection deleted",
        before_state={
            "source": str(calendar.source or "local"),
            "config_version": expected,
        },
        after_state={"deleted": True},
        details={"empty": True},
    )
    db.delete(calendar)
    db.flush()


def _owned_event(db, *, owner_id: str, uid: object) -> tuple[CalendarEvent, CalendarCal]:
    event_uid = _base_uid(uid)
    event = db.query(CalendarEvent).filter(
        CalendarEvent.uid == event_uid,
        CalendarEvent.owner_id == owner_id,
    ).first()
    if event is None:
        raise CalendarNotFound("Calendar event not found")
    calendar = _owned_calendar(
        db, owner_id=owner_id, calendar_id=event.calendar_id
    )
    return event, calendar


def _check_version(event: CalendarEvent, expected_version: object) -> int:
    expected = _expected_version(expected_version)
    if int(event.version or 1) != expected:
        raise CalendarConflict("Calendar event changed; reload it before saving")
    return expected


def _cas_event(
    db,
    *,
    event: CalendarEvent,
    owner_id: str,
    expected_version: int,
    values: Mapping[Any, Any],
) -> CalendarEvent:
    now = utcnow_naive()
    updates = dict(values)
    updates[CalendarEvent.version] = expected_version + 1
    updates[CalendarEvent.updated_at] = now
    changed = db.query(CalendarEvent).filter(
        CalendarEvent.uid == event.uid,
        CalendarEvent.owner_id == owner_id,
        CalendarEvent.version == expected_version,
    ).update(updates, synchronize_session=False)
    if changed != 1:
        db.expire_all()
        raise CalendarConflict("Calendar event changed; reload it before saving")
    db.flush()
    db.expire(event)
    db.refresh(event)
    return event


def _projection_values(event: CalendarEvent) -> dict[str, Any]:
    return {
        "title": str(event.summary or "") or "Untitled event",
        "summary": str(event.description or ""),
        "status": "cancelled" if event.status == "cancelled" else "active",
        "properties": {
            "calendar_id": event.calendar_id,
            "location": str(event.location or ""),
            "dtstart": _event_datetime_iso(event.dtstart, is_utc=bool(event.is_utc)),
            "dtend": _event_datetime_iso(event.dtend, is_utc=bool(event.is_utc)),
            "all_day": bool(event.all_day),
            "is_utc": bool(event.is_utc),
            "rrule": str(event.rrule or ""),
            "importance": str(event.importance or "normal"),
            "event_type": event.event_type,
            "event_version": int(event.version or 1),
        },
        "provenance": {"authority": "calendar_event"},
        "occurred_at": event.dtstart,
        "due_at": event.dtend,
    }


def _project_event(db, *, account: Account, event: CalendarEvent) -> LifeEntity:
    values = _projection_values(event)
    entity = db.query(LifeEntity).filter(
        LifeEntity.owner_id == account.id,
        LifeEntity.entity_type == "event",
        LifeEntity.domain_ref_type == "calendar_event",
        LifeEntity.domain_ref_id == event.uid,
    ).first()
    try:
        if entity is None:
            entity, _created = create_life_entity(
                db,
                account=account,
                entity_type="event",
                domain_ref_type="calendar_event",
                domain_ref_id=event.uid,
                idempotency_key=f"calendar-event:{account.id}:{event.uid}",
                sensitivity="private",
                confidence=100,
                reason="Calendar event projected",
                **values,
            )
            return entity
        if entity.deleted_at is not None:
            raise CalendarConflict("Calendar event projection was deleted")
        changes = {
            key: value for key, value in values.items()
            if getattr(entity, key) != value
        }
        if changes:
            entity = update_life_entity(
                db,
                owner_id=account.id,
                entity_id=entity.id,
                expected_version=int(entity.version or 1),
                changes=changes,
                reason="Calendar event projection updated",
            )
        return entity
    except LifeGraphError as exc:
        raise CalendarServiceError("Calendar event projection failed") from exc


def _linked_entities(
    db,
    *,
    account: Account,
    graph_entity: LifeEntity,
    linked_entity_ids: Iterable[object],
) -> tuple[EntityLink, ...]:
    if isinstance(linked_entity_ids, (str, bytes)):
        raise CalendarServiceError("linked_entity_ids must be a list")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in linked_entity_ids:
        entity_id = str(raw or "").strip()
        if not entity_id or len(entity_id) > 255:
            raise CalendarServiceError("linked_entity_ids contains an invalid ID")
        if entity_id not in seen:
            seen.add(entity_id)
            normalized.append(entity_id)
    if len(normalized) > MAX_LINKS:
        raise CalendarServiceError("Too many calendar entity links")
    if not normalized:
        return ()
    targets = db.query(LifeEntity).filter(
        LifeEntity.owner_id == account.id,
        LifeEntity.id.in_(normalized),
        LifeEntity.deleted_at.is_(None),
        LifeEntity.entity_type.in_(_LINK_TARGET_TYPES),
    ).all()
    target_by_id = {row.id: row for row in targets}
    if set(normalized) != set(target_by_id):
        raise CalendarNotFound("Linked Life Graph entity not found")

    created_links: list[EntityLink] = []
    for entity_id in normalized:
        if entity_id == graph_entity.id:
            raise CalendarServiceError("A calendar event cannot link to itself")
        link, created = create_entity_link(
            db,
            account=account,
            source_id=graph_entity.id,
            relation="related_to",
            target_id=entity_id,
            metadata={},
            provenance={"authority": "calendar_event"},
            confidence=100,
            sensitivity="private",
            reason="Calendar event linked",
        )
        if created:
            created_links.append(link)
    return tuple(created_links)


def _delivery_payload(event: CalendarEvent) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "event": {
            **snapshot_calendar_event(event),
            "origin": event.origin,
            "remote_href": event.remote_href,
            "remote_etag": event.remote_etag,
        },
    }


def _delivery_key(
    *,
    owner_id: str,
    event: CalendarEvent,
    operation: str,
    idempotency_key: object | None,
    proposal_id: object | None,
) -> str:
    explicit = str(idempotency_key or "").strip()
    proposal = str(proposal_id or "").strip()
    if explicit:
        material = f"explicit:{explicit}"
    elif proposal:
        material = f"proposal:{proposal}:{operation}"
    else:
        material = f"event:{event.uid}:{int(event.version or 1)}:{operation}"
    return private_digest("calendar-delivery-v1", f"{owner_id}:{material}")


def _coalesce_open_create_delivery(
    db,
    *,
    account: Account,
    calendar: CalendarCal,
    event: CalendarEvent,
    operation: str,
) -> CalendarDelivery | None:
    """Fold a later mutation into an unclaimed remote create.

    A remote object cannot be updated or deleted before it has been created.
    Pending/retry create work is therefore mutable only while no worker owns a
    lease.  The version-fenced UPDATE closes the read/claim race; if it loses,
    the caller falls through and appends a FIFO update/delete behind the worker.
    """

    if operation not in {"update", "delete"}:
        return None
    pending_create = db.query(CalendarDelivery).filter(
        CalendarDelivery.owner_id == account.id,
        CalendarDelivery.event_uid == event.uid,
        CalendarDelivery.operation == "create",
        CalendarDelivery.state.in_(("pending", "retry")),
        CalendarDelivery.claim_token.is_(None),
        CalendarDelivery.claimed_at.is_(None),
        CalendarDelivery.lease_expires_at.is_(None),
    ).order_by(
        CalendarDelivery.created_at.asc(), CalendarDelivery.id.asc()
    ).first()
    if pending_create is None:
        return None

    expected = int(pending_create.version or 1)
    now = utcnow_naive()
    values: dict[Any, Any] = {
        CalendarDelivery.payload: _delivery_payload(event),
        CalendarDelivery.expected_event_version: int(event.version or 1),
        CalendarDelivery.expected_config_version: int(calendar.config_version or 1),
        CalendarDelivery.last_error_code: None,
        CalendarDelivery.version: expected + 1,
        CalendarDelivery.updated_at: now,
    }
    if operation == "delete":
        values.update({
            CalendarDelivery.state: "cancelled",
            CalendarDelivery.next_attempt_at: None,
            CalendarDelivery.completed_at: now,
        })
    else:
        values.update({
            CalendarDelivery.state: "pending",
            CalendarDelivery.next_attempt_at: now,
            CalendarDelivery.completed_at: None,
        })
    changed = db.query(CalendarDelivery).filter(
        CalendarDelivery.id == pending_create.id,
        CalendarDelivery.owner_id == account.id,
        CalendarDelivery.version == expected,
        CalendarDelivery.state.in_(("pending", "retry")),
        CalendarDelivery.claim_token.is_(None),
        CalendarDelivery.claimed_at.is_(None),
        CalendarDelivery.lease_expires_at.is_(None),
    ).update(values, synchronize_session=False)
    if changed != 1:
        db.expire(pending_create)
        return None
    db.flush()
    db.expire(pending_create)
    db.refresh(pending_create)
    return pending_create


def _enqueue_delivery(
    db,
    *,
    account: Account,
    calendar: CalendarCal,
    event: CalendarEvent,
    operation: str,
    idempotency_key: object | None,
    proposal_id: object | None,
) -> CalendarDelivery | None:
    if str(calendar.source or "local").lower() != "caldav":
        return None
    proposal = str(proposal_id or "").strip() or None
    if proposal is not None and len(proposal) > 36:
        raise CalendarServiceError("proposal_id is invalid")
    coalesced = _coalesce_open_create_delivery(
        db,
        account=account,
        calendar=calendar,
        event=event,
        operation=operation,
    )
    if coalesced is not None:
        return coalesced
    key = _delivery_key(
        owner_id=account.id,
        event=event,
        operation=operation,
        idempotency_key=idempotency_key,
        proposal_id=proposal,
    )
    payload = _delivery_payload(event)
    existing = db.query(CalendarDelivery).filter(
        CalendarDelivery.owner_id == account.id,
        CalendarDelivery.idempotency_key == key,
    ).first()
    if existing is not None:
        matches = all((
            existing.calendar_id == calendar.id,
            existing.event_uid == event.uid,
            existing.proposal_id == proposal,
            existing.operation == operation,
            dict(existing.payload or {}) == payload,
            int(existing.expected_event_version) == int(event.version or 1),
            int(existing.expected_config_version) == int(calendar.config_version or 1),
        ))
        if not matches:
            raise CalendarConflict(
                "Calendar delivery idempotency key was already used"
            )
        return existing

    delivery = CalendarDelivery(
        id=str(uuid.uuid4()),
        owner_id=account.id,
        calendar_id=calendar.id,
        event_uid=event.uid,
        proposal_id=proposal,
        operation=operation,
        idempotency_key=key,
        payload=payload,
        expected_event_version=int(event.version or 1),
        expected_config_version=int(calendar.config_version or 1),
        state="pending",
        attempts=0,
        next_attempt_at=utcnow_naive(),
        version=1,
    )
    try:
        with db.begin_nested():
            db.add(delivery)
            db.flush()
    except IntegrityError:
        existing = db.query(CalendarDelivery).filter(
            CalendarDelivery.owner_id == account.id,
            CalendarDelivery.idempotency_key == key,
        ).first()
        if existing is None:
            raise
        if not all((
            existing.calendar_id == calendar.id,
            existing.event_uid == event.uid,
            existing.proposal_id == proposal,
            existing.operation == operation,
            dict(existing.payload or {}) == payload,
            int(existing.expected_event_version) == int(event.version or 1),
            int(existing.expected_config_version) == int(calendar.config_version or 1),
        )):
            raise CalendarConflict(
                "Calendar delivery idempotency key was already used"
            )
        return existing
    return delivery


def _delivery_operation(event: CalendarEvent, *, creating: bool = False) -> str:
    if event.status == "cancelled":
        return "delete"
    if creating:
        return "create"
    return "update"


def _open_delivery_for_event(db, *, owner_id: str, uid: str) -> CalendarDelivery | None:
    return db.query(CalendarDelivery).filter(
        CalendarDelivery.owner_id == owner_id,
        CalendarDelivery.event_uid == uid,
        CalendarDelivery.state.notin_(("completed", "cancelled")),
    ).order_by(
        CalendarDelivery.created_at.asc(), CalendarDelivery.id.asc(),
    ).first()


def _guard_remote_overwrite(db, *, event: CalendarEvent, owner_id: str) -> None:
    if str(event.caldav_sync_pending or "").strip():
        raise CalendarRemoteWritePending(
            "Calendar event has a legacy local generation awaiting delivery"
        )
    if _open_delivery_for_event(db, owner_id=owner_id, uid=str(event.uid)) is not None:
        raise CalendarRemoteWritePending(
            "Calendar event has a local generation awaiting delivery"
        )


def _remote_rrule(value: object | None, *, dtstart: datetime, is_utc: bool) -> str:
    """Validate a bounded server-authored RFC recurrence without narrowing it.

    Locally-authored rules use Restia's deliberately small allowlist.  CalDAV
    servers can legitimately return additional RFC 5545 selectors (for
    example BYYEARDAY), so pull validates with dateutil while preserving the
    server's normalized text instead of silently discarding valid events.
    """

    raw = str(value or "").strip()
    if not raw:
        return ""
    if (
        len(raw) > MAX_RRULE_CHARS
        or "\n" in raw
        or "\r" in raw
        or not raw.upper().startswith("FREQ=")
    ):
        raise CalendarServiceError("Remote recurrence rule is invalid")
    rule_start = dtstart.replace(tzinfo=timezone.utc) if is_utc else dtstart
    try:
        # Parsing is bounded by the text limit. Do not seek for an occurrence
        # here: a syntactically valid but impossible server-authored selector
        # could otherwise make pull iterate years while holding its transaction.
        rrulestr(raw, dtstart=rule_start)
    except (TypeError, ValueError, OverflowError) as exc:
        raise CalendarServiceError("Remote recurrence rule is invalid") from exc
    return raw


def _remote_event_values(
    *,
    summary: object,
    description: object,
    location: object,
    dtstart: object,
    dtend: object | None,
    all_day: bool,
    is_utc: bool,
    rrule: object,
    recurrence_exdates: object,
    status: object,
    remote_href: object | None,
    remote_etag: object | None,
) -> dict[Any, Any]:
    if not isinstance(is_utc, bool):
        raise CalendarServiceError("is_utc must be a boolean")
    start, end, parsed_is_utc = _datetime_range(
        dtstart, dtend, all_day=all_day,
    )
    if parsed_is_utc != is_utc:
        raise CalendarServiceError("Remote calendar timezone flag is inconsistent")
    remote_summary = _snapshot_string(
        str(summary or ""), field="summary", limit=MAX_SUMMARY_CHARS,
    )
    remote_description = _snapshot_string(
        str(description or ""), field="description", limit=MAX_DESCRIPTION_CHARS,
    )
    remote_location = _snapshot_string(
        str(location or ""), field="location", limit=MAX_LOCATION_CHARS,
    )
    normalized_status = str(status or "confirmed").strip().lower()
    if normalized_status not in {"confirmed", "cancelled"}:
        raise CalendarServiceError("Remote calendar event status is invalid")
    return {
        CalendarEvent.summary: remote_summary,
        CalendarEvent.description: remote_description,
        CalendarEvent.location: remote_location,
        CalendarEvent.dtstart: start,
        CalendarEvent.dtend: end,
        CalendarEvent.all_day: all_day,
        CalendarEvent.is_utc: is_utc,
        CalendarEvent.rrule: _remote_rrule(
            rrule, dtstart=start, is_utc=is_utc,
        ),
        CalendarEvent.recurrence_exdates: _normalize_exdates(recurrence_exdates),
        CalendarEvent.status: normalized_status,
        CalendarEvent.origin: "caldav",
        CalendarEvent.remote_href: _optional_text(
            remote_href, field="remote_href", limit=4_000,
        ),
        CalendarEvent.remote_etag: _optional_text(
            remote_etag, field="remote_etag", limit=1_000,
        ),
        CalendarEvent.caldav_sync_pending: None,
    }


def _result(
    *,
    event: CalendarEvent,
    graph_entity: LifeEntity,
    delivery: CalendarDelivery | None,
    links_created: tuple[EntityLink, ...],
    created: bool,
    before_snapshot: dict[str, Any] | None,
) -> CalendarMutationResult:
    after = snapshot_calendar_event(event)
    return CalendarMutationResult(
        event=event,
        graph_entity=graph_entity,
        delivery=delivery,
        links_created=links_created,
        created=created,
        event_version=int(event.version or 1),
        graph_version=int(graph_entity.version or 1),
        delivery_version=(int(delivery.version or 1) if delivery is not None else None),
        before_snapshot=before_snapshot,
        after_snapshot=after,
    )


def _create_values(
    *,
    summary: object,
    description: object,
    location: object,
    dtstart: object,
    dtend: object | None,
    all_day: bool,
    rrule: object,
    color: object | None,
    importance: object,
    event_type: object | None,
) -> dict[str, Any]:
    start, end, is_utc = _datetime_range(dtstart, dtend, all_day=all_day)
    return {
        "summary": _text(
            summary, field="summary", limit=MAX_SUMMARY_CHARS,
            required=True, collapse=True,
        ),
        "description": _text(
            description, field="description", limit=MAX_DESCRIPTION_CHARS,
        ),
        "location": _text(location, field="location", limit=MAX_LOCATION_CHARS),
        "dtstart": start,
        "dtend": end,
        "all_day": all_day,
        "is_utc": is_utc,
        "rrule": _normalize_rrule(rrule, dtstart=start, is_utc=is_utc),
        "recurrence_exdates": "",
        "color": _optional_text(color, field="color", limit=64),
        "status": "confirmed",
        "importance": _importance(importance),
        "event_type": _event_type(event_type),
    }


def _event_matches_create(
    event: CalendarEvent, *, calendar_id: str, values: Mapping[str, Any]
) -> bool:
    return event.calendar_id == calendar_id and all(
        getattr(event, field) == value for field, value in values.items()
    ) and int(event.version or 1) == 1


def create_calendar_event(
    db,
    *,
    account: Account,
    summary: object,
    dtstart: object,
    dtend: object | None = None,
    all_day: bool = False,
    calendar_id: object | None = None,
    uid: object | None = None,
    description: object = "",
    location: object = "",
    rrule: object = "",
    color: object | None = None,
    importance: object = "normal",
    event_type: object | None = None,
    linked_entity_ids: Iterable[object] = (),
    idempotency_key: object | None = None,
    proposal_id: object | None = None,
) -> CalendarMutationResult:
    owner_id = _owner_id(account)
    values = _create_values(
        summary=summary,
        description=description,
        location=location,
        dtstart=dtstart,
        dtend=dtend,
        all_day=all_day,
        rrule=rrule,
        color=color,
        importance=importance,
        event_type=event_type,
    )
    calendar = (
        _owned_calendar(db, owner_id=owner_id, calendar_id=calendar_id)
        if calendar_id is not None
        else ensure_default_calendar(db, account=account)
    )
    key = str(idempotency_key or "").strip()
    if uid is None:
        if key:
            digest = private_digest("calendar-event-create-v1", f"{owner_id}:{key}")
            event_uid = str(uuid.uuid5(_IDEMPOTENT_EVENT_NAMESPACE, digest))
        else:
            event_uid = str(uuid.uuid4())
    else:
        event_uid = _base_uid(uid)

    event = db.query(CalendarEvent).filter(
        CalendarEvent.uid == event_uid,
        CalendarEvent.owner_id == owner_id,
    ).first()
    created = False
    if event is not None:
        if not _event_matches_create(event, calendar_id=calendar.id, values=values):
            raise CalendarConflict(
                "Calendar event UID or idempotency key was already used"
            )
    else:
        event = CalendarEvent(
            uid=event_uid,
            owner_id=owner_id,
            calendar_id=calendar.id,
            origin="local",
            caldav_sync_pending=None,
            version=1,
            **values,
        )
        try:
            with db.begin_nested():
                db.add(event)
                db.flush()
        except IntegrityError as exc:
            existing = db.query(CalendarEvent).filter(
                CalendarEvent.uid == event_uid,
                CalendarEvent.owner_id == owner_id,
            ).first()
            if existing is None or not _event_matches_create(
                existing, calendar_id=calendar.id, values=values
            ):
                raise CalendarConflict(
                    "Calendar event UID or idempotency key was already used"
                ) from exc
            event = existing
        else:
            created = True

    graph_entity = _project_event(db, account=account, event=event)
    links_created = _linked_entities(
        db,
        account=account,
        graph_entity=graph_entity,
        linked_entity_ids=linked_entity_ids,
    )
    delivery = _enqueue_delivery(
        db,
        account=account,
        calendar=calendar,
        event=event,
        operation=_delivery_operation(event, creating=True),
        idempotency_key=idempotency_key,
        proposal_id=proposal_id,
    )
    if created:
        append_action_audit(
            db,
            owner_id=owner_id,
            action="calendar.event.created",
            entity_type="calendar_event",
            entity_id=event.uid,
            reason="Calendar event created",
            after_state=_structural_event_state(event),
            details={
                "delivery_enqueued": delivery is not None,
                "links_created": len(links_created),
            },
            idempotency_ref=key or None,
            reversible=True,
            undo_ref=f"calendar-event:{event.uid}:1",
        )
    return _result(
        event=event,
        graph_entity=graph_entity,
        delivery=delivery,
        links_created=links_created,
        created=created,
        before_snapshot=None if created else snapshot_calendar_event(event),
    )


def _metadata_changes(changes: Mapping[str, Any]) -> dict[Any, Any]:
    if not isinstance(changes, Mapping):
        raise CalendarServiceError("changes must be an object")
    allowed = {"summary", "description", "location", "color", "importance", "event_type"}
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise CalendarServiceError(
            "Unsupported calendar fields; use reschedule/cancel operations: "
            + ", ".join(unknown)
        )
    normalized: dict[Any, Any] = {}
    for field, value in changes.items():
        if field == "summary":
            normalized[CalendarEvent.summary] = _text(
                value, field="summary", limit=MAX_SUMMARY_CHARS,
                required=True, collapse=True,
            )
        elif field == "description":
            normalized[CalendarEvent.description] = _text(
                value, field="description", limit=MAX_DESCRIPTION_CHARS,
            )
        elif field == "location":
            normalized[CalendarEvent.location] = _text(
                value, field="location", limit=MAX_LOCATION_CHARS,
            )
        elif field == "color":
            normalized[CalendarEvent.color] = _optional_text(
                value, field="color", limit=64
            )
        elif field == "importance":
            normalized[CalendarEvent.importance] = _importance(value)
        elif field == "event_type":
            normalized[CalendarEvent.event_type] = _event_type(value)
    return normalized


def _mutate_existing(
    db,
    *,
    account: Account,
    event: CalendarEvent,
    calendar: CalendarCal,
    expected_version: object,
    values: Mapping[Any, Any],
    action: str,
    reason: str,
    changed_field_names: Iterable[str],
    linked_entity_ids: Iterable[object] = (),
    idempotency_key: object | None = None,
    proposal_id: object | None = None,
) -> CalendarMutationResult:
    expected = _check_version(event, expected_version)
    before = snapshot_calendar_event(event)
    effective = {
        column: value for column, value in values.items()
        if getattr(event, column.key) != value
    }
    if effective:
        _cas_event(
            db,
            event=event,
            owner_id=account.id,
            expected_version=expected,
            values=effective,
        )
    graph_entity = _project_event(db, account=account, event=event)
    links_created = _linked_entities(
        db,
        account=account,
        graph_entity=graph_entity,
        linked_entity_ids=linked_entity_ids,
    )
    delivery = None
    if effective:
        delivery = _enqueue_delivery(
            db,
            account=account,
            calendar=calendar,
            event=event,
            operation=_delivery_operation(event),
            idempotency_key=idempotency_key,
            proposal_id=proposal_id,
        )
        append_action_audit(
            db,
            owner_id=account.id,
            action=action,
            entity_type="calendar_event",
            entity_id=event.uid,
            reason=reason,
            before_state={
                "status": before["status"],
                "all_day": before["all_day"],
                "recurring": bool(before["rrule"]),
                "is_utc": before["is_utc"],
                "version": before["event_version"],
            },
            after_state=_structural_event_state(event),
            details={
                "fields": sorted(set(changed_field_names)),
                "delivery_enqueued": delivery is not None,
                "links_created": len(links_created),
            },
            idempotency_ref=idempotency_key,
            reversible=True,
            undo_ref=f"calendar-event:{event.uid}:{int(event.version or 1)}",
        )
    return _result(
        event=event,
        graph_entity=graph_entity,
        delivery=delivery,
        links_created=links_created,
        created=False,
        before_snapshot=before,
    )


def update_calendar_event(
    db,
    *,
    account: Account,
    uid: object,
    expected_version: int,
    changes: Mapping[str, Any],
    linked_entity_ids: Iterable[object] = (),
    idempotency_key: object | None = None,
    proposal_id: object | None = None,
) -> CalendarMutationResult:
    event, calendar = _owned_event(db, owner_id=_owner_id(account), uid=uid)
    normalized = _metadata_changes(changes)
    return _mutate_existing(
        db,
        account=account,
        event=event,
        calendar=calendar,
        expected_version=expected_version,
        values=normalized,
        action="calendar.event.updated",
        reason="Calendar event updated",
        changed_field_names=(column.key for column in normalized),
        linked_entity_ids=linked_entity_ids,
        idempotency_key=idempotency_key,
        proposal_id=proposal_id,
    )


def reschedule_calendar_event(
    db,
    *,
    account: Account,
    uid: object,
    expected_version: int,
    dtstart: object,
    dtend: object | None = None,
    all_day: object = _UNSET,
    rrule: object = _UNSET,
    idempotency_key: object | None = None,
    proposal_id: object | None = None,
) -> CalendarMutationResult:
    event, calendar = _owned_event(db, owner_id=_owner_id(account), uid=uid)
    normalized_all_day = bool(event.all_day) if all_day is _UNSET else all_day
    if not isinstance(normalized_all_day, bool):
        raise CalendarServiceError("all_day must be a boolean")
    start, end, is_utc = _datetime_range(
        dtstart, dtend, all_day=normalized_all_day
    )
    normalized_rrule = (
        str(event.rrule or "")
        if rrule is _UNSET
        else _normalize_rrule(rrule, dtstart=start, is_utc=is_utc)
    )
    if rrule is _UNSET and normalized_rrule:
        normalized_rrule = _normalize_rrule(
            normalized_rrule, dtstart=start, is_utc=is_utc
        )
    values = {
        CalendarEvent.dtstart: start,
        CalendarEvent.dtend: end,
        CalendarEvent.all_day: normalized_all_day,
        CalendarEvent.is_utc: is_utc,
        CalendarEvent.rrule: normalized_rrule,
    }
    return _mutate_existing(
        db,
        account=account,
        event=event,
        calendar=calendar,
        expected_version=expected_version,
        values=values,
        action="calendar.event.rescheduled",
        reason="Calendar event rescheduled",
        changed_field_names=(column.key for column in values),
        idempotency_key=idempotency_key,
        proposal_id=proposal_id,
    )


def cancel_calendar_event(
    db,
    *,
    account: Account,
    uid: object,
    expected_version: int,
    idempotency_key: object | None = None,
    proposal_id: object | None = None,
) -> CalendarMutationResult:
    event, calendar = _owned_event(db, owner_id=_owner_id(account), uid=uid)
    return _mutate_existing(
        db,
        account=account,
        event=event,
        calendar=calendar,
        expected_version=expected_version,
        values={CalendarEvent.status: "cancelled"},
        action="calendar.event.cancelled",
        reason="Calendar event cancelled",
        changed_field_names=("status",),
        idempotency_key=idempotency_key,
        proposal_id=proposal_id,
    )


def exclude_calendar_occurrence(
    db,
    *,
    account: Account,
    uid: object,
    expected_version: int,
    occurrence_key: object,
    idempotency_key: object | None = None,
    proposal_id: object | None = None,
) -> CalendarMutationResult:
    """Exclude one occurrence from an owned recurring series with CAS.

    Occurrence identifiers are deliberately reduced to the date/date-time key
    emitted by the calendar read model.  The base event remains the sole
    mutable authority, so the exclusion, Life Graph projection, audit, and any
    CalDAV delivery are committed in the caller's transaction.
    """

    event, calendar = _owned_event(db, owner_id=_owner_id(account), uid=uid)
    if not str(event.rrule or "").strip():
        raise CalendarServiceError("Calendar event is not recurring")
    key = str(occurrence_key or "").strip()
    expected_pattern = _DATE_ISO_RE if bool(event.all_day) else re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$"
    )
    if len(key) > 16 or expected_pattern.fullmatch(key) is None:
        raise CalendarServiceError("Recurring occurrence key is invalid")
    try:
        datetime.fromisoformat(key if "T" in key else f"{key}T00:00")
    except ValueError as exc:
        raise CalendarServiceError("Recurring occurrence key is invalid") from exc

    current = _normalize_exdates(event.recurrence_exdates)
    values = json.loads(current) if current else []
    if key not in values:
        values.append(key)
    normalized = json.dumps(
        sorted(set(values)), ensure_ascii=False, separators=(",", ":")
    )
    return _mutate_existing(
        db,
        account=account,
        event=event,
        calendar=calendar,
        expected_version=expected_version,
        values={CalendarEvent.recurrence_exdates: normalized},
        action="calendar.event.occurrence_excluded",
        reason="Calendar occurrence excluded",
        changed_field_names=("recurrence_exdates",),
        idempotency_key=idempotency_key,
        proposal_id=proposal_id,
    )


def restore_calendar_event(
    db,
    *,
    account: Account,
    uid: object,
    expected_version: int,
    idempotency_key: object | None = None,
    proposal_id: object | None = None,
) -> CalendarMutationResult:
    event, calendar = _owned_event(db, owner_id=_owner_id(account), uid=uid)
    return _mutate_existing(
        db,
        account=account,
        event=event,
        calendar=calendar,
        expected_version=expected_version,
        values={CalendarEvent.status: "confirmed"},
        action="calendar.event.restored",
        reason="Calendar event restored",
        changed_field_names=("status",),
        idempotency_key=idempotency_key,
        proposal_id=proposal_id,
    )


def restore_calendar_event_snapshot(
    db,
    *,
    account: Account,
    uid: object,
    expected_version: int,
    snapshot: Mapping[str, Any],
    idempotency_key: object | None = None,
    proposal_id: object | None = None,
) -> CalendarMutationResult:
    event_uid = _base_uid(uid)
    event, calendar = _owned_event(
        db, owner_id=_owner_id(account), uid=event_uid
    )
    normalized = _validate_snapshot(snapshot, uid=event_uid)
    if normalized.pop("calendar_id") != event.calendar_id:
        raise CalendarServiceError("Calendar event snapshot cannot move an event")
    column_by_name = {
        "summary": CalendarEvent.summary,
        "description": CalendarEvent.description,
        "location": CalendarEvent.location,
        "dtstart": CalendarEvent.dtstart,
        "dtend": CalendarEvent.dtend,
        "all_day": CalendarEvent.all_day,
        "is_utc": CalendarEvent.is_utc,
        "rrule": CalendarEvent.rrule,
        "recurrence_exdates": CalendarEvent.recurrence_exdates,
        "color": CalendarEvent.color,
        "status": CalendarEvent.status,
        "importance": CalendarEvent.importance,
        "event_type": CalendarEvent.event_type,
    }
    values = {column_by_name[name]: value for name, value in normalized.items()}
    return _mutate_existing(
        db,
        account=account,
        event=event,
        calendar=calendar,
        expected_version=expected_version,
        values=values,
        action="calendar.event.snapshot_restored",
        reason="Calendar event snapshot restored",
        changed_field_names=normalized.keys(),
        idempotency_key=idempotency_key,
        proposal_id=proposal_id,
    )


def ingest_remote_calendar_event(
    db,
    *,
    account: Account,
    calendar_id: object,
    uid: object,
    expected_version: int | None,
    summary: object,
    description: object = "",
    location: object = "",
    dtstart: object,
    dtend: object | None = None,
    all_day: bool = False,
    is_utc: bool = False,
    rrule: object = "",
    recurrence_exdates: object = None,
    status: object = "confirmed",
    remote_href: object | None = None,
    remote_etag: object | None = None,
) -> CalendarMutationResult:
    """Owner-scoped CalDAV pull upsert with no echo delivery.

    Existing rows require the exact version observed by the caller.  Pull may
    never overwrite a legacy pending marker or any open durable delivery; the
    locally committed generation must reach the connector first.  Reappearing
    remote events are restored through the same CAS, projection, and audit.
    """

    owner_id = _owner_id(account)
    event_uid = _base_uid(uid)
    calendar = _owned_calendar(
        db, owner_id=owner_id, calendar_id=calendar_id,
    )
    if str(calendar.source or "").lower() != "caldav":
        raise CalendarServiceError("Remote ingestion requires a CalDAV calendar")
    values = _remote_event_values(
        summary=summary,
        description=description,
        location=location,
        dtstart=dtstart,
        dtend=dtend,
        all_day=all_day,
        is_utc=is_utc,
        rrule=rrule,
        recurrence_exdates=recurrence_exdates,
        status=status,
        remote_href=remote_href,
        remote_etag=remote_etag,
    )
    event = db.query(CalendarEvent).filter(
        CalendarEvent.uid == event_uid,
        CalendarEvent.owner_id == owner_id,
    ).first()
    created = False
    before: dict[str, Any] | None = None
    changed_fields: list[str] = []
    if event is None:
        if expected_version is not None:
            raise CalendarConflict("Remote calendar event disappeared; retry sync")
        event = CalendarEvent(
            uid=event_uid,
            owner_id=owner_id,
            calendar_id=calendar.id,
            color=None,
            importance="normal",
            event_type=None,
            version=1,
            **{column.key: value for column, value in values.items()},
        )
        try:
            with db.begin_nested():
                db.add(event)
                db.flush()
        except IntegrityError as exc:
            raise CalendarConflict("Remote calendar event changed; retry sync") from exc
        created = True
        changed_fields = sorted(column.key for column in values)
    else:
        if event.calendar_id != calendar.id:
            raise CalendarConflict(
                "Remote event UID already belongs to another owned calendar"
            )
        if expected_version is None:
            raise CalendarConflict("Remote calendar event changed; retry sync")
        expected = _check_version(event, expected_version)
        _guard_remote_overwrite(db, event=event, owner_id=owner_id)
        before = snapshot_calendar_event(event)
        effective = {
            column: value for column, value in values.items()
            if getattr(event, column.key) != value
        }
        changed_fields = sorted(column.key for column in effective)
        if effective:
            event = _cas_event(
                db,
                event=event,
                owner_id=owner_id,
                expected_version=expected,
                values=effective,
            )

    graph_entity = _project_event(db, account=account, event=event)
    if created or changed_fields:
        append_action_audit(
            db,
            owner_id=owner_id,
            action=(
                "calendar.event.remote_ingested"
                if created else "calendar.event.remote_refreshed"
            ),
            entity_type="calendar_event",
            entity_id=event.uid,
            reason=(
                "CalDAV event ingested"
                if created else "CalDAV event refreshed"
            ),
            before_state=(
                {
                    "status": before["status"],
                    "all_day": before["all_day"],
                    "recurring": bool(before["rrule"]),
                    "is_utc": before["is_utc"],
                    "version": before["event_version"],
                }
                if before is not None else None
            ),
            after_state=_structural_event_state(event),
            details={
                "fields": changed_fields,
                "delivery_enqueued": False,
                "remote_ingestion": True,
            },
        )
    return _result(
        event=event,
        graph_entity=graph_entity,
        delivery=None,
        links_created=(),
        created=created,
        before_snapshot=before,
    )


def cancel_missing_remote_calendar_event(
    db,
    *,
    account: Account,
    calendar_id: object,
    uid: object,
    expected_version: int,
) -> CalendarMutationResult:
    """Soft-cancel a vanished remote event without enqueueing a remote echo."""

    owner_id = _owner_id(account)
    calendar = _owned_calendar(
        db, owner_id=owner_id, calendar_id=calendar_id,
    )
    if str(calendar.source or "").lower() != "caldav":
        raise CalendarServiceError("Remote cancellation requires a CalDAV calendar")
    event, event_calendar = _owned_event(db, owner_id=owner_id, uid=uid)
    if event_calendar.id != calendar.id or str(event.origin or "") != "caldav":
        raise CalendarConflict("Only a pulled event can disappear remotely")
    expected = _check_version(event, expected_version)
    _guard_remote_overwrite(db, event=event, owner_id=owner_id)
    before = snapshot_calendar_event(event)
    values = {
        CalendarEvent.status: "cancelled",
        CalendarEvent.remote_href: None,
        CalendarEvent.remote_etag: None,
        CalendarEvent.caldav_sync_pending: None,
    }
    effective = {
        column: value for column, value in values.items()
        if getattr(event, column.key) != value
    }
    if effective:
        event = _cas_event(
            db,
            event=event,
            owner_id=owner_id,
            expected_version=expected,
            values=effective,
        )
    graph_entity = _project_event(db, account=account, event=event)
    if effective:
        append_action_audit(
            db,
            owner_id=owner_id,
            action="calendar.event.remote_disappeared",
            entity_type="calendar_event",
            entity_id=event.uid,
            reason="CalDAV event disappeared",
            before_state={
                "status": before["status"],
                "all_day": before["all_day"],
                "recurring": bool(before["rrule"]),
                "is_utc": before["is_utc"],
                "version": before["event_version"],
            },
            after_state=_structural_event_state(event),
            details={
                "fields": sorted(column.key for column in effective),
                "delivery_enqueued": False,
                "remote_ingestion": True,
            },
        )
    return _result(
        event=event,
        graph_entity=graph_entity,
        delivery=None,
        links_created=(),
        created=False,
        before_snapshot=before,
    )


def adopt_legacy_caldav_delivery(
    db,
    *,
    account: Account,
    uid: object,
    expected_version: int,
) -> CalendarDelivery | None:
    """Atomically translate one legacy pending marker into the V3 outbox.

    The marker is cleared only after an equivalent durable generation exists
    (or after proving a never-created cancelled event needs no remote delete).
    No network operation occurs in this transaction.
    """

    owner_id = _owner_id(account)
    event, calendar = _owned_event(db, owner_id=owner_id, uid=uid)
    if str(calendar.source or "").lower() != "caldav":
        raise CalendarServiceError("Legacy delivery adoption requires CalDAV")
    expected = _check_version(event, expected_version)
    marker = str(event.caldav_sync_pending or "").strip().lower()
    if marker and marker not in {"create", "update", "delete"}:
        raise CalendarServiceError("Legacy CalDAV pending marker is invalid")
    implicit_create = (
        not marker
        and not event.remote_href
        and str(event.origin or "").lower() != "caldav"
        and str(event.status or "confirmed") != "cancelled"
    )
    if not marker and not implicit_create:
        return None
    operation = marker or "create"
    if str(event.status or "confirmed") == "cancelled":
        operation = "delete"

    delivery = db.query(CalendarDelivery).filter(
        CalendarDelivery.owner_id == owner_id,
        CalendarDelivery.event_uid == event.uid,
        CalendarDelivery.expected_event_version == expected,
        CalendarDelivery.state.notin_(("completed", "cancelled")),
    ).order_by(
        CalendarDelivery.created_at.asc(), CalendarDelivery.id.asc(),
    ).first()
    already_delivered = db.query(CalendarDelivery.id).filter(
        CalendarDelivery.owner_id == owner_id,
        CalendarDelivery.event_uid == event.uid,
        CalendarDelivery.expected_event_version == expected,
        CalendarDelivery.state == "completed",
    ).first() is not None
    if delivery is None and not already_delivered:
        # A cancelled event with no remote resource was never created, so
        # there is nothing to delete.  Every other legacy generation becomes
        # an encrypted durable delivery before its marker is cleared.
        if not (operation == "delete" and not event.remote_href):
            delivery = _enqueue_delivery(
                db,
                account=account,
                calendar=calendar,
                event=event,
                operation=operation,
                idempotency_key=(
                    f"legacy-caldav:{event.uid}:{expected}:{operation}"
                ),
                proposal_id=None,
            )

    changed = db.query(CalendarEvent).filter(
        CalendarEvent.uid == event.uid,
        CalendarEvent.owner_id == owner_id,
        CalendarEvent.version == expected,
        CalendarEvent.caldav_sync_pending == event.caldav_sync_pending,
    ).update({
        CalendarEvent.caldav_sync_pending: None,
        CalendarEvent.updated_at: utcnow_naive(),
    }, synchronize_session=False)
    if changed != 1:
        raise CalendarConflict("Calendar event changed during delivery adoption")
    db.flush()
    append_action_audit(
        db,
        owner_id=owner_id,
        action="calendar.delivery.legacy_adopted",
        entity_type="calendar_event",
        entity_id=event.uid,
        reason="Legacy CalDAV delivery adopted",
        before_state={"version": expected, "pending": bool(marker)},
        after_state={"version": expected, "pending": False},
        details={
            "operation": operation,
            "delivery_enqueued": delivery is not None,
            "already_delivered": already_delivered,
        },
    )
    return delivery
