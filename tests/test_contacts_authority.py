from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from core.database import (
    ActionAudit,
    Base,
    ContactDelivery,
    ContactImportRun,
    ContactRecord,
    ContactSource,
)
from routes.contacts.contacts_routes import setup_contacts_routes
from src.audit_context import bind_service_audit_context
from src.contact_service import (
    ContactConflict,
    ContactServiceError,
    create_contact,
    delete_contact,
    get_local_source,
    import_vcards,
    list_contacts,
    refresh_contact_source,
    search_contacts,
    update_contact,
    upsert_carddav_config,
)
from src.identity import ensure_account


class _IdentityAuthority:
    def __init__(self, *users: str):
        self._config_lock = threading.Lock()
        self._identity_migrations = set()
        self.retired_usernames = set()
        self.users = {user: {} for user in users}

    @property
    def is_configured(self):
        return bool(self.users)


@pytest.fixture()
def contacts_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'contacts.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    alice = ensure_account(db, "alice")
    bob = ensure_account(db, "bob")
    db.commit()
    db.close()
    try:
        yield SimpleNamespace(
            engine=engine, Session=factory, alice=alice, bob=bob, tmp_path=tmp_path
        )
    finally:
        engine.dispose()


def test_owner_isolation_composite_fk_and_same_uid(contacts_env):
    db = contacts_env.Session()
    try:
        alice_source = get_local_source(
            db, owner_id=contacts_env.alice.id, create=True
        )
        bob_source = get_local_source(
            db, owner_id=contacts_env.bob.id, create=True
        )
        for owner_id, source_id, name in (
            (contacts_env.alice.id, alice_source.id, "Alice private"),
            (contacts_env.bob.id, bob_source.id, "Bob private"),
        ):
            db.add(ContactRecord(
                id=f"row-{name}",
                owner_id=owner_id,
                source_id=source_id,
                remote_uid="shared-remote-uid",
                payload={
                    "name": name, "emails": [], "phones": [], "address": "",
                },
                raw_vcard=f"BEGIN:VCARD\nFN:{name}\nEND:VCARD",
            ))
        db.commit()

        assert [row["name"] for row in list_contacts(
            db, owner_id=contacts_env.alice.id
        )] == ["Alice private"]
        assert [row["name"] for row in search_contacts(
            db, owner_id=contacts_env.bob.id, query="private"
        )] == ["Bob private"]

        db.add(ContactRecord(
            id="cross-owner",
            owner_id=contacts_env.bob.id,
            source_id=alice_source.id,
            remote_uid="cross-owner",
            payload={"name": "leak", "emails": [], "phones": [], "address": ""},
        ))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_private_contact_fields_are_encrypted_at_rest(contacts_env, monkeypatch):
    import src.carddav_contacts as carddav

    monkeypatch.setattr(
        carddav, "validate_carddav_url", lambda value: str(value).rstrip("/")
    )
    db = contacts_env.Session()
    try:
        source = upsert_carddav_config(
            db,
            owner_id=contacts_env.alice.id,
            url="https://dav.example/addressbook",
            username="private-user",
            password="private-password",
        )
        local = get_local_source(db, owner_id=contacts_env.alice.id, create=True)
        db.add(ContactRecord(
            id="encrypted-record",
            owner_id=contacts_env.alice.id,
            source_id=local.id,
            remote_uid="encrypted-uid",
            remote_href="/private/person.vcf",
            payload={
                "name": "Private Person",
                "emails": ["private@example.test"],
                "phones": [],
                "address": "Private address",
            },
            raw_vcard="BEGIN:VCARD\nFN:Private Person\nEND:VCARD",
        ))
        db.add(ContactImportRun(
            id="encrypted-import",
            owner_id=contacts_env.alice.id,
            source_kind="test-import",
            state="completed",
            backup_settings_path="/private/settings.json.bak",
            backup_contacts_path="/private/contacts.json.bak",
            details={"contacts": 1},
        ))
        db.commit()
        assert source.password == "private-password"
    finally:
        db.close()

    with contacts_env.engine.connect() as connection:
        raw_source = connection.execute(text(
            "SELECT base_url, username, password FROM contact_sources "
            "WHERE id=:id"
        ), {"id": source.id}).one()
        raw_record = connection.execute(text(
            "SELECT remote_uid, remote_href, payload, raw_vcard FROM contact_records "
            "WHERE id='encrypted-record'"
        )).one()
        raw_import = connection.execute(text(
            "SELECT backup_settings_path, backup_contacts_path, details "
            "FROM contact_import_runs WHERE id='encrypted-import'"
        )).one()
    serialized = "\n".join(str(value) for value in (*raw_source, *raw_record, *raw_import))
    for private in (
        "dav.example", "private-user", "private-password", "Private Person",
        "private@example.test", "Private address", "encrypted-uid",
        "/private/settings.json.bak",
    ):
        assert private not in serialized
    assert raw_source.password.startswith("enc:")
    assert raw_record.remote_uid.startswith("enc:c1:")
    assert json.loads(raw_record.payload).startswith("enc:c1:")


def test_carddav_outbox_payload_is_encrypted_at_rest(contacts_env, monkeypatch):
    import src.carddav_contacts as carddav

    monkeypatch.setattr(
        carddav, "validate_carddav_url", lambda value: str(value).rstrip("/")
    )
    db = contacts_env.Session()
    try:
        upsert_carddav_config(
            db,
            owner_id=contacts_env.alice.id,
            url="https://dav.example/private-addressbook",
            username="outbox-private-user",
            password="outbox-private-password",
        )
        contact = create_contact(
            db,
            owner_id=contacts_env.alice.id,
            name="Outbox Private Person",
            email="outbox-private@example.test",
            phones=["+15550009999"],
            address="Outbox Private Address",
        )
        delivery = db.query(ContactDelivery).one()
        assert delivery.payload["uid"] == contact["uid"]
        assert "Outbox Private Person" in delivery.payload["raw_vcard"]
        delivery_id = delivery.id
        db.commit()
    finally:
        db.close()

    with contacts_env.engine.connect() as connection:
        raw_payload = connection.execute(
            text("SELECT payload FROM contact_deliveries WHERE id=:id"),
            {"id": delivery_id},
        ).scalar_one()
    assert json.loads(raw_payload).startswith("enc:c1:")
    for private in (
        contact["uid"],
        "Outbox Private Person",
        "outbox-private@example.test",
        "+15550009999",
        "Outbox Private Address",
    ):
        assert private not in raw_payload


def test_carddav_origin_change_drops_old_credentials_without_fresh_password(
    contacts_env, monkeypatch
):
    import src.carddav_contacts as carddav

    monkeypatch.setattr(
        carddav, "validate_carddav_url", lambda value: str(value).rstrip("/")
    )
    db = contacts_env.Session()
    try:
        source = upsert_carddav_config(
            db,
            owner_id=contacts_env.alice.id,
            url="https://old.example/addressbook",
            username="old-private-user",
            password="old-private-password",
        )
        db.commit()
        updated = upsert_carddav_config(
            db,
            owner_id=contacts_env.alice.id,
            url="https://new.example/addressbook",
            username="should-not-bind-without-a-new-password",
            expected_version=source.version,
        )
        db.commit()

        assert updated.base_url == "https://new.example/addressbook"
        assert updated.username is None
        assert updated.password is None
    finally:
        db.close()


def test_contact_and_config_stale_versions_are_rejected(contacts_env, monkeypatch):
    import src.carddav_contacts as carddav

    monkeypatch.setattr(
        carddav, "validate_carddav_url", lambda value: str(value).rstrip("/")
    )
    db = contacts_env.Session()
    try:
        source = upsert_carddav_config(
            db,
            owner_id=contacts_env.alice.id,
            url="https://dav.example/addressbook",
            username="first-user",
            password="first-password",
        )
        contact = create_contact(
            db,
            owner_id=contacts_env.alice.id,
            name="Version One",
            email="version-one@example.test",
        )
        db.commit()
        config_version = source.version
        record_version = contact["version"]

        upsert_carddav_config(
            db,
            owner_id=contacts_env.alice.id,
            username="second-user",
            expected_version=config_version,
        )
        updated = update_contact(
            db,
            owner_id=contacts_env.alice.id,
            uid=contact["uid"],
            name="Version Two",
            emails=["version-two@example.test"],
            phones=[],
            expected_version=record_version,
        )
        db.commit()

        with pytest.raises(ContactConflict, match="configuration changed"):
            upsert_carddav_config(
                db,
                owner_id=contacts_env.alice.id,
                username="stale-config-writer",
                expected_version=config_version,
            )
        db.rollback()
        with pytest.raises(ContactConflict, match="Contact changed"):
            update_contact(
                db,
                owner_id=contacts_env.alice.id,
                uid=contact["uid"],
                name="Stale Record Writer",
                emails=["stale-record@example.test"],
                phones=[],
                expected_version=record_version,
            )
        db.rollback()

        current = search_contacts(
            db, owner_id=contacts_env.alice.id, query="version-two"
        )[0]
        assert current["version"] == updated["version"] == record_version + 1
        assert current["name"] == "Version Two"
    finally:
        db.close()


def test_contact_action_audits_are_attributed_without_pii(contacts_env, monkeypatch):
    import src.carddav_contacts as carddav

    monkeypatch.setattr(
        carddav, "validate_carddav_url", lambda value: str(value).rstrip("/")
    )
    db = contacts_env.Session()
    try:
        bind_service_audit_context(
            db,
            account_id=contacts_env.alice.id,
            interface="cli",
            actor_type="account",
            credential_type="local_cli",
        )
        upsert_carddav_config(
            db,
            owner_id=contacts_env.alice.id,
            url="https://private-audit.example/addressbook",
            username="private-audit-login",
            password="private-audit-password",
        )
        contact = create_contact(
            db,
            owner_id=contacts_env.alice.id,
            name="Private Audit Person",
            email="private.audit@example.test",
            phones=["+15550123456"],
            address="Private Audit Address",
        )
        updated = update_contact(
            db,
            owner_id=contacts_env.alice.id,
            uid=contact["uid"],
            name="Private Audit Person Updated",
            emails=["private.updated@example.test"],
            phones=["+15550654321"],
            address="Private Audit Address Updated",
            expected_version=contact["version"],
        )
        delete_contact(
            db,
            owner_id=contacts_env.alice.id,
            uid=contact["uid"],
            expected_version=updated["version"],
        )
        db.commit()

        rows = db.query(ActionAudit).filter(
            ActionAudit.owner_id == contacts_env.alice.id,
            ActionAudit.action.in_((
                "contacts.configured",
                "contacts.created",
                "contacts.updated",
                "contacts.deleted",
            )),
        ).all()
        assert {row.action for row in rows} == {
            "contacts.configured",
            "contacts.created",
            "contacts.updated",
            "contacts.deleted",
        }
        for row in rows:
            attribution = row.details["audit"]
            assert attribution["actor_id"] == contacts_env.alice.id
            assert attribution["actor_type"] == "account"
            assert attribution["interface"] == "cli"
            assert attribution["credential_type"] == "local_cli"

        rendered = json.dumps([
            {
                "before": row.before_state,
                "after": row.after_state,
                "details": row.details,
            }
            for row in rows
        ], sort_keys=True)
        for private in (
            "private-audit.example",
            "private-audit-login",
            "private-audit-password",
            "Private Audit Person",
            "private.audit@example.test",
            "private.updated@example.test",
            "+15550123456",
            "+15550654321",
            "Private Audit Address",
        ):
            assert private not in rendered
    finally:
        db.close()


def test_duplicate_remote_and_import_uids_are_rejected(contacts_env, monkeypatch):
    import src.carddav_contacts as carddav

    monkeypatch.setattr(
        carddav, "validate_carddav_url", lambda value: str(value).rstrip("/")
    )
    db = contacts_env.Session()
    try:
        source = upsert_carddav_config(
            db,
            owner_id=contacts_env.alice.id,
            url="https://dav.example/addressbook",
        )
        db.commit()
        duplicate = {
            "uid": "duplicate-remote-uid",
            "name": "Duplicate",
            "emails": [],
            "phones": [],
            "address": "",
        }
        monkeypatch.setattr(carddav, "fetch_contacts", lambda _config: [
            dict(duplicate), dict(duplicate),
        ])
        with pytest.raises(ContactServiceError, match="duplicate contact identifiers"):
            refresh_contact_source(
                db,
                owner_id=contacts_env.alice.id,
                source_id=source.id,
                raise_errors=True,
            )
        db.rollback()
        assert db.query(ContactRecord).count() == 0

        duplicate_vcards = """BEGIN:VCARD
VERSION:3.0
UID:duplicate-import-uid
FN:First Duplicate
END:VCARD
BEGIN:VCARD
VERSION:3.0
UID:duplicate-import-uid
FN:Second Duplicate
END:VCARD
"""
        with pytest.raises(ContactServiceError, match="duplicate contact identifiers"):
            import_vcards(
                db,
                owner_id=contacts_env.alice.id,
                text=duplicate_vcards,
            )
        db.rollback()
        assert db.query(ContactRecord).count() == 0
    finally:
        db.close()


def test_failed_refresh_returns_only_same_owner_snapshot(contacts_env, monkeypatch):
    import src.carddav_contacts as carddav
    import src.contact_service as service

    monkeypatch.setattr(
        carddav, "validate_carddav_url", lambda value: str(value).rstrip("/")
    )
    db = contacts_env.Session()
    try:
        alice_source = upsert_carddav_config(
            db, owner_id=contacts_env.alice.id, url="https://alice.example/dav"
        )
        bob_source = upsert_carddav_config(
            db, owner_id=contacts_env.bob.id, url="https://bob.example/dav"
        )
        db.commit()

        def fetch(config):
            if "alice.example" in config["url"]:
                return [{
                    "uid": "alice-only", "name": "Alice only",
                    "emails": [], "phones": [], "address": "",
                }]
            raise carddav.CardDAVError("offline")

        monkeypatch.setattr(carddav, "fetch_contacts", fetch)
        assert service.refresh_contact_source(
            db, owner_id=contacts_env.alice.id, source_id=alice_source.id
        )[0]["name"] == "Alice only"
        assert service.refresh_contact_source(
            db, owner_id=contacts_env.bob.id, source_id=bob_source.id
        ) == []
    finally:
        db.close()


@pytest.fixture()
def contacts_app(contacts_env):
    app = FastAPI()
    app.state.auth_manager = _IdentityAuthority("alice", "bob")
    app.state.contacts_store_error = None

    @app.middleware("http")
    async def identity(request, call_next):
        api_owner = request.headers.get("x-api-owner")
        if api_owner:
            request.state.api_token = True
            request.state.api_token_owner = api_owner
            request.state.api_token_scopes = request.headers.get(
                "x-api-scopes", ""
            ).split(",")
            request.state.current_user = "api"
        else:
            request.state.api_token = False
            request.state.current_user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(setup_contacts_routes(session_factory=contacts_env.Session))
    return app


async def _request(app, method, path, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.request(method, path, headers=headers, **kwargs)


@pytest.mark.asyncio
async def test_api_scopes_and_owner_isolation(contacts_app):
    denied = await _request(
        contacts_app,
        "POST",
        "/api/contacts/add",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "contacts:read"},
        json={"name": "Alice contact", "email": "alice-contact@example.test"},
    )
    assert denied.status_code == 403

    created = await _request(
        contacts_app,
        "POST",
        "/api/contacts/add",
        json={"name": "Alice contact", "email": "alice-contact@example.test"},
    )
    assert created.status_code == 200, created.text

    bob = await _request(contacts_app, "GET", "/api/contacts/list", user="bob")
    assert bob.status_code == 200
    assert bob.json() == {"contacts": [], "count": 0}

    scoped = await _request(
        contacts_app,
        "GET",
        "/api/contacts/list",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "contacts:read"},
    )
    assert scoped.status_code == 200
    assert scoped.json()["contacts"][0]["name"] == "Alice contact"

    write_implies_read = await _request(
        contacts_app,
        "GET",
        "/api/contacts/list",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "contacts:write"},
    )
    assert write_implies_read.status_code == 200


@pytest.mark.asyncio
async def test_contacts_write_scope_cannot_change_carddav_configuration(
    contacts_app,
):
    denied = await _request(
        contacts_app,
        "PUT",
        "/api/contacts/config",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "contacts:write"},
        json={
            "url": "https://should-not-be-configured.example/addressbook",
            "username": "forbidden-user",
            "password": "forbidden-password",
        },
    )
    assert denied.status_code == 403


@pytest.mark.asyncio
async def test_tool_requires_owner_and_never_reads_another_owner(
    contacts_env, monkeypatch
):
    import src.tools.contacts as tools

    monkeypatch.setattr(tools, "SessionLocal", contacts_env.Session)
    db = contacts_env.Session()
    try:
        create_contact(
            db,
            owner_id=contacts_env.alice.id,
            name="Alice tool contact",
            email="tool-alice@example.test",
        )
        db.commit()
    finally:
        db.close()

    missing = await tools.do_manage_contact('{"action":"list"}', owner=None)
    assert missing["exit_code"] == 1
    bob = await tools.do_manage_contact('{"action":"list"}', owner="bob")
    assert bob == {"output": "No contacts.", "exit_code": 0}
    alice = await tools.do_manage_contact('{"action":"list"}', owner="alice")
    assert "Alice tool contact" in alice["output"]
