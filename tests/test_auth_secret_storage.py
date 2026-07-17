"""Auth secrets stay one-way/encrypted on disk and former names stay retired."""

import asyncio
import json
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

import core.database as cdb
from core.auth import AuthManager
from routes.auth_routes import SetupRequest, setup_auth_routes
import src.secret_storage as secret_storage


def _manager(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    return AuthManager(str(tmp_path / "auth.json"))


def test_totp_seed_is_encrypted_and_backup_codes_are_one_way(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.setup("alice", "correct horse battery staple")

    secret = manager.totp_generate_secret("alice")
    on_disk = (tmp_path / "auth.json").read_text(encoding="utf-8")
    assert secret not in on_disk
    assert "enc:" in on_disk

    backup = manager.totp_confirm_enable(
        "alice", pyotp.TOTP(secret).now(), "correct horse battery staple"
    )
    assert backup and len(backup) == 8
    on_disk = (tmp_path / "auth.json").read_text(encoding="utf-8")
    assert all(code not in on_disk for code in backup)
    assert manager.totp_verify("alice", backup[0]) is True
    assert manager.totp_verify("alice", backup[0]) is False


def test_backup_code_is_single_use_under_concurrent_login(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.setup("alice", "correct horse battery staple")
    secret = manager.totp_generate_secret("alice")
    backup = manager.totp_confirm_enable(
        "alice", pyotp.TOTP(secret).now(), "correct horse battery staple"
    )
    code = backup[0]

    # Stretch a successful comparison so the historical verify-before-lock
    # implementation admits multiple contenders reliably.
    import core.auth as auth_mod
    original_verify = auth_mod._verify_backup_code

    def slow_verify(candidate, protected):
        matched = original_verify(candidate, protected)
        if matched:
            time.sleep(0.03)
        return matched

    monkeypatch.setattr(auth_mod, "_verify_backup_code", slow_verify)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _i: manager.totp_verify("alice", code), range(8)))

    assert results.count(True) == 1
    assert results.count(False) == 7
    assert len(manager.users["alice"]["totp_backup_codes"]) == 7


def test_wrong_key_does_not_destroy_totp_seed_and_original_key_recovers(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.setup("alice", "correct horse battery staple")
    secret = manager.totp_generate_secret("alice")
    assert manager.totp_confirm_enable(
        "alice", pyotp.TOTP(secret).now(), "correct horse battery staple"
    )
    key_path = tmp_path / ".app_key"
    original_key = key_path.read_bytes()
    encrypted_seed = json.loads((tmp_path / "auth.json").read_text())["users"]["alice"]["totp_secret"]

    key_path.write_bytes(Fernet.generate_key())
    secret_storage._fernet = None
    wrong_key_manager = AuthManager(str(tmp_path / "auth.json"))
    assert wrong_key_manager.totp_verify("alice", pyotp.TOTP(secret).now()) is False
    assert json.loads((tmp_path / "auth.json").read_text())["users"]["alice"]["totp_secret"] == encrypted_seed

    key_path.write_bytes(original_key)
    secret_storage._fernet = None
    recovered = AuthManager(str(tmp_path / "auth.json"))
    assert recovered.totp_verify("alice", pyotp.TOTP(secret).now()) is True


def test_frame_damaged_totp_seed_stays_fail_closed_and_unchanged(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.setup("alice", "correct horse battery staple")
    secret = manager.totp_generate_secret("alice")
    assert manager.totp_confirm_enable(
        "alice", pyotp.TOTP(secret).now(), "correct horse battery staple"
    )
    auth_path = tmp_path / "auth.json"
    stored = json.loads(auth_path.read_text(encoding="utf-8"))
    damaged = stored["users"]["alice"]["totp_secret"][:-4]
    stored["users"]["alice"]["totp_secret"] = damaged
    auth_path.write_text(json.dumps(stored), encoding="utf-8")

    reloaded = AuthManager(str(auth_path))

    assert reloaded.totp_verify("alice", pyotp.TOTP(secret).now()) is False
    persisted = json.loads(auth_path.read_text(encoding="utf-8"))
    assert persisted["users"]["alice"]["totp_secret"] == damaged


def test_session_file_contains_only_token_digest_and_survives_reload(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.setup("alice", "correct horse battery staple")

    token = manager.create_session_trusted("alice")
    raw = (tmp_path / "sessions.json").read_text(encoding="utf-8")
    persisted = json.loads(raw)
    assert token not in raw
    assert all(key.startswith("sha256:") for key in persisted)

    reloaded = AuthManager(str(tmp_path / "auth.json"))
    assert reloaded.get_username_for_token(token) == "alice"


def test_deleted_profile_name_cannot_be_reused(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.setup("admin", "correct horse battery staple")
    assert manager.create_user("alice", "correct horse battery staple")

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def delete(self):
            return 0

    class _Db:
        def query(self, *args, **kwargs):
            return _Query()

    @contextmanager
    def _db_session():
        yield _Db()

    monkeypatch.setattr(cdb, "get_db_session", _db_session)
    assert manager.delete_user("alice", "admin") is True
    assert "alice" in manager.retired_usernames
    assert manager.create_user("alice", "different password") is False


def test_rename_reserves_old_name_and_explicit_rollback_restores_it(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.setup("admin", "correct horse battery staple")
    assert manager.create_user("alice", "correct horse battery staple")
    token = manager.create_session_trusted("alice")

    assert manager.rename_user("alice", "alice2", "admin") is True
    assert "alice" in manager.retired_usernames
    assert manager.create_user("alice", "different password") is False
    assert manager.get_username_for_token(token) == "alice2"

    assert manager.rollback_user_rename("alice2", "alice", "admin") is True
    assert "alice" in manager.users
    assert "alice2" not in manager.users
    assert "alice" not in manager.retired_usernames
    assert manager.get_username_for_token(token) == "alice"


def test_create_rechecks_retirement_after_acquiring_config_lock(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    checked_outside = __import__("threading").Event()
    original_property = AuthManager.retired_usernames

    def observed_retired(self):
        value = original_property.fget(self)
        checked_outside.set()
        return value

    monkeypatch.setattr(AuthManager, "retired_usernames", property(observed_retired))
    manager._config_lock.acquire()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(manager.create_user, "alice", "correct horse battery staple")
            assert checked_outside.wait(timeout=2)
            manager._config["retired_usernames"] = ["alice"]
            manager._config_lock.release()
            assert future.result(timeout=3) is False
    finally:
        if manager._config_lock.locked():
            manager._config_lock.release()


def test_login_accepts_full_length_backup_code_input():
    login_html = (__import__("pathlib").Path(__file__).parents[1] / "static" / "login.html").read_text()
    snippet = login_html.split('id="totp-input"', 1)[1].split(">", 1)[0]
    assert 'maxlength="32"' in snippet
    assert 'inputmode="numeric"' not in snippet
    assert "backup code" in login_html


def test_corrupt_existing_auth_store_fails_closed_against_setup(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    auth_path.write_text('{"users":', encoding="utf-8")
    manager = _manager(tmp_path, monkeypatch)

    assert manager.is_configured is True
    assert manager.status(None)["auth_store_error"] is True
    assert manager.setup("attacker", "correct horse battery staple") is False


def _setup_endpoint(manager):
    router = setup_auth_routes(manager)
    return next(
        route.endpoint for route in router.routes
        if getattr(route, "path", "") == "/api/auth/setup"
    )


def test_single_user_legacy_reserved_profile_is_not_auto_renamed(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    legacy = {
        "username": "demo",
        "password_hash": "legacy-single-user-hash",
        "profile_marker": "keep-owner-demo",
    }
    original = json.dumps(legacy, indent=2)
    auth_path.write_text(original, encoding="utf-8")

    manager = _manager(tmp_path, monkeypatch)

    assert manager.is_configured is True
    assert manager.status(None)["auth_store_error"] is True
    assert manager.users == {}
    assert auth_path.read_text(encoding="utf-8") == original
    assert manager.setup("attacker", "correct horse battery staple") is False


def test_sole_legacy_reserved_profile_is_preserved_and_setup_stays_closed(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    legacy = {
        "users": {
            "system": {
                "password_hash": "legacy-password-hash",
                "created": 123.0,
                "is_admin": True,
                "profile_marker": "keep-owner-system",
            }
        },
        "signup_enabled": False,
    }
    original = json.dumps(legacy, indent=2)
    auth_path.write_text(original, encoding="utf-8")

    manager = _manager(tmp_path, monkeypatch)

    assert manager.is_configured is True
    assert manager.status(None)["auth_store_error"] is True
    assert manager.users["system"]["profile_marker"] == "keep-owner-system"
    assert auth_path.read_text(encoding="utf-8") == original
    assert manager.verify_password("system", "anything") is False
    assert manager.create_session_trusted("system") is None
    assert manager.setup("attacker", "correct horse battery staple") is False

    endpoint = _setup_endpoint(manager)
    request = SimpleNamespace(client=SimpleNamespace(host="203.0.113.10"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(
            SetupRequest(username="attacker", password="correct horse battery staple"),
            request,
        ))
    assert (exc.value.status_code, exc.value.detail) == (400, "Already configured")


def test_mixed_legacy_reserved_store_fails_closed_without_dropping_profiles(tmp_path, monkeypatch):
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    admin_token = "legacy-admin-session-token"
    reserved_token = "legacy-reserved-session-token"
    legacy = {
        "users": {
            "admin": {
                "password_hash": "safe-admin-hash",
                "created": 100.0,
                "is_admin": True,
            },
            "internal-tool": {
                "password_hash": "legacy-tool-hash",
                "created": 101.0,
                "is_admin": True,
                "profile_marker": "keep-owner-internal-tool",
            },
        }
    }
    original = json.dumps(legacy, indent=2)
    auth_path.write_text(original, encoding="utf-8")
    sessions_path.write_text(json.dumps({
        admin_token: {"username": "admin", "expiry": time.time() + 3600},
        reserved_token: {"username": "internal-tool", "expiry": time.time() + 3600},
    }), encoding="utf-8")

    manager = _manager(tmp_path, monkeypatch)

    assert manager.is_configured is True
    assert manager.status(None)["auth_store_error"] is True
    assert set(manager.users) == {"admin", "internal-tool"}
    assert manager.users["internal-tool"]["profile_marker"] == "keep-owner-internal-tool"
    assert auth_path.read_text(encoding="utf-8") == original
    assert manager.verify_password("admin", "anything") is False
    assert manager.verify_password("internal-tool", "anything") is False
    assert manager.create_session_trusted("admin") is None
    assert manager.create_session_trusted("internal-tool") is None
    assert manager.validate_token(admin_token) is False
    assert manager.get_username_for_token(admin_token) is None
    assert manager.validate_token(reserved_token) is False
    assert manager.get_username_for_token(reserved_token) is None
    assert manager.is_admin("admin") is False
    assert manager.is_admin("internal-tool") is False
    assert manager.setup("attacker", "correct horse battery staple") is False
