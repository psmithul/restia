"""Credential-bearing settings are encrypted without changing caller semantics."""

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from src import settings


ROOT = Path(__file__).resolve().parents[1]


def _live_secrets():
    """Return the module currently registered after other tests reload it."""
    return importlib.import_module("src.secret_storage")


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    settings_file = tmp_path / "settings.json"
    secret_storage = _live_secrets()
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    settings._invalidate_caches()
    yield settings_file
    settings._invalidate_caches()


def test_save_encrypts_known_and_legacy_secret_settings(isolated_settings):
    secret_storage = _live_secrets()
    values = {
        "theme": "dark",
        "telegram_bot_token": "telegram-test-token",
        "telegram_webhook_secret": "telegram-test-webhook",
        "brave_api_key": "brave-test-key",
        "google_pse_key": "google-test-key",
        "tavily_api_key": "tavily-test-key",
        "serper_api_key": "serper-test-key",
        "search_url": "https://search.example.test/?api_key=search-test-key",
        "smtp_password": "legacy-test-password",
        "CUSTOM_API_KEY": "custom-test-key",
    }

    settings.save_settings(values)

    raw_text = isolated_settings.read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    for key, value in values.items():
        if key == "theme":
            assert raw[key] == value
            continue
        assert value not in raw_text
        assert secret_storage.is_encrypted(raw[key])

    loaded = settings.load_settings()
    for key, value in values.items():
        assert loaded[key] == value


def test_load_migrates_plaintext_secrets_without_materializing_defaults(isolated_settings):
    secret_storage = _live_secrets()
    legacy = {
        "theme": "light",
        "telegram_bot_token": "legacy-telegram-token",
        "miniflux_api_key": "legacy-miniflux-key",
    }
    isolated_settings.write_text(json.dumps(legacy), encoding="utf-8")

    loaded = settings.load_settings()

    assert loaded["telegram_bot_token"] == legacy["telegram_bot_token"]
    assert loaded["miniflux_api_key"] == legacy["miniflux_api_key"]
    migrated = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert set(migrated) == set(legacy)
    assert migrated["theme"] == "light"
    assert secret_storage.is_encrypted(migrated["telegram_bot_token"])
    assert secret_storage.is_encrypted(migrated["miniflux_api_key"])


def test_corrupt_secret_envelope_fails_closed_but_other_settings_load(isolated_settings):
    isolated_settings.write_text(
        json.dumps({"theme": "dark", "telegram_bot_token": "enc:not-fernet"}),
        encoding="utf-8",
    )

    loaded = settings.load_settings()

    assert loaded["theme"] == "dark"
    assert loaded["telegram_bot_token"] == ""


def test_read_only_key_directory_keeps_nonsecret_settings_and_blanks_secrets(
    isolated_settings,
    monkeypatch,
):
    secret_storage = _live_secrets()
    isolated_settings.write_text(
        json.dumps({"theme": "dark", "telegram_bot_token": "legacy-read-only-token"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        secret_storage,
        "encrypt",
        lambda _value: (_ for _ in ()).throw(PermissionError("read only")),
    )

    loaded = settings.load_settings()

    assert loaded["theme"] == "dark"
    assert loaded["telegram_bot_token"] == ""


def test_malformed_app_key_keeps_nonsecret_settings_and_fails_secrets_closed(
    isolated_settings,
):
    secret_storage = _live_secrets()
    isolated_settings.write_text(
        json.dumps({"theme": "dark", "telegram_bot_token": "legacy-token"}),
        encoding="utf-8",
    )
    secret_storage._KEY_PATH.write_bytes(b"not-a-fernet-key")
    secret_storage._fernet = None

    loaded = settings.load_settings()

    assert loaded["theme"] == "dark"
    assert loaded["telegram_bot_token"] == ""


def test_legacy_email_helper_decrypts_passwords_from_raw_settings(
    isolated_settings,
    monkeypatch,
):
    secret_storage = _live_secrets()
    import core.database as database
    from routes import email_helpers

    smtp_password = "legacy-smtp-test-password"
    imap_password = "legacy-imap-test-password"
    raw_settings = {
        "smtp_host": "smtp.example.test",
        "smtp_user": "alice@example.test",
        "smtp_password": secret_storage.encrypt(smtp_password),
        "imap_host": "imap.example.test",
        "imap_user": "alice@example.test",
        "imap_password": secret_storage.encrypt(imap_password),
    }
    monkeypatch.setattr(email_helpers, "_load_settings", lambda: raw_settings)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        lambda: (_ for _ in ()).throw(RuntimeError("no account database")),
    )

    config = email_helpers._get_email_config()

    assert config["smtp_password"] == smtp_password
    assert config["imap_password"] == imap_password


def test_email_mcp_legacy_fallback_decrypts_passwords(
    isolated_settings,
    monkeypatch,
):
    secret_storage = _live_secrets()
    from mcp_servers import email_server

    smtp_password = "mcp-smtp-test-password"
    imap_password = "mcp-imap-test-password"
    isolated_settings.write_text(
        json.dumps({
            "smtp_host": "smtp.example.test",
            "smtp_user": "alice@example.test",
            "smtp_password": secret_storage.encrypt(smtp_password),
            "imap_host": "imap.example.test",
            "imap_user": "alice@example.test",
            "imap_password": secret_storage.encrypt(imap_password),
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(email_server, "_SETTINGS_FILE", str(isolated_settings))
    monkeypatch.setattr(email_server, "_read_accounts_from_db", lambda: [])
    email_server._ACCOUNT_CACHE.clear()
    owner_token = email_server._CURRENT_OWNER.set(None)
    try:
        config = email_server._load_config()
    finally:
        email_server._CURRENT_OWNER.reset(owner_token)
        email_server._ACCOUNT_CACHE.clear()

    assert config["smtp_password"] == smtp_password
    assert config["imap_password"] == imap_password


def test_telegram_config_import_does_not_reenter_partial_secret_storage(tmp_path):
    env = os.environ.copy()
    env["RESTIA_DATA_DIR"] = str(tmp_path / "data")
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from src.telegram_bot import load_telegram_config; load_telegram_config()",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "secret_storage import failed" not in output
    assert "partially initialized module 'src.secret_storage'" not in output
