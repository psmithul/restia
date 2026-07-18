"""Deterministic owner-scoped calendar and time intelligence.

This module only reads authoritative calendar/Life Graph rows.  It proposes
free slots, preparation, follow-up, and rescheduling opportunities, but never
changes an event or task; mutations stay behind the ActionPolicy boundary.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable

from dateutil.rrule import rrulestr

from core.database import CalendarEvent, EntityLink, LifeEntity
from src.life_graph import LifeGraphError


CALENDAR_INTELLIGENCE_LIMIT = 2_000
CALENDAR_OCCURRENCE_LIMIT = 4_000
CALENDAR_TASK_LIMIT = 500
_TERMINAL_TASK_STATUSES = frozenset({
    "completed", "done", "cancelled", "canceled", "archived", "deleted",
})
_TASK_ENERGY_LEVELS = frozenset({"low", "medium", "high", "any"})
_TASK_PRIORITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}
_MEETING_CONTEXT_TYPES = frozenset({
    "person", "project", "note", "file", "event", "decision", "task",
})


def _aware(value: object, *, field: str) -> datetime:
    if isinstance(value, str):
        raw = value.strip().replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise LifeGraphError(f"{field} must be an offset-aware ISO datetime") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise LifeGraphError(f"{field} must include an explicit UTC offset")
    return value


def _bounded_int(value: object, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise LifeGraphError(f"{field} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise LifeGraphError(f"{field} must be an integer") from exc
    if number < minimum or number > maximum:
        raise LifeGraphError(f"{field} must be between {minimum} and {maximum}")
    return number


def _event_times(event: CalendarEvent, offset) -> tuple[datetime, datetime]:
    if bool(event.is_utc):
        start = event.dtstart.replace(tzinfo=timezone.utc).astimezone(offset)
        end = event.dtend.replace(tzinfo=timezone.utc).astimezone(offset)
    else:
        start = event.dtstart.replace(tzinfo=offset)
        end = event.dtend.replace(tzinfo=offset)
    return start, end


def _exdates(event: CalendarEvent) -> set[str]:
    try:
        values = json.loads(event.recurrence_exdates or "[]")
    except (TypeError, ValueError):
        return set()
    return {str(value) for value in values if isinstance(value, str)}


def _occurrences(
    events: Iterable[CalendarEvent], *, start: datetime, end: datetime,
) -> tuple[list[dict[str, Any]], bool]:
    offset = start.tzinfo
    rows: list[dict[str, Any]] = []
    truncated = False
    for event in events:
        base_start, base_end = _event_times(event, offset)
        duration = base_end - base_start
        starts = [base_start]
        if str(event.rrule or "").strip():
            try:
                rule = rrulestr(str(event.rrule), dtstart=base_start)
                starts = list(rule.between(start - duration, end, inc=True))
            except (TypeError, ValueError, OverflowError):
                starts = [base_start]
        skipped = _exdates(event)
        for occurrence_start in starts:
            occurrence_end = occurrence_start + duration
            utc_key = occurrence_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            local_key = occurrence_start.replace(tzinfo=None).isoformat()
            if utc_key in skipped or local_key in skipped:
                continue
            if occurrence_start >= end or occurrence_end <= start:
                continue
            rows.append({
                "uid": str(event.uid),
                "occurrence_id": (
                    str(event.uid) if occurrence_start == base_start
                    else f"{event.uid}::{utc_key}"
                ),
                "summary": str(event.summary or "") or "Untitled event",
                "start": occurrence_start.isoformat(),
                "end": occurrence_end.isoformat(),
                "start_value": occurrence_start,
                "end_value": occurrence_end,
                "duration_minutes": max(1, round(duration.total_seconds() / 60)),
                "all_day": bool(event.all_day),
                "event_type": str(event.event_type or "other"),
                "importance": str(event.importance or "normal"),
                "location": str(event.location or ""),
                "version": int(event.version or 1),
            })
            if len(rows) >= CALENDAR_OCCURRENCE_LIMIT:
                truncated = True
                break
        if truncated:
            break
    rows.sort(key=lambda row: (row["start_value"], row["end_value"], row["uid"]))
    return rows, truncated


def _merge_intervals(rows: Iterable[dict[str, Any]]) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for row in rows:
        start, end = row["start_value"], row["end_value"]
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return [(row[0], row[1]) for row in merged]


def _free_slots(
    rows: list[dict[str, Any]], *, start: datetime, end: datetime,
    day_start_hour: int, day_end_hour: int, minimum_minutes: int,
) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    cursor_day = start.date()
    while cursor_day <= end.date() and len(slots) < 100:
        day_start = datetime.combine(cursor_day, time(day_start_hour), tzinfo=start.tzinfo)
        day_end = (
            datetime.combine(cursor_day + timedelta(days=1), time.min, tzinfo=start.tzinfo)
            if day_end_hour == 24
            else datetime.combine(cursor_day, time(day_end_hour), tzinfo=start.tzinfo)
        )
        day_start = max(day_start, start)
        day_end = min(day_end, end)
        if day_end > day_start:
            busy = _merge_intervals(
                row for row in rows
                if not row["all_day"]
                and row["start_value"] < day_end and row["end_value"] > day_start
            )
            cursor = day_start
            for busy_start, busy_end in busy:
                if busy_start > cursor and (busy_start - cursor).total_seconds() >= minimum_minutes * 60:
                    slots.append({
                        "start": cursor.isoformat(), "end": busy_start.isoformat(),
                        "duration_minutes": round((busy_start - cursor).total_seconds() / 60),
                    })
                cursor = max(cursor, busy_end)
            if day_end > cursor and (day_end - cursor).total_seconds() >= minimum_minutes * 60:
                slots.append({
                    "start": cursor.isoformat(), "end": day_end.isoformat(),
                    "duration_minutes": round((day_end - cursor).total_seconds() / 60),
                })
        cursor_day += timedelta(days=1)
    return slots[:100]


def _task_time_blocks(
    db,
    *,
    owner_id: str,
    slots: list[dict[str, Any]],
    offset,
    preferred_energy: str,
) -> tuple[list[dict[str, Any]], dict[str, int | str | bool]]:
    candidates = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == "task",
        LifeEntity.deleted_at.is_(None),
        ~LifeEntity.status.in_(_TERMINAL_TASK_STATUSES),
    ).order_by(
        LifeEntity.due_at.is_(None), LifeEntity.due_at.asc(),
        LifeEntity.updated_at.asc(), LifeEntity.id.asc(),
    ).limit(CALENDAR_TASK_LIMIT + 1).all()
    truncated = len(candidates) > CALENDAR_TASK_LIMIT
    candidates = candidates[:CALENDAR_TASK_LIMIT]
    schedulable: list[dict[str, Any]] = []
    missing_duration = 0
    for task in candidates:
        properties = dict(task.properties or {})
        raw_effort = properties.get("effort_minutes")
        if isinstance(raw_effort, bool):
            raw_effort = None
        try:
            effort = int(raw_effort)
        except (TypeError, ValueError):
            effort = 0
        if effort < 15 or effort > 1_440:
            missing_duration += 1
            continue
        energy = str(properties.get("energy") or "any").strip().lower()
        if energy not in _TASK_ENERGY_LEVELS:
            energy = "any"
        priority = str(properties.get("priority") or "normal").strip().lower()
        if priority not in _TASK_PRIORITY_RANK:
            priority = "normal"
        due_at = task.due_at
        if due_at is not None:
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
            due_at = due_at.astimezone(offset)
        schedulable.append({
            "entity": task,
            "effort_minutes": effort,
            "energy": energy,
            "priority": priority,
            "due_at": due_at,
            "energy_match": preferred_energy == "any" or energy in {
                "any", preferred_energy,
            },
        })
    schedulable.sort(key=lambda row: (
        not row["energy_match"],
        _TASK_PRIORITY_RANK[row["priority"]],
        row["due_at"] is None,
        row["due_at"] or datetime.max.replace(tzinfo=offset),
        row["entity"].id,
    ))

    available = [{
        "cursor": datetime.fromisoformat(slot["start"]),
        "end": datetime.fromisoformat(slot["end"]),
    } for slot in slots]
    blocks: list[dict[str, Any]] = []
    for task in schedulable:
        duration = timedelta(minutes=task["effort_minutes"])
        due_at = task["due_at"]
        deadline_slots = [
            slot for slot in available
            if slot["end"] - slot["cursor"] >= duration
            and (due_at is None or slot["cursor"] + duration <= due_at)
        ]
        selected = next(iter(deadline_slots), None)
        if selected is None:
            selected = next((
                slot for slot in available
                if slot["end"] - slot["cursor"] >= duration
            ), None)
        if selected is None:
            continue
        block_start = selected["cursor"]
        block_end = block_start + duration
        selected["cursor"] = block_end
        due_text = due_at.isoformat() if due_at is not None else None
        deadline_fit = due_at is None or block_end <= due_at
        task_entity = task["entity"]
        reason = (
            f"Fits the task's {task['effort_minutes']}-minute estimate and "
            f"{task['energy']} energy requirement"
        )
        if due_text and deadline_fit:
            reason += f" before its {due_text} deadline"
        elif due_text:
            reason += f" at the earliest available time after its {due_text} deadline"
        reason += "."
        blocks.append({
            "task_id": task_entity.id,
            "task_title": str(task_entity.title or "Untitled task"),
            "start": block_start.isoformat(),
            "end": block_end.isoformat(),
            "duration_minutes": task["effort_minutes"],
            "energy": task["energy"],
            "preferred_energy_match": bool(task["energy_match"]),
            "priority": task["priority"],
            "due_at": due_text,
            "deadline_fit": deadline_fit,
            "reason": reason,
            "proposed_action": {
                "tool": "manage_calendar",
                "action": "create_event",
                "requires_confirmation": True,
                "arguments": {
                    "summary": f"Focus: {str(task_entity.title or 'Untitled task')}",
                    "dtstart": block_start.isoformat(),
                    "dtend": block_end.isoformat(),
                    "event_type": "focus",
                    "linked_entity_ids": [task_entity.id],
                },
            },
        })
        if len(blocks) >= 10:
            break
    return blocks, {
        "candidates_considered": len(candidates),
        "schedulable_candidates": len(schedulable),
        "missing_duration": missing_duration,
        "preferred_energy": preferred_energy,
        "truncated": truncated,
    }


def _linked_context(db, *, owner_id: str, event_uids: list[str]) -> dict[str, list[dict[str, str]]]:
    if not event_uids:
        return {}
    projections = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == "event",
        LifeEntity.domain_ref_type == "calendar_event",
        LifeEntity.domain_ref_id.in_(event_uids),
        LifeEntity.deleted_at.is_(None),
    ).all()
    by_projection = {row.id: row.domain_ref_id for row in projections}
    links = db.query(EntityLink).filter(
        EntityLink.owner_id == owner_id,
        EntityLink.source_id.in_(list(by_projection)),
        EntityLink.deleted_at.is_(None),
    ).all() if by_projection else []
    target_ids = [row.target_id for row in links]
    targets = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.id.in_(target_ids),
        LifeEntity.deleted_at.is_(None),
    ).all() if target_ids else []
    target_map = {row.id: row for row in targets}
    result: dict[str, list[dict[str, str]]] = defaultdict(list)
    for link in links:
        target = target_map.get(link.target_id)
        event_uid = by_projection.get(link.source_id)
        if target is None or not event_uid:
            continue
        result[event_uid].append({
            "id": target.id, "entity_type": target.entity_type,
            "title": target.title, "status": target.status,
            "relation": link.relation,
        })
    for values in result.values():
        values.sort(key=lambda row: (row["entity_type"], row["title"], row["id"]))
    return dict(result)


def calendar_time_report(
    db, *, owner_id: str, as_of: object, window_start: object,
    window_end: object, minimum_slot_minutes: object = 30,
    day_start_hour: object = 6, day_end_hour: object = 23,
    daily_capacity_minutes: object = 600, travel_buffer_minutes: object = 30,
    preferred_energy: object = "any",
) -> dict[str, Any]:
    """Return a deterministic time plan with evidence and no side effects."""

    now = _aware(as_of, field="as_of")
    start = _aware(window_start, field="window_start")
    end = _aware(window_end, field="window_end")
    if end <= start or end - start > timedelta(days=62):
        raise LifeGraphError("window_end must be after window_start and within 62 days")
    if now.utcoffset() != start.utcoffset() or start.utcoffset() != end.utcoffset():
        raise LifeGraphError("as_of and calendar window must use the same UTC offset")
    minimum = _bounded_int(minimum_slot_minutes, field="minimum_slot_minutes", minimum=15, maximum=480)
    day_start = _bounded_int(day_start_hour, field="day_start_hour", minimum=0, maximum=22)
    day_end = _bounded_int(day_end_hour, field="day_end_hour", minimum=1, maximum=24)
    if day_end <= day_start:
        raise LifeGraphError("day_end_hour must be later than day_start_hour")
    capacity = _bounded_int(daily_capacity_minutes, field="daily_capacity_minutes", minimum=30, maximum=1_440)
    travel_buffer = _bounded_int(travel_buffer_minutes, field="travel_buffer_minutes", minimum=0, maximum=240)
    energy = str(preferred_energy or "any").strip().lower()
    if energy not in _TASK_ENERGY_LEVELS:
        raise LifeGraphError("preferred_energy must be low, medium, high, or any")

    # The bounded candidate scan is owner-filtered first. Occurrence overlap is
    # evaluated after explicit timezone normalization so naive-local legacy rows
    # are never silently shifted.
    events = db.query(CalendarEvent).filter(
        CalendarEvent.owner_id == owner_id,
        CalendarEvent.status != "cancelled",
    ).order_by(CalendarEvent.dtstart.asc(), CalendarEvent.uid.asc()).limit(
        CALENDAR_INTELLIGENCE_LIMIT + 1
    ).all()
    source_truncated = len(events) > CALENDAR_INTELLIGENCE_LIMIT
    occurrences, occurrence_truncated = _occurrences(
        events[:CALENDAR_INTELLIGENCE_LIMIT], start=start, end=end,
    )
    links = _linked_context(
        db, owner_id=owner_id,
        event_uids=sorted({row["uid"] for row in occurrences}),
    )
    for row in occurrences:
        row["linked_entities"] = links.get(row["uid"], [])

    conflicts: list[dict[str, Any]] = []
    for index, left in enumerate(occurrences):
        if left["all_day"]:
            continue
        for right in occurrences[index + 1:]:
            if right["start_value"] >= left["end_value"]:
                break
            if right["all_day"] or right["end_value"] <= left["start_value"]:
                continue
            conflicts.append({
                "event_ids": [left["occurrence_id"], right["occurrence_id"]],
                "overlap_start": max(left["start_value"], right["start_value"]).isoformat(),
                "overlap_end": min(left["end_value"], right["end_value"]).isoformat(),
                "reason": "Confirmed timed events overlap.",
            })

    busy_by_day: dict[str, int] = defaultdict(int)
    for busy_start, busy_end in _merge_intervals(
        row for row in occurrences if not row["all_day"]
    ):
        cursor = busy_start
        while cursor.date() <= busy_end.date():
            boundary = min(
                busy_end,
                datetime.combine(cursor.date() + timedelta(days=1), time.min, tzinfo=start.tzinfo),
            )
            busy_by_day[cursor.date().isoformat()] += max(
                0, round((boundary - cursor).total_seconds() / 60)
            )
            cursor = boundary
            if cursor >= busy_end:
                break
    overload = [
        {"date": day, "busy_minutes": minutes, "capacity_minutes": capacity,
         "over_by_minutes": minutes - capacity}
        for day, minutes in sorted(busy_by_day.items()) if minutes > capacity
    ]

    preparation: list[dict[str, Any]] = []
    follow_up: list[dict[str, Any]] = []
    unfinished: list[dict[str, Any]] = []
    focus_risks: list[dict[str, Any]] = []
    travel_risks: list[dict[str, Any]] = []
    for index, row in enumerate(occurrences):
        linked = row["linked_entities"]
        linked_types = {item["entity_type"] for item in linked}
        kind = row["event_type"]
        if kind in {"meeting", "class", "work"} and now <= row["start_value"] <= now + timedelta(hours=48):
            preparation.append({
                "event_id": row["occurrence_id"], "summary": row["summary"],
                "starts_at": row["start"], "linked_entities": linked,
                "missing_context": sorted(_MEETING_CONTEXT_TYPES - linked_types),
                "reason": "Meeting/class starts within 48 hours.",
            })
        if kind in {"meeting", "class", "work"} and now - timedelta(hours=48) <= row["end_value"] < now:
            tasks = [item for item in linked if item["entity_type"] == "task"]
            if not tasks:
                follow_up.append({
                    "event_id": row["occurrence_id"], "summary": row["summary"],
                    "ended_at": row["end"],
                    "reason": "Recent meeting/class has no linked follow-up task.",
                    "proposed_action": "prepare_follow_up_task",
                })
        active_tasks = [
            item for item in linked
            if item["entity_type"] == "task" and item["status"] not in {"completed", "done", "cancelled"}
        ]
        if kind in {"focus", "work"} and row["end_value"] < now and active_tasks:
            unfinished.append({
                "event_id": row["occurrence_id"], "summary": row["summary"],
                "task_ids": [item["id"] for item in active_tasks],
                "reason": "Past work block still links to unfinished task work.",
                "proposed_action": "prepare_reschedule",
            })
        if kind == "focus":
            overlapping = [
                conflict for conflict in conflicts
                if row["occurrence_id"] in conflict["event_ids"]
            ]
            if overlapping:
                focus_risks.append({
                    "event_id": row["occurrence_id"], "summary": row["summary"],
                    "conflict_count": len(overlapping),
                    "reason": "Focus block overlaps another timed commitment.",
                })
        if kind == "travel":
            previous = occurrences[index - 1] if index else None
            following = occurrences[index + 1] if index + 1 < len(occurrences) else None
            for label, neighbor, gap in (
                ("before", previous, row["start_value"] - previous["end_value"] if previous else None),
                ("after", following, following["start_value"] - row["end_value"] if following else None),
            ):
                if neighbor and gap is not None and timedelta(0) <= gap < timedelta(minutes=travel_buffer):
                    travel_risks.append({
                        "event_id": row["occurrence_id"],
                        "neighbor_event_id": neighbor["occurrence_id"],
                        "position": label,
                        "gap_minutes": round(gap.total_seconds() / 60),
                        "required_buffer_minutes": travel_buffer,
                        "reason": "Travel commitment lacks the requested transition buffer.",
                    })

    free_start = max(start, now)
    free = (
        _free_slots(
            occurrences, start=free_start, end=end, day_start_hour=day_start,
            day_end_hour=day_end, minimum_minutes=minimum,
        )
        if free_start < end else []
    )
    task_blocks, task_scheduling = _task_time_blocks(
        db,
        owner_id=owner_id,
        slots=free,
        offset=start.tzinfo,
        preferred_energy=energy,
    )
    public_events = [
        {key: value for key, value in row.items() if key not in {"start_value", "end_value"}}
        for row in occurrences
    ]
    return {
        "as_of": now.isoformat(), "window_start": start.isoformat(),
        "window_end": end.isoformat(), "events": public_events,
        "event_count": len(public_events), "free_slots": free,
        "conflicts": conflicts, "overcommitment": overload,
        "meeting_preparation": preparation, "meeting_follow_up": follow_up,
        "unfinished_work": unfinished, "focus_protection": focus_risks,
        "travel_buffers": travel_risks,
        "suggested_time_blocks": task_blocks or [
            {**slot, "reason": "Earliest available bounded focus slot."}
            for slot in free[:3]
        ],
        "task_time_blocks": task_blocks,
        "task_scheduling": task_scheduling,
        "assumptions": {
            "timezone_offset": start.strftime("%z"),
            "workday_hours": [day_start, day_end],
            "minimum_slot_minutes": minimum,
            "daily_capacity_minutes": capacity,
            "travel_buffer_minutes": travel_buffer,
            "preferred_energy": energy,
            "legacy_naive_events": "interpreted in the supplied fixed offset",
        },
        "read_only": True,
        "can_reschedule_or_create": False,
        "truncated": source_truncated or occurrence_truncated or len(free) >= 100,
    }
