"""Bearer, invitation, and revocation guarantees for linked Projects."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as cdb
from routes import call_routes, link_routes, project_routes
from src.project_storage import ProjectFileStore


class _Auth:
    is_configured = True
    users = {
        "owner": {"is_admin": True},
        "bob": {"is_admin": False},
        "carol": {"is_admin": False},
    }

    @staticmethod
    def is_admin(user):
        return user == "owner"


@pytest.fixture
def federation_env(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(link_routes, "SessionLocal", factory)
    monkeypatch.setattr(project_routes, "SessionLocal", factory)
    monkeypatch.setattr(
        link_routes,
        "_project_remote_limiter",
        link_routes.RateLimiter(max_requests=10_000, window_seconds=60),
    )
    monkeypatch.setattr(
        link_routes,
        "_project_remote_invalid_limiter",
        link_routes.RateLimiter(max_requests=10_000, window_seconds=60),
    )
    monkeypatch.setenv("LINK_HUB_ENABLED", "true")
    yield factory
    engine.dispose()


def _seed(factory, *, handle="remote-one", token="remote-token", status="approved"):
    db = factory()
    try:
        guest = cdb.LinkGuest(
            handle=handle,
            token_hash=link_routes._hash_token(token),
            status=status,
        )
        project = cdb.Project(
            id="11111111-1111-1111-1111-111111111111",
            owner="owner",
            key="REST",
            name="Restia",
            description="Linked workflow",
        )
        db.add_all([guest, project])
        db.flush()
        grant = cdb.ProjectRemoteGrant(
            id="22222222-2222-2222-2222-222222222222",
            project_id=project.id,
            guest_id=guest.id,
            handle_snapshot=guest.handle,
            role="editor",
            status="pending",
            invited_by="owner",
        )
        db.add(grant)
        db.commit()
        return guest.id, project.id, grant.id
    finally:
        db.close()


def _seed_remote_assignment(factory, project_id, grant_id):
    db = factory()
    try:
        grant = db.query(cdb.ProjectRemoteGrant).filter_by(id=grant_id).one()
        grant.status = "active"
        principal = project_routes.remote_grant_principal(grant.id)
        item = cdb.ProjectWorkItem(
            id="33333333-3333-3333-3333-333333333333",
            project_id=project_id,
            stage_id=None,
            item_number=1,
            title="Remote assignment",
            reporter=principal,
            assignee=principal,
            version=4,
        )
        db.add(item)
        db.commit()
        return item.id, principal
    finally:
        db.close()


def _request(*, token=None, project_id=None, user=None, method="GET", path=None):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    request_path = path or (
        f"/api/link/projects/{project_id}"
        if project_id
        else "/api/link/projects/invitations"
    )
    return SimpleNamespace(
        headers=headers,
        method=method,
        url=SimpleNamespace(path=request_path),
        path_params={"project_id": project_id} if project_id else {},
        state=SimpleNamespace(current_user=user, api_token=False),
        client=SimpleNamespace(host="203.0.113.40"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=_Auth())),
        cookies={},
    )


def _run(coro):
    return asyncio.run(coro)


def _route(router, method, path):
    return next(
        route.endpoint
        for route in router.routes
        if route.path == path and method in getattr(route, "methods", set())
    )


def test_remote_project_dependency_requires_approved_bearer_and_active_grant(
    federation_env,
):
    guest_id, project_id, grant_id = _seed(federation_env)

    with pytest.raises(HTTPException) as missing:
        _run(link_routes.require_link_project_remote(_request(project_id=project_id)))
    assert missing.value.status_code == 401

    with pytest.raises(HTTPException) as wrong:
        _run(
            link_routes.require_link_project_remote(
                _request(token="wrong-token", project_id=project_id)
            )
        )
    assert wrong.value.status_code == 401

    with pytest.raises(HTTPException) as pending_grant:
        _run(
            link_routes.require_link_project_remote(
                _request(token="remote-token", project_id=project_id)
            )
        )
    assert pending_grant.value.status_code == 404

    db = federation_env()
    try:
        grant = db.query(cdb.ProjectRemoteGrant).filter_by(id=grant_id).one()
        grant.status = "active"
        db.commit()
    finally:
        db.close()

    request = _request(token="remote-token", project_id=project_id)
    principal = _run(link_routes.require_link_project_remote(request))
    assert principal == f"remote:{grant_id}"
    assert request.state.current_user == principal
    assert request.state.project_remote is True
    assert request.state.project_remote_guest_id == guest_id
    assert request.state.project_remote_grant_id == grant_id

    db = federation_env()
    try:
        db.query(cdb.LinkGuest).filter_by(id=guest_id).one().status = "blocked"
        db.commit()
    finally:
        db.close()
    with pytest.raises(HTTPException) as blocked:
        _run(
            link_routes.require_link_project_remote(
                _request(token="remote-token", project_id=project_id)
            )
        )
    assert blocked.value.status_code == 403
    assert blocked.value.detail == link_routes.PENDING


def test_remote_project_dependency_does_not_accept_another_guests_grant(
    federation_env,
):
    _, project_id, grant_id = _seed(federation_env)
    db = federation_env()
    try:
        db.query(cdb.ProjectRemoteGrant).filter_by(id=grant_id).one().status = "active"
        other = cdb.LinkGuest(
            handle="remote-two",
            token_hash=link_routes._hash_token("other-token"),
            status="approved",
        )
        db.add(other)
        db.commit()
    finally:
        db.close()

    with pytest.raises(HTTPException) as denied:
        _run(
            link_routes.require_link_project_remote(
                _request(token="other-token", project_id=project_id)
            )
        )
    assert denied.value.status_code == 404
    assert denied.value.detail == "Project not found"


def test_remote_project_dependency_rate_limits_authenticated_guest_identity(
    federation_env,
    monkeypatch,
):
    guest_id, project_id, grant_id = _seed(federation_env)
    db = federation_env()
    try:
        db.query(cdb.ProjectRemoteGrant).filter_by(id=grant_id).one().status = "active"
        db.commit()
    finally:
        db.close()
    seen = []
    monkeypatch.setattr(
        link_routes,
        "_project_remote_limiter",
        SimpleNamespace(check=lambda key: seen.append(key) or False),
    )
    with pytest.raises(HTTPException) as limited:
        _run(
            link_routes.require_link_project_remote(
                _request(
                    token="remote-token",
                    project_id=project_id,
                )
            )
        )
    assert limited.value.status_code == 429
    assert seen == [f"guest:{guest_id}"]


def test_remote_project_invalid_limit_ignores_spoofed_forwarded_addresses(
    federation_env,
    monkeypatch,
):
    limiter = link_routes.RateLimiter(max_requests=2, window_seconds=60)
    monkeypatch.setattr(link_routes, "_project_remote_invalid_limiter", limiter)

    statuses = []
    for index in range(3):
        request = _request(
            token="wrong-token",
            project_id="11111111-1111-1111-1111-111111111111",
        )
        request.headers["x-forwarded-for"] = f"198.51.100.{index + 1}"
        request.headers["cf-connecting-ip"] = f"192.0.2.{index + 1}"
        with pytest.raises(HTTPException) as rejected:
            _run(link_routes.require_link_project_remote(request))
        statuses.append(rejected.value.status_code)

    assert statuses == [401, 401, 429]
    assert list(limiter._log) == ["203.0.113.40"]


def test_remote_project_valid_limit_uses_guest_not_forwarded_address(
    federation_env,
    monkeypatch,
):
    guest_id, project_id, grant_id = _seed(federation_env)
    db = federation_env()
    try:
        db.query(cdb.ProjectRemoteGrant).filter_by(id=grant_id).one().status = "active"
        db.commit()
    finally:
        db.close()
    limiter = link_routes.RateLimiter(max_requests=2, window_seconds=60)
    monkeypatch.setattr(link_routes, "_project_remote_limiter", limiter)

    statuses = []
    for index in range(3):
        request = _request(token="remote-token", project_id=project_id)
        request.headers["x-forwarded-for"] = f"198.51.100.{index + 1}"
        request.headers["cf-connecting-ip"] = f"192.0.2.{index + 1}"
        try:
            _run(link_routes.require_link_project_remote(request))
            statuses.append(200)
        except HTTPException as exc:
            statuses.append(exc.status_code)

    assert statuses == [200, 200, 429]
    assert list(limiter._log) == [f"guest:{guest_id}"]


@pytest.mark.asyncio
async def test_invitation_inbox_is_whitelisted_and_response_is_version_cas(
    federation_env,
):
    _, _, grant_id = _seed(federation_env)
    app = FastAPI()
    app.include_router(link_routes.setup_link_project_invitation_routes())
    transport = httpx.ASGITransport(app=app, client=("203.0.113.41", 4321))
    async with httpx.AsyncClient(transport=transport, base_url="http://hub.test") as client:
        headers = {"Authorization": "Bearer remote-token"}
        inbox = await client.get("/api/link/projects/invitations", headers=headers)
        assert inbox.status_code == 200, inbox.text
        invitation = inbox.json()["invitations"][0]
        assert set(invitation) == {
            "id",
            "role",
            "status",
            "version",
            "invited_at",
            "responded_at",
            "project",
        }
        assert set(invitation["project"]) == {
            "id",
            "key",
            "name",
            "description",
            "color",
            "icon",
            "archived",
        }
        assert invitation["status"] == "pending"
        assert "owner" not in invitation["project"]
        assert "invited_by" not in invitation
        assert "handle_snapshot" not in invitation

        accepted = await client.post(
            f"/api/link/projects/invitations/{grant_id}/respond",
            headers=headers,
            json={"action": "accept", "version": invitation["version"]},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["invitation"]["status"] == "active"
        assert accepted.json()["invitation"]["version"] == 2

        stale = await client.post(
            f"/api/link/projects/invitations/{grant_id}/respond",
            headers=headers,
            json={"action": "decline", "version": 1},
        )
        assert stale.status_code == 409

        assert (
            await client.get("/api/link/projects/invitations", headers=headers)
        ).json() == {"invitations": []}


@pytest.mark.asyncio
async def test_invitation_routes_reject_missing_pending_and_wrong_bearers(
    federation_env,
):
    _seed(federation_env, status="pending")
    db = federation_env()
    try:
        db.add(
            cdb.LinkGuest(
                handle="remote-two",
                token_hash=link_routes._hash_token("other-token"),
                status="approved",
            )
        )
        db.commit()
    finally:
        db.close()
    app = FastAPI()
    app.include_router(link_routes.setup_link_project_invitation_routes())
    transport = httpx.ASGITransport(app=app, client=("203.0.113.42", 4321))
    async with httpx.AsyncClient(transport=transport, base_url="http://hub.test") as client:
        assert (await client.get("/api/link/projects/invitations")).status_code == 401
        pending = await client.get(
            "/api/link/projects/invitations",
            headers={"Authorization": "Bearer remote-token"},
        )
        assert pending.status_code == 403
        wrong = await client.post(
            "/api/link/projects/invitations/22222222-2222-2222-2222-222222222222/respond",
            headers={"Authorization": "Bearer other-token"},
            json={"action": "accept", "version": 1},
        )
        assert wrong.status_code == 404


@pytest.mark.asyncio
async def test_remote_project_router_is_bearer_gated_and_closed_to_local_admin_routes(
    federation_env,
    tmp_path,
):
    _, project_id, grant_id = _seed(federation_env)
    db = federation_env()
    try:
        db.query(cdb.ProjectRemoteGrant).filter_by(id=grant_id).one().status = "active"
        db.commit()
    finally:
        db.close()
    app = FastAPI()
    app.state.auth_manager = _Auth()
    app.include_router(
        project_routes.setup_project_routes(
            ProjectFileStore(tmp_path / "project-files"),
            prefix="/api/link/projects",
            remote_only=True,
            dependencies=[Depends(link_routes.require_link_project_remote)],
        )
    )
    transport = httpx.ASGITransport(app=app, client=("203.0.113.43", 4321))
    async with httpx.AsyncClient(transport=transport, base_url="http://hub.test") as client:
        assert (await client.get("/api/link/projects")).status_code == 401
        headers = {"Authorization": "Bearer remote-token"}
        project = await client.get(
            f"/api/link/projects/{project_id}",
            headers=headers,
        )
        assert project.status_code == 200, project.text
        assert project.json()["project"]["role"] == "editor"

        assert (
            await client.post(
                "/api/link/projects",
                headers=headers,
                json={"name": "Remote-owned", "key": "BAD"},
            )
        ).status_code == 405
        assert (
            await client.get("/api/link/projects/templates", headers=headers)
        ).status_code == 404
        assert (
            await client.get(
                f"/api/link/projects/{project_id}/members",
                headers=headers,
            )
        ).status_code == 404


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("home_url", "https://replacement.example"),
        ("token", "replacement-token"),
    ],
)
def test_proxy_snapshot_rejects_replaced_origin_or_bearer(
    federation_env,
    field,
    replacement,
):
    db = federation_env()
    try:
        db.add(
            cdb.HomeLink(
                local_user=link_routes.INSTANCE_LINK_USER,
                home_url="https://pinned.example",
                handle="remote-one",
                owner="owner",
                token="original-token",
            )
        )
        db.commit()
    finally:
        db.close()
    snapshot = link_routes._require_home_project_link_snapshot(_request(user="owner"))
    link_routes._assert_home_project_link_current(snapshot)

    db = federation_env()
    try:
        link = db.query(cdb.HomeLink).one()
        setattr(link, field, replacement)
        db.commit()
    finally:
        db.close()
    with pytest.raises(HTTPException) as stale:
        link_routes._assert_home_project_link_current(snapshot)
    assert stale.value.status_code == 409
    with pytest.raises(HTTPException) as indeterminate:
        link_routes._assert_home_project_link_current(snapshot, mutation=True)
    assert indeterminate.value.status_code == 409
    assert "may have succeeded" in indeterminate.value.detail
    assert "reload linked projects before retrying" in indeterminate.value.detail


def test_guest_purge_revokes_and_detaches_project_grants(federation_env):
    guest_id, project_id, grant_id = _seed(federation_env)
    item_id, principal = _seed_remote_assignment(
        federation_env, project_id, grant_id
    )
    db = federation_env()
    try:
        guest = db.query(cdb.LinkGuest).filter_by(id=guest_id).one()
        link_routes._purge_guest_identity(db, guest)
        db.commit()
        grant = db.query(cdb.ProjectRemoteGrant).filter_by(id=grant_id).one()
        assert grant.status == "revoked"
        assert grant.guest_id is None
        assert grant.version == 2
        assert grant.handle_snapshot == "remote-one"
        assert db.query(cdb.LinkGuest).filter_by(id=guest_id).first() is None
        item = db.query(cdb.ProjectWorkItem).filter_by(id=item_id).one()
        assert item.assignee is None
        assert item.reporter == principal
        assert item.version == 5
    finally:
        db.close()


def test_admin_block_revokes_and_detaches_project_grants(
    federation_env,
    monkeypatch,
):
    guest_id, project_id, grant_id = _seed(federation_env)
    item_id, principal = _seed_remote_assignment(
        federation_env, project_id, grant_id
    )
    monkeypatch.setattr(call_routes, "revoke_federated_credential", lambda _value: None)
    endpoint = _route(
        link_routes.setup_link_hub_routes(),
        "POST",
        "/api/link/admin/guests/{handle}",
    )
    request = _request(user="owner")
    result = _run(
        endpoint(
            "remote-one",
            link_routes.GuestActionRequest(action="block"),
            request,
        )
    )
    assert result["status"] == "blocked"

    db = federation_env()
    try:
        guest = db.query(cdb.LinkGuest).filter_by(id=guest_id).one()
        grant = db.query(cdb.ProjectRemoteGrant).filter_by(id=grant_id).one()
        assert guest.status == "blocked"
        assert grant.status == "revoked"
        assert grant.guest_id is None
        assert grant.version == 2
        item = db.query(cdb.ProjectWorkItem).filter_by(id=item_id).one()
        assert item.assignee is None
        assert item.reporter == principal
        assert item.version == 5
    finally:
        db.close()


def test_owner_contact_block_revokes_and_detaches_project_grants(
    federation_env,
    monkeypatch,
):
    guest_id, project_id, grant_id = _seed(federation_env)
    item_id, principal = _seed_remote_assignment(
        federation_env, project_id, grant_id
    )
    monkeypatch.setattr(call_routes, "revoke_federated_credential", lambda _value: None)
    endpoint = _route(
        link_routes.setup_link_hub_routes(),
        "POST",
        "/api/link/me/block",
    )
    result = _run(
        endpoint(
            link_routes.BlockRequest(handle="remote-one", action="block"),
            _request(user="owner"),
        )
    )
    assert result == {"ok": True, "handle": "remote-one", "blocked": True}

    db = federation_env()
    try:
        grant = db.query(cdb.ProjectRemoteGrant).filter_by(id=grant_id).one()
        assert grant.status == "revoked"
        assert grant.guest_id is None
        assert grant.version == 2
        assert db.query(cdb.LinkGuest).filter_by(id=guest_id).one().status == "approved"
        item = db.query(cdb.ProjectWorkItem).filter_by(id=item_id).one()
        assert item.assignee is None
        assert item.reporter == principal
        assert item.version == 5
    finally:
        db.close()


def test_personal_block_revokes_only_the_blocking_owners_projects(
    federation_env,
):
    guest_id, owner_project_id, owner_grant_id = _seed(federation_env)
    owner_item_id, owner_principal = _seed_remote_assignment(
        federation_env, owner_project_id, owner_grant_id
    )
    db = federation_env()
    try:
        bob_project = cdb.Project(
            id="44444444-4444-4444-4444-444444444444",
            owner="bob",
            key="BOB",
            name="Bob project",
        )
        bob_grant = cdb.ProjectRemoteGrant(
            id="55555555-5555-5555-5555-555555555555",
            project_id=bob_project.id,
            guest_id=guest_id,
            handle_snapshot="remote-one",
            role="editor",
            status="active",
            invited_by="bob",
        )
        bob_principal = project_routes.remote_grant_principal(bob_grant.id)
        bob_item = cdb.ProjectWorkItem(
            id="66666666-6666-6666-6666-666666666666",
            project_id=bob_project.id,
            item_number=1,
            title="Bob remote work",
            reporter=bob_principal,
            assignee=bob_principal,
            version=3,
        )
        db.add_all([bob_project, bob_grant, bob_item])
        db.commit()
        bob_project_id = bob_project.id
        bob_grant_id = bob_grant.id
        bob_item_id = bob_item.id
    finally:
        db.close()

    endpoint = _route(
        link_routes.setup_link_hub_routes(),
        "POST",
        "/api/link/me/block",
    )
    result = _run(endpoint(
        link_routes.BlockRequest(handle="remote-one", action="block"),
        _request(user="bob"),
    ))
    assert result["blocked"] is True

    db = federation_env()
    try:
        owner_grant = db.query(cdb.ProjectRemoteGrant).filter_by(id=owner_grant_id).one()
        bob_grant = db.query(cdb.ProjectRemoteGrant).filter_by(id=bob_grant_id).one()
        assert owner_grant.status == "active"
        assert owner_grant.guest_id == guest_id
        assert bob_grant.status == "revoked"
        assert bob_grant.guest_id is None
        owner_item = db.query(cdb.ProjectWorkItem).filter_by(id=owner_item_id).one()
        bob_item = db.query(cdb.ProjectWorkItem).filter_by(id=bob_item_id).one()
        assert owner_item.assignee == owner_principal
        assert owner_item.version == 4
        assert bob_item.assignee is None
        assert bob_item.version == 4
    finally:
        db.close()

    # The bearer remains usable on a different owner's project, but the
    # blocked owner's project is gone even if a stale grant were restored.
    allowed = _run(link_routes.require_link_project_remote(
        _request(token="remote-token", project_id=owner_project_id)
    ))
    assert allowed == f"remote:{owner_grant_id}"
    db = federation_env()
    try:
        stale = db.query(cdb.ProjectRemoteGrant).filter_by(id=bob_grant_id).one()
        stale.status = "active"
        stale.guest_id = guest_id
        db.commit()
    finally:
        db.close()
    with pytest.raises(HTTPException) as denied:
        _run(link_routes.require_link_project_remote(
            _request(token="remote-token", project_id=bob_project_id)
        ))
    assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_personal_block_hides_pending_invitation_and_prevents_accept(
    federation_env,
):
    _, _, grant_id = _seed(federation_env)
    db = federation_env()
    try:
        db.add(cdb.RemoteBlock(local_user="owner", handle="remote-one"))
        db.commit()
    finally:
        db.close()

    app = FastAPI()
    app.include_router(link_routes.setup_link_project_invitation_routes())
    transport = httpx.ASGITransport(app=app, client=("203.0.113.45", 4321))
    async with httpx.AsyncClient(transport=transport, base_url="http://hub.test") as client:
        headers = {"Authorization": "Bearer remote-token"}
        inbox = await client.get("/api/link/projects/invitations", headers=headers)
        assert inbox.status_code == 200
        assert inbox.json() == {"invitations": []}
        response = await client.post(
            f"/api/link/projects/invitations/{grant_id}/respond",
            headers=headers,
            json={"action": "accept", "version": 1},
        )
        assert response.status_code == 404


def test_app_mounts_exact_bearer_gated_hub_namespace_only():
    root = Path(__file__).resolve().parents[1]
    source = (root / "app.py").read_text("utf-8")
    bootstrap = (root / "src" / "v2" / "bootstrap.py").read_text("utf-8")
    assert '_re.compile(r"^/api/link/projects(?:/.*)?$")' in source
    assert "setup_link_project_invitation_routes()" in source
    assert 'prefix="/api/link/projects"' in bootstrap
    assert 'dependencies=[Depends(ctx.require("require_link_project_remote"))]' in bootstrap
    auth_block = source[source.index("AUTH_EXEMPT_PATTERNS") : source.index(
        "def _is_auth_exempt"
    )]
    assert '_re.compile(r"^/api/homelink/projects' not in auth_block

    invitation_router = link_routes.setup_link_project_invitation_routes()
    for route in invitation_router.routes:
        dependencies = getattr(route, "dependant", SimpleNamespace(dependencies=[])).dependencies
        assert any(
            getattr(dependency.call, "__name__", "") == "require_link_project_remote"
            for dependency in dependencies
        )
