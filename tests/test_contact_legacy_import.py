from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import Base, ContactImportRun, ContactRecord, ContactSource
from src.contact_legacy_import import (
    ContactLegacyImportError,
    adopt_legacy_contacts,
)
from src.identity import ensure_account
from src.contact_service import list_contacts


@pytest.fixture()
def import_env(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    import src.secret_storage as secret_storage
    import src.contact_legacy_import as legacy
    import src.carddav_contacts as carddav

    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(legacy, "validate_carddav_url", lambda value: str(value).rstrip("/"))
    monkeypatch.setattr(carddav, "validate_carddav_url", lambda value: str(value).rstrip("/"))
    engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy-contacts.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    alice = ensure_account(db, "alice")
    bob = ensure_account(db, "bob")
    db.commit()
    db.close()
    try:
        yield {
            "engine": engine,
            "Session": factory,
            "alice": alice,
            "bob": bob,
            "settings": tmp_path / "settings.json",
            "contacts": tmp_path / "contacts.json",
            "backups": tmp_path / "backups",
        }
    finally:
        engine.dispose()


def _write_sources(env):
    from src.secret_storage import encrypt

    env["settings"].write_text(json.dumps({
        "theme": "dark",
        "carddav_url": "https://dav.example/addressbook",
        "carddav_username": "alice-dav",
        "carddav_password": encrypt("legacy-secret"),
    }), encoding="utf-8")
    env["contacts"].write_text(json.dumps({"contacts": [{
        "uid": "legacy-uid",
        "name": "Legacy Alice",
        "emails": ["legacy@example.test"],
        "phones": ["+15550001"],
        "address": "Legacy address",
    }]}), encoding="utf-8")


def _adopt(env, **kwargs):
    return adopt_legacy_contacts(
        env["Session"],
        settings_path=env["settings"],
        contacts_path=env["contacts"],
        backup_dir=env["backups"],
        auth_enabled=True,
        primary_admin_resolver=lambda: "alice",
        environ={},
        **kwargs,
    )


def test_primary_admin_adoption_is_encrypted_backed_up_and_idempotent(import_env):
    _write_sources(import_env)
    first = _adopt(import_env)
    second = _adopt(import_env)
    assert first.owner_id == import_env["alice"].id
    assert first.contacts == 1
    assert first.carddav_configured is True
    assert first.idempotent is False
    assert second.run_id == first.run_id
    assert second.idempotent is True

    db = import_env["Session"]()
    try:
        run = db.query(ContactImportRun).one()
        rows = db.query(ContactRecord).all()
        assert len(rows) == 1
        assert rows[0].owner_id == import_env["alice"].id
        assert run.details["contacts"] == 1
        settings_backup = run.backup_settings_path
        contacts_backup = run.backup_contacts_path
        assert hashlib.sha256(open(settings_backup, "rb").read()).hexdigest() == run.settings_sha256
        assert hashlib.sha256(open(contacts_backup, "rb").read()).hexdigest() == run.contacts_sha256
    finally:
        db.close()

    with import_env["engine"].connect() as connection:
        raw = connection.execute(text(
            "SELECT base_url, username, password FROM contact_sources "
            "WHERE kind='carddav'"
        )).one()
        payload = connection.execute(text(
            "SELECT payload, raw_vcard FROM contact_records"
        )).one()
    serialized = " ".join(str(value) for value in (*raw, *payload))
    for private in (
        "dav.example", "alice-dav", "legacy-secret", "Legacy Alice",
        "legacy@example.test", "Legacy address",
    ):
        assert private not in serialized


def test_migrated_carddav_snapshot_remains_visible_when_remote_is_offline(
    import_env, monkeypatch
):
    import src.carddav_contacts as carddav

    _write_sources(import_env)
    result = _adopt(import_env)
    fetch_calls = 0

    def offline(_config):
        nonlocal fetch_calls
        fetch_calls += 1
        raise carddav.CardDAVError("offline")

    monkeypatch.setattr(carddav, "fetch_contacts", offline)

    db = import_env["Session"]()
    try:
        source = db.query(ContactSource).filter_by(
            owner_id=result.owner_id, kind="carddav"
        ).one()
        rows = list_contacts(
            db,
            owner_id=result.owner_id,
            refresh=False,
            create_local=False,
        )

        assert source.enabled is True
        # Snapshot-only list/search paths never inherit connector timeouts.
        # An explicit refresh is responsible for transitioning to error.
        assert fetch_calls == 0
        assert source.sync_state == "idle"
        assert [row["name"] for row in rows] == ["Legacy Alice"]
        assert rows[0]["emails"] == ["legacy@example.test"]
    finally:
        db.rollback()
        db.close()


def test_ambiguous_owner_fails_closed_without_import(import_env):
    _write_sources(import_env)
    with pytest.raises(ContactLegacyImportError, match="unambiguous owner"):
        adopt_legacy_contacts(
            import_env["Session"],
            settings_path=import_env["settings"],
            contacts_path=import_env["contacts"],
            backup_dir=import_env["backups"],
            auth_enabled=True,
            primary_admin_resolver=lambda: None,
            environ={},
        )
    db = import_env["Session"]()
    try:
        assert db.query(ContactRecord).count() == 0
        assert db.query(ContactImportRun).count() == 0
    finally:
        db.close()


def test_auth_disabled_adopts_only_to_default_local_owner(import_env):
    import src.contact_legacy_import as legacy

    import_env["contacts"].write_text(json.dumps([{
        "uid": "local-only", "name": "Local owner", "emails": [],
    }]), encoding="utf-8")
    result = adopt_legacy_contacts(
        import_env["Session"],
        settings_path=import_env["settings"],
        contacts_path=import_env["contacts"],
        backup_dir=import_env["backups"],
        auth_enabled=False,
        primary_admin_resolver=lambda: None,
        environ={},
    )
    db = import_env["Session"]()
    try:
        account = db.query(legacy.ContactImportRun).one()
        assert result.owner_id == account.owner_id
        assert result.owner_id not in {import_env["alice"].id, import_env["bob"].id}
    finally:
        db.close()


def test_corrupt_source_fails_then_clean_retry_succeeds(import_env):
    import_env["contacts"].write_text('{"contacts":[', encoding="utf-8")
    with pytest.raises(ContactLegacyImportError, match="bounded JSON"):
        _adopt(import_env)
    import_env["contacts"].write_text(json.dumps({"contacts": [{
        "uid": "fixed", "name": "Fixed", "emails": [],
    }]}), encoding="utf-8")
    assert _adopt(import_env).contacts == 1


def test_changed_source_after_cutover_is_rejected(import_env):
    _write_sources(import_env)
    _adopt(import_env)
    import_env["contacts"].write_text(json.dumps({"contacts": [{
        "uid": "changed", "name": "Changed", "emails": [],
    }]}), encoding="utf-8")
    with pytest.raises(ContactLegacyImportError, match="changed after"):
        _adopt(import_env)


def test_environment_carddav_is_bootstrap_only_after_cutover(import_env):
    import_env["settings"].write_text(
        json.dumps({"theme": "dark"}), encoding="utf-8"
    )
    first = adopt_legacy_contacts(
        import_env["Session"],
        settings_path=import_env["settings"],
        contacts_path=import_env["contacts"],
        backup_dir=import_env["backups"],
        auth_enabled=True,
        primary_admin_resolver=lambda: "alice",
        environ={
            "CARDDAV_URL": "https://dav.example/addressbook",
            "CARDDAV_USERNAME": "alice-dav",
            "CARDDAV_PASSWORD": "bootstrap-secret",
        },
    )
    second = adopt_legacy_contacts(
        import_env["Session"],
        settings_path=import_env["settings"],
        contacts_path=import_env["contacts"],
        backup_dir=import_env["backups"],
        auth_enabled=True,
        primary_admin_resolver=lambda: "alice",
        environ={},
    )
    assert first.carddav_configured is True
    assert second.run_id == first.run_id
    assert second.idempotent is True


def test_concurrent_adoption_converges_on_one_run(import_env):
    _write_sources(import_env)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: _adopt(import_env), range(2)))
    assert {result.run_id for result in results}.__len__() == 1
    assert sorted(result.idempotent for result in results) == [False, True]
    db = import_env["Session"]()
    try:
        assert db.query(ContactImportRun).count() == 1
        assert db.query(ContactRecord).count() == 1
    finally:
        db.close()
