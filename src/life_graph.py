"""Principal-scoped services for Restia's V3 life graph.

The graph is an integration layer over existing domain authorities.  All
records are owned by an immutable ``Account.id`` and all public mutations are
audited.  Sensitive content stays in encrypted model columns; audit rows carry
only structural state while the append-only entity-version table stores the
complete encrypted snapshot.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from typing import Any, Mapping

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from core.database import (
    LIFE_ENTITY_TYPES,
    Account,
    EntityLink,
    LifeEntity,
    LifeEntityVersion,
    LifeSource,
    PlanningItem,
    Project,
    ProjectMember,
    utcnow_naive,
)
from src.life_core import append_action_audit


MAX_JSON_BYTES = 64 * 1024
SEARCH_SCAN_LIMIT = 500
TASK_QUALITY_SCAN_LIMIT = 500
TASK_QUALITY_LINK_SCAN_LIMIT = 2_000
TRAVERSAL_EDGE_SCAN_LIMIT = 500

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_TASK_STATUSES = frozenset({
    "completed", "done", "cancelled", "canceled", "archived", "deleted",
})
_BLOCKED_STATUSES = frozenset({"blocked", "stalled"})
_WAITING_STATUSES = frozenset({
    "waiting", "waiting_on", "waiting_for", "delegated", "pending_external",
})
_IRRELEVANT_STATUSES = frozenset({"irrelevant", "obsolete", "superseded"})
_GOAL_RELATIONS = frozenset({
    "supports", "belongs_to", "part_of", "advances", "serves", "goal",
})
_NEXT_ACTION_RELATIONS = frozenset({
    "next_action", "has_next_action", "implemented_by",
})
_BLOCKING_RELATIONS = frozenset({"blocked_by", "depends_on"})
_DUPLICATE_RELATIONS = frozenset({"duplicate_of", "duplicates"})

# ActionAudit.details is intentionally plaintext structural metadata. Client
# reasons belong only in the encrypted LifeEntityVersion history; immutable
# audit rows use bounded server-authored descriptions instead.
_AUDIT_ENTITY_CREATED = "Life entity created"
_AUDIT_ENTITY_UPDATED = "Life entity updated"
_AUDIT_ENTITY_DELETED = "Life entity deleted"
_AUDIT_LINK_CREATED = "Life entities linked"
_AUDIT_LINK_DELETED = "Life entity link deleted"


class LifeGraphError(ValueError):
    """Base class for controlled life-graph failures."""


class LifeGraphNotFound(LifeGraphError):
    pass


class LifeGraphConflict(LifeGraphError):
    pass


def _bounded_text(value: object, *, limit: int, required: bool = False) -> str:
    text = " ".join(str(value or "").split())
    if required and not text:
        raise LifeGraphError("A non-empty value is required")
    return text[:limit]


def _long_text(value: object, *, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _token(value: object, *, field: str, limit: int) -> str:
    token = str(value or "").strip().lower().replace(" ", "_")
    if not token or len(token) > limit or not _TOKEN_RE.fullmatch(token):
        raise LifeGraphError(
            f"{field} must be a lowercase token using letters, numbers, _, -, or ."
        )
    return token


def _entity_type(value: object) -> str:
    normalized = _token(value, field="entity_type", limit=48)
    if normalized not in LIFE_ENTITY_TYPES:
        raise LifeGraphError("Unknown life entity type")
    return normalized


def _confidence(value: object) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise LifeGraphError("confidence must be an integer from 0 to 100") from exc
    if number < 0 or number > 100:
        raise LifeGraphError("confidence must be an integer from 0 to 100")
    return number


def _json_object(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LifeGraphError(f"{field} must be an object")
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise LifeGraphError(f"{field} must be JSON-serializable") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise LifeGraphError(f"{field} must not exceed {MAX_JSON_BYTES} bytes")
    return dict(value)


def _naive_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = _naive_utc(value)
    return normalized.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _protected_idempotency_key(value: object | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _content_sha256(value: object | None) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if not _HEX_SHA256_RE.fullmatch(text):
        raise LifeGraphError("content_sha256 must be 64 lowercase hexadecimal characters")
    return text


def _structural_entity_state(entity: LifeEntity) -> dict[str, Any]:
    return {
        "entity_type": entity.entity_type,
        "status": entity.status,
        "confidence": int(entity.confidence or 0),
        "sensitivity": entity.sensitivity,
        "domain_ref_type": entity.domain_ref_type,
        "domain_ref_id": entity.domain_ref_id,
        "occurred_at": _iso(entity.occurred_at),
        "due_at": _iso(entity.due_at),
        "review_at": _iso(entity.review_at),
        "version": int(entity.version or 1),
        "deleted_at": _iso(entity.deleted_at),
    }


def _structural_link_state(link: EntityLink) -> dict[str, Any]:
    return {
        "source_type": link.source_type,
        "source_id": link.source_id,
        "relation": link.relation,
        "target_type": link.target_type,
        "target_id": link.target_id,
        "confidence": int(link.confidence or 0),
        "sensitivity": link.sensitivity,
        "version": int(link.version or 1),
        "deleted_at": _iso(link.deleted_at),
    }


def serialize_life_source(source: LifeSource) -> dict[str, Any]:
    return {
        "id": source.id,
        "source_type": source.source_type,
        "title": source.title or "",
        "source_ref": source.source_ref,
        "safe_excerpt": source.safe_excerpt or "",
        "content_sha256": source.content_sha256,
        "observed_at": _iso(source.observed_at),
        "captured_at": _iso(source.captured_at),
        "sensitivity": source.sensitivity,
        "metadata": source.meta_data or {},
        "version": int(source.version or 1),
        "created_at": _iso(source.created_at),
        "updated_at": _iso(source.updated_at),
    }


def serialize_life_entity(entity: LifeEntity) -> dict[str, Any]:
    return {
        "id": entity.id,
        "entity_type": entity.entity_type,
        "title": entity.title or "",
        "summary": entity.summary or "",
        "status": entity.status,
        "properties": entity.properties or {},
        "provenance": entity.provenance or {},
        "confidence": int(entity.confidence or 0),
        "sensitivity": entity.sensitivity,
        "domain_ref_type": entity.domain_ref_type,
        "domain_ref_id": entity.domain_ref_id,
        "occurred_at": _iso(entity.occurred_at),
        "due_at": _iso(entity.due_at),
        "review_at": _iso(entity.review_at),
        "version": int(entity.version or 1),
        "deleted_at": _iso(entity.deleted_at),
        "created_at": _iso(entity.created_at),
        "updated_at": _iso(entity.updated_at),
    }


def serialize_life_entity_version(row: LifeEntityVersion) -> dict[str, Any]:
    return {
        "id": row.id,
        "entity_id": row.entity_id,
        "version": int(row.version),
        "snapshot": row.snapshot or {},
        "reason": row.reason or "",
        "created_at": _iso(row.created_at),
    }


def serialize_entity_link(link: EntityLink) -> dict[str, Any]:
    return {
        "id": link.id,
        "source_type": link.source_type,
        "source_id": link.source_id,
        "relation": link.relation,
        "target_type": link.target_type,
        "target_id": link.target_id,
        "metadata": link.meta_data or {},
        "provenance": link.provenance or {},
        "confidence": int(link.confidence or 0),
        "sensitivity": link.sensitivity,
        "version": int(link.version or 1),
        "deleted_at": _iso(link.deleted_at),
        "created_at": _iso(link.created_at),
        "updated_at": _iso(link.updated_at),
    }


def _snapshot(entity: LifeEntity) -> dict[str, Any]:
    return serialize_life_entity(entity)


def _append_entity_version(
    db,
    entity: LifeEntity,
    *,
    reason: str,
) -> LifeEntityVersion:
    row = LifeEntityVersion(
        id=str(uuid.uuid4()),
        owner_id=entity.owner_id,
        entity_id=entity.id,
        version=int(entity.version or 1),
        snapshot=_snapshot(entity),
        reason=_long_text(reason, limit=500) or "Life entity changed",
    )
    db.add(row)
    db.flush()
    return row


def _provenance_source_ids(value: Mapping[str, Any]) -> set[str]:
    source_ids: set[str] = set()
    one = value.get("source_id")
    if one is not None:
        if not isinstance(one, str) or not one.strip():
            raise LifeGraphError("provenance.source_id must be a non-empty string")
        source_ids.add(one.strip())
    many = value.get("source_ids")
    if many is not None:
        if not isinstance(many, list) or any(
            not isinstance(item, str) or not item.strip() for item in many
        ):
            raise LifeGraphError("provenance.source_ids must be a list of strings")
        source_ids.update(item.strip() for item in many)
    return source_ids


def _validate_provenance_sources(
    db,
    *,
    owner_id: str,
    provenance: Mapping[str, Any],
) -> None:
    source_ids = _provenance_source_ids(provenance)
    if not source_ids:
        return
    owned = {
        row[0]
        for row in db.query(LifeSource.id).filter(
            LifeSource.owner_id == owner_id,
            LifeSource.id.in_(source_ids),
        ).all()
    }
    if owned != source_ids:
        raise LifeGraphNotFound("Provenance source not found")


def _claim_domain_reference(
    db,
    *,
    account: Account,
    ref_type: str | None,
    ref_id: str | None,
) -> None:
    if ref_type is None:
        return
    if ref_type == "life_source":
        claimed = db.query(LifeSource).filter(
            LifeSource.id == ref_id,
            LifeSource.owner_id == account.id,
        ).update(
            {LifeSource.updated_at: LifeSource.updated_at},
            synchronize_session=False,
        )
    elif ref_type == "planning_item":
        claimed = db.query(PlanningItem).filter(
            PlanningItem.id == ref_id,
            PlanningItem.owner == account.username,
        ).update(
            {PlanningItem.updated_at: PlanningItem.updated_at},
            synchronize_session=False,
        )
    elif ref_type == "project":
        project = db.query(Project).filter(Project.id == ref_id).first()
        if project is None:
            raise LifeGraphNotFound("Domain record not found")
        claimed = db.query(Project).filter(Project.id == ref_id).update(
            {Project.updated_at: Project.updated_at},
            synchronize_session=False,
        )
        if claimed != 1:
            raise LifeGraphNotFound("Domain record not found")
        db.refresh(project)
        if project.owner == account.username:
            return
        claimed = db.query(ProjectMember).filter(
            ProjectMember.project_id == ref_id,
            ProjectMember.username == account.username,
        ).update(
            {ProjectMember.role: ProjectMember.role},
            synchronize_session=False,
        )
    else:
        raise LifeGraphError(
            "domain_ref_type must be life_source, planning_item, or project"
        )
    if claimed != 1:
        raise LifeGraphNotFound("Domain record not found")


def create_life_source(
    db,
    *,
    account: Account,
    source_type: object,
    title: object = "",
    source_ref: object | None = None,
    safe_excerpt: object = "",
    content_sha256: object | None = None,
    observed_at: datetime | None = None,
    sensitivity: object = "private",
    metadata: object | None = None,
    idempotency_key: object | None = None,
) -> tuple[LifeSource, bool]:
    normalized_type = _token(source_type, field="source_type", limit=48)
    normalized_title = _bounded_text(title, limit=240)
    normalized_ref = (
        _long_text(source_ref, limit=2_000) if source_ref is not None else None
    )
    normalized_excerpt = _long_text(safe_excerpt, limit=4_000)
    normalized_hash = _content_sha256(content_sha256)
    normalized_observed_at = _naive_utc(observed_at)
    normalized_sensitivity = _token(
        sensitivity, field="sensitivity", limit=24
    )
    normalized_metadata = _json_object(metadata or {}, field="metadata")
    key = _protected_idempotency_key(idempotency_key)

    def matches(existing: LifeSource) -> bool:
        return all((
            existing.source_type == normalized_type,
            existing.title == normalized_title,
            existing.source_ref == normalized_ref,
            existing.safe_excerpt == normalized_excerpt,
            existing.content_sha256 == normalized_hash,
            existing.observed_at == normalized_observed_at,
            existing.sensitivity == normalized_sensitivity,
            dict(existing.meta_data or {}) == normalized_metadata,
        ))

    if key:
        existing = db.query(LifeSource).filter(
            LifeSource.owner_id == account.id,
            LifeSource.idempotency_key == key,
        ).first()
        if existing is not None:
            if not matches(existing):
                raise LifeGraphConflict(
                    "Life source idempotency key was already used for different content"
                )
            return existing, False

    source = LifeSource(
        id=str(uuid.uuid4()),
        owner_id=account.id,
        source_type=normalized_type,
        title=normalized_title,
        source_ref=normalized_ref,
        safe_excerpt=normalized_excerpt,
        content_sha256=normalized_hash,
        observed_at=normalized_observed_at,
        sensitivity=normalized_sensitivity,
        meta_data=normalized_metadata,
        idempotency_key=key,
        version=1,
    )
    try:
        with db.begin_nested():
            db.add(source)
            db.flush()
    except IntegrityError:
        if not key:
            raise
        existing = db.query(LifeSource).filter(
            LifeSource.owner_id == account.id,
            LifeSource.idempotency_key == key,
        ).first()
        if existing is None:
            raise
        if not matches(existing):
            raise LifeGraphConflict(
                "Life source idempotency key was already used for different content"
            )
        return existing, False

    append_action_audit(
        db,
        owner_id=account.id,
        action="life.source.created",
        entity_type="life_source",
        entity_id=source.id,
        reason="Life source captured",
        after_state={
            "source_type": source.source_type,
            "sensitivity": source.sensitivity,
            "version": 1,
            "observed_at": _iso(source.observed_at),
        },
        details={"has_content_hash": bool(source.content_sha256)},
        idempotency_ref=key,
    )
    return source, True


def list_life_sources(
    db,
    *,
    owner_id: str,
    source_type: object | None = None,
    limit: int = 50,
) -> tuple[list[LifeSource], bool]:
    bounded = max(1, min(100, int(limit)))
    query = db.query(LifeSource).filter(LifeSource.owner_id == owner_id)
    if source_type:
        query = query.filter(
            LifeSource.source_type == _token(
                source_type, field="source_type", limit=48
            )
        )
    rows = query.order_by(
        LifeSource.captured_at.desc(), LifeSource.id.desc()
    ).limit(bounded + 1).all()
    return rows[:bounded], len(rows) > bounded


def _owned_entity(
    db,
    owner_id: str,
    entity_id: object,
    *,
    include_deleted: bool,
) -> LifeEntity:
    query = db.query(LifeEntity).filter(
        LifeEntity.id == str(entity_id),
        LifeEntity.owner_id == owner_id,
    )
    if not include_deleted:
        query = query.filter(LifeEntity.deleted_at.is_(None))
    entity = query.first()
    if entity is None:
        raise LifeGraphNotFound("Life entity not found")
    return entity


def get_life_entity(
    db,
    *,
    owner_id: str,
    entity_id: object,
    include_deleted: bool = False,
) -> LifeEntity:
    return _owned_entity(
        db, owner_id, entity_id, include_deleted=include_deleted
    )


def list_life_entities(
    db,
    *,
    owner_id: str,
    entity_type: object | None = None,
    status: object | None = None,
    include_deleted: bool = False,
    limit: int = 50,
) -> tuple[list[LifeEntity], bool]:
    bounded = max(1, min(100, int(limit)))
    query = db.query(LifeEntity).filter(LifeEntity.owner_id == owner_id)
    if not include_deleted:
        query = query.filter(LifeEntity.deleted_at.is_(None))
    if entity_type:
        query = query.filter(LifeEntity.entity_type == _entity_type(entity_type))
    if status:
        query = query.filter(
            LifeEntity.status == _token(status, field="status", limit=32)
        )
    rows = query.order_by(
        LifeEntity.updated_at.desc(), LifeEntity.id.desc()
    ).limit(bounded + 1).all()
    return rows[:bounded], len(rows) > bounded


def create_life_entity(
    db,
    *,
    account: Account,
    entity_type: object,
    title: object,
    summary: object = "",
    status: object = "active",
    properties: object | None = None,
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    domain_ref_type: object | None = None,
    domain_ref_id: object | None = None,
    occurred_at: datetime | None = None,
    due_at: datetime | None = None,
    review_at: datetime | None = None,
    idempotency_key: object | None = None,
    reason: object = "Life entity created",
) -> tuple[LifeEntity, bool]:
    normalized_type = _entity_type(entity_type)
    normalized_title = _bounded_text(title, limit=240, required=True)
    normalized_summary = _long_text(summary, limit=20_000)
    normalized_status = _token(status, field="status", limit=32)
    normalized_properties = _json_object(properties or {}, field="properties")
    normalized_provenance = _json_object(provenance or {}, field="provenance")
    _validate_provenance_sources(
        db, owner_id=account.id, provenance=normalized_provenance
    )
    ref_type = (
        _token(domain_ref_type, field="domain_ref_type", limit=48)
        if domain_ref_type is not None else None
    )
    ref_id = (
        _long_text(domain_ref_id, limit=255) if domain_ref_id is not None else None
    )
    if bool(ref_type) != bool(ref_id):
        raise LifeGraphError("domain_ref_type and domain_ref_id must be supplied together")
    _claim_domain_reference(
        db,
        account=account,
        ref_type=ref_type,
        ref_id=ref_id,
    )
    normalized_confidence = _confidence(confidence)
    normalized_sensitivity = _token(
        sensitivity, field="sensitivity", limit=24
    )
    normalized_occurred_at = _naive_utc(occurred_at)
    normalized_due_at = _naive_utc(due_at)
    normalized_review_at = _naive_utc(review_at)
    key = _protected_idempotency_key(idempotency_key)

    def matches(existing: LifeEntity) -> bool:
        return all((
            existing.deleted_at is None,
            existing.entity_type == normalized_type,
            existing.title == normalized_title,
            existing.summary == normalized_summary,
            existing.status == normalized_status,
            dict(existing.properties or {}) == normalized_properties,
            dict(existing.provenance or {}) == normalized_provenance,
            int(existing.confidence) == normalized_confidence,
            existing.sensitivity == normalized_sensitivity,
            existing.domain_ref_type == ref_type,
            existing.domain_ref_id == ref_id,
            existing.occurred_at == normalized_occurred_at,
            existing.due_at == normalized_due_at,
            existing.review_at == normalized_review_at,
        ))

    if key:
        existing = db.query(LifeEntity).filter(
            LifeEntity.owner_id == account.id,
            LifeEntity.idempotency_key == key,
        ).first()
        if existing is not None:
            if not matches(existing):
                raise LifeGraphConflict(
                    "Life entity idempotency key was already used for a different entity"
                )
            return existing, False

    entity = LifeEntity(
        id=str(uuid.uuid4()),
        owner_id=account.id,
        entity_type=normalized_type,
        title=normalized_title,
        summary=normalized_summary,
        status=normalized_status,
        properties=normalized_properties,
        provenance=normalized_provenance,
        confidence=normalized_confidence,
        sensitivity=normalized_sensitivity,
        domain_ref_type=ref_type,
        domain_ref_id=ref_id,
        occurred_at=normalized_occurred_at,
        due_at=normalized_due_at,
        review_at=normalized_review_at,
        idempotency_key=key,
        version=1,
    )
    try:
        with db.begin_nested():
            db.add(entity)
            db.flush()
    except IntegrityError as exc:
        if key:
            existing = db.query(LifeEntity).filter(
                LifeEntity.owner_id == account.id,
                LifeEntity.idempotency_key == key,
            ).first()
            if existing is not None:
                if not matches(existing):
                    raise LifeGraphConflict(
                        "Life entity idempotency key was already used for a different entity"
                    ) from exc
                return existing, False
        if ref_type and ref_id:
            existing = db.query(LifeEntity).filter(
                LifeEntity.owner_id == account.id,
                LifeEntity.entity_type == normalized_type,
                LifeEntity.domain_ref_type == ref_type,
                LifeEntity.domain_ref_id == ref_id,
            ).first()
            if existing is not None:
                raise LifeGraphConflict("Domain record is already linked") from exc
        raise

    _append_entity_version(db, entity, reason=str(reason or "Life entity created"))
    append_action_audit(
        db,
        owner_id=account.id,
        action="life.entity.created",
        entity_type="life_entity",
        entity_id=entity.id,
        reason=_AUDIT_ENTITY_CREATED,
        after_state=_structural_entity_state(entity),
        details={"entity_type": entity.entity_type},
        idempotency_ref=key,
        reversible=True,
        undo_ref=f"life-entity:{entity.id}:1",
    )
    return entity, True


def _check_entity_version(entity: LifeEntity, expected_version: int) -> None:
    if int(entity.version or 1) != int(expected_version):
        raise LifeGraphConflict(
            f"Life entity changed in another client (current version {int(entity.version or 1)})"
        )


def _reserve_entity_version(db, entity: LifeEntity, expected_version: int) -> None:
    _check_entity_version(entity, expected_version)
    next_version = int(expected_version) + 1
    now = utcnow_naive()
    updated = db.query(LifeEntity).filter(
        LifeEntity.id == entity.id,
        LifeEntity.owner_id == entity.owner_id,
        LifeEntity.version == int(expected_version),
    ).update(
        {LifeEntity.version: next_version, LifeEntity.updated_at: now},
        synchronize_session=False,
    )
    if updated != 1:
        db.expire_all()
        current = db.query(LifeEntity.version).filter(
            LifeEntity.id == entity.id,
            LifeEntity.owner_id == entity.owner_id,
        ).scalar()
        raise LifeGraphConflict(
            f"Life entity changed in another client (current version {int(current or 1)})"
        )
    entity.version = next_version
    entity.updated_at = now


def update_life_entity(
    db,
    *,
    owner_id: str,
    entity_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
    reason: object = "Life entity updated",
) -> LifeEntity:
    entity = _owned_entity(db, owner_id, entity_id, include_deleted=False)
    _check_entity_version(entity, expected_version)
    allowed = {
        "title", "summary", "status", "properties", "provenance",
        "confidence", "sensitivity", "occurred_at", "due_at", "review_at",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported entity fields: {', '.join(unknown)}")

    normalized: dict[str, Any] = {}
    for field, value in changes.items():
        if field == "title":
            normalized[field] = _bounded_text(value, limit=240, required=True)
        elif field == "summary":
            normalized[field] = _long_text(value, limit=20_000)
        elif field == "status":
            status = _token(value, field="status", limit=32)
            if status == "deleted":
                raise LifeGraphError("Use the delete operation to remove an entity")
            normalized[field] = status
        elif field in {"properties", "provenance"}:
            normalized[field] = _json_object(value, field=field)
        elif field == "confidence":
            normalized[field] = _confidence(value)
        elif field == "sensitivity":
            normalized[field] = _token(value, field="sensitivity", limit=24)
        else:
            normalized[field] = _naive_utc(value)
    if "provenance" in normalized:
        _validate_provenance_sources(
            db, owner_id=owner_id, provenance=normalized["provenance"]
        )
    effective = {
        key: value for key, value in normalized.items()
        if getattr(entity, key) != value
    }
    if not effective:
        return entity

    before = _structural_entity_state(entity)
    _reserve_entity_version(db, entity, expected_version)
    for field, value in effective.items():
        setattr(entity, field, value)
    db.flush()
    reason_text = str(reason or "Life entity updated")
    _append_entity_version(db, entity, reason=reason_text)
    append_action_audit(
        db,
        owner_id=owner_id,
        action="life.entity.updated",
        entity_type="life_entity",
        entity_id=entity.id,
        reason=_AUDIT_ENTITY_UPDATED,
        before_state=before,
        after_state=_structural_entity_state(entity),
        details={"fields": sorted(effective)},
        reversible=True,
        undo_ref=f"life-entity:{entity.id}:{entity.version}",
    )
    return entity


def delete_life_entity(
    db,
    *,
    owner_id: str,
    entity_id: object,
    expected_version: int,
    reason: object = "Life entity deleted",
) -> LifeEntity:
    entity = _owned_entity(db, owner_id, entity_id, include_deleted=True)
    if entity.deleted_at is not None:
        return entity
    before = _structural_entity_state(entity)
    _reserve_entity_version(db, entity, expected_version)
    entity.status = "deleted"
    entity.deleted_at = utcnow_naive()
    db.flush()
    reason_text = str(reason or "Life entity deleted")
    _append_entity_version(db, entity, reason=reason_text)
    append_action_audit(
        db,
        owner_id=owner_id,
        action="life.entity.deleted",
        entity_type="life_entity",
        entity_id=entity.id,
        reason=_AUDIT_ENTITY_DELETED,
        before_state=before,
        after_state=_structural_entity_state(entity),
        reversible=True,
        undo_ref=f"life-entity:{entity.id}:{entity.version}",
    )
    return entity


def list_life_entity_versions(
    db,
    *,
    owner_id: str,
    entity_id: object,
    limit: int = 50,
) -> tuple[list[LifeEntityVersion], bool]:
    _owned_entity(db, owner_id, entity_id, include_deleted=True)
    bounded = max(1, min(100, int(limit)))
    rows = db.query(LifeEntityVersion).filter(
        LifeEntityVersion.owner_id == owner_id,
        LifeEntityVersion.entity_id == str(entity_id),
    ).order_by(
        LifeEntityVersion.version.desc(), LifeEntityVersion.id.desc()
    ).limit(bounded + 1).all()
    return rows[:bounded], len(rows) > bounded


def _owned_link(db, owner_id: str, link_id: object) -> EntityLink:
    link = db.query(EntityLink).filter(
        EntityLink.id == str(link_id),
        EntityLink.owner_id == owner_id,
        EntityLink.source_type == "life_entity",
        EntityLink.target_type == "life_entity",
    ).first()
    if link is None:
        raise LifeGraphNotFound("Life entity link not found")
    # A corrupt/cross-owner edge must not become observable merely because its
    # own owner_id happens to match the caller.
    _owned_entity(db, owner_id, link.source_id, include_deleted=True)
    _owned_entity(db, owner_id, link.target_id, include_deleted=True)
    return link


def create_entity_link(
    db,
    *,
    account: Account,
    source_id: object,
    relation: object,
    target_id: object,
    metadata: object | None = None,
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    reason: object = "Life entities linked",
) -> tuple[EntityLink, bool]:
    source = _owned_entity(db, account.id, source_id, include_deleted=False)
    target = _owned_entity(db, account.id, target_id, include_deleted=False)
    if source.id == target.id:
        raise LifeGraphError("A life entity cannot link to itself")
    normalized_relation = _token(relation, field="relation", limit=64)
    normalized_metadata = _json_object(metadata or {}, field="metadata")
    normalized_provenance = _json_object(provenance or {}, field="provenance")
    normalized_confidence = _confidence(confidence)
    normalized_sensitivity = _token(
        sensitivity, field="sensitivity", limit=24
    )
    _validate_provenance_sources(
        db, owner_id=account.id, provenance=normalized_provenance
    )
    existing = db.query(EntityLink).filter(
        EntityLink.owner_id == account.id,
        EntityLink.source_type == "life_entity",
        EntityLink.source_id == source.id,
        EntityLink.relation == normalized_relation,
        EntityLink.target_type == "life_entity",
        EntityLink.target_id == target.id,
    ).first()
    if existing is not None:
        if existing.deleted_at is not None:
            raise LifeGraphConflict("Life entity link was deleted")
        if not all((
            dict(existing.meta_data or {}) == normalized_metadata,
            dict(existing.provenance or {}) == normalized_provenance,
            int(existing.confidence) == normalized_confidence,
            existing.sensitivity == normalized_sensitivity,
        )):
            raise LifeGraphConflict(
                "Life entity link already exists with different attributes"
            )
        return existing, False

    link = EntityLink(
        id=str(uuid.uuid4()),
        owner_id=account.id,
        source_type="life_entity",
        source_id=source.id,
        relation=normalized_relation,
        target_type="life_entity",
        target_id=target.id,
        meta_data=normalized_metadata,
        provenance=normalized_provenance,
        confidence=normalized_confidence,
        sensitivity=normalized_sensitivity,
        version=1,
    )
    try:
        with db.begin_nested():
            db.add(link)
            db.flush()
    except IntegrityError:
        existing = db.query(EntityLink).filter(
            EntityLink.owner_id == account.id,
            EntityLink.source_type == "life_entity",
            EntityLink.source_id == source.id,
            EntityLink.relation == normalized_relation,
            EntityLink.target_type == "life_entity",
            EntityLink.target_id == target.id,
        ).first()
        if existing is None:
            raise
        if existing.deleted_at is not None:
            raise LifeGraphConflict("Life entity link was deleted")
        if not all((
            dict(existing.meta_data or {}) == normalized_metadata,
            dict(existing.provenance or {}) == normalized_provenance,
            int(existing.confidence) == normalized_confidence,
            existing.sensitivity == normalized_sensitivity,
        )):
            raise LifeGraphConflict(
                "Life entity link already exists with different attributes"
            )
        return existing, False

    append_action_audit(
        db,
        owner_id=account.id,
        action="life.link.created",
        entity_type="entity_link",
        entity_id=link.id,
        reason=_AUDIT_LINK_CREATED,
        after_state=_structural_link_state(link),
        details={"relation": link.relation},
        reversible=True,
        undo_ref=f"life-link:{link.id}:1",
    )
    return link, True


def list_entity_links(
    db,
    *,
    owner_id: str,
    entity_id: object,
    direction: str = "both",
    relation: object | None = None,
    include_deleted: bool = False,
    limit: int = 100,
) -> tuple[list[EntityLink], bool]:
    entity = _owned_entity(
        db, owner_id, entity_id, include_deleted=include_deleted
    )
    normalized_direction = str(direction or "both").strip().lower()
    if normalized_direction not in {"both", "incoming", "outgoing"}:
        raise LifeGraphError("direction must be both, incoming, or outgoing")
    bounded = max(1, min(100, int(limit)))
    source_entity = aliased(LifeEntity)
    target_entity = aliased(LifeEntity)
    query = db.query(EntityLink).join(
        source_entity,
        and_(
            EntityLink.source_type == "life_entity",
            EntityLink.source_id == source_entity.id,
            source_entity.owner_id == owner_id,
        ),
    ).join(
        target_entity,
        and_(
            EntityLink.target_type == "life_entity",
            EntityLink.target_id == target_entity.id,
            target_entity.owner_id == owner_id,
        ),
    ).filter(EntityLink.owner_id == owner_id)
    if not include_deleted:
        query = query.filter(
            EntityLink.deleted_at.is_(None),
            source_entity.deleted_at.is_(None),
            target_entity.deleted_at.is_(None),
        )
    if normalized_direction == "outgoing":
        query = query.filter(EntityLink.source_id == entity.id)
    elif normalized_direction == "incoming":
        query = query.filter(EntityLink.target_id == entity.id)
    else:
        query = query.filter(or_(
            EntityLink.source_id == entity.id,
            EntityLink.target_id == entity.id,
        ))
    if relation:
        query = query.filter(
            EntityLink.relation == _token(relation, field="relation", limit=64)
        )
    rows = query.order_by(
        EntityLink.created_at.asc(), EntityLink.id.asc()
    ).limit(bounded + 1).all()
    return rows[:bounded], len(rows) > bounded


def _reserve_link_version(db, link: EntityLink, expected_version: int) -> None:
    if int(link.version or 1) != int(expected_version):
        raise LifeGraphConflict(
            f"Life entity link changed in another client (current version {int(link.version or 1)})"
        )
    next_version = int(expected_version) + 1
    now = utcnow_naive()
    updated = db.query(EntityLink).filter(
        EntityLink.id == link.id,
        EntityLink.owner_id == link.owner_id,
        EntityLink.version == int(expected_version),
    ).update(
        {EntityLink.version: next_version, EntityLink.updated_at: now},
        synchronize_session=False,
    )
    if updated != 1:
        db.expire_all()
        current = db.query(EntityLink.version).filter(
            EntityLink.id == link.id,
            EntityLink.owner_id == link.owner_id,
        ).scalar()
        raise LifeGraphConflict(
            f"Life entity link changed in another client (current version {int(current or 1)})"
        )
    link.version = next_version
    link.updated_at = now


def delete_entity_link(
    db,
    *,
    owner_id: str,
    link_id: object,
    expected_version: int,
    reason: object = "Life entity link deleted",
) -> EntityLink:
    link = _owned_link(db, owner_id, link_id)
    if link.deleted_at is not None:
        return link
    before = _structural_link_state(link)
    _reserve_link_version(db, link, expected_version)
    link.deleted_at = utcnow_naive()
    db.flush()
    append_action_audit(
        db,
        owner_id=owner_id,
        action="life.link.deleted",
        entity_type="entity_link",
        entity_id=link.id,
        reason=_AUDIT_LINK_DELETED,
        before_state=before,
        after_state=_structural_link_state(link),
        reversible=True,
        undo_ref=f"life-link:{link.id}:{link.version}",
    )
    return link


def search_life_entities(
    db,
    *,
    owner_id: str,
    query_text: object,
    entity_type: object | None = None,
    status: object | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    needle = str(query_text or "").strip().casefold()
    if not needle:
        raise LifeGraphError("q is required")
    bounded = max(1, min(100, int(limit)))
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.deleted_at.is_(None),
    )
    if entity_type:
        query = query.filter(LifeEntity.entity_type == _entity_type(entity_type))
    if status:
        query = query.filter(
            LifeEntity.status == _token(status, field="status", limit=32)
        )
    candidates = query.order_by(
        LifeEntity.updated_at.desc(), LifeEntity.id.desc()
    ).limit(SEARCH_SCAN_LIMIT + 1).all()
    scan_truncated = len(candidates) > SEARCH_SCAN_LIMIT
    candidates = candidates[:SEARCH_SCAN_LIMIT]

    matches: list[tuple[tuple[int, str, str], dict[str, Any]]] = []
    for entity in candidates:
        title = str(entity.title or "").casefold()
        summary = str(entity.summary or "").casefold()
        properties = json.dumps(
            entity.properties or {}, ensure_ascii=False, sort_keys=True
        ).casefold()
        if title == needle:
            rank, field = 0, "title"
        elif title.startswith(needle):
            rank, field = 1, "title"
        elif needle in title:
            rank, field = 2, "title"
        elif needle in summary:
            rank, field = 3, "summary"
        elif needle in properties:
            rank, field = 4, "properties"
        else:
            continue
        matches.append((
            (rank, str(entity.title or "").casefold(), entity.id),
            {"entity": serialize_life_entity(entity), "match": field, "rank": rank},
        ))
    matches.sort(key=lambda value: value[0])
    results = [value for _, value in matches[:bounded]]
    return {
        "items": results,
        "count": len(results),
        "scanned": len(candidates),
        "truncated": scan_truncated or len(matches) > bounded,
    }


def traverse_life_graph(
    db,
    *,
    owner_id: str,
    entity_id: object,
    depth: int = 2,
    limit: int = 100,
) -> dict[str, Any]:
    root = _owned_entity(db, owner_id, entity_id, include_deleted=False)
    bounded_depth = max(1, min(4, int(depth)))
    bounded_limit = max(1, min(200, int(limit)))
    entities: dict[str, LifeEntity] = {root.id: root}
    links: dict[str, EntityLink] = {}
    frontier: set[str] = {root.id}
    truncated = False
    depth_reached = 0

    for level in range(1, bounded_depth + 1):
        if not frontier or len(entities) >= bounded_limit:
            break
        rows = db.query(EntityLink).filter(
            EntityLink.owner_id == owner_id,
            EntityLink.source_type == "life_entity",
            EntityLink.target_type == "life_entity",
            EntityLink.deleted_at.is_(None),
            or_(
                EntityLink.source_id.in_(frontier),
                EntityLink.target_id.in_(frontier),
            ),
        ).order_by(
            EntityLink.created_at.asc(), EntityLink.id.asc()
        ).limit(TRAVERSAL_EDGE_SCAN_LIMIT + 1).all()
        if len(rows) > TRAVERSAL_EDGE_SCAN_LIMIT:
            truncated = True
            rows = rows[:TRAVERSAL_EDGE_SCAN_LIMIT]

        endpoint_ids = {
            endpoint
            for row in rows
            for endpoint in (row.source_id, row.target_id)
        }
        owned_rows = db.query(LifeEntity).filter(
            LifeEntity.owner_id == owner_id,
            LifeEntity.id.in_(endpoint_ids),
            LifeEntity.deleted_at.is_(None),
        ).all() if endpoint_ids else []
        owned = {row.id: row for row in owned_rows}
        next_frontier: set[str] = set()
        for link in rows:
            if link.source_id not in owned or link.target_id not in owned:
                continue
            other_ids = (
                {link.target_id} if link.source_id in frontier else set()
            ) | ({link.source_id} if link.target_id in frontier else set())
            admitted = True
            for other_id in sorted(other_ids):
                if other_id in entities:
                    continue
                if len(entities) >= bounded_limit:
                    truncated = True
                    admitted = False
                    continue
                entities[other_id] = owned[other_id]
                next_frontier.add(other_id)
            if admitted and link.source_id in entities and link.target_id in entities:
                links[link.id] = link
        frontier = next_frontier
        depth_reached = level

    return {
        "root_id": root.id,
        "entities": [serialize_life_entity(row) for row in entities.values()],
        "links": [serialize_entity_link(row) for row in links.values()],
        "depth_requested": bounded_depth,
        "depth_reached": depth_reached,
        "truncated": truncated,
    }


def list_decisions_for_review(
    db,
    *,
    owner_id: str,
    due_before: datetime | None = None,
    limit: int = 50,
) -> tuple[list[LifeEntity], bool]:
    cutoff = _naive_utc(due_before) or utcnow_naive()
    bounded = max(1, min(100, int(limit)))
    rows = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == "decision",
        LifeEntity.deleted_at.is_(None),
        LifeEntity.review_at.isnot(None),
        LifeEntity.review_at <= cutoff,
        ~LifeEntity.status.in_(("superseded", "cancelled", "deleted")),
    ).order_by(
        LifeEntity.review_at.asc(), LifeEntity.id.asc()
    ).limit(bounded + 1).all()
    return rows[:bounded], len(rows) > bounded


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _values(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _normalized_task_title(value: object) -> str:
    return " ".join(re.findall(r"[\w]+", str(value or "").casefold()))


def _task_deadline(task: LifeEntity, properties: Mapping[str, Any]) -> datetime | None:
    if task.due_at is not None:
        return _naive_utc(task.due_at)
    raw = properties.get("deadline")
    if isinstance(raw, datetime):
        return _naive_utc(raw)
    if isinstance(raw, date):
        return datetime.combine(raw, time.max)
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return datetime.combine(date.fromisoformat(value), time.max)
        return _naive_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        # Task metadata is connector-controlled and may predate the typed
        # deadline contract. Invalid legacy values stay visible in properties
        # but must not crash the entire quality report.
        return None


def task_quality_report(
    db,
    *,
    owner_id: str,
    now: datetime | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    cutoff = _naive_utc(now) or utcnow_naive()
    bounded = max(1, min(100, int(limit)))
    candidates = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == "task",
        LifeEntity.deleted_at.is_(None),
        ~LifeEntity.status.in_(tuple(_TERMINAL_TASK_STATUSES)),
    ).order_by(
        LifeEntity.due_at.asc(), LifeEntity.updated_at.desc(), LifeEntity.id.asc()
    ).limit(TASK_QUALITY_SCAN_LIMIT + 1).all()
    scan_truncated = len(candidates) > TASK_QUALITY_SCAN_LIMIT
    tasks = candidates[:TASK_QUALITY_SCAN_LIMIT]
    task_ids = {task.id for task in tasks}
    if not task_ids:
        return {
            "items": [], "count": 0, "scanned": 0,
            "flag_counts": {}, "truncated": scan_truncated,
        }

    base_link_filters = (
        EntityLink.owner_id == owner_id,
        EntityLink.source_type == "life_entity",
        EntityLink.target_type == "life_entity",
        EntityLink.deleted_at.is_(None),
    )
    direct_links = db.query(EntityLink).filter(
        *base_link_filters,
        or_(
            EntityLink.source_id.in_(task_ids),
            EntityLink.target_id.in_(task_ids),
        ),
    ).order_by(EntityLink.id.asc()).limit(
        TASK_QUALITY_LINK_SCAN_LIMIT + 1
    ).all()
    direct_link_truncated = len(direct_links) > TASK_QUALITY_LINK_SCAN_LIMIT
    direct_links = direct_links[:TASK_QUALITY_LINK_SCAN_LIMIT]

    # Follow only the declared upward relation direction. Querying the whole
    # owner's edge table and then taking its first N rows made unrelated edges
    # crowd out a task's actual Project -> Goal path.
    goal_links: list[EntityLink] = []
    goal_link_truncated = False
    frontier = set(task_ids)
    visited_goal_nodes = set(task_ids)
    for _ in range(5):
        if not frontier:
            break
        remaining = TASK_QUALITY_LINK_SCAN_LIMIT - len(goal_links)
        if remaining <= 0:
            goal_link_truncated = True
            break
        rows = db.query(EntityLink).filter(
            *base_link_filters,
            EntityLink.relation.in_(tuple(_GOAL_RELATIONS)),
            EntityLink.source_id.in_(frontier),
        ).order_by(EntityLink.id.asc()).limit(remaining + 1).all()
        if len(rows) > remaining:
            goal_link_truncated = True
            rows = rows[:remaining]
        goal_links.extend(rows)
        target_ids = {row.target_id for row in rows}
        owned_targets = {
            row[0]
            for row in db.query(LifeEntity.id).filter(
                LifeEntity.owner_id == owner_id,
                LifeEntity.id.in_(target_ids),
                LifeEntity.deleted_at.is_(None),
            ).all()
        } if target_ids else set()
        frontier = owned_targets - visited_goal_nodes
        visited_goal_nodes.update(frontier)

    links_by_id = {link.id: link for link in (*direct_links, *goal_links)}
    links = list(links_by_id.values())
    direct_link_ids = {link.id for link in direct_links}
    link_truncated = direct_link_truncated or goal_link_truncated
    endpoint_ids = {
        endpoint for link in links for endpoint in (link.source_id, link.target_id)
    }
    for task in tasks:
        props = task.properties or {}
        endpoint_ids.update(_values(props.get("goal_id")))
        endpoint_ids.update(_values(props.get("goal_ids")))
        endpoint_ids.update(_values(props.get("duplicate_of")))
    owned_entities = {
        row.id: row
        for row in db.query(LifeEntity).filter(
            LifeEntity.owner_id == owner_id,
            LifeEntity.id.in_(endpoint_ids),
            LifeEntity.deleted_at.is_(None),
        ).all()
    } if endpoint_ids else {}
    by_task: dict[str, list[EntityLink]] = defaultdict(list)
    goal_adjacency: dict[str, set[str]] = defaultdict(set)
    for link in links:
        if link.source_id not in owned_entities or link.target_id not in owned_entities:
            continue
        if link.source_id in task_ids:
            by_task[link.source_id].append(link)
        if link.target_id in task_ids and link.id in direct_link_ids:
            by_task[link.target_id].append(link)
        if link.relation in _GOAL_RELATIONS:
            goal_adjacency[link.source_id].add(link.target_id)

    def reaches_goal(task_id: str) -> bool:
        frontier = {task_id}
        visited = {task_id}
        for _ in range(5):
            next_frontier: set[str] = set()
            for current_id in frontier:
                for candidate_id in goal_adjacency.get(current_id, set()):
                    if candidate_id in visited:
                        continue
                    candidate = owned_entities.get(candidate_id)
                    if candidate is None:
                        continue
                    if candidate.entity_type == "goal":
                        return True
                    visited.add(candidate_id)
                    next_frontier.add(candidate_id)
            if not next_frontier:
                break
            frontier = next_frontier
        return False

    title_counts = Counter(
        normalized for normalized in (
            _normalized_task_title(task.title) for task in tasks
        ) if normalized
    )
    flag_counts: Counter[str] = Counter()
    report: list[dict[str, Any]] = []
    for task in tasks:
        props = task.properties or {}
        task_links = by_task.get(task.id, [])
        flags: list[str] = []
        deadline = _task_deadline(task, props)
        if deadline is not None and deadline < cutoff:
            flags.append("overdue")

        link_relations = {link.relation for link in task_links}
        if (
            task.status in _BLOCKED_STATUSES
            or _truthy(props.get("blocked"))
            or _values(props.get("blocked_by"))
            or _values(props.get("blocked_by_id"))
            or any(
                link.source_id == task.id
                and link.relation in _BLOCKING_RELATIONS
                for link in task_links
            )
        ):
            flags.append("blocked")
        if task.status in _WAITING_STATUSES or _values(props.get("waiting_on")):
            flags.append("waiting")
        if (
            task.status in _IRRELEVANT_STATUSES
            or _truthy(props.get("irrelevant"))
            or str(props.get("relevance") or "").strip().lower()
            in {"none", "irrelevant", "obsolete"}
        ):
            flags.append("irrelevant")

        has_action_link = any(
            link.relation in _NEXT_ACTION_RELATIONS
            and link.source_id == task.id
            and owned_entities.get(
                link.target_id
            ) is not None
            for link in task_links
        )
        property_actions = (
            _values(props.get("next_action"))
            + _values(props.get("next_action_id"))
            + _values(props.get("next_action_ids"))
        )
        if (
            not property_actions
            and not has_action_link
            and not direct_link_truncated
        ):
            flags.append("missing_next_action")

        normalized_title = _normalized_task_title(task.title)
        duplicate_ref = _values(props.get("duplicate_of"))
        if (
            (normalized_title and title_counts[normalized_title] > 1)
            or any(
                value in owned_entities
                and owned_entities[value].entity_type == "task"
                for value in duplicate_ref
            )
            or bool(link_relations & _DUPLICATE_RELATIONS)
        ):
            flags.append("duplicate")

        property_goals = {
            value for value in (
                _values(props.get("goal_id")) + _values(props.get("goal_ids"))
            )
            if value in owned_entities
            and owned_entities[value].entity_type == "goal"
        }
        linked_goal = reaches_goal(task.id)
        if not property_goals and not linked_goal and not goal_link_truncated:
            flags.append("goal_disconnected")

        flag_counts.update(flags)
        report.append({"entity": serialize_life_entity(task), "flags": flags})

    report.sort(key=lambda item: (
        -len(item["flags"]),
        item["entity"].get("due_at") or "9999",
        str(item["entity"].get("title") or "").casefold(),
        item["entity"]["id"],
    ))
    return {
        "items": report[:bounded],
        "count": min(len(report), bounded),
        "scanned": len(tasks),
        "flag_counts": dict(sorted(flag_counts.items())),
        "truncated": scan_truncated or link_truncated or len(report) > bounded,
    }
