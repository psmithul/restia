"""Typed V3 human-task authority on the canonical Life graph.

ScheduledTask remains the automation scheduler.  This module is deliberately
for work a person intends to complete, stored as encrypted, Account.id-owned
``LifeEntity(entity_type="task")`` rows.  It adds a bounded schema without
creating another task table or allowing model output to execute an action.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any, Mapping

from core.database import Account, LifeEntity, LifeSource, utcnow_naive
from src.life_graph import (
    LifeGraphError,
    LifeGraphNotFound,
    create_life_entity,
    delete_life_entity,
    get_life_entity,
    list_life_entity_versions,
    serialize_life_entity,
    serialize_life_entity_version,
    update_life_entity,
)


TASK_SCHEMA_VERSION = 1
TASK_SCAN_LIMIT = 500
TASK_STATUSES = frozenset({
    "backlog", "active", "in_progress", "waiting", "blocked", "someday",
    "completed", "cancelled",
})
TASK_PRIORITIES = frozenset({"low", "normal", "high", "critical"})
TASK_ENERGY_LEVELS = frozenset({"low", "medium", "high", "any"})
SOURCE_KINDS = frozenset({
    "manual", "inbox", "email", "telegram", "meeting", "upload",
    "import", "integration",
})
SENSITIVITIES = frozenset({"private", "restricted"})
_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_SECRET_PARTS = (
    "password", "secret", "token", "credential", "cookie", "authorization",
    "api_key", "private_key",
)
_EXECUTOR_KEYS = frozenset({
    "execute", "executor", "tool_call", "external_action", "webhook",
    "send", "send_email", "send_message", "payment", "purchase", "booking",
    "automation",
})
_REFERENCE_TYPES = {
    "project_id": frozenset({"project"}),
    "people_ids": frozenset({"person"}),
    "dependency_ids": frozenset({"task", "milestone"}),
    "document_ids": frozenset({"file", "note", "source", "document"}),
}
_EVIDENCE_TYPES = frozenset({
    "file", "note", "source", "document", "message", "metric",
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


def _integer(value: object, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise LifeGraphError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise LifeGraphError(f"{field} must be an integer") from exc
    if parsed < minimum or parsed > maximum:
        raise LifeGraphError(f"{field} must be from {minimum} to {maximum}")
    return parsed


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
            raise LifeGraphError(f"{field} must be an ISO-8601 date or datetime") from exc
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


def _safe_json(value: object, *, field: str, max_bytes: int = 32_000) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if any(part in key for part in _SECRET_PARTS):
                raise LifeGraphError(f"{field} must not contain credentials or secrets")
            if key in _EXECUTOR_KEYS:
                raise LifeGraphError(f"{field} cannot request an external action")
            _safe_json(child, field=field, max_bytes=max_bytes)
    elif isinstance(value, list):
        for child in value:
            _safe_json(child, field=field, max_bytes=max_bytes)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise LifeGraphError(f"{field} must be JSON-serializable") from exc
    if len(encoded) > max_bytes:
        raise LifeGraphError(f"{field} must not exceed {max_bytes} bytes")


def _string_list(
    value: object | None, *, field: str, max_items: int, item_limit: int,
    tokens: bool = False,
) -> list[str]:
    if value is None:
        rows: list[object] = []
    elif isinstance(value, list):
        rows = value
    else:
        raise LifeGraphError(f"{field} must be a list")
    if len(rows) > max_items:
        raise LifeGraphError(f"{field} must not contain more than {max_items} items")
    result: list[str] = []
    seen: set[str] = set()
    for row in rows:
        item = (
            _token(row, field=field, limit=item_limit)
            if tokens else _text(row, field=field, limit=item_limit, required=True)
        )
        marker = item.casefold()
        if marker not in seen:
            result.append(item)
            seen.add(marker)
    return result


def _entity_ids(value: object | None, *, field: str, max_items: int = 50) -> list[str]:
    rows = _string_list(
        value, field=field, max_items=max_items, item_limit=64, tokens=False
    )
    for item in rows:
        if len(item) > 64 or any(character.isspace() for character in item):
            raise LifeGraphError(f"{field} contains an invalid entity id")
    return rows


def _source(value: object | None) -> dict[str, Any]:
    if value is None:
        raw: Mapping[str, Any] = {"kind": "manual", "label": "User entry"}
    elif isinstance(value, Mapping):
        raw = value
    else:
        raise LifeGraphError("source must be an object")
    unknown = set(raw) - {"kind", "label", "external_id", "source_id"}
    if unknown:
        raise LifeGraphError("source contains unsupported fields")
    kind = _token(raw.get("kind") or "manual", field="source.kind")
    if kind not in SOURCE_KINDS:
        raise LifeGraphError("source.kind is unsupported")
    result = {
        "kind": kind,
        "label": _text(
            raw.get("label") or "User entry", field="source.label", limit=240,
            required=True,
        ),
        "external_id": _text(
            raw.get("external_id", ""), field="source.external_id", limit=500
        ) or None,
        "source_id": _text(
            raw.get("source_id", ""), field="source.source_id", limit=64
        ) or None,
    }
    if kind in {"import", "integration", "email", "telegram", "meeting", "upload"} and not (
        result["external_id"] or result["source_id"]
    ):
        raise LifeGraphError("Non-manual task sources require external_id or source_id")
    _safe_json(result, field="source", max_bytes=4_000)
    return result


def _completion_evidence(value: object | None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 30:
        raise LifeGraphError("completion_evidence must be a list of at most 30 items")
    result: list[dict[str, Any]] = []
    for row in value:
        if isinstance(row, str):
            raw: Mapping[str, Any] = {"label": row}
        elif isinstance(row, Mapping):
            raw = row
        else:
            raise LifeGraphError("each completion evidence item must be a string or object")
        unknown = set(raw) - {"label", "entity_id", "source_id", "url", "recorded_at"}
        if unknown:
            raise LifeGraphError("completion evidence contains unsupported fields")
        item = {
            "label": _text(
                raw.get("label"), field="completion_evidence.label", limit=1_000,
                required=True, preserve_lines=True,
            ),
            "entity_id": _text(
                raw.get("entity_id", ""), field="completion_evidence.entity_id", limit=64
            ) or None,
            "source_id": _text(
                raw.get("source_id", ""), field="completion_evidence.source_id", limit=64
            ) or None,
            "url": _text(
                raw.get("url", ""), field="completion_evidence.url", limit=2_000
            ) or None,
            "recorded_at": _iso(
                raw.get("recorded_at") or utcnow_naive(),
                field="completion_evidence.recorded_at",
            ),
        }
        result.append(item)
    _safe_json(result, field="completion_evidence", max_bytes=24_000)
    return result


def validate_task_properties(value: object, *, status: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LifeGraphError("task properties must be an object")
    normalized_status = _token(status, field="status", limit=32)
    if normalized_status not in TASK_STATUSES:
        raise LifeGraphError("task status is unsupported")
    priority = _token(value.get("priority") or "normal", field="priority")
    if priority not in TASK_PRIORITIES:
        raise LifeGraphError("priority must be low, normal, high, or critical")
    energy = _token(value.get("energy") or "any", field="energy")
    if energy not in TASK_ENERGY_LEVELS:
        raise LifeGraphError("energy must be low, medium, high, or any")
    project_id = _text(value.get("project_id", ""), field="project_id", limit=64) or None
    evidence = _completion_evidence(value.get("completion_evidence"))
    completed_at = _iso(value.get("completed_at"), field="completed_at")
    if normalized_status == "completed":
        if not evidence:
            raise LifeGraphError("completed tasks require completion_evidence")
        completed_at = completed_at or _iso(utcnow_naive(), field="completed_at")
    elif completed_at is not None:
        raise LifeGraphError("completed_at is only valid for completed tasks")
    properties = {
        "task_schema_version": TASK_SCHEMA_VERSION,
        "definition_of_done": _text(
            value.get("definition_of_done"), field="definition_of_done",
            limit=4_000, required=True, preserve_lines=True,
        ),
        "priority": priority,
        "effort_minutes": _integer(
            value.get("effort_minutes"), field="effort_minutes",
            minimum=1, maximum=10_080,
        ),
        "energy": energy,
        "contexts": _string_list(
            value.get("contexts"), field="contexts", max_items=20,
            item_limit=64, tokens=True,
        ),
        "project_id": project_id,
        "people_ids": _entity_ids(value.get("people_ids"), field="people_ids"),
        "dependency_ids": _entity_ids(
            value.get("dependency_ids"), field="dependency_ids"
        ),
        "document_ids": _entity_ids(
            value.get("document_ids"), field="document_ids"
        ),
        "source": _source(value.get("source")),
        "next_action": _text(
            value.get("next_action", ""), field="next_action", limit=1_000,
            preserve_lines=True,
        ) or None,
        "waiting_on": _text(
            value.get("waiting_on", ""), field="waiting_on", limit=1_000,
            preserve_lines=True,
        ) or None,
        "completion_evidence": evidence,
        "completed_at": completed_at,
    }
    if normalized_status == "waiting" and not properties["waiting_on"]:
        raise LifeGraphError("waiting tasks require waiting_on")
    if normalized_status == "blocked" and not (
        properties["dependency_ids"] or properties["waiting_on"]
    ):
        raise LifeGraphError("blocked tasks require a dependency or waiting_on")
    _safe_json(properties, field="task properties")
    return properties


def is_typed_task_payload(entity_type: object, properties: object | None = None) -> bool:
    return str(entity_type or "").strip().lower() == "task" and isinstance(
        properties, Mapping
    ) and (
        properties.get("task_schema_version") is not None
        or properties.get("definition_of_done") is not None
        or properties.get("effort_minutes") is not None
    )


def _owned_task(db, owner_id: str, entity_id: object, *, include_deleted: bool = False) -> LifeEntity:
    try:
        entity = get_life_entity(
            db, owner_id=owner_id, entity_id=entity_id,
            include_deleted=include_deleted,
        )
    except LifeGraphNotFound as exc:
        raise LifeGraphNotFound("Task not found") from exc
    if entity.entity_type != "task":
        raise LifeGraphNotFound("Task not found")
    validate_task_properties(entity.properties or {}, status=entity.status)
    return entity


def _validate_authority(db, *, owner_id: str, properties: Mapping[str, Any], task_id: str | None = None) -> None:
    source_id = properties["source"].get("source_id")
    if source_id and db.query(LifeSource.id).filter(
        LifeSource.id == source_id, LifeSource.owner_id == owner_id,
    ).scalar() is None:
        raise LifeGraphNotFound("Task source not found")
    fields: dict[str, list[str]] = {
        "people_ids": list(properties["people_ids"]),
        "dependency_ids": list(properties["dependency_ids"]),
        "document_ids": list(properties["document_ids"]),
    }
    if properties.get("project_id"):
        fields["project_id"] = [properties["project_id"]]
    for field, ids in fields.items():
        allowed = _REFERENCE_TYPES[field]
        for entity_id in ids:
            if task_id and entity_id == task_id:
                raise LifeGraphError("A task cannot depend on or reference itself")
            target = get_life_entity(db, owner_id=owner_id, entity_id=entity_id)
            if target.entity_type not in allowed:
                raise LifeGraphError(
                    f"{field} must reference: {', '.join(sorted(allowed))}"
                )
    for row in properties["completion_evidence"]:
        source_ref = row.get("source_id")
        if source_ref and db.query(LifeSource.id).filter(
            LifeSource.id == source_ref, LifeSource.owner_id == owner_id,
        ).scalar() is None:
            raise LifeGraphNotFound("Completion evidence source not found")
        entity_ref = row.get("entity_id")
        if entity_ref:
            target = get_life_entity(db, owner_id=owner_id, entity_id=entity_ref)
            if target.entity_type not in _EVIDENCE_TYPES:
                raise LifeGraphError("Completion evidence entity type is unsupported")


def _provenance(value: object | None) -> dict[str, Any]:
    if value is None:
        result: dict[str, Any] = {}
    elif isinstance(value, Mapping):
        result = dict(value)
    else:
        raise LifeGraphError("provenance must be an object")
    _safe_json(result, field="provenance", max_bytes=16_000)
    result.setdefault("capture", "manual")
    result["domain"] = "tasks"
    return result


def create_task_record(
    db,
    *,
    account: Account,
    title: object,
    definition_of_done: object,
    effort_minutes: object,
    priority: object = "normal",
    deadline: object | None = None,
    energy: object = "any",
    contexts: object | None = None,
    project_id: object | None = None,
    people_ids: object | None = None,
    dependency_ids: object | None = None,
    document_ids: object | None = None,
    source: object | None = None,
    status: object = "active",
    next_action: object | None = None,
    waiting_on: object | None = None,
    completion_evidence: object | None = None,
    completed_at: object | None = None,
    note: object = "",
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    normalized_status = _token(status, field="status", limit=32)
    normalized_sensitivity = _token(sensitivity, field="sensitivity", limit=24)
    if normalized_sensitivity not in SENSITIVITIES:
        raise LifeGraphError("Task sensitivity must be private or restricted")
    properties = validate_task_properties({
        "definition_of_done": definition_of_done,
        "priority": priority,
        "effort_minutes": effort_minutes,
        "energy": energy,
        "contexts": contexts,
        "project_id": project_id,
        "people_ids": people_ids,
        "dependency_ids": dependency_ids,
        "document_ids": document_ids,
        "source": source,
        "next_action": next_action,
        "waiting_on": waiting_on,
        "completion_evidence": completion_evidence,
        "completed_at": completed_at,
    }, status=normalized_status)
    _validate_authority(db, owner_id=account.id, properties=properties)
    return create_life_entity(
        db,
        account=account,
        entity_type="task",
        title=_text(title, field="title", limit=240, required=True),
        summary=_text(note, field="note", limit=20_000, preserve_lines=True),
        status=normalized_status,
        properties=properties,
        provenance=_provenance(provenance),
        confidence=confidence,
        sensitivity=normalized_sensitivity,
        due_at=_datetime(deadline, field="deadline"),
        idempotency_key=idempotency_key,
        reason="Typed task created",
    )


def update_task_record(
    db,
    *,
    account: Account,
    entity_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    entity = _owned_task(db, account.id, entity_id)
    allowed = {
        "title", "note", "definition_of_done", "priority", "deadline",
        "effort_minutes", "energy", "contexts", "project_id", "people_ids",
        "dependency_ids", "document_ids", "source", "status", "next_action",
        "waiting_on", "completion_evidence", "completed_at", "provenance",
        "confidence", "sensitivity",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported task fields: {', '.join(unknown)}")
    current = validate_task_properties(entity.properties or {}, status=entity.status)
    new_status = _token(changes.get("status", entity.status), field="status", limit=32)
    merged = dict(current)
    for field in (
        "definition_of_done", "priority", "effort_minutes", "energy", "contexts",
        "project_id", "people_ids", "dependency_ids", "document_ids", "source",
        "next_action", "waiting_on", "completion_evidence", "completed_at",
    ):
        if field in changes:
            merged[field] = changes[field]
    if new_status == "completed" and entity.status != "completed" and "completed_at" not in changes:
        merged["completed_at"] = utcnow_naive()
    if new_status != "completed":
        merged["completed_at"] = None
        if entity.status == "completed" and "completion_evidence" not in changes:
            merged["completion_evidence"] = []
    properties = validate_task_properties(merged, status=new_status)
    _validate_authority(
        db, owner_id=account.id, properties=properties, task_id=entity.id
    )
    entity_changes: dict[str, Any] = {
        "status": new_status,
        "properties": properties,
    }
    if "title" in changes:
        entity_changes["title"] = _text(
            changes["title"], field="title", limit=240, required=True
        )
    if "note" in changes:
        entity_changes["summary"] = _text(
            changes["note"], field="note", limit=20_000, preserve_lines=True
        )
    if "deadline" in changes:
        entity_changes["due_at"] = _datetime(changes["deadline"], field="deadline")
    if "provenance" in changes:
        entity_changes["provenance"] = _provenance(changes["provenance"])
    for field in ("confidence", "sensitivity"):
        if field in changes:
            entity_changes[field] = changes[field]
    if "sensitivity" in entity_changes:
        sensitivity = _token(entity_changes["sensitivity"], field="sensitivity", limit=24)
        if sensitivity not in SENSITIVITIES:
            raise LifeGraphError("Task sensitivity must be private or restricted")
        entity_changes["sensitivity"] = sensitivity
    return update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=entity_changes,
        reason="Typed task updated",
    )


def delete_task_record(
    db, *, owner_id: str, entity_id: object, expected_version: int, reason: object
) -> LifeEntity:
    entity = _owned_task(db, owner_id, entity_id)
    return delete_life_entity(
        db, owner_id=owner_id, entity_id=entity.id,
        expected_version=expected_version, reason=reason,
    )


def serialize_task_record(entity: LifeEntity) -> dict[str, Any]:
    properties = validate_task_properties(entity.properties or {}, status=entity.status)
    payload = serialize_life_entity(entity)
    payload.update({
        **properties,
        "deadline": payload["due_at"],
        "note": payload["summary"],
        "execution_policy": {
            "record_only": True,
            "can_execute_external_action": False,
            "scheduled_task_authority": False,
        },
    })
    return payload


def get_task_record(db, *, owner_id: str, entity_id: object) -> dict[str, Any]:
    return serialize_task_record(_owned_task(db, owner_id, entity_id))


def list_task_records(
    db, *, owner_id: str, status: object | None = None, limit: int = 50
) -> tuple[list[dict[str, Any]], bool]:
    bounded = max(1, min(100, int(limit)))
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == "task",
        LifeEntity.deleted_at.is_(None),
    )
    if status:
        normalized = _token(status, field="status", limit=32)
        if normalized not in TASK_STATUSES:
            raise LifeGraphError("task status is unsupported")
        query = query.filter(LifeEntity.status == normalized)
    rows = query.order_by(
        LifeEntity.due_at.asc(), LifeEntity.updated_at.desc(), LifeEntity.id.asc()
    ).limit(TASK_SCAN_LIMIT + 1).all()
    scan_truncated = len(rows) > TASK_SCAN_LIMIT
    rows = rows[:TASK_SCAN_LIMIT]
    typed = [
        serialize_task_record(row) for row in rows
        if isinstance(row.properties, Mapping)
        and row.properties.get("task_schema_version") == TASK_SCHEMA_VERSION
    ]
    return typed[:bounded], scan_truncated or len(typed) > bounded


def search_task_records(
    db, *, owner_id: str, query_text: object, status: object | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    needle = _text(query_text, field="q", limit=500, required=True).casefold()
    rows, truncated = list_task_records(
        db, owner_id=owner_id, status=status, limit=100
    )
    matches = [
        row for row in rows
        if needle in json.dumps({
            "title": row["title"], "note": row["note"],
            "definition_of_done": row["definition_of_done"],
            "next_action": row["next_action"], "waiting_on": row["waiting_on"],
            "contexts": row["contexts"],
        }, ensure_ascii=False).casefold()
    ]
    bounded = max(1, min(100, int(limit)))
    return {
        "items": matches[:bounded],
        "count": min(len(matches), bounded),
        "truncated": truncated or len(matches) > bounded,
    }


def task_record_history(
    db, *, owner_id: str, entity_id: object, limit: int = 100
) -> tuple[list[dict[str, Any]], bool]:
    entity = _owned_task(db, owner_id, entity_id, include_deleted=True)
    rows, truncated = list_life_entity_versions(
        db, owner_id=owner_id, entity_id=entity.id, limit=limit
    )
    return [serialize_life_entity_version(row) for row in rows], truncated


__all__ = [
    "TASK_SCHEMA_VERSION", "TASK_STATUSES", "create_task_record",
    "delete_task_record", "get_task_record", "is_typed_task_payload",
    "list_task_records", "search_task_records", "serialize_task_record",
    "task_record_history", "update_task_record", "validate_task_properties",
]
