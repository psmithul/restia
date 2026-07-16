"""Server-owned completion identity for Note checklists.

The public ``item.id`` belongs to the editing client and is allowed to change.
Progression evidence must not. Three private fields are persisted inside the
Note JSON but removed from API responses:

``_restia_evidence_id``
    Stable identity of the checklist row.  It survives edits, reorders, and
    public-id rotation.

``_restia_progression_cycle``
    Stable identity of the current recurring occurrence.  A changed due date
    never changes it; only :func:`advance_recurring_note` rotates the cycle.

``_restia_progression_due``
    Server-owned scheduled date for that cycle. It prevents a client from
    repeatedly completing future occurrences on the same day.

"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Iterable


EVIDENCE_ID_KEY = "_restia_evidence_id"
CYCLE_ID_KEY = "_restia_progression_cycle"
CYCLE_DUE_KEY = "_restia_progression_due"
_PRIVATE_ITEM_KEYS = {EVIDENCE_ID_KEY, CYCLE_ID_KEY, CYCLE_DUE_KEY}
_COMPLETION_TYPES = {"todo", "checklist", "goal"}


def _clean_identifier(value: Any, *, limit: int = 120) -> str:
    return str(value or "").strip()[:limit]


def _text_identity(item: dict[str, Any]) -> str:
    return re.sub(r"\s+", " ", str(item.get("text") or "")).strip().casefold()


def _is_recurring(repeat: Any) -> bool:
    return str(repeat or "none").strip().lower() != "none"


def _new_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cycle_is_due(
    item: dict[str, Any],
    now: datetime,
    *,
    tz_name: str | None = None,
) -> bool:
    """Allow a recurring occurrence on its scheduled local date, not before."""

    value = _clean_identifier(item.get(CYCLE_DUE_KEY), limit=120)
    if not value:
        return True
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(str(tz_name)) if tz_name else None
    except Exception:
        zone = None
    aware_now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    if zone is not None:
        local_now = aware_now.astimezone(zone)
        scheduled = (
            parsed.replace(tzinfo=zone)
            if parsed.tzinfo is None
            else parsed.astimezone(zone)
        )
    elif parsed.tzinfo is None:
        local_now = now.astimezone() if now.tzinfo is not None else now
        scheduled = parsed
    else:
        # Without an owner IANA zone, fail safe at the exact instant rather
        # than treating a UTC calendar date as the user's local cycle date.
        return aware_now.astimezone(timezone.utc) >= parsed.astimezone(timezone.utc)
    return local_now.date() >= scheduled.date()


def public_note_items(items: Iterable[Any] | None) -> list[Any]:
    """Return API-safe items without Restia's private evidence metadata."""

    public: list[Any] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            public.append(raw)
            continue
        public.append({key: value for key, value in raw.items() if key not in _PRIVATE_ITEM_KEYS})
    return public


def completion_items_fully_done(note_type: Any, items: Any) -> bool:
    """Whether a structured to-do has real items and every item is complete."""

    if str(note_type or "").strip().lower() not in _COMPLETION_TYPES:
        return False
    raw = items
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            return False
    if not isinstance(raw, list) or not raw:
        return False
    entries = [item for item in raw if isinstance(item, dict)]
    return bool(entries) and len(entries) == len(raw) and all(
        bool(item.get("done") or item.get("checked")) for item in entries
    )


def _canonical_old_items(
    old_items: Iterable[Any] | None,
    *,
    repeat: Any,
    due_date: Any,
) -> list[dict[str, Any]]:
    """Adopt legacy rows into stable evidence without invalidating old awards."""

    canonical: list[dict[str, Any]] = []
    used_evidence: set[str] = set()
    recurring = _is_recurring(repeat)
    # Before V2 recurring keys used due_date directly.  Adopting that value as
    # the first private cycle preserves idempotency for already-awarded rows;
    # all later cycles are opaque server UUIDs.
    legacy_cycle = _clean_identifier(due_date, limit=80) or "unscheduled"

    for raw in old_items or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        evidence = _clean_identifier(item.get(EVIDENCE_ID_KEY))
        if not evidence:
            # Existing rows predate private evidence.  Adopt their old public
            # id once so a completion already recorded under that key remains
            # idempotent.  New rows always receive an opaque UUID below.
            evidence = _clean_identifier(item.get("id")) or _new_id()
        if evidence in used_evidence:
            evidence = _new_id()
        used_evidence.add(evidence)
        item[EVIDENCE_ID_KEY] = evidence
        has_private_cycle = CYCLE_ID_KEY in item
        cycle = _clean_identifier(item.get(CYCLE_ID_KEY), limit=80)
        if not has_private_cycle and recurring:
            cycle = legacy_cycle
        item[CYCLE_ID_KEY] = cycle
        cycle_due = _clean_identifier(item.get(CYCLE_DUE_KEY), limit=120)
        item[CYCLE_DUE_KEY] = (
            cycle_due or (str(due_date or "") if recurring else "")
        )
        canonical.append(item)
    return canonical


def recurring_occurrence_is_due(
    *,
    items_json: str | None,
    repeat: Any,
    due_date: Any,
    now: datetime | None = None,
    tz_name: str | None = None,
) -> bool:
    """Check the hidden authoritative due date before rotating a cycle."""

    if not _is_recurring(repeat):
        return False
    try:
        raw_items = json.loads(items_json or "[]")
    except (TypeError, json.JSONDecodeError):
        raw_items = []
    canonical = _canonical_old_items(
        raw_items,
        repeat=repeat,
        due_date=due_date,
    )
    current = now or _utcnow()
    if not canonical:
        return _cycle_is_due(
            {CYCLE_DUE_KEY: str(due_date or "")},
            current,
            tz_name=tz_name,
        )
    return all(_cycle_is_due(item, current, tz_name=tz_name) for item in canonical)


def normalize_created_items(
    items: Iterable[Any] | None,
    *,
    repeat: Any = "none",
    due_date: Any = None,
) -> list[dict[str, Any]]:
    """Normalize a new checklist while ignoring all client evidence fields."""

    normalized: list[dict[str, Any]] = []
    used_public_ids: set[str] = set()
    cycle = _new_id() if _is_recurring(repeat) else ""
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        item = {key: value for key, value in raw.items() if key not in _PRIVATE_ITEM_KEYS}
        public_id = _clean_identifier(item.get("id"))
        if not public_id or public_id in used_public_ids:
            public_id = _new_id()
        used_public_ids.add(public_id)
        item["id"] = public_id
        item["text"] = str(item.get("text") or "")[:2000]
        item["done"] = bool(item.get("done") or item.get("checked"))
        item.pop("checked", None)
        item[EVIDENCE_ID_KEY] = _new_id()
        item[CYCLE_ID_KEY] = cycle
        item[CYCLE_DUE_KEY] = str(due_date or "") if cycle else ""
        normalized.append(item)
    return normalized


def normalize_updated_items(
    old_items: Iterable[Any] | None,
    submitted_items: Iterable[Any] | None,
    *,
    repeat: Any = "none",
    due_date: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return canonical old/new items with stable server evidence identities.

    Matching first uses the previous public id, then text (which makes public-id
    rotation harmless), and finally position when the list length is unchanged
    (normal editors may rewrite both text and ids in one save).  An unrecognized
    row gets new server evidence and a pre-checked new row is never awarded by
    the caller because it has no previous state.
    """

    canonical_old = _canonical_old_items(old_items, repeat=repeat, due_date=due_date)
    by_public_id: dict[str, list[int]] = defaultdict(list)
    by_text: dict[str, list[int]] = defaultdict(list)
    for index, item in enumerate(canonical_old):
        public_id = _clean_identifier(item.get("id"))
        if public_id:
            by_public_id[public_id].append(index)
        by_text[_text_identity(item)].append(index)

    recurring = _is_recurring(repeat)
    current_cycle = next(
        (
            _clean_identifier(item.get(CYCLE_ID_KEY), limit=80)
            for item in canonical_old
            if _clean_identifier(item.get(CYCLE_ID_KEY), limit=80)
        ),
        _new_id() if recurring else "",
    )
    current_cycle_due = next(
        (
            _clean_identifier(item.get(CYCLE_DUE_KEY), limit=120)
            for item in canonical_old
            if _clean_identifier(item.get(CYCLE_DUE_KEY), limit=120)
        ),
        str(due_date or "") if recurring else "",
    )
    submitted = [raw for raw in (submitted_items or []) if isinstance(raw, dict)]
    used_old: set[int] = set()
    used_public_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []

    def available(candidates: Iterable[int]) -> list[int]:
        return [index for index in candidates if index not in used_old]

    for position, raw in enumerate(submitted):
        clean = {key: value for key, value in raw.items() if key not in _PRIVATE_ITEM_KEYS}
        submitted_id = _clean_identifier(clean.get("id"))
        match: int | None = None
        id_matches = available(by_public_id.get(submitted_id, ())) if submitted_id else []
        if id_matches:
            match = min(id_matches, key=lambda index: abs(index - position))
        if match is None:
            text_matches = available(by_text.get(_text_identity(clean), ()))
            if text_matches:
                match = min(text_matches, key=lambda index: abs(index - position))
        if (
            match is None
            and submitted_id
            and len(submitted) == len(canonical_old)
            and position < len(canonical_old)
            and position not in used_old
        ):
            match = position

        previous = canonical_old[match] if match is not None else None
        if match is not None:
            used_old.add(match)

        public_id = submitted_id
        if not public_id or public_id in used_public_ids:
            public_id = _new_id()
        used_public_ids.add(public_id)
        clean["id"] = public_id
        clean["text"] = str(clean.get("text") or "")[:2000]
        clean["done"] = bool(clean.get("done") or clean.get("checked"))
        clean.pop("checked", None)
        if previous is not None:
            clean[EVIDENCE_ID_KEY] = previous[EVIDENCE_ID_KEY]
            # Empty is a real server-owned value for a row that began as a
            # one-off. Changing ``repeat`` in the same PUT must not silently
            # mint a recurring key before the server advances an occurrence.
            clean[CYCLE_ID_KEY] = (
                previous[CYCLE_ID_KEY]
                if CYCLE_ID_KEY in previous
                else current_cycle
            )
            clean[CYCLE_DUE_KEY] = (
                previous[CYCLE_DUE_KEY]
                if CYCLE_DUE_KEY in previous
                else current_cycle_due
            )
            # Completion timestamps are evidence, not editable client fields.
            if previous.get("completed_at"):
                clean["completed_at"] = previous["completed_at"]
            else:
                clean.pop("completed_at", None)
        else:
            clean[EVIDENCE_ID_KEY] = _new_id()
            clean[CYCLE_ID_KEY] = current_cycle
            clean[CYCLE_DUE_KEY] = current_cycle_due
            clean.pop("completed_at", None)
        normalized.append(clean)
    return canonical_old, normalized


def evidence_event_key(note_id: str, item: dict[str, Any]) -> str:
    """Build an event key exclusively from server-owned evidence fields."""

    evidence = _clean_identifier(item.get(EVIDENCE_ID_KEY))
    if not evidence:
        raise ValueError("Checklist item is missing server evidence identity")
    cycle = _clean_identifier(item.get(CYCLE_ID_KEY), limit=80)
    suffix = f":{cycle}" if cycle else ""
    return f"todo:{str(note_id)[:80]}:{evidence}{suffix}"


def award_note_item_completions(
    db: Any,
    *,
    note: Any,
    owner: str | None,
    old_items: Iterable[Any],
    new_items: list[dict[str, Any]],
    occurred_at: datetime | None = None,
) -> int:
    """Award false-to-true transitions using only private evidence identity."""

    from src.progression import award_progression_event

    try:
        from src.notification_preferences import load_notification_preferences

        owner_timezone = str(load_notification_preferences(owner).get("timezone") or "UTC")
    except Exception:
        owner_timezone = "UTC"

    old_by_evidence = {
        str(item.get(EVIDENCE_ID_KEY)): item
        for item in old_items
        if isinstance(item, dict) and str(item.get(EVIDENCE_ID_KEY) or "").strip()
    }
    created_count = 0
    for item in new_items:
        if not isinstance(item, dict) or not item.get("done"):
            continue
        evidence_id = str(item.get(EVIDENCE_ID_KEY) or "").strip()
        previous = old_by_evidence.get(evidence_id)
        # A brand-new pre-checked item is not verified work.
        if previous is None or bool(previous.get("done") or previous.get("checked")):
            continue
        completed_at = occurred_at or _utcnow()
        if not _cycle_is_due(item, completed_at, tz_name=owner_timezone):
            continue
        item["completed_at"] = completed_at.isoformat(timespec="seconds") + "Z"
        _row, created = award_progression_event(
            db,
            owner=owner,
            event_key=evidence_event_key(str(note.id), item),
            source_type="todo_item_completed",
            source_id=f"{note.id}:{evidence_id}",
            title=str(item.get("text") or getattr(note, "title", "") or "To do completed"),
            details={
                "note_id": str(note.id),
                "note_title": str(getattr(note, "title", "") or "")[:240],
            },
            occurred_at=completed_at,
        )
        created_count += int(created)
    return created_count


def advance_recurring_note(note: Any, next_due_date: str) -> None:
    """Advance a recurring Note and rotate its private completion cycle once."""

    if not _is_recurring(getattr(note, "repeat", "none")):
        raise ValueError("Only a recurring note can advance its progression cycle")
    try:
        raw_items = json.loads(getattr(note, "items", None) or "[]")
    except (TypeError, json.JSONDecodeError):
        raw_items = []
    canonical = _canonical_old_items(
        raw_items,
        repeat=getattr(note, "repeat", "none"),
        due_date=getattr(note, "due_date", None),
    )
    new_cycle = _new_id()
    for item in canonical:
        item[CYCLE_ID_KEY] = new_cycle
        item[CYCLE_DUE_KEY] = str(next_due_date)
        item["done"] = False
        item.pop("checked", None)
        item.pop("completed_at", None)
    note.items = json.dumps(canonical) if canonical else getattr(note, "items", None)
    note.due_date = str(next_due_date)


def recurring_advance_values(
    *,
    items_json: str | None,
    repeat: Any,
    due_date: Any,
    next_due_date: str,
) -> tuple[str | None, str]:
    """Build one immutable recurrence update for a compare-and-swap write."""

    target = SimpleNamespace(
        items=items_json,
        repeat=repeat,
        due_date=due_date,
    )
    advance_recurring_note(target, next_due_date)
    return target.items, str(target.due_date)
