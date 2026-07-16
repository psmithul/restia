"""Notification command-center API.

Aggregates everything that previously lived behind scattered red dots into
one feed the frontend bell dropdown renders:

- emails needing reply   (email urgency scanner state, score >= 2, unread)
- tasks due              (todo/checklist notes overdue or due in 24h)
- calendar reminders     (events starting in the next 24h)
- unread direct messages (grouped by sender)
- AI suggestions         (rule-based, each with a ready-to-send chat prompt)
- long-running jobs      (recent task-scheduler completions)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from src.auth_helpers import (
    legacy_owner_storage_key,
    owner_storage_key,
    require_user,
    resolved_request_owner,
)
from src.constants import DATA_DIR

logger = logging.getLogger(__name__)

_scheduler_ref = None


def _parse_due_local(value, zone=None):
    """Parse a note due_date (naive local / offset ISO / trailing Z) to a
    naive server-local datetime, or None."""
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except Exception:
        return None
    if zone is not None:
        d = d.replace(tzinfo=zone) if d.tzinfo is None else d.astimezone(zone)
    elif d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)
    return d


def _emails_needing_reply(owner: str) -> list[dict]:
    slug = owner_storage_key(owner)
    path = Path(DATA_DIR) / f"email_urgency_state_{slug}.json"
    legacy_path = Path(DATA_DIR) / f"email_urgency_state_{legacy_owner_storage_key(owner)}.json"
    if not path.exists() and legacy_path != path and legacy_path.exists():
        path = legacy_path
    if not path.exists():
        return []
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(state, dict):
        return []
    state_owner = state.get("owner")
    # Even a path that is primary for a legacy-safe username may also be an
    # old lossy path for another identity. Only the embedded exact owner can
    # prove attribution; ownerless historical payloads fail closed.
    if state_owner != (owner or ""):
        return []
    out = []
    for key, v in (state.get("per_uid") or {}).items():
        if not isinstance(v, dict):
            continue
        if v.get("score", 0) < 2 or not v.get("unread"):
            continue
        uid = str(key).split(":", 1)[-1]
        out.append({
            "uid": uid,
            "subject": (v.get("subject") or "(no subject)")[:160],
            "from": (v.get("from") or "")[:120],
            "score": v.get("score", 2),
            "reason": (v.get("reason") or "")[:160],
            "open_hash": f"#email=INBOX:{uid}",
        })
    out.sort(key=lambda e: -e["score"])
    return out[:10]


def _todos_due(owner: str) -> list[dict]:
    from core.database import Note, SessionLocal
    from src.auth_helpers import owner_filter
    from src.note_progression import completion_items_fully_done
    from src.notification_preferences import load_notification_preferences
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(str(load_notification_preferences(owner).get("timezone") or "UTC"))
    except Exception:
        zone = timezone.utc
    now = datetime.now(zone)
    horizon = now + timedelta(hours=24)
    db = SessionLocal()
    try:
        q = db.query(Note).filter(Note.archived == False)  # noqa: E712
        q = q.filter(Note.due_date.isnot(None), Note.due_date != "")
        if owner:
            q = owner_filter(q, Note, owner, include_shared=False)
        out = []
        for n in q.limit(300).all():
            if completion_items_fully_done(n.note_type, n.items):
                continue
            due = _parse_due_local(n.due_date, zone)
            if not due or due > horizon:
                continue
            out.append({
                "id": n.id,
                "title": (n.title or n.content or "Untitled")[:120],
                "due_date": n.due_date,
                "due_label": due.strftime("%a %H:%M"),
                "overdue": due < now,
                "repeat": (n.repeat or "none"),
                "note_type": n.note_type or "note",
                "open_hash": f"#open=notes&note={n.id}",
                "_sort_due": due.timestamp(),
            })
        out.sort(key=lambda t: t["_sort_due"])
        for item in out:
            item.pop("_sort_due", None)
        return out[:10]
    finally:
        db.close()


def _events_upcoming(owner: str) -> list[dict]:
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    from routes.calendar_routes import _expand_rrule
    from src.notification_preferences import load_notification_preferences
    from sqlalchemy import and_, or_
    from zoneinfo import ZoneInfo

    try:
        zone = ZoneInfo(str(load_notification_preferences(owner).get("timezone") or "UTC"))
    except Exception:
        zone = timezone.utc
    now = datetime.now(zone)
    horizon = now + timedelta(hours=24)
    local_start = now.replace(tzinfo=None)
    local_end = horizon.replace(tzinfo=None)
    utc_start = now.astimezone(timezone.utc).replace(tzinfo=None)
    utc_end = horizon.astimezone(timezone.utc).replace(tzinfo=None)
    broad_start = min(local_start, utc_start) - timedelta(days=1)
    broad_end = max(local_end, utc_end) + timedelta(days=1)
    db = SessionLocal()
    try:
        q = (
            db.query(CalendarEvent)
            .join(CalendarCal, CalendarEvent.calendar_id == CalendarCal.id)
            .filter(
                or_(
                    and_(CalendarEvent.rrule.isnot(None), CalendarEvent.rrule != ""),
                    and_(
                        CalendarEvent.dtstart >= broad_start,
                        CalendarEvent.dtstart <= broad_end,
                    ),
                ),
                CalendarEvent.status != "cancelled",
            )
        )
        if owner:
            q = q.filter(CalendarCal.owner == owner)
        out = []
        for ev in q.order_by(CalendarEvent.dtstart.desc()).limit(2500).all():
            range_start, range_end = (
                (utc_start, utc_end)
                if getattr(ev, "is_utc", False)
                else (local_start, local_end)
            )
            if ev.rrule and str(ev.rrule).strip():
                occurrences = _expand_rrule(
                    ev,
                    range_start,
                    range_end,
                    limit=16,
                    work_limit=1000,
                )
            else:
                occurrences = [{
                    "uid": ev.uid,
                    "dtstart": ev.dtstart.isoformat() if ev.dtstart else "",
                    "all_day": bool(ev.all_day),
                    "summary": ev.summary,
                }]
            for occurrence in occurrences:
                raw_start = str(occurrence.get("dtstart") or "")
                if not raw_start:
                    continue
                try:
                    parsed = datetime.fromisoformat(raw_start.replace("Z", "+00:00"))
                except ValueError:
                    continue
                start = (
                    parsed.replace(tzinfo=timezone.utc).astimezone(zone)
                    if getattr(ev, "is_utc", False) and parsed.tzinfo is None
                    else parsed.astimezone(zone)
                    if parsed.tzinfo is not None
                    else parsed.replace(tzinfo=zone)
                )
                if start < now or start > horizon:
                    continue
                all_day = bool(occurrence.get("all_day", ev.all_day))
                out.append({
                    "id": occurrence.get("uid") or ev.uid,
                    "summary": (occurrence.get("summary") or ev.summary or "Untitled event")[:120],
                    "start": start.isoformat(),
                    "start_label": "all day" if all_day else start.strftime("%a %H:%M"),
                    "all_day": all_day,
                })
        out.sort(key=lambda item: item["start"])
        return out[:8]
    finally:
        db.close()


def _messages_unread(owner: str) -> list[dict]:
    """Unread direct messages grouped by sender, newest conversation first."""
    if not owner:
        return []
    from core.database import DirectMessage, SessionLocal

    db = SessionLocal()
    try:
        rows = (
            db.query(DirectMessage)
            .filter(
                DirectMessage.recipient == owner,
                DirectMessage.read_at.is_(None),
            )
            .order_by(DirectMessage.created_at.desc())
            .limit(200)
            .all()
        )
        by_sender: dict[str, dict] = {}
        for msg in rows:
            item = by_sender.get(msg.sender)
            if item is None:
                item = {
                    "sender": msg.sender,
                    "preview": (msg.body or "")[:160],
                    "last_at": msg.created_at.isoformat() + "Z" if msg.created_at else None,
                    "unread": 0,
                }
                by_sender[msg.sender] = item
            item["unread"] += 1
        return list(by_sender.values())[:20]
    finally:
        db.close()


def _suggestions(emails: list, todos: list, events: list) -> list[dict]:
    """Rule-based suggestions, each with a ready-to-send agent prompt."""
    out = []
    overdue = [t for t in todos if t.get("overdue")]
    if overdue:
        names = ", ".join(t["title"][:40] for t in overdue[:3])
        out.append({
            "id": "reschedule-overdue",
            "text": f"{len(overdue)} to-do{'s' if len(overdue) != 1 else ''} overdue — want me to reschedule?",
            "prompt": f"Reschedule my overdue to-dos ({names}) to a sensible time later today.",
        })
    if emails:
        out.append({
            "id": "draft-replies",
            "text": f"{len(emails)} email{'s' if len(emails) != 1 else ''} likely need a reply — I can draft them.",
            "prompt": "Draft replies to my urgent unread emails and show them to me before sending anything.",
        })
    soon = [e for e in events if not e.get("all_day")][:1]
    if soon:
        out.append({
            "id": f"prep-{soon[0]['id']}",
            "text": f"“{soon[0]['summary']}” is coming up {soon[0]['start_label']} — need prep?",
            "prompt": f"Help me prepare for my upcoming event \"{soon[0]['summary']}\".",
        })
    return out[:4]


def setup_notification_center_routes(task_scheduler=None) -> APIRouter:
    global _scheduler_ref
    _scheduler_ref = task_scheduler
    router = APIRouter(prefix="/api/notifications", tags=["notifications"])

    @router.get("/center")
    async def notification_center(
        request: Request,
        admitted_user: str = Depends(require_user),
    ):
        owner = resolved_request_owner(request, admitted_user=admitted_user)
        emails, todos, events, messages, jobs = [], [], [], [], []
        try:
            emails = _emails_needing_reply(owner)
        except Exception:
            logger.debug("notification center: email section failed", exc_info=True)
        try:
            todos = _todos_due(owner)
        except Exception:
            logger.debug("notification center: todos section failed", exc_info=True)
        try:
            events = _events_upcoming(owner)
        except Exception:
            logger.debug("notification center: events section failed", exc_info=True)
        try:
            messages = _messages_unread(owner)
        except Exception:
            logger.debug("notification center: messages section failed", exc_info=True)
        try:
            if _scheduler_ref is not None and hasattr(_scheduler_ref, "recent_notifications"):
                jobs = _scheduler_ref.recent_notifications(owner=owner)[:8]
        except Exception:
            logger.debug("notification center: jobs section failed", exc_info=True)
        suggestions = _suggestions(emails, todos, events)
        unread_message_count = sum(int(m.get("unread") or 0) for m in messages)
        return {
            "emails": emails,
            "todos": todos,
            "events": events,
            "messages": messages,
            "suggestions": suggestions,
            "jobs": jobs,
            "count": len(emails) + len(todos) + len(events) + len(jobs) + unread_message_count,
            "generated_at": datetime.now().isoformat(),
        }

    return router
