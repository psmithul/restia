"""Bounded, principal-scoped automation definitions for Restia V3.

This module is deliberately split into two phases:

* :func:`evaluate_automation` is a deterministic, read-only classifier.  It
  never commits, executes a connector, invokes a model, or writes model output.
* :func:`prepare_automation_run` persists an encrypted preparation record and
  creates canonical :class:`ActionProposal` rows only for approved-send or
  notification actions.  It still performs no external action.

Actual reversible mutations remain the responsibility of their typed domain
services.  External sends remain behind Restia's existing fresh-human approval
boundary.  WhatsApp is intentionally absent from the send-capable channel set.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from core.database import (
    LIFE_ENTITY_TYPES,
    Account,
    LifeEntity,
    LifeSource,
)
from src.action_policy import (
    ActionPolicyConflict,
    ActionPolicyDenied,
    create_action_proposal,
    get_effective_action_policy,
)
from src.email_outbound import EmailOutboundError, prepare_agent_email_action
from src.life_graph import (
    LifeGraphConflict,
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


AUTOMATION_SCHEMA_VERSION = 1
AUTOMATION_ENTITY_TYPE = "automation"
AUTOMATION_RUN_ENTITY_TYPE = "action"
AUTOMATION_DEFINITION_KIND = "automation_definition"
AUTOMATION_RUN_KIND = "automation_preparation"
MAX_ACTIONS = 32
MAX_SOURCE_IDS = 64
MAX_JSON_BYTES = 64 * 1024

TRIGGER_TYPES = frozenset({
    "time",
    "email",
    "calendar",
    "overdue_task",
    "upload",
    "person",
    "metric_threshold",
    "location",
    "form",
    "project_status",
})

ACTION_TYPES = frozenset({
    "create_entity",
    "schedule",
    "database_update",
    "draft",
    "approved_send",
    "briefing",
    "file_move",
    "report",
    "information_request",
    "notification",
    "agent",
    "workflow",
})

DRAFT_CHANNELS = frozenset({
    "document", "email", "restia_message", "telegram", "whatsapp",
})
# WhatsApp stays read-only.  Adding it here would create a generic send path.
SEND_CHANNELS = frozenset({"email", "restia_message", "telegram"})
NOTIFICATION_CHANNELS = frozenset({"email", "push", "telegram", "web"})

EXECUTION_CONTRACT = {
    "evaluation_side_effects": False,
    "classification_external_actions": False,
    "model_write_authority": False,
    "whatsapp_send": False,
    "prepared_domain_actions_require_typed_executor": True,
}

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_CRON_RE = re.compile(r"^[0-9*/?,\- ]{9,96}$")
_URL_CREDENTIAL_RE = re.compile(
    r"[a-z][a-z0-9+.-]*://[^/@\s]+:[^/@\s]+@", re.IGNORECASE
)
_BEARER_RE = re.compile(r"\bauthorization\s*:\s*bearer\s+\S+", re.IGNORECASE)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

_SECRET_KEY_PARTS = (
    "password", "passwd", "passcode", "secret", "token", "credential", "cookie",
    "authorization", "api_key", "private_key", "seed_phrase",
    "recovery_phrase", "access_token", "refresh_token", "bearer_token",
)
_EXECUTOR_KEY_PARTS = (
    "shell", "command", "script", "raw_sql", "sql_query", "webhook",
    "callback_url", "http_request", "tool_call", "function_call",
    "executor", "external_action", "dispatch_payload", "request_headers",
    "model_output", "llm_output", "write_model_output", "raw_prompt",
)

_CALENDAR_EVENTS = frozenset({
    "created", "updated", "cancelled", "started", "meeting_ended",
})
_PERSON_EVENTS = frozenset({
    "created", "updated", "follow_up_due", "birthday", "relationship_risk",
})
_LOCATION_EVENTS = frozenset({"enter", "exit"})
_METRIC_OPERATORS = frozenset({"gt", "gte", "lt", "lte", "eq"})
_SCHEDULE_KINDS = frozenset({"once", "daily", "weekly", "monthly", "cron"})
_REPORT_KINDS = frozenset({
    "daily", "weekly", "project", "finance", "health", "custom",
})
_REPORT_FORMATS = frozenset({"csv", "json", "markdown", "pdf"})
_AGENT_ROLES = frozenset({"analysis", "planning", "research", "summarization"})
_AGENT_OUTPUTS = frozenset({"briefing", "draft", "report"})
_WORKFLOW_TYPES = frozenset({
    "daily_brief", "intake_triage", "meeting_end_followup",
})

_ENTITY_DOMAINS = {
    "event": "calendar",
    "file": "files",
    "health_record": "health",
    "message": "communications",
    "note": "notes",
    "project": "projects",
    "task": "tasks",
    "transaction": "finance",
}
_CRITICAL_DOMAINS = frozenset({
    "banking", "destructive", "finance", "financial", "healthcare",
    "legal", "medical", "money", "payments", "permissions", "security",
})
_ACTION_FLOORS = {
    "create_entity": 4,
    "schedule": 4,
    "database_update": 4,
    "draft": 3,
    "approved_send": 5,
    "briefing": 3,
    "file_move": 4,
    "report": 3,
    "information_request": 3,
    "notification": 5,
    "agent": 3,
    "workflow": 3,
}


class AutomationError(LifeGraphError):
    """Base class for controlled automation failures."""


class AutomationNotFound(AutomationError):
    pass


class AutomationConflict(AutomationError):
    pass


class AutomationPolicyDenied(AutomationError):
    pass


@dataclass(frozen=True)
class AutomationEvaluation:
    automation_id: str
    automation_version: int
    matched: bool
    trigger_type: str
    event_fingerprint: str
    event_summary: dict[str, Any]
    source_ids: tuple[str, ...]
    plans: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AutomationPreparation:
    evaluation: AutomationEvaluation
    run_entity: LifeEntity | None
    created: bool
    proposal_ids: tuple[str, ...]
    confirmation_tokens: dict[str, str]


def _compact_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())


def _forbidden_key(value: object) -> bool:
    key = str(value or "").strip().lower().replace("-", "_")
    compact = _compact_key(value)
    if any(_compact_key(part) in compact for part in _SECRET_KEY_PARTS):
        return True
    executor_compact = {_compact_key(part) for part in _EXECUTOR_KEY_PARTS}
    if compact in executor_compact:
        return True
    if key.startswith((
        "execute_", "executor_", "shell_", "command_", "script_",
        "webhook_", "callback_", "http_request_", "tool_call_",
        "function_call_", "dispatch_", "model_output_", "llm_output_",
    )):
        return True
    # Compound spellings such as toolCall or callbackUrl still fail closed,
    # without treating ordinary keys such as ``description`` as a script.
    return any(marker in compact for marker in (
        "executor", "externalaction", "toolcall", "functioncall",
        "modeloutput", "llmoutput", "callbackurl", "httprequest",
        "rawsql", "sqlquery",
    ))


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
        raise AutomationError(f"{field} is required")
    if len(normalized) > limit:
        raise AutomationError(f"{field} must not exceed {limit} characters")
    if (
        _URL_CREDENTIAL_RE.search(normalized)
        or _BEARER_RE.search(normalized)
        or _PRIVATE_KEY_RE.search(normalized)
    ):
        raise AutomationError(f"{field} must not contain credentials or secrets")
    return normalized


def _token(value: object, *, field: str, limit: int = 64) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    if not normalized or len(normalized) > limit or not _TOKEN_RE.fullmatch(normalized):
        raise AutomationError(
            f"{field} must be a lowercase token using letters, numbers, _, -, or ."
        )
    return normalized


def _bounded_object(value: object | None, *, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise AutomationError(f"{field} must be an object")
    result = {str(key): child for key, child in value.items()}
    _assert_safe_tree(result, field=field)
    try:
        encoded = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise AutomationError(f"{field} must be JSON-serializable") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise AutomationError(f"{field} must not exceed {MAX_JSON_BYTES} bytes")
    return result


def _assert_safe_tree(value: object, *, field: str) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            if _forbidden_key(raw_key):
                raise AutomationError(
                    f"{field} must not contain credentials, executors, raw model output, "
                    "or transport callbacks"
                )
            _assert_safe_tree(child, field=field)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_safe_tree(child, field=field)
    elif isinstance(value, str):
        _text(value, field=field, limit=20_000, preserve_lines=True)


def _strict_fields(
    value: Mapping[str, Any], *, field: str, allowed: set[str], required: set[str]
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise AutomationError(f"Unsupported {field} fields: {', '.join(unknown)}")
    missing = sorted(name for name in required if value.get(name) in (None, ""))
    if missing:
        raise AutomationError(f"Missing {field} fields: {', '.join(missing)}")


def _naive_utc(value: object | None, *, field: str) -> datetime | None:
    if value in (None, ""):
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
            raise AutomationError(f"{field} must be an ISO-8601 datetime") from exc
    else:
        raise AutomationError(f"{field} must be an ISO-8601 datetime")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value
    if normalized.tzinfo is not None:
        normalized = normalized.astimezone(timezone.utc).replace(tzinfo=None)
    return normalized.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value: object, *, field: str) -> int | float:
    if isinstance(value, bool):
        raise AutomationError(f"{field} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AutomationError(f"{field} must be a finite number") from exc
    if not number.is_finite():
        raise AutomationError(f"{field} must be a finite number")
    if number == number.to_integral_value():
        return int(number)
    return float(number)


def _positive_int(value: object, *, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise AutomationError(f"{field} must be an integer >= {minimum}")
    return value


def _string_list(
    value: object | None,
    *,
    field: str,
    limit: int = 32,
    item_limit: int = 255,
    required: bool = False,
) -> list[str]:
    if value is None:
        values: Sequence[object] = ()
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        raise AutomationError(f"{field} must be a list")
    if len(values) > limit:
        raise AutomationError(f"{field} must contain at most {limit} items")
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        clean = _text(item, field=field, limit=item_limit, required=True)
        if clean not in seen:
            seen.add(clean)
            result.append(clean)
    if required and not result:
        raise AutomationError(f"{field} must contain at least one item")
    return result


def _owned_source_ids(
    db, *, owner_id: str, value: object | None, required: bool = False
) -> list[str]:
    source_ids = _string_list(
        value, field="source_ids", limit=MAX_SOURCE_IDS, item_limit=36,
        required=required,
    )
    if not source_ids:
        return []
    rows = db.query(LifeSource.id).filter(
        LifeSource.owner_id == owner_id,
        LifeSource.id.in_(source_ids),
    ).all()
    if {str(row[0]) for row in rows} != set(source_ids):
        raise AutomationNotFound("One or more sources were not found")
    return sorted(source_ids)


def _owned_entity(db, *, owner_id: str, entity_id: object) -> LifeEntity:
    try:
        return get_life_entity(db, owner_id=owner_id, entity_id=entity_id)
    except LifeGraphNotFound as exc:
        raise AutomationNotFound("Referenced entity was not found") from exc


def _entity_domain(entity_type: str) -> str:
    return _ENTITY_DOMAINS.get(entity_type, "planning")


def _normalize_trigger(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AutomationError("trigger must be an object")
    _strict_fields(raw, field="trigger", allowed={"type", "config"}, required={"type"})
    trigger_type = _token(raw.get("type"), field="trigger.type", limit=32)
    if trigger_type not in TRIGGER_TYPES:
        raise AutomationError("Unknown automation trigger type")
    config = _bounded_object(raw.get("config") or {}, field="trigger.config")

    if trigger_type == "time":
        _strict_fields(
            config, field="time trigger", allowed={"schedule_key", "timezone"},
            required={"schedule_key"},
        )
        normalized = {
            "schedule_key": _token(config["schedule_key"], field="schedule_key"),
        }
        if config.get("timezone"):
            normalized["timezone"] = _text(
                config["timezone"], field="timezone", limit=64, required=True
            )
    elif trigger_type == "email":
        _strict_fields(
            config,
            field="email trigger",
            allowed={"account_id", "from_email", "from_domain", "subject_contains", "tags_any"},
            required=set(),
        )
        if not any(config.get(key) for key in config):
            raise AutomationError("email trigger requires at least one condition")
        normalized = {}
        for key in ("account_id", "subject_contains"):
            if config.get(key):
                normalized[key] = _text(config[key], field=key, limit=255, required=True)
        if config.get("from_email"):
            sender = _text(config["from_email"], field="from_email", limit=320, required=True).lower()
            if not _EMAIL_RE.fullmatch(sender):
                raise AutomationError("from_email must be an email address")
            normalized["from_email"] = sender
        if config.get("from_domain"):
            normalized["from_domain"] = _text(
                config["from_domain"], field="from_domain", limit=253, required=True
            ).lower().lstrip("@")
        if config.get("tags_any") is not None:
            normalized["tags_any"] = sorted(
                _token(tag, field="tags_any item")
                for tag in _string_list(config["tags_any"], field="tags_any", required=True)
            )
    elif trigger_type == "calendar":
        _strict_fields(
            config,
            field="calendar trigger",
            allowed={"event", "calendar_id", "event_id", "title_contains"},
            required={"event"},
        )
        event = _token(config["event"], field="calendar event")
        if event not in _CALENDAR_EVENTS:
            raise AutomationError("Unknown calendar trigger event")
        normalized = {"event": event}
        for key in ("calendar_id", "event_id", "title_contains"):
            if config.get(key):
                normalized[key] = _text(config[key], field=key, limit=255, required=True)
    elif trigger_type == "overdue_task":
        _strict_fields(
            config,
            field="overdue task trigger",
            allowed={"minimum_overdue_minutes", "project_id"},
            required=set(),
        )
        normalized = {
            "minimum_overdue_minutes": _positive_int(
                config.get("minimum_overdue_minutes", 0),
                field="minimum_overdue_minutes",
            )
        }
        if config.get("project_id"):
            normalized["project_id"] = _text(
                config["project_id"], field="project_id", limit=255, required=True
            )
    elif trigger_type == "upload":
        _strict_fields(
            config,
            field="upload trigger",
            allowed={"extension", "mime_prefix", "source"},
            required=set(),
        )
        if not any(config.get(key) for key in config):
            raise AutomationError("upload trigger requires at least one condition")
        normalized = {}
        for key in ("extension", "mime_prefix", "source"):
            if config.get(key):
                normalized[key] = _text(config[key], field=key, limit=255, required=True).lower()
        if "extension" in normalized:
            normalized["extension"] = normalized["extension"].lstrip(".")
    elif trigger_type == "person":
        _strict_fields(
            config,
            field="person trigger",
            allowed={"event", "person_id"},
            required={"event"},
        )
        event = _token(config["event"], field="person event")
        if event not in _PERSON_EVENTS:
            raise AutomationError("Unknown person trigger event")
        normalized = {"event": event}
        if config.get("person_id"):
            normalized["person_id"] = _text(
                config["person_id"], field="person_id", limit=255, required=True
            )
    elif trigger_type == "metric_threshold":
        _strict_fields(
            config,
            field="metric trigger",
            allowed={"metric", "operator", "threshold"},
            required={"metric", "operator", "threshold"},
        )
        operator = _token(config["operator"], field="metric operator")
        if operator not in _METRIC_OPERATORS:
            raise AutomationError("Unknown metric threshold operator")
        normalized = {
            "metric": _token(config["metric"], field="metric", limit=64),
            "operator": operator,
            "threshold": _number(config["threshold"], field="threshold"),
        }
    elif trigger_type == "location":
        _strict_fields(
            config,
            field="location trigger",
            allowed={"event", "place_id"},
            required={"event", "place_id"},
        )
        event = _token(config["event"], field="location event")
        if event not in _LOCATION_EVENTS:
            raise AutomationError("Unknown location trigger event")
        normalized = {
            "event": event,
            "place_id": _text(config["place_id"], field="place_id", limit=255, required=True),
        }
    elif trigger_type == "form":
        _strict_fields(
            config,
            field="form trigger",
            allowed={"form_id", "submission_type"},
            required={"form_id"},
        )
        normalized = {
            "form_id": _text(config["form_id"], field="form_id", limit=255, required=True),
        }
        if config.get("submission_type"):
            normalized["submission_type"] = _token(
                config["submission_type"], field="submission_type"
            )
    else:  # project_status
        _strict_fields(
            config,
            field="project status trigger",
            allowed={"from_status", "project_id", "to_status"},
            required={"to_status"},
        )
        normalized = {
            "to_status": _token(config["to_status"], field="to_status"),
        }
        for key in ("from_status", "project_id"):
            if config.get(key):
                normalizer = _token if key == "from_status" else _text
                if key == "from_status":
                    normalized[key] = normalizer(config[key], field=key)
                else:
                    normalized[key] = normalizer(
                        config[key], field=key, limit=255, required=True
                    )
    return {"type": trigger_type, "config": normalized}


def _normalize_event(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AutomationError("event must be an object")
    event = _bounded_object(raw, field="event")
    event_type = _token(event.get("type"), field="event.type", limit=32)
    if event_type not in TRIGGER_TYPES:
        raise AutomationError("Unknown automation event type")
    common = {"type", "source_ids", "occurred_at"}
    type_fields = {
        "time": {"schedule_key"},
        "email": {"account_id", "from_email", "message_id", "subject", "tags"},
        "calendar": {"calendar_id", "ended_at", "event", "event_id", "title"},
        "overdue_task": {"overdue_minutes", "project_id", "task_id"},
        "upload": {"file_id", "filename", "mime", "source"},
        "person": {"event", "person_id"},
        "metric_threshold": {"metric", "value"},
        "location": {"event", "place_id"},
        "form": {"fields", "form_id", "submission_id", "submission_type"},
        "project_status": {"from_status", "project_id", "to_status"},
    }[event_type]
    unknown = sorted(set(event) - common - type_fields)
    if unknown:
        raise AutomationError(f"Unsupported {event_type} event fields: {', '.join(unknown)}")
    normalized: dict[str, Any] = {"type": event_type}
    if event.get("source_ids") is not None:
        normalized["source_ids"] = _string_list(
            event["source_ids"], field="event.source_ids", limit=MAX_SOURCE_IDS,
            item_limit=36,
        )
    occurred = _naive_utc(event.get("occurred_at"), field="event.occurred_at")
    if occurred is not None:
        normalized["occurred_at"] = _iso(occurred)

    required_by_type = {
        "time": {"schedule_key"},
        "email": {"message_id"},
        "calendar": {"event", "event_id"},
        "overdue_task": {"overdue_minutes", "task_id"},
        "upload": {"file_id", "filename", "mime"},
        "person": {"event", "person_id"},
        "metric_threshold": {"metric", "value"},
        "location": {"event", "place_id"},
        "form": {"form_id", "submission_id"},
        "project_status": {"project_id", "to_status"},
    }[event_type]
    missing = sorted(name for name in required_by_type if event.get(name) in (None, ""))
    if missing:
        raise AutomationError(f"Missing {event_type} event fields: {', '.join(missing)}")

    for key in sorted(type_fields):
        if key not in event or event[key] in (None, ""):
            continue
        value = event[key]
        if key == "fields":
            normalized[key] = _bounded_object(value, field="event.fields")
        elif key == "tags":
            normalized[key] = sorted(
                _token(tag, field="event.tags item")
                for tag in _string_list(value, field="event.tags")
            )
        elif key in {"overdue_minutes"}:
            normalized[key] = _positive_int(value, field=key)
        elif key == "value":
            normalized[key] = _number(value, field=key)
        elif key == "ended_at":
            normalized[key] = _iso(_naive_utc(value, field=key))
        elif key in {"event", "metric", "submission_type", "from_status", "to_status"}:
            normalized[key] = _token(value, field=key)
        elif key == "from_email":
            sender = _text(value, field=key, limit=320, required=True).lower()
            if not _EMAIL_RE.fullmatch(sender):
                raise AutomationError("event.from_email must be an email address")
            normalized[key] = sender
        else:
            normalized[key] = _text(value, field=key, limit=500, required=True)
    return normalized


def _normalize_entity_changes(value: object) -> dict[str, Any]:
    changes = _bounded_object(value, field="database_update.changes")
    allowed = {
        "confidence", "due_at", "occurred_at", "properties", "provenance",
        "review_at", "sensitivity", "status", "summary", "title",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise AutomationError(
            "database_update only supports versioned LifeEntity fields; unsupported: "
            + ", ".join(unknown)
        )
    if not changes:
        raise AutomationError("database_update.changes must not be empty")
    return changes


def _recipient(channel: str, value: object, *, required: bool = True) -> str:
    clean = _text(value, field="recipient", limit=500, required=required)
    if channel == "email" and clean and not _EMAIL_RE.fullmatch(clean):
        raise AutomationError("recipient must be an email address")
    return clean


def _normalize_action(db, *, owner_id: str, raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise AutomationError("Each automation action must be an object")
    _strict_fields(
        raw,
        field="action",
        allowed={"type", "config", "autonomy_level"},
        required={"type"},
    )
    action_type = _token(raw.get("type"), field="action.type", limit=32)
    if action_type not in ACTION_TYPES:
        raise AutomationError("Unknown automation action type")
    config = _bounded_object(raw.get("config") or {}, field=f"{action_type}.config")
    domain = "information"
    target_type = action_type
    target_id: str | None = None

    if action_type == "create_entity":
        _strict_fields(
            config,
            field="create_entity",
            allowed={"entity_type", "properties", "provenance", "source_ids", "status", "summary", "title"},
            required={"entity_type", "title"},
        )
        entity_type = _token(config["entity_type"], field="entity_type", limit=48)
        if entity_type not in LIFE_ENTITY_TYPES:
            raise AutomationError("Unknown LifeEntity type")
        source_ids = _owned_source_ids(db, owner_id=owner_id, value=config.get("source_ids"))
        normalized_config = {
            "entity_type": entity_type,
            "title": _text(config["title"], field="title", limit=240, required=True),
            "summary": _text(config.get("summary"), field="summary", limit=20_000, preserve_lines=True),
            "status": _token(config.get("status") or "active", field="status", limit=32),
            "properties": _bounded_object(config.get("properties") or {}, field="properties"),
            "provenance": _bounded_object(config.get("provenance") or {}, field="provenance"),
            "source_ids": source_ids,
        }
        domain = _entity_domain(entity_type)
        target_type = "life_entity"
    elif action_type == "schedule":
        _strict_fields(
            config,
            field="schedule",
            allowed={"cron_expression", "name", "schedule", "scheduled_at", "scheduled_day", "scheduled_time", "timezone"},
            required={"name", "schedule"},
        )
        schedule = _token(config["schedule"], field="schedule")
        if schedule not in _SCHEDULE_KINDS:
            raise AutomationError("Unknown schedule kind")
        normalized_config = {
            "name": _text(config["name"], field="name", limit=240, required=True),
            "schedule": schedule,
        }
        if schedule == "once":
            scheduled_at = _naive_utc(config.get("scheduled_at"), field="scheduled_at")
            if scheduled_at is None:
                raise AutomationError("once schedules require scheduled_at")
            normalized_config["scheduled_at"] = _iso(scheduled_at)
        elif schedule == "cron":
            expression = _text(
                config.get("cron_expression"), field="cron_expression", limit=96, required=True
            )
            if not _CRON_RE.fullmatch(expression) or len(expression.split()) != 5:
                raise AutomationError("cron_expression must be a bounded five-field cron")
            normalized_config["cron_expression"] = expression
        else:
            scheduled_time = _text(
                config.get("scheduled_time"), field="scheduled_time", limit=5, required=True
            )
            if not _TIME_RE.fullmatch(scheduled_time):
                raise AutomationError("scheduled_time must use HH:MM")
            normalized_config["scheduled_time"] = scheduled_time
            if schedule in {"weekly", "monthly"}:
                day = _positive_int(config.get("scheduled_day"), field="scheduled_day")
                if (schedule == "weekly" and day > 6) or (schedule == "monthly" and not 1 <= day <= 31):
                    raise AutomationError("scheduled_day is out of range")
                normalized_config["scheduled_day"] = day
        if config.get("timezone"):
            normalized_config["timezone"] = _text(
                config["timezone"], field="timezone", limit=64, required=True
            )
        domain = "tasks"
        target_type = "scheduled_task"
    elif action_type == "database_update":
        _strict_fields(
            config,
            field="database_update",
            allowed={"changes", "entity_id", "expected_version"},
            required={"changes", "entity_id", "expected_version"},
        )
        entity = _owned_entity(db, owner_id=owner_id, entity_id=config["entity_id"])
        expected_version = _positive_int(
            config["expected_version"], field="expected_version", minimum=1
        )
        normalized_config = {
            "entity_id": entity.id,
            "expected_version": expected_version,
            "changes": _normalize_entity_changes(config["changes"]),
        }
        domain = _entity_domain(entity.entity_type)
        target_type = "life_entity"
        target_id = entity.id
    elif action_type in {"draft", "approved_send"}:
        _strict_fields(
            config,
            field=action_type,
            allowed={
                "body", "channel", "email_account_id", "recipient",
                "source_ids", "subject", "thread_id",
            },
            required={"body", "channel"} | ({"recipient"} if action_type == "approved_send" else set()),
        )
        channel = _token(config["channel"], field="channel")
        allowed_channels = DRAFT_CHANNELS if action_type == "draft" else SEND_CHANNELS
        if channel not in allowed_channels:
            if channel == "whatsapp" and action_type == "approved_send":
                raise AutomationError("WhatsApp is read-only; approved sends are not supported")
            raise AutomationError(f"Unsupported {action_type} channel")
        source_ids = _owned_source_ids(db, owner_id=owner_id, value=config.get("source_ids"))
        normalized_config = {
            "channel": channel,
            "recipient": _recipient(
                channel, config.get("recipient"), required=action_type == "approved_send"
            ),
            "subject": _text(config.get("subject"), field="subject", limit=500),
            "body": _text(
                config["body"], field="body", limit=20_000, required=True, preserve_lines=True
            ),
            "source_ids": source_ids,
        }
        if channel == "email" and action_type == "approved_send":
            if not normalized_config["subject"]:
                raise AutomationError("approved email sends require a subject")
            normalized_config["email_account_id"] = _text(
                config.get("email_account_id"),
                field="email_account_id",
                limit=255,
            )
        if config.get("thread_id"):
            normalized_config["thread_id"] = _text(
                config["thread_id"], field="thread_id", limit=255, required=True
            )
            target_id = normalized_config["thread_id"]
        domain = "email" if channel == "email" else "communications"
        target_type = "message"
    elif action_type == "briefing":
        _strict_fields(
            config,
            field="briefing",
            allowed={"sections", "source_ids", "title"},
            required={"title"},
        )
        normalized_config = {
            "title": _text(config["title"], field="title", limit=240, required=True),
            "sections": _string_list(config.get("sections"), field="sections", item_limit=120),
            "source_ids": _owned_source_ids(db, owner_id=owner_id, value=config.get("source_ids")),
        }
        target_type = "briefing"
    elif action_type == "file_move":
        _strict_fields(
            config,
            field="file_move",
            allowed={"current_location", "destination", "expected_version", "file_entity_id"},
            required={"destination", "expected_version", "file_entity_id"},
        )
        entity = _owned_entity(db, owner_id=owner_id, entity_id=config["file_entity_id"])
        if entity.entity_type != "file":
            raise AutomationError("file_move requires an owned file entity")
        normalized_config = {
            "file_entity_id": entity.id,
            "expected_version": _positive_int(
                config["expected_version"], field="expected_version", minimum=1
            ),
            "current_location": _text(
                config.get("current_location"), field="current_location", limit=1_000
            ),
            "destination": _text(
                config["destination"], field="destination", limit=1_000, required=True
            ),
        }
        domain = "files"
        target_type = "file"
        target_id = entity.id
    elif action_type == "report":
        _strict_fields(
            config,
            field="report",
            allowed={"format", "report_kind", "source_entity_ids", "source_ids", "title"},
            required={"format", "report_kind", "title"},
        )
        report_kind = _token(config["report_kind"], field="report_kind")
        report_format = _token(config["format"], field="format")
        if report_kind not in _REPORT_KINDS or report_format not in _REPORT_FORMATS:
            raise AutomationError("Unsupported report kind or format")
        entity_ids = _string_list(
            config.get("source_entity_ids"), field="source_entity_ids", limit=64,
            item_limit=36,
        )
        for entity_id in entity_ids:
            _owned_entity(db, owner_id=owner_id, entity_id=entity_id)
        normalized_config = {
            "title": _text(config["title"], field="title", limit=240, required=True),
            "report_kind": report_kind,
            "format": report_format,
            "source_entity_ids": entity_ids,
            "source_ids": _owned_source_ids(db, owner_id=owner_id, value=config.get("source_ids")),
        }
        target_type = "report"
    elif action_type == "information_request":
        _strict_fields(
            config,
            field="information_request",
            allowed={"questions", "source_entity_ids", "source_ids", "title"},
            required={"questions", "title"},
        )
        questions = _string_list(
            config["questions"], field="questions", limit=20, item_limit=1_000,
            required=True,
        )
        entity_ids = _string_list(
            config.get("source_entity_ids"), field="source_entity_ids", limit=64,
            item_limit=36,
        )
        for entity_id in entity_ids:
            _owned_entity(db, owner_id=owner_id, entity_id=entity_id)
        normalized_config = {
            "title": _text(config["title"], field="title", limit=240, required=True),
            "questions": questions,
            "source_entity_ids": entity_ids,
            "source_ids": _owned_source_ids(db, owner_id=owner_id, value=config.get("source_ids")),
        }
        target_type = "information_request"
    elif action_type == "notification":
        _strict_fields(
            config,
            field="notification",
            allowed={"channel", "message", "recipient", "source_ids", "title"},
            required={"channel", "message", "title"},
        )
        channel = _token(config["channel"], field="channel")
        if channel not in NOTIFICATION_CHANNELS:
            if channel == "whatsapp":
                raise AutomationError("WhatsApp is read-only; notifications cannot send there")
            raise AutomationError("Unsupported notification channel")
        normalized_config = {
            "channel": channel,
            "recipient": _recipient(channel, config.get("recipient"), required=False),
            "title": _text(config["title"], field="title", limit=240, required=True),
            "message": _text(
                config["message"], field="message", limit=4_000, required=True,
                preserve_lines=True,
            ),
            "source_ids": _owned_source_ids(db, owner_id=owner_id, value=config.get("source_ids")),
        }
        domain = "communications"
        target_type = "notification"
    elif action_type == "agent":
        _strict_fields(
            config,
            field="agent",
            allowed={"objective", "output_type", "role", "source_ids"},
            required={"objective", "output_type", "role"},
        )
        role = _token(config["role"], field="role")
        output_type = _token(config["output_type"], field="output_type")
        if role not in _AGENT_ROLES or output_type not in _AGENT_OUTPUTS:
            raise AutomationError("Unsupported bounded agent role or output type")
        normalized_config = {
            "role": role,
            "objective": _text(
                config["objective"], field="objective", limit=2_000, required=True
            ),
            "output_type": output_type,
            "source_ids": _owned_source_ids(db, owner_id=owner_id, value=config.get("source_ids")),
            "write_mode": "none",
        }
        target_type = "agent_preparation"
    else:  # workflow
        _strict_fields(
            config,
            field="workflow",
            allowed={"parameters", "source_ids", "workflow_type"},
            required={"workflow_type"},
        )
        workflow_type = _token(config["workflow_type"], field="workflow_type")
        if workflow_type not in _WORKFLOW_TYPES:
            raise AutomationError("Unsupported bounded workflow type")
        normalized_config = {
            "workflow_type": workflow_type,
            "parameters": _bounded_object(config.get("parameters") or {}, field="parameters"),
            "source_ids": _owned_source_ids(db, owner_id=owner_id, value=config.get("source_ids")),
            "output_mode": "prepare_only",
        }
        domain = "planning"
        target_type = "workflow_preparation"

    floor = _ACTION_FLOORS[action_type]
    if domain in _CRITICAL_DOMAINS:
        floor = max(floor, 6)
    requested = raw.get("autonomy_level", floor)
    if type(requested) is not int or requested < floor or requested > 6:
        raise AutomationError(
            f"{action_type}.autonomy_level must be an integer from {floor} to 6"
        )
    external = action_type in {"approved_send", "notification"}
    effective_level = max(requested, 5 if external else floor)
    return {
        "type": action_type,
        "domain": domain,
        "autonomy_level": effective_level,
        "external": external,
        "target_type": target_type,
        "target_id": target_id,
        "config": normalized_config,
    }


def _normalize_actions(db, *, owner_id: str, value: object) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)) or not value:
        raise AutomationError("actions must be a non-empty list")
    if len(value) > MAX_ACTIONS:
        raise AutomationError(f"actions must contain at most {MAX_ACTIONS} items")
    actions = [_normalize_action(db, owner_id=owner_id, raw=item) for item in value]
    encoded = json.dumps(actions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise AutomationError(f"actions must not exceed {MAX_JSON_BYTES} bytes")
    return actions


def _definition_properties(
    *, enabled: bool, trigger: dict[str, Any], actions: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": AUTOMATION_SCHEMA_VERSION,
        "record_kind": AUTOMATION_DEFINITION_KIND,
        "enabled": enabled,
        "trigger": trigger,
        "actions": actions,
        "execution_contract": dict(EXECUTION_CONTRACT),
    }


def _is_definition(entity: LifeEntity) -> bool:
    properties = entity.properties or {}
    return bool(
        entity.entity_type == AUTOMATION_ENTITY_TYPE
        and properties.get("record_kind") == AUTOMATION_DEFINITION_KIND
        and properties.get("schema_version") == AUTOMATION_SCHEMA_VERSION
    )


def _owned_automation(
    db,
    *,
    owner_id: str,
    automation_id: object,
    include_deleted: bool = False,
) -> LifeEntity:
    try:
        entity = get_life_entity(
            db,
            owner_id=owner_id,
            entity_id=automation_id,
            include_deleted=include_deleted,
        )
    except LifeGraphNotFound as exc:
        raise AutomationNotFound("Automation definition not found") from exc
    if not _is_definition(entity):
        raise AutomationNotFound("Automation definition not found")
    return entity


def serialize_automation_definition(entity: LifeEntity) -> dict[str, Any]:
    if not _is_definition(entity):
        raise AutomationError("Entity is not an automation definition")
    properties = dict(entity.properties or {})
    return {
        "id": entity.id,
        "name": entity.title or "",
        "description": entity.summary or "",
        "status": entity.status,
        "enabled": bool(properties.get("enabled")),
        "trigger": dict(properties.get("trigger") or {}),
        "actions": list(properties.get("actions") or []),
        "execution_contract": dict(properties.get("execution_contract") or {}),
        "source_ids": list((entity.provenance or {}).get("source_ids") or []),
        "sensitivity": entity.sensitivity,
        "version": int(entity.version or 1),
        "deleted_at": serialize_life_entity(entity)["deleted_at"],
        "created_at": serialize_life_entity(entity)["created_at"],
        "updated_at": serialize_life_entity(entity)["updated_at"],
    }


def create_automation_definition(
    db,
    *,
    account: Account,
    name: object,
    trigger: object,
    actions: object,
    description: object = "",
    enabled: bool = True,
    source_ids: object | None = None,
    sensitivity: object = "private",
    idempotency_key: object,
) -> tuple[LifeEntity, bool]:
    if not isinstance(enabled, bool):
        raise AutomationError("enabled must be true or false")
    normalized_trigger = _normalize_trigger(trigger)
    normalized_actions = _normalize_actions(db, owner_id=account.id, value=actions)
    normalized_sources = _owned_source_ids(
        db, owner_id=account.id, value=source_ids
    )
    normalized_sensitivity = _token(sensitivity, field="sensitivity", limit=24)
    if normalized_sensitivity not in {"private", "restricted"}:
        raise AutomationError("Automation sensitivity must be private or restricted")
    try:
        return create_life_entity(
            db,
            account=account,
            entity_type=AUTOMATION_ENTITY_TYPE,
            title=_text(name, field="name", limit=240, required=True),
            summary=_text(
                description, field="description", limit=20_000, preserve_lines=True
            ),
            status="active" if enabled else "paused",
            properties=_definition_properties(
                enabled=enabled,
                trigger=normalized_trigger,
                actions=normalized_actions,
            ),
            provenance={"domain": "automation_engine", "source_ids": normalized_sources},
            confidence=100,
            sensitivity=normalized_sensitivity,
            idempotency_key=_text(
                idempotency_key, field="idempotency_key", limit=1_024, required=True
            ),
            reason="Automation definition created",
        )
    except LifeGraphConflict as exc:
        raise AutomationConflict(str(exc)) from exc


def get_automation_definition(
    db, *, owner_id: str, automation_id: object
) -> LifeEntity:
    return _owned_automation(db, owner_id=owner_id, automation_id=automation_id)


def list_automation_definitions(
    db, *, owner_id: str, enabled: bool | None = None, limit: int = 50
) -> tuple[list[LifeEntity], bool]:
    bounded = max(1, min(100, int(limit)))
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == AUTOMATION_ENTITY_TYPE,
        LifeEntity.deleted_at.is_(None),
    )
    if enabled is not None:
        query = query.filter(
            LifeEntity.status == ("active" if enabled else "paused")
        )
    rows = query.order_by(LifeEntity.updated_at.desc(), LifeEntity.id.desc()).limit(
        bounded + 1
    ).all()
    filtered = [row for row in rows if _is_definition(row)]
    if enabled is not None:
        filtered = [
            row for row in filtered
            if bool((row.properties or {}).get("enabled")) is enabled
        ]
    return filtered[:bounded], len(filtered) > bounded


def update_automation_definition(
    db,
    *,
    owner_id: str,
    automation_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    if not isinstance(changes, Mapping):
        raise AutomationError("changes must be an object")
    unknown = sorted(
        set(changes)
        - {"actions", "description", "enabled", "name", "sensitivity", "source_ids", "trigger"}
    )
    if unknown:
        raise AutomationError(f"Unsupported automation fields: {', '.join(unknown)}")
    entity = _owned_automation(db, owner_id=owner_id, automation_id=automation_id)
    current = serialize_automation_definition(entity)
    enabled = changes.get("enabled", current["enabled"])
    if not isinstance(enabled, bool):
        raise AutomationError("enabled must be true or false")
    trigger = _normalize_trigger(changes.get("trigger", current["trigger"]))
    action_input = changes.get("actions")
    if action_input is None:
        # Stored actions also carry server-derived policy metadata.  Feed only
        # the caller-owned fields back through the normalizer on an unrelated
        # definition update so derived domain/target/external values cannot be
        # mistaken for user input (or become editable by round-trip).
        action_input = []
        for action in current["actions"]:
            config = dict(action["config"])
            config.pop("write_mode", None)
            config.pop("output_mode", None)
            action_input.append({
                "type": action["type"],
                "config": config,
                "autonomy_level": action["autonomy_level"],
            })
    actions = _normalize_actions(
        db, owner_id=owner_id, value=action_input
    )
    sources = _owned_source_ids(
        db, owner_id=owner_id, value=changes.get("source_ids", current["source_ids"])
    )
    sensitivity = _token(
        changes.get("sensitivity", current["sensitivity"]),
        field="sensitivity",
        limit=24,
    )
    if sensitivity not in {"private", "restricted"}:
        raise AutomationError("Automation sensitivity must be private or restricted")
    try:
        return update_life_entity(
            db,
            owner_id=owner_id,
            entity_id=entity.id,
            expected_version=expected_version,
            changes={
                "title": _text(
                    changes.get("name", current["name"]),
                    field="name", limit=240, required=True,
                ),
                "summary": _text(
                    changes.get("description", current["description"]),
                    field="description", limit=20_000, preserve_lines=True,
                ),
                "status": "active" if enabled else "paused",
                "properties": _definition_properties(
                    enabled=enabled, trigger=trigger, actions=actions
                ),
                "provenance": {"domain": "automation_engine", "source_ids": sources},
                "sensitivity": sensitivity,
            },
            reason="Automation definition updated",
        )
    except LifeGraphConflict as exc:
        raise AutomationConflict(str(exc)) from exc


def automation_definition_history(
    db,
    *,
    owner_id: str,
    automation_id: object,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    """Return version snapshots for one owned definition, including deletes."""

    _owned_automation(
        db,
        owner_id=owner_id,
        automation_id=automation_id,
        include_deleted=True,
    )
    try:
        rows, truncated = list_life_entity_versions(
            db,
            owner_id=owner_id,
            entity_id=automation_id,
            limit=limit,
        )
    except LifeGraphNotFound as exc:
        raise AutomationNotFound("Automation definition not found") from exc
    return [serialize_life_entity_version(row) for row in rows], truncated


def delete_automation_definition(
    db,
    *,
    owner_id: str,
    automation_id: object,
    expected_version: int,
    reason: object,
) -> LifeEntity:
    """Soft-delete one owned definition behind Life graph CAS."""

    entity = _owned_automation(
        db,
        owner_id=owner_id,
        automation_id=automation_id,
        include_deleted=True,
    )
    try:
        return delete_life_entity(
            db,
            owner_id=owner_id,
            entity_id=entity.id,
            expected_version=expected_version,
            reason=_text(reason, field="reason", limit=1_000, required=True),
        )
    except LifeGraphConflict as exc:
        raise AutomationConflict(str(exc)) from exc
    except LifeGraphNotFound as exc:
        raise AutomationNotFound("Automation definition not found") from exc


def _metric_matches(operator: str, actual: object, threshold: object) -> bool:
    left = Decimal(str(actual))
    right = Decimal(str(threshold))
    return {
        "gt": left > right,
        "gte": left >= right,
        "lt": left < right,
        "lte": left <= right,
        "eq": left == right,
    }[operator]


def _trigger_matches(trigger: Mapping[str, Any], event: Mapping[str, Any]) -> bool:
    trigger_type = str(trigger["type"])
    if event.get("type") != trigger_type:
        return False
    config = trigger["config"]
    if trigger_type == "time":
        return event.get("schedule_key") == config["schedule_key"]
    if trigger_type == "email":
        sender = str(event.get("from_email") or "").lower()
        tags = set(event.get("tags") or [])
        return all((
            not config.get("account_id") or event.get("account_id") == config["account_id"],
            not config.get("from_email") or sender == config["from_email"],
            not config.get("from_domain") or sender.rpartition("@")[2] == config["from_domain"],
            not config.get("subject_contains")
            or config["subject_contains"].casefold() in str(event.get("subject") or "").casefold(),
            not config.get("tags_any") or bool(tags & set(config["tags_any"])),
        ))
    if trigger_type == "calendar":
        return all((
            event.get("event") == config["event"],
            not config.get("calendar_id") or event.get("calendar_id") == config["calendar_id"],
            not config.get("event_id") or event.get("event_id") == config["event_id"],
            not config.get("title_contains")
            or config["title_contains"].casefold() in str(event.get("title") or "").casefold(),
        ))
    if trigger_type == "overdue_task":
        return all((
            int(event.get("overdue_minutes", -1)) >= int(config["minimum_overdue_minutes"]),
            not config.get("project_id") or event.get("project_id") == config["project_id"],
        ))
    if trigger_type == "upload":
        filename = str(event.get("filename") or "").lower()
        extension = filename.rpartition(".")[2] if "." in filename else ""
        return all((
            not config.get("extension") or extension == config["extension"],
            not config.get("mime_prefix") or str(event.get("mime") or "").lower().startswith(config["mime_prefix"]),
            not config.get("source") or str(event.get("source") or "").lower() == config["source"],
        ))
    if trigger_type == "person":
        return all((
            event.get("event") == config["event"],
            not config.get("person_id") or event.get("person_id") == config["person_id"],
        ))
    if trigger_type == "metric_threshold":
        return bool(
            event.get("metric") == config["metric"]
            and _metric_matches(config["operator"], event["value"], config["threshold"])
        )
    if trigger_type == "location":
        return bool(
            event.get("event") == config["event"]
            and event.get("place_id") == config["place_id"]
        )
    if trigger_type == "form":
        return all((
            event.get("form_id") == config["form_id"],
            not config.get("submission_type")
            or event.get("submission_type") == config["submission_type"],
        ))
    return all((
        event.get("to_status") == config["to_status"],
        not config.get("from_status") or event.get("from_status") == config["from_status"],
        not config.get("project_id") or event.get("project_id") == config["project_id"],
    ))


def _policy_plan(db, *, owner_id: str, index: int, action: Mapping[str, Any]) -> dict[str, Any]:
    policy = get_effective_action_policy(
        db, owner_id=owner_id, domain=action["domain"]
    )
    rules = dict(policy.rules or {})
    if not policy.enabled or rules.get("automations_enabled") is False:
        raise AutomationPolicyDenied(
            f"Automations are disabled for the {action['domain']} domain"
        )
    blocked = rules.get("blocked_automation_actions", [])
    if not isinstance(blocked, list) or any(not isinstance(item, str) for item in blocked):
        raise AutomationPolicyDenied("Automation policy rules are malformed")
    if action["type"] in blocked:
        raise AutomationPolicyDenied(
            f"{action['type']} is blocked by the {action['domain']} policy"
        )
    policy_cap = int(policy.max_autonomy)
    if "automation_max_autonomy" in rules:
        rule_cap = rules["automation_max_autonomy"]
        if type(rule_cap) is not int or not 1 <= rule_cap <= 6:
            raise AutomationPolicyDenied("Automation policy rules are malformed")
        policy_cap = min(policy_cap, rule_cap)
    level = int(action["autonomy_level"])
    if level > policy_cap:
        raise AutomationPolicyDenied(
            f"Automation action level {level} exceeds the {action['domain']} domain cap {policy_cap}"
        )
    extra_confirmation = rules.get("require_automation_confirmation", False)
    if not isinstance(extra_confirmation, bool):
        raise AutomationPolicyDenied("Automation policy rules are malformed")
    requires_approval = bool(
        action["external"] or level >= 5 or extra_confirmation
    )
    return {
        "index": index,
        "type": action["type"],
        "domain": action["domain"],
        "autonomy_level": level,
        "external": bool(action["external"]),
        "requires_approval": requires_approval,
        "execution_mode": (
            "approval_required" if requires_approval
            else "typed_executor_required" if level >= 4
            else "prepare_only"
        ),
        "target_type": action["target_type"],
        "target_id": action.get("target_id"),
        "config": dict(action["config"]),
        "policy_version": int(policy.version),
    }


def _event_fingerprint(event: Mapping[str, Any]) -> str:
    encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _event_summary(event: Mapping[str, Any]) -> dict[str, Any]:
    structural_keys = {
        "type", "schedule_key", "account_id", "message_id", "calendar_id",
        "event", "event_id", "task_id", "file_id", "person_id", "metric",
        "place_id", "form_id", "submission_id", "project_id", "from_status",
        "to_status", "occurred_at", "ended_at",
    }
    return {key: event[key] for key in sorted(structural_keys & set(event))}


def evaluate_automation(
    db,
    *,
    owner_id: str,
    automation_id: object,
    event: object,
) -> AutomationEvaluation:
    """Classify one event without writing or invoking any executor/model."""

    normalized_event = _normalize_event(event)
    with db.no_autoflush:
        automation = _owned_automation(
            db, owner_id=owner_id, automation_id=automation_id
        )
        properties = dict(automation.properties or {})
        trigger = dict(properties["trigger"])
        matched = bool(
            properties.get("enabled")
            and automation.status == "active"
            and _trigger_matches(trigger, normalized_event)
        )
        plans = tuple(
            _policy_plan(db, owner_id=owner_id, index=index, action=action)
            for index, action in enumerate(properties.get("actions") or [])
        ) if matched else ()
        source_ids = _owned_source_ids(
            db,
            owner_id=owner_id,
            value=sorted(set(
                list((automation.provenance or {}).get("source_ids") or [])
                + list(normalized_event.get("source_ids") or [])
            )),
        )
    return AutomationEvaluation(
        automation_id=automation.id,
        automation_version=int(automation.version or 1),
        matched=matched,
        trigger_type=str(trigger["type"]),
        event_fingerprint=_event_fingerprint(normalized_event),
        event_summary=_event_summary(normalized_event),
        source_ids=tuple(source_ids),
        plans=plans,
    )


def _external_action_identifiers(plan: Mapping[str, Any]) -> tuple[str, str]:
    if plan["type"] == "approved_send":
        if plan["config"]["channel"] == "email":
            return "email", "send_email"
        return "communications", "send_message"
    if plan["type"] == "notification":
        return "communications", "send_message"
    raise AutomationError("Only approved sends and notifications create external proposals")


def prepare_automation_run(
    db,
    *,
    account: Account,
    automation_id: object,
    event: object,
    idempotency_key: object,
) -> AutomationPreparation:
    """Persist a preparation record; never execute a prepared action."""

    raw_key = _text(
        idempotency_key, field="idempotency_key", limit=1_024, required=True
    )
    evaluation = evaluate_automation(
        db, owner_id=account.id, automation_id=automation_id, event=event
    )
    if not evaluation.matched:
        return AutomationPreparation(evaluation, None, False, (), {})

    proposal_ids: list[str] = []
    confirmation_tokens: dict[str, str] = {}
    prepared_plans: list[dict[str, Any]] = []
    for plan in evaluation.plans:
        prepared = dict(plan)
        if plan["external"]:
            proposal_domain, proposal_action = _external_action_identifiers(plan)
            try:
                proposal_key = (
                    f"life-automation-proposal:{account.id}:"
                    f"{evaluation.automation_id}:{raw_key}:{plan['index']}"
                )
                if (
                    plan["type"] == "approved_send"
                    and plan["config"]["channel"] == "email"
                ):
                    # Use the existing immutable email-draft authority so a
                    # later human approval can queue the exact reviewed bytes.
                    # This path still performs no SMTP/IMAP network action and
                    # intentionally exposes no confirmation token.
                    email_result = prepare_agent_email_action(
                        db,
                        owner_username=account.username,
                        email_account_id=plan["config"].get("email_account_id"),
                        to=plan["config"]["recipient"],
                        subject=plan["config"]["subject"],
                        body=plan["config"]["body"],
                        source={
                            "automation_id": evaluation.automation_id,
                            "automation_version": evaluation.automation_version,
                            "event_fingerprint": evaluation.event_fingerprint,
                            "life_source_ids": list(evaluation.source_ids),
                        },
                        idempotency_key=proposal_key,
                    )
                    proposal = email_result.proposal
                    proposal_created = bool(email_result.created)
                    confirmation_token = None
                else:
                    proposal_result = create_action_proposal(
                        db,
                        owner_id=account.id,
                        domain=proposal_domain,
                        action=proposal_action,
                        autonomy_level=max(5, int(plan["autonomy_level"])),
                        target_type=plan["target_type"],
                        target_id=plan.get("target_id"),
                        payload={
                            "automation_id": evaluation.automation_id,
                            "automation_version": evaluation.automation_version,
                            "typed_action": plan["type"],
                            "event_fingerprint": evaluation.event_fingerprint,
                            "config": dict(plan["config"]),
                        },
                        reason="Automation prepared an external action for human review",
                        sources={
                            "life_source_ids": list(evaluation.source_ids),
                            "automation_id": evaluation.automation_id,
                        },
                        external=True,
                        idempotency_key=proposal_key,
                    )
                    proposal = proposal_result.proposal
                    proposal_created = bool(proposal_result.created)
                    confirmation_token = proposal_result.confirmation_token
            except (ActionPolicyConflict, ActionPolicyDenied, EmailOutboundError) as exc:
                if isinstance(exc, ActionPolicyConflict):
                    raise AutomationConflict(str(exc)) from exc
                if isinstance(exc, EmailOutboundError):
                    raise AutomationError(str(exc)) from exc
                raise AutomationPolicyDenied(str(exc)) from exc
            prepared["proposal_id"] = proposal.id
            prepared["proposal_state"] = proposal.state
            proposal_ids.append(proposal.id)
            if proposal_created and confirmation_token:
                confirmation_tokens[proposal.id] = confirmation_token
        prepared_plans.append(prepared)

    automation = _owned_automation(
        db, owner_id=account.id, automation_id=evaluation.automation_id
    )
    properties = {
        "schema_version": AUTOMATION_SCHEMA_VERSION,
        "record_kind": AUTOMATION_RUN_KIND,
        "automation_id": evaluation.automation_id,
        "automation_version": evaluation.automation_version,
        "event_fingerprint": evaluation.event_fingerprint,
        "event_summary": evaluation.event_summary,
        "trigger_type": evaluation.trigger_type,
        "state": "prepared",
        "action_plans": prepared_plans,
        "execution_contract": dict(EXECUTION_CONTRACT),
    }
    try:
        run_entity, created = create_life_entity(
            db,
            account=account,
            entity_type=AUTOMATION_RUN_ENTITY_TYPE,
            title=f"Automation preparation: {automation.title}"[:240],
            summary=(
                "Prepared typed actions only; no connector or domain mutation was executed."
            ),
            status="prepared",
            properties=properties,
            provenance={
                "domain": "automation_engine",
                "source_ids": list(evaluation.source_ids),
            },
            confidence=100,
            sensitivity=automation.sensitivity,
            idempotency_key=(
                f"life-automation-run:{account.id}:"
                f"{evaluation.automation_id}:{raw_key}"
            ),
            reason="Automation actions prepared",
        )
    except LifeGraphConflict as exc:
        raise AutomationConflict(str(exc)) from exc
    return AutomationPreparation(
        evaluation=evaluation,
        run_entity=run_entity,
        created=created,
        proposal_ids=tuple(proposal_ids),
        confirmation_tokens=confirmation_tokens,
    )


def prepare_meeting_end_workflow(
    db,
    *,
    account: Account,
    meeting_entity_id: object,
    source_ids: object,
    follow_ups: object,
    idempotency_key: object,
    ended_at: object | None = None,
) -> AutomationPreparation:
    """Prepare source-backed meeting follow-ups without sending anything.

    Every follow-up becomes a Level-5 external proposal containing the exact
    encrypted draft.  Approval and delivery remain separate, existing
    boundaries.  At least one owned LifeSource is mandatory.
    """

    meeting = _owned_entity(
        db, owner_id=account.id, entity_id=meeting_entity_id
    )
    if meeting.entity_type != "event":
        raise AutomationError("meeting_entity_id must reference an owned event")
    owned_sources = _owned_source_ids(
        db, owner_id=account.id, value=source_ids, required=True
    )
    if not isinstance(follow_ups, (list, tuple)) or not follow_ups:
        raise AutomationError("follow_ups must be a non-empty list")
    if len(follow_ups) > 20:
        raise AutomationError("follow_ups must contain at most 20 drafts")
    actions: list[dict[str, Any]] = [{
        "type": "briefing",
        "config": {
            "title": f"Follow-up briefing: {meeting.title}"[:240],
            "sections": ["decisions", "commitments", "open_questions", "follow_ups"],
            "source_ids": owned_sources,
        },
    }]
    for follow_up in follow_ups:
        if not isinstance(follow_up, Mapping):
            raise AutomationError("Each follow-up draft must be an object")
        _strict_fields(
            follow_up,
            field="follow_up",
            allowed={
                "body", "channel", "email_account_id", "recipient",
                "subject", "thread_id",
            },
            required={"body", "channel", "recipient"},
        )
        config = dict(follow_up)
        config["source_ids"] = owned_sources
        actions.append({"type": "approved_send", "config": config})

    raw_key = _text(
        idempotency_key, field="idempotency_key", limit=1_024, required=True
    )
    definition, _created = create_automation_definition(
        db,
        account=account,
        name=f"Meeting-end follow-up: {meeting.title}"[:240],
        description=(
            "Source-backed meeting follow-up preparation; sends require fresh approval."
        ),
        trigger={
            "type": "calendar",
            "config": {"event": "meeting_ended", "event_id": meeting.id},
        },
        actions=actions,
        source_ids=owned_sources,
        sensitivity=meeting.sensitivity,
        idempotency_key=f"meeting-end-definition:{account.id}:{raw_key}",
    )
    normalized_ended = _naive_utc(ended_at, field="ended_at")
    event: dict[str, Any] = {
        "type": "calendar",
        "event": "meeting_ended",
        "event_id": meeting.id,
        "title": meeting.title,
        "source_ids": owned_sources,
    }
    if normalized_ended is not None:
        event["ended_at"] = _iso(normalized_ended)
    return prepare_automation_run(
        db,
        account=account,
        automation_id=definition.id,
        event=event,
        idempotency_key=f"meeting-end-run:{account.id}:{raw_key}",
    )


__all__ = [
    "ACTION_TYPES",
    "AUTOMATION_DEFINITION_KIND",
    "AUTOMATION_RUN_KIND",
    "AutomationConflict",
    "AutomationError",
    "AutomationEvaluation",
    "AutomationNotFound",
    "AutomationPolicyDenied",
    "AutomationPreparation",
    "DRAFT_CHANNELS",
    "EXECUTION_CONTRACT",
    "NOTIFICATION_CHANNELS",
    "SEND_CHANNELS",
    "TRIGGER_TYPES",
    "automation_definition_history",
    "create_automation_definition",
    "delete_automation_definition",
    "evaluate_automation",
    "get_automation_definition",
    "list_automation_definitions",
    "prepare_automation_run",
    "prepare_meeting_end_workflow",
    "serialize_automation_definition",
    "update_automation_definition",
]
