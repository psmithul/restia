"""Focused persistence and timer regressions for Study Mode."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import StudyState
import routes.study_routes as study_routes
import src.study_mode as study


_PEER = ("203.0.113.9", 54321)


class _Identity:
    """Set the owner state normally populated by the auth middleware."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            user = dict(scope.get("headers") or []).get(b"x-test-user")
            if user:
                scope.setdefault("state", {})["current_user"] = user.decode()
        await self.app(scope, receive, send)


class _Clock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


@pytest.fixture
def study_db(monkeypatch, tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'study.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(study, "SessionLocal", factory)
    yield factory
    engine.dispose()


def _app():
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(is_configured=True)
    app.include_router(study_routes.setup_study_routes())
    return _Identity(app)


def _client():
    transport = httpx.ASGITransport(app=_app(), client=_PEER)
    return httpx.AsyncClient(transport=transport, base_url="http://study.test")


async def test_goal_validation_persistence_and_owner_isolation(study_db):
    alice = {"x-test-user": "alice"}
    bob = {"x-test-user": "bob"}

    async with _client() as client:
        saved = await client.put(
            "/api/study/goal",
            headers=alice,
            json={
                "goal_text": "  Derive rigid-body dynamics  ",
                "target_minutes": 1_200,
                "target_date": "2026-12-31",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["goal_text"] == "Derive rigid-body dynamics"

        other = await client.put(
            "/api/study/goal",
            headers=bob,
            json={"goal_text": "Learn controls", "target_minutes": 600},
        )
        assert other.status_code == 200

        alice_state = (await client.get("/api/study/state", headers=alice)).json()
        bob_state = (await client.get("/api/study/state", headers=bob)).json()

    assert alice_state["goal_text"] == "Derive rigid-body dynamics"
    assert alice_state["target_minutes"] == 1_200
    assert alice_state["target_date"] == "2026-12-31"
    assert bob_state["goal_text"] == "Learn controls"
    assert bob_state["target_minutes"] == 600
    assert bob_state["target_date"] is None

    db = study_db()
    rows = {row.id: row for row in db.query(StudyState).all()}
    db.close()
    assert set(rows) == {"user:alice", "user:bob"}
    assert rows["user:alice"].owner == "alice"
    assert rows["user:bob"].owner == "bob"


async def test_configured_auth_rejects_missing_identity(study_db, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("LOCALHOST_BYPASS", raising=False)

    async with _client() as client:
        assert (await client.get("/api/study/state")).status_code == 401
        assert (
            await client.put(
                "/api/study/goal",
                json={"goal_text": "Mechanics", "target_minutes": 60},
            )
        ).status_code == 401
        assert (await client.post("/api/study/timer/start")).status_code == 401

    db = study_db()
    assert db.query(StudyState).count() == 0
    db.close()


async def test_auth_disabled_keeps_local_study_mode_working(study_db, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    async with _client() as client:
        saved = await client.put(
            "/api/study/goal",
            json={"goal_text": "Learn controls", "target_minutes": 300},
        )
        state = await client.get("/api/study/state")

    assert saved.status_code == 200
    assert state.status_code == 200
    assert state.json()["goal_text"] == "Learn controls"


async def test_first_open_bootstraps_a_starter_goal_and_timer(study_db):
    async with _client() as client:
        initial = await client.get(
            "/api/study/state", headers={"x-test-user": "alice"}
        )
        response = await client.post(
            "/api/study/timer/start", headers={"x-test-user": "alice"}
        )

    assert initial.status_code == 200
    assert initial.json()["goal_text"] == study.DEFAULT_STUDY_GOAL
    assert initial.json()["target_minutes"] == study.DEFAULT_TARGET_MINUTES
    assert response.status_code == 200
    assert response.json()["timer_running"] is True
    db = study_db()
    row = db.query(StudyState).filter_by(owner="alice").one()
    db.close()
    assert row.goal_text == study.DEFAULT_STUDY_GOAL
    assert row.target_minutes == study.DEFAULT_TARGET_MINUTES


async def test_timer_http_lifecycle_survives_reload(study_db, monkeypatch):
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)
    headers = {"x-test-user": "alice"}

    async with _client() as client:
        started = await client.post("/api/study/timer/start", headers=headers)
        assert started.status_code == 200
        clock.advance(65)
        running = await client.get("/api/study/state", headers=headers)
        assert running.json()["timer_seconds"] == 65
        paused = await client.post("/api/study/timer/pause", headers=headers)
        assert paused.json()["timer_running"] is False
        assert paused.json()["timer_seconds"] == 65

    # A fresh client simulates reopening the Study surface after navigation.
    clock.advance(300)
    async with _client() as client:
        reloaded = await client.get("/api/study/state", headers=headers)
        finished = await client.post("/api/study/timer/finish", headers=headers)

    assert reloaded.json()["timer_seconds"] == 65
    assert finished.json()["timer_seconds"] == 0
    assert finished.json()["total_seconds"] == 65


@pytest.mark.parametrize(
    "payload",
    [
        {"goal_text": "   ", "target_minutes": 15},
        {"goal_text": "Mechanics", "target_minutes": 14},
        {"goal_text": "Mechanics", "target_minutes": 525_601},
        {
            "goal_text": "Mechanics",
            "target_minutes": 60,
            "target_date": "2026-02-30",
        },
        {
            "goal_text": "Mechanics",
            "target_minutes": 60,
            "target_date": "31-12-2026",
        },
    ],
)
async def test_invalid_goal_payloads_are_rejected_without_persisting(study_db, payload):
    async with _client() as client:
        response = await client.put(
            "/api/study/goal", headers={"x-test-user": "alice"}, json=payload
        )
    assert response.status_code == 422

    db = study_db()
    assert db.query(StudyState).count() == 0
    db.close()


def test_owner_key_is_stable_and_normalized():
    assert study.owner_key("alice") == "user:alice"
    assert study.owner_key("  alice  ") == "user:alice"
    assert study.owner_key(None) == study.LOCAL_OWNER_KEY
    assert study.owner_key("   ") == study.LOCAL_OWNER_KEY


def test_authenticated_profile_never_claims_anonymous_study_state(study_db):
    study.save_study_goal(None, "Local controls goal", 240, "2026-08-15")

    alice = study.get_study_state("alice")

    assert alice["goal_text"] == study.DEFAULT_STUDY_GOAL
    db = study_db()
    rows = db.query(StudyState).all()
    db.close()
    assert {(row.owner, row.goal_text) for row in rows} == {
        (None, "Local controls goal"),
        ("alice", study.DEFAULT_STUDY_GOAL),
    }


def test_startup_owner_migration_assigns_local_study_state_to_admin(
    study_db, monkeypatch, tmp_path
):
    study.save_study_goal(None, "Local controls goal", 240, "2026-08-15")
    auth_file = tmp_path / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "users": {
                    "alice": {"is_admin": True},
                    "bob": {"is_admin": False},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{tmp_path / 'study.db'}")

    cdb._migrate_assign_legacy_owner()

    db = study_db()
    row = db.query(StudyState).filter_by(id=study.LOCAL_OWNER_KEY).one()
    db.close()
    assert row.owner == "alice"
    assert row.goal_text == "Local controls goal"


def test_startup_owner_migration_does_not_duplicate_existing_admin_state(
    study_db, monkeypatch, tmp_path
):
    study.save_study_goal(None, "Quarantined local goal", 240, "2026-08-15")
    study.save_study_goal("alice", "Authenticated goal", 600, "2026-09-01")
    (tmp_path / "auth.json").write_text(
        json.dumps({"users": {"alice": {"is_admin": True}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{tmp_path / 'study.db'}")

    cdb._migrate_assign_legacy_owner()

    db = study_db()
    rows = db.query(StudyState).order_by(StudyState.id).all()
    snapshot = [(row.id, row.owner, row.goal_text) for row in rows]
    db.close()
    assert snapshot == [
        (study.LOCAL_OWNER_KEY, None, "Quarantined local goal"),
        ("user:alice", "alice", "Authenticated goal"),
    ]


def test_concurrent_first_open_is_idempotent(study_db):
    barrier = Barrier(2)

    def open_study():
        barrier.wait(timeout=5)
        return study.get_study_state("alice")

    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(lambda _: open_study(), range(2)))

    assert {state["goal_text"] for state in states} == {study.DEFAULT_STUDY_GOAL}
    db = study_db()
    rows = db.query(StudyState).filter_by(owner="alice").all()
    db.close()
    assert len(rows) == 1


def test_timer_start_pause_resume_finish_and_idempotence(study_db, monkeypatch):
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)
    study.save_study_goal("alice", "Dynamics", 15, None)

    started = study.start_study_timer("alice")
    assert started["timer_running"] is True
    assert started["timer_seconds"] == 0

    clock.advance(90)
    started_again = study.start_study_timer("alice")
    assert started_again["timer_seconds"] == 90

    db = study_db()
    row = db.query(StudyState).filter_by(id="user:alice").one()
    assert row.timer_started_at == datetime(2026, 7, 14, 9, 0, 0)
    db.close()

    paused = study.pause_study_timer("alice")
    assert paused["timer_running"] is False
    assert paused["timer_seconds"] == 90

    clock.advance(300)
    still_paused = study.get_study_state("alice")
    assert still_paused["timer_seconds"] == 90

    resumed = study.start_study_timer("alice")
    assert resumed["timer_running"] is True
    assert resumed["timer_seconds"] == 90

    clock.advance(30)
    finished = study.finish_study_timer("alice")
    assert finished["timer_running"] is False
    assert finished["timer_seconds"] == 0
    assert finished["total_seconds"] == 120
    assert finished["studied_seconds"] == 120

    # Repeated terminal actions must not double-count the completed block.
    assert study.finish_study_timer("alice")["total_seconds"] == 120
    assert study.pause_study_timer("alice")["total_seconds"] == 120


def test_new_goal_requires_explicit_reset_but_same_goal_edits_preserve_effort(study_db):
    study.save_study_goal("alice", "Goal A", 60, None)
    db = study_db()
    row = db.query(StudyState).filter_by(owner="alice").one()
    row.total_seconds = 3_600
    db.commit()
    db.close()

    edited = study.save_study_goal("alice", "Goal A", 120, "2026-12-31")
    assert edited["total_seconds"] == 3_600
    assert edited["target_minutes"] == 120

    with pytest.raises(study.StudyGoalConflictError):
        study.save_study_goal("alice", "Goal B", 30, None)
    unchanged = study.get_study_state("alice")
    assert unchanged["goal_text"] == "Goal A"
    assert unchanged["total_seconds"] == 3_600

    replaced = study.save_study_goal(
        "alice", "Goal B", 30, None, reset_progress=True
    )
    assert replaced["goal_text"] == "Goal B"
    assert replaced["total_seconds"] == 0
    assert replaced["timer_seconds"] == 0
    assert replaced["progress_percent"] == 0.0


def test_owner_column_lookup_retains_study_state_after_profile_rename(study_db):
    study.save_study_goal("alice", "Dynamics", 300, None)
    db = study_db()
    row = db.query(StudyState).filter_by(owner="alice").one()
    original_id = row.id
    row.owner = "alice2"  # mirrors the generic auth owner migration
    row.total_seconds = 900
    db.commit()
    db.close()

    renamed = study.get_study_state("alice2")
    assert renamed["goal_text"] == "Dynamics"
    assert renamed["total_seconds"] == 900
    study.save_study_goal("alice2", "Dynamics", 360, None)

    db = study_db()
    rows = db.query(StudyState).all()
    db.close()
    assert len(rows) == 1
    assert rows[0].id == original_id
    assert rows[0].owner == "alice2"


def test_reused_old_username_gets_a_distinct_study_row_after_rename(study_db):
    study.save_study_goal("alice", "Original", 60, None)
    db = study_db()
    original = db.query(StudyState).filter_by(owner="alice").one()
    original.owner = "alice2"
    db.commit()
    db.close()

    study.save_study_goal("alice", "New profile goal", 60, None)
    db = study_db()
    rows = db.query(StudyState).order_by(StudyState.owner).all()
    db.close()
    assert [(row.owner, row.goal_text) for row in rows] == [
        ("alice", "New profile goal"),
        ("alice2", "Original"),
    ]
    assert len({row.id for row in rows}) == 2


def test_backward_clock_drift_never_creates_negative_time(study_db, monkeypatch):
    clock = _Clock(datetime(2026, 7, 14, 12, 0, 0))
    monkeypatch.setattr(study, "_now", clock)
    study.save_study_goal("alice", "Controls", 15, None)
    study.start_study_timer("alice")

    clock.advance(-300)
    live = study.get_study_state("alice")
    paused = study.pause_study_timer("alice")

    assert live["timer_seconds"] == 0
    assert live["studied_seconds"] == 0
    assert paused["timer_seconds"] == 0
    assert paused["total_seconds"] == 0
    assert paused["remaining_seconds"] == 15 * 60


def test_progress_is_clamped_at_both_bounds():
    state = StudyState(
        id="user:alice",
        owner="alice",
        goal_text="Mechanics",
        target_minutes=1,
        total_seconds=90,
        current_session_seconds=0,
        timer_running=False,
    )
    complete = study.serialize_study_state(state)
    assert complete["studied_seconds"] == 90
    assert complete["remaining_seconds"] == 0
    assert complete["progress_percent"] == 100.0

    state.total_seconds = -90
    state.current_session_seconds = -30
    empty = study.serialize_study_state(state)
    assert empty["studied_seconds"] == 0
    assert empty["remaining_seconds"] == 60
    assert empty["progress_percent"] == 0.0
