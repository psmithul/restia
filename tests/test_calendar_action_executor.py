from __future__ import annotations

import json
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    ActionProposal,
    CalendarActionUndo,
    CalendarCal,
    CalendarEvent,
    LifeEntity,
    Base,
)
from routes.action_policy_routes import ExecuteBody, ReverseBody
from src.action_policy import (
    ActionPolicyError,
    ActionPolicyNotFound,
    approve_action,
    create_action_proposal,
    issue_confirmation,
)
from src.audit_context import SESSION_AUDIT_CONTEXT_KEY
from src.calendar_action_executor import (
    execute_calendar_action,
    reverse_calendar_action,
)
from src.calendar_service import (
    CalendarServiceError,
    create_calendar_event,
    snapshot_calendar_event,
    update_calendar_event,
)


@pytest.fixture()
def calendar_action_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'calendar-actions.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    alice = Account(id=str(uuid.uuid4()), username="alice")
    bob = Account(id=str(uuid.uuid4()), username="bob")
    db.add_all([alice, bob])
    db.flush()
    alice_calendar = CalendarCal(
        id="calendar-alice",
        owner_id=alice.id,
        owner="alice",
        name="Alice",
        source="local",
        config_version=1,
    )
    bob_calendar = CalendarCal(
        id="calendar-bob",
        owner_id=bob.id,
        owner="bob",
        name="Bob",
        source="local",
        config_version=1,
    )
    db.add_all([alice_calendar, bob_calendar])
    db.commit()
    try:
        yield SimpleNamespace(
            engine=engine,
            Session=factory,
            db=db,
            alice=alice,
            bob=bob,
            alice_calendar=alice_calendar,
            bob_calendar=bob_calendar,
        )
    finally:
        db.close()
        engine.dispose()


def _proposal(env, *, action: str, target_id=None, payload=None, key=None):
    return create_action_proposal(
        env.db,
        owner_id=env.alice.id,
        domain="calendar",
        action=action,
        autonomy_level=4,
        target_type="event",
        target_id=target_id,
        payload=payload or {},
        reason="Calendar action test",
        sources={"kind": "test"},
        external=False,
        idempotency_key=key or f"{action}:{uuid.uuid4()}",
    ).proposal


def _create_payload(**changes):
    payload = {
        "calendar_id": "calendar-alice",
        "summary": "Design review",
        "dtstart": "2026-07-21T09:00:00",
        "dtend": "2026-07-21T10:00:00",
        "description": "Review the controller design",
    }
    payload.update(changes)
    return payload


def test_create_and_reverse_are_server_owned_and_version_fenced(calendar_action_env):
    env = calendar_action_env
    proposal = _proposal(
        env,
        action="create_event",
        payload=_create_payload(),
        key="calendar-create",
    )
    execution = execute_calendar_action(
        env.db,
        account=env.alice,
        proposal_id=proposal.id,
        expected_version=proposal.version,
    )
    env.db.commit()

    assert execution.proposal.state == "completed"
    assert execution.proposal.version == 3
    assert execution.proposal.undo_ref == execution.undo.id
    assert execution.undo.state == "ready"
    assert execution.undo.operation == "create"
    assert execution.undo.before_state["event"] is None
    assert execution.undo.created_link_ids == {"ids": []}
    event = env.db.query(CalendarEvent).filter_by(uid=execution.mutation.event.uid).one()
    graph = env.db.query(LifeEntity).filter_by(
        owner_id=env.alice.id,
        domain_ref_type="calendar_event",
        domain_ref_id=event.uid,
    ).one()
    assert event.owner_id == env.alice.id
    assert event.status == "confirmed"
    assert execution.undo.result_event_version == event.version
    assert execution.undo.life_entity_id == graph.id
    assert execution.undo.result_graph_version == graph.version

    reversal = reverse_calendar_action(
        env.db,
        account=env.alice,
        proposal_id=proposal.id,
        expected_version=execution.proposal.version,
    )
    env.db.commit()

    assert reversal.proposal.state == "reversed"
    assert reversal.undo.state == "used"
    assert reversal.undo.version == 2
    env.db.refresh(event)
    env.db.refresh(graph)
    assert event.status == "cancelled"
    assert graph.status == "cancelled"


def test_update_reversal_replays_one_exact_encrypted_snapshot(calendar_action_env):
    env = calendar_action_env
    original = create_calendar_event(
        env.db,
        account=env.alice,
        uid="existing-event",
        calendar_id=env.alice_calendar.id,
        summary="Original title",
        description="Original description",
        dtstart=datetime(2026, 7, 21, 11),
        dtend=datetime(2026, 7, 21, 12),
        idempotency_key="seed-existing-event",
    )
    env.db.commit()

    proposal = _proposal(
        env,
        action="update_event",
        target_id=original.event.uid,
        payload={
            "expected_event_version": original.event_version,
            "changes": {
                "summary": "Updated title",
                "description": "Updated description",
            },
        },
        key="calendar-update",
    )
    execution = execute_calendar_action(
        env.db,
        account=env.alice,
        proposal_id=proposal.id,
        expected_version=proposal.version,
    )
    env.db.commit()
    assert execution.mutation.event.summary == "Updated title"
    assert execution.undo.before_state["event"]["summary"] == "Original title"

    reversed_action = reverse_calendar_action(
        env.db,
        account=env.alice,
        proposal_id=proposal.id,
        expected_version=execution.proposal.version,
    )
    env.db.commit()
    assert reversed_action.mutation.event.summary == "Original title"
    assert reversed_action.mutation.event.description == "Original description"
    assert reversed_action.mutation.event.version == execution.mutation.event_version + 1

    with env.engine.connect() as connection:
        stored = connection.execute(text(
            "SELECT before_state, created_link_ids "
            "FROM calendar_action_undos WHERE id = :id"
        ), {"id": execution.undo.id}).one()
    rendered = json.dumps(stored, default=str)
    assert "Original title" not in rendered
    assert "enc:c1:" in rendered


def test_cross_owner_target_fails_without_leaving_undo_or_execution_state(
    calendar_action_env,
):
    env = calendar_action_env
    env.db.add(CalendarEvent(
        uid="bob-private",
        owner_id=env.bob.id,
        calendar_id=env.bob_calendar.id,
        summary="Bob private event",
        dtstart=datetime(2026, 7, 21, 13),
        dtend=datetime(2026, 7, 21, 14),
        version=1,
    ))
    env.db.commit()
    proposal = _proposal(
        env,
        action="update_event",
        target_id="bob-private",
        payload={
            "expected_event_version": 1,
            "changes": {"summary": "Cross owner"},
        },
        key="cross-owner-update",
    )
    env.db.commit()

    with pytest.raises(ActionPolicyNotFound, match="Calendar event"):
        execute_calendar_action(
            env.db,
            account=env.alice,
            proposal_id=proposal.id,
            expected_version=proposal.version,
        )
    env.db.rollback()
    persisted = env.db.query(ActionProposal).filter_by(id=proposal.id).one()
    assert persisted.state == "prepared"
    assert env.db.query(CalendarActionUndo).filter_by(proposal_id=proposal.id).count() == 0


def test_failure_after_domain_mutation_rolls_back_every_authority_row(
    calendar_action_env, monkeypatch
):
    env = calendar_action_env
    proposal = _proposal(
        env,
        action="create_event",
        payload=_create_payload(summary="Rollback test"),
        key="rollback-create",
    )
    env.db.commit()

    import src.calendar_action_executor as executor

    real_create = executor.create_calendar_event

    def mutate_then_fail(*args, **kwargs):
        real_create(*args, **kwargs)
        raise CalendarServiceError("injected failure")

    monkeypatch.setattr(executor, "create_calendar_event", mutate_then_fail)
    with pytest.raises(ActionPolicyError, match="injected failure"):
        execute_calendar_action(
            env.db,
            account=env.alice,
            proposal_id=proposal.id,
            expected_version=proposal.version,
        )
    env.db.rollback()

    persisted = env.db.query(ActionProposal).filter_by(id=proposal.id).one()
    assert persisted.state == "prepared"
    assert persisted.undo_ref is None
    assert env.db.query(CalendarActionUndo).filter_by(proposal_id=proposal.id).count() == 0
    assert env.db.query(CalendarEvent).filter(
        CalendarEvent.uid.like("restia-action-%")
    ).count() == 0
    assert env.db.query(LifeEntity).filter_by(
        owner_id=env.alice.id, domain_ref_type="calendar_event"
    ).count() == 0


def test_calendar_action_request_bodies_reject_client_undo_and_results():
    with pytest.raises(ValidationError):
        ExecuteBody.model_validate({"version": 1, "undo_ref": "client-made"})
    with pytest.raises(ValidationError):
        ReverseBody.model_validate({"version": 1, "result": {"fake": True}})


def test_cancel_requires_human_approval_then_executes_and_reverses_exact_snapshot(
    calendar_action_env,
):
    env = calendar_action_env
    original = create_calendar_event(
        env.db,
        account=env.alice,
        uid="approval-cancel-event",
        calendar_id=env.alice_calendar.id,
        summary="Customer review",
        description="Preserve  exact\nspacing",
        location="Room 5",
        dtstart=datetime(2026, 7, 24, 11),
        dtend=datetime(2026, 7, 24, 12),
        idempotency_key="seed-approval-cancel",
    )
    original_snapshot = snapshot_calendar_event(original.event)
    env.db.commit()

    creation = create_action_proposal(
        env.db,
        owner_id=env.alice.id,
        domain="calendar",
        action="cancel_event",
        autonomy_level=1,
        target_type="event",
        target_id=original.event.uid,
        payload={"expected_event_version": original.event_version},
        reason="Email says the appointment was cancelled",
        sources={"interface": "email"},
        external=False,
        idempotency_key="email-cancel-approval",
    )
    env.db.commit()
    assert creation.proposal.autonomy_level == 5
    assert creation.proposal.external is True
    assert creation.proposal.requires_confirmation is True
    assert creation.confirmation_token

    with pytest.raises(ActionPolicyError, match="expected one of: approved"):
        execute_calendar_action(
            env.db,
            account=env.alice,
            proposal_id=creation.proposal.id,
            expected_version=creation.proposal.version,
        )
    env.db.rollback()
    unchanged = env.db.query(CalendarEvent).filter_by(
        uid=original.event.uid, owner_id=env.alice.id
    ).one()
    assert unchanged.status == "confirmed"
    assert unchanged.version == original.event_version
    assert env.db.query(CalendarActionUndo).filter_by(
        proposal_id=creation.proposal.id
    ).count() == 0

    env.db.info[SESSION_AUDIT_CONTEXT_KEY] = {
        "actor_type": "account",
        "actor_id": env.alice.id,
        "credential_type": "session",
        "interface": "web",
    }
    proposal = env.db.query(ActionProposal).filter_by(
        id=creation.proposal.id, owner_id=env.alice.id
    ).one()
    approved = approve_action(
        env.db,
        owner_id=env.alice.id,
        proposal_id=proposal.id,
        expected_version=proposal.version,
        confirmation_token=creation.confirmation_token,
    )
    execution = execute_calendar_action(
        env.db,
        account=env.alice,
        proposal_id=approved.id,
        expected_version=approved.version,
    )
    env.db.commit()

    assert execution.proposal.state == "completed"
    assert execution.undo.operation == "update"
    assert execution.undo.before_state == {
        "format": "restia.calendar.undo.v1",
        "event": original_snapshot,
    }
    assert execution.mutation.event.status == "cancelled"
    assert execution.mutation.event.version == original.event_version + 1

    challenge = issue_confirmation(
        env.db,
        owner_id=env.alice.id,
        proposal_id=execution.proposal.id,
        expected_version=execution.proposal.version,
        purpose="reverse",
    )
    reversal = reverse_calendar_action(
        env.db,
        account=env.alice,
        proposal_id=challenge.proposal.id,
        expected_version=challenge.proposal.version,
        confirmation_token=challenge.confirmation_token,
    )
    env.db.commit()

    restored = snapshot_calendar_event(reversal.mutation.event)
    assert reversal.proposal.state == "reversed"
    assert restored["event_version"] == original_snapshot["event_version"] + 2
    assert {
        key: value for key, value in restored.items() if key != "event_version"
    } == {
        key: value for key, value in original_snapshot.items()
        if key != "event_version"
    }
    with env.engine.connect() as connection:
        encrypted = connection.execute(text(
            "SELECT before_state FROM calendar_action_undos WHERE id = :id"
        ), {"id": execution.undo.id}).scalar_one()
    assert "Customer review" not in str(encrypted)
    assert "enc:c1:" in str(encrypted)


def test_cancel_execution_rejects_stale_event_version_without_partial_state(
    calendar_action_env,
):
    env = calendar_action_env
    original = create_calendar_event(
        env.db,
        account=env.alice,
        uid="stale-cancel-event",
        calendar_id=env.alice_calendar.id,
        summary="Original",
        dtstart=datetime(2026, 7, 26, 11),
        dtend=datetime(2026, 7, 26, 12),
        idempotency_key="seed-stale-cancel",
    )
    env.db.commit()
    creation = create_action_proposal(
        env.db,
        owner_id=env.alice.id,
        domain="calendar",
        action="cancel_event",
        autonomy_level=5,
        target_type="event",
        target_id=original.event.uid,
        payload={"expected_event_version": original.event_version},
        external=True,
        idempotency_key="stale-cancel-proposal",
    )
    env.db.info[SESSION_AUDIT_CONTEXT_KEY] = {
        "actor_type": "account",
        "actor_id": env.alice.id,
        "credential_type": "session",
        "interface": "web",
    }
    approved = approve_action(
        env.db,
        owner_id=env.alice.id,
        proposal_id=creation.proposal.id,
        expected_version=creation.proposal.version,
        confirmation_token=creation.confirmation_token,
    )
    update_calendar_event(
        env.db,
        account=env.alice,
        uid=original.event.uid,
        expected_version=original.event_version,
        changes={"summary": "Changed concurrently"},
        idempotency_key="concurrent-edit-before-cancel",
    )
    env.db.commit()

    with pytest.raises(ActionPolicyError, match="changed; reload"):
        execute_calendar_action(
            env.db,
            account=env.alice,
            proposal_id=approved.id,
            expected_version=approved.version,
        )
    env.db.rollback()

    event = env.db.query(CalendarEvent).filter_by(
        uid=original.event.uid, owner_id=env.alice.id
    ).one()
    proposal = env.db.query(ActionProposal).filter_by(
        id=approved.id, owner_id=env.alice.id
    ).one()
    assert event.summary == "Changed concurrently"
    assert event.status == "confirmed"
    assert event.version == original.event_version + 1
    assert proposal.state == "approved"
    db_count = env.db.query(CalendarActionUndo).filter_by(
        proposal_id=proposal.id
    ).count()
    assert db_count == 0
