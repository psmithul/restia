from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Account, AuthSession, Base, MfaFactor
from routes.security_routes import setup_security_routes
from src.backup_encryption import encrypt_backup
from src.identity import ensure_account
from src.profile_configuration_service import put_configuration
from src.security_posture import build_security_posture


PASSPHRASE = "a separate secure backup passphrase"


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
def posture_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(secret_storage, "_digest_key", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'posture.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
    db = factory()
    try:
        alice = ensure_account(db, "alice")
        bob = ensure_account(db, "bob")
        db.add_all([
            AuthSession(
                id=str(uuid.uuid4()),
                account_id=alice.id,
                token_digest="a" * 64,
                expires_at=(now + timedelta(days=1)).replace(tzinfo=None),
                interface="web",
                auth_method="local",
                auth_epoch=alice.auth_epoch,
            ),
            MfaFactor(
                id=str(uuid.uuid4()),
                account_id=alice.id,
                kind="totp",
                state="active",
                confirmed_at=now.replace(tzinfo=None),
            ),
        ])
        put_configuration(
            db,
            account=alice,
            namespace="integration",
            key="home",
            value={
                "id": "home",
                "name": "Home",
                "base_url": "https://home.example.test",
                "auth_type": "bearer",
                "api_key": "credential-that-must-never-appear",
                "permissions": {
                    "allowed_methods": ["GET", "POST"],
                    "allowed_path_prefixes": ["/api/states", "/api/services/light"],
                    "require_action_approval_for_writes": True,
                },
            },
            expected_version=None,
            source="domain_service",
        )
        db.commit()
        ids = {"alice": alice.id, "bob": bob.id}
    finally:
        db.close()

    backups = tmp_path / "backups"
    backups.mkdir()
    plain = tmp_path / "snapshot.tar.gz"
    plain.write_bytes(b"private backup payload" * 100)
    encrypted = backups / "restia-v3.tar.gz.restia"
    encrypt_backup(plain, encrypted, PASSPHRASE)
    timestamp = now.timestamp()
    encrypted.touch()
    import os

    os.utime(encrypted, (timestamp, timestamp))

    app = FastAPI()
    app.state.auth_manager = _IdentityAuthority("alice", "bob")

    @app.middleware("http")
    async def inject_identity(request, call_next):
        token_owner = request.headers.get("x-api-owner")
        if token_owner:
            request.state.api_token = True
            request.state.api_token_owner = token_owner
            request.state.api_token_scopes = request.headers.get(
                "x-api-scopes", ""
            ).split(",")
            request.state.current_user = "api"
        else:
            request.state.api_token = False
            request.state.current_user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(
        setup_security_routes(session_factory=factory, backup_dir=backups)
    )
    yield SimpleNamespace(
        app=app, Session=factory, engine=engine, ids=ids, backups=backups, now=now,
    )
    engine.dispose()


def test_security_posture_reports_controls_without_secrets(posture_env):
    db = posture_env.Session()
    try:
        alice = db.query(Account).filter_by(username="alice").one()
        posture = build_security_posture(
            db,
            account=alice,
            backup_dir=posture_env.backups,
            now=posture_env.now,
        )
        assert posture["overall"] == "attention"
        assert posture["backups"]["current"] is True
        device_unlock = next(
            item for item in posture["checks"]
            if item["id"] == "biometric_device_controls"
        )
        assert device_unlock["status"] == "attention"
        assert "0 server-verified passkey(s)" in device_unlock["detail"]
        connector = next(
            item for item in posture["checks"]
            if item["id"] == "connector_permissions"
        )
        assert "1 connector(s), 1 with approved-write grants" in connector["detail"]
        rendered = json.dumps(posture)
        assert "credential-that-must-never-appear" not in rendered
        assert "token_digest" not in rendered
        assert "password" not in posture["backups"]["command"].lower()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_security_posture_route_is_authenticated_scoped_and_scope_gated(posture_env):
    transport = httpx.ASGITransport(app=posture_env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unauthenticated = await client.get("/api/life/security-posture")
        assert unauthenticated.status_code == 401

        alice = await client.get(
            "/api/life/security-posture", headers={"x-user": "alice"},
        )
        assert alice.status_code == 200, alice.text
        assert "1 connector(s)" in next(
            item for item in alice.json()["checks"]
            if item["id"] == "connector_permissions"
        )["detail"]

        bob = await client.get(
            "/api/life/security-posture", headers={"x-user": "bob"},
        )
        assert bob.status_code == 200, bob.text
        assert "0 connector(s)" in next(
            item for item in bob.json()["checks"]
            if item["id"] == "connector_permissions"
        )["detail"]

        denied = await client.get(
            "/api/life/security-posture",
            headers={"x-api-owner": "alice", "x-api-scopes": "todo:read"},
        )
        assert denied.status_code == 403
        allowed = await client.get(
            "/api/life/security-posture",
            headers={"x-api-owner": "alice", "x-api-scopes": "life:read"},
        )
        assert allowed.status_code == 200


def test_application_registers_security_posture_router_once():
    source = (Path(__file__).resolve().parents[1] / "app.py").read_text(
        encoding="utf-8"
    )
    assert source.count("from routes.security_routes import setup_security_routes") == 1
    assert source.count("app.include_router(setup_security_routes())") == 1
