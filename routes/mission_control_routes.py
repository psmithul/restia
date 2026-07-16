"""Read-only Mission Control aggregation over Restia's existing stores.

The endpoint in this module deliberately owns no persistence.  It assembles a
small, owner-scoped Today view from Calendar, Projects, scheduled Tasks, Study
Mode, Notes, email-urgency state, and the last persisted Daily Brief, then adds
a redacted service-health summary.  Every source is bounded and isolated so
one unavailable subsystem is visible without hiding the rest of the snapshot.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time as monotonic_time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, Query, Request
from sqlalchemy import and_, case, func, or_, select

from core.database import (
    AuthIdentity,
    CalendarCal,
    CalendarEvent,
    InboxItem,
    Note,
    PlanningItem,
    Project,
    ProjectActivity,
    ProjectMember,
    ProjectStage,
    ProjectWorkItem,
    ScheduledTask,
    Session as ChatSession,
    SessionLocal,
    StudyState,
    TaskRun,
    ProgressionEvent,
)
from routes.calendar_routes import (
    FALLBACK_OWNER as CALENDAR_FALLBACK_OWNER,
    _expand_rrule,
)
from routes.project_routes import EXPLICIT_PROJECT_FALLBACK_OWNER, FALLBACK_PROJECT_OWNER
from src.auth_helpers import (
    DEFAULT_LOCAL_OWNER,
    effective_owner,
    legacy_owner_storage_key,
    owner_storage_key,
    require_user,
)
from src.constants import DATA_DIR
from src.study_mode import build_study_tracker, serialize_study_state
from src.planning import normalize_planning_owner, serialize_planning_item
from src.progression import build_progression_summary, normalize_progression_owner
from src.identity import LOCAL_IDENTITY_PROVIDER, normalize_identity


logger = logging.getLogger(__name__)

CALENDAR_ITEM_LIMIT = 20
PROJECT_WORK_ITEM_LIMIT = 20
GOAL_ITEM_LIMIT = 10
TASK_ITEM_LIMIT = 20
STUDY_REVIEW_ITEM_LIMIT = 10
IMPORTANT_MAIL_ITEM_LIMIT = 10
NOTES_TODAY_ITEM_LIMIT = 10
PLANNING_ITEM_LIMIT = 20
DAILY_BRIEF_ITEM_LIMIT = 1
NEXT_ACTION_LIMIT = 3
ACTIVITY_ITEM_LIMIT = 50
INBOX_PREVIEW_LIMIT = 5

_CALENDAR_RECURRING_SCAN_LIMIT = 500
_CALENDAR_OCCURRENCE_WORK_LIMIT = 210
_IMPORTANT_MAIL_STATE_BYTES = 2 * 1024 * 1024
_IMPORTANT_MAIL_SCAN_LIMIT = 500
_NOTES_TODAY_SCAN_LIMIT = 100
_NOTES_TODAY_ITEMS_BYTES = 32 * 1024
_NOTES_TODAY_STEP_LIMIT = 100
_DAILY_BRIEF_CONTENT_LIMIT = 4000
_HEALTH_SERVICE_LIMIT = 20
_HEALTH_TIMEOUT_SECONDS = 3.0
_HEALTH_CACHE_TTL_SECONDS = 30.0
_ACTIVITY_SOURCE_SCAN_LIMIT = 100
_HIGH_PRIORITIES = ("high", "highest", "critical")
_HEALTH_STATUSES = {"ok", "degraded", "down", "disabled"}


@dataclass(frozen=True)
class _OwnerScope:
    """Concrete private-owner scope, or the safe first-run sentinel scope."""

    owner: Optional[str]
    project_actor: str
    calendar_owner: str
    available: bool = True
    include_unowned: bool = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_utc(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    return _as_utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _items_source(items: list[dict[str, Any]], *, truncated: bool = False) -> dict[str, Any]:
    return {
        "status": "ok",
        "items": items,
        "count": len(items),
        "truncated": bool(truncated),
    }


def _source_problem(
    source: str,
    *,
    status: str = "error",
    code: Optional[str] = None,
    message: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "items": [],
        "count": 0,
        "truncated": False,
        "error": {
            "code": code or f"{source}_unavailable",
            "message": message or f"Could not load {source.replace('_', ' ')}.",
        },
    }


def _resolve_owner_scope(request: Request) -> _OwnerScope:
    """Authenticate once and resolve only identities safe for private reads.

    ``require_user`` is the route security gate.  Its empty result is admitted
    only in Restia's explicit auth-disabled/first-run modes.  In those modes a
    single configured local owner may still be resolved by ``effective_owner``;
    otherwise we use the same first-run sentinels as Calendar and Projects.
    A configured multi-profile installation with no resolvable owner fails
    closed instead of broadening a query to every row.
    """

    authenticated = str(require_user(request) or "").strip()
    if authenticated:
        return _OwnerScope(
            owner=authenticated,
            project_actor=authenticated.lower(),
            calendar_owner=authenticated,
        )

    auth_manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
    users = getattr(auth_manager, "users", None)
    if isinstance(users, dict):
        configured_users = {
            str(name).strip().lower(): data
            for name, data in users.items()
            if str(name).strip()
        }
        resolved = ""
        if EXPLICIT_PROJECT_FALLBACK_OWNER:
            if EXPLICIT_PROJECT_FALLBACK_OWNER in configured_users:
                resolved = EXPLICIT_PROJECT_FALLBACK_OWNER
        elif len(configured_users) == 1:
            resolved = next(iter(configured_users))
        else:
            admins = [
                name
                for name, data in configured_users.items()
                if isinstance(data, dict) and data.get("is_admin")
            ]
            if len(admins) == 1:
                resolved = admins[0]
        if resolved:
            return _OwnerScope(
                owner=resolved,
                project_actor=resolved,
                # Calendar's auth-disabled API always writes/reads through its
                # explicit fallback owner, while Tasks and Study may have both
                # the resolved local profile and legacy/unowned rows.
                calendar_owner=CALENDAR_FALLBACK_OWNER,
                include_unowned=True,
            )
        if configured_users:
            return _OwnerScope(
                owner=None,
                project_actor="",
                calendar_owner="",
                available=False,
            )
    else:
        resolved = str(effective_owner(request) or "").strip()
        if resolved:
            return _OwnerScope(
                owner=resolved,
                project_actor=resolved.lower(),
                calendar_owner=CALENDAR_FALLBACK_OWNER,
                include_unowned=True,
            )

    return _OwnerScope(
        owner=None,
        project_actor=FALLBACK_PROJECT_OWNER,
        calendar_owner=CALENDAR_FALLBACK_OWNER,
        include_unowned=True,
    )


def _calendar_local_sort(value: dict[str, Any], utc_offset_minutes: int) -> tuple[Any, ...]:
    raw = str(value.get("start") or "")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            local_zone = timezone(timedelta(minutes=utc_offset_minutes))
            parsed = parsed.astimezone(local_zone).replace(tzinfo=None)
        return (parsed, str(value.get("id") or ""))
    except ValueError:
        return (datetime.max, str(value.get("id") or ""))


def _load_calendar(
    session_factory: Callable[[], Any],
    scope: _OwnerScope,
    *,
    local_start: datetime,
    local_end: datetime,
    utc_start: datetime,
    utc_end: datetime,
    utc_offset_minutes: int,
) -> dict[str, Any]:
    db = session_factory()
    try:
        expanded: list[dict[str, Any]] = []
        truncated = False
        # Legacy/local rows are stored as naive local wall time. Imported rows
        # marked is_utc are stored as naive UTC instants. Query each against the
        # matching window so a non-zero client offset does not shift one class.
        for is_utc, start_at, end_at in (
            (False, local_start, local_end),
            (True, utc_start, utc_end),
        ):
            utc_filter = (
                CalendarEvent.is_utc.is_(True)
                if is_utc
                else or_(CalendarEvent.is_utc.is_(False), CalendarEvent.is_utc.is_(None))
            )
            common_filters = (
                CalendarCal.owner == scope.calendar_owner,
                CalendarEvent.status != "cancelled",
                utc_filter,
            )
            # Exact-overlap rows are already relevance-filtered in SQL. At
            # most 20 can survive the final combined response, so 21 is enough
            # to detect truncation without loading the whole day.
            direct_rows = (
                db.query(CalendarEvent)
                .join(CalendarCal)
                .filter(
                    *common_filters,
                    or_(CalendarEvent.rrule == "", CalendarEvent.rrule.is_(None)),
                    CalendarEvent.dtstart < end_at,
                    CalendarEvent.dtend > start_at,
                )
                .order_by(CalendarEvent.dtstart.asc(), CalendarEvent.uid.asc())
                .limit(CALENDAR_ITEM_LIMIT + 1)
                .all()
            )
            if len(direct_rows) > CALENDAR_ITEM_LIMIT:
                truncated = True
                direct_rows = direct_rows[:CALENDAR_ITEM_LIMIT]

            # RRULE relevance cannot be decided correctly from DTSTART alone:
            # an old series may be active today while a newer one already
            # expired. Scan a larger, still-hard-bounded candidate set and let
            # the existing recurrence engine decide which series intersects
            # the requested day. Newest-first avoids the previous oldest-first
            # starvation where expired historical rows consumed the whole cap.
            recurring_rows = (
                db.query(CalendarEvent)
                .join(CalendarCal)
                .filter(
                    *common_filters,
                    CalendarEvent.rrule.isnot(None),
                    CalendarEvent.rrule != "",
                    CalendarEvent.dtstart < end_at,
                )
                .order_by(CalendarEvent.dtstart.desc(), CalendarEvent.uid.asc())
                .limit(_CALENDAR_RECURRING_SCAN_LIMIT + 1)
                .all()
            )
            if len(recurring_rows) > _CALENDAR_RECURRING_SCAN_LIMIT:
                truncated = True
                recurring_rows = recurring_rows[:_CALENDAR_RECURRING_SCAN_LIMIT]

            for event in [*direct_rows, *recurring_rows]:
                # Mission Control needs at most 20 occurrences total.  Keep
                # each series expansion just above that cap so a minutely rule
                # cannot materialize the Calendar route's larger default.
                occurrences = _expand_rrule(
                    event,
                    start_at,
                    end_at,
                    limit=CALENDAR_ITEM_LIMIT + 1,
                    work_limit=_CALENDAR_OCCURRENCE_WORK_LIMIT,
                )
                truncated = (
                    truncated
                    or bool(getattr(occurrences, "truncated", False))
                    or any(bool(row.get("truncated")) for row in occurrences)
                )
                for row in occurrences:
                    expanded.append({
                        "id": row.get("uid"),
                        "title": row.get("summary") or "",
                        "start": row.get("dtstart"),
                        "end": row.get("dtend"),
                        "all_day": bool(row.get("all_day")),
                        "calendar": row.get("calendar") or "",
                        "importance": row.get("importance") or "normal",
                        "event_type": row.get("event_type"),
                        "location": row.get("location") or "",
                    })

        expanded.sort(key=lambda row: _calendar_local_sort(row, utc_offset_minutes))
        if len(expanded) > CALENDAR_ITEM_LIMIT:
            truncated = True
        return _items_source(expanded[:CALENDAR_ITEM_LIMIT], truncated=truncated)
    finally:
        db.close()


def _load_project_work(
    session_factory: Callable[[], Any],
    scope: _OwnerScope,
    *,
    today: date,
) -> dict[str, Any]:
    db = session_factory()
    try:
        actor = scope.project_actor
        membership_ids = db.query(ProjectMember.project_id).filter(
            func.lower(ProjectMember.username) == actor
        )
        today_text = today.isoformat()
        due_rank = case(
            (ProjectWorkItem.due_date < today_text, 0),
            (ProjectWorkItem.due_date == today_text, 1),
            else_=2,
        )
        priority_rank = case(
            (ProjectWorkItem.priority == "critical", 0),
            (ProjectWorkItem.priority == "highest", 1),
            (ProjectWorkItem.priority == "high", 2),
            else_=3,
        )
        rows = (
            db.query(ProjectWorkItem, Project, ProjectStage)
            .join(Project, Project.id == ProjectWorkItem.project_id)
            .outerjoin(ProjectStage, ProjectStage.id == ProjectWorkItem.stage_id)
            .filter(
                or_(func.lower(Project.owner) == actor, Project.id.in_(membership_ids)),
                Project.archived.is_(False),
                ProjectWorkItem.archived.is_(False),
                ProjectWorkItem.completed_at.is_(None),
                or_(ProjectStage.id.is_(None), ProjectStage.category != "done"),
                or_(
                    ProjectWorkItem.due_date <= today_text,
                    ProjectWorkItem.priority.in_(_HIGH_PRIORITIES),
                ),
            )
            .order_by(
                due_rank.asc(),
                priority_rank.asc(),
                ProjectWorkItem.due_date.asc(),
                ProjectWorkItem.updated_at.desc(),
                ProjectWorkItem.id.asc(),
            )
            .limit(PROJECT_WORK_ITEM_LIMIT + 1)
            .all()
        )
        truncated = len(rows) > PROJECT_WORK_ITEM_LIMIT
        items = [
            {
                "id": item.id,
                "project_id": project.id,
                "project_key": project.key,
                "project_name": project.name,
                "key": f"{project.key}-{item.item_number}",
                "title": item.title,
                "priority": item.priority or "medium",
                "due_date": item.due_date,
                "overdue": bool(item.due_date and item.due_date < today_text),
                "due_today": item.due_date == today_text,
                "assignee": item.assignee,
                "stage": stage.name if stage is not None else None,
                "stage_category": stage.category if stage is not None else None,
            }
            for item, project, stage in rows[:PROJECT_WORK_ITEM_LIMIT]
        ]
        return _items_source(items, truncated=truncated)
    finally:
        db.close()


def _planning_owner(scope: _OwnerScope) -> str:
    return normalize_planning_owner(
        scope.owner or scope.calendar_owner or scope.project_actor or DEFAULT_LOCAL_OWNER
    )


def _load_planning(
    session_factory: Callable[[], Any],
    scope: _OwnerScope,
    *,
    today: date,
) -> dict[str, Any]:
    """Return a small mix of open commitments and recently completed work."""

    db = session_factory()
    try:
        owner = _planning_owner(scope)
        status_rank = case((PlanningItem.status == "open", 0), else_=1)
        due_rank = case((PlanningItem.due_date.is_(None), 1), else_=0)
        rows = (
            db.query(PlanningItem)
            .filter(PlanningItem.owner == owner)
            .order_by(
                status_rank.asc(),
                due_rank.asc(),
                PlanningItem.due_date.asc(),
                PlanningItem.updated_at.desc(),
                PlanningItem.id.asc(),
            )
            .limit(PLANNING_ITEM_LIMIT + 1)
            .all()
        )
        today_text = today.isoformat()
        payload: list[dict[str, Any]] = []
        for item in rows[:PLANNING_ITEM_LIMIT]:
            serialized = serialize_planning_item(item)
            serialized["overdue"] = bool(
                item.status == "open" and item.due_date and item.due_date < today_text
            )
            serialized["due_today"] = bool(
                item.status == "open" and item.due_date == today_text
            )
            payload.append(serialized)
        source = _items_source(payload, truncated=len(rows) > PLANNING_ITEM_LIMIT)
        source["open_count"] = sum(1 for row in payload if row.get("status") == "open")
        return source
    finally:
        db.close()


def _empty_inbox_source() -> dict[str, Any]:
    return {
        "status": "ok",
        "items": [],
        "count": 0,
        "truncated": False,
        "unprocessed_count": 0,
        "kinds": {},
        "oldest_at": None,
        "latest_at": None,
    }


def _load_inbox(
    session_factory: Callable[[], Any],
    scope: _OwnerScope,
) -> dict[str, Any]:
    """Return a bounded, content-free view of the owner's Inbox workload.

    Mission Control is read-only, so an owner who has never opened Universal
    Inbox must not gain Account/AuthIdentity rows merely by loading Today.  We
    resolve only an existing local identity/account and otherwise return the
    same empty shape as an owner with no captures.
    """

    owner = normalize_identity(
        scope.owner
        or scope.project_actor
        or scope.calendar_owner
        or DEFAULT_LOCAL_OWNER
    )
    if not owner:
        return _empty_inbox_source()

    db = session_factory()
    try:
        account_id = (
            db.query(AuthIdentity.account_id)
            .filter(
                AuthIdentity.provider == LOCAL_IDENTITY_PROVIDER,
                AuthIdentity.subject == owner,
            )
            .scalar()
        )
        if not account_id:
            return _empty_inbox_source()

        owned_unprocessed = (
            InboxItem.owner_id == account_id,
            InboxItem.status == "inbox",
        )
        count, oldest_at, latest_at = (
            db.query(
                func.count(InboxItem.id),
                func.min(InboxItem.created_at),
                func.max(InboxItem.created_at),
            )
            .filter(*owned_unprocessed)
            .one()
        )
        unprocessed_count = int(count or 0)
        if not unprocessed_count:
            return _empty_inbox_source()

        kind_rows = (
            db.query(InboxItem.kind, func.count(InboxItem.id))
            .filter(*owned_unprocessed)
            .group_by(InboxItem.kind)
            .order_by(InboxItem.kind.asc())
            .all()
        )
        preview_rows = (
            db.query(InboxItem)
            .filter(*owned_unprocessed)
            .order_by(InboxItem.created_at.asc(), InboxItem.id.asc())
            .limit(INBOX_PREVIEW_LIMIT)
            .all()
        )
        previews = [
            {
                "id": item.id,
                "title": item.title or "",
                "kind": item.kind,
                "confidence": int(item.classification_confidence or 0),
                "reason": item.classification_reason or "",
            }
            for item in preview_rows
        ]
        return {
            "status": "ok",
            "items": previews,
            "count": unprocessed_count,
            "truncated": unprocessed_count > INBOX_PREVIEW_LIMIT,
            "unprocessed_count": unprocessed_count,
            "kinds": {
                str(kind): int(kind_count or 0)
                for kind, kind_count in kind_rows
            },
            "oldest_at": _iso_utc(oldest_at),
            "latest_at": _iso_utc(latest_at),
        }
    finally:
        db.close()


def _study_owner_filters(scope: _OwnerScope) -> tuple[Any, Any]:
    if scope.owner is None:
        return StudyState.owner.is_(None), ChatSession.owner.is_(None)
    if scope.include_unowned:
        return (
            or_(StudyState.owner == scope.owner, StudyState.owner.is_(None)),
            or_(ChatSession.owner == scope.owner, ChatSession.owner.is_(None)),
        )
    return StudyState.owner == scope.owner, ChatSession.owner == scope.owner


def _load_goals(
    session_factory: Callable[[], Any],
    scope: _OwnerScope,
    *,
    now_naive: datetime,
) -> dict[str, Any]:
    db = session_factory()
    try:
        state_owner, session_owner = _study_owner_filters(scope)
        target_rank = case((StudyState.target_date.is_(None), 1), else_=0)
        rows = (
            db.query(StudyState, ChatSession)
            .join(ChatSession, ChatSession.id == StudyState.id)
            .filter(
                state_owner,
                session_owner,
                ChatSession.mode == "study",
                ChatSession.archived.is_(False),
                StudyState.setup_initialized.is_(True),
                StudyState.goal_text != "",
            )
            .order_by(
                target_rank.asc(),
                StudyState.target_date.asc(),
                StudyState.updated_at.desc(),
                StudyState.id.asc(),
            )
            .limit(GOAL_ITEM_LIMIT + 1)
            .all()
        )
        truncated = len(rows) > GOAL_ITEM_LIMIT
        items: list[dict[str, Any]] = []
        for state, workspace in rows[:GOAL_ITEM_LIMIT]:
            serialized = serialize_study_state(state, now=now_naive)
            tracker = build_study_tracker(serialized, workspace.name)
            items.append({
                "source": "study",
                "session_id": state.id,
                "workspace_name": workspace.name or "Study workspace",
                "goal": state.goal_text or "",
                "target_date": state.target_date,
                "progress_percent": serialized["progress_percent"],
                "mastery_status": tracker["mastery"]["status"],
                "next_step": tracker["mastery"]["next_evidence"],
            })
        return _items_source(items, truncated=truncated)
    finally:
        db.close()


def _owned_task_query(query: Any, scope: _OwnerScope) -> Any:
    if scope.owner is None:
        return query.filter(ScheduledTask.owner.is_(None))
    if scope.include_unowned:
        return query.filter(
            or_(ScheduledTask.owner == scope.owner, ScheduledTask.owner.is_(None))
        )
    return query.filter(ScheduledTask.owner == scope.owner)


def _timestamp_number(value: Optional[datetime]) -> float:
    if value is None:
        return 0.0
    return _as_utc(value).timestamp()


def _load_tasks(
    session_factory: Callable[[], Any],
    scope: _OwnerScope,
    *,
    utc_start: datetime,
    utc_end: datetime,
) -> dict[str, Any]:
    db = session_factory()
    try:
        start_naive = utc_start.replace(tzinfo=None)
        end_naive = utc_end.replace(tzinfo=None)
        latest_started_query = _owned_task_query(
            db.query(
                TaskRun.task_id.label("task_id"),
                func.max(TaskRun.started_at).label("started_at"),
            ).join(ScheduledTask, ScheduledTask.id == TaskRun.task_id),
            scope,
        )
        latest_started = latest_started_query.group_by(TaskRun.task_id).subquery()
        # A timestamp tie is uncommon but possible in a batch. Pick one stable
        # run id so the response never duplicates a task or changes ordering.
        latest_ids = (
            db.query(func.max(TaskRun.id).label("id"))
            .join(
                latest_started,
                and_(
                    latest_started.c.task_id == TaskRun.task_id,
                    latest_started.c.started_at == TaskRun.started_at,
                ),
            )
            .group_by(TaskRun.task_id)
            .subquery()
        )
        run_query = (
            db.query(TaskRun, ScheduledTask)
            .join(ScheduledTask, ScheduledTask.id == TaskRun.task_id)
            .filter(
                TaskRun.id.in_(select(latest_ids.c.id)),
                TaskRun.status.in_(("running", "error")),
            )
        )
        attention_rank = case((TaskRun.status == "error", 0), else_=1)
        run_rows = (
            _owned_task_query(run_query, scope)
            .order_by(
                attention_rank.asc(), TaskRun.started_at.desc(), TaskRun.id.asc()
            )
            .limit(TASK_ITEM_LIMIT + 1)
            .all()
        )
        raw_run_overflow = len(run_rows) > TASK_ITEM_LIMIT
        run_rows = run_rows[:TASK_ITEM_LIMIT]

        scheduled_query = db.query(ScheduledTask).filter(
            ScheduledTask.status == "active",
            ScheduledTask.trigger_type == "schedule",
            ScheduledTask.next_run.isnot(None),
            ScheduledTask.next_run >= start_naive,
            ScheduledTask.next_run < end_naive,
        )
        scheduled_rows = (
            _owned_task_query(scheduled_query, scope)
            .order_by(ScheduledTask.next_run.asc(), ScheduledTask.id.asc())
            .limit(TASK_ITEM_LIMIT + 1)
            .all()
        )

        candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        run_task_ids: set[str] = set()
        for run, task in run_rows:
            # One attention row per task keeps repeated failures from drowning
            # out every other scheduled task in the concise snapshot.
            if task.id in run_task_ids:
                continue
            run_task_ids.add(task.id)
            failed = run.status == "error"
            candidates.append((
                (0 if failed else 1, -_timestamp_number(run.started_at), task.id, run.id),
                {
                    "kind": "failed_run" if failed else "running_run",
                    "run_id": run.id,
                    "task_id": task.id,
                    "task_name": task.name or "Untitled Task",
                    "status": run.status,
                    "scheduled_for": _iso_utc(task.next_run),
                    "started_at": _iso_utc(run.started_at),
                    "finished_at": _iso_utc(run.finished_at),
                },
            ))

        for task in scheduled_rows:
            if task.id in run_task_ids:
                continue
            candidates.append((
                (2, _timestamp_number(task.next_run), task.id, ""),
                {
                    "kind": "scheduled",
                    "run_id": None,
                    "task_id": task.id,
                    "task_name": task.name or "Untitled Task",
                    "status": "scheduled",
                    "scheduled_for": _iso_utc(task.next_run),
                    "started_at": None,
                    "finished_at": None,
                },
            ))

        candidates.sort(key=lambda row: row[0])
        items = [row for _, row in candidates[:TASK_ITEM_LIMIT]]
        truncated = (
            raw_run_overflow
            or len(scheduled_rows) > TASK_ITEM_LIMIT
            or len(candidates) > TASK_ITEM_LIMIT
        )
        return _items_source(items, truncated=truncated)
    finally:
        db.close()


def _load_study_reviews(
    session_factory: Callable[[], Any],
    scope: _OwnerScope,
    *,
    now_naive: datetime,
) -> dict[str, Any]:
    db = session_factory()
    try:
        state_owner, session_owner = _study_owner_filters(scope)
        rows = (
            db.query(StudyState, ChatSession)
            .join(ChatSession, ChatSession.id == StudyState.id)
            .filter(
                state_owner,
                session_owner,
                ChatSession.mode == "study",
                ChatSession.archived.is_(False),
                StudyState.next_review_at.isnot(None),
                StudyState.next_review_at <= now_naive,
                StudyState.review_count > 0,
            )
            .order_by(StudyState.next_review_at.asc(), StudyState.id.asc())
            .limit(STUDY_REVIEW_ITEM_LIMIT + 1)
            .all()
        )
        truncated = len(rows) > STUDY_REVIEW_ITEM_LIMIT
        items: list[dict[str, Any]] = []
        for state, workspace in rows[:STUDY_REVIEW_ITEM_LIMIT]:
            serialized = serialize_study_state(state, now=now_naive)
            tracker = build_study_tracker(serialized, workspace.name)
            items.append({
                "session_id": state.id,
                "workspace_name": workspace.name or "Study workspace",
                "goal": state.goal_text or "",
                "due_at": _iso_utc(state.next_review_at),
                "review_level": max(0, int(state.review_level or 0)),
                "review_count": max(0, int(state.review_count or 0)),
                "mastery_status": tracker["mastery"]["status"],
                "next_step": tracker["mastery"]["next_evidence"],
            })
        return _items_source(items, truncated=truncated)
    finally:
        db.close()


def _important_mail_paths(
    data_dir: Path, scope: _OwnerScope
) -> list[tuple[Path, str]]:
    owners: list[Optional[str]] = [scope.owner]
    # Auth-disabled first-run data historically lives under ``default``.  A
    # resolved single local profile may also have rows under its username, so
    # read both within that already-safe local scope.  Authenticated requests
    # never inherit the unowned/default file.
    if scope.include_unowned and scope.owner is not None:
        owners.append(None)
    paths: list[tuple[Path, str]] = []
    for owner in owners:
        path = data_dir / f"email_urgency_state_{owner_storage_key(owner)}.json"
        # A lossy pre-V2 path is considered only when no collision-resistant
        # state exists. `_load_important_mail` then requires its embedded exact
        # owner before exposing any private header metadata.
        legacy_path = data_dir / f"email_urgency_state_{legacy_owner_storage_key(owner)}.json"
        if not path.exists() and legacy_path != path and legacy_path.exists():
            path = legacy_path
        candidate = (path, owner or "")
        if candidate not in paths:
            paths.append(candidate)
    return paths


def _load_important_mail(data_dir: Path, scope: _OwnerScope) -> dict[str, Any]:
    merged: dict[str, dict[str, Any]] = {}
    raw_count = 0
    for path, expected_owner in _important_mail_paths(data_dir, scope):
        if not path.exists():
            continue
        if path.stat().st_size > _IMPORTANT_MAIL_STATE_BYTES:
            raise ValueError("email urgency state exceeds the bounded read size")
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise TypeError("email urgency state is not an object")
        # The historical filename slug is not injective (for example ``a/b``
        # and ``a_b`` collide).  The scanner persists the exact owner inside
        # the file; verify it before exposing any private header metadata.
        state_owner = state.get("owner")
        if not isinstance(state_owner, str) or state_owner != expected_owner:
            raise ValueError("email urgency state owner does not match request")
        per_uid = state.get("per_uid") or {}
        if not isinstance(per_uid, dict):
            raise TypeError("email urgency entries are not an object")
        raw_count += len(per_uid)
        for key, value in per_uid.items():
            if isinstance(value, dict):
                # Prefer the resolved owner's file when a legacy/default file
                # contains the same account + UID key.
                merged.setdefault(str(key), value)

    ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for key, value in merged.items():
        try:
            score = int(value.get("score", 0))
        except (TypeError, ValueError):
            continue
        if score < 2 or value.get("unread") is not True:
            continue
        account_id, separator, uid = key.rpartition(":")
        if not separator:
            account_id, uid = "", key
        subject = str(value.get("subject") or "(no subject)")[:160]
        sender = str(value.get("from") or "")[:120]
        reason = str(value.get("reason") or "")[:160]
        item = {
            "id": key[:240],
            "account_id": account_id[:80],
            "uid": uid[:120],
            "subject": subject,
            "sender": sender,
            "score": min(score, 3),
            "reason": reason,
        }
        ranked.append(((-item["score"], subject.lower(), key), item))

    ranked.sort(key=lambda row: row[0])
    items = [item for _, item in ranked[:IMPORTANT_MAIL_ITEM_LIMIT]]
    truncated = (
        raw_count > _IMPORTANT_MAIL_SCAN_LIMIT
        or len(ranked) > IMPORTANT_MAIL_ITEM_LIMIT
    )
    return _items_source(items, truncated=truncated)


def _owned_note_query(query: Any, scope: _OwnerScope) -> Any:
    if scope.owner is None:
        return query.filter(Note.owner.is_(None))
    if scope.include_unowned:
        return query.filter(or_(Note.owner == scope.owner, Note.owner.is_(None)))
    return query.filter(Note.owner == scope.owner)


def _load_notes_today(
    session_factory: Callable[[], Any],
    scope: _OwnerScope,
) -> dict[str, Any]:
    """Return one unchecked step per active to-do or goal, never note bodies."""

    db = session_factory()
    try:
        note_filters = (
            Note.archived.is_(False),
            Note.note_type.in_(("todo", "checklist", "goal")),
            Note.items.isnot(None),
            Note.items != "",
        )
        # Do not load a complete Note row: ``content`` is unrelated to Today
        # and is intentionally unbounded in the Notes editor. Also omit an
        # oversized checklist before transferring/parsing it in Python.
        oversized_query = db.query(Note.id).filter(
            *note_filters,
            func.length(Note.items) > _NOTES_TODAY_ITEMS_BYTES,
        )
        oversized_present = (
            _owned_note_query(oversized_query, scope).limit(1).first() is not None
        )
        query = db.query(
            Note.id.label("id"),
            func.substr(Note.title, 1, 160).label("title"),
            Note.note_type.label("note_type"),
            Note.items.label("items"),
            func.substr(Note.due_date, 1, 80).label("due_date"),
            Note.pinned.label("pinned"),
            Note.updated_at.label("updated_at"),
        ).filter(
            *note_filters,
            func.length(Note.items) <= _NOTES_TODAY_ITEMS_BYTES,
        )
        rows = (
            _owned_note_query(query, scope)
            .order_by(
                Note.pinned.desc(),
                Note.sort_order.asc(),
                Note.updated_at.desc(),
                Note.id.asc(),
            )
            .limit(_NOTES_TODAY_SCAN_LIMIT + 1)
            .all()
        )
        scan_truncated = len(rows) > _NOTES_TODAY_SCAN_LIMIT
        items: list[dict[str, Any]] = []
        active_count = 0
        payload_truncated = oversized_present
        for note in rows[:_NOTES_TODAY_SCAN_LIMIT]:
            try:
                steps = json.loads(note.items or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                payload_truncated = True
                continue
            if not isinstance(steps, list) or not steps:
                continue
            if len(steps) > _NOTES_TODAY_STEP_LIMIT:
                payload_truncated = True
            next_index: Optional[int] = None
            next_text = ""
            completed = 0
            for index, step in enumerate(steps[:_NOTES_TODAY_STEP_LIMIT]):
                if not isinstance(step, dict):
                    continue
                if bool(step.get("done")):
                    completed += 1
                elif next_index is None:
                    next_index = index
                    next_text = str(step.get("text") or "").strip()[:300]
            if next_index is None:
                continue
            active_count += 1
            if len(items) >= NOTES_TODAY_ITEM_LIMIT:
                continue
            total = len(steps)
            items.append({
                "id": note.id,
                "title": (note.title or "Untitled goal")[:160],
                "kind": "goal" if note.note_type == "goal" else "todo",
                "next_step": next_text or "Continue this goal",
                "next_step_index": next_index,
                "completed_steps": completed,
                "total_steps": total,
                "progress_percent": round((completed / total) * 100) if total else 0,
                "due_date": str(note.due_date or "")[:80] or None,
                "pinned": bool(note.pinned),
                "updated_at": _iso_utc(note.updated_at),
            })
        return _items_source(
            items,
            truncated=(
                scan_truncated
                or payload_truncated
                or active_count > NOTES_TODAY_ITEM_LIMIT
            ),
        )
    finally:
        db.close()


def _load_daily_brief(
    session_factory: Callable[[], Any],
    scope: _OwnerScope,
) -> dict[str, Any]:
    """Reuse the newest persisted successful Daily Brief; never run it here."""

    db = session_factory()
    try:
        query = (
            db.query(TaskRun, ScheduledTask)
            .join(ScheduledTask, ScheduledTask.id == TaskRun.task_id)
            .filter(
                ScheduledTask.task_type == "action",
                ScheduledTask.action == "daily_brief",
                TaskRun.status == "success",
                TaskRun.result.isnot(None),
                TaskRun.result != "",
            )
        )
        row = (
            _owned_task_query(query, scope)
            .order_by(TaskRun.started_at.desc(), TaskRun.id.desc())
            .limit(DAILY_BRIEF_ITEM_LIMIT)
            .first()
        )
        if row is None:
            return _items_source([])
        run, task = row
        content = str(run.result or "")
        content_truncated = len(content) > _DAILY_BRIEF_CONTENT_LIMIT
        return _items_source([{
            "run_id": run.id,
            "task_id": task.id,
            "task_name": (task.name or "Daily Brief")[:160],
            "generated_at": _iso_utc(run.finished_at or run.started_at),
            "content": content[:_DAILY_BRIEF_CONTENT_LIMIT],
            "content_truncated": content_truncated,
        }], truncated=content_truncated)
    finally:
        db.close()


def _load_progression(
    session_factory: Callable[[], Any],
    scope: _OwnerScope,
    *,
    utc_offset_minutes: int,
    now_utc: datetime,
) -> dict[str, Any]:
    summary = build_progression_summary(
        owner=scope.owner or _planning_owner(scope),
        session_factory=session_factory,
        utc_offset_minutes=utc_offset_minutes,
        now=now_utc,
    )
    recent = summary.get("recent_events")
    items = recent if isinstance(recent, list) else []
    return {
        "status": "ok",
        "items": items,
        "count": len(items),
        "truncated": False,
        **summary,
    }


def _source_items(source: dict[str, Any]) -> list[dict[str, Any]]:
    rows = source.get("items") if source.get("status") == "ok" else None
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _build_next_actions(sources: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Derive a stable, explainable priority queue without an LLM call."""

    candidates: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def add(
        rank: int,
        source_id: Any,
        *,
        kind: str,
        title: Any,
        detail: Any,
        target: str,
        urgency: str,
        tie: Any = "",
    ) -> None:
        stable_id = str(source_id or "")[:240]
        if not stable_id:
            return
        candidates.append(((rank, str(tie or ""), stable_id), {
            "id": f"{kind}:{stable_id}",
            "kind": kind,
            "title": str(title or "Untitled")[:180],
            "detail": str(detail or "")[:240],
            "target": target,
            "urgency": urgency,
            "source_id": stable_id,
        }))

    for row in _source_items(sources.get("tasks", {})):
        if row.get("kind") == "failed_run":
            add(
                0,
                row.get("run_id") or row.get("task_id"),
                kind="failed_task",
                title=f"Fix {row.get('task_name') or 'failed task'}",
                detail="The latest run failed and still needs attention.",
                target="tasks",
                urgency="critical",
                tie=row.get("started_at"),
            )

    project_rows = _source_items(sources.get("project_work", {}))
    for row in project_rows:
        if row.get("overdue"):
            add(
                1,
                row.get("id"),
                kind="overdue_project_work",
                title=row.get("title"),
                detail=f"{row.get('key') or row.get('project_name') or 'Project'} was due {row.get('due_date') or 'earlier'}.",
                target="projects",
                urgency="critical",
                tie=row.get("due_date"),
            )

    for row in _source_items(sources.get("important_mail", {})):
        score = int(row.get("score") or 0)
        add(
            2 if score >= 3 else 3,
            row.get("id"),
            kind="important_mail",
            title=row.get("subject"),
            detail=row.get("reason") or row.get("sender") or "Unread email needs a reply.",
            target="email",
            urgency="critical" if score >= 3 else "attention",
            tie=row.get("subject"),
        )

    for row in _source_items(sources.get("study_reviews", {})):
        add(
            4,
            row.get("session_id"),
            kind="study_review",
            title=f"Review {row.get('goal') or row.get('workspace_name') or 'study goal'}",
            detail=row.get("next_step") or "Complete the due review.",
            target="study",
            urgency="attention",
            tie=row.get("due_at"),
        )

    for row in _source_items(sources.get("planning", {})):
        if row.get("status") != "open":
            continue
        if row.get("overdue") or row.get("due_today"):
            add(
                4 if row.get("overdue") else 5,
                row.get("id"),
                kind="planning_item",
                title=row.get("title"),
                detail="Overdue planning item" if row.get("overdue") else "Due today",
                target="home",
                urgency="critical" if row.get("overdue") else "attention",
                tie=row.get("due_date"),
            )

    for row in project_rows:
        if row.get("due_today") and not row.get("overdue"):
            add(
                5,
                row.get("id"),
                kind="project_work_due_today",
                title=row.get("title"),
                detail=f"{row.get('key') or row.get('project_name') or 'Project'} is due today.",
                target="projects",
                urgency="attention",
                tie=row.get("key"),
            )

    for row in _source_items(sources.get("notes_today", {})):
        add(
            6,
            row.get("id"),
            kind="goal_step",
            title=row.get("next_step"),
            detail=f"Next step for {row.get('title') or 'goal'}.",
            target="todos" if row.get("kind") == "todo" else "notes",
            urgency="normal",
            tie=row.get("due_date") or row.get("title"),
        )

    for row in project_rows:
        if not row.get("overdue") and not row.get("due_today"):
            add(
                7,
                row.get("id"),
                kind="high_priority_project_work",
                title=row.get("title"),
                detail=f"High-priority work in {row.get('project_name') or 'Projects'}.",
                target="projects",
                urgency="normal",
                tie=row.get("due_date") or row.get("key"),
            )

    for row in _source_items(sources.get("goals", {})):
        add(
            8,
            row.get("session_id"),
            kind="study_goal",
            title=row.get("next_step") or row.get("goal"),
            detail=f"Next step for {row.get('goal') or 'study goal'}.",
            target="study",
            urgency="normal",
            tie=row.get("target_date") or row.get("goal"),
        )

    for row in _source_items(sources.get("tasks", {})):
        if row.get("kind") == "scheduled":
            add(
                9,
                row.get("task_id"),
                kind="scheduled_task",
                title=row.get("task_name"),
                detail=f"Scheduled for {row.get('scheduled_for') or 'today'}.",
                target="tasks",
                urgency="normal",
                tie=row.get("scheduled_for"),
            )

    candidates.sort(key=lambda row: row[0])
    seen: set[str] = set()
    actions: list[dict[str, Any]] = []
    for _, item in candidates:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        actions.append(item)
        if len(actions) == NEXT_ACTION_LIMIT:
            break
    return actions


async def _load_health(
    collector: Callable[..., Any],
    rag_manager: Any,
    memory_vector: Any,
) -> dict[str, Any]:
    if inspect.iscoroutinefunction(collector):
        result = collector(rag_manager, memory_vector)
    else:
        # A custom/synchronous probe must not monopolize the event loop used by
        # Today and Activity. Production's collector is async, but this keeps
        # the injectable seam bounded as well.
        result = await asyncio.to_thread(collector, rag_manager, memory_vector)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, dict):
        raise TypeError("health collector returned a non-object response")
    overall = str(result.get("overall") or "").lower()
    if overall not in {"ok", "degraded", "down"}:
        raise ValueError("health collector returned an invalid overall status")

    services: list[dict[str, str]] = []
    raw_services = result.get("services")
    if not isinstance(raw_services, list):
        raise TypeError("health collector returned an invalid services list")
    for row in raw_services[:_HEALTH_SERVICE_LIMIT]:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        status = str(row.get("status") or "").lower()
        if name and status in _HEALTH_STATUSES:
            services.append({"name": name[:80], "status": status})
    services.sort(key=lambda row: (row["name"].lower(), row["name"]))
    return {
        "status": overall,
        "overall": overall,
        "services": services,
        "truncated": len(raw_services) > _HEALTH_SERVICE_LIMIT,
    }


class _HealthSnapshotCache:
    """Small stale-if-error cache so health probes never stall work views."""

    def __init__(self, ttl_seconds: float, timeout_seconds: float):
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.timeout_seconds = max(0.05, float(timeout_seconds))
        self.value: dict[str, Any] | None = None
        self.expires_at = 0.0
        self.lock: asyncio.Lock | None = None

    async def get(
        self,
        collector: Callable[..., Any],
        rag_manager: Any,
        memory_vector: Any,
    ) -> dict[str, Any]:
        now = monotonic_time.monotonic()
        if self.value is not None and now < self.expires_at:
            return dict(self.value)
        if self.lock is None:
            self.lock = asyncio.Lock()
        async with self.lock:
            now = monotonic_time.monotonic()
            if self.value is not None and now < self.expires_at:
                return dict(self.value)
            try:
                value = await asyncio.wait_for(
                    _load_health(collector, rag_manager, memory_vector),
                    timeout=self.timeout_seconds,
                )
            except Exception:
                if self.value is not None:
                    return {**self.value, "stale": True}
                raise
            self.value = dict(value)
            self.expires_at = monotonic_time.monotonic() + self.ttl_seconds
            return dict(value)


def _safe_load(source: str, loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return loader()
    except Exception as exc:
        # Database/driver exception text may contain credential-bearing DSNs.
        # Keep diagnostics useful without copying raw exception content to logs.
        logger.error(
            "Mission Control %s source failed (%s)", source, type(exc).__name__
        )
        return _source_problem(source)


def _activity_target(source_type: str, details: Any = None) -> str:
    if (
        source_type == "todo_item_completed"
        and isinstance(details, dict)
        and details.get("planning_item_id")
    ):
        return "home"
    if source_type.startswith("project_"):
        return "projects"
    if source_type == "todo_item_completed":
        return "todos"
    if source_type == "calendar_event_completed":
        return "calendar"
    if source_type == "study_review_passed":
        return "study"
    return "home"


def _load_activity_feed(
    session_factory: Callable[[], Any],
    scope: _OwnerScope,
    *,
    limit: int,
    before: datetime | None,
    before_id: str | None = None,
) -> dict[str, Any]:
    """Merge three bounded owner-scoped audit sources into one stable feed."""

    bounded_limit = max(1, min(ACTIVITY_ITEM_LIMIT, int(limit)))
    scan_limit = min(_ACTIVITY_SOURCE_SCAN_LIMIT, bounded_limit * 2)
    candidates: list[tuple[datetime, str, dict[str, Any]]] = []
    source_overflow = False

    cursor_id = str(before_id or "").strip()
    cursor_prefix, cursor_source_id = (
        cursor_id.split(":", 1) if ":" in cursor_id else ("", "")
    )

    def apply_cursor(query: Any, occurred_column: Any, id_column: Any, prefix: str):
        """Apply the global ``(occurred_at, stable_id)`` activity cursor.

        Timestamp-only cursors retain the original strict-before contract.
        Composite cursors include tied timestamps and compare the source prefix
        plus row id, so a full page ending in a tie cannot skip the remaining
        events on the next page.
        """

        if before is None:
            return query
        if not cursor_prefix or not cursor_source_id:
            return query.filter(occurred_column < before)
        if prefix < cursor_prefix:
            return query.filter(occurred_column <= before)
        if prefix > cursor_prefix:
            return query.filter(occurred_column < before)
        return query.filter(
            or_(
                occurred_column < before,
                and_(occurred_column == before, id_column < cursor_source_id),
            )
        )

    db = session_factory()
    try:
        run_occurred = func.coalesce(TaskRun.finished_at, TaskRun.started_at)
        run_query = (
            db.query(TaskRun, ScheduledTask)
            .join(ScheduledTask, ScheduledTask.id == TaskRun.task_id)
        )
        run_query = _owned_task_query(run_query, scope)
        run_query = apply_cursor(
            run_query, run_occurred, TaskRun.id, "automation"
        )
        run_rows = (
            run_query.order_by(run_occurred.desc(), TaskRun.id.desc())
            .limit(scan_limit + 1)
            .all()
        )
        source_overflow = source_overflow or len(run_rows) > scan_limit
        for run, task in run_rows[:scan_limit]:
            occurred = run.finished_at or run.started_at
            if occurred is None:
                continue
            status = str(run.status or "unknown")
            candidates.append((occurred, f"automation:{run.id}", {
                "id": f"automation:{run.id}",
                "source": "automation",
                "title": (task.name or "Automation")[:240],
                "detail": f"Automation {status}",
                "status": status,
                "occurred_at": _iso_utc(occurred),
                "target": "tasks",
                "xp": 0,
            }))

        actor = scope.project_actor
        membership_ids = db.query(ProjectMember.project_id).filter(
            func.lower(ProjectMember.username) == actor
        )
        project_query = (
            db.query(ProjectActivity, Project)
            .join(Project, Project.id == ProjectActivity.project_id)
            .filter(
                or_(func.lower(Project.owner) == actor, Project.id.in_(membership_ids))
            )
        )
        project_query = apply_cursor(
            project_query,
            ProjectActivity.created_at,
            ProjectActivity.id,
            "project",
        )
        project_rows = (
            project_query.order_by(
                ProjectActivity.created_at.desc(), ProjectActivity.id.desc()
            )
            .limit(scan_limit + 1)
            .all()
        )
        source_overflow = source_overflow or len(project_rows) > scan_limit
        for activity, project in project_rows[:scan_limit]:
            occurred = activity.created_at
            if occurred is None:
                continue
            candidates.append((occurred, f"project:{activity.id}", {
                "id": f"project:{activity.id}",
                "source": "project",
                "title": (activity.summary or project.name or "Project updated")[:240],
                "detail": f"{project.name} · {str(activity.event_type or 'updated').replace('_', ' ')}"[:300],
                "status": str(activity.event_type or "updated"),
                "occurred_at": _iso_utc(occurred),
                "target": "projects",
                "xp": 0,
            }))

        progression_owner = normalize_progression_owner(
            scope.owner or _planning_owner(scope)
        )
        progression_query = db.query(ProgressionEvent).filter(
            ProgressionEvent.owner == progression_owner
        )
        progression_query = apply_cursor(
            progression_query,
            ProgressionEvent.occurred_at,
            ProgressionEvent.id,
            "progression",
        )
        progression_rows = (
            progression_query.order_by(
                ProgressionEvent.occurred_at.desc(), ProgressionEvent.id.desc()
            )
            .limit(scan_limit + 1)
            .all()
        )
        source_overflow = source_overflow or len(progression_rows) > scan_limit
        for event in progression_rows[:scan_limit]:
            occurred = event.occurred_at
            if occurred is None:
                continue
            candidates.append((occurred, f"progression:{event.id}", {
                "id": f"progression:{event.id}",
                "source": "progression",
                "title": (event.title or "Objective cleared")[:240],
                "detail": f"Objective cleared · +{int(event.xp or 0)} XP",
                "status": event.source_type,
                "occurred_at": _iso_utc(occurred),
                "target": _activity_target(
                    str(event.source_type or ""), event.details
                ),
                "xp": int(event.xp or 0),
            }))

        candidates.sort(key=lambda row: (row[0], row[1]), reverse=True)
        if before is not None and cursor_id:
            candidates = [
                row for row in candidates
                if (row[0], row[1]) < (before, cursor_id)
            ]
        items = [payload for _, _, payload in candidates[:bounded_limit]]
        counts: dict[str, int] = {}
        for item in items:
            source_name = str(item.get("source") or "unknown")
            counts[source_name] = counts.get(source_name, 0) + 1
        return {
            "status": "ok",
            "items": items,
            "count": len(items),
            "truncated": source_overflow or len(candidates) > bounded_limit,
            "source_counts": counts,
            "next_before": items[-1].get("occurred_at") if len(items) == bounded_limit else None,
            "next_before_id": items[-1].get("id") if len(items) == bounded_limit else None,
        }
    finally:
        db.close()


def setup_mission_control_routes(
    rag_manager: Any = None,
    memory_vector: Any = None,
    *,
    session_factory: Callable[[], Any] = SessionLocal,
    health_collector: Optional[Callable[..., Any]] = None,
    now_factory: Callable[[], datetime] = _utc_now,
    data_dir: Path | str = DATA_DIR,
    health_timeout_seconds: float = _HEALTH_TIMEOUT_SECONDS,
    health_cache_ttl_seconds: float = _HEALTH_CACHE_TTL_SECONDS,
) -> APIRouter:
    """Build the read-only Mission Control router.

    The injectable seams are intentionally small: they make the aggregation
    deterministic in focused tests while production keeps the real database,
    clock, and bounded service-health collector.
    """

    router = APIRouter(prefix="/api/mission-control", tags=["mission-control"])
    mission_data_dir = Path(data_dir)
    health_cache = _HealthSnapshotCache(
        ttl_seconds=health_cache_ttl_seconds,
        timeout_seconds=health_timeout_seconds,
    )

    @router.get("/today")
    async def today_snapshot(
        request: Request,
        utc_offset_minutes: int = Query(
            default=0,
            ge=-840,
            le=840,
            description="Signed minutes local time is ahead of UTC (India is 330).",
        ),
    ) -> dict[str, Any]:
        scope = _resolve_owner_scope(request)
        now_utc = _as_utc(now_factory())
        local_now = now_utc + timedelta(minutes=utc_offset_minutes)
        local_date = local_now.date()
        local_start = datetime.combine(local_date, time.min)
        local_end = local_start + timedelta(days=1)
        utc_start = (local_start - timedelta(minutes=utc_offset_minutes)).replace(
            tzinfo=timezone.utc
        )
        utc_end = (local_end - timedelta(minutes=utc_offset_minutes)).replace(
            tzinfo=timezone.utc
        )
        now_naive = now_utc.replace(tzinfo=None)

        if scope.available:
            calendar = _safe_load(
                "calendar",
                lambda: _load_calendar(
                    session_factory,
                    scope,
                    local_start=local_start,
                    local_end=local_end,
                    utc_start=utc_start.replace(tzinfo=None),
                    utc_end=utc_end.replace(tzinfo=None),
                    utc_offset_minutes=utc_offset_minutes,
                ),
            )
            project_work = _safe_load(
                "project_work",
                lambda: _load_project_work(session_factory, scope, today=local_date),
            )
            planning = _safe_load(
                "planning",
                lambda: _load_planning(session_factory, scope, today=local_date),
            )
            inbox = _safe_load(
                "inbox",
                lambda: _load_inbox(session_factory, scope),
            )
            goals = _safe_load(
                "goals",
                lambda: _load_goals(session_factory, scope, now_naive=now_naive),
            )
            tasks = _safe_load(
                "tasks",
                lambda: _load_tasks(
                    session_factory,
                    scope,
                    utc_start=utc_start,
                    utc_end=utc_end,
                ),
            )
            study_reviews = _safe_load(
                "study_reviews",
                lambda: _load_study_reviews(session_factory, scope, now_naive=now_naive),
            )
            important_mail = _safe_load(
                "important_mail",
                lambda: _load_important_mail(mission_data_dir, scope),
            )
            notes_today = _safe_load(
                "notes_today",
                lambda: _load_notes_today(session_factory, scope),
            )
            daily_brief = _safe_load(
                "daily_brief",
                lambda: _load_daily_brief(session_factory, scope),
            )
            progression = _safe_load(
                "progression",
                lambda: _load_progression(
                    session_factory,
                    scope,
                    utc_offset_minutes=utc_offset_minutes,
                    now_utc=now_utc,
                ),
            )
        else:
            owner_problem = {
                "status": "unavailable",
                "code": "owner_unavailable",
                "message": "Owner scope could not be resolved for this request.",
            }
            calendar = _source_problem("calendar", **owner_problem)
            project_work = _source_problem("project_work", **owner_problem)
            planning = _source_problem("planning", **owner_problem)
            inbox = _source_problem("inbox", **owner_problem)
            goals = _source_problem("goals", **owner_problem)
            tasks = _source_problem("tasks", **owner_problem)
            study_reviews = _source_problem("study_reviews", **owner_problem)
            important_mail = _source_problem("important_mail", **owner_problem)
            notes_today = _source_problem("notes_today", **owner_problem)
            daily_brief = _source_problem("daily_brief", **owner_problem)
            progression = _source_problem("progression", **owner_problem)

        collector = health_collector
        if collector is None:
            from src.service_health import collect_service_health

            collector = collect_service_health
        try:
            health = await health_cache.get(collector, rag_manager, memory_vector)
        except Exception as exc:
            logger.error(
                "Mission Control health source failed (%s)", type(exc).__name__
            )
            health = {
                "status": "error",
                "overall": "unknown",
                "services": [],
                "truncated": False,
                "error": {
                    "code": "health_unavailable",
                    "message": "Could not load service health.",
                },
            }

        sources = {
            "calendar": calendar,
            "project_work": project_work,
            "planning": planning,
            "inbox": inbox,
            "goals": goals,
            "tasks": tasks,
            "study_reviews": study_reviews,
            "important_mail": important_mail,
            "notes_today": notes_today,
            "daily_brief": daily_brief,
            "progression": progression,
            "health": health,
        }
        next_actions = _build_next_actions(sources)
        return {
            "date": local_date.isoformat(),
            "as_of": _iso_utc(now_utc),
            "utc_offset_minutes": utc_offset_minutes,
            "summary": {
                "calendar": calendar["count"],
                "project_work": project_work["count"],
                "planning": int(planning.get("open_count", planning["count"])),
                "inbox": int(inbox.get("unprocessed_count", inbox["count"])),
                "goals": goals["count"],
                "tasks": tasks["count"],
                "study_reviews": study_reviews["count"],
                "important_mail": important_mail["count"],
                "notes_today": notes_today["count"],
                "daily_brief": daily_brief["count"],
                "progression": int(
                    (progression.get("profile") or {}).get("total_xp", 0)
                    if isinstance(progression.get("profile"), dict)
                    else 0
                ),
                "next_actions": len(next_actions),
                "health": health["overall"],
            },
            "next_actions": next_actions,
            "sources": sources,
        }

    @router.get("/activity")
    async def activity_snapshot(
        request: Request,
        limit: int = Query(default=30, ge=1, le=ACTIVITY_ITEM_LIMIT),
        before: datetime | None = Query(
            default=None,
            description="Return activity strictly before this ISO-8601 timestamp.",
        ),
        before_id: str | None = Query(
            default=None,
            max_length=300,
            description="Stable id paired with before so equal timestamps are not skipped.",
        ),
    ) -> dict[str, Any]:
        scope = _resolve_owner_scope(request)
        before_naive = _as_utc(before).replace(tzinfo=None) if before else None
        if scope.available:
            feed = _safe_load(
                "activity",
                lambda: _load_activity_feed(
                    session_factory,
                    scope,
                    limit=limit,
                    before=before_naive,
                    before_id=before_id,
                ),
            )
        else:
            feed = _source_problem(
                "activity",
                status="unavailable",
                code="owner_unavailable",
                message="Owner scope could not be resolved for this request.",
            )

        collector = health_collector
        if collector is None:
            from src.service_health import collect_service_health

            collector = collect_service_health
        try:
            health = await health_cache.get(collector, rag_manager, memory_vector)
        except Exception as exc:
            logger.error(
                "Mission Control activity health failed (%s)", type(exc).__name__
            )
            health = {
                "status": "error",
                "overall": "unknown",
                "services": [],
                "truncated": False,
                "cached": False,
                "stale": False,
                "error": {
                    "code": "health_unavailable",
                    "message": "Could not load service health.",
                },
            }
        return {
            "as_of": _iso_utc(_as_utc(now_factory())),
            "feed": feed,
            "health": health,
        }

    return router
