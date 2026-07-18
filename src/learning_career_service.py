"""Typed V3 Learning & Career authority on Restia's canonical Life graph.

Learning and career records are encrypted, ``Account.id``-owned
``LifeEntity`` rows.  Every record is backed by at least one owner-scoped
``LifeSource`` and may reference only owner-scoped graph entities.  The module
is record and read-model only: it cannot apply, submit, message, upload, or
otherwise mutate an external system.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import urlsplit

from core.database import Account, LifeEntity, LifeSource
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


LEARNING_CAREER_SCHEMA_VERSION = 1
LEARNING_ENTITY_TYPE = "learning_record"
CAREER_ENTITY_TYPE = "career_item"
LEARNING_CAREER_SCAN_LIMIT = 1_000
LEARNING_CAREER_CHAIN_LIMIT = 250

LEARNING_RECORD_KINDS = frozenset({
    "skill",
    "course",
    "paper",
    "book",
    "learning_objective",
    "note",
    "practice",
    "project",
    "progress",
    "review",
})
CAREER_RECORD_KINDS = frozenset({
    "role",
    "company",
    "university",
    "application",
    "resume",
    "portfolio",
    "achievement",
    "networking",
    "interview_prep",
    "gap",
})

RECORD_KINDS = {
    "learning": LEARNING_RECORD_KINDS,
    "career": CAREER_RECORD_KINDS,
}
ENTITY_TYPES = {
    "learning": LEARNING_ENTITY_TYPE,
    "career": CAREER_ENTITY_TYPE,
}
STATUSES = frozenset({
    "active", "planned", "in_progress", "completed", "paused", "archived",
    "rejected", "withdrawn",
})
SENSITIVITIES = frozenset({"private", "restricted"})
SOURCE_RELATIONS = frozenset({
    "supports", "describes", "evidence", "syllabus", "publication",
    "assessment", "application_material", "portfolio_evidence", "reference",
})
ENTITY_RELATIONS = frozenset({
    "requires_capability", "has_gap", "addressed_by", "evidenced_by",
    "weekly_action", "related_to", "supports", "evidence_for", "part_of",
    "target_role", "target_company", "target_university", "supersedes",
})

DETAIL_SCHEMAS: dict[tuple[str, str], dict[str, str]] = {
    ("learning", "skill"): {
        "proficiency_level": "text", "target_level": "text",
        "category": "text", "assessment_note": "long_text",
    },
    ("learning", "course"): {
        "provider": "text", "url": "url", "started_on": "date",
        "completed_on": "date", "progress_percent": "percent",
        "credential_name": "text",
    },
    ("learning", "paper"): {
        "authors": "text_list", "venue": "text", "published_on": "date",
        "url": "url", "citation": "long_text",
    },
    ("learning", "book"): {
        "authors": "text_list", "publisher": "text", "published_on": "date",
        "url": "url", "isbn": "text", "progress_percent": "percent",
    },
    ("learning", "learning_objective"): {
        "target_date": "date", "success_metric": "long_text",
        "priority": "priority", "scope": "long_text",
    },
    ("learning", "note"): {
        "topic": "text", "locator": "text", "keywords": "text_list",
    },
    ("learning", "practice"): {
        "practiced_on": "date", "duration_minutes": "positive_integer",
        "score": "decimal", "method": "text", "reflection": "long_text",
    },
    ("learning", "project"): {
        "started_on": "date", "completed_on": "date",
        "repository_url": "url", "outcome": "long_text",
        "technologies": "text_list",
    },
    ("learning", "progress"): {
        "measured_on": "date", "progress_percent": "percent",
        "metric": "text", "value": "decimal", "unit": "text",
    },
    ("learning", "review"): {
        "reviewed_on": "date", "rating": "rating", "findings": "long_text",
        "next_steps": "text_list",
    },
    ("career", "role"): {
        "organization": "text", "location": "text", "level": "text",
        "employment_type": "text", "target_date": "date", "url": "url",
    },
    ("career", "company"): {
        "industry": "text", "location": "text", "website": "url",
        "interest_level": "rating",
    },
    ("career", "university"): {
        "program": "text", "department": "text", "location": "text",
        "deadline": "date", "url": "url",
    },
    ("career", "application"): {
        "application_state": "text", "deadline": "date",
        "submitted_on": "date", "decision_on": "date", "url": "url",
    },
    ("career", "resume"): {
        "version_label": "text", "storage_ref": "long_text",
        "updated_on": "date", "audience": "text",
    },
    ("career", "portfolio"): {
        "url": "url", "storage_ref": "long_text", "theme": "text",
        "last_reviewed_on": "date",
    },
    ("career", "achievement"): {
        "achieved_on": "date", "issuer": "text", "evidence_label": "text",
    },
    ("career", "networking"): {
        "contact_label": "text", "occasion": "text", "occurred_on": "date",
        "next_follow_up_on": "date", "notes": "long_text",
    },
    ("career", "interview_prep"): {
        "interview_on": "date", "focus_areas": "text_list",
        "readiness_percent": "percent", "format": "text",
    },
    ("career", "gap"): {
        "current_level": "text", "target_level": "text",
        "priority": "priority", "evidence": "long_text",
    },
}

CHAIN_RELATIONS = frozenset({
    "requires_capability", "has_gap", "addressed_by", "evidenced_by",
    "weekly_action",
})
CHAIN_TARGETS: dict[str, tuple[set[tuple[str, str]], set[tuple[str, str]]]] = {
    "requires_capability": (
        {("career", kind) for kind in ("role", "application", "company", "university")},
        {("learning", "skill")},
    ),
    "has_gap": ({("learning", "skill")}, {("career", "gap")}),
    "addressed_by": (
        {("career", "gap")},
        {("learning", kind) for kind in ("learning_objective", "course", "project")},
    ),
    "evidenced_by": (
        {("learning", kind) for kind in ("learning_objective", "course", "project")},
        {("career", "portfolio")},
    ),
    "weekly_action": (
        {("career", "portfolio")},
        {("learning", kind) for kind in ("practice", "project")},
    ),
}

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_URL_CREDENTIAL_RE = re.compile(
    r"[a-z][a-z0-9+.-]*://[^/@\s]+:[^/@\s]+@", re.IGNORECASE
)
_BEARER_RE = re.compile(r"\bauthorization\s*:\s*bearer\s+\S+", re.IGNORECASE)
_BASIC_AUTH_RE = re.compile(r"\bauthorization\s*:\s*basic\s+\S+", re.IGNORECASE)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_SECRET_KEY_PARTS = (
    "password", "passwd", "passcode", "secret", "token", "credential",
    "cookie", "authorization", "api_key", "private_key", "seed_phrase",
    "recovery_phrase",
)
_EXECUTOR_KEYS = frozenset({
    "action", "execute", "executor", "tool_call", "external_action", "webhook",
    "send", "send_message", "send_email", "message_payload", "recipient",
    "apply_now", "submit", "submit_application", "upload", "dispatch",
    "http_request", "shell", "command", "operation", "workflow", "payload",
    "request_payload", "executor_payload", "intent",
})


def _is_secret_key(value: object) -> bool:
    key = str(value or "").strip().lower().replace("-", "_")
    # A course credential's public display name is ordinary learning metadata,
    # not an authentication credential.  It is an explicitly typed safe field.
    if key == "credential_name":
        return False
    compact = re.sub(r"[^a-z0-9]", "", key)
    return any(
        re.sub(r"[^a-z0-9]", "", part) in compact
        for part in _SECRET_KEY_PARTS
    )


def _is_executor_key(value: object) -> bool:
    key = str(value or "").strip().lower().replace("-", "_")
    compact = re.sub(r"[^a-z0-9]", "", key)
    return (
        key in _EXECUTOR_KEYS
        or key.startswith(("send_", "dispatch_", "execute_", "submit_", "upload_"))
        or compact in {
            "action", "execute", "executor", "toolcall", "externalaction",
            "webhook", "send", "sendmessage", "sendemail", "messagepayload",
            "recipient", "applynow", "submit", "submitapplication", "upload",
            "dispatch", "httprequest", "shell", "command", "operation",
            "workflow", "payload", "requestpayload", "executorpayload", "intent",
        }
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
        or _BASIC_AUTH_RE.search(normalized)
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


def _date(value: object | None, *, field: str, required: bool = False) -> date | None:
    if value is None or value == "":
        if required:
            raise LifeGraphError(f"{field} is required")
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise LifeGraphError(f"{field} must be an ISO-8601 date") from exc
    raise LifeGraphError(f"{field} must be an ISO-8601 date")


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


def _decimal(value: object, *, field: str) -> str:
    if isinstance(value, bool):
        raise LifeGraphError(f"{field} must be a finite decimal number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise LifeGraphError(f"{field} must be a finite decimal number") from exc
    if not number.is_finite() or len(number.as_tuple().digits) > 18:
        raise LifeGraphError(f"{field} must be a finite decimal number")
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _url(value: object, *, field: str) -> str:
    normalized = _text(value, field=field, limit=2_000)
    if not normalized:
        return ""
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LifeGraphError(f"{field} must be an http or https URL")
    if parsed.username is not None or parsed.password is not None:
        raise LifeGraphError(f"{field} must not contain credentials")
    return normalized


def _assert_safe_payload(value: object, *, field: str) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key or "").strip().lower().replace("-", "_")
            if _is_secret_key(key):
                raise LifeGraphError(f"{field} must not contain credentials or secrets")
            if _is_executor_key(key):
                raise LifeGraphError(
                    f"{field} cannot request applications, submissions, messages, "
                    "uploads, or another external action"
                )
            _assert_safe_payload(child, field=field)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_safe_payload(child, field=field)
    elif isinstance(value, str):
        normalized = _text(value, field=field, limit=20_000, preserve_lines=True)
        command_token = normalized.strip().lower().replace("-", "_")
        if re.fullmatch(
            r"(?:apply|submit|send|dispatch|execute|upload)(?:_[a-z0-9]+)+",
            command_token,
        ):
            raise LifeGraphError(
                f"{field} cannot contain an external action instruction"
            )


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
    if isinstance(value, (list, tuple)):
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
    if not isinstance(result, dict):  # Defensive: input is already a Mapping.
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


def _domain_and_kind(domain: object, record_kind: object) -> tuple[str, str]:
    normalized_domain = _token(domain, field="domain", limit=16)
    if normalized_domain not in RECORD_KINDS:
        raise LifeGraphError("domain must be learning or career")
    normalized_kind = _token(record_kind, field="record_kind", limit=48)
    if normalized_kind not in RECORD_KINDS[normalized_domain]:
        raise LifeGraphError(
            f"Unsupported {normalized_domain} record kind: {normalized_kind}"
        )
    return normalized_domain, normalized_kind


def _normalize_detail_value(value: object, *, field: str, value_type: str) -> Any:
    if value_type == "text":
        return _text(value, field=field, limit=500)
    if value_type == "long_text":
        return _text(value, field=field, limit=5_000, preserve_lines=True)
    if value_type == "text_list":
        if not isinstance(value, list) or len(value) > 50:
            raise LifeGraphError(f"{field} must be a list with at most 50 items")
        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            normalized = _text(item, field=field, limit=500, required=True)
            marker = normalized.casefold()
            if marker not in seen:
                seen.add(marker)
                result.append(normalized)
        return result
    if value_type == "date":
        parsed = _date(value, field=field)
        return parsed.isoformat() if parsed is not None else None
    if value_type == "url":
        return _url(value, field=field)
    if value_type == "positive_integer":
        return _integer(value, field=field, minimum=1, maximum=100_000)
    if value_type == "percent":
        return _integer(value, field=field, minimum=0, maximum=100)
    if value_type == "rating":
        return _integer(value, field=field, minimum=0, maximum=10)
    if value_type == "decimal":
        return _decimal(value, field=field)
    if value_type == "priority":
        normalized = _token(value, field=field, limit=16)
        if normalized not in {"low", "normal", "high", "urgent"}:
            raise LifeGraphError(f"{field} must be low, normal, high, or urgent")
        return normalized
    raise LifeGraphError(f"Unsupported detail field type for {field}")


def _details(domain: str, record_kind: str, value: object | None) -> dict[str, Any]:
    raw = _bounded_object(value, field="details", max_bytes=32_000)
    schema = DETAIL_SCHEMAS[(domain, record_kind)]
    unknown = sorted(set(raw) - set(schema))
    if unknown:
        raise LifeGraphError(
            f"Unsupported {record_kind} detail fields: {', '.join(unknown)}"
        )
    return {
        key: _normalize_detail_value(raw[key], field=f"details.{key}", value_type=schema[key])
        for key in sorted(raw)
    }


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
            raise LifeGraphNotFound("Learning/Career source not found")
        relation = _token(raw.get("relation") or "supports", field="source link relation")
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
            "locator": _text(raw.get("locator"), field="source link locator", limit=1_000),
        })
    return sorted(result, key=lambda row: (row["source_id"], row["relation"]))


def _stored_identity(entity: LifeEntity) -> tuple[str, str] | None:
    properties = entity.properties if isinstance(entity.properties, Mapping) else {}
    if properties.get("learning_career_schema_version") != LEARNING_CAREER_SCHEMA_VERSION:
        return None
    try:
        domain, record_kind = _domain_and_kind(
            properties.get("domain"), properties.get("record_kind")
        )
    except LifeGraphError:
        return None
    if entity.entity_type != ENTITY_TYPES[domain]:
        return None
    return domain, record_kind


def _validate_chain_link(
    *, source_identity: tuple[str, str], relation: str, target: LifeEntity
) -> None:
    if relation not in CHAIN_RELATIONS:
        return
    allowed_sources, allowed_targets = CHAIN_TARGETS[relation]
    if source_identity not in allowed_sources:
        raise LifeGraphError(
            f"{relation} is not valid for {source_identity[0]} {source_identity[1]} records"
        )
    target_identity = _stored_identity(target)
    if target_identity not in allowed_targets:
        expected = ", ".join(
            f"{domain}:{kind}" for domain, kind in sorted(allowed_targets)
        )
        raise LifeGraphError(f"{relation} target must be one of: {expected}")


def _entity_links(
    db,
    *,
    owner_id: str,
    domain: str,
    record_kind: str,
    value: object | None,
    current_entity_id: str | None = None,
    allow_deleted_targets: bool = False,
) -> list[dict[str, str]]:
    if value is None:
        rows: list[object] = []
    elif isinstance(value, list):
        rows = value
    else:
        raise LifeGraphError("entity_links must be a list")
    if len(rows) > 50:
        raise LifeGraphError("entity_links must not contain more than 50 items")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in rows:
        raw = _bounded_object(item, field="entity link", max_bytes=4_000)
        unknown = sorted(set(raw) - {"entity_id", "relation", "label"})
        if unknown:
            raise LifeGraphError(f"Unsupported entity link fields: {', '.join(unknown)}")
        entity_id = _text(
            raw.get("entity_id"), field="entity_links.entity_id", limit=36,
            required=True,
        )
        if current_entity_id and entity_id == current_entity_id:
            raise LifeGraphError("A Learning/Career record cannot reference itself")
        target_query = db.query(LifeEntity).filter(
            LifeEntity.id == entity_id,
            LifeEntity.owner_id == owner_id,
        )
        if not allow_deleted_targets:
            target_query = target_query.filter(LifeEntity.deleted_at.is_(None))
        target = target_query.first()
        if target is None:
            raise LifeGraphNotFound("Linked Life entity not found")
        relation = _token(raw.get("relation"), field="entity link relation")
        if relation not in ENTITY_RELATIONS:
            raise LifeGraphError(
                "entity link relation must be one of: "
                + ", ".join(sorted(ENTITY_RELATIONS))
            )
        _validate_chain_link(
            source_identity=(domain, record_kind), relation=relation, target=target
        )
        marker = (entity_id, relation)
        if marker in seen:
            raise LifeGraphError("entity_links must be unique by entity and relation")
        seen.add(marker)
        result.append({
            "entity_id": entity_id,
            "relation": relation,
            "label": _text(raw.get("label"), field="entity link label", limit=240),
        })
    return sorted(result, key=lambda row: (row["relation"], row["entity_id"]))


def _weekly_action(
    domain: str, record_kind: str, value: object | None
) -> dict[str, Any] | None:
    if value is None:
        return None
    if (domain, record_kind) not in {
        ("learning", "practice"), ("learning", "project")
    }:
        raise LifeGraphError("weekly_action is allowed only on practice or project records")
    raw = _bounded_object(value, field="weekly_action", max_bytes=8_000)
    unknown = sorted(
        set(raw)
        - {"week_start", "definition_of_done", "estimated_minutes", "priority", "status"}
    )
    if unknown:
        raise LifeGraphError(f"Unsupported weekly action fields: {', '.join(unknown)}")
    week_start = _date(raw.get("week_start"), field="weekly_action.week_start", required=True)
    assert week_start is not None
    if week_start.weekday() != 0:
        raise LifeGraphError("weekly_action.week_start must be a Monday")
    status = _token(raw.get("status") or "planned", field="weekly_action.status")
    if status not in {"planned", "in_progress", "completed", "skipped"}:
        raise LifeGraphError(
            "weekly_action.status must be planned, in_progress, completed, or skipped"
        )
    priority = _token(raw.get("priority") or "normal", field="weekly_action.priority")
    if priority not in {"low", "normal", "high", "urgent"}:
        raise LifeGraphError("weekly_action.priority must be low, normal, high, or urgent")
    return {
        "week_start": week_start.isoformat(),
        "definition_of_done": _text(
            raw.get("definition_of_done"), field="weekly_action.definition_of_done",
            limit=2_000, required=True, preserve_lines=True,
        ),
        "estimated_minutes": _integer(
            raw.get("estimated_minutes"), field="weekly_action.estimated_minutes",
            minimum=1, maximum=10_080,
        ),
        "priority": priority,
        "status": status,
    }


def validate_learning_career_properties(
    db,
    *,
    owner_id: str,
    value: object,
    current_entity_id: str | None = None,
    allow_deleted_entity_links: bool = False,
) -> dict[str, Any]:
    raw = _bounded_object(value, field="Learning/Career properties", max_bytes=64_000)
    allowed = {
        "learning_career_schema_version", "domain", "record_kind", "details",
        "source_links", "entity_links", "weekly_action",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported Learning/Career fields: {', '.join(unknown)}")
    if raw.get("learning_career_schema_version") != LEARNING_CAREER_SCHEMA_VERSION:
        raise LifeGraphError("Unsupported Learning/Career schema version")
    domain, record_kind = _domain_and_kind(raw.get("domain"), raw.get("record_kind"))
    properties = {
        "learning_career_schema_version": LEARNING_CAREER_SCHEMA_VERSION,
        "domain": domain,
        "record_kind": record_kind,
        "details": _details(domain, record_kind, raw.get("details")),
        "source_links": _source_links(
            db, owner_id=owner_id, value=raw.get("source_links")
        ),
        "entity_links": _entity_links(
            db,
            owner_id=owner_id,
            domain=domain,
            record_kind=record_kind,
            value=raw.get("entity_links"),
            current_entity_id=current_entity_id,
            allow_deleted_targets=allow_deleted_entity_links,
        ),
        "weekly_action": _weekly_action(
            domain, record_kind, raw.get("weekly_action")
        ),
    }
    _bounded_object(properties, field="Learning/Career properties", max_bytes=64_000)
    return properties


def is_typed_learning_career_payload(
    entity_type: object, properties: object | None = None
) -> bool:
    normalized_type = str(entity_type or "").strip().lower()
    if normalized_type in {LEARNING_ENTITY_TYPE, CAREER_ENTITY_TYPE}:
        return True
    if not isinstance(properties, Mapping):
        return False
    if properties.get("learning_career_schema_version") is not None:
        return True
    domain = str(properties.get("domain") or "").strip().lower()
    record_kind = str(properties.get("record_kind") or "").strip().lower()
    return domain in RECORD_KINDS and record_kind in RECORD_KINDS[domain]


def _normalize_provenance(
    value: object | None,
    *,
    domain: str,
    record_kind: str,
    source_links: list[Mapping[str, str]],
    strict_source_ids: bool,
) -> dict[str, Any]:
    provenance = _bounded_object(value, field="provenance", max_bytes=16_000)
    allowed = {
        "source_id", "source_ids", "capture", "observed_at", "note",
        "import_ref", "domain", "record_kind",
    }
    unknown = sorted(set(provenance) - allowed)
    if unknown:
        raise LifeGraphError(
            f"Unsupported Learning/Career provenance fields: {', '.join(unknown)}"
        )
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
    capture = _token(provenance.get("capture") or "manual", field="provenance.capture")
    if capture not in {"manual", "import", "email", "calendar", "document", "provider"}:
        raise LifeGraphError(
            "provenance.capture must be manual, import, email, calendar, document, or provider"
        )
    observed_at = _datetime(provenance.get("observed_at"), field="provenance.observed_at")
    note = _text(
        provenance.get("note"), field="provenance.note", limit=2_000,
        preserve_lines=True,
    )
    import_ref = _text(
        provenance.get("import_ref"), field="provenance.import_ref", limit=240
    )
    provenance["source_ids"] = source_ids
    provenance["domain"] = domain
    provenance["record_kind"] = record_kind
    provenance["capture"] = capture
    provenance["observed_at"] = (
        observed_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        if observed_at is not None else None
    )
    provenance["note"] = note
    provenance["import_ref"] = import_ref
    return provenance


def _owned_record(
    db, owner_id: str, entity_id: object, *, include_deleted: bool = False
) -> LifeEntity:
    try:
        entity = get_life_entity(
            db, owner_id=owner_id, entity_id=entity_id, include_deleted=include_deleted
        )
    except LifeGraphNotFound as exc:
        raise LifeGraphNotFound("Learning/Career record not found") from exc
    identity = _stored_identity(entity)
    if identity is None:
        raise LifeGraphNotFound("Learning/Career record not found")
    _validate_entity_authority(db, owner_id=owner_id, entity=entity)
    return entity


def _validate_entity_authority(db, *, owner_id: str, entity: LifeEntity) -> None:
    identity = _stored_identity(entity)
    if identity is None:
        raise LifeGraphNotFound("Learning/Career record not found")
    validate_learning_career_properties(
        db,
        owner_id=owner_id,
        value=entity.properties or {},
        current_entity_id=entity.id,
        allow_deleted_entity_links=True,
    )
    source_ids = {
        row["source_id"] for row in (entity.properties or {}).get("source_links", [])
    }
    provenance_ids = set((entity.provenance or {}).get("source_ids") or [])
    if source_ids != provenance_ids:
        raise LifeGraphError("Learning/Career provenance does not match source links")
    normalized_provenance = _normalize_provenance(
        entity.provenance or {},
        domain=identity[0],
        record_kind=identity[1],
        source_links=(entity.properties or {}).get("source_links", []),
        strict_source_ids=True,
    )
    if dict(entity.provenance or {}) != normalized_provenance:
        raise LifeGraphError("Learning/Career provenance is malformed")


def create_learning_career_record(
    db,
    *,
    account: Account,
    domain: object,
    record_kind: object,
    title: object,
    summary: object = "",
    details: object | None = None,
    source_links: object,
    entity_links: object | None = None,
    weekly_action: object | None = None,
    status: object = "active",
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    occurred_at: object | None = None,
    due_at: object | None = None,
    review_at: object | None = None,
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    normalized_domain, normalized_kind = _domain_and_kind(domain, record_kind)
    normalized_status = _token(status, field="status", limit=32)
    if normalized_status not in STATUSES:
        raise LifeGraphError("Unsupported Learning/Career status")
    normalized_sensitivity = _token(sensitivity, field="sensitivity", limit=24)
    if normalized_sensitivity not in SENSITIVITIES:
        raise LifeGraphError("Learning/Career sensitivity must be private or restricted")
    properties = validate_learning_career_properties(
        db,
        owner_id=account.id,
        value={
            "learning_career_schema_version": LEARNING_CAREER_SCHEMA_VERSION,
            "domain": normalized_domain,
            "record_kind": normalized_kind,
            "details": details or {},
            "source_links": source_links,
            "entity_links": entity_links or [],
            "weekly_action": weekly_action,
        },
    )
    normalized_provenance = _normalize_provenance(
        provenance,
        domain=normalized_domain,
        record_kind=normalized_kind,
        source_links=properties["source_links"],
        strict_source_ids=True,
    )
    return create_life_entity(
        db,
        account=account,
        entity_type=ENTITY_TYPES[normalized_domain],
        title=_text(title, field="title", limit=240, required=True),
        summary=_text(summary, field="summary", limit=20_000, preserve_lines=True),
        status=normalized_status,
        properties=properties,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=normalized_sensitivity,
        occurred_at=_datetime(occurred_at, field="occurred_at"),
        due_at=_datetime(due_at, field="due_at"),
        review_at=_datetime(review_at, field="review_at"),
        idempotency_key=idempotency_key,
        reason=f"{normalized_domain.title()} {normalized_kind} recorded",
    )


def update_learning_career_record(
    db,
    *,
    account: Account,
    entity_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    entity = _owned_record(db, account.id, entity_id)
    allowed = {
        "title", "summary", "details", "source_links", "entity_links",
        "weekly_action", "status", "provenance", "confidence", "sensitivity",
        "occurred_at", "due_at", "review_at",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported Learning/Career fields: {', '.join(unknown)}")
    current = dict(entity.properties or {})
    domain, record_kind = _domain_and_kind(current["domain"], current["record_kind"])
    merged = dict(current)
    for field in ("details", "source_links", "entity_links", "weekly_action"):
        if field in changes:
            merged[field] = changes[field]
    properties = validate_learning_career_properties(
        db,
        owner_id=account.id,
        value=merged,
        current_entity_id=entity.id,
        allow_deleted_entity_links="entity_links" not in changes,
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
        status = _token(changes["status"], field="status", limit=32)
        if status not in STATUSES:
            raise LifeGraphError("Unsupported Learning/Career status")
        entity_changes["status"] = status
    provenance_value: object = changes.get("provenance", dict(entity.provenance or {}))
    if "source_links" in changes and "provenance" not in changes:
        provenance_value = dict(entity.provenance or {})
        provenance_value.pop("source_id", None)
        provenance_value.pop("source_ids", None)
    entity_changes["provenance"] = _normalize_provenance(
        provenance_value,
        domain=domain,
        record_kind=record_kind,
        source_links=properties["source_links"],
        strict_source_ids="provenance" in changes,
    )
    if "confidence" in changes:
        entity_changes["confidence"] = changes["confidence"]
    if "sensitivity" in changes:
        sensitivity = _token(changes["sensitivity"], field="sensitivity", limit=24)
        if sensitivity not in SENSITIVITIES:
            raise LifeGraphError(
                "Learning/Career sensitivity must be private or restricted"
            )
        entity_changes["sensitivity"] = sensitivity
    for field in ("occurred_at", "due_at", "review_at"):
        if field in changes:
            entity_changes[field] = _datetime(changes[field], field=field)
    return update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=entity_changes,
        reason=f"{domain.title()} {record_kind} updated",
    )


def delete_learning_career_record(
    db,
    *,
    owner_id: str,
    entity_id: object,
    expected_version: int,
    reason: object = "Learning/Career record deleted",
) -> LifeEntity:
    entity = _owned_record(db, owner_id, entity_id)
    return delete_life_entity(
        db,
        owner_id=owner_id,
        entity_id=entity.id,
        expected_version=expected_version,
        reason=reason,
    )


def serialize_learning_career_record(entity: LifeEntity) -> dict[str, Any]:
    identity = _stored_identity(entity)
    if identity is None:
        raise LifeGraphError("Entity is not a typed Learning/Career record")
    payload = serialize_life_entity(entity)
    properties = dict(entity.properties or {})
    payload.update(properties)
    payload["execution_policy"] = {
        "record_only": True,
        "can_apply_or_submit": False,
        "can_send_messages": False,
        "can_execute_external_actions": False,
        "uses_model_inference": False,
    }
    return payload


def get_learning_career_record(
    db, *, owner_id: str, entity_id: object
) -> dict[str, Any]:
    return serialize_learning_career_record(_owned_record(db, owner_id, entity_id))


def _record_candidates(
    db,
    *,
    owner_id: str,
    domain: object | None = None,
    record_kind: object | None = None,
    status: object | None = None,
    limit: int = LEARNING_CAREER_SCAN_LIMIT,
) -> tuple[list[LifeEntity], bool]:
    normalized_domain = None
    if domain is not None:
        normalized_domain = _token(domain, field="domain", limit=16)
        if normalized_domain not in RECORD_KINDS:
            raise LifeGraphError("domain must be learning or career")
    normalized_kind = None
    if record_kind is not None:
        if normalized_domain is None:
            matches = [
                candidate_domain
                for candidate_domain, kinds in RECORD_KINDS.items()
                if str(record_kind).strip().lower().replace(" ", "_") in kinds
            ]
            if len(matches) != 1:
                raise LifeGraphError("domain is required when filtering by record_kind")
            normalized_domain = matches[0]
        _, normalized_kind = _domain_and_kind(normalized_domain, record_kind)
    normalized_status = None
    if status is not None:
        normalized_status = _token(status, field="status", limit=32)
        if normalized_status not in STATUSES:
            raise LifeGraphError("Unsupported Learning/Career status")
    bounded = max(1, min(LEARNING_CAREER_SCAN_LIMIT, int(limit)))
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type.in_((LEARNING_ENTITY_TYPE, CAREER_ENTITY_TYPE)),
        LifeEntity.deleted_at.is_(None),
    )
    if normalized_domain:
        query = query.filter(LifeEntity.entity_type == ENTITY_TYPES[normalized_domain])
    if normalized_status:
        query = query.filter(LifeEntity.status == normalized_status)
    rows = query.order_by(
        LifeEntity.updated_at.desc(), LifeEntity.id.desc()
    ).limit(bounded + 1).all()
    result: list[LifeEntity] = []
    for entity in rows[:bounded]:
        identity = _stored_identity(entity)
        if identity is None:
            continue
        if normalized_kind and identity != (normalized_domain, normalized_kind):
            continue
        _validate_entity_authority(db, owner_id=owner_id, entity=entity)
        result.append(entity)
    return result, len(rows) > bounded


def list_learning_career_records(
    db,
    *,
    owner_id: str,
    domain: object | None = None,
    record_kind: object | None = None,
    status: object | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    bounded = max(1, min(100, int(limit)))
    rows, scan_truncated = _record_candidates(
        db,
        owner_id=owner_id,
        domain=domain,
        record_kind=record_kind,
        status=status,
        limit=LEARNING_CAREER_SCAN_LIMIT,
    )
    return [serialize_learning_career_record(row) for row in rows[:bounded]], (
        scan_truncated or len(rows) > bounded
    )


def search_learning_career_records(
    db,
    *,
    owner_id: str,
    query_text: object,
    domain: object | None = None,
    record_kind: object | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    needle = str(query_text or "").strip().casefold()
    if not needle:
        raise LifeGraphError("q is required")
    bounded = max(1, min(100, int(limit)))
    rows, scan_truncated = _record_candidates(
        db,
        owner_id=owner_id,
        domain=domain,
        record_kind=record_kind,
        limit=LEARNING_CAREER_SCAN_LIMIT,
    )
    matches: list[tuple[tuple[int, str, str], dict[str, Any]]] = []
    for entity in rows:
        record = serialize_learning_career_record(entity)
        title = record["title"].casefold()
        summary = record["summary"].casefold()
        structured = json.dumps(
            {
                "details": record["details"],
                "source_links": record["source_links"],
                "entity_links": record["entity_links"],
            },
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
        elif needle in structured:
            rank, field = 4, "record"
        else:
            continue
        matches.append(((rank, title, entity.id), {
            "record": record, "match": field, "rank": rank,
        }))
    matches.sort(key=lambda item: item[0])
    items = [item for _, item in matches[:bounded]]
    return {
        "items": items,
        "count": len(items),
        "scanned": len(rows),
        "truncated": scan_truncated or len(matches) > bounded,
    }


def learning_career_history(
    db, *, owner_id: str, entity_id: object, limit: int = 50
) -> tuple[list[dict[str, Any]], bool]:
    entity = _owned_record(db, owner_id, entity_id, include_deleted=True)
    rows, truncated = list_life_entity_versions(
        db, owner_id=owner_id, entity_id=entity.id, limit=limit
    )
    return [serialize_life_entity_version(row) for row in rows], truncated


def _chain_item(entity: LifeEntity) -> dict[str, Any]:
    properties = entity.properties or {}
    return {
        "id": entity.id,
        "domain": properties["domain"],
        "record_kind": properties["record_kind"],
        "title": entity.title or "",
        "summary": entity.summary or "",
        "status": entity.status,
        "version": int(entity.version or 1),
        "source_ids": sorted({
            row["source_id"] for row in properties.get("source_links", [])
        }),
        "due_at": serialize_life_entity(entity)["due_at"],
        "weekly_action": properties.get("weekly_action"),
    }


def career_learning_plan(
    db,
    *,
    owner_id: str,
    career_target_id: object,
    week_start: object,
) -> dict[str, Any]:
    """Build a deterministic, source-backed career-to-weekly-action chain."""

    target = _owned_record(db, owner_id, career_target_id)
    target_identity = _stored_identity(target)
    if target_identity not in CHAIN_TARGETS["requires_capability"][0]:
        raise LifeGraphError(
            "career_target_id must reference a role, application, company, or university"
        )
    normalized_week = _date(week_start, field="week_start", required=True)
    assert normalized_week is not None
    if normalized_week.weekday() != 0:
        raise LifeGraphError("week_start must be a Monday")

    rows, truncated = _record_candidates(
        db, owner_id=owner_id, limit=LEARNING_CAREER_SCAN_LIMIT
    )
    by_id = {entity.id: entity for entity in rows}

    def linked(entity: LifeEntity, relation: str) -> list[LifeEntity]:
        result = []
        for link in (entity.properties or {}).get("entity_links", []):
            if link.get("relation") != relation:
                continue
            candidate = by_id.get(str(link.get("entity_id") or ""))
            if candidate is not None:
                result.append(candidate)
        return sorted(result, key=lambda row: ((row.title or "").casefold(), row.id))

    chains: list[dict[str, Any]] = []
    seen_chain_keys: set[tuple[str, ...]] = set()
    for capability in linked(target, "requires_capability"):
        for gap in linked(capability, "has_gap"):
            for plan in linked(gap, "addressed_by"):
                for portfolio in linked(plan, "evidenced_by"):
                    actions = linked(portfolio, "weekly_action")
                    matching_actions = [
                        action
                        for action in actions
                        if (action.properties or {}).get("weekly_action", {}).get(
                            "week_start"
                        ) == normalized_week.isoformat()
                    ]
                    if not matching_actions:
                        key = (capability.id, gap.id, plan.id, portfolio.id, "")
                        if key not in seen_chain_keys:
                            seen_chain_keys.add(key)
                            chains.append({
                                "capability": _chain_item(capability),
                                "gap": _chain_item(gap),
                                "learning_plan": _chain_item(plan),
                                "portfolio": _chain_item(portfolio),
                                "weekly_action": None,
                                "complete": False,
                            })
                    for action in matching_actions:
                        key = (capability.id, gap.id, plan.id, portfolio.id, action.id)
                        if key in seen_chain_keys:
                            continue
                        seen_chain_keys.add(key)
                        chains.append({
                            "capability": _chain_item(capability),
                            "gap": _chain_item(gap),
                            "learning_plan": _chain_item(plan),
                            "portfolio": _chain_item(portfolio),
                            "weekly_action": _chain_item(action),
                            "complete": True,
                        })
                    if len(chains) >= LEARNING_CAREER_CHAIN_LIMIT:
                        break
                if len(chains) >= LEARNING_CAREER_CHAIN_LIMIT:
                    break
            if len(chains) >= LEARNING_CAREER_CHAIN_LIMIT:
                break
        if len(chains) >= LEARNING_CAREER_CHAIN_LIMIT:
            break

    complete_count = sum(1 for row in chains if row["complete"])
    return {
        "career_target": _chain_item(target),
        "week_start": normalized_week.isoformat(),
        "method": "explicit_owner_scoped_links_v1",
        "uses_model_inference": False,
        "chains": chains,
        "chain_count": len(chains),
        "complete_chain_count": complete_count,
        "truncated": truncated or len(chains) >= LEARNING_CAREER_CHAIN_LIMIT,
        "coverage": {
            "has_capability": bool(linked(target, "requires_capability")),
            "has_complete_weekly_chain": complete_count > 0,
            "all_records_source_backed": bool(chains) and bool(
                _chain_item(target)["source_ids"]
            ) and all(
                bool(item["source_ids"])
                for chain in chains
                for item in (
                    chain["capability"], chain["gap"], chain["learning_plan"],
                    chain["portfolio"], chain["weekly_action"],
                )
                if item is not None
            ),
        },
        "execution_policy": {
            "record_only": True,
            "can_apply_or_submit": False,
            "can_send_messages": False,
            "can_execute_external_actions": False,
        },
    }


__all__ = [
    "CAREER_RECORD_KINDS",
    "LEARNING_RECORD_KINDS",
    "career_learning_plan",
    "create_learning_career_record",
    "delete_learning_career_record",
    "get_learning_career_record",
    "is_typed_learning_career_payload",
    "learning_career_history",
    "list_learning_career_records",
    "search_learning_career_records",
    "serialize_learning_career_record",
    "update_learning_career_record",
    "validate_learning_career_properties",
]
