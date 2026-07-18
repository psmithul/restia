from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from core.database import Account, Base
from src.action_policy import (
    ActionPolicyDenied,
    create_action_proposal,
    get_effective_action_policy,
    serialize_action_policy,
    serialize_action_proposal,
    set_action_policy,
    start_action,
)
from src.audit_context import bind_request_audit_context


@pytest.fixture()
def autonomy_env(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'autonomy-examples.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    alice = Account(id=str(uuid.uuid4()), username="alice", status="active")
    db.add(alice)
    db.commit()
    yield SimpleNamespace(db=db, alice=alice, engine=engine)
    db.close()
    engine.dispose()


def _human(db, account: Account) -> None:
    request = Request({
        "type": "http",
        "method": "PUT",
        "path": "/api/life/policies/files",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1234),
    })
    request.state.api_token = False
    request.state.current_user = account.username
    bind_request_audit_context(db, request, account)


def _create(env, *, key: str, **values):
    payload = {
        "owner_id": env.alice.id,
        "domain": "general",
        "action": "observe_record",
        "autonomy_level": 1,
        "target_type": "source",
        "target_id": "example",
        "payload": {},
        "reason": "V3 policy example",
        "sources": {"source_id": "source-example"},
        "external": False,
        "idempotency_key": key,
    }
    payload.update(values)
    return create_action_proposal(env.db, **payload)


def test_levels_one_to_five_and_draft_calendar_send_examples(autonomy_env):
    env = autonomy_env
    observe = _create(env, key="observe")
    suggest = _create(
        env,
        key="suggest",
        action="suggest_plan",
        autonomy_level=2,
    )
    draft = _create(
        env,
        key="draft",
        domain="email",
        action="prepare_email_draft",
        autonomy_level=3,
        target_type="message",
    )
    calendar = _create(
        env,
        key="calendar",
        domain="calendar",
        action="reschedule_event",
        autonomy_level=4,
        target_type="event",
    )
    send = _create(
        env,
        key="send",
        domain="email",
        action="send_email",
        autonomy_level=1,
        target_type="message",
    )

    rows = [observe, suggest, draft, calendar, send]
    assert [item.proposal.autonomy_level for item in rows] == [1, 2, 3, 4, 5]
    assert [serialize_action_proposal(item.proposal)["autonomy_label"] for item in rows] == [
        "observe", "suggest", "prepare", "execute_reversible", "execute_external",
    ]
    assert all(item.confirmation_token is None for item in rows[:4])
    assert send.confirmation_token
    assert send.proposal.external is True
    assert send.proposal.requires_confirmation is True

    for item in rows[:3]:
        with pytest.raises(ActionPolicyDenied, match="cannot enter execution"):
            start_action(
                env.db,
                owner_id=env.alice.id,
                proposal_id=item.proposal.id,
                expected_version=item.proposal.version,
            )


def test_whatsapp_stays_read_only_and_deletion_finance_legal_medical_are_level_six(
    autonomy_env,
):
    env = autonomy_env
    whatsapp = get_effective_action_policy(
        env.db, owner_id=env.alice.id, domain="whatsapp",
    )
    assert whatsapp.max_autonomy == 1
    with pytest.raises(ActionPolicyDenied, match="domain cap"):
        _create(
            env,
            key="whatsapp-reply",
            domain="whatsapp",
            action="reply_whatsapp",
            autonomy_level=1,
            target_type="message",
        )

    with pytest.raises(ActionPolicyDenied, match="domain cap"):
        _create(
            env,
            key="delete-file-default",
            domain="files",
            action="delete_file",
            autonomy_level=1,
            target_type="file",
        )

    _human(env.db, env.alice)
    files_policy = set_action_policy(
        env.db,
        owner_id=env.alice.id,
        domain="files",
        max_autonomy=6,
        external_requires_confirmation=True,
        enabled=True,
    )
    serialized_policy = serialize_action_policy(files_policy)
    assert serialized_policy["max_autonomy"] == 6
    assert serialized_policy["max_autonomy_label"] == "high_risk"

    high_risk = [
        _create(
            env,
            key="delete-file-approved-path",
            domain="files",
            action="delete_file",
            autonomy_level=1,
            target_type="file",
        ),
        _create(
            env,
            key="finance-transfer",
            domain="finance",
            action="transfer_money",
            autonomy_level=1,
            target_type="transaction",
        ),
        _create(
            env,
            key="legal-contract",
            domain="legal",
            action="sign_contract",
            autonomy_level=1,
            target_type="contract",
        ),
        _create(
            env,
            key="medical-change",
            domain="medical",
            action="change_medication",
            autonomy_level=1,
            target_type="health_record",
        ),
    ]
    assert all(item.proposal.autonomy_level == 6 for item in high_risk)
    assert all(item.proposal.requires_confirmation is True for item in high_risk)
    assert all(item.confirmation_token for item in high_risk)


def test_domain_caps_can_only_be_loosened_by_the_owning_human_session(autonomy_env):
    env = autonomy_env
    with pytest.raises(ActionPolicyDenied, match="owning human"):
        set_action_policy(
            env.db,
            owner_id=env.alice.id,
            domain="calendar",
            max_autonomy=6,
            external_requires_confirmation=True,
            enabled=True,
        )
    _human(env.db, env.alice)
    policy = set_action_policy(
        env.db,
        owner_id=env.alice.id,
        domain="calendar",
        max_autonomy=6,
        external_requires_confirmation=True,
        enabled=True,
        rules={"allowed_calendars": ["personal"]},
    )
    assert policy.max_autonomy == 6
    assert policy.rules == {"allowed_calendars": ["personal"]}
