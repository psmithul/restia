"""Calendar routes — local SQLite-backed calendar CRUD."""

import logging
import hashlib
import json
import re
import uuid
from datetime import datetime, date, timedelta
from typing import Optional, List

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Query, Depends
from pydantic import BaseModel, Field
from sqlalchemy import or_, and_
from dateutil.rrule import rrulestr

from core.database import SessionLocal, CalendarCal, CalendarEvent
from src.auth_helpers import DEFAULT_LOCAL_OWNER, require_user
from src.calendar_service import (
    CalendarConflict,
    CalendarNotFound,
    CalendarServiceError,
    cancel_calendar_event,
    create_local_calendar,
    create_calendar_event,
    delete_empty_calendar_collection,
    ensure_default_calendar,
    exclude_calendar_occurrence,
    reschedule_calendar_event,
    update_calendar_collection,
    update_calendar_event,
)
from src.calendar_intelligence import calendar_time_report
from src.life_graph import LifeGraphError
from src.identity import request_account_transaction
from src.upload_limits import read_upload_limited, ICS_MAX_BYTES

logger = logging.getLogger(__name__)


def _ics_naive_dtstart(dt):
    """Naive value matching how import_ics STORES CalendarEvent.dtstart.

    Timed tz-aware events are stored as UTC with tzinfo stripped, all-day
    dates as midnight datetimes, naive datetimes unchanged. The ICS dedup
    must compute the same value or a re-import never matches the stored row.
    """
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            from datetime import timezone as _tz
            return dt.astimezone(_tz.utc).replace(tzinfo=None)
        return dt
    if isinstance(dt, date):
        return datetime(dt.year, dt.month, dt.day)
    return dt


def _ensure_positive_duration(start_dt, end_dt, all_day):
    """Clamp an imported event's end so it has a positive duration.

    Some .ics exporters write a single-day all-day event with DTEND equal to
    DTSTART (treating DTEND as inclusive rather than the RFC 5545 exclusive
    bound). Stored verbatim that produces a zero-duration row, which the
    list_events overlap filter (dtstart < end AND dtend > start) silently
    drops — the event never appears on the calendar even though the web UI
    would otherwise show it. Normalize a non-positive end to the same default
    span used when DTEND is absent: one day for all-day events, one hour
    otherwise.
    """
    if end_dt <= start_dt:
        return start_dt + (timedelta(days=1) if all_day else timedelta(hours=1))
    return end_dt


# Single-user fallback identity. Used only when:
#   1. The app is configured for single-user (no auth middleware), AND
#   2. The request didn't resolve to an authenticated user.
# Override at deploy time via `RESTIA_FALLBACK_OWNER` env var. In a real
# multi-user install set `RESTIA_SINGLE_USER=0` so unauthenticated requests
# are rejected instead of silently writing to this address.
import os as _os


def _env_alias(new_name: str, old_name: str, default: str = "") -> str:
    return _os.environ.get(new_name) or _os.environ.get(old_name) or default


FALLBACK_OWNER = DEFAULT_LOCAL_OWNER
_SINGLE_USER_MODE = _env_alias("RESTIA_SINGLE_USER", "ODYSSEUS_SINGLE_USER", "1") != "0"


def _require_user(request: Request) -> str:
    """Return the authenticated user. Uses require_user so AUTH_ENABLED=false
    and single-user mode both work: require_user returns "" when auth is
    disabled or unconfigured, and only raises 401 when auth is configured but
    the caller is unauthenticated. Falls back to FALLBACK_OWNER for calendar
    writes so data isn't stored under an empty owner in single-user mode."""
    user = require_user(request)
    if user:
        return user
    # require_user returned "" — auth is off or unconfigured (single-user).
    # Use FALLBACK_OWNER so calendar rows have a stable owner for filtering.
    return FALLBACK_OWNER


def _get_or_404_calendar(db, cal_id: str, owner: str) -> CalendarCal:
    cal = db.query(CalendarCal).filter(CalendarCal.id == cal_id).first()
    if not cal:
        raise HTTPException(404, "Calendar not found")
    # Tighten the legacy null-owner gate (v2 review HIGH-12): if the
    # caller is authenticated AND the calendar's owner is null OR
    # belongs to a different user, treat it as not-found. The previous
    # rule (`if cal.owner and cal.owner != owner`) silently allowed any
    # authenticated user to read/edit any calendar with owner=None.
    if owner and (cal.owner is None or cal.owner != owner):
        raise HTTPException(404, "Calendar not found")
    return cal


def _get_or_404_event(db, uid: str, owner: str) -> CalendarEvent:
    ev = db.query(CalendarEvent).join(CalendarCal).filter(CalendarEvent.uid == uid).first()
    if not ev:
        raise HTTPException(404, "Event not found")
    cal = ev.calendar
    if owner and cal and (cal.owner is None or cal.owner != owner):
        raise HTTPException(404, "Event not found")
    return ev


def _ics_escape(text: str) -> str:
    """Escape a value for an iCalendar TEXT field (RFC 5545 §3.3.11).

    Backslash, semicolon and comma are structural in TEXT values and must be
    escaped, and newlines become a literal ``\\n``. Backslash is escaped first
    so the escapes we add aren't re-escaped.
    """
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _safe_ics_filename(name: str) -> str:
    """Return a conservative .ics filename safe for Content-Disposition."""
    stem = name if isinstance(name, str) else ""
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", stem).strip("._-")
    if not stem:
        stem = "calendar"
    return f"{stem[:128]}.ics"


def _resolve_base_uid(uid: str) -> str:
    """Extract the base series UID from a compound occurrence UID.

    Compound UIDs have the form ``{base_uid}::{date_suffix}``.
    For plain UIDs (no ``::``), returns the UID unchanged.
    """
    if not uid:
        raise ValueError("empty uid")
    idx = uid.find("::")
    if idx == -1:
        return uid       # plain UID — no suffix
    base = uid[:idx]
    if not base:
        raise ValueError("malformed compound UID: missing base before ::")
    return base


# ── Pydantic models ──

class EventCreate(BaseModel):
    summary: str
    dtstart: str  # ISO 8601
    dtend: Optional[str] = None
    all_day: bool = False
    description: str = ""
    location: str = ""
    calendar_href: Optional[str] = None  # calendar id
    rrule: Optional[str] = None
    color: Optional[str] = None  # per-event color override
    importance: str = Field(default="normal", max_length=16)
    event_type: Optional[str] = Field(default=None, max_length=32)
    linked_entity_ids: List[str] = Field(default_factory=list, max_length=100)
    idempotency_key: Optional[str] = None


class EventUpdate(BaseModel):
    version: int = Field(ge=1)
    summary: Optional[str] = None
    dtstart: Optional[str] = None
    dtend: Optional[str] = None
    all_day: Optional[bool] = None
    description: Optional[str] = None
    location: Optional[str] = None
    rrule: Optional[str] = None
    color: Optional[str] = None
    importance: Optional[str] = Field(default=None, max_length=16)
    event_type: Optional[str] = Field(default=None, max_length=32)
    linked_entity_ids: Optional[List[str]] = Field(default=None, max_length=100)


def _raise_calendar_service_error(exc: CalendarServiceError) -> None:
    if isinstance(exc, CalendarNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, CalendarConflict):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


# ── Helpers ──

# Per-request user time context. chat_routes sets this from browser timezone
# headers so natural-language times the LLM emits ("today at 9pm") are parsed
# in the user's timezone, not the server's clock. None = unknown, fall back to
# legacy server-local behavior.
from src.user_time import (
    get_user_tz_name,
    get_user_tz_offset,
    now_user_local,
    set_user_tz_name,
    set_user_tz_offset,
    user_timezone,
)


def parse_due_for_user(s: str) -> str:
    """Parse a due-date string emitted by the LLM / agent in the USER's tz.

    Returns an ISO 8601 string with explicit offset (e.g. "2026-05-13T21:00:00+09:00")
    so downstream consumers preserve the absolute moment. Falls back to the
    legacy naive ISO when no user offset is set.

    Handles three input shapes:
      - Tz-aware ISO ("...Z" or "...+09:00") → returned as ISO with offset.
      - Naive ISO ("2026-05-13T21:00:00") → attach the user's offset.
      - Natural-language ("today at 9pm", "tomorrow 14:00", "in 2 hours") →
        evaluated against the user's local "now" instead of the server's,
        then ISO-with-offset.
    """
    from datetime import timezone as _tz, timedelta as _td
    offset = get_user_tz_offset()
    tz_name = get_user_tz_name()
    s = (s or "").strip()
    if not s:
        return s

    # Tz-aware ISO short-circuit — preserve as-is.
    try:
        _s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
        parsed = datetime.fromisoformat(_s2)
        if parsed.tzinfo is not None:
            return parsed.isoformat()
    except ValueError:
        parsed = None

    if offset is None and not tz_name:
        # No user tz known — preserve legacy behavior (naive server-local).
        return _parse_dt(s).isoformat()

    user_tz = user_timezone()

    # Naive ISO → tag with user tz.
    if parsed is not None and parsed.tzinfo is None:
        return parsed.replace(tzinfo=user_tz).isoformat()

    # Natural language — evaluate against user's "now".
    server_now_utc = datetime.now(_tz.utc)
    user_now = now_user_local(server_now_utc)
    # Patch datetime.now() inside _parse_dt by leveraging the user's clock:
    # we re-implement the small natural-language phrases here against user_now
    # so the result is naturally in the user's tz.
    import re as _re
    lower = s.lower().strip()

    def _parse_time(t):
        t = _re.sub(r'\b([ap])\s*\.?\s*m\.?\b', r'\1m', t.strip(), flags=_re.IGNORECASE)
        m = _re.match(r'^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$', t, _re.IGNORECASE)
        if not m: return None
        h = int(m.group(1)); mn = int(m.group(2) or 0); ampm = (m.group(3) or "").lower()
        if ampm == "pm" and h < 12: h += 12
        elif ampm == "am" and h == 12: h = 0
        if not (0 <= h < 24 and 0 <= mn < 60): return None
        return h, mn

    today = user_now.replace(hour=0, minute=0, second=0, microsecond=0)

    m = _re.match(r'^(today|tonight|tomorrow|tmrw|yesterday)(?:\s+at)?\s*(.*)$', lower)
    if m:
        word, rest = m.group(1), m.group(2).strip()
        base = today
        if word in ("tomorrow", "tmrw"): base = today + _td(days=1)
        elif word == "yesterday":         base = today - _td(days=1)
        if not rest:
            return base.isoformat()
        t = _parse_time(rest)
        if t is not None:
            return base.replace(hour=t[0], minute=t[1]).isoformat()

    # Time-first: "3pm today", "11pm today", "9am tomorrow"
    m = _re.match(r'^(.+?)\s+(today|tonight|tomorrow|tmrw|yesterday)$', lower)
    if m:
        time_part, word = m.group(1).strip(), m.group(2)
        base = today
        if word in ("tomorrow", "tmrw"): base = today + _td(days=1)
        elif word == "yesterday":        base = today - _td(days=1)
        t = _parse_time(time_part)
        if t is not None:
            return base.replace(hour=t[0], minute=t[1]).isoformat()

    m = _re.match(r'^in\s+(\d+)\s*(hour|hr|minute|min|day)s?\s*$', lower)
    if m:
        n = int(m.group(1)); unit = m.group(2)
        if unit in ("hour", "hr"):  return (user_now + _td(hours=n)).isoformat()
        if unit in ("minute", "min"): return (user_now + _td(minutes=n)).isoformat()
        if unit == "day":             return (user_now + _td(days=n)).isoformat()

    t = _parse_time(lower)
    if t is not None:
        return today.replace(hour=t[0], minute=t[1]).isoformat()

    # Last resort: dateutil. Trust it but apply user tz if it returned naive.
    try:
        from dateutil import parser as _du
        parsed2 = _du.parse(s)
        if parsed2.tzinfo is None:
            parsed2 = parsed2.replace(tzinfo=user_tz)
        return parsed2.isoformat()
    except Exception:
        # Final fallback: legacy parser, naive.
        return _parse_dt(s).isoformat()


def _parse_dt_pair(s: str):
    """Parse a date/datetime string and return ``(datetime, is_utc)``.

    is_utc is True iff the input carried explicit timezone info (Z, +HH:MM,
    -HH:MM); the returned datetime is naive UTC. Otherwise the datetime is
    naive-local (legacy behavior). DB column is naive — callers that care
    about tz semantics should set ``CalendarEvent.is_utc`` accordingly.
    """
    from datetime import timezone as _tz
    s = (s or "").strip()
    if not s:
        raise ValueError("empty datetime string")
    try:
        if len(s) == 10:
            return datetime.fromisoformat(s), False
        _s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
        parsed = datetime.fromisoformat(_s2)
        if parsed.tzinfo is not None:
            return parsed.astimezone(_tz.utc).replace(tzinfo=None), True
        return parsed, False
    except ValueError:
        return _parse_dt(s), False


def _parse_dt(s: str) -> datetime:
    """Parse a date/datetime string.

    Strict ISO first (cheapest path; this is what most callers pass). On
    failure, fall through a small natural-language parser that handles the
    phrasings LLMs commonly emit when given prompts like "1pm tomorrow":
      - today/tomorrow/yesterday [at] HH(:MM)? (am/pm)?
      - next <weekday> [at] HH(:MM)? (am/pm)?
      - in N hour(s)/minute(s)/day(s)
      - bare time today: "1pm", "13:00"
      - YYYY-MM-DD optionally followed by time
    Anything still unparsed falls to dateutil.parser, which handles most
    other absolute formats. Local-naive datetimes returned to match the
    DB schema (CalendarEvent.dtstart is naive).
    """
    import re as _re
    s = (s or "").strip()
    if not s:
        raise ValueError("empty datetime string")
    # Fast path: strict ISO
    try:
        if len(s) == 10:
            return datetime.fromisoformat(s)
        _s2 = s.replace("Z", "+00:00") if s.endswith("Z") else s
        parsed = datetime.fromisoformat(_s2)
        # Strip tz for the legacy callers — they expect naive. Real tz
        # handling lives in _parse_dt_pair.
        if parsed.tzinfo is not None:
            from datetime import timezone as _tz
            return parsed.astimezone(_tz.utc).replace(tzinfo=None)
        return parsed
    except ValueError:
        pass

    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    lower = s.lower().strip()

    def _parse_time(t: str):
        """Return (hour, minute) from '1pm', '1:30 PM', '13:00', etc., or None."""
        t = _re.sub(r'\b([ap])\s*\.?\s*m\.?\b', r'\1m', t.strip(), flags=_re.IGNORECASE)
        m = _re.match(r'^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$', t, _re.IGNORECASE)
        if not m:
            return None
        h = int(m.group(1))
        mn = int(m.group(2) or 0)
        ampm = (m.group(3) or "").lower()
        if ampm == "pm" and h < 12:
            h += 12
        elif ampm == "am" and h == 12:
            h = 0
        if not (0 <= h < 24 and 0 <= mn < 60):
            return None
        return h, mn

    # today/tonight/tomorrow/yesterday [at] TIME
    m = _re.match(r'^(today|tonight|tomorrow|tmrw|yesterday)(?:\s+at)?\s*(.*)$', lower)
    if m:
        word, rest = m.group(1), m.group(2).strip()
        base = today
        if word in ("tomorrow", "tmrw"):
            base = today + timedelta(days=1)
        elif word == "yesterday":
            base = today - timedelta(days=1)
        if not rest:
            return base
        t = _parse_time(rest)
        if t is not None:
            return base.replace(hour=t[0], minute=t[1])

    # time-first: "3pm today", "9am tomorrow", "11pm tonight"
    # (parity with parse_due_for_user, which handles these via the same form)
    m = _re.match(r'^(.+?)\s+(today|tonight|tomorrow|tmrw|yesterday)$', lower)
    if m:
        time_part, word = m.group(1).strip(), m.group(2)
        base = today
        if word in ("tomorrow", "tmrw"):
            base = today + timedelta(days=1)
        elif word == "yesterday":
            base = today - timedelta(days=1)
        t = _parse_time(time_part)
        if t is not None:
            return base.replace(hour=t[0], minute=t[1])

    # next <weekday> [at] TIME
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    m = _re.match(r'^next\s+(\w+)(?:\s+at)?\s*(.*)$', lower)
    if m and m.group(1) in weekdays:
        target_dow = weekdays.index(m.group(1))
        days = (target_dow - today.weekday()) % 7 or 7
        base = today + timedelta(days=days)
        rest = m.group(2).strip()
        if not rest:
            return base
        t = _parse_time(rest)
        if t is not None:
            return base.replace(hour=t[0], minute=t[1])

    # in N hours/minutes/days
    m = _re.match(r'^in\s+(\d+)\s*(hour|hr|minute|min|day)s?\s*$', lower)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit in ("hour", "hr"):
            return now + timedelta(hours=n)
        if unit in ("minute", "min"):
            return now + timedelta(minutes=n)
        if unit == "day":
            return now + timedelta(days=n)

    # Bare time → today at that time
    t = _parse_time(lower)
    if t is not None:
        return today.replace(hour=t[0], minute=t[1])

    # Last resort: dateutil's fuzzy parser
    try:
        from dateutil import parser as _du
        parsed = _du.parse(s)
        # Strip tz like every other return path above — this function's
        # contract is naive datetimes (CalendarEvent.dtstart is naive). An
        # offset-bearing non-ISO input (e.g. RFC-2822 "Mon, 05 Jan 2026
        # 14:00:00 +0900") otherwise leaked tz-aware into the naive column and
        # crashed read-back comparisons in _expand_rrule with "can't compare
        # offset-naive and offset-aware datetimes".
        if parsed.tzinfo is not None:
            from datetime import timezone as _tz
            return parsed.astimezone(_tz.utc).replace(tzinfo=None)
        return parsed
    except Exception:
        raise ValueError(f"could not parse datetime: {s!r}")


def _event_to_dict(ev: CalendarEvent) -> dict:
    """Convert a CalendarEvent model to the API dict format.

    Timed events whose stored datetimes represent UTC (is_utc=True) are
    serialized with a trailing `Z` so the frontend `new Date()` interprets
    them as absolute UTC and renders in the user's current local time. Legacy
    rows without the flag are emitted as naive ISO (read as local) to avoid
    silently shifting existing events.
    """
    if ev.all_day:
        start_str = ev.dtstart.strftime("%Y-%m-%d")
        end_str = ev.dtend.strftime("%Y-%m-%d")
    else:
        suffix = "Z" if getattr(ev, "is_utc", False) else ""
        start_str = ev.dtstart.isoformat() + suffix
        end_str = ev.dtend.isoformat() + suffix
    return {
        "uid": ev.uid,
        "summary": ev.summary or "",
        "dtstart": start_str,
        "dtend": end_str,
        "all_day": ev.all_day,
        "is_utc": bool(getattr(ev, "is_utc", False)),
        "description": ev.description or "",
        "location": ev.location or "",
        "rrule": ev.rrule or "",
        "recurrence_exdates": _recurrence_exdates(ev),
        "calendar": ev.calendar.name if ev.calendar else "",
        "calendar_href": ev.calendar_id,
        "color": ev.color or (ev.calendar.color if ev.calendar else ""),
        "event_type": getattr(ev, "event_type", None),
        "importance": getattr(ev, "importance", None) or "normal",
        "version": int(getattr(ev, "version", 1) or 1),
    }


# ── Recurrence expansion ──

_RRULE_EXPANSION_LIMIT = 1000
_RRULE_EXPANSION_WORK_LIMIT = 10000
_RRULE_TEXT_LIMIT = 4096
_CALENDAR_LIST_CANDIDATE_LIMIT = 2500
_CALENDAR_LIST_RECURRENCE_WORK_LIMIT = 100000
_CALENDAR_LIST_OUTPUT_LIMIT = 2000

_RRULE_FIXED_FREQUENCY_SECONDS = {
    "SECONDLY": 1,
    "MINUTELY": 60,
    "HOURLY": 60 * 60,
    "DAILY": 24 * 60 * 60,
    "WEEKLY": 7 * 24 * 60 * 60,
}

_RRULE_EXPANDING_PARTS = {
    "YEARLY": (
        "BYMONTH", "BYYEARDAY", "BYWEEKNO", "BYMONTHDAY", "BYDAY",
        "BYHOUR", "BYMINUTE", "BYSECOND",
    ),
    "MONTHLY": ("BYMONTHDAY", "BYDAY", "BYHOUR", "BYMINUTE", "BYSECOND"),
    "WEEKLY": ("BYDAY", "BYHOUR", "BYMINUTE", "BYSECOND"),
    "DAILY": ("BYHOUR", "BYMINUTE", "BYSECOND"),
    "HOURLY": ("BYMINUTE", "BYSECOND"),
    "MINUTELY": ("BYSECOND",),
    "SECONDLY": (),
}


class _ExpandedOccurrences(list):
    """List-compatible recurrence result with truncation metadata.

    The attribute matters when every examined occurrence was excluded: there
    are no dictionaries on which to carry the historical ``truncated`` flag.
    """

    def __init__(self, values=(), *, truncated: bool = False):
        super().__init__(values)
        self.truncated = bool(truncated)


class _RecurrenceBudget:
    """One hard work/output budget shared by every series in a request."""

    def __init__(self, *, work_limit: int, output_limit: int):
        self.remaining_work = max(0, int(work_limit))
        self.remaining_output = max(0, int(output_limit))
        self.exhausted = False

    def consume_work(self, amount: int = 1) -> bool:
        amount = max(0, int(amount))
        if amount > self.remaining_work:
            self.remaining_work = 0
            self.exhausted = True
            return False
        self.remaining_work -= amount
        return True

    def consume_output(self) -> bool:
        if self.remaining_output < 1:
            self.exhausted = True
            return False
        self.remaining_output -= 1
        return True


def _rrule_parameters(raw: str) -> dict[str, str]:
    """Parse the single RRULE clause used by CalendarEvent rows."""

    text_value = str(raw or "").strip()
    if not text_value or len(text_value) > _RRULE_TEXT_LIMIT:
        return {}
    lines = [line.strip() for line in text_value.splitlines() if line.strip()]
    if len(lines) != 1:
        return {}
    clause = lines[0]
    if clause.upper().startswith("RRULE:"):
        clause = clause.split(":", 1)[1]
    parameters: dict[str, str] = {}
    for token in clause.split(";"):
        key, separator, value = token.partition("=")
        if separator and key.strip() and value.strip():
            parameters[key.strip().upper()] = value.strip()
    return parameters


def _positive_rrule_int(value: Optional[str], default: int) -> int:
    try:
        parsed = int(str(value or ""))
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _rrule_seek_work_estimate(
    ev: CalendarEvent,
    expand_start: datetime,
    parameters: dict[str, str],
    *,
    dtstart: Optional[datetime] = None,
) -> int:
    """Conservatively estimate occurrences dateutil would seek past.

    ``rrule.xafter`` advances from DTSTART internally before yielding its first
    value. This estimate lets callers rebase or reject pathological historical
    rules before entering that unobservable loop.
    """

    frequency = parameters.get("FREQ", "").upper()
    if frequency not in _RRULE_EXPANDING_PARTS:
        return _RRULE_EXPANSION_WORK_LIMIT + 1
    rule_start = ev.dtstart if dtstart is None else dtstart
    try:
        if rule_start >= expand_start:
            return 0
    except TypeError:
        return _RRULE_EXPANSION_WORK_LIMIT + 1

    interval = _positive_rrule_int(parameters.get("INTERVAL"), 1)
    if frequency in _RRULE_FIXED_FREQUENCY_SECONDS:
        try:
            seconds = max(0.0, (expand_start - rule_start).total_seconds())
        except (TypeError, OverflowError):
            return _RRULE_EXPANSION_WORK_LIMIT + 1
        period_seconds = _RRULE_FIXED_FREQUENCY_SECONDS[frequency] * interval
        base_periods = int(seconds // period_seconds) + 1
    elif frequency == "MONTHLY":
        months = (
            (expand_start.year - rule_start.year) * 12
            + expand_start.month
            - rule_start.month
        )
        base_periods = max(1, months // interval + 1)
    else:  # YEARLY
        years = max(0, expand_start.year - rule_start.year)
        base_periods = max(1, years // interval + 1)

    expansion_factor = 1
    for name in _RRULE_EXPANDING_PARTS[frequency]:
        raw_values = parameters.get(name)
        if not raw_values:
            continue
        value_count = sum(1 for value in raw_values.split(",") if value.strip())
        expansion_factor *= max(1, value_count)
        if expansion_factor > _RRULE_EXPANSION_WORK_LIMIT:
            expansion_factor = _RRULE_EXPANSION_WORK_LIMIT + 1
            break

    estimate = base_periods * expansion_factor
    if "COUNT" in parameters:
        count = _positive_rrule_int(parameters.get("COUNT"), estimate)
        estimate = min(estimate, count)
    return estimate


def _rrule_rebased_dtstart(
    ev: CalendarEvent,
    expand_start: datetime,
    parameters: dict[str, str],
) -> Optional[datetime]:
    """Fast-forward an infinite fixed-frequency rule without changing phase."""

    if "COUNT" in parameters:
        # COUNT is relative to the original DTSTART, so rebasing would reset it.
        return None
    frequency = parameters.get("FREQ", "").upper()
    unit_seconds = _RRULE_FIXED_FREQUENCY_SECONDS.get(frequency)
    if unit_seconds is None:
        # Month/year arithmetic can clamp dates (Jan 31 -> Feb 28), changing
        # recurrence semantics. Reject those rare huge seeks instead.
        return None
    interval = _positive_rrule_int(parameters.get("INTERVAL"), 1)
    period_seconds = unit_seconds * interval
    try:
        elapsed = (expand_start - ev.dtstart).total_seconds()
    except (TypeError, OverflowError):
        return None
    if elapsed <= period_seconds:
        return ev.dtstart
    # Leave one full base period before the expansion window so overlap and
    # BY* rules retain context while historical iteration stays constant-sized.
    periods = max(0, int(elapsed // period_seconds) - 1)
    try:
        return ev.dtstart + timedelta(seconds=periods * period_seconds)
    except (OverflowError, ValueError):
        return None


def _recurrence_exdates(ev: CalendarEvent) -> list[str]:
    raw = getattr(ev, "recurrence_exdates", "") or ""
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except Exception:
        return []
    if not isinstance(values, list):
        return []
    return [str(v) for v in values if isinstance(v, str) and v.strip()]


def _occurrence_exdate_key(uid: str, ev: CalendarEvent) -> str:
    if "::" not in uid:
        return ""
    suffix = uid.split("::", 1)[1]
    if ev.all_day:
        return suffix[:10]
    return suffix[:16]


def _expand_rrule(
    ev: CalendarEvent,
    start: datetime,
    end: datetime,
    *,
    limit: int = _RRULE_EXPANSION_LIMIT,
    work_limit: Optional[int] = None,
    budget: Optional[_RecurrenceBudget] = None,
) -> List[dict]:
    """Expand a single recurring CalendarEvent into occurrence dicts.

    Each occurrence gets a stable compound UID of the form
    ``{base_uid}::{date_or_datetime}`` so the frontend can tell
    occurrences apart while the series UID is still recoverable
    for edit/delete targeting.

    Non-recurring events (empty rrule) are returned as a single-item
    list — the caller doesn't need to branch.
    """
    duration = ev.dtend - ev.dtstart
    expansion_limit = max(1, min(int(limit), _RRULE_EXPANSION_LIMIT))
    requested_work_limit = (
        expansion_limit * 10 if work_limit is None else int(work_limit)
    )
    occurrence_work_limit = max(
        expansion_limit,
        min(requested_work_limit, _RRULE_EXPANSION_WORK_LIMIT),
    )
    occurrence_seek_limit = occurrence_work_limit

    if not ev.rrule or not ev.rrule.strip():
        # Non-recurring — return the base event as-is. list_events
        # already filters non-recurring rows with the overlap check
        # in SQL, so we don't re-check here.
        d = _event_to_dict(ev)
        d["is_recurrence"] = False
        d["series_uid"] = ev.uid
        d["truncated"] = False
        return _ExpandedOccurrences([d])

    # Parse the rrule, applying it to the base dtstart.
    rrule_str = ev.rrule
    if len(str(rrule_str or "")) > _RRULE_TEXT_LIMIT:
        return _ExpandedOccurrences(truncated=True)
    if ev.dtstart is not None and getattr(ev.dtstart, "tzinfo", None) is None:
        # Events are stored with a naive (UTC) dtstart, but standard .ics
        # exporters (Google/Apple/Outlook/Fastmail) write the bound as an
        # absolute UTC value, e.g. UNTIL=20240105T090000Z. dateutil refuses to
        # mix a tz-aware UNTIL with a naive DTSTART ("RRULE UNTIL values must be
        # specified in UTC when DTSTART is timezone-aware"), so the except branch
        # below would silently collapse the whole series to a single event.
        # Drop the trailing Z so UNTIL matches the naive DTSTART.
        import re as _re
        rrule_str = _re.sub(
            r"(UNTIL=\d{8}(?:T\d{6})?)Z", r"\1", rrule_str, flags=_re.IGNORECASE
        )
    if "\n" in rrule_str or "\r" in rrule_str:
        return _ExpandedOccurrences(truncated=True)
    expand_start = start - duration
    parameters = _rrule_parameters(rrule_str)
    rule_dtstart = ev.dtstart
    # A short malformed rule is cheap to parse and retains the historical
    # base-event fallback below. Valid rules get the bounded seek preflight.
    if parameters.get("FREQ", "").upper() in _RRULE_EXPANDING_PARTS:
        seek_estimate = _rrule_seek_work_estimate(ev, expand_start, parameters)
        if seek_estimate > occurrence_seek_limit:
            rule_dtstart = _rrule_rebased_dtstart(ev, expand_start, parameters)
            if rule_dtstart is None:
                return _ExpandedOccurrences(truncated=True)
        charged_seek = _rrule_seek_work_estimate(
            ev,
            expand_start,
            parameters,
            dtstart=rule_dtstart,
        )
        if budget is not None and not budget.consume_work(charged_seek):
            return _ExpandedOccurrences(truncated=True)
    try:
        rule = rrulestr(rrule_str, dtstart=rule_dtstart)
    except Exception as ex:
        logger.warning(
            "Failed to parse rrule=%r for event %s: %s", ev.rrule, ev.uid, ex
        )
        d = _event_to_dict(ev)
        d["is_recurrence"] = False
        d["series_uid"] = ev.uid
        d["truncated"] = False
        # Malformed RRULE rows are fetched by the recurring SQL branch
        # with only dtstart < end_dt — the base event may not actually
        # overlap the window. Only return if it does.
        if ev.dtstart < end and ev.dtend > start:
            if budget is not None and not budget.consume_output():
                return _ExpandedOccurrences(truncated=True)
            return _ExpandedOccurrences([d])
        return _ExpandedOccurrences()

    # Expand from start - duration so multi-day / overnight occurrences
    # that start before the window but end inside it are captured
    # (matching non-recurring overlap semantics: dtstart < end AND
    # dtend > start).
    results = _ExpandedOccurrences()
    truncated = False
    examined = 0
    base = _event_to_dict(ev)
    exdates = set(_recurrence_exdates(ev))

    occurrence_iterator = iter(rule.xafter(expand_start, inc=True))
    while True:
        if examined >= occurrence_work_limit:
            truncated = True
            break
        if budget is not None and not budget.consume_work():
            truncated = True
            break
        try:
            occ_start = next(occurrence_iterator)
        except StopIteration:
            break
        examined += 1

        if occ_start >= end:
            break

        occ_end = occ_start + duration

        # Overlap filter: occurrence must intersect [start, end).
        # This enforces exclusive-end semantics (occ_start >= end is
        # excluded) and includes multi-day crossings (occ_end > start).
        if occ_end <= start:
            continue

        if len(results) >= expansion_limit:
            truncated = True
            break

        # Build the compound uid: {base_uid}::{date} or ::{datetime}
        if ev.all_day:
            occ_uid = f"{ev.uid}::{occ_start.strftime('%Y-%m-%d')}"
            exdate_key = occ_start.strftime("%Y-%m-%d")
        else:
            occ_uid = f"{ev.uid}::{occ_start.strftime('%Y-%m-%dT%H:%M')}"
            exdate_key = occ_start.strftime("%Y-%m-%dT%H:%M")

        if exdate_key in exdates:
            continue

        if budget is not None and not budget.consume_output():
            truncated = True
            break

        d = dict(base)
        d["uid"] = occ_uid
        d["series_uid"] = ev.uid
        d["is_recurrence"] = True
        d["truncated"] = False

        if ev.all_day:
            d["dtstart"] = occ_start.strftime("%Y-%m-%d")
            d["dtend"] = occ_end.strftime("%Y-%m-%d")
        else:
            suffix = "Z" if getattr(ev, "is_utc", False) else ""
            d["dtstart"] = occ_start.isoformat() + suffix
            d["dtend"] = occ_end.isoformat() + suffix
            d["is_utc"] = bool(getattr(ev, "is_utc", False))

        results.append(d)

    if truncated:
        for d in results:
            d["truncated"] = True
    results.truncated = truncated

    return results


def _list_events_for_owner(
    db,
    *,
    owner_id: str,
    start_dt: datetime,
    end_dt: datetime,
    calendar: str = "",
) -> dict:
    """Build the bounded calendar read model for one immutable Account.id."""

    def scoped_events():
        query = db.query(CalendarEvent).join(CalendarCal).filter(
            CalendarEvent.status != "cancelled",
            CalendarEvent.owner_id == owner_id,
            CalendarCal.owner_id == owner_id,
        )
        if calendar:
            query = query.filter(
                (CalendarEvent.calendar_id == calendar)
                | (CalendarCal.name == calendar)
            )
        return query

    direct_events = (
        scoped_events()
        .filter(
            or_(CalendarEvent.rrule == "", CalendarEvent.rrule.is_(None)),
            CalendarEvent.dtstart < end_dt,
            CalendarEvent.dtend > start_dt,
        )
        .order_by(CalendarEvent.dtstart.asc(), CalendarEvent.uid.asc())
        .limit(_CALENDAR_LIST_OUTPUT_LIMIT + 1)
        .all()
    )
    direct_overflow = len(direct_events) > _CALENDAR_LIST_OUTPUT_LIMIT
    direct_events = [
        event
        for event in direct_events[:_CALENDAR_LIST_OUTPUT_LIMIT]
        if not (event.rrule and event.rrule.strip())
    ]

    recurring_events = (
        scoped_events()
        .filter(
            CalendarEvent.rrule.isnot(None),
            CalendarEvent.rrule != "",
            CalendarEvent.dtstart < end_dt,
        )
        .order_by(CalendarEvent.dtstart.desc(), CalendarEvent.uid.asc())
        .limit(_CALENDAR_LIST_CANDIDATE_LIMIT + 1)
        .all()
    )
    recurring_overflow = len(recurring_events) > _CALENDAR_LIST_CANDIDATE_LIMIT
    recurring_events = [
        event
        for event in recurring_events[:_CALENDAR_LIST_CANDIDATE_LIMIT]
        if event.rrule and event.rrule.strip()
    ]

    expanded = []
    for event in direct_events:
        expanded.extend(_expand_rrule(event, start_dt, end_dt))

    truncated = direct_overflow or recurring_overflow
    budget = _RecurrenceBudget(
        work_limit=_CALENDAR_LIST_RECURRENCE_WORK_LIMIT,
        output_limit=max(0, _CALENDAR_LIST_OUTPUT_LIMIT - len(direct_events)),
    )
    for event in recurring_events:
        if budget.remaining_work < 1 or budget.remaining_output < 1:
            truncated = True
            break
        occurrences = _expand_rrule(
            event,
            start_dt,
            end_dt,
            limit=max(1, min(_RRULE_EXPANSION_LIMIT, budget.remaining_output)),
            work_limit=max(
                1,
                min(_RRULE_EXPANSION_WORK_LIMIT, budget.remaining_work),
            ),
            budget=budget,
        )
        truncated = truncated or bool(getattr(occurrences, "truncated", False))
        expanded.extend(occurrences)
        if budget.exhausted:
            truncated = True
            break

    truncated = truncated or any(event.get("truncated") for event in expanded)
    expanded.sort(key=lambda value: value["dtstart"])
    if len(expanded) > _CALENDAR_LIST_OUTPUT_LIMIT:
        truncated = True
        expanded = expanded[:_CALENDAR_LIST_OUTPUT_LIMIT]
    response: dict = {"events": expanded}
    if truncated:
        response["truncated"] = True
    return response


# ── Routes ──

def setup_calendar_routes() -> APIRouter:
    router = APIRouter(prefix="/api/calendar", tags=["calendar"])

    # ── CalDAV multi-account helpers ─────────────────────────────────────────

    def _get_caldav_accounts(owner: str) -> list:
        from src.caldav_sync import _load_caldav_accounts
        return _load_caldav_accounts(owner)

    def _caldav_delivery_marker(account: dict) -> str:
        """Hash only connector fields that can change remote delivery authority."""

        material = {
            "id": str(account.get("id") or ""),
            "url": str(account.get("url") or "").strip(),
            "username": str(account.get("username") or "").strip(),
            "password": str(account.get("password") or ""),
            "oauth_provider": str(account.get("oauth_provider") or ""),
            "oauth_refresh_token": str(
                account.get("oauth_refresh_token") or ""
            ),
        }
        encoded = json.dumps(
            material, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _fence_changed_caldav_accounts(
        owner: str,
        before: list,
        after: list,
    ) -> None:
        """Advance collection generations before connector preferences change.

        CalendarDelivery rows capture ``CalendarCal.config_version``.  A
        credential, principal, or endpoint change must therefore advance every
        bound collection before the new preference file becomes visible to a
        worker.  If the later file write fails the extra generation is safe:
        queued work conflicts instead of running against uncertain authority.
        """

        from src.identity import find_account
        from src.life_core import append_action_audit

        def markers(rows: list) -> dict[str, str]:
            values: dict[str, str] = {}
            for raw in rows:
                if not isinstance(raw, dict):
                    raise HTTPException(400, "CalDAV account configuration is invalid")
                account_id = str(raw.get("id") or "").strip()
                if not account_id or account_id in values:
                    raise HTTPException(400, "CalDAV account IDs must be unique")
                values[account_id] = _caldav_delivery_marker(raw)
            return values

        old = markers(before)
        new = markers(after)
        changed_ids = {
            account_id
            for account_id in set(old) | set(new)
            if old.get(account_id) != new.get(account_id)
        }
        if not changed_ids:
            return

        db = SessionLocal()
        try:
            account = find_account(db, owner)
            if account is None:
                raise HTTPException(401, "Authenticated calendar account not found")
            rows = db.query(CalendarCal).filter(
                CalendarCal.owner_id == account.id,
                CalendarCal.source == "caldav",
            ).all()
            # A legacy collection without an account_id selects the only saved
            # connector. Any binding-set change can change that selection, so
            # it is always fenced when at least one delivery-sensitive account
            # changes.
            for calendar in rows:
                connector_id = str(calendar.account_id or "")
                if connector_id and connector_id not in changed_ids:
                    continue
                expected = int(calendar.config_version or 1)
                updated = db.query(CalendarCal).filter(
                    CalendarCal.id == calendar.id,
                    CalendarCal.owner_id == account.id,
                    CalendarCal.config_version == expected,
                ).update(
                    {
                        CalendarCal.config_version: expected + 1,
                        CalendarCal.updated_at: datetime.utcnow(),
                    },
                    synchronize_session=False,
                )
                if updated != 1:
                    raise CalendarConflict(
                        "CalDAV configuration changed in another client"
                    )
                append_action_audit(
                    db,
                    owner_id=account.id,
                    action="calendar.caldav.configuration_changed",
                    entity_type="calendar",
                    entity_id=calendar.id,
                    reason="CalDAV delivery authority changed",
                    before_state={"config_version": expected},
                    after_state={"config_version": expected + 1},
                    details={"connector_binding_changed": True},
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _save_caldav_accounts(owner: str, accounts: list) -> None:
        from routes.prefs_routes import _load_for_user, _save_for_user
        prefs = _load_for_user(owner) or {}
        previous = list(prefs.get("caldav_accounts") or [])
        _fence_changed_caldav_accounts(owner, previous, accounts)
        prefs["caldav_accounts"] = accounts
        prefs.pop("caldav", None)
        _save_for_user(owner, prefs)

    # ── Google OAuth Routes ──────────────────────────────────────────────────

    @router.get("/oauth/google/authorize")
    async def google_oauth_authorize(account_id: str = Query(...), request: Request = None, owner: str = Depends(_require_user)):
        import urllib.parse
        import os
        from routes.email_helpers import make_oauth_state
        client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
        if not client_id:
            raise HTTPException(400, "GOOGLE_OAUTH_CLIENT_ID not set — add it to .env")
        redirect_uri = (
            os.environ.get("GOOGLE_OAUTH_REDIRECT_URI_CALENDAR")
            or f"http://{request.headers.get('host', 'localhost:7000')}/api/calendar/oauth/google/callback"
        )
        state = make_oauth_state(account_id, owner)
        params = urllib.parse.urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/calendar email",
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        })
        from fastapi.responses import RedirectResponse as _RR
        return _RR(f"https://accounts.google.com/o/oauth2/v2/auth?{params}")

    @router.get("/oauth/google/callback")
    async def google_oauth_callback(
        code: str = Query(None),
        state: str = Query(None),
        error: str = Query(None),
        request: Request = None,
    ):
        import time
        import os
        from fastapi.responses import RedirectResponse as _RR
        from routes.email_helpers import verify_oauth_state

        if error:
            return _RR("/?section=integrations&calendar_oauth_error=google_error")
        if not code or not state:
            return _RR("/?section=integrations&calendar_oauth_error=missing_code")
        state_data = verify_oauth_state(state)
        if not state_data:
            return _RR("/?section=integrations&calendar_oauth_error=invalid_state")

        account_id = state_data.get("a", "")
        owner = state_data.get("o", "")
        client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
        client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
        redirect_uri = (
            os.environ.get("GOOGLE_OAUTH_REDIRECT_URI_CALENDAR")
            or f"http://{request.headers.get('host', 'localhost:7000')}/api/calendar/oauth/google/callback"
        )
        import httpx as _httpx
        try:
            resp = _httpx.post("https://oauth2.googleapis.com/token", data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            }, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            logger.warning("Google Calendar token exchange failed")
            return _RR("/?section=integrations&calendar_oauth_error=token_exchange_failed")

        access_token = data.get("access_token", "")
        refresh_token = data.get("refresh_token", "")
        expiry = str(int(time.time()) + data.get("expires_in", 3600))

        email_addr = ""
        try:
            ui = _httpx.get("https://www.googleapis.com/oauth2/v1/userinfo",
                            headers={"Authorization": f"Bearer {access_token}"}, timeout=10)
            if ui.is_success:
                email_addr = ui.json().get("email", "")
        except Exception:
            pass

        if not email_addr:
            return _RR("/?section=integrations&calendar_oauth_error=no_email")

        from src.secret_storage import encrypt as _enc
        accounts = _get_caldav_accounts(owner)

        # See if account exists, otherwise create it
        acc = next((a for a in accounts if a.get("id") == account_id), None)
        if not acc:
            acc = {"id": account_id}
            accounts.append(acc)

        acc["label"] = acc.get("label") or f"Google ({email_addr})"
        acc["url"] = f"https://apidata.googleusercontent.com/caldav/v2/{email_addr}/events"
        acc["username"] = email_addr
        acc["password"] = ""
        acc["oauth_provider"] = "google"
        acc["oauth_access_token"] = _enc(access_token)
        if refresh_token:
            acc["oauth_refresh_token"] = _enc(refresh_token)
        acc["oauth_token_expiry"] = expiry

        _save_caldav_accounts(owner, accounts)
        return _RR("/?section=integrations")

    # ── CalDAV config routes (backward-compat single-account API) ────────────

    @router.get("/config")
    async def get_config(request: Request):
        """Legacy single-account endpoint — returns the first configured account."""
        owner = _require_user(request)
        accounts = _get_caldav_accounts(owner)
        if not accounts:
            return {"url": "", "username": "", "password": "", "has_password": False, "local": True}
        first = accounts[0]
        pw = first.get("password") or ""
        has_pw = False
        if pw:
            try:
                from src.secret_storage import decrypt
                has_pw = bool(decrypt(pw))
            except Exception:
                has_pw = bool(pw)
        return {
            "url": first.get("url", "") or "",
            "username": first.get("username", "") or "",
            "password": "",
            "has_password": has_pw,
            "local": not bool(first.get("url")),
        }

    @router.post("/config")
    async def save_config(request: Request):
        """Legacy single-account endpoint — upserts the first account."""
        owner = _require_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        accounts = _get_caldav_accounts(owner)
        if not (body.get("url") or "").strip():
            _save_caldav_accounts(owner, [])
            return {"ok": True, "cleared": True}
        from src.caldav_sync import validate_caldav_url
        try:
            validated_url = validate_caldav_url(body.get("url", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if accounts:
            acc = dict(accounts[0])
        else:
            import uuid as _uuid
            acc = {"id": str(_uuid.uuid4()), "label": "CalDAV"}
        acc["url"] = validated_url
        acc["username"] = (body.get("username") or "").strip()
        if body.get("password"):
            from src.secret_storage import encrypt
            acc["password"] = encrypt(body["password"])
        new_accounts = [acc] + (accounts[1:] if len(accounts) > 1 else [])
        _save_caldav_accounts(owner, new_accounts)
        return {"ok": True}

    # ── CalDAV multi-account CRUD ─────────────────────────────────────────────

    @router.get("/config/accounts")
    async def list_caldav_accounts(request: Request):
        """Return all configured CalDAV accounts (passwords never returned)."""
        owner = _require_user(request)
        accounts = _get_caldav_accounts(owner)
        safe = []
        for acc in accounts:
            pw = acc.get("password") or ""
            has_pw = False
            if pw:
                try:
                    from src.secret_storage import decrypt
                    has_pw = bool(decrypt(pw))
                except Exception:
                    has_pw = bool(pw)
            safe.append({
                "id": acc.get("id", ""),
                "label": acc.get("label", "") or acc.get("url", ""),
                "url": acc.get("url", "") or "",
                "username": acc.get("username", "") or "",
                "has_password": has_pw,
                "oauth_provider": acc.get("oauth_provider", "") or "",
                "oauth_connected": bool(acc.get("oauth_provider") and acc.get("oauth_access_token")),
            })
        return {"accounts": safe}

    @router.post("/config/accounts")
    async def add_caldav_account(request: Request):
        """Add a new CalDAV account."""
        import uuid as _uuid
        owner = _require_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        from src.caldav_sync import validate_caldav_url
        try:
            url = validate_caldav_url(body.get("url", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        if not body.get("password"):
            raise HTTPException(400, "Password is required")
        from src.secret_storage import encrypt
        new_acc = {
            "id": str(_uuid.uuid4()),
            "label": (body.get("label") or "").strip() or "CalDAV",
            "url": url,
            "username": (body.get("username") or "").strip(),
            "password": encrypt(body["password"]),
        }
        accounts = _get_caldav_accounts(owner)
        accounts.append(new_acc)
        _save_caldav_accounts(owner, accounts)
        return {"ok": True, "id": new_acc["id"]}

    @router.put("/config/accounts/{account_id}")
    async def update_caldav_account(account_id: str, request: Request):
        """Update an existing CalDAV account by id."""
        owner = _require_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        accounts = _get_caldav_accounts(owner)
        idx = next((i for i, a in enumerate(accounts) if a.get("id") == account_id), None)
        if idx is None:
            raise HTTPException(404, "Account not found")
        acc = dict(accounts[idx])
        if body.get("url"):
            from src.caldav_sync import validate_caldav_url
            try:
                acc["url"] = validate_caldav_url(body["url"])
            except ValueError as e:
                raise HTTPException(400, str(e))
        if body.get("label") is not None:
            acc["label"] = (body.get("label") or "").strip() or "CalDAV"
        if body.get("username") is not None:
            acc["username"] = (body.get("username") or "").strip()
        if body.get("password"):
            from src.secret_storage import encrypt
            acc["password"] = encrypt(body["password"])
        accounts[idx] = acc
        _save_caldav_accounts(owner, accounts)
        return {"ok": True}

    @router.delete("/config/accounts/{account_id}")
    async def delete_caldav_account(account_id: str, request: Request):
        """Remove a CalDAV account by id."""
        owner = _require_user(request)
        accounts = _get_caldav_accounts(owner)
        new_accounts = [a for a in accounts if a.get("id") != account_id]
        if len(new_accounts) == len(accounts):
            raise HTTPException(404, "Account not found")
        _save_caldav_accounts(owner, new_accounts)
        return {"ok": True}

    @router.post("/test")
    async def test_connection(request: Request):
        """Probe a CalDAV server with a PROPFIND. Accepts an optional body:
        {url, username, password} to test before saving, or {account_id} to
        test an already-saved account. Falls back to the first saved account
        when nothing is provided."""
        owner = _require_user(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        url = (body.get("url") or "").strip()
        user = (body.get("username") or "").strip()
        pw = body.get("password") or ""
        access_token = ""
        saved_oauth_provider = ""
        if not (url and user and pw):
            # Look up a saved account: by id if supplied, else first account.
            accounts = _get_caldav_accounts(owner)
            acc = None
            if body.get("account_id"):
                acc = next((a for a in accounts if a.get("id") == body["account_id"]), None)
            if acc is None and accounts:
                acc = accounts[0]
            if acc:
                url = url or (acc.get("url") or "")
                user = user or (acc.get("username") or "")
                saved_oauth_provider = acc.get("oauth_provider") or ""
                if saved_oauth_provider == "google":
                    from src.caldav_sync import _ensure_google_calendar_token
                    access_token = _ensure_google_calendar_token(acc, owner) or ""
                if not pw:
                    pw = acc.get("password") or ""
                    if pw:
                        try:
                            from src.secret_storage import decrypt
                            pw = decrypt(pw)
                        except Exception:
                            pass
        if not (url and user and (pw or access_token)):
            return {"ok": False, "error": "Missing URL, username, or password/token"}
        from src.caldav_sync import validate_caldav_url
        try:
            url = validate_caldav_url(url)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        import httpx
        propfind_body = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/>'
            '</d:prop></d:propfind>'
        )
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=False, trust_env=False) as cx:
                headers = {"Depth": "0", "Content-Type": "application/xml"}
                if access_token:
                    headers["Authorization"] = f"Bearer {access_token}"
                r = await cx.request(
                    "PROPFIND", url,
                    auth=None if access_token else (user, pw),
                    headers=headers,
                    content=propfind_body,
                )
                # If the server demands Digest (Baïkal default, SabreDAV-based
                # servers, Radicale with htdigest), the Basic attempt above
                # 401s. Retry once with httpx.DigestAuth so this test matches
                # what the real sync does via caldav.DAVClient in
                # src/caldav_sync.py (which negotiates the scheme).
                if (not access_token) and r.status_code == 401 and "digest" in r.headers.get("www-authenticate", "").lower():
                    r = await cx.request(
                        "PROPFIND", url,
                        auth=httpx.DigestAuth(user, pw),
                        headers=headers,
                        content=propfind_body,
                    )
            # 207 = Multi-Status — standard CalDAV success. 200 also
            # acceptable. Anything else (401/403/404/5xx) means trouble.
            if r.status_code in (200, 207):
                return {"ok": True}
            if r.status_code == 401:
                return {"ok": False, "error": "Auth failed — check username/password"}
            if r.status_code == 403:
                return {"ok": False, "error": "Forbidden — user can't access that URL"}
            if r.status_code == 404:
                return {"ok": False, "error": "Not found — check the URL path"}
            if 300 <= r.status_code < 400:
                return {"ok": False, "error": "Redirects are not followed for CalDAV safety; use the final URL"}
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        except httpx.ConnectError as e:
            return {"ok": False, "error": f"Connection refused: {e}"[:200]}
        except httpx.TimeoutException:
            return {"ok": False, "error": "Connection timed out"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    @router.post("/sync")
    async def sync_caldav_endpoint(request: Request, direction: str = "pull"):
        """Sync events with the configured CalDAV server.
        Returns counts + any per-calendar errors. Called by the frontend
        on calendar open and by the periodic scheduler loop."""
        owner = _require_user(request)
        from src.caldav_sync import sync_caldav_direction
        return await sync_caldav_direction(owner, direction)


    @router.delete("/calendars/{cal_id}")
    async def delete_calendar(
        request: Request,
        cal_id: str,
        version: int = Query(..., ge=1),
    ):
        db = SessionLocal()
        try:
            with request_account_transaction(db, request, write=True) as account:
                delete_empty_calendar_collection(
                    db,
                    account=account,
                    calendar_id=cal_id,
                    expected_version=version,
                )
                return {"ok": True}
        except CalendarServiceError as exc:
            _raise_calendar_service_error(exc)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to delete calendar %s: %s", cal_id, e)
            raise HTTPException(500, "Failed to delete calendar")
        finally:
            db.close()


    @router.get("/calendars")
    async def list_calendars(request: Request):
        db = SessionLocal()
        try:
            # The historical GET establishes the first Personal calendar. Keep
            # that contract, but perform the lazy write behind the identity
            # barrier and commit it as one V3 account-scoped transaction.
            with request_account_transaction(db, request, write=True) as account:
                ensure_default_calendar(db, account=account)
                cals = db.query(CalendarCal).filter(
                    CalendarCal.owner_id == account.id
                ).order_by(CalendarCal.created_at.asc(), CalendarCal.id.asc()).all()
                return {"calendars": [
                    {
                        "name": c.name,
                        "href": c.id,
                        "color": c.color,
                        "source": c.source,
                        "version": int(c.config_version or 1),
                    }
                    for c in cals
                ]}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to list calendars: %s", e)
            raise HTTPException(500, "Failed to list calendars")
        finally:
            db.close()

    @router.get("/events")
    async def list_events(request: Request, start: str, end: str, calendar: str = ""):
        try:
            start_dt = _parse_dt(start)
            end_dt = _parse_dt(end)
        except ValueError:
            # A malformed range (e.g. a stray "NaN-NaN-NaN" from the client)
            # shouldn't spam the user with an error notification on every poll —
            # just log it and return no events for this window.
            logger.warning("list_events: unparseable range start=%r end=%r", start, end)
            return {"events": []}
        db = SessionLocal()
        try:
            with request_account_transaction(db, request, write=False) as account:
                if account is None:
                    return {"events": []}
                return _list_events_for_owner(
                    db,
                    owner_id=account.id,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    calendar=calendar,
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to list events: %s", e)
            raise HTTPException(500, "Failed to list events")
        finally:
            db.close()

    @router.get("/time-intelligence")
    async def time_intelligence(
        request: Request,
        as_of: datetime = Query(...),
        start: datetime = Query(...),
        end: datetime = Query(...),
        minimum_slot_minutes: int = Query(default=30, ge=15, le=480),
        day_start_hour: int = Query(default=6, ge=0, le=22),
        day_end_hour: int = Query(default=23, ge=1, le=24),
        daily_capacity_minutes: int = Query(default=600, ge=30, le=1440),
        travel_buffer_minutes: int = Query(default=30, ge=0, le=240),
    ):
        db = SessionLocal()
        try:
            with request_account_transaction(db, request, write=False) as account:
                if account is None:
                    return {
                        "events": [], "event_count": 0, "free_slots": [],
                        "conflicts": [], "read_only": True,
                        "can_reschedule_or_create": False, "truncated": False,
                    }
                return calendar_time_report(
                    db,
                    owner_id=account.id,
                    as_of=as_of,
                    window_start=start,
                    window_end=end,
                    minimum_slot_minutes=minimum_slot_minutes,
                    day_start_hour=day_start_hour,
                    day_end_hour=day_end_hour,
                    daily_capacity_minutes=daily_capacity_minutes,
                    travel_buffer_minutes=travel_buffer_minutes,
                )
        except LifeGraphError as exc:
            db.rollback()
            raise HTTPException(400, str(exc)) from exc
        except HTTPException:
            raise
        except Exception:
            db.rollback()
            logger.exception("Failed to compute calendar time intelligence")
            raise HTTPException(500, "Failed to compute calendar time intelligence")
        finally:
            db.close()

    @router.post("/events")
    async def create_event(request: Request, data: EventCreate):
        db = SessionLocal()
        try:
            with request_account_transaction(db, request, write=True) as account:
                result = create_calendar_event(
                    db,
                    account=account,
                    summary=data.summary,
                    dtstart=data.dtstart,
                    dtend=data.dtend,
                    all_day=data.all_day,
                    calendar_id=data.calendar_href,
                    description=data.description,
                    location=data.location,
                    rrule=data.rrule or "",
                    color=data.color,
                    importance=data.importance,
                    event_type=data.event_type,
                    linked_entity_ids=data.linked_entity_ids,
                    idempotency_key=data.idempotency_key,
                )
                return {
                    "ok": True,
                    "uid": result.event.uid,
                    "version": result.event_version,
                    "created": result.created,
                    "event": _event_to_dict(result.event),
                }
        except CalendarServiceError as exc:
            _raise_calendar_service_error(exc)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to create event: %s", e)
            raise HTTPException(500, "Failed to create event")
        finally:
            db.close()

    @router.put("/events/{uid}")
    async def update_event(request: Request, uid: str, data: EventUpdate):
        try:
            base_uid = _resolve_base_uid(uid)
        except ValueError as e:
            raise HTTPException(400, str(e))
        db = SessionLocal()
        try:
            with request_account_transaction(db, request, write=True) as account:
                metadata = {
                    field: getattr(data, field)
                    for field in (
                        "summary", "description", "location", "color",
                        "importance", "event_type",
                    )
                    if field in data.model_fields_set
                }
                schedule_fields = {
                    field for field in ("dtstart", "dtend", "all_day", "rrule")
                    if field in data.model_fields_set
                }
                result = update_calendar_event(
                    db,
                    account=account,
                    uid=base_uid,
                    expected_version=data.version,
                    changes=metadata,
                    linked_entity_ids=data.linked_entity_ids or (),
                )
                if schedule_fields:
                    event = result.event
                    serialized = _event_to_dict(event)
                    if (
                        "all_day" in schedule_fields
                        and not {"dtstart", "dtend"}.issubset(schedule_fields)
                    ):
                        raise CalendarServiceError(
                            "dtstart and dtend are required when all_day changes"
                        )
                    schedule_kwargs = {
                        "account": account,
                        "uid": base_uid,
                        "expected_version": result.event_version,
                        "dtstart": (
                            data.dtstart
                            if "dtstart" in schedule_fields
                            else serialized["dtstart"]
                        ),
                        "dtend": (
                            data.dtend
                            if "dtend" in schedule_fields
                            else serialized["dtend"]
                        ),
                    }
                    if "all_day" in schedule_fields:
                        schedule_kwargs["all_day"] = data.all_day
                    if "rrule" in schedule_fields:
                        schedule_kwargs["rrule"] = data.rrule or ""
                    result = reschedule_calendar_event(db, **schedule_kwargs)
                return {
                    "ok": True,
                    "uid": result.event.uid,
                    "version": result.event_version,
                    "event": _event_to_dict(result.event),
                }
        except CalendarServiceError as exc:
            _raise_calendar_service_error(exc)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to update event: %s", e)
            raise HTTPException(500, "Failed to update event")
        finally:
            db.close()

    @router.delete("/events/{uid}")
    async def delete_event(
        request: Request,
        uid: str,
        scope: str = "series",
        version: int = Query(..., ge=1),
    ):
        normalized_scope = str(scope or "series").strip().lower()
        if normalized_scope not in {"series", "occurrence", "instance"}:
            raise HTTPException(400, "scope must be series or occurrence")
        if normalized_scope in {"occurrence", "instance"} and "::" not in uid:
            raise HTTPException(
                400, "An occurrence scope requires a recurring occurrence uid"
            )
        try:
            base_uid = _resolve_base_uid(uid)
        except ValueError as e:
            raise HTTPException(400, str(e))
        db = SessionLocal()
        try:
            with request_account_transaction(db, request, write=True) as account:
                is_occurrence_delete = (
                    normalized_scope in {"occurrence", "instance"}
                )
                if is_occurrence_delete:
                    event = db.query(CalendarEvent).filter(
                        CalendarEvent.uid == base_uid,
                        CalendarEvent.owner_id == account.id,
                    ).first()
                    if event is None:
                        raise CalendarNotFound("Calendar event not found")
                    key = _occurrence_exdate_key(uid, event)
                    if not key:
                        raise CalendarServiceError(
                            "Invalid recurring occurrence uid"
                        )
                    result = exclude_calendar_occurrence(
                        db,
                        account=account,
                        uid=base_uid,
                        expected_version=version,
                        occurrence_key=key,
                    )
                    return {
                        "ok": True,
                        "scope": "occurrence",
                        "exdate": key,
                        "uid": result.event.uid,
                        "version": result.event_version,
                        "event": _event_to_dict(result.event),
                    }
                result = cancel_calendar_event(
                    db,
                    account=account,
                    uid=base_uid,
                    expected_version=version,
                )
                return {
                    "ok": True,
                    "scope": "series",
                    "uid": result.event.uid,
                    "version": result.event_version,
                    "event": _event_to_dict(result.event),
                }
        except CalendarServiceError as exc:
            _raise_calendar_service_error(exc)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to delete event: %s", e)
            raise HTTPException(500, "Failed to delete event")
        finally:
            db.close()

    @router.post("/calendars")
    async def create_calendar(request: Request, name: str = "Imported", color: str = "#5b8abf"):
        db = SessionLocal()
        try:
            with request_account_transaction(db, request, write=True) as account:
                cal = create_local_calendar(
                    db,
                    account=account,
                    name=name,
                    color=color,
                )
                return {
                    "ok": True,
                    "id": cal.id,
                    "name": cal.name,
                    "color": cal.color,
                    "version": int(cal.config_version or 1),
                }
        except CalendarServiceError as exc:
            _raise_calendar_service_error(exc)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to create calendar: %s", e)
            raise HTTPException(500, "Failed to create calendar")
        finally:
            db.close()

    @router.put("/calendars/{cal_id}")
    async def update_calendar(
        request: Request,
        cal_id: str,
        version: int = Query(..., ge=1),
        name: str = None,
        color: str = None,
    ):
        db = SessionLocal()
        try:
            with request_account_transaction(db, request, write=True) as account:
                cal = update_calendar_collection(
                    db,
                    account=account,
                    calendar_id=cal_id,
                    expected_version=version,
                    name=name,
                    color=color,
                )
                return {"ok": True, "version": int(cal.config_version or 1)}
        except CalendarServiceError as exc:
            _raise_calendar_service_error(exc)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to update calendar: %s", e)
            raise HTTPException(500, "Failed to update calendar")
        finally:
            db.close()


    # Hard cap on ICS upload (ICS_MAX_BYTES, default 10 MB). Loading the whole
    # file into memory is unavoidable with python-icalendar, so an unbounded
    # upload would OOM.

    @router.post("/import")
    async def import_ics(request: Request, file: UploadFile = File(...), calendar_name: str = ""):
        """Import events from an .ics file (scoped to caller's account)."""
        from icalendar import Calendar as iCal

        db = SessionLocal()
        try:
            content = await read_upload_limited(file, ICS_MAX_BYTES, "ICS file")
            try:
                cal_data = iCal.from_ical(content)
            except Exception as e:
                raise HTTPException(400, f"Invalid ICS file: {e}")

            # Sanitize display name — length cap + strip control chars
            raw_name = calendar_name.strip() or (file.filename or "").replace(".ics", "").replace("_", " ").strip() or "Imported"
            cal_display = "".join(c for c in raw_name if c.isprintable())[:120] or "Imported"

            with request_account_transaction(db, request, write=True) as account:
                target_cal = db.query(CalendarCal).filter(
                    CalendarCal.name == cal_display,
                    CalendarCal.owner_id == account.id,
                ).order_by(CalendarCal.created_at.asc(), CalendarCal.id.asc()).first()
                if target_cal is None:
                    target_cal = create_local_calendar(
                        db,
                        account=account,
                        calendar_id=str(uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"restia:ics-calendar:{account.id}:{cal_display}",
                        )),
                        name=cal_display,
                        color="#7c4dff",
                        source="import",
                    )

                imported = skipped = repaired = 0
                for comp in cal_data.walk():
                    if comp.name != "VEVENT":
                        continue
                    dtstart = comp.get("dtstart")
                    if not dtstart:
                        skipped += 1
                        continue

                    dt_val = dtstart.dt
                    all_day = isinstance(dt_val, date) and not isinstance(
                        dt_val, datetime
                    )
                    from datetime import timezone as _tz
                    if all_day:
                        start_dt = datetime(dt_val.year, dt_val.month, dt_val.day)
                        raw_end = comp.get("dtend")
                        end_dt = (
                            datetime(
                                raw_end.dt.year, raw_end.dt.month, raw_end.dt.day
                            )
                            if raw_end else start_dt + timedelta(days=1)
                        )
                        start_value = start_dt.date().isoformat()
                        end_value = end_dt.date().isoformat()
                    else:
                        if getattr(dt_val, "tzinfo", None) is not None:
                            start_dt = dt_val.astimezone(_tz.utc).replace(tzinfo=None)
                            start_value = start_dt.replace(
                                tzinfo=_tz.utc
                            ).isoformat().replace("+00:00", "Z")
                        else:
                            start_dt = dt_val
                            start_value = start_dt.isoformat()
                        raw_end = comp.get("dtend")
                        if raw_end:
                            end_dt = raw_end.dt
                            if getattr(end_dt, "tzinfo", None) is not None:
                                end_dt = end_dt.astimezone(_tz.utc).replace(
                                    tzinfo=None
                                )
                                end_value = end_dt.replace(
                                    tzinfo=_tz.utc
                                ).isoformat().replace("+00:00", "Z")
                            else:
                                end_value = end_dt.isoformat()
                        else:
                            end_dt = start_dt + timedelta(hours=1)
                            end_value = (
                                end_dt.replace(tzinfo=_tz.utc).isoformat().replace(
                                    "+00:00", "Z"
                                )
                                if start_value.endswith("Z") else end_dt.isoformat()
                            )

                    end_dt = _ensure_positive_duration(start_dt, end_dt, all_day)
                    if all_day:
                        end_value = end_dt.date().isoformat()
                    elif end_dt <= start_dt:
                        # Kept for defensive readability; the clamp above makes
                        # this branch unreachable.
                        end_value = end_dt.isoformat()
                    elif start_value.endswith("Z"):
                        end_value = end_dt.replace(
                            tzinfo=_tz.utc
                        ).isoformat().replace("+00:00", "Z")

                    summary = " ".join(
                        str(comp.get("summary", "") or "").strip().split()
                    )
                    if not summary:
                        skipped += 1
                        continue
                    description = str(comp.get("description", "") or "").strip()
                    location = str(comp.get("location", "") or "").strip()
                    recurrence = (
                        comp.get("rrule").to_ical().decode()
                        if comp.get("rrule") else ""
                    )

                    existing = db.query(CalendarEvent).filter(
                        CalendarEvent.owner_id == account.id,
                        CalendarEvent.calendar_id == target_cal.id,
                        CalendarEvent.dtstart == start_dt,
                        CalendarEvent.summary == summary,
                    ).first()
                    if existing is not None:
                        fixed_end = _ensure_positive_duration(
                            existing.dtstart,
                            existing.dtend,
                            bool(existing.all_day),
                        )
                        if fixed_end != existing.dtend:
                            existing_wire = _event_to_dict(existing)
                            repair_start = existing_wire["dtstart"]
                            repair_end = (
                                fixed_end.date().isoformat()
                                if existing.all_day
                                else (
                                    fixed_end.isoformat() + "Z"
                                    if existing.is_utc else fixed_end.isoformat()
                                )
                            )
                            reschedule_calendar_event(
                                db,
                                account=account,
                                uid=existing.uid,
                                expected_version=int(existing.version or 1),
                                dtstart=repair_start,
                                dtend=repair_end,
                                all_day=bool(existing.all_day),
                                rrule=existing.rrule or "",
                            )
                            repaired += 1
                        else:
                            # Project legacy rows even when the import itself is
                            # a no-op; the service remains the only event writer.
                            update_calendar_event(
                                db,
                                account=account,
                                uid=existing.uid,
                                expected_version=int(existing.version or 1),
                                changes={},
                            )
                        skipped += 1
                        continue

                    source_uid = str(comp.get("uid", "") or "").strip()
                    identity_material = json.dumps(
                        {
                            "calendar_id": target_cal.id,
                            "source_uid": source_uid,
                            "summary": summary,
                            "dtstart": start_value,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    import_key = "ics:" + hashlib.sha256(
                        identity_material.encode("utf-8")
                    ).hexdigest()
                    result = create_calendar_event(
                        db,
                        account=account,
                        calendar_id=target_cal.id,
                        summary=summary,
                        description=description,
                        location=location,
                        dtstart=start_value,
                        dtend=end_value,
                        all_day=all_day,
                        rrule=recurrence,
                        idempotency_key=import_key,
                    )
                    if result.created:
                        imported += 1
                    else:
                        skipped += 1

                return {
                    "ok": True,
                    "imported": imported,
                    "skipped": skipped,
                    "repaired": repaired,
                    "calendar": cal_display,
                    "calendar_id": target_cal.id,
                }
        except CalendarServiceError as exc:
            _raise_calendar_service_error(exc)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            logger.error("Failed to import ICS: %s", e)
            raise HTTPException(500, "Failed to import ICS")
        finally:
            db.close()

    @router.get("/export/{cal_id}")
    async def export_ics(request: Request, cal_id: str):
        """Export a calendar as .ics file."""
        from fastapi.responses import Response

        db = SessionLocal()
        try:
            with request_account_transaction(db, request, write=False) as account:
                if account is None:
                    raise HTTPException(404, "Calendar not found")
                cal = db.query(CalendarCal).filter(
                    CalendarCal.id == cal_id,
                    CalendarCal.owner_id == account.id,
                ).first()
                if cal is None:
                    raise HTTPException(404, "Calendar not found")
                events = db.query(CalendarEvent).filter(
                    CalendarEvent.calendar_id == cal_id,
                    CalendarEvent.owner_id == account.id,
                    CalendarEvent.status != "cancelled",
                ).all()

                lines = [
                    "BEGIN:VCALENDAR",
                    "VERSION:2.0",
                    "PRODID:-//Restia//Calendar//EN",
                    f"X-WR-CALNAME:{_ics_escape(cal.name)}",
                ]
                for ev in events:
                    lines.append("BEGIN:VEVENT")
                    lines.append(f"UID:{ev.uid}")
                    lines.append(f"SUMMARY:{_ics_escape(ev.summary or '')}")
                    if ev.all_day:
                        lines.append(
                            f"DTSTART;VALUE=DATE:{ev.dtstart.strftime('%Y%m%d')}"
                        )
                        lines.append(
                            f"DTEND;VALUE=DATE:{ev.dtend.strftime('%Y%m%d')}"
                        )
                    else:
                        suffix = "Z" if getattr(ev, "is_utc", False) else ""
                        lines.append(
                            f"DTSTART:{ev.dtstart.strftime('%Y%m%dT%H%M%S')}{suffix}"
                        )
                        lines.append(
                            f"DTEND:{ev.dtend.strftime('%Y%m%dT%H%M%S')}{suffix}"
                        )
                    if ev.description:
                        lines.append(f"DESCRIPTION:{_ics_escape(ev.description)}")
                    if ev.location:
                        lines.append(f"LOCATION:{_ics_escape(ev.location)}")
                    if ev.rrule:
                        lines.append(f"RRULE:{ev.rrule}")
                    lines.append("END:VEVENT")
                lines.append("END:VCALENDAR")

                ics_data = "\r\n".join(lines)
                download_name = _safe_ics_filename(cal.name)
                return Response(
                    content=ics_data,
                    media_type="text/calendar",
                    headers={
                        "Content-Disposition": (
                            f'attachment; filename="{download_name}"'
                        ),
                        "X-Content-Type-Options": "nosniff",
                    },
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Failed to export ICS: %s", e)
            raise HTTPException(500, "Failed to export ICS")
        finally:
            db.close()

    @router.post("/quick-parse")
    async def quick_parse(request: Request):
        """Parse a natural-language event description into structured fields.

        Input: {"text": "lunch with sara friday 1pm downtown", "tz": "America/New_York"}
        Output: {"ok": true, "event": {"summary", "dtstart", "dtend",
                  "all_day", "location", "description"}, "confidence": 0.0-1.0}

        Anchored on the server's current date/time so phrases like
        "tomorrow", "next Tuesday", "in 30 minutes" resolve correctly.
        Uses the "utility" endpoint (small / fast model) to keep latency low.
        """
        owner = _require_user(request)
        from src.endpoint_resolver import resolve_endpoint
        from src.llm_core import llm_call_async
        from src.text_helpers import strip_think
        import json as _json
        import re as _re

        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "text is required")
        from src.user_time import (
            clear_user_time_context,
            current_datetime_prompt,
            now_user_local,
            set_user_tz_name,
            set_user_tz_offset,
        )

        clear_user_time_context()
        tz_hint = (body.get("tz") or "").strip()
        if body.get("tz_offset") is not None:
            set_user_tz_offset(body.get("tz_offset"))
        if tz_hint:
            set_user_tz_name(tz_hint)

        url, model, headers = resolve_endpoint("utility", owner=owner or None)
        if not url:
            url, model, headers = resolve_endpoint("default", owner=owner or None)
        if not url or not model:
            return {"ok": False, "error": "No LLM endpoint configured"}

        now = now_user_local()
        now_iso = now.strftime("%Y-%m-%dT%H:%M:%S")
        # The model gets only the schema it needs to fill out; we re-validate
        # everything client-side too.
        system_prompt = (
            current_datetime_prompt()
            + "You are a calendar event parser. Read the user's one-line "
            "description and emit STRICT JSON describing the event. "
            f"The current user-local timestamp is {now_iso}. "
            + "Resolve relative dates (\"tomorrow\", \"friday\", \"next monday\", "
              "\"in 30 minutes\") against today. Default duration is 60 minutes "
              "when no end time is given. If the text mentions a date with no "
              "time, treat it as an all-day event.\n\n"
              "Output ONLY this JSON shape, nothing else:\n"
              "{\n"
              '  "summary": "<event title, capitalized>",\n'
              '  "dtstart": "<YYYY-MM-DDTHH:MM:00>",\n'
              '  "dtend":   "<YYYY-MM-DDTHH:MM:00>",\n'
              '  "all_day": <true|false>,\n'
              '  "location": "<place or empty>",\n'
              '  "description": "",\n'
              '  "confidence": <0.0-1.0>\n'
              "}\n"
              "For all-day events use \"YYYY-MM-DD\" (no time) for both fields.\n"
              "CRITICAL: DO NOT OUTPUT ANY CONVERSATIONAL TEXT, GREETINGS, OR NOTES. OUTPUT ONLY THE JSON BLOCK STARTING WITH {."
        )

        try:
            raw = await llm_call_async(
                url=url, model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Parse this event into JSON:\n\n{text}"},
                ],
                headers=headers,
                temperature=0.0,
                timeout=300,
            )
        except Exception as e:
            return {"ok": False, "error": f"LLM call failed: {e}"}

        cleaned = strip_think(raw or "", prose=False, prompt_echo=True)
        cleaned = _re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=_re.MULTILINE).strip()
        m = _re.search(r"\{[\s\S]*\}", cleaned)
        if not m:
            return {"ok": False, "error": "Could not extract JSON", "raw": cleaned[:400]}
        try:
            parsed = _json.loads(m.group())
        except Exception as e:
            return {"ok": False, "error": f"Invalid JSON: {e}", "raw": cleaned[:400]}

        # Light validation / defaults so the frontend can trust the shape.
        summary = (parsed.get("summary") or text)[:200]
        # Strip stale relative/absolute time tokens that the LLM (or the
        # user's raw input) sometimes leaks into the summary — these
        # would otherwise be displayed verbatim in reminder notifications
        # that fire much later, when "in 29 min" is no longer true. The
        # actual timing lives in dtstart/dtend.
        summary = _re.sub(r'\bin\s+\d+\s*(min|minute|hour|hr|day)s?\b', '', summary, flags=_re.IGNORECASE)
        summary = _re.sub(r'\(\s*\d{1,2}:\d{2}\s*\)', '', summary)
        summary = _re.sub(r'\b\d{1,2}(:\d{2})?\s*(am|pm)\b', '', summary, flags=_re.IGNORECASE)
        summary = _re.sub(r'\s+@\s+(?=\d)', ' ', summary)  # drop "@" when right before a time
        summary = _re.sub(r'\s+', ' ', summary).strip(' -—,@')
        all_day = bool(parsed.get("all_day"))
        dtstart = (parsed.get("dtstart") or "").strip()
        dtend   = (parsed.get("dtend") or "").strip()
        # Force naive-local on LLM output. The model is anchored on the
        # user's local "now" via the system prompt, so its emitted
        # datetime is already meant to be the user's wall-clock time.
        # Some models append `Z` or a tz offset anyway, which would
        # make `_parse_dt_pair` flag the row as UTC and shift the
        # displayed time forward by the user's tz offset. Strip any
        # trailing tz marker so the time is stored exactly as the LLM
        # wrote it.
        def _strip_tz(s):
            if not s:
                return s
            s = s.strip()
            # Strip "Z"
            if s.endswith('Z') or s.endswith('z'):
                s = s[:-1]
            # Strip "+HH:MM" / "-HH:MM" if it followed a T-time
            s = _re.sub(r'[+-]\d{2}:?\d{2}$', '', s)
            return s
        dtstart = _strip_tz(dtstart)
        dtend   = _strip_tz(dtend)
        if not dtstart:
            return {"ok": False, "error": "Model did not produce a start time", "raw": cleaned[:400]}
        if not dtend:
            # Auto-fill +60 min for timed events; +0 for all-day (single-day).
            try:
                if all_day:
                    dtend = dtstart
                else:
                    dt = datetime.fromisoformat(dtstart)
                    dtend = (dt + timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:00")
            except Exception:
                dtend = dtstart

        return {
            "ok": True,
            "event": {
                "summary": summary,
                "dtstart": dtstart,
                "dtend": dtend,
                "all_day": all_day,
                "location": (parsed.get("location") or "").strip()[:200],
                "description": (parsed.get("description") or "").strip()[:2000],
            },
            "confidence": float(parsed.get("confidence", 0.7) or 0.7),
        }

    return router
