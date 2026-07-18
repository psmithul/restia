from __future__ import annotations

import hashlib
import smtplib
import sqlite3
import uuid
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    ActionProposal,
    Base,
    EmailAccount,
    EmailOutboundDelivery,
    EmailOutboundDraft,
)
from src.action_policy import approve_action, issue_confirmation
from src.audit_context import SESSION_AUDIT_CONTEXT_KEY
from src.email_delivery_worker import (
    EmailDeliveryWorkerError,
    deliver_one_email_outbound,
)
from src.email_outbound import prepare_agent_email_action, queue_approved_email_action


@pytest.fixture()
def worker_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'email-worker.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    alice = Account(id=str(uuid.uuid4()), username="alice")
    bob = Account(id=str(uuid.uuid4()), username="bob")
    db.add_all([alice, bob])
    db.flush()
    mail = EmailAccount(
        id="mail-alice",
        owner="alice",
        name="Alice Mail",
        enabled=True,
        is_default=True,
        imap_host="imap.example.test",
        imap_user="alice@example.test",
        smtp_host="smtp.example.test",
        smtp_user="alice@example.test",
        from_address="alice@example.test",
    )
    db.add(mail)
    db.commit()
    try:
        yield SimpleNamespace(
            Session=factory, db=db, alice=alice, bob=bob, mail=mail
        )
    finally:
        db.close()
        engine.dispose()


def _prepare(env, **overrides):
    values = {
        "owner_username": "alice",
        "email_account_id": env.mail.id,
        "to": "Ada <ada@example.test>",
        "cc": "team@example.test",
        "bcc": "archive@example.test",
        "subject": "Controller review",
        "body": "Please review the controller notes.",
        "body_html": "<p>Please <strong>review</strong>.</p>",
        "attachments": [],
        "source": {"session_id": "chat-1"},
        "idempotency_key": str(uuid.uuid4()),
    }
    values.update(overrides)
    return prepare_agent_email_action(env.db, **values)


def _approve_and_queue(env, **overrides):
    prepared = _prepare(env, **overrides)
    env.db.info[SESSION_AUDIT_CONTEXT_KEY] = {
        "actor_type": "account",
        "actor_id": env.alice.id,
        "credential_type": "session",
        "interface": "web",
    }
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
    queued = queue_approved_email_action(
        env.db,
        account=env.alice,
        proposal_id=approved.id,
        expected_version=approved.version,
    )
    env.db.commit()
    return SimpleNamespace(
        delivery_id=queued.delivery.id,
        draft_id=queued.draft.id,
        proposal_id=queued.proposal.id,
        message_id=queued.delivery.payload["message_id"],
    )


def _deps(env, *, send_smtp, attachment_loader=None, appended=None):
    appended = appended if appended is not None else []

    def get_config(account_id, *, owner):
        assert account_id == env.mail.id
        assert owner == "alice"
        return {
            "account_id": account_id,
            "smtp_host": "smtp.example.test",
            "smtp_port": 465,
            "smtp_user": "alice@example.test",
            "smtp_password": "test-only-password",
            "from_address": "alice@example.test",
        }

    class FakeImap:
        def append(self, folder, flags, date, payload):
            appended.append((folder, flags, date, payload))
            return "OK", [b"stored"]

    @contextmanager
    def imap_factory(account_id, *, owner):
        assert account_id == env.mail.id
        assert owner == "alice"
        yield FakeImap()

    result = {
        "session_factory": env.Session,
        "send_smtp": send_smtp,
        "get_email_config": get_config,
        "imap_factory": imap_factory,
        "detect_sent_folder": lambda _imap: "Sent",
        "quote_folder": lambda folder: folder,
    }
    if attachment_loader is not None:
        result["attachment_loader"] = attachment_loader
    return result


def _delivery(env, delivery_id):
    db = env.Session()
    try:
        return db.query(EmailOutboundDelivery).filter_by(id=delivery_id).one()
    finally:
        db.close()


def test_success_commits_claim_before_smtp_and_completes_exact_snapshot(worker_env):
    env = worker_env
    queued = _approve_and_queue(
        env,
        body_html='<p>Hello<script>steal()</script><a href="javascript:bad">x</a></p>',
    )
    captured = {}
    appended = []

    def send_smtp(config, from_address, recipients, message_bytes):
        check = env.Session()
        try:
            row = check.query(EmailOutboundDelivery).filter_by(
                id=queued.delivery_id
            ).one()
            assert row.state == "claimed"
            assert row.claim_token_digest
        finally:
            check.close()
        captured.update(
            config=config,
            from_address=from_address,
            recipients=recipients,
            message_bytes=message_bytes,
        )

    result = deliver_one_email_outbound(
        **_deps(env, send_smtp=send_smtp, appended=appended)
    )

    assert result is not None and result.state == "delivered"
    assert result.network_performed and result.sent_appended
    assert captured["from_address"] == "alice@example.test"
    assert captured["recipients"] == [
        "ada@example.test", "team@example.test", "archive@example.test"
    ]
    message = BytesParser(policy=policy.default).parsebytes(
        captured["message_bytes"]
    )
    assert message["Message-ID"] == queued.message_id
    assert message["Bcc"] is None
    html_part = message.get_body(preferencelist=("html",)).get_content()
    assert "<script" not in html_part
    assert "javascript:" not in html_part
    assert appended and appended[0][0] == "Sent"
    row = _delivery(env, queued.delivery_id)
    assert row.state == "delivered" and row.last_error_code is None
    check = env.Session()
    try:
        assert check.query(ActionProposal).filter_by(
            id=queued.proposal_id
        ).one().state == "completed"
        assert check.query(EmailOutboundDraft).filter_by(
            id=queued.draft_id
        ).one().state == "delivered"
    finally:
        check.close()


@pytest.mark.parametrize(
    ("failure", "state", "code"),
    [
        (TimeoutError("contains private endpoint"), "retry", "smtp_unavailable"),
        (
            smtplib.SMTPAuthenticationError(535, b"credential rejected: secret"),
            "failed",
            "smtp_auth_failed",
        ),
    ],
)
def test_transport_failures_store_only_stable_codes(
    worker_env, failure, state, code
):
    env = worker_env
    queued = _approve_and_queue(env)

    def send_smtp(*_args):
        raise failure

    result = deliver_one_email_outbound(**_deps(env, send_smtp=send_smtp))
    assert result is not None and result.state == state
    assert result.error_code == code
    row = _delivery(env, queued.delivery_id)
    assert row.state == state and row.last_error_code == code
    assert "private" not in str(row.last_error_code)
    assert "secret" not in str(row.last_error_code)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("from", "account_snapshot_changed"),
        ("owner", "email_account_owner_mismatch"),
        ("digest", "content_digest_mismatch"),
    ],
)
def test_changed_account_owner_or_approved_content_fails_before_network(
    worker_env, mutation, code
):
    env = worker_env
    queued = _approve_and_queue(env)
    if mutation == "from":
        env.mail.from_address = "changed@example.test"
    elif mutation == "owner":
        env.mail.owner = "bob"
    else:
        delivery = env.db.query(EmailOutboundDelivery).filter_by(
            id=queued.delivery_id
        ).one()
        payload = dict(delivery.payload)
        payload["subject"] = "Tampered after approval"
        delivery.payload = payload
    env.db.commit()

    result = deliver_one_email_outbound(
        **_deps(
            env,
            send_smtp=lambda *_args: pytest.fail("SMTP must not be reached"),
        )
    )
    assert result is not None and result.state == "failed"
    assert result.error_code == code and not result.network_performed
    assert _delivery(env, queued.delivery_id).last_error_code == code


@pytest.mark.parametrize(
    ("loader", "code"),
    [
        (
            lambda _item, _owner: (_ for _ in ()).throw(FileNotFoundError()),
            "attachment_unavailable",
        ),
        (lambda _item, _owner: (b"different", "text/plain"),
         "attachment_digest_mismatch"),
        (
            lambda _item, _owner: (_ for _ in ()).throw(
                EmailDeliveryWorkerError("attachment_owner_mismatch")
            ),
            "attachment_owner_mismatch",
        ),
    ],
)
def test_attachment_resolution_is_fail_closed(worker_env, loader, code):
    env = worker_env
    approved_bytes = b"approved attachment"
    queued = _approve_and_queue(
        env,
        attachments=[{
            "id": "upload-1",
            "name": "review.txt",
            "content_type": "text/plain",
            "sha256": hashlib.sha256(approved_bytes).hexdigest(),
        }],
    )
    result = deliver_one_email_outbound(
        **_deps(
            env,
            send_smtp=lambda *_args: pytest.fail("SMTP must not be reached"),
            attachment_loader=loader,
        )
    )
    assert result is not None and result.state == "failed"
    assert result.error_code == code and not result.network_performed
    assert _delivery(env, queued.delivery_id).last_error_code == code


def test_worker_has_nothing_to_claim_before_human_approval(worker_env):
    env = worker_env
    _prepare(env)
    env.db.commit()
    calls = []
    result = deliver_one_email_outbound(
        **_deps(env, send_smtp=lambda *_args: calls.append(True))
    )
    assert result is None
    assert calls == []
    assert env.db.query(EmailOutboundDelivery).count() == 0


def test_scheduled_poller_registers_canonical_outbox_drain(tmp_path, monkeypatch):
    import routes.email_pollers as pollers

    scheduled_db = tmp_path / "scheduled-emails.db"
    connection = sqlite3.connect(scheduled_db)
    connection.execute("""
        CREATE TABLE scheduled_emails (
            id TEXT PRIMARY KEY,
            to_addr TEXT,
            cc TEXT,
            bcc TEXT,
            subject TEXT,
            body TEXT,
            in_reply_to TEXT,
            references_hdr TEXT,
            attachments TEXT,
            account_id TEXT,
            odysseus_kind TEXT,
            owner TEXT,
            send_at TEXT,
            status TEXT,
            error TEXT
        )
    """)
    connection.commit()
    connection.close()
    calls = []

    def drain():
        calls.append(True)
        return {
            "attempted": 2,
            "delivered": ["approved-1"],
            "retried": [{"id": "approved-2", "error": "smtp_unavailable"}],
            "failed": [],
            "incomplete": [],
            "at_least_once": True,
        }

    monkeypatch.setattr(pollers, "SCHEDULED_DB", scheduled_db)
    monkeypatch.setattr(pollers, "drain_email_outbox_once", drain)

    result = pollers._scheduled_poll_once()

    assert calls == [True]
    assert result["sent"] == ["approved-1"]
    assert result["failed"] == [
        {"id": "approved-2", "error": "smtp_unavailable"}
    ]
    assert result["canonical"]["attempted"] == 2
    assert result["canonical"]["at_least_once"] is True
