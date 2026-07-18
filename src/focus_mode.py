"""Recoverable, principal-scoped focus sessions for the V3 life graph.

The database partial unique index is the final authority for the one-live-
session invariant.  This service adds owner gates, state transitions,
optimistic compare-and-swap updates, bounded journals, and immutable action
audits around that authority.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from core.database import (
    Account,
    EntityLink,
    FocusSession,
    LifeEntity,
    PlanningItem,
    utcnow_naive,
)
from src.life_core import append_action_audit
from src.life_graph import create_life_entity
from src.planning import create_planning_item, normalize_planning_owner


FOCUSABLE_ENTITY_TYPES = frozenset({"task", "action", "milestone"})
FOCUSABLE_ENTITY_STATUSES = frozenset({"active", "open", "in_progress"})
LIVE_FOCUS_STATES = frozenset({"active", "paused"})
TERMINAL_FOCUS_STATES = frozenset({"completed", "abandoned"})
MAX_FOCUS_ENTRIES = 100
MAX_ENTRY_TEXT_LENGTH = 4_000
MAX_ENTRY_METADATA_BYTES = 16 * 1024
MAX_DEFINITION_OF_DONE_LENGTH = 20_000
MAX_FOLLOW_UPS = 20
MAX_FOCUS_CONTEXT = 12
FOCUS_CONTEXT_ENTITY_TYPES = frozenset({
    "note", "file", "project", "source", "workspace",
})


class FocusError(ValueError):
    """Base class for controlled focus-domain failures."""


class FocusNotFound(FocusError):
    pass


class FocusConflict(FocusError):
    pass


def _as_naive_utc(value: datetime | None = None) -> datetime:
    value = value or utcnow_naive()
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _clean_required_text(value: object, *, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise FocusError(f"{field} is required")
    if len(text) > limit:
        raise FocusError(f"{field} must not exceed {limit} characters")
    return text


def _clean_optional_text(value: object, *, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise FocusError(f"{field} must not exceed {limit} characters")
    return text


def _clean_metadata(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise FocusError("metadata must be an object")
    metadata = dict(value)
    try:
        encoded = json.dumps(
            metadata, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise FocusError("metadata must be JSON-serializable") from exc
    if len(encoded) > MAX_ENTRY_METADATA_BYTES:
        raise FocusError(
            f"metadata must not exceed {MAX_ENTRY_METADATA_BYTES} bytes"
        )
    return metadata


def _entries(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    raw = value.get("entries")
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _follow_up_ids(value: object) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    raw = value.get("ids")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item or "").strip()]


def _state(session: FocusSession) -> dict[str, Any]:
    return {
        "state": session.state,
        "version": int(session.version or 1),
        "entity_id": session.entity_id,
        "elapsed_seconds": int(session.elapsed_seconds or 0),
        "interruption_count": len(_entries(session.interruptions)),
        "progress_count": len(_entries(session.progress)),
        "evidence_count": len(_entries(session.evidence)),
        "follow_up_count": len(_follow_up_ids(session.follow_up_entity_ids)),
    }


def effective_elapsed_seconds(
    session: FocusSession, *, now: datetime | None = None
) -> int:
    """Return persisted time plus the current active interval, if any."""

    elapsed = max(0, int(session.elapsed_seconds or 0))
    if session.state != "active" or session.active_since is None:
        return elapsed
    current = _as_naive_utc(now)
    return elapsed + max(0, int((current - session.active_since).total_seconds()))


def serialize_focus_session(
    session: FocusSession,
    *,
    now: datetime | None = None,
    entity: LifeEntity | None = None,
    context: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": session.id,
        "entity_id": session.entity_id,
        "state": session.state,
        "definition_of_done": session.definition_of_done or "",
        "started_at": _iso_utc(session.started_at),
        "active_since": _iso_utc(session.active_since),
        "paused_at": _iso_utc(session.paused_at),
        "completed_at": _iso_utc(session.completed_at),
        "elapsed_seconds": effective_elapsed_seconds(session, now=now),
        "interruptions": _entries(session.interruptions),
        "progress": _entries(session.progress),
        "evidence": _entries(session.evidence),
        "follow_up_entity_ids": _follow_up_ids(session.follow_up_entity_ids),
        "version": int(session.version or 1),
        "created_at": _iso_utc(session.created_at),
        "updated_at": _iso_utc(session.updated_at),
    }
    if entity is not None:
        payload["entity"] = {
            "id": entity.id,
            "entity_type": entity.entity_type,
            "title": entity.title or "",
            "summary": entity.summary or "",
            "status": entity.status,
            "properties": dict(entity.properties or {}),
            "domain_ref_type": entity.domain_ref_type,
            "domain_ref_id": entity.domain_ref_id,
            "version": int(entity.version or 1),
        }
    payload["context"] = [dict(item) for item in (context or [])]
    return payload


def _owned_entity(db: Any, owner_id: str, entity_id: str) -> LifeEntity:
    entity = (
        db.query(LifeEntity)
        .filter(
            LifeEntity.id == str(entity_id),
            LifeEntity.owner_id == owner_id,
        )
        .first()
    )
    if entity is None:
        raise FocusNotFound("Life entity not found")
    return entity


def _focusable_entity(
    db: Any,
    *,
    owner_id: str,
    entity_id: str,
    expected_version: int,
) -> LifeEntity:
    entity = _owned_entity(db, owner_id, entity_id)
    if int(entity.version or 1) != int(expected_version):
        raise FocusConflict(
            "Life entity changed in another client "
            f"(current version {int(entity.version or 1)})"
        )
    if (
        entity.deleted_at is not None
        or entity.status not in FOCUSABLE_ENTITY_STATUSES
        or entity.entity_type not in FOCUSABLE_ENTITY_TYPES
    ):
        raise FocusConflict(
            "Focus can start only on an owned actionable task, action, or milestone"
        )
    return entity


def _claim_focusable_entity(
    db: Any,
    *,
    entity: LifeEntity,
    expected_version: int,
) -> LifeEntity:
    """Atomically revalidate and lock the target through focus insertion.

    A conditional no-op update is portable across SQLite and PostgreSQL.  It
    obtains the database writer/row lock while ensuring a concurrent status,
    deletion, type, ownership, or version change cannot slip between the
    preflight read and the focus-session insert.
    """

    claimed = (
        db.query(LifeEntity)
        .filter(
            LifeEntity.id == entity.id,
            LifeEntity.owner_id == entity.owner_id,
            LifeEntity.version == int(expected_version),
            LifeEntity.deleted_at.is_(None),
            LifeEntity.entity_type.in_(tuple(FOCUSABLE_ENTITY_TYPES)),
            LifeEntity.status.in_(tuple(FOCUSABLE_ENTITY_STATUSES)),
        )
        .update(
            {LifeEntity.updated_at: LifeEntity.updated_at},
            synchronize_session=False,
        )
    )
    if claimed != 1:
        db.expire_all()
        current = (
            db.query(LifeEntity)
            .filter(
                LifeEntity.id == entity.id,
                LifeEntity.owner_id == entity.owner_id,
            )
            .first()
        )
        if current is None:
            raise FocusNotFound("Life entity not found")
        raise FocusConflict(
            "Life entity changed or is no longer actionable "
            f"(current version {int(current.version or 1)})"
        )
    return entity


def _claim_canonical_planning_item(
    db: Any,
    *,
    account: Account,
    entity: LifeEntity,
) -> None:
    """Lock and revalidate a PlanningItem-backed task before Focus starts.

    LifeEntity is a graph projection, not the authority for Planning work.  A
    task may have been completed, removed, or reassigned without its projection
    being refreshed yet.  The conditional no-op update both proves ownership
    and actionability and takes the canonical row lock in the same transaction
    as the Focus lease insertion.
    """

    if entity.domain_ref_type != "planning_item":
        return
    planning_item_id = str(entity.domain_ref_id or "").strip()
    if not planning_item_id:
        raise FocusConflict(
            "Canonical planning task is missing or no longer actionable"
        )

    claimed = (
        db.query(PlanningItem)
        .filter(
            PlanningItem.id == planning_item_id,
            PlanningItem.owner == normalize_planning_owner(account.username),
            PlanningItem.status == "open",
            PlanningItem.completed_at.is_(None),
        )
        .update(
            {PlanningItem.updated_at: PlanningItem.updated_at},
            synchronize_session=False,
        )
    )
    if claimed != 1:
        raise FocusConflict(
            "Canonical planning task is missing or no longer actionable"
        )


def _owned_session(db: Any, owner_id: str, session_id: str) -> FocusSession:
    session = (
        db.query(FocusSession)
        .filter(
            FocusSession.id == str(session_id),
            FocusSession.owner_id == owner_id,
        )
        .first()
    )
    if session is None:
        raise FocusNotFound("Focus session not found")
    return session


def get_current_focus_session(
    db: Any, *, owner_id: str
) -> FocusSession | None:
    rows = (
        db.query(FocusSession)
        .filter(
            FocusSession.owner_id == owner_id,
            FocusSession.state.in_(tuple(LIVE_FOCUS_STATES)),
        )
        .order_by(FocusSession.updated_at.desc(), FocusSession.id.desc())
        .limit(2)
        .all()
    )
    if len(rows) > 1:
        # A deployment missing the partial unique index must not pick a lease
        # arbitrarily and let two clients believe they own focus.
        raise FocusConflict("Multiple live focus sessions require repair")
    return rows[0] if rows else None


def list_focus_history(
    db: Any, *, owner_id: str, limit: int = 50
) -> tuple[list[FocusSession], bool]:
    bounded = max(1, min(100, int(limit)))
    rows = (
        db.query(FocusSession)
        .filter(
            FocusSession.owner_id == owner_id,
            FocusSession.state.in_(tuple(TERMINAL_FOCUS_STATES)),
        )
        .order_by(
            FocusSession.completed_at.desc(),
            FocusSession.updated_at.desc(),
            FocusSession.id.desc(),
        )
        .limit(bounded + 1)
        .all()
    )
    return rows[:bounded], len(rows) > bounded


def get_owned_focus_entities(
    db: Any, *, owner_id: str, entity_ids: Iterable[str]
) -> dict[str, LifeEntity]:
    ids = {str(value) for value in entity_ids if str(value or "").strip()}
    if not ids:
        return {}
    rows = (
        db.query(LifeEntity)
        .filter(LifeEntity.owner_id == owner_id, LifeEntity.id.in_(ids))
        .all()
    )
    return {row.id: row for row in rows}


def get_focus_context(
    db: Any,
    *,
    owner_id: str,
    entity_id: str,
    limit: int = MAX_FOCUS_CONTEXT,
) -> list[dict[str, Any]]:
    """Return bounded one-hop work context without crossing principals.

    Focus deliberately shows only context-bearing Life nodes. The canonical
    graph remains authoritative; no browser-side inference or duplicated
    relationship store is introduced here.
    """

    entity = _owned_entity(db, owner_id, entity_id)
    bounded = max(1, min(MAX_FOCUS_CONTEXT, int(limit)))
    links = (
        db.query(EntityLink)
        .filter(
            EntityLink.owner_id == owner_id,
            EntityLink.deleted_at.is_(None),
            EntityLink.source_type == "life_entity",
            EntityLink.target_type == "life_entity",
            or_(
                EntityLink.source_id == entity.id,
                EntityLink.target_id == entity.id,
            ),
        )
        .order_by(EntityLink.created_at.asc(), EntityLink.id.asc())
        .limit((bounded * 3) + 1)
        .all()
    )
    related_ids = [
        link.target_id if link.source_id == entity.id else link.source_id
        for link in links
    ]
    related = get_owned_focus_entities(
        db, owner_id=owner_id, entity_ids=related_ids
    )
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in links:
        related_id = (
            link.target_id if link.source_id == entity.id else link.source_id
        )
        item = related.get(related_id)
        if (
            item is None
            or item.deleted_at is not None
            or item.entity_type not in FOCUS_CONTEXT_ENTITY_TYPES
            or item.id in seen
        ):
            continue
        seen.add(item.id)
        rows.append({
            "id": item.id,
            "entity_type": item.entity_type,
            "title": item.title or "",
            "summary": item.summary or "",
            "status": item.status,
            "relation": link.relation,
            "direction": "outgoing" if link.source_id == entity.id else "incoming",
            "domain_ref_type": item.domain_ref_type,
            "domain_ref_id": item.domain_ref_id,
            "version": int(item.version or 1),
        })
        if len(rows) == bounded:
            break
    return rows


def resolve_planning_focus_entity(
    db: Any,
    *,
    account: Account,
    planning_item_id: str,
    expected_planning_version: int,
) -> LifeEntity:
    """Resolve a canonical Today Planning row into an actionable Life node.

    Today remains read-only. The projection is created only when the principal
    explicitly starts Focus, in the same transaction as the focus lease.
    """

    planning_owner = normalize_planning_owner(account.username)
    item = (
        db.query(PlanningItem)
        .filter(
            PlanningItem.id == str(planning_item_id),
            PlanningItem.owner == planning_owner,
        )
        .first()
    )
    if item is None:
        raise FocusNotFound("Planning item not found")
    current_version = int(item.version or 1)
    if current_version != int(expected_planning_version):
        raise FocusConflict(
            "Planning item changed in another client "
            f"(current version {current_version})"
        )
    claimed = (
        db.query(PlanningItem)
        .filter(
            PlanningItem.id == item.id,
            PlanningItem.owner == planning_owner,
            PlanningItem.version == current_version,
            PlanningItem.status == "open",
            PlanningItem.completed_at.is_(None),
        )
        .update(
            {PlanningItem.updated_at: PlanningItem.updated_at},
            synchronize_session=False,
        )
    )
    if claimed != 1:
        raise FocusConflict("Planning item is no longer actionable")

    existing = (
        db.query(LifeEntity)
        .filter(
            LifeEntity.owner_id == account.id,
            LifeEntity.domain_ref_type == "planning_item",
            LifeEntity.domain_ref_id == item.id,
        )
        .first()
    )
    if existing is not None:
        if (
            existing.deleted_at is not None
            or existing.entity_type != "task"
            or existing.status not in FOCUSABLE_ENTITY_STATUSES
        ):
            raise FocusConflict(
                "Planning item's Life representation is no longer actionable"
            )
        return existing

    task, _created = create_life_entity(
        db,
        account=account,
        entity_type="task",
        title=item.title,
        summary=item.details or "",
        status="open",
        properties={"source": "planning"},
        provenance={"source": "focus_today"},
        confidence=100,
        sensitivity="private",
        domain_ref_type="planning_item",
        domain_ref_id=item.id,
        idempotency_key=f"focus-planning:{item.id}",
        reason="Planning item represented in Life when Focus started",
    )
    return task


def _check_version(session: FocusSession, expected_version: int) -> None:
    if int(session.version or 1) != int(expected_version):
        raise FocusConflict(
            "Focus session changed in another client "
            f"(current version {int(session.version or 1)})"
        )


def _reserve_version(
    db: Any,
    session: FocusSession,
    expected_version: int,
    *,
    now: datetime,
) -> None:
    _check_version(session, expected_version)
    next_version = int(expected_version) + 1
    updated = (
        db.query(FocusSession)
        .filter(
            FocusSession.id == session.id,
            FocusSession.owner_id == session.owner_id,
            FocusSession.version == int(expected_version),
        )
        .update(
            {FocusSession.version: next_version, FocusSession.updated_at: now},
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.expire_all()
        current = (
            db.query(FocusSession.version)
            .filter(
                FocusSession.id == session.id,
                FocusSession.owner_id == session.owner_id,
            )
            .scalar()
        )
        if current is None:
            raise FocusNotFound("Focus session not found")
        raise FocusConflict(
            "Focus session changed in another client "
            f"(current version {int(current)})"
        )
    session.version = next_version
    session.updated_at = now


def start_focus_session(
    db: Any,
    *,
    account: Account,
    entity_id: str,
    expected_entity_version: int,
    definition_of_done: object,
    now: datetime | None = None,
) -> FocusSession:
    current_time = _as_naive_utc(now)
    definition = _clean_required_text(
        definition_of_done,
        field="definition_of_done",
        limit=MAX_DEFINITION_OF_DONE_LENGTH,
    )
    entity = _focusable_entity(
        db,
        owner_id=account.id,
        entity_id=entity_id,
        expected_version=expected_entity_version,
    )
    if get_current_focus_session(db, owner_id=account.id) is not None:
        raise FocusConflict("A live focus session already exists")
    _claim_canonical_planning_item(db, account=account, entity=entity)
    entity = _claim_focusable_entity(
        db,
        entity=entity,
        expected_version=expected_entity_version,
    )

    session = FocusSession(
        id=str(uuid.uuid4()),
        owner_id=account.id,
        entity_id=entity.id,
        state="active",
        definition_of_done=definition,
        started_at=current_time,
        active_since=current_time,
        elapsed_seconds=0,
        interruptions={"entries": []},
        progress={"entries": []},
        evidence={"entries": []},
        follow_up_entity_ids={"ids": []},
        version=1,
    )
    try:
        # The partial unique index, not the preflight query, resolves two
        # concurrent starts for the same principal.
        with db.begin_nested():
            db.add(session)
            db.flush()
    except IntegrityError as exc:
        raise FocusConflict("A live focus session already exists") from exc

    append_action_audit(
        db,
        owner_id=account.id,
        action="focus.started",
        entity_type="focus_session",
        entity_id=session.id,
        reason="Principal started a focused work lease",
        after_state=_state(session),
        details={
            "target_entity_id": entity.id,
            "target_entity_type": entity.entity_type,
        },
    )
    return session


def pause_focus_session(
    db: Any,
    *,
    owner_id: str,
    session_id: str,
    expected_version: int,
    now: datetime | None = None,
) -> FocusSession:
    session = _owned_session(db, owner_id, session_id)
    _check_version(session, expected_version)
    if session.state != "active":
        raise FocusConflict("Only an active focus session can be paused")
    current_time = _as_naive_utc(now)
    before = _state(session)
    elapsed = effective_elapsed_seconds(session, now=current_time)
    _reserve_version(db, session, expected_version, now=current_time)
    session.elapsed_seconds = elapsed
    session.state = "paused"
    session.active_since = None
    session.paused_at = current_time
    db.flush()
    append_action_audit(
        db,
        owner_id=owner_id,
        action="focus.paused",
        entity_type="focus_session",
        entity_id=session.id,
        reason="Principal paused focused work",
        before_state=before,
        after_state=_state(session),
    )
    return session


def resume_focus_session(
    db: Any,
    *,
    owner_id: str,
    session_id: str,
    expected_version: int,
    now: datetime | None = None,
) -> FocusSession:
    session = _owned_session(db, owner_id, session_id)
    _check_version(session, expected_version)
    if session.state != "paused":
        raise FocusConflict("Only a paused focus session can be resumed")
    current_time = _as_naive_utc(now)
    before = _state(session)
    _reserve_version(db, session, expected_version, now=current_time)
    session.state = "active"
    # ``started_at`` is the immutable overall beginning; ``active_since`` marks
    # only the current interval used by the recoverable timer.
    session.active_since = current_time
    session.paused_at = None
    db.flush()
    append_action_audit(
        db,
        owner_id=owner_id,
        action="focus.resumed",
        entity_type="focus_session",
        entity_id=session.id,
        reason="Principal resumed focused work",
        before_state=before,
        after_state=_state(session),
    )
    return session


def _append_entry(
    db: Any,
    *,
    owner_id: str,
    session_id: str,
    expected_version: int,
    field: str,
    text: object,
    metadata: object = None,
    now: datetime | None = None,
) -> tuple[FocusSession, dict[str, Any]]:
    if field not in {"interruptions", "progress", "evidence"}:
        raise FocusError("Unknown focus journal")
    session = _owned_session(db, owner_id, session_id)
    _check_version(session, expected_version)
    if session.state not in LIVE_FOCUS_STATES:
        raise FocusConflict("Focus entries require an active or paused session")
    current_time = _as_naive_utc(now)
    entry = {
        "id": str(uuid.uuid4()),
        "at": _iso_utc(current_time),
        "text": _clean_required_text(
            text, field="text", limit=MAX_ENTRY_TEXT_LENGTH
        ),
        "metadata": _clean_metadata(metadata),
    }
    before = _state(session)
    existing = _entries(getattr(session, field))
    bounded = (existing + [entry])[-MAX_FOCUS_ENTRIES:]
    _reserve_version(db, session, expected_version, now=current_time)
    setattr(session, field, {"entries": bounded})
    db.flush()
    action = {
        "interruptions": "focus.interruption_added",
        "progress": "focus.progress_added",
        "evidence": "focus.evidence_added",
    }[field]
    append_action_audit(
        db,
        owner_id=owner_id,
        action=action,
        entity_type="focus_session",
        entity_id=session.id,
        reason=f"Principal appended a focus {field[:-1] if field.endswith('s') else field} entry",
        before_state=before,
        after_state=_state(session),
        details={"entry_id": entry["id"], "journal": field},
    )
    return session, entry


def add_focus_interruption(db: Any, **kwargs: Any):
    return _append_entry(db, field="interruptions", **kwargs)


def add_focus_progress(db: Any, **kwargs: Any):
    return _append_entry(db, field="progress", **kwargs)


def add_focus_evidence(db: Any, **kwargs: Any):
    return _append_entry(db, field="evidence", **kwargs)


def _normalize_follow_ups(
    follow_ups: Iterable[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    rows = list(follow_ups or [])
    if len(rows) > MAX_FOLLOW_UPS:
        raise FocusError(f"follow_ups must not exceed {MAX_FOLLOW_UPS} entries")
    normalized: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise FocusError("Each follow-up must be an object")
        due_at = raw.get("due_at")
        if due_at is not None and not isinstance(due_at, datetime):
            raise FocusError("follow-up due_at must be a datetime")
        normalized.append({
            "title": _clean_required_text(
                raw.get("title"), field="follow-up title", limit=240
            ),
            "summary": _clean_optional_text(
                raw.get("summary"), field="follow-up summary", limit=20_000
            ),
            "definition_of_done": _clean_optional_text(
                raw.get("definition_of_done"),
                field="follow-up definition_of_done",
                limit=MAX_DEFINITION_OF_DONE_LENGTH,
            ),
            "due_at": _as_naive_utc(due_at) if due_at is not None else None,
        })
    return normalized


def _create_follow_up_tasks(
    db: Any,
    *,
    owner_id: str,
    session: FocusSession,
    definitions: list[dict[str, Any]],
) -> list[LifeEntity]:
    account = db.query(Account).filter(Account.id == owner_id).first()
    if account is None:
        raise FocusNotFound("Account not found")
    tasks: list[LifeEntity] = []
    for index, definition in enumerate(definitions):
        summary = definition["summary"]
        definition_of_done = definition["definition_of_done"]
        planning_details = summary
        if definition_of_done:
            planning_details = (
                f"{summary}\n\n" if summary else ""
            ) + f"Definition of done:\n{definition_of_done}"
        due_at = definition["due_at"]
        planning_item = create_planning_item(
            db,
            owner=account.username,
            title=definition["title"],
            details=planning_details,
            due_date=due_at.date().isoformat() if due_at is not None else None,
            source="focus",
        )
        task, _created = create_life_entity(
            db,
            account=account,
            entity_type="task",
            title=definition["title"],
            summary=summary,
            status="open",
            properties={
                "definition_of_done": definition_of_done,
                "source_focus_session_id": session.id,
            },
            provenance={
                "source": "focus_follow_up",
                "focus_session_id": session.id,
            },
            confidence=100,
            sensitivity="private",
            domain_ref_type="planning_item",
            domain_ref_id=planning_item.id,
            due_at=due_at,
            idempotency_key=f"focus:{session.id}:follow-up:{index}",
            reason="Focus session created a Planning follow-up representation",
        )
        tasks.append(task)
    return tasks


def _finish_focus_session(
    db: Any,
    *,
    owner_id: str,
    session_id: str,
    expected_version: int,
    state: str,
    follow_ups: Iterable[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> tuple[FocusSession, list[LifeEntity]]:
    if state not in TERMINAL_FOCUS_STATES:
        raise FocusError("Unknown terminal focus state")
    session = _owned_session(db, owner_id, session_id)
    if session.state == state:
        # Safe retry after a committed response was lost. Follow-up IDs already
        # persisted on the session are authoritative and no new rows are made.
        ids = _follow_up_ids(session.follow_up_entity_ids)
        owned = get_owned_focus_entities(
            db,
            owner_id=owner_id,
            entity_ids=ids,
        )
        tasks = [owned[entity_id] for entity_id in ids if entity_id in owned]
        return session, tasks
    if session.state in TERMINAL_FOCUS_STATES:
        raise FocusConflict(
            f"A {session.state} focus session cannot be changed to {state}"
        )
    _check_version(session, expected_version)
    definitions = _normalize_follow_ups(follow_ups)
    current_time = _as_naive_utc(now)
    before = _state(session)
    elapsed = effective_elapsed_seconds(session, now=current_time)
    _reserve_version(db, session, expected_version, now=current_time)
    tasks = _create_follow_up_tasks(
        db,
        owner_id=owner_id,
        session=session,
        definitions=definitions,
    )
    session.state = state
    session.elapsed_seconds = elapsed
    session.active_since = None
    session.paused_at = None
    session.completed_at = current_time
    session.follow_up_entity_ids = {"ids": [task.id for task in tasks]}
    db.flush()
    append_action_audit(
        db,
        owner_id=owner_id,
        action=f"focus.{state}",
        entity_type="focus_session",
        entity_id=session.id,
        reason=f"Principal {state} focused work",
        before_state=before,
        after_state=_state(session),
        details={
            "follow_up_entity_ids": [task.id for task in tasks],
            "follow_up_planning_item_ids": [
                task.domain_ref_id for task in tasks
            ],
        },
    )
    return session, tasks


def complete_focus_session(db: Any, **kwargs: Any):
    return _finish_focus_session(db, state="completed", **kwargs)


def abandon_focus_session(db: Any, **kwargs: Any):
    return _finish_focus_session(db, state="abandoned", **kwargs)
