from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.project_routes as project_routes
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


@pytest.fixture
def env(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'project-progression.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(project_routes, "SessionLocal", factory)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(
        is_configured=True,
        users={"alice": {"is_admin": True}},
        is_admin=lambda user: user == "alice",
    )
    app.include_router(
        project_routes.setup_project_routes(ProjectFileStore(tmp_path / "files"))
    )
    yield _Identity(app), factory
    engine.dispose()


@pytest.mark.asyncio
async def test_project_completion_events_are_verified_and_idempotent(env):
    app, factory = env
    headers = {"x-test-user": "alice"}
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 443))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://projects.test"
    ) as client:
        created_response = await client.post(
            "/api/projects",
            headers=headers,
            json={"name": "Launch V2", "key": "VTX"},
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        project = created["project"]
        project_id = project["id"]
        backlog = next(row for row in created["stages"] if row["category"] == "backlog")
        done = next(row for row in created["stages"] if row["category"] == "done")

        item_response = await client.post(
            f"/api/projects/{project_id}/items",
            headers=headers,
            json={"title": "Verify the release", "stage_id": backlog["id"]},
        )
        assert item_response.status_code == 201
        item = item_response.json()["item"]
        checklist_response = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/checklist",
            headers=headers,
            json={"text": "Run focused tests"},
        )
        checklist = checklist_response.json()["checklist_item"]
        checked = await client.patch(
            f"/api/projects/{project_id}/items/{item['id']}/checklist/{checklist['id']}",
            headers=headers,
            json={"done": True},
        )
        assert checked.status_code == 200

        moved = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/move",
            headers=headers,
            json={"stage_id": done["id"], "version": item["version"]},
        )
        assert moved.status_code == 200, moved.text
        item = moved.json()["item"]
        completed = await client.post(
            f"/api/projects/{project_id}/complete",
            headers=headers,
            json={"version": project["version"]},
        )
        assert completed.status_code == 200, completed.text
        project = completed.json()["project"]

        # Reopen every state and complete it again. Stable event keys ensure
        # the same work cannot mint another reward.
        reopened = await client.post(
            f"/api/projects/{project_id}/reopen",
            headers=headers,
            json={"version": project["version"]},
        )
        project = reopened.json()["project"]
        moved_back = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/move",
            headers=headers,
            json={"stage_id": backlog["id"], "version": item["version"]},
        )
        item = moved_back.json()["item"]
        moved_again = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/move",
            headers=headers,
            json={"stage_id": done["id"], "version": item["version"]},
        )
        assert moved_again.status_code == 200
        assert (
            await client.patch(
                f"/api/projects/{project_id}/items/{item['id']}/checklist/{checklist['id']}",
                headers=headers,
                json={"done": False},
            )
        ).status_code == 200
        assert (
            await client.patch(
                f"/api/projects/{project_id}/items/{item['id']}/checklist/{checklist['id']}",
                headers=headers,
                json={"done": True},
            )
        ).status_code == 200
        completed_again = await client.post(
            f"/api/projects/{project_id}/complete",
            headers=headers,
            json={"version": project["version"]},
        )
        assert completed_again.status_code == 200

    db = factory()
    try:
        rows = (
            db.query(cdb.ProgressionEvent)
            .filter(cdb.ProgressionEvent.owner == "alice")
            .order_by(cdb.ProgressionEvent.source_type.asc())
            .all()
        )
        assert [row.source_type for row in rows] == [
            "project_checklist_completed",
            "project_completed",
            "project_work_item_completed",
        ]
        assert sum(row.xp for row in rows) == 260
    finally:
        db.close()


@pytest.mark.asyncio
async def test_submit_work_to_done_awards_item_completion_once(env):
    app, factory = env
    headers = {"x-test-user": "alice"}
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 443))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://projects.test"
    ) as client:
        created_response = await client.post(
            "/api/projects",
            headers=headers,
            json={"name": "Submit V2", "key": "SUB"},
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        project_id = created["project"]["id"]
        backlog = next(row for row in created["stages"] if row["category"] == "backlog")
        done = next(row for row in created["stages"] if row["category"] == "done")

        item_response = await client.post(
            f"/api/projects/{project_id}/items",
            headers=headers,
            json={"title": "Upload the release proof", "stage_id": backlog["id"]},
        )
        assert item_response.status_code == 201, item_response.text
        item = item_response.json()["item"]

        first_submission = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=headers,
            files={"file": ("release-proof.txt", b"verified\n", "text/plain")},
            data={
                "kind": "deliverable",
                "submission_note": "Ready to ship",
                "transition_stage_id": done["id"],
                "version": str(item["version"]),
            },
        )
        assert first_submission.status_code == 201, first_submission.text
        item = first_submission.json()["item"]
        assert item["stage_id"] == done["id"]
        assert item["completed_at"]

        moved_back = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/move",
            headers=headers,
            json={"stage_id": backlog["id"], "version": item["version"]},
        )
        assert moved_back.status_code == 200, moved_back.text
        item = moved_back.json()["item"]

        second_submission = await client.post(
            f"/api/projects/{project_id}/items/{item['id']}/attachments",
            headers=headers,
            files={"file": ("release-proof-v2.txt", b"verified again\n", "text/plain")},
            data={
                "kind": "deliverable",
                "submission_note": "Ready again",
                "transition_stage_id": done["id"],
                "version": str(item["version"]),
            },
        )
        assert second_submission.status_code == 201, second_submission.text
        assert second_submission.json()["item"]["completed_at"]

    db = factory()
    try:
        rows = (
            db.query(cdb.ProgressionEvent)
            .filter(
                cdb.ProgressionEvent.owner == "alice",
                cdb.ProgressionEvent.source_type == "project_work_item_completed",
            )
            .all()
        )
        assert len(rows) == 1
        assert rows[0].event_key == f"project-item:{item['id']}:completed"
        assert rows[0].source_id == item["id"]
        assert rows[0].xp == 50
        assert rows[0].details["actor"] == "alice"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_bulk_stage_lifecycle_paths_award_each_item_completion_once(env):
    app, factory = env
    headers = {"x-test-user": "alice"}
    transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 443))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://projects.test"
    ) as client:
        created_response = await client.post(
            "/api/projects",
            headers=headers,
            json={"name": "Bulk lifecycle", "key": "BLK"},
        )
        assert created_response.status_code == 201, created_response.text
        created = created_response.json()
        project_id = created["project"]["id"]
        backlog = next(row for row in created["stages"] if row["category"] == "backlog")
        done = next(row for row in created["stages"] if row["category"] == "done")

        category_item = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=headers,
                json={"title": "Complete through category", "stage_id": backlog["id"]},
            )
        ).json()["item"]
        for category in ("done", "backlog", "done"):
            changed = await client.patch(
                f"/api/projects/{project_id}/stages/{backlog['id']}",
                headers=headers,
                json={"category": category},
            )
            assert changed.status_code == 200, changed.text

        temporary = await client.post(
            f"/api/projects/{project_id}/stages",
            headers=headers,
            json={"name": "Temporary", "category": "backlog"},
        )
        assert temporary.status_code == 201, temporary.text
        temporary_id = temporary.json()["stage"]["id"]
        moved_by_delete = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=headers,
                json={"title": "Complete through stage deletion", "stage_id": temporary_id},
            )
        ).json()["item"]
        restored_item = (
            await client.post(
                f"/api/projects/{project_id}/items",
                headers=headers,
                json={"title": "Complete through restore", "stage_id": temporary_id},
            )
        ).json()["item"]
        archived = await client.post(
            f"/api/projects/{project_id}/items/{restored_item['id']}/archive",
            headers=headers,
            json={"version": restored_item["version"]},
        )
        assert archived.status_code == 200, archived.text

        deleted = await client.delete(
            f"/api/projects/{project_id}/stages/{temporary_id}",
            headers=headers,
            params={"move_to_stage_id": done["id"]},
        )
        assert deleted.status_code == 200, deleted.text
        active_after_delete = await client.get(
            f"/api/projects/{project_id}/items/{moved_by_delete['id']}",
            headers=headers,
        )
        assert active_after_delete.json()["item"]["completed_at"]
        archived_after_delete = await client.get(
            f"/api/projects/{project_id}/items/{restored_item['id']}",
            headers=headers,
        )
        archived_payload = archived_after_delete.json()["item"]
        assert archived_payload["archived"] is True
        assert archived_payload["completed_at"] is None

        restored = await client.post(
            f"/api/projects/{project_id}/items/{restored_item['id']}/restore",
            headers=headers,
            json={"version": archived_payload["version"]},
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["item"]["completed_at"]

    db = factory()
    try:
        rows = (
            db.query(cdb.ProgressionEvent)
            .filter(
                cdb.ProgressionEvent.owner == "alice",
                cdb.ProgressionEvent.source_type == "project_work_item_completed",
            )
            .all()
        )
        assert {row.source_id for row in rows} == {
            category_item["id"],
            moved_by_delete["id"],
            restored_item["id"],
        }
        assert len(rows) == 3
        assert sum(row.xp for row in rows) == 150
    finally:
        db.close()
