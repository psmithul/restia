"""End-to-end contracts for adding Restia installations to a project."""

from __future__ import annotations

import sqlite3

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from routes import link_routes, project_routes
from src.public_origin import canonical_shared_origin, is_loopback_origin
from src.project_storage import ProjectFileStore


class _Identity:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            user = dict(scope.get("headers") or []).get(b"x-test-user")
            if user:
                scope.setdefault("state", {})["current_user"] = user.decode()
        await self.app(scope, receive, send)


class _Auth:
    is_configured = True
    users = {
        "alice": {"is_admin": True},
        "bob": {"is_admin": False},
    }

    @staticmethod
    def is_admin(user):
        return user == "alice"


@pytest.fixture
def pairing_env(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pairing.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(project_routes, "SessionLocal", factory)
    monkeypatch.setattr(link_routes, "SessionLocal", factory)
    monkeypatch.setattr(
        project_routes,
        "get_setting",
        lambda key, default="", owner=None: "https://hub.example"
        if key == "app_public_url"
        else default,
    )
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
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("LINK_HUB_ENABLED", "true")

    app = FastAPI()
    app.state.auth_manager = _Auth()
    app.include_router(
        project_routes.setup_project_routes(
            ProjectFileStore(tmp_path / "project-files")
        )
    )
    app.include_router(link_routes.setup_link_hub_routes())
    app.include_router(link_routes.setup_link_project_invitation_routes())
    yield _Identity(app), factory
    engine.dispose()


def _headers(user: str) -> dict[str, str]:
    return {"x-test-user": user}


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def _create_project(client, user="alice", key="PAIR"):
    response = await client.post(
        "/api/projects",
        headers=_headers(user),
        json={"name": f"{user.title()} project", "key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()["project"]


def test_existing_link_invite_table_migrates_project_pairing_columns(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE link_guests (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE link_invites ("
            "id INTEGER PRIMARY KEY, code_hash TEXT, created_by TEXT, "
            "max_uses INTEGER, uses INTEGER, revoked BOOLEAN)"
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{path}")
    cdb._migrate_add_link_invite_columns()
    cdb._migrate_add_link_invite_columns()

    conn = sqlite3.connect(path)
    try:
        guest_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(link_guests)")
        }
        invite_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(link_invites)")
        }
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(link_invites)")
        }
    finally:
        conn.close()
    assert {"invite_id", "pubkey"} <= guest_columns
    assert {"project_id", "project_role", "hub_url"} <= invite_columns
    assert "ix_link_invites_project_id" in indexes


def test_shared_origin_is_https_except_for_explicit_loopback():
    assert canonical_shared_origin("https://PAIR.Example:443/") == "https://pair.example"
    assert canonical_shared_origin("http://127.0.0.1:7000") == "http://127.0.0.1:7000"
    assert canonical_shared_origin("http://localhost:7000") == "http://localhost:7000"
    assert is_loopback_origin("http://127.0.0.1:7000") is True
    for unsafe in (
        "http://192.168.1.10:7000",
        "https://user:pass@example.com",
        "https://example.com/path",
        "https://example.com?code=secret",
        "javascript:alert(1)",
    ):
        assert canonical_shared_origin(unsafe) == ""


async def test_project_pairing_is_hash_only_atomic_and_acceptance_ready(pairing_env):
    app, factory = pairing_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://hub.example",
    ) as client:
        project = await _create_project(client)
        project_id = project["id"]

        created = await client.post(
            f"/api/projects/{project_id}/pairing-invitations",
            headers=_headers("alice"),
            json={"role": "editor"},
        )
        assert created.status_code == 201, created.text
        pairing = created.json()["pairing"]
        assert pairing["status"] == "waiting"
        assert pairing["role"] == "editor"
        assert pairing["hub_url"] == "https://hub.example"
        assert len(pairing["code"]) > 20

        db = factory()
        try:
            stored = db.query(cdb.LinkInvite).filter_by(id=pairing["id"]).one()
            assert stored.code_hash == link_routes._hash_code(pairing["code"])
            assert stored.code_hash != pairing["code"]
            assert stored.project_id == project_id
            assert stored.project_role == "editor"
            assert stored.max_uses == 1
            assert (stored.expires_at - stored.created_at).total_seconds() <= 30 * 60
        finally:
            db.close()

        status = await client.get(
            f"/api/projects/{project_id}/pairing-invitations/{pairing['id']}",
            headers=_headers("alice"),
        )
        assert status.status_code == 200
        assert "code" not in status.json()["pairing"]

        redeemed = await client.post(
            "/api/link/redeem",
            json={"handle": "robot-lab", "code": pairing["code"]},
        )
        assert redeemed.status_code == 200, redeemed.text
        token = redeemed.json()["token"]

        paired = await client.get(
            f"/api/projects/{project_id}/pairing-invitations/{pairing['id']}",
            headers=_headers("alice"),
        )
        assert paired.status_code == 200
        paired_body = paired.json()["pairing"]
        assert paired_body["status"] == "paired"
        assert paired_body["handle"] == "robot-lab"
        assert paired_body["grant"] is None

        reloaded = await client.get(
            f"/api/projects/{project_id}/linked-instances",
            headers=_headers("alice"),
        )
        assert reloaded.status_code == 200
        assert reloaded.json()["pairing"]["id"] == pairing["id"]
        assert reloaded.json()["pairing"]["status"] == "paired"
        assert reloaded.json()["pairing"]["handle"] == "robot-lab"
        assert "code" not in reloaded.json()["pairing"]

        # A Home Link code establishes installation trust only. Possession of
        # it cannot grant project access until the owner confirms the exact
        # newly linked handle through the ordinary project invitation route.
        empty_inbox = await client.get(
            "/api/link/projects/invitations", headers=_bearer(token)
        )
        assert empty_inbox.status_code == 200
        assert empty_inbox.json()["invitations"] == []

        invited = await client.post(
            f"/api/projects/{project_id}/remote-invitations",
            headers=_headers("alice"),
            json={
                "handle": "robot-lab",
                "role": "editor",
                "pairing_invite_id": pairing["id"],
            },
        )
        assert invited.status_code == 201, invited.text

        reused = await client.post(
            "/api/link/redeem",
            json={"handle": "second-lab", "code": pairing["code"]},
        )
        assert reused.status_code == 403
        assert reused.json()["detail"] == "Invalid or expired invite code"

        inbox = await client.get(
            "/api/link/projects/invitations", headers=_bearer(token)
        )
        assert inbox.status_code == 200, inbox.text
        invitation = inbox.json()["invitations"][0]
        assert invitation["project"]["id"] == project_id
        assert invitation["role"] == "editor"

        accepted = await client.post(
            f"/api/link/projects/invitations/{invitation['id']}/respond",
            headers=_bearer(token),
            json={"action": "accept", "version": invitation["version"]},
        )
        assert accepted.status_code == 200, accepted.text

        active = await client.get(
            f"/api/projects/{project_id}/pairing-invitations/{pairing['id']}",
            headers=_headers("alice"),
        )
        assert active.json()["pairing"]["status"] == "active"


async def test_pairing_and_instance_discovery_fail_closed_for_local_non_admins(
    pairing_env,
):
    app, factory = pairing_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://hub.example",
    ) as client:
        admin_project = await _create_project(client)
        project_id = admin_project["id"]
        await client.post(
            f"/api/projects/{project_id}/members",
            headers=_headers("alice"),
            json={"username": "bob", "role": "editor"},
        )

        non_owner_list = await client.get(
            f"/api/projects/{project_id}/linked-instances",
            headers=_headers("bob"),
        )
        assert non_owner_list.status_code == 403
        non_owner_pair = await client.post(
            f"/api/projects/{project_id}/pairing-invitations",
            headers=_headers("bob"),
            json={"role": "viewer"},
        )
        assert non_owner_pair.status_code == 403

        bob_project = await _create_project(client, user="bob", key="BOB")
        non_admin_list = await client.get(
            f"/api/projects/{bob_project['id']}/linked-instances",
            headers=_headers("bob"),
        )
        assert non_admin_list.status_code == 200
        assert non_admin_list.json()["can_create_pairing"] is False
        non_admin_pair = await client.post(
            f"/api/projects/{bob_project['id']}/pairing-invitations",
            headers=_headers("bob"),
            json={"role": "viewer"},
        )
        assert non_admin_pair.status_code == 403

        db = factory()
        try:
            db.add(
                cdb.LinkGuest(
                    handle="existing-lab",
                    token_hash="e" * 64,
                    status="approved",
                )
            )
            db.commit()
        finally:
            db.close()
        invited = await client.post(
            f"/api/projects/{bob_project['id']}/remote-invitations",
            headers=_headers("bob"),
            json={"handle": "existing-lab", "role": "viewer"},
        )
        assert invited.status_code == 201, invited.text
        grant = invited.json()["grant"]
        updated = await client.patch(
            f"/api/projects/{bob_project['id']}/remote-grants/{grant['id']}",
            headers=_headers("bob"),
            json={"role": "editor", "version": grant["version"]},
        )
        assert updated.status_code == 200, updated.text
        removed = await client.delete(
            f"/api/projects/{bob_project['id']}/remote-grants/{grant['id']}",
            headers=_headers("bob"),
            params={"version": updated.json()["grant"]["version"]},
        )
        assert removed.status_code == 200, removed.text


async def test_two_restia_installations_pair_independently_and_unused_code_revokes(
    pairing_env,
):
    app, _ = pairing_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://hub.example",
    ) as client:
        project = await _create_project(client)
        project_id = project["id"]
        pairings = []
        for role, handle in (("viewer", "design-lab"), ("editor", "build-lab")):
            response = await client.post(
                f"/api/projects/{project_id}/pairing-invitations",
                headers=_headers("alice"),
                json={"role": role},
            )
            assert response.status_code == 201
            pairing = response.json()["pairing"]
            pairings.append(pairing)
            redeemed = await client.post(
                "/api/link/redeem",
                json={"handle": handle, "code": pairing["code"]},
            )
            assert redeemed.status_code == 200

        linked = await client.get(
            f"/api/projects/{project_id}/linked-instances",
            headers=_headers("alice"),
        )
        assert linked.status_code == 200
        assert linked.json()["handles"] == ["build-lab", "design-lab"]
        assert {
            (row["id"], row["handle"], row["status"])
            for row in linked.json()["pairings"]
        } == {
            (pairings[0]["id"], "design-lab", "paired"),
            (pairings[1]["id"], "build-lab", "paired"),
        }

        unused = await client.post(
            f"/api/projects/{project_id}/pairing-invitations",
            headers=_headers("alice"),
            json={"role": "viewer"},
        )
        unused_pairing = unused.json()["pairing"]
        recovered = await client.get(
            f"/api/projects/{project_id}/linked-instances",
            headers=_headers("alice"),
        )
        recovered_pairing = recovered.json()["pairing"]
        assert recovered_pairing["id"] == unused_pairing["id"]
        assert recovered_pairing["status"] == "waiting"
        assert "code" not in recovered_pairing
        duplicate = await client.post(
            f"/api/projects/{project_id}/pairing-invitations",
            headers=_headers("alice"),
            json={"role": "editor"},
        )
        assert duplicate.status_code == 409
        assert "revoke" in duplicate.json()["detail"].lower()
        revoked = await client.delete(
            f"/api/projects/{project_id}/pairing-invitations/{unused_pairing['id']}",
            headers=_headers("alice"),
        )
        assert revoked.status_code == 200
        assert revoked.json()["pairing"]["status"] == "revoked"
        denied = await client.post(
            "/api/link/redeem",
            json={"handle": "late-lab", "code": unused_pairing["code"]},
        )
        assert denied.status_code == 403


async def test_project_lifecycle_revokes_every_unused_pairing_code(pairing_env):
    app, _ = pairing_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://hub.example",
    ) as client:
        archived = await _create_project(client, key="ARC")
        archived_pairing = (
            await client.post(
                f"/api/projects/{archived['id']}/pairing-invitations",
                headers=_headers("alice"),
                json={"role": "viewer"},
            )
        ).json()["pairing"]
        response = await client.post(
            f"/api/projects/{archived['id']}/archive",
            headers=_headers("alice"),
            json={"version": archived["version"]},
        )
        assert response.status_code == 200, response.text
        assert (
            await client.post(
                "/api/link/redeem",
                json={"handle": "archive-lab", "code": archived_pairing["code"]},
            )
        ).status_code == 403

        deleted = await _create_project(client, key="DEL")
        deleted_pairing = (
            await client.post(
                f"/api/projects/{deleted['id']}/pairing-invitations",
                headers=_headers("alice"),
                json={"role": "viewer"},
            )
        ).json()["pairing"]
        response = await client.delete(
            f"/api/projects/{deleted['id']}",
            headers=_headers("alice"),
            params={"confirm_key": "DEL"},
        )
        assert response.status_code == 200, response.text
        assert (
            await client.post(
                "/api/link/redeem",
                json={"handle": "delete-lab", "code": deleted_pairing["code"]},
            )
        ).status_code == 403

        transferred = await _create_project(client, key="MOVE")
        transferred_pairing = (
            await client.post(
                f"/api/projects/{transferred['id']}/pairing-invitations",
                headers=_headers("alice"),
                json={"role": "editor"},
            )
        ).json()["pairing"]
        member = await client.post(
            f"/api/projects/{transferred['id']}/members",
            headers=_headers("alice"),
            json={"username": "bob", "role": "editor"},
        )
        assert member.status_code == 201, member.text
        response = await client.post(
            f"/api/projects/{transferred['id']}/transfer",
            headers=_headers("alice"),
            json={"username": "bob", "version": transferred["version"]},
        )
        assert response.status_code == 200, response.text
        assert (
            await client.post(
                "/api/link/redeem",
                json={"handle": "transfer-lab", "code": transferred_pairing["code"]},
            )
        ).status_code == 403


async def test_pairing_confirmation_is_bound_to_exact_redeemed_invite(pairing_env):
    app, _ = pairing_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://hub.example",
    ) as client:
        project = await _create_project(client)
        first = (
            await client.post(
                f"/api/projects/{project['id']}/pairing-invitations",
                headers=_headers("alice"),
                json={"role": "editor"},
            )
        ).json()["pairing"]
        assert (
            await client.post(
                "/api/link/redeem",
                json={"handle": "replaceable-lab", "code": first["code"]},
            )
        ).status_code == 200
        deleted = await client.post(
            "/api/link/admin/guests/replaceable-lab",
            headers=_headers("alice"),
            json={"action": "delete"},
        )
        assert deleted.status_code == 200, deleted.text

        second = (
            await client.post(
                f"/api/projects/{project['id']}/pairing-invitations",
                headers=_headers("alice"),
                json={"role": "viewer"},
            )
        ).json()["pairing"]
        assert (
            await client.post(
                "/api/link/redeem",
                json={"handle": "replaceable-lab", "code": second["code"]},
            )
        ).status_code == 200

        stale = await client.post(
            f"/api/projects/{project['id']}/remote-invitations",
            headers=_headers("alice"),
            json={
                "handle": "replaceable-lab",
                "role": "editor",
                "pairing_invite_id": first["id"],
            },
        )
        assert stale.status_code == 409
        exact = await client.post(
            f"/api/projects/{project['id']}/remote-invitations",
            headers=_headers("alice"),
            json={
                "handle": "replaceable-lab",
                "role": "viewer",
                "pairing_invite_id": second["id"],
            },
        )
        assert exact.status_code == 201, exact.text


async def test_blocked_pairing_is_terminal_and_cannot_be_invited(pairing_env):
    app, _ = pairing_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://hub.example",
    ) as client:
        project = await _create_project(client)
        pairing = (
            await client.post(
                f"/api/projects/{project['id']}/pairing-invitations",
                headers=_headers("alice"),
                json={"role": "editor"},
            )
        ).json()["pairing"]
        assert (
            await client.post(
                "/api/link/redeem",
                json={"handle": "blocked-lab", "code": pairing["code"]},
            )
        ).status_code == 200
        blocked = await client.post(
            "/api/link/admin/guests/blocked-lab",
            headers=_headers("alice"),
            json={"action": "block"},
        )
        assert blocked.status_code == 200, blocked.text
        status = await client.get(
            f"/api/projects/{project['id']}/pairing-invitations/{pairing['id']}",
            headers=_headers("alice"),
        )
        assert status.status_code == 200
        assert status.json()["pairing"]["status"] == "blocked"
        invited = await client.post(
            f"/api/projects/{project['id']}/remote-invitations",
            headers=_headers("alice"),
            json={
                "handle": "blocked-lab",
                "role": "editor",
                "pairing_invite_id": pairing["id"],
            },
        )
        assert invited.status_code == 404


async def test_advertised_pairing_origin_never_trusts_a_remote_host_header(
    pairing_env,
    monkeypatch,
):
    app, _ = pairing_env
    monkeypatch.setattr(
        project_routes, "get_setting", lambda _key, default="", owner=None: default
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://attacker.example",
    ) as client:
        project = await _create_project(client)
        linked = await client.get(
            f"/api/projects/{project['id']}/linked-instances",
            headers=_headers("alice"),
        )
        assert linked.status_code == 200
        assert linked.json()["hub_url"] == ""
        missing = await client.post(
            f"/api/projects/{project['id']}/pairing-invitations",
            headers=_headers("alice"),
            json={"role": "viewer"},
        )
        assert missing.status_code == 400
        unsafe = await client.post(
            f"/api/projects/{project['id']}/pairing-invitations",
            headers=_headers("alice"),
            json={"role": "viewer", "hub_url": "http://192.168.1.5:7000"},
        )
        assert unsafe.status_code == 400
        explicit = await client.post(
            f"/api/projects/{project['id']}/pairing-invitations",
            headers=_headers("alice"),
            json={"role": "viewer", "hub_url": "https://restia.example"},
        )
        assert explicit.status_code == 201, explicit.text
        invite_id = explicit.json()["pairing"]["id"]
        await client.delete(
            f"/api/projects/{project['id']}/pairing-invitations/{invite_id}",
            headers=_headers("alice"),
        )

        monkeypatch.setattr(
            project_routes,
            "get_setting",
            lambda key, default="", owner=None: "https://trusted.example"
            if key == "app_public_url"
            else default,
        )
        configured = await client.get(
            f"/api/projects/{project['id']}/linked-instances",
            headers=_headers("alice"),
        )
        assert configured.json()["hub_url"] == "https://trusted.example"

    monkeypatch.setattr(
        project_routes, "get_setting", lambda _key, default="", owner=None: default
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:7000",
    ) as loopback_client:
        linked = await loopback_client.get(
            f"/api/projects/{project['id']}/linked-instances",
            headers=_headers("alice"),
        )
        assert linked.json()["hub_url"] == "http://127.0.0.1:7000"
