"""Typed V3 Journal & Reflection authority on Restia's canonical Life graph.

Journal entries are encrypted, Account.id-owned ``LifeEntity`` rows.  The
service validates a private bounded schema and produces deterministic period
reviews from explicit structured fields.  It never invokes a model, schedules
an action, or performs an external mutation.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Mapping

from core.database import Account, LifeEntity, LifeSource
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


JOURNAL_SCHEMA_VERSION = 1
JOURNAL_ENTITY_TYPE = "journal_entry"
JOURNAL_SCAN_LIMIT = 1_000
JOURNAL_MAX_REPORT_ENTRIES = 1_000

JOURNAL_LIST_FIELDS = (
    "moments",
    "wins",
    "difficulties",
    "lessons",
    "gratitude",
    "ideas",
    "decisions",
    "principles",
    "time_notes",
    "relationship_notes",
    "goal_progress",
    "next_changes",
    "changes",
    "improvements",
)
JOURNAL_REVIEW_PERIODS = frozenset({"weekly", "monthly", "annual"})
JOURNAL_STATUSES = frozenset({"active", "archived"})
JOURNAL_SENSITIVITIES = frozenset({"private", "restricted"})
PROMISE_STATUSES = frozenset({"open", "kept", "broken", "cancelled"})

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_URL_CREDENTIAL_RE = re.compile(
    r"^[a-z][a-z0-9+.-]*://[^/@\s]+:[^/@\s]+@", re.IGNORECASE
)
_BEARER_RE = re.compile(r"\bauthorization\s*:\s*bearer\s+\S+", re.IGNORECASE)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "passcode",
    "secret",
    "token",
    "credential",
    "cookie",
    "authorization",
    "api_key",
    "private_key",
    "seed_phrase",
    "recovery_phrase",
)
_ACTION_KEYS = frozenset({
    "action",
    "execute",
    "executor",
    "tool_call",
    "external_action",
    "webhook",
    "send_message",
    "send_email",
    "payment",
    "purchase",
    "automation",
    "command",
    "shell",
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
    if _URL_CREDENTIAL_RE.search(normalized):
        raise LifeGraphError(f"{field} must not contain embedded credentials")
    if _BEARER_RE.search(normalized) or _PRIVATE_KEY_RE.search(normalized):
        raise LifeGraphError(f"{field} must not contain credentials")
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


def _bounded_json(value: object | None, *, field: str, max_bytes: int) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LifeGraphError(f"{field} must be an object")
    _assert_private_record(value, field=field)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise LifeGraphError(f"{field} must be JSON-serializable") from exc
    if len(encoded) > max_bytes:
        raise LifeGraphError(f"{field} must not exceed {max_bytes} bytes")
    return dict(value)


def _assert_private_record(value: object, *, field: str = "journal entry") -> None:
    """Reject credential containers and executable/external action payloads."""

    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key or "").strip().lower().replace("-", "_")
            if any(part in key for part in _SECRET_KEY_PARTS):
                raise LifeGraphError(f"{field} must not contain credentials")
            if key in _ACTION_KEYS:
                raise LifeGraphError(f"{field} must not contain action payloads")
            _assert_private_record(nested, field=field)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            _assert_private_record(nested, field=field)
        return
    if isinstance(value, str):
        _text(value, field=field, limit=20_000, preserve_lines=True)


def _score(value: object | None, *, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise LifeGraphError(f"{field} must be an integer from 0 to 10")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise LifeGraphError(f"{field} must be an integer from 0 to 10") from exc
    if result < 0 or result > 10:
        raise LifeGraphError(f"{field} must be an integer from 0 to 10")
    return result


def _mood(value: object | None) -> dict[str, Any]:
    if value is None:
        return {"label": "", "score": None, "energy": None}
    if not isinstance(value, Mapping):
        raise LifeGraphError("mood must be an object")
    unknown = sorted(set(value) - {"label", "score", "energy"})
    if unknown:
        raise LifeGraphError(f"Unsupported mood fields: {', '.join(unknown)}")
    return {
        "label": _text(value.get("label", ""), field="mood.label", limit=100),
        "score": _score(value.get("score"), field="mood.score"),
        "energy": _score(value.get("energy"), field="mood.energy"),
    }


def _string_list(
    value: object | None,
    *,
    field: str,
    max_items: int = 50,
    item_limit: int = 2_000,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LifeGraphError(f"{field} must be a list")
    if len(value) > max_items:
        raise LifeGraphError(f"{field} must not contain more than {max_items} items")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(
            item, field=field, limit=item_limit, required=True, preserve_lines=True
        )
        marker = text.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        result.append(text)
    return result


def _pattern_tags(value: object | None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LifeGraphError("pattern_tags must be a list")
    if len(value) > 50:
        raise LifeGraphError("pattern_tags must not contain more than 50 items")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        tag = _token(item, field="pattern tag", limit=64)
        if tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


def _promises(value: object | None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LifeGraphError("promises must be a list")
    if len(value) > 50:
        raise LifeGraphError("promises must not contain more than 50 items")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        if isinstance(item, str):
            raw: Mapping[str, Any] = {"text": item}
        elif isinstance(item, Mapping):
            raw = item
        else:
            raise LifeGraphError("each promise must be a string or object")
        unknown = sorted(
            set(raw) - {"id", "text", "status", "due_date", "completed_on", "note"}
        )
        if unknown:
            raise LifeGraphError(f"Unsupported promise fields: {', '.join(unknown)}")
        promise_id = _token(raw.get("id") or f"promise_{index}", field="promise.id")
        if promise_id in seen:
            raise LifeGraphError("promise ids must be unique within an entry")
        seen.add(promise_id)
        status = _token(raw.get("status") or "open", field="promise.status")
        if status not in PROMISE_STATUSES:
            raise LifeGraphError("promise.status must be open, kept, broken, or cancelled")
        due_date = _date(raw.get("due_date"), field="promise.due_date")
        completed_on = _date(raw.get("completed_on"), field="promise.completed_on")
        if status == "open" and completed_on is not None:
            raise LifeGraphError("open promises cannot have completed_on")
        if status in {"kept", "broken"} and completed_on is None:
            raise LifeGraphError(f"{status} promises require completed_on")
        result.append({
            "id": promise_id,
            "text": _text(
                raw.get("text"), field="promise.text", limit=2_000,
                required=True, preserve_lines=True,
            ),
            "status": status,
            "due_date": due_date.isoformat() if due_date else None,
            "completed_on": completed_on.isoformat() if completed_on else None,
            "note": _text(
                raw.get("note", ""), field="promise.note", limit=2_000,
                preserve_lines=True,
            ),
        })
    return result


def _evidence(value: object | None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LifeGraphError("evidence must be a list")
    if len(value) > 50:
        raise LifeGraphError("evidence must not contain more than 50 items")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        if isinstance(item, str):
            raw: Mapping[str, Any] = {"label": item}
        elif isinstance(item, Mapping):
            raw = item
        else:
            raise LifeGraphError("each evidence item must be a string or object")
        unknown = sorted(set(raw) - {"id", "label", "source_id", "entity_id", "reference"})
        if unknown:
            raise LifeGraphError(f"Unsupported evidence fields: {', '.join(unknown)}")
        evidence_id = _token(raw.get("id") or f"evidence_{index}", field="evidence.id")
        if evidence_id in seen:
            raise LifeGraphError("evidence ids must be unique within an entry")
        seen.add(evidence_id)
        source_id = str(raw.get("source_id") or "").strip() or None
        entity_id = str(raw.get("entity_id") or "").strip() or None
        if source_id and len(source_id) > 36:
            raise LifeGraphError("evidence.source_id must not exceed 36 characters")
        if entity_id and len(entity_id) > 36:
            raise LifeGraphError("evidence.entity_id must not exceed 36 characters")
        result.append({
            "id": evidence_id,
            "label": _text(
                raw.get("label"), field="evidence.label", limit=1_000, required=True
            ),
            "source_id": source_id,
            "entity_id": entity_id,
            "reference": _text(
                raw.get("reference", ""), field="evidence.reference", limit=2_000
            ),
        })
    return result


def validate_journal_properties(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LifeGraphError("journal properties must be an object")
    allowed = {
        "journal_schema_version",
        "entry_date",
        "mood",
        *JOURNAL_LIST_FIELDS,
        "promises",
        "pattern_tags",
        "evidence",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported journal fields: {', '.join(unknown)}")
    version = value.get("journal_schema_version", JOURNAL_SCHEMA_VERSION)
    if version != JOURNAL_SCHEMA_VERSION:
        raise LifeGraphError("Unsupported journal schema version")
    entry_date = _date(value.get("entry_date"), field="entry_date", required=True)
    assert entry_date is not None
    normalized: dict[str, Any] = {
        "journal_schema_version": JOURNAL_SCHEMA_VERSION,
        "entry_date": entry_date.isoformat(),
        "mood": _mood(value.get("mood")),
    }
    for field in JOURNAL_LIST_FIELDS:
        normalized[field] = _string_list(value.get(field), field=field)
    normalized["promises"] = _promises(value.get("promises"))
    normalized["pattern_tags"] = _pattern_tags(value.get("pattern_tags"))
    normalized["evidence"] = _evidence(value.get("evidence"))
    _assert_private_record(normalized)
    encoded = json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise LifeGraphError("journal properties must not exceed 65536 bytes")
    return normalized


def is_typed_journal_payload(entity_type: object, properties: object | None = None) -> bool:
    normalized_type = str(entity_type or "").strip().lower()
    return normalized_type == JOURNAL_ENTITY_TYPE or (
        isinstance(properties, Mapping)
        and properties.get("journal_schema_version") is not None
    )


def _validate_evidence_authority(
    db, *, owner_id: str, evidence: Iterable[Mapping[str, Any]]
) -> None:
    source_ids = {str(row["source_id"]) for row in evidence if row.get("source_id")}
    if source_ids:
        owned_sources = {
            row[0]
            for row in db.query(LifeSource.id).filter(
                LifeSource.owner_id == owner_id,
                LifeSource.id.in_(source_ids),
            ).all()
        }
        if owned_sources != source_ids:
            raise LifeGraphNotFound("Journal evidence source not found")
    entity_ids = {str(row["entity_id"]) for row in evidence if row.get("entity_id")}
    if entity_ids:
        owned_entities = {
            row[0]
            for row in db.query(LifeEntity.id).filter(
                LifeEntity.owner_id == owner_id,
                LifeEntity.id.in_(entity_ids),
                LifeEntity.deleted_at.is_(None),
            ).all()
        }
        if owned_entities != entity_ids:
            raise LifeGraphNotFound("Journal evidence entity not found")


def _provenance(value: object | None) -> dict[str, Any]:
    provenance = _bounded_json(value, field="provenance", max_bytes=16_000)
    provenance.setdefault("capture", "manual")
    provenance["domain"] = "journal"
    return provenance


def _owned_journal_entry(
    db, owner_id: str, entity_id: object, *, include_deleted: bool = False
) -> LifeEntity:
    entity = get_life_entity(
        db, owner_id=owner_id, entity_id=entity_id, include_deleted=include_deleted
    )
    if entity.entity_type != JOURNAL_ENTITY_TYPE:
        raise LifeGraphNotFound("Journal entry not found")
    validate_journal_properties(entity.properties or {})
    return entity


def create_journal_entry(
    db,
    *,
    account: Account,
    title: object,
    entry_date: object,
    body: object = "",
    mood: object | None = None,
    moments: object | None = None,
    wins: object | None = None,
    difficulties: object | None = None,
    lessons: object | None = None,
    gratitude: object | None = None,
    ideas: object | None = None,
    decisions: object | None = None,
    principles: object | None = None,
    time_notes: object | None = None,
    relationship_notes: object | None = None,
    goal_progress: object | None = None,
    next_changes: object | None = None,
    promises: object | None = None,
    changes: object | None = None,
    improvements: object | None = None,
    pattern_tags: object | None = None,
    evidence: object | None = None,
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    normalized_title = _text(title, field="title", limit=240, required=True)
    normalized_body = _text(
        body, field="body", limit=20_000, preserve_lines=True
    )
    normalized_sensitivity = _token(sensitivity, field="sensitivity", limit=24)
    if normalized_sensitivity not in JOURNAL_SENSITIVITIES:
        raise LifeGraphError("Journal sensitivity must be private or restricted")
    properties = validate_journal_properties({
        "entry_date": entry_date,
        "mood": mood,
        "moments": moments,
        "wins": wins,
        "difficulties": difficulties,
        "lessons": lessons,
        "gratitude": gratitude,
        "ideas": ideas,
        "decisions": decisions,
        "principles": principles,
        "time_notes": time_notes,
        "relationship_notes": relationship_notes,
        "goal_progress": goal_progress,
        "next_changes": next_changes,
        "promises": promises,
        "changes": changes,
        "improvements": improvements,
        "pattern_tags": pattern_tags,
        "evidence": evidence,
    })
    _validate_evidence_authority(
        db, owner_id=account.id, evidence=properties["evidence"]
    )
    normalized_provenance = _provenance(provenance)
    parsed_date = _date(entry_date, field="entry_date", required=True)
    assert parsed_date is not None
    return create_life_entity(
        db,
        account=account,
        entity_type=JOURNAL_ENTITY_TYPE,
        title=normalized_title,
        summary=normalized_body,
        status="active",
        properties=properties,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=normalized_sensitivity,
        occurred_at=datetime.combine(parsed_date, time.min),
        idempotency_key=idempotency_key,
        reason="Journal entry captured",
    )


def update_journal_entry(
    db,
    *,
    account: Account,
    entity_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    entity = _owned_journal_entry(db, account.id, entity_id)
    allowed = {
        "title",
        "entry_date",
        "body",
        "mood",
        *JOURNAL_LIST_FIELDS,
        "promises",
        "pattern_tags",
        "evidence",
        "provenance",
        "confidence",
        "sensitivity",
        "status",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported journal fields: {', '.join(unknown)}")
    current = validate_journal_properties(entity.properties or {})
    merged = dict(current)
    for field in (
        "entry_date",
        "mood",
        *JOURNAL_LIST_FIELDS,
        "promises",
        "pattern_tags",
        "evidence",
    ):
        if field in changes:
            merged[field] = changes[field]
    properties = validate_journal_properties(merged)
    _validate_evidence_authority(
        db, owner_id=account.id, evidence=properties["evidence"]
    )
    entity_changes: dict[str, Any] = {"properties": properties}
    if "title" in changes:
        entity_changes["title"] = _text(
            changes["title"], field="title", limit=240, required=True
        )
    if "body" in changes:
        entity_changes["summary"] = _text(
            changes["body"], field="body", limit=20_000, preserve_lines=True
        )
    if "entry_date" in changes:
        parsed_date = _date(changes["entry_date"], field="entry_date", required=True)
        assert parsed_date is not None
        entity_changes["occurred_at"] = datetime.combine(parsed_date, time.min)
    if "provenance" in changes:
        entity_changes["provenance"] = _provenance(changes["provenance"])
    if "confidence" in changes:
        entity_changes["confidence"] = changes["confidence"]
    if "sensitivity" in changes:
        sensitivity = _token(changes["sensitivity"], field="sensitivity", limit=24)
        if sensitivity not in JOURNAL_SENSITIVITIES:
            raise LifeGraphError("Journal sensitivity must be private or restricted")
        entity_changes["sensitivity"] = sensitivity
    if "status" in changes:
        status = _token(changes["status"], field="status", limit=32)
        if status not in JOURNAL_STATUSES:
            raise LifeGraphError("Journal status must be active or archived")
        entity_changes["status"] = status
    return update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=entity_changes,
        reason="Journal entry updated",
    )


def delete_journal_entry(
    db,
    *,
    owner_id: str,
    entity_id: object,
    expected_version: int,
    reason: object = "Journal entry deleted",
) -> LifeEntity:
    entity = _owned_journal_entry(db, owner_id, entity_id)
    return delete_life_entity(
        db,
        owner_id=owner_id,
        entity_id=entity.id,
        expected_version=expected_version,
        reason=reason,
    )


def serialize_journal_entry(entity: LifeEntity) -> dict[str, Any]:
    if entity.entity_type != JOURNAL_ENTITY_TYPE:
        raise LifeGraphError("Entity is not a journal entry")
    properties = validate_journal_properties(entity.properties or {})
    payload = serialize_life_entity(entity)
    payload.update(properties)
    payload["body"] = payload["summary"]
    payload["execution_policy"] = {
        "record_only": True,
        "can_execute_external_actions": False,
        "uses_model_inference": False,
    }
    return payload


def get_journal_entry(db, *, owner_id: str, entity_id: object) -> dict[str, Any]:
    return serialize_journal_entry(_owned_journal_entry(db, owner_id, entity_id))


def _journal_candidates(
    db,
    *,
    owner_id: str,
    from_date: object | None = None,
    to_date: object | None = None,
    status: object | None = None,
    limit: int = JOURNAL_SCAN_LIMIT,
) -> tuple[list[LifeEntity], bool]:
    start = _date(from_date, field="from_date")
    end = _date(to_date, field="to_date")
    if start and end and start > end:
        raise LifeGraphError("from_date must not be after to_date")
    normalized_status = None
    if status:
        normalized_status = _token(status, field="status", limit=32)
        if normalized_status not in JOURNAL_STATUSES:
            raise LifeGraphError("Journal status must be active or archived")
    bounded = max(1, min(JOURNAL_SCAN_LIMIT, int(limit)))
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == JOURNAL_ENTITY_TYPE,
        LifeEntity.deleted_at.is_(None),
    )
    if start is not None:
        query = query.filter(LifeEntity.occurred_at >= datetime.combine(start, time.min))
    if end is not None:
        query = query.filter(
            LifeEntity.occurred_at < datetime.combine(end + timedelta(days=1), time.min)
        )
    if normalized_status:
        query = query.filter(LifeEntity.status == normalized_status)
    rows = query.order_by(
        LifeEntity.occurred_at.desc(), LifeEntity.updated_at.desc(), LifeEntity.id.desc()
    ).limit(bounded + 1).all()
    result: list[LifeEntity] = []
    for row in rows[:bounded]:
        try:
            validate_journal_properties(row.properties or {})
        except LifeGraphError:
            continue
        result.append(row)
    return result, len(rows) > bounded


def list_journal_entries(
    db,
    *,
    owner_id: str,
    from_date: object | None = None,
    to_date: object | None = None,
    status: object | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    bounded = max(1, min(100, int(limit)))
    rows, scan_truncated = _journal_candidates(
        db,
        owner_id=owner_id,
        from_date=from_date,
        to_date=to_date,
        status=status,
        limit=JOURNAL_SCAN_LIMIT,
    )
    return [serialize_journal_entry(row) for row in rows[:bounded]], (
        scan_truncated or len(rows) > bounded
    )


def search_journal_entries(
    db,
    *,
    owner_id: str,
    query_text: object,
    from_date: object | None = None,
    to_date: object | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    needle = str(query_text or "").strip().casefold()
    if not needle:
        raise LifeGraphError("q is required")
    bounded = max(1, min(100, int(limit)))
    rows, scan_truncated = _journal_candidates(
        db,
        owner_id=owner_id,
        from_date=from_date,
        to_date=to_date,
        limit=JOURNAL_SCAN_LIMIT,
    )
    matches: list[tuple[tuple[int, str, str], dict[str, Any]]] = []
    for entity in rows:
        entry = serialize_journal_entry(entity)
        title = entry["title"].casefold()
        body = entry["body"].casefold()
        structured = json.dumps(
            {key: entry[key] for key in (*JOURNAL_LIST_FIELDS, "promises", "pattern_tags")},
            ensure_ascii=False,
            sort_keys=True,
        ).casefold()
        if title == needle:
            rank, field = 0, "title"
        elif title.startswith(needle):
            rank, field = 1, "title"
        elif needle in title:
            rank, field = 2, "title"
        elif needle in body:
            rank, field = 3, "body"
        elif needle in structured:
            rank, field = 4, "reflection"
        else:
            continue
        matches.append(((rank, title, entity.id), {
            "entry": entry,
            "match": field,
            "rank": rank,
        }))
    matches.sort(key=lambda item: item[0])
    items = [item for _, item in matches[:bounded]]
    return {
        "items": items,
        "count": len(items),
        "scanned": len(rows),
        "truncated": scan_truncated or len(matches) > bounded,
    }


def journal_entry_history(
    db, *, owner_id: str, entity_id: object, limit: int = 50
) -> tuple[list[dict[str, Any]], bool]:
    entity = _owned_journal_entry(
        db, owner_id, entity_id, include_deleted=True
    )
    rows, truncated = list_life_entity_versions(
        db, owner_id=owner_id, entity_id=entity.id, limit=limit
    )
    chronological = list(reversed(rows))
    result: list[dict[str, Any]] = []
    previous: Mapping[str, Any] | None = None
    for row in chronological:
        snapshot = dict(row.snapshot or {})
        properties = validate_journal_properties(snapshot.get("properties") or {})
        changed: list[str] = []
        if previous is not None:
            previous_properties = validate_journal_properties(
                previous.get("properties") or {}
            )
            for field in (
                "title", "summary", "status", "occurred_at", "confidence",
                "sensitivity", "provenance",
            ):
                if previous.get(field) != snapshot.get(field):
                    changed.append("body" if field == "summary" else field)
            for field in ("mood", *JOURNAL_LIST_FIELDS, "promises", "pattern_tags", "evidence"):
                if previous_properties.get(field) != properties.get(field):
                    changed.append(field)
        result.append({
            "id": row.id,
            "version": int(row.version),
            "created_at": row.created_at.replace(
                tzinfo=timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "reason": row.reason or "",
            "kind": "created" if previous is None else "journal_entry_changed",
            "changed_fields": changed,
            "entry": {
                "title": snapshot.get("title") or "",
                "status": snapshot.get("status") or "active",
                "entry_date": properties["entry_date"],
                "mood": properties["mood"],
                "promise_count": len(properties["promises"]),
            },
        })
        previous = snapshot
    return list(reversed(result)), truncated


def _period_bounds(period: object, anchor_date: object) -> tuple[str, date, date]:
    normalized = _token(period, field="period", limit=16)
    if normalized not in JOURNAL_REVIEW_PERIODS:
        raise LifeGraphError("period must be weekly, monthly, or annual")
    anchor = _date(anchor_date, field="anchor_date", required=True)
    assert anchor is not None
    if normalized == "weekly":
        start = anchor - timedelta(days=anchor.weekday())
        exclusive_end = start + timedelta(days=7)
    elif normalized == "monthly":
        start = anchor.replace(day=1)
        if start.month == 12:
            exclusive_end = date(start.year + 1, 1, 1)
        else:
            exclusive_end = date(start.year, start.month + 1, 1)
    else:
        start = date(anchor.year, 1, 1)
        exclusive_end = date(anchor.year + 1, 1, 1)
    return normalized, start, exclusive_end


def _aggregate_explicit_items(
    entries: Iterable[dict[str, Any]], field: str
) -> list[dict[str, Any]]:
    occurrences: dict[str, dict[str, Any]] = {}
    for entry in entries:
        for text in entry[field]:
            marker = text.casefold()
            row = occurrences.setdefault(marker, {
                "text": text,
                "occurrences": 0,
                "entry_ids": [],
                "entry_dates": [],
            })
            row["occurrences"] += 1
            row["entry_ids"].append(entry["id"])
            row["entry_dates"].append(entry["entry_date"])
    return sorted(
        occurrences.values(),
        key=lambda row: (-row["occurrences"], row["text"].casefold()),
    )


def journal_period_review(
    db,
    *,
    owner_id: str,
    period: object,
    anchor_date: object,
) -> dict[str, Any]:
    normalized_period, start, exclusive_end = _period_bounds(period, anchor_date)
    rows, truncated = _journal_candidates(
        db,
        owner_id=owner_id,
        from_date=start,
        to_date=exclusive_end - timedelta(days=1),
        limit=JOURNAL_MAX_REPORT_ENTRIES,
    )
    entries = [serialize_journal_entry(row) for row in reversed(rows)]

    mood_scores = [
        entry["mood"]["score"] for entry in entries
        if entry["mood"]["score"] is not None
    ]
    energy_scores = [
        entry["mood"]["energy"] for entry in entries
        if entry["mood"]["energy"] is not None
    ]
    mood_labels = Counter(
        entry["mood"]["label"].casefold() for entry in entries
        if entry["mood"]["label"]
    )

    repeated: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        for tag in entry["pattern_tags"]:
            key = ("pattern_tag", tag.casefold())
            item = repeated.setdefault(key, {
                "pattern": tag,
                "source_field": "pattern_tags",
                "occurrences": 0,
                "entry_ids": [],
                "entry_dates": [],
            })
            item["occurrences"] += 1
            item["entry_ids"].append(entry["id"])
            item["entry_dates"].append(entry["entry_date"])
        for field in ("wins", "difficulties", "lessons", "gratitude", "ideas", "decisions"):
            for text in entry[field]:
                key = (field, text.casefold())
                item = repeated.setdefault(key, {
                    "pattern": text,
                    "source_field": field,
                    "occurrences": 0,
                    "entry_ids": [],
                    "entry_dates": [],
                })
                item["occurrences"] += 1
                item["entry_ids"].append(entry["id"])
                item["entry_dates"].append(entry["entry_date"])

    # Include carry-in promises created before this review period and still
    # explicitly open as of its end. This remains bounded and never infers
    # completion from prose.
    commitment_rows, commitment_truncated = _journal_candidates(
        db,
        owner_id=owner_id,
        to_date=exclusive_end - timedelta(days=1),
        limit=JOURNAL_MAX_REPORT_ENTRIES,
    )
    unfinished: list[dict[str, Any]] = []
    for row in reversed(commitment_rows):
        entry = serialize_journal_entry(row)
        for promise in entry["promises"]:
            if promise["status"] != "open":
                continue
            unfinished.append({
                **promise,
                "entry_id": entry["id"],
                "entry_date": entry["entry_date"],
                "carried_in": date.fromisoformat(entry["entry_date"]) < start,
                "overdue_at_period_end": bool(
                    promise["due_date"]
                    and date.fromisoformat(promise["due_date"]) < exclusive_end
                ),
            })
    unfinished.sort(key=lambda item: (
        item["due_date"] is None,
        item["due_date"] or "9999-12-31",
        item["entry_date"],
        item["id"],
    ))

    evidence: list[dict[str, Any]] = []
    for entry in entries:
        for item in entry["evidence"]:
            evidence.append({
                **item,
                "entry_id": entry["id"],
                "entry_date": entry["entry_date"],
            })

    return {
        "period": normalized_period,
        "period_start": start.isoformat(),
        "period_end": (exclusive_end - timedelta(days=1)).isoformat(),
        "method": "deterministic_structured_fields_v1",
        "uses_model_inference": False,
        "entry_count": len(entries),
        "entry_ids": [entry["id"] for entry in entries],
        "mood": {
            "score_count": len(mood_scores),
            "average_score": (
                round(sum(mood_scores) / len(mood_scores), 2) if mood_scores else None
            ),
            "score_change": (
                mood_scores[-1] - mood_scores[0] if len(mood_scores) >= 2 else None
            ),
            "energy_count": len(energy_scores),
            "average_energy": (
                round(sum(energy_scores) / len(energy_scores), 2)
                if energy_scores else None
            ),
            "labels": [
                {"label": label, "count": count}
                for label, count in sorted(
                    mood_labels.items(), key=lambda item: (-item[1], item[0])
                )
            ],
        },
        "changes": _aggregate_explicit_items(entries, "changes"),
        "improvements": _aggregate_explicit_items(entries, "improvements"),
        "principles": _aggregate_explicit_items(entries, "principles"),
        "time": _aggregate_explicit_items(entries, "time_notes"),
        "relationships": _aggregate_explicit_items(entries, "relationship_notes"),
        "goal_progress": _aggregate_explicit_items(entries, "goal_progress"),
        "next_changes": _aggregate_explicit_items(entries, "next_changes"),
        "repeated_patterns": sorted(
            (item for item in repeated.values() if item["occurrences"] >= 2),
            key=lambda item: (
                -item["occurrences"], item["source_field"], item["pattern"].casefold()
            ),
        ),
        "unfinished_commitments": unfinished,
        "evidence": evidence,
        "coverage": {
            field: sum(len(entry[field]) for entry in entries)
            for field in (*JOURNAL_LIST_FIELDS, "promises", "pattern_tags", "evidence")
        },
        "truncated": truncated or commitment_truncated,
        "execution_policy": {
            "record_only": True,
            "can_execute_external_actions": False,
        },
    }
