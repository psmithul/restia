from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Base
from routes.life_routes import setup_life_routes
from routes.personal_knowledge_routes import setup_personal_knowledge_routes


ROOT = Path(__file__).resolve().parents[1]


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
def knowledge_routes_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'knowledge-routes.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.state.auth_manager = _IdentityAuthority("alice", "bob")

    @app.middleware("http")
    async def inject_identity(request, call_next):
        request.state.api_token = False
        request.state.current_user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(setup_life_routes(session_factory=factory))
    app.include_router(setup_personal_knowledge_routes(session_factory=factory))
    yield SimpleNamespace(app=app, Session=factory, engine=engine)
    engine.dispose()


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test",
    ) as client:
        return await client.request(method, path, headers=headers, **kwargs)


def test_real_app_registers_personal_knowledge_router_once():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert source.count(
        "from routes.personal_knowledge_routes import setup_personal_knowledge_routes"
    ) == 1
    assert source.count(
        "app.include_router(setup_personal_knowledge_routes())"
    ) == 1


@pytest.mark.asyncio
async def test_personal_knowledge_api_is_owner_scoped_cited_versioned_and_guarded(
    knowledge_routes_env,
):
    env = knowledge_routes_env
    source_response = await _call(
        env,
        "POST",
        "/api/life/knowledge/sources",
        json={
            "source_kind": "lesson",
            "title": "Reviewed lesson",
            "observed_at": "2026-07-17T09:00:00+00:00",
            "source_ref": "lesson:reviewed-control-system",
            "safe_excerpt": "The reviewed bounded evidence.",
            "details": {"capture": "manual"},
        },
    )
    assert source_response.status_code == 201, source_response.text
    source_id = source_response.json()["source"]["id"]

    record_response = await _call(
        env,
        "POST",
        "/api/life/knowledge/records",
        json={
            "memory_kind": "semantic",
            "title": "Control insight",
            "statement": "The plant requires a source-backed damping review.",
            "epistemic_status": "confirmed_fact",
            "claim_origin": "source",
            "reviewed_at": "2026-07-17T09:30:00+00:00",
            "stale_after": "2026-08-17T09:30:00+00:00",
            "citations": [{
                "target_kind": "life_source",
                "target_id": source_id,
                "relation": "supports",
                "locator": "reviewed excerpt",
            }],
            "tags": ["controls", "reviewed"],
            "details": {"scope": "private"},
        },
    )
    assert record_response.status_code == 201, record_response.text
    record = record_response.json()["record"]
    record_id = record["id"]
    assert record["epistemic_status"] == "confirmed_fact"
    assert record["citations"][0]["available"] is True

    bob_get = await _call(
        env, "GET", f"/api/life/knowledge/records/{record_id}", user="bob",
    )
    assert bob_get.status_code == 404
    bob_sources = await _call(
        env, "GET", "/api/life/knowledge/sources", user="bob",
    )
    assert bob_sources.json()["items"] == []

    search = await _call(
        env,
        "GET",
        "/api/life/knowledge/records/search",
        params={"q": "damping", "as_of": "2026-07-18T00:00:00+00:00"},
    )
    assert search.status_code == 200, search.text
    assert search.json()["items"][0]["record"]["id"] == record_id
    evidence = await _call(
        env,
        "GET",
        "/api/life/knowledge/evidence",
        params={"q": "damping", "as_of": "2026-07-18T00:00:00+00:00"},
    )
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["evidence"][0]["record_id"] == record_id
    assert evidence.json()["synthesizes_answer"] is False

    generic_create = await _call(
        env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "source",
            "title": "Bypass",
            "properties": {
                "personal_knowledge_schema_version": 1,
                "memory_kind": "semantic",
            },
        },
    )
    assert generic_create.status_code == 400
    assert "knowledge/records" in generic_create.json()["detail"]
    malformed_generic_create = await _call(
        env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "source",
            "title": "Malformed bypass",
            "properties": {"memory_kind": []},
        },
    )
    assert malformed_generic_create.status_code == 201
    generic_source_create = await _call(
        env,
        "POST",
        "/api/life/sources",
        json={
            "source_type": "knowledge_lesson",
            "title": "Forged source",
            "observed_at": "2026-07-17T09:00:00+00:00",
            "metadata": {"knowledge_source_kind": "lesson"},
        },
    )
    assert generic_source_create.status_code == 400
    assert "knowledge/sources" in generic_source_create.json()["detail"]

    generic_list = await _call(
        env, "GET", "/api/life/entities", params={"entity_type": "source"}
    )
    assert record_id not in {item["id"] for item in generic_list.json()["items"]}
    assert generic_list.json()["typed_personal_knowledge_excluded"] is True
    generic_search = await _call(
        env, "GET", "/api/life/search", params={"q": "damping"}
    )
    assert record_id not in {item["id"] for item in generic_search.json()["items"]}
    generic_get = await _call(
        env, "GET", f"/api/life/entities/{record_id}"
    )
    assert generic_get.status_code == 400
    assert "/api/life/knowledge" in generic_get.json()["detail"]
    generic_sources = await _call(env, "GET", "/api/life/sources")
    assert source_id not in {item["id"] for item in generic_sources.json()["items"]}
    assert generic_sources.json()["typed_personal_knowledge_excluded"] is True

    generic_update = await _call(
        env,
        "PATCH",
        f"/api/life/entities/{record_id}",
        json={"version": 1, "summary": "Bypass"},
    )
    assert generic_update.status_code == 400
    generic_delete = await _call(
        env,
        "DELETE",
        f"/api/life/entities/{record_id}",
        json={"version": 1},
    )
    assert generic_delete.status_code == 400

    update = await _call(
        env,
        "PATCH",
        f"/api/life/knowledge/records/{record_id}",
        json={
            "version": 1,
            "statement": "The reviewed plant still requires damping validation.",
        },
    )
    assert update.status_code == 200, update.text
    assert update.json()["record"]["version"] == 2
    stale_update = await _call(
        env,
        "PATCH",
        f"/api/life/knowledge/records/{record_id}",
        json={"version": 1, "statement": "stale write"},
    )
    assert stale_update.status_code == 409
    history = await _call(
        env,
        "GET",
        f"/api/life/knowledge/records/{record_id}/history",
    )
    assert [row["version"] for row in history.json()["items"]] == [2, 1]

    deleted = await _call(
        env,
        "DELETE",
        f"/api/life/knowledge/records/{record_id}",
        json={"version": 2, "reason": "User removed private knowledge"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["record"]["status"] == "deleted"
    after_delete = await _call(
        env, "GET", f"/api/life/knowledge/records/{record_id}",
    )
    assert after_delete.status_code == 404
