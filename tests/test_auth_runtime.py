from __future__ import annotations

import json
from datetime import datetime

import bcrypt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    AccountCapability,
    AccountRole,
    ApiToken,
    AuthIdentity,
    AuthImportRun,
    AuthPolicy,
    AuthSession,
    Base,
    LocalCredential,
    MfaFactor,
    MfaRecoveryCode,
    RetiredAuthSubject,
)
from src.auth_runtime import build_auth_manager


TABLES = (
    Account.__table__,
    AuthIdentity.__table__,
    AuthPolicy.__table__,
    LocalCredential.__table__,
    MfaFactor.__table__,
    MfaRecoveryCode.__table__,
    AccountRole.__table__,
    AccountCapability.__table__,
    AuthSession.__table__,
    RetiredAuthSubject.__table__,
    AuthImportRun.__table__,
    ApiToken.__table__,
)
TOKEN_KEY = b"auth-runtime-test-token-key-material-32b"


@pytest.fixture()
def runtime_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'runtime.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine, tables=TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory, tmp_path
    engine.dispose()


def _write_legacy(path, *, password="correct horse battery staple"):
    payload = {
        "users": {
            "alice": {
                "password_hash": bcrypt.hashpw(
                    password.encode("utf-8"), bcrypt.gensalt(rounds=4)
                ).decode("ascii"),
                "created": datetime(2026, 7, 16).timestamp(),
                "is_admin": True,
            }
        },
        "signup_enabled": False,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _build(factory, root):
    return build_auth_manager(
        factory,
        auth_path=root / "auth.json",
        sessions_path=root / "sessions.json",
        backup_dir=root / "backups",
        token_hmac_key=TOKEN_KEY,
    )


def test_fresh_install_has_unlocked_database_setup(runtime_env):
    factory, root = runtime_env
    manager = _build(factory, root)

    assert manager.auth_store_error is False
    assert manager.is_configured is False
    assert manager.setup("alice", "correct horse battery staple") is True


def test_legacy_source_imports_before_database_manager_is_returned(runtime_env):
    factory, root = runtime_env
    _write_legacy(root / "auth.json")
    (root / "sessions.json").write_text("{}", encoding="utf-8")

    manager = _build(factory, root)

    assert manager.auth_store_error is False
    assert manager.verify_password("alice", "correct horse battery staple")
    assert (root / "backups").is_dir()


def test_corrupt_or_changed_legacy_source_locks_every_auth_path(runtime_env):
    factory, root = runtime_env
    auth_path = root / "auth.json"
    _write_legacy(auth_path)
    (root / "sessions.json").write_text("{}", encoding="utf-8")
    assert _build(factory, root).auth_store_error is False

    auth_path.write_text('{"users":', encoding="utf-8")
    locked = _build(factory, root)

    assert locked.auth_store_error is True
    assert locked.is_configured is True
    assert locked.verify_password("alice", "correct horse battery staple") is False


def test_completed_cutover_uses_database_not_stale_json(runtime_env):
    factory, root = runtime_env
    _write_legacy(root / "auth.json")
    (root / "sessions.json").write_text("{}", encoding="utf-8")
    manager = _build(factory, root)
    assert manager.change_password(
        "alice",
        "correct horse battery staple",
        "database only changed password",
    )

    restarted = _build(factory, root)

    assert restarted.auth_store_error is False
    assert restarted.verify_password("alice", "database only changed password")


def test_missing_source_requires_verified_completed_backup(runtime_env):
    factory, root = runtime_env
    auth_path = root / "auth.json"
    _write_legacy(auth_path)
    (root / "sessions.json").write_text("{}", encoding="utf-8")
    assert _build(factory, root).auth_store_error is False
    auth_path.unlink()
    (root / "sessions.json").unlink()

    assert _build(factory, root).auth_store_error is False

    backup = next((root / "backups").glob("auth.*.json.bak"))
    backup.write_bytes(b"corrupt")
    assert _build(factory, root).auth_store_error is True


def test_orphan_legacy_session_file_closes_first_run_setup(runtime_env):
    factory, root = runtime_env
    (root / "sessions.json").write_text("{}", encoding="utf-8")

    manager = _build(factory, root)

    assert manager.auth_store_error is True
    assert manager.setup("attacker", "correct horse battery staple") is False


def test_database_profile_helpers_are_deterministic_and_drive_ownerless_events(
    monkeypatch,
):
    from src import auth_runtime, event_bus

    class Auth:
        def list_users(self):
            return [
                {"username": "zoe", "is_admin": False},
                {"username": "Bob", "is_admin": True},
                {"username": "alice", "is_admin": True},
            ]

    monkeypatch.setattr(auth_runtime, "get_auth_manager", lambda: Auth())

    assert auth_runtime.active_auth_usernames() == ("alice", "bob", "zoe")
    assert auth_runtime.primary_admin_username() == "alice"
    assert event_bus._resolve_event_owner(None) == "alice"
    assert event_bus._resolve_event_owner("zoe") == "zoe"


def test_runtime_owner_resolution_never_reads_retired_auth_json():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    event_source = (root / "src" / "event_bus.py").read_text(encoding="utf-8")
    app_source = (root / "app.py").read_text(encoding="utf-8")
    database_source = (root / "core" / "database.py").read_text(encoding="utf-8")

    assert "AUTH_FILE" not in event_source
    assert "open(auth_path" not in app_source
    owner_migration = database_source.split(
        "def _migrate_assign_legacy_owner", 1
    )[1].split("def ", 1)[0]
    assert "auth.json" not in owner_migration
    assert "AUTH_FILE" not in owner_migration
