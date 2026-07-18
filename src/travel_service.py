"""Typed V3 Travel authority and deterministic offline Travel Mode.

Travel is a record-only domain over encrypted, ``Account.id``-owned
``LifeEntity(entity_type="trip")`` rows.  A root ``trip`` record owns bounded
typed child facts (research, budgets, transport, lodging, visas, itinerary,
packing, reservations, local transport, documents, contacts, expenses, and
calendar references).  The module never books, purchases, sends, or performs
network I/O.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.database import (
    Account,
    CalendarCal,
    CalendarEvent,
    Document,
    LifeEntity,
    LifeSource,
    Note,
    PlanningItem,
    Project,
    ProjectMember,
)
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    create_life_entity,
    delete_life_entity,
    get_life_entity,
    list_life_entity_versions,
    serialize_life_entity,
    update_life_entity,
)


TRAVEL_SCHEMA_VERSION = 1
TRAVEL_ENTITY_TYPE = "trip"
TRAVEL_SCAN_LIMIT = 750
TRAVEL_MODE_MAX_TRIPS = 3
TRAVEL_MODE_MAX_FACTS = 100

TRAVEL_RECORD_KINDS = frozenset({
    "trip",
    "research",
    "budget",
    "transport",
    "lodging",
    "visa",
    "itinerary",
    "packing",
    "reservation",
    "local_transport",
    "document",
    "contact",
    "expense",
    "calendar_reference",
})
TRAVEL_STATUSES = frozenset({
    "planned", "active", "completed", "cancelled", "archived",
})
TRAVEL_SENSITIVITIES = frozenset({"private", "restricted"})

_DETAIL_FIELDS: dict[str, frozenset[str]] = {
    "trip": frozenset({
        "destination", "trip_timezone", "purpose", "home_base", "country_codes",
    }),
    "research": frozenset({"topic", "finding", "url", "checked_at"}),
    "budget": frozenset({"category", "amount", "currency", "spent_amount"}),
    "transport": frozenset({
        "mode", "carrier", "service_number", "origin", "destination", "seat",
        "terminal", "reference",
    }),
    "lodging": frozenset({
        "name", "address", "check_in_at", "check_out_at", "reference",
    }),
    "visa": frozenset({
        "jurisdiction", "visa_type", "visa_status", "expires_on",
    }),
    "itinerary": frozenset({"day_label", "location", "activity"}),
    "packing": frozenset({"category", "item", "quantity", "packed"}),
    "reservation": frozenset({
        "reservation_type", "provider", "location", "reference",
        "reservation_status",
    }),
    "local_transport": frozenset({
        "mode", "provider", "route", "origin", "destination", "reference",
    }),
    "document": frozenset({
        "document_kind", "storage_ref", "expires_on", "content_sha256",
    }),
    "contact": frozenset({"name", "role", "phone", "email", "address"}),
    "expense": frozenset({
        "category", "amount", "currency", "merchant", "occurred_on",
    }),
    "calendar_reference": frozenset({"label"}),
}

_REQUIRED_DETAIL_FIELDS: dict[str, frozenset[str]] = {
    "trip": frozenset({"destination", "trip_timezone"}),
    "research": frozenset({"topic"}),
    "budget": frozenset({"category", "amount", "currency"}),
    "transport": frozenset({"mode", "origin", "destination"}),
    "lodging": frozenset({"name"}),
    "visa": frozenset({"jurisdiction", "visa_status"}),
    "itinerary": frozenset({"activity"}),
    "packing": frozenset({"item"}),
    "reservation": frozenset({"reservation_type", "provider"}),
    "local_transport": frozenset({"mode", "origin", "destination"}),
    "document": frozenset({"document_kind"}),
    "contact": frozenset({"name", "role"}),
    "expense": frozenset({"category", "amount", "currency"}),
    "calendar_reference": frozenset({"label"}),
}

_DATETIME_DETAIL_FIELDS = frozenset({
    "checked_at", "check_in_at", "check_out_at",
})
_DATE_DETAIL_FIELDS = frozenset({"expires_on", "occurred_on"})
_DECIMAL_DETAIL_FIELDS = frozenset({"amount", "spent_amount"})
_BOOLEAN_DETAIL_FIELDS = frozenset({"packed"})
_LIST_DETAIL_FIELDS = frozenset({"country_codes"})

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_URL_CREDENTIAL_RE = re.compile(
    r"^[a-z][a-z0-9+.-]*://[^/@\s]+:[^/@\s]+@", re.IGNORECASE
)
_URL_QUERY_SECRET_RE = re.compile(
    r"[?&](?:token|key|secret|password|signature|sig|x-amz-credential|"
    r"x-amz-signature)=",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bauthorization\s*:\s*bearer\s+\S+", re.IGNORECASE)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_DIGIT_RUN_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

_SECRET_KEY_PARTS = (
    "password", "passwd", "passcode", "secret", "token", "credential",
    "cookie", "authorization", "api_key", "private_key", "seed_phrase",
    "recovery_phrase", "pin", "cvv", "cvc", "card_number", "payment_data",
    "account_number", "routing_number", "iban", "swift_code", "bank_login",
)
_ACTION_KEYS = frozenset({
    "action", "execute", "executor", "tool_call", "external_action", "webhook",
    "send", "send_message", "send_email", "book", "booking_executor", "purchase",
    "pay", "payment", "transfer", "wire", "place_order", "cancel_reservation",
    "automation", "command", "shell",
})


def _text(
    value: object,
    *,
    field: str,
    limit: int,
    required: bool = False,
    preserve_lines: bool = False,
) -> str:
    raw = str(value or "").strip()
    normalized = raw if preserve_lines else " ".join(raw.split())
    if required and not normalized:
        raise LifeGraphError(f"{field} is required")
    if len(normalized) > limit:
        raise LifeGraphError(f"{field} must not exceed {limit} characters")
    if (
        _URL_CREDENTIAL_RE.search(normalized)
        or _URL_QUERY_SECRET_RE.search(normalized)
        or _BEARER_RE.search(normalized)
        or _PRIVATE_KEY_RE.search(normalized)
    ):
        raise LifeGraphError(f"{field} must not contain credentials")
    # Canonical entity/source identifiers are UUIDs. Their numeric groups can
    # coincidentally satisfy Luhn, but they are not payment-card material.
    if not _UUID_RE.fullmatch(normalized):
        for match in _DIGIT_RUN_RE.finditer(normalized):
            digits = "".join(
                character for character in match.group(0) if character.isdigit()
            )
            if _passes_luhn(digits):
                raise LifeGraphError(f"{field} must not contain payment-card data")
    return normalized


def _passes_luhn(digits: str) -> bool:
    if len(digits) < 13 or len(digits) > 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, character in enumerate(digits):
        number = int(character)
        if index % 2 == parity:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _token(value: object, *, field: str, limit: int = 64) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    if not normalized or len(normalized) > limit or not _TOKEN_RE.fullmatch(normalized):
        raise LifeGraphError(
            f"{field} must be a lowercase token using letters, numbers, _, -, or ."
        )
    return normalized


def _instant(
    value: object | None,
    *,
    field: str,
    required: bool = False,
    require_offset: bool = True,
) -> datetime | None:
    if value is None or value == "":
        if required:
            raise LifeGraphError(f"{field} is required")
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise LifeGraphError(f"{field} must be an ISO-8601 datetime") from exc
    else:
        raise LifeGraphError(f"{field} must be an ISO-8601 datetime")
    if require_offset and (parsed.tzinfo is None or parsed.utcoffset() is None):
        raise LifeGraphError(f"{field} must include an explicit UTC offset")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_instant(value: object | None, *, field: str, required: bool = False) -> str | None:
    parsed = _instant(value, field=field, required=required)
    if parsed is None:
        return None
    return parsed.isoformat().replace("+00:00", "Z")


def _date(value: object | None, *, field: str) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = date.fromisoformat(value.strip())
        except ValueError as exc:
            raise LifeGraphError(f"{field} must be an ISO-8601 date") from exc
    else:
        raise LifeGraphError(f"{field} must be an ISO-8601 date")
    return parsed.isoformat()


def _decimal(value: object, *, field: str) -> str:
    if isinstance(value, bool) or value is None or value == "":
        raise LifeGraphError(f"{field} must be a finite decimal number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise LifeGraphError(f"{field} must be a finite decimal number") from exc
    if not number.is_finite() or abs(number) > Decimal("999999999999999"):
        raise LifeGraphError(f"{field} must be a finite bounded decimal number")
    if number.as_tuple().exponent < -4:
        raise LifeGraphError(f"{field} must not have more than 4 decimal places")
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return "0" if normalized in {"", "-0"} else normalized


def _currency(value: object) -> str:
    normalized = str(value or "").strip().upper()
    if not _CURRENCY_RE.fullmatch(normalized):
        raise LifeGraphError("currency must be a three-letter ISO code")
    return normalized


def _assert_safe_record(value: object, *, field: str = "travel record") -> None:
    """Fail closed on credential, payment, and executable action containers."""

    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key or "").strip().lower().replace("-", "_")
            if any(part in key for part in _SECRET_KEY_PARTS):
                raise LifeGraphError(f"{field} must not contain credentials or payment data")
            if key in _ACTION_KEYS:
                raise LifeGraphError(f"{field} must not contain action payloads")
            _assert_safe_record(nested, field=field)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _assert_safe_record(nested, field=field)
        return
    if isinstance(value, str):
        _text(value, field=field, limit=20_000, preserve_lines=True)


def _id_list(value: object | None, *, field: str, max_items: int = 50) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LifeGraphError(f"{field} must be a list")
    if len(value) > max_items:
        raise LifeGraphError(f"{field} must not contain more than {max_items} items")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        identifier = _text(item, field=field, limit=255, required=True)
        if identifier not in seen:
            seen.add(identifier)
            result.append(identifier)
    return result


def _details(value: object | None, *, record_kind: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LifeGraphError("details must be an object")
    # Classify dangerous containers before reporting ordinary schema errors so
    # callers receive the fail-closed policy reason, not a misleading unknown
    # field message.
    _assert_safe_record(value)
    allowed = _DETAIL_FIELDS[record_kind]
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported {record_kind} detail fields: {', '.join(unknown)}")
    missing = [
        field for field in _REQUIRED_DETAIL_FIELDS[record_kind]
        if value.get(field) is None or value.get(field) == ""
    ]
    if missing:
        raise LifeGraphError(f"Missing required {record_kind} details: {', '.join(sorted(missing))}")

    result: dict[str, Any] = {}
    for field, raw in value.items():
        if field in _DATETIME_DETAIL_FIELDS:
            result[field] = _iso_instant(raw, field=f"details.{field}")
        elif field in _DATE_DETAIL_FIELDS:
            result[field] = _date(raw, field=f"details.{field}")
        elif field in _DECIMAL_DETAIL_FIELDS:
            result[field] = _decimal(raw, field=f"details.{field}")
            if Decimal(result[field]) < 0:
                raise LifeGraphError(f"details.{field} must not be negative")
        elif field == "currency":
            result[field] = _currency(raw)
        elif field in _BOOLEAN_DETAIL_FIELDS:
            if not isinstance(raw, bool):
                raise LifeGraphError(f"details.{field} must be a boolean")
            result[field] = raw
        elif field in _LIST_DETAIL_FIELDS:
            result[field] = [
                _text(item, field=f"details.{field}", limit=8, required=True).upper()
                for item in _id_list(raw, field=f"details.{field}", max_items=25)
            ]
        elif field == "content_sha256":
            digest = str(raw or "").strip().lower()
            if digest and not _SHA256_RE.fullmatch(digest):
                raise LifeGraphError("details.content_sha256 must be a SHA-256 hex digest")
            result[field] = digest
        elif field == "quantity":
            if isinstance(raw, bool):
                raise LifeGraphError("details.quantity must be an integer from 1 to 1000")
            try:
                quantity = int(raw)
            except (TypeError, ValueError) as exc:
                raise LifeGraphError("details.quantity must be an integer from 1 to 1000") from exc
            if quantity < 1 or quantity > 1000:
                raise LifeGraphError("details.quantity must be an integer from 1 to 1000")
            result[field] = quantity
        else:
            result[field] = _text(
                raw,
                field=f"details.{field}",
                limit=4_000 if field in {"finding", "address"} else 1_000,
                required=field in _REQUIRED_DETAIL_FIELDS[record_kind],
                preserve_lines=field == "finding",
            )

    if record_kind == "trip":
        timezone_name = result["trip_timezone"]
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise LifeGraphError("details.trip_timezone must be a valid IANA timezone") from exc
    if record_kind == "document" and "storage_ref" in result:
        result["storage_ref"] = _text(
            result["storage_ref"], field="details.storage_ref", limit=2_000
        )
    _assert_safe_record(result)
    return result


def validate_travel_properties(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LifeGraphError("travel properties must be an object")
    allowed = {
        "travel_schema_version", "record_kind", "trip_id", "starts_at", "ends_at",
        "offline_available", "details", "related_entity_ids", "source_ids",
        "calendar_event_ids",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported travel fields: {', '.join(unknown)}")
    if value.get("travel_schema_version", TRAVEL_SCHEMA_VERSION) != TRAVEL_SCHEMA_VERSION:
        raise LifeGraphError("Unsupported travel schema version")
    record_kind = _token(value.get("record_kind"), field="record_kind")
    if record_kind not in TRAVEL_RECORD_KINDS:
        raise LifeGraphError("Unknown travel record kind")

    trip_id = str(value.get("trip_id") or "").strip() or None
    if record_kind == "trip" and trip_id is not None:
        raise LifeGraphError("A root trip context cannot reference another trip")
    if record_kind != "trip" and trip_id is None:
        raise LifeGraphError("trip_id is required for travel child records")
    if trip_id and len(trip_id) > 36:
        raise LifeGraphError("trip_id must not exceed 36 characters")

    starts_at = _iso_instant(
        value.get("starts_at"), field="starts_at", required=record_kind == "trip"
    )
    ends_at = _iso_instant(
        value.get("ends_at"), field="ends_at", required=record_kind == "trip"
    )
    if bool(starts_at) != bool(ends_at):
        raise LifeGraphError("starts_at and ends_at must be supplied together")
    if starts_at and ends_at:
        start = _instant(starts_at, field="starts_at", required=True)
        end = _instant(ends_at, field="ends_at", required=True)
        assert start is not None and end is not None
        if end <= start:
            raise LifeGraphError("ends_at must be after starts_at")

    offline_available = value.get("offline_available", record_kind == "trip")
    if not isinstance(offline_available, bool):
        raise LifeGraphError("offline_available must be a boolean")
    details = _details(value.get("details"), record_kind=record_kind)
    related_entity_ids = _id_list(
        value.get("related_entity_ids"), field="related_entity_ids"
    )
    source_ids = _id_list(value.get("source_ids"), field="source_ids")
    calendar_event_ids = _id_list(
        value.get("calendar_event_ids"), field="calendar_event_ids"
    )
    if record_kind == "calendar_reference" and not calendar_event_ids:
        raise LifeGraphError("calendar_reference records require calendar_event_ids")
    if record_kind == "document" and offline_available and not details.get("storage_ref"):
        raise LifeGraphError(
            "Offline-available documents require an explicit storage_ref"
        )
    if (
        record_kind == "document"
        and offline_available
        and str(details.get("storage_ref") or "").lower().startswith(("http://", "https://"))
    ):
        raise LifeGraphError(
            "Offline-available documents require a local or vault storage_ref"
        )
    normalized = {
        "travel_schema_version": TRAVEL_SCHEMA_VERSION,
        "record_kind": record_kind,
        "trip_id": trip_id,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "offline_available": offline_available,
        "details": details,
        "related_entity_ids": related_entity_ids,
        "source_ids": source_ids,
        "calendar_event_ids": calendar_event_ids,
    }
    _assert_safe_record(normalized)
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise LifeGraphError("travel properties must not exceed 65536 bytes")
    return normalized


def is_typed_travel_payload(entity_type: object, properties: object | None = None) -> bool:
    return (
        str(entity_type or "").strip().lower() == TRAVEL_ENTITY_TYPE
        and isinstance(properties, Mapping)
        and properties.get("travel_schema_version") is not None
    ) or (
        isinstance(properties, Mapping)
        and properties.get("travel_schema_version") is not None
    )


def _provenance(value: object | None, *, record_kind: str) -> dict[str, Any]:
    if value is None:
        raw: Mapping[str, Any] = {}
    elif isinstance(value, Mapping):
        raw = value
    else:
        raise LifeGraphError("provenance must be an object")
    _assert_safe_record(raw, field="provenance")
    try:
        encoded = json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise LifeGraphError("provenance must be JSON-serializable") from exc
    if len(encoded) > 16_000:
        raise LifeGraphError("provenance must not exceed 16000 bytes")
    provenance = dict(raw)
    provenance.setdefault("capture", "manual")
    provenance["domain"] = "travel"
    provenance["record_kind"] = record_kind
    return provenance


def _owned_trip_context(db, *, owner_id: str, trip_id: object) -> LifeEntity:
    try:
        trip = get_life_entity(db, owner_id=owner_id, entity_id=trip_id)
    except LifeGraphNotFound as exc:
        raise LifeGraphNotFound("Travel trip context not found") from exc
    if trip.entity_type != TRAVEL_ENTITY_TYPE:
        raise LifeGraphNotFound("Travel trip context not found")
    properties = validate_travel_properties(trip.properties or {})
    if properties["record_kind"] != "trip":
        raise LifeGraphNotFound("Travel trip context not found")
    return trip


def _validate_reference_authority(
    db,
    *,
    owner_id: str,
    properties: Mapping[str, Any],
    entity_id: str | None = None,
) -> None:
    trip_id = properties.get("trip_id")
    if trip_id:
        _owned_trip_context(db, owner_id=owner_id, trip_id=trip_id)

    entity_ids = set(properties.get("related_entity_ids") or [])
    if entity_id and entity_id in entity_ids:
        raise LifeGraphError("related_entity_ids cannot contain the record itself")
    if entity_ids:
        owned = {
            row[0]
            for row in db.query(LifeEntity.id).filter(
                LifeEntity.owner_id == owner_id,
                LifeEntity.id.in_(entity_ids),
                LifeEntity.deleted_at.is_(None),
            ).all()
        }
        if owned != entity_ids:
            raise LifeGraphNotFound("Travel related LifeEntity not found")

    source_ids = set(properties.get("source_ids") or [])
    if source_ids:
        owned_sources = {
            row[0]
            for row in db.query(LifeSource.id).filter(
                LifeSource.owner_id == owner_id,
                LifeSource.id.in_(source_ids),
            ).all()
        }
        if owned_sources != source_ids:
            raise LifeGraphNotFound("Travel LifeSource not found")

    calendar_ids = set(properties.get("calendar_event_ids") or [])
    if any("::" in identifier for identifier in calendar_ids):
        raise LifeGraphError(
            "calendar_event_ids must reference base events, not generated occurrences"
        )
    if calendar_ids:
        owned_calendar_ids = {
            row[0]
            for row in db.query(CalendarCal.id).filter(
                CalendarCal.owner_id == owner_id
            ).all()
        }
        owned_events = {
            row[0]
            for row in db.query(CalendarEvent.uid).filter(
                CalendarEvent.owner_id == owner_id,
                CalendarEvent.calendar_id.in_(owned_calendar_ids),
                CalendarEvent.uid.in_(calendar_ids),
            ).all()
        }
        if owned_events != calendar_ids:
            raise LifeGraphNotFound("Travel calendar event not found")


def _validate_domain_reference_authority(db, *, owner_id: str, entity: LifeEntity) -> None:
    """Re-check the optional canonical domain pointer without mutating its target."""

    ref_type = entity.domain_ref_type
    ref_id = entity.domain_ref_id
    if ref_type is None and ref_id is None:
        return
    if not ref_type or not ref_id:
        raise LifeGraphError("Travel domain reference is malformed")
    account = db.query(Account).filter(Account.id == owner_id).first()
    if account is None:
        raise LifeGraphNotFound("Travel domain reference owner not found")

    found = False
    if ref_type == "life_source":
        found = db.query(LifeSource.id).filter(
            LifeSource.id == ref_id, LifeSource.owner_id == owner_id
        ).scalar() is not None
    elif ref_type == "planning_item":
        found = db.query(PlanningItem.id).filter(
            PlanningItem.id == ref_id, PlanningItem.owner == account.username
        ).scalar() is not None
    elif ref_type == "project":
        project = db.query(Project).filter(Project.id == ref_id).first()
        found = bool(project and (
            project.owner == account.username
            or db.query(ProjectMember.project_id).filter(
                ProjectMember.project_id == ref_id,
                ProjectMember.username == account.username,
            ).scalar() is not None
        ))
    elif ref_type == "note":
        found = db.query(Note.id).filter(
            Note.id == ref_id, Note.owner == account.username
        ).scalar() is not None
    elif ref_type == "document":
        found = db.query(Document.id).filter(
            Document.id == ref_id, Document.owner == account.username
        ).scalar() is not None
    elif ref_type == "calendar_event":
        if "::" in str(ref_id):
            raise LifeGraphError(
                "calendar_event domain reference must use a base event"
            )
        found = db.query(CalendarEvent.uid).join(
            CalendarCal,
            (CalendarCal.id == CalendarEvent.calendar_id)
            & (CalendarCal.owner_id == CalendarEvent.owner_id),
        ).filter(
            CalendarEvent.uid == ref_id,
            CalendarEvent.owner_id == owner_id,
            CalendarCal.owner_id == owner_id,
        ).scalar() is not None
    else:
        raise LifeGraphError("Travel domain reference type is unsupported")
    if not found:
        raise LifeGraphNotFound("Travel domain record not found")


def _owned_travel_record(
    db,
    *,
    owner_id: str,
    entity_id: object,
    include_deleted: bool = False,
) -> LifeEntity:
    entity = get_life_entity(
        db, owner_id=owner_id, entity_id=entity_id, include_deleted=include_deleted
    )
    if entity.entity_type != TRAVEL_ENTITY_TYPE or not is_typed_travel_payload(
        entity.entity_type, entity.properties
    ):
        raise LifeGraphNotFound("Travel record not found")
    properties = validate_travel_properties(entity.properties or {})
    if not include_deleted:
        _validate_reference_authority(
            db, owner_id=owner_id, properties=properties, entity_id=entity.id
        )
        _validate_domain_reference_authority(db, owner_id=owner_id, entity=entity)
    return entity


def create_travel_record(
    db,
    *,
    account: Account,
    record_kind: object,
    title: object,
    details: object,
    trip_id: object | None = None,
    summary: object = "",
    starts_at: object | None = None,
    ends_at: object | None = None,
    offline_available: object | None = None,
    related_entity_ids: object | None = None,
    source_ids: object | None = None,
    calendar_event_ids: object | None = None,
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    status: object = "planned",
    domain_ref_type: object | None = None,
    domain_ref_id: object | None = None,
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    kind = _token(record_kind, field="record_kind")
    normalized_title = _text(title, field="title", limit=240, required=True)
    normalized_summary = _text(
        summary, field="summary", limit=20_000, preserve_lines=True
    )
    normalized_status = _token(status, field="status", limit=32)
    if normalized_status not in TRAVEL_STATUSES:
        raise LifeGraphError("Travel status is invalid")
    normalized_sensitivity = _token(sensitivity, field="sensitivity", limit=24)
    if normalized_sensitivity not in TRAVEL_SENSITIVITIES:
        raise LifeGraphError("Travel sensitivity must be private or restricted")
    raw_properties: dict[str, Any] = {
        "record_kind": kind,
        "trip_id": trip_id,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "details": details,
        "related_entity_ids": related_entity_ids,
        "source_ids": source_ids,
        "calendar_event_ids": calendar_event_ids,
    }
    if offline_available is not None:
        raw_properties["offline_available"] = offline_available
    properties = validate_travel_properties(raw_properties)
    _validate_reference_authority(
        db, owner_id=account.id, properties=properties
    )
    normalized_provenance = _provenance(provenance, record_kind=kind)
    started = _instant(properties["starts_at"], field="starts_at")
    ended = _instant(properties["ends_at"], field="ends_at")
    return create_life_entity(
        db,
        account=account,
        entity_type=TRAVEL_ENTITY_TYPE,
        title=normalized_title,
        summary=normalized_summary,
        status=normalized_status,
        properties=properties,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=normalized_sensitivity,
        domain_ref_type=domain_ref_type,
        domain_ref_id=domain_ref_id,
        occurred_at=started.replace(tzinfo=None) if started else None,
        due_at=ended.replace(tzinfo=None) if ended else None,
        idempotency_key=idempotency_key,
        reason="Travel record captured",
    )


def update_travel_record(
    db,
    *,
    account: Account,
    entity_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    entity = _owned_travel_record(db, owner_id=account.id, entity_id=entity_id)
    allowed = {
        "title", "summary", "status", "trip_id", "starts_at", "ends_at",
        "offline_available", "details", "related_entity_ids", "source_ids",
        "calendar_event_ids", "provenance", "confidence", "sensitivity",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported travel fields: {', '.join(unknown)}")
    current = validate_travel_properties(entity.properties or {})
    merged = dict(current)
    for field in (
        "trip_id", "starts_at", "ends_at", "offline_available", "details",
        "related_entity_ids", "source_ids", "calendar_event_ids",
    ):
        if field in changes:
            merged[field] = changes[field]
    properties = validate_travel_properties(merged)
    _validate_reference_authority(
        db, owner_id=account.id, properties=properties, entity_id=entity.id
    )
    entity_changes: dict[str, Any] = {"properties": properties}
    if "title" in changes:
        entity_changes["title"] = _text(
            changes["title"], field="title", limit=240, required=True
        )
    if "summary" in changes:
        entity_changes["summary"] = _text(
            changes["summary"], field="summary", limit=20_000, preserve_lines=True
        )
    if "status" in changes:
        status = _token(changes["status"], field="status", limit=32)
        if status not in TRAVEL_STATUSES:
            raise LifeGraphError("Travel status is invalid")
        entity_changes["status"] = status
    if "provenance" in changes:
        entity_changes["provenance"] = _provenance(
            changes["provenance"], record_kind=properties["record_kind"]
        )
    if "confidence" in changes:
        entity_changes["confidence"] = changes["confidence"]
    if "sensitivity" in changes:
        sensitivity = _token(changes["sensitivity"], field="sensitivity", limit=24)
        if sensitivity not in TRAVEL_SENSITIVITIES:
            raise LifeGraphError("Travel sensitivity must be private or restricted")
        entity_changes["sensitivity"] = sensitivity
    started = _instant(properties["starts_at"], field="starts_at")
    ended = _instant(properties["ends_at"], field="ends_at")
    entity_changes["occurred_at"] = started.replace(tzinfo=None) if started else None
    entity_changes["due_at"] = ended.replace(tzinfo=None) if ended else None
    return update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=entity_changes,
        reason="Travel record updated",
    )


def delete_travel_record(
    db,
    *,
    owner_id: str,
    entity_id: object,
    expected_version: int,
    reason: object = "Travel record deleted",
) -> LifeEntity:
    entity = _owned_travel_record(db, owner_id=owner_id, entity_id=entity_id)
    properties = validate_travel_properties(entity.properties or {})
    if properties["record_kind"] == "trip":
        children = db.query(LifeEntity.id).filter(
            LifeEntity.owner_id == owner_id,
            LifeEntity.entity_type == TRAVEL_ENTITY_TYPE,
            LifeEntity.deleted_at.is_(None),
        ).all()
        for (candidate_id,) in children:
            if candidate_id == entity.id:
                continue
            candidate = get_life_entity(
                db, owner_id=owner_id, entity_id=candidate_id
            )
            if not is_typed_travel_payload(
                candidate.entity_type, candidate.properties
            ):
                continue
            candidate_properties = validate_travel_properties(
                candidate.properties or {}
            )
            if candidate_properties.get("trip_id") == entity.id:
                raise LifeGraphConflict(
                    "Delete or move travel child records before deleting the trip context"
                )
    return delete_life_entity(
        db,
        owner_id=owner_id,
        entity_id=entity.id,
        expected_version=expected_version,
        reason=reason,
    )


def serialize_travel_record(entity: LifeEntity) -> dict[str, Any]:
    if entity.entity_type != TRAVEL_ENTITY_TYPE:
        raise LifeGraphError("Entity is not a Travel record")
    properties = validate_travel_properties(entity.properties or {})
    payload = serialize_life_entity(entity)
    payload.update(properties)
    payload["execution_policy"] = {
        "record_only": True,
        "can_book": False,
        "can_purchase": False,
        "can_send": False,
        "uses_network": False,
        "uses_model_inference": False,
    }
    if properties["record_kind"] == "document":
        payload["document_availability"] = {
            "available_offline": bool(properties["offline_available"]),
            "has_storage_ref": bool(properties["details"].get("storage_ref")),
        }
    return payload


def get_travel_record(db, *, owner_id: str, entity_id: object) -> dict[str, Any]:
    return serialize_travel_record(
        _owned_travel_record(db, owner_id=owner_id, entity_id=entity_id)
    )


def _travel_candidates(
    db,
    *,
    owner_id: str,
    record_kind: object | None = None,
    trip_id: object | None = None,
    status: object | None = None,
    limit: int = TRAVEL_SCAN_LIMIT,
) -> tuple[list[LifeEntity], bool]:
    normalized_kind = None
    if record_kind:
        normalized_kind = _token(record_kind, field="record_kind")
        if normalized_kind not in TRAVEL_RECORD_KINDS:
            raise LifeGraphError("Unknown travel record kind")
    normalized_trip_id = str(trip_id or "").strip() or None
    if normalized_trip_id:
        _owned_trip_context(db, owner_id=owner_id, trip_id=normalized_trip_id)
    normalized_status = None
    if status:
        normalized_status = _token(status, field="status", limit=32)
        if normalized_status not in TRAVEL_STATUSES:
            raise LifeGraphError("Travel status is invalid")
    bounded = max(1, min(TRAVEL_SCAN_LIMIT, int(limit)))
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == TRAVEL_ENTITY_TYPE,
        LifeEntity.deleted_at.is_(None),
    )
    if normalized_status:
        query = query.filter(LifeEntity.status == normalized_status)
    rows = query.order_by(
        LifeEntity.occurred_at.asc(), LifeEntity.updated_at.desc(), LifeEntity.id.asc()
    ).limit(bounded + 1).all()
    result: list[LifeEntity] = []
    for row in rows[:bounded]:
        # Legacy generic trip nodes are not part of the typed authority. Once
        # a row declares the Travel schema, malformed/cross-owner references
        # are surfaced rather than silently omitted.
        if not is_typed_travel_payload(row.entity_type, row.properties):
            continue
        properties = validate_travel_properties(row.properties or {})
        _validate_reference_authority(
            db, owner_id=owner_id, properties=properties, entity_id=row.id
        )
        _validate_domain_reference_authority(
            db, owner_id=owner_id, entity=row
        )
        if normalized_kind and properties["record_kind"] != normalized_kind:
            continue
        if normalized_trip_id and properties.get("trip_id") != normalized_trip_id:
            continue
        result.append(row)
    return result, len(rows) > bounded


def list_travel_records(
    db,
    *,
    owner_id: str,
    record_kind: object | None = None,
    trip_id: object | None = None,
    status: object | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    bounded = max(1, min(100, int(limit)))
    rows, scan_truncated = _travel_candidates(
        db,
        owner_id=owner_id,
        record_kind=record_kind,
        trip_id=trip_id,
        status=status,
        limit=TRAVEL_SCAN_LIMIT,
    )
    return [serialize_travel_record(row) for row in rows[:bounded]], (
        scan_truncated or len(rows) > bounded
    )


def travel_record_history(
    db, *, owner_id: str, entity_id: object, limit: int = 50
) -> tuple[list[dict[str, Any]], bool]:
    entity = _owned_travel_record(
        db, owner_id=owner_id, entity_id=entity_id, include_deleted=True
    )
    rows, truncated = list_life_entity_versions(
        db, owner_id=owner_id, entity_id=entity.id, limit=limit
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        snapshot = dict(row.snapshot or {})
        properties = validate_travel_properties(snapshot.get("properties") or {})
        items.append({
            "id": row.id,
            "version": int(row.version),
            "reason": row.reason or "",
            "created_at": row.created_at.replace(tzinfo=timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "record_kind": properties["record_kind"],
            "status": snapshot.get("status"),
            "snapshot": snapshot,
        })
    return items, truncated


def _mode_fact(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": record["id"],
        "record_kind": record["record_kind"],
        "title": record["title"],
        "summary": record["summary"],
        "status": record["status"],
        "trip_id": record["trip_id"],
        "starts_at": record["starts_at"],
        "ends_at": record["ends_at"],
        "offline_available": record["offline_available"],
        "details": record["details"],
        "provenance": record["provenance"],
        "confidence": record["confidence"],
        "sensitivity": record["sensitivity"],
        "document_availability": record.get("document_availability"),
    }


def travel_mode(
    db,
    *,
    owner_id: str,
    as_of: object,
    offline_only: bool = True,
    trip_limit: int = TRAVEL_MODE_MAX_TRIPS,
    fact_limit: int = 50,
) -> dict[str, Any]:
    """Build an immediate deterministic read model without network or model calls."""

    reference = _instant(as_of, field="as_of", required=True, require_offset=True)
    assert reference is not None
    bounded_trips = max(1, min(TRAVEL_MODE_MAX_TRIPS, int(trip_limit)))
    bounded_facts = max(1, min(TRAVEL_MODE_MAX_FACTS, int(fact_limit)))
    rows, scan_truncated = _travel_candidates(
        db, owner_id=owner_id, limit=TRAVEL_SCAN_LIMIT
    )
    serialized = [serialize_travel_record(row) for row in rows]
    trip_records = [row for row in serialized if row["record_kind"] == "trip"]
    current: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []
    for trip in trip_records:
        starts = _instant(trip["starts_at"], field="starts_at", required=True)
        ends = _instant(trip["ends_at"], field="ends_at", required=True)
        assert starts is not None and ends is not None
        if starts <= reference < ends and trip["status"] not in {
            "cancelled", "archived", "completed",
        }:
            current.append(trip)
        elif starts >= reference and trip["status"] not in {
            "cancelled", "archived", "completed",
        }:
            upcoming.append(trip)
    current.sort(key=lambda item: (item["starts_at"], item["id"]))
    upcoming.sort(key=lambda item: (item["starts_at"], item["id"]))
    if offline_only:
        current = [trip for trip in current if trip["offline_available"]]
        upcoming = [trip for trip in upcoming if trip["offline_available"]]
    current = current[:bounded_trips]
    upcoming = upcoming[:bounded_trips]
    selected_trip_ids = {row["id"] for row in (*current, *upcoming)}

    facts: list[dict[str, Any]] = []
    all_selected_documents: list[dict[str, Any]] = []
    for row in serialized:
        if row["record_kind"] == "trip" or row.get("trip_id") not in selected_trip_ids:
            continue
        if row["status"] in {"cancelled", "archived"}:
            continue
        if row["record_kind"] == "document":
            all_selected_documents.append(row)
        if offline_only and not row["offline_available"]:
            continue
        facts.append(row)
    facts.sort(key=lambda item: (
        item["trip_id"] or "",
        item["starts_at"] or "9999-12-31T23:59:59Z",
        item["record_kind"],
        item["id"],
    ))
    fact_truncated = len(facts) > bounded_facts
    visible_facts = facts[:bounded_facts]
    documents = [
        _mode_fact(row) for row in all_selected_documents
        if not offline_only or row["offline_available"]
    ][:bounded_facts]
    available_documents = sum(
        1 for row in all_selected_documents if row["offline_available"]
    )
    unavailable_documents = len(all_selected_documents) - available_documents
    return {
        "as_of": reference.isoformat().replace("+00:00", "Z"),
        "offline_only": bool(offline_only),
        "current_trips": [_mode_fact(row) for row in current],
        "next_trips": [_mode_fact(row) for row in upcoming],
        "facts": [_mode_fact(row) for row in visible_facts],
        "documents": documents,
        "document_availability": {
            "total": len(all_selected_documents),
            "available_offline": available_documents,
            "unavailable_offline": unavailable_documents,
        },
        "bounds": {
            "trip_limit": bounded_trips,
            "fact_limit": bounded_facts,
            "scanned": len(rows),
            "truncated": bool(scan_truncated or fact_truncated),
        },
        "execution_policy": {
            "record_only": True,
            "uses_network": False,
            "uses_model_inference": False,
            "can_book": False,
            "can_purchase": False,
            "can_send": False,
        },
    }
