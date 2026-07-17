from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from core.database import Account, ActionAudit, ActionPolicy, ActionProposal, Base
from routes.action_policy_routes import setup_action_policy_routes
from src.action_policy import (
    ActionApprovalRequired,
    ActionConfirmationExpired,
    ActionPolicyConflict,
    ActionPolicyDenied,
    ActionPolicyError,
    ActionPolicyNotFound,
    approve_action,
    complete_action,
    create_action_proposal,
    fail_action,
    get_action_proposal,
    issue_confirmation,
    reject_action,
    reverse_action,
    serialize_action_proposal,
    set_action_policy,
    start_action,
)
from src.audit_context import bind_request_audit_context
from src.identity import ensure_account


class _IdentityAuthority:
    def __init__(self, *usernames: str):
        self._config_lock = threading.Lock()
        self._identity_migrations: set[str] = set()
        self.retired_usernames: set[str] = set()
        self.users = {name: {} for name in usernames}

    @property
    def is_configured(self) -> bool:
        return bool(self.users)


@pytest.fixture()
def policy_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'action-policy.db'}",
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


def _request(account: Account, *, api_token: bool = False) -> Request:
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/life/actions/test/approve",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1234),
    })
    request.state.api_token = api_token
    request.state.api_token_id = "token-row" if api_token else None
    request.state.current_user = "api" if api_token else account.username
    return request


def _bind_human(db, account: Account) -> None:
    request = _request(account)
    bind_request_audit_context(db, request, account)


def _bind_api(db, account: Account) -> None:
    request = _request(account, api_token=True)
    bind_request_audit_context(db, request, account)


def _proposal(env, **overrides):
    values = {
        "owner_id": env.alice.id,
        "domain": "email",
        "action": "send",
        "autonomy_level": 5,
        "target_type": "message",
        "target_id": "draft-1",
        "payload": {"subject": "Hello"},
        "reason": "User asked to send a message",
        "sources": {"message": "draft-1"},
        "external": True,
        "idempotency_key": "request-1",
    }
    values.update(overrides)
    return create_action_proposal(env.db, **values)


def test_safe_defaults_critical_escalation_and_hashed_secrets(policy_env):
    raw_key = "raw-idempotency-secret"
    created = _proposal(
        policy_env,
        domain="finance",
        action="transfer",
        autonomy_level=1,
        target_type="transaction",
        idempotency_key=raw_key,
    )
    row = created.proposal

    assert row.autonomy_level == 6
    assert row.requires_confirmation is True
    assert created.confirmation_token
    assert row.idempotency_key == "sha256:" + hashlib.sha256(raw_key.encode()).hexdigest()
    assert raw_key not in row.idempotency_key
    assert row.confirmation_digest != created.confirmation_token
    assert len(row.confirmation_digest) == 64
    rendered = json.dumps(serialize_action_proposal(row), sort_keys=True)
    assert raw_key not in rendered
    assert created.confirmation_token not in rendered
    audit_rendered = json.dumps(
        [audit.details for audit in policy_env.db.query(ActionAudit).all()],
        sort_keys=True,
    )
    assert raw_key not in audit_rendered
    assert created.confirmation_token not in audit_rendered

    with pytest.raises(ActionPolicyDenied, match="domain cap"):
        _proposal(
            policy_env,
            domain="unknown_domain",
            action="execute",
            autonomy_level=4,
            idempotency_key="unknown-cap",
        )

    # A model cannot evade the Level 5 boundary by mislabelling an outbound
    # send as observation/preparation.
    outbound = _proposal(
        policy_env,
        domain="email",
        action="send_email",
        autonomy_level=1,
        idempotency_key="mislabelled-send",
    )
    assert outbound.proposal.autonomy_level == 5
    assert outbound.proposal.requires_confirmation is True

    neutral_external = _proposal(
        policy_env,
        domain="communications",
        action="sync_record",
        autonomy_level=1,
        external=True,
        idempotency_key="neutral-external",
    )
    assert neutral_external.proposal.autonomy_level == 5
    assert neutral_external.proposal.external is True
    assert neutral_external.proposal.requires_confirmation is True

    # Domain is caller-controlled. Representative side effects remain guarded
    # even when a model tries to disguise them as generic work.
    with pytest.raises(ActionPolicyDenied, match="domain cap"):
        _proposal(
            policy_env,
            domain="general",
            action="send_email",
            autonomy_level=1,
            external=False,
            idempotency_key="disguised-send",
        )

    with pytest.raises(ActionPolicyDenied, match="domain cap"):
        _proposal(
            policy_env,
            domain="general",
            action="transfer_money",
            autonomy_level=1,
            target_type="transaction",
            idempotency_key="disguised-finance",
        )

    permission_change = _proposal(
        policy_env,
        domain="permissions",
        action="grant_access",
        autonomy_level=2,
        idempotency_key="permission-change",
    )
    assert permission_change.proposal.autonomy_level == 6
    assert permission_change.proposal.requires_confirmation is True


def test_server_registry_normalizes_risk_and_unknown_mutations_fail_closed(
    policy_env,
):
    disguised = _proposal(
        policy_env,
        domain="email",
        action="dispatch_email",
        autonomy_level=4,
        external=False,
        idempotency_key="registry-dispatch",
    )
    assert disguised.proposal.autonomy_level == 5
    assert disguised.proposal.external is True
    assert disguised.proposal.requires_confirmation is True
    assert disguised.confirmation_token

    created_audit = (
        policy_env.db.query(ActionAudit)
        .filter_by(entity_id=disguised.proposal.id, action="action_proposal.created")
        .one()
    )
    assert created_audit.details["risk_source"] == "registry"
    assert created_audit.details["requested_autonomy_level"] == 4
    assert created_audit.details["requested_external"] is False
    assert created_audit.details["external"] is True

    with pytest.raises(ActionPolicyDenied, match="domain cap"):
        _proposal(
            policy_env,
            domain="email",
            action="courier_email",
            autonomy_level=4,
            external=False,
            idempotency_key="unknown-mutation",
        )

    # A known reversible verb is not sufficient: the authoritative domain and
    # target contract must match too.
    with pytest.raises(ActionPolicyDenied, match="domain cap"):
        _proposal(
            policy_env,
            domain="email",
            action="update_event",
            autonomy_level=4,
            target_type="message",
            external=False,
            idempotency_key="cross-field-mutation",
        )

    _bind_human(policy_env.db, policy_env.alice)
    set_action_policy(
        policy_env.db,
        owner_id=policy_env.alice.id,
        domain="email",
        max_autonomy=6,
        external_requires_confirmation=True,
        enabled=True,
    )
    policy_env.db.commit()
    unknown_at_explicit_cap = _proposal(
        policy_env,
        domain="email",
        action="courier_email",
        autonomy_level=4,
        external=False,
        idempotency_key="unknown-mutation-at-six",
    )
    assert unknown_at_explicit_cap.proposal.autonomy_level == 6
    assert unknown_at_explicit_cap.proposal.requires_confirmation is True
    unknown_audit = (
        policy_env.db.query(ActionAudit)
        .filter_by(
            entity_id=unknown_at_explicit_cap.proposal.id,
            action="action_proposal.created",
        )
        .one()
    )
    assert unknown_audit.details["risk_source"] == "unknown_executable"

    mismatched_at_explicit_cap = _proposal(
        policy_env,
        domain="email",
        action="update_event",
        autonomy_level=4,
        target_type="message",
        external=False,
        idempotency_key="cross-field-mutation-at-six",
    )
    assert mismatched_at_explicit_cap.proposal.autonomy_level == 6
    assert mismatched_at_explicit_cap.proposal.requires_confirmation is True
    mismatch_audit = (
        policy_env.db.query(ActionAudit)
        .filter_by(
            entity_id=mismatched_at_explicit_cap.proposal.id,
            action="action_proposal.created",
        )
        .one()
    )
    assert mismatch_audit.details["risk_source"] == "registry_context_mismatch"

    known_reversible = _proposal(
        policy_env,
        domain="calendar",
        action="update_event",
        autonomy_level=4,
        target_type="event",
        external=False,
        idempotency_key="registered-reversible",
    )
    assert known_reversible.proposal.autonomy_level == 4
    assert known_reversible.proposal.external is False
    assert known_reversible.proposal.requires_confirmation is False


def test_external_confirmation_setting_only_tightens_and_never_weakens_level_five(
    policy_env,
):
    with pytest.raises(ActionPolicyDenied, match="domain cap"):
        _proposal(
            policy_env,
            domain="calendar",
            action="create_event",
            autonomy_level=4,
            target_type="event",
            external=True,
            idempotency_key="calendar-default",
        )

    # External work is Level 5 even when its action name is neutral, so the
    # domain must explicitly permit Level 5 and confirmation stays mandatory.
    _bind_human(policy_env.db, policy_env.alice)
    policy = set_action_policy(
        policy_env.db,
        owner_id=policy_env.alice.id,
        domain="calendar",
        max_autonomy=5,
        external_requires_confirmation=True,
        enabled=True,
    )
    policy_env.db.commit()
    assert policy.version == 1
    calendar = _proposal(
        policy_env,
        domain="calendar",
        action="create_event",
        autonomy_level=4,
        target_type="event",
        external=True,
        idempotency_key="calendar-explicit",
    )
    assert calendar.proposal.autonomy_level == 5
    assert calendar.proposal.requires_confirmation is True

    policy = set_action_policy(
        policy_env.db,
        owner_id=policy_env.alice.id,
        domain="calendar",
        max_autonomy=5,
        external_requires_confirmation=False,
        enabled=True,
        expected_version=policy.version,
    )
    assert policy.external_requires_confirmation is False
    still_level_five = _proposal(
        policy_env,
        domain="calendar",
        action="sync_record",
        autonomy_level=1,
        external=True,
        idempotency_key="calendar-no-extra-fence",
    )
    assert still_level_five.proposal.autonomy_level == 5
    assert still_level_five.proposal.requires_confirmation is True

    email_policy = set_action_policy(
        policy_env.db,
        owner_id=policy_env.alice.id,
        domain="email",
        max_autonomy=5,
        external_requires_confirmation=False,
        enabled=True,
    )
    policy_env.db.commit()
    assert email_policy.external_requires_confirmation is False
    level_five = _proposal(policy_env, idempotency_key="level-five-still-confirmed")
    assert level_five.proposal.requires_confirmation is True
    assert level_five.confirmation_token


def test_execution_revalidates_risk_for_proposals_from_an_older_registry(policy_env):
    created = _proposal(
        policy_env,
        domain="calendar",
        action="update_event",
        autonomy_level=4,
        target_type="event",
        external=False,
        idempotency_key="legacy-risk-revalidation",
    )
    # Simulate weaker metadata persisted by a process running the former
    # action-name-only classifier.
    created.proposal.domain = "email"
    created.proposal.target_type = "message"
    policy_env.db.flush()

    with pytest.raises(ActionPolicyDenied, match="risk classification is stale"):
        start_action(
            policy_env.db,
            owner_id=policy_env.alice.id,
            proposal_id=created.proposal.id,
            expected_version=1,
            undo_ref="should-not-run",
        )
    assert created.proposal.state == "prepared"


def test_idempotent_retry_converges_and_payload_reuse_conflicts(policy_env):
    first = _proposal(policy_env, idempotency_key="retry-key")
    policy_env.db.commit()
    second = _proposal(policy_env, idempotency_key="retry-key")
    assert second.created is False
    assert second.proposal.id == first.proposal.id
    assert second.confirmation_token is None
    assert second.proposal.external is True
    assert serialize_action_proposal(second.proposal)["external"] is True

    normalized_retry = _proposal(
        policy_env,
        idempotency_key="retry-key",
        external=False,
    )
    assert normalized_retry.created is False
    assert normalized_retry.proposal.id == first.proposal.id
    assert normalized_retry.proposal.external is True

    with pytest.raises(ActionPolicyConflict, match="different action proposal"):
        _proposal(
            policy_env,
            idempotency_key="retry-key",
            payload={"subject": "Different"},
        )
    assert (
        policy_env.db.query(ActionProposal)
        .filter_by(owner_id=policy_env.alice.id)
        .count()
        == 1
    )


def test_only_human_owner_can_approve_and_tokens_are_version_bound_single_use(
    policy_env,
):
    created = _proposal(policy_env, idempotency_key="approval-token")
    policy_env.db.commit()

    with pytest.raises(ActionPolicyDenied, match="human web session"):
        approve_action(
            policy_env.db,
            owner_id=policy_env.alice.id,
            proposal_id=created.proposal.id,
            expected_version=1,
            confirmation_token=created.confirmation_token,
        )

    _bind_api(policy_env.db, policy_env.alice)
    with pytest.raises(ActionPolicyDenied, match="human web session"):
        approve_action(
            policy_env.db,
            owner_id=policy_env.alice.id,
            proposal_id=created.proposal.id,
            expected_version=1,
            confirmation_token=created.confirmation_token,
        )

    _bind_human(policy_env.db, policy_env.alice)
    challenge = issue_confirmation(
        policy_env.db,
        owner_id=policy_env.alice.id,
        proposal_id=created.proposal.id,
        expected_version=1,
    )
    assert challenge.proposal.version == 2
    with pytest.raises(ActionPolicyDenied, match="Invalid confirmation token"):
        approve_action(
            policy_env.db,
            owner_id=policy_env.alice.id,
            proposal_id=created.proposal.id,
            expected_version=2,
            confirmation_token=created.confirmation_token,
        )

    approved = approve_action(
        policy_env.db,
        owner_id=policy_env.alice.id,
        proposal_id=created.proposal.id,
        expected_version=2,
        confirmation_token=challenge.confirmation_token,
    )
    policy_env.db.commit()
    assert approved.state == "approved"
    assert approved.version == 3
    assert approved.confirmation_digest is None
    assert approved.approved_by_account_id == policy_env.alice.id

    with pytest.raises(ActionPolicyConflict):
        approve_action(
            policy_env.db,
            owner_id=policy_env.alice.id,
            proposal_id=created.proposal.id,
            expected_version=2,
            confirmation_token=challenge.confirmation_token,
        )
    with pytest.raises(ActionPolicyNotFound):
        get_action_proposal(
            policy_env.db,
            owner_id=policy_env.bob.id,
            proposal_id=created.proposal.id,
        )


def test_expired_confirmation_and_approval_lease_fail_closed(policy_env):
    clock = datetime(2026, 7, 17, 12, 0, 0)
    created = _proposal(policy_env, idempotency_key="expiry", now=clock)
    _bind_human(policy_env.db, policy_env.alice)

    with pytest.raises(ActionConfirmationExpired, match="token expired"):
        approve_action(
            policy_env.db,
            owner_id=policy_env.alice.id,
            proposal_id=created.proposal.id,
            expected_version=1,
            confirmation_token=created.confirmation_token,
            now=clock + timedelta(minutes=11),
        )

    challenge = issue_confirmation(
        policy_env.db,
        owner_id=policy_env.alice.id,
        proposal_id=created.proposal.id,
        expected_version=1,
        now=clock + timedelta(minutes=11),
    )
    approved = approve_action(
        policy_env.db,
        owner_id=policy_env.alice.id,
        proposal_id=created.proposal.id,
        expected_version=2,
        confirmation_token=challenge.confirmation_token,
        now=clock + timedelta(minutes=12),
    )
    with pytest.raises(ActionConfirmationExpired, match="approval expired"):
        start_action(
            policy_env.db,
            owner_id=policy_env.alice.id,
            proposal_id=approved.id,
            expected_version=3,
            now=clock + timedelta(minutes=18),
        )


def test_lifecycle_reverse_requires_fresh_approval_and_every_mutation_is_audited(
    policy_env,
):
    _bind_human(policy_env.db, policy_env.alice)
    created = _proposal(policy_env, idempotency_key="lifecycle")
    approved = approve_action(
        policy_env.db,
        owner_id=policy_env.alice.id,
        proposal_id=created.proposal.id,
        expected_version=1,
        confirmation_token=created.confirmation_token,
    )
    executing = start_action(
        policy_env.db,
        owner_id=policy_env.alice.id,
        proposal_id=approved.id,
        expected_version=2,
    )
    completed = complete_action(
        policy_env.db,
        owner_id=policy_env.alice.id,
        proposal_id=executing.id,
        expected_version=3,
        result={"message_id": "sent-1"},
        undo_ref="undo-send-1",
    )
    with pytest.raises(ActionApprovalRequired):
        reverse_action(
            policy_env.db,
            owner_id=policy_env.alice.id,
            proposal_id=completed.id,
            expected_version=4,
        )
    reverse_challenge = issue_confirmation(
        policy_env.db,
        owner_id=policy_env.alice.id,
        proposal_id=completed.id,
        expected_version=4,
        purpose="reverse",
    )
    reversed_row = reverse_action(
        policy_env.db,
        owner_id=policy_env.alice.id,
        proposal_id=completed.id,
        expected_version=5,
        confirmation_token=reverse_challenge.confirmation_token,
        result={"reversed": True},
    )
    policy_env.db.commit()
    assert reversed_row.state == "reversed"
    assert reversed_row.version == 6

    actions = [
        row.action
        for row in policy_env.db.query(ActionAudit)
        .filter_by(owner_id=policy_env.alice.id, entity_id=completed.id)
        .order_by(ActionAudit.created_at, ActionAudit.id)
        .all()
    ]
    assert actions == [
        "action_proposal.created",
        "action_proposal.approved",
        "action_proposal.executing",
        "action_proposal.completed",
        "action_proposal.confirmation_issued",
        "action_proposal.reversed",
    ]


def test_reject_and_fail_are_explicit_versioned_audited_transitions(policy_env):
    rejected_created = _proposal(
        policy_env,
        domain="general",
        action="prepare_summary",
        autonomy_level=3,
        external=False,
        idempotency_key="reject-transition",
    )
    rejected = reject_action(
        policy_env.db,
        owner_id=policy_env.alice.id,
        proposal_id=rejected_created.proposal.id,
        expected_version=1,
        reason="Not the right time",
    )
    assert rejected.state == "rejected"
    assert rejected.version == 2
    assert rejected.result == {"rejection_reason": "Not the right time"}

    failed_created = _proposal(
        policy_env,
        domain="calendar",
        action="update_event",
        autonomy_level=4,
        target_type="event",
        external=False,
        idempotency_key="fail-transition",
    )
    executing = start_action(
        policy_env.db,
        owner_id=policy_env.alice.id,
        proposal_id=failed_created.proposal.id,
        expected_version=1,
        undo_ref="calendar-event:restore:failed-run",
    )
    failed = fail_action(
        policy_env.db,
        owner_id=policy_env.alice.id,
        proposal_id=executing.id,
        expected_version=2,
        result={"code": "connector_unavailable"},
    )
    policy_env.db.commit()
    assert failed.state == "failed"
    assert failed.version == 3

    mutations = {
        row.action
        for row in policy_env.db.query(ActionAudit)
        .filter(
            ActionAudit.entity_id.in_([
                rejected_created.proposal.id,
                failed_created.proposal.id,
            ])
        )
        .all()
    }
    assert {
        "action_proposal.created",
        "action_proposal.rejected",
        "action_proposal.executing",
        "action_proposal.failed",
    } <= mutations


@pytest.mark.parametrize("level", [1, 2, 3])
def test_observe_suggest_and_prepare_cannot_enter_execution(policy_env, level):
    created = _proposal(
        policy_env,
        domain="general",
        action={1: "observe_record", 2: "suggest_plan", 3: "prepare_summary"}[level],
        autonomy_level=level,
        external=False,
        idempotency_key=f"non-executable-{level}",
    )
    with pytest.raises(ActionPolicyDenied, match="cannot enter execution"):
        start_action(
            policy_env.db,
            owner_id=policy_env.alice.id,
            proposal_id=created.proposal.id,
            expected_version=1,
        )
    assert created.proposal.state == "prepared"
    assert created.proposal.version == 1


def test_non_executable_label_cannot_be_upgraded_into_level_four_execution(
    policy_env,
):
    _bind_human(policy_env.db, policy_env.alice)
    set_action_policy(
        policy_env.db,
        owner_id=policy_env.alice.id,
        domain="general",
        max_autonomy=6,
        external_requires_confirmation=True,
        enabled=True,
    )
    policy_env.db.commit()

    upgraded = _proposal(
        policy_env,
        domain="general",
        action="prepare_summary",
        autonomy_level=4,
        target_type="note",
        external=False,
        idempotency_key="prepare-cannot-upgrade",
    )
    assert upgraded.proposal.autonomy_level == 6
    assert upgraded.proposal.requires_confirmation is True
    assert upgraded.confirmation_token
    created_audit = (
        policy_env.db.query(ActionAudit)
        .filter_by(entity_id=upgraded.proposal.id, action="action_proposal.created")
        .one()
    )
    assert created_audit.details["risk_source"] == "non_executable_escalation"

    with pytest.raises(ActionPolicyConflict, match="approved"):
        start_action(
            policy_env.db,
            owner_id=policy_env.alice.id,
            proposal_id=upgraded.proposal.id,
            expected_version=1,
        )


def test_level_four_requires_reversal_path_before_execution(policy_env):
    _bind_human(policy_env.db, policy_env.alice)
    set_action_policy(
        policy_env.db,
        owner_id=policy_env.alice.id,
        domain="calendar",
        max_autonomy=4,
        external_requires_confirmation=False,
        enabled=True,
    )
    created = _proposal(
        policy_env,
        domain="calendar",
        action="reschedule_event",
        autonomy_level=4,
        target_type="event",
        external=False,
        idempotency_key="reversible-before-execute",
    )
    with pytest.raises(ActionPolicyError, match="before execution"):
        start_action(
            policy_env.db,
            owner_id=policy_env.alice.id,
            proposal_id=created.proposal.id,
            expected_version=1,
        )
    assert created.proposal.state == "prepared"
    assert created.proposal.version == 1

    executing = start_action(
        policy_env.db,
        owner_id=policy_env.alice.id,
        proposal_id=created.proposal.id,
        expected_version=1,
        undo_ref="calendar-event:restore:v1",
    )
    assert executing.state == "executing"
    assert executing.undo_ref == "calendar-event:restore:v1"
    completed = complete_action(
        policy_env.db,
        owner_id=policy_env.alice.id,
        proposal_id=executing.id,
        expected_version=2,
        result={"rescheduled": True},
    )
    assert completed.undo_ref == "calendar-event:restore:v1"


def test_optimistic_policy_and_execution_races_have_one_winner(policy_env):
    _bind_human(policy_env.db, policy_env.alice)
    policy = set_action_policy(
        policy_env.db,
        owner_id=policy_env.alice.id,
        domain="calendar",
        max_autonomy=4,
        external_requires_confirmation=False,
        enabled=True,
    )
    policy_env.db.commit()
    set_action_policy(
        policy_env.db,
        owner_id=policy_env.alice.id,
        domain="calendar",
        max_autonomy=3,
        external_requires_confirmation=True,
        enabled=True,
        expected_version=policy.version,
    )
    policy_env.db.commit()
    with pytest.raises(ActionPolicyConflict):
        set_action_policy(
            policy_env.db,
            owner_id=policy_env.alice.id,
            domain="calendar",
            max_autonomy=4,
            external_requires_confirmation=False,
            enabled=True,
            expected_version=1,
        )

    # Restore level 4 and prepare a no-confirmation reversible action.
    current = policy_env.db.query(ActionPolicy).filter_by(
        owner_id=policy_env.alice.id, domain="calendar"
    ).one()
    set_action_policy(
        policy_env.db,
        owner_id=policy_env.alice.id,
        domain="calendar",
        max_autonomy=4,
        external_requires_confirmation=False,
        enabled=True,
        expected_version=current.version,
    )
    created = _proposal(
        policy_env,
        domain="calendar",
        action="create_event",
        autonomy_level=4,
        target_type="event",
        external=False,
        idempotency_key="race",
    )
    policy_env.db.commit()

    first_db = policy_env.Session()
    stale_db = policy_env.Session()
    try:
        first = get_action_proposal(
            first_db, owner_id=policy_env.alice.id, proposal_id=created.proposal.id
        )
        stale = get_action_proposal(
            stale_db, owner_id=policy_env.alice.id, proposal_id=created.proposal.id
        )
        assert first.version == stale.version == 1
        winner = start_action(
            first_db,
            owner_id=policy_env.alice.id,
            proposal_id=first.id,
            expected_version=1,
            undo_ref="calendar-event:restore:race",
        )
        first_db.commit()
        assert winner.state == "executing"
        with pytest.raises(ActionPolicyConflict):
            start_action(
                stale_db,
                owner_id=policy_env.alice.id,
                proposal_id=stale.id,
                expected_version=1,
                undo_ref="calendar-event:restore:race",
            )
    finally:
        first_db.close()
        stale_db.close()


@pytest.fixture()
def route_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'action-policy-routes.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    for username in ("alice", "bob"):
        ensure_account(db, username)
    db.commit()
    db.close()

    app = FastAPI()
    app.state.auth_manager = _IdentityAuthority("alice", "bob")

    @app.middleware("http")
    async def inject_identity(request, call_next):
        token_owner = request.headers.get("x-api-owner")
        if token_owner:
            request.state.api_token = True
            request.state.api_token_owner = token_owner
            request.state.api_token_scopes = request.headers.get(
                "x-api-scopes", ""
            ).split(",")
            request.state.api_token_id = "test-token"
            request.state.current_user = "api"
        else:
            request.state.api_token = False
            request.state.current_user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(setup_action_policy_routes(session_factory=factory))
    try:
        yield SimpleNamespace(app=app, Session=factory)
    finally:
        engine.dispose()


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, **kwargs)


@pytest.mark.asyncio
async def test_routes_enforce_life_scopes_owner_isolation_and_human_approval(route_env):
    payload = {
        "domain": "email",
        "action": "send",
        "autonomy_level": 5,
        "target_type": "message",
        "target_id": "draft-route",
        "payload": {"subject": "Route"},
        "external": True,
        "idempotency_key": "route-create",
    }
    denied = await _call(
        route_env,
        "POST",
        "/api/life/actions",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:read"},
        json=payload,
    )
    assert denied.status_code == 403

    created = await _call(route_env, "POST", "/api/life/actions", json=payload)
    assert created.status_code == 201, created.text
    action = created.json()["action"]
    token = created.json()["confirmation_token"]
    assert token

    wrong_owner = await _call(
        route_env, "GET", f"/api/life/actions/{action['id']}", user="bob"
    )
    assert wrong_owner.status_code == 404
    bob_list = await _call(route_env, "GET", "/api/life/actions", user="bob")
    assert bob_list.status_code == 200
    assert bob_list.json()["count"] == 0

    scoped_read = await _call(
        route_env,
        "GET",
        "/api/life/actions",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:read"},
    )
    assert scoped_read.status_code == 200
    assert scoped_read.json()["count"] == 1

    model_approval = await _call(
        route_env,
        "POST",
        f"/api/life/actions/{action['id']}/approve",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:write"},
        json={"version": action["version"], "confirmation_token": token},
    )
    assert model_approval.status_code == 403

    approved = await _call(
        route_env,
        "POST",
        f"/api/life/actions/{action['id']}/approve",
        json={"version": action["version"], "confirmation_token": token},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["action"]["state"] == "approved"

    stale = await _call(
        route_env,
        "POST",
        f"/api/life/actions/{action['id']}/reject",
        json={"version": action["version"], "reason": "stale"},
    )
    assert stale.status_code == 409


@pytest.mark.asyncio
async def test_action_route_uses_server_risk_instead_of_caller_external_hint(route_env):
    created = await _call(
        route_env,
        "POST",
        "/api/life/actions",
        json={
            "domain": "email",
            "action": "dispatch_email",
            "autonomy_level": 4,
            "target_type": "message",
            "target_id": "draft-disguised",
            "external": False,
            "idempotency_key": "route-registry-dispatch",
        },
    )
    assert created.status_code == 201, created.text
    action = created.json()["action"]
    assert action["autonomy_level"] == 5
    assert action["external"] is True
    assert action["requires_confirmation"] is True
    assert created.json()["confirmation_token"]

    denied = await _call(
        route_env,
        "POST",
        "/api/life/actions",
        json={
            "domain": "email",
            "action": "courier_email",
            "autonomy_level": 4,
            "target_type": "message",
            "target_id": "draft-unknown",
            "external": False,
            "idempotency_key": "route-unknown-mutation",
        },
    )
    assert denied.status_code == 403
    assert "domain cap" in denied.json()["detail"]


@pytest.mark.asyncio
async def test_action_route_rejects_cross_field_and_non_executable_bypasses(route_env):
    cross_field = await _call(
        route_env,
        "POST",
        "/api/life/actions",
        json={
            "domain": "email",
            "action": "update_event",
            "autonomy_level": 4,
            "target_type": "message",
            "external": False,
            "idempotency_key": "route-cross-field-mutation",
        },
    )
    assert cross_field.status_code == 403
    assert "level 6" in cross_field.json()["detail"]

    non_executable = await _call(
        route_env,
        "POST",
        "/api/life/actions",
        json={
            "domain": "general",
            "action": "prepare_summary",
            "autonomy_level": 4,
            "target_type": "note",
            "external": False,
            "idempotency_key": "route-prepare-upgrade",
        },
    )
    assert non_executable.status_code == 403
    assert "level 6" in non_executable.json()["detail"]


@pytest.mark.asyncio
async def test_execute_route_enforces_level_semantics_and_reversal_path(route_env):
    prepared = await _call(
        route_env,
        "POST",
        "/api/life/actions",
        json={
            "domain": "general",
            "action": "prepare_summary",
            "autonomy_level": 3,
            "target_type": "note",
            "external": False,
            "idempotency_key": "route-level-three",
        },
    )
    assert prepared.status_code == 201, prepared.text
    prepared_action = prepared.json()["action"]
    denied = await _call(
        route_env,
        "POST",
        f"/api/life/actions/{prepared_action['id']}/execute",
        json={"version": prepared_action["version"]},
    )
    assert denied.status_code == 403

    reversible = await _call(
        route_env,
        "POST",
        "/api/life/actions",
        json={
            "domain": "calendar",
            "action": "update_event",
            "autonomy_level": 4,
            "target_type": "event",
            "external": False,
            "idempotency_key": "route-level-four",
        },
    )
    assert reversible.status_code == 201, reversible.text
    reversible_action = reversible.json()["action"]
    missing_undo = await _call(
        route_env,
        "POST",
        f"/api/life/actions/{reversible_action['id']}/execute",
        json={"version": reversible_action["version"]},
    )
    assert missing_undo.status_code == 400

    executing = await _call(
        route_env,
        "POST",
        f"/api/life/actions/{reversible_action['id']}/execute",
        json={
            "version": reversible_action["version"],
            "undo_ref": "calendar-event:restore:route",
        },
    )
    assert executing.status_code == 200, executing.text
    assert executing.json()["action"]["state"] == "executing"
    assert (
        executing.json()["action"]["undo_ref"]
        == "calendar-event:restore:route"
    )


@pytest.mark.asyncio
async def test_policy_routes_require_scope_and_optimistic_version(route_env):
    denied = await _call(
        route_env,
        "PUT",
        "/api/life/policies/calendar",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:read"},
        json={"max_autonomy": 4},
    )
    assert denied.status_code == 403

    created = await _call(
        route_env,
        "PUT",
        "/api/life/policies/calendar",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:write"},
        json={
            "max_autonomy": 3,
            "external_requires_confirmation": True,
            "enabled": True,
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["policy"]["version"] == 1

    indirect_self_approval = await _call(
        route_env,
        "PUT",
        "/api/life/policies/calendar",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:write"},
        json={
            "max_autonomy": 4,
            "external_requires_confirmation": False,
            "enabled": True,
            "version": 1,
        },
    )
    assert indirect_self_approval.status_code == 403

    human_update = await _call(
        route_env,
        "PUT",
        "/api/life/policies/calendar",
        json={
            "max_autonomy": 4,
            "external_requires_confirmation": False,
            "enabled": True,
            "version": 1,
        },
    )
    assert human_update.status_code == 200, human_update.text
    assert human_update.json()["policy"]["version"] == 2

    stale = await _call(
        route_env,
        "PUT",
        "/api/life/policies/calendar",
        json={"max_autonomy": 3, "version": 99},
    )
    assert stale.status_code == 409

    inspected = await _call(
        route_env,
        "GET",
        "/api/life/policies/calendar",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:read"},
    )
    assert inspected.status_code == 200
    assert inspected.json()["policy"]["persisted"] is True
