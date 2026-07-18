from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest


def _telegram_config(*, owner="alice", chat_owners=None):
    from src.telegram_bot import TelegramConfig

    return TelegramConfig(
        enabled=True,
        bot_token="1:test",
        webhook_secret="secret",
        allowed_chat_ids=frozenset({"111"}),
        allow_all_chats=False,
        owner=owner,
        session_map={},
        chat_owners=chat_owners or {},
    )


def _ensure_telegram_accounts():
    from core.database import Account, SessionLocal

    db = SessionLocal()
    try:
        for account_id, username in (
            ("account-alice", "telegram-hook-alice"),
            ("account-bob", "telegram-hook-bob"),
        ):
            if db.query(Account).filter(Account.id == account_id).first() is None:
                db.add(Account(
                    id=account_id, username=username, status="active"
                ))
        db.commit()
    finally:
        db.close()


def test_email_parser_preserves_rfc_thread_evidence():
    from routes.email_routes import _parse_email_list_record

    raw = (
        b"Subject: Re: Project\r\n"
        b"From: Alex <alex@example.test>\r\n"
        b"Date: Fri, 17 Jul 2026 09:00:00 +0000\r\n"
        b"Message-ID: <reply@example.test>\r\n"
        b"References: <root@example.test> <middle@example.test>\r\n"
        b"In-Reply-To: <middle@example.test>\r\n\r\n"
    )
    parsed = _parse_email_list_record(
        b"1 (UID 7 FLAGS () RFC822.SIZE 200)", raw,
    )

    assert parsed is not None
    assert parsed["references"] == [
        "<root@example.test>",
        "<middle@example.test>",
    ]
    assert parsed["in_reply_to"] == "<middle@example.test>"


def test_list_and_indexed_search_drain_projection_ledger_before_cache_returns():
    import inspect
    import routes.email_routes as routes

    source = inspect.getsource(routes.setup_email_routes)
    list_drain = source.index("await _asyncio.to_thread(\n            _drain_email_life_projection_ledger")
    memory_hit = source.index("cached = _list_cache_get(ck)")
    index_hit = source.index("indexed = _email_index_page(")
    search_drain = source.index(
        "_drain_email_life_projection_ledger(\n                owner,",
        list_drain + 1,
    )
    indexed_search = source.index("indexed_emails, indexed_total, indexed_at = _email_index_search(")

    assert list_drain < memory_hit < index_hit
    assert search_drain < indexed_search


def test_email_hook_runs_after_index_commit_and_keeps_batch_order(
    tmp_path, monkeypatch,
):
    import routes.email_helpers as helpers
    import routes.email_routes as routes

    path = tmp_path / "scheduled.db"
    monkeypatch.setattr(helpers, "SCHEDULED_DB", path)
    monkeypatch.setattr(routes, "SCHEDULED_DB", path)
    helpers._init_scheduled_db()
    observed = []

    def ingest(**kwargs):
        # A second connection can see the row only after the writer commits.
        with sqlite3.connect(path) as conn:
            rows = conn.execute(
                "SELECT uid, subject FROM email_message_index ORDER BY uid"
            ).fetchall()
        observed.append((rows, [row["uid"] for row in kwargs["emails"]]))

    monkeypatch.setattr(routes, "ingest_email_headers", ingest)
    routes._email_index_upsert(
        "alice",
        "mail-a",
        "INBOX",
        [
            {"uid": "2", "message_id": "<two@test>", "subject": "Two"},
            {"uid": "1", "message_id": "<one@test>", "subject": "One"},
        ],
    )

    assert [entry[0] for entry in observed] == [
        [('1', 'One'), ('2', 'Two')],
        [('1', 'One'), ('2', 'Two')],
    ]
    assert [entry[1][0] for entry in observed] == ["2", "1"]


def test_email_ingestion_failure_retains_cache_and_durable_retry(
    tmp_path, monkeypatch,
):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.database import Account, Base, EmailLifeProjection
    import routes.email_helpers as helpers
    import routes.email_routes as routes
    import src.email_life_projection_ledger as projection_ledger

    path = tmp_path / "scheduled.db"
    authority = create_engine(
        f"sqlite:///{tmp_path / 'authority.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(authority)
    factory = sessionmaker(
        autocommit=False, autoflush=False, bind=authority,
    )
    db = factory()
    db.add(Account(id="account-alice", username="alice", status="active"))
    db.commit()
    db.close()
    monkeypatch.setattr(projection_ledger, "SessionLocal", factory)
    monkeypatch.setattr(helpers, "SCHEDULED_DB", path)
    monkeypatch.setattr(routes, "SCHEDULED_DB", path)
    helpers._init_scheduled_db()

    def fail(**kwargs):
        raise RuntimeError("life database unavailable")

    monkeypatch.setattr(routes, "ingest_email_headers", fail)
    # Projection is a secondary consumer. Once the cache row commits and the
    # canonical SQL outbox accepts its encrypted payload, a Life outage must
    # not fail the authoritative IMAP list/search response.
    routes._email_index_upsert(
        "alice",
        "mail-a",
        "INBOX",
        [{"uid": "1", "message_id": "<one@test>", "subject": "One"}],
    )

    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT subject FROM email_message_index WHERE uid='1'"
        ).fetchone() == ("One",)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='email_life_projection_ledger'"
        ).fetchone() is None
    db = factory()
    projection = db.query(EmailLifeProjection).one()
    assert projection.state == "failed"
    assert projection.payload["subject"] == "One"
    projection.next_attempt_at = datetime(2000, 1, 1)
    db.commit()
    db.close()

    retried = []
    monkeypatch.setattr(
        routes,
        "ingest_email_headers",
        lambda **kwargs: retried.extend(kwargs["emails"]),
    )
    routes._drain_email_life_projection_ledger(
        "alice", "mail-a", "INBOX", raise_on_failure=True,
    )
    assert [row["uid"] for row in retried] == ["1"]
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT subject FROM email_message_index WHERE uid='1'"
        ).fetchone() == ("One",)
    db = factory()
    projection = db.query(EmailLifeProjection).one()
    assert projection.state == "completed"
    assert projection.payload is None
    db.close()
    authority.dispose()


def test_telegram_ingestion_skips_commands_and_unlinked_chats(monkeypatch):
    import routes.telegram_routes as routes
    from src.telegram_bot import TelegramIncomingMessage

    calls = []
    monkeypatch.setattr(
        routes,
        "ingest_telegram_message",
        lambda **kwargs: calls.append(kwargs),
    )
    linked = _telegram_config(owner="alice")
    unlinked = _telegram_config(owner=None)

    assert routes._ingest_telegram_incoming(
        linked,
        TelegramIncomingMessage("111", "/new", 1),
        fingerprint="bot",
        update_id=10,
    ) is None
    assert routes._ingest_telegram_incoming(
        unlinked,
        TelegramIncomingMessage("111", "ordinary", 2),
        fingerprint="bot",
        update_id=11,
    ) is None
    routes._ingest_telegram_incoming(
        linked,
        TelegramIncomingMessage("111", "ordinary", 3),
        fingerprint="bot",
        update_id=12,
    )

    assert len(calls) == 1
    assert calls[0]["owner"] == "alice"
    assert calls[0]["update_id"] == 12


@pytest.mark.asyncio
async def test_telegram_durable_claim_precedes_ingestion_and_duplicate_skips_it(
    tmp_path, monkeypatch,
):
    import routes.telegram_routes as routes
    import src.constants as constants

    _ensure_telegram_accounts()

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    events = []

    def ingest(**kwargs):
        events.append("ingest")

    async def build(*args):
        events.append("build")
        return "answer"

    async def reply(*args):
        events.append("reply")

    monkeypatch.setattr(routes, "ingest_telegram_message", ingest)
    monkeypatch.setattr(routes, "_build_message_reply", build)
    monkeypatch.setattr(routes, "_reply", reply)
    monkeypatch.setattr(
        routes, "_telegram_owner_account_id", lambda *args: "account-alice",
    )
    update = {
        "update_id": 155,
        "message": {"message_id": 7, "chat": {"id": 111}, "text": "hello"},
    }

    await routes._process_update_durably(object(), None, _telegram_config(), update)
    await routes._process_update_durably(object(), None, _telegram_config(), update)

    assert events == ["ingest", "build", "reply"]


@pytest.mark.asyncio
async def test_telegram_ingestion_failure_keeps_claim_retryable(
    tmp_path, monkeypatch,
):
    import routes.telegram_routes as routes
    import src.constants as constants
    from core.database import SessionLocal, TelegramInboundUpdate
    from src.telegram_delivery import telegram_runtime_authority
    from src.telegram_identity import telegram_bot_fingerprint

    _ensure_telegram_accounts()

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    config = _telegram_config()
    update = {
        "update_id": 56,
        "message": {"message_id": 8, "chat": {"id": 111}, "text": "retry me"},
    }
    calls = []

    def fail(**kwargs):
        calls.append("failed-ingest")
        raise RuntimeError("life database unavailable")

    async def build(*args):
        calls.append("build")
        return "answer"

    async def reply(*args):
        calls.append("reply")

    monkeypatch.setattr(routes, "ingest_telegram_message", fail)
    monkeypatch.setattr(routes, "_build_message_reply", build)
    monkeypatch.setattr(routes, "_reply", reply)
    monkeypatch.setattr(
        routes, "_telegram_owner_account_id", lambda *args: "account-alice",
    )
    with pytest.raises(RuntimeError, match="life database unavailable"):
        await routes._process_update_durably(object(), None, config, update)

    fingerprint = telegram_bot_fingerprint(bot_token=config.bot_token)
    assert telegram_runtime_authority.load_inbound_record(
        fingerprint, 56
    )["status"] == "processing"
    db = SessionLocal()
    try:
        db.query(TelegramInboundUpdate).filter(
            TelegramInboundUpdate.bot_fingerprint == fingerprint,
            TelegramInboundUpdate.update_id == 56,
        ).update({
            TelegramInboundUpdate.processing_lease_expires_at: datetime(
                2000, 1, 1
            )
        }, synchronize_session=False)
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(
        routes,
        "ingest_telegram_message",
        lambda **kwargs: calls.append("successful-ingest"),
    )
    await routes._process_update_durably(object(), None, config, update)

    assert calls == ["failed-ingest", "successful-ingest", "build", "reply"]


@pytest.mark.asyncio
async def test_telegram_relink_after_crash_discards_without_new_owner_projection(
    tmp_path, monkeypatch,
):
    import routes.telegram_routes as routes
    import src.constants as constants
    from core.database import SessionLocal, TelegramInboundUpdate
    from src.telegram_delivery import telegram_runtime_authority
    from src.telegram_identity import telegram_bot_fingerprint

    _ensure_telegram_accounts()

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    account_ids = {"alice": "account-alice", "bob": "account-bob"}
    monkeypatch.setattr(
        routes,
        "_telegram_owner_account_id",
        lambda config, incoming: account_ids[str(config.owner)],
    )
    projections = []
    replies = []

    def ingest(**kwargs):
        projections.append(kwargs["expected_owner_id"])

    async def build(*args):
        return "owner-private answer"

    async def reply(*args):
        replies.append(args[-1])

    real_store = telegram_runtime_authority.store_inbound_reply
    monkeypatch.setattr(
        telegram_runtime_authority,
        "store_inbound_reply",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("crash after projection")
        ),
    )
    monkeypatch.setattr(routes, "ingest_telegram_message", ingest)
    monkeypatch.setattr(routes, "_build_message_reply", build)
    monkeypatch.setattr(routes, "_reply", reply)
    update = {
        "update_id": 57,
        "message": {"message_id": 9, "chat": {"id": 111}, "text": "private"},
    }

    with pytest.raises(RuntimeError, match="crash after projection"):
        await routes._process_update_durably(
            object(), None, _telegram_config(owner="alice"), update,
        )

    fingerprint = telegram_bot_fingerprint(bot_token="1:test")
    db = SessionLocal()
    try:
        db.query(TelegramInboundUpdate).filter(
            TelegramInboundUpdate.bot_fingerprint == fingerprint,
            TelegramInboundUpdate.update_id == 57,
        ).update({
            TelegramInboundUpdate.processing_lease_expires_at: datetime(
                2000, 1, 1
            )
        }, synchronize_session=False)
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(
        telegram_runtime_authority, "store_inbound_reply", real_store
    )

    # The same chat is now linked to another Restia account. The old update is
    # acknowledged as terminally discarded: it is neither reprojected into Bob
    # nor replied with Alice's private answer.
    await routes._process_update_durably(
        object(), None, _telegram_config(owner="bob"), update,
    )

    record = telegram_runtime_authority.load_inbound_record(fingerprint, 57)
    assert record["status"] == "discarded"
    assert record["owner_account_id"] == "account-alice"
    assert projections == ["account-alice"]
    assert replies == []


@pytest.mark.asyncio
async def test_telegram_missing_update_id_ingests_before_compatibility_processing(
    monkeypatch,
):
    import routes.telegram_routes as routes

    events = []

    def ingest(**kwargs):
        assert kwargs["update_id"] is None
        events.append("ingest")

    async def process(*args):
        events.append("process")

    monkeypatch.setattr(routes, "ingest_telegram_message", ingest)
    monkeypatch.setattr(routes, "_process_message", process)
    monkeypatch.setattr(
        routes, "_telegram_owner_account_id", lambda *args: "account-alice",
    )
    await routes._process_update_durably(
        object(),
        None,
        _telegram_config(),
        {"message": {"message_id": 9, "chat": {"id": 111}, "text": "fallback"}},
    )

    assert events == ["ingest", "process"]
