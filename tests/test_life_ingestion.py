from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    ActionAudit,
    Base,
    CalendarEvent,
    EntityLink,
    InboxItem,
    LifeEntity,
    LifeSource,
    PlanningItem,
)
from src.life_ingestion import (
    LifeIngestionError,
    ingest_email_headers,
    ingest_inbox_capture,
    ingest_telegram_message,
)


@pytest.fixture()
def ingestion_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'life-ingestion.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield SimpleNamespace(Session=factory, engine=engine)
    engine.dispose()


def _email(
    *,
    uid: str,
    message_id: str = "",
    subject: str = "Status update",
    references: list[str] | None = None,
    in_reply_to: str = "",
) -> dict:
    return {
        "uid": uid,
        "message_id": message_id,
        "subject": subject,
        "from_name": "Alex",
        "from_address": "alex@example.test",
        "date": "2026-07-17T09:00:00+00:00",
        "date_epoch": 1784278800,
        "references": references or [],
        "in_reply_to": in_reply_to,
    }


def test_email_projection_is_idempotent_and_groups_only_by_rfc_thread_evidence(
    ingestion_db,
):
    root = _email(uid="1", message_id="<root@example.test>")
    reply = _email(
        uid="2",
        message_id="<reply@example.test>",
        subject="Re: Status update",
        references=["<root@example.test>"],
        in_reply_to="<root@example.test>",
    )

    first = ingest_email_headers(
        owner="Alice",
        account_id="mail-a",
        folder="INBOX",
        emails=[root, reply],
        session_factory=ingestion_db.Session,
    )
    retried = ingest_email_headers(
        owner="alice",
        account_id="mail-a",
        folder="Archive",
        emails=[root, reply],
        session_factory=ingestion_db.Session,
    )

    assert first[0].thread_id == first[1].thread_id
    assert [row.source_id for row in retried] == [row.source_id for row in first]
    assert all(not row.source_created for row in retried)
    assert all(not row.message_created for row in retried)
    assert all(not row.link_created for row in retried)

    db = ingestion_db.Session()
    try:
        account = db.query(Account).filter_by(username="alice").one()
        assert db.query(LifeSource).filter_by(owner_id=account.id).count() == 2
        assert db.query(LifeEntity).filter_by(
            owner_id=account.id, entity_type="communication_thread"
        ).count() == 1
        messages = db.query(LifeEntity).filter_by(
            owner_id=account.id, entity_type="message"
        ).all()
        assert len(messages) == 2
        assert {row.domain_ref_type for row in messages} == {"life_source"}
        assert db.query(EntityLink).filter_by(owner_id=account.id).count() == 2
        assert db.query(PlanningItem).count() == 0
        assert db.query(CalendarEvent).count() == 0
        audits = db.query(ActionAudit).all()
        assert audits
        assert {row.details["audit"]["interface"] for row in audits} == {"email"}
        assert {row.details["audit"]["actor_type"] for row in audits} == {
            "connector"
        }
    finally:
        db.close()


@pytest.mark.parametrize(
    ("public_source", "stored_source"),
    [
        ("thoughts", "text"),
        ("voice notes", "voice"),
        ("screenshots", "screenshot"),
        ("links", "link"),
        ("emails", "email"),
        ("WhatsApp", "whatsapp"),
        ("files", "file"),
        ("meeting notes", "meeting_note"),
        ("tasks", "task"),
        ("ideas", "idea"),
        ("receipts", "receipt"),
        ("reminders", "reminder"),
        ("saved posts", "saved_post"),
        ("research papers", "research_paper"),
    ],
)
def test_universal_inbox_accepts_every_named_capture_source_owner_scoped(
    ingestion_db, public_source, stored_source,
):
    result = ingest_inbox_capture(
        owner="alice",
        source_type=public_source,
        title=f"Capture from {public_source}",
        content="Destination-free evidence",
        metadata={
            "ingestion_contract": {"owner_scoped": False},
            "client_label": public_source,
        },
        idempotency_key=f"named-source:{stored_source}",
        session_factory=ingestion_db.Session,
    )

    db = ingestion_db.Session()
    try:
        item = db.query(InboxItem).filter_by(id=result.inbox_id).one()
        contract = item.meta_data["ingestion_contract"]
        assert result.owner_id == item.owner_id
        assert result.source_type == item.source_type == stored_source
        assert contract == {
            "version": 1,
            "source_type": stored_source,
            "owner_scoped": True,
            "destination_required": False,
            "classification_can_execute_external_action": False,
            "model_output_has_write_authority": False,
        }
        assert item.meta_data["client_label"] == public_source
        audit = db.query(ActionAudit).filter_by(owner_id=item.owner_id).one()
        assert audit.details["audit"]["interface"] == "domain_service"
    finally:
        db.close()


def test_universal_inbox_rejects_unknown_capture_sources(ingestion_db):
    with pytest.raises(
        LifeIngestionError, match="Unsupported Universal Inbox capture source"
    ):
        ingest_inbox_capture(
            owner="alice",
            source_type="unreviewed external destination",
            content="Must fail before storage",
            session_factory=ingestion_db.Session,
        )

    with ingestion_db.Session() as db:
        assert db.query(InboxItem).count() == 0


def test_email_identity_is_owner_and_mailbox_scoped(ingestion_db):
    row = _email(uid="7", message_id="<shared@example.test>")
    alice_a = ingest_email_headers(
        owner="alice",
        account_id="mail-a",
        folder="INBOX",
        emails=[row],
        session_factory=ingestion_db.Session,
    )[0]
    alice_b = ingest_email_headers(
        owner="alice",
        account_id="mail-b",
        folder="INBOX",
        emails=[row],
        session_factory=ingestion_db.Session,
    )[0]
    bob_a = ingest_email_headers(
        owner="bob",
        account_id="mail-a",
        folder="INBOX",
        emails=[row],
        session_factory=ingestion_db.Session,
    )[0]

    assert len({alice_a.source_id, alice_b.source_id, bob_a.source_id}) == 3
    assert alice_a.owner_id != bob_a.owner_id
    assert alice_a.thread_id != alice_b.thread_id
    assert alice_a.thread_id != bob_a.thread_id


def test_email_missing_message_id_fallback_detects_uid_reuse(ingestion_db):
    first_row = _email(uid="9", message_id="", subject="First occupant")
    reused_uid = _email(uid="9", message_id="", subject="Different occupant")

    first = ingest_email_headers(
        owner="alice",
        account_id="mail-a",
        folder="INBOX",
        emails=[first_row],
        session_factory=ingestion_db.Session,
    )[0]
    retry = ingest_email_headers(
        owner="alice",
        account_id="mail-a",
        folder="INBOX",
        emails=[first_row],
        session_factory=ingestion_db.Session,
    )[0]
    recycled = ingest_email_headers(
        owner="alice",
        account_id="mail-a",
        folder="INBOX",
        emails=[reused_uid],
        session_factory=ingestion_db.Session,
    )[0]

    assert retry.source_id == first.source_id
    assert retry.source_created is False
    assert recycled.source_id != first.source_id
    assert recycled.thread_id != first.thread_id


def test_telegram_projection_reuses_chat_thread_and_has_deterministic_fallback(
    ingestion_db,
):
    first = ingest_telegram_message(
        owner="alice",
        bot_fingerprint="bot-a",
        chat_id="111",
        text="First message",
        message_id=10,
        update_id=100,
        session_factory=ingestion_db.Session,
    )
    second = ingest_telegram_message(
        owner="alice",
        bot_fingerprint="bot-a",
        chat_id="111",
        text="Second message",
        message_id=11,
        update_id=101,
        session_factory=ingestion_db.Session,
    )
    retry = ingest_telegram_message(
        owner="alice",
        bot_fingerprint="bot-a",
        chat_id="111",
        text="First message",
        message_id=10,
        update_id=100,
        session_factory=ingestion_db.Session,
    )
    fallback = ingest_telegram_message(
        owner="alice",
        bot_fingerprint="bot-a",
        chat_id="111",
        text="Compatibility message",
        message_id=12,
        update_id=None,
        session_factory=ingestion_db.Session,
    )
    fallback_retry = ingest_telegram_message(
        owner="alice",
        bot_fingerprint="bot-a",
        chat_id="111",
        text="Compatibility message",
        message_id=12,
        update_id=None,
        session_factory=ingestion_db.Session,
    )
    edited = ingest_telegram_message(
        owner="alice",
        bot_fingerprint="bot-a",
        chat_id="111",
        text="Edited compatibility message",
        message_id=12,
        update_id=None,
        session_factory=ingestion_db.Session,
    )

    assert first.thread_id == second.thread_id == fallback.thread_id == edited.thread_id
    assert retry.source_id == first.source_id
    assert retry.source_created is False
    assert fallback_retry.source_id == fallback.source_id
    assert edited.source_id != fallback.source_id


def test_telegram_identity_is_owner_scoped(ingestion_db):
    kwargs = {
        "bot_fingerprint": "bot-a",
        "chat_id": "111",
        "text": "Same evidence",
        "message_id": 5,
        "update_id": 50,
        "session_factory": ingestion_db.Session,
    }
    alice = ingest_telegram_message(owner="alice", **kwargs)
    bob = ingest_telegram_message(owner="bob", **kwargs)

    assert alice.owner_id != bob.owner_id
    assert alice.source_id != bob.source_id
    assert alice.thread_id != bob.thread_id


def test_connector_content_and_transport_identifiers_are_encrypted_at_rest(
    ingestion_db,
):
    secret_text = "Private Telegram evidence 7f8a3"
    private_chat_id = "987654321"
    ingest_telegram_message(
        owner="alice",
        bot_fingerprint="bot-private",
        chat_id=private_chat_id,
        text=secret_text,
        message_id=77,
        update_id=700,
        session_factory=ingestion_db.Session,
    )

    with ingestion_db.engine.connect() as connection:
        source_rows = connection.exec_driver_sql(
            "SELECT title, source_ref, safe_excerpt, metadata "
            "FROM life_sources"
        ).fetchall()
        entity_rows = connection.exec_driver_sql(
            "SELECT title, summary, properties, provenance FROM life_entities"
        ).fetchall()
        link_rows = connection.exec_driver_sql(
            "SELECT metadata, provenance FROM entity_links"
        ).fetchall()
        audit_rows = connection.exec_driver_sql(
            "SELECT action, entity_type, before_state, after_state, details "
            "FROM action_audit"
        ).fetchall()

    encrypted_rendering = " ".join(
        str(value)
        for rows in (source_rows, entity_rows, link_rows)
        for row in rows
        for value in row
    )
    assert "enc:c1:" in encrypted_rendering
    assert secret_text not in encrypted_rendering
    assert private_chat_id not in encrypted_rendering
    audit_rendering = " ".join(str(value) for row in audit_rows for value in row)
    assert secret_text not in audit_rendering
    assert private_chat_id not in audit_rendering
    with ingestion_db.Session() as db:
        audits = db.query(ActionAudit).all()
        assert {row.details["audit"]["interface"] for row in audits} == {
            "telegram"
        }
        assert {row.details["audit"]["actor_type"] for row in audits} == {
            "connector"
        }


def test_projection_failure_rolls_back_every_graph_record(
    ingestion_db, monkeypatch,
):
    import src.life_ingestion as ingestion

    def fail_link(*args, **kwargs):
        raise RuntimeError("link unavailable")

    monkeypatch.setattr(ingestion, "create_entity_link", fail_link)
    with pytest.raises(LifeIngestionError, match="Telegram Life ingestion failed"):
        ingest_telegram_message(
            owner="alice",
            bot_fingerprint="bot-a",
            chat_id="111",
            text="Atomic message",
            message_id=5,
            update_id=50,
            session_factory=ingestion_db.Session,
        )

    db = ingestion_db.Session()
    try:
        assert db.query(Account).count() == 0
        assert db.query(LifeSource).count() == 0
        assert db.query(LifeEntity).count() == 0
        assert db.query(EntityLink).count() == 0
    finally:
        db.close()


def test_telegram_projection_fails_closed_if_account_binding_changes(ingestion_db):
    with pytest.raises(LifeIngestionError, match="Telegram Life ingestion failed"):
        ingest_telegram_message(
            owner="alice",
            bot_fingerprint="bot-a",
            chat_id="111",
            text="Do not rebind this message",
            message_id=5,
            update_id=50,
            expected_owner_id="different-account-id",
            session_factory=ingestion_db.Session,
        )

    db = ingestion_db.Session()
    try:
        assert db.query(Account).count() == 0
        assert db.query(LifeSource).count() == 0
        assert db.query(LifeEntity).count() == 0
        assert db.query(EntityLink).count() == 0
    finally:
        db.close()
