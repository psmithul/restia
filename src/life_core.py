"""Deterministic V3 universal-inbox domain service.

The first vertical slice intentionally reuses the existing Planning service for
human tasks and creates only explicit graph links for project information.
Kinds whose safe domain adapter is not yet available remain classified and
reviewable; processing them fails explicitly without changing state.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from core.database import (
    Account,
    ActionAudit,
    EntityLink,
    InboxItem,
    LifeEntity,
    Project,
    ProjectMember,
    utcnow_naive,
)
from src.audit_context import build_action_audit_details
from src.planning import create_planning_item


INBOX_KINDS = frozenset({
    "task",
    "event",
    "note",
    "person_update",
    "project_information",
    "decision",
    "reference_material",
    "expense",
    "goal",
    "habit",
    "someday_idea",
    "archive",
})
INBOX_STATUSES = frozenset({"inbox", "processed", "archived"})
PROCESSABLE_KINDS = INBOX_KINDS
MAX_INBOX_METADATA_BYTES = 32 * 1024

_LIFE_ENTITY_KIND_MAP = {
    "event": "event",
    "note": "note",
    "person_update": "interaction",
    "decision": "decision",
    "reference_material": "source",
    "expense": "transaction",
    "goal": "goal",
    "habit": "habit",
    "someday_idea": "note",
}


class LifeCoreError(ValueError):
    pass


class LifeCoreNotFound(LifeCoreError):
    pass


class LifeCoreConflict(LifeCoreError):
    pass


class LifeCoreUnsupported(LifeCoreError):
    def __init__(self, kind: str, message: str | None = None):
        self.kind = kind
        super().__init__(message or f"Processing is not implemented for inbox kind '{kind}'")


_CLASSIFIERS: tuple[tuple[str, re.Pattern[str], int, str], ...] = (
    ("archive", re.compile(r"\b(?:archive|ignore this|no action|for records only)\b", re.I), 96, "explicit archive intent"),
    ("expense", re.compile(r"(?:\b(?:receipt|invoice|expense|spent|paid|purchase|subscription charge)\b|(?:₹|\$|€|£)\s?\d)", re.I), 94, "financial or receipt language"),
    ("decision", re.compile(r"\b(?:decision|decided|chose|chosen|approved option|we will use)\b", re.I), 93, "decision language"),
    ("habit", re.compile(r"\b(?:habit|routine|every day|each day|daily practice|every morning|every evening)\b", re.I), 92, "recurring behavior language"),
    ("goal", re.compile(r"\b(?:goal|objective|aim to|target outcome|want to achieve)\b", re.I), 91, "goal or objective language"),
    ("event", re.compile(r"\b(?:meeting|appointment|calendar event|reservation|flight|class at|call at|interview at)\b|\b(?:today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(?:at\s+)?\d{1,2}(?::\d{2})?\b", re.I), 90, "scheduled event language"),
    ("person_update", re.compile(r"\b(?:spoke with|met with|heard from|called|emailed|promised|told)\s+[A-Z][\w.-]+", re.I), 88, "interaction with a person"),
    ("someday_idea", re.compile(r"\b(?:someday|maybe later|one day|idea for|could build|might build)\b", re.I), 85, "non-committed future idea"),
    ("project_information", re.compile(r"\b(?:project|milestone|sprint|roadmap|deliverable|requirement|workstream)\b", re.I), 86, "project context language"),
    ("task", re.compile(r"\b(?:todo|to-do|task|remind me|follow up|need to|must|finish|complete|submit|send|buy|book|call)\b", re.I), 84, "action or commitment language"),
    ("reference_material", re.compile(r"(?:https?://|\bwww\.|\b(?:paper|article|bookmark|reference|research source|watch later|read later)\b)", re.I), 82, "reference material language"),
)


def normalize_kind(value: object) -> str:
    kind = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if kind not in INBOX_KINDS:
        raise LifeCoreError("Unknown inbox kind")
    return kind


def normalize_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LifeCoreError("metadata must be an object")
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise LifeCoreError("metadata must be JSON-serializable") from exc
    if len(encoded) > MAX_INBOX_METADATA_BYTES:
        raise LifeCoreError(
            f"metadata must not exceed {MAX_INBOX_METADATA_BYTES} bytes"
        )
    return dict(value)


def classify_inbox_text(title: object = "", content: object = "") -> dict[str, Any]:
    """Classify capture text without an LLM or external state."""

    text = " ".join(part for part in (str(title or "").strip(), str(content or "").strip()) if part)
    for kind, pattern, confidence, reason in _CLASSIFIERS:
        if pattern.search(text):
            return {"kind": kind, "confidence": confidence, "reason": reason}
    return {"kind": "note", "confidence": 60, "reason": "default unstructured note"}


def serialize_inbox_item(item: InboxItem) -> dict[str, Any]:
    def iso(value: datetime | None) -> str | None:
        return value.isoformat() + "Z" if value else None

    return {
        "id": item.id,
        "title": item.title or "",
        "content": item.content or "",
        "kind": item.kind,
        "status": item.status,
        "source_type": item.source_type,
        "source_ref": item.source_ref,
        "metadata": item.meta_data or {},
        "classification_confidence": int(item.classification_confidence or 0),
        "classification_reason": item.classification_reason or "",
        "processed_target_type": item.processed_target_type,
        "processed_target_id": item.processed_target_id,
        "processed_at": iso(item.processed_at),
        "archived_at": iso(item.archived_at),
        "version": int(item.version or 1),
        "created_at": iso(item.created_at),
        "updated_at": iso(item.updated_at),
    }


def _state(item: InboxItem) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "status": item.status,
        "version": int(item.version or 1),
        "target_type": item.processed_target_type,
        "target_id": item.processed_target_id,
    }


def append_action_audit(
    db,
    *,
    owner_id: str,
    action: str,
    entity_type: str,
    entity_id: str,
    reason: str,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
    idempotency_ref: object | None = None,
    reversible: bool = False,
    undo_ref: object | None = None,
    outcome: str = "success",
) -> ActionAudit:
    audit = ActionAudit(
        id=str(uuid.uuid4()),
        owner_id=owner_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_state=dict(before_state or {}),
        after_state=dict(after_state or {}),
        details=build_action_audit_details(
            db,
            owner_id=owner_id,
            reason=reason,
            outcome=outcome,
            idempotency_ref=idempotency_ref,
            reversible=reversible,
            undo_ref=undo_ref,
            domain_details=details,
        ),
    )
    db.add(audit)
    db.flush()
    return audit


def _owned_item(db, owner_id: str, item_id: str) -> InboxItem:
    item = (
        db.query(InboxItem)
        .filter(InboxItem.id == str(item_id), InboxItem.owner_id == owner_id)
        .first()
    )
    if item is None:
        raise LifeCoreNotFound("Inbox item not found")
    return item


def get_inbox_item(db, *, owner_id: str, item_id: str) -> InboxItem:
    """Return one item through the same fail-closed owner gate as mutations."""

    return _owned_item(db, owner_id, item_id)


def _check_version(item: InboxItem, expected_version: int) -> None:
    if int(item.version or 1) != int(expected_version):
        raise LifeCoreConflict(
            f"Inbox item changed in another client (current version {int(item.version or 1)})"
        )


def _reserve_version(db, item: InboxItem, expected_version: int) -> None:
    _check_version(item, expected_version)
    now = utcnow_naive()
    next_version = int(expected_version) + 1
    updated = (
        db.query(InboxItem)
        .filter(
            InboxItem.id == item.id,
            InboxItem.owner_id == item.owner_id,
            InboxItem.version == int(expected_version),
        )
        .update(
            {InboxItem.version: next_version, InboxItem.updated_at: now},
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.expire_all()
        current = (
            db.query(InboxItem.version)
            .filter(InboxItem.id == item.id, InboxItem.owner_id == item.owner_id)
            .scalar()
        )
        raise LifeCoreConflict(
            f"Inbox item changed in another client (current version {int(current or 1)})"
        )
    item.version = next_version
    item.updated_at = now


def create_inbox_item(
    db,
    *,
    account: Account,
    title: object = "",
    content: object = "",
    kind: object | None = None,
    source_type: object = "user",
    source_ref: object | None = None,
    metadata: dict[str, Any] | None = None,
    idempotency_key: object | None = None,
) -> tuple[InboxItem, bool]:
    clean_title = " ".join(str(title or "").split())[:240]
    clean_content = str(content or "").strip()[:100_000]
    if not clean_title and not clean_content:
        raise LifeCoreError("title or content is required")
    raw_key = str(idempotency_key or "").strip()
    key = (
        "sha256:" + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()
        if raw_key else None
    )
    if kind is None:
        classification = classify_inbox_text(clean_title, clean_content)
    else:
        classification = {
            "kind": normalize_kind(kind),
            "confidence": 100,
            "reason": "explicit capture kind",
        }
    clean_source_type = (
        str(source_type or "user").strip().lower() or "user"
    )[:48]
    clean_source_ref = (
        str(source_ref).strip()[:500] if source_ref is not None else None
    )
    clean_metadata = normalize_metadata(metadata or {})

    def matches(existing: InboxItem) -> bool:
        return all((
            existing.title == clean_title,
            existing.content == clean_content,
            existing.kind == classification["kind"],
            existing.source_type == clean_source_type,
            existing.source_ref == clean_source_ref,
            dict(existing.meta_data or {}) == clean_metadata,
        ))

    if key:
        existing = (
            db.query(InboxItem)
            .filter(InboxItem.owner_id == account.id, InboxItem.idempotency_key == key)
            .first()
        )
        if existing is not None:
            if not matches(existing):
                raise LifeCoreConflict(
                    "Inbox idempotency key was already used for a different capture"
                )
            return existing, False

    item = InboxItem(
        id=str(uuid.uuid4()),
        owner_id=account.id,
        title=clean_title,
        content=clean_content,
        kind=classification["kind"],
        status="inbox",
        source_type=clean_source_type,
        source_ref=clean_source_ref,
        meta_data=clean_metadata,
        classification_confidence=classification["confidence"],
        classification_reason=classification["reason"],
        idempotency_key=key,
        version=1,
    )
    try:
        # A unique owner/key constraint is the authority.  The savepoint lets
        # simultaneous retries converge on the committed winner instead of
        # turning the losing request into a 500.
        with db.begin_nested():
            db.add(item)
            db.flush()
    except IntegrityError:
        if not key:
            raise
        existing = (
            db.query(InboxItem)
            .filter(InboxItem.owner_id == account.id, InboxItem.idempotency_key == key)
            .first()
        )
        if existing is None:
            raise
        if not matches(existing):
            raise LifeCoreConflict(
                "Inbox idempotency key was already used for a different capture"
            )
        return existing, False
    append_action_audit(
        db,
        owner_id=account.id,
        action="inbox.created",
        entity_type="inbox_item",
        entity_id=item.id,
        reason="Capture entered Universal Inbox",
        after_state=_state(item),
        details={"source_type": item.source_type},
        idempotency_ref=item.idempotency_key,
    )
    return item, True


def list_inbox_items(
    db,
    *,
    owner_id: str,
    status: str = "inbox",
    kind: str | None = None,
    limit: int = 50,
    before_updated_at: datetime | None = None,
    before_id: str | None = None,
) -> tuple[list[InboxItem], bool]:
    normalized_status = str(status or "inbox").strip().lower()
    if normalized_status not in {"all", *INBOX_STATUSES}:
        raise LifeCoreError("status must be all, inbox, processed, or archived")
    query = db.query(InboxItem).filter(InboxItem.owner_id == owner_id)
    if normalized_status != "all":
        query = query.filter(InboxItem.status == normalized_status)
    if kind:
        query = query.filter(InboxItem.kind == normalize_kind(kind))
    if (before_updated_at is None) != (before_id is None):
        raise LifeCoreError("Inbox pagination requires both cursor fields")
    if before_updated_at is not None and before_id is not None:
        query = query.filter(
            or_(
                InboxItem.updated_at < before_updated_at,
                and_(
                    InboxItem.updated_at == before_updated_at,
                    InboxItem.id < before_id,
                ),
            )
        )
    bounded = max(1, min(100, int(limit)))
    rows = (
        query.order_by(InboxItem.updated_at.desc(), InboxItem.id.desc())
        .limit(bounded + 1)
        .all()
    )
    return rows[:bounded], len(rows) > bounded


_UNSET = object()


def update_inbox_item(
    db,
    *,
    owner_id: str,
    item_id: str,
    expected_version: int,
    title: Any = _UNSET,
    content: Any = _UNSET,
    kind: Any = _UNSET,
    source_ref: Any = _UNSET,
    metadata: Any = _UNSET,
) -> InboxItem:
    item = _owned_item(db, owner_id, item_id)
    _check_version(item, expected_version)
    if item.status != "inbox":
        raise LifeCoreConflict("Only inbox items can be edited")
    before = _state(item)
    changes: dict[str, Any] = {}
    if title is not _UNSET:
        value = " ".join(str(title or "").split())[:240]
        if value != item.title:
            changes["title"] = value
    if content is not _UNSET:
        value = str(content or "").strip()[:100_000]
        if value != item.content:
            changes["content"] = value
    if kind is not _UNSET:
        value = normalize_kind(kind)
        if value != item.kind:
            changes["kind"] = value
            changes["classification_confidence"] = 100
            changes["classification_reason"] = "explicit inbox edit"
    if source_ref is not _UNSET:
        value = str(source_ref).strip()[:500] if source_ref is not None else None
        if value != item.source_ref:
            changes["source_ref"] = value
    if metadata is not _UNSET:
        value = normalize_metadata(metadata)
        if value != (item.meta_data or {}):
            changes["meta_data"] = value
    if not changes:
        return item
    if not changes.get("title", item.title) and not changes.get("content", item.content):
        raise LifeCoreError("title or content is required")

    _reserve_version(db, item, expected_version)
    for field, value in changes.items():
        setattr(item, field, value)
    db.flush()
    append_action_audit(
        db,
        owner_id=owner_id,
        action="inbox.updated",
        entity_type="inbox_item",
        entity_id=item.id,
        reason="Inbox capture fields changed",
        before_state=before,
        after_state=_state(item),
        details={"fields": sorted(changes)},
    )
    return item


def classify_inbox_item(
    db,
    *,
    owner_id: str,
    item_id: str,
    expected_version: int,
) -> InboxItem:
    item = _owned_item(db, owner_id, item_id)
    _check_version(item, expected_version)
    if item.status != "inbox":
        raise LifeCoreConflict("Only inbox items can be classified")
    result = classify_inbox_text(item.title, item.content)
    if (
        result["kind"] == item.kind
        and result["confidence"] == int(item.classification_confidence or 0)
        and result["reason"] == (item.classification_reason or "")
    ):
        return item
    before = _state(item)
    _reserve_version(db, item, expected_version)
    item.kind = result["kind"]
    item.classification_confidence = result["confidence"]
    item.classification_reason = result["reason"]
    db.flush()
    append_action_audit(
        db,
        owner_id=owner_id,
        action="inbox.classified",
        entity_type="inbox_item",
        entity_id=item.id,
        reason="Inbox classification refreshed",
        before_state=before,
        after_state=_state(item),
        details={"reason": result["reason"]},
    )
    return item


def _ensure_link(
    db,
    *,
    owner_id: str,
    source_id: str,
    relation: str,
    target_type: str,
    target_id: str,
) -> tuple[EntityLink, bool]:
    link = (
        db.query(EntityLink)
        .filter(
            EntityLink.owner_id == owner_id,
            EntityLink.source_type == "inbox_item",
            EntityLink.source_id == source_id,
            EntityLink.relation == relation,
            EntityLink.target_type == target_type,
            EntityLink.target_id == target_id,
        )
        .first()
    )
    if link is not None:
        return link, False
    link = EntityLink(
        id=str(uuid.uuid4()),
        owner_id=owner_id,
        source_type="inbox_item",
        source_id=source_id,
        relation=relation,
        target_type=target_type,
        target_id=target_id,
    )
    db.add(link)
    db.flush()
    append_action_audit(
        db,
        owner_id=owner_id,
        action="entity.linked",
        entity_type="entity_link",
        entity_id=link.id,
        reason="Inbox item linked to a domain entity",
        after_state={
            "source_type": "inbox_item",
            "source_id": source_id,
            "relation": relation,
            "target_type": target_type,
            "target_id": target_id,
        },
    )
    return link, True


def _inbox_life_source(db, *, account: Account, item: InboxItem):
    """Create one immutable provenance record for an explicitly processed capture."""

    # Local import avoids a module cycle: life_graph reuses this module's
    # append-only audit helper.
    from src.life_graph import LifeGraphError, create_life_source

    digest_input = f"{item.title or ''}\n{item.content or ''}".encode("utf-8")
    source_type = re.sub(
        r"[^a-z0-9_.-]+", "_", str(item.source_type or "inbox").strip().lower()
    ).strip("_.-") or "inbox"
    try:
        source, _ = create_life_source(
            db,
            account=account,
            source_type=source_type[:48],
            title=item.title or (item.content or "")[:240],
            source_ref=f"inbox_item:{item.id}",
            safe_excerpt=(item.content or item.title or "")[:4_000],
            content_sha256=hashlib.sha256(digest_input).hexdigest(),
            observed_at=item.created_at,
            metadata={
                "inbox_item_id": item.id,
                "classification_kind": item.kind,
            },
            idempotency_key=f"inbox-source:{item.id}",
        )
    except LifeGraphError as exc:
        raise LifeCoreError(f"Could not create Inbox provenance: {exc}") from exc
    return source


def _inbox_entity_properties(item: InboxItem) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    if item.kind == "task":
        # The PlanningItem remains the execution authority, while the graph
        # representation carries the complete task-quality contract from day
        # one. Connectors can fill these fields at capture time without
        # creating another task store.
        properties = {
            "definition_of_done": "",
            "priority": "normal",
            "deadline": None,
            "effort_minutes": None,
            "energy": "any",
            "context": [],
            "project_id": None,
            "people_ids": [],
            "dependency_ids": [],
            "document_ids": [],
            "source": "inbox",
            # Unknown is materially different from repeating the task title:
            # the latter suppresses the missing-next-action detector while
            # providing no executable next step.
            "next_action": "",
            "completion_evidence": [],
        }
    properties.update(dict(item.meta_data or {}))
    properties.update({
        "inbox_item_id": item.id,
        "inbox_kind": item.kind,
    })
    return properties


def _create_inbox_life_entity(
    db,
    *,
    account: Account,
    item: InboxItem,
    source_id: str,
    entity_type: str,
    title: str | None = None,
    status: str = "active",
    domain_ref_type: str | None = None,
    domain_ref_id: str | None = None,
    idempotency_suffix: str = "entity",
):
    from src.life_graph import LifeGraphError, create_life_entity

    try:
        entity, _ = create_life_entity(
            db,
            account=account,
            entity_type=entity_type,
            title=title or item.title or (item.content or "")[:240] or "Inbox capture",
            summary=item.content if item.content != item.title else "",
            status=status,
            properties=_inbox_entity_properties(item),
            provenance={"source_id": source_id},
            domain_ref_type=domain_ref_type,
            domain_ref_id=domain_ref_id,
            idempotency_key=f"inbox-{idempotency_suffix}:{item.id}",
            reason=f"Inbox {item.kind} processed into the life graph",
        )
    except LifeGraphError as exc:
        raise LifeCoreError(f"Could not create Inbox life record: {exc}") from exc
    return entity


def _ensure_project_graph_entity(
    db,
    *,
    account: Account,
    project: Project,
    source_id: str,
) -> LifeEntity:
    existing = db.query(LifeEntity).filter(
        LifeEntity.owner_id == account.id,
        LifeEntity.entity_type == "project",
        LifeEntity.domain_ref_type == "project",
        LifeEntity.domain_ref_id == project.id,
    ).first()
    if existing is not None:
        if existing.deleted_at is not None:
            raise LifeCoreConflict("The project's Life record is deleted")
        return existing

    # Project fields remain authoritative in the existing Project table. The
    # graph node is only its principal-scoped cross-domain representation.
    from src.life_graph import LifeGraphError, create_life_entity

    try:
        entity, _ = create_life_entity(
            db,
            account=account,
            entity_type="project",
            title=project.name or project.key or "Project",
            summary="",
            status="active",
            properties={"project_key": project.key or ""},
            provenance={"source_id": source_id},
            domain_ref_type="project",
            domain_ref_id=project.id,
            idempotency_key=f"project-domain:{project.id}",
            reason="Existing project represented in the life graph",
        )
    except LifeGraphError as exc:
        raise LifeCoreError(f"Could not represent project in the life graph: {exc}") from exc
    return entity


def _claim_accessible_project(
    db,
    *,
    account: Account,
    project_id: str,
) -> Project:
    """Revalidate project access and hold it through adapter commit.

    The no-op writes are deliberate: PostgreSQL holds row locks and SQLite
    holds its writer lock, so ownership changes, membership revocation, or
    project deletion cannot race the graph records created afterward.
    """

    project = db.query(Project).filter(Project.id == project_id).first()
    if project is None:
        raise LifeCoreNotFound("Project not found")
    claimed_project = db.query(Project).filter(
        Project.id == project_id,
    ).update(
        {Project.updated_at: Project.updated_at},
        synchronize_session=False,
    )
    if claimed_project != 1:
        raise LifeCoreNotFound("Project not found")
    db.refresh(project)
    if project.owner == account.username:
        return project
    claimed_membership = db.query(ProjectMember).filter(
        ProjectMember.project_id == project_id,
        ProjectMember.username == account.username,
    ).update(
        {ProjectMember.role: ProjectMember.role},
        synchronize_session=False,
    )
    if claimed_membership != 1:
        raise LifeCoreNotFound("Project not found")
    return project


def _connect_life_entities(
    db,
    *,
    account: Account,
    source_id: str,
    relation: str,
    target_id: str,
    reason: str,
) -> None:
    from src.life_graph import LifeGraphError, create_entity_link

    try:
        create_entity_link(
            db,
            account=account,
            source_id=source_id,
            relation=relation,
            target_id=target_id,
            reason=reason,
        )
    except LifeGraphError as exc:
        raise LifeCoreError(f"Could not link Inbox life records: {exc}") from exc


def process_inbox_item(
    db,
    *,
    account: Account,
    item_id: str,
    expected_version: int,
    project_id: str | None = None,
) -> InboxItem:
    item = _owned_item(db, account.id, item_id)
    # Safe idempotent retry after a committed response was lost. No new target,
    # link, audit row, or version is produced for an already-final item.
    if item.status in {"processed", "archived"}:
        if item.status == "processed" and item.kind == "project_information":
            requested_project = str(project_id or "").strip()
            if (
                requested_project != str(item.processed_target_id or "")
                or item.processed_target_type != "project"
            ):
                raise LifeCoreConflict(
                    "Processed project information cannot be retried with a different project"
                )
        return item

    if item.kind not in PROCESSABLE_KINDS:
        raise LifeCoreUnsupported(
            item.kind,
            f"Inbox kind '{item.kind}' is classified and reviewable, but its safe processing adapter is not implemented",
        )
    _check_version(item, expected_version)
    before = _state(item)

    if item.kind == "archive":
        _reserve_version(db, item, expected_version)
        item.status = "archived"
        item.archived_at = utcnow_naive()
        action = "inbox.archived"
        details: dict[str, Any] = {"via": "process"}
    elif item.kind == "task":
        _reserve_version(db, item, expected_version)
        source = _inbox_life_source(db, account=account, item=item)
        task = create_planning_item(
            db,
            owner=account.username,
            title=item.title or item.content[:240],
            details=item.content if item.content != item.title else "",
            source="inbox",
        )
        entity = _create_inbox_life_entity(
            db,
            account=account,
            item=item,
            source_id=source.id,
            entity_type="task",
            status="open",
            domain_ref_type="planning_item",
            domain_ref_id=task.id,
            idempotency_suffix="task",
        )
        _ensure_link(
            db,
            owner_id=account.id,
            source_id=item.id,
            relation="created",
            target_type="planning_item",
            target_id=task.id,
        )
        _ensure_link(
            db,
            owner_id=account.id,
            source_id=item.id,
            relation="represents",
            target_type="life_entity",
            target_id=entity.id,
        )
        item.status = "processed"
        item.processed_target_type = "planning_item"
        item.processed_target_id = task.id
        item.processed_at = utcnow_naive()
        action = "inbox.processed"
        details = {
            "adapter": "planning_with_life_graph",
            "target_id": task.id,
            "life_entity_id": entity.id,
        }
    elif item.kind == "project_information":
        project_key = str(project_id or "").strip()
        if not project_key:
            raise LifeCoreUnsupported(
                item.kind,
                "Project information requires an explicit accessible project_id",
            )
        project = _claim_accessible_project(
            db,
            account=account,
            project_id=project_key,
        )
        _reserve_version(db, item, expected_version)
        source = _inbox_life_source(db, account=account, item=item)
        project_entity = _ensure_project_graph_entity(
            db, account=account, project=project, source_id=source.id
        )
        information_entity = _create_inbox_life_entity(
            db,
            account=account,
            item=item,
            source_id=source.id,
            entity_type="note",
            idempotency_suffix="project-information",
        )
        _connect_life_entities(
            db,
            account=account,
            source_id=information_entity.id,
            relation="about",
            target_id=project_entity.id,
            reason="Captured project information linked to its project",
        )
        _ensure_link(
            db,
            owner_id=account.id,
            source_id=item.id,
            relation="about",
            target_type="project",
            target_id=project.id,
        )
        _ensure_link(
            db,
            owner_id=account.id,
            source_id=item.id,
            relation="represents",
            target_type="life_entity",
            target_id=information_entity.id,
        )
        item.status = "processed"
        item.processed_target_type = "project"
        item.processed_target_id = project.id
        item.processed_at = utcnow_naive()
        action = "inbox.processed"
        details = {
            "adapter": "project_link_with_life_graph",
            "target_id": project.id,
            "life_entity_id": information_entity.id,
            "project_entity_id": project_entity.id,
        }
    else:
        entity_type = _LIFE_ENTITY_KIND_MAP.get(item.kind)
        if entity_type is None:
            raise LifeCoreUnsupported(item.kind)
        _reserve_version(db, item, expected_version)
        source = _inbox_life_source(db, account=account, item=item)
        entity = _create_inbox_life_entity(
            db,
            account=account,
            item=item,
            source_id=source.id,
            entity_type=entity_type,
            status="someday" if item.kind == "someday_idea" else "active",
            domain_ref_type="life_source" if item.kind == "reference_material" else None,
            domain_ref_id=source.id if item.kind == "reference_material" else None,
            idempotency_suffix=item.kind,
        )
        _ensure_link(
            db,
            owner_id=account.id,
            source_id=item.id,
            relation="represents",
            target_type="life_entity",
            target_id=entity.id,
        )
        item.status = "processed"
        item.processed_target_type = "life_entity"
        item.processed_target_id = entity.id
        item.processed_at = utcnow_naive()
        action = "inbox.processed"
        details = {
            "adapter": "life_graph",
            "target_id": entity.id,
            "entity_type": entity.entity_type,
        }

    db.flush()
    append_action_audit(
        db,
        owner_id=account.id,
        action=action,
        entity_type="inbox_item",
        entity_id=item.id,
        reason=(
            "Inbox item archived during processing"
            if action == "inbox.archived"
            else "Inbox item processed through a safe domain adapter"
        ),
        before_state=before,
        after_state=_state(item),
        details=details,
        idempotency_ref=item.idempotency_key,
    )
    return item


def archive_inbox_item(
    db,
    *,
    owner_id: str,
    item_id: str,
    expected_version: int,
) -> InboxItem:
    item = _owned_item(db, owner_id, item_id)
    if item.status == "archived":
        return item
    before = _state(item)
    _reserve_version(db, item, expected_version)
    item.status = "archived"
    item.archived_at = utcnow_naive()
    db.flush()
    append_action_audit(
        db,
        owner_id=owner_id,
        action="inbox.archived",
        entity_type="inbox_item",
        entity_id=item.id,
        reason="Inbox item archived",
        before_state=before,
        after_state=_state(item),
    )
    return item
