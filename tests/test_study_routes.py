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
import routes.chat_routes as chat_routes
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
    monkeypatch.setattr(chat_routes, "SessionLocal", factory)
    monkeypatch.setattr(
        study_routes,
        "effective_owner",
        lambda request: getattr(request.state, "current_user", None),
    )
    monkeypatch.setattr(
        study_routes,
        "resolved_request_owner",
        lambda request, admitted_user="": str(admitted_user or "local").lower(),
    )
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


def _add_study_session(
    factory,
    session_id: str,
    owner=None,
    mode: str = "study",
    name: str | None = None,
):
    db = factory()
    db.add(
        DbSession(
            id=session_id,
            owner=owner,
            name=name if name is not None else f"Study {session_id}",
            endpoint_url="http://model.test/v1/chat/completions",
            model="test-model",
            mode=mode,
        )
    )
    db.commit()
    db.close()


def _study_url(path: str, session_id: str) -> str:
    return f"{path}?session_id={session_id}"


def _accept_study_prompt(owner, session_id: str, prompt: str = "continue") -> dict:
    """Mirror the accepted chat-turn transaction, not the UI preflight."""

    return study.initialize_study_workspace(
        owner,
        session_id,
        prompt,
        record_prompt_activity=True,
    )


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


@pytest.mark.parametrize("topic", ["Calculus", "C++", "PID"])
def test_single_concept_is_a_substantive_initial_study_prompt(topic):
    setup = study.derive_study_setup(topic)

    assert setup["workspace_name"] == topic
    assert topic in setup["goal_text"]


def test_setup_does_not_repeat_first_principles_in_the_title_and_goal():
    setup = study.derive_study_setup(
        "Master PID control from first principles in 5 hours"
    )

    assert setup["workspace_name"] == "PID control"
    assert setup["target_minutes"] == 300
    assert setup["goal_text"].count("from first principles") == 1


@pytest.mark.parametrize(
    ("prompt", "title"),
    [
        ("Teach me 量子力学", "量子力学"),
        ("Learn क्वांटम यांत्रिकी", "क्वांटम यांत्रिकी"),
        ("Study ديناميكا الموائع", "ديناميكا الموائع"),
        ("Master квантовую механику", "Квантовую механику"),
    ],
)
def test_non_latin_prompts_are_substantive_and_keep_a_readable_title(prompt, title):
    setup = study.derive_study_setup(prompt)

    assert setup["workspace_name"] == title
    assert title in setup["goal_text"]


def test_symbol_only_prompt_is_not_a_substantive_topic():
    assert study.is_substantive_study_prompt("🚀✨") is False


@pytest.mark.parametrize(
    "filler", ["", "hello", "help me", "teach me", "continue", "okay"]
)
def test_conversational_filler_does_not_initialize_a_learning_goal(filler):
    assert study.is_substantive_study_prompt(filler) is False


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

        started = _accept_study_prompt("alice", "dynamics")
        assert started["timer_running"] is True
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
    clock.advance(43_200)
    clean = study.record_study_review("alice", "controls", "clean")["review"]
    assert (clean["level"], clean["count"], clean["due_in_seconds"]) == (2, 3, 86_400)
    clock.advance(86_400)
    transfer = study.record_study_review("alice", "controls", "transfer")["review"]
    assert (transfer["level"], transfer["count"], transfer["due_in_seconds"]) == (4, 4, 604_800)

    with pytest.raises(ValueError, match="exactly one of"):
        study.record_study_review("alice", "controls", "Clean")


def test_review_cannot_be_reposted_to_farm_progression_xp(study_db, monkeypatch):
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)

    first = study.record_study_review("alice", "controls", "clean")
    with pytest.raises(study.StudyReviewNotDueError, match="not due yet"):
        study.record_study_review("alice", "controls", "transfer")

    db = study_db()
    try:
        state = db.query(StudyState).one()
        events = db.query(cdb.ProgressionEvent).all()
        assert state.review_count == 1
        assert state.last_review_result == "clean"
        assert len(events) == 1
        assert events[0].source_type == "study_review_passed"
        assert events[0].xp == 35
    finally:
        db.close()

    clock.advance(first["review"]["due_in_seconds"])
    second = study.record_study_review("alice", "controls", "transfer")
    assert second["review"]["count"] == 2


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
        cross_owner_initialize = await client.post(
            _study_url("/api/study/initialize", "alice-study"),
            headers={"x-test-user": "bob"},
            json={"prompt": "Teach me dynamics"},
        )
        ordinary_initialize = await client.post(
            _study_url("/api/study/initialize", "ordinary"),
            headers={"x-test-user": "alice"},
            json={"prompt": "Teach me dynamics"},
        )

    assert missing_id.status_code == 422
    assert cross_owner.status_code == 404
    assert ordinary.status_code == 409
    assert cross_owner_initialize.status_code == 404
    assert ordinary_initialize.status_code == 409
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
        too_soon = await client.post(
            _study_url("/api/study/review", "alice-study"),
            headers={"x-test-user": "alice"},
            json={"outcome": "transfer"},
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
    assert too_soon.status_code == 400
    assert "not due yet" in too_soon.text
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


async def test_initialize_starts_on_first_prompt_then_derives_and_saves_setup_once(
    study_db, monkeypatch
):
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)
    _add_study_session(
        study_db, "alice-study", owner="alice", name="Study 1"
    )
    cached = SimpleNamespace(name="Study 1", mode=None)
    manager = SimpleNamespace(sessions={"alice-study": cached})
    monkeypatch.setattr(study, "get_session_manager_instance", lambda: manager)
    headers = {"x-test-user": "alice"}

    async with _client() as client:
        entered = await client.post(
            _study_url("/api/study/initialize", "alice-study"),
            headers=headers,
            json={"prompt": ""},
        )
        clock.advance(30)
        first_prompt_preflight = await client.post(
            _study_url("/api/study/initialize", "alice-study"),
            headers=headers,
            json={"prompt": "Teach me feedback control from scratch in 5 hours"},
        )
        first_prompt = _accept_study_prompt(
            "alice", "alice-study", "Teach me feedback control from scratch in 5 hours"
        )
        clock.advance(15)
        later_prompt_preflight = await client.post(
            _study_url("/api/study/initialize", "alice-study"),
            headers=headers,
            json={"prompt": "Teach me thermodynamics in 2 hours"},
        )
        later_prompt = _accept_study_prompt(
            "alice", "alice-study", "Teach me thermodynamics in 2 hours"
        )

    assert entered.status_code == 200
    assert entered.json()["timer_running"] is False
    assert entered.json()["last_prompt_at"] is None
    assert entered.json()["idle_pause_at"] is None
    assert entered.json()["goal_text"] == study.DEFAULT_STUDY_GOAL
    assert entered.json()["goal_initialized"] is False
    assert entered.json()["title_initialized"] is False

    preflight = first_prompt_preflight.json()
    assert first_prompt_preflight.status_code == 200
    assert preflight["goal_initialized"] is True
    assert preflight["title_initialized"] is True
    assert preflight["timer_running"] is False
    assert preflight["last_prompt_at"] is None

    initialized = first_prompt
    assert initialized["workspace_name"] == "Feedback control"
    assert initialized["goal_text"] == (
        "Explain Feedback control from first principles, complete 3 progressively "
        "harder checks without hints, and demonstrate the skill in 1 novel application."
    )
    assert initialized["target_minutes"] == 300
    assert initialized["timer_running"] is True
    assert initialized["timer_seconds"] == 0
    assert initialized["last_prompt_at"] == "2026-07-14T09:00:30Z"
    assert initialized["idle_pause_at"] == "2026-07-14T09:10:30Z"
    assert initialized["idle_seconds_remaining"] == 600
    assert initialized["goal_initialized"] is False
    assert initialized["title_initialized"] is False

    unrefreshed = later_prompt_preflight.json()
    assert unrefreshed["timer_seconds"] == 15
    assert unrefreshed["last_prompt_at"] == "2026-07-14T09:00:30Z"
    assert unrefreshed["idle_seconds_remaining"] == 585

    repeated = later_prompt
    assert repeated["workspace_name"] == "Feedback control"
    assert repeated["goal_text"] == initialized["goal_text"]
    assert repeated["target_minutes"] == 300
    assert repeated["timer_seconds"] == 15
    assert repeated["last_prompt_at"] == "2026-07-14T09:00:45Z"
    assert repeated["idle_pause_at"] == "2026-07-14T09:10:45Z"
    assert repeated["idle_seconds_remaining"] == 600
    assert repeated["goal_initialized"] is False
    assert repeated["title_initialized"] is False
    assert cached.name == "Feedback control"
    assert cached.mode == "study"

    db = study_db()
    workspace = db.query(DbSession).filter_by(id="alice-study").one()
    state = db.query(StudyState).filter_by(id="alice-study").one()
    assert workspace.name == "Feedback control"
    assert state.goal_text == initialized["goal_text"]
    assert state.timer_started_at == datetime(2026, 7, 14, 9, 0, 30)
    assert state.last_prompt_at == datetime(2026, 7, 14, 9, 0, 45)
    db.close()


def test_accepted_chat_initializer_commits_prompt_before_provider_failure(
    study_db, monkeypatch
):
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0, 123456))
    monkeypatch.setattr(study, "_now", clock)
    _add_study_session(study_db, "alice-study", owner="alice", name="Study 1")
    sess = SimpleNamespace(name="Study 1", mode="study")
    manager = SimpleNamespace(sessions={"alice-study": sess})
    monkeypatch.setattr(study, "get_session_manager_instance", lambda: manager)

    initialized = chat_routes._initialize_study_turn(
        manager,
        sess,
        "alice",
        "alice-study",
        "continue",
    )

    assert initialized["timer_running"] is True
    assert initialized["last_prompt_at"] == "2026-07-14T09:00:00.123456Z"
    assert initialized["idle_pause_at"] == "2026-07-14T09:10:00.123456Z"
    # The provider runs only after this helper returns. A later provider error
    # must not roll back the already-accepted user turn's focus lease.
    with pytest.raises(RuntimeError, match="provider unavailable"):
        raise RuntimeError("provider unavailable")

    persisted = study.get_study_state("alice", "alice-study")
    assert persisted["timer_running"] is True
    assert persisted["last_prompt_at"] == initialized["last_prompt_at"]


def test_accepted_chat_uses_persisted_unowned_workspace_in_local_mode(
    study_db, monkeypatch
):
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)
    _add_study_session(study_db, "local-study", owner=None, name="Study 1")
    sess = SimpleNamespace(name="Study 1", mode="study")
    manager = SimpleNamespace(sessions={"local-study": sess})
    monkeypatch.setattr(study, "get_session_manager_instance", lambda: manager)

    # AUTH_ENABLED=false can still resolve a configured single-user profile
    # for the request while newly created chat rows remain intentionally
    # unowned. The persisted workspace owner is authoritative after the chat
    # route's ownership check.
    initialized = chat_routes._initialize_study_turn(
        manager,
        sess,
        "configured-profile",
        "local-study",
        "Teach me orbital mechanics",
    )

    assert initialized["timer_running"] is True
    assert initialized["last_prompt_at"] == "2026-07-14T09:00:00Z"
    persisted = study.get_study_state(None, "local-study")
    assert persisted["timer_running"] is True
    assert persisted["last_prompt_at"] == initialized["last_prompt_at"]

    clock.advance(30)
    refreshed = chat_routes._refresh_study_turn_for_stream(
        "configured-profile", "local-study", initialized
    )
    assert refreshed["timer_seconds"] == 30
    assert refreshed["idle_seconds_remaining"] == 570


async def test_attachment_only_accepted_turn_starts_and_refreshes_prompt_lease(
    study_db, monkeypatch
):
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)
    _add_study_session(study_db, "alice-study", owner="alice", name="Study 1")
    headers = {"x-test-user": "alice"}

    async with _client() as client:
        public_setup = await client.post(
            _study_url("/api/study/initialize", "alice-study"),
            headers=headers,
            json={"prompt": ""},
        )
        accepted = study.initialize_study_workspace(
            "alice",
            "alice-study",
            "",
            record_prompt_activity=True,
        )
        clock.advance(30)
        public_preflight = await client.post(
            _study_url("/api/study/initialize", "alice-study"),
            headers=headers,
            json={"prompt": ""},
        )
        refreshed = study.initialize_study_workspace(
            "alice",
            "alice-study",
            "",
            record_prompt_activity=True,
        )

    assert public_setup.json()["timer_running"] is False
    assert public_setup.json()["last_prompt_at"] is None
    assert accepted["timer_running"] is True
    assert accepted["last_prompt_at"] == "2026-07-14T09:00:00Z"
    assert accepted["idle_seconds_remaining"] == 600
    assert public_preflight.json()["timer_seconds"] == 30
    assert public_preflight.json()["last_prompt_at"] == accepted["last_prompt_at"]
    assert public_preflight.json()["idle_seconds_remaining"] == 570
    assert refreshed["timer_seconds"] == 30
    assert refreshed["last_prompt_at"] == "2026-07-14T09:00:30Z"
    assert refreshed["idle_seconds_remaining"] == 600


def test_delayed_stream_event_keeps_original_prompt_deadline(study_db, monkeypatch):
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)
    _add_study_session(study_db, "alice-study", owner="alice", name="Study 1")
    sess = SimpleNamespace(name="Study 1", mode="study")
    manager = SimpleNamespace(sessions={"alice-study": sess})
    monkeypatch.setattr(study, "get_session_manager_instance", lambda: manager)
    accepted = chat_routes._initialize_study_turn(
        manager, sess, "alice", "alice-study", "continue"
    )

    # Simulate slow attachment/context preprocessing before the first SSE
    # event. The refreshed event ages the existing lease; it does not renew it.
    clock.advance(75)
    event = chat_routes._refresh_study_turn_for_stream(
        "alice", "alice-study", accepted
    )

    assert event["timer_running"] is True
    assert event["timer_seconds"] == 75
    assert event["last_prompt_at"] == "2026-07-14T09:00:00Z"
    assert event["idle_pause_at"] == "2026-07-14T09:10:00Z"
    assert event["idle_seconds_remaining"] == 525


async def test_initialize_never_overwrites_an_intentional_title_or_manual_goal(
    study_db,
):
    _add_study_session(
        study_db,
        "controls",
        owner="alice",
        name="Controls interview prep",
    )
    study.save_study_goal(
        "alice", "controls", "Pass the controls whiteboard interview", 240, None
    )

    async with _client() as client:
        response = await client.post(
            _study_url("/api/study/initialize", "controls"),
            headers={"x-test-user": "alice"},
            json={"prompt": "Teach me rigid body dynamics from scratch"},
        )

    payload = response.json()
    assert response.status_code == 200
    assert payload["workspace_name"] == "Controls interview prep"
    assert payload["goal_text"] == "Pass the controls whiteboard interview"
    assert payload["target_minutes"] == 240
    assert payload["timer_running"] is False
    assert payload["goal_initialized"] is False
    assert payload["title_initialized"] is False


async def test_manual_goal_equal_to_starter_is_durably_established(study_db):
    _add_study_session(study_db, "same-wording", owner="alice", name="Study 1")
    saved = study.save_study_goal(
        "alice",
        "same-wording",
        study.DEFAULT_STUDY_GOAL,
        study.DEFAULT_TARGET_MINUTES,
        None,
    )

    async with _client() as client:
        response = await client.post(
            _study_url("/api/study/initialize", "same-wording"),
            headers={"x-test-user": "alice"},
            json={"prompt": "Teach me thermodynamics from scratch"},
        )

    assert saved["setup_initialized"] is True
    assert response.status_code == 200
    payload = response.json()
    assert payload["goal_text"] == study.DEFAULT_STUDY_GOAL
    assert payload["workspace_name"] == "Study 1"
    assert payload["goal_initialized"] is False
    assert payload["title_initialized"] is False
    assert payload["tracker"]["learning_goal"]["source"] == "established"

    db = study_db()
    state = db.query(StudyState).filter_by(id="same-wording").one()
    assert state.setup_initialized is True
    db.close()


def test_atomic_chat_promotion_rolls_back_mode_and_state_on_commit_failure(
    study_db, monkeypatch
):
    _add_study_session(
        study_db, "promotion-fails", owner="alice", mode="chat", name="Chat"
    )
    cached = SimpleNamespace(name="Chat", mode="chat")
    monkeypatch.setattr(
        study,
        "get_session_manager_instance",
        lambda: SimpleNamespace(sessions={"promotion-fails": cached}),
    )

    failing_db = study_db()

    def fail_commit():
        raise RuntimeError("forced atomic commit failure")

    failing_db.commit = fail_commit
    monkeypatch.setattr(study, "SessionLocal", lambda: failing_db)

    with pytest.raises(RuntimeError, match="forced atomic commit failure"):
        study.initialize_study_workspace(
            "alice",
            "promotion-fails",
            "Teach me control theory",
            promote_to_study=True,
        )

    db = study_db()
    workspace = db.query(DbSession).filter_by(id="promotion-fails").one()
    assert workspace.mode == "chat"
    assert db.query(StudyState).filter_by(id="promotion-fails").count() == 0
    db.close()
    assert cached.name == "Chat"
    assert cached.mode == "chat"


async def test_study_500_response_is_sanitized_and_exception_is_logged(
    study_db, monkeypatch, caplog
):
    _add_study_session(study_db, "broken-state", owner="alice")
    secret_detail = "database failed with password=do-not-return"

    def fail_state(*_args, **_kwargs):
        raise RuntimeError(secret_detail)

    monkeypatch.setattr(study_routes, "get_study_state", fail_state)
    caplog.set_level("ERROR", logger="routes.study_routes")

    async with _client() as client:
        response = await client.get(
            _study_url("/api/study/state", "broken-state"),
            headers={"x-test-user": "alice"},
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Could not load Study workspace state."}
    assert secret_detail not in response.text
    assert secret_detail in caplog.text


async def test_automatic_title_derivation_cannot_run_again_after_goal_initialization(
    study_db,
):
    _add_study_session(study_db, "study-once", owner="alice", name="Study 1")
    headers = {"x-test-user": "alice"}
    async with _client() as client:
        first = await client.post(
            _study_url("/api/study/initialize", "study-once"),
            headers=headers,
            json={"prompt": "Teach me feedback control"},
        )

        # Simulate an intentional later rename that happens to look like the
        # original placeholder. An established goal is the durable one-time
        # marker, so a later chat prompt must not auto-name the workspace again.
        db = study_db()
        db.query(DbSession).filter_by(id="study-once").one().name = "Study 1"
        db.commit()
        db.close()

        second = await client.post(
            _study_url("/api/study/initialize", "study-once"),
            headers=headers,
            json={"prompt": "Teach me thermodynamics"},
        )

    assert first.json()["title_initialized"] is True
    assert second.status_code == 200
    assert second.json()["workspace_name"] == "Study 1"
    assert second.json()["goal_text"] == first.json()["goal_text"]
    assert second.json()["title_initialized"] is False
    assert second.json()["goal_initialized"] is False


async def test_switching_workspace_pauses_owner_timer_without_starting_on_entry(
    study_db, monkeypatch
):
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)
    _add_study_session(study_db, "alice-a", owner="alice", name="Study 1")
    _add_study_session(study_db, "alice-b", owner="alice", name="Study 2")
    _add_study_session(study_db, "bob-a", owner="bob", name="Study 1")
    assert _accept_study_prompt("alice", "alice-a")["timer_running"] is True
    assert _accept_study_prompt("bob", "bob-a")["timer_running"] is True

    async with _client() as client:
        clock.advance(90)
        switched = await client.post(
            _study_url("/api/study/initialize", "alice-b"),
            headers={"x-test-user": "alice"},
            json={},
        )
        started_after_prompt = _accept_study_prompt("alice", "alice-b")
        clock.advance(10)
        same_workspace = await client.post(
            _study_url("/api/study/initialize", "alice-b"),
            headers={"x-test-user": "alice"},
            json={},
        )
        switched_back = await client.post(
            _study_url("/api/study/initialize", "alice-a"),
            headers={"x-test-user": "alice"},
            json={},
        )

    assert switched.json()["timer_running"] is False
    assert switched.json()["timer_seconds"] == 0
    assert started_after_prompt["timer_running"] is True
    assert same_workspace.json()["timer_seconds"] == 10
    assert switched_back.json()["timer_seconds"] == 90
    assert switched_back.json()["timer_running"] is False

    db = study_db()
    rows = {row.id: row for row in db.query(StudyState).all()}
    db.close()
    assert rows["alice-a"].timer_running is False
    assert rows["alice-a"].current_session_seconds == 90
    assert rows["alice-b"].timer_running is False
    assert rows["alice-b"].current_session_seconds == 10
    assert rows["bob-a"].timer_running is True
    assert sum(row.timer_running for row in rows.values() if row.owner == "alice") == 0


async def test_tracker_keeps_effort_and_mastery_evidence_distinct(study_db):
    _add_study_session(study_db, "alice-study", owner="alice", name="Study 1")
    headers = {"x-test-user": "alice"}

    async with _client() as client:
        initialized = await client.post(
            _study_url("/api/study/initialize", "alice-study"),
            headers=headers,
            json={"prompt": "Learn state space control deeply"},
        )
        reviewed = await client.post(
            _study_url("/api/study/review", "alice-study"),
            headers=headers,
            json={"outcome": "clean"},
        )

    tracker = initialized.json()["tracker"]
    assert tracker["active_workspace"] == {
        "session_id": "alice-study",
        "title": "State space control",
        "mode": "study",
    }
    assert tracker["effort"]["progress_percent"] == initialized.json()[
        "progress_percent"
    ]
    assert tracker["mastery"]["status"] == "not_measured"
    assert "progress_percent" not in tracker["mastery"]
    assert "not timer time" in tracker["mastery"]["note"]
    assert reviewed.json()["tracker"]["mastery"]["status"] == "clean_recall"
    assert reviewed.json()["tracker"]["mastery"]["next_evidence"] == (
        "Apply the skill to one novel transfer task."
    )


def test_tracker_does_not_overstate_legacy_review_rows_without_evidence():
    tracker = study.build_study_tracker(
        {
            "session_id": "legacy",
            "goal_text": "Learn controls",
            "target_minutes": 60,
            "review": {"count": 1, "level": 1, "last_result": None},
        },
        "Controls",
    )

    assert tracker["mastery"]["status"] == "evidence_pending"
    assert tracker["mastery"]["next_evidence"] == (
        "Complete one closed-book recall check."
    )


async def test_first_open_bootstraps_goal_but_manual_timer_start_is_rejected(study_db):
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
    assert initial.json()["timer_running"] is False
    assert response.status_code == 400
    assert response.json() == {
        "detail": "Send a Study prompt to start or resume focus time."
    }
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

    started = _accept_study_prompt("alice", "alice-study")
    assert started["timer_running"] is True
    async with _client() as client:
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


async def test_prompt_lease_auto_pauses_at_ten_minutes_and_resumes_without_idle_gap(
    study_db, monkeypatch
):
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)
    headers = {"x-test-user": "alice"}
    _add_study_session(study_db, "alice-study", owner="alice")

    async with _client() as client:
        entered = await client.post(
            _study_url("/api/study/initialize", "alice-study"),
            headers=headers,
            json={"prompt": ""},
        )
        clock.advance(30)
        prompted = _accept_study_prompt("alice", "alice-study")
        clock.advance(599)
        almost_idle = await client.get(
            _study_url("/api/study/state", "alice-study"), headers=headers
        )
        clock.advance(1)
        idle = await client.get(
            _study_url("/api/study/state", "alice-study"), headers=headers
        )
        clock.advance(3_600)
        after_closed_tab_gap = await client.get(
            _study_url("/api/study/state", "alice-study"), headers=headers
        )
        resumed = _accept_study_prompt("alice", "alice-study", "okay")
        clock.advance(30)
        finished = await client.post(
            _study_url("/api/study/timer/finish", "alice-study"), headers=headers
        )

    assert entered.json()["timer_running"] is False
    assert prompted["last_prompt_at"] == "2026-07-14T09:00:30Z"
    assert prompted["idle_pause_at"] == "2026-07-14T09:10:30Z"
    assert almost_idle.json()["timer_running"] is True
    assert almost_idle.json()["timer_seconds"] == 599
    assert almost_idle.json()["idle_seconds_remaining"] == 1
    assert idle.json()["timer_running"] is False
    assert idle.json()["timer_seconds"] == 600
    assert idle.json()["idle_pause_at"] is None
    assert idle.json()["idle_seconds_remaining"] is None
    assert after_closed_tab_gap.json()["timer_seconds"] == 600
    assert resumed["timer_running"] is True
    assert resumed["timer_seconds"] == 600
    assert resumed["last_prompt_at"] == "2026-07-14T10:10:30Z"
    assert finished.json()["timer_running"] is False
    assert finished.json()["total_seconds"] == 630

    db = study_db()
    row = db.query(StudyState).filter_by(id="alice-study").one()
    assert row.timer_running is False
    assert row.timer_started_at is None
    assert row.current_session_seconds == 0
    assert row.total_seconds == 630
    db.close()


def test_legacy_entry_started_timer_without_prompt_clock_pauses_without_new_time(
    study_db, monkeypatch
):
    clock = _Clock(datetime(2026, 7, 14, 12, 0, 0))
    monkeypatch.setattr(study, "_now", clock)
    db = study_db()
    db.add(
        StudyState(
            id="legacy-study",
            owner="alice",
            goal_text="Controls",
            target_minutes=60,
            timer_running=True,
            timer_started_at=datetime(2026, 7, 14, 9, 0, 0),
            last_prompt_at=None,
        )
    )
    db.commit()
    legacy = db.query(StudyState).filter_by(id="legacy-study").one()
    serialized = study.serialize_study_state(legacy, now=clock.value)
    db.close()

    assert serialized["timer_running"] is False
    assert serialized["timer_seconds"] == 0

    state = study.get_study_state("alice", "legacy-study")

    assert state["timer_running"] is False
    assert state["timer_seconds"] == 0
    assert state["last_prompt_at"] is None
    db = study_db()
    row = db.query(StudyState).filter_by(id="legacy-study").one()
    assert row.timer_running is False
    assert row.timer_started_at is None
    assert row.current_session_seconds == 0
    db.close()


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
    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{tmp_path / 'study.db'}")

    cdb._migrate_assign_legacy_owner("alice")

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
    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{tmp_path / 'study.db'}")

    cdb._migrate_assign_legacy_owner("alice")

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


def test_prompt_start_pause_resume_finish_and_idempotence(study_db, monkeypatch):
    clock = _Clock(datetime(2026, 7, 14, 9, 0, 0))
    monkeypatch.setattr(study, "_now", clock)
    _add_study_session(study_db, "alice-study", owner="alice")
    study.save_study_goal("alice", "alice-study", "Dynamics", 15, None)

    started = _accept_study_prompt("alice", "alice-study")
    assert started["timer_running"] is True
    assert started["timer_seconds"] == 0

    clock.advance(90)
    started_again = _accept_study_prompt("alice", "alice-study", "okay")
    assert started_again["timer_seconds"] == 90
    assert started_again["idle_seconds_remaining"] == 600

    with pytest.raises(
        study.StudyGoalRequiredError,
        match="Send a Study prompt to start or resume focus time",
    ):
        study.start_study_timer("alice", "alice-study")

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

    resumed = _accept_study_prompt("alice", "alice-study")
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
    _add_study_session(study_db, "alice-study", owner="alice")
    study.save_study_goal("alice", "alice-study", "Controls", 15, None)
    _accept_study_prompt("alice", "alice-study")

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


def test_study_setup_sentinel_migration_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-study-setup.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE study_states (
            id TEXT PRIMARY KEY,
            goal_text TEXT NOT NULL DEFAULT '',
            target_minutes INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        "INSERT INTO study_states (id, goal_text, target_minutes) VALUES (?, ?, ?)",
        ("legacy", study.DEFAULT_STUDY_GOAL, study.DEFAULT_TARGET_MINUTES),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{db_path}")
    cdb._migrate_add_study_setup_initialized_column()
    cdb._migrate_add_study_setup_initialized_column()

    conn = sqlite3.connect(db_path)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(study_states)").fetchall()
    }
    initialized = conn.execute(
        "SELECT setup_initialized FROM study_states WHERE id = 'legacy'"
    ).fetchone()
    conn.close()

    assert "setup_initialized" in columns
    assert initialized == (0,)


def test_study_prompt_activity_migration_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy-study-prompt-activity.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE study_states (
            id TEXT PRIMARY KEY,
            timer_started_at DATETIME,
            timer_running BOOLEAN NOT NULL DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{db_path}")
    cdb._migrate_add_study_last_prompt_at_column()
    cdb._migrate_add_study_last_prompt_at_column()

    conn = sqlite3.connect(db_path)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(study_states)").fetchall()
    }
    conn.close()

    assert "last_prompt_at" in columns


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
