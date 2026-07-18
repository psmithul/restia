"""The model calendar tool must stay behind V3 identity and action authority."""

from __future__ import annotations

import json
import uuid

import pytest

import core.database as cdb
from core.database import (
    Account,
    ActionPolicy,
    ActionProposal,
    CalendarActionUndo,
    CalendarEvent,
    LifeEntity,
    Note,
)
from tests.helpers.sqlite_db import make_temp_sqlite


_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)


@pytest.fixture(autouse=True)
def _bind_temp_db(monkeypatch):
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    yield


def _owner(prefix: str = "calendar-tool") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _create(owner: str, **overrides):
    from src.tool_implementations import do_manage_calendar

    payload = {
        "action": "create_event",
        "summary": "Design review",
        "dtstart": "2126-08-03T10:00:00Z",
        "dtend": "2126-08-03T11:00:00Z",
        **overrides,
    }
    return await do_manage_calendar(json.dumps(payload), owner=owner)


async def test_create_is_owned_completed_level_four_and_reversible():
    owner = _owner()
    result = await _create(owner)
    assert result["exit_code"] == 0, result
    assert result["version"] == 1

    db = _TS()
    try:
        account = db.query(Account).filter(Account.username == owner).one()
        event = db.query(CalendarEvent).filter(
            CalendarEvent.uid == result["uid"],
            CalendarEvent.owner_id == account.id,
        ).one()
        proposal = db.query(ActionProposal).filter(
            ActionProposal.id == result["proposal_id"],
            ActionProposal.owner_id == account.id,
        ).one()
        assert event.version == 1
        assert proposal.state == "completed"
        assert proposal.autonomy_level == 4
        assert proposal.domain == "calendar"
        assert proposal.action == "create_event"
        assert proposal.target_type == "event"
        assert proposal.target_id is None
        assert proposal.external is False
        assert proposal.requires_confirmation is False
        assert proposal.undo_ref
        assert db.query(CalendarActionUndo).filter(
            CalendarActionUndo.id == proposal.undo_ref,
            CalendarActionUndo.owner_id == account.id,
        ).count() == 1
        assert db.query(LifeEntity).filter(
            LifeEntity.owner_id == account.id,
            LifeEntity.domain_ref_type == "calendar_event",
            LifeEntity.domain_ref_id == event.uid,
        ).count() == 1
        assert db.query(Note).filter(Note.owner == owner).count() == 0
    finally:
        db.close()


async def test_repeated_create_reuses_completed_proposal_and_event():
    owner = _owner("idempotent")
    first = await _create(owner)
    second = await _create(owner)
    assert first["exit_code"] == second["exit_code"] == 0
    assert second["duplicate"] is True
    assert second["uid"] == first["uid"]
    assert second["proposal_id"] == first["proposal_id"]

    db = _TS()
    try:
        account = db.query(Account).filter(Account.username == owner).one()
        assert db.query(CalendarEvent).filter(
            CalendarEvent.owner_id == account.id
        ).count() == 1
        assert db.query(ActionProposal).filter(
            ActionProposal.owner_id == account.id
        ).count() == 1
    finally:
        db.close()


async def test_reads_are_account_id_scoped_and_expose_versions():
    from src.tool_implementations import do_manage_calendar

    alice = _owner("alice")
    bob = _owner("bob")
    alice_event = await _create(alice, summary="Alice only")
    bob_event = await _create(bob, summary="Bob only")
    assert alice_event["exit_code"] == bob_event["exit_code"] == 0

    listed = await do_manage_calendar(json.dumps({
        "action": "list_events",
        "start": "2126-08-03T00:00:00Z",
        "end": "2126-08-04T00:00:00Z",
    }), owner=alice)
    assert listed["exit_code"] == 0, listed
    assert [(row["uid"], row["version"]) for row in listed["events"]] == [
        (alice_event["uid"], 1)
    ]
    assert "(v1)" in listed["response"]
    assert bob_event["uid"] not in listed["response"]


async def test_update_requires_current_version_and_uses_exact_dispatchers():
    from src.tool_implementations import do_manage_calendar

    owner = _owner("update")
    created = await _create(owner)
    missing = await do_manage_calendar(json.dumps({
        "action": "update_event",
        "uid": created["uid"],
        "summary": "Changed",
    }), owner=owner)
    assert missing["exit_code"] == 1
    assert "version is required" in missing["error"]

    updated = await do_manage_calendar(json.dumps({
        "action": "update_event",
        "uid": created["uid"],
        "version": created["version"],
        "summary": "Changed",
        "dtstart": "2126-08-03T12:00:00Z",
        "dtend": "2126-08-03T13:00:00Z",
    }), owner=owner)
    assert updated["exit_code"] == 0, updated
    assert updated["version"] == 3
    assert len(updated["proposal_ids"]) == 2

    retried = await do_manage_calendar(json.dumps({
        "action": "update_event",
        "uid": created["uid"],
        "version": created["version"],
        "summary": "Changed",
        "dtstart": "2126-08-03T12:00:00Z",
        "dtend": "2126-08-03T13:00:00Z",
    }), owner=owner)
    assert retried["exit_code"] == 0, retried
    assert retried["duplicate"] is True
    assert retried["version"] == 3
    assert retried["proposal_ids"] == updated["proposal_ids"]

    db = _TS()
    try:
        account = db.query(Account).filter(Account.username == owner).one()
        event = db.query(CalendarEvent).filter(
            CalendarEvent.uid == created["uid"],
            CalendarEvent.owner_id == account.id,
        ).one()
        assert event.summary == "Changed"
        assert event.dtstart.hour == 12
        proposals = db.query(ActionProposal).filter(
            ActionProposal.owner_id == account.id,
            ActionProposal.id.in_(updated["proposal_ids"]),
        ).all()
        assert {row.action for row in proposals} == {
            "update_event", "reschedule_event"
        }
        assert {row.state for row in proposals} == {"completed"}
        assert db.query(ActionProposal).filter(
            ActionProposal.owner_id == account.id
        ).count() == 3  # create + metadata update + reschedule
    finally:
        db.close()


async def test_stale_update_and_delete_fail_without_mutation_or_proposal():
    from src.tool_implementations import do_manage_calendar

    owner = _owner("fail-closed")
    created = await _create(owner)
    stale = await do_manage_calendar(json.dumps({
        "action": "update_event",
        "uid": created["uid"],
        "version": 99,
        "summary": "Must not persist",
    }), owner=owner)
    assert stale["exit_code"] == 1
    assert stale["conflict"] is True

    deletion = await do_manage_calendar(json.dumps({
        "action": "delete_event",
        "uid": created["uid"],
    }), owner=owner)
    assert deletion["exit_code"] == 1
    assert deletion["manual_required"] is True

    db = _TS()
    try:
        account = db.query(Account).filter(Account.username == owner).one()
        event = db.query(CalendarEvent).filter(
            CalendarEvent.uid == created["uid"],
            CalendarEvent.owner_id == account.id,
        ).one()
        assert event.summary == "Design review"
        assert event.status == "confirmed"
        assert event.version == 1
        proposals = db.query(ActionProposal).filter(
            ActionProposal.owner_id == account.id
        ).all()
        assert [(row.action, row.state) for row in proposals] == [
            ("create_event", "completed")
        ]
    finally:
        db.close()


async def test_cancel_is_exact_pending_idempotent_and_never_leaks_a_token():
    from src.tool_implementations import do_manage_calendar

    owner = _owner("cancel-proposal")
    created = await _create(owner, summary="Review to cancel")

    missing = await do_manage_calendar(json.dumps({
        "action": "cancel_event",
        "uid": created["uid"],
    }), owner=owner)
    assert missing["exit_code"] == 1
    assert "version is required" in missing["error"]

    stale = await do_manage_calendar(json.dumps({
        "action": "cancel_event",
        "uid": created["uid"],
        "version": created["version"] + 10,
        "idempotency_key": "email-source-cancel",
    }), owner=owner)
    assert stale["exit_code"] == 1
    assert stale["conflict"] is True

    request = {
        "action": "cancel_event",
        "uid": created["uid"],
        "version": created["version"],
        "idempotency_key": "email-source-cancel",
    }
    first = await do_manage_calendar(json.dumps(request), owner=owner)
    second = await do_manage_calendar(json.dumps(request), owner=owner)
    assert first["exit_code"] == second["exit_code"] == 0
    assert first["proposal_state"] == "prepared"
    assert first["requires_approval"] is True
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["proposal_id"] == first["proposal_id"]
    rendered = json.dumps([first, second], sort_keys=True)
    assert "confirmation_token" not in rendered
    assert "confirmation_digest" not in rendered

    db = _TS()
    try:
        account = db.query(Account).filter(Account.username == owner).one()
        event = db.query(CalendarEvent).filter(
            CalendarEvent.uid == created["uid"],
            CalendarEvent.owner_id == account.id,
        ).one()
        proposal = db.query(ActionProposal).filter(
            ActionProposal.id == first["proposal_id"],
            ActionProposal.owner_id == account.id,
        ).one()
        assert event.status == "confirmed"
        assert event.version == created["version"]
        assert proposal.action == "cancel_event"
        assert proposal.autonomy_level == 5
        assert proposal.external is True
        assert proposal.requires_confirmation is True
        assert proposal.state == "prepared"
        assert db.query(ActionProposal).filter(
            ActionProposal.owner_id == account.id,
            ActionProposal.action == "cancel_event",
        ).count() == 1
        assert db.query(CalendarActionUndo).filter(
            CalendarActionUndo.proposal_id == proposal.id,
        ).count() == 0
    finally:
        db.close()


async def test_cancel_is_owner_scoped_and_delete_remains_fail_closed():
    from src.tool_implementations import do_manage_calendar

    alice = _owner("cancel-alice")
    bob = _owner("cancel-bob")
    alice_event = await _create(alice, summary="Alice private")
    await _create(bob, summary="Bob private")

    cross_owner = await do_manage_calendar(json.dumps({
        "action": "cancel_event",
        "uid": alice_event["uid"],
        "version": alice_event["version"],
        "idempotency_key": "cross-owner-cancel",
    }), owner=bob)
    assert cross_owner["exit_code"] == 1
    assert "not found" in cross_owner["error"]

    deletion = await do_manage_calendar(json.dumps({
        "action": "delete_event",
        "uid": alice_event["uid"],
    }), owner=alice)
    assert deletion["exit_code"] == 1
    assert deletion["manual_required"] is True
    assert "Use cancel_event" in deletion["error"]

    db = _TS()
    try:
        alice_account = db.query(Account).filter(Account.username == alice).one()
        bob_account = db.query(Account).filter(Account.username == bob).one()
        assert db.query(CalendarEvent).filter(
            CalendarEvent.uid == alice_event["uid"],
            CalendarEvent.owner_id == alice_account.id,
            CalendarEvent.status == "confirmed",
        ).count() == 1
        assert db.query(ActionProposal).filter(
            ActionProposal.owner_id == bob_account.id,
            ActionProposal.action == "cancel_event",
        ).count() == 0
    finally:
        db.close()


async def test_tight_policy_prevents_tool_event_and_rolls_back_proposal():
    from src.identity import ensure_account

    owner = _owner("policy")
    db = _TS()
    try:
        account = ensure_account(db, owner)
        db.add(ActionPolicy(
            id=str(uuid.uuid4()),
            owner_id=account.id,
            domain="calendar",
            max_autonomy=3,
            external_requires_confirmation=True,
            enabled=True,
            rules={},
            version=1,
        ))
        db.commit()
        owner_id = account.id
    finally:
        db.close()

    result = await _create(owner)
    assert result["exit_code"] == 1
    assert "exceeds" in result["error"]

    db = _TS()
    try:
        assert db.query(CalendarEvent).filter(
            CalendarEvent.owner_id == owner_id
        ).count() == 0
        assert db.query(ActionProposal).filter(
            ActionProposal.owner_id == owner_id
        ).count() == 0
    finally:
        db.close()


async def test_missing_owner_and_cross_owner_calendar_selector_fail_closed():
    from src.tool_implementations import do_manage_calendar

    missing = await do_manage_calendar(json.dumps({
        "action": "list_events",
    }), owner=None)
    assert missing["exit_code"] == 1
    assert "authenticated calendar owner" in missing["error"]

    bob = _owner("selector-bob")
    bob_result = await _create(bob)
    db = _TS()
    try:
        bob_event = db.query(CalendarEvent).filter(
            CalendarEvent.uid == bob_result["uid"]
        ).one()
        bob_calendar_id = bob_event.calendar_id
    finally:
        db.close()

    alice = _owner("selector-alice")
    blocked = await _create(alice, calendar_href=bob_calendar_id)
    assert blocked["exit_code"] == 1
    assert blocked["error"] == "Calendar not found"
    db = _TS()
    try:
        # Account first-touch and all calendar-domain rows share the same outer
        # SQLite transaction and therefore roll back together.
        assert db.query(Account).filter(Account.username == alice).count() == 0
    finally:
        db.close()


async def test_unexpected_database_errors_are_not_returned_to_the_model(
    monkeypatch, caplog
):
    from src.tool_implementations import do_manage_calendar

    class ExplodingSession:
        def query(self, _model):
            raise RuntimeError("postgresql://admin:super-secret@db.internal/restia")

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(cdb, "SessionLocal", ExplodingSession)
    result = await do_manage_calendar(json.dumps({
        "action": "list_events",
        "start": "2126-08-03T00:00:00Z",
        "end": "2126-08-04T00:00:00Z",
    }), owner=_owner("safe-error"))
    assert result == {
        "error": "Calendar tool failed safely; no changes were committed",
        "exit_code": 1,
    }
    assert "secret" not in result["error"]
    assert all("secret" not in record.getMessage() for record in caplog.records)
