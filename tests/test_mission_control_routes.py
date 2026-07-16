"""Focused contract, owner-scope, and failure-isolation tests for Mission Control."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.calendar_routes as calendar_routes
import routes.mission_control_routes as mission
from src.identity import ensure_account


_PEER = ("203.0.113.52", 54321)
_NOW = datetime(2026, 7, 15, 6, 30, tzinfo=timezone.utc)


class _Identity:
    """Set the owner state normally populated by Restia's auth middleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            user = dict(scope.get("headers") or []).get(b"x-test-user")
            if user:
                scope.setdefault("state", {})["current_user"] = user.decode()
        await self.app(scope, receive, send)


async def _health(_rag, _memory):
    return {
        "overall": "degraded",
        "timestamp": "ignored",
        "services": [
            {
                "name": "providers",
                "status": "degraded",
                "detail": "contains internal context",
                "meta": {"url": "https://user:password@example.test/?token=secret"},
            },
            {"name": "email", "status": "ok", "detail": "2/2 reachable"},
            {"name": "", "status": "down", "detail": "invalid row"},
        ],
    }


def _build_app(factory, *, health_collector=_health, data_dir: Path | None = None):
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(
        is_configured=True,
        users={"alice": {"is_admin": True}, "bob": {"is_admin": False}},
    )
    app.include_router(mission.setup_mission_control_routes(
        session_factory=factory,
        health_collector=health_collector,
        now_factory=lambda: _NOW,
        data_dir=data_dir or Path("data"),
    ))
    return _Identity(app)


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=_PEER),
        base_url="http://mission-control.test",
    )


def _headers(owner: str = "alice") -> dict[str, str]:
    return {"x-test-user": owner}


def test_app_registers_read_only_mission_control_router_without_llm_calls():
    app_source = Path("app.py").read_text(encoding="utf-8")
    bootstrap_source = Path("src/v2/bootstrap.py").read_text(encoding="utf-8")
    route_source = Path("routes/mission_control_routes.py").read_text(encoding="utf-8")

    assert "v2_feature_registry.install(app)" in app_source
    assert "from routes.mission_control_routes import setup_mission_control_routes" in bootstrap_source
    assert 'FeatureSpec("mission-control"' in bootstrap_source
    assert "@router.get(\"/today\")" in route_source
    assert "@router.post" not in route_source
    assert "llm_call" not in route_source


@pytest.fixture
def mission_env(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("LOCALHOST_BYPASS", raising=False)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mission-control.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    yield _build_app(factory, data_dir=data_dir), factory, data_dir
    engine.dispose()


def _seed_snapshot(factory, data_dir: Path) -> None:
    now = _NOW.replace(tzinfo=None)
    db = factory()
    try:
        alice_cal = cdb.CalendarCal(
            id="cal-alice", owner="alice", name="Alice calendar", color="#123456"
        )
        bob_cal = cdb.CalendarCal(
            id="cal-bob", owner="bob", name="Bob calendar", color="#654321"
        )
        db.add_all([alice_cal, bob_cal])
        db.add_all([
            cdb.CalendarEvent(
                uid="event-local",
                calendar=alice_cal,
                summary="Design review",
                dtstart=datetime(2026, 7, 15, 9, 0),
                dtend=datetime(2026, 7, 15, 10, 0),
                is_utc=False,
                importance="high",
            ),
            # 01:30 in +05:30. This proves UTC rows use the offset-adjusted
            # window instead of the naive local wall-time window.
            cdb.CalendarEvent(
                uid="event-utc-boundary",
                calendar=alice_cal,
                summary="Early lab check",
                dtstart=datetime(2026, 7, 14, 20, 0),
                dtend=datetime(2026, 7, 14, 21, 0),
                is_utc=True,
            ),
            cdb.CalendarEvent(
                uid="event-bob",
                calendar=bob_cal,
                summary="BOB_ONLY_EVENT",
                dtstart=datetime(2026, 7, 15, 9, 0),
                dtend=datetime(2026, 7, 15, 10, 0),
                is_utc=False,
            ),
        ])

        alice_project = cdb.Project(
            id="project-alice", owner="alice", key="APP", name="Applications"
        )
        alice_open = cdb.ProjectStage(
            id="stage-alice-open",
            project=alice_project,
            name="To do",
            category="todo",
            position=0,
        )
        alice_done = cdb.ProjectStage(
            id="stage-alice-done",
            project=alice_project,
            name="Done",
            category="done",
            position=1,
        )
        shared_project = cdb.Project(
            id="project-shared", owner="bob", key="LAB", name="Shared Lab"
        )
        shared_open = cdb.ProjectStage(
            id="stage-shared-open",
            project=shared_project,
            name="In progress",
            category="in_progress",
            position=0,
        )
        private_project = cdb.Project(
            id="project-bob-private", owner="bob", key="BOB", name="BOB_ONLY_PROJECT"
        )
        private_open = cdb.ProjectStage(
            id="stage-bob-open",
            project=private_project,
            name="To do",
            category="todo",
            position=0,
        )
        db.add_all([
            alice_project,
            alice_open,
            alice_done,
            shared_project,
            shared_open,
            private_project,
            private_open,
            cdb.ProjectMember(
                project=shared_project,
                username="alice",
                role="viewer",
                added_by="bob",
            ),
        ])
        db.add_all([
            cdb.ProjectWorkItem(
                id="item-overdue",
                project=alice_project,
                stage=alice_open,
                item_number=1,
                title="Submit transcript",
                priority="medium",
                reporter="alice",
                due_date="2026-07-14",
            ),
            cdb.ProjectWorkItem(
                id="item-high",
                project=alice_project,
                stage=alice_open,
                item_number=2,
                title="Draft SOP",
                priority="high",
                reporter="alice",
                due_date="2026-07-30",
            ),
            cdb.ProjectWorkItem(
                id="item-low-future",
                project=alice_project,
                stage=alice_open,
                item_number=3,
                title="Future low-priority work",
                priority="low",
                reporter="alice",
                due_date="2026-08-30",
            ),
            cdb.ProjectWorkItem(
                id="item-done",
                project=alice_project,
                stage=alice_done,
                item_number=4,
                title="Completed critical work",
                priority="critical",
                reporter="alice",
                completed_at=now - timedelta(days=1),
            ),
            cdb.ProjectWorkItem(
                id="item-shared",
                project=shared_project,
                stage=shared_open,
                item_number=1,
                title="Calibrate shared rig",
                priority="critical",
                reporter="bob",
            ),
            cdb.ProjectWorkItem(
                id="item-bob-private",
                project=private_project,
                stage=private_open,
                item_number=1,
                title="BOB_ONLY_WORK",
                priority="critical",
                reporter="bob",
            ),
        ])

        db.add_all([
            cdb.Session(
                id="study-alice",
                owner="alice",
                name="Controls review",
                endpoint_url="http://model.test/v1/chat/completions",
                model="test-model",
                mode="study",
            ),
            cdb.StudyState(
                id="study-alice",
                owner="alice",
                goal_text="Master state-space control",
                target_minutes=600,
                target_date="2026-07-31",
                total_seconds=1800,
                setup_initialized=True,
                review_level=2,
                review_count=3,
                last_review_result="clean",
                last_reviewed_at=now - timedelta(days=1),
                next_review_at=now - timedelta(minutes=30),
            ),
            cdb.Session(
                id="study-bob",
                owner="bob",
                name="BOB_ONLY_STUDY",
                endpoint_url="http://model.test/v1/chat/completions",
                model="test-model",
                mode="study",
            ),
            cdb.StudyState(
                id="study-bob",
                owner="bob",
                goal_text="BOB_ONLY_GOAL",
                target_minutes=60,
                setup_initialized=True,
                review_level=1,
                review_count=1,
                last_review_result="missed",
                next_review_at=now - timedelta(hours=1),
            ),
        ])

        db.add_all([
            cdb.Note(
                id="goal-note-alice",
                owner="alice",
                title="Build controls portfolio",
                content="private long-form note body that Mission Control must omit",
                note_type="goal",
                items=json.dumps([
                    {"text": "Select the final experiment", "done": True},
                    {"text": "Record the rig response", "done": False},
                    {"text": "Write the analysis", "done": False},
                ]),
                pinned=True,
                due_date="2026-07-20",
            ),
            cdb.Note(
                id="goal-note-complete",
                owner="alice",
                title="Completed goal",
                note_type="goal",
                items=json.dumps([{"text": "Already done", "done": True}]),
            ),
            cdb.Note(
                id="goal-note-bob",
                owner="bob",
                title="BOB_ONLY_NOTE",
                content="BOB_ONLY_NOTE_BODY",
                note_type="goal",
                items=json.dumps([{"text": "BOB_ONLY_STEP", "done": False}]),
            ),
        ])

        failed = cdb.ScheduledTask(
            id="task-failed",
            owner="alice",
            name="Application scan",
            status="active",
            trigger_type="schedule",
            next_run=now + timedelta(hours=4),
        )
        running = cdb.ScheduledTask(
            id="task-running",
            owner="alice",
            name="Lab literature scan",
            status="active",
            trigger_type="schedule",
            next_run=now + timedelta(hours=6),
        )
        scheduled = cdb.ScheduledTask(
            id="task-scheduled",
            owner="alice",
            name="Evening review",
            status="active",
            trigger_type="schedule",
            next_run=now + timedelta(hours=2),
        )
        recovered = cdb.ScheduledTask(
            id="task-recovered",
            owner="alice",
            name="Recovered task",
            status="active",
            trigger_type="schedule",
            next_run=now + timedelta(days=1),
        )
        bob_task = cdb.ScheduledTask(
            id="task-bob",
            owner="bob",
            name="BOB_ONLY_TASK",
            status="active",
            trigger_type="schedule",
            next_run=now + timedelta(hours=1),
        )
        daily_brief = cdb.ScheduledTask(
            id="task-daily-brief",
            owner="alice",
            name="Morning Daily Brief",
            task_type="action",
            action="daily_brief",
            status="active",
            trigger_type="schedule",
            next_run=now + timedelta(days=1),
        )
        bob_daily_brief = cdb.ScheduledTask(
            id="task-daily-brief-bob",
            owner="bob",
            name="BOB_ONLY_DAILY_BRIEF",
            task_type="action",
            action="daily_brief",
            status="active",
            trigger_type="schedule",
            next_run=now + timedelta(days=1),
        )
        spoofed_daily_brief = cdb.ScheduledTask(
            id="task-spoofed-daily-brief",
            owner="alice",
            name="Not actually a Daily Brief action",
            task_type="llm",
            action="daily_brief",
            status="active",
            trigger_type="schedule",
            next_run=now + timedelta(days=1),
        )
        db.add_all([
            failed,
            running,
            scheduled,
            recovered,
            bob_task,
            daily_brief,
            bob_daily_brief,
            spoofed_daily_brief,
        ])
        db.add_all([
            cdb.TaskRun(
                id="run-failed",
                task=failed,
                status="error",
                # An unresolved latest failure remains actionable even when it
                # predates today.
                started_at=now - timedelta(days=2),
                finished_at=now - timedelta(days=2) + timedelta(minutes=1),
                error="private provider failure detail",
            ),
            cdb.TaskRun(
                id="run-running",
                task=running,
                status="running",
                started_at=now - timedelta(days=1),
            ),
            cdb.TaskRun(
                id="run-recovered-error",
                task=recovered,
                status="error",
                started_at=now - timedelta(hours=3),
                finished_at=now - timedelta(hours=2, minutes=59),
                error="old recovered failure",
            ),
            cdb.TaskRun(
                id="run-recovered-success",
                task=recovered,
                status="success",
                started_at=now - timedelta(hours=1),
                finished_at=now - timedelta(minutes=59),
            ),
            cdb.TaskRun(
                id="run-bob",
                task=bob_task,
                status="error",
                started_at=now - timedelta(minutes=5),
                error="BOB_ONLY_ERROR",
            ),
            cdb.TaskRun(
                id="run-daily-brief",
                task=daily_brief,
                status="success",
                started_at=now - timedelta(hours=2),
                finished_at=now - timedelta(hours=2) + timedelta(minutes=1),
                result="Daily brief — Wednesday, July 15, 2026\nCalendar: Design review",
            ),
            cdb.TaskRun(
                id="run-daily-brief-bob",
                task=bob_daily_brief,
                status="success",
                started_at=now - timedelta(minutes=30),
                finished_at=now - timedelta(minutes=29),
                result="BOB_ONLY_BRIEF",
            ),
            cdb.TaskRun(
                id="run-spoofed-daily-brief",
                task=spoofed_daily_brief,
                status="success",
                started_at=now - timedelta(minutes=10),
                finished_at=now - timedelta(minutes=9),
                result="SENSITIVE_ARBITRARY_LLM_RESULT",
            ),
        ])
        db.commit()
    finally:
        db.close()

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "email_urgency_state_alice.json").write_text(
        json.dumps({
            "owner": "alice",
            "per_uid": {
                "account-a:42": {
                    "score": 3,
                    "unread": True,
                    "subject": "Application deadline moved",
                    "from": "Admissions Office <admissions@example.test>",
                    "reason": "Reply needed today",
                    "body": "SENSITIVE_EMAIL_BODY_MUST_NOT_LEAK",
                },
                "account-a:43": {
                    "score": 1,
                    "unread": True,
                    "subject": "Low priority newsletter",
                },
            },
        }),
        encoding="utf-8",
    )


@pytest.mark.anyio
async def test_today_snapshot_is_owner_scoped_deterministic_and_redacted(mission_env):
    app, factory, data_dir = mission_env
    _seed_snapshot(factory, data_dir)

    async with _client(app) as client:
        first = await client.get(
            "/api/mission-control/today?utc_offset_minutes=330",
            headers=_headers(),
        )
        second = await client.get(
            "/api/mission-control/today?utc_offset_minutes=330",
            headers=_headers(),
        )

    assert first.status_code == 200, first.text
    assert first.json() == second.json()
    body = first.json()
    assert body["date"] == "2026-07-15"
    assert body["as_of"] == "2026-07-15T06:30:00Z"
    assert body["utc_offset_minutes"] == 330
    assert body["summary"] == {
        "calendar": 2,
        "project_work": 3,
        "planning": 0,
        "inbox": 0,
        "goals": 1,
        "tasks": 3,
        "study_reviews": 1,
        "important_mail": 1,
        "notes_today": 1,
        "daily_brief": 1,
        "progression": 0,
        "next_actions": 3,
        "health": "degraded",
    }

    sources = body["sources"]
    assert set(sources) == {
        "calendar", "project_work", "planning", "inbox", "goals", "tasks", "study_reviews",
        "important_mail", "notes_today", "daily_brief", "progression", "health"
    }
    assert {row["id"] for row in sources["calendar"]["items"]} == {
        "event-local", "event-utc-boundary"
    }
    project_items = sources["project_work"]["items"]
    assert project_items[0]["id"] == "item-overdue"
    assert {row["id"] for row in project_items} == {
        "item-overdue", "item-high", "item-shared"
    }
    assert sources["goals"]["items"][0] == {
        **sources["goals"]["items"][0],
        "session_id": "study-alice",
        "goal": "Master state-space control",
        "next_step": "Apply the skill to one novel transfer task.",
    }
    assert [row["kind"] for row in sources["tasks"]["items"]] == [
        "failed_run", "running_run", "scheduled"
    ]
    assert sources["study_reviews"]["items"][0]["session_id"] == "study-alice"
    assert sources["important_mail"]["items"] == [{
        "id": "account-a:42",
        "account_id": "account-a",
        "uid": "42",
        "subject": "Application deadline moved",
        "sender": "Admissions Office <admissions@example.test>",
        "score": 3,
        "reason": "Reply needed today",
    }]
    assert sources["notes_today"]["items"][0] == {
        **sources["notes_today"]["items"][0],
        "id": "goal-note-alice",
        "next_step": "Record the rig response",
        "next_step_index": 1,
        "completed_steps": 1,
        "total_steps": 3,
        "progress_percent": 33,
    }
    assert sources["daily_brief"]["items"][0] == {
        "run_id": "run-daily-brief",
        "task_id": "task-daily-brief",
        "task_name": "Morning Daily Brief",
        "generated_at": "2026-07-15T04:31:00Z",
        "content": "Daily brief — Wednesday, July 15, 2026\nCalendar: Design review",
        "content_truncated": False,
    }
    assert [row["id"] for row in body["next_actions"]] == [
        "failed_task:run-failed",
        "overdue_project_work:item-overdue",
        "important_mail:account-a:42",
    ]
    assert len(body["next_actions"]) == mission.NEXT_ACTION_LIMIT
    assert sources["health"] == {
        "status": "degraded",
        "overall": "degraded",
        "services": [
            {"name": "email", "status": "ok"},
            {"name": "providers", "status": "degraded"},
        ],
        "truncated": False,
    }

    rendered = json.dumps(body, sort_keys=True)
    assert "BOB_ONLY" not in rendered
    assert "private provider failure detail" not in rendered
    assert "password" not in rendered
    assert "token=secret" not in rendered
    assert "SENSITIVE_EMAIL_BODY_MUST_NOT_LEAK" not in rendered
    assert "SENSITIVE_ARBITRARY_LLM_RESULT" not in rendered
    assert "private long-form note body" not in rendered


@pytest.mark.anyio
async def test_today_snapshot_requires_auth_and_bounds_timezone_offset(mission_env):
    app, _factory, _data_dir = mission_env
    async with _client(app) as client:
        unauthenticated = await client.get("/api/mission-control/today")
        invalid_offset = await client.get(
            "/api/mission-control/today?utc_offset_minutes=841",
            headers=_headers(),
        )

    assert unauthenticated.status_code == 401
    assert invalid_offset.status_code == 422


@pytest.mark.anyio
async def test_today_inbox_empty_state_is_read_only(mission_env):
    app, factory, _data_dir = mission_env

    async with _client(app) as client:
        response = await client.get(
            "/api/mission-control/today", headers=_headers()
        )

    assert response.status_code == 200
    assert response.json()["sources"]["inbox"] == {
        "status": "ok",
        "items": [],
        "count": 0,
        "truncated": False,
        "unprocessed_count": 0,
        "kinds": {},
        "oldest_at": None,
        "latest_at": None,
    }
    assert response.json()["summary"]["inbox"] == 0

    db = factory()
    try:
        assert db.query(cdb.Account).count() == 0
        assert db.query(cdb.AuthIdentity).count() == 0
    finally:
        db.close()


@pytest.mark.anyio
async def test_today_inbox_is_owner_scoped_and_omits_encrypted_content(mission_env):
    app, factory, _data_dir = mission_env
    db = factory()
    try:
        alice = ensure_account(db, "alice")
        bob = ensure_account(db, "bob")
        db.add_all([
            cdb.InboxItem(
                id="inbox-alice",
                owner_id=alice.id,
                title="Alice action",
                content="ALICE_ENCRYPTED_CONTENT_MUST_NOT_LEAK",
                kind="task",
                status="inbox",
                classification_confidence=91,
                classification_reason="action language",
                created_at=datetime(2026, 7, 14, 5, 0),
                updated_at=datetime(2026, 7, 14, 5, 0),
            ),
            cdb.InboxItem(
                id="inbox-alice-processed",
                owner_id=alice.id,
                title="Already processed",
                content="PROCESSED_CONTENT_MUST_NOT_LEAK",
                kind="note",
                status="processed",
                created_at=datetime(2026, 7, 14, 6, 0),
                updated_at=datetime(2026, 7, 14, 6, 0),
            ),
            cdb.InboxItem(
                id="inbox-bob",
                owner_id=bob.id,
                title="BOB_ONLY_INBOX_TITLE",
                content="BOB_ONLY_INBOX_CONTENT",
                kind="decision",
                status="inbox",
                classification_confidence=88,
                classification_reason="decision language",
                created_at=datetime(2026, 7, 14, 7, 0),
                updated_at=datetime(2026, 7, 14, 7, 0),
            ),
        ])
        db.commit()
    finally:
        db.close()

    async with _client(app) as client:
        alice_response = await client.get(
            "/api/mission-control/today", headers=_headers("alice")
        )
        bob_response = await client.get(
            "/api/mission-control/today", headers=_headers("bob")
        )

    assert alice_response.status_code == 200
    alice_source = alice_response.json()["sources"]["inbox"]
    assert alice_source["count"] == 1
    assert alice_source["unprocessed_count"] == 1
    assert alice_source["kinds"] == {"task": 1}
    assert alice_source["items"] == [{
        "id": "inbox-alice",
        "title": "Alice action",
        "kind": "task",
        "confidence": 91,
        "reason": "action language",
    }]
    assert alice_source["oldest_at"] == "2026-07-14T05:00:00Z"
    assert alice_source["latest_at"] == "2026-07-14T05:00:00Z"
    rendered = json.dumps(alice_response.json(), sort_keys=True)
    assert "ALICE_ENCRYPTED_CONTENT_MUST_NOT_LEAK" not in rendered
    assert "PROCESSED_CONTENT_MUST_NOT_LEAK" not in rendered
    assert "BOB_ONLY" not in rendered

    assert bob_response.status_code == 200
    bob_source = bob_response.json()["sources"]["inbox"]
    assert bob_source["count"] == 1
    assert [row["id"] for row in bob_source["items"]] == ["inbox-bob"]


@pytest.mark.anyio
async def test_today_inbox_preview_and_kind_breakdown_are_bounded(mission_env):
    app, factory, _data_dir = mission_env
    db = factory()
    try:
        alice = ensure_account(db, "alice")
        for index in range(7):
            db.add(cdb.InboxItem(
                id=f"inbox-{index}",
                owner_id=alice.id,
                title=f"Capture {index}",
                content=f"PRIVATE_CAPTURE_CONTENT_{index}",
                kind="task" if index % 2 == 0 else "note",
                status="inbox",
                classification_confidence=70 + index,
                classification_reason=f"reason {index}",
                created_at=datetime(2026, 7, 1, index, 0),
                updated_at=datetime(2026, 7, 1, index, 0),
            ))
        db.commit()
    finally:
        db.close()

    async with _client(app) as client:
        response = await client.get(
            "/api/mission-control/today", headers=_headers()
        )

    assert response.status_code == 200
    source = response.json()["sources"]["inbox"]
    assert source["count"] == 7
    assert source["unprocessed_count"] == 7
    assert source["kinds"] == {"note": 3, "task": 4}
    assert source["truncated"] is True
    assert len(source["items"]) == mission.INBOX_PREVIEW_LIMIT
    assert [row["id"] for row in source["items"]] == [
        "inbox-0", "inbox-1", "inbox-2", "inbox-3", "inbox-4"
    ]
    assert source["oldest_at"] == "2026-07-01T00:00:00Z"
    assert source["latest_at"] == "2026-07-01T06:00:00Z"
    assert "PRIVATE_CAPTURE_CONTENT" not in response.text


@pytest.mark.anyio
async def test_today_snapshot_bounds_calendar_items(mission_env):
    app, factory, _data_dir = mission_env
    db = factory()
    calendar = cdb.CalendarCal(id="many-cal", owner="alice", name="Many")
    db.add(calendar)
    db.add_all([
        cdb.CalendarEvent(
            uid=f"event-{index:02d}",
            calendar=calendar,
            summary=f"Event {index:02d}",
            dtstart=datetime(2026, 7, 15, 0, 0) + timedelta(minutes=index),
            dtend=datetime(2026, 7, 15, 0, 30) + timedelta(minutes=index),
            is_utc=False,
        )
        for index in range(mission.CALENDAR_ITEM_LIMIT + 5)
    ])
    db.commit()
    db.close()

    async with _client(app) as client:
        response = await client.get(
            "/api/mission-control/today?utc_offset_minutes=330", headers=_headers()
        )

    assert response.status_code == 200
    calendar_source = response.json()["sources"]["calendar"]
    assert calendar_source["count"] == mission.CALENDAR_ITEM_LIMIT
    assert calendar_source["truncated"] is True
    assert [row["id"] for row in calendar_source["items"]] == [
        f"event-{index:02d}" for index in range(mission.CALENDAR_ITEM_LIMIT)
    ]


@pytest.mark.anyio
async def test_recurring_calendar_scan_does_not_let_expired_history_hide_today(mission_env):
    app, factory, _data_dir = mission_env
    db = factory()
    calendar = cdb.CalendarCal(id="recurring-cal", owner="alice", name="Recurring")
    db.add(calendar)
    db.add_all([
        cdb.CalendarEvent(
            uid=f"expired-series-{index:03d}",
            calendar=calendar,
            summary="Expired",
            dtstart=datetime(2000, 1, 1) + timedelta(days=index),
            dtend=datetime(2000, 1, 1, 1) + timedelta(days=index),
            is_utc=False,
            rrule="FREQ=DAILY;COUNT=1",
        )
        for index in range(100)
    ])
    db.add(cdb.CalendarEvent(
        uid="current-recurring-series",
        calendar=calendar,
        summary="Current recurring event",
        dtstart=datetime(2026, 7, 15, 8, 0),
        dtend=datetime(2026, 7, 15, 9, 0),
        is_utc=False,
        rrule="FREQ=DAILY;COUNT=1",
    ))
    db.commit()
    db.close()

    async with _client(app) as client:
        response = await client.get("/api/mission-control/today", headers=_headers())

    assert response.status_code == 200
    calendar_source = response.json()["sources"]["calendar"]
    assert [row["id"] for row in calendar_source["items"]] == [
        "current-recurring-series::2026-07-15T08:00"
    ]
    assert calendar_source["truncated"] is False


@pytest.mark.anyio
async def test_recurring_calendar_expansion_is_bounded_per_series(
    monkeypatch, mission_env
):
    app, factory, _data_dir = mission_env
    db = factory()
    calendar = cdb.CalendarCal(id="dense-cal", owner="alice", name="Dense")
    db.add(calendar)
    db.add(cdb.CalendarEvent(
        uid="dense-series",
        calendar=calendar,
        summary="Dense recurring event",
        dtstart=datetime(2026, 7, 15, 0, 0),
        dtend=datetime(2026, 7, 15, 0, 1),
        is_utc=False,
        rrule="FREQ=MINUTELY",
    ))
    db.commit()
    db.close()

    original_expand = mission._expand_rrule
    observed: list[tuple[int | None, int | None, int]] = []

    def observed_expand(*args, **kwargs):
        rows = original_expand(*args, **kwargs)
        observed.append((kwargs.get("limit"), kwargs.get("work_limit"), len(rows)))
        return rows

    monkeypatch.setattr(mission, "_expand_rrule", observed_expand)
    async with _client(app) as client:
        response = await client.get("/api/mission-control/today", headers=_headers())

    assert response.status_code == 200
    source = response.json()["sources"]["calendar"]
    assert source["count"] == mission.CALENDAR_ITEM_LIMIT
    assert source["truncated"] is True
    assert observed == [(
        mission.CALENDAR_ITEM_LIMIT + 1,
        mission._CALENDAR_OCCURRENCE_WORK_LIMIT,
        mission.CALENDAR_ITEM_LIMIT + 1,
    )]


def test_rrule_work_limit_counts_excluded_occurrences_and_reports_truncation():
    calendar = cdb.CalendarCal(id="excluded-cal", owner="alice", name="Excluded")
    event = cdb.CalendarEvent(
        uid="excluded-series",
        calendar=calendar,
        summary="Excluded dense series",
        dtstart=datetime(2026, 7, 15, 0, 0),
        dtend=datetime(2026, 7, 15, 0, 0, 1),
        is_utc=False,
        rrule="FREQ=SECONDLY",
        recurrence_exdates=json.dumps([
            f"2026-07-15T{hour:02d}:{minute:02d}"
            for hour in range(24)
            for minute in range(60)
        ]),
    )

    rows = mission._expand_rrule(
        event,
        datetime(2026, 7, 15),
        datetime(2026, 7, 16),
        limit=mission.CALENDAR_ITEM_LIMIT + 1,
    )

    assert rows == []
    assert rows.truncated is True


def test_old_dense_rrule_is_rebased_before_dateutil_seeks(monkeypatch):
    calendar = cdb.CalendarCal(id="old-dense-cal", owner="alice", name="Old dense")
    event = cdb.CalendarEvent(
        uid="old-dense-series",
        calendar=calendar,
        summary="Old dense series",
        dtstart=datetime(2026, 7, 14, 0, 0),
        dtend=datetime(2026, 7, 14, 0, 0, 1),
        is_utc=False,
        rrule="FREQ=SECONDLY",
    )
    original = calendar_routes.rrulestr
    observed_starts: list[datetime] = []

    def observed_rrulestr(value, *, dtstart):
        observed_starts.append(dtstart)
        return original(value, dtstart=dtstart)

    monkeypatch.setattr(calendar_routes, "rrulestr", observed_rrulestr)
    rows = calendar_routes._expand_rrule(
        event,
        datetime(2026, 7, 15),
        datetime(2026, 7, 16),
        limit=21,
        work_limit=21,
    )

    assert len(rows) == 20
    assert rows.truncated is True
    assert observed_starts == [datetime(2026, 7, 14, 23, 59, 58)]


def test_old_large_count_rrule_fails_bounded_before_dateutil(monkeypatch):
    calendar = cdb.CalendarCal(id="finite-cal", owner="alice", name="Finite")
    event = cdb.CalendarEvent(
        uid="finite-dense-series",
        calendar=calendar,
        summary="Finite dense series",
        dtstart=datetime(2026, 7, 14, 0, 0),
        dtend=datetime(2026, 7, 14, 0, 0, 1),
        is_utc=False,
        rrule="FREQ=SECONDLY;COUNT=100000",
    )

    def must_not_parse(*_args, **_kwargs):
        raise AssertionError("dateutil must not seek a large historical COUNT rule")

    monkeypatch.setattr(calendar_routes, "rrulestr", must_not_parse)
    rows = calendar_routes._expand_rrule(
        event,
        datetime(2026, 7, 15),
        datetime(2026, 7, 16),
        limit=21,
        work_limit=21,
    )

    assert rows == []
    assert rows.truncated is True


@pytest.mark.anyio
async def test_unresolved_failure_is_ranked_before_running_run_limit(mission_env):
    app, factory, _data_dir = mission_env
    now = _NOW.replace(tzinfo=None)
    db = factory()
    failed = cdb.ScheduledTask(
        id="older-failed-task",
        owner="alice",
        name="Older unresolved failure",
        status="active",
        trigger_type="schedule",
        next_run=now + timedelta(days=1),
    )
    db.add(failed)
    db.add(cdb.TaskRun(
        id="older-failed-run",
        task=failed,
        status="error",
        started_at=now - timedelta(days=3),
        finished_at=now - timedelta(days=3) + timedelta(minutes=1),
    ))
    for index in range(mission.TASK_ITEM_LIMIT + 1):
        task = cdb.ScheduledTask(
            id=f"running-task-{index:02d}",
            owner="alice",
            name=f"Running {index:02d}",
            status="active",
            trigger_type="schedule",
            next_run=now + timedelta(days=1),
        )
        db.add(task)
        db.add(cdb.TaskRun(
            id=f"running-run-{index:02d}",
            task=task,
            status="running",
            started_at=now - timedelta(minutes=index),
        ))
    db.commit()
    db.close()

    async with _client(app) as client:
        response = await client.get("/api/mission-control/today", headers=_headers())

    assert response.status_code == 200
    task_source = response.json()["sources"]["tasks"]
    assert task_source["items"][0]["run_id"] == "older-failed-run"
    assert task_source["items"][0]["kind"] == "failed_run"
    assert task_source["count"] == mission.TASK_ITEM_LIMIT
    assert task_source["truncated"] is True


@pytest.mark.anyio
async def test_important_mail_is_bounded_redacted_and_drives_only_three_actions(
    mission_env
):
    app, _factory, data_dir = mission_env
    per_uid = {
        f"account:{index:02d}": {
            "score": 3,
            "unread": True,
            "subject": f"Important {index:02d}",
            "from": "sender@example.test",
            "reason": "Needs a reply",
            "body": f"PRIVATE_MAIL_BODY_{index:02d}",
        }
        for index in range(mission.IMPORTANT_MAIL_ITEM_LIMIT + 5)
    }
    (data_dir / "email_urgency_state_alice.json").write_text(
        json.dumps({"owner": "alice", "per_uid": per_uid}), encoding="utf-8"
    )

    async with _client(app) as client:
        response = await client.get("/api/mission-control/today", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    source = body["sources"]["important_mail"]
    assert source["count"] == mission.IMPORTANT_MAIL_ITEM_LIMIT
    assert source["truncated"] is True
    assert [row["uid"] for row in source["items"]] == [
        f"{index:02d}" for index in range(mission.IMPORTANT_MAIL_ITEM_LIMIT)
    ]
    assert [row["source_id"] for row in body["next_actions"]] == [
        "account:00", "account:01", "account:02"
    ]
    assert len(body["next_actions"]) == mission.NEXT_ACTION_LIMIT
    assert "PRIVATE_MAIL_BODY" not in response.text


@pytest.mark.anyio
async def test_important_mail_slug_collision_cannot_cross_owner_boundary(mission_env):
    app, _factory, data_dir = mission_env
    # Both usernames map to email_urgency_state_a_b.json under the historical
    # scanner filename convention. Embedded exact-owner validation must fail
    # closed instead of returning a/b's private header metadata to a_b.
    (data_dir / "email_urgency_state_a_b.json").write_text(
        json.dumps({
            "owner": "a/b",
            "per_uid": {
                "account:99": {
                    "score": 3,
                    "unread": True,
                    "subject": "CROSS_OWNER_PRIVATE_SUBJECT",
                },
            },
        }),
        encoding="utf-8",
    )

    async with _client(app) as client:
        response = await client.get(
            "/api/mission-control/today", headers=_headers("a_b")
        )

    assert response.status_code == 200
    source = response.json()["sources"]["important_mail"]
    assert source["status"] == "error"
    assert source["items"] == []
    assert "CROSS_OWNER_PRIVATE_SUBJECT" not in response.text


@pytest.mark.anyio
async def test_notes_today_and_daily_brief_are_bounded_without_note_bodies(mission_env):
    app, factory, _data_dir = mission_env
    now = _NOW.replace(tzinfo=None)
    db = factory()
    db.add_all([
        cdb.Note(
            id=f"goal-{index:02d}",
            owner="alice",
            title=f"Goal {index:02d}",
            content=f"PRIVATE_NOTE_BODY_{index:02d}",
            note_type="goal",
            items=json.dumps([{"text": f"Step {index:02d}", "done": False}]),
            sort_order=index,
        )
        for index in range(mission.NOTES_TODAY_ITEM_LIMIT + 2)
    ])
    brief = cdb.ScheduledTask(
        id="bounded-brief",
        owner="alice",
        name="Bounded Daily Brief",
        task_type="action",
        action="daily_brief",
        status="active",
        trigger_type="schedule",
        next_run=now + timedelta(days=1),
    )
    db.add(brief)
    db.add(cdb.TaskRun(
        id="bounded-brief-run",
        task=brief,
        status="success",
        started_at=now - timedelta(minutes=2),
        finished_at=now - timedelta(minutes=1),
        result="B" * (mission._DAILY_BRIEF_CONTENT_LIMIT + 5),
    ))
    db.commit()
    db.close()

    async with _client(app) as client:
        response = await client.get("/api/mission-control/today", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    notes = body["sources"]["notes_today"]
    assert notes["count"] == mission.NOTES_TODAY_ITEM_LIMIT
    assert notes["truncated"] is True
    assert "PRIVATE_NOTE_BODY" not in response.text
    brief_source = body["sources"]["daily_brief"]
    assert brief_source["count"] == 1
    assert brief_source["truncated"] is True
    assert len(brief_source["items"][0]["content"]) == mission._DAILY_BRIEF_CONTENT_LIMIT
    assert brief_source["items"][0]["content_truncated"] is True


@pytest.mark.anyio
async def test_notes_today_omits_oversized_checklists_before_python_loading(mission_env):
    app, factory, _data_dir = mission_env
    db = factory()
    db.add_all([
        cdb.Note(
            id="normal-goal",
            owner="alice",
            title="Normal goal",
            note_type="goal",
            items=json.dumps([{"text": "Safe next step", "done": False}]),
        ),
        cdb.Note(
            id="oversized-goal",
            owner="alice",
            title="Oversized goal",
            content="UNUSED_OVERSIZED_NOTE_BODY",
            note_type="goal",
            items=json.dumps([{
                "text": "X" * (mission._NOTES_TODAY_ITEMS_BYTES + 1),
                "done": False,
            }]),
        ),
    ])
    db.commit()
    db.close()

    async with _client(app) as client:
        response = await client.get("/api/mission-control/today", headers=_headers())

    assert response.status_code == 200
    source = response.json()["sources"]["notes_today"]
    assert [row["id"] for row in source["items"]] == ["normal-goal"]
    assert source["truncated"] is True
    assert "UNUSED_OVERSIZED_NOTE_BODY" not in response.text


@pytest.mark.anyio
async def test_auth_disabled_single_profile_matches_each_source_owner_convention(
    monkeypatch, mission_env
):
    _unused_app, factory, data_dir = mission_env
    monkeypatch.setenv("AUTH_ENABLED", "false")
    now = _NOW.replace(tzinfo=None)
    db = factory()
    local_account = ensure_account(db, "alice")
    calendar = cdb.CalendarCal(
        id="local-calendar",
        owner=mission.CALENDAR_FALLBACK_OWNER,
        name="Local calendar",
    )
    project = cdb.Project(
        id="local-project", owner="alice", key="LOC", name="Local project"
    )
    stage = cdb.ProjectStage(
        id="local-stage", project=project, name="To do", category="todo"
    )
    local_daily_brief = cdb.ScheduledTask(
        id="local-daily-brief",
        owner=None,
        name="Local Daily Brief",
        task_type="action",
        action="daily_brief",
        status="active",
        trigger_type="schedule",
        next_run=now + timedelta(days=1),
    )
    db.add_all([
        calendar,
        cdb.CalendarEvent(
            uid="local-event",
            calendar=calendar,
            summary="Local event",
            dtstart=datetime(2026, 7, 15, 9),
            dtend=datetime(2026, 7, 15, 10),
            is_utc=False,
        ),
        project,
        stage,
        cdb.ProjectWorkItem(
            id="local-item",
            project=project,
            stage=stage,
            item_number=1,
            title="Local project work",
            priority="high",
            reporter="alice",
        ),
        cdb.Session(
            id="local-study",
            owner=None,
            name="Local Study",
            endpoint_url="http://model.test/v1/chat/completions",
            model="test-model",
            mode="study",
        ),
        cdb.StudyState(
            id="local-study",
            owner=None,
            goal_text="Local study goal",
            target_minutes=60,
            setup_initialized=True,
            review_count=1,
            review_level=1,
            last_review_result="missed",
            next_review_at=now - timedelta(minutes=1),
        ),
        cdb.ScheduledTask(
            id="local-task",
            owner=None,
            name="Local scheduled task",
            status="active",
            trigger_type="schedule",
            next_run=now + timedelta(hours=1),
        ),
        cdb.Note(
            id="local-goal-note",
            owner=None,
            title="Local goal",
            note_type="goal",
            items=json.dumps([{"text": "Local next step", "done": False}]),
        ),
        cdb.InboxItem(
            id="local-inbox",
            owner_id=local_account.id,
            title="Local Inbox capture",
            content="LOCAL_INBOX_CONTENT_MUST_NOT_LEAK",
            kind="note",
            status="inbox",
            classification_confidence=60,
            classification_reason="default unstructured note",
            created_at=now - timedelta(hours=2),
            updated_at=now - timedelta(hours=2),
        ),
        local_daily_brief,
        cdb.TaskRun(
            id="local-daily-brief-run",
            task=local_daily_brief,
            status="success",
            started_at=now - timedelta(hours=1),
            finished_at=now - timedelta(minutes=59),
            result="Local persisted brief",
        ),
    ])
    db.commit()
    db.close()

    (data_dir / "email_urgency_state_default.json").write_text(
        json.dumps({
            "owner": "",
            "per_uid": {
                "local-account:7": {
                    "score": 2,
                    "unread": True,
                    "subject": "Local important mail",
                },
            },
        }),
        encoding="utf-8",
    )

    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(
        is_configured=True, users={"alice": {"is_admin": True}}
    )
    app.include_router(mission.setup_mission_control_routes(
        session_factory=factory,
        health_collector=_health,
        now_factory=lambda: _NOW,
        data_dir=data_dir,
    ))
    async with _client(_Identity(app)) as client:
        response = await client.get("/api/mission-control/today")

    assert response.status_code == 200
    assert response.json()["summary"] == {
        "calendar": 1,
        "project_work": 1,
        "planning": 0,
        "inbox": 1,
        "goals": 1,
        "tasks": 1,
        "study_reviews": 1,
        "important_mail": 1,
        "notes_today": 1,
        "daily_brief": 1,
        "progression": 0,
        "next_actions": 3,
        "health": "degraded",
    }
    assert response.json()["sources"]["inbox"]["items"] == [{
        "id": "local-inbox",
        "title": "Local Inbox capture",
        "kind": "note",
        "confidence": 60,
        "reason": "default unstructured note",
    }]
    assert "LOCAL_INBOX_CONTENT_MUST_NOT_LEAK" not in response.text


@pytest.mark.anyio
async def test_source_failures_are_explicit_and_do_not_leak_exception_text(
    monkeypatch, caplog, tmp_path
):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    secret_marker = "credential-bearing-dsn-marker"

    def broken_factory():
        raise RuntimeError(secret_marker)

    app = _build_app(broken_factory, data_dir=tmp_path)
    caplog.set_level(logging.ERROR, logger="routes.mission_control_routes")
    async with _client(app) as client:
        response = await client.get("/api/mission-control/today", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    for source_name in (
        "calendar",
        "project_work",
        "inbox",
        "goals",
        "tasks",
        "study_reviews",
        "notes_today",
        "daily_brief",
    ):
        source = body["sources"][source_name]
        assert source["status"] == "error"
        assert source["error"] == {
            "code": f"{source_name}_unavailable",
            "message": f"Could not load {source_name.replace('_', ' ')}.",
        }
    assert body["sources"]["health"]["overall"] == "degraded"
    assert body["sources"]["important_mail"] == {
        "status": "ok",
        "items": [],
        "count": 0,
        "truncated": False,
    }
    assert secret_marker not in response.text
    assert secret_marker not in caplog.text


@pytest.mark.anyio
async def test_malformed_mail_state_fails_only_that_source_without_leaking_text(
    mission_env, caplog
):
    app, _factory, data_dir = mission_env
    secret_marker = "PRIVATE_PARSE_MARKER"
    (data_dir / "email_urgency_state_alice.json").write_text(
        "{" + secret_marker, encoding="utf-8"
    )
    caplog.set_level(logging.ERROR, logger="routes.mission_control_routes")

    async with _client(app) as client:
        response = await client.get("/api/mission-control/today", headers=_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["sources"]["important_mail"]["status"] == "error"
    assert body["sources"]["important_mail"]["error"] == {
        "code": "important_mail_unavailable",
        "message": "Could not load important mail.",
    }
    for name in (
        "calendar", "project_work", "inbox", "goals", "tasks", "study_reviews",
        "notes_today", "daily_brief",
    ):
        assert body["sources"][name]["status"] == "ok"
    assert secret_marker not in response.text
    assert secret_marker not in caplog.text


@pytest.mark.anyio
async def test_auth_disabled_ambiguous_profiles_fail_closed_per_source(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    def database_must_not_be_opened():
        raise AssertionError("ambiguous ownership must stop before querying")

    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(
        is_configured=True,
        users={"alice": {"is_admin": False}, "bob": {"is_admin": False}},
    )
    app.include_router(mission.setup_mission_control_routes(
        session_factory=database_must_not_be_opened,
        health_collector=_health,
        now_factory=lambda: _NOW,
        data_dir=tmp_path,
    ))

    async with _client(_Identity(app)) as client:
        response = await client.get("/api/mission-control/today")

    assert response.status_code == 200
    body = response.json()
    for name in (
        "calendar",
        "project_work",
        "inbox",
        "goals",
        "tasks",
        "study_reviews",
        "important_mail",
        "notes_today",
        "daily_brief",
    ):
        assert body["sources"][name]["status"] == "unavailable"
        assert body["sources"][name]["error"]["code"] == "owner_unavailable"
    assert body["sources"]["health"]["status"] == "degraded"


@pytest.mark.anyio
async def test_home_includes_ordinary_todo_checklist_steps(mission_env):
    app, factory, _data_dir = mission_env
    db = factory()
    db.add_all([
        cdb.Note(
            id="todo-alice",
            owner="alice",
            title="Application checklist",
            note_type="todo",
            items=json.dumps([
                {"id": "done", "text": "Collect transcript", "done": True},
                {"id": "next", "text": "Request recommendation", "done": False},
            ]),
        ),
        cdb.Note(
            id="todo-bob",
            owner="bob",
            title="Private Bob todo",
            note_type="todo",
            items=json.dumps([{"text": "BOB_PRIVATE_STEP", "done": False}]),
        ),
    ])
    db.commit()
    db.close()

    async with _client(app) as client:
        response = await client.get("/api/mission-control/today", headers=_headers())

    assert response.status_code == 200
    todos = response.json()["sources"]["notes_today"]["items"]
    assert todos == [{
        **todos[0],
        "id": "todo-alice",
        "kind": "todo",
        "next_step": "Request recommendation",
        "completed_steps": 1,
        "total_steps": 2,
    }]
    assert "BOB_PRIVATE_STEP" not in response.text


@pytest.mark.anyio
async def test_activity_is_distinct_bounded_and_owner_scoped(mission_env):
    app, factory, _data_dir = mission_env
    now = _NOW.replace(tzinfo=None)
    db = factory()
    alice_project = cdb.Project(
        id="activity-project-alice", owner="alice", key="ACT", name="Controls rig"
    )
    bob_project = cdb.Project(
        id="activity-project-bob", owner="bob", key="BOB", name="BOB_SECRET_PROJECT"
    )
    alice_task = cdb.ScheduledTask(
        id="activity-automation-alice", owner="alice", name="Daily sync"
    )
    bob_task = cdb.ScheduledTask(
        id="activity-automation-bob", owner="bob", name="BOB_SECRET_AUTOMATION"
    )
    db.add_all([alice_project, bob_project, alice_task, bob_task])
    db.flush()
    db.add_all([
        cdb.ProjectActivity(
            id="project-activity-alice",
            project_id=alice_project.id,
            actor="alice",
            event_type="work_item_completed",
            summary="Closed ACT-1",
            created_at=now - timedelta(minutes=2),
        ),
        cdb.ProjectActivity(
            id="project-activity-bob",
            project_id=bob_project.id,
            actor="bob",
            event_type="updated",
            summary="BOB_SECRET_ACTIVITY",
            created_at=now - timedelta(minutes=1),
        ),
        cdb.TaskRun(
            id="automation-run-alice",
            task_id=alice_task.id,
            status="success",
            started_at=now - timedelta(minutes=4),
            finished_at=now - timedelta(minutes=3),
        ),
        cdb.TaskRun(
            id="automation-run-bob",
            task_id=bob_task.id,
            status="error",
            started_at=now - timedelta(minutes=5),
        ),
        cdb.ProgressionEvent(
            id="progression-activity-alice",
            owner="alice",
            event_key="activity:test",
            source_type="todo_item_completed",
            source_id="todo-1",
            title="Request recommendation",
            xp=20,
            occurred_at=now - timedelta(minutes=1),
        ),
    ])
    db.commit()
    db.close()

    async with _client(app) as client:
        response = await client.get(
            "/api/mission-control/activity?limit=2", headers=_headers()
        )

    assert response.status_code == 200
    body = response.json()
    assert body["feed"]["count"] == 2
    assert body["feed"]["truncated"] is True
    assert [row["source"] for row in body["feed"]["items"]] == [
        "progression", "project"
    ]
    assert body["feed"]["next_before"]
    assert "BOB_SECRET" not in response.text
    assert body["health"]["overall"] == "degraded"


@pytest.mark.anyio
async def test_activity_composite_cursor_keeps_equal_timestamp_events(mission_env):
    app, factory, _data_dir = mission_env
    occurred = _NOW.replace(tzinfo=None) - timedelta(minutes=1)
    db = factory()
    try:
        for suffix in ("a", "b", "c"):
            db.add(cdb.ProgressionEvent(
                id=f"tied-{suffix}",
                owner="alice",
                event_key=f"activity:tied:{suffix}",
                source_type="todo_item_completed",
                source_id=f"todo-{suffix}",
                title=f"Tied {suffix}",
                xp=20,
                occurred_at=occurred,
            ))
        db.commit()
    finally:
        db.close()

    async with _client(app) as client:
        first = await client.get(
            "/api/mission-control/activity",
            params={"limit": 2},
            headers=_headers(),
        )
        first_feed = first.json()["feed"]
        second = await client.get(
            "/api/mission-control/activity",
            params={
                "limit": 2,
                "before": first_feed["next_before"],
                "before_id": first_feed["next_before_id"],
            },
            headers=_headers(),
        )

    assert first.status_code == 200
    assert second.status_code == 200
    first_ids = [row["id"] for row in first_feed["items"]]
    second_ids = [row["id"] for row in second.json()["feed"]["items"]]
    assert first_ids == ["progression:tied-c", "progression:tied-b"]
    assert second_ids == ["progression:tied-a"]
    assert not set(first_ids) & set(second_ids)


@pytest.mark.anyio
async def test_activity_routes_planning_completion_back_to_home(mission_env):
    app, factory, _data_dir = mission_env
    db = factory()
    try:
        db.add(cdb.ProgressionEvent(
            id="planning-clear",
            owner="alice",
            event_key="planning:item-1:completed",
            source_type="todo_item_completed",
            source_id="item-1",
            title="Finish control report",
            xp=20,
            details={"planning_item_id": "item-1"},
            occurred_at=_NOW.replace(tzinfo=None),
        ))
        db.commit()
    finally:
        db.close()

    async with _client(app) as client:
        response = await client.get(
            "/api/mission-control/activity", headers=_headers()
        )

    assert response.status_code == 200
    event = response.json()["feed"]["items"][0]
    assert event["id"] == "progression:planning-clear"
    assert event["target"] == "home"


@pytest.mark.anyio
async def test_health_snapshot_is_shared_between_today_and_activity(mission_env):
    _app, factory, data_dir = mission_env
    calls = 0

    async def counted_health(_rag, _memory):
        nonlocal calls
        calls += 1
        return {"overall": "ok", "services": []}

    app = _build_app(factory, health_collector=counted_health, data_dir=data_dir)
    async with _client(app) as client:
        today = await client.get("/api/mission-control/today", headers=_headers())
        activity = await client.get("/api/mission-control/activity", headers=_headers())

    assert today.status_code == 200
    assert activity.status_code == 200
    assert calls == 1
