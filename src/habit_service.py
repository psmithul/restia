"""Typed V3 Habits & Routines authority on the canonical Life graph.

Habit definitions are owner-scoped ``LifeEntity(entity_type="habit")`` rows.
Routine observations use the existing ``metric`` entity type so this domain
does not introduce a parallel store or require a schema migration.  The
service adds bounded schemas, optimistic concurrency, provenance checks, and
deterministic reports; it never executes an external action.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.database import Account, LifeEntity
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


HABIT_SCHEMA_VERSION = 1
HABIT_LOG_SCHEMA_VERSION = 1
HABIT_ENTITY_TYPE = "habit"
HABIT_LOG_ENTITY_TYPE = "metric"
HABIT_SCAN_LIMIT = 500
HABIT_LOG_SCAN_LIMIT = 2_000
MAX_REPORT_DAYS = 366

ROUTINE_TYPES = frozenset({
    "morning", "evening", "workout", "meals", "review", "learning",
    "finance", "relationship", "maintenance", "sleep", "custom",
})
HABIT_STATUSES = frozenset({"active", "paused", "archived"})
CADENCES = frozenset({"daily", "weekdays", "weekly", "interval", "custom"})
TRIGGER_KINDS = frozenset({
    "time", "after_event", "location", "context", "manual",
})
RECOVERY_STRATEGIES = frozenset({
    "same_day", "next_available", "next_scheduled", "manual",
})
LOG_RESULTS = frozenset({
    "completed", "partial", "skipped", "missed", "recovered",
})
EVIDENCE_STATUSES = frozenset({"completed", "partial", "skipped"})
SOURCE_KINDS = frozenset({"manual", "import", "integration"})
SENSITIVITIES = frozenset({"private", "restricted"})

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_SECRET_KEY_PARTS = (
    "password", "secret", "token", "credential", "cookie", "authorization",
    "api_key", "private_key",
)
_AUTONOMOUS_KEYS = frozenset({
    "execute", "executor", "tool_call", "external_action", "webhook",
    "send_message", "send_email", "payment", "purchase", "automation",
})
_RESULT_PRIORITY = {
    "recovered": 5,
    "completed": 4,
    "partial": 3,
    "skipped": 2,
    "missed": 1,
}


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
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise LifeGraphError(f"{field} must be an integer from {minimum} to {maximum}")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise LifeGraphError(
            f"{field} must be an integer from {minimum} to {maximum}"
        ) from exc
    if number < minimum or number > maximum:
        raise LifeGraphError(f"{field} must be an integer from {minimum} to {maximum}")
    return number


def _date(value: object | None, *, field: str, required: bool = False) -> date | None:
    if value is None or value == "":
        if required:
            raise LifeGraphError(f"{field} is required")
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
    return parsed


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
        parsed = datetime.combine(value, time.min)
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


def _iso_datetime(value: object | None, *, field: str) -> str | None:
    parsed = _datetime(value, field=field)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _aware_datetime(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise LifeGraphError(
                f"{field} must be an ISO-8601 datetime with a UTC offset"
            ) from exc
    else:
        raise LifeGraphError(f"{field} must be an ISO-8601 datetime with a UTC offset")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LifeGraphError(f"{field} must include a UTC offset")
    return parsed


def _bounded_json(
    value: object | None, *, field: str, max_bytes: int = 24_000
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LifeGraphError(f"{field} must be an object")
    result = dict(value)
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


def _assert_safe_payload(value: object, *, field: str) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower()
            if any(part in key for part in _SECRET_KEY_PARTS):
                raise LifeGraphError(f"{field} must not contain credentials or secrets")
            if key in _AUTONOMOUS_KEYS:
                raise LifeGraphError(
                    f"{field} cannot request autonomous or external actions"
                )
            _assert_safe_payload(child, field=field)
    elif isinstance(value, list):
        for child in value:
            _assert_safe_payload(child, field=field)


def _string_list(
    value: object | None,
    *,
    field: str,
    max_items: int,
    item_limit: int,
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
            if tokens
            else _text(row, field=field, limit=item_limit, required=True)
        )
        marker = item.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result


def _schedule(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LifeGraphError("schedule must be an object")
    cadence = _token(value.get("cadence"), field="schedule.cadence")
    if cadence not in CADENCES:
        raise LifeGraphError(
            "schedule.cadence must be daily, weekdays, weekly, interval, or custom"
        )
    start_date = _date(
        value.get("start_date"), field="schedule.start_date", required=True
    )
    end_date = _date(value.get("end_date"), field="schedule.end_date")
    if end_date is not None and start_date is not None and end_date < start_date:
        raise LifeGraphError("schedule.end_date must not be before schedule.start_date")
    raw_days = value.get("days_of_week") or []
    if not isinstance(raw_days, list) or len(raw_days) > 7:
        raise LifeGraphError("schedule.days_of_week must be a list of at most 7 weekdays")
    days = sorted({
        _integer(day, field="schedule.days_of_week", minimum=0, maximum=6)
        for day in raw_days
    })
    if cadence in {"weekly", "custom"} and not days:
        raise LifeGraphError(f"{cadence} schedules require days_of_week")
    if cadence in {"daily", "weekdays", "interval"} and days:
        raise LifeGraphError(f"{cadence} schedules must not define days_of_week")
    raw_interval = value.get("interval_days")
    interval_days = None
    if cadence == "interval":
        interval_days = _integer(
            raw_interval, field="schedule.interval_days", minimum=1, maximum=365
        )
    elif raw_interval not in (None, ""):
        raise LifeGraphError("schedule.interval_days is only valid for interval schedules")
    time_of_day = str(value.get("time_of_day") or "").strip() or None
    if time_of_day is not None and not _TIME_RE.fullmatch(time_of_day):
        raise LifeGraphError("schedule.time_of_day must use HH:MM in 24-hour time")
    timezone_name = str(value.get("timezone") or "UTC").strip()
    if not timezone_name or len(timezone_name) > 64:
        raise LifeGraphError("schedule.timezone must be a valid IANA timezone")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise LifeGraphError(
            "schedule.timezone must be a valid IANA timezone"
        ) from exc
    return {
        "cadence": cadence,
        "days_of_week": days,
        "interval_days": interval_days,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat() if end_date else None,
        "time_of_day": time_of_day,
        "timezone": timezone_name,
        "grace_minutes": _integer(
            value.get("grace_minutes", 0),
            field="schedule.grace_minutes",
            minimum=0,
            maximum=720,
        ),
    }


def _triggers(value: object | None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise LifeGraphError("triggers must be a list of at most 20 items")
    result: list[dict[str, Any]] = []
    for row in value:
        if not isinstance(row, Mapping):
            raise LifeGraphError("each trigger must be an object")
        kind = _token(row.get("kind"), field="trigger.kind")
        if kind not in TRIGGER_KINDS:
            raise LifeGraphError("trigger.kind is unsupported")
        cue = _text(row.get("cue"), field="trigger.cue", limit=500, required=True)
        at = str(row.get("at") or "").strip() or None
        if kind == "time":
            if at is None or not _TIME_RE.fullmatch(at):
                raise LifeGraphError("time triggers require at in HH:MM format")
        elif at is not None:
            raise LifeGraphError("trigger.at is only valid for time triggers")
        result.append({"kind": kind, "cue": cue, "at": at})
    return result


def _checklist(value: object | None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 50:
        raise LifeGraphError("checklist must be a list of at most 50 items")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(value, start=1):
        if isinstance(row, str):
            raw: Mapping[str, Any] = {"label": row}
        elif isinstance(row, Mapping):
            raw = row
        else:
            raise LifeGraphError("each checklist item must be a string or object")
        item_id = _token(
            raw.get("id") or f"item_{index}", field="checklist.id", limit=64
        )
        if item_id in seen:
            raise LifeGraphError("checklist ids must be unique")
        seen.add(item_id)
        result.append({
            "id": item_id,
            "label": _text(
                raw.get("label"), field="checklist.label", limit=300, required=True
            ),
            "required": bool(raw.get("required", True)),
        })
    return result


def _minimum_viable(
    value: object | None,
    *,
    duration_minutes: int,
    checklist: list[dict[str, Any]],
) -> dict[str, Any]:
    if value is None:
        raw: Mapping[str, Any] = {}
    elif isinstance(value, Mapping):
        raw = value
    else:
        raise LifeGraphError("minimum_viable must be an object")
    duration = _integer(
        raw.get("duration_minutes", min(duration_minutes, 5)),
        field="minimum_viable.duration_minutes",
        minimum=1,
        maximum=1_440,
    )
    if duration > duration_minutes:
        raise LifeGraphError("minimum_viable.duration_minutes cannot exceed duration_minutes")
    item_ids = _string_list(
        raw.get("checklist_item_ids"),
        field="minimum_viable.checklist_item_ids",
        max_items=50,
        item_limit=64,
        tokens=True,
    )
    known = {item["id"] for item in checklist}
    unknown = sorted(set(item_ids) - known)
    if unknown:
        raise LifeGraphError(
            "minimum_viable checklist items do not exist: " + ", ".join(unknown)
        )
    return {
        "duration_minutes": duration,
        "checklist_item_ids": item_ids,
        "description": _text(
            raw.get("description", ""),
            field="minimum_viable.description",
            limit=1_000,
            preserve_lines=True,
        ),
    }


def _recovery_rules(value: object | None, *, duration_minutes: int) -> dict[str, Any]:
    if value is None:
        raw: Mapping[str, Any] = {}
    elif isinstance(value, Mapping):
        raw = value
    else:
        raise LifeGraphError("recovery_rules must be an object")
    strategy = _token(
        raw.get("strategy") or "next_available", field="recovery_rules.strategy"
    )
    if strategy not in RECOVERY_STRATEGIES:
        raise LifeGraphError("recovery_rules.strategy is unsupported")
    minimum_duration = _integer(
        raw.get("minimum_duration_minutes", min(duration_minutes, 5)),
        field="recovery_rules.minimum_duration_minutes",
        minimum=1,
        maximum=1_440,
    )
    if minimum_duration > duration_minutes:
        raise LifeGraphError(
            "recovery_rules.minimum_duration_minutes cannot exceed duration_minutes"
        )
    return {
        "strategy": strategy,
        "window_hours": _integer(
            raw.get("window_hours", 24),
            field="recovery_rules.window_hours",
            minimum=1,
            maximum=720,
        ),
        "minimum_duration_minutes": minimum_duration,
        "note": _text(
            raw.get("note", ""), field="recovery_rules.note", limit=1_000,
            preserve_lines=True,
        ),
    }


def validate_habit_properties(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LifeGraphError("habit properties must be an object")
    routine_type = _token(value.get("routine_type"), field="routine_type")
    if routine_type not in ROUTINE_TYPES:
        raise LifeGraphError(
            "routine_type must be one of: " + ", ".join(sorted(ROUTINE_TYPES))
        )
    duration = _integer(
        value.get("duration_minutes"), field="duration_minutes", minimum=1, maximum=1_440
    )
    checklist = _checklist(value.get("checklist"))
    properties = {
        "habit_schema_version": HABIT_SCHEMA_VERSION,
        "routine_type": routine_type,
        "schedule": _schedule(value.get("schedule")),
        "triggers": _triggers(value.get("triggers")),
        "checklist": checklist,
        "contexts": _string_list(
            value.get("contexts"), field="contexts", max_items=20,
            item_limit=64, tokens=True,
        ),
        "duration_minutes": duration,
        "minimum_viable": _minimum_viable(
            value.get("minimum_viable"),
            duration_minutes=duration,
            checklist=checklist,
        ),
        "recovery_rules": _recovery_rules(
            value.get("recovery_rules"), duration_minutes=duration
        ),
    }
    _bounded_json(properties, field="habit properties")
    return properties


def is_typed_habit_payload(entity_type: object, properties: object | None = None) -> bool:
    normalized = str(entity_type or "").strip().lower()
    if not isinstance(properties, Mapping):
        return False
    if normalized == HABIT_ENTITY_TYPE:
        return (
            properties.get("habit_schema_version") is not None
            or (
                properties.get("routine_type") is not None
                and properties.get("schedule") is not None
            )
        )
    return normalized == HABIT_LOG_ENTITY_TYPE and (
        properties.get("habit_log_schema_version") is not None
        or (
            properties.get("habit_id") is not None
            and properties.get("result") in LOG_RESULTS
            and properties.get("scheduled_for") is not None
        )
    )


def _owned_habit(
    db, owner_id: str, entity_id: object, *, include_deleted: bool = False
) -> LifeEntity:
    try:
        entity = get_life_entity(
            db, owner_id=owner_id, entity_id=entity_id, include_deleted=include_deleted
        )
    except LifeGraphNotFound as exc:
        raise LifeGraphNotFound("Habit not found") from exc
    if entity.entity_type != HABIT_ENTITY_TYPE:
        raise LifeGraphNotFound("Habit not found")
    validate_habit_properties(entity.properties or {})
    return entity


def _normalize_provenance(value: object | None) -> dict[str, Any]:
    provenance = _bounded_json(value, field="provenance", max_bytes=16_000)
    provenance.setdefault("capture", "manual")
    provenance["domain"] = "habits"
    return provenance


def create_habit(
    db,
    *,
    account: Account,
    title: object,
    routine_type: object,
    schedule: object,
    duration_minutes: object,
    triggers: object | None = None,
    checklist: object | None = None,
    contexts: object | None = None,
    minimum_viable: object | None = None,
    recovery_rules: object | None = None,
    note: object = "",
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    normalized_sensitivity = _token(sensitivity, field="sensitivity", limit=24)
    if normalized_sensitivity not in SENSITIVITIES:
        raise LifeGraphError("Habit sensitivity must be private or restricted")
    properties = validate_habit_properties({
        "routine_type": routine_type,
        "schedule": schedule,
        "triggers": triggers,
        "checklist": checklist,
        "contexts": contexts,
        "duration_minutes": duration_minutes,
        "minimum_viable": minimum_viable,
        "recovery_rules": recovery_rules,
    })
    return create_life_entity(
        db,
        account=account,
        entity_type=HABIT_ENTITY_TYPE,
        title=_text(title, field="title", limit=240, required=True),
        summary=_text(note, field="note", limit=20_000, preserve_lines=True),
        status="active",
        properties=properties,
        provenance=_normalize_provenance(provenance),
        confidence=confidence,
        sensitivity=normalized_sensitivity,
        occurred_at=datetime.combine(
            _date(properties["schedule"]["start_date"], field="schedule.start_date", required=True),
            time.min,
        ),
        idempotency_key=idempotency_key,
        reason="Habit created",
    )


def update_habit(
    db,
    *,
    account: Account,
    entity_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    entity = _owned_habit(db, account.id, entity_id)
    allowed = {
        "title", "routine_type", "schedule", "triggers", "checklist", "contexts",
        "duration_minutes", "minimum_viable", "recovery_rules", "note",
        "provenance", "confidence", "sensitivity", "status",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported habit fields: {', '.join(unknown)}")
    current = validate_habit_properties(entity.properties or {})
    merged = dict(current)
    for field in (
        "routine_type", "schedule", "triggers", "checklist", "contexts",
        "duration_minutes", "minimum_viable", "recovery_rules",
    ):
        if field in changes:
            merged[field] = changes[field]
    properties = validate_habit_properties(merged)
    entity_changes: dict[str, Any] = {"properties": properties}
    if "title" in changes:
        entity_changes["title"] = _text(
            changes["title"], field="title", limit=240, required=True
        )
    if "note" in changes:
        entity_changes["summary"] = _text(
            changes["note"], field="note", limit=20_000, preserve_lines=True
        )
    if "provenance" in changes:
        entity_changes["provenance"] = _normalize_provenance(changes["provenance"])
    if "confidence" in changes:
        entity_changes["confidence"] = changes["confidence"]
    if "sensitivity" in changes:
        sensitivity = _token(changes["sensitivity"], field="sensitivity", limit=24)
        if sensitivity not in SENSITIVITIES:
            raise LifeGraphError("Habit sensitivity must be private or restricted")
        entity_changes["sensitivity"] = sensitivity
    if "status" in changes:
        status = _token(changes["status"], field="status", limit=32)
        if status not in HABIT_STATUSES:
            raise LifeGraphError("Habit status must be active, paused, or archived")
        entity_changes["status"] = status
    if "schedule" in changes:
        entity_changes["occurred_at"] = datetime.combine(
            _date(properties["schedule"]["start_date"], field="schedule.start_date", required=True),
            time.min,
        )
    return update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=entity_changes,
        reason="Habit updated",
    )


def delete_habit(
    db, *, owner_id: str, entity_id: object, expected_version: int, reason: object
) -> LifeEntity:
    entity = _owned_habit(db, owner_id, entity_id)
    return delete_life_entity(
        db,
        owner_id=owner_id,
        entity_id=entity.id,
        expected_version=expected_version,
        reason=reason,
    )


def serialize_habit(entity: LifeEntity) -> dict[str, Any]:
    if entity.entity_type != HABIT_ENTITY_TYPE:
        raise LifeGraphError("Entity is not a habit")
    properties = validate_habit_properties(entity.properties or {})
    payload = serialize_life_entity(entity)
    payload.update({
        "routine_type": properties["routine_type"],
        "schedule": properties["schedule"],
        "triggers": properties["triggers"],
        "checklist": properties["checklist"],
        "contexts": properties["contexts"],
        "duration_minutes": properties["duration_minutes"],
        "minimum_viable": properties["minimum_viable"],
        "recovery_rules": properties["recovery_rules"],
        "note": payload["summary"],
        "execution_policy": {
            "record_only": True,
            "can_execute_external_action": False,
        },
    })
    return payload


def get_habit(db, *, owner_id: str, entity_id: object) -> dict[str, Any]:
    return serialize_habit(_owned_habit(db, owner_id, entity_id))


def _habit_candidates(
    db,
    *,
    owner_id: str,
    routine_type: object | None = None,
    status: object | None = None,
) -> tuple[list[LifeEntity], bool]:
    normalized_type = None
    if routine_type:
        normalized_type = _token(routine_type, field="routine_type")
        if normalized_type not in ROUTINE_TYPES:
            raise LifeGraphError("Unsupported routine_type")
    normalized_status = None
    if status:
        normalized_status = _token(status, field="status", limit=32)
        if normalized_status not in HABIT_STATUSES:
            raise LifeGraphError("Unsupported habit status")
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == HABIT_ENTITY_TYPE,
        LifeEntity.deleted_at.is_(None),
    )
    if normalized_status:
        query = query.filter(LifeEntity.status == normalized_status)
    rows = query.order_by(
        LifeEntity.updated_at.desc(), LifeEntity.id.desc()
    ).limit(HABIT_SCAN_LIMIT + 1).all()
    truncated = len(rows) > HABIT_SCAN_LIMIT
    result: list[LifeEntity] = []
    for row in rows[:HABIT_SCAN_LIMIT]:
        try:
            properties = validate_habit_properties(row.properties or {})
        except LifeGraphError:
            continue
        if normalized_type and properties["routine_type"] != normalized_type:
            continue
        result.append(row)
    return result, truncated


def list_habits(
    db,
    *,
    owner_id: str,
    routine_type: object | None = None,
    status: object | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    bounded = max(1, min(100, int(limit)))
    rows, truncated = _habit_candidates(
        db, owner_id=owner_id, routine_type=routine_type, status=status
    )
    return [serialize_habit(row) for row in rows[:bounded]], (
        truncated or len(rows) > bounded
    )


def search_habits(
    db,
    *,
    owner_id: str,
    query_text: object,
    routine_type: object | None = None,
    status: object | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    needle = str(query_text or "").strip().casefold()
    if not needle:
        raise LifeGraphError("q is required")
    bounded = max(1, min(100, int(limit)))
    rows, scan_truncated = _habit_candidates(
        db, owner_id=owner_id, routine_type=routine_type, status=status
    )
    matches: list[tuple[tuple[int, str, str], dict[str, Any]]] = []
    for entity in rows:
        habit = serialize_habit(entity)
        title = str(habit["title"]).casefold()
        note = str(habit["note"]).casefold()
        body = json.dumps({
            "routine_type": habit["routine_type"],
            "schedule": habit["schedule"],
            "contexts": habit["contexts"],
            "triggers": habit["triggers"],
            "checklist": habit["checklist"],
            "duration_minutes": habit["duration_minutes"],
            "minimum_viable": habit["minimum_viable"],
            "recovery_rules": habit["recovery_rules"],
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
        matches.append(((rank, title, entity.id), {
            "habit": habit, "match": field, "rank": rank,
        }))
    matches.sort(key=lambda row: row[0])
    items = [item for _, item in matches[:bounded]]
    return {
        "items": items,
        "count": len(items),
        "scanned": len(rows),
        "truncated": scan_truncated or len(matches) > bounded,
    }


def _source(value: object | None) -> dict[str, Any]:
    if value is None:
        raw: Mapping[str, Any] = {"kind": "manual", "label": "User entry"}
    elif isinstance(value, Mapping):
        raw = value
    else:
        raise LifeGraphError("source must be an object")
    kind = _token(raw.get("kind") or "manual", field="source.kind")
    if kind not in SOURCE_KINDS:
        raise LifeGraphError("source.kind must be manual, import, or integration")
    result = {
        "kind": kind,
        "label": _text(
            raw.get("label") or "User entry", field="source.label", limit=240,
            required=True,
        ),
        "external_id": _text(
            raw.get("external_id", ""), field="source.external_id", limit=500
        ) or None,
    }
    if kind != "manual" and not result["external_id"]:
        raise LifeGraphError("Imported habit logs require source.external_id")
    _bounded_json(result, field="source", max_bytes=4_000)
    return result


def _checklist_evidence(
    value: object | None, *, checklist: list[dict[str, Any]], result: str
) -> list[dict[str, Any]]:
    if value is None:
        rows: list[object] = []
    elif isinstance(value, list):
        rows = value
    else:
        raise LifeGraphError("checklist_evidence must be a list")
    if len(rows) > 50:
        raise LifeGraphError("checklist_evidence must not contain more than 50 items")
    known = {item["id"]: item for item in checklist}
    evidence: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            raise LifeGraphError("each checklist evidence item must be an object")
        item_id = _token(row.get("item_id"), field="checklist_evidence.item_id")
        if item_id not in known:
            raise LifeGraphError(f"Unknown checklist evidence item: {item_id}")
        if item_id in seen:
            raise LifeGraphError("checklist evidence item ids must be unique")
        seen.add(item_id)
        status = _token(row.get("status"), field="checklist_evidence.status")
        if status not in EVIDENCE_STATUSES:
            raise LifeGraphError("checklist evidence status is unsupported")
        evidence.append({
            "item_id": item_id,
            "status": status,
            "note": _text(
                row.get("note", ""), field="checklist_evidence.note", limit=500,
                preserve_lines=True,
            ),
        })
    if result in {"completed", "recovered"}:
        evidence_by_id = {item["item_id"]: item["status"] for item in evidence}
        incomplete = [
            item["id"] for item in checklist
            if item["required"] and evidence_by_id.get(item["id"]) != "completed"
        ]
        if incomplete:
            raise LifeGraphError(
                "completed or recovered logs require completed evidence for: "
                + ", ".join(incomplete)
            )
    return evidence


def validate_habit_log_properties(
    value: object,
    *,
    habit_properties: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LifeGraphError("habit log properties must be an object")
    habit_id = _text(value.get("habit_id"), field="habit_id", limit=36, required=True)
    result = _token(value.get("result"), field="result")
    if result not in LOG_RESULTS:
        raise LifeGraphError(
            "result must be completed, partial, skipped, missed, or recovered"
        )
    logged_at = _datetime(value.get("logged_at"), field="logged_at", required=True)
    scheduled_for = _date(
        value.get("scheduled_for"), field="scheduled_for", required=True
    )
    quality_value = value.get("quality")
    quality = None if quality_value is None else _integer(
        quality_value, field="quality", minimum=0, maximum=100
    )
    if result in {"completed", "partial", "recovered"} and quality is None:
        raise LifeGraphError("completed, partial, and recovered logs require quality")
    friction = _integer(
        value.get("friction", 0), field="friction", minimum=0, maximum=100
    )
    duration = _integer(
        value.get("duration_minutes", 0),
        field="duration_minutes",
        minimum=0,
        maximum=1_440,
    )
    if result in {"completed", "recovered"} and duration < 1:
        raise LifeGraphError("completed and recovered logs require duration_minutes")
    if result in {"skipped", "missed"} and duration != 0:
        raise LifeGraphError("skipped and missed logs must use duration_minutes 0")
    failure_causes = _string_list(
        value.get("failure_causes"), field="failure_causes", max_items=20,
        item_limit=240,
    )
    if result in {"skipped", "missed"} and not failure_causes:
        raise LifeGraphError("skipped and missed logs require failure_causes")
    if result in {"completed", "recovered"} and failure_causes:
        raise LifeGraphError("completed and recovered logs must not contain failure_causes")
    source = _source(value.get("source"))
    recovery_id = _text(
        value.get("recovery_of_log_id", ""),
        field="recovery_of_log_id",
        limit=36,
    ) or None
    if result == "recovered" and recovery_id is None:
        raise LifeGraphError("recovered logs require recovery_of_log_id")
    if result != "recovered" and recovery_id is not None:
        raise LifeGraphError("recovery_of_log_id is only valid for recovered logs")
    raw_checklist_snapshot = value.get("checklist_snapshot")
    checklist_snapshot = (
        _checklist(raw_checklist_snapshot)
        if raw_checklist_snapshot is not None
        else _checklist(list(habit_properties.get("checklist") or []))
    )
    properties = {
        "habit_log_schema_version": HABIT_LOG_SCHEMA_VERSION,
        "habit_id": habit_id,
        # Preserve the definition used to judge this observation. Later habit
        # edits must not invalidate historical checklist evidence.
        "checklist_snapshot": checklist_snapshot,
        "result": result,
        "logged_at": _iso_datetime(logged_at, field="logged_at"),
        "scheduled_for": scheduled_for.isoformat(),
        "quality": quality,
        "friction": friction,
        "failure_causes": failure_causes,
        "duration_minutes": duration,
        "checklist_evidence": _checklist_evidence(
            value.get("checklist_evidence"),
            checklist=checklist_snapshot,
            result=result,
        ),
        "source": source,
        "recovery_of_log_id": recovery_id,
    }
    _bounded_json(properties, field="habit log properties")
    return properties


def _owned_habit_log(
    db, owner_id: str, entity_id: object, *, include_deleted: bool = False
) -> tuple[LifeEntity, LifeEntity]:
    try:
        entity = get_life_entity(
            db, owner_id=owner_id, entity_id=entity_id, include_deleted=include_deleted
        )
    except LifeGraphNotFound as exc:
        raise LifeGraphNotFound("Habit log not found") from exc
    raw = entity.properties or {}
    if entity.entity_type != HABIT_LOG_ENTITY_TYPE or not isinstance(raw, Mapping) or (
        raw.get("habit_log_schema_version") != HABIT_LOG_SCHEMA_VERSION
    ):
        raise LifeGraphNotFound("Habit log not found")
    habit = _owned_habit(
        db, owner_id, raw.get("habit_id"), include_deleted=True
    )
    validate_habit_log_properties(raw, habit_properties=habit.properties or {})
    return entity, habit


def _validate_recovery_reference(
    db,
    *,
    owner_id: str,
    habit_id: str,
    properties: Mapping[str, Any],
) -> None:
    recovery_id = properties.get("recovery_of_log_id")
    if not recovery_id:
        return
    recovered_log, _ = _owned_habit_log(db, owner_id, recovery_id)
    recovered_properties = validate_habit_log_properties(
        recovered_log.properties or {},
        habit_properties=_owned_habit(
            db, owner_id, habit_id, include_deleted=True
        ).properties or {},
    )
    if recovered_properties["habit_id"] != habit_id:
        raise LifeGraphNotFound("Recovery log source not found")
    if recovered_properties["result"] not in {"missed", "skipped"}:
        raise LifeGraphError("recovery_of_log_id must reference a missed or skipped log")
    if recovered_properties["scheduled_for"] != properties["scheduled_for"]:
        raise LifeGraphError("Recovered and missed logs must have the same scheduled_for date")


def _validate_scheduled_occurrence(
    habit: LifeEntity, *, scheduled_for: object
) -> None:
    scheduled = _date(scheduled_for, field="scheduled_for", required=True)
    assert scheduled is not None
    if not expected_routine_dates(
        habit, from_date=scheduled, to_date=scheduled
    ):
        raise LifeGraphError(
            "scheduled_for must be an occurrence in the habit schedule"
        )


def create_habit_log(
    db,
    *,
    account: Account,
    habit_id: object,
    result: object,
    logged_at: object,
    scheduled_for: object,
    quality: object | None = None,
    friction: object = 0,
    failure_causes: object | None = None,
    duration_minutes: object = 0,
    checklist_evidence: object | None = None,
    source: object | None = None,
    recovery_of_log_id: object | None = None,
    note: object = "",
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    habit = _owned_habit(db, account.id, habit_id)
    raw_properties = {
        "habit_id": habit.id,
        "result": result,
        "logged_at": logged_at,
        "scheduled_for": scheduled_for,
        "quality": quality,
        "friction": friction,
        "failure_causes": failure_causes,
        "duration_minutes": duration_minutes,
        "checklist_evidence": checklist_evidence,
        "source": source,
        "recovery_of_log_id": recovery_of_log_id,
    }
    raw_key = str(idempotency_key or "").strip()
    existing: LifeEntity | None = None
    if raw_key:
        protected_key = "sha256:" + hashlib.sha256(
            raw_key.encode("utf-8")
        ).hexdigest()
        existing = db.query(LifeEntity).filter(
            LifeEntity.owner_id == account.id,
            LifeEntity.idempotency_key == protected_key,
        ).first()
        if existing is not None:
            existing_properties = existing.properties or {}
            if not (
                existing.entity_type == HABIT_LOG_ENTITY_TYPE
                and isinstance(existing_properties, Mapping)
                and existing_properties.get("habit_log_schema_version")
                == HABIT_LOG_SCHEMA_VERSION
                and existing_properties.get("habit_id") == habit.id
            ):
                raise LifeGraphConflict(
                    "Habit log idempotency key was already used for different content"
                )
            # Derived checklist evidence remains anchored to the definition at
            # first capture, even if the habit is edited before a replay.
            raw_properties["checklist_snapshot"] = existing_properties.get(
                "checklist_snapshot"
            )
    properties = validate_habit_log_properties(
        raw_properties, habit_properties=habit.properties or {}
    )
    normalized_note = _text(
        note, field="note", limit=20_000, preserve_lines=True
    )
    normalized_provenance = _normalize_provenance(provenance)
    normalized_confidence = _integer(
        confidence, field="confidence", minimum=0, maximum=100
    )
    normalized_sensitivity = _token(sensitivity, field="sensitivity", limit=24)
    if normalized_sensitivity not in SENSITIVITIES:
        raise LifeGraphError("Habit log sensitivity must be private or restricted")
    normalized_logged_at = _datetime(
        logged_at, field="logged_at", required=True
    )
    if existing is not None:
        if not all((
            existing.deleted_at is None,
            dict(existing.properties or {}) == properties,
            existing.summary == normalized_note,
            dict(existing.provenance or {}) == normalized_provenance,
            int(existing.confidence or 0) == normalized_confidence,
            existing.sensitivity == normalized_sensitivity,
            existing.occurred_at == normalized_logged_at,
        )):
            raise LifeGraphConflict(
                "Habit log idempotency key was already used for different content"
            )
        return existing, False
    _validate_scheduled_occurrence(
        habit, scheduled_for=properties["scheduled_for"]
    )
    _validate_recovery_reference(
        db, owner_id=account.id, habit_id=habit.id, properties=properties
    )
    if properties["source"]["kind"] != "manual":
        if not str(idempotency_key or "").strip():
            raise LifeGraphError("Imported habit logs require idempotency_key")
    entity, created = create_life_entity(
        db,
        account=account,
        entity_type=HABIT_LOG_ENTITY_TYPE,
        title=f"{habit.title} — {properties['result']}",
        summary=normalized_note,
        status="active",
        properties=properties,
        provenance=normalized_provenance,
        confidence=normalized_confidence,
        sensitivity=normalized_sensitivity,
        occurred_at=normalized_logged_at,
        idempotency_key=idempotency_key,
        reason="Habit log captured",
    )
    return entity, created


def update_habit_log(
    db,
    *,
    account: Account,
    entity_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    entity, habit = _owned_habit_log(db, account.id, entity_id)
    allowed = {
        "result", "logged_at", "scheduled_for", "quality", "friction",
        "failure_causes", "duration_minutes", "checklist_evidence", "source",
        "recovery_of_log_id", "note", "provenance", "confidence", "sensitivity",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported habit log fields: {', '.join(unknown)}")
    current = validate_habit_log_properties(
        entity.properties or {}, habit_properties=habit.properties or {}
    )
    merged = dict(current)
    for field in (
        "result", "logged_at", "scheduled_for", "quality", "friction",
        "failure_causes", "duration_minutes", "checklist_evidence", "source",
        "recovery_of_log_id",
    ):
        if field in changes:
            merged[field] = changes[field]
    properties = validate_habit_log_properties(
        merged, habit_properties=habit.properties or {}
    )
    _validate_scheduled_occurrence(
        habit, scheduled_for=properties["scheduled_for"]
    )
    _validate_recovery_reference(
        db, owner_id=account.id, habit_id=habit.id, properties=properties
    )
    entity_changes: dict[str, Any] = {
        "properties": properties,
        "title": f"{habit.title} — {properties['result']}",
    }
    if "logged_at" in changes:
        entity_changes["occurred_at"] = _datetime(
            properties["logged_at"], field="logged_at", required=True
        )
    if "note" in changes:
        entity_changes["summary"] = _text(
            changes["note"], field="note", limit=20_000, preserve_lines=True
        )
    if "provenance" in changes:
        entity_changes["provenance"] = _normalize_provenance(changes["provenance"])
    if "confidence" in changes:
        entity_changes["confidence"] = changes["confidence"]
    if "sensitivity" in changes:
        sensitivity = _token(changes["sensitivity"], field="sensitivity", limit=24)
        if sensitivity not in SENSITIVITIES:
            raise LifeGraphError("Habit log sensitivity must be private or restricted")
        entity_changes["sensitivity"] = sensitivity
    return update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=entity_changes,
        reason="Habit log corrected",
    )


def delete_habit_log(
    db, *, owner_id: str, entity_id: object, expected_version: int, reason: object
) -> LifeEntity:
    entity, _ = _owned_habit_log(db, owner_id, entity_id)
    return delete_life_entity(
        db, owner_id=owner_id, entity_id=entity.id,
        expected_version=expected_version, reason=reason,
    )


def serialize_habit_log(entity: LifeEntity, *, habit: LifeEntity | None = None) -> dict[str, Any]:
    raw = entity.properties or {}
    if entity.entity_type != HABIT_LOG_ENTITY_TYPE or not isinstance(raw, Mapping):
        raise LifeGraphError("Entity is not a habit log")
    if habit is None:
        raise LifeGraphError("Habit authority is required to serialize a habit log")
    properties = validate_habit_log_properties(
        raw, habit_properties=habit.properties or {}
    )
    payload = serialize_life_entity(entity)
    payload.update({
        "habit_id": properties["habit_id"],
        "habit_title": habit.title,
        "result": properties["result"],
        "logged_at": properties["logged_at"],
        "scheduled_for": properties["scheduled_for"],
        "quality": properties["quality"],
        "friction": properties["friction"],
        "failure_causes": properties["failure_causes"],
        "duration_minutes": properties["duration_minutes"],
        "checklist_snapshot": properties["checklist_snapshot"],
        "checklist_evidence": properties["checklist_evidence"],
        "source": properties["source"],
        "recovery_of_log_id": properties["recovery_of_log_id"],
        "note": payload["summary"],
    })
    return payload


def get_habit_log(db, *, owner_id: str, entity_id: object) -> dict[str, Any]:
    entity, habit = _owned_habit_log(db, owner_id, entity_id)
    return serialize_habit_log(entity, habit=habit)


def _habit_log_candidates(
    db,
    *,
    owner_id: str,
    habit_id: object | None = None,
    result: object | None = None,
    from_date: object | None = None,
    to_date: object | None = None,
) -> tuple[list[tuple[LifeEntity, LifeEntity]], bool]:
    normalized_result = None
    if result:
        normalized_result = _token(result, field="result")
        if normalized_result not in LOG_RESULTS:
            raise LifeGraphError("Unsupported habit log result")
    normalized_habit_id = str(habit_id or "").strip() or None
    if normalized_habit_id:
        _owned_habit(db, owner_id, normalized_habit_id, include_deleted=True)
    start = _date(from_date, field="from_date")
    end = _date(to_date, field="to_date")
    if start and end and start > end:
        raise LifeGraphError("from_date must not be after to_date")
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == HABIT_LOG_ENTITY_TYPE,
        LifeEntity.deleted_at.is_(None),
    )
    rows = query.order_by(
        LifeEntity.occurred_at.desc(), LifeEntity.id.desc()
    ).limit(HABIT_LOG_SCAN_LIMIT + 1).all()
    truncated = len(rows) > HABIT_LOG_SCAN_LIMIT
    habits: dict[str, LifeEntity] = {}
    result_rows: list[tuple[LifeEntity, LifeEntity]] = []
    for row in rows[:HABIT_LOG_SCAN_LIMIT]:
        raw = row.properties or {}
        if not isinstance(raw, Mapping) or raw.get("habit_log_schema_version") != 1:
            continue
        row_habit_id = str(raw.get("habit_id") or "")
        if normalized_habit_id and row_habit_id != normalized_habit_id:
            continue
        try:
            habit = habits.get(row_habit_id)
            if habit is None:
                habit = _owned_habit(
                    db, owner_id, row_habit_id, include_deleted=True
                )
                habits[row_habit_id] = habit
            properties = validate_habit_log_properties(
                raw, habit_properties=habit.properties or {}
            )
        except LifeGraphError:
            continue
        scheduled = _date(
            properties["scheduled_for"], field="scheduled_for", required=True
        )
        if normalized_result and properties["result"] != normalized_result:
            continue
        if start and scheduled < start:
            continue
        if end and scheduled > end:
            continue
        result_rows.append((row, habit))
    return result_rows, truncated


def list_habit_logs(
    db,
    *,
    owner_id: str,
    habit_id: object | None = None,
    result: object | None = None,
    from_date: object | None = None,
    to_date: object | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    bounded = max(1, min(100, int(limit)))
    rows, truncated = _habit_log_candidates(
        db, owner_id=owner_id, habit_id=habit_id, result=result,
        from_date=from_date, to_date=to_date,
    )
    return [
        serialize_habit_log(entity, habit=habit)
        for entity, habit in rows[:bounded]
    ], truncated or len(rows) > bounded


def search_habit_logs(
    db,
    *,
    owner_id: str,
    query_text: object,
    habit_id: object | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    needle = str(query_text or "").strip().casefold()
    if not needle:
        raise LifeGraphError("q is required")
    bounded = max(1, min(100, int(limit)))
    rows, truncated = _habit_log_candidates(
        db, owner_id=owner_id, habit_id=habit_id
    )
    matches: list[tuple[tuple[int, str], dict[str, Any]]] = []
    for entity, habit in rows:
        log = serialize_habit_log(entity, habit=habit)
        title = str(log["habit_title"]).casefold()
        note = str(log["note"]).casefold()
        body = json.dumps({
            "result": log["result"],
            "failure_causes": log["failure_causes"],
            "checklist_evidence": log["checklist_evidence"],
            "source": log["source"],
            "provenance": log["provenance"],
        }, ensure_ascii=False, sort_keys=True).casefold()
        if needle in title:
            rank, field = 0, "habit_title"
        elif needle in note:
            rank, field = 1, "note"
        elif needle in body:
            rank, field = 2, "properties"
        else:
            continue
        matches.append(((rank, entity.id), {"log": log, "match": field, "rank": rank}))
    matches.sort(key=lambda row: row[0])
    items = [item for _, item in matches[:bounded]]
    return {
        "items": items, "count": len(items), "scanned": len(rows),
        "truncated": truncated or len(matches) > bounded,
    }


def _history(
    db,
    *,
    owner_id: str,
    entity_id: object,
    kind: str,
    validator,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    rows, truncated = list_life_entity_versions(
        db, owner_id=owner_id, entity_id=entity_id, limit=limit
    )
    chronological = list(reversed(rows))
    previous: Mapping[str, Any] | None = None
    result: list[dict[str, Any]] = []
    for row in chronological:
        snapshot = dict(row.snapshot or {})
        properties = validator(snapshot.get("properties") or {})
        changed: list[str] = []
        if previous is not None:
            previous_properties = validator(previous.get("properties") or {})
            for field in (
                "title", "summary", "status", "occurred_at", "confidence",
                "sensitivity", "provenance",
            ):
                if previous.get(field) != snapshot.get(field):
                    changed.append(field)
            for field in properties:
                if previous_properties.get(field) != properties.get(field):
                    changed.append(field)
        result.append({
            "id": row.id,
            "version": int(row.version),
            "created_at": row.created_at.replace(tzinfo=timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "reason": row.reason or "",
            "kind": "created" if previous is None else f"{kind}_changed",
            "changed_fields": changed,
            "snapshot": {
                "title": snapshot.get("title") or "",
                "status": snapshot.get("status") or "active",
                "properties": properties,
            },
        })
        previous = snapshot
    return list(reversed(result)), truncated


def habit_history(
    db, *, owner_id: str, entity_id: object, limit: int = 50
) -> tuple[list[dict[str, Any]], bool]:
    entity = _owned_habit(db, owner_id, entity_id, include_deleted=True)
    return _history(
        db, owner_id=owner_id, entity_id=entity.id, kind="habit",
        validator=validate_habit_properties, limit=limit,
    )


def habit_log_history(
    db, *, owner_id: str, entity_id: object, limit: int = 50
) -> tuple[list[dict[str, Any]], bool]:
    entity, habit = _owned_habit_log(
        db, owner_id, entity_id, include_deleted=True
    )

    def validator(value: object) -> dict[str, Any]:
        return validate_habit_log_properties(
            value, habit_properties=habit.properties or {}
        )

    return _history(
        db, owner_id=owner_id, entity_id=entity.id, kind="habit_log",
        validator=validator, limit=limit,
    )


def _report_range(from_date: object, to_date: object) -> tuple[date, date]:
    start = _date(from_date, field="from_date", required=True)
    end = _date(to_date, field="to_date", required=True)
    assert start is not None and end is not None
    if start > end:
        raise LifeGraphError("from_date must not be after to_date")
    if (end - start).days + 1 > MAX_REPORT_DAYS:
        raise LifeGraphError(f"Reports are limited to {MAX_REPORT_DAYS} days")
    return start, end


def expected_routine_dates(
    habit: LifeEntity, *, from_date: object, to_date: object
) -> list[date]:
    properties = validate_habit_properties(habit.properties or {})
    start, end = _report_range(from_date, to_date)
    schedule = properties["schedule"]
    schedule_start = _date(
        schedule["start_date"], field="schedule.start_date", required=True
    )
    schedule_end = _date(schedule.get("end_date"), field="schedule.end_date")
    assert schedule_start is not None
    start = max(start, schedule_start)
    if schedule_end is not None:
        end = min(end, schedule_end)
    if start > end:
        return []
    days: list[date] = []
    current = start
    cadence = schedule["cadence"]
    while current <= end:
        included = False
        if cadence == "daily":
            included = True
        elif cadence == "weekdays":
            included = current.weekday() < 5
        elif cadence in {"weekly", "custom"}:
            included = current.weekday() in schedule["days_of_week"]
        elif cadence == "interval":
            included = (current - schedule_start).days % schedule["interval_days"] == 0
        if included:
            days.append(current)
        current += timedelta(days=1)
    return days


def _analytics_for_habit(
    db,
    *,
    owner_id: str,
    habit: LifeEntity,
    from_date: date,
    to_date: date,
) -> dict[str, Any]:
    expected = expected_routine_dates(habit, from_date=from_date, to_date=to_date)
    rows, truncated = _habit_log_candidates(
        db, owner_id=owner_id, habit_id=habit.id,
        from_date=from_date, to_date=to_date,
    )
    by_date: dict[date, list[dict[str, Any]]] = {}
    for entity, row_habit in rows:
        log = serialize_habit_log(entity, habit=row_habit)
        scheduled = _date(log["scheduled_for"], field="scheduled_for", required=True)
        assert scheduled is not None
        by_date.setdefault(scheduled, []).append(log)
    strongest: dict[date, dict[str, Any]] = {}
    for scheduled, logs in by_date.items():
        strongest[scheduled] = sorted(
            logs,
            key=lambda row: (
                -_RESULT_PRIORITY[row["result"]],
                str(row["logged_at"]),
                str(row["id"]),
            ),
        )[0]
    counts = Counter()
    credit = 0.0
    qualities: list[int] = []
    frictions: list[int] = []
    failure_causes: Counter[str] = Counter()
    for scheduled in expected:
        log = strongest.get(scheduled)
        if log is None:
            counts["missing"] += 1
            continue
        result = log["result"]
        counts[result] += 1
        if result in {"completed", "recovered"}:
            credit += 1.0
        elif result == "partial":
            credit += 0.5
        if log["quality"] is not None:
            qualities.append(int(log["quality"]))
        frictions.append(int(log["friction"]))
        failure_causes.update(log["failure_causes"])
    current_streak = 0
    for scheduled in reversed(expected):
        result = strongest.get(scheduled, {}).get("result")
        if result not in {"completed", "recovered"}:
            break
        current_streak += 1
    longest_streak = 0
    running = 0
    for scheduled in expected:
        result = strongest.get(scheduled, {}).get("result")
        if result in {"completed", "recovered"}:
            running += 1
            longest_streak = max(longest_streak, running)
        else:
            running = 0
    missed_outstanding = counts["missing"] + counts["missed"] + counts["skipped"]
    recovered = counts["recovered"]
    recovery_opportunities = missed_outstanding + recovered
    return {
        "habit_id": habit.id,
        "title": habit.title,
        "routine_type": validate_habit_properties(habit.properties or {})["routine_type"],
        "expected_count": len(expected),
        "completed_count": counts["completed"],
        "partial_count": counts["partial"],
        "skipped_count": counts["skipped"],
        "missed_count": counts["missed"] + counts["missing"],
        "recovered_count": recovered,
        "consistency_percent": round(100.0 * credit / len(expected), 1) if expected else 0.0,
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "recovery_percent": (
            round(100.0 * recovered / recovery_opportunities, 1)
            if recovery_opportunities else 0.0
        ),
        "average_quality": round(sum(qualities) / len(qualities), 1) if qualities else None,
        "average_friction": round(sum(frictions) / len(frictions), 1) if frictions else None,
        "failure_causes": [
            {"cause": cause, "count": count}
            for cause, count in sorted(
                failure_causes.items(), key=lambda row: (-row[1], row[0].casefold())
            )
        ],
        "scan_truncated": truncated,
    }


def weekly_habit_report(
    db,
    *,
    owner_id: str,
    week_start: object,
    habit_id: object | None = None,
) -> dict[str, Any]:
    start = _date(week_start, field="week_start", required=True)
    assert start is not None
    end = start + timedelta(days=6)
    if habit_id:
        habits = [_owned_habit(db, owner_id, habit_id)]
        truncated = False
    else:
        habits, truncated = _habit_candidates(db, owner_id=owner_id, status="active")
    items = [
        _analytics_for_habit(
            db, owner_id=owner_id, habit=habit,
            from_date=start, to_date=end,
        )
        for habit in sorted(habits, key=lambda row: (row.title.casefold(), row.id))
    ]
    expected = sum(item["expected_count"] for item in items)
    weighted = sum(
        item["consistency_percent"] * item["expected_count"] for item in items
    )
    return {
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "items": items,
        "count": len(items),
        "overall_consistency_percent": round(weighted / expected, 1) if expected else 0.0,
        "truncated": truncated or any(item["scan_truncated"] for item in items),
        "scoring_policy": {
            "completed": 1.0,
            "recovered": 1.0,
            "partial": 0.5,
            "skipped": 0.0,
            "missed_or_missing": 0.0,
            "streak_success_states": ["completed", "recovered"],
            "recovery_denominator": "recovered plus outstanding missed, skipped, or missing",
        },
    }


def missed_routine_report(
    db,
    *,
    owner_id: str,
    as_of: object,
    lookback_days: int = 14,
    habit_id: object | None = None,
) -> dict[str, Any]:
    moment = _aware_datetime(as_of, field="as_of")
    lookback = _integer(
        lookback_days, field="lookback_days", minimum=1, maximum=MAX_REPORT_DAYS
    )
    if habit_id:
        habits = [_owned_habit(db, owner_id, habit_id)]
        truncated = False
    else:
        habits, truncated = _habit_candidates(db, owner_id=owner_id, status="active")
    items: list[dict[str, Any]] = []
    for habit in habits:
        properties = validate_habit_properties(habit.properties or {})
        schedule = properties["schedule"]
        schedule_zone = ZoneInfo(schedule["timezone"])
        local_moment = moment.astimezone(schedule_zone)
        start = local_moment.date() - timedelta(days=lookback - 1)
        expected = expected_routine_dates(
            habit, from_date=start, to_date=local_moment.date()
        )
        if expected and expected[-1] == local_moment.date():
            at = schedule.get("time_of_day")
            if at:
                hour, minute = (int(part) for part in at.split(":"))
                due = datetime.combine(
                    local_moment.date(), time(hour, minute), tzinfo=schedule_zone
                ) + timedelta(minutes=schedule["grace_minutes"])
                if local_moment < due:
                    expected.pop()
            else:
                # A date-only routine is not declared missed until the day ends.
                expected.pop()
        rows, scan_truncated = _habit_log_candidates(
            db, owner_id=owner_id, habit_id=habit.id,
            from_date=start, to_date=local_moment.date(),
        )
        truncated = truncated or scan_truncated
        by_date: dict[date, list[dict[str, Any]]] = {}
        for entity, row_habit in rows:
            log = serialize_habit_log(entity, habit=row_habit)
            scheduled = _date(
                log["scheduled_for"], field="scheduled_for", required=True
            )
            assert scheduled is not None
            by_date.setdefault(scheduled, []).append(log)
        for scheduled in expected:
            logs = by_date.get(scheduled, [])
            strongest = max(
                logs, key=lambda row: _RESULT_PRIORITY[row["result"]], default=None
            )
            if strongest and strongest["result"] in {"completed", "recovered", "partial"}:
                continue
            items.append({
                "habit_id": habit.id,
                "title": habit.title,
                "scheduled_for": scheduled.isoformat(),
                "state": strongest["result"] if strongest else "missing_log",
                "failure_causes": strongest["failure_causes"] if strongest else [],
                "recovery_rules": properties["recovery_rules"],
            })
    items.sort(key=lambda row: (row["scheduled_for"], row["title"].casefold(), row["habit_id"]))
    return {
        "as_of": moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "lookback_days": lookback,
        "items": items,
        "count": len(items),
        "truncated": truncated,
        "time_basis": "aware as_of converted to each habit's IANA schedule timezone",
    }


def weekly_adjustment_report(
    db,
    *,
    owner_id: str,
    week_start: object,
    habit_id: object | None = None,
) -> dict[str, Any]:
    report = weekly_habit_report(
        db, owner_id=owner_id, week_start=week_start, habit_id=habit_id
    )
    items: list[dict[str, Any]] = []
    for stats in report["items"]:
        adjustments: list[dict[str, str]] = []
        consistency = stats["consistency_percent"]
        friction = stats["average_friction"]
        if consistency >= 85 and (friction is None or friction <= 40):
            adjustments.append({
                "type": "maintain",
                "reason": "Consistency is at least 85% with manageable recorded friction.",
            })
        if consistency < 50:
            adjustments.append({
                "type": "shrink_minimum_viable",
                "reason": "Consistency is below 50%; review a smaller minimum viable version.",
            })
        if friction is not None and friction >= 65:
            adjustments.append({
                "type": "reduce_friction",
                "reason": "Average recorded friction is at least 65/100.",
            })
        if stats["partial_count"] >= 2:
            adjustments.append({
                "type": "simplify_checklist",
                "reason": "At least two scheduled routines were only partially completed.",
            })
        if stats["missed_count"] > 0 and stats["recovered_count"] == 0:
            adjustments.append({
                "type": "plan_recovery",
                "reason": "There were missed routines and no recorded recovery.",
            })
        if not adjustments:
            adjustments.append({
                "type": "review_evidence",
                "reason": (
                    "The bounded weekly evidence does not trigger another "
                    "deterministic rule."
                ),
            })
        items.append({
            "habit_id": stats["habit_id"],
            "title": stats["title"],
            "evidence": {
                "expected_count": stats["expected_count"],
                "consistency_percent": consistency,
                "average_friction": friction,
                "partial_count": stats["partial_count"],
                "missed_count": stats["missed_count"],
                "recovered_count": stats["recovered_count"],
            },
            "adjustments": adjustments,
        })
    return {
        "week_start": report["week_start"],
        "week_end": report["week_end"],
        "items": items,
        "count": len(items),
        "truncated": report["truncated"],
        "execution_policy": {
            "record_only": True,
            "can_apply_automatically": False,
        },
    }
