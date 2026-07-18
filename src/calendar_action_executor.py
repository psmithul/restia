"""Server-owned execution and reversal for reversible calendar proposals.

The generic ActionPolicy lifecycle deliberately does not know how to mutate a
domain record.  This module is the reviewed dispatcher for the exact calendar
tuples Restia currently supports.  Proposal state, local calendar authority,
Life Graph projection, undo state, connector outbox, and audit rows are all
written through the caller's single SQL transaction; no network I/O occurs
here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from core.database import (
    Account,
    ActionProposal,
    CalendarActionUndo,
    CalendarEvent,
    EntityLink,
    LifeEntity,
    utcnow_naive,
)
from src.action_policy import (
    ActionPolicyConflict,
    ActionPolicyError,
    ActionPolicyNotFound,
    complete_action,
    get_action_proposal,
    reverse_action,
    start_action,
)
from src.calendar_service import (
    CalendarConflict,
    CalendarMutationResult,
    CalendarNotFound,
    CalendarServiceError,
    cancel_calendar_event,
    create_calendar_event,
    restore_calendar_event_snapshot,
    reschedule_calendar_event,
    snapshot_calendar_event,
    update_calendar_event,
)
from src.life_graph import LifeGraphConflict, LifeGraphError, delete_entity_link


CALENDAR_EXECUTION_TUPLES = frozenset({
    ("calendar", "create_event", "event"),
    ("calendar", "update_event", "event"),
    ("calendar", "reschedule_event", "event"),
    ("calendar", "cancel_event", "event"),
})

_CREATE_FIELDS = frozenset({
    "summary",
    "dtstart",
    "dtend",
    "all_day",
    "calendar_id",
    "description",
    "location",
    "rrule",
    "color",
    "importance",
    "event_type",
    "linked_entity_ids",
})
_UPDATE_FIELDS = frozenset({
    "expected_event_version",
    "changes",
    "linked_entity_ids",
})
_RESCHEDULE_FIELDS = frozenset({
    "expected_event_version",
    "dtstart",
    "dtend",
    "all_day",
    "rrule",
})
_CANCEL_FIELDS = frozenset({"expected_event_version"})
_MISSING = object()


@dataclass(frozen=True)
class CalendarActionExecution:
    proposal: ActionProposal
    undo: CalendarActionUndo
    mutation: CalendarMutationResult


def is_server_calendar_action(proposal: ActionProposal) -> bool:
    return (
        str(proposal.domain),
        str(proposal.action),
        str(proposal.target_type),
    ) in CALENDAR_EXECUTION_TUPLES


def _clean_payload(proposal: ActionProposal, allowed: frozenset[str]) -> dict[str, Any]:
    payload = proposal.payload or {}
    if not isinstance(payload, dict):
        raise ActionPolicyError("Calendar action payload must be an object")
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ActionPolicyError(
            "Unsupported calendar action fields: " + ", ".join(unknown)
        )
    return dict(payload)


def _required(payload: Mapping[str, Any], name: str) -> Any:
    value = payload.get(name, _MISSING)
    if value is _MISSING or value is None or value == "":
        raise ActionPolicyError(f"Calendar action payload requires {name}")
    return value


def _positive_version(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ActionPolicyError("expected_event_version must be a positive integer")
    return value


def _linked_entity_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
    raw = payload.get("linked_entity_ids", ())
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise ActionPolicyError("linked_entity_ids must be an array")
    values: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = str(item or "").strip()
        if not value or len(value) > 255:
            raise ActionPolicyError("linked_entity_ids contains an invalid entity ID")
        if value not in seen:
            seen.add(value)
            values.append(value)
    if len(values) > 50:
        raise ActionPolicyError("linked_entity_ids must not contain more than 50 IDs")
    return tuple(values)


def _base_uid(value: Any) -> str:
    uid = str(value or "").strip()
    if not uid:
        raise ActionPolicyError("Calendar action target_id is required")
    if len(uid) > 255:
        raise ActionPolicyError("Calendar event UID is too long")
    if "::" in uid:
        raise ActionPolicyError(
            "Calendar actions require a base event UID, not a recurrence occurrence"
        )
    return uid


def _owned_event(db, *, owner_id: str, uid: str) -> CalendarEvent:
    event = db.query(CalendarEvent).filter(
        CalendarEvent.uid == uid,
        CalendarEvent.owner_id == owner_id,
    ).first()
    if event is None:
        raise ActionPolicyNotFound("Calendar event not found")
    return event


def _owned_graph_entity(db, *, owner_id: str, entity_id: str) -> LifeEntity:
    entity = db.query(LifeEntity).filter(
        LifeEntity.id == entity_id,
        LifeEntity.owner_id == owner_id,
        LifeEntity.deleted_at.is_(None),
    ).first()
    if entity is None:
        raise ActionPolicyConflict("Calendar Life Graph projection is missing")
    return entity


def _stable_created_uid(proposal: ActionProposal) -> str:
    # A proposal ID is server-issued and immutable, so retries after a rolled
    # back transaction address the same CalDAV object instead of creating a
    # duplicate with a new random UID.
    return f"restia-action-{proposal.id}@restia.local"


def _new_undo(
    db,
    *,
    proposal: ActionProposal,
    event_uid: str,
    before_state: Mapping[str, Any] | None,
) -> CalendarActionUndo:
    # The encrypted undo schema classifies mutations as create/update/
    # reschedule. A cancellation is an update of the event's status; the
    # proposal retains the exact cancel_event action for risk/audit semantics.
    operation = (
        "update"
        if proposal.action == "cancel_event"
        else proposal.action.removesuffix("_event")
    )
    undo = CalendarActionUndo(
        id=str(uuid.uuid4()),
        owner_id=proposal.owner_id,
        proposal_id=proposal.id,
        event_uid=event_uid,
        operation=operation,
        before_state=dict(before_state or {}),
        result_event_version=None,
        life_entity_id=None,
        result_graph_version=None,
        created_link_ids={"ids": []},
        state="ready",
        version=1,
    )
    db.add(undo)
    db.flush()
    return undo


def _bind_undo_result(
    db,
    *,
    undo: CalendarActionUndo,
    mutation: CalendarMutationResult,
) -> None:
    undo.result_event_version = int(mutation.event_version)
    undo.life_entity_id = mutation.graph_entity.id
    undo.result_graph_version = int(mutation.graph_version)
    undo.created_link_ids = {
        "ids": [link.id for link in mutation.links_created],
    }
    db.flush()


def _result_payload(mutation: CalendarMutationResult) -> dict[str, Any]:
    delivery = mutation.delivery
    return {
        "event_uid": mutation.event.uid,
        "event_version": int(mutation.event_version),
        "life_entity_id": mutation.graph_entity.id,
        "life_entity_version": int(mutation.graph_version),
        "delivery_id": delivery.id if delivery is not None else None,
        "delivery_version": (
            int(mutation.delivery_version)
            if mutation.delivery_version is not None else None
        ),
    }


def _translate_calendar_error(exc: Exception) -> ActionPolicyError:
    if isinstance(exc, CalendarNotFound):
        return ActionPolicyNotFound(str(exc))
    if isinstance(exc, (CalendarConflict, LifeGraphConflict)):
        return ActionPolicyConflict(str(exc))
    if isinstance(exc, (CalendarServiceError, LifeGraphError)):
        return ActionPolicyError(str(exc))
    if isinstance(exc, ActionPolicyError):
        return exc
    return ActionPolicyError("Calendar action execution failed")


def execute_calendar_action(
    db,
    *,
    account: Account,
    proposal_id: object,
    expected_version: int,
) -> CalendarActionExecution:
    """Execute one exact registered calendar proposal inside the caller tx."""

    proposal = get_action_proposal(
        db, owner_id=account.id, proposal_id=proposal_id
    )
    if not is_server_calendar_action(proposal):
        raise ActionPolicyError("Action has no reviewed server executor")

    try:
        if proposal.action == "create_event":
            if proposal.target_id:
                raise ActionPolicyError(
                    "Calendar create target IDs are assigned by the server"
                )
            payload = _clean_payload(proposal, _CREATE_FIELDS)
            uid = _stable_created_uid(proposal)
            undo = _new_undo(
                db,
                proposal=proposal,
                event_uid=uid,
                before_state={"format": "restia.calendar.undo.v1", "event": None},
            )
            started = start_action(
                db,
                owner_id=account.id,
                proposal_id=proposal.id,
                expected_version=expected_version,
                undo_ref=undo.id,
            )
            mutation = create_calendar_event(
                db,
                account=account,
                uid=uid,
                summary=_required(payload, "summary"),
                dtstart=_required(payload, "dtstart"),
                dtend=payload.get("dtend"),
                all_day=payload.get("all_day", False),
                calendar_id=payload.get("calendar_id"),
                description=payload.get("description", ""),
                location=payload.get("location", ""),
                rrule=payload.get("rrule", ""),
                color=payload.get("color"),
                importance=payload.get("importance", "normal"),
                event_type=payload.get("event_type"),
                linked_entity_ids=_linked_entity_ids(payload),
                idempotency_key=f"calendar-action:{proposal.id}:execute",
                proposal_id=proposal.id,
            )
        else:
            uid = _base_uid(proposal.target_id)
            if proposal.action == "update_event":
                payload = _clean_payload(proposal, _UPDATE_FIELDS)
                changes = _required(payload, "changes")
                if not isinstance(changes, dict):
                    raise ActionPolicyError("changes must be an object")
                target_event_version = _positive_version(
                    _required(payload, "expected_event_version")
                )
                linked_ids = _linked_entity_ids(payload)
            elif proposal.action == "reschedule_event":
                payload = _clean_payload(proposal, _RESCHEDULE_FIELDS)
                target_event_version = _positive_version(
                    _required(payload, "expected_event_version")
                )
                _required(payload, "dtstart")
            else:
                payload = _clean_payload(proposal, _CANCEL_FIELDS)
                target_event_version = _positive_version(
                    _required(payload, "expected_event_version")
                )
            event = _owned_event(db, owner_id=account.id, uid=uid)
            before = {
                "format": "restia.calendar.undo.v1",
                "event": snapshot_calendar_event(event),
            }
            undo = _new_undo(
                db,
                proposal=proposal,
                event_uid=uid,
                before_state=before,
            )
            started = start_action(
                db,
                owner_id=account.id,
                proposal_id=proposal.id,
                expected_version=expected_version,
                undo_ref=undo.id,
            )
            if proposal.action == "update_event":
                mutation = update_calendar_event(
                    db,
                    account=account,
                    uid=uid,
                    expected_version=target_event_version,
                    changes=changes,
                    linked_entity_ids=linked_ids,
                    idempotency_key=f"calendar-action:{proposal.id}:execute",
                    proposal_id=proposal.id,
                )
            elif proposal.action == "reschedule_event":
                kwargs: dict[str, Any] = {
                    "db": db,
                    "account": account,
                    "uid": uid,
                    "expected_version": target_event_version,
                    "dtstart": _required(payload, "dtstart"),
                    "dtend": payload.get("dtend"),
                    "idempotency_key": f"calendar-action:{proposal.id}:execute",
                    "proposal_id": proposal.id,
                }
                if "all_day" in payload:
                    kwargs["all_day"] = payload["all_day"]
                if "rrule" in payload:
                    kwargs["rrule"] = payload["rrule"]
                mutation = reschedule_calendar_event(**kwargs)
            else:
                mutation = cancel_calendar_event(
                    db,
                    account=account,
                    uid=uid,
                    expected_version=target_event_version,
                    idempotency_key=f"calendar-action:{proposal.id}:execute",
                    proposal_id=proposal.id,
                )

        _bind_undo_result(db, undo=undo, mutation=mutation)
        completed = complete_action(
            db,
            owner_id=account.id,
            proposal_id=proposal.id,
            expected_version=int(started.version),
            result=_result_payload(mutation),
            undo_ref=undo.id,
        )
        return CalendarActionExecution(completed, undo, mutation)
    except Exception as exc:
        translated = _translate_calendar_error(exc)
        if translated is exc:
            raise
        raise translated from exc


def _owned_undo(
    db,
    *,
    proposal: ActionProposal,
) -> CalendarActionUndo:
    undo = db.query(CalendarActionUndo).filter(
        CalendarActionUndo.id == proposal.undo_ref,
        CalendarActionUndo.owner_id == proposal.owner_id,
        CalendarActionUndo.proposal_id == proposal.id,
    ).first()
    if undo is None:
        raise ActionPolicyConflict("Calendar reversal record is missing")
    return undo


def _reserve_undo(db, undo: CalendarActionUndo) -> None:
    current = int(undo.version or 1)
    if undo.state != "ready":
        raise ActionPolicyConflict("Calendar action has already been reversed")
    used_at = utcnow_naive()
    updated = db.query(CalendarActionUndo).filter(
        CalendarActionUndo.id == undo.id,
        CalendarActionUndo.owner_id == undo.owner_id,
        CalendarActionUndo.version == current,
        CalendarActionUndo.state == "ready",
    ).update(
        {
            CalendarActionUndo.state: "used",
            CalendarActionUndo.used_at: used_at,
            CalendarActionUndo.version: current + 1,
            CalendarActionUndo.updated_at: used_at,
        },
        synchronize_session=False,
    )
    if updated != 1:
        db.expire_all()
        raise ActionPolicyConflict("Calendar action reversal was claimed elsewhere")
    db.expire(undo)
    db.refresh(undo)


def _delete_action_links(db, *, owner_id: str, link_ids: Any) -> None:
    if not isinstance(link_ids, dict) or set(link_ids) != {"ids"}:
        raise ActionPolicyConflict("Calendar reversal link record is invalid")
    values = link_ids.get("ids")
    if not isinstance(values, list):
        raise ActionPolicyConflict("Calendar reversal link record is invalid")
    for raw_id in values:
        link = db.query(EntityLink).filter(
            EntityLink.id == str(raw_id),
            EntityLink.owner_id == owner_id,
            EntityLink.source_type == "life_entity",
            EntityLink.target_type == "life_entity",
        ).first()
        if link is None:
            raise ActionPolicyConflict("Calendar reversal link is missing")
        if link.deleted_at is None:
            delete_entity_link(
                db,
                owner_id=owner_id,
                link_id=link.id,
                expected_version=int(link.version or 1),
                reason="Calendar action reversal removed its created link",
            )


def reverse_calendar_action(
    db,
    *,
    account: Account,
    proposal_id: object,
    expected_version: int,
    confirmation_token: object | None = None,
) -> CalendarActionExecution:
    """Reverse one completed calendar action with event/graph/undo CAS fences."""

    proposal = get_action_proposal(
        db, owner_id=account.id, proposal_id=proposal_id
    )
    if not is_server_calendar_action(proposal):
        raise ActionPolicyError("Action has no reviewed server reversal")
    if type(expected_version) is not int or expected_version < 1:
        raise ActionPolicyError("version must be a positive integer")
    if int(proposal.version or 1) != expected_version:
        raise ActionPolicyConflict(
            f"Action proposal changed in another client (current version {int(proposal.version or 1)})"
        )
    if proposal.state != "completed":
        raise ActionPolicyConflict(
            f"Action proposal is {proposal.state}; expected completed"
        )

    try:
        undo = _owned_undo(db, proposal=proposal)
        event = _owned_event(db, owner_id=account.id, uid=undo.event_uid)
        if int(event.version or 1) != int(undo.result_event_version or 0):
            raise ActionPolicyConflict(
                "Calendar event changed after this action; reversal requires review"
            )
        if not undo.life_entity_id or not undo.result_graph_version:
            raise ActionPolicyConflict("Calendar reversal projection record is incomplete")
        graph = _owned_graph_entity(
            db, owner_id=account.id, entity_id=undo.life_entity_id
        )
        if int(graph.version or 1) != int(undo.result_graph_version):
            raise ActionPolicyConflict(
                "Calendar Life Graph projection changed after this action"
            )
        _reserve_undo(db, undo)

        if undo.operation == "create":
            mutation = cancel_calendar_event(
                db,
                account=account,
                uid=undo.event_uid,
                expected_version=int(undo.result_event_version),
                idempotency_key=f"calendar-action:{proposal.id}:reverse",
                proposal_id=proposal.id,
            )
        elif undo.operation in {"update", "reschedule"}:
            before = undo.before_state or {}
            if not isinstance(before, dict) or before.get("format") != "restia.calendar.undo.v1":
                raise ActionPolicyConflict("Calendar reversal snapshot is invalid")
            snapshot = before.get("event")
            if not isinstance(snapshot, dict):
                raise ActionPolicyConflict("Calendar reversal snapshot is missing")
            mutation = restore_calendar_event_snapshot(
                db,
                account=account,
                uid=undo.event_uid,
                expected_version=int(undo.result_event_version),
                snapshot=snapshot,
                idempotency_key=f"calendar-action:{proposal.id}:reverse",
                proposal_id=proposal.id,
            )
        else:
            raise ActionPolicyConflict("Calendar reversal operation is invalid")

        _delete_action_links(
            db,
            owner_id=account.id,
            link_ids=undo.created_link_ids or {"ids": []},
        )
        reversed_proposal = reverse_action(
            db,
            owner_id=account.id,
            proposal_id=proposal.id,
            expected_version=expected_version,
            result={"reversed": True, **_result_payload(mutation)},
            confirmation_token=confirmation_token,
        )
        return CalendarActionExecution(reversed_proposal, undo, mutation)
    except Exception as exc:
        translated = _translate_calendar_error(exc)
        if translated is exc:
            raise
        raise translated from exc


__all__ = [
    "CALENDAR_EXECUTION_TUPLES",
    "CalendarActionExecution",
    "execute_calendar_action",
    "is_server_calendar_action",
    "reverse_calendar_action",
]
