"""Typed V3 Work and Business workspaces on Restia's canonical Life graph.

Both workspace definitions and their records are encrypted, immutable-principal
``LifeEntity`` rows owned by ``Account.id``.  Records cannot move between
workspaces.  The only cross-workspace visibility comes from explicit,
owner-validated ``EntityLink`` rows created by this module.

This is a record and read-model boundary.  It intentionally has no capability
to send outreach, submit proposals, make payments, or store credentials.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any, Mapping

from core.database import Account, EntityLink, LifeEntity, LifeSource
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    create_entity_link,
    create_life_entity,
    delete_entity_link,
    delete_life_entity,
    get_life_entity,
    list_life_entity_versions,
    serialize_entity_link,
    serialize_life_entity,
    serialize_life_entity_version,
    update_life_entity,
)


WORK_BUSINESS_WORKSPACE_SCHEMA_VERSION = 1
WORK_BUSINESS_RECORD_SCHEMA_VERSION = 1
WORK_BUSINESS_RELATION_SCHEMA_VERSION = 1
WORK_BUSINESS_WORKSPACE_ENTITY_TYPE = "workspace"
WORK_BUSINESS_SCAN_LIMIT = 1_000

WORKSPACE_KINDS = frozenset({"work", "business"})
WORK_BUSINESS_RECORD_KINDS = frozenset({
    "objective",
    "project",
    "person",
    "meeting",
    "task",
    "note",
    "file",
    "decision",
    "metric",
    "document",
    "opportunity",
    "customer",
    "outreach",
    "proposal",
    "follow_up",
    "revenue",
    "experiment",
    "process",
    "lesson",
    "roadmap",
})

RECORD_ENTITY_TYPES: dict[str, str] = {
    "objective": "goal",
    "project": "project",
    "person": "person",
    "meeting": "event",
    "task": "task",
    "note": "note",
    "file": "file",
    "decision": "decision",
    "metric": "metric",
    "document": "file",
    "opportunity": "milestone",
    "customer": "person",
    "outreach": "communication_thread",
    "proposal": "file",
    "follow_up": "reminder",
    "revenue": "transaction",
    "experiment": "milestone",
    "process": "note",
    "lesson": "note",
    "roadmap": "milestone",
}

WORKSPACE_STATUSES = frozenset({"active", "archived"})
RECORD_STATUSES = frozenset({
    "active", "planned", "in_progress", "waiting", "blocked", "completed",
    "won", "lost", "paused", "cancelled", "archived",
})
SENSITIVITIES = frozenset({"private", "restricted"})
SOURCE_RELATIONS = frozenset({
    "supports", "evidence", "reference", "meeting_record", "document",
    "metric", "import", "manual",
})
CROSS_WORKSPACE_RELATIONS = frozenset({
    "depends_on", "supports", "informs", "references", "handoff_to",
    "derived_from", "customer_of", "proposal_for", "measured_by",
})

EXTERNAL_ACTION_POLICY = {
    "record_only": True,
    "can_send_outreach": False,
    "can_send_messages": False,
    "can_submit_proposals": False,
    "can_make_payments": False,
}

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_URL_CREDENTIAL_RE = re.compile(
    r"[a-z][a-z0-9+.-]*://[^/@\s]+:[^/@\s]+@", re.IGNORECASE
)
_BEARER_RE = re.compile(r"\bauthorization\s*:\s*bearer\s+\S+", re.IGNORECASE)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_SECRET_KEY_PARTS = (
    "password", "passwd", "passcode", "secret", "token", "credential",
    "cookie", "authorization", "api_key", "private_key", "seed_phrase",
    "recovery_phrase", "bank_login", "card_number", "account_number",
)
_EXECUTOR_KEYS = frozenset({
    "action", "execute", "executor", "tool_call", "external_action",
    "webhook", "send", "send_message", "send_email", "message_payload",
    "payment", "pay", "transfer", "charge", "submit", "submit_proposal",
    "send_proposal", "dispatch", "http_request", "shell", "command",
    "tool", "function_call", "arguments", "payload", "request_body",
})


def _compact_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())


def _is_secret_key(value: object) -> bool:
    compact = _compact_key(value)
    return any(_compact_key(part) in compact for part in _SECRET_KEY_PARTS)


def _is_executor_key(value: object) -> bool:
    key = str(value or "").strip().lower().replace("-", "_")
    compact = _compact_key(key)
    return (
        key in _EXECUTOR_KEYS
        or key.startswith((
            "send_", "execute_", "dispatch_", "submit_", "payment_",
            "transfer_", "charge_",
        ))
        or compact in {_compact_key(item) for item in _EXECUTOR_KEYS}
        or any(
            marker in compact
            for marker in (
                "executor", "toolcall", "externalaction", "webhook",
                "httprequest",
            )
        )
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
    if (
        _URL_CREDENTIAL_RE.search(normalized)
        or _BEARER_RE.search(normalized)
        or _PRIVATE_KEY_RE.search(normalized)
    ):
        raise LifeGraphError(f"{field} must not contain credentials or secrets")
    return normalized


def _token(value: object, *, field: str, limit: int = 64) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    if not normalized or len(normalized) > limit or not _TOKEN_RE.fullmatch(normalized):
        raise LifeGraphError(
            f"{field} must be a lowercase token using letters, numbers, _, -, or ."
        )
    return normalized


def _datetime(value: object | None, *, field: str) -> datetime | None:
    if value is None or value == "":
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
            raise LifeGraphError(f"{field} must be an ISO-8601 datetime") from exc
    else:
        raise LifeGraphError(f"{field} must be an ISO-8601 datetime")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        normalized = value
        if normalized.tzinfo is not None:
            normalized = normalized.astimezone(timezone.utc).replace(tzinfo=None)
        return normalized.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    return value


def _assert_safe_payload(value: object, *, field: str) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if (
                str(raw_key).strip().lower().replace("-", "_")
                == "external_action_policy"
                and child == EXTERNAL_ACTION_POLICY
            ):
                continue
            if _is_secret_key(raw_key):
                raise LifeGraphError(f"{field} must not contain credentials or secrets")
            if _is_executor_key(raw_key):
                raise LifeGraphError(
                    f"{field} cannot request outreach, sends, proposal submission, "
                    "payments, or another external action"
                )
            _assert_safe_payload(child, field=field)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_safe_payload(child, field=field)
    elif isinstance(value, str):
        _text(value, field=field, limit=20_000, preserve_lines=True)


def _bounded_object(
    value: object | None, *, field: str, max_bytes: int = 32_000
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


def _source_links(db, *, owner_id: str, value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise LifeGraphError("source_links must contain at least one source-backed link")
    if len(value) > 20:
        raise LifeGraphError("source_links must not contain more than 20 items")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        raw = _bounded_object(item, field="source link", max_bytes=4_000)
        unknown = sorted(set(raw) - {"source_id", "relation", "label", "locator"})
        if unknown:
            raise LifeGraphError(f"Unsupported source link fields: {', '.join(unknown)}")
        source_id = _text(
            raw.get("source_id"), field="source_links.source_id", limit=36,
            required=True,
        )
        source = db.query(LifeSource).filter(
            LifeSource.id == source_id,
            LifeSource.owner_id == owner_id,
        ).first()
        if source is None:
            raise LifeGraphNotFound("Work/Business source not found")
        relation = _token(
            raw.get("relation") or "supports", field="source link relation"
        )
        if relation not in SOURCE_RELATIONS:
            raise LifeGraphError(
                "source link relation must be one of: "
                + ", ".join(sorted(SOURCE_RELATIONS))
            )
        marker = (source_id, relation)
        if marker in seen:
            raise LifeGraphError("source_links must be unique by source and relation")
        seen.add(marker)
        result.append({
            "source_id": source_id,
            "relation": relation,
            "label": _text(raw.get("label"), field="source link label", limit=240),
            "locator": _text(
                raw.get("locator"), field="source link locator", limit=1_000
            ),
        })
    return sorted(result, key=lambda row: (row["source_id"], row["relation"]))


def _normalize_provenance(
    value: object | None,
    *,
    source_links: list[Mapping[str, str]],
    workspace_id: str | None,
    workspace_kind: str,
    record_kind: str,
    strict_source_ids: bool,
) -> dict[str, Any]:
    provenance = _bounded_object(value, field="provenance", max_bytes=16_000)
    source_ids = sorted({row["source_id"] for row in source_links})
    supplied: set[str] = set()
    if provenance.get("source_id") is not None:
        supplied.add(_text(
            provenance.pop("source_id"), field="provenance.source_id", limit=36,
            required=True,
        ))
    if provenance.get("source_ids") is not None:
        raw_many = provenance.pop("source_ids")
        if not isinstance(raw_many, list):
            raise LifeGraphError("provenance.source_ids must be a list")
        supplied.update(
            _text(item, field="provenance.source_ids", limit=36, required=True)
            for item in raw_many
        )
    if strict_source_ids and supplied and supplied != set(source_ids):
        raise LifeGraphError("provenance sources must exactly match source_links")
    provenance["source_ids"] = source_ids
    provenance["authority"] = "work_business_v1"
    provenance["workspace_kind"] = workspace_kind
    provenance["record_kind"] = record_kind
    if workspace_id is not None:
        provenance["workspace_id"] = workspace_id
    provenance.setdefault("capture", "manual")
    return provenance


def _workspace_kind(value: object) -> str:
    normalized = _token(value, field="workspace_kind", limit=16)
    if normalized not in WORKSPACE_KINDS:
        raise LifeGraphError("workspace_kind must be work or business")
    return normalized


def _record_kind(value: object) -> str:
    normalized = _token(value, field="record_kind", limit=48)
    if normalized not in WORK_BUSINESS_RECORD_KINDS:
        raise LifeGraphError(f"Unsupported Work/Business record kind: {normalized}")
    return normalized


def _status(value: object, *, workspace: bool) -> str:
    normalized = _token(value, field="status", limit=32)
    allowed = WORKSPACE_STATUSES if workspace else RECORD_STATUSES
    if normalized not in allowed:
        label = "workspace" if workspace else "record"
        raise LifeGraphError(f"Unsupported Work/Business {label} status")
    return normalized


def _sensitivity(value: object) -> str:
    normalized = _token(value, field="sensitivity", limit=24)
    if normalized not in SENSITIVITIES:
        raise LifeGraphError("Work/Business sensitivity must be private or restricted")
    return normalized


def validate_workspace_properties(
    db, *, owner_id: str, value: object
) -> dict[str, Any]:
    raw = _bounded_object(value, field="Work/Business workspace", max_bytes=64_000)
    allowed = {
        "work_business_workspace_schema_version", "workspace_kind", "purpose",
        "details", "source_links", "external_action_policy",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported workspace fields: {', '.join(unknown)}")
    if (
        raw.get("work_business_workspace_schema_version")
        != WORK_BUSINESS_WORKSPACE_SCHEMA_VERSION
    ):
        raise LifeGraphError("Unsupported Work/Business workspace schema version")
    if raw.get("external_action_policy") not in (None, EXTERNAL_ACTION_POLICY):
        raise LifeGraphError("Work/Business external action policy is server-controlled")
    return {
        "work_business_workspace_schema_version": (
            WORK_BUSINESS_WORKSPACE_SCHEMA_VERSION
        ),
        "workspace_kind": _workspace_kind(raw.get("workspace_kind")),
        "purpose": _text(
            raw.get("purpose"), field="purpose", limit=5_000,
            preserve_lines=True,
        ),
        "details": _bounded_object(raw.get("details"), field="details"),
        "source_links": _source_links(
            db, owner_id=owner_id, value=raw.get("source_links")
        ),
        "external_action_policy": dict(EXTERNAL_ACTION_POLICY),
    }


def validate_workspace_record_properties(
    db, *, owner_id: str, value: object
) -> dict[str, Any]:
    raw = _bounded_object(value, field="Work/Business record", max_bytes=64_000)
    allowed = {
        "work_business_record_schema_version", "workspace_id", "workspace_kind",
        "record_kind", "details", "source_links", "external_action_policy",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported Work/Business fields: {', '.join(unknown)}")
    if (
        raw.get("work_business_record_schema_version")
        != WORK_BUSINESS_RECORD_SCHEMA_VERSION
    ):
        raise LifeGraphError("Unsupported Work/Business record schema version")
    if raw.get("external_action_policy") not in (None, EXTERNAL_ACTION_POLICY):
        raise LifeGraphError("Work/Business external action policy is server-controlled")
    return {
        "work_business_record_schema_version": WORK_BUSINESS_RECORD_SCHEMA_VERSION,
        "workspace_id": _text(
            raw.get("workspace_id"), field="workspace_id", limit=36, required=True
        ),
        "workspace_kind": _workspace_kind(raw.get("workspace_kind")),
        "record_kind": _record_kind(raw.get("record_kind")),
        "details": _bounded_object(raw.get("details"), field="details"),
        "source_links": _source_links(
            db, owner_id=owner_id, value=raw.get("source_links")
        ),
        "external_action_policy": dict(EXTERNAL_ACTION_POLICY),
    }


def is_typed_work_business_payload(
    entity_type: object, properties: object | None = None
) -> bool:
    if not isinstance(properties, Mapping):
        return False
    return (
        properties.get("work_business_workspace_schema_version") is not None
        or properties.get("work_business_record_schema_version") is not None
    )


def generic_link_requires_work_business_route(
    db,
    *,
    owner_id: str,
    source_id: object,
    target_id: object,
    metadata: object | None,
) -> bool:
    """Return true when a generic link would bypass typed workspace authority."""
    if isinstance(metadata, Mapping) and (
        metadata.get("work_business_relation_schema_version") is not None
    ):
        return True
    endpoints = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.id.in_([str(source_id), str(target_id)]),
        LifeEntity.deleted_at.is_(None),
    ).all()
    identities = {
        entity.id: _record_identity(entity)
        for entity in endpoints
    }
    source = identities.get(str(source_id))
    target = identities.get(str(target_id))
    return bool(source and target and source[0] != target[0])


def is_work_business_relation_id(
    db, *, owner_id: str, relation_id: object
) -> bool:
    link = db.query(EntityLink).filter(
        EntityLink.id == str(relation_id),
        EntityLink.owner_id == owner_id,
    ).first()
    return bool(
        link is not None
        and isinstance(link.meta_data, Mapping)
        and link.meta_data.get("work_business_relation_schema_version") is not None
    )


def _workspace_identity(entity: LifeEntity) -> str | None:
    properties = entity.properties if isinstance(entity.properties, Mapping) else {}
    if (
        entity.entity_type != WORK_BUSINESS_WORKSPACE_ENTITY_TYPE
        or properties.get("work_business_workspace_schema_version")
        != WORK_BUSINESS_WORKSPACE_SCHEMA_VERSION
    ):
        return None
    try:
        return _workspace_kind(properties.get("workspace_kind"))
    except LifeGraphError:
        return None


def _record_identity(entity: LifeEntity) -> tuple[str, str, str] | None:
    properties = entity.properties if isinstance(entity.properties, Mapping) else {}
    if (
        properties.get("work_business_record_schema_version")
        != WORK_BUSINESS_RECORD_SCHEMA_VERSION
    ):
        return None
    try:
        workspace_id = _text(
            properties.get("workspace_id"), field="workspace_id", limit=36,
            required=True,
        )
        workspace_kind = _workspace_kind(properties.get("workspace_kind"))
        record_kind = _record_kind(properties.get("record_kind"))
    except LifeGraphError:
        return None
    if entity.entity_type != RECORD_ENTITY_TYPES[record_kind]:
        return None
    return workspace_id, workspace_kind, record_kind


def _validate_workspace_authority(db, *, owner_id: str, entity: LifeEntity) -> str:
    workspace_kind = _workspace_identity(entity)
    if workspace_kind is None:
        raise LifeGraphNotFound("Work/Business workspace not found")
    properties = validate_workspace_properties(
        db, owner_id=owner_id, value=entity.properties or {}
    )
    source_ids = {row["source_id"] for row in properties["source_links"]}
    provenance = dict(entity.provenance or {})
    if set(provenance.get("source_ids") or []) != source_ids:
        raise LifeGraphError("Workspace provenance does not match source links")
    if (
        provenance.get("authority") != "work_business_v1"
        or provenance.get("workspace_kind") != workspace_kind
        or provenance.get("record_kind") != "workspace"
    ):
        raise LifeGraphError("Workspace provenance is malformed")
    return workspace_kind


def _owned_workspace(
    db, owner_id: str, workspace_id: object, *, include_deleted: bool = False
) -> LifeEntity:
    try:
        entity = get_life_entity(
            db,
            owner_id=owner_id,
            entity_id=workspace_id,
            include_deleted=include_deleted,
        )
    except LifeGraphNotFound as exc:
        raise LifeGraphNotFound("Work/Business workspace not found") from exc
    _validate_workspace_authority(db, owner_id=owner_id, entity=entity)
    return entity


def _validate_record_authority(
    db,
    *,
    owner_id: str,
    entity: LifeEntity,
    workspace_id: str | None = None,
    include_deleted_workspace: bool = False,
) -> tuple[str, str, str]:
    identity = _record_identity(entity)
    if identity is None:
        raise LifeGraphNotFound("Work/Business record not found")
    stored_workspace_id, workspace_kind, record_kind = identity
    if workspace_id is not None and stored_workspace_id != workspace_id:
        raise LifeGraphNotFound("Work/Business record not found")
    workspace = _owned_workspace(
        db,
        owner_id,
        stored_workspace_id,
        include_deleted=include_deleted_workspace,
    )
    if _workspace_identity(workspace) != workspace_kind:
        raise LifeGraphError("Record workspace authority is inconsistent")
    properties = validate_workspace_record_properties(
        db, owner_id=owner_id, value=entity.properties or {}
    )
    source_ids = {row["source_id"] for row in properties["source_links"]}
    provenance = dict(entity.provenance or {})
    if set(provenance.get("source_ids") or []) != source_ids:
        raise LifeGraphError("Record provenance does not match source links")
    if (
        provenance.get("authority") != "work_business_v1"
        or provenance.get("workspace_id") != stored_workspace_id
        or provenance.get("workspace_kind") != workspace_kind
        or provenance.get("record_kind") != record_kind
    ):
        raise LifeGraphError("Record provenance is malformed")
    return identity


def _owned_record(
    db,
    owner_id: str,
    workspace_id: object,
    entity_id: object,
    *,
    include_deleted: bool = False,
) -> LifeEntity:
    normalized_workspace_id = str(workspace_id)
    try:
        entity = get_life_entity(
            db,
            owner_id=owner_id,
            entity_id=entity_id,
            include_deleted=include_deleted,
        )
    except LifeGraphNotFound as exc:
        raise LifeGraphNotFound("Work/Business record not found") from exc
    _validate_record_authority(
        db,
        owner_id=owner_id,
        entity=entity,
        workspace_id=normalized_workspace_id,
        include_deleted_workspace=include_deleted,
    )
    return entity


def create_work_business_workspace(
    db,
    *,
    account: Account,
    workspace_kind: object,
    title: object,
    purpose: object = "",
    details: object | None = None,
    source_links: object,
    status: object = "active",
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    review_at: object | None = None,
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    normalized_kind = _workspace_kind(workspace_kind)
    properties = validate_workspace_properties(
        db,
        owner_id=account.id,
        value={
            "work_business_workspace_schema_version": (
                WORK_BUSINESS_WORKSPACE_SCHEMA_VERSION
            ),
            "workspace_kind": normalized_kind,
            "purpose": purpose,
            "details": details or {},
            "source_links": source_links,
        },
    )
    normalized_provenance = _normalize_provenance(
        provenance,
        source_links=properties["source_links"],
        workspace_id=None,
        workspace_kind=normalized_kind,
        record_kind="workspace",
        strict_source_ids=True,
    )
    key = (
        f"work-business-workspace:{normalized_kind}:{idempotency_key}"
        if idempotency_key
        else None
    )
    return create_life_entity(
        db,
        account=account,
        entity_type=WORK_BUSINESS_WORKSPACE_ENTITY_TYPE,
        title=_text(title, field="title", limit=240, required=True),
        summary=properties["purpose"],
        status=_status(status, workspace=True),
        properties=properties,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=_sensitivity(sensitivity),
        review_at=_datetime(review_at, field="review_at"),
        idempotency_key=key,
        reason=f"{normalized_kind.title()} workspace created",
    )


def update_work_business_workspace(
    db,
    *,
    account: Account,
    workspace_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    entity = _owned_workspace(db, account.id, workspace_id)
    allowed = {
        "title", "purpose", "details", "source_links", "status", "provenance",
        "confidence", "sensitivity", "review_at",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported workspace fields: {', '.join(unknown)}")
    current = dict(entity.properties or {})
    merged = dict(current)
    for field in ("purpose", "details", "source_links"):
        if field in changes:
            merged[field] = changes[field]
    properties = validate_workspace_properties(
        db, owner_id=account.id, value=merged
    )
    entity_changes: dict[str, Any] = {"properties": properties}
    if "title" in changes:
        entity_changes["title"] = _text(
            changes["title"], field="title", limit=240, required=True
        )
    if "purpose" in changes:
        entity_changes["summary"] = properties["purpose"]
    if "status" in changes:
        entity_changes["status"] = _status(changes["status"], workspace=True)
    provenance_value: object = changes.get("provenance", dict(entity.provenance or {}))
    if "source_links" in changes and "provenance" not in changes:
        provenance_value = dict(entity.provenance or {})
        provenance_value.pop("source_id", None)
        provenance_value.pop("source_ids", None)
    entity_changes["provenance"] = _normalize_provenance(
        provenance_value,
        source_links=properties["source_links"],
        workspace_id=None,
        workspace_kind=properties["workspace_kind"],
        record_kind="workspace",
        strict_source_ids="provenance" in changes,
    )
    if "confidence" in changes:
        entity_changes["confidence"] = changes["confidence"]
    if "sensitivity" in changes:
        entity_changes["sensitivity"] = _sensitivity(changes["sensitivity"])
    if "review_at" in changes:
        entity_changes["review_at"] = _datetime(
            changes["review_at"], field="review_at"
        )
    return update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=entity_changes,
        reason="Work/Business workspace updated",
    )


def get_work_business_workspace(
    db, *, owner_id: str, workspace_id: object, include_deleted: bool = False
) -> dict[str, Any]:
    entity = _owned_workspace(
        db, owner_id, workspace_id, include_deleted=include_deleted
    )
    return serialize_work_business_workspace(entity)


def list_work_business_workspaces(
    db,
    *,
    owner_id: str,
    workspace_kind: object | None = None,
    status: object | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    bounded = max(1, min(100, int(limit)))
    normalized_kind = _workspace_kind(workspace_kind) if workspace_kind else None
    normalized_status = _status(status, workspace=True) if status else None
    candidates = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == WORK_BUSINESS_WORKSPACE_ENTITY_TYPE,
        LifeEntity.deleted_at.is_(None),
    ).order_by(
        LifeEntity.updated_at.desc(), LifeEntity.id.desc()
    ).limit(WORK_BUSINESS_SCAN_LIMIT + 1).all()
    scan_truncated = len(candidates) > WORK_BUSINESS_SCAN_LIMIT
    rows: list[dict[str, Any]] = []
    for entity in candidates[:WORK_BUSINESS_SCAN_LIMIT]:
        identity = _workspace_identity(entity)
        if identity is None:
            continue
        _validate_workspace_authority(db, owner_id=owner_id, entity=entity)
        if normalized_kind and identity != normalized_kind:
            continue
        if normalized_status and entity.status != normalized_status:
            continue
        rows.append(serialize_work_business_workspace(entity))
    return rows[:bounded], scan_truncated or len(rows) > bounded


def workspace_history(
    db, *, owner_id: str, workspace_id: object, limit: int = 50
) -> tuple[list[dict[str, Any]], bool]:
    entity = _owned_workspace(db, owner_id, workspace_id, include_deleted=True)
    rows, truncated = list_life_entity_versions(
        db, owner_id=owner_id, entity_id=entity.id, limit=limit
    )
    return [serialize_life_entity_version(row) for row in rows], truncated


def delete_work_business_workspace(
    db,
    *,
    owner_id: str,
    workspace_id: object,
    expected_version: int,
    reason: object = "Work/Business workspace deleted",
) -> LifeEntity:
    entity = _owned_workspace(db, owner_id, workspace_id)
    active_records, _ = _workspace_record_candidates(
        db, owner_id=owner_id, workspace_id=entity.id
    )
    if active_records:
        raise LifeGraphConflict("Delete workspace records before deleting the workspace")
    return delete_life_entity(
        db,
        owner_id=owner_id,
        entity_id=entity.id,
        expected_version=expected_version,
        reason=reason,
    )


def create_work_business_record(
    db,
    *,
    account: Account,
    workspace_id: object,
    record_kind: object,
    title: object,
    summary: object = "",
    details: object | None = None,
    source_links: object,
    status: object = "active",
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    occurred_at: object | None = None,
    due_at: object | None = None,
    review_at: object | None = None,
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    workspace = _owned_workspace(db, account.id, workspace_id)
    normalized_workspace_kind = _workspace_identity(workspace)
    assert normalized_workspace_kind is not None
    normalized_record_kind = _record_kind(record_kind)
    properties = validate_workspace_record_properties(
        db,
        owner_id=account.id,
        value={
            "work_business_record_schema_version": WORK_BUSINESS_RECORD_SCHEMA_VERSION,
            "workspace_id": workspace.id,
            "workspace_kind": normalized_workspace_kind,
            "record_kind": normalized_record_kind,
            "details": details or {},
            "source_links": source_links,
        },
    )
    normalized_provenance = _normalize_provenance(
        provenance,
        source_links=properties["source_links"],
        workspace_id=workspace.id,
        workspace_kind=normalized_workspace_kind,
        record_kind=normalized_record_kind,
        strict_source_ids=True,
    )
    key = (
        f"work-business-record:{workspace.id}:{idempotency_key}"
        if idempotency_key
        else None
    )
    return create_life_entity(
        db,
        account=account,
        entity_type=RECORD_ENTITY_TYPES[normalized_record_kind],
        title=_text(title, field="title", limit=240, required=True),
        summary=_text(
            summary, field="summary", limit=20_000, preserve_lines=True
        ),
        status=_status(status, workspace=False),
        properties=properties,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=_sensitivity(sensitivity),
        occurred_at=_datetime(occurred_at, field="occurred_at"),
        due_at=_datetime(due_at, field="due_at"),
        review_at=_datetime(review_at, field="review_at"),
        idempotency_key=key,
        reason=f"{normalized_workspace_kind.title()} {normalized_record_kind} recorded",
    )


def update_work_business_record(
    db,
    *,
    account: Account,
    workspace_id: object,
    record_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    entity = _owned_record(db, account.id, workspace_id, record_id)
    allowed = {
        "title", "summary", "details", "source_links", "status", "provenance",
        "confidence", "sensitivity", "occurred_at", "due_at", "review_at",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported Work/Business fields: {', '.join(unknown)}")
    current = dict(entity.properties or {})
    merged = dict(current)
    for field in ("details", "source_links"):
        if field in changes:
            merged[field] = changes[field]
    properties = validate_workspace_record_properties(
        db, owner_id=account.id, value=merged
    )
    entity_changes: dict[str, Any] = {"properties": properties}
    if "title" in changes:
        entity_changes["title"] = _text(
            changes["title"], field="title", limit=240, required=True
        )
    if "summary" in changes:
        entity_changes["summary"] = _text(
            changes["summary"], field="summary", limit=20_000,
            preserve_lines=True,
        )
    if "status" in changes:
        entity_changes["status"] = _status(changes["status"], workspace=False)
    provenance_value: object = changes.get("provenance", dict(entity.provenance or {}))
    if "source_links" in changes and "provenance" not in changes:
        provenance_value = dict(entity.provenance or {})
        provenance_value.pop("source_id", None)
        provenance_value.pop("source_ids", None)
    entity_changes["provenance"] = _normalize_provenance(
        provenance_value,
        source_links=properties["source_links"],
        workspace_id=properties["workspace_id"],
        workspace_kind=properties["workspace_kind"],
        record_kind=properties["record_kind"],
        strict_source_ids="provenance" in changes,
    )
    if "confidence" in changes:
        entity_changes["confidence"] = changes["confidence"]
    if "sensitivity" in changes:
        entity_changes["sensitivity"] = _sensitivity(changes["sensitivity"])
    for field in ("occurred_at", "due_at", "review_at"):
        if field in changes:
            entity_changes[field] = _datetime(changes[field], field=field)
    return update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=entity_changes,
        reason="Work/Business record updated",
    )


def get_work_business_record(
    db,
    *,
    owner_id: str,
    workspace_id: object,
    record_id: object,
    include_deleted: bool = False,
) -> dict[str, Any]:
    entity = _owned_record(
        db,
        owner_id,
        workspace_id,
        record_id,
        include_deleted=include_deleted,
    )
    return serialize_work_business_record(entity)


def _workspace_record_candidates(
    db, *, owner_id: str, workspace_id: str
) -> tuple[list[LifeEntity], bool]:
    candidates = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.deleted_at.is_(None),
        LifeEntity.entity_type.in_(sorted(set(RECORD_ENTITY_TYPES.values()))),
    ).order_by(
        LifeEntity.updated_at.desc(), LifeEntity.id.desc()
    ).limit(WORK_BUSINESS_SCAN_LIMIT + 1).all()
    truncated = len(candidates) > WORK_BUSINESS_SCAN_LIMIT
    rows: list[LifeEntity] = []
    for entity in candidates[:WORK_BUSINESS_SCAN_LIMIT]:
        identity = _record_identity(entity)
        if identity is None or identity[0] != workspace_id:
            continue
        _validate_record_authority(
            db, owner_id=owner_id, entity=entity, workspace_id=workspace_id
        )
        rows.append(entity)
    return rows, truncated


def list_work_business_records(
    db,
    *,
    owner_id: str,
    workspace_id: object,
    record_kind: object | None = None,
    status: object | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    workspace = _owned_workspace(db, owner_id, workspace_id)
    bounded = max(1, min(100, int(limit)))
    normalized_kind = _record_kind(record_kind) if record_kind else None
    normalized_status = _status(status, workspace=False) if status else None
    candidates, scan_truncated = _workspace_record_candidates(
        db, owner_id=owner_id, workspace_id=workspace.id
    )
    rows = [
        serialize_work_business_record(entity)
        for entity in candidates
        if (not normalized_kind or _record_identity(entity)[2] == normalized_kind)
        and (not normalized_status or entity.status == normalized_status)
    ]
    return rows[:bounded], scan_truncated or len(rows) > bounded


def search_work_business_records(
    db,
    *,
    owner_id: str,
    workspace_id: object,
    query_text: object,
    record_kind: object | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    workspace = _owned_workspace(db, owner_id, workspace_id)
    needle = str(query_text or "").strip().casefold()
    if not needle:
        raise LifeGraphError("q is required")
    bounded = max(1, min(100, int(limit)))
    normalized_kind = _record_kind(record_kind) if record_kind else None
    candidates, scan_truncated = _workspace_record_candidates(
        db, owner_id=owner_id, workspace_id=workspace.id
    )
    matches: list[tuple[tuple[int, str, str], dict[str, Any]]] = []
    for entity in candidates:
        identity = _record_identity(entity)
        assert identity is not None
        if normalized_kind and identity[2] != normalized_kind:
            continue
        title = str(entity.title or "").casefold()
        summary = str(entity.summary or "").casefold()
        details = json.dumps(
            (entity.properties or {}).get("details") or {},
            ensure_ascii=False,
            sort_keys=True,
        ).casefold()
        if title == needle:
            rank, field = 0, "title"
        elif title.startswith(needle):
            rank, field = 1, "title"
        elif needle in title:
            rank, field = 2, "title"
        elif needle in summary:
            rank, field = 3, "summary"
        elif needle in details:
            rank, field = 4, "details"
        else:
            continue
        matches.append((
            (rank, str(entity.title or "").casefold(), entity.id),
            {
                "record": serialize_work_business_record(entity),
                "match": field,
                "rank": rank,
            },
        ))
    matches.sort(key=lambda row: row[0])
    return {
        "items": [row for _, row in matches[:bounded]],
        "count": min(len(matches), bounded),
        "scanned": len(candidates),
        "truncated": scan_truncated or len(matches) > bounded,
    }


def record_history(
    db,
    *,
    owner_id: str,
    workspace_id: object,
    record_id: object,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    entity = _owned_record(
        db, owner_id, workspace_id, record_id, include_deleted=True
    )
    rows, truncated = list_life_entity_versions(
        db, owner_id=owner_id, entity_id=entity.id, limit=limit
    )
    return [serialize_life_entity_version(row) for row in rows], truncated


def delete_work_business_record(
    db,
    *,
    owner_id: str,
    workspace_id: object,
    record_id: object,
    expected_version: int,
    reason: object = "Work/Business record deleted",
) -> LifeEntity:
    entity = _owned_record(db, owner_id, workspace_id, record_id)
    active_relations = db.query(EntityLink.id).filter(
        EntityLink.owner_id == owner_id,
        EntityLink.source_type == "life_entity",
        EntityLink.target_type == "life_entity",
        EntityLink.deleted_at.is_(None),
        ((EntityLink.source_id == entity.id) | (EntityLink.target_id == entity.id)),
    ).limit(1).first()
    if active_relations is not None:
        raise LifeGraphConflict(
            "Delete explicit cross-workspace relations before deleting this record"
        )
    return delete_life_entity(
        db,
        owner_id=owner_id,
        entity_id=entity.id,
        expected_version=expected_version,
        reason=reason,
    )


def _validate_relation_metadata(
    db, *, owner_id: str, value: object
) -> dict[str, Any]:
    raw = _bounded_object(value, field="cross-workspace relation", max_bytes=32_000)
    allowed = {
        "work_business_relation_schema_version", "source_workspace_id",
        "target_workspace_id", "context", "source_links", "external_action_policy",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported relation fields: {', '.join(unknown)}")
    if (
        raw.get("work_business_relation_schema_version")
        != WORK_BUSINESS_RELATION_SCHEMA_VERSION
    ):
        raise LifeGraphError("Unsupported Work/Business relation schema version")
    if raw.get("external_action_policy") not in (None, EXTERNAL_ACTION_POLICY):
        raise LifeGraphError("Work/Business external action policy is server-controlled")
    source_workspace_id = _text(
        raw.get("source_workspace_id"), field="source_workspace_id", limit=36,
        required=True,
    )
    target_workspace_id = _text(
        raw.get("target_workspace_id"), field="target_workspace_id", limit=36,
        required=True,
    )
    if source_workspace_id == target_workspace_id:
        raise LifeGraphError("Cross-workspace relations require two different workspaces")
    return {
        "work_business_relation_schema_version": WORK_BUSINESS_RELATION_SCHEMA_VERSION,
        "source_workspace_id": source_workspace_id,
        "target_workspace_id": target_workspace_id,
        "context": _bounded_object(raw.get("context"), field="relation context"),
        "source_links": _source_links(
            db, owner_id=owner_id, value=raw.get("source_links")
        ),
        "external_action_policy": dict(EXTERNAL_ACTION_POLICY),
    }


def _normalize_relation_provenance(
    value: object | None,
    *,
    metadata: Mapping[str, Any],
    strict_source_ids: bool,
) -> dict[str, Any]:
    provenance = _bounded_object(value, field="relation provenance", max_bytes=16_000)
    source_ids = sorted({row["source_id"] for row in metadata["source_links"]})
    supplied: set[str] = set()
    if provenance.get("source_id") is not None:
        supplied.add(str(provenance.pop("source_id")))
    if provenance.get("source_ids") is not None:
        raw_many = provenance.pop("source_ids")
        if not isinstance(raw_many, list):
            raise LifeGraphError("relation provenance.source_ids must be a list")
        supplied.update(str(item) for item in raw_many)
    if strict_source_ids and supplied and supplied != set(source_ids):
        raise LifeGraphError("relation provenance must exactly match source_links")
    provenance.update({
        "source_ids": source_ids,
        "authority": "work_business_relation_v1",
        "source_workspace_id": metadata["source_workspace_id"],
        "target_workspace_id": metadata["target_workspace_id"],
    })
    provenance.setdefault("capture", "manual")
    return provenance


def create_cross_workspace_relation(
    db,
    *,
    account: Account,
    source_workspace_id: object,
    source_record_id: object,
    relation: object,
    target_workspace_id: object,
    target_record_id: object,
    context: object | None,
    source_links: object,
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
) -> tuple[EntityLink, bool]:
    source_workspace = _owned_workspace(db, account.id, source_workspace_id)
    target_workspace = _owned_workspace(db, account.id, target_workspace_id)
    if source_workspace.id == target_workspace.id:
        raise LifeGraphError("Cross-workspace relations require two different workspaces")
    source_record = _owned_record(
        db, account.id, source_workspace.id, source_record_id
    )
    target_record = _owned_record(
        db, account.id, target_workspace.id, target_record_id
    )
    normalized_relation = _token(relation, field="relation", limit=64)
    if normalized_relation not in CROSS_WORKSPACE_RELATIONS:
        raise LifeGraphError(
            "relation must be one of: "
            + ", ".join(sorted(CROSS_WORKSPACE_RELATIONS))
        )
    metadata = _validate_relation_metadata(
        db,
        owner_id=account.id,
        value={
            "work_business_relation_schema_version": (
                WORK_BUSINESS_RELATION_SCHEMA_VERSION
            ),
            "source_workspace_id": source_workspace.id,
            "target_workspace_id": target_workspace.id,
            "context": context or {},
            "source_links": source_links,
        },
    )
    normalized_provenance = _normalize_relation_provenance(
        provenance, metadata=metadata, strict_source_ids=True
    )
    return create_entity_link(
        db,
        account=account,
        source_id=source_record.id,
        relation=normalized_relation,
        target_id=target_record.id,
        metadata=metadata,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=_sensitivity(sensitivity),
        reason="Explicit cross-workspace relation created",
    )


def _validate_relation_authority(
    db, *, owner_id: str, link: EntityLink
) -> tuple[LifeEntity, LifeEntity, dict[str, Any]]:
    if (
        link.owner_id != owner_id
        or link.source_type != "life_entity"
        or link.target_type != "life_entity"
    ):
        raise LifeGraphNotFound("Cross-workspace relation not found")
    if link.relation not in CROSS_WORKSPACE_RELATIONS:
        raise LifeGraphError("Cross-workspace relation type is invalid")
    metadata = _validate_relation_metadata(
        db, owner_id=owner_id, value=link.meta_data or {}
    )
    source_record = _owned_record(
        db,
        owner_id,
        metadata["source_workspace_id"],
        link.source_id,
        include_deleted=link.deleted_at is not None,
    )
    target_record = _owned_record(
        db,
        owner_id,
        metadata["target_workspace_id"],
        link.target_id,
        include_deleted=link.deleted_at is not None,
    )
    provenance = dict(link.provenance or {})
    source_ids = {row["source_id"] for row in metadata["source_links"]}
    if (
        provenance.get("authority") != "work_business_relation_v1"
        or provenance.get("source_workspace_id") != metadata["source_workspace_id"]
        or provenance.get("target_workspace_id") != metadata["target_workspace_id"]
        or set(provenance.get("source_ids") or []) != source_ids
    ):
        raise LifeGraphError("Cross-workspace relation provenance is malformed")
    return source_record, target_record, metadata


def _owned_relation(
    db, owner_id: str, relation_id: object, *, include_deleted: bool = False
) -> tuple[EntityLink, LifeEntity, LifeEntity, dict[str, Any]]:
    query = db.query(EntityLink).filter(
        EntityLink.id == str(relation_id),
        EntityLink.owner_id == owner_id,
        EntityLink.source_type == "life_entity",
        EntityLink.target_type == "life_entity",
    )
    if not include_deleted:
        query = query.filter(EntityLink.deleted_at.is_(None))
    link = query.first()
    if link is None or not isinstance(link.meta_data, Mapping) or (
        link.meta_data.get("work_business_relation_schema_version")
        != WORK_BUSINESS_RELATION_SCHEMA_VERSION
    ):
        raise LifeGraphNotFound("Cross-workspace relation not found")
    source, target, metadata = _validate_relation_authority(
        db, owner_id=owner_id, link=link
    )
    return link, source, target, metadata


def serialize_cross_workspace_relation(
    link: EntityLink,
    *,
    source_record: LifeEntity,
    target_record: LifeEntity,
) -> dict[str, Any]:
    serialized = serialize_entity_link(link)
    serialized["source_record"] = serialize_work_business_record(source_record)
    serialized["target_record"] = serialize_work_business_record(target_record)
    serialized["execution_policy"] = dict(EXTERNAL_ACTION_POLICY)
    return serialized


def list_cross_workspace_relations(
    db,
    *,
    owner_id: str,
    workspace_id: object,
    direction: str = "both",
    relation: object | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    workspace = _owned_workspace(db, owner_id, workspace_id)
    normalized_direction = str(direction or "both").strip().lower()
    if normalized_direction not in {"both", "incoming", "outgoing"}:
        raise LifeGraphError("direction must be both, incoming, or outgoing")
    normalized_relation = (
        _token(relation, field="relation", limit=64) if relation else None
    )
    if normalized_relation and normalized_relation not in CROSS_WORKSPACE_RELATIONS:
        raise LifeGraphError("Unsupported cross-workspace relation")
    bounded = max(1, min(100, int(limit)))
    candidates = db.query(EntityLink).filter(
        EntityLink.owner_id == owner_id,
        EntityLink.source_type == "life_entity",
        EntityLink.target_type == "life_entity",
        EntityLink.deleted_at.is_(None),
    ).order_by(
        EntityLink.created_at.asc(), EntityLink.id.asc()
    ).limit(WORK_BUSINESS_SCAN_LIMIT + 1).all()
    scan_truncated = len(candidates) > WORK_BUSINESS_SCAN_LIMIT
    rows: list[dict[str, Any]] = []
    for link in candidates[:WORK_BUSINESS_SCAN_LIMIT]:
        metadata = link.meta_data if isinstance(link.meta_data, Mapping) else {}
        if (
            metadata.get("work_business_relation_schema_version")
            != WORK_BUSINESS_RELATION_SCHEMA_VERSION
        ):
            continue
        source, target, normalized_metadata = _validate_relation_authority(
            db, owner_id=owner_id, link=link
        )
        if normalized_relation and link.relation != normalized_relation:
            continue
        outgoing = normalized_metadata["source_workspace_id"] == workspace.id
        incoming = normalized_metadata["target_workspace_id"] == workspace.id
        if normalized_direction == "outgoing" and not outgoing:
            continue
        if normalized_direction == "incoming" and not incoming:
            continue
        if normalized_direction == "both" and not (outgoing or incoming):
            continue
        rows.append(serialize_cross_workspace_relation(
            link, source_record=source, target_record=target
        ))
    return rows[:bounded], scan_truncated or len(rows) > bounded


def delete_cross_workspace_relation(
    db,
    *,
    owner_id: str,
    relation_id: object,
    expected_version: int,
    reason: object = "Cross-workspace relation deleted",
) -> EntityLink:
    link, _, _, _ = _owned_relation(db, owner_id, relation_id)
    return delete_entity_link(
        db,
        owner_id=owner_id,
        link_id=link.id,
        expected_version=expected_version,
        reason=reason,
    )


def workspace_summary(
    db, *, owner_id: str, workspace_id: object
) -> dict[str, Any]:
    workspace = _owned_workspace(db, owner_id, workspace_id)
    records, record_scan_truncated = _workspace_record_candidates(
        db, owner_id=owner_id, workspace_id=workspace.id
    )
    serialized = [serialize_work_business_record(row) for row in records]
    by_kind = Counter(row["record_kind"] for row in serialized)
    by_status = Counter(row["status"] for row in serialized)
    due = sorted(
        (row for row in serialized if row["due_at"] is not None),
        key=lambda row: (row["due_at"], row["id"]),
    )[:10]
    recent = serialized[:10]
    relations, relations_truncated = list_cross_workspace_relations(
        db, owner_id=owner_id, workspace_id=workspace.id, limit=100
    )
    return {
        "workspace": serialize_work_business_workspace(workspace),
        "totals": {
            "records": len(serialized),
            "active": sum(1 for row in serialized if row["status"] == "active"),
            "completed": sum(
                1 for row in serialized if row["status"] == "completed"
            ),
            "cross_workspace_relations": len(relations),
        },
        "by_kind": {
            kind: int(by_kind.get(kind, 0))
            for kind in sorted(WORK_BUSINESS_RECORD_KINDS)
        },
        "by_status": {
            status: int(by_status[status]) for status in sorted(by_status)
        },
        "due_records": due,
        "recent_records": recent,
        "method": "deterministic_owner_scoped_workspace_summary_v1",
        "uses_model_inference": False,
        "truncated": record_scan_truncated or relations_truncated,
        "execution_policy": dict(EXTERNAL_ACTION_POLICY),
    }


def serialize_work_business_workspace(entity: LifeEntity) -> dict[str, Any]:
    base = serialize_life_entity(entity)
    properties = dict(entity.properties or {})
    base.update({
        "workspace_kind": properties.get("workspace_kind"),
        "purpose": properties.get("purpose") or "",
        "details": properties.get("details") or {},
        "source_links": properties.get("source_links") or [],
        "execution_policy": dict(EXTERNAL_ACTION_POLICY),
    })
    return base


def serialize_work_business_record(entity: LifeEntity) -> dict[str, Any]:
    base = serialize_life_entity(entity)
    properties = dict(entity.properties or {})
    base.update({
        "workspace_id": properties.get("workspace_id"),
        "workspace_kind": properties.get("workspace_kind"),
        "record_kind": properties.get("record_kind"),
        "details": properties.get("details") or {},
        "source_links": properties.get("source_links") or [],
        "execution_policy": dict(EXTERNAL_ACTION_POLICY),
    })
    return base
