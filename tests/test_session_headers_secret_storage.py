"""Session endpoint authorization headers remain dicts but are encrypted on disk."""

import importlib
import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as database


def _live_secrets():
    """Return the module currently registered after other tests reload it."""
    return importlib.import_module("src.secret_storage")


@pytest.fixture
def session_store(tmp_path, monkeypatch):
    secret_storage = _live_secrets()
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    database.Session.__table__.create(engine)
    yield engine, sessionmaker(bind=engine)
    engine.dispose()


def _insert_session(factory, session_id: str, headers: dict):
    with factory() as db:
        db.add(database.Session(
            id=session_id,
            name="Encrypted headers test",
            endpoint_url="https://models.example.test/v1",
            model="test-model",
            headers=headers,
        ))
        db.commit()


def test_session_headers_are_encrypted_at_rest_and_read_as_dict(session_store):
    secret_storage = _live_secrets()
    engine, factory = session_store
    headers = {
        "Authorization": "Bearer session-test-secret",
        "X-API-Key": "session-test-api-key",
    }
    _insert_session(factory, "encrypted", headers)

    with engine.connect() as conn:
        raw = conn.exec_driver_sql(
            "SELECT headers FROM sessions WHERE id = 'encrypted'"
        ).scalar_one()
    assert "session-test-secret" not in raw
    assert "session-test-api-key" not in raw
    assert secret_storage.is_encrypted(json.loads(raw))

    with factory() as db:
        assert db.get(database.Session, "encrypted").headers == headers


def test_legacy_plaintext_headers_are_read_and_migrated_idempotently(
    session_store,
    monkeypatch,
):
    secret_storage = _live_secrets()
    engine, factory = session_store
    legacy = {"Authorization": "Bearer legacy-session-secret"}
    _insert_session(factory, "legacy", {})
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE sessions SET headers = ? WHERE id = 'legacy'",
            (json.dumps(legacy),),
        )

    with factory() as db:
        assert db.get(database.Session, "legacy").headers == legacy

    monkeypatch.setattr(database, "engine", engine)
    database._migrate_encrypt_session_headers()
    with engine.connect() as conn:
        migrated = conn.exec_driver_sql(
            "SELECT headers FROM sessions WHERE id = 'legacy'"
        ).scalar_one()
    assert "legacy-session-secret" not in migrated
    assert secret_storage.is_encrypted(json.loads(migrated))

    database._migrate_encrypt_session_headers()
    with engine.connect() as conn:
        assert conn.exec_driver_sql(
            "SELECT headers FROM sessions WHERE id = 'legacy'"
        ).scalar_one() == migrated
    with factory() as db:
        assert db.get(database.Session, "legacy").headers == legacy


def test_session_header_migration_failure_log_never_contains_bound_secret(
    session_store,
    monkeypatch,
    caplog,
):
    secret_storage = _live_secrets()
    engine, factory = session_store
    secret = "migration-log-test-secret"
    _insert_session(factory, "migration-failure", {})
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE sessions SET headers = ? WHERE id = 'migration-failure'",
            (json.dumps({"Authorization": f"Bearer {secret}"}),),
        )

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(
        secret_storage,
        "encrypt_plaintext",
        lambda _value: (_ for _ in ()).throw(RuntimeError(f"failed for {secret}")),
    )
    caplog.set_level("WARNING", logger="core.database")

    database._migrate_encrypt_session_headers()

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "Session-header encryption migration skipped" in rendered
    assert secret not in rendered


def test_wrong_key_fails_closed_without_destroying_session_header_ciphertext(
    session_store,
):
    secret_storage = _live_secrets()
    engine, factory = session_store
    headers = {"Authorization": "Bearer recovery-test-secret"}
    _insert_session(factory, "recovery", headers)
    key_path = secret_storage._KEY_PATH
    original_key = key_path.read_bytes()
    with engine.connect() as conn:
        original_raw = conn.exec_driver_sql(
            "SELECT headers FROM sessions WHERE id = 'recovery'"
        ).scalar_one()

    key_path.write_bytes(Fernet.generate_key())
    secret_storage._fernet = None
    with factory() as db:
        assert db.get(database.Session, "recovery").headers == {}
    with engine.connect() as conn:
        assert conn.exec_driver_sql(
            "SELECT headers FROM sessions WHERE id = 'recovery'"
        ).scalar_one() == original_raw

    key_path.write_bytes(original_key)
    secret_storage._fernet = None
    with factory() as db:
        assert db.get(database.Session, "recovery").headers == headers
