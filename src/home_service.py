"""Typed V3 Home and Personal Administration records.

The canonical authority is an encrypted, ``Account.id``-owned ``LifeEntity``
with ``entity_type == "home_record"``.  This module is intentionally record
only: it can describe documents, renewals, repairs, deliveries, and household
work, but it cannot submit forms, renew policies, make purchases, contact a
provider, or invoke another external executor.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from core.database import Account, Document, LifeEntity, LifeSource
from src.life_graph import (
    LifeGraphError,
    LifeGraphNotFound,
    create_life_entity,
    delete_life_entity,
    get_life_entity,
    list_life_entity_versions,
    serialize_life_entity,
    update_life_entity,
)


HOME_SCHEMA_VERSION = 1
HOME_ENTITY_TYPE = "home_record"
HOME_SCAN_LIMIT = 750
HOME_ALERT_MAX_HORIZON_DAYS = 365
HOME_RECORD_ONLY_NOTICE = (
    "Home records and alerts are private planning data. Restia does not renew, "
    "purchase, submit, send, book, call, or otherwise execute an external action."
)

HOME_RECORD_TYPES = frozenset({
    "identity_document",
    "insurance",
    "warranty",
    "renewal",
    "inventory_item",
    "repair",
    "purchase",
    "delivery",
    "vehicle",
    "travel_document",
    "form",
    "provider",
    "household_routine",
    "emergency_information",
})

_SENSITIVITIES = frozenset({"private", "restricted"})
_ENTITY_STATUSES = frozenset({"active", "archived"})
_SOURCE_KINDS = frozenset({
    "manual", "user_observation", "document", "provider", "import",
    "email", "calendar", "receipt", "warranty_document",
})
_EXTERNAL_SOURCE_KINDS = _SOURCE_KINDS - {"manual", "user_observation"}
_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_LOCAL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")
_URL_CREDENTIAL_RE = re.compile(
    r"^[a-z][a-z0-9+.-]*://[^/@\s]+:[^/@\s]+@", re.I
)
_PAYMENT_CANDIDATE_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")

_SECRET_KEY_PARTS = (
    "password", "passwd", "passcode", "secret", "token", "credential",
    "cookie", "authorization", "auth_header", "api_key", "private_key",
    "seed_phrase", "recovery_phrase", "pin", "cvv", "cvc",
    "card_number", "payment_number", "bank_account", "routing_number",
    "account_number", "iban",
)
_EXECUTOR_KEYS = frozenset({
    "execute", "executor", "tool_call", "action_payload", "external_action",
    "command", "webhook", "request_body", "http_headers", "send_email",
    "send_message", "submit_form", "renew_now", "purchase_now", "pay_now",
    "book_now", "call_provider", "cancel_service",
})

_DETAIL_FIELDS: dict[str, frozenset[str]] = {
    "identity_document": frozenset({
        "document_kind", "issuer", "jurisdiction", "identifier_last4",
        "record_status",
    }),
    "insurance": frozenset({
        "policy_kind", "provider_name", "coverage_summary",
        "policy_reference", "record_status",
    }),
    "warranty": frozenset({
        "item_name", "provider_name", "coverage_summary",
        "warranty_reference", "record_status",
    }),
    "renewal": frozenset({
        "renewal_kind", "provider_name", "cadence", "renewal_reference",
        "record_status",
    }),
    "inventory_item": frozenset({
        "category", "location", "quantity", "unit", "serial_reference",
        "record_status",
    }),
    "repair": frozenset({
        "item_name", "repair_kind", "provider_name", "service_reference",
        "record_status",
    }),
    "purchase": frozenset({
        "merchant", "category", "order_reference", "record_status",
    }),
    "delivery": frozenset({
        "carrier", "tracking_reference", "delivery_window", "record_status",
    }),
    "vehicle": frozenset({
        "vehicle_kind", "make", "model", "year", "registration_reference",
        "vin_last6", "record_status",
    }),
    "travel_document": frozenset({
        "document_kind", "issuer", "country", "identifier_last4",
        "record_status",
    }),
    "form": frozenset({
        "form_kind", "organization", "submission_reference", "record_status",
    }),
    "provider": frozenset({
        "provider_kind", "provider_name", "service_area", "provider_reference",
        "record_status",
    }),
    "household_routine": frozenset({
        "routine_kind", "cadence", "instructions", "record_status",
    }),
    "emergency_information": frozenset({
        "information_kind", "contact_label", "instructions", "record_status",
    }),
}

_REQUIRED_DETAIL_FIELDS: dict[str, tuple[str, ...]] = {
    "identity_document": ("document_kind", "issuer"),
    "insurance": ("policy_kind", "provider_name"),
    "warranty": ("item_name", "provider_name"),
    "renewal": ("renewal_kind", "provider_name", "cadence"),
    "inventory_item": ("category",),
    "repair": ("item_name", "repair_kind"),
    "purchase": ("merchant", "category"),
    "delivery": ("carrier",),
    "vehicle": ("vehicle_kind", "make", "model"),
    "travel_document": ("document_kind", "issuer", "country"),
    "form": ("form_kind", "organization"),
    "provider": ("provider_kind", "provider_name"),
    "household_routine": ("routine_kind", "cadence", "instructions"),
    "emergency_information": ("information_kind", "instructions"),
}

_STATUS_CHOICES: dict[str, tuple[str, frozenset[str]]] = {
    "identity_document": (
        "active", frozenset({"active", "expired", "replaced", "revoked"}),
    ),
    "insurance": (
        "active", frozenset({"active", "expired", "cancelled"}),
    ),
    "warranty": (
        "active", frozenset({"active", "expired", "claimed", "cancelled"}),
    ),
    "renewal": (
        "open", frozenset({"open", "completed", "cancelled"}),
    ),
    "inventory_item": (
        "active", frozenset({"active", "disposed", "lost", "donated"}),
    ),
    "repair": (
        "needed", frozenset({
            "needed", "scheduled", "in_progress", "completed", "cancelled",
        }),
    ),
    "purchase": (
        "recorded", frozenset({
            "recorded", "ordered", "received", "returned", "cancelled",
        }),
    ),
    "delivery": (
        "expected", frozenset({"expected", "delayed", "delivered", "cancelled"}),
    ),
    "vehicle": (
        "active", frozenset({"active", "sold", "inactive"}),
    ),
    "travel_document": (
        "active", frozenset({"active", "expired", "replaced", "revoked"}),
    ),
    "form": (
        "pending", frozenset({
            "draft", "pending", "submitted", "completed", "cancelled",
        }),
    ),
    "provider": (
        "active", frozenset({"active", "inactive"}),
    ),
    "household_routine": (
        "active", frozenset({"active", "paused", "completed", "cancelled"}),
    ),
    "emergency_information": (
        "active", frozenset({"active", "superseded", "archived"}),
    ),
}

_TERMINAL_RECORD_STATUSES: dict[str, frozenset[str]] = {
    "identity_document": frozenset({"replaced", "revoked"}),
    "insurance": frozenset({"cancelled"}),
    "warranty": frozenset({"claimed", "cancelled"}),
    "renewal": frozenset({"completed", "cancelled"}),
    "inventory_item": frozenset({"disposed", "lost", "donated"}),
    "repair": frozenset({"completed", "cancelled"}),
    "purchase": frozenset({"received", "returned", "cancelled"}),
    "delivery": frozenset({"delivered", "cancelled"}),
    "vehicle": frozenset({"sold", "inactive"}),
    "travel_document": frozenset({"replaced", "revoked"}),
    "form": frozenset({"submitted", "completed", "cancelled"}),
    "provider": frozenset({"inactive"}),
    "household_routine": frozenset({"paused", "completed", "cancelled"}),
    "emergency_information": frozenset({"superseded", "archived"}),
}

_EXPIRY_REQUIRED = frozenset({"insurance", "warranty", "travel_document"})
_DUE_REQUIRED = frozenset({
    "renewal", "delivery", "form", "household_routine",
})


def _luhn_valid(digits: str) -> bool:
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        number = int(char)
        if index % 2 == parity:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _assert_no_payment_number(value: str, *, field: str) -> None:
    for match in _PAYMENT_CANDIDATE_RE.finditer(value):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            raise LifeGraphError(
                f"{field} must not contain a full payment-card number; "
                "store only a masked reference"
            )


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
    if _URL_CREDENTIAL_RE.search(normalized):
        raise LifeGraphError(f"{field} must not contain embedded credentials")
    _assert_no_payment_number(normalized, field=field)
    return normalized


def _token(value: object, *, field: str, limit: int = 64) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    if not normalized or len(normalized) > limit or not _TOKEN_RE.fullmatch(normalized):
        raise LifeGraphError(
            f"{field} must be a lowercase token using letters, numbers, _, -, or ."
        )
    return normalized


def _datetime(
    value: object | None, *, field: str, required: bool = False
) -> datetime | None:
    if value is None or value == "":
        if required:
            raise LifeGraphError(f"{field} is required")
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise LifeGraphError(
                f"{field} must be an ISO-8601 date or datetime"
            ) from exc
    else:
        raise LifeGraphError(f"{field} must be an ISO-8601 date or datetime")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _iso(value: object | None, *, field: str) -> str | None:
    parsed = _datetime(value, field=field)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        return _iso(value, field="datetime")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    return value


def _assert_safe_payload(value: object, *, field: str = "payload") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if any(part in key for part in _SECRET_KEY_PARTS):
                raise LifeGraphError(
                    "Home records must not contain credentials, secrets, or full "
                    "financial-account identifiers"
                )
            if key in _EXECUTOR_KEYS:
                raise LifeGraphError(
                    "Home records cannot contain an autonomous executor payload"
                )
            _assert_safe_payload(child, field=f"{field}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_safe_payload(child, field=f"{field}[{index}]")
    elif isinstance(value, str):
        if _URL_CREDENTIAL_RE.search(value):
            raise LifeGraphError("Home records must not contain embedded credentials")
        _assert_no_payment_number(value, field=field)


def _bounded_object(
    value: object | None, *, field: str, max_bytes: int = 24_000
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LifeGraphError(f"{field} must be an object")
    result = _json_safe(dict(value))
    if not isinstance(result, dict):
        raise LifeGraphError(f"{field} must be an object")
    _assert_safe_payload(result, field=field)
    try:
        encoded = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise LifeGraphError(f"{field} must be JSON-serializable") from exc
    if len(encoded) > max_bytes:
        raise LifeGraphError(f"{field} must not exceed {max_bytes} bytes")
    return result


def _exact_fields(
    value: Mapping[str, Any], *, allowed: set[str] | frozenset[str], field: str
) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise LifeGraphError(f"Unsupported {field} fields: {', '.join(unknown)}")


def _masked_reference(value: object | None, *, field: str, maximum: int) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    compact = re.sub(r"[\s*•-]", "", raw)
    if not re.fullmatch(rf"[A-Za-z0-9]{{2,{maximum}}}", compact):
        raise LifeGraphError(
            f"{field} must contain only the final 2 to {maximum} letters or numbers"
        )
    return "••••" + compact.upper()


def _positive_number(value: object, *, field: str) -> float:
    if isinstance(value, bool):
        raise LifeGraphError(f"{field} must be a positive number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LifeGraphError(f"{field} must be a positive number") from exc
    if not (0 < number <= 1_000_000):
        raise LifeGraphError(f"{field} must be greater than 0 and at most 1000000")
    return number


def _details(value: object | None, *, record_type: str) -> dict[str, Any]:
    details = _bounded_object(value, field="details")
    _exact_fields(
        details, allowed=_DETAIL_FIELDS[record_type], field=f"{record_type} detail"
    )

    for name in _DETAIL_FIELDS[record_type]:
        if name in {"record_status", "quantity", "year", "identifier_last4", "vin_last6"}:
            continue
        if name in details:
            limit = 4_000 if name in {"coverage_summary", "instructions"} else 500
            details[name] = _text(
                details.get(name), field=f"details.{name}", limit=limit,
                preserve_lines=name in {"coverage_summary", "instructions"},
            )
    for name in _REQUIRED_DETAIL_FIELDS[record_type]:
        details[name] = _text(
            details.get(name), field=f"details.{name}",
            limit=4_000 if name == "instructions" else 500,
            required=True, preserve_lines=name == "instructions",
        )

    default_status, statuses = _STATUS_CHOICES[record_type]
    status = _token(
        details.get("record_status") or default_status,
        field="details.record_status",
    )
    if status not in statuses:
        raise LifeGraphError(
            "details.record_status must be one of: " + ", ".join(sorted(statuses))
        )
    details["record_status"] = status

    if "identifier_last4" in details:
        details["identifier_last4"] = _masked_reference(
            details.get("identifier_last4"), field="details.identifier_last4",
            maximum=6,
        )
    if "vin_last6" in details:
        details["vin_last6"] = _masked_reference(
            details.get("vin_last6"), field="details.vin_last6", maximum=6
        )
    if "quantity" in details:
        details["quantity"] = _positive_number(
            details.get("quantity"), field="details.quantity"
        )
    if "year" in details:
        if isinstance(details.get("year"), bool):
            raise LifeGraphError("details.year must be between 1886 and 2200")
        try:
            year = int(details.get("year"))
        except (TypeError, ValueError) as exc:
            raise LifeGraphError("details.year must be between 1886 and 2200") from exc
        if year < 1886 or year > 2200:
            raise LifeGraphError("details.year must be between 1886 and 2200")
        details["year"] = year
    return details


def _source(value: object) -> dict[str, Any]:
    source = _bounded_object(value, field="source", max_bytes=8_000)
    _exact_fields(
        source,
        allowed={"kind", "label", "source_id", "reference", "observed_at"},
        field="source",
    )
    kind = _token(source.get("kind"), field="source.kind")
    if kind not in _SOURCE_KINDS:
        raise LifeGraphError(
            "source.kind must be one of: " + ", ".join(sorted(_SOURCE_KINDS))
        )
    result = {
        "kind": kind,
        "label": _text(
            source.get("label"), field="source.label", limit=240, required=True
        ),
        "source_id": None,
        "reference": _text(
            source.get("reference"), field="source.reference", limit=1_000
        ) or None,
        "observed_at": _iso(source.get("observed_at"), field="source.observed_at"),
    }
    if source.get("source_id") is not None:
        source_id = str(source.get("source_id") or "").strip()
        if not _LOCAL_ID_RE.fullmatch(source_id):
            raise LifeGraphError("source.source_id is not a valid local identifier")
        result["source_id"] = source_id
    if kind in _EXTERNAL_SOURCE_KINDS and not (
        result["source_id"] or result["reference"]
    ):
        raise LifeGraphError(
            "External home sources require source_id or a bounded source reference"
        )
    return result


def _reference_list(
    value: object | None, *, field: str, maximum: int
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LifeGraphError(f"references.{field} must be a list")
    if len(value) > maximum:
        raise LifeGraphError(
            f"references.{field} must not contain more than {maximum} items"
        )
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = str(item or "").strip()
        if not _LOCAL_ID_RE.fullmatch(normalized):
            raise LifeGraphError(
                f"references.{field} contains an invalid local identifier"
            )
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


def _references(value: object | None) -> dict[str, list[str]]:
    references = _bounded_object(value, field="references", max_bytes=12_000)
    _exact_fields(
        references,
        allowed={"file_entity_ids", "document_ids", "entity_ids"},
        field="reference",
    )
    return {
        "file_entity_ids": _reference_list(
            references.get("file_entity_ids"), field="file_entity_ids", maximum=20
        ),
        "document_ids": _reference_list(
            references.get("document_ids"), field="document_ids", maximum=20
        ),
        "entity_ids": _reference_list(
            references.get("entity_ids"), field="entity_ids", maximum=50
        ),
    }


def _provenance(
    value: object | None, *, source: Mapping[str, Any]
) -> dict[str, Any]:
    provenance = _bounded_object(value, field="provenance", max_bytes=16_000)
    supplied_source_id = str(provenance.get("source_id") or "").strip() or None
    if supplied_source_id and supplied_source_id != source.get("source_id"):
        raise LifeGraphError("provenance.source_id must match source.source_id")
    provenance["domain"] = "home_admin"
    provenance["source_kind"] = source["kind"]
    provenance["source_label"] = source["label"]
    if source.get("source_id"):
        provenance["source_id"] = source["source_id"]
    else:
        provenance.pop("source_id", None)
    return provenance


def validate_home_properties(value: object) -> dict[str, Any]:
    properties = _bounded_object(value, field="home properties", max_bytes=48_000)
    _exact_fields(
        properties,
        allowed={
            "home_schema_version", "record_type", "effective_at", "expires_at",
            "due_at", "details", "references", "source",
        },
        field="home property",
    )
    supplied_version = properties.get("home_schema_version", HOME_SCHEMA_VERSION)
    if supplied_version != HOME_SCHEMA_VERSION:
        raise LifeGraphError("Unsupported home schema version")
    record_type = _token(properties.get("record_type"), field="record_type")
    if record_type not in HOME_RECORD_TYPES:
        raise LifeGraphError(
            "record_type must be one of: " + ", ".join(sorted(HOME_RECORD_TYPES))
        )
    effective_at = _datetime(
        properties.get("effective_at"), field="effective_at", required=True
    )
    expires_at = _datetime(properties.get("expires_at"), field="expires_at")
    due_at = _datetime(properties.get("due_at"), field="due_at")
    if record_type in _EXPIRY_REQUIRED and expires_at is None:
        raise LifeGraphError(f"{record_type} records require expires_at")
    if record_type in _DUE_REQUIRED and due_at is None:
        raise LifeGraphError(f"{record_type} records require due_at")
    if expires_at is not None and expires_at < effective_at:
        raise LifeGraphError("expires_at must not be before effective_at")
    if due_at is not None and due_at < effective_at:
        raise LifeGraphError("due_at must not be before effective_at")
    return {
        "home_schema_version": HOME_SCHEMA_VERSION,
        "record_type": record_type,
        "effective_at": _iso(effective_at, field="effective_at"),
        "expires_at": _iso(expires_at, field="expires_at"),
        "due_at": _iso(due_at, field="due_at"),
        "details": _details(properties.get("details"), record_type=record_type),
        "references": _references(properties.get("references")),
        "source": _source(properties.get("source")),
    }


def is_typed_home_payload(
    entity_type: object, properties: object | None = None
) -> bool:
    if str(entity_type or "").strip().lower() == HOME_ENTITY_TYPE:
        return True
    # Do not claim generic notes/assets merely because they happen to use a
    # ``record_type`` property with the same word. The schema marker is the
    # only typed signal outside the dedicated ``home_record`` entity type.
    return (
        isinstance(properties, Mapping)
        and properties.get("home_schema_version") is not None
    )


def _owned_home_record(db, owner_id: str, entity_id: object) -> LifeEntity:
    entity = get_life_entity(db, owner_id=owner_id, entity_id=entity_id)
    if entity.entity_type != HOME_ENTITY_TYPE:
        raise LifeGraphNotFound("Home record not found")
    validate_home_properties(entity.properties or {})
    return entity


def _validate_reference_authority(
    db,
    *,
    account: Account,
    properties: Mapping[str, Any],
    current_entity_id: str | None = None,
) -> None:
    source_id = properties["source"].get("source_id")
    if source_id and db.query(LifeSource.id).filter(
        LifeSource.id == source_id,
        LifeSource.owner_id == account.id,
    ).scalar() is None:
        raise LifeGraphNotFound("Home source not found")

    references = properties["references"]
    file_ids = set(references["file_entity_ids"])
    entity_ids = set(references["entity_ids"])
    if current_entity_id and current_entity_id in file_ids | entity_ids:
        raise LifeGraphError("A Home record cannot reference itself")
    all_entity_ids = file_ids | entity_ids
    if all_entity_ids:
        rows = db.query(LifeEntity).filter(
            LifeEntity.owner_id == account.id,
            LifeEntity.id.in_(all_entity_ids),
            LifeEntity.deleted_at.is_(None),
        ).all()
        by_id = {row.id: row for row in rows}
        if set(by_id) != all_entity_ids:
            raise LifeGraphNotFound("Referenced Life entity not found")
        if any(by_id[file_id].entity_type != "file" for file_id in file_ids):
            raise LifeGraphNotFound("Referenced file entity not found")

    document_ids = set(references["document_ids"])
    if document_ids:
        owned_document_ids = {
            row[0]
            for row in db.query(Document.id).filter(
                Document.id.in_(document_ids),
                Document.owner == account.username,
            ).all()
        }
        if owned_document_ids != document_ids:
            raise LifeGraphNotFound("Referenced document not found")


def _earliest_deadline(properties: Mapping[str, Any]) -> datetime | None:
    deadlines = [
        parsed
        for parsed in (
            _datetime(properties.get("due_at"), field="due_at"),
            _datetime(properties.get("expires_at"), field="expires_at"),
        )
        if parsed is not None
    ]
    return min(deadlines) if deadlines else None


def create_home_record(
    db,
    *,
    account: Account,
    record_type: object,
    title: object,
    effective_at: object,
    source: object,
    details: object | None = None,
    references: object | None = None,
    expires_at: object | None = None,
    due_at: object | None = None,
    note: object = "",
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    normalized_title = _text(
        title, field="title", limit=240, required=True
    )
    normalized_note = _text(
        note, field="note", limit=20_000, preserve_lines=True
    )
    normalized_sensitivity = _token(sensitivity, field="sensitivity")
    if normalized_sensitivity not in _SENSITIVITIES:
        raise LifeGraphError("Home record sensitivity must be private or restricted")
    properties = validate_home_properties({
        "record_type": record_type,
        "effective_at": effective_at,
        "expires_at": expires_at,
        "due_at": due_at,
        "details": details,
        "references": references,
        "source": source,
    })
    _validate_reference_authority(db, account=account, properties=properties)
    normalized_provenance = _provenance(provenance, source=properties["source"])
    return create_life_entity(
        db,
        account=account,
        entity_type=HOME_ENTITY_TYPE,
        title=normalized_title,
        summary=normalized_note,
        status="active",
        properties=properties,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=normalized_sensitivity,
        occurred_at=_datetime(effective_at, field="effective_at", required=True),
        due_at=_earliest_deadline(properties),
        idempotency_key=idempotency_key,
        reason="Home record captured",
    )


def update_home_record(
    db,
    *,
    account: Account,
    entity_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    entity = _owned_home_record(db, account.id, entity_id)
    allowed = {
        "title", "effective_at", "expires_at", "due_at", "details",
        "references", "source", "note", "provenance", "confidence",
        "sensitivity", "status",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported Home fields: {', '.join(unknown)}")
    _assert_safe_payload(changes, field="changes")
    current = validate_home_properties(entity.properties or {})
    merged = dict(current)
    for field in (
        "effective_at", "expires_at", "due_at", "details", "references", "source"
    ):
        if field in changes:
            merged[field] = changes[field]
    properties = validate_home_properties(merged)
    _validate_reference_authority(
        db,
        account=account,
        properties=properties,
        current_entity_id=entity.id,
    )
    provenance_value = changes.get("provenance", entity.provenance or {})
    if properties["source"] != current["source"] and "provenance" not in changes:
        provenance_value = dict(entity.provenance or {})
        provenance_value.pop("source_id", None)
    entity_changes: dict[str, Any] = {
        "properties": properties,
        "provenance": _provenance(provenance_value, source=properties["source"]),
        "occurred_at": _datetime(
            properties["effective_at"], field="effective_at", required=True
        ),
        "due_at": _earliest_deadline(properties),
    }
    if "title" in changes:
        entity_changes["title"] = _text(
            changes["title"], field="title", limit=240, required=True
        )
    if "note" in changes:
        entity_changes["summary"] = _text(
            changes["note"], field="note", limit=20_000, preserve_lines=True
        )
    if "confidence" in changes:
        entity_changes["confidence"] = changes["confidence"]
    if "sensitivity" in changes:
        sensitivity = _token(changes["sensitivity"], field="sensitivity")
        if sensitivity not in _SENSITIVITIES:
            raise LifeGraphError(
                "Home record sensitivity must be private or restricted"
            )
        entity_changes["sensitivity"] = sensitivity
    if "status" in changes:
        status = _token(changes["status"], field="status")
        if status not in _ENTITY_STATUSES:
            raise LifeGraphError("Home record status must be active or archived")
        entity_changes["status"] = status
    return update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=entity_changes,
        reason="Home record updated",
    )


def delete_home_record(
    db,
    *,
    owner_id: str,
    entity_id: object,
    expected_version: int,
    reason: object = "Home record deleted",
) -> LifeEntity:
    entity = _owned_home_record(db, owner_id, entity_id)
    return delete_life_entity(
        db,
        owner_id=owner_id,
        entity_id=entity.id,
        expected_version=expected_version,
        reason=_text(reason, field="reason", limit=500, required=True),
    )


def serialize_home_record(entity: LifeEntity) -> dict[str, Any]:
    if entity.entity_type != HOME_ENTITY_TYPE:
        raise LifeGraphError("Entity is not a Home record")
    properties = validate_home_properties(entity.properties or {})
    result = serialize_life_entity(entity)
    result.update({
        "note": entity.summary or "",
        "record_type": properties["record_type"],
        "effective_at": properties["effective_at"],
        "expires_at": properties["expires_at"],
        "due_at": properties["due_at"],
        "details": properties["details"],
        "references": properties["references"],
        "source": properties["source"],
        "record_only_notice": HOME_RECORD_ONLY_NOTICE,
        "execution_policy": {
            "record_only": True,
            "can_execute_external_action": False,
        },
    })
    return result


def get_home_record(db, *, owner_id: str, entity_id: object) -> dict[str, Any]:
    return serialize_home_record(_owned_home_record(db, owner_id, entity_id))


def _home_candidates(
    db,
    *,
    owner_id: str,
    record_type: object | None = None,
    status: object | None = None,
    include_archived: bool = False,
) -> tuple[list[LifeEntity], bool]:
    normalized_type = None
    if record_type:
        normalized_type = _token(record_type, field="record_type")
        if normalized_type not in HOME_RECORD_TYPES:
            raise LifeGraphError("Unsupported Home record_type")
    normalized_status = None
    if status:
        normalized_status = _token(status, field="status")
        if normalized_status not in _ENTITY_STATUSES:
            raise LifeGraphError("Home record status must be active or archived")
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == HOME_ENTITY_TYPE,
        LifeEntity.deleted_at.is_(None),
    )
    if normalized_status:
        query = query.filter(LifeEntity.status == normalized_status)
    elif not include_archived:
        query = query.filter(LifeEntity.status == "active")
    rows = query.order_by(
        LifeEntity.updated_at.desc(), LifeEntity.id.desc()
    ).limit(HOME_SCAN_LIMIT + 1).all()
    truncated = len(rows) > HOME_SCAN_LIMIT
    selected: list[LifeEntity] = []
    for row in rows[:HOME_SCAN_LIMIT]:
        properties = validate_home_properties(row.properties or {})
        if normalized_type and properties["record_type"] != normalized_type:
            continue
        selected.append(row)
    return selected, truncated


def list_home_records(
    db,
    *,
    owner_id: str,
    record_type: object | None = None,
    status: object | None = None,
    include_archived: bool = False,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    bounded = max(1, min(100, int(limit)))
    rows, scan_truncated = _home_candidates(
        db,
        owner_id=owner_id,
        record_type=record_type,
        status=status,
        include_archived=include_archived,
    )
    return [serialize_home_record(row) for row in rows[:bounded]], (
        scan_truncated or len(rows) > bounded
    )


def search_home_records(
    db,
    *,
    owner_id: str,
    query_text: object,
    record_type: object | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    needle = str(query_text or "").strip().casefold()
    if not needle:
        raise LifeGraphError("q is required")
    bounded = max(1, min(100, int(limit)))
    rows, scan_truncated = _home_candidates(
        db, owner_id=owner_id, record_type=record_type
    )
    matches: list[tuple[tuple[int, str, str], dict[str, Any]]] = []
    for row in rows:
        record = serialize_home_record(row)
        title = record["title"].casefold()
        note = record["note"].casefold()
        body = json.dumps({
            "record_type": record["record_type"],
            "details": record["details"],
            "source": record["source"],
        }, ensure_ascii=False, sort_keys=True).casefold()
        if title == needle:
            rank, field = 0, "title"
        elif title.startswith(needle):
            rank, field = 1, "title"
        elif needle in title:
            rank, field = 2, "title"
        elif needle in note:
            rank, field = 3, "note"
        elif needle in body:
            rank, field = 4, "properties"
        else:
            continue
        matches.append((
            (rank, title, row.id),
            {"record": record, "match": field, "rank": rank},
        ))
    matches.sort(key=lambda item: item[0])
    items = [item for _, item in matches[:bounded]]
    return {
        "items": items,
        "count": len(items),
        "scanned": len(rows),
        "truncated": scan_truncated or len(matches) > bounded,
        "record_only_notice": HOME_RECORD_ONLY_NOTICE,
    }


def _record_allows_alert(properties: Mapping[str, Any]) -> bool:
    record_type = str(properties["record_type"])
    status = str(properties["details"].get("record_status") or "")
    return status not in _TERMINAL_RECORD_STATUSES[record_type]


def home_alert_report(
    db,
    *,
    owner_id: str,
    as_of: object,
    horizon_days: int = 90,
    include_overdue: bool = True,
    record_type: object | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Return deterministic, source-backed due/expiry inputs.

    ``as_of`` is mandatory and no wall clock is consulted. The bounded horizon
    prevents an unbounded future scan and makes repeated calls reproducible.
    """
    reference_time = _datetime(as_of, field="as_of", required=True)
    if isinstance(horizon_days, bool):
        raise LifeGraphError(
            f"horizon_days must be between 0 and {HOME_ALERT_MAX_HORIZON_DAYS}"
        )
    try:
        bounded_horizon = int(horizon_days)
    except (TypeError, ValueError) as exc:
        raise LifeGraphError(
            f"horizon_days must be between 0 and {HOME_ALERT_MAX_HORIZON_DAYS}"
        ) from exc
    if bounded_horizon < 0 or bounded_horizon > HOME_ALERT_MAX_HORIZON_DAYS:
        raise LifeGraphError(
            f"horizon_days must be between 0 and {HOME_ALERT_MAX_HORIZON_DAYS}"
        )
    window_end = reference_time + timedelta(days=bounded_horizon)
    bounded_limit = max(1, min(100, int(limit)))
    rows, scan_truncated = _home_candidates(
        db, owner_id=owner_id, record_type=record_type
    )
    alerts: list[dict[str, Any]] = []
    for row in rows:
        properties = validate_home_properties(row.properties or {})
        if not _record_allows_alert(properties):
            continue
        for deadline_kind, field in (("due", "due_at"), ("expiry", "expires_at")):
            deadline = _datetime(properties.get(field), field=field)
            if deadline is None or deadline > window_end:
                continue
            if not include_overdue and deadline < reference_time:
                continue
            alerts.append({
                "id": f"{row.id}:{deadline_kind}",
                "record_id": row.id,
                "record_type": properties["record_type"],
                "title": row.title or "",
                "deadline_kind": deadline_kind,
                "deadline_at": _iso(deadline, field=field),
                "overdue": deadline < reference_time,
                "source": properties["source"],
                "provenance": row.provenance or {},
                "confidence": int(row.confidence or 0),
                "sensitivity": row.sensitivity,
                "version": int(row.version or 1),
            })
    alerts.sort(key=lambda item: (
        item["deadline_at"], item["deadline_kind"], item["record_id"]
    ))
    items = alerts[:bounded_limit]
    return {
        "items": items,
        "count": len(items),
        "as_of": _iso(reference_time, field="as_of"),
        "horizon_days": bounded_horizon,
        "window_end": _iso(window_end, field="window_end"),
        "include_overdue": bool(include_overdue),
        "scanned": len(rows),
        "truncated": scan_truncated or len(alerts) > bounded_limit,
        "rules": {
            "due": "due_at is on or before the bounded window end",
            "expiry": "expires_at is on or before the bounded window end",
            "overdue": "deadline_at is strictly before the explicit as_of",
            "terminal_records": "completed, cancelled, replaced, or otherwise terminal records are excluded",
        },
        "record_only_notice": HOME_RECORD_ONLY_NOTICE,
    }


def home_record_history(
    db,
    *,
    owner_id: str,
    entity_id: object,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    entity = _owned_home_record(db, owner_id, entity_id)
    rows, truncated = list_life_entity_versions(
        db, owner_id=owner_id, entity_id=entity.id, limit=limit
    )
    chronological = list(reversed(rows))
    result: list[dict[str, Any]] = []
    previous: Mapping[str, Any] | None = None
    for row in chronological:
        snapshot = dict(row.snapshot or {})
        properties = validate_home_properties(snapshot.get("properties") or {})
        changed: list[str] = []
        if previous is not None:
            previous_properties = validate_home_properties(
                previous.get("properties") or {}
            )
            for field in (
                "title", "summary", "status", "occurred_at", "due_at",
                "confidence", "sensitivity", "provenance",
            ):
                if previous.get(field) != snapshot.get(field):
                    if field not in changed:
                        changed.append(field)
            for field in (
                "effective_at", "expires_at", "due_at", "details",
                "references", "source",
            ):
                if previous_properties.get(field) != properties.get(field):
                    if field not in changed:
                        changed.append(field)
        created_at = row.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        else:
            created_at = created_at.astimezone(timezone.utc)
        result.append({
            "id": row.id,
            "version": int(row.version),
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
            "reason": row.reason or "",
            "kind": "created" if previous is None else "home_record_changed",
            "changed_fields": changed,
            "record": {
                "title": snapshot.get("title") or "",
                "status": snapshot.get("status") or "active",
                "record_type": properties["record_type"],
                "effective_at": properties["effective_at"],
                "expires_at": properties["expires_at"],
                "due_at": properties["due_at"],
            },
        })
        previous = snapshot
    return list(reversed(result)), truncated


__all__ = [
    "HOME_ALERT_MAX_HORIZON_DAYS",
    "HOME_ENTITY_TYPE",
    "HOME_RECORD_ONLY_NOTICE",
    "HOME_RECORD_TYPES",
    "HOME_SCHEMA_VERSION",
    "create_home_record",
    "delete_home_record",
    "get_home_record",
    "home_alert_report",
    "home_record_history",
    "is_typed_home_payload",
    "list_home_records",
    "search_home_records",
    "serialize_home_record",
    "update_home_record",
    "validate_home_properties",
]
