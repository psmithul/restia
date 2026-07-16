"""Deterministic, evidence-backed progression for Restia V2.

The progression system never calls a model and has no public mutation API.
Feature routes award an immutable event only after they verify a real state
transition. Stable event keys make XP idempotent across retries and prevent a
user from farming the same item by repeatedly reopening it.
"""

from __future__ import annotations

import math
import uuid
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from core.database import ProgressionEvent, SessionLocal, utcnow_naive
from src.auth_helpers import DEFAULT_LOCAL_OWNER


XP_AWARDS = {
    "project_checklist_completed": 10,
    "todo_item_completed": 20,
    "calendar_event_completed": 25,
    "study_review_passed": 35,
    "project_work_item_completed": 50,
    "project_completed": 200,
}

SOURCE_LABELS = {
    "project_checklist_completed": "Checklist cleared",
    "todo_item_completed": "To do cleared",
    "calendar_event_completed": "Calendar objective cleared",
    "study_review_passed": "Study review cleared",
    "project_work_item_completed": "Project task cleared",
    "project_completed": "Project cleared",
}

_RECENT_EVENT_LIMIT = 12
_STREAK_SCAN_LIMIT = 5000


def normalize_progression_owner(owner: str | None) -> str:
    """Return a concrete, case-stable owner without broadening account scope."""

    return str(owner or DEFAULT_LOCAL_OWNER).strip().lower() or DEFAULT_LOCAL_OWNER


def award_progression_event(
    db: Any,
    *,
    owner: str | None,
    event_key: str,
    source_type: str,
    source_id: str = "",
    title: str = "Completed work",
    xp: int | None = None,
    details: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> tuple[ProgressionEvent, bool]:
    """Add one completion event to an existing transaction.

    Returns ``(event, created)``. Duplicate event keys return the original row
    and do not disturb the caller's transaction. Callers remain responsible for
    committing the enclosing state transition and this event together.
    """

    owner_key = normalize_progression_owner(owner)
    event_key = str(event_key or "").strip()[:180]
    source_type = str(source_type or "").strip()[:48]
    source_id = str(source_id or "").strip()[:180]
    if not event_key:
        raise ValueError("A progression event key is required")
    if source_type not in XP_AWARDS:
        raise ValueError(f"Unsupported progression source: {source_type}")
    award = XP_AWARDS[source_type] if xp is None else int(xp)
    if award < 1 or award > 1000:
        raise ValueError("Progression XP must be between 1 and 1000")

    existing = (
        db.query(ProgressionEvent)
        .filter(
            ProgressionEvent.owner == owner_key,
            ProgressionEvent.event_key == event_key,
        )
        .first()
    )
    if existing is not None:
        return existing, False

    row = ProgressionEvent(
        id=str(uuid.uuid4()),
        owner=owner_key,
        event_key=event_key,
        source_type=source_type,
        source_id=source_id,
        title=(str(title or "Completed work").strip() or "Completed work")[:240],
        xp=award,
        details=dict(details or {}),
        occurred_at=occurred_at or utcnow_naive(),
    )
    try:
        # A savepoint turns a concurrent duplicate into a harmless no-op while
        # preserving the feature route's surrounding transaction.
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        existing = (
            db.query(ProgressionEvent)
            .filter(
                ProgressionEvent.owner == owner_key,
                ProgressionEvent.event_key == event_key,
            )
            .first()
        )
        if existing is None:
            raise
        return existing, False
    return row, True


def xp_before_level(level: int) -> int:
    """Cumulative XP required to enter ``level`` (level one starts at zero)."""

    safe_level = max(1, int(level))
    completed_levels = safe_level - 1
    return 50 * completed_levels * (completed_levels + 1)


def profile_for_xp(total_xp: int) -> dict[str, Any]:
    total = max(0, int(total_xp or 0))
    # Solve 50*k*(k+1) <= XP for completed levels k, then guard the result
    # against floating-point boundaries with the exact integer formula.
    completed = max(0, int((math.sqrt(1 + (total / 12.5)) - 1) // 2))
    level = completed + 1
    while xp_before_level(level + 1) <= total:
        level += 1
    while level > 1 and xp_before_level(level) > total:
        level -= 1

    current_floor = xp_before_level(level)
    next_floor = xp_before_level(level + 1)
    if level >= 30:
        rank = "S"
        rank_name = "S-Rank"
    elif level >= 20:
        rank = "A"
        rank_name = "A-Rank"
    elif level >= 15:
        rank = "B"
        rank_name = "B-Rank"
    elif level >= 10:
        rank = "C"
        rank_name = "C-Rank"
    elif level >= 5:
        rank = "D"
        rank_name = "D-Rank"
    else:
        rank = "E"
        rank_name = "E-Rank"
    into_level = total - current_floor
    level_span = max(1, next_floor - current_floor)
    return {
        "level": level,
        "rank": rank,
        "rank_name": rank_name,
        "total_xp": total,
        "level_xp": into_level,
        "next_level_xp": level_span,
        "xp_to_next_level": max(0, next_floor - total),
        "progress_percent": min(99, int(into_level * 100 / level_span)),
    }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _naive_utc(value: datetime) -> datetime:
    return _as_utc(value).replace(tzinfo=None)


def _local_date(value: datetime, offset_minutes: int) -> date:
    return (_as_utc(value) + timedelta(minutes=offset_minutes)).date()


def _streaks(days: Iterable[date], today: date) -> tuple[int, int]:
    unique = sorted(set(days), reverse=True)
    if not unique:
        return 0, 0

    current = 0
    cursor = today if unique[0] == today else today - timedelta(days=1)
    available = set(unique)
    while cursor in available:
        current += 1
        cursor -= timedelta(days=1)

    longest = 1
    run = 1
    ascending = sorted(available)
    for previous, next_day in zip(ascending, ascending[1:]):
        if next_day == previous + timedelta(days=1):
            run += 1
            longest = max(longest, run)
        else:
            run = 1
    return current, longest


def _event_payload(row: ProgressionEvent) -> dict[str, Any]:
    occurred = row.occurred_at
    if occurred is not None:
        occurred = _as_utc(occurred).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {
        "id": row.id,
        "source_type": row.source_type,
        "source_label": SOURCE_LABELS.get(row.source_type, "Objective cleared"),
        "source_id": row.source_id,
        "title": row.title,
        "xp": int(row.xp or 0),
        "details": row.details if isinstance(row.details, dict) else {},
        "occurred_at": occurred,
    }


def build_progression_summary(
    *,
    owner: str | None,
    session_factory: Callable[[], Any] = SessionLocal,
    utc_offset_minutes: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build an owner-scoped rank, quest, streak, and achievement snapshot."""

    if utc_offset_minutes < -840 or utc_offset_minutes > 840:
        raise ValueError("utc_offset_minutes must be between -840 and 840")
    owner_key = normalize_progression_owner(owner)
    now_utc = _as_utc(now or datetime.now(timezone.utc))
    local_now = now_utc + timedelta(minutes=utc_offset_minutes)
    today = local_now.date()
    local_day_start = datetime.combine(today, time.min)
    today_start = _naive_utc(
        (local_day_start - timedelta(minutes=utc_offset_minutes)).replace(tzinfo=timezone.utc)
    )
    tomorrow_start = today_start + timedelta(days=1)
    week_start_local = datetime.combine(today - timedelta(days=today.weekday()), time.min)
    week_start = _naive_utc(
        (week_start_local - timedelta(minutes=utc_offset_minutes)).replace(tzinfo=timezone.utc)
    )

    db = session_factory()
    try:
        base = db.query(ProgressionEvent).filter(ProgressionEvent.owner == owner_key)
        total_xp = int(base.with_entities(func.coalesce(func.sum(ProgressionEvent.xp), 0)).scalar() or 0)
        total_clears = int(base.with_entities(func.count(ProgressionEvent.id)).scalar() or 0)
        recent = (
            base.order_by(ProgressionEvent.occurred_at.desc(), ProgressionEvent.id.desc())
            .limit(_RECENT_EVENT_LIMIT)
            .all()
        )
        today_rows = (
            base.filter(
                ProgressionEvent.occurred_at >= today_start,
                ProgressionEvent.occurred_at < tomorrow_start,
            )
            .order_by(ProgressionEvent.occurred_at.asc(), ProgressionEvent.id.asc())
            .all()
        )
        today_counts = Counter(row.source_type for row in today_rows)
        today_xp = sum(int(row.xp or 0) for row in today_rows)
        week_xp = int(
            base.filter(ProgressionEvent.occurred_at >= week_start)
            .with_entities(func.coalesce(func.sum(ProgressionEvent.xp), 0))
            .scalar()
            or 0
        )
        source_counts = {
            str(source): int(count)
            for source, count in (
                base.with_entities(ProgressionEvent.source_type, func.count(ProgressionEvent.id))
                .group_by(ProgressionEvent.source_type)
                .all()
            )
        }
        timestamp_rows = (
            base.with_entities(ProgressionEvent.occurred_at)
            .order_by(ProgressionEvent.occurred_at.desc())
            .limit(_STREAK_SCAN_LIMIT + 1)
            .all()
        )
        history_truncated = len(timestamp_rows) > _STREAK_SCAN_LIMIT
        dates = [
            _local_date(row[0], utc_offset_minutes)
            for row in timestamp_rows[:_STREAK_SCAN_LIMIT]
            if row[0] is not None
        ]
        current_streak, longest_streak = _streaks(dates, today)
        profile = profile_for_xp(total_xp)

        first_event = (
            base.order_by(ProgressionEvent.occurred_at.asc(), ProgressionEvent.id.asc()).first()
        )
        first_project = (
            base.filter(ProgressionEvent.source_type == "project_completed")
            .order_by(ProgressionEvent.occurred_at.asc(), ProgressionEvent.id.asc())
            .first()
        )
        tactical_count = sum(
            source_counts.get(kind, 0)
            for kind in ("todo_item_completed", "project_work_item_completed", "calendar_event_completed")
        )

        quests = [
            {
                "id": "first-clear",
                "title": "First clear",
                "description": "Complete one real objective today.",
                "current": min(total := len(today_rows), 1),
                "target": 1,
                "complete": total >= 1,
            },
            {
                "id": "project-pressure",
                "title": "Project pressure",
                "description": "Complete one project task today.",
                "current": min(project_done := today_counts.get("project_work_item_completed", 0), 1),
                "target": 1,
                "complete": project_done >= 1,
            },
            {
                "id": "triple-clear",
                "title": "Triple clear",
                "description": "Clear three objectives across Restia today.",
                "current": min(len(today_rows), 3),
                "target": 3,
                "complete": len(today_rows) >= 3,
            },
        ]

        achievements = [
            {
                "id": "awakening",
                "title": "Awakening",
                "description": "Record your first verified completion.",
                "unlocked": first_event is not None,
                "unlocked_at": _event_payload(first_event)["occurred_at"] if first_event else None,
            },
            {
                "id": "tactical-ten",
                "title": "Tactical Ten",
                "description": "Clear ten todos, calendar objectives, or project tasks.",
                "unlocked": tactical_count >= 10,
                "unlocked_at": None,
            },
            {
                "id": "project-breaker",
                "title": "Project Breaker",
                "description": "Complete an entire project.",
                "unlocked": first_project is not None,
                "unlocked_at": _event_payload(first_project)["occurred_at"] if first_project else None,
            },
            {
                "id": "three-day-streak",
                "title": "Momentum",
                "description": "Maintain a three-day completion streak.",
                "unlocked": longest_streak >= 3,
                "unlocked_at": None,
            },
            {
                "id": "rank-d",
                "title": "Rank Advancement",
                "description": "Reach D-Rank.",
                "unlocked": profile["level"] >= 5,
                "unlocked_at": None,
            },
        ]

        return {
            "owner": owner_key,
            "profile": profile,
            "today": {
                "date": today.isoformat(),
                "xp": today_xp,
                "clears": len(today_rows),
                "quests": quests,
                "quests_complete": sum(1 for quest in quests if quest["complete"]),
            },
            "week": {
                "xp": week_xp,
                "target_xp": 500,
                "progress_percent": 100 if week_xp >= 500 else int(week_xp * 100 / 500),
            },
            "streak": {
                "current_days": current_streak,
                "longest_days": longest_streak,
                "history_truncated": history_truncated,
            },
            "stats": {
                "total_clears": total_clears,
                "source_counts": source_counts,
            },
            "achievements": achievements,
            "recent_events": [_event_payload(row) for row in recent],
        }
    finally:
        db.close()
