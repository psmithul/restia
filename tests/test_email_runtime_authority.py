from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    Base,
    EmailAccount,
    EmailAutomationRun,
    EmailScheduledDelivery,
    EmailTagState,
)
import src.email_runtime_authority as authority


@pytest.fixture()
def email_runtime_env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'main.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = factory()
    db.add(Account(id="account-alice", username="alice", status="active"))
    db.add(EmailAccount(
        id="mail-alice", owner="alice", name="Personal", is_default=True,
        enabled=True, from_address="alice@example.test",
    ))
    db.add(EmailAccount(
        id="mail-alice-work", owner="alice", name="Work", is_default=False,
        enabled=True, from_address="alice-work@example.test",
    ))
    db.commit()
    db.close()
    monkeypatch.setattr(authority, "SessionLocal", factory)
    try:
        yield engine, factory, tmp_path
    finally:
        engine.dispose()


def test_rules_and_tag_payloads_are_account_owned_and_encrypted(email_runtime_env):
    engine, factory, _tmp_path = email_runtime_env
    rules = authority.get_email_automation_rules(
        "alice", legacy_settings={"email_auto_tag": True},
    )
    assert rules["email_auto_tag"] is True
    assert rules["email_auto_spam"] is False
    updated = authority.set_email_automation_rules(
        "account-alice", {"email_auto_spam": True},
    )
    assert updated["email_auto_tag"] is True
    assert updated["email_auto_spam"] is True

    secret = "Private acquisition codename violet-771"
    authority.upsert_email_tag_state(
        owner="alice", account_id="mail-alice",
        message_id="<private-message@example.test>", uid="42",
        folder="INBOX", subject=secret, sender="Alex <alex@example.test>",
        tags=["urgent", "receipt"], spam_verdict=False,
    )
    rows = authority.list_email_tag_states(
        owner="account-alice", account_id="mail-alice", folder="INBOX",
        uids=["42"],
    )
    assert rows[0]["subject"] == secret
    assert rows[0]["tags"] == ["urgent", "receipt"]

    with engine.connect() as connection:
        owner_id, message_digest, location_digest, payload = connection.execute(
            text(
                "SELECT owner_id, message_digest, location_digest, payload "
                "FROM email_tag_states"
            )
        ).one()
        stored_rules = connection.execute(
            text("SELECT rules FROM email_automation_rules")
        ).scalar_one()
    rendering = " ".join(map(str, (payload, stored_rules)))
    assert owner_id == "account-alice"
    assert len(message_digest) == len(location_digest) == 64
    assert secret not in rendering
    assert "private-message" not in rendering
    assert "enc:c1:" in rendering

    db = factory()
    assert db.query(EmailTagState).one().owner_id == "account-alice"
    db.close()


def test_tag_identity_does_not_collapse_messages_without_legacy_uids(
    email_runtime_env,
):
    _engine, factory, _tmp_path = email_runtime_env
    for message_id, tag in (
        ("<first-no-uid@example.test>", "work"),
        ("<second-no-uid@example.test>", "personal"),
    ):
        authority.upsert_email_tag_state(
            owner="alice", account_id="mail-alice",
            message_id=message_id, uid="", folder="INBOX", tags=[tag],
        )

    rows = authority.list_email_tag_states(
        owner="alice", account_id="mail-alice", folder="INBOX",
    )
    assert {row["message_id"] for row in rows} == {
        "<first-no-uid@example.test>",
        "<second-no-uid@example.test>",
    }
    db = factory()
    try:
        assert db.query(EmailTagState).count() == 2
        assert len({row.location_digest for row in db.query(EmailTagState)}) == 2
    finally:
        db.close()


def test_spam_override_is_scoped_to_one_email_account_and_folder(
    email_runtime_env,
):
    _engine, _factory, _tmp_path = email_runtime_env
    for account_id, folder, message_id in (
        ("mail-alice", "INBOX", "<personal-spam@example.test>"),
        ("mail-alice-work", "Archive", "<work-spam@example.test>"),
    ):
        authority.upsert_email_tag_state(
            owner="alice", account_id=account_id, message_id=message_id,
            uid="42", folder=folder, tags=["marketing"], spam_verdict=True,
        )

    assert authority.unflag_email_spam(
        owner="alice", account_id="mail-alice", folder="INBOX", uid="42",
    ) == 1
    personal = authority.list_email_tag_states(
        owner="alice", account_id="mail-alice", uids=["42"],
    )[0]
    work = authority.list_email_tag_states(
        owner="alice", account_id="mail-alice-work", uids=["42"],
    )[0]
    assert personal["spam_verdict"] is False
    assert work["spam_verdict"] is True


def test_automation_claim_is_fenced_and_completion_is_idempotent(email_runtime_env):
    _engine, factory, _tmp_path = email_runtime_env
    claim = authority.claim_email_automation(
        owner="alice", account_id="mail-alice", operation="calendar",
        message_id="<calendar@example.test>",
        payload={"message_id": "<calendar@example.test>", "uid": "8"},
    )
    assert claim is not None
    assert authority.claim_email_automation(
        owner="alice", account_id="mail-alice", operation="calendar",
        message_id="<calendar@example.test>",
    ) is None
    assert authority.complete_email_automation(
        replace(claim, version=claim.version - 1),
        result={"event_uids": ["event-unsafe"]},
    ) is False
    assert authority.complete_email_automation(
        claim, result={"event_uids": ["event-1"]},
    ) is True
    assert authority.complete_email_automation(claim) is False
    results = authority.completed_email_automation_results(
        owner="alice", account_id="mail-alice", operation="calendar",
        message_ids=["<calendar@example.test>"],
    )
    assert results == {
        "<calendar@example.test>": {"event_uids": ["event-1"]},
    }
    db = factory()
    row = db.query(EmailAutomationRun).one()
    assert row.state == "completed"
    assert row.claim_token_digest is None
    db.close()


def test_manual_schedule_uses_database_claim_and_safe_failure_state(email_runtime_env):
    engine, factory, _tmp_path = email_runtime_env
    secret = "Private scheduled message body"
    row = authority.create_scheduled_delivery(
        owner="alice", email_account_id="mail-alice",
        scheduled_for=datetime.utcnow() - timedelta(minutes=1),
        payload={
            "to": "recipient@example.test", "subject": "Private subject",
            "body": secret, "attachments": [],
        },
        delivery_id="schedule-1", idempotency_key="manual:schedule-1",
    )
    assert row.owner_id == "account-alice"
    claim = authority.claim_due_scheduled_delivery()
    assert claim is not None
    assert authority.claim_due_scheduled_delivery() is None
    assert authority.complete_scheduled_delivery(
        replace(claim, version=claim.version - 1)
    ) is False
    assert authority.fail_scheduled_delivery(
        claim, error_code="password=must-not-persist",
    ) is True

    db = factory()
    delivery = db.query(EmailScheduledDelivery).one()
    assert delivery.state == "retry"
    assert delivery.last_error_code == "smtp_failed"
    delivery.next_attempt_at = datetime(2000, 1, 1)
    db.commit()
    db.close()
    retry = authority.claim_due_scheduled_delivery()
    assert retry is not None
    assert authority.complete_scheduled_delivery(
        retry, provider_message_id="<provider-private@example.test>",
    ) is True

    with engine.connect() as connection:
        payload, provider_id, error_code = connection.execute(text(
            "SELECT payload, provider_message_id, last_error_code "
            "FROM email_scheduled_deliveries"
        )).one()
    rendering = f"{payload} {provider_id}"
    assert secret not in rendering
    assert "provider-private" not in rendering
    assert rendering.count("enc:c1:") == 2
    assert error_code is None


def test_legacy_import_is_bounded_replay_safe_and_source_read_only(
    email_runtime_env,
):
    _engine, factory, tmp_path = email_runtime_env
    sidecar = tmp_path / "scheduled_emails.db"
    with sqlite3.connect(sidecar) as connection:
        connection.execute(
            "CREATE TABLE email_tags (message_id TEXT PRIMARY KEY, owner TEXT, "
            "account_id TEXT, uid TEXT, folder TEXT, subject TEXT, sender TEXT, "
            "tags TEXT, spam_verdict INTEGER, spam_reason TEXT, moved_to TEXT, "
            "model_used TEXT)"
        )
        connection.execute(
            "INSERT INTO email_tags VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "<legacy@example.test>", "alice", "mail-alice", "5", "INBOX",
                "Legacy private subject", "sender@example.test", '["receipt"]',
                0, "", "", "legacy-model",
            ),
        )
        connection.execute(
            "CREATE TABLE scheduled_emails (id TEXT PRIMARY KEY, to_addr TEXT, "
            "cc TEXT, bcc TEXT, subject TEXT, body TEXT, in_reply_to TEXT, "
            "references_hdr TEXT, attachments TEXT, send_at TEXT, created_at TEXT, "
            "status TEXT, error TEXT, owner TEXT, account_id TEXT, odysseus_kind TEXT)"
        )
        connection.execute(
            "INSERT INTO scheduled_emails VALUES (?, ?, '', '', ?, ?, '', '', "
            "'[]', ?, ?, 'pending', NULL, 'alice', 'mail-alice', 'scheduled')",
            (
                "legacy-schedule", "recipient@example.test", "Legacy schedule",
                "Legacy body", "2099-01-01T00:00:00",
                "2026-07-17T00:00:00",
            ),
        )
    before = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    first = authority.import_legacy_email_runtime(
        owner="alice", sidecar_path=sidecar, limit=10,
    )
    second = authority.import_legacy_email_runtime(
        owner="alice", sidecar_path=sidecar, limit=10,
    )
    after = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    assert first == {"tags": 1, "schedules": 1, "automation": 1}
    assert second == {"tags": 0, "schedules": 0, "automation": 0}
    assert before == after
    db = factory()
    assert db.query(EmailTagState).count() == 1
    assert db.query(EmailScheduledDelivery).count() == 1
    assert db.query(EmailAutomationRun).count() == 1
    db.close()


def test_minimal_legacy_agent_draft_is_skipped_without_mutating_source(
    email_runtime_env,
):
    _engine, factory, tmp_path = email_runtime_env
    sidecar = tmp_path / "minimal-legacy-schedules.db"
    with sqlite3.connect(sidecar) as connection:
        connection.execute(
            "CREATE TABLE scheduled_emails (id TEXT PRIMARY KEY, "
            "to_addr TEXT, subject TEXT, body TEXT, attachments TEXT, "
            "send_at TEXT, created_at TEXT, status TEXT, account_id TEXT, "
            "owner TEXT)"
        )
        connection.execute(
            "INSERT INTO scheduled_emails VALUES (?, ?, ?, ?, '[]', ?, ?, ?, ?, ?)",
            (
                "legacy-agent-draft", "recipient@example.test", "Private",
                "Must stay behind Level 5 review", "2099-01-01T00:00:00",
                "2026-07-17T00:00:00", "agent_draft", "mail-alice", "alice",
            ),
        )
    before = hashlib.sha256(sidecar.read_bytes()).hexdigest()

    first = authority.import_legacy_email_runtime(
        owner="alice", sidecar_path=sidecar,
    )
    second = authority.import_legacy_email_runtime(
        owner="alice", sidecar_path=sidecar,
    )

    assert first == second == {"tags": 0, "schedules": 0, "automation": 0}
    assert hashlib.sha256(sidecar.read_bytes()).hexdigest() == before
    db = factory()
    try:
        assert db.query(EmailScheduledDelivery).count() == 0
    finally:
        db.close()
