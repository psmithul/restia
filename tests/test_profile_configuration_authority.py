from __future__ import annotations

import hashlib
import json
import threading
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from core.database import Account, Base
from routes.profile_configuration_routes import setup_profile_configuration_routes
from src.audit_context import bind_service_audit_context
from src.profile_configuration_import import adopt_legacy_profile_configuration
from src.profile_configuration_models import ProfileConfiguration
from src.profile_configuration_service import (
    MAX_IMPORT_ENTRIES,
    ProfileConfigurationConflict,
    ProfileConfigurationError,
    delete_configuration,
    get_configuration,
    import_legacy_configuration,
    list_configurations,
    put_configuration,
    serialize_configuration,
)


class _IdentityAuthority:
    def __init__(self, *usernames: str):
        self._config_lock = threading.Lock()
        self._identity_migrations: set[str] = set()
        self.retired_usernames: set[str] = set()
        self.users = {name: {} for name in usernames}

    @property
    def is_configured(self) -> bool:
        return bool(self.users)


@pytest.fixture()
def configuration_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'profile-configuration.db'}",
        connect_args={"check_same_thread": False},
    )
    # Importing the service/models above registers the isolated tables.
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    db = factory()
    alice = Account(id=str(uuid.uuid4()), username="alice", status="active")
    bob = Account(id=str(uuid.uuid4()), username="bob", status="active")
    db.add_all([alice, bob])
    db.commit()
    db.close()
    yield SimpleNamespace(engine=engine, Session=factory, alice=alice, bob=bob)
    engine.dispose()


def test_owner_isolation_and_private_values_are_encrypted(configuration_env):
    env = configuration_env
    db = env.Session()
    try:
        private = put_configuration(
            db,
            account=env.alice,
            namespace="preference",
            key="email.signature",
            value={"text": "private-signature-marker"},
            source="browser",
        )
        public = put_configuration(
            db,
            account=env.alice,
            namespace="feature",
            key="gallery",
            value=False,
            source="browser",
        )
        db.commit()
        assert serialize_configuration(private.record)["value"] == {
            "text": "private-signature-marker"
        }
        assert serialize_configuration(public.record)["private"] is False
        assert list_configurations(
            db, owner_id=env.bob.id, namespace="preference",
        )[0] == []

        with env.engine.connect() as connection:
            raw_private = connection.execute(text(
                "SELECT private_value FROM profile_configurations "
                "WHERE id = :id"
            ), {"id": private.record.id}).scalar_one()
            raw_public = connection.execute(text(
                "SELECT public_value FROM profile_configurations "
                "WHERE id = :id"
            ), {"id": public.record.id}).scalar_one()
        assert "private-signature-marker" not in str(raw_private)
        assert "enc:c1:" in str(raw_private)
        assert json.loads(raw_public) == {"value": False}
    finally:
        db.close()


def test_server_bound_web_interface_is_preserved_separately_from_write_source(
    configuration_env,
):
    env = configuration_env
    db = env.Session()
    try:
        bind_service_audit_context(
            db,
            account_id=env.alice.id,
            interface="web",
            credential_type="session",
        )
        result = put_configuration(
            db,
            account=env.alice,
            namespace="preference",
            key="ui.compact_mode",
            value=True,
            source="browser",
        )
        serialized = serialize_configuration(result.record)
        assert serialized["source"] == "browser"
        assert serialized["updated_interface"] == "web"
    finally:
        db.close()


def test_optimistic_versions_idempotency_and_non_destructive_delete(configuration_env):
    env = configuration_env
    db = env.Session()
    try:
        created = put_configuration(
            db,
            account=env.alice,
            namespace="setting",
            key="tts_voice",
            value="alloy",
            source="api",
            idempotency_key="voice-create-1",
        )
        db.flush()
        replay = put_configuration(
            db,
            account=env.alice,
            namespace="setting",
            key="tts_voice",
            value="alloy",
            source="api",
            idempotency_key="voice-create-1",
        )
        assert created.record.version == 1
        assert replay.idempotent is True
        with pytest.raises(ProfileConfigurationConflict, match="different request"):
            put_configuration(
                db,
                account=env.alice,
                namespace="setting",
                key="tts_voice",
                value="echo",
                expected_version=1,
                source="api",
                idempotency_key="voice-create-1",
            )
        with pytest.raises(ProfileConfigurationConflict, match="expected_version"):
            put_configuration(
                db,
                account=env.alice,
                namespace="setting",
                key="tts_voice",
                value="echo",
                source="api",
            )
        updated = put_configuration(
            db,
            account=env.alice,
            namespace="setting",
            key="tts_voice",
            value="echo",
            expected_version=1,
            source="api",
        )
        assert updated.record.version == 2
        with pytest.raises(ProfileConfigurationConflict, match="superseded"):
            put_configuration(
                db,
                account=env.alice,
                namespace="setting",
                key="tts_voice",
                value="alloy",
                source="api",
                idempotency_key="voice-create-1",
            )
        with pytest.raises(ProfileConfigurationConflict, match="version"):
            put_configuration(
                db,
                account=env.alice,
                namespace="setting",
                key="tts_voice",
                value="nova",
                expected_version=1,
                source="api",
            )
        deleted = delete_configuration(
            db,
            account=env.alice,
            namespace="setting",
            key="tts_voice",
            expected_version=2,
            source="api",
            idempotency_key="voice-delete-1",
        )
        assert deleted.record.state == "deleted"
        assert deleted.record.version == 3
        replay_delete = delete_configuration(
            db,
            account=env.alice,
            namespace="setting",
            key="tts_voice",
            expected_version=2,
            source="api",
            idempotency_key="voice-delete-1",
        )
        assert replay_delete.idempotent is True
        assert get_configuration(
            db,
            owner_id=env.alice.id,
            namespace="setting",
            key="tts_voice",
            include_deleted=True,
        ).state == "deleted"
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize(
    "namespace,key,value,match",
    [
        ("preference", "database_url", "postgresql://private", "deployment"),
        ("preference", "restia_encryption_key", "private", "deployment"),
        ("setting", "email_auto_tag", True, "email automation rules"),
        ("setting", "unknown_setting", True, "Unknown mutable"),
        ("feature", "unknown_feature", True, "Unknown feature"),
    ],
)
def test_deployment_and_split_authority_keys_are_rejected(
    configuration_env, namespace, key, value, match,
):
    db = configuration_env.Session()
    try:
        with pytest.raises(ProfileConfigurationError, match=match):
            put_configuration(
                db,
                account=configuration_env.alice,
                namespace=namespace,
                key=key,
                value=value,
            )
    finally:
        db.rollback()
        db.close()


def test_legacy_import_is_bounded_idempotent_and_never_overwrites(configuration_env):
    env = configuration_env
    db = env.Session()
    try:
        put_configuration(
            db,
            account=env.alice,
            namespace="setting",
            key="tts_voice",
            value="canonical",
        )
        payload = {
            "tts_voice": "legacy",
            "tts_speed": "1.25",
            "database_url": "postgresql://must-not-import",
            "email_auto_tag": True,
        }
        digest = hashlib.sha256(json.dumps(payload).encode()).hexdigest()
        first = import_legacy_configuration(
            db,
            account=env.alice,
            source_kind="settings_json",
            source_sha256=digest,
            payload=payload,
        )
        second = import_legacy_configuration(
            db,
            account=env.alice,
            source_kind="settings_json",
            source_sha256=digest,
            payload=payload,
        )
        assert first.imported == 1
        assert first.skipped == 3
        assert second.idempotent is True
        assert serialize_configuration(get_configuration(
            db,
            owner_id=env.alice.id,
            namespace="setting",
            key="tts_voice",
        ))["value"] == "canonical"
        assert serialize_configuration(get_configuration(
            db,
            owner_id=env.alice.id,
            namespace="setting",
            key="tts_speed",
        ))["value"] == "1.25"

        preferences = {
            "_users": {
                "alice": {"sidebar_collapsed": {"brain": True}},
                "bob": {"sidebar_collapsed": {"email": True}},
            }
        }
        prefs_digest = hashlib.sha256(json.dumps(preferences).encode()).hexdigest()
        import_legacy_configuration(
            db,
            account=env.alice,
            source_kind="user_prefs_json",
            source_sha256=prefs_digest,
            payload=preferences,
        )
        assert serialize_configuration(get_configuration(
            db,
            owner_id=env.alice.id,
            namespace="preference",
            key="sidebar_collapsed",
        ))["value"] == {"brain": True}
        assert list_configurations(
            db, owner_id=env.bob.id, namespace="preference",
        )[0] == []

        too_many = {f"pref_{index}": index for index in range(MAX_IMPORT_ENTRIES + 1)}
        with pytest.raises(ProfileConfigurationError, match="entry limit"):
            import_legacy_configuration(
                db,
                account=env.alice,
                source_kind="user_prefs_json",
                source_sha256=hashlib.sha256(b"too-many").hexdigest(),
                payload=too_many,
            )
    finally:
        db.rollback()
        db.close()


def test_legacy_miniflux_settings_become_one_private_integration(configuration_env):
    env = configuration_env
    db = env.Session()
    try:
        existing = put_configuration(
            db,
            account=env.alice,
            namespace="integration",
            key="existing-miniflux",
            value={
                "id": "existing-miniflux",
                "preset": "miniflux",
                "name": "Miniflux",
                "auth_type": "header",
                "auth_header": "X-Auth-Token",
                "auth_param": "",
                "description": "",
                "api_key": "canonical-secret",
                "base_url": "https://rss.example.test",
                "enabled": True,
            },
        )
        payload = {
            "miniflux_url": "https://legacy-rss.example.test/v1",
            "miniflux_api_key": "legacy-secret",
        }
        duplicate = import_legacy_configuration(
            db,
            account=env.alice,
            source_kind="settings_json",
            source_sha256=hashlib.sha256(b"legacy-miniflux-duplicate").hexdigest(),
            payload=payload,
        )
        assert duplicate.imported == 0
        assert duplicate.skipped == 1

        second_payload = dict(payload)
        result = import_legacy_configuration(
            db,
            account=env.bob,
            source_kind="settings_json",
            source_sha256=hashlib.sha256(b"legacy-miniflux-new").hexdigest(),
            payload=second_payload,
        )
        assert result.imported == 1
        record = get_configuration(
            db,
            owner_id=env.bob.id,
            namespace="integration",
            key="miniflux",
        )
        serialized = serialize_configuration(record)
        assert serialized["private"] is True
        assert serialized["value"]["api_key"] == "legacy-secret"
        assert serialized["value"]["base_url"] == "https://legacy-rss.example.test/v1"
        assert serialize_configuration(existing.record)["value"]["api_key"] == "canonical-secret"
    finally:
        db.rollback()
        db.close()


def test_legacy_file_adoption_is_non_destructive_and_profile_scoped(
    configuration_env, tmp_path,
):
    env = configuration_env
    paths = {
        "settings_path": tmp_path / "settings.json",
        "preferences_path": tmp_path / "user_prefs.json",
        "features_path": tmp_path / "features.json",
        "integrations_path": tmp_path / "integrations.json",
    }
    payloads = {
        "settings_path": {
            "tts_voice": "nova",
            "miniflux_url": "https://rss.example.test",
            "miniflux_api_key": "legacy-miniflux-secret",
        },
        "preferences_path": {
            "_users": {
                "alice": {"theme": "paper"},
                "bob": {"theme": "dark"},
            },
        },
        "features_path": {"gallery": False},
        "integrations_path": [{
            "id": "notes-api",
            "name": "Notes API",
            "base_url": "https://notes.example.test",
        }],
    }
    originals: dict[str, bytes] = {}
    for name, path in paths.items():
        path.write_text(json.dumps(payloads[name]), encoding="utf-8")
        originals[name] = path.read_bytes()

    first = adopt_legacy_profile_configuration(
        session_factory=env.Session,
        auth_enabled=True,
        primary_admin_resolver=lambda: "alice",
        profile_usernames=("alice", "bob"),
        **paths,
    )
    second = adopt_legacy_profile_configuration(
        session_factory=env.Session,
        auth_enabled=True,
        primary_admin_resolver=lambda: "alice",
        profile_usernames=("alice", "bob"),
        **paths,
    )
    assert first.source_preserved is True
    assert first.imported == 6
    assert second.idempotent_runs == second.runs == 5
    for name, path in paths.items():
        assert path.read_bytes() == originals[name]

    db = env.Session()
    try:
        alice_theme = serialize_configuration(get_configuration(
            db,
            owner_id=env.alice.id,
            namespace="preference",
            key="theme",
        ))["value"]
        bob_theme = serialize_configuration(get_configuration(
            db,
            owner_id=env.bob.id,
            namespace="preference",
            key="theme",
        ))["value"]
        assert alice_theme == "paper"
        assert bob_theme == "dark"
    finally:
        db.close()


def test_legacy_runtime_adapters_converge_on_account_owned_sql(
    configuration_env, monkeypatch,
):
    import core.database as database
    import routes.prefs_routes as prefs_routes
    from src import integrations, settings

    env = configuration_env
    monkeypatch.setattr(database, "SessionLocal", env.Session)
    monkeypatch.setattr(prefs_routes, "SessionLocal", env.Session)
    settings._invalidate_caches()

    settings.save_settings({"tts_voice": "nova"}, owner="alice")
    settings.save_features({"gallery": False}, owner="alice")
    prefs_routes._save_for_user("alice", {"theme": "paper"})
    integrations.save_integrations([{
        "id": "notes",
        "name": "Notes",
        "base_url": "https://notes.example.test",
    }], owner="alice")

    assert settings.load_settings(owner="alice")["tts_voice"] == "nova"
    assert settings.load_settings(owner="bob")["tts_voice"] == settings.DEFAULT_SETTINGS["tts_voice"]
    assert settings.load_features(owner="alice")["gallery"] is False
    assert settings.load_features(owner="bob")["gallery"] is True
    assert prefs_routes._load_for_user("alice") == {"theme": "paper"}
    assert prefs_routes._load_for_user("bob") == {}
    assert integrations.load_integrations(owner="alice")[0]["id"] == "notes"
    assert integrations.load_integrations(owner="bob") == []

    db = env.Session()
    try:
        rows = db.query(ProfileConfiguration).filter(
            ProfileConfiguration.owner_id == env.alice.id,
        ).all()
        assert {row.namespace for row in rows} == {
            "setting", "feature", "preference", "integration",
        }
    finally:
        db.close()


@pytest.fixture()
def configuration_routes_env(configuration_env):
    env = configuration_env
    app = FastAPI()
    app.state.auth_manager = _IdentityAuthority("alice", "bob")

    @app.middleware("http")
    async def inject_identity(request, call_next):
        api_owner = request.headers.get("x-api-owner")
        request.state.api_token = bool(api_owner)
        request.state.api_token_owner = api_owner
        request.state.api_token_scopes = (
            request.headers.get("x-api-scopes", "").split(",") if api_owner else []
        )
        request.state.current_user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(setup_profile_configuration_routes(session_factory=env.Session))
    return SimpleNamespace(**env.__dict__, app=app)


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, **kwargs)


@pytest.mark.asyncio
async def test_browser_and_scoped_api_use_same_account_owned_configuration(
    configuration_routes_env,
):
    env = configuration_routes_env
    created = await _call(
        env,
        "PUT",
        "/api/profile/configuration/preference/timezone",
        json={"value": "Asia/Kolkata", "idempotency_key": "timezone-1"},
    )
    assert created.status_code == 200, created.text
    assert created.json()["configuration"]["version"] == 1

    api_read = await _call(
        env,
        "GET",
        "/api/profile/configuration/preference/timezone",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "profile:read"},
    )
    assert api_read.status_code == 200, api_read.text
    assert api_read.json()["configuration"]["value"] == "Asia/Kolkata"
    denied = await _call(
        env,
        "PUT",
        "/api/profile/configuration/preference/timezone",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "profile:read"},
        json={"value": "UTC", "expected_version": 1},
    )
    assert denied.status_code == 403
    bob = await _call(
        env,
        "GET",
        "/api/profile/configuration/preference/timezone",
        user="bob",
    )
    assert bob.status_code == 404


def test_profile_configuration_migration_upgrades_after_email_runtime_head(tmp_path):
    from alembic import command
    from src.database_migrations import _alembic_config

    database_url = f"sqlite:///{tmp_path / 'profile-configuration-migration.db'}"
    engine = create_engine(database_url)
    config = _alembic_config(database_url)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.stamp(config, "20260716_0001")
        command.upgrade(config, "20260728_0013")
    inspector = inspect(engine)
    assert {
        "profile_configurations",
        "profile_configuration_mutations",
        "profile_configuration_import_runs",
    } <= set(inspector.get_table_names())
    assert {
        column["name"]
        for column in inspector.get_columns("profile_configurations")
    } >= {
        "owner_id", "namespace", "key", "public_value", "private_value",
        "version", "deleted_at",
    }
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "20260727_0012")
    assert "profile_configurations" not in inspect(engine).get_table_names()
    engine.dispose()
