from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.note_routes as note_routes
import src.note_progression as note_progression
from core.database import Note, ProgressionEvent
from src.progression import (
    award_progression_event,
    build_progression_summary,
    profile_for_xp,
)


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
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'progression.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def test_level_thresholds_do_not_round_up_early():
    assert profile_for_xp(0)["level"] == 1
    assert profile_for_xp(99)["level"] == 1
    assert profile_for_xp(100)["level"] == 2
    assert profile_for_xp(299)["progress_percent"] == 99
    assert profile_for_xp(300)["level"] == 3


def test_awards_are_idempotent_and_owner_scoped(factory):
    db = factory()
    try:
        first, created = award_progression_event(
            db,
            owner="Alice",
            event_key="todo:n1:i1",
            source_type="todo_item_completed",
            source_id="n1:i1",
            title="Ship it",
        )
        db.commit()
        assert created is True
        duplicate, created = award_progression_event(
            db,
            owner="alice",
            event_key="todo:n1:i1",
            source_type="todo_item_completed",
            source_id="n1:i1",
            title="Ship it again",
        )
        assert created is False
        assert duplicate.id == first.id

        _, created = award_progression_event(
            db,
            owner="bob",
            event_key="todo:n1:i1",
            source_type="todo_item_completed",
            source_id="n1:i1",
            title="Bob's clear",
        )
        db.commit()
        assert created is True
        assert db.query(ProgressionEvent).count() == 2
    finally:
        db.close()


def test_summary_builds_quests_streaks_and_achievements(factory):
    now = datetime(2026, 7, 16, 12, tzinfo=timezone.utc)
    db = factory()
    try:
        for index, days_ago in enumerate((2, 1, 0)):
            award_progression_event(
                db,
                owner="alice",
                event_key=f"todo:n:{index}",
                source_type="todo_item_completed",
                source_id=f"n:{index}",
                title=f"Clear {index}",
                occurred_at=(now - timedelta(days=days_ago)).replace(tzinfo=None),
            )
        award_progression_event(
            db,
            owner="alice",
            event_key="project:p1:w1",
            source_type="project_work_item_completed",
            source_id="w1",
            title="Finish task",
            occurred_at=now.replace(tzinfo=None),
        )
        award_progression_event(
            db,
            owner="alice",
            event_key="project:p1:complete",
            source_type="project_completed",
            source_id="p1",
            title="Finish project",
            occurred_at=now.replace(tzinfo=None),
        )
        db.commit()
    finally:
        db.close()

    summary = build_progression_summary(
        owner="alice", session_factory=factory, now=now, utc_offset_minutes=0
    )
    assert summary["profile"]["total_xp"] == 310
    assert summary["profile"]["level"] == 3
    assert summary["today"]["clears"] == 3
    assert summary["today"]["quests_complete"] == 3
    assert summary["streak"] == {
        "current_days": 3,
        "longest_days": 3,
        "history_truncated": False,
    }
    assert {row["id"] for row in summary["achievements"] if row["unlocked"]} >= {
        "awakening",
        "project-breaker",
        "three-day-streak",
    }


@pytest.mark.asyncio
async def test_todo_false_to_true_awards_once_even_after_reopen(factory, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(note_routes, "SessionLocal", factory)
    db = factory()
    try:
        db.add(
            Note(
                id="note-1",
                owner="alice",
                title="Launch",
                note_type="todo",
                items='[{"id":"step-1","text":"Run checks","done":false}]',
            )
        )
        db.commit()
    finally:
        db.close()

    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True)
    app.include_router(note_routes.setup_note_routes())
    transport = httpx.ASGITransport(app=_Identity(app), client=("203.0.113.5", 443))
    async with httpx.AsyncClient(transport=transport, base_url="http://restia.test") as client:
        headers = {"x-test-user": "alice"}
        completed = await client.put(
            "/api/notes/note-1",
            headers=headers,
            json={"items": [{"id": "step-1", "text": "Run checks", "done": True}]},
        )
        assert completed.status_code == 200
        assert completed.json()["items"][0]["completed_at"].endswith("Z")
        assert not any(
            key.startswith("_restia_") for key in completed.json()["items"][0]
        )
        assert (
            await client.put(
                "/api/notes/note-1",
                headers=headers,
                json={
                    "due_date": "2099-02-01T09:00:00Z",
                    "repeat": "daily",
                    "items": [{"id": "rotated-2", "text": "Run checks", "done": False}],
                },
            )
        ).status_code == 200
        assert (
            await client.put(
                "/api/notes/note-1",
                headers=headers,
                json={"items": [{"id": "rotated-3", "text": "Run checks", "done": True}]},
            )
        ).status_code == 200

    db = factory()
    try:
        rows = db.query(ProgressionEvent).filter(ProgressionEvent.owner == "alice").all()
        assert len(rows) == 1
        assert rows[0].source_type == "todo_item_completed"
        assert rows[0].xp == 20
    finally:
        db.close()


@pytest.mark.asyncio
async def test_recurring_todo_cycle_is_server_owned_and_due_date_is_not_an_xp_key(
    factory, monkeypatch
):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(note_routes, "SessionLocal", factory)
    monkeypatch.setattr(
        note_progression,
        "_utcnow",
        lambda: datetime(2026, 7, 16, 12, 0, 0),
    )
    db = factory()
    try:
        db.add(
            Note(
                id="habit-1",
                owner="alice",
                title="Daily controls",
                note_type="todo",
                repeat="daily",
                due_date="2026-07-16T09:00:00Z",
                items='[{"id":"public-1","text":"Run drill","done":false}]',
            )
        )
        db.commit()
    finally:
        db.close()

    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True)
    app.include_router(note_routes.setup_note_routes())
    transport = httpx.ASGITransport(app=_Identity(app), client=("203.0.113.5", 443))
    async with httpx.AsyncClient(transport=transport, base_url="http://restia.test") as client:
        headers = {"x-test-user": "alice"}
        first = await client.put(
            "/api/notes/habit-1",
            headers=headers,
            json={"items": [{"id": "rotated-a", "text": "Run drill", "done": True}]},
        )
        assert first.status_code == 200, first.text

        # Arbitrary due-date/public-id edits and a reopen do not mint another
        # occurrence. Only the server-owned advance endpoint rotates the key.
        reopened = await client.put(
            "/api/notes/habit-1",
            headers=headers,
            json={
                "due_date": "2099-06-01T09:00:00Z",
                "items": [{"id": "rotated-b", "text": "Run drill", "done": False}],
            },
        )
        assert reopened.status_code == 200, reopened.text
        duplicate = await client.put(
            "/api/notes/habit-1",
            headers=headers,
            json={"items": [{"id": "rotated-c", "text": "Run drill", "done": True}]},
        )
        assert duplicate.status_code == 200, duplicate.text
        advanced = await client.post(
            "/api/notes/habit-1/advance-recurrence", headers=headers
        )
        assert advanced.status_code == 200, advanced.text
        assert advanced.json()["items"][0]["done"] is False
        advanced_due = datetime.fromisoformat(
            advanced.json()["due_date"].replace("Z", "+00:00")
        ).replace(tzinfo=None, hour=12, minute=0, second=0, microsecond=0)
        monkeypatch.setattr(note_progression, "_utcnow", lambda: advanced_due)
        second = await client.put(
            "/api/notes/habit-1",
            headers=headers,
            json={"items": [{"id": "rotated-d", "text": "Run drill", "done": True}]},
        )
        assert second.status_code == 200, second.text

    db = factory()
    try:
        rows = (
            db.query(ProgressionEvent)
            .filter(
                ProgressionEvent.owner == "alice",
                ProgressionEvent.source_type == "todo_item_completed",
            )
            .order_by(ProgressionEvent.occurred_at.asc())
            .all()
        )
        assert len(rows) == 2
        assert len({row.event_key for row in rows}) == 2
        assert all("2099-06-01" not in row.event_key for row in rows)
        raw = json.loads(db.query(Note).filter(Note.id == "habit-1").one().items)
        assert raw[0]["_restia_evidence_id"]
        assert raw[0]["_restia_progression_cycle"]
    finally:
        db.close()
