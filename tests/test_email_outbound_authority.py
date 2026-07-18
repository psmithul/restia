from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from core.database import (
    Account,
    ActionProposal,
    Base,
    EmailAccount,
    EmailOutboundDelivery,
    EmailOutboundDraft,
)
from routes.action_policy_routes import _serialize_action, setup_action_policy_routes
from src.action_policy import (
    ActionPolicyError,
    approve_action,
    create_action_proposal,
    issue_confirmation,
)
from src.audit_context import SESSION_AUDIT_CONTEXT_KEY
from src.email_outbound import (
    EmailOutboundError,
    adopt_legacy_agent_email_drafts,
    claim_email_delivery,
    complete_email_delivery,
    import_legacy_agent_email_drafts,
    is_server_email_action,
    prepare_agent_email_action,
    queue_approved_email_action,
    retry_email_delivery,
)
from src.identity import ensure_account


@pytest.fixture()
def email_outbound_env(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'email-outbound.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    alice = Account(id=str(uuid.uuid4()), username="alice")
    bob = Account(id=str(uuid.uuid4()), username="bob")
    db.add_all([alice, bob])
    db.flush()
    alice_mail = EmailAccount(
        id="mail-alice",
        owner="alice",
        name="Alice Mail",
        enabled=True,
        is_default=True,
        imap_host="imap.example.test",
        imap_port=993,
        imap_user="alice@example.test",
        smtp_host="smtp.example.test",
        smtp_port=465,
        smtp_user="alice@example.test",
        from_address="alice@example.test",
    )
    bob_mail = EmailAccount(
        id="mail-bob",
        owner="bob",
        name="Bob Mail",
        enabled=True,
        is_default=True,
        imap_host="imap.example.test",
        imap_port=993,
        imap_user="bob@example.test",
        smtp_host="smtp.example.test",
        smtp_port=465,
        smtp_user="bob@example.test",
        from_address="bob@example.test",
    )
    db.add_all([alice_mail, bob_mail])
    db.commit()
    try:
        yield SimpleNamespace(
            engine=engine,
            Session=factory,
            db=db,
            alice=alice,
            bob=bob,
            alice_mail=alice_mail,
            bob_mail=bob_mail,
            tmp_path=tmp_path,
        )
    finally:
        db.close()
        engine.dispose()


def _prepare(env, **overrides):
    values = {
        "owner_username": "alice",
        "email_account_id": env.alice_mail.id,
        "to": "Ada Lovelace <ada@example.test>",
        "cc": "team@example.test",
        "bcc": "archive@example.test",
        "subject": "Controller review",
        "body": "Please review the attached controller notes.",
        "attachments": [{
            "id": "attachment-1",
            "name": "controller.txt",
            "sha256": "a" * 64,
        }],
        "source": {"session_id": "chat-1"},
        "idempotency_key": "agent-call-1",
    }
    values.update(overrides)
    return prepare_agent_email_action(env.db, **values)


def _human_context(env):
    env.db.info[SESSION_AUDIT_CONTEXT_KEY] = {
        "actor_type": "account",
        "actor_id": env.alice.id,
        "credential_type": "session",
        "interface": "web",
    }


def _approve(env, prepared):
    _human_context(env)
    challenge = issue_confirmation(
        env.db,
        owner_id=env.alice.id,
        proposal_id=prepared.proposal.id,
        expected_version=prepared.proposal.version,
        purpose="approve",
    )
    approved = approve_action(
        env.db,
        owner_id=env.alice.id,
        proposal_id=prepared.proposal.id,
        expected_version=challenge.proposal.version,
        confirmation_token=challenge.confirmation_token,
    )
    env.db.flush()
    return approved


def test_prepare_is_exact_level_five_owner_scoped_and_idempotent(email_outbound_env):
    env = email_outbound_env
    prepared = _prepare(env)
    repeated = _prepare(env)
    env.db.commit()

    assert prepared.created is True
    assert repeated.created is False
    assert repeated.proposal.id == prepared.proposal.id
    assert repeated.draft.id == prepared.draft.id
    proposal = prepared.proposal
    assert proposal.owner_id == env.alice.id
    assert proposal.autonomy_level == 5
    assert proposal.requires_confirmation is True
    assert proposal.external is True
    assert proposal.state == "prepared"
    assert proposal.target_type == "email_draft"
    assert proposal.target_id == prepared.draft.id
    assert prepared.draft.owner_id == env.alice.id
    assert prepared.draft.email_account_id == env.alice_mail.id
    assert prepared.draft.state == "pending_review"
    assert proposal.payload == prepared.draft.content
    assert proposal.payload["to"] == ["Ada Lovelace <ada@example.test>"]
    assert proposal.payload["cc"] == ["team@example.test"]
    assert proposal.payload["bcc"] == ["archive@example.test"]
    assert proposal.payload["subject"] == "Controller review"
    assert proposal.payload["body"].startswith("Please review")
    assert proposal.payload["attachments"][0]["id"] == "attachment-1"
    assert proposal.payload["account"] == {
        "id": "mail-alice",
        "name": "Alice Mail",
        "from_address": "alice@example.test",
    }
    assert proposal.payload["delivery"] == {
        "mode": "durable_outbox",
        "network_on_request": False,
    }
    rendered = json.dumps(_serialize_action(env.db, proposal), sort_keys=True)
    assert "confirmation_token" not in rendered
    assert _serialize_action(env.db, proposal)["reviewed_server_executor"] is True
    assert env.db.query(ActionProposal).count() == 1
    assert env.db.query(EmailOutboundDraft).count() == 1

    with env.engine.connect() as connection:
        stored = connection.execute(text(
            "SELECT content, source FROM email_outbound_drafts"
        )).one()
    assert "Controller review" not in str(stored.content)
    assert "chat-1" not in str(stored.source)
    assert "enc:c1:" in str(stored.content)
    assert "enc:c1:" in str(stored.source)


def test_owner_isolation_and_forged_action_never_gain_executor(email_outbound_env):
    env = email_outbound_env
    with pytest.raises(EmailOutboundError, match="not available"):
        _prepare(
            env,
            owner_username="bob",
            email_account_id=env.alice_mail.id,
            idempotency_key="bob-cross-owner",
        )

    forged = create_action_proposal(
        env.db,
        owner_id=env.alice.id,
        domain="email",
        action="send_email",
        autonomy_level=5,
        target_type="email_draft",
        target_id="forged-draft",
        payload={"draft_id": "forged-draft"},
        reason="Unlinked public proposal",
        sources={"interface": "api"},
        external=True,
        idempotency_key="forged-action",
    ).proposal
    assert is_server_email_action(env.db, forged) is False
    assert _serialize_action(env.db, forged)["reviewed_server_executor"] is False


def test_approval_only_queues_and_worker_lifecycle_is_claim_fenced(
    email_outbound_env, monkeypatch
):
    env = email_outbound_env
    prepared = _prepare(env)
    approved = _approve(env, prepared)

    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("request-path network attempted")

    import socket
    import smtplib
    import imaplib

    monkeypatch.setattr(socket, "create_connection", network_forbidden)
    monkeypatch.setattr(smtplib, "SMTP", network_forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", network_forbidden)
    monkeypatch.setattr(imaplib, "IMAP4", network_forbidden)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", network_forbidden)

    queued = queue_approved_email_action(
        env.db,
        account=env.alice,
        proposal_id=approved.id,
        expected_version=approved.version,
    )
    env.db.commit()
    assert queued.proposal.state == "executing"
    assert queued.draft.state == "queued"
    assert queued.delivery.state == "queued"
    assert queued.delivery.attempts == 0
    assert queued.delivery.payload == queued.draft.content
    assert queued.delivery.content_sha256 == queued.draft.content_sha256

    claim = claim_email_delivery(env.db)
    assert claim is not None
    env.db.commit()
    env.db.refresh(queued.delivery)
    assert queued.delivery.state == "claimed"
    assert queued.delivery.attempts == 1
    assert claim.content == queued.delivery.payload
    assert queued.delivery.claim_token_digest
    assert claim.claim_token not in queued.delivery.claim_token_digest

    assert claim_email_delivery(env.db) is None
    retry_email_delivery(
        env.db,
        delivery_id=claim.delivery_id,
        claim_token=claim.claim_token,
        error_code="smtp_temporarily_unavailable",
        now=datetime.utcnow(),
    )
    env.db.commit()
    env.db.refresh(queued.delivery)
    assert queued.delivery.state == "retry"
    assert queued.delivery.last_error_code == "smtp_temporarily_unavailable"

    later = datetime.utcnow() + timedelta(hours=2)
    second_claim = claim_email_delivery(env.db, now=later)
    assert second_claim is not None
    env.db.commit()
    completed = complete_email_delivery(
        env.db,
        delivery_id=second_claim.delivery_id,
        claim_token=second_claim.claim_token,
        provider_message_id="<provider-result@example.test>",
        now=later + timedelta(seconds=1),
    )
    env.db.commit()
    env.db.refresh(prepared.proposal)
    env.db.refresh(prepared.draft)
    assert completed.state == "delivered"
    assert prepared.proposal.state == "completed"
    assert prepared.draft.state == "delivered"
    assert prepared.proposal.result["delivery_state"] == "delivered"


def test_unapproved_email_cannot_be_queued(email_outbound_env):
    env = email_outbound_env
    prepared = _prepare(env)
    with pytest.raises(ActionPolicyError):
        queue_approved_email_action(
            env.db,
            account=env.alice,
            proposal_id=prepared.proposal.id,
            expected_version=prepared.proposal.version,
        )
    assert env.db.query(EmailOutboundDelivery).count() == 0


@pytest.mark.asyncio
async def test_codex_send_prepares_level_five_action_and_never_calls_manual_smtp_route(
    email_outbound_env, monkeypatch
):
    env = email_outbound_env
    import core.database as database
    import smtplib
    from routes.codex_routes import setup_codex_routes

    monkeypatch.setattr(database, "SessionLocal", env.Session)

    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("Codex email action attempted request-path network")

    monkeypatch.setattr(smtplib, "SMTP", network_forbidden)
    monkeypatch.setattr(smtplib, "SMTP_SSL", network_forbidden)
    manual_router = APIRouter()

    @manual_router.post("/api/email/send")
    async def manual_send_must_not_run():
        raise AssertionError("Codex called the manual compose SMTP route")

    router = setup_codex_routes(email_router=manual_router)
    endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/api/codex/emails/send" and "POST" in route.methods
    )
    request = Request({
        "type": "http",
        "method": "POST",
        "path": "/api/codex/emails/send",
        "headers": [],
        "state": {},
    })
    request.state.api_token = True
    request.state.api_token_owner = "alice"
    request.state.api_token_scopes = ["email:send"]

    result = await endpoint(
        request=request,
        body={
            "to": "Ada <ada@example.test>",
            "subject": "Codex review",
            "body": "Review this before delivery.",
            "body_html": "<p>Review this before delivery.</p>",
        },
    )

    assert result["success"] is True
    assert result["pending"] is True
    assert result["requires_confirmation"] is True
    assert result["network_performed"] is False
    check = env.Session()
    try:
        proposal = check.query(ActionProposal).filter(
            ActionProposal.id == result["pending_id"]
        ).one()
        draft = check.query(EmailOutboundDraft).filter(
            EmailOutboundDraft.id == result["draft_id"]
        ).one()
        assert proposal.autonomy_level == 5
        assert proposal.state == "prepared"
        assert proposal.payload == draft.content
        assert proposal.payload["account"]["id"] == env.alice_mail.id
        assert proposal.payload["body_html"] == (
            "<p>Review this before delivery.</p>"
        )
        assert check.query(EmailOutboundDelivery).count() == 0
    finally:
        check.close()


@pytest.mark.asyncio
async def test_reviewed_http_executor_only_enqueues_after_fresh_human_approval(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'email-route.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    owner = ensure_account(db, "alice")
    db.add(EmailAccount(
        id="mail-alice",
        owner="alice",
        name="Alice Mail",
        enabled=True,
        is_default=True,
        imap_user="alice@example.test",
        smtp_user="alice@example.test",
        from_address="alice@example.test",
    ))
    db.commit()
    prepared = prepare_agent_email_action(
        db,
        owner_username="alice",
        email_account_id="mail-alice",
        to="ada@example.test",
        subject="Route review",
        body="Queue only after approval.",
        idempotency_key="email-route-action",
    )
    db.commit()
    proposal_id = prepared.proposal.id
    db.close()

    class _IdentityAuthority:
        def __init__(self):
            self._config_lock = threading.Lock()
            self._identity_migrations: set[str] = set()
            self.retired_usernames: set[str] = set()
            self.users = {"alice": {}}

        @property
        def is_configured(self):
            return True

    app = FastAPI()
    app.state.auth_manager = _IdentityAuthority()

    @app.middleware("http")
    async def inject_identity(request, call_next):
        request.state.api_token = False
        request.state.current_user = "alice"
        return await call_next(request)

    app.include_router(setup_action_policy_routes(session_factory=factory))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        inspected = await client.get(f"/api/life/actions/{proposal_id}")
        assert inspected.status_code == 200, inspected.text
        action = inspected.json()["action"]
        assert action["reviewed_server_executor"] is True
        assert action["reviewed_server_reversal"] is False

        challenge = await client.post(
            f"/api/life/actions/{proposal_id}/confirmation",
            json={"version": action["version"], "purpose": "approve"},
        )
        assert challenge.status_code == 200, challenge.text
        token = challenge.json()["confirmation_token"]
        challenged = challenge.json()["action"]

        approved = await client.post(
            f"/api/life/actions/{proposal_id}/approve",
            json={
                "version": challenged["version"],
                "confirmation_token": token,
            },
        )
        assert approved.status_code == 200, approved.text
        approved_action = approved.json()["action"]
        assert approved_action["state"] == "approved"

        executed = await client.post(
            f"/api/life/actions/{proposal_id}/execute",
            json={"version": approved_action["version"]},
        )
        assert executed.status_code == 200, executed.text
        payload = executed.json()
        assert payload["action"]["state"] == "executing"
        assert payload["delivery"]["state"] == "queued"
        assert payload["delivery"]["network_performed"] is False

    check = factory()
    try:
        delivery = check.query(EmailOutboundDelivery).one()
        assert delivery.owner_id == owner.id
        assert delivery.state == "queued"
    finally:
        check.close()
        engine.dispose()


def test_legacy_agent_drafts_import_idempotently_without_modifying_source(
    email_outbound_env,
):
    env = email_outbound_env
    source = env.tmp_path / "scheduled_emails.db"
    connection = sqlite3.connect(source)
    connection.execute("""
        CREATE TABLE scheduled_emails (
            id TEXT PRIMARY KEY,
            to_addr TEXT NOT NULL,
            cc TEXT,
            bcc TEXT,
            subject TEXT,
            body TEXT NOT NULL,
            in_reply_to TEXT,
            references_hdr TEXT,
            attachments TEXT,
            send_at TEXT,
            created_at TEXT,
            status TEXT NOT NULL,
            error TEXT,
            owner TEXT,
            account_id TEXT,
            odysseus_kind TEXT
        )
    """)
    connection.execute(
        "INSERT INTO scheduled_emails "
        "(id, to_addr, subject, body, attachments, created_at, status, owner, account_id) "
        "VALUES (?, ?, ?, ?, ?, ?, 'agent_draft', ?, ?)",
        (
            "legacy-1",
            "legacy-recipient@example.test",
            "Legacy draft",
            "Preserve this exact body.",
            "[]",
            "2026-07-01T00:00:00",
            "alice",
            env.alice_mail.id,
        ),
    )
    connection.commit()
    connection.close()
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    first = import_legacy_agent_email_drafts(env.db, source_path=source)
    env.db.commit()
    second = import_legacy_agent_email_drafts(env.db, source_path=source)
    env.db.commit()

    assert first.imported == 1 and first.reused == 0
    assert second.imported == 0 and second.reused == 1
    assert first.source_preserved and second.source_preserved
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    check = sqlite3.connect(source)
    try:
        assert check.execute(
            "SELECT status, subject, body FROM scheduled_emails WHERE id='legacy-1'"
        ).fetchone() == (
            "agent_draft",
            "Legacy draft",
            "Preserve this exact body.",
        )
    finally:
        check.close()
    assert env.db.query(EmailOutboundDraft).count() == 1


def test_legacy_adoption_rolls_back_all_rows_on_ambiguity_and_preserves_source(
    email_outbound_env,
):
    env = email_outbound_env
    source = env.tmp_path / "scheduled-emails-fail-closed.db"
    connection = sqlite3.connect(source)
    connection.execute("""
        CREATE TABLE scheduled_emails (
            id TEXT PRIMARY KEY,
            to_addr TEXT NOT NULL,
            subject TEXT,
            body TEXT NOT NULL,
            status TEXT NOT NULL,
            owner TEXT,
            account_id TEXT
        )
    """)
    connection.executemany(
        "INSERT INTO scheduled_emails "
        "(id, to_addr, subject, body, status, owner, account_id) "
        "VALUES (?, ?, ?, ?, 'agent_draft', ?, ?)",
        [
            (
                "a-valid-first",
                "valid@example.test",
                "Valid before failure",
                "This row must roll back too.",
                "alice",
                env.alice_mail.id,
            ),
            (
                "z-invalid-owner",
                "invalid@example.test",
                "Unknown owner",
                "This forces the atomic rollback.",
                "missing-owner",
                env.alice_mail.id,
            ),
        ],
    )
    connection.commit()
    connection.close()
    before = hashlib.sha256(source.read_bytes()).hexdigest()

    with pytest.raises(EmailOutboundError, match="owner does not exist"):
        adopt_legacy_agent_email_drafts(
            session_factory=env.Session,
            source_path=source,
        )

    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    check = env.Session()
    try:
        assert check.query(ActionProposal).count() == 0
        assert check.query(EmailOutboundDraft).count() == 0
    finally:
        check.close()
