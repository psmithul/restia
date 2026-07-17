from __future__ import annotations

import hashlib
import json
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from core.database import Account, ActionAudit, Base
from src.audit_context import (
    bind_request_audit_context,
    bind_service_audit_context,
    build_action_audit_details,
)
from src.life_core import (
    append_action_audit,
    archive_inbox_item,
    classify_inbox_item,
    create_inbox_item,
    process_inbox_item,
    update_inbox_item,
)


@pytest.fixture()
def audit_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    engine = create_engine(
        f"sqlite:///{tmp_path / 'action-audit.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    account = Account(
        id=str(uuid.uuid4()),
        username="private-audit-user@example.test",
        display_name="Private Audit User",
    )
    db.add(account)
    db.commit()
    try:
        yield db, account
    finally:
        db.close()
        engine.dispose()


def _request(*, headers: dict[str, str] | None = None, **state_values) -> Request:
    encoded_headers = [
        (name.lower().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/inbox",
        "headers": encoded_headers,
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1234),
    })
    for name, value in state_values.items():
        setattr(request.state, name, value)
    return request


def _audit_payload(row: ActionAudit) -> dict:
    payload = row.details.get("audit")
    assert isinstance(payload, dict)
    return payload


def test_direct_service_audit_uses_domain_service_attribution(audit_env):
    db, account = audit_env

    item, created = create_inbox_item(
        db,
        account=account,
        title="Direct service capture",
        kind="note",
    )
    assert created is True
    db.commit()

    row = db.query(ActionAudit).filter_by(
        action="inbox.created", entity_id=item.id
    ).one()
    audit = _audit_payload(row)
    assert audit["actor_type"] == "service"
    assert audit["actor_id"] == account.id
    assert audit["interface"] == "domain_service"
    assert audit["credential_type"] == "internal"
    assert audit["credential_id"] is None
    assert audit["workflow_id"] is None


def test_session_and_api_token_bindings_share_actor_without_leaking_secrets(
    audit_env,
):
    db, account = audit_env
    raw_bearer = "ody_RAW_BEARER_SECRET_MUST_NOT_BE_AUDITED"
    raw_cookie = "RAW_SESSION_COOKIE_MUST_NOT_BE_AUDITED"
    token_id = "api-token-row-9f6f"
    workflow_id = "workflow-run-14a2"

    session_request = _request(
        headers={"cookie": f"restia_session={raw_cookie}"},
        api_token=False,
        current_user=account.username,
    )
    bind_request_audit_context(db, session_request, account)
    append_action_audit(
        db,
        owner_id=account.id,
        action="test.session",
        entity_type="test",
        entity_id="session-event",
        reason="Session attribution test",
    )

    api_request = _request(
        headers={"authorization": f"Bearer {raw_bearer}"},
        api_token=True,
        api_token_id=token_id,
        api_token_owner=account.username,
        current_user="api",
        workflow_id=workflow_id,
    )
    bind_request_audit_context(db, api_request, account)
    append_action_audit(
        db,
        owner_id=account.id,
        action="test.api_token",
        entity_type="test",
        entity_id="api-event",
        reason="API-token attribution test",
    )
    db.commit()

    rows = {
        row.action: row
        for row in db.query(ActionAudit)
        .filter(ActionAudit.action.in_(("test.session", "test.api_token")))
        .all()
    }
    session_audit = _audit_payload(rows["test.session"])
    api_audit = _audit_payload(rows["test.api_token"])

    assert session_audit["actor_id"] == api_audit["actor_id"] == account.id
    assert session_audit["actor_type"] == "account"
    assert api_audit["actor_type"] == "api_token"
    assert session_audit["interface"] == "web"
    assert api_audit["interface"] == "api"
    assert session_audit["credential_type"] == "session"
    assert api_audit["credential_type"] == "api_token"
    assert session_audit["credential_id"] is None
    assert api_audit["credential_id"] == token_id
    assert api_audit["workflow_id"] == workflow_id

    rendered = json.dumps(
        [rows["test.session"].details, rows["test.api_token"].details],
        sort_keys=True,
    )
    assert raw_bearer not in rendered
    assert raw_cookie not in rendered
    assert account.username not in rendered
    assert account.display_name not in rendered


def test_raw_inbox_idempotency_key_is_stored_only_as_one_way_reference(audit_env):
    db, account = audit_env
    raw_key = "capture-retry-secret-2026-07-16"
    expected_ref = "sha256:" + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    item, created = create_inbox_item(
        db,
        account=account,
        title="Idempotent private capture",
        kind="note",
        idempotency_key=raw_key,
    )
    assert created is True
    db.commit()

    row = db.query(ActionAudit).filter_by(
        action="inbox.created", entity_id=item.id
    ).one()
    assert item.idempotency_key == expected_ref
    assert _audit_payload(row)["idempotency_ref"] == expected_ref
    assert raw_key not in item.idempotency_key
    assert raw_key not in json.dumps(row.details, sort_keys=True)


def test_fake_sha256_prefix_is_hashed_instead_of_trusted(audit_env):
    db, account = audit_env
    attacker_value = "sha256:" + ("not-hex!" * 8)

    audit = build_action_audit_details(
        db,
        owner_id=account.id,
        reason="Reject attacker-controlled digest labels",
        idempotency_ref=attacker_value,
    )["audit"]

    assert audit["idempotency_ref"] == (
        "sha256:" + hashlib.sha256(attacker_value.encode("utf-8")).hexdigest()
    )


@pytest.mark.parametrize(
    ("interface", "actor_type", "credential_type"),
    [
        ("cli", "account", "local_cli"),
        ("telegram", "account", "telegram_link"),
        ("voice", "account", "voice_session"),
        ("automation", "automation", "scheduled_task"),
        ("home_link", "linked_instance", "home_link_grant"),
        ("internal_tool", "service", "internal_tool"),
    ],
)
def test_trusted_non_http_interfaces_bind_explicit_audit_attribution(
    audit_env,
    interface,
    actor_type,
    credential_type,
):
    db, account = audit_env
    credential_id = f"{interface}-credential-row"
    workflow_id = f"{interface}-workflow-run"

    bound = bind_service_audit_context(
        db,
        account_id=account.id,
        interface=interface,
        actor_type=actor_type,
        credential_type=credential_type,
        credential_id=credential_id,
        workflow_id=workflow_id,
    )
    details = build_action_audit_details(
        db,
        owner_id=account.id,
        reason="Trusted adapter attribution test",
    )

    assert bound == {
        "actor_type": actor_type,
        "actor_id": account.id,
        "interface": interface,
        "credential_type": credential_type,
        "credential_id": credential_id,
        "workflow_id": workflow_id,
    }
    audit = details["audit"]
    assert audit["actor_id"] == account.id
    assert audit["actor_type"] == actor_type
    assert audit["interface"] == interface
    assert audit["credential_type"] == credential_type
    assert audit["credential_id"] == credential_id
    assert audit["workflow_id"] == workflow_id


def test_service_audit_binding_rejects_untrusted_interface(audit_env):
    db, account = audit_env

    with pytest.raises(ValueError, match="Unknown action audit interface"):
        bind_service_audit_context(
            db,
            account_id=account.id,
            interface="arbitrary-client-header",
        )


def test_every_inbox_transition_audit_has_required_reason_outcome_and_undo_shape(
    audit_env,
):
    db, account = audit_env

    edited, _ = create_inbox_item(
        db, account=account, title="Draft capture", kind="note"
    )
    update_inbox_item(
        db,
        owner_id=account.id,
        item_id=edited.id,
        expected_version=1,
        title="Edited capture",
    )

    classified, _ = create_inbox_item(
        db,
        account=account,
        title="Need to send the launch report",
        kind="note",
    )
    classify_inbox_item(
        db,
        owner_id=account.id,
        item_id=classified.id,
        expected_version=1,
    )

    task, _ = create_inbox_item(
        db,
        account=account,
        title="Send the launch report",
        kind="task",
    )
    process_inbox_item(
        db,
        account=account,
        item_id=task.id,
        expected_version=1,
    )

    archived, _ = create_inbox_item(
        db, account=account, title="Old reference", kind="note"
    )
    archive_inbox_item(
        db,
        owner_id=account.id,
        item_id=archived.id,
        expected_version=1,
    )
    db.commit()

    rows = db.query(ActionAudit).filter_by(owner_id=account.id).all()
    assert {
        "inbox.created",
        "inbox.updated",
        "inbox.classified",
        "entity.linked",
        "inbox.processed",
        "inbox.archived",
    }.issubset({row.action for row in rows})

    for row in rows:
        audit = _audit_payload(row)
        assert {"reason", "outcome", "reversible", "undo_ref"}.issubset(audit)
        assert isinstance(audit["reason"], str) and audit["reason"]
        assert audit["outcome"] == "success"
        assert isinstance(audit["reversible"], bool)
        if audit["reversible"]:
            assert isinstance(audit["undo_ref"], str) and audit["undo_ref"]
        else:
            assert audit["undo_ref"] is None


def test_invalid_action_audit_outcome_fails_closed_without_persisting(audit_env):
    db, account = audit_env
    before = db.query(ActionAudit).count()

    with pytest.raises(ValueError, match="Unknown action audit outcome"):
        build_action_audit_details(
            db,
            owner_id=account.id,
            reason="Invalid outcome must not be accepted",
            outcome="partially_succeeded",
        )

    assert db.query(ActionAudit).count() == before
