from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import (
    Account,
    Base,
    CalendarCal,
    CalendarEvent,
    LifeEntity,
    PlanningItem,
    ProgressionEvent,
)
from routes.planning_routes import setup_planning_routes
from routes.progression_routes import setup_progression_routes
from src.auth_helpers import DEFAULT_LOCAL_OWNER, _local_owner_from_env


@pytest.fixture()
def planning_env(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    app = FastAPI()

    @app.middleware("http")
    async def identity(request: Request, call_next):
        request.state.current_user = request.headers.get("x-user") or "alice"
        return await call_next(request)

    app.include_router(setup_planning_routes(session_factory=factory))
    yield app, factory
    engine.dispose()


def test_local_owner_fallback_ignores_blank_environment_aliases(monkeypatch):
    monkeypatch.setenv("RESTIA_FALLBACK_OWNER", "   ")
    monkeypatch.setenv("ODYSSEUS_FALLBACK_OWNER", "  Alice@Example.Test  ")
    assert _local_owner_from_env() == "alice@example.test"
    monkeypatch.setenv("ODYSSEUS_FALLBACK_OWNER", "   ")
    assert _local_owner_from_env() == "owner@localhost"


@pytest.mark.anyio
async def test_planning_crud_is_versioned_and_owner_scoped(planning_env):
    app, _factory = planning_env
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/planning",
            headers={"x-user": "alice"},
            json={"title": "Submit controls application", "due_date": "2026-07-20"},
        )
        assert created.status_code == 201
        item = created.json()
        assert item["version"] == 1
        assert item["status"] == "open"

        bob = await client.get("/api/planning", headers={"x-user": "bob"})
        assert bob.status_code == 200
        assert bob.json()["items"] == []
        forbidden = await client.post(
            f"/api/planning/{item['id']}/complete",
            headers={"x-user": "bob"},
            json={"version": 1},
        )
        assert forbidden.status_code == 404

        changed = await client.patch(
            f"/api/planning/{item['id']}",
            headers={"x-user": "alice"},
            json={"version": 1, "priority": "high", "details": "Finish the portfolio"},
        )
        assert changed.status_code == 200
        assert changed.json()["version"] == 2
        stale = await client.patch(
            f"/api/planning/{item['id']}",
            headers={"x-user": "alice"},
            json={"version": 1, "title": "Stale overwrite"},
        )
        assert stale.status_code == 409


@pytest.mark.anyio
async def test_planning_schedule_links_one_owned_calendar_event(planning_env):
    app, factory = planning_env
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        item = (
            await client.post(
                "/api/planning",
                headers={"x-user": "alice"},
                json={"title": "Run controls experiment", "priority": "critical"},
            )
        ).json()
        scheduled = await client.post(
            f"/api/planning/{item['id']}/schedule",
            headers={"x-user": "alice"},
            json={
                "version": 1,
                "start": "2026-07-21T03:30:00Z",
                "end": "2026-07-21T04:15:00Z",
                "due_date": "2026-07-21",
                "add_to_calendar": True,
            },
        )
        assert scheduled.status_code == 200, scheduled.text
        payload = scheduled.json()
        assert payload["version"] == 2
        assert payload["calendar_event_uid"]
        assert payload["scheduled_start"] == "2026-07-21T03:30:00Z"

        rescheduled = await client.post(
            f"/api/planning/{item['id']}/schedule",
            headers={"x-user": "alice"},
            json={
                "version": 2,
                "start": "2026-07-22T03:30:00Z",
                "end": "2026-07-22T04:15:00Z",
                "due_date": "2026-07-22",
            },
        )
        assert rescheduled.status_code == 200
        assert rescheduled.json()["calendar_event_uid"] == payload["calendar_event_uid"]

    db = factory()
    try:
        account = db.query(Account).filter(Account.username == "alice").one()
        assert db.query(CalendarCal).filter(
            CalendarCal.owner_id == account.id
        ).count() == 1
        assert db.query(CalendarEvent).count() == 1
        event = db.query(CalendarEvent).one()
        assert event.owner_id == account.id
        assert event.summary == "Run controls experiment"
        assert event.is_utc is True
        projection = db.query(LifeEntity).filter(
            LifeEntity.owner_id == account.id,
            LifeEntity.entity_type == "event",
            LifeEntity.domain_ref_id == event.uid,
        ).one()
        assert projection.properties["event_version"] == event.version
    finally:
        db.close()


@pytest.mark.anyio
async def test_planning_first_completion_awards_xp_once_after_reopen(planning_env):
    app, factory = planning_env
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        item = (
            await client.post(
                "/api/planning",
                headers={"x-user": "alice"},
                json={"title": "Finish prototype"},
            )
        ).json()
        completed = await client.post(
            f"/api/planning/{item['id']}/complete",
            headers={"x-user": "alice"},
            json={"version": 1},
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "completed"
        reopened = await client.post(
            f"/api/planning/{item['id']}/reopen",
            headers={"x-user": "alice"},
            json={"version": 2},
        )
        assert reopened.status_code == 200
        completed_again = await client.post(
            f"/api/planning/{item['id']}/complete",
            headers={"x-user": "alice"},
            json={"version": 3},
        )
        assert completed_again.status_code == 200

    db = factory()
    try:
        planning = db.query(PlanningItem).filter(PlanningItem.id == item["id"]).one()
        assert planning.version == 4
        events = db.query(ProgressionEvent).filter(
            ProgressionEvent.owner == "alice",
            ProgressionEvent.source_type == "todo_item_completed",
        ).all()
        assert len(events) == 1
        assert events[0].xp == 20
    finally:
        db.close()


@pytest.mark.anyio
async def test_auth_disabled_planning_completion_is_visible_in_progression(
    monkeypatch,
):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(
        "src.auth_helpers.configured_single_user_owner", lambda _request=None: None
    )
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    app = FastAPI()
    app.include_router(setup_planning_routes(session_factory=factory))
    app.include_router(setup_progression_routes(session_factory=factory))
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://local-restia.test"
        ) as client:
            created = await client.post(
                "/api/planning", json={"title": "Finish local-first setup"}
            )
            assert created.status_code == 201, created.text
            completed = await client.post(
                f"/api/planning/{created.json()['id']}/complete",
                json={"version": 1},
            )
            assert completed.status_code == 200, completed.text
            progression = await client.get("/api/progression")

        assert progression.status_code == 200, progression.text
        profile = progression.json()
        assert profile["owner"] == DEFAULT_LOCAL_OWNER
        assert profile["profile"]["total_xp"] == 20
        assert profile["recent_events"][0]["source_id"] == created.json()["id"]

        db = factory()
        try:
            assert db.query(PlanningItem).one().owner == DEFAULT_LOCAL_OWNER
            assert db.query(ProgressionEvent).one().owner == DEFAULT_LOCAL_OWNER
        finally:
            db.close()
    finally:
        engine.dispose()
