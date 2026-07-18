"""Typed human tasks stay canonical, owner-scoped, and separate from automations."""

from __future__ import annotations

import uuid
import threading
from datetime import datetime, timedelta

import pytest
import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import core.database as cdb
from core.database import ActionAudit, LifeEntity, LifeEntityVersion, ScheduledTask
from src.identity import ensure_account
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    create_entity_link,
    create_life_entity,
    task_quality_report,
)
from src.task_record_service import (
    create_task_record,
    delete_task_record,
    get_task_record,
    list_task_records,
    search_task_records,
    task_record_history,
    update_task_record,
)
from routes.task_record_routes import setup_task_record_routes
from routes.life_routes import setup_life_routes
from tests.helpers.sqlite_db import make_temp_sqlite


_Session, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)


def _owner(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _references(db, account):
    goal, _ = create_life_entity(
        db, account=account, entity_type="goal", title="Graduate study",
        provenance={"interface": "test"},
    )
    project, _ = create_life_entity(
        db, account=account, entity_type="project", title="Application pack",
        provenance={"interface": "test"},
    )
    create_entity_link(
        db, account=account, source_id=project.id, relation="supports",
        target_id=goal.id, provenance={"interface": "test"},
    )
    person, _ = create_life_entity(
        db, account=account, entity_type="person", title="Recommender",
        provenance={"interface": "test"},
    )
    document, _ = create_life_entity(
        db, account=account, entity_type="file", title="Resume.pdf",
        provenance={"interface": "test"},
    )
    dependency, _ = create_life_entity(
        db, account=account, entity_type="task", title="Get transcript",
        properties={"next_action": "Request it"},
        provenance={"interface": "test"},
    )
    return goal, project, person, document, dependency


def _task_values(project, person, document, dependency):
    return {
        "title": "Finish application essay",
        "definition_of_done": "Final PDF is reviewed and ready to upload.",
        "effort_minutes": 120,
        "priority": "high",
        "deadline": "2026-07-20T18:00:00+05:30",
        "energy": "high",
        "contexts": ["laptop", "deep_work"],
        "project_id": project.id,
        "people_ids": [person.id],
        "dependency_ids": [dependency.id],
        "document_ids": [document.id],
        "source": {"kind": "manual", "label": "Planning review"},
        "status": "active",
        "next_action": "Rewrite the opening paragraph.",
        "note": "Keep the control-systems story concrete.",
        "provenance": {"interface": "test"},
    }


def test_complete_structured_task_is_canonical_and_not_a_scheduler_row():
    db = _Session()
    try:
        account = ensure_account(db, _owner("task"))
        goal, project, person, document, dependency = _references(db, account)
        task, created = create_task_record(
            db, account=account,
            **_task_values(project, person, document, dependency),
        )
        db.commit()

        payload = get_task_record(db, owner_id=account.id, entity_id=task.id)
        assert created is True
        assert payload["task_schema_version"] == 1
        assert payload["definition_of_done"].startswith("Final PDF")
        assert payload["priority"] == "high"
        assert payload["effort_minutes"] == 120
        assert payload["energy"] == "high"
        assert payload["contexts"] == ["laptop", "deep_work"]
        assert payload["project_id"] == project.id
        assert payload["people_ids"] == [person.id]
        assert payload["dependency_ids"] == [dependency.id]
        assert payload["document_ids"] == [document.id]
        assert payload["source"] == {
            "kind": "manual", "label": "Planning review",
            "external_id": None, "source_id": None,
        }
        assert payload["deadline"] == "2026-07-20T12:30:00Z"
        assert payload["next_action"].startswith("Rewrite")
        assert payload["completion_evidence"] == []
        assert payload["execution_policy"] == {
            "record_only": True,
            "can_execute_external_action": False,
            "scheduled_task_authority": False,
        }
        assert db.query(ScheduledTask).count() == 0

        quality = task_quality_report(
            db, owner_id=account.id, now=datetime(2026, 7, 18), limit=20
        )
        row = next(item for item in quality["items"] if item["entity"]["id"] == task.id)
        assert "goal_disconnected" not in row["flags"]
        assert goal.id != project.id
    finally:
        db.close()


def test_owner_reference_fences_and_secret_executor_payloads_fail_closed():
    db = _Session()
    try:
        alice = ensure_account(db, _owner("alice-task"))
        bob = ensure_account(db, _owner("bob-task"))
        _goal, project, person, document, dependency = _references(db, alice)
        _bgoal, bob_project, _bperson, _bdoc, _bdep = _references(db, bob)
        values = _task_values(project, person, document, dependency)
        values["project_id"] = bob_project.id
        with pytest.raises(LifeGraphNotFound):
            create_task_record(db, account=alice, **values)

        values = _task_values(project, person, document, dependency)
        values["source"] = {
            "kind": "manual", "label": "bad", "password": "must-not-store"
        }
        with pytest.raises(LifeGraphError, match="unsupported fields"):
            create_task_record(db, account=alice, **values)

        values = _task_values(project, person, document, dependency)
        values["completion_evidence"] = [{
            "label": "bad", "executor": {"send_email": True}
        }]
        with pytest.raises(LifeGraphError, match="unsupported fields"):
            create_task_record(db, account=alice, **values)

        assert not any(
            isinstance(row.properties, dict)
            and row.properties.get("task_schema_version") == 1
            for row in db.query(LifeEntity).filter(
                LifeEntity.owner_id == alice.id
            ).all()
        )
    finally:
        db.rollback()
        db.close()


def test_completion_requires_evidence_and_cas_history_delete_are_owner_scoped():
    db = _Session()
    try:
        alice = ensure_account(db, _owner("complete-task"))
        bob = ensure_account(db, _owner("other-task"))
        _goal, project, person, document, dependency = _references(db, alice)
        task, _ = create_task_record(
            db, account=alice,
            **_task_values(project, person, document, dependency),
        )
        db.commit()

        with pytest.raises(LifeGraphError, match="completion_evidence"):
            update_task_record(
                db, account=alice, entity_id=task.id, expected_version=1,
                changes={"status": "completed"},
            )
        db.rollback()
        completed = update_task_record(
            db, account=alice, entity_id=task.id, expected_version=1,
            changes={
                "status": "completed",
                "completion_evidence": [{
                    "label": "Reviewed final PDF", "entity_id": document.id,
                    "recorded_at": "2026-07-18T14:00:00Z",
                }],
            },
        )
        db.commit()
        assert completed.version == 2
        payload = get_task_record(db, owner_id=alice.id, entity_id=task.id)
        assert payload["status"] == "completed"
        assert payload["completed_at"]
        assert payload["completion_evidence"][0]["entity_id"] == document.id

        with pytest.raises(LifeGraphConflict):
            update_task_record(
                db, account=alice, entity_id=task.id, expected_version=1,
                changes={"note": "stale"},
            )
        db.rollback()
        with pytest.raises(LifeGraphNotFound):
            get_task_record(db, owner_id=bob.id, entity_id=task.id)

        history, truncated = task_record_history(
            db, owner_id=alice.id, entity_id=task.id
        )
        assert truncated is False
        assert [row["version"] for row in history] == [2, 1]
        deleted = delete_task_record(
            db, owner_id=alice.id, entity_id=task.id,
            expected_version=2, reason="Duplicate commitment",
        )
        db.commit()
        assert deleted.deleted_at is not None and deleted.version == 3
        assert db.query(LifeEntityVersion).filter_by(entity_id=task.id).count() == 3
        assert db.query(ActionAudit).filter_by(entity_id=task.id).count() >= 3
    finally:
        db.close()


def test_list_search_waiting_and_blocked_validation_are_bounded():
    db = _Session()
    try:
        account = ensure_account(db, _owner("list-task"))
        _goal, project, person, document, dependency = _references(db, account)
        values = _task_values(project, person, document, dependency)
        first, _ = create_task_record(db, account=account, **values)
        second_values = dict(values)
        second_values.update({
            "title": "Wait for recommendation",
            "status": "waiting",
            "waiting_on": "Professor reply",
            "next_action": "Follow up Friday",
            "idempotency_key": "waiting-task",
        })
        second, _ = create_task_record(db, account=account, **second_values)
        db.commit()

        items, truncated = list_task_records(
            db, owner_id=account.id, status="active", limit=1
        )
        assert [row["id"] for row in items] == [first.id]
        assert truncated is False
        result = search_task_records(
            db, owner_id=account.id, query_text="Professor", limit=10
        )
        assert [row["id"] for row in result["items"]] == [second.id]

        bad = dict(values)
        bad.update({"title": "Blocked without cause", "status": "blocked"})
        bad["dependency_ids"] = []
        bad["waiting_on"] = None
        with pytest.raises(LifeGraphError, match="blocked tasks require"):
            create_task_record(db, account=account, **bad)
    finally:
        db.rollback()
        db.close()


@pytest.mark.asyncio
async def test_strict_task_api_is_owner_scoped_and_separate_from_automation(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'task-api.db'}",
        connect_args={"check_same_thread": False},
    )
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        alice = ensure_account(db, "alice")
        _goal, project, person, document, dependency = _references(db, alice)
        db.commit()
    finally:
        db.close()

    class _Auth:
        def __init__(self):
            self._config_lock = threading.Lock()
            self._identity_migrations: set[str] = set()
            self.retired_usernames: set[str] = set()
            self.users = {"alice": {}, "bob": {}}

        @property
        def is_configured(self):
            return True

    app = FastAPI()
    app.state.auth_manager = _Auth()

    @app.middleware("http")
    async def inject_identity(request, call_next):
        request.state.api_token = False
        request.state.current_user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(setup_life_routes(session_factory=factory))
    app.include_router(setup_task_record_routes(session_factory=factory))
    payload = _task_values(project, person, document, dependency)
    payload["deadline"] = "2026-07-20T18:00:00+05:30"
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            created = await client.post(
                "/api/life/tasks", headers={"x-user": "alice"}, json=payload
            )
            assert created.status_code == 201, created.text
            task = created.json()["task"]
            assert task["execution_policy"]["scheduled_task_authority"] is False

            hidden = await client.get(
                f"/api/life/tasks/{task['id']}", headers={"x-user": "bob"}
            )
            assert hidden.status_code == 404
            updated = await client.patch(
                f"/api/life/tasks/{task['id']}",
                headers={"x-user": "alice"},
                json={"version": 1, "next_action": "Write the final draft."},
            )
            assert updated.status_code == 200, updated.text
            assert updated.json()["task"]["version"] == 2
            stale = await client.patch(
                f"/api/life/tasks/{task['id']}",
                headers={"x-user": "alice"},
                json={"version": 1, "note": "stale"},
            )
            assert stale.status_code == 409
            bypass = await client.post(
                "/api/life/entities",
                headers={"x-user": "alice"},
                json={
                    "entity_type": "task",
                    "title": "Bypass",
                    "properties": {
                        "task_schema_version": 1,
                        "definition_of_done": "Would bypass typed validation",
                        "effort_minutes": 15,
                    },
                },
            )
            assert bypass.status_code == 400
            assert "/api/life/tasks" in bypass.text
    finally:
        engine.dispose()
