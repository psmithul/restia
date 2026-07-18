"""Typed V3 Relationship Manager records on Restia's canonical Life graph.

Relationship profiles, interactions, promises, and follow-ups remain encrypted,
owner-scoped ``LifeEntity`` rows.  Existing ``ContactRecord`` rows stay the
contact authority: profiles retain only a validated contact reference and
never copy email addresses, phone numbers, or connector credentials.

This module is deliberately record-only.  It has no message delivery surface;
any future personal message is a Level 5 external action requiring explicit
approval through the canonical action-policy flow.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any, Mapping

from core.database import (
    Account,
    ContactRecord,
    EntityLink,
    LifeEntity,
    LifeSource,
)
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    create_entity_link,
    create_life_entity,
    get_life_entity,
    list_entity_links,
    list_life_entity_versions,
    serialize_entity_link,
    serialize_life_entity,
    serialize_life_entity_version,
    update_life_entity,
)


RELATIONSHIP_SCHEMA_VERSION = 1
RELATIONSHIP_PROFILE_ENTITY_TYPE = "person"
RELATIONSHIP_SCAN_LIMIT = 500
RELATIONSHIP_RECORD_SCAN_LIMIT = 1_000
PERSONAL_MESSAGE_MIN_AUTONOMY = 5

RELATIONSHIP_RECORD_TYPES = {
    "profile": "person",
    "interaction": "interaction",
    "commitment": "commitment",
    "follow_up": "reminder",
}
SUBJECT_KINDS = frozenset({"person", "organization"})
PROFILE_STATUSES = frozenset({"active", "dormant", "archived"})
INTERACTION_STATUSES = frozenset({"recorded", "corrected", "archived"})
COMMITMENT_STATUSES = frozenset({"open", "fulfilled", "cancelled", "archived"})
FOLLOW_UP_STATUSES = frozenset({"open", "completed", "cancelled", "archived"})
INTERACTION_DIRECTIONS = frozenset({"inbound", "outbound", "mutual", "unknown"})
COMMITMENT_DIRECTIONS = frozenset({"made_by_me", "made_to_me", "mutual"})
FOLLOW_UP_PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
FOLLOW_UP_KINDS = frozenset({
    "follow_up", "unanswered_message", "project_relevance",
})
SENSITIVITIES = frozenset({"private", "restricted"})
CONTACT_ORIGIN_KINDS = frozenset({
    "manual", "contact", "email", "calendar", "telegram", "meeting",
    "referral", "import", "other",
})
LINK_RELATIONS = {
    "project": "related_project",
    "file": "related_file",
}

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_SECRET_KEY_PARTS = (
    "password", "secret", "token", "credential", "cookie", "authorization",
    "api_key", "private_key",
)
_EXTERNAL_ACTION_KEYS = frozenset({
    "send", "send_message", "send_email", "message_payload", "recipient",
    "execute", "executor", "tool_call", "webhook", "external_action",
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
    return normalized


def _token(value: object, *, field: str, limit: int = 64) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    if not normalized or len(normalized) > limit or not _TOKEN_RE.fullmatch(normalized):
        raise LifeGraphError(
            f"{field} must be a lowercase token using letters, numbers, _, -, or ."
        )
    return normalized


def _integer(
    value: object, *, field: str, minimum: int, maximum: int
) -> int:
    if isinstance(value, bool):
        raise LifeGraphError(f"{field} must be an integer from {minimum} to {maximum}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise LifeGraphError(
            f"{field} must be an integer from {minimum} to {maximum}"
        ) from exc
    if result < minimum or result > maximum:
        raise LifeGraphError(f"{field} must be an integer from {minimum} to {maximum}")
    return result


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
            raise LifeGraphError(f"{field} must be an ISO-8601 date or datetime") from exc
    else:
        raise LifeGraphError(f"{field} must be an ISO-8601 date or datetime")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _iso_datetime(value: object | None, *, field: str) -> str | None:
    parsed = _datetime(value, field=field)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _date(value: object, *, field: str) -> str:
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


def _is_external_action_key(value: object) -> bool:
    key = str(value or "").strip().lower().replace("-", "_")
    compact = re.sub(r"[^a-z0-9]", "", key)
    return (
        key in _EXTERNAL_ACTION_KEYS
        or key.startswith(("send_", "deliver_", "dispatch_"))
        or compact in {
            "send", "sendmessage", "sendemail", "messagepayload", "recipient",
            "execute", "executor", "toolcall", "webhook", "externalaction",
        }
    )


def _assert_safe_payload(value: object, *, field: str) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if any(part in key for part in _SECRET_KEY_PARTS):
                raise LifeGraphError(f"{field} must not contain credentials or secrets")
            if _is_external_action_key(key):
                raise LifeGraphError(
                    f"{field} cannot request messaging or another external action"
                )
            _assert_safe_payload(child, field=field)
    elif isinstance(value, list):
        for child in value:
            _assert_safe_payload(child, field=field)


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_json_safe(child) for child in value]
    return value


def _bounded_object(
    value: object | None, *, field: str, max_bytes: int = 24_000
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LifeGraphError(f"{field} must be an object")
    result = _json_safe(dict(value))
    if not isinstance(result, dict):  # Defensive: the input contract is a mapping.
        raise LifeGraphError(f"{field} must be an object")
    try:
        encoded = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise LifeGraphError(f"{field} must be JSON-serializable") from exc
    if len(encoded) > max_bytes:
        raise LifeGraphError(f"{field} must not exceed {max_bytes} bytes")
    _assert_safe_payload(result, field=field)
    return result


def _exact_fields(
    value: Mapping[str, Any], *, allowed: set[str], field: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported {field} fields: {', '.join(unknown)}")


def _owned_source(db, owner_id: str, source_id: object, *, field: str) -> LifeSource:
    source = db.query(LifeSource).filter(
        LifeSource.id == str(source_id or "").strip(),
        LifeSource.owner_id == owner_id,
    ).first()
    if source is None:
        raise LifeGraphNotFound(f"{field} source not found")
    return source


def _owned_contact(
    db,
    owner_id: str,
    contact_record_id: object,
    *,
    include_deleted: bool = False,
) -> ContactRecord:
    query = db.query(ContactRecord).filter(
        ContactRecord.id == str(contact_record_id or "").strip(),
        ContactRecord.owner_id == owner_id,
    )
    if not include_deleted:
        query = query.filter(ContactRecord.deleted_at.is_(None))
    contact = query.first()
    if contact is None:
        raise LifeGraphNotFound("Contact record not found")
    return contact


def _contact_name(contact: ContactRecord) -> str:
    payload = contact.payload if isinstance(contact.payload, Mapping) else {}
    return _text(payload.get("name"), field="contact name", limit=240)


def _source_id(
    db, owner_id: str, value: object, *, field: str
) -> str:
    source_id = _text(value, field=f"{field}.source_id", limit=36, required=True)
    _owned_source(db, owner_id, source_id, field=field)
    return source_id


def _normalize_provenance(
    db,
    *,
    owner_id: str,
    value: object | None,
    record_kind: str,
    required: bool,
) -> dict[str, Any]:
    provenance = _bounded_object(value, field="provenance", max_bytes=16_000)
    source_id = provenance.get("source_id")
    if source_id is None and required:
        raise LifeGraphError("provenance.source_id is required for relationship records")
    if source_id is not None:
        provenance["source_id"] = _source_id(
            db, owner_id, source_id, field="provenance"
        )
    provenance["domain"] = "relationships"
    provenance["record_kind"] = record_kind
    provenance.setdefault("capture", "manual")
    return provenance


def _validate_stored_provenance(
    db,
    *,
    owner_id: str,
    value: object,
    record_kind: str,
    required: bool,
) -> dict[str, Any]:
    provenance = _bounded_object(value, field="provenance", max_bytes=16_000)
    if provenance.get("domain") != "relationships":
        raise LifeGraphError("Relationship provenance domain is malformed")
    if provenance.get("record_kind") != record_kind:
        raise LifeGraphError("Relationship provenance record kind is malformed")
    source_id = provenance.get("source_id")
    if source_id is None:
        if required:
            raise LifeGraphError("Relationship record provenance is not source-backed")
    else:
        _source_id(db, owner_id, source_id, field="provenance")
    return provenance


def _contact_origin(
    db,
    *,
    owner_id: str,
    value: object,
    contact_record_id: str | None,
) -> dict[str, Any]:
    raw = _bounded_object(value, field="contact_origin", max_bytes=8_000)
    _exact_fields(
        raw,
        allowed={"kind", "label", "source_id", "observed_at"},
        field="contact_origin",
    )
    kind = _token(raw.get("kind"), field="contact_origin.kind")
    if kind not in CONTACT_ORIGIN_KINDS:
        raise LifeGraphError(
            "contact_origin.kind must be one of: "
            + ", ".join(sorted(CONTACT_ORIGIN_KINDS))
        )
    source_id = raw.get("source_id")
    if source_id is None and contact_record_id is None:
        raise LifeGraphError(
            "contact_origin.source_id is required without a contact_record_id"
        )
    return {
        "kind": kind,
        "label": _text(
            raw.get("label"), field="contact_origin.label", limit=240, required=True
        ),
        "source_id": (
            _source_id(db, owner_id, source_id, field="contact_origin")
            if source_id is not None else None
        ),
        "observed_at": _iso_datetime(
            raw.get("observed_at"), field="contact_origin.observed_at"
        ),
    }


def _important_dates(db, owner_id: str, value: object | None) -> list[dict[str, Any]]:
    if value is None:
        rows: list[object] = []
    elif isinstance(value, list):
        rows = value
    else:
        raise LifeGraphError("important_dates must be a list")
    if len(rows) > 40:
        raise LifeGraphError("important_dates must not contain more than 40 items")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        raw = _bounded_object(row, field="important_date", max_bytes=4_000)
        _exact_fields(
            raw,
            allowed={"label", "date", "recurring_annually", "source_id"},
            field="important_date",
        )
        item = {
            "label": _text(
                raw.get("label"), field="important_date.label", limit=160,
                required=True,
            ),
            "date": _date(raw.get("date"), field="important_date.date"),
            "recurring_annually": bool(raw.get("recurring_annually", False)),
            "source_id": _source_id(
                db, owner_id, raw.get("source_id"), field="important_date"
            ),
        }
        key = (item["label"].casefold(), item["date"])
        if key in seen:
            raise LifeGraphError("important_dates must be unique by label and date")
        seen.add(key)
        result.append(item)
    return result


def _preferences(db, owner_id: str, value: object | None) -> list[dict[str, str]]:
    if value is None:
        rows: list[object] = []
    elif isinstance(value, list):
        rows = value
    else:
        raise LifeGraphError("preferences must be a list")
    if len(rows) > 60:
        raise LifeGraphError("preferences must not contain more than 60 items")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        raw = _bounded_object(row, field="preference", max_bytes=4_000)
        _exact_fields(
            raw,
            allowed={"key", "value", "source_id"},
            field="preference",
        )
        key = _token(raw.get("key"), field="preference.key")
        if any(part in key for part in _SECRET_KEY_PARTS):
            raise LifeGraphError("preferences must not contain credentials or secrets")
        if _is_external_action_key(key):
            raise LifeGraphError(
                "preferences cannot request messaging or another external action"
            )
        if key in seen:
            raise LifeGraphError("preference keys must be unique")
        seen.add(key)
        result.append({
            "key": key,
            "value": _text(
                raw.get("value"), field="preference.value", limit=1_000,
                required=True, preserve_lines=True,
            ),
            "source_id": _source_id(
                db, owner_id, raw.get("source_id"), field="preference"
            ),
        })
    return result


def _care_plan(
    db, owner_id: str, value: object | None
) -> dict[str, Any] | None:
    if value is None:
        return None
    raw = _bounded_object(value, field="care_plan", max_bytes=4_000)
    _exact_fields(
        raw,
        allowed={"interval_days", "next_due_at", "source_id"},
        field="care_plan",
    )
    next_due_at = _iso_datetime(
        raw.get("next_due_at"), field="care_plan.next_due_at"
    )
    if next_due_at is None:
        raise LifeGraphError("care_plan.next_due_at is required")
    return {
        "interval_days": _integer(
            raw.get("interval_days"), field="care_plan.interval_days",
            minimum=1, maximum=3_650,
        ),
        "next_due_at": next_due_at,
        "source_id": _source_id(
            db, owner_id, raw.get("source_id"), field="care_plan"
        ),
    }


def validate_profile_properties(
    db, *, owner_id: str, value: object
) -> dict[str, Any]:
    raw = _bounded_object(value, field="relationship profile properties")
    _exact_fields(
        raw,
        allowed={
            "relationship_schema_version", "relationship_record_kind",
            "subject_kind", "relationship_type", "contact_record_id",
            "contact_origin", "important_dates", "preferences", "care_plan",
        },
        field="relationship profile",
    )
    if raw.get("relationship_schema_version") != RELATIONSHIP_SCHEMA_VERSION:
        raise LifeGraphError("Unsupported relationship schema version")
    if raw.get("relationship_record_kind") != "profile":
        raise LifeGraphError("relationship_record_kind must be profile")
    subject_kind = _token(raw.get("subject_kind"), field="subject_kind")
    if subject_kind not in SUBJECT_KINDS:
        raise LifeGraphError("subject_kind must be person or organization")
    contact_record_id = raw.get("contact_record_id")
    normalized_contact_id = None
    if contact_record_id is not None:
        if subject_kind != "person":
            raise LifeGraphError("Only person profiles can reference a contact record")
        normalized_contact_id = _owned_contact(
            db, owner_id, contact_record_id, include_deleted=True
        ).id
    properties = {
        "relationship_schema_version": RELATIONSHIP_SCHEMA_VERSION,
        "relationship_record_kind": "profile",
        "subject_kind": subject_kind,
        "relationship_type": _token(
            raw.get("relationship_type"), field="relationship_type"
        ),
        "contact_record_id": normalized_contact_id,
        "contact_origin": _contact_origin(
            db,
            owner_id=owner_id,
            value=raw.get("contact_origin"),
            contact_record_id=normalized_contact_id,
        ),
        "important_dates": _important_dates(
            db, owner_id, raw.get("important_dates")
        ),
        "preferences": _preferences(db, owner_id, raw.get("preferences")),
        "care_plan": _care_plan(db, owner_id, raw.get("care_plan")),
    }
    _bounded_object(properties, field="relationship profile properties")
    return properties


def is_typed_relationship_payload(
    entity_type: object, properties: object | None = None
) -> bool:
    """Detect typed relationship records without claiming generic person nodes."""

    if not isinstance(properties, Mapping):
        return False
    normalized_type = str(entity_type or "").strip().lower()
    marker = properties.get("relationship_schema_version") is not None
    kind = properties.get("relationship_record_kind")
    child_hint = any(
        key in properties for key in ("relationship_profile_id", "profile_id")
    ) and normalized_type in {"interaction", "commitment", "reminder"}
    return bool(marker or kind is not None or child_hint)


def _owned_profile(
    db, owner_id: str, entity_id: object, *, include_deleted: bool = False
) -> LifeEntity:
    try:
        entity = get_life_entity(
            db, owner_id=owner_id, entity_id=entity_id,
            include_deleted=include_deleted,
        )
    except LifeGraphNotFound as exc:
        raise LifeGraphNotFound("Relationship profile not found") from exc
    if entity.entity_type != RELATIONSHIP_PROFILE_ENTITY_TYPE:
        raise LifeGraphNotFound("Relationship profile not found")
    validate_profile_properties(db, owner_id=owner_id, value=entity.properties or {})
    _validate_stored_provenance(
        db,
        owner_id=owner_id,
        value=entity.provenance or {},
        record_kind="profile",
        required=False,
    )
    return entity


def _normalize_link_targets(
    db,
    *,
    owner_id: str,
    organization_entity_id: object | None,
    linked_entity_ids: object | None,
) -> list[tuple[str, LifeEntity]]:
    raw_ids: list[object]
    if linked_entity_ids is None:
        raw_ids = []
    elif isinstance(linked_entity_ids, list):
        raw_ids = linked_entity_ids
    else:
        raise LifeGraphError("linked_entity_ids must be a list")
    if len(raw_ids) > 50:
        raise LifeGraphError("linked_entity_ids must not contain more than 50 items")
    targets: list[tuple[str, LifeEntity]] = []
    seen: set[str] = set()
    for raw_id in raw_ids:
        target = get_life_entity(db, owner_id=owner_id, entity_id=raw_id)
        relation = LINK_RELATIONS.get(target.entity_type)
        if relation is None:
            raise LifeGraphError("Relationship context links must target projects or files")
        if target.id in seen:
            continue
        seen.add(target.id)
        targets.append((relation, target))
    if organization_entity_id is not None:
        organization = _owned_profile(db, owner_id, organization_entity_id)
        properties = validate_profile_properties(
            db, owner_id=owner_id, value=organization.properties or {}
        )
        if properties["subject_kind"] != "organization":
            raise LifeGraphError("organization_entity_id must reference an organization profile")
        if organization.id in seen:
            raise LifeGraphError("organization_entity_id must be separate from context links")
        targets.append(("member_of", organization))
    return targets


def _create_links(
    db,
    *,
    account: Account,
    profile: LifeEntity,
    targets: list[tuple[str, LifeEntity]],
    provenance: Mapping[str, Any],
    confidence: object,
    sensitivity: object,
) -> list[EntityLink]:
    result: list[EntityLink] = []
    for relation, target in targets:
        link, _ = create_entity_link(
            db,
            account=account,
            source_id=profile.id,
            relation=relation,
            target_id=target.id,
            provenance=dict(provenance),
            confidence=confidence,
            sensitivity=sensitivity,
            reason="Relationship context linked",
        )
        result.append(link)
    return result


def create_relationship_profile(
    db,
    *,
    account: Account,
    title: object,
    subject_kind: object,
    relationship_type: object,
    contact_origin: object,
    contact_record_id: object | None = None,
    important_dates: object | None = None,
    preferences: object | None = None,
    care_plan: object | None = None,
    organization_entity_id: object | None = None,
    linked_entity_ids: object | None = None,
    private_notes: object = "",
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    normalized_subject = _token(subject_kind, field="subject_kind")
    if contact_record_id is not None:
        _owned_contact(db, account.id, contact_record_id)
    raw_properties = {
        "relationship_schema_version": RELATIONSHIP_SCHEMA_VERSION,
        "relationship_record_kind": "profile",
        "subject_kind": normalized_subject,
        "relationship_type": relationship_type,
        "contact_record_id": contact_record_id,
        "contact_origin": contact_origin,
        "important_dates": important_dates,
        "preferences": preferences,
        "care_plan": care_plan,
    }
    properties = validate_profile_properties(
        db, owner_id=account.id, value=raw_properties
    )
    contact = (
        _owned_contact(
            db, account.id, properties["contact_record_id"], include_deleted=True
        )
        if properties["contact_record_id"] else None
    )
    normalized_title = _contact_name(contact) if contact is not None else ""
    if not normalized_title:
        normalized_title = _text(title, field="title", limit=240, required=True)
    normalized_provenance = _normalize_provenance(
        db,
        owner_id=account.id,
        value=provenance,
        record_kind="profile",
        required=False,
    )
    origin_source = properties["contact_origin"].get("source_id")
    if origin_source and normalized_provenance.get("source_id") is None:
        normalized_provenance["source_id"] = origin_source
    targets = _normalize_link_targets(
        db,
        owner_id=account.id,
        organization_entity_id=organization_entity_id,
        linked_entity_ids=linked_entity_ids,
    )
    normalized_sensitivity = _token(
        sensitivity, field="sensitivity", limit=24
    )
    if normalized_sensitivity not in SENSITIVITIES:
        raise LifeGraphError("Relationship sensitivity must be private or restricted")
    entity, created = create_life_entity(
        db,
        account=account,
        entity_type=RELATIONSHIP_PROFILE_ENTITY_TYPE,
        title=normalized_title,
        summary=_text(
            private_notes, field="private_notes", limit=20_000,
            preserve_lines=True,
        ),
        status="active",
        properties=properties,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=normalized_sensitivity,
        idempotency_key=idempotency_key,
        reason="Relationship profile created",
    )
    if not created:
        existing_links, _ = list_entity_links(
            db, owner_id=account.id, entity_id=entity.id,
            direction="outgoing", limit=100,
        )
        actual = {
            (row.relation, row.target_id)
            for row in existing_links
            if row.relation in {"member_of", "related_project", "related_file"}
        }
        requested = {(relation, target.id) for relation, target in targets}
        if requested != actual:
            raise LifeGraphConflict(
                "Relationship idempotency key was already used with different links"
            )
        return entity, False
    _create_links(
        db,
        account=account,
        profile=entity,
        targets=targets,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=normalized_sensitivity,
    )
    return entity, created


def update_relationship_profile(
    db,
    *,
    account: Account,
    entity_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    entity = _owned_profile(db, account.id, entity_id)
    allowed = {
        "title", "relationship_type", "contact_origin", "important_dates",
        "preferences", "care_plan", "private_notes", "provenance",
        "confidence", "sensitivity", "status",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported relationship fields: {', '.join(unknown)}")
    current = validate_profile_properties(
        db, owner_id=account.id, value=entity.properties or {}
    )
    merged = dict(current)
    for field in (
        "relationship_type", "contact_origin", "important_dates",
        "preferences", "care_plan",
    ):
        if field in changes:
            merged[field] = changes[field]
    properties = validate_profile_properties(db, owner_id=account.id, value=merged)
    entity_changes: dict[str, Any] = {"properties": properties}
    if "title" in changes:
        entity_changes["title"] = _text(
            changes["title"], field="title", limit=240, required=True
        )
    if "private_notes" in changes:
        entity_changes["summary"] = _text(
            changes["private_notes"], field="private_notes", limit=20_000,
            preserve_lines=True,
        )
    if "provenance" in changes:
        entity_changes["provenance"] = _normalize_provenance(
            db, owner_id=account.id, value=changes["provenance"],
            record_kind="profile", required=False,
        )
    if "confidence" in changes:
        entity_changes["confidence"] = changes["confidence"]
    if "sensitivity" in changes:
        sensitivity = _token(
            changes["sensitivity"], field="sensitivity", limit=24
        )
        if sensitivity not in SENSITIVITIES:
            raise LifeGraphError("Relationship sensitivity must be private or restricted")
        entity_changes["sensitivity"] = sensitivity
    if "status" in changes:
        status = _token(changes["status"], field="status", limit=32)
        if status not in PROFILE_STATUSES:
            raise LifeGraphError("Relationship status must be active, dormant, or archived")
        entity_changes["status"] = status
    return update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=entity_changes,
        reason="Relationship profile updated",
    )


def link_relationship_context(
    db,
    *,
    account: Account,
    profile_id: object,
    target_id: object,
    provenance: object | None,
    confidence: object = 100,
    sensitivity: object = "private",
) -> tuple[EntityLink, bool]:
    profile = _owned_profile(db, account.id, profile_id)
    target = get_life_entity(db, owner_id=account.id, entity_id=target_id)
    if target.entity_type in LINK_RELATIONS:
        relation = LINK_RELATIONS[target.entity_type]
    elif target.entity_type == RELATIONSHIP_PROFILE_ENTITY_TYPE:
        target_properties = validate_profile_properties(
            db, owner_id=account.id, value=target.properties or {}
        )
        if target_properties["subject_kind"] != "organization":
            raise LifeGraphError("Relationship profile links can only target organizations")
        relation = "member_of"
    else:
        raise LifeGraphError(
            "Relationship context links must target projects, files, or organizations"
        )
    normalized_provenance = _normalize_provenance(
        db, owner_id=account.id, value=provenance,
        record_kind="profile_link", required=True,
    )
    normalized_sensitivity = _token(
        sensitivity, field="sensitivity", limit=24
    )
    if normalized_sensitivity not in SENSITIVITIES:
        raise LifeGraphError("Relationship sensitivity must be private or restricted")
    return create_entity_link(
        db,
        account=account,
        source_id=profile.id,
        relation=relation,
        target_id=target.id,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=normalized_sensitivity,
        reason="Relationship context linked",
    )


def _profile_links(db, owner_id: str, profile: LifeEntity) -> list[dict[str, Any]]:
    rows, _ = list_entity_links(
        db, owner_id=owner_id, entity_id=profile.id,
        direction="outgoing", limit=100,
    )
    result: list[dict[str, Any]] = []
    for link in rows:
        if link.relation not in {"member_of", "related_project", "related_file"}:
            continue
        target = get_life_entity(db, owner_id=owner_id, entity_id=link.target_id)
        result.append({
            "link": serialize_entity_link(link),
            "target": serialize_life_entity(target),
        })
    return result


def _last_interaction_at(db, owner_id: str, profile_id: str) -> str | None:
    rows = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == "interaction",
        LifeEntity.deleted_at.is_(None),
    ).order_by(LifeEntity.occurred_at.desc(), LifeEntity.id.desc()).limit(
        RELATIONSHIP_RECORD_SCAN_LIMIT
    ).all()
    for row in rows:
        properties = row.properties if isinstance(row.properties, Mapping) else {}
        if (
            properties.get("relationship_schema_version") == RELATIONSHIP_SCHEMA_VERSION
            and properties.get("relationship_record_kind") == "interaction"
            and properties.get("relationship_profile_id") == profile_id
        ):
            try:
                _validate_stored_provenance(
                    db,
                    owner_id=owner_id,
                    value=row.provenance or {},
                    record_kind="interaction",
                    required=True,
                )
            except LifeGraphError:
                continue
            return _iso_datetime(row.occurred_at, field="occurred_at")
    return None


def serialize_relationship_profile(
    db, *, owner_id: str, entity: LifeEntity, include_links: bool = True
) -> dict[str, Any]:
    properties = validate_profile_properties(
        db, owner_id=owner_id, value=entity.properties or {}
    )
    payload = serialize_life_entity(entity)
    contact = None
    if properties["contact_record_id"]:
        row = _owned_contact(
            db, owner_id, properties["contact_record_id"], include_deleted=True
        )
        contact = {
            "id": row.id,
            "source_id": row.source_id,
            "version": int(row.version or 1),
            "name": _contact_name(row),
        }
    payload.update({
        "subject_kind": properties["subject_kind"],
        "relationship_type": properties["relationship_type"],
        "contact_authority": contact,
        "contact_origin": properties["contact_origin"],
        "important_dates": properties["important_dates"],
        "preferences": properties["preferences"],
        "care_plan": properties["care_plan"],
        "private_notes": payload["summary"],
        "last_interaction_at": _last_interaction_at(db, owner_id, entity.id),
        "links": _profile_links(db, owner_id, entity) if include_links else [],
        "execution_policy": {
            "record_only": True,
            "can_send_personal_message": False,
            "future_personal_message_min_autonomy": PERSONAL_MESSAGE_MIN_AUTONOMY,
            "future_personal_message_requires_confirmation": True,
        },
    })
    return payload


def get_relationship_profile(db, *, owner_id: str, entity_id: object) -> dict[str, Any]:
    return serialize_relationship_profile(
        db, owner_id=owner_id, entity=_owned_profile(db, owner_id, entity_id)
    )


def list_relationship_profiles(
    db,
    *,
    owner_id: str,
    subject_kind: object | None = None,
    status: object | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    normalized_subject = None
    if subject_kind is not None:
        normalized_subject = _token(subject_kind, field="subject_kind")
        if normalized_subject not in SUBJECT_KINDS:
            raise LifeGraphError("subject_kind must be person or organization")
    normalized_status = None
    if status is not None:
        normalized_status = _token(status, field="status", limit=32)
        if normalized_status not in PROFILE_STATUSES:
            raise LifeGraphError("Unsupported relationship status")
    bounded = max(1, min(100, int(limit)))
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == RELATIONSHIP_PROFILE_ENTITY_TYPE,
        LifeEntity.deleted_at.is_(None),
    )
    if normalized_status:
        query = query.filter(LifeEntity.status == normalized_status)
    rows = query.order_by(LifeEntity.updated_at.desc(), LifeEntity.id.desc()).limit(
        RELATIONSHIP_SCAN_LIMIT + 1
    ).all()
    scan_truncated = len(rows) > RELATIONSHIP_SCAN_LIMIT
    result: list[dict[str, Any]] = []
    for row in rows[:RELATIONSHIP_SCAN_LIMIT]:
        try:
            properties = validate_profile_properties(
                db, owner_id=owner_id, value=row.properties or {}
            )
        except LifeGraphError:
            continue
        if normalized_subject and properties["subject_kind"] != normalized_subject:
            continue
        result.append(serialize_relationship_profile(
            db, owner_id=owner_id, entity=row, include_links=False
        ))
    return result[:bounded], scan_truncated or len(result) > bounded


def _record_properties(
    db,
    *,
    owner_id: str,
    record_kind: str,
    profile_id: object,
    channel: object | None = None,
    direction: object | None = None,
    priority: object | None = None,
    reminder_kind: object | None = None,
) -> dict[str, Any]:
    _owned_profile(db, owner_id, profile_id)
    result: dict[str, Any] = {
        "relationship_schema_version": RELATIONSHIP_SCHEMA_VERSION,
        "relationship_record_kind": record_kind,
        "relationship_profile_id": str(profile_id),
    }
    if record_kind == "interaction":
        result["channel"] = _token(channel, field="channel")
        normalized_direction = _token(direction or "unknown", field="direction")
        if normalized_direction not in INTERACTION_DIRECTIONS:
            raise LifeGraphError(
                "interaction direction must be inbound, outbound, mutual, or unknown"
            )
        result["direction"] = normalized_direction
    elif record_kind == "commitment":
        normalized_direction = _token(direction, field="direction")
        if normalized_direction not in COMMITMENT_DIRECTIONS:
            raise LifeGraphError(
                "commitment direction must be made_by_me, made_to_me, or mutual"
            )
        result["direction"] = normalized_direction
    elif record_kind == "follow_up":
        normalized_priority = _token(priority or "normal", field="priority")
        if normalized_priority not in FOLLOW_UP_PRIORITIES:
            raise LifeGraphError("follow-up priority must be low, normal, high, or urgent")
        result["priority"] = normalized_priority
        normalized_reminder_kind = _token(
            reminder_kind or "follow_up", field="reminder_kind"
        )
        if normalized_reminder_kind not in FOLLOW_UP_KINDS:
            raise LifeGraphError(
                "reminder_kind must be follow_up, unanswered_message, or project_relevance"
            )
        result["reminder_kind"] = normalized_reminder_kind
    else:
        raise LifeGraphError("Unsupported relationship record kind")
    return result


def _create_relationship_record(
    db,
    *,
    account: Account,
    profile_id: object,
    record_kind: str,
    title: object,
    note: object,
    occurred_at: object | None,
    due_at: object | None,
    channel: object | None,
    direction: object | None,
    priority: object | None,
    reminder_kind: object | None,
    provenance: object | None,
    confidence: object,
    sensitivity: object,
    idempotency_key: object | None,
) -> tuple[LifeEntity, bool]:
    profile = _owned_profile(db, account.id, profile_id)
    properties = _record_properties(
        db,
        owner_id=account.id,
        record_kind=record_kind,
        profile_id=profile.id,
        channel=channel,
        direction=direction,
        priority=priority,
        reminder_kind=reminder_kind,
    )
    normalized_provenance = _normalize_provenance(
        db, owner_id=account.id, value=provenance,
        record_kind=record_kind, required=True,
    )
    if record_kind == "interaction":
        normalized_occurred_at = _datetime(
            occurred_at, field="occurred_at", required=True
        )
        normalized_due_at = None
        status = "recorded"
        relation = "has_interaction"
    else:
        normalized_occurred_at = _datetime(
            occurred_at, field="occurred_at"
        ) or datetime.utcnow()
        normalized_due_at = _datetime(due_at, field="due_at", required=True)
        status = "open"
        relation = "has_commitment" if record_kind == "commitment" else "has_follow_up"
    normalized_sensitivity = _token(
        sensitivity, field="sensitivity", limit=24
    )
    if normalized_sensitivity not in SENSITIVITIES:
        raise LifeGraphError("Relationship sensitivity must be private or restricted")
    entity, created = create_life_entity(
        db,
        account=account,
        entity_type=RELATIONSHIP_RECORD_TYPES[record_kind],
        title=_text(title, field="title", limit=240, required=True),
        summary=_text(note, field="note", limit=20_000, preserve_lines=True),
        status=status,
        properties=properties,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=normalized_sensitivity,
        occurred_at=normalized_occurred_at,
        due_at=normalized_due_at,
        idempotency_key=idempotency_key,
        reason=f"Relationship {record_kind.replace('_', ' ')} recorded",
    )
    create_entity_link(
        db,
        account=account,
        source_id=profile.id,
        relation=relation,
        target_id=entity.id,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=normalized_sensitivity,
        reason=f"Relationship {record_kind.replace('_', ' ')} linked",
    )
    return entity, created


def create_interaction(
    db, *, account: Account, profile_id: object, title: object,
    occurred_at: object, channel: object, direction: object = "unknown",
    note: object = "", provenance: object | None = None,
    confidence: object = 100, sensitivity: object = "private",
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    return _create_relationship_record(
        db, account=account, profile_id=profile_id, record_kind="interaction",
        title=title, note=note, occurred_at=occurred_at, due_at=None,
        channel=channel, direction=direction, priority=None, reminder_kind=None,
        provenance=provenance, confidence=confidence, sensitivity=sensitivity,
        idempotency_key=idempotency_key,
    )


def create_commitment(
    db, *, account: Account, profile_id: object, title: object,
    due_at: object, direction: object, occurred_at: object | None = None,
    note: object = "", provenance: object | None = None,
    confidence: object = 100, sensitivity: object = "private",
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    return _create_relationship_record(
        db, account=account, profile_id=profile_id, record_kind="commitment",
        title=title, note=note, occurred_at=occurred_at, due_at=due_at,
        channel=None, direction=direction, priority=None, reminder_kind=None,
        provenance=provenance, confidence=confidence, sensitivity=sensitivity,
        idempotency_key=idempotency_key,
    )


def create_follow_up(
    db, *, account: Account, profile_id: object, title: object,
    due_at: object, priority: object = "normal", occurred_at: object | None = None,
    reminder_kind: object = "follow_up",
    note: object = "", provenance: object | None = None,
    confidence: object = 100, sensitivity: object = "private",
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    normalized_kind = _token(reminder_kind, field="reminder_kind")
    if normalized_kind == "project_relevance":
        profile = _owned_profile(db, account.id, profile_id)
        links, _ = list_entity_links(
            db, owner_id=account.id, entity_id=profile.id,
            direction="outgoing", relation="related_project", limit=1,
        )
        if not links:
            raise LifeGraphError(
                "project_relevance reminders require an owner-scoped related project"
            )
    return _create_relationship_record(
        db, account=account, profile_id=profile_id, record_kind="follow_up",
        title=title, note=note, occurred_at=occurred_at, due_at=due_at,
        channel=None, direction=None, priority=priority,
        reminder_kind=normalized_kind,
        provenance=provenance, confidence=confidence, sensitivity=sensitivity,
        idempotency_key=idempotency_key,
    )


def _owned_relationship_record(
    db, owner_id: str, entity_id: object
) -> tuple[LifeEntity, str, dict[str, Any]]:
    try:
        entity = get_life_entity(db, owner_id=owner_id, entity_id=entity_id)
    except LifeGraphNotFound as exc:
        raise LifeGraphNotFound("Relationship record not found") from exc
    properties = entity.properties if isinstance(entity.properties, Mapping) else {}
    if properties.get("relationship_schema_version") != RELATIONSHIP_SCHEMA_VERSION:
        raise LifeGraphNotFound("Relationship record not found")
    kind = str(properties.get("relationship_record_kind") or "")
    expected_type = RELATIONSHIP_RECORD_TYPES.get(kind)
    if kind == "profile" or expected_type != entity.entity_type:
        raise LifeGraphNotFound("Relationship record not found")
    normalized = _record_properties(
        db,
        owner_id=owner_id,
        record_kind=kind,
        profile_id=properties.get("relationship_profile_id"),
        channel=properties.get("channel"),
        direction=properties.get("direction"),
        priority=properties.get("priority"),
        reminder_kind=properties.get("reminder_kind"),
    )
    if normalized != dict(properties):
        raise LifeGraphError("Relationship record properties are malformed")
    _validate_stored_provenance(
        db,
        owner_id=owner_id,
        value=entity.provenance or {},
        record_kind=kind,
        required=True,
    )
    return entity, kind, normalized


def serialize_relationship_record(db, *, owner_id: str, entity: LifeEntity) -> dict[str, Any]:
    owned, kind, properties = _owned_relationship_record(db, owner_id, entity.id)
    payload = serialize_life_entity(owned)
    payload.update({
        "record_kind": kind,
        "profile_id": properties["relationship_profile_id"],
        "note": payload["summary"],
        "channel": properties.get("channel"),
        "direction": properties.get("direction"),
        "priority": properties.get("priority"),
        "reminder_kind": properties.get("reminder_kind"),
        "source_backed": bool(payload["provenance"].get("source_id")),
        "execution_policy": {
            "record_only": True,
            "can_send_personal_message": False,
            "future_personal_message_min_autonomy": PERSONAL_MESSAGE_MIN_AUTONOMY,
            "future_personal_message_requires_confirmation": True,
        },
    })
    return payload


def get_relationship_record(db, *, owner_id: str, entity_id: object) -> dict[str, Any]:
    entity, _, _ = _owned_relationship_record(db, owner_id, entity_id)
    return serialize_relationship_record(db, owner_id=owner_id, entity=entity)


def update_relationship_record(
    db,
    *,
    account: Account,
    entity_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    entity, kind, current = _owned_relationship_record(db, account.id, entity_id)
    allowed = {
        "title", "note", "status", "occurred_at", "due_at", "channel",
        "direction", "priority", "reminder_kind", "provenance", "confidence", "sensitivity",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported relationship record fields: {', '.join(unknown)}")
    merged = dict(current)
    for field in ("channel", "direction", "priority", "reminder_kind"):
        if field in changes:
            merged[field] = changes[field]
    properties = _record_properties(
        db,
        owner_id=account.id,
        record_kind=kind,
        profile_id=current["relationship_profile_id"],
        channel=merged.get("channel"),
        direction=merged.get("direction"),
        priority=merged.get("priority"),
        reminder_kind=merged.get("reminder_kind"),
    )
    entity_changes: dict[str, Any] = {"properties": properties}
    if "title" in changes:
        entity_changes["title"] = _text(
            changes["title"], field="title", limit=240, required=True
        )
    if "note" in changes:
        entity_changes["summary"] = _text(
            changes["note"], field="note", limit=20_000, preserve_lines=True
        )
    if "occurred_at" in changes:
        entity_changes["occurred_at"] = _datetime(
            changes["occurred_at"], field="occurred_at", required=kind == "interaction"
        )
    if "due_at" in changes:
        if kind == "interaction":
            raise LifeGraphError("Interactions do not have due_at")
        entity_changes["due_at"] = _datetime(
            changes["due_at"], field="due_at", required=True
        )
    if "status" in changes:
        status = _token(changes["status"], field="status", limit=32)
        allowed_statuses = {
            "interaction": INTERACTION_STATUSES,
            "commitment": COMMITMENT_STATUSES,
            "follow_up": FOLLOW_UP_STATUSES,
        }[kind]
        if status not in allowed_statuses:
            raise LifeGraphError(
                f"Unsupported {kind.replace('_', ' ')} status"
            )
        entity_changes["status"] = status
    if "provenance" in changes:
        entity_changes["provenance"] = _normalize_provenance(
            db, owner_id=account.id, value=changes["provenance"],
            record_kind=kind, required=True,
        )
    if "confidence" in changes:
        entity_changes["confidence"] = changes["confidence"]
    if "sensitivity" in changes:
        sensitivity = _token(
            changes["sensitivity"], field="sensitivity", limit=24
        )
        if sensitivity not in SENSITIVITIES:
            raise LifeGraphError("Relationship sensitivity must be private or restricted")
        entity_changes["sensitivity"] = sensitivity
    return update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=entity_changes,
        reason=f"Relationship {kind.replace('_', ' ')} updated",
    )


def list_relationship_records(
    db,
    *,
    owner_id: str,
    profile_id: object,
    record_kind: object | None = None,
    status: object | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    profile = _owned_profile(db, owner_id, profile_id)
    normalized_kind = None
    if record_kind is not None:
        normalized_kind = _token(record_kind, field="record_kind")
        if normalized_kind not in {"interaction", "commitment", "follow_up"}:
            raise LifeGraphError("record_kind must be interaction, commitment, or follow_up")
    normalized_status = (
        _token(status, field="status", limit=32) if status is not None else None
    )
    entity_types = (
        [RELATIONSHIP_RECORD_TYPES[normalized_kind]]
        if normalized_kind else ["interaction", "commitment", "reminder"]
    )
    rows = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type.in_(entity_types),
        LifeEntity.deleted_at.is_(None),
    ).order_by(
        LifeEntity.occurred_at.desc(), LifeEntity.id.desc()
    ).limit(RELATIONSHIP_RECORD_SCAN_LIMIT + 1).all()
    scan_truncated = len(rows) > RELATIONSHIP_RECORD_SCAN_LIMIT
    result: list[dict[str, Any]] = []
    for row in rows[:RELATIONSHIP_RECORD_SCAN_LIMIT]:
        properties = row.properties if isinstance(row.properties, Mapping) else {}
        if (
            properties.get("relationship_schema_version") != RELATIONSHIP_SCHEMA_VERSION
            or properties.get("relationship_profile_id") != profile.id
        ):
            continue
        kind = properties.get("relationship_record_kind")
        if normalized_kind and kind != normalized_kind:
            continue
        if normalized_status and row.status != normalized_status:
            continue
        try:
            result.append(serialize_relationship_record(
                db, owner_id=owner_id, entity=row
            ))
        except LifeGraphError:
            continue
    bounded = max(1, min(100, int(limit)))
    return result[:bounded], scan_truncated or len(result) > bounded


def relationship_history(
    db, *, owner_id: str, entity_id: object, limit: int = 50
) -> tuple[list[dict[str, Any]], bool]:
    try:
        _owned_profile(db, owner_id, entity_id, include_deleted=True)
    except LifeGraphNotFound:
        _owned_relationship_record(db, owner_id, entity_id)
    rows, truncated = list_life_entity_versions(
        db, owner_id=owner_id, entity_id=entity_id, limit=limit
    )
    return [serialize_life_entity_version(row) for row in rows], truncated


def relationship_reminders(
    db,
    *,
    owner_id: str,
    due_before: object,
    as_of: object | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return deterministic reminders from explicit, source-backed records only."""

    before = _datetime(due_before, field="due_before", required=True)
    now = _datetime(as_of, field="as_of") or datetime.utcnow()
    bounded = max(1, min(100, int(limit)))
    items: list[dict[str, Any]] = []

    rows = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type.in_(["commitment", "reminder"]),
        LifeEntity.deleted_at.is_(None),
        LifeEntity.due_at.is_not(None),
        LifeEntity.due_at <= before,
    ).order_by(LifeEntity.due_at.asc(), LifeEntity.id.asc()).limit(
        RELATIONSHIP_RECORD_SCAN_LIMIT + 1
    ).all()
    record_scan_truncated = len(rows) > RELATIONSHIP_RECORD_SCAN_LIMIT
    for row in rows[:RELATIONSHIP_RECORD_SCAN_LIMIT]:
        properties = row.properties if isinstance(row.properties, Mapping) else {}
        kind = properties.get("relationship_record_kind")
        active = (
            kind == "commitment" and row.status == "open"
        ) or (kind == "follow_up" and row.status == "open")
        provenance = row.provenance if isinstance(row.provenance, Mapping) else {}
        if (
            properties.get("relationship_schema_version") != RELATIONSHIP_SCHEMA_VERSION
            or not active
            or not provenance.get("source_id")
        ):
            continue
        try:
            _validate_stored_provenance(
                db,
                owner_id=owner_id,
                value=provenance,
                record_kind=str(kind),
                required=True,
            )
        except LifeGraphError:
            continue
        try:
            profile = _owned_profile(
                db, owner_id, properties.get("relationship_profile_id")
            )
        except LifeGraphError:
            continue
        items.append({
            "kind": kind,
            "reminder_kind": (
                properties.get("reminder_kind")
                if kind == "follow_up" else "commitment"
            ),
            "record_id": row.id,
            "profile_id": profile.id,
            "profile_title": profile.title,
            "title": row.title,
            "due_at": _iso_datetime(row.due_at, field="due_at"),
            "overdue": bool(row.due_at and row.due_at < now),
            "source_id": provenance["source_id"],
            "source_backed": True,
            "calculation": "explicit_due_at",
        })

    profiles = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == RELATIONSHIP_PROFILE_ENTITY_TYPE,
        LifeEntity.status == "active",
        LifeEntity.deleted_at.is_(None),
    ).order_by(LifeEntity.id.asc()).limit(RELATIONSHIP_SCAN_LIMIT + 1).all()
    profile_scan_truncated = len(profiles) > RELATIONSHIP_SCAN_LIMIT
    for profile in profiles[:RELATIONSHIP_SCAN_LIMIT]:
        try:
            properties = validate_profile_properties(
                db, owner_id=owner_id, value=profile.properties or {}
            )
        except LifeGraphError:
            continue
        care = properties.get("care_plan")
        if care is None:
            continue
        due_at = _datetime(care["next_due_at"], field="care_plan.next_due_at")
        if due_at is None or due_at > before:
            continue
        items.append({
            "kind": "relationship_care",
            "record_id": profile.id,
            "profile_id": profile.id,
            "profile_title": profile.title,
            "title": "Relationship care check-in",
            "due_at": _iso_datetime(due_at, field="care_plan.next_due_at"),
            "overdue": due_at < now,
            "source_id": care["source_id"],
            "source_backed": True,
            "calculation": "explicit_care_plan_next_due_at",
            "interval_days": care["interval_days"],
        })

    items.sort(key=lambda item: (
        str(item["due_at"]), str(item["kind"]), str(item["record_id"])
    ))
    return {
        "items": items[:bounded],
        "count": min(len(items), bounded),
        "truncated": (
            record_scan_truncated or profile_scan_truncated or len(items) > bounded
        ),
        "as_of": _iso_datetime(now, field="as_of"),
        "due_before": _iso_datetime(before, field="due_before"),
        "inference_policy": {
            "explicit_source_backed_records_only": True,
            "speculative_messaging_inference": False,
        },
        "execution_policy": {
            "record_only": True,
            "can_send_personal_message": False,
            "future_personal_message_min_autonomy": PERSONAL_MESSAGE_MIN_AUTONOMY,
            "future_personal_message_requires_confirmation": True,
        },
    }


__all__ = [
    "PERSONAL_MESSAGE_MIN_AUTONOMY",
    "RELATIONSHIP_SCHEMA_VERSION",
    "create_commitment",
    "create_follow_up",
    "create_interaction",
    "create_relationship_profile",
    "get_relationship_profile",
    "get_relationship_record",
    "is_typed_relationship_payload",
    "link_relationship_context",
    "list_relationship_profiles",
    "list_relationship_records",
    "relationship_history",
    "relationship_reminders",
    "serialize_relationship_profile",
    "serialize_relationship_record",
    "update_relationship_profile",
    "update_relationship_record",
    "validate_profile_properties",
]
