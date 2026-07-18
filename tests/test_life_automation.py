from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    ActionPolicy,
    ActionProposal,
    Base,
    EmailAccount,
    EmailOutboundDelivery,
    EmailOutboundDraft,
    LifeEntity,
    ScheduledTask,
)
from src.life_automation import (
    ACTION_TYPES,
    SEND_CHANNELS,
    TRIGGER_TYPES,
    AutomationConflict,
    AutomationError,
    AutomationNotFound,
    AutomationPolicyDenied,
    create_automation_definition,
    evaluate_automation,
    get_automation_definition,
    list_automation_definitions,
    prepare_automation_run,
    prepare_meeting_end_workflow,
    serialize_automation_definition,
    update_automation_definition,
)
from src.life_graph import create_life_entity, create_life_source


@pytest.fixture()
def automation_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'life-automation.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    alice = Account(id=str(uuid.uuid4()), username="alice")
    bob = Account(id=str(uuid.uuid4()), username="bob")
    db.add_all([alice, bob])
    db.commit()
    try:
        yield SimpleNamespace(
            engine=engine, Session=factory, db=db, alice=alice, bob=bob
        )
    finally:
        db.close()
        engine.dispose()


def _briefing_action(title: str = "Daily briefing") -> dict:
    return {
        "type": "briefing",
        "config": {"title": title, "sections": ["risks", "next_actions"]},
    }


def _definition(env, *, key="definition-1", trigger=None, actions=None, **overrides):
    values = {
        "account": env.alice,
        "name": "Bounded automation",
        "description": "Prepare a deterministic result",
        "trigger": trigger or {
            "type": "time",
            "config": {"schedule_key": "morning"},
        },
        "actions": actions or [_briefing_action()],
        "idempotency_key": key,
    }
    values.update(overrides)
    return create_automation_definition(env.db, **values)


def _source(env, account, suffix: str):
    return create_life_source(
        env.db,
        account=account,
        source_type="meeting_record",
        title=f"Meeting record {suffix}",
        safe_excerpt="Verified meeting notes",
        idempotency_key=f"source-{suffix}",
    )[0]


def _entity(env, account, entity_type: str, title: str, suffix: str):
    return create_life_entity(
        env.db,
        account=account,
        entity_type=entity_type,
        title=title,
        properties={"fixture": suffix},
        idempotency_key=f"entity-{suffix}",
    )[0]


def _email_account(env, account, suffix: str = "default") -> EmailAccount:
    row = EmailAccount(
        id=f"mail-{suffix}",
        owner=account.username,
        name="Test mailbox",
        is_default=True,
        enabled=True,
        imap_user=f"{account.username}@example.com",
        smtp_user=f"{account.username}@example.com",
        from_address=f"{account.username}@example.com",
    )
    env.db.add(row)
    env.db.flush()
    return row


def test_contract_covers_all_required_trigger_and_action_types():
    assert TRIGGER_TYPES == {
        "time",
        "email",
        "calendar",
        "overdue_task",
        "upload",
        "person",
        "metric_threshold",
        "location",
        "form",
        "project_status",
    }
    assert ACTION_TYPES == {
        "create_entity",
        "schedule",
        "database_update",
        "draft",
        "approved_send",
        "briefing",
        "file_move",
        "report",
        "information_request",
        "notification",
        "agent",
        "workflow",
    }
    assert "whatsapp" not in SEND_CHANNELS


def test_definition_is_account_owned_versioned_and_exactly_idempotent(automation_env):
    first, created = _definition(automation_env)
    second, created_again = _definition(automation_env)
    assert created is True
    assert created_again is False
    assert second.id == first.id
    assert second.idempotency_key.startswith("sha256:")
    assert "definition-1" not in second.idempotency_key

    rendered = serialize_automation_definition(first)
    assert rendered["enabled"] is True
    assert rendered["execution_contract"] == {
        "evaluation_side_effects": False,
        "classification_external_actions": False,
        "model_write_authority": False,
        "whatsapp_send": False,
        "prepared_domain_actions_require_typed_executor": True,
    }
    assert get_automation_definition(
        automation_env.db, owner_id=automation_env.alice.id,
        automation_id=first.id,
    ).id == first.id
    with pytest.raises(AutomationNotFound):
        get_automation_definition(
            automation_env.db, owner_id=automation_env.bob.id,
            automation_id=first.id,
        )
    alice_rows, truncated = list_automation_definitions(
        automation_env.db, owner_id=automation_env.alice.id
    )
    bob_rows, _ = list_automation_definitions(
        automation_env.db, owner_id=automation_env.bob.id
    )
    assert [row.id for row in alice_rows] == [first.id]
    assert truncated is False
    assert bob_rows == []

    with pytest.raises(AutomationConflict, match="idempotency"):
        _definition(automation_env, name="Different automation")


def test_definition_update_uses_cas_and_revalidates_actions(automation_env):
    definition, _ = _definition(
        automation_env,
        actions=[{
            "type": "agent",
            "config": {
                "role": "analysis", "objective": "Review cited facts",
                "output_type": "report",
            },
        }],
    )
    updated = update_automation_definition(
        automation_env.db,
        owner_id=automation_env.alice.id,
        automation_id=definition.id,
        expected_version=1,
        changes={"enabled": False, "name": "Paused automation"},
    )
    assert updated.version == 2
    assert updated.status == "paused"
    assert serialize_automation_definition(updated)["enabled"] is False
    assert serialize_automation_definition(updated)["actions"][0]["config"][
        "write_mode"
    ] == "none"
    with pytest.raises(AutomationConflict, match="current version"):
        update_automation_definition(
            automation_env.db,
            owner_id=automation_env.alice.id,
            automation_id=definition.id,
            expected_version=1,
            changes={"name": "Stale"},
        )


@pytest.mark.parametrize(
    ("trigger", "event"),
    [
        (
            {"type": "time", "config": {"schedule_key": "morning"}},
            {"type": "time", "schedule_key": "morning"},
        ),
        (
            {
                "type": "email",
                "config": {"from_domain": "example.com", "tags_any": ["urgent"]},
            },
            {
                "type": "email", "message_id": "msg-1",
                "from_email": "person@example.com", "subject": "Hello",
                "tags": ["urgent"],
            },
        ),
        (
            {
                "type": "calendar",
                "config": {"event": "meeting_ended", "event_id": "meeting-1"},
            },
            {
                "type": "calendar", "event": "meeting_ended",
                "event_id": "meeting-1", "title": "Review",
            },
        ),
        (
            {
                "type": "overdue_task",
                "config": {"minimum_overdue_minutes": 30},
            },
            {"type": "overdue_task", "task_id": "task-1", "overdue_minutes": 45},
        ),
        (
            {"type": "upload", "config": {"extension": "pdf"}},
            {
                "type": "upload", "file_id": "file-1",
                "filename": "review.PDF", "mime": "application/pdf",
            },
        ),
        (
            {"type": "person", "config": {"event": "follow_up_due"}},
            {"type": "person", "event": "follow_up_due", "person_id": "person-1"},
        ),
        (
            {
                "type": "metric_threshold",
                "config": {"metric": "sleep_hours", "operator": "lt", "threshold": 7},
            },
            {"type": "metric_threshold", "metric": "sleep_hours", "value": 6.5},
        ),
        (
            {"type": "location", "config": {"event": "enter", "place_id": "office"}},
            {"type": "location", "event": "enter", "place_id": "office"},
        ),
        (
            {"type": "form", "config": {"form_id": "intake"}},
            {
                "type": "form", "form_id": "intake", "submission_id": "sub-1",
                "fields": {"topic": "A", "description": "Ordinary form text"},
            },
        ),
        (
            {"type": "project_status", "config": {"to_status": "blocked"}},
            {"type": "project_status", "project_id": "project-1", "from_status": "active", "to_status": "blocked"},
        ),
    ],
)
def test_every_typed_trigger_matches_deterministically_without_writes(
    automation_env, monkeypatch, trigger, event
):
    definition, _ = _definition(
        automation_env,
        key=f"trigger-{trigger['type']}",
        trigger=trigger,
    )
    before_entities = automation_env.db.query(LifeEntity).count()
    before_proposals = automation_env.db.query(ActionProposal).count()

    def unexpected_commit():
        raise AssertionError("evaluation must not commit")

    monkeypatch.setattr(automation_env.db, "commit", unexpected_commit)
    evaluation = evaluate_automation(
        automation_env.db,
        owner_id=automation_env.alice.id,
        automation_id=definition.id,
        event=event,
    )
    assert evaluation.matched is True
    assert evaluation.trigger_type == trigger["type"]
    assert len(evaluation.event_fingerprint) == 64
    assert evaluation.plans[0]["execution_mode"] == "prepare_only"
    assert automation_env.db.query(LifeEntity).count() == before_entities
    assert automation_env.db.query(ActionProposal).count() == before_proposals


def test_wrong_trigger_event_is_read_only_non_match(automation_env):
    definition, _ = _definition(automation_env)
    before = automation_env.db.query(LifeEntity).count()
    result = prepare_automation_run(
        automation_env.db,
        account=automation_env.alice,
        automation_id=definition.id,
        event={
            "type": "email", "message_id": "msg-not-matched",
            "from_email": "sender@example.com",
        },
        idempotency_key="non-match",
    )
    assert result.evaluation.matched is False
    assert result.run_entity is None
    assert result.proposal_ids == ()
    assert automation_env.db.query(LifeEntity).count() == before


def test_all_action_types_prepare_under_policy_without_executing(automation_env, monkeypatch):
    source = _source(automation_env, automation_env.alice, "all-actions")
    email_account = _email_account(automation_env, automation_env.alice, "all-actions")
    task = _entity(automation_env, automation_env.alice, "task", "Review plan", "task")
    file_entity = _entity(
        automation_env, automation_env.alice, "file", "Source document", "file"
    )
    actions = [
        {
            "type": "create_entity",
            "config": {
                "entity_type": "note", "title": "Prepared note",
                "properties": {"kind": "meeting_note"},
                "source_ids": [source.id],
            },
        },
        {
            "type": "schedule",
            "config": {"name": "Daily review", "schedule": "daily", "scheduled_time": "09:00"},
        },
        {
            "type": "database_update",
            "config": {
                "entity_id": task.id, "expected_version": 1,
                "changes": {"status": "in_progress"},
            },
        },
        {
            "type": "draft",
            "config": {"channel": "whatsapp", "recipient": "+910000000000", "body": "Draft only"},
        },
        {
            "type": "approved_send",
            "config": {
                "channel": "email", "recipient": "person@example.com",
                "subject": "Follow-up", "body": "Exact approved draft",
                "email_account_id": email_account.id,
                "source_ids": [source.id],
            },
        },
        {
            "type": "briefing",
            "config": {"title": "Brief", "sections": ["risks"], "source_ids": [source.id]},
        },
        {
            "type": "file_move",
            "config": {"file_entity_id": file_entity.id, "expected_version": 1, "destination": "archive/"},
        },
        {
            "type": "report",
            "config": {
                "title": "Weekly report", "report_kind": "weekly",
                "format": "markdown", "source_entity_ids": [task.id],
                "source_ids": [source.id],
            },
        },
        {
            "type": "information_request",
            "config": {"title": "Missing details", "questions": ["Who owns this?"], "source_entity_ids": [task.id]},
        },
        {
            "type": "notification",
            "config": {"channel": "web", "title": "Review", "message": "Approval is waiting", "source_ids": [source.id]},
        },
        {
            "type": "agent",
            "config": {"role": "analysis", "objective": "Compare the cited facts", "output_type": "report", "source_ids": [source.id]},
        },
        {
            "type": "workflow",
            "config": {"workflow_type": "intake_triage", "parameters": {"scope": "today"}, "source_ids": [source.id]},
        },
    ]
    definition, _ = _definition(
        automation_env, key="all-actions-definition", actions=actions
    )
    task_version_before = task.version

    def unexpected_commit():
        raise AssertionError("preparation must not commit")

    monkeypatch.setattr(automation_env.db, "commit", unexpected_commit)
    prepared = prepare_automation_run(
        automation_env.db,
        account=automation_env.alice,
        automation_id=definition.id,
        event={"type": "time", "schedule_key": "morning", "source_ids": [source.id]},
        idempotency_key="all-actions-run",
    )
    assert prepared.created is True
    assert prepared.run_entity is not None
    assert {plan["type"] for plan in prepared.evaluation.plans} == ACTION_TYPES
    assert len(prepared.proposal_ids) == 2
    # Email drafts deliberately discard preparation-time confirmation
    # material; the reviewed web route issues a fresh challenge.  The web
    # notification proposal exposes its own short-lived challenge here.
    assert len(prepared.confirmation_tokens) == 1
    proposals = automation_env.db.query(ActionProposal).filter(
        ActionProposal.owner_id == automation_env.alice.id
    ).all()
    assert {proposal.action for proposal in proposals} == {"send_email", "send_message"}
    assert all(proposal.state == "prepared" for proposal in proposals)
    assert all(proposal.requires_confirmation for proposal in proposals)
    assert all(proposal.external for proposal in proposals)
    assert automation_env.db.query(EmailOutboundDraft).count() == 1
    assert automation_env.db.query(ScheduledTask).count() == 0
    assert automation_env.db.query(EmailOutboundDelivery).count() == 0
    automation_env.db.refresh(task)
    assert task.version == task_version_before
    assert task.status == "active"


def test_run_idempotency_is_exact_and_owner_scoped(automation_env):
    definition, _ = _definition(automation_env)
    event = {"type": "time", "schedule_key": "morning"}
    first = prepare_automation_run(
        automation_env.db,
        account=automation_env.alice,
        automation_id=definition.id,
        event=event,
        idempotency_key="exact-run",
    )
    second = prepare_automation_run(
        automation_env.db,
        account=automation_env.alice,
        automation_id=definition.id,
        event=event,
        idempotency_key="exact-run",
    )
    assert first.created is True
    assert second.created is False
    assert second.run_entity.id == first.run_entity.id
    assert automation_env.db.query(LifeEntity).filter(
        LifeEntity.owner_id == automation_env.alice.id,
        LifeEntity.entity_type == "action",
    ).count() == 1

    changed = {"type": "time", "schedule_key": "morning", "occurred_at": "2026-07-17T10:00:00Z"}
    with pytest.raises(AutomationConflict, match="idempotency"):
        prepare_automation_run(
            automation_env.db,
            account=automation_env.alice,
            automation_id=definition.id,
            event=changed,
            idempotency_key="exact-run",
        )
    with pytest.raises(AutomationNotFound):
        prepare_automation_run(
            automation_env.db,
            account=automation_env.bob,
            automation_id=definition.id,
            event=event,
            idempotency_key="exact-run",
        )


def test_current_domain_policy_can_tighten_or_disable_automation(automation_env):
    definition, _ = _definition(
        automation_env,
        key="scheduled-policy",
        actions=[{
            "type": "schedule",
            "config": {"name": "Review", "schedule": "daily", "scheduled_time": "08:00"},
        }],
    )
    automation_env.db.add(ActionPolicy(
        id=str(uuid.uuid4()), owner_id=automation_env.alice.id,
        domain="tasks", max_autonomy=3,
        external_requires_confirmation=True, enabled=True, rules={}, version=1,
    ))
    automation_env.db.flush()
    with pytest.raises(AutomationPolicyDenied, match="domain cap"):
        evaluate_automation(
            automation_env.db,
            owner_id=automation_env.alice.id,
            automation_id=definition.id,
            event={"type": "time", "schedule_key": "morning"},
        )

    policy = automation_env.db.query(ActionPolicy).filter_by(
        owner_id=automation_env.alice.id, domain="tasks"
    ).one()
    policy.max_autonomy = 4
    policy.rules = {"blocked_automation_actions": ["schedule"]}
    automation_env.db.flush()
    with pytest.raises(AutomationPolicyDenied, match="blocked"):
        evaluate_automation(
            automation_env.db,
            owner_id=automation_env.alice.id,
            automation_id=definition.id,
            event={"type": "time", "schedule_key": "morning"},
        )


def test_meeting_end_workflow_only_prepares_source_backed_approved_drafts(
    automation_env, monkeypatch
):
    source = _source(automation_env, automation_env.alice, "meeting-end")
    email_account = _email_account(automation_env, automation_env.alice, "meeting-end")
    meeting = create_life_entity(
        automation_env.db,
        account=automation_env.alice,
        entity_type="event",
        title="Design review",
        summary="Reviewed the release gate",
        provenance={"source_ids": [source.id]},
        occurred_at=datetime(2026, 7, 17, 9, 0),
        idempotency_key="meeting-event",
    )[0]
    initial_event_version = meeting.version

    def unexpected_commit():
        raise AssertionError("meeting preparation must not commit")

    monkeypatch.setattr(automation_env.db, "commit", unexpected_commit)
    first = prepare_meeting_end_workflow(
        automation_env.db,
        account=automation_env.alice,
        meeting_entity_id=meeting.id,
        source_ids=[source.id],
        follow_ups=[
            {
                "channel": "email", "recipient": "alex@example.com",
                "subject": "Design review follow-up",
                "body": "Here are the cited next steps.",
                "email_account_id": email_account.id,
            },
            {
                "channel": "restia_message", "recipient": "person-2",
                "body": "Please review the cited decision.",
            },
        ],
        ended_at="2026-07-17T10:00:00Z",
        idempotency_key="meeting-end-1",
    )
    assert first.run_entity is not None
    assert first.created is True
    assert len(first.proposal_ids) == 2
    assert len(first.confirmation_tokens) == 1
    assert [plan["type"] for plan in first.evaluation.plans] == [
        "briefing", "approved_send", "approved_send"
    ]
    proposals = automation_env.db.query(ActionProposal).filter(
        ActionProposal.id.in_(first.proposal_ids)
    ).all()
    assert len(proposals) == 2
    assert all(proposal.state == "prepared" for proposal in proposals)
    assert all(proposal.autonomy_level == 5 for proposal in proposals)
    assert all(proposal.external and proposal.requires_confirmation for proposal in proposals)
    email_proposal = next(row for row in proposals if row.action == "send_email")
    restia_proposal = next(row for row in proposals if row.action == "send_message")
    assert email_proposal.sources["life_source_ids"] == [source.id]
    assert email_proposal.target_type == "email_draft"
    assert restia_proposal.sources["life_source_ids"] == [source.id]
    assert restia_proposal.payload["typed_action"] == "approved_send"
    assert automation_env.db.query(EmailOutboundDraft).filter_by(
        owner_id=automation_env.alice.id,
        proposal_id=email_proposal.id,
        state="pending_review",
    ).count() == 1
    assert automation_env.db.query(EmailOutboundDelivery).count() == 0
    assert automation_env.db.query(ScheduledTask).count() == 0
    automation_env.db.refresh(meeting)
    assert meeting.version == initial_event_version

    second = prepare_meeting_end_workflow(
        automation_env.db,
        account=automation_env.alice,
        meeting_entity_id=meeting.id,
        source_ids=[source.id],
        follow_ups=[
            {
                "channel": "email", "recipient": "alex@example.com",
                "subject": "Design review follow-up",
                "body": "Here are the cited next steps.",
                "email_account_id": email_account.id,
            },
            {
                "channel": "restia_message", "recipient": "person-2",
                "body": "Please review the cited decision.",
            },
        ],
        ended_at="2026-07-17T10:00:00Z",
        idempotency_key="meeting-end-1",
    )
    assert second.created is False
    assert second.run_entity.id == first.run_entity.id
    assert second.proposal_ids == first.proposal_ids
    assert second.confirmation_tokens == {}
    assert automation_env.db.query(ActionProposal).count() == 2


def test_meeting_workflow_rejects_missing_cross_owner_sources_and_whatsapp_send(
    automation_env,
):
    meeting = _entity(
        automation_env, automation_env.alice, "event", "Private meeting", "private-meeting"
    )
    bob_source = _source(automation_env, automation_env.bob, "bob-only")
    with pytest.raises(AutomationNotFound, match="sources"):
        prepare_meeting_end_workflow(
            automation_env.db,
            account=automation_env.alice,
            meeting_entity_id=meeting.id,
            source_ids=[bob_source.id],
            follow_ups=[{
                "channel": "email", "recipient": "person@example.com", "body": "Hi"
            }],
            idempotency_key="cross-owner-source",
        )

    alice_source = _source(automation_env, automation_env.alice, "alice-only")
    with pytest.raises(AutomationError, match="WhatsApp is read-only"):
        prepare_meeting_end_workflow(
            automation_env.db,
            account=automation_env.alice,
            meeting_entity_id=meeting.id,
            source_ids=[alice_source.id],
            follow_ups=[{
                "channel": "whatsapp", "recipient": "+910000000000", "body": "Hi"
            }],
            idempotency_key="whatsapp-send",
        )


def test_cross_owner_entity_targets_are_not_observable(automation_env):
    bob_task = _entity(
        automation_env, automation_env.bob, "task", "Bob private task", "bob-task"
    )
    with pytest.raises(AutomationNotFound, match="entity"):
        _definition(
            automation_env,
            key="cross-owner-target",
            actions=[{
                "type": "database_update",
                "config": {
                    "entity_id": bob_task.id,
                    "expected_version": 1,
                    "changes": {"status": "completed"},
                },
            }],
        )


@pytest.mark.parametrize(
    ("trigger", "actions", "event", "message"),
    [
        (
            {"type": "time", "config": {"schedule_key": "morning", "api_key": "secret"}},
            [_briefing_action()],
            None,
            "credentials",
        ),
        (
            {"type": "time", "config": {"schedule_key": "morning"}},
            [{
                "type": "create_entity",
                "config": {
                    "entity_type": "note", "title": "Unsafe",
                    "properties": {"tool_call": {"name": "send"}},
                },
            }],
            None,
            "executors",
        ),
        (
            {"type": "time", "config": {"schedule_key": "morning"}},
            [{
                "type": "workflow",
                "config": {
                    "workflow_type": "intake_triage",
                    "parameters": {"command": "run arbitrary code"},
                },
            }],
            None,
            "executors",
        ),
        (
            {"type": "time", "config": {"schedule_key": "morning"}},
            [_briefing_action()],
            {
                "type": "form", "form_id": "intake", "submission_id": "sub",
                "fields": {"model_output": "write this directly"},
            },
            "model output",
        ),
    ],
)
def test_malformed_secret_executor_and_model_output_shapes_fail_closed(
    automation_env, trigger, actions, event, message
):
    if event is None:
        with pytest.raises(AutomationError, match=message):
            _definition(
                automation_env,
                key=f"unsafe-{uuid.uuid4()}",
                trigger=trigger,
                actions=actions,
            )
        return
    definition, _ = _definition(
        automation_env, key=f"safe-{uuid.uuid4()}", trigger=trigger, actions=actions
    )
    with pytest.raises(AutomationError, match=message):
        evaluate_automation(
            automation_env.db,
            owner_id=automation_env.alice.id,
            automation_id=definition.id,
            event=event,
        )


def test_bounded_agent_is_prepare_only_and_has_no_model_write_authority(automation_env):
    definition, _ = _definition(
        automation_env,
        key="agent-prepare-only",
        actions=[{
            "type": "agent",
            "config": {
                "role": "research",
                "objective": "Compare source-backed options",
                "output_type": "report",
            },
        }],
    )
    evaluation = evaluate_automation(
        automation_env.db,
        owner_id=automation_env.alice.id,
        automation_id=definition.id,
        event={"type": "time", "schedule_key": "morning"},
    )
    plan = evaluation.plans[0]
    assert plan["execution_mode"] == "prepare_only"
    assert plan["config"]["write_mode"] == "none"
    assert plan["external"] is False
    assert automation_env.db.query(ActionProposal).count() == 0
