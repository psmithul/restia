"""Deterministic persistence for Restia V2 human planning items.

Planning is intentionally model-free.  It separates work the user performs
from ``ScheduledTask`` automations, provides optimistic concurrency for linked
instances, and awards progression only on the first verified completion.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case

from core.database import (
    CalendarCal,
    CalendarEvent,
    PlanningItem,
    utcnow_naive,
)
from src.progression import award_progression_event
from src.auth_helpers import DEFAULT_LOCAL_OWNER


ALLOWED_PRIORITIES = {"low", "normal", "high", "critical"}
ALLOWED_STATUSES = {"open", "completed"}


class PlanningError(ValueError):
    """Base class for controlled planning-domain failures."""


class PlanningNotFound(PlanningError):
    pass


class PlanningConflict(PlanningError):
    pass


def normalize_planning_owner(owner: str | None) -> str:
    return str(owner or DEFAULT_LOCAL_OWNER).strip().lower() or DEFAULT_LOCAL_OWNER


def _clean_title(value: Any) -> str:
    title = " ".join(str(value or "").split()).strip()
    if not title:
        raise PlanningError("title is required")
    return title[:240]


def _clean_details(value: Any) -> str:
    return str(value or "").strip()[:20_000]


def _clean_priority(value: Any) -> str:
    priority = str(value or "normal").strip().lower()
    if priority not in ALLOWED_PRIORITIES:
        raise PlanningError("priority must be low, normal, high, or critical")
    return priority


def _clean_due_date(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    raw = str(value).strip()
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise PlanningError("due_date must use YYYY-MM-DD") from exc


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def serialize_planning_item(item: PlanningItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "title": item.title,
        "details": item.details or "",
        "status": item.status,
        "priority": item.priority or "normal",
        "due_date": item.due_date,
        "scheduled_start": _iso_utc(item.scheduled_start),
        "scheduled_end": _iso_utc(item.scheduled_end),
        "calendar_id": item.calendar_id,
        "calendar_event_uid": item.calendar_event_uid,
        "completed_at": _iso_utc(item.completed_at),
        "source": item.source or "user",
        "version": int(item.version or 1),
        "created_at": _iso_utc(item.created_at),
        "updated_at": _iso_utc(item.updated_at),
    }


def _owned_item(db: Any, owner: str | None, item_id: str) -> PlanningItem:
    row = (
        db.query(PlanningItem)
        .filter(
            PlanningItem.id == str(item_id),
            PlanningItem.owner == normalize_planning_owner(owner),
        )
        .first()
    )
    if row is None:
        raise PlanningNotFound("Planning item not found")
    return row


def _check_version(item: PlanningItem, expected_version: int) -> None:
    if int(expected_version) != int(item.version or 1):
        raise PlanningConflict(
            f"Planning item changed in another client (current version {int(item.version or 1)})"
        )


def _reserve_next_version(db: Any, item: PlanningItem, expected_version: int) -> None:
    """Atomically reserve the next version before mutating linked records."""

    _check_version(item, expected_version)
    next_version = int(expected_version) + 1
    updated = (
        db.query(PlanningItem)
        .filter(
            PlanningItem.id == item.id,
            PlanningItem.owner == item.owner,
            PlanningItem.version == int(expected_version),
        )
        .update(
            {
                PlanningItem.version: next_version,
                PlanningItem.updated_at: utcnow_naive(),
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        db.expire_all()
        current = (
            db.query(PlanningItem.version)
            .filter(PlanningItem.id == item.id, PlanningItem.owner == item.owner)
            .scalar()
        )
        raise PlanningConflict(
            f"Planning item changed in another client (current version {int(current or 1)})"
        )
    item.version = next_version


def create_planning_item(
    db: Any,
    *,
    owner: str | None,
    title: str,
    details: str = "",
    priority: str = "normal",
    due_date: str | None = None,
    source: str = "user",
) -> PlanningItem:
    item = PlanningItem(
        id=str(uuid.uuid4()),
        owner=normalize_planning_owner(owner),
        title=_clean_title(title),
        details=_clean_details(details),
        priority=_clean_priority(priority),
        due_date=_clean_due_date(due_date),
        status="open",
        source=(str(source or "user").strip().lower() or "user")[:24],
        version=1,
    )
    db.add(item)
    db.flush()
    return item


def list_planning_items(
    db: Any,
    *,
    owner: str | None,
    status: str = "all",
    limit: int = 50,
) -> tuple[list[PlanningItem], bool]:
    normalized_status = str(status or "all").strip().lower()
    if normalized_status not in {"all", *ALLOWED_STATUSES}:
        raise PlanningError("status must be all, open, or completed")
    bounded_limit = max(1, min(100, int(limit)))
    query = db.query(PlanningItem).filter(
        PlanningItem.owner == normalize_planning_owner(owner)
    )
    if normalized_status != "all":
        query = query.filter(PlanningItem.status == normalized_status)
    status_rank = case((PlanningItem.status == "open", 0), else_=1)
    due_rank = case((PlanningItem.due_date.is_(None), 1), else_=0)
    rows = (
        query.order_by(
            status_rank.asc(),
            due_rank.asc(),
            PlanningItem.due_date.asc(),
            PlanningItem.updated_at.desc(),
            PlanningItem.id.asc(),
        )
        .limit(bounded_limit + 1)
        .all()
    )
    return rows[:bounded_limit], len(rows) > bounded_limit


_UNSET = object()


def update_planning_item(
    db: Any,
    *,
    owner: str | None,
    item_id: str,
    expected_version: int,
    title: Any = _UNSET,
    details: Any = _UNSET,
    priority: Any = _UNSET,
    due_date: Any = _UNSET,
) -> PlanningItem:
    item = _owned_item(db, owner, item_id)
    _reserve_next_version(db, item, expected_version)
    if title is not _UNSET:
        item.title = _clean_title(title)
    if details is not _UNSET:
        item.details = _clean_details(details)
    if priority is not _UNSET:
        item.priority = _clean_priority(priority)
    if due_date is not _UNSET:
        item.due_date = _clean_due_date(due_date)
    if item.calendar_event_uid:
        event = (
            db.query(CalendarEvent)
            .join(CalendarCal, CalendarCal.id == CalendarEvent.calendar_id)
            .filter(
                CalendarEvent.uid == item.calendar_event_uid,
                CalendarCal.owner == item.owner,
            )
            .first()
        )
        if event is not None:
            event.summary = item.title
            event.description = item.details
    db.flush()
    return item


def complete_planning_item(
    db: Any,
    *,
    owner: str | None,
    item_id: str,
    expected_version: int,
    now: datetime | None = None,
) -> PlanningItem:
    item = _owned_item(db, owner, item_id)
    _check_version(item, expected_version)
    if item.status == "completed":
        return item
    _reserve_next_version(db, item, expected_version)
    completed_at = _as_naive_utc(now or utcnow_naive())
    item.status = "completed"
    item.completed_at = completed_at
    award_progression_event(
        db,
        owner=item.owner,
        event_key=f"planning:{item.id}:completed",
        source_type="todo_item_completed",
        source_id=item.id,
        title=item.title,
        details={"planning_item_id": item.id},
        occurred_at=completed_at,
    )
    db.flush()
    return item


def reopen_planning_item(
    db: Any,
    *,
    owner: str | None,
    item_id: str,
    expected_version: int,
) -> PlanningItem:
    item = _owned_item(db, owner, item_id)
    _check_version(item, expected_version)
    if item.status == "open":
        return item
    _reserve_next_version(db, item, expected_version)
    item.status = "open"
    item.completed_at = None
    db.flush()
    return item


def _owner_calendar(db: Any, owner: str, calendar_id: str | None) -> CalendarCal:
    query = db.query(CalendarCal).filter(CalendarCal.owner == owner)
    if calendar_id:
        calendar = query.filter(CalendarCal.id == str(calendar_id)).first()
        if calendar is None:
            raise PlanningNotFound("Calendar not found")
        return calendar
    calendar = query.order_by(CalendarCal.created_at.asc(), CalendarCal.id.asc()).first()
    if calendar is None:
        calendar = CalendarCal(
            id=str(uuid.uuid4()),
            owner=owner,
            name="Personal",
            color="#5b8abf",
            source="local",
        )
        db.add(calendar)
        db.flush()
    return calendar


def schedule_planning_item(
    db: Any,
    *,
    owner: str | None,
    item_id: str,
    expected_version: int,
    start: datetime,
    end: datetime | None = None,
    due_date: str | None = None,
    add_to_calendar: bool = True,
    calendar_id: str | None = None,
) -> PlanningItem:
    item = _owned_item(db, owner, item_id)
    _reserve_next_version(db, item, expected_version)
    start_utc = _as_naive_utc(start)
    end_utc = _as_naive_utc(end) if end is not None else start_utc + timedelta(minutes=30)
    if end_utc <= start_utc:
        raise PlanningError("schedule end must be after start")
    if end_utc - start_utc > timedelta(days=7):
        raise PlanningError("schedule duration must not exceed seven days")

    item.scheduled_start = start_utc
    item.scheduled_end = end_utc
    item.due_date = _clean_due_date(due_date) if due_date is not None else start_utc.date().isoformat()

    linked_event = None
    if item.calendar_event_uid:
        linked_event = (
            db.query(CalendarEvent)
            .join(CalendarCal, CalendarCal.id == CalendarEvent.calendar_id)
            .filter(
                CalendarEvent.uid == item.calendar_event_uid,
                CalendarCal.owner == item.owner,
            )
            .first()
        )

    if not add_to_calendar:
        if linked_event is not None:
            db.delete(linked_event)
        item.calendar_id = None
        item.calendar_event_uid = None
    else:
        calendar = _owner_calendar(db, item.owner, calendar_id or item.calendar_id)
        if linked_event is None:
            linked_event = CalendarEvent(
                uid=str(uuid.uuid4()),
                calendar_id=calendar.id,
                summary=item.title,
                description=item.details,
                dtstart=start_utc,
                dtend=end_utc,
                all_day=False,
                is_utc=True,
                origin="local",
                event_type="work",
                importance="high" if item.priority in {"high", "critical"} else "normal",
            )
            db.add(linked_event)
        else:
            linked_event.calendar_id = calendar.id
            linked_event.summary = item.title
            linked_event.description = item.details
            linked_event.dtstart = start_utc
            linked_event.dtend = end_utc
            linked_event.is_utc = True
            linked_event.status = "confirmed"
        item.calendar_id = calendar.id
        item.calendar_event_uid = linked_event.uid

    db.flush()
    return item
