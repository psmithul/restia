"""Owner-scoped calendar tool backed by Restia's reviewed action executor.

The model-facing ``manage_calendar`` tool is intentionally a thin adapter:
reads resolve the legacy login alias to immutable ``Account.id`` ownership.
Reversible create/update/reschedule writes are persisted as exact Level-4
``ActionProposal`` rows and executed by ``calendar_action_executor`` in the
same database transaction. Cancellation is only prepared as a Level-5 pending
proposal for human approval; this tool never receives or returns its approval
token and never performs connector I/O.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional

from sqlalchemy import func

from src.tools._common import _parse_tool_args


logger = logging.getLogger(__name__)

_REMINDER_FIELDS = frozenset({
    "reminder_minutes",
    "remind_before_minutes",
    "alarm_minutes",
    "reminder",
    "alarm",
})
_REPEAT_OFF = frozenset({"none", "no", "off", "false", "single"})
_METADATA_FIELDS = (
    "summary",
    "description",
    "location",
    "color",
    "importance",
)


def _error(message: object, **extra: Any) -> Dict[str, Any]:
    return {"error": str(message), "exit_code": 1, **extra}


def _required_owner(owner: Optional[str]) -> str:
    from src.identity import normalize_identity

    normalized = normalize_identity(owner)
    if not normalized:
        raise ValueError("A concrete authenticated calendar owner is required")
    return normalized


def _base_uid(value: object) -> str:
    uid = str(value or "").strip()
    if not uid:
        raise ValueError("uid is required")
    if len(uid) > 255:
        raise ValueError("Calendar event UID is too long")
    if "::" in uid:
        raise ValueError(
            "Calendar writes require the base event UID, not a recurrence occurrence"
        )
    return uid


def _positive_version(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("version is required and must be a positive integer")
    return value


def _has_reminder_request(args: Mapping[str, Any]) -> bool:
    for field in _REMINDER_FIELDS:
        if field not in args:
            continue
        value = args.get(field)
        if value in (None, "", False):
            continue
        if str(value).strip().lower() in {"none", "no", "off", "false"}:
            continue
        return True
    description = str(args.get("description") or "")
    return bool(re.search(r"\b(remind|reminder|alarm)\b", description, re.I))


def _normalize_action_datetime(value: object, *, all_day: bool) -> str:
    """Return JSON-safe ISO input while preserving the user's time context."""

    from routes.calendar_routes import parse_due_for_user

    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Calendar datetime is required")
    if all_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    return parse_due_for_user(raw)


def _parse_duration(value: object) -> timedelta | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    hours = re.search(r"(\d+)\s*(?:h|hr|hours?)", raw)
    minutes = re.search(r"(\d+)\s*(?:m|min|minutes?)", raw)
    seconds = (
        (int(hours.group(1)) * 3600 if hours else 0)
        + (int(minutes.group(1)) * 60 if minutes else 0)
    )
    return timedelta(seconds=seconds) if seconds > 0 else None


def _add_duration(value: str, duration: timedelta, *, all_day: bool) -> str:
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(raw)
    end = parsed + duration
    if all_day:
        return end.date().isoformat()
    return end.isoformat().replace("+00:00", "Z") if value.endswith("Z") else end.isoformat()


def _event_datetime(event, value: datetime, *, all_day: bool | None = None) -> str:
    resolved_all_day = bool(event.all_day) if all_day is None else bool(all_day)
    if resolved_all_day:
        return value.date().isoformat()
    if bool(event.is_utc):
        return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    return value.isoformat()


def _proposal_idempotency_key(
    *,
    owner_id: str,
    action: str,
    target_id: str | None,
    payload: Mapping[str, Any],
    explicit: object | None,
) -> str:
    if explicit is not None and str(explicit).strip():
        return "manage-calendar:" + str(explicit).strip()
    material = json.dumps(
        {
            "owner_id": owner_id,
            "action": action,
            "target_id": target_id,
            "payload": dict(payload),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"manage-calendar:{action}:{digest}"


def _owned_calendar(db, *, owner_id: str, selector: object):
    from core.database import CalendarCal

    value = str(selector or "").strip()
    if not value:
        return None
    exact = db.query(CalendarCal).filter(
        CalendarCal.owner_id == owner_id,
        CalendarCal.id == value,
    ).first()
    if exact is not None:
        return exact
    by_name = db.query(CalendarCal).filter(
        CalendarCal.owner_id == owner_id,
        func.lower(CalendarCal.name) == value.lower(),
    ).all()
    if len(by_name) == 1:
        return by_name[0]
    if len(by_name) > 1:
        raise ValueError("Calendar name is ambiguous; use its full calendar ID")
    by_prefix = db.query(CalendarCal).filter(
        CalendarCal.owner_id == owner_id,
        CalendarCal.id.startswith(value, autoescape=True),
    ).limit(2).all()
    if len(by_prefix) == 1:
        return by_prefix[0]
    if len(by_prefix) > 1:
        raise ValueError("Calendar ID prefix is ambiguous; use the full calendar ID")
    raise ValueError("Calendar not found")


def _owned_event(db, *, owner_id: str, uid: str):
    from core.database import CalendarEvent

    return db.query(CalendarEvent).filter(
        CalendarEvent.uid == uid,
        CalendarEvent.owner_id == owner_id,
    ).first()


def _proposal_result_event(db, *, owner_id: str, proposal):
    result = dict(proposal.result or {})
    uid = str(result.get("event_uid") or "").strip()
    return _owned_event(db, owner_id=owner_id, uid=uid) if uid else None


def _execute_level_four(
    db,
    *,
    account,
    action: str,
    target_id: str | None,
    payload: dict[str, Any],
    reason: str,
    explicit_idempotency_key: object | None = None,
):
    from src.action_policy import ActionPolicyConflict, create_action_proposal
    from src.calendar_action_executor import execute_calendar_action

    creation = create_action_proposal(
        db,
        owner_id=account.id,
        domain="calendar",
        action=action,
        autonomy_level=4,
        target_type="event",
        target_id=target_id,
        payload=payload,
        reason=reason,
        sources={"interface": "agent_tool", "tool": "manage_calendar"},
        external=False,
        idempotency_key=_proposal_idempotency_key(
            owner_id=account.id,
            action=action,
            target_id=target_id,
            payload=payload,
            explicit=explicit_idempotency_key,
        ),
    )
    proposal = creation.proposal
    if proposal.state == "completed":
        event = _proposal_result_event(db, owner_id=account.id, proposal=proposal)
        if event is None:
            raise ActionPolicyConflict(
                "Completed calendar proposal points to a missing owned event"
            )
        result_version = int(dict(proposal.result or {}).get("event_version") or 0)
        if result_version < 1:
            raise ActionPolicyConflict(
                "Completed calendar proposal has an invalid result version"
            )
        return proposal, event, True, result_version
    if proposal.state != "prepared" or proposal.requires_confirmation:
        raise ActionPolicyConflict(
            "Calendar proposal is not eligible for automatic Level-4 execution"
        )
    execution = execute_calendar_action(
        db,
        account=account,
        proposal_id=proposal.id,
        expected_version=int(proposal.version or 1),
    )
    return (
        execution.proposal,
        execution.mutation.event,
        False,
        int(execution.mutation.event_version),
    )


def _prepare_level_five_cancel(
    db,
    *,
    account,
    target_id: str,
    expected_event_version: int,
    explicit_idempotency_key: object | None = None,
):
    """Persist one cancellation proposal without approving or executing it."""

    from src.action_policy import ActionPolicyConflict, create_action_proposal

    payload = {"expected_event_version": expected_event_version}
    creation = create_action_proposal(
        db,
        owner_id=account.id,
        domain="calendar",
        action="cancel_event",
        autonomy_level=5,
        target_type="event",
        target_id=target_id,
        payload=payload,
        reason="Calendar event cancellation requested through the agent tool",
        sources={"interface": "agent_tool", "tool": "manage_calendar"},
        external=True,
        idempotency_key=_proposal_idempotency_key(
            owner_id=account.id,
            action="cancel_event",
            target_id=target_id,
            payload=payload,
            explicit=explicit_idempotency_key,
        ),
    )
    proposal = creation.proposal
    if (
        int(proposal.autonomy_level) != 5
        or not proposal.external
        or not proposal.requires_confirmation
    ):
        raise ActionPolicyConflict(
            "Calendar cancellation proposal has an invalid risk classification"
        )
    if proposal.state != "prepared":
        raise ActionPolicyConflict(
            "Calendar cancellation proposal is no longer pending human approval"
        )
    # Deliberately ignore creation.confirmation_token. The model-facing tool
    # must never receive it; a human web session issues a fresh challenge via
    # the action-policy API when the proposal is reviewed.
    return proposal, not creation.created


def _first_nonempty(args: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = args.get(name)
        if value not in (None, ""):
            return value
    return None


def _read_account(db, owner_name: str):
    from src.identity import find_account

    return find_account(db, owner_name)


def _empty_read(action: str) -> Dict[str, Any]:
    if action == "list_calendars":
        return {
            "response": "No calendars found.",
            "calendars": [],
            "exit_code": 0,
        }
    return {
        "response": "No calendar events found.",
        "events": [],
        "exit_code": 0,
    }


async def do_manage_calendar(content: str, owner: Optional[str] = None) -> Dict:
    """List or safely propose owner-scoped calendar event mutations."""

    from core.database import CalendarCal, CalendarEvent, SessionLocal
    from routes.calendar_routes import _parse_dt
    from src.action_policy import ActionPolicyConflict, ActionPolicyError
    from src.calendar_service import CalendarServiceError
    from src.identity import ensure_account

    try:
        args = _parse_tool_args(content)
    except ValueError:
        return _error("Invalid JSON arguments")

    # Some models emit {"events": [...]} instead of separate create calls.
    # Each child retains the exact same proposal/transaction boundary, so a
    # partial batch reports committed successes without concealing failures.
    if isinstance(args.get("events"), list) and not args.get("action"):
        results: list[Dict[str, Any]] = []
        for raw_event in args["events"]:
            if not isinstance(raw_event, dict):
                continue
            event = dict(raw_event)
            for field, target in (("start", "dtstart"), ("end", "dtend")):
                value = event.pop(field, None)
                if value is not None and target not in event:
                    event[target] = (
                        value.get("dateTime", value)
                        if isinstance(value, dict)
                        else value
                    )
            event.setdefault("action", "create_event")
            results.append(await do_manage_calendar(json.dumps(event), owner=owner))
        if not results:
            return _error("No events to create")
        created = [row for row in results if row.get("exit_code") == 0]
        failed = [row for row in results if row.get("exit_code") != 0]
        parts: list[str] = []
        if created:
            parts.append(
                f"Created {len(created)} event(s):\n"
                + "\n".join(str(row.get("response") or "") for row in created)
            )
        if failed:
            parts.append(
                f"Failed to create {len(failed)} event(s). First error: "
                + str(failed[0].get("error") or "Unknown error")
            )
        return {
            "response": "\n\n".join(parts),
            "exit_code": 1 if failed else 0,
            "created_count": len(created),
            "failed_count": len(failed),
            "results": results,
        }

    action = str(args.get("action") or "list_events").replace("-", "_").strip().lower()
    action = {
        "create": "create_event",
        "update": "update_event",
        "cancel": "cancel_event",
        "delete": "delete_event",
        "list": "list_events",
    }.get(action, action)
    try:
        owner_name = _required_owner(owner)
    except ValueError as exc:
        return _error(exc)

    db = SessionLocal()
    try:
        # SQLite defers its physical BEGIN until DML. Identity, proposal, Life
        # graph, and calendar services use nested savepoints for idempotency;
        # without an explicit outer transaction, releasing the first savepoint
        # can become the database-level commit and survive a later failure.
        if action in {"create_event", "update_event", "cancel_event"}:
            bind = db.get_bind()
            if bind.dialect.name == "sqlite":
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")

        if action == "list_calendars":
            account = _read_account(db, owner_name)
            if account is None:
                return _empty_read(action)
            rows = db.query(CalendarCal).filter(
                CalendarCal.owner_id == account.id
            ).order_by(CalendarCal.created_at.asc(), CalendarCal.id.asc()).all()
            calendars = [
                {"name": row.name, "href": row.id, "source": row.source}
                for row in rows
            ]
            lines = [f"Found {len(calendars)} calendar(s):"]
            lines.extend(
                f"- {row['name']} ({row['href'][:8]})" for row in calendars
            )
            return {
                "response": "\n".join(lines) if calendars else "No calendars found.",
                "calendars": calendars,
                "exit_code": 0,
            }

        if action == "list_events":
            try:
                start_raw = _first_nonempty(
                    args,
                    "start", "start_time", "start_date", "range_start",
                    "from", "dtstart", "since",
                )
                end_raw = _first_nonempty(
                    args,
                    "end", "end_time", "end_date", "range_end",
                    "to", "dtend", "until",
                )
                query_raw = args.get("query") or args.get("date_range") or args.get("range")
                if query_raw and (not start_raw or not end_raw):
                    return _error(
                        "list_events needs explicit start/end ISO datetimes; "
                        f"resolve the requested range ({query_raw!r}) and call "
                        "manage_calendar again."
                    )
                start_dt = (
                    _parse_dt(start_raw)
                    if start_raw
                    else datetime.utcnow().replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                )
                end_dt = _parse_dt(end_raw) if end_raw else start_dt + timedelta(days=14)
            except ValueError as exc:
                return _error(f"Invalid date format: {exc}")
            if end_dt <= start_dt:
                end_dt = start_dt + timedelta(days=1)

            account = _read_account(db, owner_name)
            if account is None:
                return _empty_read(action)
            query = db.query(CalendarEvent).join(CalendarCal).filter(
                CalendarEvent.owner_id == account.id,
                CalendarCal.owner_id == account.id,
                CalendarEvent.dtstart < end_dt,
                CalendarEvent.dtend > start_dt,
                CalendarEvent.status != "cancelled",
            )
            calendar_filter = str(args.get("calendar") or "").strip()
            if calendar_filter:
                query = query.filter(
                    (CalendarEvent.calendar_id == calendar_filter)
                    | (func.lower(CalendarCal.name) == calendar_filter.lower())
                    | CalendarCal.id.startswith(calendar_filter, autoescape=True)
                )
            rows = query.order_by(CalendarEvent.dtstart, CalendarEvent.uid).all()
            events: list[dict[str, Any]] = []
            for event in rows:
                if event.all_day:
                    start = event.dtstart.date().isoformat()
                    end = event.dtend.date().isoformat()
                else:
                    start = _event_datetime(event, event.dtstart)
                    end = _event_datetime(event, event.dtend)
                events.append({
                    "uid": event.uid,
                    "version": int(event.version or 1),
                    "summary": event.summary or "",
                    "dtstart": start,
                    "dtend": end,
                    "all_day": bool(event.all_day),
                    "description": event.description or "",
                    "location": event.location or "",
                    "calendar": event.calendar.name if event.calendar else "",
                    "calendar_href": event.calendar_id,
                    "event_type": event.event_type or "",
                    "importance": event.importance or "normal",
                    "rrule": event.rrule or "",
                })
            if not events:
                response = (
                    f"No events between {start_dt.date().isoformat()} and "
                    f"{end_dt.date().isoformat()}."
                )
            else:
                lines = [
                    f"Found {len(events)} event(s) between "
                    f"{start_dt.date().isoformat()} and {end_dt.date().isoformat()}:"
                ]
                for event in events:
                    when = (
                        f"{event['dtstart']} (all day)"
                        if event["all_day"]
                        else f"{event['dtstart']} -> {event['dtend']}"
                    )
                    line = (
                        f"- {when}: [{event['summary']}](#event-{event['uid']}) "
                        f"(v{event['version']})"
                    )
                    if event["event_type"]:
                        line += f" #{event['event_type']}"
                    if event["importance"] != "normal":
                        line += f" !{event['importance']}"
                    if event["rrule"]:
                        line += f" repeats({event['rrule']})"
                    if event["location"]:
                        line += f" @ {event['location']}"
                    if event["calendar"]:
                        line += f" ({event['calendar']})"
                    if event["description"]:
                        description = event["description"].strip().replace("\n", " ")
                        if len(description) > 120:
                            description = description[:117] + "..."
                        line += f"\n    {description}"
                    lines.append(line)
                response = "\n".join(lines)
            return {"response": response, "events": events, "exit_code": 0}

        if action == "create_event":
            if _has_reminder_request(args):
                return _error(
                    "Calendar reminders are a separate explicit action and are not "
                    "supported inside manage_calendar yet; create the reminder with "
                    "manage_notes so it has its own review and lifecycle.",
                    reminder_requires_separate_action=True,
                )
            summary = args.get("summary")
            start_raw = _first_nonempty(args, "dtstart", "start", "start_time", "when")
            if not summary or not start_raw:
                return _error("summary and dtstart are required")
            account = ensure_account(db, owner_name)
            selector = args.get("calendar_href") or args.get("calendar")
            calendar = (
                _owned_calendar(db, owner_id=account.id, selector=selector)
                if selector
                else None
            )
            all_day = bool(args.get("all_day", False))
            start = _normalize_action_datetime(start_raw, all_day=all_day)
            end_raw = _first_nonempty(args, "dtend", "end", "end_time")
            end = (
                _normalize_action_datetime(end_raw, all_day=all_day)
                if end_raw
                else None
            )
            if end is None:
                duration = _parse_duration(args.get("duration"))
                if duration is not None:
                    end = _add_duration(start, duration, all_day=all_day)
            event_type = _first_nonempty(args, "event_type", "tag", "category", "type")
            payload: dict[str, Any] = {
                "summary": summary,
                "dtstart": start,
                "all_day": all_day,
                "description": args.get("description", "") or "",
                "location": args.get("location", "") or "",
                "rrule": args.get("rrule", "") or "",
                "importance": args.get("importance") or "normal",
            }
            if end is not None:
                payload["dtend"] = end
            if calendar is not None:
                payload["calendar_id"] = calendar.id
            if args.get("color") is not None:
                payload["color"] = args.get("color")
            if event_type is not None:
                payload["event_type"] = event_type
            if args.get("linked_entity_ids") is not None:
                payload["linked_entity_ids"] = args.get("linked_entity_ids")
            proposal, event, duplicate, _result_version = _execute_level_four(
                db,
                account=account,
                action="create_event",
                target_id=None,
                payload=payload,
                reason="Calendar event requested through the agent tool",
                explicit_idempotency_key=args.get("idempotency_key"),
            )
            db.commit()
            tag_blurb = f" [{event.event_type}]" if event.event_type else ""
            prefix = "Event already exists" if duplicate else "Created event"
            return {
                "response": (
                    f"{prefix} [{event.summary}](#event-{event.uid}){tag_blurb} "
                    f"on {start_raw} (v{int(event.version or 1)})"
                ),
                "uid": event.uid,
                "version": int(event.version or 1),
                "proposal_id": proposal.id,
                "proposal_version": int(proposal.version or 1),
                "anchor": f"[{event.summary}](#event-{event.uid})",
                "duplicate": duplicate,
                "exit_code": 0,
            }

        if action == "update_event":
            uid = _base_uid(args.get("uid"))
            version = _positive_version(
                args.get("version", args.get("expected_event_version"))
            )
            if _has_reminder_request(args):
                return _error(
                    "Calendar reminders are a separate explicit action and are not "
                    "supported inside manage_calendar yet; use manage_notes.",
                    reminder_requires_separate_action=True,
                )
            account = _read_account(db, owner_name)
            if account is None:
                return _error(f"Event {uid} not found")
            event = _owned_event(db, owner_id=account.id, uid=uid)
            if event is None:
                return _error(f"Event {uid} not found")
            metadata: dict[str, Any] = {
                field: args[field] for field in _METADATA_FIELDS if field in args
            }
            tag_key = next(
                (name for name in ("event_type", "tag", "category", "type") if name in args),
                None,
            )
            if tag_key is not None:
                metadata["event_type"] = args.get(tag_key)

            start_raw = _first_nonempty(args, "dtstart", "start", "start_time", "when")
            end_raw = _first_nonempty(args, "dtend", "end", "end_time")
            repeat_off = str(args.get("repeat") or "").strip().lower() in _REPEAT_OFF
            schedule_requested = bool(
                start_raw is not None
                or end_raw is not None
                or "all_day" in args
                or "rrule" in args
                or repeat_off
            )
            if not metadata and not schedule_requested:
                return _error("No supported calendar fields were provided to update")

            proposal_ids: list[str] = []
            proposal_versions: list[int] = []
            duplicate = False
            current_version = version
            if metadata:
                payload = {
                    "expected_event_version": current_version,
                    "changes": metadata,
                }
                if args.get("linked_entity_ids") is not None:
                    payload["linked_entity_ids"] = args.get("linked_entity_ids")
                proposal, event, reused, result_version = _execute_level_four(
                    db,
                    account=account,
                    action="update_event",
                    target_id=uid,
                    payload=payload,
                    reason="Calendar event metadata update requested through the agent tool",
                    explicit_idempotency_key=args.get("idempotency_key"),
                )
                proposal_ids.append(proposal.id)
                proposal_versions.append(int(proposal.version or 1))
                # Use the proposal's recorded result version, not the event's
                # current version. On an idempotent retry a later proposal may
                # already have advanced the row, while this exact action still
                # produced the earlier version needed by the next step.
                current_version = result_version
                duplicate = duplicate or reused

            if schedule_requested:
                target_all_day = (
                    args.get("all_day") if "all_day" in args else bool(event.all_day)
                )
                if not isinstance(target_all_day, bool):
                    raise ValueError("all_day must be a boolean")
                start = (
                    _normalize_action_datetime(start_raw, all_day=target_all_day)
                    if start_raw is not None
                    else _event_datetime(event, event.dtstart, all_day=target_all_day)
                )
                end: str | None
                if end_raw is not None:
                    end = _normalize_action_datetime(end_raw, all_day=target_all_day)
                elif "all_day" in args and target_all_day != bool(event.all_day):
                    end = None
                elif start_raw is not None:
                    duration = event.dtend - event.dtstart
                    end = _add_duration(start, duration, all_day=target_all_day)
                else:
                    end = _event_datetime(event, event.dtend, all_day=target_all_day)
                schedule_payload: dict[str, Any] = {
                    "expected_event_version": current_version,
                    "dtstart": start,
                }
                if end is not None:
                    schedule_payload["dtend"] = end
                if "all_day" in args:
                    schedule_payload["all_day"] = target_all_day
                if "rrule" in args:
                    schedule_payload["rrule"] = args.get("rrule") or ""
                elif repeat_off:
                    schedule_payload["rrule"] = ""
                schedule_key = args.get("idempotency_key")
                if schedule_key is not None and metadata:
                    schedule_key = f"{schedule_key}:reschedule"
                proposal, event, reused, _result_version = _execute_level_four(
                    db,
                    account=account,
                    action="reschedule_event",
                    target_id=uid,
                    payload=schedule_payload,
                    reason="Calendar event schedule update requested through the agent tool",
                    explicit_idempotency_key=schedule_key,
                )
                proposal_ids.append(proposal.id)
                proposal_versions.append(int(proposal.version or 1))
                duplicate = duplicate or reused

            db.commit()
            return {
                "response": f"Updated event {uid} (v{int(event.version or 1)})",
                "uid": uid,
                "version": int(event.version or 1),
                "proposal_id": proposal_ids[0],
                "proposal_ids": proposal_ids,
                "proposal_versions": proposal_versions,
                "duplicate": duplicate,
                "exit_code": 0,
            }

        if action == "cancel_event":
            uid = _base_uid(args.get("uid"))
            version = _positive_version(
                args.get("version", args.get("expected_event_version"))
            )
            account = _read_account(db, owner_name)
            event = (
                _owned_event(db, owner_id=account.id, uid=uid)
                if account is not None
                else None
            )
            if event is None:
                return _error(f"Event {uid} not found")
            current_version = int(event.version or 1)
            if current_version != version:
                raise ActionPolicyConflict(
                    "Calendar event changed in another client "
                    f"(current version {current_version})"
                )
            proposal, duplicate = _prepare_level_five_cancel(
                db,
                account=account,
                target_id=uid,
                expected_event_version=version,
                explicit_idempotency_key=args.get("idempotency_key"),
            )
            db.commit()
            return {
                "response": (
                    f"Proposed cancellation of [{event.summary}](#event-{uid}) "
                    f"at v{version}; human approval is required before execution."
                ),
                "uid": uid,
                "version": version,
                "proposal_id": proposal.id,
                "proposal_version": int(proposal.version or 1),
                "proposal_state": proposal.state,
                "requires_approval": True,
                "duplicate": duplicate,
                "exit_code": 0,
            }

        if action == "delete_event":
            uid = _base_uid(args.get("uid"))
            account = _read_account(db, owner_name)
            event = (
                _owned_event(db, owner_id=account.id, uid=uid)
                if account is not None
                else None
            )
            if event is None:
                return _error(f"Event {uid} not found")
            return _error(
                "Deleting calendar events is manual-only. Use cancel_event with "
                "the exact uid and version to prepare a reversible cancellation "
                "for human approval.",
                manual_required=True,
                uid=uid,
                version=int(event.version or 1),
            )

        return _error(
            f"Unknown action: {action}. Use list_events, create_event, "
            "update_event, cancel_event, delete_event, list_calendars"
        )
    except ActionPolicyConflict as exc:
        db.rollback()
        logger.warning(
            "manage_calendar rejected request (%s)", type(exc).__name__
        )
        return _error(exc, conflict=True)
    except ActionPolicyError as exc:
        db.rollback()
        logger.warning(
            "manage_calendar rejected request (%s)", type(exc).__name__
        )
        return _error(exc)
    except (CalendarServiceError, ValueError) as exc:
        db.rollback()
        logger.warning(
            "manage_calendar rejected request (%s)", type(exc).__name__
        )
        return _error(exc)
    except Exception as exc:
        db.rollback()
        # Never echo exception text or a traceback here: database drivers may
        # include connection strings or credentials in either one.
        logger.error("manage_calendar failed safely (%s)", type(exc).__name__)
        return _error("Calendar tool failed safely; no changes were committed")
    finally:
        db.close()
