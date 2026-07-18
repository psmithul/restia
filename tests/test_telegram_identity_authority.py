from __future__ import annotations

import hashlib
from datetime import timedelta, timezone

import pytest
from alembic import command
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    AuthIdentity,
    Base,
    TelegramConversationBinding,
    TelegramIdentityImportRun,
    TelegramLinkCode,
    TelegramPrincipal,
)
from src.identity import ensure_account
import src.secret_storage as secret_storage
import src.telegram_identity as authority
from src.database_migrations import (
    LEGACY_BASELINE_REVISION,
    _alembic_config,
    schema_revision_status,
)


@pytest.fixture
def telegram_store(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(secret_storage, "_digest_key", None)
    engine = create_engine(f"sqlite:///{tmp_path / 'telegram-authority.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(authority, "SessionLocal", factory)
    authority.unlock_telegram_identity_authority()
    yield engine, factory
    authority.unlock_telegram_identity_authority()
    engine.dispose()


def _account(factory, username: str) -> str:
    db = factory()
    try:
        account = ensure_account(db, username)
        db.commit()
        return str(account.id)
    finally:
        db.close()


def test_link_codes_are_private_expiring_transactional_and_single_use(
    telegram_store, monkeypatch
):
    engine, factory = telegram_store
    account_id = _account(factory, "alice")
    bot = authority.telegram_bot_fingerprint(bot_id="bot-1")

    code, expires_at = authority.create_link_code_for_owner(
        "alice", bot, ttl_seconds=600
    )
    assert expires_at > 0
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT account_id, code_digest FROM telegram_link_codes"
        )).one()
    assert row.account_id == account_id
    assert row.code_digest != code
    assert code not in str(row)

    assert authority.consume_link_code_for_chat(code, "987654", bot) == "alice"
    assert authority.consume_link_code_for_chat(code, "987654", bot) is None
    with engine.connect() as connection:
        stored_chat = connection.execute(text(
            "SELECT chat_id, chat_id_digest FROM telegram_principals"
        )).one()
    assert stored_chat.chat_id.startswith("enc:c1:")
    assert "987654" not in str(stored_chat)
    assert authority.load_telegram_authority_projection(bot).chat_owners == {
        "987654": "alice"
    }

    expired_code, _ = authority.create_link_code_for_owner("alice", bot)
    original_now = authority._utcnow()
    monkeypatch.setattr(
        authority, "_utcnow", lambda: original_now + timedelta(hours=2)
    )
    assert authority.consume_link_code_for_chat(
        expired_code, "987654", bot
    ) is None


def test_relink_changes_account_id_and_invalidates_old_conversation(
    telegram_store,
):
    _engine, factory = telegram_store
    alice_id = _account(factory, "alice")
    bob_id = _account(factory, "bob")
    bot = authority.telegram_bot_fingerprint(bot_id="bot-1")

    alice_code, _ = authority.create_link_code_for_owner("alice", bot)
    assert authority.consume_link_code_for_chat(
        alice_code, "4242", bot
    ) == "alice"
    authority.set_conversation_binding(bot, "4242", "alice-session")
    assert authority.get_conversation_binding(bot, "4242") == "alice-session"

    bob_code, _ = authority.create_link_code_for_owner("bob", bot)
    assert authority.consume_link_code_for_chat(bob_code, "4242", bot) == "bob"
    assert authority.get_conversation_binding(bot, "4242") is None
    projection = authority.load_telegram_authority_projection(bot)
    assert projection.chat_owners == {"4242": "bob"}

    db = factory()
    try:
        principal = db.query(TelegramPrincipal).one()
        assert principal.account_id == bob_id
        assert principal.account_id != alice_id
        assert db.query(TelegramConversationBinding).count() == 0
    finally:
        db.close()


def test_legacy_import_is_idempotent_owner_scoped_and_retains_source(
    telegram_store, tmp_path
):
    engine, factory = telegram_store
    _account(factory, "alice")
    bot_id = "legacy-bot"
    plaintext_code = "OLDLINK8"
    legacy_digest = hashlib.sha256(plaintext_code.encode("utf-8")).hexdigest()
    expires = int(
        (authority._utcnow() + timedelta(minutes=5))
        .replace(tzinfo=timezone.utc)
        .timestamp()
    )
    settings = {
        "telegram_bot_id": bot_id,
        "telegram_chat_owners": {"111222": "alice"},
        "telegram_allowed_chat_ids": ["111222"],
        "telegram_session_map": {"111222": "legacy-session"},
        "telegram_link_codes": {
            legacy_digest: {"owner": "alice", "expires_at": expires}
        },
        "telegram_owner": "alice",
    }
    source = tmp_path / "settings.json"
    source.write_text('{"recovery":"source stays intact"}', encoding="utf-8")

    result = authority.adopt_legacy_telegram_identity(
        settings=settings,
        source_path=source,
        auth_enabled=True,
    )
    assert result.state == "completed"
    assert (result.principals, result.bindings, result.link_codes) == (1, 1, 1)
    assert source.read_text(encoding="utf-8") == (
        '{"recovery":"source stays intact"}'
    )
    repeated = authority.adopt_legacy_telegram_identity(
        settings=settings,
        source_path=source,
        auth_enabled=True,
    )
    assert repeated.state == "already_completed"

    bot = authority.telegram_bot_fingerprint(bot_id=bot_id)
    projection = authority.load_telegram_authority_projection(bot)
    assert projection.chat_owners == {"111222": "alice"}
    assert projection.session_map == {"111222": "legacy-session"}
    assert authority.consume_link_code_for_chat(
        plaintext_code, "333444", bot
    ) == "alice"

    with engine.connect() as connection:
        principal = connection.execute(text(
            "SELECT chat_id, chat_id_digest FROM telegram_principals "
            "WHERE chat_id_digest IS NOT NULL"
        )).first()
        binding = connection.execute(text(
            "SELECT session_id FROM telegram_conversation_bindings"
        )).first()
        import_run = connection.execute(text(
            "SELECT source_path, details FROM telegram_identity_import_runs"
        )).one()
    assert principal is not None and "111222" not in str(principal)
    assert binding is not None and "legacy-session" not in str(binding)
    assert import_run.source_path.startswith("enc:c1:")
    assert isinstance(import_run.details, str) and import_run.details.startswith('"enc:c1:')


def test_external_and_local_login_identities_converge_on_same_telegram_account(
    telegram_store,
):
    _engine, factory = telegram_store
    account_id = _account(factory, "alice")
    db = factory()
    try:
        db.add(AuthIdentity(
            id="external-identity",
            account_id=account_id,
            provider="supabase",
            issuer="https://identity.example/restia",
            subject="opaque-external-subject",
            state="active",
        ))
        db.commit()
    finally:
        db.close()

    bot = authority.telegram_bot_fingerprint(bot_id="bot-cross-interface")
    code, _ = authority.create_link_code_for_owner("alice", bot)
    assert authority.consume_link_code_for_chat(code, "9090", bot) == "alice"
    db = factory()
    try:
        principal = db.query(TelegramPrincipal).one()
        assert principal.account_id == account_id
        identities = db.query(AuthIdentity).filter(
            AuthIdentity.account_id == account_id
        ).all()
        assert {row.provider for row in identities} == {"local", "supabase"}
    finally:
        db.close()


def test_import_conflict_locks_runtime_without_partial_rows(telegram_store):
    _engine, factory = telegram_store
    _account(factory, "alice")
    settings = {
        "telegram_bot_id": "bot-conflict",
        "telegram_chat_owners": {"1": "missing-owner"},
    }
    with pytest.raises(authority.TelegramIdentityImportError) as failure:
        authority.adopt_legacy_telegram_identity(settings=settings)
    assert failure.value.code == "unknown_owner"
    with pytest.raises(authority.TelegramIdentityError, match="locked"):
        authority.load_telegram_authority_projection(
            authority.telegram_bot_fingerprint(bot_id="bot-conflict")
        )
    authority.unlock_telegram_identity_authority()
    db = factory()
    try:
        assert db.query(TelegramPrincipal).count() == 0
        assert db.query(TelegramIdentityImportRun).count() == 0
        assert db.query(TelegramLinkCode).count() == 0
    finally:
        db.close()


def test_legacy_open_ended_chat_authority_is_rejected(
    telegram_store, tmp_path,
):
    _engine, factory = telegram_store
    _account(factory, "alice")

    with pytest.raises(
        authority.TelegramIdentityImportError,
        match="unsupported_allow_all_chats",
    ):
        authority.adopt_legacy_telegram_identity(
            settings={
                "telegram_bot_id": "legacy-bot",
                "telegram_owner": "alice",
                "telegram_allow_all_chats": True,
            },
            source_path=tmp_path / "settings.json",
            auth_enabled=True,
        )

    authority.unlock_telegram_identity_authority()
    db = factory()
    try:
        assert db.query(TelegramPrincipal).count() == 0
        assert db.query(TelegramIdentityImportRun).count() == 0
    finally:
        db.close()


def test_runtime_config_reads_sql_projection_not_legacy_settings(
    telegram_store, monkeypatch
):
    _engine, factory = telegram_store
    _account(factory, "alice")
    import src.telegram_bot as telegram

    settings = {
        "telegram_enabled": True,
        "telegram_bot_id": "runtime-bot",
        "telegram_bot_token": "123:token",
        "telegram_chat_owners": {"plaintext-legacy-chat": "alice"},
        "telegram_session_map": {
            "plaintext-legacy-chat": "plaintext-legacy-session"
        },
        "telegram_allowed_chat_ids": ["plaintext-legacy-chat"],
        "telegram_owner": "alice",
        "telegram_allow_all_chats": True,
    }
    monkeypatch.setattr(telegram, "load_settings", lambda: dict(settings))

    before = telegram.load_telegram_config()
    assert before.chat_owners == {}
    assert before.session_map == {}
    assert before.allowed_chat_ids == frozenset()
    assert before.owner is None
    assert before.allow_all_chats is False

    code, _ = authority.create_link_code_for_owner(
        "alice", before.bot_fingerprint
    )
    assert authority.consume_link_code_for_chat(
        code, "sql-chat", before.bot_fingerprint
    ) == "alice"
    authority.set_conversation_binding(
        before.bot_fingerprint, "sql-chat", "sql-session"
    )
    after = telegram.load_telegram_config()
    assert after.chat_owners == {"sql-chat": "alice"}
    assert after.session_map == {"sql-chat": "sql-session"}


def test_revision_0006_builds_reviewed_constraints_and_empty_downgrade(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(secret_storage, "_digest_key", None)
    database_url = f"sqlite:///{tmp_path / 'telegram-revision.db'}"
    engine = create_engine(database_url)
    config = _alembic_config(database_url)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.stamp(config, LEGACY_BASELINE_REVISION)
        command.upgrade(config, "20260721_0006")
    assert schema_revision_status(engine).current_revisions == (
        "20260721_0006",
    )
    inspector = inspect(engine)
    assert {
        "telegram_principals",
        "telegram_conversation_bindings",
        "telegram_link_codes",
        "telegram_identity_import_runs",
    } <= set(inspector.get_table_names())
    principal_uniques = {
        tuple(row.get("column_names") or ())
        for row in inspector.get_unique_constraints("telegram_principals")
    }
    assert ("id", "account_id") in principal_uniques
    assert ("bot_fingerprint", "chat_id_digest") in principal_uniques
    binding_fks = inspector.get_foreign_keys(
        "telegram_conversation_bindings"
    )
    assert any(
        tuple(row.get("constrained_columns") or ())
        == ("principal_id", "account_id")
        and tuple(row.get("referred_columns") or ()) == ("id", "account_id")
        for row in binding_fks
    )
    live_code_index = next(
        row for row in inspector.get_indexes("telegram_link_codes")
        if row.get("name") == "uq_telegram_link_codes_account_live"
    )
    assert bool(live_code_index.get("unique")) is True
    assert tuple(live_code_index.get("column_names") or ()) == (
        "account_id", "bot_fingerprint",
    )

    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "20260720_0005")
    assert schema_revision_status(engine).current_revisions == (
        "20260720_0005",
    )
    assert "telegram_principals" not in set(inspect(engine).get_table_names())
    engine.dispose()


def test_revision_0006_refuses_to_destroy_authority_data(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(secret_storage, "_digest_key", None)
    database_url = f"sqlite:///{tmp_path / 'telegram-downgrade.db'}"
    engine = create_engine(database_url)
    config = _alembic_config(database_url)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.stamp(config, LEGACY_BASELINE_REVISION)
        command.upgrade(config, "20260721_0006")
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        account = ensure_account(db, "alice")
        bot = authority.telegram_bot_fingerprint(bot_id="bot-downgrade")
        db.add(TelegramPrincipal(
            id="principal",
            account_id=account.id,
            bot_fingerprint=bot,
            chat_id="private-chat",
            chat_id_digest=authority.telegram_chat_digest(bot, "private-chat"),
            state="linked",
        ))
        db.commit()
    finally:
        db.close()

    with pytest.raises(RuntimeError, match="export or unlink"):
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "20260720_0005")
    assert schema_revision_status(engine).current_revisions == (
        "20260721_0006",
    )
    with engine.connect() as connection:
        stored = connection.execute(text(
            "SELECT chat_id FROM telegram_principals"
        )).scalar_one()
    assert stored.startswith("enc:c1:")
    assert "private-chat" not in stored
    engine.dispose()
