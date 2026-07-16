from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.note_routes as note_routes
import routes.planner_routes as planner_routes
import routes.study_routes as study_routes
import src.study_mode as study_mode
import src.note_progression as note_progression
from core.database import (
    CalendarCal,
    Note,
    PlanningItem,
    ProgressionEvent,
    Session as DbSession,
    StudyState,
)
from routes.planning_routes import setup_planning_routes
from routes.progression_routes import setup_progression_routes
from src.auth_helpers import DEFAULT_LOCAL_OWNER
from src.note_progression import (
    advance_recurring_note,
    normalize_created_items,
    recurring_advance_values,
)


class _Identity:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope.setdefault("state", {})["current_user"] = "alice"
        await self.app(scope, receive, send)


@pytest.fixture
def hardening_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'progression-hardening.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    yield factory
    engine.dispose()


def _authenticated_note_app(factory, monkeypatch) -> FastAPI:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(note_routes, "SessionLocal", factory)
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True)
    app.include_router(note_routes.setup_note_routes())
    return _Identity(app)


@pytest.mark.anyio
async def test_public_item_id_and_due_date_rotation_cannot_duplicate_xp(
    hardening_db, monkeypatch
):
    app = _authenticated_note_app(hardening_db, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/notes",
            json={
                "title": "Daily controls drill",
                "note_type": "todo",
                "repeat": "daily",
                "due_date": "2026-07-16T09:00",
                "items": [{"id": "client-original", "text": "Tune PID", "done": False}],
            },
        )
        note_id = created.json()["id"]
        assert "_restia_evidence_id" not in json.dumps(created.json())

        completed = await client.put(
            f"/api/notes/{note_id}",
            json={
                "repeat": "daily",
                "due_date": "2030-01-01T09:00",
                "items": [{"id": "client-rotated-1", "text": "Tune PID", "done": True}],
            },
        )
        assert completed.status_code == 200, completed.text

        # The agent/tool path must preserve the same private evidence even
        # though it replaces the public JSON payload and schedule fields.
        from src.tools.notes import do_manage_notes

        monkeypatch.setattr(cdb, "SessionLocal", hardening_db)
        tool_result = await do_manage_notes(
            json.dumps(
                {
                    "action": "update",
                    "id": note_id,
                    "repeat": "none",
                    "due_date": "2040-01-01T09:00",
                    "items": [
                        {"id": "client-rotated-2", "text": "Tune PID", "done": False}
                    ],
                }
            ),
            owner="alice",
        )
        assert tool_result["exit_code"] == 0, tool_result
        completed_again = await client.put(
            f"/api/notes/{note_id}",
            json={
                "repeat": "daily",
                "due_date": "2050-01-01T09:00",
                "items": [{"id": "client-rotated-3", "text": "Tune PID", "done": True}],
            },
        )
        assert completed_again.status_code == 200, completed_again.text

    db = hardening_db()
    try:
        events = db.query(ProgressionEvent).all()
        assert len(events) == 1
        assert events[0].xp == 20
        assert "client-" not in events[0].event_key
        assert "2030-" not in events[0].event_key
        assert "2040-" not in events[0].event_key
        assert "2050-" not in events[0].event_key
    finally:
        db.close()


@pytest.mark.anyio
async def test_server_recurrence_advance_creates_exactly_one_new_cycle(
    hardening_db, monkeypatch
):
    monkeypatch.setattr(
        note_progression,
        "_utcnow",
        lambda: datetime(2026, 7, 16, 12, 0, 0),
    )
    app = _authenticated_note_app(hardening_db, monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = (
            await client.post(
                "/api/notes",
                json={
                    "title": "Daily review",
                    "note_type": "todo",
                    "repeat": "daily",
                    "due_date": "2026-07-16T09:00",
                    "items": [{"id": "browser-id", "text": "Review notes", "done": False}],
                },
            )
        ).json()
        note_id = created["id"]
        assert (
            await client.put(
                f"/api/notes/{note_id}",
                json={"items": [{"id": "first-id", "text": "Review notes", "done": True}]},
            )
        ).status_code == 200

        db = hardening_db()
        note = db.query(Note).filter(Note.id == note_id).one()
        advance_recurring_note(note, "2026-07-17T09:00")
        db.commit()
        db.close()

        monkeypatch.setattr(
            note_progression,
            "_utcnow",
            lambda: datetime(2026, 7, 17, 12, 0, 0),
        )

        assert (
            await client.put(
                f"/api/notes/{note_id}",
                json={"items": [{"id": "second-id", "text": "Review notes", "done": True}]},
            )
        ).status_code == 200
        assert (
            await client.put(
                f"/api/notes/{note_id}",
                json={"items": [{"id": "third-id", "text": "Review notes", "done": False}]},
            )
        ).status_code == 200
        assert (
            await client.put(
                f"/api/notes/{note_id}",
                json={"items": [{"id": "fourth-id", "text": "Review notes", "done": True}]},
            )
        ).status_code == 200

    db = hardening_db()
    try:
        events = db.query(ProgressionEvent).order_by(ProgressionEvent.occurred_at).all()
        assert len(events) == 2
        assert len({event.event_key for event in events}) == 2
    finally:
        db.close()


@pytest.mark.anyio
async def test_future_recurring_cycle_cannot_be_completed_or_advanced_for_xp(
    hardening_db, monkeypatch
):
    app = _authenticated_note_app(hardening_db, monkeypatch)
    due_now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = (
            await client.post(
                "/api/notes",
                json={
                    "title": "One cycle per day",
                    "note_type": "todo",
                    "repeat": "daily",
                    "due_date": due_now,
                    "items": [{"id": "today", "text": "Train", "done": False}],
                },
            )
        ).json()
        note_id = created["id"]
        assert (
            await client.put(
                f"/api/notes/{note_id}",
                json={"items": [{"id": "today", "text": "Train", "done": True}]},
            )
        ).status_code == 200
        advanced = await client.post(f"/api/notes/{note_id}/advance-recurrence")
        assert advanced.status_code == 200, advanced.text

        # Marking tomorrow complete today may update the checklist, but it is
        # not verified progression evidence and cannot rotate another cycle.
        early = await client.put(
            f"/api/notes/{note_id}",
            json={"items": [{"id": "tomorrow", "text": "Train", "done": True}]},
        )
        assert early.status_code == 200, early.text
        blocked = await client.post(f"/api/notes/{note_id}/advance-recurrence")
        assert blocked.status_code == 409
        assert "not due" in blocked.json()["detail"].lower()

    db = hardening_db()
    try:
        events = db.query(ProgressionEvent).all()
        assert len(events) == 1
        assert events[0].xp == 20
    finally:
        db.close()


def test_recurrence_compare_and_swap_rejects_concurrent_repeat_change(hardening_db):
    due = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    items = normalize_created_items(
        [{"id": "row", "text": "Train", "done": True}],
        repeat="daily",
        due_date=due,
    )
    seed = hardening_db()
    seed.add(
        Note(
            id="cas-note",
            owner="alice",
            title="CAS",
            note_type="todo",
            repeat="daily",
            due_date=due,
            items=json.dumps(items),
        )
    )
    seed.commit()
    seed.close()

    first = hardening_db()
    stale = first.query(Note).filter(Note.id == "cas-note").one()
    old_due, old_items, old_repeat = stale.due_date, stale.items, stale.repeat
    advanced_items, advanced_due = recurring_advance_values(
        items_json=old_items,
        repeat=old_repeat,
        due_date=old_due,
        next_due_date="2099-01-02T09:00:00Z",
    )
    second = hardening_db()
    fresh = second.query(Note).filter(Note.id == "cas-note").one()
    fresh.repeat = "none"
    second.commit()
    second.close()

    updated = (
        first.query(Note)
        .filter(
            Note.id == "cas-note",
            Note.due_date == old_due,
            Note.items == old_items,
            Note.repeat == old_repeat,
            Note.archived.is_(False),
        )
        .update(
            {Note.items: advanced_items, Note.due_date: advanced_due},
            synchronize_session=False,
        )
    )
    assert updated == 0
    first.rollback()
    first.close()

    verify = hardening_db()
    try:
        row = verify.query(Note).filter(Note.id == "cas-note").one()
        assert row.repeat == "none"
        assert row.due_date == old_due
        assert row.items == old_items
    finally:
        verify.close()


def _single_profile_app() -> FastAPI:
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(
        is_configured=True,
        users={"alice": {"is_admin": True}},
        is_admin=lambda owner: owner == "alice",
    )
    return app


@pytest.mark.anyio
async def test_auth_disabled_planner_and_legacy_note_xp_use_single_profile(
    hardening_db, monkeypatch, tmp_path
):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(note_routes, "SessionLocal", hardening_db)
    monkeypatch.setattr(planner_routes, "SessionLocal", hardening_db)
    monkeypatch.setattr(planner_routes, "DATA_DIR", tmp_path)

    async def fake_plan(*_args, **_kwargs):
        return {
            "summary": "Ship the prototype",
            "research": [],
            "strategy": "Execute",
            "tasks": [{"text": "Build prototype", "due_in_days": 1}],
            "calendar": [{"summary": "Prototype review", "day_offset": 1, "duration_minutes": 30}],
        }

    monkeypatch.setattr(planner_routes, "_optimize_plan", fake_plan)
    db = hardening_db()
    db.add(
        Note(
            id="legacy-note",
            owner=None,
            title="Legacy task",
            note_type="todo",
            items='[{"id":"legacy-client","text":"Close legacy task","done":false}]',
        )
    )
    db.commit()
    db.close()

    app = _single_profile_app()
    app.include_router(note_routes.setup_note_routes())
    app.include_router(planner_routes.setup_planner_routes())
    app.include_router(setup_planning_routes(session_factory=hardening_db))
    app.include_router(setup_progression_routes(session_factory=hardening_db))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/api/notes")
        assert "legacy-note" in {note["id"] for note in listed.json()["notes"]}
        assert (
            await client.put(
                "/api/notes/legacy-note",
                json={"items": [{"id": "rotated", "text": "Close legacy task", "done": True}]},
            )
        ).status_code == 200
        planned = await client.post(
            "/api/planner/run",
            json={"goal": "Ship prototype", "web_research": False},
        )
        assert planned.status_code == 200, planned.text
        planning_id = planned.json()["planning_item_ids"][0]
        assert (
            await client.post(
                f"/api/planning/{planning_id}/complete", json={"version": 1}
            )
        ).status_code == 200
        progression = (await client.get("/api/progression")).json()

    assert progression["owner"] == "alice"
    assert progression["profile"]["total_xp"] == 40
    db = hardening_db()
    try:
        assert db.query(Note).filter(Note.id == "legacy-note").one().owner is None
        generated = db.query(Note).filter(Note.label == "planner").all()
        assert generated and {note.owner for note in generated} == {"alice"}
        assert db.query(PlanningItem).one().owner == "alice"
        assert db.query(CalendarCal).one().owner == DEFAULT_LOCAL_OWNER
        assert {event.owner for event in db.query(ProgressionEvent).all()} == {"alice"}
    finally:
        db.close()


@pytest.mark.anyio
async def test_auth_disabled_legacy_study_review_xp_uses_single_profile(
    hardening_db, monkeypatch
):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(study_routes, "SessionLocal", hardening_db)
    monkeypatch.setattr(study_mode, "SessionLocal", hardening_db)
    db = hardening_db()
    db.add(
        DbSession(
            id="legacy-study",
            owner=None,
            name="Legacy study",
            endpoint_url="http://model.test/v1/chat/completions",
            model="test-model",
            mode="study",
        )
    )
    db.add(
        StudyState(
            id="legacy-study",
            owner=None,
            goal_text="Master controls",
            target_minutes=600,
            setup_initialized=True,
        )
    )
    db.commit()
    db.close()

    app = _single_profile_app()
    app.include_router(study_routes.setup_study_routes())
    app.include_router(setup_progression_routes(session_factory=hardening_db))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        reviewed = await client.post(
            "/api/study/review?session_id=legacy-study", json={"outcome": "clean"}
        )
        assert reviewed.status_code == 200, reviewed.text
        progression = (await client.get("/api/progression")).json()

    assert progression["owner"] == "alice"
    assert progression["profile"]["total_xp"] == 35
    db = hardening_db()
    try:
        assert db.query(StudyState).one().owner is None
        event = db.query(ProgressionEvent).one()
        assert event.owner == "alice"
        assert event.source_type == "study_review_passed"
    finally:
        db.close()
