"""Focused persistence and timer regressions for Study Mode."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import sqlite3
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import core.session_manager as session_manager_module
from core.database import Session as DbSession, StudyState
from core.session_manager import SessionManager
import routes.session_routes as session_routes
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
    monkeypatch.setattr(study_routes, "SessionLocal", factory)
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


def _add_study_session(factory, session_id: str, owner=None, mode: str = "study"):
    db = factory()
    db.add(
        DbSession(
            id=session_id,
            owner=owner,
            name=f"Study {session_id}",
            endpoint_url="http://model.test/v1/chat/completions",
            model="test-model",
            mode=mode,
        )
    )
    db.commit()
    db.close()


def _study_url(path: str, session_id: str) -> str:
    return f"{path}?session_id={session_id}"


def _add_legacy_state(factory, *, owner, goal: str, minutes: int = 240):
    db = factory()
    db.add(
        StudyState(
            id=study.owner_key(owner),
            owner=owner,
            goal_text=goal,
            target_minutes=minutes,
        )
    )
    db.commit()
    db.close()


async def test_goal_validation_persistence_and_owner_isolation(study_db):
    alice = {"x-test-user": "alice"}
    bob = {"x-test-user": "bob"}
    _add_study_session(study_db, "alice-study", owner="alice")
    _add_study_session(study_db, "bob-study", owner="bob")

    async with _client() as client:
        saved = await client.put(
            _study_url("/api/study/goal", "alice-study"),
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
            _study_url("/api/study/goal", "bob-study"),
            headers=bob,
            json={"goal_text": "Learn controls", "target_minutes": 600},
        )
        assert other.status_code == 200

        alice_state = (
            await client.get(_study_url("/api/study/state", "alice-study"), headers=alice)
        ).json()
        bob_state = (
            await client.get(_study_url("/api/study/state", "bob-study"), headers=bob)
        ).json()

    assert alice_state["goal_text"] == "Derive rigid-body dynamics"
    assert alice_state["target_minutes"] == 1_200
    assert alice_state["target_date"] == "2026-12-31"
    assert bob_state["goal_text"] == "Learn controls"
    assert bob_state["target_minutes"] == 600
    assert bob_state["target_date"] is None

    db = study_db()
    rows = {row.id: row for row in db.query(StudyState).all()}
    db.close()
    assert set(rows) == {"alice-study", "bob-study"}
    assert rows["alice-study"].owner == "alice"
    assert rows["bob-study"].owner == "bob"


async def test_same_owner_study_sessions_keep_goals_and_timers_isolated(
    study_db, monkeypatch
):
    _add_study_session(study_db, "dynamics", owner="alice")
    _add_study_session(study_db, "controls", owner="alice")
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)
    headers = {"x-test-user": "alice"}

    async with _client() as client:
        for session_id, goal in (
            ("dynamics", "Master rigid-body dynamics"),
            ("controls", "Master feedback control"),
        ):
            response = await client.put(
                _study_url("/api/study/goal", session_id),
                headers=headers,
                json={"goal_text": goal, "target_minutes": 600},
            )
            assert response.status_code == 200

        assert (
            await client.post(
                _study_url("/api/study/timer/start", "dynamics"), headers=headers
            )
        ).status_code == 200
        clock.advance(90)
        dynamics = (
            await client.get(
                _study_url("/api/study/state", "dynamics"), headers=headers
            )
        ).json()
        controls = (
            await client.get(
                _study_url("/api/study/state", "controls"), headers=headers
            )
        ).json()

    assert dynamics["session_id"] == "dynamics"
    assert dynamics["goal_text"] == "Master rigid-body dynamics"
    assert dynamics["timer_seconds"] == 90
    assert controls["session_id"] == "controls"
    assert controls["goal_text"] == "Master feedback control"
    assert controls["timer_seconds"] == 0


def test_review_scheduler_uses_evidence_levels_and_due_clock(study_db, monkeypatch):
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)

    initial = study.get_study_state("alice", "controls")
    assert initial["review"] == {
        "level": 0,
        "count": 0,
        "last_result": None,
        "last_reviewed_at": None,
        "next_review_at": None,
        "due": False,
        "due_in_seconds": None,
        "status": "not_scheduled",
    }

    missed = study.record_study_review("alice", "controls", "missed")["review"]
    assert missed == {
        "level": 0,
        "count": 1,
        "last_result": "missed",
        "last_reviewed_at": "2026-07-14T09:00:00Z",
        "next_review_at": "2026-07-14T09:10:00Z",
        "due": False,
        "due_in_seconds": 600,
        "status": "scheduled",
    }

    clock.advance(600)
    due = study.get_study_state("alice", "controls")["review"]
    assert due["due"] is True
    assert due["due_in_seconds"] == 0
    assert due["status"] == "due_now"

    hinted = study.record_study_review("alice", "controls", "hinted")["review"]
    assert (hinted["level"], hinted["count"], hinted["due_in_seconds"]) == (1, 2, 43_200)
    clean = study.record_study_review("alice", "controls", "clean")["review"]
    assert (clean["level"], clean["count"], clean["due_in_seconds"]) == (2, 3, 86_400)
    transfer = study.record_study_review("alice", "controls", "transfer")["review"]
    assert (transfer["level"], transfer["count"], transfer["due_in_seconds"]) == (4, 4, 604_800)

    with pytest.raises(ValueError, match="exactly one of"):
        study.record_study_review("alice", "controls", "Clean")


def test_review_evidence_is_session_isolated_and_goal_reset_clears_it(
    study_db, monkeypatch
):
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)

    study.save_study_goal("alice", "dynamics", "Dynamics", 60, None)
    study.save_study_goal("alice", "controls", "Controls", 60, None)
    study.record_study_review("alice", "dynamics", "clean")
    study.record_study_review("alice", "controls", "transfer")

    assert study.get_study_state("alice", "dynamics")["review"]["level"] == 2
    assert study.get_study_state("alice", "controls")["review"]["level"] == 3

    reset = study.save_study_goal(
        "alice", "dynamics", "Dynamics", 60, None, reset_progress=True
    )
    assert reset["review"]["status"] == "not_scheduled"
    assert reset["review"]["count"] == 0
    assert study.get_study_state("alice", "controls")["review"]["count"] == 1

    study.record_study_review("alice", "dynamics", "clean")
    replaced = study.save_study_goal(
        "alice", "dynamics", "Fluid dynamics", 90, None
    )
    assert replaced["goal_text"] == "Fluid dynamics"
    assert replaced["review"]["count"] == 0
    assert replaced["review"]["next_review_at"] is None


async def test_study_routes_require_owned_study_session_and_session_id(study_db):
    _add_study_session(study_db, "alice-study", owner="alice")
    _add_study_session(study_db, "ordinary", owner="alice", mode="chat")

    async with _client() as client:
        missing_id = await client.get(
            "/api/study/state", headers={"x-test-user": "alice"}
        )
        cross_owner = await client.get(
            _study_url("/api/study/state", "alice-study"),
            headers={"x-test-user": "bob"},
        )
        ordinary = await client.get(
            _study_url("/api/study/state", "ordinary"),
            headers={"x-test-user": "alice"},
        )

    assert missing_id.status_code == 422
    assert cross_owner.status_code == 404
    assert ordinary.status_code == 409
    db = study_db()
    assert db.query(StudyState).count() == 0
    db.close()


async def test_review_api_validates_outcome_and_owner_scope(study_db, monkeypatch):
    _add_study_session(study_db, "alice-study", owner="alice")
    _add_study_session(study_db, "ordinary", owner="alice", mode="chat")
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)

    async with _client() as client:
        saved = await client.post(
            _study_url("/api/study/review", "alice-study"),
            headers={"x-test-user": "alice"},
            json={"outcome": "clean"},
        )
        invalid = await client.post(
            _study_url("/api/study/review", "alice-study"),
            headers={"x-test-user": "alice"},
            json={"outcome": "almost"},
        )
        missing = await client.post(
            _study_url("/api/study/review", "alice-study"),
            headers={"x-test-user": "alice"},
            json={},
        )
        cross_owner = await client.post(
            _study_url("/api/study/review", "alice-study"),
            headers={"x-test-user": "bob"},
            json={"outcome": "transfer"},
        )
        ordinary = await client.post(
            _study_url("/api/study/review", "ordinary"),
            headers={"x-test-user": "alice"},
            json={"outcome": "clean"},
        )
        missing_session_id = await client.post(
            "/api/study/review",
            headers={"x-test-user": "alice"},
            json={"outcome": "clean"},
        )

    assert saved.status_code == 200
    assert saved.json()["review"]["last_result"] == "clean"
    assert saved.json()["review"]["next_review_at"] == "2026-07-15T09:00:00Z"
    assert invalid.status_code == 422
    assert "missed" in invalid.text and "transfer" in invalid.text
    assert missing.status_code == 422
    assert cross_owner.status_code == 404
    assert ordinary.status_code == 409
    assert missing_session_id.status_code == 422
    assert study.get_study_state("alice", "alice-study")["review"]["count"] == 1


async def test_configured_auth_rejects_missing_identity(study_db, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("LOCALHOST_BYPASS", raising=False)

    async with _client() as client:
        assert (
            await client.get(_study_url("/api/study/state", "missing"))
        ).status_code == 401
        assert (
            await client.put(
                _study_url("/api/study/goal", "missing"),
                json={"goal_text": "Mechanics", "target_minutes": 60},
            )
        ).status_code == 401
        assert (
            await client.post(_study_url("/api/study/timer/start", "missing"))
        ).status_code == 401
        assert (
            await client.post(
                _study_url("/api/study/review", "missing"),
                json={"outcome": "clean"},
            )
        ).status_code == 401

    db = study_db()
    assert db.query(StudyState).count() == 0
    db.close()


async def test_auth_disabled_keeps_local_study_mode_working(study_db, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    _add_study_session(study_db, "local-study", owner=None)

    async with _client() as client:
        saved = await client.put(
            _study_url("/api/study/goal", "local-study"),
            json={"goal_text": "Learn controls", "target_minutes": 300},
        )
        state = await client.get(_study_url("/api/study/state", "local-study"))

    assert saved.status_code == 200
    assert state.status_code == 200
    assert state.json()["goal_text"] == "Learn controls"


async def test_first_open_bootstraps_a_starter_goal_and_timer(study_db):
    _add_study_session(study_db, "alice-study", owner="alice")
    async with _client() as client:
        initial = await client.get(
            _study_url("/api/study/state", "alice-study"),
            headers={"x-test-user": "alice"},
        )
        response = await client.post(
            _study_url("/api/study/timer/start", "alice-study"),
            headers={"x-test-user": "alice"},
        )

    assert initial.status_code == 200
    assert initial.json()["goal_text"] == study.DEFAULT_STUDY_GOAL
    assert initial.json()["target_minutes"] == study.DEFAULT_TARGET_MINUTES
    assert response.status_code == 200
    assert response.json()["timer_running"] is True
    db = study_db()
    row = db.query(StudyState).filter_by(id="alice-study", owner="alice").one()
    db.close()
    assert row.goal_text == study.DEFAULT_STUDY_GOAL
    assert row.target_minutes == study.DEFAULT_TARGET_MINUTES


async def test_timer_http_lifecycle_survives_reload(study_db, monkeypatch):
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)
    headers = {"x-test-user": "alice"}
    _add_study_session(study_db, "alice-study", owner="alice")

    async with _client() as client:
        started = await client.post(
            _study_url("/api/study/timer/start", "alice-study"), headers=headers
        )
        assert started.status_code == 200
        clock.advance(65)
        running = await client.get(
            _study_url("/api/study/state", "alice-study"), headers=headers
        )
        assert running.json()["timer_seconds"] == 65
        paused = await client.post(
            _study_url("/api/study/timer/pause", "alice-study"), headers=headers
        )
        assert paused.json()["timer_running"] is False
        assert paused.json()["timer_seconds"] == 65

    # A fresh client simulates reopening the Study surface after navigation.
    clock.advance(300)
    async with _client() as client:
        reloaded = await client.get(
            _study_url("/api/study/state", "alice-study"), headers=headers
        )
        finished = await client.post(
            _study_url("/api/study/timer/finish", "alice-study"), headers=headers
        )

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
    _add_study_session(study_db, "alice-study", owner="alice")
    async with _client() as client:
        response = await client.put(
            _study_url("/api/study/goal", "alice-study"),
            headers={"x-test-user": "alice"},
            json=payload,
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
    study.save_study_goal(
        None, "local-study", "Local controls goal", 240, "2026-08-15"
    )

    alice = study.get_study_state("alice", "alice-study")

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
    _add_legacy_state(study_db, owner=None, goal="Local controls goal")
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
    _add_legacy_state(study_db, owner=None, goal="Quarantined local goal")
    db = study_db()
    db.add(
        StudyState(
            id="alice-study",
            owner="alice",
            goal_text="Authenticated goal",
            target_minutes=600,
            target_date="2026-09-01",
        )
    )
    db.commit()
    db.close()
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
        ("alice-study", "alice", "Authenticated goal"),
        (study.LOCAL_OWNER_KEY, None, "Quarantined local goal"),
    ]


def test_legacy_owner_global_state_is_claimed_once_without_cloning_effort(study_db):
    _add_legacy_state(study_db, owner="alice", goal="Legacy dynamics", minutes=600)
    db = study_db()
    legacy = db.query(StudyState).filter_by(id="user:alice").one()
    legacy.total_seconds = 1_800
    db.commit()
    db.close()

    claimed = study.get_study_state("alice", "first-study")
    fresh = study.get_study_state("alice", "second-study")

    assert claimed["session_id"] == "first-study"
    assert claimed["goal_text"] == "Legacy dynamics"
    assert claimed["total_seconds"] == 1_800
    assert fresh["session_id"] == "second-study"
    assert fresh["goal_text"] == study.DEFAULT_STUDY_GOAL
    assert fresh["total_seconds"] == 0
    db = study_db()
    rows = db.query(StudyState).order_by(StudyState.id).all()
    db.close()
    assert [(row.id, row.total_seconds) for row in rows] == [
        ("first-study", 1_800),
        ("second-study", 0),
    ]


def test_concurrent_first_open_is_idempotent(study_db):
    barrier = Barrier(2)

    def open_study():
        barrier.wait(timeout=5)
        return study.get_study_state("alice", "alice-study")

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
    study.save_study_goal("alice", "alice-study", "Dynamics", 15, None)

    started = study.start_study_timer("alice", "alice-study")
    assert started["timer_running"] is True
    assert started["timer_seconds"] == 0

    clock.advance(90)
    started_again = study.start_study_timer("alice", "alice-study")
    assert started_again["timer_seconds"] == 90

    db = study_db()
    row = db.query(StudyState).filter_by(id="alice-study").one()
    assert row.timer_started_at == datetime(2026, 7, 14, 9, 0, 0)
    db.close()

    paused = study.pause_study_timer("alice", "alice-study")
    assert paused["timer_running"] is False
    assert paused["timer_seconds"] == 90

    clock.advance(300)
    still_paused = study.get_study_state("alice", "alice-study")
    assert still_paused["timer_seconds"] == 90

    resumed = study.start_study_timer("alice", "alice-study")
    assert resumed["timer_running"] is True
    assert resumed["timer_seconds"] == 90

    clock.advance(30)
    finished = study.finish_study_timer("alice", "alice-study")
    assert finished["timer_running"] is False
    assert finished["timer_seconds"] == 0
    assert finished["total_seconds"] == 120
    assert finished["studied_seconds"] == 120

    # Repeated terminal actions must not double-count the completed block.
    assert study.finish_study_timer("alice", "alice-study")["total_seconds"] == 120
    assert study.pause_study_timer("alice", "alice-study")["total_seconds"] == 120


def test_new_goal_requires_explicit_reset_but_same_goal_edits_preserve_effort(study_db):
    study.save_study_goal("alice", "alice-study", "Goal A", 60, None)
    db = study_db()
    row = db.query(StudyState).filter_by(owner="alice").one()
    row.total_seconds = 3_600
    db.commit()
    db.close()

    edited = study.save_study_goal(
        "alice", "alice-study", "Goal A", 120, "2026-12-31"
    )
    assert edited["total_seconds"] == 3_600
    assert edited["target_minutes"] == 120

    with pytest.raises(study.StudyGoalConflictError):
        study.save_study_goal("alice", "alice-study", "Goal B", 30, None)
    unchanged = study.get_study_state("alice", "alice-study")
    assert unchanged["goal_text"] == "Goal A"
    assert unchanged["total_seconds"] == 3_600

    replaced = study.save_study_goal(
        "alice", "alice-study", "Goal B", 30, None, reset_progress=True
    )
    assert replaced["goal_text"] == "Goal B"
    assert replaced["total_seconds"] == 0
    assert replaced["timer_seconds"] == 0
    assert replaced["progress_percent"] == 0.0


def test_owner_column_lookup_retains_study_state_after_profile_rename(study_db):
    study.save_study_goal("alice", "alice-study", "Dynamics", 300, None)
    db = study_db()
    row = db.query(StudyState).filter_by(owner="alice").one()
    original_id = row.id
    row.owner = "alice2"  # mirrors the generic auth owner migration
    row.total_seconds = 900
    db.commit()
    db.close()

    renamed = study.get_study_state("alice2", "alice-study")
    assert renamed["goal_text"] == "Dynamics"
    assert renamed["total_seconds"] == 900
    study.save_study_goal("alice2", "alice-study", "Dynamics", 360, None)

    db = study_db()
    rows = db.query(StudyState).all()
    db.close()
    assert len(rows) == 1
    assert rows[0].id == original_id
    assert rows[0].owner == "alice2"


def test_reused_old_username_gets_a_distinct_study_row_after_rename(study_db):
    study.save_study_goal("alice", "original-study", "Original", 60, None)
    db = study_db()
    original = db.query(StudyState).filter_by(owner="alice").one()
    original.owner = "alice2"
    db.commit()
    db.close()

    study.save_study_goal("alice", "new-study", "New profile goal", 60, None)
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
    study.save_study_goal("alice", "alice-study", "Controls", 15, None)
    study.start_study_timer("alice", "alice-study")

    clock.advance(-300)
    live = study.get_study_state("alice", "alice-study")
    paused = study.pause_study_timer("alice", "alice-study")

    assert live["timer_seconds"] == 0
    assert live["studied_seconds"] == 0
    assert paused["timer_seconds"] == 0
    assert paused["total_seconds"] == 0
    assert paused["remaining_seconds"] == 15 * 60


def test_progress_is_clamped_at_both_bounds():
    state = StudyState(
        id="alice-study",
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


def test_empty_study_sessions_load_after_restart_and_survive_cleanup(
    study_db, monkeypatch
):
    _add_study_session(study_db, "empty-study", owner="alice")
    _add_study_session(study_db, "empty-chat", owner="alice", mode="chat")
    db = study_db()
    old = datetime(2020, 1, 1, 0, 0, 0)
    for row in db.query(DbSession).all():
        row.created_at = old
        row.last_accessed = old
    db.commit()
    db.close()
    monkeypatch.setattr(session_manager_module, "SessionLocal", study_db)

    manager = SessionManager()
    assert "empty-study" in manager.sessions
    assert "empty-chat" not in manager.sessions

    stats = manager.cleanup_empty_sessions(min_age_hours=1)

    db = study_db()
    remaining = {row.id for row in db.query(DbSession).all()}
    db.close()
    assert remaining == {"empty-study"}
    assert stats["deleted_empty"] == 1


def test_deleting_one_study_session_removes_only_its_workspace_state(
    study_db, monkeypatch
):
    _add_study_session(study_db, "study-a", owner="alice")
    _add_study_session(study_db, "study-b", owner="alice")
    study.get_study_state("alice", "study-a")
    study.get_study_state("alice", "study-b")
    monkeypatch.setattr(session_manager_module, "SessionLocal", study_db)
    manager = SessionManager()

    assert manager.delete_session("study-a") is True

    db = study_db()
    assert {row.id for row in db.query(DbSession).all()} == {"study-b"}
    assert {row.id for row in db.query(StudyState).all()} == {"study-b"}
    db.close()


def test_study_review_migration_is_idempotent_and_backfills_existing_rows(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "legacy-study.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE study_states (
            id TEXT PRIMARY KEY,
            owner TEXT,
            goal_text TEXT NOT NULL DEFAULT '',
            target_minutes INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT INTO study_states (id, owner, goal_text, target_minutes) VALUES (?, ?, ?, ?)",
        ("legacy-study", "alice", "Legacy controls", 60),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{db_path}")
    cdb._migrate_add_study_review_columns()
    cdb._migrate_add_study_review_columns()

    conn = sqlite3.connect(db_path)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(study_states)").fetchall()
    }
    review = conn.execute(
        """
        SELECT review_level, review_count, last_review_result,
               last_reviewed_at, next_review_at
        FROM study_states WHERE id = 'legacy-study'
        """
    ).fetchone()
    conn.close()

    assert {
        "review_level",
        "review_count",
        "last_review_result",
        "last_reviewed_at",
        "next_review_at",
    } <= columns
    assert review == (0, 0, None, None, None)


def test_session_create_accepts_and_returns_study_mode(monkeypatch):
    captured = {}

    class _Manager:
        def create_session(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(name=kwargs["name"])

    monkeypatch.setattr("src.event_bus.fire_event", lambda *_args, **_kwargs: None)
    route_count = len(session_routes.router.routes)
    try:
        router = session_routes.setup_session_routes(_Manager(), {})
        handler = next(
            route.endpoint
            for route in reversed(router.routes)
            if route.path == "/api/session" and "POST" in route.methods
        )
        app = SimpleNamespace(
            state=SimpleNamespace(
                auth_manager=SimpleNamespace(is_admin=lambda _user: True)
            )
        )
        request = Request(
            scope={
                "type": "http",
                "app": app,
                "state": {"current_user": "alice"},
            }
        )

        response = handler(
            request=request,
            name="Study controls",
            endpoint_url="http://model.test/v1/chat/completions",
            model="test-model",
            rag=None,
            skip_validation="true",
            api_key="",
            endpoint_id="",
            mode="study",
        )
    finally:
        del session_routes.router.routes[route_count:]

    assert captured["owner"] == "alice"
    assert captured["mode"] == "study"
    assert response.mode == "study"


def test_session_create_rejects_unknown_mode():
    route_count = len(session_routes.router.routes)
    try:
        router = session_routes.setup_session_routes(SimpleNamespace(), {})
        handler = next(
            route.endpoint
            for route in reversed(router.routes)
            if route.path == "/api/session" and "POST" in route.methods
        )
        request = Request(scope={"type": "http", "state": {"current_user": "alice"}})

        with pytest.raises(HTTPException) as exc:
            handler(
                request=request,
                name="Bad",
                endpoint_url="",
                model="",
                rag=None,
                skip_validation="true",
                api_key="",
                endpoint_id="",
                mode="agent",
            )
    finally:
        del session_routes.router.routes[route_count:]

    assert exc.value.status_code == 400
