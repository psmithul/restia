"""Typed Decision authority built on Restia's canonical Life graph.

Decisions deliberately remain ``LifeEntity`` records so search, ownership,
provenance, links, optimistic concurrency, encrypted history, and audit all use
the same authority as the rest of Life.  This module adds the bounded schema
and review semantics that a generic graph node cannot enforce on its own.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from core.database import Account, LifeEntity, LifeSource, utcnow_naive
from src.life_graph import (
    LifeGraphError,
    LifeGraphNotFound,
    create_entity_link,
    create_life_entity,
    get_life_entity,
    list_entity_links,
    list_life_entity_versions,
    search_life_entities,
    serialize_entity_link,
    serialize_life_entity,
    update_life_entity,
)


DECISION_SCHEMA_VERSION = 1
DECISION_SCAN_LIMIT = 500
MAX_OPTIONS = 20
MAX_REASONS = 30
MAX_RISKS = 30
MAX_ASSUMPTIONS = 40
MAX_PEOPLE = 30
MAX_EVIDENCE = 40

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_ASSUMPTION_STATUSES = frozenset({"unverified", "valid", "invalid", "retired"})
_OUTCOME_STATUSES = frozenset({
    "pending", "successful", "mixed", "unsuccessful", "unknown",
})
_TERMINAL_DECISION_STATUSES = frozenset({
    "superseded", "cancelled", "canceled", "deleted",
})
_DECISION_STATUSES = frozenset({"active", "reviewed", "superseded", "cancelled"})
_LINK_RELATIONS = {
    "person": "involves",
    "project": "relates_to",
    "file": "supported_by",
    "goal": "supports",
}
_EVIDENCE_ENTITY_TYPES = frozenset({"file", "note", "source", "message", "learning_record"})


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


def _identifier(value: object, *, field: str, fallback: str | None = None) -> str:
    raw = str(value or fallback or "").strip().lower().replace(" ", "_")
    if not raw or len(raw) > 64 or not _ID_RE.fullmatch(raw):
        raise LifeGraphError(
            f"{field} must be a lowercase identifier using letters, numbers, _, -, or ."
        )
    return raw


def _datetime(value: object | None, *, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    parsed: datetime
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


def _string_list(
    value: object | None,
    *,
    field: str,
    max_items: int,
    item_limit: int,
    required: bool = False,
) -> list[str]:
    if value is None:
        rows: list[object] = []
    elif isinstance(value, list):
        rows = value
    else:
        raise LifeGraphError(f"{field} must be a list")
    if required and not rows:
        raise LifeGraphError(f"{field} must contain at least one item")
    if len(rows) > max_items:
        raise LifeGraphError(f"{field} must not contain more than {max_items} items")
    result: list[str] = []
    seen: set[str] = set()
    for row in rows:
        text = _text(row, field=field, limit=item_limit, required=True)
        marker = text.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        result.append(text)
    if required and not result:
        raise LifeGraphError(f"{field} must contain at least one item")
    return result


def _options(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise LifeGraphError("options must be a list")
    if len(value) < 2 or len(value) > MAX_OPTIONS:
        raise LifeGraphError(f"options must contain between 2 and {MAX_OPTIONS} items")
    result: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_labels: set[str] = set()
    for index, row in enumerate(value, start=1):
        if isinstance(row, str):
            label = _text(row, field="option label", limit=240, required=True)
            details = ""
            raw_id = None
        elif isinstance(row, Mapping):
            label = _text(row.get("label"), field="option label", limit=240, required=True)
            details = _text(
                row.get("details", ""), field="option details", limit=2_000,
                preserve_lines=True,
            )
            raw_id = row.get("id")
        else:
            raise LifeGraphError("each option must be a string or object")
        option_id = _identifier(raw_id, field="option id", fallback=f"option_{index}")
        if option_id in seen_ids:
            raise LifeGraphError("option ids must be unique")
        label_key = label.casefold()
        if label_key in seen_labels:
            raise LifeGraphError("option labels must be unique")
        seen_ids.add(option_id)
        seen_labels.add(label_key)
        result.append({"id": option_id, "label": label, "details": details})
    return result


def _assumptions(value: object | None) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LifeGraphError("assumptions must be a list")
    if len(value) > MAX_ASSUMPTIONS:
        raise LifeGraphError(
            f"assumptions must not contain more than {MAX_ASSUMPTIONS} items"
        )
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(value, start=1):
        if isinstance(row, str):
            raw: Mapping[str, Any] = {"text": row}
        elif isinstance(row, Mapping):
            raw = row
        else:
            raise LifeGraphError("each assumption must be a string or object")
        assumption_id = _identifier(
            raw.get("id"), field="assumption id", fallback=f"assumption_{index}"
        )
        if assumption_id in seen:
            raise LifeGraphError("assumption ids must be unique")
        seen.add(assumption_id)
        status = str(raw.get("status") or "unverified").strip().lower()
        if status not in _ASSUMPTION_STATUSES:
            raise LifeGraphError(
                "assumption status must be unverified, valid, invalid, or retired"
            )
        result.append({
            "id": assumption_id,
            "text": _text(
                raw.get("text"), field="assumption text", limit=1_000, required=True,
                preserve_lines=True,
            ),
            "status": status,
            "review_at": _iso(raw.get("review_at"), field="assumption review_at"),
            "recorded_at": _iso(raw.get("recorded_at"), field="assumption recorded_at"),
            "last_reviewed_at": _iso(
                raw.get("last_reviewed_at"), field="assumption last_reviewed_at"
            ),
            "note": _text(
                raw.get("note", ""), field="assumption note", limit=2_000,
                preserve_lines=True,
            ),
        })
    return result


def _evidence(value: object | None) -> list[dict[str, str | None]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LifeGraphError("evidence must be a list")
    if len(value) > MAX_EVIDENCE:
        raise LifeGraphError(f"evidence must not contain more than {MAX_EVIDENCE} items")
    result: list[dict[str, str | None]] = []
    for row in value:
        if isinstance(row, str):
            raw: Mapping[str, Any] = {"label": row}
        elif isinstance(row, Mapping):
            raw = row
        else:
            raise LifeGraphError("each evidence item must be a string or object")
        source_id = str(raw.get("source_id") or "").strip() or None
        entity_id = str(raw.get("entity_id") or "").strip() or None
        if source_id and len(source_id) > 36:
            raise LifeGraphError("evidence source_id must not exceed 36 characters")
        if entity_id and len(entity_id) > 36:
            raise LifeGraphError("evidence entity_id must not exceed 36 characters")
        result.append({
            "label": _text(
                raw.get("label"), field="evidence label", limit=1_000, required=True,
                preserve_lines=True,
            ),
            "url": _text(raw.get("url", ""), field="evidence url", limit=2_000) or None,
            "source_id": source_id,
            "entity_id": entity_id,
        })
    return result


def _outcome(value: object | None, *, default_pending: bool = True) -> dict[str, Any]:
    if value is None:
        if not default_pending:
            raise LifeGraphError("outcome is required")
        raw: Mapping[str, Any] = {}
    elif isinstance(value, Mapping):
        raw = value
    else:
        raise LifeGraphError("outcome must be an object")
    status = str(raw.get("status") or "pending").strip().lower()
    if status not in _OUTCOME_STATUSES:
        raise LifeGraphError(
            "outcome status must be pending, successful, mixed, unsuccessful, or unknown"
        )
    recorded_at = _iso(raw.get("recorded_at"), field="outcome recorded_at")
    if status != "pending" and recorded_at is None:
        recorded_at = _iso(utcnow_naive(), field="outcome recorded_at")
    return {
        "status": status,
        "summary": _text(
            raw.get("summary", ""), field="outcome summary", limit=4_000,
            preserve_lines=True,
        ),
        "recorded_at": recorded_at,
    }


def validate_decision_properties(value: object) -> dict[str, Any]:
    """Return the canonical bounded Decision properties object."""
    if not isinstance(value, Mapping):
        raise LifeGraphError("decision properties must be an object")
    options = _options(value.get("options"))
    chosen = _identifier(value.get("chosen_option"), field="chosen_option")
    if chosen not in {row["id"] for row in options}:
        raise LifeGraphError("chosen_option must reference one of the supplied option ids")
    return {
        "decision_schema_version": DECISION_SCHEMA_VERSION,
        "context": _text(
            value.get("context"), field="context", limit=10_000, required=True,
            preserve_lines=True,
        ),
        "options": options,
        "chosen_option": chosen,
        "reasons": _string_list(
            value.get("reasons"), field="reasons", max_items=MAX_REASONS,
            item_limit=2_000, required=True,
        ),
        "risks": _string_list(
            value.get("risks"), field="risks", max_items=MAX_RISKS,
            item_limit=2_000,
        ),
        "assumptions": _assumptions(value.get("assumptions")),
        "people": _string_list(
            value.get("people"), field="people", max_items=MAX_PEOPLE,
            item_limit=240,
        ),
        "evidence": _evidence(value.get("evidence")),
        "outcome": _outcome(value.get("outcome")),
        "last_review": _last_review(value.get("last_review")),
    }


def _last_review(value: object | None) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise LifeGraphError("last_review must be an object")
    reviewed_at = _iso(value.get("reviewed_at"), field="last_review reviewed_at")
    if reviewed_at is None:
        raise LifeGraphError("last_review reviewed_at is required")
    return {
        "reviewed_at": reviewed_at,
        "summary": _text(
            value.get("summary"), field="last_review summary", limit=4_000,
            required=True, preserve_lines=True,
        ),
    }


def _owned_typed_decision(db, owner_id: str, entity_id: object) -> LifeEntity:
    entity = get_life_entity(db, owner_id=owner_id, entity_id=entity_id)
    if entity.entity_type != "decision":
        raise LifeGraphNotFound("Decision not found")
    validate_decision_properties(entity.properties or {})
    return entity


def _validate_evidence_authority(
    db, *, owner_id: str, evidence: Iterable[Mapping[str, Any]]
) -> set[str]:
    entity_ids: set[str] = set()
    for row in evidence:
        source_id = row.get("source_id")
        if source_id and db.query(LifeSource.id).filter(
            LifeSource.id == source_id, LifeSource.owner_id == owner_id,
        ).scalar() is None:
            raise LifeGraphNotFound("Evidence source not found")
        entity_id = row.get("entity_id")
        if entity_id:
            target = get_life_entity(db, owner_id=owner_id, entity_id=entity_id)
            if target.entity_type not in _EVIDENCE_ENTITY_TYPES:
                raise LifeGraphError(
                    "Evidence entity must be a file, note, source, message, "
                    "or learning record"
                )
            entity_ids.add(target.id)
    return entity_ids


def _validated_link_targets(db, *, owner_id: str, entity_ids: object | None) -> list[LifeEntity]:
    if entity_ids is None:
        return []
    if not isinstance(entity_ids, list):
        raise LifeGraphError("linked_entity_ids must be a list")
    if len(entity_ids) > 50:
        raise LifeGraphError("linked_entity_ids must not contain more than 50 items")
    result: list[LifeEntity] = []
    seen: set[str] = set()
    for raw in entity_ids:
        entity_id = str(raw or "").strip()
        if not entity_id or entity_id in seen:
            continue
        target = get_life_entity(db, owner_id=owner_id, entity_id=entity_id)
        if target.entity_type not in _LINK_RELATIONS:
            raise LifeGraphError("Decisions may link only to people, projects, files, and goals")
        seen.add(entity_id)
        result.append(target)
    return result


def _create_links(
    db,
    *,
    account: Account,
    decision: LifeEntity,
    targets: Iterable[LifeEntity],
    evidence_entity_ids: Iterable[str],
    provenance: Mapping[str, Any],
) -> None:
    evidence_ids = set(evidence_entity_ids)
    by_id = {target.id: target for target in targets}
    for entity_id in evidence_ids:
        if entity_id not in by_id:
            by_id[entity_id] = get_life_entity(
                db, owner_id=account.id, entity_id=entity_id
            )
    for target in by_id.values():
        relation = (
            "supported_by" if target.id in evidence_ids
            else _LINK_RELATIONS[target.entity_type]
        )
        create_entity_link(
            db,
            account=account,
            source_id=decision.id,
            relation=relation,
            target_id=target.id,
            provenance=provenance,
            reason="Decision context linked",
        )


def create_decision(
    db,
    *,
    account: Account,
    title: object,
    decision_date: object | None,
    context: object,
    options: object,
    chosen_option: object,
    reasons: object,
    risks: object | None = None,
    assumptions: object | None = None,
    people: object | None = None,
    evidence: object | None = None,
    review_at: object | None = None,
    outcome: object | None = None,
    linked_entity_ids: object | None = None,
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    normalized_provenance = (
        dict(provenance or {})
        if isinstance(provenance or {}, Mapping)
        else provenance
    )
    occurred_at = _datetime(decision_date, field="decision_date") or utcnow_naive()
    properties = validate_decision_properties({
        "context": context,
        "options": options,
        "chosen_option": chosen_option,
        "reasons": reasons,
        "risks": risks,
        "assumptions": assumptions,
        "people": people,
        "evidence": evidence,
        "outcome": outcome,
        "last_review": None,
    })
    recorded_at = _iso(occurred_at, field="decision_date")
    for assumption in properties["assumptions"]:
        assumption["recorded_at"] = recorded_at
    evidence_entity_ids = _validate_evidence_authority(
        db, owner_id=account.id, evidence=properties["evidence"]
    )
    targets = _validated_link_targets(
        db, owner_id=account.id, entity_ids=linked_entity_ids
    )
    normalized_review_at = _datetime(review_at, field="review_at")
    normalized_title = _text(title, field="title", limit=240, required=True)
    entity, created = create_life_entity(
        db,
        account=account,
        entity_type="decision",
        title=normalized_title,
        summary=properties["context"],
        status="active",
        properties=properties,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=sensitivity,
        occurred_at=occurred_at,
        review_at=normalized_review_at,
        idempotency_key=idempotency_key,
        reason="Decision recorded",
    )
    _create_links(
        db,
        account=account,
        decision=entity,
        targets=targets,
        evidence_entity_ids=evidence_entity_ids,
        provenance=normalized_provenance,
    )
    return entity, created


def update_decision(
    db,
    *,
    account: Account,
    entity_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    entity = _owned_typed_decision(db, account.id, entity_id)
    current = validate_decision_properties(entity.properties or {})
    allowed = {
        "title", "decision_date", "context", "options", "chosen_option",
        "reasons", "risks", "assumptions", "people", "evidence",
        "review_at", "outcome", "confidence", "sensitivity", "status",
        "linked_entity_ids", "provenance",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported decision fields: {', '.join(unknown)}")
    normalized_changes = dict(changes)
    if "assumptions" in normalized_changes:
        raw_assumptions = normalized_changes["assumptions"]
        if not isinstance(raw_assumptions, list):
            raise LifeGraphError("assumptions must be a list")
        current_by_id = {
            row["id"]: row for row in current["assumptions"]
        }
        prepared: list[dict[str, Any]] = []
        recorded_at = _iso(utcnow_naive(), field="assumption recorded_at")
        for raw in raw_assumptions:
            if not isinstance(raw, Mapping):
                raise LifeGraphError("each assumption must be an object")
            if "last_reviewed_at" in raw or "recorded_at" in raw:
                raise LifeGraphError(
                    "assumption review timestamps are managed by the review operation"
                )
            row = dict(raw)
            assumption_id = _identifier(row.get("id"), field="assumption id")
            previous = current_by_id.get(assumption_id)
            if previous is not None:
                requested_status = str(
                    row.get("status") or previous["status"]
                ).strip().lower()
                if requested_status != previous["status"]:
                    raise LifeGraphError(
                        "Use the decision review operation to change assumption status"
                    )
                row["status"] = previous["status"]
                row["recorded_at"] = previous.get("recorded_at") or recorded_at
                row["last_reviewed_at"] = previous.get("last_reviewed_at")
            else:
                requested_status = str(row.get("status") or "unverified").strip().lower()
                if requested_status != "unverified":
                    raise LifeGraphError("New assumptions must start as unverified")
                row["status"] = "unverified"
                row["recorded_at"] = recorded_at
                row["last_reviewed_at"] = None
            prepared.append(row)
        normalized_changes["assumptions"] = prepared
    merged = {
        **current,
        **{key: value for key, value in normalized_changes.items() if key in current},
    }
    properties = validate_decision_properties(merged)
    evidence_entity_ids = _validate_evidence_authority(
        db, owner_id=account.id, evidence=properties["evidence"]
    )
    entity_changes: dict[str, Any] = {"properties": properties, "summary": properties["context"]}
    if "title" in normalized_changes:
        entity_changes["title"] = _text(
            normalized_changes["title"], field="title", limit=240, required=True
        )
    if "decision_date" in normalized_changes:
        if normalized_changes["decision_date"] is None:
            raise LifeGraphError("decision_date cannot be cleared")
        entity_changes["occurred_at"] = _datetime(
            normalized_changes["decision_date"], field="decision_date"
        )
    for field in ("review_at", "confidence", "sensitivity", "status", "provenance"):
        if field in normalized_changes:
            entity_changes[field] = normalized_changes[field]
    if "status" in entity_changes:
        status = str(entity_changes["status"] or "").strip().lower()
        if status not in _DECISION_STATUSES:
            raise LifeGraphError(
                "decision status must be active, reviewed, superseded, or cancelled"
            )
        entity_changes["status"] = status
    updated = update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=entity_changes,
        reason="Decision details updated",
    )
    targets = (
        _validated_link_targets(
            db,
            owner_id=account.id,
            entity_ids=normalized_changes["linked_entity_ids"],
        )
        if "linked_entity_ids" in normalized_changes else []
    )
    if targets or evidence_entity_ids:
        _create_links(
            db,
            account=account,
            decision=updated,
            targets=targets,
            evidence_entity_ids=evidence_entity_ids,
            provenance=updated.provenance or {},
        )
    return updated


def review_decision(
    db,
    *,
    account: Account,
    entity_id: object,
    expected_version: int,
    summary: object,
    reviewed_at: object | None = None,
    assumption_updates: object | None = None,
    outcome: object | None = None,
    next_review_at: object | None = None,
) -> LifeEntity:
    entity = _owned_typed_decision(db, account.id, entity_id)
    review_summary = _text(
        summary, field="review summary", limit=4_000, required=True,
        preserve_lines=True,
    )
    reviewed = _datetime(reviewed_at, field="reviewed_at") or utcnow_naive()
    properties = validate_decision_properties(entity.properties or {})
    assumptions_by_id = {row["id"]: dict(row) for row in properties["assumptions"]}
    updates = assumption_updates or []
    if not isinstance(updates, list):
        raise LifeGraphError("assumption_updates must be a list")
    if len(updates) > MAX_ASSUMPTIONS:
        raise LifeGraphError("Too many assumption updates")
    seen: set[str] = set()
    for raw in updates:
        if not isinstance(raw, Mapping):
            raise LifeGraphError("each assumption update must be an object")
        assumption_id = _identifier(raw.get("id"), field="assumption id")
        if assumption_id in seen:
            raise LifeGraphError("assumption updates must be unique")
        seen.add(assumption_id)
        current = assumptions_by_id.get(assumption_id)
        if current is None:
            raise LifeGraphNotFound("Decision assumption not found")
        status = str(raw.get("status") or current["status"]).strip().lower()
        if status not in _ASSUMPTION_STATUSES:
            raise LifeGraphError(
                "assumption status must be unverified, valid, invalid, or retired"
            )
        current.update({
            "status": status,
            "review_at": _iso(raw.get("review_at"), field="assumption review_at")
            if "review_at" in raw else current.get("review_at"),
            "last_reviewed_at": _iso(reviewed, field="assumption last_reviewed_at"),
            "note": _text(
                raw.get("note", current.get("note", "")), field="assumption note",
                limit=2_000, preserve_lines=True,
            ),
        })
    properties["assumptions"] = list(assumptions_by_id.values())
    properties["last_review"] = {
        "reviewed_at": _iso(reviewed, field="reviewed_at"),
        "summary": review_summary,
    }
    if outcome is not None:
        normalized_outcome = _outcome(outcome, default_pending=False)
        normalized_outcome["recorded_at"] = _iso(reviewed, field="outcome recorded_at")
        properties["outcome"] = normalized_outcome
    properties = validate_decision_properties(properties)
    return update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes={
            "properties": properties,
            "review_at": _datetime(next_review_at, field="next_review_at"),
        },
        reason=f"Decision reviewed: {review_summary}",
    )


def _decision_links(db, *, owner_id: str, entity_id: str) -> list[dict[str, Any]]:
    rows, _ = list_entity_links(
        db, owner_id=owner_id, entity_id=entity_id, direction="outgoing", limit=100
    )
    target_ids = [row.target_id for row in rows]
    targets = {
        row.id: row for row in db.query(LifeEntity).filter(
            LifeEntity.owner_id == owner_id,
            LifeEntity.id.in_(target_ids),
            LifeEntity.deleted_at.is_(None),
        ).all()
    } if target_ids else {}
    result: list[dict[str, Any]] = []
    for link in rows:
        target = targets.get(link.target_id)
        if target is None:
            continue
        result.append({
            "link": serialize_entity_link(link),
            "target": {
                "id": target.id,
                "entity_type": target.entity_type,
                "title": target.title or "",
                "status": target.status,
                "version": int(target.version or 1),
            },
        })
    return result


def serialize_decision(
    entity: LifeEntity, *, links: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    if entity.entity_type != "decision":
        raise LifeGraphError("Entity is not a decision")
    properties = validate_decision_properties(entity.properties or {})
    payload = serialize_life_entity(entity)
    payload.update({
        "decision_date": payload["occurred_at"],
        "context": properties["context"],
        "options": properties["options"],
        "chosen_option": properties["chosen_option"],
        "reasons": properties["reasons"],
        "risks": properties["risks"],
        "assumptions": properties["assumptions"],
        "people": properties["people"],
        "evidence": properties["evidence"],
        "outcome": properties["outcome"],
        "last_review": properties["last_review"],
        "links": links or [],
    })
    return payload


def get_decision(db, *, owner_id: str, entity_id: object) -> dict[str, Any]:
    entity = _owned_typed_decision(db, owner_id, entity_id)
    return serialize_decision(
        entity, links=_decision_links(db, owner_id=owner_id, entity_id=entity.id)
    )


def list_decisions(
    db,
    *,
    owner_id: str,
    status: str | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    bounded = max(1, min(100, int(limit)))
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == "decision",
        LifeEntity.deleted_at.is_(None),
    )
    if status:
        query = query.filter(LifeEntity.status == str(status).strip().lower())
    rows = query.order_by(
        LifeEntity.occurred_at.desc(), LifeEntity.updated_at.desc(), LifeEntity.id.desc()
    ).limit(bounded + 1).all()
    result: list[dict[str, Any]] = []
    for row in rows[:bounded]:
        try:
            result.append(serialize_decision(row))
        except LifeGraphError:
            # Legacy untyped decision nodes remain visible through /entities,
            # but do not masquerade as records in the typed Decisions system.
            continue
    return result, len(rows) > bounded


def search_decisions(
    db, *, owner_id: str, query_text: object, limit: int = 25
) -> dict[str, Any]:
    result = search_life_entities(
        db,
        owner_id=owner_id,
        query_text=query_text,
        entity_type="decision",
        limit=limit,
    )
    items: list[dict[str, Any]] = []
    for match in result["items"]:
        entity = get_life_entity(
            db, owner_id=owner_id, entity_id=match["entity"]["id"]
        )
        try:
            decision = serialize_decision(entity)
        except LifeGraphError:
            continue
        items.append({"decision": decision, "match": match["match"], "rank": match["rank"]})
    return {**result, "items": items, "count": len(items)}


def list_due_decision_reviews(
    db,
    *,
    owner_id: str,
    due_before: object | None = None,
    stale_after_days: int = 30,
    limit: int = 50,
) -> dict[str, Any]:
    cutoff = _datetime(due_before, field="due_before") or utcnow_naive()
    stale_days = max(1, min(3650, int(stale_after_days)))
    stale_cutoff = cutoff - timedelta(days=stale_days)
    bounded = max(1, min(100, int(limit)))
    candidates = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == "decision",
        LifeEntity.deleted_at.is_(None),
        ~LifeEntity.status.in_(tuple(_TERMINAL_DECISION_STATUSES)),
    ).order_by(
        LifeEntity.review_at.asc(), LifeEntity.occurred_at.asc(), LifeEntity.id.asc()
    ).limit(DECISION_SCAN_LIMIT + 1).all()
    scan_truncated = len(candidates) > DECISION_SCAN_LIMIT
    items: list[tuple[datetime, dict[str, Any]]] = []
    for entity in candidates[:DECISION_SCAN_LIMIT]:
        try:
            properties = validate_decision_properties(entity.properties or {})
        except LifeGraphError:
            continue
        due_assumptions: list[str] = []
        stale_assumptions: list[str] = []
        earliest = entity.review_at if entity.review_at and entity.review_at <= cutoff else None
        for assumption in properties["assumptions"]:
            if assumption["status"] not in {"unverified", "valid"}:
                continue
            assumption_due = _datetime(
                assumption.get("review_at"), field="assumption review_at"
            )
            if assumption_due is not None and assumption_due <= cutoff:
                due_assumptions.append(assumption["id"])
                earliest = min(earliest, assumption_due) if earliest else assumption_due
            reviewed_at = _datetime(
                assumption.get("last_reviewed_at"), field="assumption last_reviewed_at"
            )
            recorded_at = _datetime(
                assumption.get("recorded_at"), field="assumption recorded_at"
            )
            freshness = reviewed_at or recorded_at or entity.occurred_at or entity.created_at
            if freshness is not None and freshness <= stale_cutoff:
                stale_assumptions.append(assumption["id"])
                earliest = min(earliest, freshness) if earliest else freshness
        if earliest is None:
            continue
        reasons: list[str] = []
        if entity.review_at is not None and entity.review_at <= cutoff:
            reasons.append("decision_review_due")
        if due_assumptions:
            reasons.append("assumption_review_due")
        if stale_assumptions:
            reasons.append("assumption_stale")
        decision = serialize_decision(entity)
        decision.update({
            "due_reasons": reasons,
            "due_assumption_ids": due_assumptions,
            "stale_assumption_ids": stale_assumptions,
        })
        items.append((earliest, decision))
    items.sort(key=lambda row: (row[0], row[1]["id"]))
    selected = [row for _, row in items[:bounded]]
    return {
        "items": selected,
        "count": len(selected),
        "scanned": min(len(candidates), DECISION_SCAN_LIMIT),
        "truncated": scan_truncated or len(items) > bounded,
        "due_before": _iso(cutoff, field="due_before"),
        "stale_after_days": stale_days,
    }


def decision_history(
    db, *, owner_id: str, entity_id: object, limit: int = 50
) -> tuple[list[dict[str, Any]], bool]:
    entity = _owned_typed_decision(db, owner_id, entity_id)
    rows, truncated = list_life_entity_versions(
        db, owner_id=owner_id, entity_id=entity.id, limit=limit
    )
    chronological = list(reversed(rows))
    items: list[dict[str, Any]] = []
    previous: Mapping[str, Any] | None = None
    for row in chronological:
        snapshot = dict(row.snapshot or {})
        properties = validate_decision_properties(snapshot.get("properties") or {})
        changed: list[str] = []
        kinds: list[str] = []
        if previous is None:
            kinds.append("created")
        else:
            previous_properties = validate_decision_properties(
                previous.get("properties") or {}
            )
            for field in (
                "title", "summary", "status", "occurred_at", "review_at",
                "confidence", "sensitivity", "provenance",
            ):
                if previous.get(field) != snapshot.get(field):
                    changed.append(field)
            for field in (
                "context", "options", "chosen_option", "reasons", "risks",
                "assumptions", "people", "evidence", "outcome", "last_review",
            ):
                if previous_properties.get(field) != properties.get(field):
                    changed.append(field)
            if properties.get("last_review") != previous_properties.get("last_review"):
                kinds.append("review_recorded")
            if properties.get("outcome") != previous_properties.get("outcome"):
                kinds.append("outcome_changed")
            if properties.get("assumptions") != previous_properties.get("assumptions"):
                kinds.append("assumptions_changed")
            if changed and not kinds:
                kinds.append("decision_changed")
        items.append({
            "id": row.id,
            "version": int(row.version),
            "created_at": row.created_at.replace(
                tzinfo=timezone.utc
            ).isoformat().replace("+00:00", "Z"),
            "reason": row.reason or "",
            "kinds": kinds,
            "changed_fields": changed,
            "decision": {
                "title": snapshot.get("title") or "",
                "status": snapshot.get("status") or "active",
                "decision_date": snapshot.get("occurred_at"),
                "review_at": snapshot.get("review_at"),
                "chosen_option": properties["chosen_option"],
                "assumptions": properties["assumptions"],
                "outcome": properties["outcome"],
                "last_review": properties["last_review"],
            },
        })
        previous = snapshot
    return list(reversed(items)), truncated
