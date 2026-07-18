"""V3 calendar/time intelligence remains deterministic and proposal-only."""

from __future__ import annotations

import threading
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Account, Base
from routes import calendar_routes
from src.calendar_intelligence import calendar_time_report
from src.calendar_service import create_calendar_event
from src.life_graph import LifeGraphError, create_life_entity


class _IdentityAuthority:
    def __init__(self, *usernames: str):
        self._config_lock = threading.Lock()
        self._identity_migrations: set[str] = set()
        self.retired_usernames: set[str] = set()
        self.users = {name: {} for name in usernames}

    @property
    def is_configured(self) -> bool:
        return True


@pytest.fixture()
def time_env(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'calendar-time.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        alice = Account(id=str(uuid.uuid4()), username="alice", status="active", auth_epoch=1)
        bob = Account(id=str(uuid.uuid4()), username="bob", status="active", auth_epoch=1)
        db.add_all((alice, bob))
        db.commit()
        alice_id, bob_id = alice.id, bob.id
    monkeypatch.setattr(calendar_routes, "SessionLocal", factory)
    app = FastAPI()
    app.state.auth_manager = _IdentityAuthority("alice", "bob")

    @app.middleware("http")
    async def inject_identity(request, call_next):
        request.state.api_token = False
        request.state.current_user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(calendar_routes.setup_calendar_routes())
    yield SimpleNamespace(
        Session=factory, engine=engine, app=app,
        alice_id=alice_id, bob_id=bob_id,
    )
    engine.dispose()


def _account(db, env, name="alice"):
    account_id = env.alice_id if name == "alice" else env.bob_id
    return db.query(Account).filter(Account.id == account_id).one()


def _entity(db, account, entity_type, title):
    row, _ = create_life_entity(
        db, account=account, entity_type=entity_type, title=title,
        idempotency_key=f"calendar-time:{entity_type}:{title}",
    )
    return row


def test_calendar_time_report_covers_free_time_conflicts_context_and_buffers(time_env):
    with time_env.Session() as db:
        alice = _account(db, time_env)
        bob = _account(db, time_env, "bob")
        project = _entity(db, alice, "project", "V3 launch")
        task = _entity(db, alice, "task", "Finish migration verification")
        create_calendar_event(
            db, account=alice, summary="Recent review", event_type="meeting",
            dtstart="2026-07-16T14:00:00+05:30",
            dtend="2026-07-16T15:00:00+05:30",
        )
        create_calendar_event(
            db, account=alice, summary="Past implementation block", event_type="work",
            dtstart="2026-07-16T09:00:00+05:30",
            dtend="2026-07-16T10:00:00+05:30",
            linked_entity_ids=[task.id],
        )
        meeting = create_calendar_event(
            db, account=alice, summary="Launch meeting", event_type="meeting",
            importance="high", dtstart="2026-07-17T15:00:00+05:30",
            dtend="2026-07-17T16:00:00+05:30",
            linked_entity_ids=[project.id],
        )
        focus = create_calendar_event(
            db, account=alice, summary="Protected focus", event_type="focus",
            dtstart="2026-07-17T15:30:00+05:30",
            dtend="2026-07-17T17:00:00+05:30",
        )
        travel = create_calendar_event(
            db, account=alice, summary="Travel to campus", event_type="travel",
            dtstart="2026-07-18T09:00:00+05:30",
            dtend="2026-07-18T10:00:00+05:30",
        )
        adjacent = create_calendar_event(
            db, account=alice, summary="Lab", event_type="class",
            dtstart="2026-07-18T10:15:00+05:30",
            dtend="2026-07-18T12:00:00+05:30",
        )
        create_calendar_event(
            db, account=bob, summary="Bob private overlap", event_type="meeting",
            dtstart="2026-07-17T15:00:00+05:30",
            dtend="2026-07-17T18:00:00+05:30",
        )
        db.commit()

        report = calendar_time_report(
            db, owner_id=alice.id,
            as_of="2026-07-17T12:00:00+05:30",
            window_start="2026-07-16T00:00:00+05:30",
            window_end="2026-07-19T00:00:00+05:30",
            minimum_slot_minutes=30, daily_capacity_minutes=120,
            travel_buffer_minutes=30,
        )

    assert report["event_count"] == 6
    assert report["read_only"] is True
    assert report["can_reschedule_or_create"] is False
    assert report["free_slots"]
    assert report["suggested_time_blocks"][0]["reason"]
    assert report["conflicts"] == [{
        "event_ids": [meeting.event.uid, focus.event.uid],
        "overlap_start": "2026-07-17T15:30:00+05:30",
        "overlap_end": "2026-07-17T16:00:00+05:30",
        "reason": "Confirmed timed events overlap.",
    }]
    assert report["focus_protection"][0]["event_id"] == focus.event.uid
    assert report["meeting_preparation"][0]["event_id"] == meeting.event.uid
    assert report["meeting_preparation"][0]["linked_entities"][0]["id"] == project.id
    assert report["meeting_follow_up"][0]["summary"] == "Recent review"
    assert report["unfinished_work"][0]["task_ids"] == [task.id]
    assert report["travel_buffers"] == [{
        "event_id": travel.event.uid,
        "neighbor_event_id": adjacent.event.uid,
        "position": "after", "gap_minutes": 15,
        "required_buffer_minutes": 30,
        "reason": "Travel commitment lacks the requested transition buffer.",
    }]
    assert report["overcommitment"]
    assert "Bob" not in str(report)


def test_calendar_time_report_requires_one_explicit_offset(time_env):
    with time_env.Session() as db:
        with pytest.raises(LifeGraphError, match="explicit UTC offset"):
            calendar_time_report(
                db, owner_id=time_env.alice_id,
                as_of="2026-07-17T12:00:00",
                window_start="2026-07-17T00:00:00+05:30",
                window_end="2026-07-18T00:00:00+05:30",
            )
        with pytest.raises(LifeGraphError, match="same UTC offset"):
            calendar_time_report(
                db, owner_id=time_env.alice_id,
                as_of="2026-07-17T12:00:00+05:30",
                window_start="2026-07-17T00:00:00+05:30",
                window_end="2026-07-18T00:00:00+00:00",
            )


@pytest.mark.anyio
async def test_calendar_routes_expose_typed_context_and_read_only_intelligence(time_env):
    transport = httpx.ASGITransport(app=time_env.app)
    headers = {"x-user": "alice"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/api/calendar/events", headers=headers, json={
                "summary": "Deep work", "dtstart": "2026-07-17T09:00:00+05:30",
                "dtend": "2026-07-17T11:00:00+05:30",
                "event_type": "focus", "importance": "high",
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["event"]["event_type"] == "focus"
        report = await client.get(
            "/api/calendar/time-intelligence", headers=headers, params={
                "as_of": "2026-07-17T08:00:00+05:30",
                "start": "2026-07-17T06:00:00+05:30",
                "end": "2026-07-18T00:00:00+05:30",
            },
        )
        assert report.status_code == 200, report.text
        assert report.json()["event_count"] == 1
        assert report.json()["read_only"] is True
        assert report.json()["can_reschedule_or_create"] is False
