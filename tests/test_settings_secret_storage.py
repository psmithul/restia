"""Private profile settings are encrypted while legacy email reads remain safe."""

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import core.database as database
from core.database import Account, Base
from src import settings
from src.profile_configuration_models import ProfileConfiguration  # noqa: F401


ROOT = Path(__file__).resolve().parents[1]


def _live_secrets():
    return importlib.import_module("src.secret_storage")


@pytest.fixture()
def settings_db(tmp_path, monkeypatch):
    secret_storage = _live_secrets()
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(f"sqlite:///{tmp_path / 'settings-secrets.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    db = factory()
    db.add(Account(id=str(uuid.uuid4()), username="alice", status="active"))
    db.commit()
    db.close()
    monkeypatch.setattr(database, "SessionLocal", factory)
    settings._invalidate_caches()
    yield engine
    settings._invalidate_caches()
    engine.dispose()


def test_private_setting_is_ciphertext_at_rest_and_plaintext_at_runtime(settings_db):
    token = "telegram-test-token"
    settings.save_settings({"telegram_bot_token": token}, owner="alice")

    with settings_db.connect() as connection:
        raw = connection.execute(text(
            "SELECT private_value FROM profile_configurations "
            "WHERE namespace = 'setting' AND key = 'telegram_bot_token'"
        )).scalar_one()
    assert token not in str(raw)
    assert "enc:c1:" in str(raw)
    assert settings.load_settings(owner="alice")["telegram_bot_token"] == token


def test_corrupt_ciphertext_fails_loudly(settings_db):
    settings.save_settings({"telegram_bot_token": "valid-token"}, owner="alice")
    with settings_db.begin() as connection:
        connection.execute(text(
            "UPDATE profile_configurations SET private_value = :payload "
            "WHERE namespace = 'setting' AND key = 'telegram_bot_token'"
        ), {"payload": json.dumps("enc:not-a-valid-envelope")})
    settings._invalidate_caches()

    with pytest.raises(Exception):
        settings.load_settings(owner="alice")


def test_legacy_email_helper_decrypts_passwords_from_retained_source(tmp_path, monkeypatch):
    secret_storage = _live_secrets()
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


def test_email_mcp_legacy_fallback_decrypts_passwords(tmp_path, monkeypatch):
    secret_storage = _live_secrets()
    from mcp_servers import email_server

    settings_file = tmp_path / "settings.json"
    smtp_password = "mcp-smtp-test-password"
    imap_password = "mcp-imap-test-password"
    settings_file.write_text(json.dumps({
        "smtp_host": "smtp.example.test",
        "smtp_user": "alice@example.test",
        "smtp_password": secret_storage.encrypt(smtp_password),
        "imap_host": "imap.example.test",
        "imap_user": "alice@example.test",
        "imap_password": secret_storage.encrypt(imap_password),
    }), encoding="utf-8")
    monkeypatch.setattr(email_server, "_SETTINGS_FILE", str(settings_file))
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
