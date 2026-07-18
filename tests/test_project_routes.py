"""Focused API regressions for Jira-style project workflows."""

from __future__ import annotations

import asyncio
import base64
import io
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier, Event
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.project_routes as project_routes
from routes.auth_routes import SESSION_COOKIE, setup_auth_routes
from src.project_storage import ProjectFileStore


_PEER = ("203.0.113.44", 54321)


class _Identity:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            user = dict(scope.get("headers") or []).get(b"x-test-user")
            if user:
                scope.setdefault("state", {})["current_user"] = user.decode()
        await self.app(scope, receive, send)


@pytest.fixture
def project_env(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'projects.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(project_routes, "SessionLocal", factory)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("LOCALHOST_BYPASS", raising=False)

    store = ProjectFileStore(tmp_path / "project-files")
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(
        is_configured=True,
        users={"alice": {"is_admin": True}, "bob": {"is_admin": False}},
        is_admin=lambda user: user == "alice",
    )
    app.include_router(project_routes.setup_project_routes(store))
    yield _Identity(app), factory, store
    engine.dispose()


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=_PEER),
        base_url="http://projects.test",
    )


def _headers(user: str) -> dict[str, str]:
    return {"x-test-user": user}


async def _create_project(client, *, key="ENG", template="general"):
    response = await client.post(
        "/api/projects",
        headers=_headers("alice"),
        json={"name": "Engineering", "key": key, "template": template},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _mount_remote_projects(app_wrapper, factory, store, guest_id: int):
    async def remote_identity(request: Request):
        project_id = request.path_params.get("project_id")
        grant_id = None
        if project_id:
            db = factory()
            try:
                grant = db.query(cdb.ProjectRemoteGrant).filter(
                    cdb.ProjectRemoteGrant.project_id == project_id,
                    cdb.ProjectRemoteGrant.guest_id == guest_id,
                    cdb.ProjectRemoteGrant.status == "active",
                ).first()
                grant_id = grant.id if grant else None
            finally:
                db.close()
        project_routes.set_remote_project_context(request, guest_id, grant_id)

    app_wrapper.app.include_router(
        project_routes.setup_project_routes(
            store,
            prefix="/api/link/projects",
            remote_only=True,
            dependencies=[Depends(remote_identity)],
        )
    )


def _docx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr(
            "word/document.xml",
            """
            <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
              <w:body><w:p><w:r><w:t>Verified project work</w:t></w:r></w:p></w:body>
            </w:document>
            """,
        )
    return output.getvalue()


def _xlsx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr(
            "xl/workbook.xml",
            """
            <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
              xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
              <sheets><sheet name="Results" sheetId="1" r:id="rId1"/></sheets>
            </workbook>
            """,
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            """
            <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
              <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
            </Relationships>
            """,
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """
            <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
              <sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Force</t></is></c><c r="B1"><v>42</v></c></row></sheetData>
            </worksheet>
            """,
        )
    return output.getvalue()


def _pptx_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr(
            "ppt/presentation.xml",
            '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>',
        )
        archive.writestr(
            "ppt/slides/slide1.xml",
            """
            <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
              <a:p><a:r><a:t>Design review</a:t></a:r></a:p>
            </p:sld>
            """,
        )
    return output.getvalue()


async def test_project_templates_members_and_owner_scoping(project_env):
    app, _, _ = project_env
    async with _client(app) as client:
        assert (await client.get("/api/projects")).status_code == 401

        templates = await client.get("/api/projects/templates", headers=_headers("alice"))
        assert templates.status_code == 200
        template_ids = {row["id"] for row in templates.json()["templates"]}
        assert {
            "general",
            "software",
            "research",
            "content",
            "personal",
            "coursework",
            "gtm",
            "applications",
            "engineering_labbook",
            "opportunity_radar",
            "gtm_pipeline",
            "weekly_review",
        } <= template_ids

        created = await _create_project(client)
        project = created["project"]
        project_id = project["id"]
        assert project["key"] == "ENG"
        assert project["role"] == "owner"
        assert len(created["stages"]) == 5
        unsafe_color = await client.patch(
            f"/api/projects/{project_id}",
            headers=_headers("alice"),
            json={"color": "url(//tracker)", "version": project["version"]},
        )
        assert unsafe_color.status_code == 400

        duplicate = await client.post(
            "/api/projects",
            headers=_headers("alice"),
            json={"name": "Duplicate", "key": "ENG"},
        )
        assert duplicate.status_code == 409
        assert (await client.get(f"/api/projects/{project_id}", headers=_headers("bob"))).status_code == 404

        ghost = await client.post(
            f"/api/projects/{project_id}/members",
            headers=_headers("alice"),
            json={"username": "future-signup", "role": "editor"},
        )
        assert ghost.status_code == 400

        member = await client.post(
            f"/api/projects/{project_id}/members",
            headers=_headers("alice"),
            json={"username": "Bob", "role": "viewer"},
        )
        assert member.status_code == 201
        assert member.json()["member"] == {
            **member.json()["member"],
            "username": "bob",
            "role": "viewer",
        }
        bob_project = await client.get(f"/api/projects/{project_id}", headers=_headers("bob"))
        assert bob_project.status_code == 200
        assert bob_project.json()["project"]["role"] == "viewer"

        first_stage = created["stages"][0]["id"]
        viewer_assignment = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            json={"title": "Viewer cannot own this", "stage_id": first_stage, "assignee": "bob"},
        )
        assert viewer_assignment.status_code == 400
        forbidden = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("bob"),
            json={"title": "Cannot create", "stage_id": first_stage},
        )
        assert forbidden.status_code == 403
        promoted = await client.patch(
            f"/api/projects/{project_id}/members/bob",
            headers=_headers("alice"),
            json={"role": "editor"},
        )
        assert promoted.status_code == 200
        item = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("bob"),
            json={"title": "Build rig", "stage_id": first_stage, "assignee": "bob"},
        )
        assert item.status_code == 201, item.text
        assert item.json()["item"]["key"] == "ENG-1"
        assert item.json()["item"]["reporter"] == "bob"

        downgraded = await client.patch(
            f"/api/projects/{project_id}/members/bob",
            headers=_headers("alice"),
            json={"role": "viewer"},
        )
        assert downgraded.status_code == 200
        unassigned = await client.get(
            f"/api/projects/{project_id}/items/{item.json()['item']['id']}",
            headers=_headers("alice"),
        )
        assert unassigned.json()["item"]["assignee"] is None
        assert unassigned.json()["item"]["version"] == 2

        transferred = await client.post(
            f"/api/projects/{project_id}/transfer",
            headers=_headers("alice"),
            json={"username": "bob", "version": project["version"]},
        )
        assert transferred.status_code == 200, transferred.text
        assert transferred.json()["project"]["owner"] == "bob"
        assert transferred.json()["project"]["role"] == "editor"
        bob_owned = await client.get(f"/api/projects/{project_id}", headers=_headers("bob"))
        assert bob_owned.json()["project"]["role"] == "owner"
        assigned_to_previous_owner = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("bob"),
            json={"title": "Handoff", "stage_id": first_stage, "assignee": "alice"},
        )
        assert assigned_to_previous_owner.status_code == 201
        removed = await client.delete(
            f"/api/projects/{project_id}/members/alice",
            headers=_headers("bob"),
        )
        assert removed.status_code == 200
        handed_off = await client.get(
            f"/api/projects/{project_id}/items/{assigned_to_previous_owner.json()['item']['id']}",
            headers=_headers("bob"),
        )
        assert handed_off.json()["item"]["assignee"] is None
        assert handed_off.json()["item"]["version"] == 2
        assert (await client.get(f"/api/projects/{project_id}", headers=_headers("alice"))).status_code == 404


@pytest.mark.parametrize(
    ("template_id", "expected_name", "expected_stages"),
    [
        (
            "applications",
            "Applications Cockpit",
            [
                ("Researching", "backlog", "#64748b"),
                ("Shortlisted", "todo", "#3b82f6"),
                ("Preparing", "in_progress", "#f59e0b"),
                ("Submitted", "review", "#8b5cf6"),
                ("Decision", "done", "#22c55e"),
            ],
        ),
        (
            "engineering_labbook",
            "Engineering Labbook",
            [
                ("Ideas & Questions", "backlog", "#64748b"),
                ("Planned", "todo", "#3b82f6"),
                ("Setup & Calibration", "todo", "#06b6d4"),
                ("Experiment Running", "in_progress", "#f59e0b"),
                ("Analysis", "review", "#8b5cf6"),
                ("Findings", "done", "#22c55e"),
            ],
        ),
        (
            "opportunity_radar",
            "Opportunity Radar",
            [
                ("Discovered", "backlog", "#64748b"),
                ("Evaluating", "todo", "#3b82f6"),
                ("Qualified", "todo", "#06b6d4"),
                ("Pursuing", "in_progress", "#f59e0b"),
                ("Waiting", "review", "#8b5cf6"),
                ("Closed", "done", "#22c55e"),
            ],
        ),
        (
            "gtm_pipeline",
            "GTM Pipeline",
            [
                ("Accounts", "backlog", "#64748b"),
                ("Qualified", "todo", "#3b82f6"),
                ("Outreach", "in_progress", "#f59e0b"),
                ("Conversation", "in_progress", "#06b6d4"),
                ("Pilot / Proposal", "review", "#8b5cf6"),
                ("Closed", "done", "#22c55e"),
            ],
        ),
        (
            "weekly_review",
            "Weekly Review",
            [
                ("Capture", "backlog", "#64748b"),
                ("Review", "todo", "#3b82f6"),
                ("Decide", "in_progress", "#f59e0b"),
                ("Scheduled", "review", "#8b5cf6"),
                ("Closed", "done", "#22c55e"),
            ],
        ),
    ],
)
async def test_v2_project_templates_list_create_and_detail(
    project_env,
    template_id,
    expected_name,
    expected_stages,
):
    app, _, _ = project_env
    async with _client(app) as client:
        listed = await client.get("/api/projects/templates", headers=_headers("alice"))
        assert listed.status_code == 200
        listed_template = next(
            row for row in listed.json()["templates"] if row["id"] == template_id
        )
        assert listed_template == {
            "id": template_id,
            "name": expected_name,
            "stages": [
                {"name": name, "category": category, "color": color}
                for name, category, color in expected_stages
            ],
        }

        created_response = await client.post(
            "/api/projects",
            headers=_headers("alice"),
            json={
                "name": expected_name,
                "key": {
                    "applications": "APPS",
                    "engineering_labbook": "LAB",
                    "opportunity_radar": "RADAR",
                    "gtm_pipeline": "GTM",
                    "weekly_review": "WEEK",
                }[template_id],
                "template": template_id,
            },
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        assert created["project"]["template"] == template_id
        assert [
            (stage["name"], stage["category"], stage["color"])
            for stage in created["stages"]
        ] == expected_stages
        assert [stage["position"] for stage in created["stages"]] == list(
            range(len(expected_stages))
        )

        detail_response = await client.get(
            f"/api/projects/{created['project']['id']}",
            headers=_headers("alice"),
        )
        assert detail_response.status_code == 200
        detail = detail_response.json()
        assert detail["project"]["template"] == template_id
        assert detail["stages"] == created["stages"]


@pytest.mark.parametrize("template", ["not-a-template", "   ", ""])
async def test_project_creation_rejects_invalid_template_loudly(
    project_env,
    template,
):
    app, factory, _ = project_env
    async with _client(app) as client:
        rejected = await client.post(
            "/api/projects",
            headers=_headers("alice"),
            json={"name": "Invalid template", "key": "BAD", "template": template},
        )
    assert rejected.status_code == 400
    assert "template" in rejected.json()["detail"].lower()
    db = factory()
    assert db.query(cdb.Project).count() == 0
    db.close()


async def test_project_completion_is_explicit_guarded_and_reversible(project_env):
    app, _, _ = project_env
    async with _client(app) as client:
        created = await _create_project(client, key="SHIP")
        project = created["project"]
        project_id = project["id"]
        first_stage = created["stages"][0]
        done_stage = next(stage for stage in created["stages"] if stage["category"] == "done")

        assert project["status"] == "active"
        assert project["completed_at"] is None

        item_response = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            json={"title": "Ship the outcome", "stage_id": first_stage["id"]},
        )
        assert item_response.status_code == 201, item_response.text
        item = item_response.json()["item"]

        blocked = await client.post(
            f"/api/projects/{project_id}/complete",
            headers=_headers("alice"),
            json={"version": project["version"]},
        )
        assert blocked.status_code == 409
        assert "remaining 1 active work item" in blocked.json()["detail"]

        moved = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/move",
            headers=_headers("alice"),
            json={
                "stage_id": done_stage["id"],
                "version": item["version"],
            },
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["item"]["completed_at"] is not None

        completed = await client.post(
            f"/api/projects/{project_id}/complete",
            headers=_headers("alice"),
            json={"version": project["version"]},
        )
        assert completed.status_code == 200, completed.text
        completed_project = completed.json()["project"]
        assert completed_project["status"] == "completed"
        assert completed_project["completed_at"] is not None

        read_only = await client.patch(
            f"/api/projects/{project_id}",
            headers=_headers("alice"),
            json={"description": "Should not change", "version": completed_project["version"]},
        )
        assert read_only.status_code == 409
        assert "Reopen" in read_only.json()["detail"]

        reopened = await client.post(
            f"/api/projects/{project_id}/reopen",
            headers=_headers("alice"),
            json={"version": completed_project["version"]},
        )
        assert reopened.status_code == 200, reopened.text
        reopened_project = reopened.json()["project"]
        assert reopened_project["status"] == "active"
        assert reopened_project["completed_at"] is None

        editable_again = await client.patch(
            f"/api/projects/{project_id}",
            headers=_headers("alice"),
            json={"description": "Outcome recorded", "version": reopened_project["version"]},
        )
        assert editable_again.status_code == 200, editable_again.text


async def test_project_context_brief_prioritizes_risk_and_surfaces_evidence(project_env):
    app, _, _ = project_env
    async with _client(app) as client:
        created = await _create_project(client, key="LAB", template="engineering_labbook")
        project_id = created["project"]["id"]
        first_stage = created["stages"][0]["id"]

        overdue = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            json={
                "title": "Calibrate the load cell",
                "description": "Sensitive raw setup notes stay out of the brief.",
                "stage_id": first_stage,
                "priority": "high",
                "due_date": "2000-01-01",
            },
        )
        assert overdue.status_code == 201, overdue.text
        overdue_item = overdue.json()["item"]
        for index in range(6):
            added = await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={
                    "title": f"Experiment follow-up {index}",
                    "stage_id": first_stage,
                    "priority": "medium",
                },
            )
            assert added.status_code == 201

        pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
        uploaded = await client.post(
            f"/api/projects/{project_id}/items/{overdue_item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("calibration.pdf", pdf, "application/pdf")},
            data={"kind": "deliverable", "description": "Calibration evidence"},
        )
        assert uploaded.status_code == 201, uploaded.text

        response = await client.get(
            f"/api/projects/{project_id}/context", headers=_headers("alice")
        )
        assert response.status_code == 200, response.text
        context = response.json()
        assert context["project"]["template"] == "engineering_labbook"
        assert "hypothesis" in context["guidance"].lower()
        assert "7 open items" in context["brief"]
        assert context["next_actions"][0]["id"] == overdue_item["id"]
        assert context["next_actions"][0]["reason"].startswith("Overdue since")
        assert len(context["next_actions"]) == context["limits"]["next_actions"] == 5
        assert "description" not in context["next_actions"][0]
        assert context["evidence"][0]["name"] == "calibration.pdf"
        assert context["evidence"][0]["description"] == "Calibration evidence"
        assert len(context["recent_activity"]) <= context["limits"]["recent_activity"]

        hidden = await client.get(
            f"/api/projects/{project_id}/context", headers=_headers("bob")
        )
        assert hidden.status_code == 404


async def test_remote_grant_lifecycle_authorization_and_safe_serialization(project_env):
    app, factory, store = project_env
    db = factory()
    approved = cdb.LinkGuest(
        handle="remote-one",
        token_hash="b" * 64,
        status="approved",
    )
    pending = cdb.LinkGuest(
        handle="not-approved",
        token_hash="c" * 64,
        status="pending",
    )
    db.add_all([approved, pending])
    db.commit()
    approved_id = approved.id
    db.close()
    _mount_remote_projects(app, factory, store, approved_id)

    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        first_stage = created["stages"][0]["id"]
        added_editor = await client.post(
            f"/api/projects/{project_id}/members",
            headers=_headers("alice"),
            json={"username": "bob", "role": "editor"},
        )
        assert added_editor.status_code == 201, added_editor.text

        linked = await client.get(
            f"/api/projects/{project_id}/linked-instances", headers=_headers("alice")
        )
        assert linked.status_code == 200
        assert linked.json()["handles"] == ["remote-one"]

        unapproved = await client.post(
            f"/api/projects/{project_id}/remote-invitations",
            headers=_headers("alice"),
            json={"handle": "not-approved", "role": "editor"},
        )
        assert unapproved.status_code == 404

        invited = await client.post(
            f"/api/projects/{project_id}/remote-invitations",
            headers=_headers("alice"),
            json={"handle": "remote-one", "role": "editor"},
        )
        assert invited.status_code == 201, invited.text
        grant = invited.json()["grant"]
        assert grant["kind"] == "instance"
        assert grant["handle"] == "remote-one"
        assert grant["status"] == "pending"

        pending_list = await client.get("/api/link/projects")
        assert pending_list.status_code == 200
        assert pending_list.json()["projects"] == []
        assert (await client.post(
            "/api/link/projects",
            json={"name": "Forbidden", "key": "NO"},
        )).status_code == 403

        db = factory()
        active = db.query(cdb.ProjectRemoteGrant).filter_by(id=grant["id"]).one()
        active.status = "active"
        active.responded_at = cdb.utcnow_naive()
        active.version += 1
        peer = cdb.LinkGuest(
            handle="remote-two",
            token_hash="e" * 64,
            status="approved",
        )
        db.add(peer)
        db.flush()
        peer_grant = cdb.ProjectRemoteGrant(
            id="99999999-9999-9999-9999-999999999999",
            project_id=project_id,
            guest_id=peer.id,
            handle_snapshot=peer.handle,
            role="editor",
            status="active",
            invited_by="alice",
            responded_at=cdb.utcnow_naive(),
        )
        db.add(peer_grant)
        db.commit()
        active_version = active.version
        peer_principal = project_routes.remote_grant_principal(peer_grant.id)
        db.close()

        # Internal Home Link principal strings are never authentication. Even
        # a legacy/manually injected local profile with the exact same name
        # must stay on the ordinary local authorization path.
        spoofed_users = [
            project_routes.remote_instance_principal(approved_id),
            project_routes.remote_grant_principal(grant["id"]),
        ]
        for spoofed_user in spoofed_users:
            app.app.state.auth_manager.users[spoofed_user] = {"is_admin": False}
            spoofed_board = await client.get(
                f"/api/projects/{project_id}/board",
                headers=_headers(spoofed_user),
            )
            assert spoofed_board.status_code == 404
            spoofed_create = await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers(spoofed_user),
                json={"title": "Principal spoof", "stage_id": first_stage},
            )
            assert spoofed_create.status_code == 404

        owner_assigned = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            json={
                "title": "Owner-only identity",
                "stage_id": first_stage,
                "assignee": "alice",
            },
        )
        assert owner_assigned.status_code == 201
        editor_assigned = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            json={
                "title": "Other local identity",
                "stage_id": first_stage,
                "assignee": "bob",
            },
        )
        assert editor_assigned.status_code == 201
        peer_assigned = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            json={
                "title": "Other linked identity",
                "stage_id": first_stage,
                "assignee": peer_principal,
            },
        )
        assert peer_assigned.status_code == 201
        for hidden_guess in ("alice", "bob", "future-signup"):
            hidden_filter = await client.get(
                f"/api/link/projects/{project_id}/items?assignee={hidden_guess}"
            )
            assert hidden_filter.status_code == 400
        owner_alias = await client.get(
            f"/api/link/projects/{project_id}/items?assignee=instance"
        )
        assert owner_alias.status_code == 200
        assert {row["id"] for row in owner_alias.json()["items"]} == {
            owner_assigned.json()["item"]["id"],
            editor_assigned.json()["item"]["id"],
        }
        assert {row["assignee"] for row in owner_alias.json()["items"]} == {
            "instance"
        }

        remote_list = await client.get("/api/link/projects")
        assert remote_list.status_code == 200, remote_list.text
        assert [row["id"] for row in remote_list.json()["projects"]] == [project_id]
        assert remote_list.json()["projects"][0]["owner"] == "instance"

        board = await client.get(f"/api/link/projects/{project_id}/board")
        assert board.status_code == 200, board.text
        board_body = board.json()
        assert board_body["actor"] == "me"
        assert board_body["project"]["owner"] == "instance"
        assert {row["username"] for row in board_body["members"]} == {
            "instance",
            "me",
            peer_principal,
        }
        assert all(row.get("handle") != "remote-one" for row in board_body["members"])

        created_item = await client.post(
            f"/api/link/projects/{project_id}/items",
            json={"title": "Remote work", "stage_id": first_stage, "assignee": "me"},
        )
        assert created_item.status_code == 201, created_item.text
        item = created_item.json()["item"]
        assert item["reporter"] == "me"
        assert item["assignee"] == "me"
        mine = await client.get(
            f"/api/link/projects/{project_id}/items?assignee=me"
        )
        assert mine.status_code == 200
        assert [row["id"] for row in mine.json()["items"]] == [item["id"]]
        principal = project_routes.remote_grant_principal(grant["id"])
        exact_remote = await client.get(
            f"/api/link/projects/{project_id}/items",
            params={"assignee": peer_principal},
        )
        assert exact_remote.status_code == 200
        assert [row["id"] for row in exact_remote.json()["items"]] == [
            peer_assigned.json()["item"]["id"]
        ]

        db = factory()
        stored_item = db.query(cdb.ProjectWorkItem).filter_by(id=item["id"]).one()
        assert stored_item.reporter == principal
        assert stored_item.assignee == principal
        db.close()

        downgraded = await client.patch(
            f"/api/projects/{project_id}/remote-grants/{grant['id']}",
            headers=_headers("alice"),
            json={"role": "viewer", "version": active_version},
        )
        assert downgraded.status_code == 200, downgraded.text
        assert downgraded.json()["unassigned_items"] == 1
        viewer_version = downgraded.json()["grant"]["version"]
        forbidden = await client.post(
            f"/api/link/projects/{project_id}/items",
            json={"title": "Viewer write", "stage_id": first_stage},
        )
        assert forbidden.status_code == 403
        assert (await client.get(f"/api/link/projects/{project_id}/board")).status_code == 200

        revoked = await client.delete(
            f"/api/projects/{project_id}/remote-grants/{grant['id']}?version={viewer_version}",
            headers=_headers("alice"),
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["grant"]["status"] == "revoked"
        assert (await client.get(f"/api/link/projects/{project_id}/board")).status_code == 404

        reinvited = await client.post(
            f"/api/projects/{project_id}/remote-invitations",
            headers=_headers("alice"),
            json={"handle": "remote-one", "role": "editor"},
        )
        assert reinvited.status_code == 201, reinvited.text
        assert reinvited.json()["grant"]["id"] == grant["id"]
        assert reinvited.json()["grant"]["status"] == "pending"


async def test_remote_project_list_keeps_each_projects_own_role(project_env):
    app, factory, store = project_env
    db = factory()
    guest = cdb.LinkGuest(
        handle="multi-project",
        token_hash="d" * 64,
        status="approved",
    )
    db.add(guest)
    db.commit()
    guest_id = guest.id
    db.close()
    _mount_remote_projects(app, factory, store, guest_id)

    async with _client(app) as client:
        first = await _create_project(client, key="ONE")
        second = await _create_project(client, key="TWO")
        for project, role in ((first, "editor"), (second, "viewer")):
            invited = await client.post(
                f"/api/projects/{project['project']['id']}/remote-invitations",
                headers=_headers("alice"),
                json={"handle": "multi-project", "role": role},
            )
            assert invited.status_code == 201, invited.text

        db = factory()
        grants = db.query(cdb.ProjectRemoteGrant).filter_by(guest_id=guest_id).all()
        assert len(grants) == 2
        for grant in grants:
            grant.status = "active"
            grant.responded_at = cdb.utcnow_naive()
            grant.version += 1
        db.commit()
        db.close()

        response = await client.get("/api/link/projects")
        assert response.status_code == 200, response.text
        roles_by_key = {
            project["key"]: project["role"]
            for project in response.json()["projects"]
        }
        assert roles_by_key == {"ONE": "editor", "TWO": "viewer"}


async def test_project_owner_cannot_invite_a_personally_blocked_instance(project_env):
    app, factory, _ = project_env
    db = factory()
    guest = cdb.LinkGuest(
        handle="blocked-instance",
        token_hash="e" * 64,
        status="approved",
    )
    db.add(guest)
    db.add(cdb.RemoteBlock(local_user="alice", handle=guest.handle))
    db.commit()
    db.close()

    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        linked = await client.get(
            f"/api/projects/{project_id}/linked-instances", headers=_headers("alice")
        )
        assert linked.status_code == 200
        assert linked.json()["instances"] == []
        denied = await client.post(
            f"/api/projects/{project_id}/remote-invitations",
            headers=_headers("alice"),
            json={"handle": "blocked-instance", "role": "editor"},
        )
        assert denied.status_code == 409
        assert "unblock" in denied.json()["detail"].lower()


async def test_remote_grant_tombstones_are_pruned_before_new_invite(
    project_env,
    monkeypatch,
):
    app, factory, _ = project_env
    monkeypatch.setattr(project_routes, "REMOTE_GRANT_TOMBSTONE_LIMIT", 3)
    db = factory()
    guest = cdb.LinkGuest(
        handle="fresh-instance",
        token_hash="f" * 64,
        status="approved",
    )
    db.add(guest)
    db.commit()
    guest_id = guest.id
    db.close()

    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        db = factory()
        for index in range(6):
            db.add(cdb.ProjectRemoteGrant(
                id=f"dead-{index}",
                project_id=project_id,
                guest_id=None,
                handle_snapshot=f"retired-{index}",
                role="viewer",
                status="revoked",
                invited_by="alice",
                revoked_at=cdb.utcnow_naive(),
            ))
        db.commit()
        db.close()

        invited = await client.post(
            f"/api/projects/{project_id}/remote-invitations",
            headers=_headers("alice"),
            json={"handle": "fresh-instance", "role": "viewer"},
        )
        assert invited.status_code == 201, invited.text

    db = factory()
    assert db.query(cdb.ProjectRemoteGrant).filter(
        cdb.ProjectRemoteGrant.project_id == project_id,
        cdb.ProjectRemoteGrant.guest_id.is_(None),
    ).count() == 3
    assert db.query(cdb.ProjectRemoteGrant).filter(
        cdb.ProjectRemoteGrant.project_id == project_id,
        cdb.ProjectRemoteGrant.guest_id == guest_id,
        cdb.ProjectRemoteGrant.status == "pending",
    ).count() == 1
    db.close()


def test_concurrent_remote_invitations_are_serialized(project_env):
    app, factory, _ = project_env

    async def seed():
        async with _client(app) as client:
            created = await _create_project(client)
            return created["project"]["id"]

    project_id = asyncio.run(seed())
    db = factory()
    guest = cdb.LinkGuest(
        handle="race-instance",
        token_hash="9" * 64,
        status="approved",
    )
    db.add(guest)
    db.commit()
    guest_id = guest.id
    db.close()
    barrier = Barrier(2)

    def invite(role):
        async def request_invite():
            async with _client(app) as client:
                response = await client.post(
                    f"/api/projects/{project_id}/remote-invitations",
                    headers=_headers("alice"),
                    json={"handle": "race-instance", "role": role},
                )
                return response.status_code

        barrier.wait()
        return asyncio.run(request_invite())

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(invite, ("viewer", "editor")))
    assert statuses == [201, 409]
    db = factory()
    grants = db.query(cdb.ProjectRemoteGrant).filter(
        cdb.ProjectRemoteGrant.project_id == project_id,
        cdb.ProjectRemoteGrant.guest_id == guest_id,
    ).all()
    assert len(grants) == 1
    assert grants[0].status == "pending"
    assert grants[0].role in {"viewer", "editor"}
    db.close()


async def test_workflow_wip_versions_hierarchy_and_tracking(project_env):
    app, _, _ = project_env
    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        stages = created["stages"]
        backlog = next(row for row in stages if row["category"] == "backlog")
        doing = next(row for row in stages if row["category"] == "in_progress")
        done = next(row for row in stages if row["category"] == "done")

        wip = await client.patch(
            f"/api/projects/{project_id}/stages/{doing['id']}",
            headers=_headers("alice"),
            json={"wip_limit": 1},
        )
        assert wip.status_code == 200

        parent_response = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            json={"title": "Parent", "stage_id": backlog["id"]},
        )
        parent = parent_response.json()["item"]
        active = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={"title": "Active", "stage_id": doing["id"], "estimate_minutes": 120},
            )
        ).json()["item"]
        rejected = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            json={"title": "Over limit", "stage_id": doing["id"]},
        )
        assert rejected.status_code == 409

        child_response = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            json={
                "title": "Child",
                "stage_id": backlog["id"],
                "item_type": "subtask",
                "parent_id": parent["id"],
            },
        )
        child = child_response.json()["item"]
        assert child["key"] == "ENG-3"  # WIP rejection never consumed a key.

        parent_to_subtask = await client.patch(
            f"/api/projects/{project_id}/items/{parent['id']}",
            headers=_headers("alice"),
            json={"item_type": "subtask", "parent_id": child["id"], "version": parent["version"]},
        )
        assert parent_to_subtask.status_code == 409
        parent_delete = await client.delete(
            f"/api/projects/{project_id}/items/{parent['id']}?version={parent['version']}",
            headers=_headers("alice"),
        )
        assert parent_delete.status_code == 409
        parent_archive = await client.post(
            f"/api/projects/{project_id}/items/{parent['id']}/archive",
            headers=_headers("alice"),
            json={"version": parent["version"]},
        )
        assert parent_archive.status_code == 409

        moved_response = await client.post(
            f"/api/projects/{project_id}/items/{active['id']}/move",
            headers=_headers("alice"),
            json={"stage_id": done["id"], "version": active["version"]},
        )
        assert moved_response.status_code == 200
        moved = moved_response.json()["item"]
        assert moved["version"] == 2 and moved["completed_at"]
        completed_at = moved["completed_at"]
        stale = await client.post(
            f"/api/projects/{project_id}/items/{active['id']}/move",
            headers=_headers("alice"),
            json={"stage_id": backlog["id"], "version": active["version"]},
        )
        assert stale.status_code == 409
        same_stage = await client.post(
            f"/api/projects/{project_id}/items/{active['id']}/move",
            headers=_headers("alice"),
            json={"stage_id": done["id"], "position": 0, "version": moved["version"]},
        )
        assert same_stage.status_code == 200
        moved = same_stage.json()["item"]
        assert moved["completed_at"] == completed_at

        checklist = await client.post(
            f"/api/projects/{project_id}/items/{active['id']}/checklist",
            headers=_headers("alice"),
            json={"text": "Attach calculation"},
        )
        assert checklist.status_code == 201
        check_id = checklist.json()["checklist_item"]["id"]
        assert (
            await client.patch(
                f"/api/projects/{project_id}/items/{active['id']}/checklist/{check_id}",
                headers=_headers("alice"),
                json={"done": True},
            )
        ).json()["checklist_item"]["done"] is True
        comment = await client.post(
            f"/api/projects/{project_id}/items/{active['id']}/comments",
            headers=_headers("alice"),
            json={"body": "Verified against hand calculations."},
        )
        assert comment.status_code == 201

        board = await client.get(f"/api/projects/{project_id}/board", headers=_headers("alice"))
        assert board.status_code == 200
        assert board.json()["overview"]["done_items"] == 1
        assert board.json()["overview"]["completion_percent"] == 33
        global_view = await client.get("/api/projects/overview", headers=_headers("alice"))
        assert global_view.status_code == 200
        assert global_view.json()["totals"]["projects"] == 1
        assert global_view.json()["totals"]["done_items"] == 1

        blocked_update = await client.patch(
            f"/api/projects/{project_id}/items/{child['id']}",
            headers=_headers("alice"),
            json={"blocked_by_id": parent["id"], "version": child["version"]},
        )
        assert blocked_update.status_code == 200
        child = blocked_update.json()["item"]
        at_risk = await client.get(f"/api/projects/{project_id}/overview", headers=_headers("alice"))
        assert at_risk.json()["overview"]["blocked_items"] == 1
        parent_done = await client.post(
            f"/api/projects/{project_id}/items/{parent['id']}/move",
            headers=_headers("alice"),
            json={"stage_id": done["id"], "version": parent["version"]},
        )
        assert parent_done.status_code == 200
        parent = parent_done.json()["item"]
        unblocked = await client.get(f"/api/projects/{project_id}/overview", headers=_headers("alice"))
        assert unblocked.json()["overview"]["blocked_items"] == 0

        temporary_stage = await client.post(
            f"/api/projects/{project_id}/stages",
            headers=_headers("alice"),
            json={"name": "Temporary", "category": "todo"},
        )
        assert temporary_stage.status_code == 201
        temp_stage_id = temporary_stage.json()["stage"]["id"]
        temp_item = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            json={"title": "Migrate me", "stage_id": temp_stage_id},
        )
        assert temp_item.status_code == 201
        stage_deleted = await client.delete(
            f"/api/projects/{project_id}/stages/{temp_stage_id}?move_to_stage_id={backlog['id']}",
            headers=_headers("alice"),
        )
        assert stage_deleted.status_code == 200, stage_deleted.text
        migrated = await client.get(
            f"/api/projects/{project_id}/items/{temp_item.json()['item']['id']}",
            headers=_headers("alice"),
        )
        assert migrated.json()["item"]["stage_id"] == backlog["id"]

        archived = await client.post(
            f"/api/projects/{project_id}/items/{child['id']}/archive",
            headers=_headers("alice"),
            json={"version": child["version"]},
        )
        assert archived.status_code == 200
        archived_version = archived.json()["item"]["version"]
        archived_edit = await client.patch(
            f"/api/projects/{project_id}/items/{child['id']}",
            headers=_headers("alice"),
            json={"title": "Should fail", "version": archived_version},
        )
        assert archived_edit.status_code == 409
        archived_comment = await client.post(
            f"/api/projects/{project_id}/items/{child['id']}/comments",
            headers=_headers("alice"),
            json={"body": "Should also fail"},
        )
        assert archived_comment.status_code == 409
        archived_parent = await client.post(
            f"/api/projects/{project_id}/items/{parent['id']}/archive",
            headers=_headers("alice"),
            json={"version": parent["version"]},
        )
        assert archived_parent.status_code == 200
        hidden_parent_restore = await client.post(
            f"/api/projects/{project_id}/items/{child['id']}/restore",
            headers=_headers("alice"),
            json={"version": archived_version},
        )
        assert hidden_parent_restore.status_code == 409
        restored_parent = await client.post(
            f"/api/projects/{project_id}/items/{parent['id']}/restore",
            headers=_headers("alice"),
            json={"version": archived_parent.json()["item"]["version"]},
        )
        assert restored_parent.status_code == 200
        restored = await client.post(
            f"/api/projects/{project_id}/items/{child['id']}/restore",
            headers=_headers("alice"),
            json={"version": archived.json()["item"]["version"]},
        )
        assert restored.status_code == 200

        project_archive = await client.post(
            f"/api/projects/{project_id}/archive",
            headers=_headers("alice"),
            json={"version": created["project"]["version"]},
        )
        assert project_archive.status_code == 200
        restore = await client.post(
            f"/api/projects/{project_id}/restore",
            headers=_headers("alice"),
            json={"version": project_archive.json()["project"]["version"]},
        )
        assert restore.status_code == 200


async def test_durable_submissions_download_security_and_cleanup(project_env):
    app, factory, store = project_env
    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        backlog = next(row for row in created["stages"] if row["category"] == "backlog")
        review = next(row for row in created["stages"] if row["category"] == "review")
        item = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={"title": "Submit design", "stage_id": backlog["id"]},
            )
        ).json()["item"]

        pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
        submitted = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("design.pdf", pdf, "application/pdf")},
            data={
                "kind": "deliverable",
                "description": "Final design review pack",
                "submission_note": "Ready for review",
                "transition_stage_id": review["id"],
                "version": str(item["version"]),
            },
        )
        assert submitted.status_code == 201, submitted.text
        body = submitted.json()
        attachment = body["attachment"]
        assert attachment["kind"] == "deliverable"
        assert attachment["sha256"]
        assert body["item"]["stage_id"] == review["id"]
        assert body["item"]["version"] == 2

        db = factory()
        row = db.query(cdb.ProjectAttachment).filter(cdb.ProjectAttachment.id == attachment["id"]).one()
        storage_key = row.storage_key
        assert store.resolve(storage_key).read_bytes() == pdf
        db.close()
        download = await client.get(attachment["download_url"], headers=_headers("alice"))
        assert download.status_code == 200
        assert download.content == pdf
        assert download.headers["x-content-type-options"] == "nosniff"
        assert download.headers["cache-control"] == "private, no-store"
        assert download.headers["content-disposition"].startswith("attachment;")
        assert (await client.get(attachment["download_url"], headers=_headers("bob"))).status_code == 404

        activity = await client.get(
            f"/api/projects/{project_id}/activity?work_item_id={item['id']}",
            headers=_headers("alice"),
        )
        events = [row["event_type"] for row in activity.json()["activity"]]
        assert events.count("work_submitted") == 1
        assert "attachment_added" not in events

        docx = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("calculation.docx", _docx_bytes(), "application/octet-stream")},
            data={"kind": "draft"},
        )
        assert docx.status_code == 201, docx.text
        assert docx.json()["attachment"]["mime"].endswith("wordprocessingml.document")

        traversal = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("../escape.pdf", pdf, "application/pdf")},
        )
        assert traversal.status_code == 400
        invalid_pdf = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("fake.pdf", b"not pdf", "application/pdf")},
        )
        assert invalid_pdf.status_code == 400
        html = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("payload.html", b"<script>alert(1)</script>", "text/html")},
        )
        assert html.status_code == 400

        deleted = await client.delete(
            f"/api/projects/{project_id}/attachments/{attachment['id']}",
            headers=_headers("alice"),
        )
        assert deleted.status_code == 200
        assert not store.resolve(storage_key, must_exist=False).exists()
        assert (await client.get(attachment["download_url"], headers=_headers("alice"))).status_code == 404

        project_deleted = await client.delete(
            f"/api/projects/{project_id}?confirm_key=ENG",
            headers=_headers("alice"),
        )
        assert project_deleted.status_code == 200, project_deleted.text
        assert not (store.root / project_id).exists()
        db = factory()
        assert db.query(cdb.Project).filter(cdb.Project.id == project_id).count() == 0
        db.close()


async def test_attachment_viewer_supports_safe_types_ranges_and_fallbacks(project_env):
    app, _, _ = project_env
    async with _client(app) as client:
        created = await _create_project(client, key="VIEW")
        project_id = created["project"]["id"]
        item = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={
                    "title": "Inspect evidence",
                    "stage_id": created["stages"][0]["id"],
                },
            )
        ).json()["item"]

        pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
        uploaded_pdf = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("design review.pdf", pdf, "application/pdf")},
        )
        assert uploaded_pdf.status_code == 201, uploaded_pdf.text
        pdf_attachment = uploaded_pdf.json()["attachment"]
        pdf_view = f"/api/projects/attachments/{pdf_attachment['id']}/view"

        full = await client.get(pdf_view, headers=_headers("alice"))
        assert full.status_code == 200
        assert full.content == pdf
        assert full.headers["content-type"] == "application/pdf"
        assert full.headers["content-disposition"].startswith("inline;")
        assert full.headers["accept-ranges"] == "bytes"
        assert full.headers["cache-control"] == "private, no-store"
        assert full.headers["x-content-type-options"] == "nosniff"
        assert full.headers["cross-origin-resource-policy"] == "same-origin"

        partial = await client.get(
            pdf_view,
            headers={**_headers("alice"), "Range": "bytes=5-13"},
        )
        assert partial.status_code == 206
        assert partial.content == pdf[5:14]
        assert partial.headers["content-range"] == f"bytes 5-13/{len(pdf)}"
        assert partial.headers["content-length"] == "9"

        suffix = await client.get(
            pdf_view,
            headers={**_headers("alice"), "Range": "bytes=-5"},
        )
        assert suffix.status_code == 206
        assert suffix.content == pdf[-5:]
        assert suffix.headers["content-range"] == (
            f"bytes {len(pdf) - 5}-{len(pdf) - 1}/{len(pdf)}"
        )

        invalid = await client.get(
            pdf_view,
            headers={**_headers("alice"), "Range": "bytes=0-1,4-5"},
        )
        assert invalid.status_code == 416
        assert invalid.headers["content-range"] == f"bytes */{len(pdf)}"
        assert invalid.headers["accept-ranges"] == "bytes"
        assert (await client.get(pdf_view, headers=_headers("bob"))).status_code == 404

        text_bytes = "first line\nsecond line\n".encode()
        uploaded_text = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("notes.txt", text_bytes, "text/plain")},
        )
        text_view = (
            f"/api/projects/attachments/{uploaded_text.json()['attachment']['id']}/view"
        )
        text_preview = await client.get(
            text_view,
            headers={**_headers("alice"), "Range": "bytes=0-9"},
        )
        assert text_preview.status_code == 206
        assert text_preview.content == text_bytes[:10]
        assert text_preview.headers["content-type"].startswith("text/plain")

        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        uploaded_image = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("evidence.png", png, "image/png")},
        )
        assert uploaded_image.status_code == 201, uploaded_image.text
        image_view = (
            f"/api/projects/attachments/{uploaded_image.json()['attachment']['id']}/view"
        )
        image_preview = await client.get(image_view, headers=_headers("alice"))
        assert image_preview.status_code == 200
        assert image_preview.content == png
        assert image_preview.headers["content-type"] == "image/png"

        uploaded_docx = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("calculation.docx", _docx_bytes(), "application/octet-stream")},
        )
        docx_view = (
            f"/api/projects/attachments/{uploaded_docx.json()['attachment']['id']}/view"
        )
        fallback = await client.get(docx_view, headers=_headers("alice"))
        assert fallback.status_code == 415
        assert "Download it to open it locally" in fallback.json()["detail"]
        docx_preview_url = (
            f"/api/projects/attachments/{uploaded_docx.json()['attachment']['id']}/preview"
        )
        docx_preview = await client.get(docx_preview_url, headers=_headers("alice"))
        assert docx_preview.status_code == 200, docx_preview.text
        assert docx_preview.json()["sections"][0]["text"] == "Verified project work"
        assert docx_preview.headers["cache-control"] == "private, no-store"
        assert docx_preview.headers["x-content-type-options"] == "nosniff"
        assert (await client.get(docx_preview_url, headers=_headers("bob"))).status_code == 404

        uploaded_xlsx = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("results.xlsx", _xlsx_bytes(), "application/octet-stream")},
        )
        assert uploaded_xlsx.status_code == 201, uploaded_xlsx.text
        xlsx_preview = await client.get(
            f"/api/projects/attachments/{uploaded_xlsx.json()['attachment']['id']}/preview",
            headers=_headers("alice"),
        )
        assert xlsx_preview.status_code == 200, xlsx_preview.text
        assert xlsx_preview.json()["sections"][0]["rows"] == [["Force", "42"]]

        uploaded_pptx = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("review.pptx", _pptx_bytes(), "application/octet-stream")},
        )
        assert uploaded_pptx.status_code == 201, uploaded_pptx.text
        pptx_preview = await client.get(
            f"/api/projects/attachments/{uploaded_pptx.json()['attachment']['id']}/preview",
            headers=_headers("alice"),
        )
        assert pptx_preview.status_code == 200, pptx_preview.text
        assert pptx_preview.json()["sections"] == [
            {"kind": "text", "title": "Slide 1", "text": "Design review"}
        ]

        corrupt_docx_bytes = io.BytesIO()
        with zipfile.ZipFile(corrupt_docx_bytes, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types />")
            archive.writestr("word/document.xml", "<w:document>")
        corrupt_docx = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("corrupt.docx", corrupt_docx_bytes.getvalue(), "application/octet-stream")},
        )
        assert corrupt_docx.status_code == 201, corrupt_docx.text
        corrupt_preview = await client.get(
            f"/api/projects/attachments/{corrupt_docx.json()['attachment']['id']}/preview",
            headers=_headers("alice"),
        )
        assert corrupt_preview.status_code == 422
        assert "Download it" in corrupt_preview.json()["detail"]


@pytest.mark.parametrize("download_source", ["local", "linked"])
@pytest.mark.parametrize("damage", ["same_size", "truncated", "missing"])
async def test_attachment_download_fails_closed_on_storage_integrity_damage(
    project_env,
    download_source,
    damage,
):
    app, factory, store = project_env
    guest_id = None
    if download_source == "linked":
        db = factory()
        guest = cdb.LinkGuest(
            handle=f"integrity-{damage.replace('_', '-')}",
            token_hash="d" * 64,
            status="approved",
        )
        db.add(guest)
        db.commit()
        guest_id = guest.id
        db.close()
        _mount_remote_projects(app, factory, store, guest_id)

    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        item = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={
                    "title": "Integrity check",
                    "stage_id": created["stages"][0]["id"],
                },
            )
        ).json()["item"]
        uploaded = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("integrity.pdf", pdf, "application/pdf")},
        )
        assert uploaded.status_code == 201, uploaded.text
        attachment = uploaded.json()["attachment"]

        if guest_id is not None:
            db = factory()
            db.add(
                cdb.ProjectRemoteGrant(
                    id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                    project_id=project_id,
                    guest_id=guest_id,
                    handle_snapshot=f"integrity-{damage.replace('_', '-')}",
                    role="editor",
                    status="active",
                    invited_by="alice",
                )
            )
            db.commit()
            db.close()
            download_url = (
                f"/api/link/projects/attachments/{attachment['id']}/download"
            )
            download_headers = {}
        else:
            download_url = attachment["download_url"]
            download_headers = _headers("alice")

        clean = await client.get(download_url, headers=download_headers)
        assert clean.status_code == 200, clean.text
        assert clean.content == pdf

        db = factory()
        row = db.query(cdb.ProjectAttachment).filter_by(id=attachment["id"]).one()
        path = store.resolve(row.storage_key)
        db.close()
        if damage == "same_size":
            tampered = bytearray(pdf)
            tampered[len(tampered) // 2] ^= 1
            path.write_bytes(tampered)
            assert path.stat().st_size == len(pdf)
        elif damage == "truncated":
            path.write_bytes(pdf[:-7])
        else:
            path.unlink()

        if download_source == "local":
            unauthorized = await client.get(
                download_url,
                headers=_headers("bob"),
            )
            assert unauthorized.status_code == 404

        rejected = await client.get(download_url, headers=download_headers)
        assert rejected.status_code == 500
        assert rejected.json() == {
            "detail": "Stored attachment failed integrity verification"
        }
        assert pdf not in rejected.content


async def test_attachment_download_streams_the_verified_snapshot_after_path_replacement(
    project_env,
    monkeypatch,
):
    app, factory, store = project_env
    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        item = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={"title": "Snapshot check", "stage_id": created["stages"][0]["id"]},
            )
        ).json()["item"]
        uploaded = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("snapshot.pdf", pdf, "application/pdf")},
        )
        assert uploaded.status_code == 201, uploaded.text
        attachment = uploaded.json()["attachment"]

        db = factory()
        row = db.query(cdb.ProjectAttachment).filter_by(id=attachment["id"]).one()
        path = store.resolve(row.storage_key)
        db.close()

        original_snapshot = project_routes._verified_attachment_snapshot

        def snapshot_then_replace(*args, **kwargs):
            verified = original_snapshot(*args, **kwargs)
            assert verified is not None
            replacement = path.with_name(f"{path.name}.replacement")
            replacement.write_bytes(b"X" * len(pdf))
            replacement.replace(path)
            return verified

        monkeypatch.setattr(
            project_routes,
            "_verified_attachment_snapshot",
            snapshot_then_replace,
        )
        download = await client.get(
            attachment["download_url"],
            headers=_headers("alice"),
        )
        assert download.status_code == 200, download.text
        assert download.content == pdf
        assert path.read_bytes() == b"X" * len(pdf)


async def test_verified_attachment_response_closes_snapshot_on_client_disconnect():
    snapshot = tempfile.SpooledTemporaryFile(max_size=1, mode="w+b")
    snapshot.write(b"verified bytes")
    snapshot.seek(0)
    response = project_routes._VerifiedAttachmentResponse(
        snapshot,
        media_type="application/octet-stream",
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def disconnected_send(message):
        if message["type"] == "http.response.body":
            raise OSError("client disconnected")

    with pytest.raises(Exception) as exc_info:
        await response(
            {"type": "http", "method": "GET", "path": "/", "asgi": {"spec_version": "2.4"}},
            receive,
            disconnected_send,
        )
    assert exc_info.type.__name__ == "ClientDisconnect"
    assert snapshot.closed


async def test_cancelled_attachment_verification_closes_the_late_worker_result(
    monkeypatch,
    tmp_path,
):
    started = Event()
    worker_release = Event()
    gate = project_routes._AttachmentDownloadGate(total_limit=1, principal_limit=1)
    gate_release = gate.try_acquire("guest:42")
    assert gate_release is not None
    snapshot = tempfile.SpooledTemporaryFile(max_size=1, mode="w+b")
    snapshot.write(b"verified bytes")
    snapshot.seek(0)

    def delayed_snapshot(*_args, **_kwargs):
        started.set()
        assert worker_release.wait(timeout=5)
        return snapshot, len(b"verified bytes")

    monkeypatch.setattr(
        project_routes,
        "_verified_attachment_snapshot",
        delayed_snapshot,
    )
    preparation = asyncio.create_task(
        project_routes._prepare_verified_attachment_snapshot(
            tmp_path / "unused",
            1,
            "0" * 64,
        )
    )
    assert await asyncio.to_thread(started.wait, 2)
    preparation.cancel()
    await asyncio.sleep(0)
    assert not preparation.done()
    assert gate.active_counts() == (1, {"guest:42": 1})
    worker_release.set()
    with pytest.raises(asyncio.CancelledError):
        await preparation
    gate_release()
    assert snapshot.closed
    assert gate.active_counts() == (0, {})


def test_attachment_download_gate_bounds_global_and_per_principal_slots():
    gate = project_routes._AttachmentDownloadGate(total_limit=2, principal_limit=1)
    release_a = gate.try_acquire("guest:1")
    assert release_a is not None
    assert gate.try_acquire("guest:1") is None
    release_b = gate.try_acquire("guest:2")
    assert release_b is not None
    assert gate.try_acquire("guest:3") is None
    assert gate.active_counts() == (2, {"guest:1": 1, "guest:2": 1})

    release_a()
    release_a()  # Response cleanup is deliberately idempotent.
    release_c = gate.try_acquire("guest:3")
    assert release_c is not None
    release_b()
    release_c()
    assert gate.active_counts() == (0, {})


async def test_attachment_download_route_fails_fast_at_concurrency_limit(
    project_env,
    monkeypatch,
):
    app, _, _ = project_env
    gate = project_routes._AttachmentDownloadGate(total_limit=1, principal_limit=1)
    monkeypatch.setattr(project_routes, "_attachment_download_gate", gate)
    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"

    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        item = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={"title": "Bound downloads", "stage_id": created["stages"][0]["id"]},
            )
        ).json()["item"]
        uploaded = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("bounded.pdf", pdf, "application/pdf")},
        )
        assert uploaded.status_code == 201, uploaded.text
        download_url = uploaded.json()["attachment"]["download_url"]

        release = gate.try_acquire("profile:alice")
        assert release is not None
        limited = await client.get(download_url, headers=_headers("alice"))
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "2"
        assert "already active" in limited.json()["detail"]
        release()

        downloaded = await client.get(download_url, headers=_headers("alice"))
        assert downloaded.status_code == 200
        assert downloaded.content == pdf
        assert gate.active_counts() == (0, {})


def test_attachment_download_principal_collapses_remote_grants_to_one_guest():
    token = project_routes._PROJECT_REMOTE_CONTEXT.set(
        {"guest_id": 42, "grant_id": "grant-a", "grant_ids": {"grant-a"}}
    )
    try:
        assert project_routes._attachment_download_principal(
            "remote:grant-a"
        ) == "guest:42"
    finally:
        project_routes._PROJECT_REMOTE_CONTEXT.reset(token)


async def test_attachment_pagination_and_project_storage_quota(project_env, monkeypatch):
    app, factory, store = project_env
    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
    monkeypatch.setattr(project_routes, "PROJECT_STORAGE_MAX_BYTES", len(pdf) * 2)

    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        item = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={"title": "Quota", "stage_id": created["stages"][0]["id"]},
            )
        ).json()["item"]
        upload_url = f"/api/projects/{project_id}/items/{item['id']}/attachments"
        for index in range(2):
            response = await client.post(
                upload_url,
                headers=_headers("alice"),
                files={"file": (f"design-{index}.pdf", pdf, "application/pdf")},
            )
            assert response.status_code == 201, response.text

        first_page = await client.get(
            upload_url,
            headers=_headers("alice"),
            params={"limit": 1, "offset": 0},
        )
        assert first_page.status_code == 200
        assert first_page.json()["total"] == 2
        assert len(first_page.json()["attachments"]) == 1
        assert first_page.json()["next_offset"] == 1
        second_page = await client.get(
            upload_url,
            headers=_headers("alice"),
            params={"limit": 1, "offset": first_page.json()["next_offset"]},
        )
        assert second_page.json()["total"] == 2
        assert len(second_page.json()["attachments"]) == 1
        assert second_page.json()["next_offset"] is None

        rejected = await client.post(
            upload_url,
            headers=_headers("alice"),
            files={"file": ("over-quota.pdf", pdf, "application/pdf")},
        )
        assert rejected.status_code == 413
        assert "quota" in rejected.json()["detail"].lower()

    db = factory()
    assert db.query(cdb.ProjectAttachment).count() == 2
    db.close()
    assert len([path for path in store.root.rglob("*") if path.is_file()]) == 2


async def test_owner_and_install_attachment_quotas_span_projects(project_env, monkeypatch):
    app, factory, store = project_env
    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
    monkeypatch.setattr(project_routes, "PROJECT_STORAGE_MAX_BYTES", len(pdf) * 10)
    monkeypatch.setattr(project_routes, "PROJECT_OWNER_STORAGE_MAX_BYTES", len(pdf))
    monkeypatch.setattr(project_routes, "PROJECT_GLOBAL_STORAGE_MAX_BYTES", len(pdf) * 10)

    async with _client(app) as client:
        first = await _create_project(client, key="ONE")
        second = await _create_project(client, key="TWO")
        first_item = (
            await client.post(
                f"/api/projects/{first['project']['id']}/items",
                headers=_headers("alice"),
                json={"title": "First", "stage_id": first["stages"][0]["id"]},
            )
        ).json()["item"]
        second_item = (
            await client.post(
                f"/api/projects/{second['project']['id']}/items",
                headers=_headers("alice"),
                json={"title": "Second", "stage_id": second["stages"][0]["id"]},
            )
        ).json()["item"]
        first_upload = await client.post(
            f"/api/projects/{first['project']['id']}/items/{first_item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("first.pdf", pdf, "application/pdf")},
        )
        assert first_upload.status_code == 201, first_upload.text
        owner_rejected = await client.post(
            f"/api/projects/{second['project']['id']}/items/{second_item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("owner-overflow.pdf", pdf, "application/pdf")},
        )
        assert owner_rejected.status_code == 413
        assert "profile" in owner_rejected.json()["detail"].lower()

        monkeypatch.setattr(project_routes, "PROJECT_OWNER_STORAGE_MAX_BYTES", len(pdf) * 10)
        monkeypatch.setattr(project_routes, "PROJECT_GLOBAL_STORAGE_MAX_BYTES", len(pdf))
        global_rejected = await client.post(
            f"/api/projects/{second['project']['id']}/items/{second_item['id']}/attachments",
            headers=_headers("alice"),
            files={"file": ("global-overflow.pdf", pdf, "application/pdf")},
        )
        assert global_rejected.status_code == 413
        assert "restia" in global_rejected.json()["detail"].lower()

    db = factory()
    assert db.query(cdb.ProjectAttachment).count() == 1
    db.close()
    assert len([path for path in store.root.rglob("*") if path.is_file()]) == 1


async def test_project_creation_quota_is_owner_scoped(project_env, monkeypatch):
    app, _, _ = project_env
    monkeypatch.setattr(project_routes, "PROJECT_MAX_PROJECTS_PER_OWNER", 1)
    async with _client(app) as client:
        await _create_project(client)
        rejected = await client.post(
            "/api/projects",
            headers=_headers("alice"),
            json={"name": "Overflow", "key": "OVER"},
        )
        assert rejected.status_code == 409
        assert "project limit" in rejected.json()["detail"].lower()


async def test_transfer_enforces_recipient_project_and_storage_quotas(project_env, monkeypatch):
    app, _, _ = project_env
    pdf = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
    monkeypatch.setattr(project_routes, "PROJECT_STORAGE_MAX_BYTES", len(pdf) * 10)
    monkeypatch.setattr(project_routes, "PROJECT_OWNER_STORAGE_MAX_BYTES", len(pdf) * 10)
    monkeypatch.setattr(project_routes, "PROJECT_GLOBAL_STORAGE_MAX_BYTES", len(pdf) * 10)

    async with _client(app) as client:
        alice_project = await _create_project(client, key="MOVE")
        bob_response = await client.post(
            "/api/projects",
            headers=_headers("bob"),
            json={"name": "Bob's project", "key": "BOB"},
        )
        assert bob_response.status_code == 201, bob_response.text
        bob_project = bob_response.json()

        for owner, created in (("alice", alice_project), ("bob", bob_project)):
            project_id = created["project"]["id"]
            item_response = await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers(owner),
                json={"title": f"{owner} work", "stage_id": created["stages"][0]["id"]},
            )
            assert item_response.status_code == 201, item_response.text
            item_id = item_response.json()["item"]["id"]
            upload = await client.post(
                f"/api/projects/{project_id}/items/{item_id}/attachments",
                headers=_headers(owner),
                files={"file": (f"{owner}.pdf", pdf, "application/pdf")},
            )
            assert upload.status_code == 201, upload.text

        project_id = alice_project["project"]["id"]
        member = await client.post(
            f"/api/projects/{project_id}/members",
            headers=_headers("alice"),
            json={"username": "bob", "role": "editor"},
        )
        assert member.status_code == 201, member.text

        monkeypatch.setattr(project_routes, "PROJECT_MAX_PROJECTS_PER_OWNER", 1)
        project_quota = await client.post(
            f"/api/projects/{project_id}/transfer",
            headers=_headers("alice"),
            json={"username": "bob", "version": alice_project["project"]["version"]},
        )
        assert project_quota.status_code == 409
        assert "project limit" in project_quota.json()["detail"].lower()

        monkeypatch.setattr(project_routes, "PROJECT_MAX_PROJECTS_PER_OWNER", 200)
        monkeypatch.setattr(project_routes, "PROJECT_OWNER_STORAGE_MAX_BYTES", len(pdf))
        storage_quota = await client.post(
            f"/api/projects/{project_id}/transfer",
            headers=_headers("alice"),
            json={"username": "bob", "version": alice_project["project"]["version"]},
        )
        assert storage_quota.status_code == 409
        assert "storage quota" in storage_quota.json()["detail"].lower()


async def test_stage_creation_is_bounded_per_project(project_env, monkeypatch):
    app, _, _ = project_env
    monkeypatch.setattr(project_routes, "PROJECT_MAX_STAGES_PER_PROJECT", 5)
    async with _client(app) as client:
        created = await _create_project(client)
        rejected = await client.post(
            f"/api/projects/{created['project']['id']}/stages",
            headers=_headers("alice"),
            json={"name": "Overflow", "category": "todo"},
        )
        assert rejected.status_code == 409
        assert "stage limit" in rejected.json()["detail"].lower()


async def test_project_template_cannot_bypass_stage_limit(project_env, monkeypatch):
    app, factory, _ = project_env
    monkeypatch.setattr(project_routes, "PROJECT_MAX_STAGES_PER_PROJECT", 3)
    async with _client(app) as client:
        rejected = await client.post(
            "/api/projects",
            headers=_headers("alice"),
            json={"name": "Too many template stages", "key": "CAP", "template": "general"},
        )
    assert rejected.status_code == 409
    assert "stage" in rejected.json()["detail"].lower()
    db = factory()
    assert db.query(cdb.Project).count() == 0
    db.close()


def test_non_sql_quota_mutexes_exist_without_owned_projects(project_env, monkeypatch):
    _, factory, _ = project_env
    monkeypatch.setattr(project_routes, "_is_sqlite", lambda _db: False)
    owner_key = project_routes._owner_quota_lock_key("owner-with-no-projects")
    global_key = project_routes._GLOBAL_PROJECT_STORAGE_LOCK_KEY

    db = factory()
    project_routes._lock_named_quota_keys(db, global_key, owner_key)
    db.commit()
    assert {
        row[0] for row in db.query(cdb.ProjectQuotaLock.key).all()
    } == {global_key, owner_key}

    project = cdb.Project(id="lock-project", owner="alice", key="LOCK", name="Lock cleanup")
    db.add(project)
    db.commit()
    activity_key = project_routes._activity_quota_lock_key(project.id)
    project_routes._lock_named_quota_keys(
        db,
        activity_key,
        project_id=project.id,
    )
    db.commit()
    assert db.query(cdb.ProjectQuotaLock).filter(
        cdb.ProjectQuotaLock.key == activity_key,
        cdb.ProjectQuotaLock.project_id == project.id,
    ).count() == 1
    db.delete(project)
    db.commit()
    assert db.query(cdb.ProjectQuotaLock).filter(
        cdb.ProjectQuotaLock.key == activity_key
    ).count() == 0
    db.close()


async def test_archiving_a_blocker_requires_all_dependent_links_cleared(project_env):
    app, _, _ = project_env
    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        backlog = next(row for row in created["stages"] if row["category"] == "backlog")
        done = next(row for row in created["stages"] if row["category"] == "done")
        blocker = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={"title": "Blocker", "stage_id": backlog["id"]},
            )
        ).json()["item"]
        dependent = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={
                    "title": "Completed dependent",
                    "stage_id": done["id"],
                    "blocked_by_id": blocker["id"],
                },
            )
        ).json()["item"]

        rejected = await client.post(
            f"/api/projects/{project_id}/items/{blocker['id']}/archive",
            headers=_headers("alice"),
            json={"version": blocker["version"]},
        )
        assert rejected.status_code == 409
        cleared = await client.patch(
            f"/api/projects/{project_id}/items/{dependent['id']}",
            headers=_headers("alice"),
            json={"clear_blocked_by": True, "version": dependent["version"]},
        )
        assert cleared.status_code == 200
        archived = await client.post(
            f"/api/projects/{project_id}/items/{blocker['id']}/archive",
            headers=_headers("alice"),
            json={"version": blocker["version"]},
        )
        assert archived.status_code == 200


def test_concurrent_cas_and_wip_admission_are_serialized(project_env):
    app, factory, _ = project_env

    async def seed():
        async with _client(app) as client:
            created = await _create_project(client)
            project_id = created["project"]["id"]
            backlog = next(row for row in created["stages"] if row["category"] == "backlog")
            doing = next(row for row in created["stages"] if row["category"] == "in_progress")
            response = await client.patch(
                f"/api/projects/{project_id}/stages/{doing['id']}",
                headers=_headers("alice"),
                json={"wip_limit": 1},
            )
            assert response.status_code == 200
            items = []
            for title in ("One", "Two", "CAS"):
                response = await client.post(
                    f"/api/projects/{project_id}/items",
                    headers=_headers("alice"),
                    json={"title": title, "stage_id": backlog["id"]},
                )
                assert response.status_code == 201
                items.append(response.json()["item"])
            return project_id, doing["id"], items

    project_id, doing_id, items = asyncio.run(seed())

    move_barrier = Barrier(2)

    def move(item):
        async def request_move():
            async with _client(app) as client:
                response = await client.post(
                    f"/api/projects/{project_id}/items/{item['id']}/move",
                    headers=_headers("alice"),
                    json={"stage_id": doing_id, "version": item["version"]},
                )
                return response.status_code

        move_barrier.wait()
        return asyncio.run(request_move())

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(move, items[:2]))
    assert statuses == [200, 409]
    db = factory()
    assert db.query(cdb.ProjectWorkItem).filter(
        cdb.ProjectWorkItem.project_id == project_id,
        cdb.ProjectWorkItem.stage_id == doing_id,
        cdb.ProjectWorkItem.archived.is_(False),
    ).count() == 1
    db.close()

    cas_barrier = Barrier(2)
    cas_item = items[2]

    def update_title(title):
        async def request_update():
            async with _client(app) as client:
                response = await client.patch(
                    f"/api/projects/{project_id}/items/{cas_item['id']}",
                    headers=_headers("alice"),
                    json={"title": title, "version": cas_item["version"]},
                )
                return response.status_code

        cas_barrier.wait()
        return asyncio.run(request_update())

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(update_title, ("CAS A", "CAS B")))
    assert statuses == [200, 409]
    db = factory()
    cas_row = db.query(cdb.ProjectWorkItem).filter(cdb.ProjectWorkItem.id == cas_item["id"]).one()
    assert cas_row.version == 2
    assert cas_row.title in {"CAS A", "CAS B"}
    db.close()


async def test_single_profile_claims_first_run_projects_without_key_collisions(project_env):
    app, factory, _ = project_env
    app.app.state.auth_manager.users = {"alice": {"is_admin": True}}
    db = factory()
    db.add_all(
        [
            cdb.Project(
                id="alice-existing",
                owner="alice",
                key="ENG",
                name="Current Engineering",
                template="general",
            ),
            cdb.Project(
                id="fallback-conflict",
                owner=project_routes.FALLBACK_PROJECT_OWNER,
                key="ENG",
                name="Earlier Engineering",
                template="general",
            ),
            cdb.Project(
                id="fallback-lab",
                owner=project_routes.FALLBACK_PROJECT_OWNER,
                key="LAB",
                name="Lab Work",
                template="general",
            ),
        ]
    )
    db.commit()
    db.close()

    async with _client(app) as client:
        response = await client.get("/api/projects", headers=_headers("alice"))
    assert response.status_code == 200
    assert len(response.json()["projects"]) == 3
    db = factory()
    claimed = db.query(cdb.Project).order_by(cdb.Project.id).all()
    assert {row.owner for row in claimed} == {"alice"}
    assert len({row.key for row in claimed}) == 3
    db.close()


async def test_multi_profile_install_does_not_auto_claim_first_run_owner(project_env):
    app, factory, _ = project_env
    db = factory()
    db.add(
        cdb.Project(
            id="unclaimed-first-run",
            owner=project_routes.FIRST_RUN_PROJECT_OWNER,
            key="FIRST",
            name="First-run work",
            template="general",
        )
    )
    db.commit()
    db.close()

    async with _client(app) as client:
        response = await client.get("/api/projects", headers=_headers("alice"))
    assert response.status_code == 200
    assert response.json()["projects"] == []
    db = factory()
    assert db.query(cdb.Project).filter(cdb.Project.id == "unclaimed-first-run").one().owner == (
        project_routes.FIRST_RUN_PROJECT_OWNER
    )
    db.close()


async def test_identity_migration_blocks_auth_disabled_project_writes(project_env, monkeypatch):
    app, factory, _ = project_env
    monkeypatch.setenv("AUTH_ENABLED", "false")
    app.app.state.auth_manager._identity_migrations = {"alice", "alicia"}
    async with _client(app) as client:
        response = await client.post(
            "/api/projects",
            json={"name": "Must wait", "key": "WAIT"},
        )
    assert response.status_code == 409
    assert "migration" in response.json()["detail"].lower()
    db = factory()
    assert db.query(cdb.Project).count() == 0
    db.close()


def test_write_session_rejects_stale_bypass_actor_after_profile_rename(project_env):
    app, _, _ = project_env
    app.app.state.auth_manager.users = {"alicia": {"is_admin": True}}
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/projects",
        "headers": [],
        "app": app.app,
        "state": {},
    }
    request = Request(scope)
    token = project_routes._PROJECT_AUTH_CONTEXT.set((request, "alice"))
    try:
        with pytest.raises(HTTPException) as exc_info:
            with project_routes._db_session():
                pass
    finally:
        project_routes._PROJECT_AUTH_CONTEXT.reset(token)
    assert exc_info.value.status_code == 409


async def test_auth_disabled_multi_admin_ownership_fails_loudly(project_env, monkeypatch):
    app, _, _ = project_env
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(project_routes, "EXPLICIT_PROJECT_FALLBACK_OWNER", "")
    app.app.state.auth_manager.users = {
        "alice": {"is_admin": True},
        "bob": {"is_admin": True},
    }
    async with _client(app) as client:
        response = await client.get("/api/projects")
    assert response.status_code == 409
    assert "RESTIA_FALLBACK_OWNER" in response.json()["detail"]


async def test_explicit_fallback_profile_is_never_treated_as_first_run_sentinel(project_env, monkeypatch):
    app, factory, _ = project_env
    monkeypatch.setattr(project_routes, "EXPLICIT_PROJECT_FALLBACK_OWNER", "bob")
    monkeypatch.setattr(project_routes, "FALLBACK_PROJECT_OWNER", "bob")
    db = factory()
    db.add(
        cdb.Project(
            id="bob-owned",
            owner="bob",
            key="BOB",
            name="Bob's project",
            template="general",
        )
    )
    db.commit()
    db.close()

    async with _client(app) as client:
        alice_view = await client.get("/api/projects", headers=_headers("alice"))
        assert alice_view.status_code == 200
        assert alice_view.json()["projects"] == []

        monkeypatch.setenv("AUTH_ENABLED", "false")
        fallback_view = await client.get("/api/projects")
        assert fallback_view.status_code == 200
        assert [row["id"] for row in fallback_view.json()["projects"]] == ["bob-owned"]

    db = factory()
    assert db.query(cdb.Project).filter(cdb.Project.id == "bob-owned").one().owner == "bob"
    db.close()


async def test_legacy_sentinel_profile_is_not_auto_claimed(project_env):
    app, factory, _ = project_env
    app.app.state.auth_manager.users = {
        "alice": {"is_admin": True},
        "owner@localhost": {"is_admin": False},
    }
    db = factory()
    db.add(
        cdb.Project(
            id="legacy-sentinel-owned",
            owner=project_routes.FIRST_RUN_PROJECT_OWNER,
            key="LEG",
            name="Legacy sentinel profile project",
            template="general",
        )
    )
    db.commit()
    db.close()
    async with _client(app) as client:
        response = await client.get("/api/projects", headers=_headers("alice"))
    assert response.status_code == 200
    assert response.json()["projects"] == []
    db = factory()
    assert db.query(cdb.Project).filter(cdb.Project.id == "legacy-sentinel-owned").one().owner == "owner@localhost"
    db.close()


async def test_profile_delete_blocks_owned_projects_then_cleans_membership(project_env, monkeypatch):
    _, factory, _ = project_env
    monkeypatch.setattr(cdb, "SessionLocal", factory)

    class _AuthManager:
        def __init__(self):
            self.users = {
                "admin": {"is_admin": True},
                "alice": {"is_admin": False},
                "bob": {"is_admin": False},
                "charlie": {"is_admin": False},
            }
            self.delete_calls = 0
            self.on_delete = None

        def get_username_for_token(self, token):
            return "admin" if token == "admin-token" else None

        def is_admin(self, username):
            return username == "admin"

        def delete_user(self, username, requesting_user):
            self.delete_calls += 1
            if username not in self.users or requesting_user != "admin":
                return False
            if callable(self.on_delete):
                self.on_delete(username)
            self.users.pop(username)
            return True

    db = factory()
    project = cdb.Project(
        id="owned-project",
        owner="alice",
        key="OWN",
        name="Owned",
        template="general",
    )
    stage = cdb.ProjectStage(
        id="owned-stage",
        project_id=project.id,
        name="To Do",
        category="todo",
        position=0,
    )
    db.add_all([project, stage])
    db.commit()
    db.close()

    manager = _AuthManager()
    auth_app = FastAPI()
    auth_app.state.auth_manager = manager
    auth_app.include_router(setup_auth_routes(manager))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=auth_app, client=_PEER),
        base_url="http://auth.test",
        cookies={SESSION_COOKIE: "admin-token"},
    ) as client:
        blocked = await client.request("DELETE", "/api/auth/profiles", json={"username": "alice"})
        assert blocked.status_code == 409
        assert manager.delete_calls == 0

        db = factory()
        project = db.query(cdb.Project).filter(cdb.Project.id == "owned-project").one()
        project.owner = "bob"
        db.add(
            cdb.ProjectMember(
                project_id=project.id,
                username="alice",
                role="editor",
                added_by="bob",
            )
        )
        db.add(
            cdb.ProjectWorkItem(
                id="assigned-item",
                project_id=project.id,
                stage_id="owned-stage",
                item_number=1,
                title="Assigned",
                reporter="bob",
                assignee="alice",
            )
        )
        db.add(
            cdb.ProjectQuotaLock(
                key=cdb.project_owner_quota_lock_key("alice"),
            )
        )
        db.commit()
        db.close()

        deleted = await client.request("DELETE", "/api/auth/profiles", json={"username": "alice"})
        assert deleted.status_code == 200, deleted.text
        assert manager.delete_calls == 1

        manager._identity_migrations = {"charlie"}
        target_renaming = await client.request(
            "DELETE", "/api/auth/profiles", json={"username": "charlie"}
        )
        assert target_renaming.status_code == 409
        assert manager.delete_calls == 1
        manager._identity_migrations = {"admin"}
        admin_renaming = await client.request(
            "DELETE", "/api/auth/profiles", json={"username": "charlie"}
        )
        assert admin_renaming.status_code == 409
        assert manager.delete_calls == 1
        manager._identity_migrations = set()

        def create_raced_project(_username):
            raced_db = factory()
            raced_db.add_all(
                [
                    cdb.Project(
                        id="admin-race-key",
                        owner="admin",
                        key="RACE",
                        name="Existing admin project",
                        template="general",
                    ),
                    cdb.Project(
                        id="raced-project",
                        owner="charlie",
                        key="RACE",
                        name="Raced one",
                        template="general",
                    ),
                    cdb.Project(
                        id="raced-project-2",
                        owner="charlie",
                        key="RACE2",
                        name="Raced two",
                        template="general",
                    ),
                    cdb.ProjectQuotaLock(
                        key=cdb.project_owner_quota_lock_key("charlie"),
                    ),
                ]
            )
            raced_db.commit()
            raced_db.close()

        manager.on_delete = create_raced_project
        raced_delete = await client.request("DELETE", "/api/auth/profiles", json={"username": "charlie"})
        assert raced_delete.status_code == 200, raced_delete.text

    db = factory()
    assert db.query(cdb.ProjectMember).filter(cdb.ProjectMember.username == "alice").count() == 0
    cleaned_item = db.query(cdb.ProjectWorkItem).filter(cdb.ProjectWorkItem.id == "assigned-item").one()
    assert cleaned_item.assignee is None
    assert cleaned_item.version == 2
    raced_projects = db.query(cdb.Project).filter(
        cdb.Project.id.in_(["raced-project", "raced-project-2"])
    ).all()
    assert {row.owner for row in raced_projects} == {"admin"}
    assert len({row.key for row in raced_projects}) == 2
    assert "RACE" not in {row.key for row in raced_projects}
    assert db.query(cdb.ProjectActivity).filter(
        cdb.ProjectActivity.project_id.in_(["raced-project", "raced-project-2"]),
        cdb.ProjectActivity.event_type == "project_owner_recovered",
    ).count() == 2
    assert db.query(cdb.ProjectQuotaLock).filter(
        cdb.ProjectQuotaLock.key.in_(
            [
                cdb.project_owner_quota_lock_key("alice"),
                cdb.project_owner_quota_lock_key("charlie"),
            ]
        )
    ).count() == 0
    db.close()


async def test_activity_retention_is_bounded_under_comment_churn(project_env, monkeypatch):
    app, factory, _ = project_env
    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        item = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={"title": "Churn", "stage_id": created["stages"][0]["id"]},
            )
        ).json()["item"]
        monkeypatch.setattr(project_routes, "PROJECT_MAX_ACTIVITY_PER_PROJECT", 3)

        for index in range(5):
            added = await client.post(
                f"/api/projects/{project_id}/items/{item['id']}/comments",
                headers=_headers("alice"),
                json={"body": f"Temporary note {index}"},
            )
            assert added.status_code == 201, added.text
            removed = await client.delete(
                f"/api/projects/{project_id}/items/{item['id']}/comments/"
                f"{added.json()['comment']['id']}",
                headers=_headers("alice"),
            )
            assert removed.status_code == 200, removed.text

        activity = await client.get(
            f"/api/projects/{project_id}/activity",
            headers=_headers("alice"),
        )
        assert activity.status_code == 200
        assert len(activity.json()["activity"]) == 3
        assert activity.json()["activity"][0]["event_type"] == "comment_deleted"

    db = factory()
    assert db.query(cdb.ProjectActivity).filter(
        cdb.ProjectActivity.project_id == project_id
    ).count() == 3
    db.close()


async def test_stage_mutations_do_not_load_work_item_descriptions(project_env):
    app, factory, _ = project_env
    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        source = created["stages"][0]
        destination = created["stages"][1]
        for index in range(2):
            response = await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={
                    "title": f"Large work item {index}",
                    "description": "x" * 20_000,
                    "stage_id": source["id"],
                },
            )
            assert response.status_code == 201, response.text

        statements: list[str] = []
        engine = factory.kw["bind"]

        def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            updated = await client.patch(
                f"/api/projects/{project_id}/stages/{source['id']}",
                headers=_headers("alice"),
                json={"category": "done"},
            )
            assert updated.status_code == 200, updated.text
            deleted = await client.delete(
                f"/api/projects/{project_id}/stages/{source['id']}",
                headers=_headers("alice"),
                params={"move_to_stage_id": destination["id"]},
            )
            assert deleted.status_code == 200, deleted.text
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)

    item_loads = [
        statement.lower()
        for statement in statements
        if "select project_work_items." in statement.lower()
        and "from project_work_items" in statement.lower()
        and "count(" not in statement.lower()
    ]
    assert item_loads
    assert all("project_work_items.description" not in statement for statement in item_loads)


async def test_item_offset_pagination_has_a_unique_tie_breaker(project_env):
    app, factory, _ = project_env
    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        stage_id = created["stages"][0]["id"]
        tied_at = datetime(2030, 1, 1, 0, 0, 0)
        db = factory()
        for item_id, item_number in (("item-b", 101), ("item-a", 100)):
            db.add(
                cdb.ProjectWorkItem(
                    id=item_id,
                    project_id=project_id,
                    stage_id=stage_id,
                    item_number=item_number,
                    title=item_id,
                    description="large" * 1_000,
                    reporter="alice",
                    position=4,
                    archived=True,
                    created_at=tied_at,
                    updated_at=tied_at,
                )
            )
        db.commit()
        db.close()

        first = await client.get(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            params={"archived": True, "limit": 1, "offset": 0},
        )
        second = await client.get(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            params={"archived": True, "limit": 1, "offset": 1},
        )
        assert first.status_code == second.status_code == 200
        assert [first.json()["items"][0]["id"], second.json()["items"][0]["id"]] == [
            "item-a",
            "item-b",
        ]


async def test_activity_composite_cursor_does_not_skip_timestamp_ties(project_env):
    app, factory, _ = project_env
    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        tied_at = datetime(2030, 1, 1, 0, 0, 0)
        db = factory()
        for index in range(5):
            db.add(
                cdb.ProjectActivity(
                    id=f"tie-{index}",
                    project_id=project_id,
                    actor="alice",
                    event_type="test_tie",
                    summary=f"Tie {index}",
                    payload={},
                    created_at=tied_at,
                )
            )
        db.commit()
        db.close()

        seen = []
        before = None
        for _ in range(3):
            params = {"limit": 2}
            if before:
                params["before"] = before
            response = await client.get(
                f"/api/projects/{project_id}/activity",
                headers=_headers("alice"),
                params=params,
            )
            assert response.status_code == 200
            payload = response.json()
            seen.extend(row["id"] for row in payload["activity"] if row["event_type"] == "test_tie")
            before = payload["next_before"]
        assert len(seen) == 5
        assert len(set(seen)) == 5


async def test_comment_detail_and_pagination_retrieve_all_timestamp_ties(project_env, monkeypatch):
    app, factory, _ = project_env
    monkeypatch.setattr(project_routes, "ITEM_DETAIL_COMMENT_LIMIT", 2)
    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        item = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={"title": "Discuss", "stage_id": created["stages"][0]["id"]},
            )
        ).json()["item"]
        tied_at = datetime(2030, 1, 1, 0, 0, 0)
        db = factory()
        for index in range(5):
            db.add(
                cdb.ProjectComment(
                    id=f"comment-tie-{index}",
                    work_item_id=item["id"],
                    author="alice",
                    body=f"Comment {index}",
                    created_at=tied_at,
                )
            )
        db.commit()
        db.close()

        detail = await client.get(
            f"/api/projects/{project_id}/items/{item['id']}",
            headers=_headers("alice"),
        )
        assert detail.status_code == 200, detail.text
        payload = detail.json()
        assert payload["comments_total"] == 5
        assert payload["comments_truncated"] is True
        seen = [row["id"] for row in payload["comments"]]
        before = payload["comments_next_before"]

        while before:
            page = await client.get(
                f"/api/projects/{project_id}/items/{item['id']}/comments",
                headers=_headers("alice"),
                params={"limit": 2, "before": before},
            )
            assert page.status_code == 200, page.text
            page_payload = page.json()
            seen.extend(row["id"] for row in page_payload["comments"])
            before = page_payload["next_before"]

        assert len(seen) == 5
        assert set(seen) == {f"comment-tie-{index}" for index in range(5)}


async def test_compact_board_pagination_and_work_row_quotas(project_env, monkeypatch):
    app, _, _ = project_env
    monkeypatch.setattr(project_routes, "PROJECT_MAX_ACTIVE_ITEMS", 2)
    monkeypatch.setattr(project_routes, "PROJECT_MAX_ITEMS", 3)
    monkeypatch.setattr(project_routes, "PROJECT_MAX_CHECKLIST_ITEMS_PER_ITEM", 1)
    monkeypatch.setattr(project_routes, "PROJECT_MAX_COMMENTS_PER_ITEM", 1)

    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        stage_id = created["stages"][0]["id"]

        first = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={"title": "Large specification", "description": "x" * 20_000, "stage_id": stage_id},
            )
        ).json()["item"]
        second = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={"title": "Second", "stage_id": stage_id},
            )
        ).json()["item"]
        active_overflow = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            json={"title": "Too many active", "stage_id": stage_id},
        )
        assert active_overflow.status_code == 409

        archived = await client.post(
            f"/api/projects/{project_id}/items/{first['id']}/archive",
            headers=_headers("alice"),
            json={"version": first["version"]},
        )
        assert archived.status_code == 200
        third = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            json={"title": "Third", "stage_id": stage_id},
        )
        assert third.status_code == 201

        await client.post(
            f"/api/projects/{project_id}/items/{second['id']}/archive",
            headers=_headers("alice"),
            json={"version": second["version"]},
        )
        retained_overflow = await client.post(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            json={"title": "Too many retained", "stage_id": stage_id},
        )
        assert retained_overflow.status_code == 409

        board = await client.get(
            f"/api/projects/{project_id}/board?include_archived=true",
            headers=_headers("alice"),
        )
        assert board.status_code == 200, board.text
        assert len(board.json()["items"]) == 3
        assert all("description" not in item for item in board.json()["items"])
        detail = await client.get(
            f"/api/projects/{project_id}/items/{first['id']}",
            headers=_headers("alice"),
        )
        assert detail.json()["item"]["description"] == "x" * 20_000

        page_one = await client.get(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            params={"archived": True, "limit": 1},
        )
        assert page_one.status_code == 200
        assert page_one.json()["total"] == 2
        assert page_one.json()["next_offset"] == 1
        page_two = await client.get(
            f"/api/projects/{project_id}/items",
            headers=_headers("alice"),
            params={"archived": True, "limit": 1, "offset": 1},
        )
        assert page_two.json()["next_offset"] is None

        active_item = third.json()["item"]
        checklist = await client.post(
            f"/api/projects/{project_id}/items/{active_item['id']}/checklist",
            headers=_headers("alice"),
            json={"text": "First step"},
        )
        assert checklist.status_code == 201
        assert (
            await client.post(
                f"/api/projects/{project_id}/items/{active_item['id']}/checklist",
                headers=_headers("alice"),
                json={"text": "Overflow step"},
            )
        ).status_code == 409
        comment = await client.post(
            f"/api/projects/{project_id}/items/{active_item['id']}/comments",
            headers=_headers("alice"),
            json={"body": "First comment"},
        )
        assert comment.status_code == 201
        assert (
            await client.post(
                f"/api/projects/{project_id}/items/{active_item['id']}/comments",
                headers=_headers("alice"),
                json={"body": "Overflow comment"},
            )
        ).status_code == 409


async def test_archived_blocker_must_be_restored_before_dependent(project_env):
    app, _, _ = project_env
    async with _client(app) as client:
        created = await _create_project(client)
        project_id = created["project"]["id"]
        stage_id = created["stages"][0]["id"]
        blocker = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={"title": "Blocker", "stage_id": stage_id},
            )
        ).json()["item"]
        dependent = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=_headers("alice"),
                json={"title": "Dependent", "stage_id": stage_id},
            )
        ).json()["item"]
        dependent = (
            await client.patch(
                f"/api/projects/{project_id}/items/{dependent['id']}",
                headers=_headers("alice"),
                json={"blocked_by_id": blocker["id"], "version": dependent["version"]},
            )
        ).json()["item"]
        dependent = (
            await client.post(
                f"/api/projects/{project_id}/items/{dependent['id']}/archive",
                headers=_headers("alice"),
                json={"version": dependent["version"]},
            )
        ).json()["item"]
        blocker = (
            await client.post(
                f"/api/projects/{project_id}/items/{blocker['id']}/archive",
                headers=_headers("alice"),
                json={"version": blocker["version"]},
            )
        ).json()["item"]

        rejected = await client.post(
            f"/api/projects/{project_id}/items/{dependent['id']}/restore",
            headers=_headers("alice"),
            json={"version": dependent["version"]},
        )
        assert rejected.status_code == 409
        restored_blocker = await client.post(
            f"/api/projects/{project_id}/items/{blocker['id']}/restore",
            headers=_headers("alice"),
            json={"version": blocker["version"]},
        )
        assert restored_blocker.status_code == 200
        restored_dependent = await client.post(
            f"/api/projects/{project_id}/items/{dependent['id']}/restore",
            headers=_headers("alice"),
            json={"version": dependent["version"]},
        )
        assert restored_dependent.status_code == 200
