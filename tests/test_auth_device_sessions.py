from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from routes.auth_routes import SESSION_COOKIE, setup_auth_routes
from src.database_auth_manager import DatabaseAuthManager


@pytest.fixture()
def device_session_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'device-sessions.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = [datetime(2026, 7, 17, 12, 0, 0)]
    manager = DatabaseAuthManager(
        factory,
        token_hmac_key=b"device-session-test-key-material-32b",
        now=lambda: now[0],
    )
    assert manager.setup("alice", "correct horse battery staple")
    manager.signup_enabled = True
    assert manager.create_user("bob", "correct horse battery staple")
    app = FastAPI()
    app.include_router(setup_auth_routes(manager, identity_renamer=lambda *_args, **_kwargs: None))
    yield SimpleNamespace(app=app, manager=manager, Session=factory, engine=engine)
    engine.dispose()


def test_database_device_session_listing_and_owner_scoped_revocation(device_session_env):
    env = device_session_env
    first = env.manager.create_session("alice", "correct horse battery staple")
    second = env.manager.create_session("alice", "correct horse battery staple")
    bob = env.manager.create_session("bob", "correct horse battery staple")
    assert first and second and bob

    sessions = env.manager.list_user_sessions("alice", second)
    assert len(sessions) == 2
    assert sum(bool(item["current"]) for item in sessions) == 1
    assert all("token" not in item for item in sessions)
    assert all(item["interface"] == "web" for item in sessions)
    current = next(item for item in sessions if item["current"])
    assert env.manager.resolve_session(second).credential_id == current["id"]

    bob_id = env.manager.resolve_session(bob).credential_id
    assert env.manager.revoke_user_session("alice", bob_id) is False
    assert env.manager.resolve_session(bob) is not None
    first_id = env.manager.resolve_session(first).credential_id
    assert env.manager.revoke_user_session("alice", first_id) is True
    assert env.manager.resolve_session(first) is None
    assert env.manager.resolve_session(second) is not None


@pytest.mark.asyncio
async def test_auth_session_routes_hide_tokens_and_revoke_current_or_other_device(
    device_session_env,
):
    env = device_session_env
    current_token = env.manager.create_session("alice", "correct horse battery staple")
    other_token = env.manager.create_session("alice", "correct horse battery staple")
    bob_token = env.manager.create_session("bob", "correct horse battery staple")
    assert current_token and other_token and bob_token
    current_id = env.manager.resolve_session(current_token).credential_id
    other_id = env.manager.resolve_session(other_token).credential_id
    bob_id = env.manager.resolve_session(bob_token).credential_id

    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        cookies = {SESSION_COOKIE: current_token}
        listing = await client.get("/api/auth/sessions", cookies=cookies)
        assert listing.status_code == 200, listing.text
        body = listing.json()
        assert body["count"] == 2
        rendered = listing.text
        assert current_token not in rendered
        assert other_token not in rendered
        assert all("token_digest" not in item for item in body["sessions"])

        foreign = await client.delete(f"/api/auth/sessions/{bob_id}", cookies=cookies)
        assert foreign.status_code == 404
        assert env.manager.resolve_session(bob_token) is not None

        revoked_other = await client.delete(
            f"/api/auth/sessions/{other_id}", cookies=cookies,
        )
        assert revoked_other.status_code == 200, revoked_other.text
        assert revoked_other.json()["current"] is False
        assert env.manager.resolve_session(other_token) is None
        assert env.manager.resolve_session(current_token) is not None

        revoked_current = await client.delete(
            f"/api/auth/sessions/{current_id}", cookies=cookies,
        )
        assert revoked_current.status_code == 200, revoked_current.text
        assert revoked_current.json()["current"] is True
        assert env.manager.resolve_session(current_token) is None
        assert SESSION_COOKIE in revoked_current.headers.get("set-cookie", "")
