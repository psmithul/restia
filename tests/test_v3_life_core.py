from __future__ import annotations

import ast
import asyncio
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from core.database import (
    Account,
    ActionAudit,
    AuthIdentity,
    Base,
    EntityLink,
    InboxItem,
    LifeEntity,
    LifeSource,
    PlanningItem,
    Project,
)
from routes.inbox_routes import setup_inbox_routes
from src.identity import ensure_account, rename_local_identity, resolve_request_account
from src.life_core import (
    INBOX_KINDS,
    LifeCoreConflict,
    classify_inbox_text,
    create_inbox_item,
)


class _IdentityAuthority:
    """Minimal AuthManager contract used by V3 identity barrier tests."""

    def __init__(self, *usernames: str):
        self._config_lock = threading.Lock()
        self._identity_migrations: set[str] = set()
        self.retired_usernames: set[str] = set()
        self.users = {name: {} for name in usernames}

    @property
    def is_configured(self) -> bool:
        return bool(self.users)


class _StaleFirstQuery:
    """Return one stale miss, then delegate (models a concurrent winner)."""

    def __init__(self, query, owner, model):
        self._query = query
        self._owner = owner
        self._model = model

    def filter(self, *criteria):
        self._query = self._query.filter(*criteria)
        return self

    def first(self):
        seen = self._owner._seen.get(self._model, 0)
        self._owner._seen[self._model] = seen + 1
        if seen == 0:
            return None
        return self._query.first()


class _StaleFirstSession:
    def __init__(self, session, *models):
        self._session = session
        self._models = set(models)
        self._seen = {}

    def query(self, *entities):
        query = self._session.query(*entities)
        model = entities[0] if len(entities) == 1 else None
        if model in self._models:
            return _StaleFirstQuery(query, self, model)
        return query

    def __getattr__(self, name):
        return getattr(self._session, name)


@pytest.fixture()
def life_env(tmp_path):
    db_path = tmp_path / "life-core.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    identity_authority = _IdentityAuthority("alice", "bob")
    app.state.auth_manager = identity_authority

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

    app.include_router(setup_inbox_routes(
        session_factory=factory,
        cursor_signing_key=b"restia-inbox-test-signing-key",
    ))
    return SimpleNamespace(
        app=app,
        Session=factory,
        db_path=db_path,
        auth_manager=identity_authority,
    )


def _request(*, user: str | None = None, api_owner: str | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1234),
        "app": SimpleNamespace(state=SimpleNamespace()),
    }
    request = Request(scope)
    if api_owner:
        request.state.api_token = True
        request.state.api_token_owner = api_owner
        request.state.api_token_scopes = ["todos:read", "todos:write"]
        request.state.current_user = "api"
    else:
        request.state.api_token = False
        request.state.current_user = user
    return request


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, **kwargs)


def test_identity_mapping_is_stable_and_cookie_api_owner_share_account(life_env):
    db = life_env.Session()
    try:
        first = ensure_account(db, "Alice")
        db.commit()
        stable_id = first.id
        assert ensure_account(db, "alice").id == stable_id
        assert resolve_request_account(db, _request(user="alice")).id == stable_id
        assert resolve_request_account(db, _request(api_owner="alice")).id == stable_id
        db.commit()
        assert db.query(Account).count() == 1
        assert db.query(AuthIdentity).count() == 1
    finally:
        db.close()

def test_local_rename_preserves_account_and_external_subjects(life_env):
    db = life_env.Session()
    try:
        account = ensure_account(db, "alice")
        stable_id = account.id
        db.add(AuthIdentity(
            id="external-identity",
            account_id=stable_id,
            provider="oidc",
            issuer="https://issuer.example.test",
            subject="provider-subject-123",
        ))
        db.commit()

        renamed = rename_local_identity(db, "alice", "alice2")
        db.commit()

        assert renamed is not None
        assert renamed.id == stable_id
        assert renamed.username == "alice2"
        assert db.query(AuthIdentity).filter_by(
            provider="local", subject="alice"
        ).count() == 0
        assert db.query(AuthIdentity).filter_by(
            provider="local", subject="alice2", account_id=stable_id
        ).count() == 1
        assert db.query(AuthIdentity).filter_by(
            provider="oidc", subject="provider-subject-123", account_id=stable_id
        ).count() == 1
    finally:
        db.close()


def test_external_identity_is_never_inferred_from_matching_username(life_env):
    db = life_env.Session()
    try:
        local = ensure_account(db, "same@example.com")
        db.commit()
        with pytest.raises(ValueError, match="explicit account linking"):
            ensure_account(
                db,
                "same@example.com",
                provider="oidc",
                subject="same@example.com",
            )
        assert db.query(Account).count() == 1
        assert db.query(AuthIdentity).filter_by(provider="oidc").count() == 0
        assert db.query(Account).one().id == local.id
    finally:
        db.close()


def test_external_identity_resolution_requires_exact_issuer_and_opaque_subject(life_env):
    db = life_env.Session()
    try:
        account = ensure_account(db, "alice")
        db.add(AuthIdentity(
            id="supabase-identity",
            account_id=account.id,
            provider="supabase",
            issuer="https://project-a.supabase.co/auth/v1",
            subject=" CaseSensitiveOpaqueSub ",
            state="active",
        ))
        db.commit()

        resolved = ensure_account(
            db,
            "alice",
            provider="supabase",
            issuer="https://project-a.supabase.co/auth/v1",
            subject=" CaseSensitiveOpaqueSub ",
        )
        assert resolved.id == account.id

        with pytest.raises(ValueError, match="explicit account linking"):
            ensure_account(
                db,
                "alice",
                provider="supabase",
                issuer="https://project-a.supabase.co/auth/v1",
                subject="CaseSensitiveOpaqueSub",
            )
        with pytest.raises(ValueError, match="explicit account linking"):
            ensure_account(
                db,
                "alice",
                provider="supabase",
                issuer="https://project-b.supabase.co/auth/v1",
                subject=" CaseSensitiveOpaqueSub ",
            )
    finally:
        db.close()


def test_external_identity_rejects_unicode_control_subject(life_env):
    db = life_env.Session()
    try:
        with pytest.raises(ValueError, match="explicit account linking"):
            ensure_account(
                db,
                "alice",
                provider="supabase",
                issuer="https://project-a.supabase.co/auth/v1",
                subject="opaque\u0085subject",
            )
    finally:
        db.close()


def test_concurrent_first_touch_identity_conflict_selects_winner(life_env):
    db = life_env.Session()
    try:
        winner = ensure_account(db, "alice")
        db.commit()
        stable_id = winner.id

        # Both initial reads are forced stale; the savepoint insert hits the
        # same unique conflict a concurrent losing request observes.
        raced = ensure_account(
            _StaleFirstSession(db, AuthIdentity, Account), "alice"
        )
        db.commit()
        assert raced.id == stable_id
        assert db.query(Account).count() == 1
        assert db.query(AuthIdentity).count() == 1
    finally:
        db.close()


def test_inbox_deep_link_serves_the_authenticated_spa():
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    tree = ast.parse(app_path.read_text(encoding="utf-8"), filename=str(app_path))
    route = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "serve_inbox"
    )
    assert any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "get"
        and decorator.args
        and isinstance(decorator.args[0], ast.Constant)
        and decorator.args[0].value == "/inbox"
        for decorator in route.decorator_list
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "serve_index"
        for node in ast.walk(route)
    )
    exempt_paths = {
        value.value
        for node in ast.walk(tree)
        if isinstance(node, (ast.Set, ast.List, ast.Tuple))
        for value in node.elts
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    }
    # /inbox is not one of AuthMiddleware's public exact/prefix routes.
    assert "/inbox" not in exempt_paths


@pytest.mark.asyncio
async def test_first_inbox_read_is_empty_without_creating_identity_rows(life_env):
    response = await _call(life_env, "GET", "/api/inbox")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "items": [],
        "count": 0,
        "truncated": False,
        "next_cursor": None,
    }
    malformed = await _call(
        life_env, "GET", "/api/inbox", params={"cursor": "not-a-cursor"}
    )
    assert malformed.status_code == 400

    db = life_env.Session()
    try:
        assert db.query(Account).count() == 0
        assert db.query(AuthIdentity).count() == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_reserved_old_identity_rejects_reads_and_mutations_without_rebinding(
    life_env,
):
    manager = life_env.auth_manager
    with manager._config_lock:
        manager._identity_migrations.update({"alice", "alice2"})

    read = await _call(life_env, "GET", "/api/inbox")
    write = await _call(
        life_env,
        "POST",
        "/api/inbox",
        json={"title": "Must not bind stale Alice"},
    )
    assert read.status_code == 409
    assert write.status_code == 409

    db = life_env.Session()
    try:
        assert db.query(Account).count() == 0
        assert db.query(AuthIdentity).count() == 0
        assert db.query(InboxItem).count() == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_stale_cookie_and_api_owner_fail_after_alice_is_renamed(life_env):
    # Both requests have already been authenticated/attributed as Alice, but
    # the live authority has completed Alice -> Alice2 before either reaches
    # the V3 SQL boundary. Neither stale principal may recreate Alice.
    manager = life_env.auth_manager
    with manager._config_lock:
        manager.users["alice2"] = manager.users.pop("alice")
        manager.retired_usernames.add("alice")

    cookie = await _call(
        life_env,
        "POST",
        "/api/inbox",
        json={"title": "Stale cookie capture"},
    )
    api = await _call(
        life_env,
        "POST",
        "/api/inbox",
        user=None,
        headers={
            "x-api-owner": "alice",
            "x-api-scopes": "todos:write",
        },
        json={"title": "Stale cached API owner capture"},
    )
    assert cookie.status_code == 409
    assert api.status_code == 409

    db = life_env.Session()
    try:
        assert db.query(Account).count() == 0
        assert db.query(AuthIdentity).count() == 0
        assert db.query(InboxItem).count() == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_stale_cookie_fails_after_profile_is_deleted(life_env):
    manager = life_env.auth_manager
    with manager._config_lock:
        manager.users.pop("alice")
        manager.retired_usernames.add("alice")

    read = await _call(life_env, "GET", "/api/inbox")
    write = await _call(
        life_env,
        "POST",
        "/api/inbox",
        json={"title": "Deleted owner must not be rebound"},
    )
    assert read.status_code == 409
    assert write.status_code == 409

    db = life_env.Session()
    try:
        assert db.query(Account).count() == 0
        assert db.query(AuthIdentity).count() == 0
        assert db.query(InboxItem).count() == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_identity_lock_serializes_inbox_commit_before_profile_rename(
    life_env, monkeypatch
):
    commit_entered = threading.Event()
    allow_commit = threading.Event()
    first_commit_lock = threading.Lock()
    first_commit = True
    session_class = life_env.Session.class_
    original_commit = session_class.commit

    def blocking_first_commit(session):
        nonlocal first_commit
        should_block = False
        with first_commit_lock:
            if first_commit:
                first_commit = False
                should_block = True
        if should_block:
            commit_entered.set()
            if not allow_commit.wait(timeout=3):
                raise RuntimeError("test did not release inbox commit")
        return original_commit(session)

    monkeypatch.setattr(session_class, "commit", blocking_first_commit)
    capture_task = asyncio.create_task(
        _call(
            life_env,
            "POST",
            "/api/inbox",
            json={"title": "Commit before rename", "kind": "note"},
        )
    )
    assert await asyncio.to_thread(commit_entered.wait, 2)

    rename_reserved = threading.Event()
    rename_finished = threading.Event()

    def rename_alice():
        manager = life_env.auth_manager
        with manager._config_lock:
            manager._identity_migrations.update({"alice", "alice2"})
            manager.users["alice2"] = manager.users.pop("alice")
            manager.retired_usernames.add("alice")
            rename_reserved.set()
        db = life_env.Session()
        try:
            rename_local_identity(db, "alice", "alice2")
            db.commit()
        finally:
            db.close()
        with manager._config_lock:
            manager._identity_migrations.difference_update({"alice", "alice2"})
        rename_finished.set()

    rename_thread = threading.Thread(target=rename_alice, daemon=True)
    rename_thread.start()
    # The V3 request owns config-lock -> SQL until commit. The rename cannot
    # reserve Alice while that transaction is pending.
    assert not await asyncio.to_thread(rename_reserved.wait, 0.1)
    allow_commit.set()
    response = await asyncio.wait_for(capture_task, timeout=3)
    assert response.status_code == 201, response.text
    assert await asyncio.to_thread(rename_finished.wait, 3)
    rename_thread.join(timeout=1)

    db = life_env.Session()
    try:
        account = db.query(Account).one()
        item = db.query(InboxItem).one()
        assert account.username == "alice2"
        assert item.owner_id == account.id
        assert db.query(AuthIdentity).filter_by(
            provider="local", subject="alice"
        ).count() == 0
        assert db.query(AuthIdentity).filter_by(
            provider="local", subject="alice2", account_id=account.id
        ).count() == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_auth_disabled_single_local_owner_still_reads_and_writes(
    life_env, monkeypatch
):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    manager = life_env.auth_manager
    with manager._config_lock:
        manager.users = {"localadmin": {"is_admin": True}}

    empty = await _call(life_env, "GET", "/api/inbox", user=None)
    assert empty.status_code == 200, empty.text
    created = await _call(
        life_env,
        "POST",
        "/api/inbox",
        user=None,
        json={"title": "Local-only capture", "kind": "note"},
    )
    assert created.status_code == 201, created.text

    db = life_env.Session()
    try:
        assert db.query(Account).one().username == "localadmin"
        assert db.query(InboxItem).count() == 1
    finally:
        db.close()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Remind me to submit the report", "task"),
        ("Meeting tomorrow at 10", "event"),
        ("The sky looked orange this evening", "note"),
        ("Spoke with Rakesh about the trainer", "person_update"),
        ("Robotics project milestone is stable", "project_information"),
        ("Decided to use PID control", "decision"),
        ("Read later https://example.com/paper", "reference_material"),
        ("Paid ₹500 for the bus receipt", "expense"),
        ("Goal: finish the controls course", "goal"),
        ("Every morning stretch routine", "habit"),
        ("Someday build a robot arm", "someday_idea"),
        ("Archive this, no action", "archive"),
    ],
)
def test_full_deterministic_taxonomy(text, expected):
    result = classify_inbox_text(text)
    assert result["kind"] == expected
    assert result["confidence"] > 0
    assert set(INBOX_KINDS) == {
        "task", "event", "note", "person_update", "project_information",
        "decision", "reference_material", "expense", "goal", "habit",
        "someday_idea", "archive",
    }


@pytest.mark.asyncio
async def test_inbox_crud_is_owner_scoped_versioned_and_idempotent(life_env):
    payload = {
        "title": "Submit report",
        "content": "Remind me to submit the report",
        "idempotency_key": "capture-1",
    }
    created = await _call(life_env, "POST", "/api/inbox", json=payload)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["created"] is True
    item_id = body["item"]["id"]
    assert body["item"]["kind"] == "task"
    assert body["item"]["version"] == 1

    duplicate = await _call(life_env, "POST", "/api/inbox", json=payload)
    assert duplicate.status_code == 201
    assert duplicate.json()["created"] is False
    assert duplicate.json()["item"]["id"] == item_id

    alice = await _call(life_env, "GET", "/api/inbox")
    bob = await _call(life_env, "GET", "/api/inbox", user="bob")
    assert [row["id"] for row in alice.json()["items"]] == [item_id]
    assert bob.json()["items"] == []
    assert (await _call(life_env, "GET", f"/api/inbox/{item_id}", user="bob")).status_code == 404

    updated = await _call(
        life_env,
        "PATCH",
        f"/api/inbox/{item_id}",
        json={"version": 1, "title": "Submit final report"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["item"]["version"] == 2
    assert updated.json()["item"]["title"] == "Submit final report"
    stale = await _call(
        life_env,
        "PATCH",
        f"/api/inbox/{item_id}",
        json={"version": 1, "title": "Stale overwrite"},
    )
    assert stale.status_code == 409

    db = life_env.Session()
    try:
        assert db.query(InboxItem).count() == 1
        assert [row.action for row in db.query(ActionAudit).order_by(ActionAudit.created_at)] == [
            "inbox.created", "inbox.updated"
        ]
    finally:
        db.close()


def test_concurrent_idempotency_conflict_selects_capture_winner(life_env):
    db = life_env.Session()
    try:
        account = ensure_account(db, "alice")
        winner, created = create_inbox_item(
            db,
            account=account,
            title="First capture",
            kind="note",
            idempotency_key="same-request",
        )
        db.commit()
        assert created is True

        raced, created = create_inbox_item(
            _StaleFirstSession(db, InboxItem),
            account=account,
            title="First capture",
            kind="note",
            idempotency_key="same-request",
        )
        db.commit()
        assert created is False
        assert raced.id == winner.id
        assert db.query(InboxItem).count() == 1
        assert db.query(ActionAudit).filter_by(action="inbox.created").count() == 1
        with pytest.raises(LifeCoreConflict, match="different capture"):
            create_inbox_item(
                db,
                account=account,
                title="Mismatched retry",
                kind="note",
                idempotency_key="same-request",
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_api_owner_matches_cookie_owner_and_scopes_are_enforced(life_env):
    created = await _call(
        life_env,
        "POST",
        "/api/inbox",
        user=None,
        headers={
            "x-api-owner": "alice",
            "x-api-scopes": "todos:write",
        },
        json={"title": "Captured from API", "kind": "note"},
    )
    assert created.status_code == 201, created.text
    item_id = created.json()["item"]["id"]

    cookie_view = await _call(life_env, "GET", "/api/inbox", user="alice")
    assert [item["id"] for item in cookie_view.json()["items"]] == [item_id]

    denied_read = await _call(
        life_env,
        "GET",
        "/api/inbox",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "todos:write"},
    )
    assert denied_read.status_code == 403
    denied_write = await _call(
        life_env,
        "POST",
        "/api/inbox",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "todos:read"},
        json={"title": "Must not be created"},
    )
    assert denied_write.status_code == 403


@pytest.mark.asyncio
async def test_classify_and_life_graph_processing_are_explicit(life_env):
    created = await _call(
        life_env,
        "POST",
        "/api/inbox",
        json={"title": "Paid ₹500", "kind": "note"},
    )
    item = created.json()["item"]
    classified = await _call(
        life_env,
        "POST",
        f"/api/inbox/{item['id']}/classify",
        json={"version": item["version"]},
    )
    assert classified.status_code == 200
    item = classified.json()["item"]
    assert item["kind"] == "expense"
    processed = await _call(
        life_env,
        "POST",
        f"/api/inbox/{item['id']}/process",
        json={"version": item["version"]},
    )
    assert processed.status_code == 200, processed.text
    result = processed.json()["item"]
    assert result["status"] == "processed"
    assert result["processed_target_type"] == "life_entity"
    current = (await _call(life_env, "GET", f"/api/inbox/{item['id']}")).json()["item"]
    assert current["status"] == "processed"
    assert current["version"] == item["version"] + 1

    db = life_env.Session()
    try:
        entity = db.query(LifeEntity).one()
        assert entity.id == result["processed_target_id"]
        assert entity.entity_type == "transaction"
        assert entity.owner_id == db.query(Account).filter_by(username="alice").one().id
        assert db.query(LifeSource).count() == 1
    finally:
        db.close()


@pytest.mark.parametrize(
    ("kind", "entity_type", "entity_status"),
    [
        ("event", "event", "active"),
        ("note", "note", "active"),
        ("person_update", "interaction", "active"),
        ("decision", "decision", "active"),
        ("reference_material", "source", "active"),
        ("expense", "transaction", "active"),
        ("goal", "goal", "active"),
        ("habit", "habit", "active"),
        ("someday_idea", "note", "someday"),
    ],
)
@pytest.mark.asyncio
async def test_every_non_project_inbox_kind_has_a_source_backed_adapter(
    life_env, kind, entity_type, entity_status
):
    created = await _call(
        life_env,
        "POST",
        "/api/inbox",
        json={
            "title": f"Capture {kind}",
            "content": f"Private content for {kind}",
            "kind": kind,
            "metadata": {"origin": "test"},
        },
    )
    assert created.status_code == 201, created.text
    item = created.json()["item"]
    processed = await _call(
        life_env,
        "POST",
        f"/api/inbox/{item['id']}/process",
        json={"version": item["version"]},
    )
    assert processed.status_code == 200, processed.text
    result = processed.json()["item"]
    assert result["processed_target_type"] == "life_entity"

    db = life_env.Session()
    try:
        entity = db.query(LifeEntity).filter_by(id=result["processed_target_id"]).one()
        source = db.query(LifeSource).one()
        assert entity.entity_type == entity_type
        assert entity.status == entity_status
        assert entity.provenance == {"source_id": source.id}
        assert entity.properties["inbox_item_id"] == item["id"]
        assert entity.properties["origin"] == "test"
        assert db.query(EntityLink).filter_by(
            source_type="inbox_item",
            source_id=item["id"],
            relation="represents",
            target_type="life_entity",
            target_id=entity.id,
        ).count() == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_task_processing_reuses_planning_and_is_retry_idempotent(life_env):
    created = await _call(
        life_env,
        "POST",
        "/api/inbox",
        json={"title": "Finish controller", "kind": "task", "content": "Implement the heading controller"},
    )
    item = created.json()["item"]
    processed = await _call(
        life_env,
        "POST",
        f"/api/inbox/{item['id']}/process",
        json={"version": 1},
    )
    assert processed.status_code == 200, processed.text
    result = processed.json()["item"]
    assert result["status"] == "processed"
    assert result["processed_target_type"] == "planning_item"
    assert result["version"] == 2

    retry = await _call(
        life_env,
        "POST",
        f"/api/inbox/{item['id']}/process",
        json={"version": 1},
    )
    assert retry.status_code == 200
    assert retry.json()["item"]["processed_target_id"] == result["processed_target_id"]

    db = life_env.Session()
    try:
        planning = db.query(PlanningItem).one()
        assert planning.id == result["processed_target_id"]
        assert planning.owner == "alice"
        assert planning.source == "inbox"
        task_entity = db.query(LifeEntity).filter_by(entity_type="task").one()
        assert task_entity.domain_ref_type == "planning_item"
        assert task_entity.domain_ref_id == planning.id
        assert {
            "definition_of_done", "priority", "deadline", "effort_minutes",
            "energy", "context", "project_id", "people_ids", "dependency_ids",
            "document_ids", "source", "next_action",
            "completion_evidence",
        } <= set(task_entity.properties)
        assert task_entity.status == "open"
        assert db.query(PlanningItem).count() == 1
        assert db.query(LifeSource).count() == 1
        assert db.query(EntityLink).count() == 2
        actions = [row.action for row in db.query(ActionAudit).all()]
        assert actions.count("inbox.processed") == 1
        assert actions.count("entity.linked") == 2
    finally:
        db.close()

    with sqlite3.connect(life_env.db_path) as raw_db:
        raw_title, raw_details = raw_db.execute(
            "SELECT title, details FROM planning_items"
        ).fetchone()
    assert raw_title.startswith("enc:")
    assert raw_details.startswith("enc:")
    assert "Finish controller" not in raw_title
    assert "Implement the heading controller" not in raw_details


@pytest.mark.asyncio
async def test_planning_item_round_trips_literal_encryption_prefix(life_env):
    foreign_fernet = Fernet(Fernet.generate_key())
    literal = "enc:" + foreign_fernet.encrypt(
        b"literal token-looking planning title"
    ).decode("ascii")
    created = await _call(
        life_env,
        "POST",
        "/api/inbox",
        json={"title": literal, "kind": "task", "content": "Keep the prefix"},
    )
    item = created.json()["item"]
    processed = await _call(
        life_env,
        "POST",
        f"/api/inbox/{item['id']}/process",
        json={"version": 1},
    )
    assert processed.status_code == 200, processed.text

    db = life_env.Session()
    try:
        planning = db.query(PlanningItem).one()
        assert planning.title == literal
    finally:
        db.close()

    with sqlite3.connect(life_env.db_path) as raw_db:
        raw_title = raw_db.execute(
            "SELECT title FROM planning_items"
        ).fetchone()[0]
    assert raw_title.startswith("enc:")
    assert raw_title != literal
    assert literal not in raw_title


@pytest.mark.asyncio
async def test_archive_and_project_link_processing(life_env):
    archived = await _call(
        life_env, "POST", "/api/inbox", json={"title": "Ignore", "kind": "archive"}
    )
    archive_item = archived.json()["item"]
    first = await _call(
        life_env,
        "POST",
        f"/api/inbox/{archive_item['id']}/archive",
        json={"version": 1},
    )
    assert first.json()["item"]["status"] == "archived"
    retry = await _call(
        life_env,
        "POST",
        f"/api/inbox/{archive_item['id']}/archive",
        json={"version": 1},
    )
    assert retry.status_code == 200

    db = life_env.Session()
    try:
        db.add(Project(
            id="project-1", owner="alice", key="LIFE", name="Life OS",
            description="", template="general", color="#5b8abf",
        ))
        db.commit()
    finally:
        db.close()
    project_capture = await _call(
        life_env,
        "POST",
        "/api/inbox",
        json={"title": "Life OS project milestone", "kind": "project_information"},
    )
    item = project_capture.json()["item"]
    linked = await _call(
        life_env,
        "POST",
        f"/api/inbox/{item['id']}/process",
        json={"version": 1, "project_id": "project-1"},
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["item"]["processed_target_type"] == "project"
    db = life_env.Session()
    try:
        project_entity = db.query(LifeEntity).filter_by(
            entity_type="project", domain_ref_type="project", domain_ref_id="project-1"
        ).one()
        information = db.query(LifeEntity).filter_by(entity_type="note").one()
        assert db.query(LifeSource).count() == 1
        assert db.query(EntityLink).filter_by(
            source_type="life_entity",
            source_id=information.id,
            relation="about",
            target_type="life_entity",
            target_id=project_entity.id,
        ).count() == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_direct_get_is_not_limited_to_latest_hundred(life_env):
    db = life_env.Session()
    try:
        account = ensure_account(db, "alice")
        oldest = None
        for index in range(105):
            row, _ = create_inbox_item(
                db, account=account, title=f"Capture {index}", kind="note"
            )
            oldest = oldest or row.id
        db.commit()
    finally:
        db.close()
    response = await _call(life_env, "GET", f"/api/inbox/{oldest}")
    assert response.status_code == 200
    assert response.json()["item"]["id"] == oldest


@pytest.mark.asyncio
async def test_metadata_is_bounded_and_capture_content_is_encrypted_at_rest(life_env):
    secret_title = "Private health result"
    secret_content = "Blood pressure 120 over 80"
    secret_ref = "message://private-thread"
    response = await _call(
        life_env,
        "POST",
        "/api/inbox",
        json={
            "title": secret_title,
            "content": secret_content,
            "source_ref": secret_ref,
            "metadata": {"private": "finance note"},
        },
    )
    assert response.status_code == 201, response.text
    with sqlite3.connect(life_env.db_path) as raw:
        stored = raw.execute(
            "SELECT title, content, source_ref, metadata FROM inbox_items"
        ).fetchone()
    rendered = " ".join(str(value) for value in stored)
    assert secret_title not in rendered
    assert secret_content not in rendered
    assert secret_ref not in rendered
    assert "finance note" not in rendered

    oversized = await _call(
        life_env,
        "POST",
        "/api/inbox",
        json={"title": "Too large", "metadata": {"blob": "x" * (33 * 1024)}},
    )
    assert oversized.status_code == 400


def test_action_audit_rows_cannot_be_updated(life_env):
    db = life_env.Session()
    try:
        account = ensure_account(db, "alice")
        create_inbox_item(db, account=account, title="Immutable audit", kind="note")
        db.commit()
        audit = db.query(ActionAudit).one()
        audit.action = "tampered"
        with pytest.raises(RuntimeError, match="append-only"):
            db.commit()
        db.rollback()
    finally:
        db.close()


def test_action_audit_sqlite_guards_block_bulk_update_and_delete(life_env):
    db = life_env.Session()
    try:
        account = ensure_account(db, "alice")
        create_inbox_item(db, account=account, title="Durable audit", kind="note")
        db.commit()
        audit_id = db.query(ActionAudit.id).scalar()
        with pytest.raises(Exception, match="append-only"):
            db.query(ActionAudit).filter(ActionAudit.id == audit_id).update(
                {ActionAudit.action: "tampered"}, synchronize_session=False
            )
        db.rollback()
        with pytest.raises(Exception, match="append-only"):
            db.query(ActionAudit).filter(ActionAudit.id == audit_id).delete(
                synchronize_session=False
            )
        db.rollback()
        assert db.query(ActionAudit).filter(ActionAudit.id == audit_id).count() == 1

        account = db.query(Account).filter_by(username="alice").one()
        db.delete(account)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        assert db.query(Account).filter_by(username="alice").count() == 1
        assert db.query(ActionAudit).filter(ActionAudit.id == audit_id).count() == 1
    finally:
        db.close()

    with sqlite3.connect(life_env.db_path) as raw:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(
                "UPDATE action_audit SET action = 'raw-tamper' WHERE id = ?",
                (audit_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM action_audit WHERE id = ?", (audit_id,))


def _seed_equal_timestamp_inbox_rows(
    env, *, username: str, prefix: str, count: int
) -> list[str]:
    timestamp = datetime(2026, 7, 16, 12, 30, 45, 123456)
    ids = [f"{prefix}-{index:04d}" for index in range(count)]
    db = env.Session()
    try:
        account = ensure_account(db, username)
        for index, item_id in enumerate(ids):
            db.add(InboxItem(
                id=item_id,
                owner_id=account.id,
                title=f"Capture {index}",
                content=f"Equal timestamp capture {index}",
                kind="note",
                status="inbox",
                source_type="user",
                meta_data={},
                classification_confidence=60,
                classification_reason="seeded pagination fixture",
                version=1,
                created_at=timestamp,
                updated_at=timestamp,
            ))
        db.commit()
    finally:
        db.close()
    return ids


@pytest.mark.asyncio
async def test_inbox_keyset_pagination_discovers_all_equal_timestamp_rows(life_env):
    seeded = _seed_equal_timestamp_inbox_rows(
        life_env, username="alice", prefix="alice", count=207
    )

    response = await _call(life_env, "GET", "/api/inbox", params={"limit": 100})
    assert response.status_code == 200, response.text
    seen: list[str] = []
    page_sizes: list[int] = []
    while True:
        body = response.json()
        page_ids = [row["id"] for row in body["items"]]
        page_sizes.append(body["count"])
        assert body["count"] == len(page_ids)
        assert body["truncated"] is bool(body["next_cursor"])
        assert not set(seen).intersection(page_ids)
        seen.extend(page_ids)
        if not body["next_cursor"]:
            break
        response = await _call(
            life_env,
            "GET",
            "/api/inbox",
            params={"limit": 100, "cursor": body["next_cursor"]},
        )
        assert response.status_code == 200, response.text

    assert page_sizes == [100, 100, 7]
    assert seen == sorted(seeded, reverse=True)
    assert len(seen) == len(set(seen)) == 207


@pytest.mark.asyncio
async def test_inbox_cursor_rejects_malformed_tampered_and_wrong_owner_use(life_env):
    _seed_equal_timestamp_inbox_rows(
        life_env, username="alice", prefix="alice-private", count=3
    )
    _seed_equal_timestamp_inbox_rows(
        life_env, username="bob", prefix="bob-private", count=2
    )
    first = await _call(life_env, "GET", "/api/inbox", params={"limit": 1})
    cursor = first.json()["next_cursor"]
    assert cursor

    malformed = await _call(
        life_env, "GET", "/api/inbox", params={"cursor": "not-a-cursor"}
    )
    assert malformed.status_code == 400
    assert malformed.json()["detail"] == "Invalid inbox cursor"
    oversized = await _call(
        life_env, "GET", "/api/inbox", params={"cursor": "x" * 1025}
    )
    assert oversized.status_code == 400
    assert oversized.json()["detail"] == "Invalid inbox cursor"

    midpoint = len(cursor) // 2
    replacement = "A" if cursor[midpoint] != "A" else "B"
    tampered_cursor = cursor[:midpoint] + replacement + cursor[midpoint + 1:]
    tampered = await _call(
        life_env, "GET", "/api/inbox", params={"cursor": tampered_cursor}
    )
    assert tampered.status_code == 400
    assert tampered.json()["detail"] == "Invalid inbox cursor"

    wrong_owner = await _call(
        life_env,
        "GET",
        "/api/inbox",
        user="bob",
        params={"cursor": cursor},
    )
    assert wrong_owner.status_code == 400
    assert wrong_owner.json()["detail"] == "Invalid inbox cursor"
    bob_normal = await _call(life_env, "GET", "/api/inbox", user="bob")
    assert {row["id"] for row in bob_normal.json()["items"]} == {
        "bob-private-0000", "bob-private-0001"
    }

    wrong_filter = await _call(
        life_env,
        "GET",
        "/api/inbox",
        params={"status": "all", "cursor": cursor},
    )
    assert wrong_filter.status_code == 400
    assert (await _call(
        life_env, "GET", "/api/inbox", params={"limit": 101}
    )).status_code == 422
