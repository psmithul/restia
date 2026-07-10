"""LOCALHOST_BYPASS loopback callers must not 401 on page-load fetches.

The auth middleware admits direct-loopback requests wholesale when
LOCALHOST_BYPASS=true, and the shared ``require_user`` mirrors that by
resolving them to the anonymous "" user. Routes that hand-roll their own
guard and skip that case 401 a caller the middleware just let through —
and because the SPA's global fetch wrapper redirects to /login on ANY 401,
one such route fetched at boot (``/api/research/active``, ``/api/models``)
locks the whole app into an infinite /login redirect loop.

Regression for the research routes' local ``_require_user`` and the
``/api/models`` anonymous gate.
"""

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI

_LOOPBACK_PEER = ("127.0.0.1", 54321)
_LAN_PEER = ("203.0.113.7", 54321)


def _client(app, peer):
    transport = httpx.ASGITransport(app=app, client=peer)
    return httpx.AsyncClient(transport=transport, base_url="http://bypass.test")


def _bypass_env(monkeypatch):
    """Configured-auth install with the dev loopback bypass switched on."""
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("LOCALHOST_BYPASS", "true")


def _research_app():
    from routes.research.research_routes import setup_research_routes

    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True)
    handler = SimpleNamespace(_active_tasks={})
    app.include_router(setup_research_routes(handler))
    return app


@pytest.mark.asyncio
async def test_research_active_admits_bypass_loopback(monkeypatch):
    _bypass_env(monkeypatch)
    async with _client(_research_app(), _LOOPBACK_PEER) as client:
        res = await client.get("/api/research/active")
    assert res.status_code == 200
    assert res.json() == {"active": []}


@pytest.mark.asyncio
async def test_research_active_still_rejects_anonymous_lan(monkeypatch):
    """The bypass is loopback-only; an unauthenticated LAN caller stays 401."""
    _bypass_env(monkeypatch)
    async with _client(_research_app(), _LAN_PEER) as client:
        res = await client.get("/api/research/active")
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_api_models_admits_bypass_loopback(monkeypatch, tmp_path):
    _bypass_env(monkeypatch)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    import core.database as cdb
    import routes.model_routes as mr

    engine = create_engine(
        f"sqlite:///{tmp_path / 'models.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    monkeypatch.setattr(mr, "SessionLocal", sessionmaker(bind=engine))

    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True)
    app.include_router(mr.setup_model_routes(MagicMock()))

    async with _client(app, _LOOPBACK_PEER) as client:
        res = await client.get("/api/models?background=false")
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_api_models_still_rejects_anonymous_lan(monkeypatch, tmp_path):
    _bypass_env(monkeypatch)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    import core.database as cdb
    import routes.model_routes as mr

    engine = create_engine(
        f"sqlite:///{tmp_path / 'models.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    monkeypatch.setattr(mr, "SessionLocal", sessionmaker(bind=engine))

    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True)
    app.include_router(mr.setup_model_routes(MagicMock()))

    async with _client(app, _LAN_PEER) as client:
        res = await client.get("/api/models?background=false")
    assert res.status_code == 401
