from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from core.database import Account, Base
from src.telegram_delivery import (
    TelegramInboundInFlight,
    TelegramPollingLeaseLost,
    TelegramRuntimeAuthority,
)


@pytest.fixture()
def authority(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'runtime.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    db = factory()
    db.add_all([
        Account(id="account-alice", username="alice", status="active"),
        Account(id="account-bob", username="bob", status="active"),
    ])
    db.commit()
    db.close()
    return TelegramRuntimeAuthority(factory), factory, engine


def _bot(label: str = "bot") -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def test_two_workers_are_database_fenced_and_cursor_survives_restart(authority):
    port, factory, _engine = authority
    other = TelegramRuntimeAuthority(factory)
    clock = datetime(2026, 7, 17, 10, 0, 0)
    first = port.acquire_polling_lease(
        bot_fingerprint=_bot(), worker_id="worker-a", now=clock,
    )
    assert first is not None
    assert other.acquire_polling_lease(
        bot_fingerprint=_bot(), worker_id="worker-b", now=clock,
    ) is None

    renewed = port.renew_polling_lease(first, now=clock + timedelta(seconds=30))
    assert renewed is not None
    assert renewed.fencing_token == first.fencing_token
    takeover = other.acquire_polling_lease(
        bot_fingerprint=_bot(),
        worker_id="worker-b",
        now=clock + timedelta(seconds=121),
    )
    assert takeover is not None
    assert takeover.fencing_token > first.fencing_token
    with pytest.raises(TelegramPollingLeaseLost):
        port.advance_polling_cursor(
            first, 41, now=clock + timedelta(seconds=122)
        )

    assert other.advance_polling_cursor(
        takeover, 41, now=clock + timedelta(seconds=122)
    ) == 41
    restarted = TelegramRuntimeAuthority(factory)
    assert restarted.polling_cursor(_bot()) == 41


def test_poison_attempts_dead_letter_and_cursor_commit_atomically(authority):
    port, factory, _engine = authority
    clock = datetime(2026, 7, 17, 11, 0, 0)
    lease = port.acquire_polling_lease(
        bot_fingerprint=_bot("poison"), worker_id="worker", now=clock,
    )
    assert lease is not None
    for expected in (1, 2):
        result = port.record_handler_failure(
            lease,
            update_id=50,
            error_type="private message must not be stored",
            max_attempts=3,
            now=clock + timedelta(seconds=expected),
        )
        assert result.attempts == expected
        assert result.resolved is False
        assert result.next_offset is None
    resolved = port.record_handler_failure(
        lease,
        update_id=50,
        error_type="private message must not be stored",
        max_attempts=3,
        now=clock + timedelta(seconds=3),
    )
    assert resolved.resolved is True
    assert resolved.next_offset == 51
    assert port.dead_letter_count(_bot("poison")) == 1

    db = factory()
    try:
        row = db.execute(text(
            "SELECT error_type, attempts FROM telegram_dead_letters"
        )).one()
    finally:
        db.close()
    assert row == ("RuntimeError", 3)


def test_inbound_owner_processing_and_reply_claims_are_independently_fenced(
    authority,
):
    port, factory, _engine = authority
    other = TelegramRuntimeAuthority(factory)
    clock = datetime(2026, 7, 17, 12, 0, 0)
    acquired, processing = port.claim_inbound_processing(
        bot_fingerprint=_bot("inbound"),
        update_id=7,
        chat_id="private-chat",
        owner_account_id="account-alice",
        worker_id="worker-a",
        now=clock,
    )
    assert acquired is True
    assert processing is not None
    duplicate, duplicate_record = other.claim_inbound_processing(
        bot_fingerprint=_bot("inbound"),
        update_id=7,
        chat_id="private-chat",
        owner_account_id="account-alice",
        worker_id="worker-b",
        now=clock + timedelta(seconds=1),
    )
    assert duplicate is False
    assert duplicate_record["status"] == "processing"

    delivery = port.store_inbound_reply(
        bot_fingerprint=_bot("inbound"),
        update_id=7,
        chat_id="private-chat",
        owner_account_id="account-alice",
        reply_text="private reply",
        processing_claim_token=processing["processing_claim_token"],
        now=clock + timedelta(seconds=2),
    )
    claimed, pending = other.claim_reply_delivery(
        bot_fingerprint=_bot("inbound"),
        update_id=7,
        chat_id="private-chat",
        owner_account_id="account-alice",
        worker_id="worker-b",
        now=clock + timedelta(seconds=3),
    )
    assert claimed is False
    assert pending["status"] == "reply_pending"
    assert port.mark_inbound_delivered(
        bot_fingerprint=_bot("inbound"),
        update_id=7,
        reply_claim_token="wrong-token",
        now=clock + timedelta(seconds=4),
    ) is False
    assert port.mark_inbound_delivered(
        bot_fingerprint=_bot("inbound"),
        update_id=7,
        reply_claim_token=delivery["reply_claim_token"],
        now=clock + timedelta(seconds=4),
    ) is True
    assert other.claim_inbound_processing(
        bot_fingerprint=_bot("inbound"),
        update_id=7,
        chat_id="private-chat",
        owner_account_id="account-alice",
        worker_id="worker-b",
        now=clock + timedelta(seconds=5),
    )[1]["status"] == "delivered"


def test_restart_reclaims_expired_processing_but_old_claim_is_fenced(authority):
    port, factory, _engine = authority
    restarted = TelegramRuntimeAuthority(factory)
    clock = datetime(2026, 7, 17, 13, 0, 0)
    acquired, old = port.claim_inbound_processing(
        bot_fingerprint=_bot("restart"),
        update_id=8,
        chat_id="chat",
        owner_account_id="account-alice",
        worker_id="old-worker",
        now=clock,
    )
    assert acquired is True
    recovered, new = restarted.claim_inbound_processing(
        bot_fingerprint=_bot("restart"),
        update_id=8,
        chat_id="chat",
        owner_account_id="account-alice",
        worker_id="new-worker",
        now=clock + timedelta(minutes=6),
    )
    assert recovered is True
    with pytest.raises(TelegramInboundInFlight):
        port.store_inbound_reply(
            bot_fingerprint=_bot("restart"),
            update_id=8,
            chat_id="chat",
            owner_account_id="account-alice",
            reply_text="stale",
            processing_claim_token=old["processing_claim_token"],
            now=clock + timedelta(minutes=6, seconds=1),
        )
    delivery = restarted.store_inbound_reply(
        bot_fingerprint=_bot("restart"),
        update_id=8,
        chat_id="chat",
        owner_account_id="account-alice",
        reply_text="fresh",
        processing_claim_token=new["processing_claim_token"],
        now=clock + timedelta(minutes=6, seconds=1),
    )
    assert delivery["reply_text"] == "fresh"


def test_relink_during_crash_discards_without_cross_owner_retry(authority):
    port, _factory, _engine = authority
    clock = datetime(2026, 7, 17, 14, 0, 0)
    port.claim_inbound_processing(
        bot_fingerprint=_bot("relink"),
        update_id=9,
        chat_id="chat",
        owner_account_id="account-alice",
        worker_id="old-worker",
        now=clock,
    )
    acquired, record = port.claim_inbound_processing(
        bot_fingerprint=_bot("relink"),
        update_id=9,
        chat_id="chat",
        owner_account_id="account-bob",
        worker_id="new-worker",
        now=clock + timedelta(minutes=6),
    )
    assert acquired is False
    assert record == {
        "chat_id": "chat",
        "reply_text": "",
        "owner_account_id": "account-alice",
        "status": "discarded",
    }


def test_legacy_sidecars_are_bounded_import_only_and_preserved(
    authority, tmp_path,
):
    port, _factory, _engine = authority
    from src.telegram_inbound_ledger import (
        bot_fingerprint as legacy_fingerprint,
        claim_inbound_processing,
        store_inbound_reply,
    )

    token = "123456:abcdefghijklmnopqrstuvwxyz"
    full_legacy = hashlib.sha256(token.encode("utf-8")).hexdigest()
    (tmp_path / "telegram_polling_state.json").write_text(json.dumps({
        "bot_fingerprint": full_legacy,
        "offset": 77,
    }), encoding="utf-8")
    (tmp_path / "telegram_dead_letters.json").write_text(json.dumps([{
        "update_id": 70,
        "failed_at": 1_700_000_000,
        "error_type": "ValueError",
        "attempts": 3,
    }]), encoding="utf-8")
    inbound = tmp_path / "telegram_inbound_ledger.sqlite3"
    acquired, _ = claim_inbound_processing(
        inbound,
        fingerprint=legacy_fingerprint(token),
        update_id=75,
        chat_id="legacy-chat",
        owner_account_id="account-alice",
    )
    assert acquired is True
    store_inbound_reply(
        inbound,
        fingerprint=legacy_fingerprint(token),
        update_id=75,
        chat_id="legacy-chat",
        owner_account_id="account-alice",
        reply_text="legacy reply",
    )

    result = port.adopt_legacy_sidecars(
        bot_fingerprint=_bot("canonical"),
        bot_token=token,
        data_dir=tmp_path,
    )
    assert result.offset_imported is True
    assert result.dead_letters_imported == 1
    assert result.inbound_imported == 1
    assert port.polling_cursor(_bot("canonical")) == 77
    assert port.load_inbound_record(_bot("canonical"), 75) == {
        "chat_id": "legacy-chat",
        "reply_text": "legacy reply",
        "owner_account_id": "account-alice",
        "status": "reply_pending",
    }
    assert inbound.exists()
    assert port.adopt_legacy_sidecars(
        bot_fingerprint=_bot("canonical"),
        bot_token=token,
        data_dir=tmp_path,
    ).state == "already_imported"


def test_foreign_bot_dead_letters_are_ignored_without_exact_offset_scope(
    authority, tmp_path,
):
    port, _factory, _engine = authority
    token = "123456:abcdefghijklmnopqrstuvwxyz"
    (tmp_path / "telegram_polling_state.json").write_text(json.dumps({
        "bot_fingerprint": hashlib.sha256(b"another-token").hexdigest(),
        "offset": 900,
    }), encoding="utf-8")
    (tmp_path / "telegram_dead_letters.json").write_text(json.dumps([{
        "update_id": 899,
        "error_type": "ValueError",
        "attempts": 3,
    }]), encoding="utf-8")

    result = port.adopt_legacy_sidecars(
        bot_fingerprint=_bot("foreign-scope"),
        bot_token=token,
        data_dir=tmp_path,
    )
    assert result.offset_imported is False
    assert result.dead_letters_imported == 0
    assert port.polling_cursor(_bot("foreign-scope")) is None


def test_tasks_disabled_does_not_disable_telegram_polling(monkeypatch):
    from src.telegram_runtime import inprocess_telegram_polling_enabled

    monkeypatch.setenv("RESTIA_INPROCESS_TASKS", "0")
    monkeypatch.setenv("ODYSSEUS_INPROCESS_TASKS", "0")
    monkeypatch.delenv("RESTIA_INPROCESS_TELEGRAM", raising=False)
    monkeypatch.delenv("ODYSSEUS_INPROCESS_TELEGRAM", raising=False)
    assert inprocess_telegram_polling_enabled() is True


def test_revision_0009_is_in_the_executable_head_chain(tmp_path):
    from src.database_migrations import (
        SCHEMA_HEAD_REVISION,
        schema_revision_status,
        upgrade_schema,
        validate_head_schema,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    result = upgrade_schema(engine)
    assert result.current_revisions == (SCHEMA_HEAD_REVISION,)
    assert SCHEMA_HEAD_REVISION >= "20260724_0009"
    assert schema_revision_status(engine).matches_expected is True
    validate_head_schema(engine)
    assert {
        "telegram_polling_states",
        "telegram_dead_letters",
        "telegram_inbound_updates",
        "telegram_runtime_import_runs",
    } <= set(inspect(engine).get_table_names())


def test_revision_0009_refuses_to_downgrade_retained_runtime_state(tmp_path):
    from alembic import command

    from src.database_migrations import (
        _alembic_config,
        schema_revision_status,
        upgrade_schema,
    )

    engine = create_engine(f"sqlite:///{tmp_path / 'downgrade.db'}")
    upgrade_schema(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO telegram_polling_states(bot_fingerprint) "
            "VALUES (:fingerprint)"
        ), {"fingerprint": _bot("retained")})
    config = _alembic_config(str(engine.url))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        with pytest.raises(RuntimeError, match="contains polling or delivery state"):
            command.downgrade(config, "20260723_0008")
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM telegram_polling_states"))
    config = _alembic_config(str(engine.url))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "20260723_0008")
    assert schema_revision_status(engine).current_revisions == ("20260723_0008",)
    assert "telegram_polling_states" not in inspect(engine).get_table_names()
