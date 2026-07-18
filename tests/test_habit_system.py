"""Focused contracts for V3 typed Habits & Routines."""

from __future__ import annotations

import threading
from datetime import date, datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import ActionAudit, Base, LifeEntity, LifeEntityVersion
from routes.life_routes import setup_life_routes
from src.habit_service import (
    ROUTINE_TYPES,
    create_habit,
    create_habit_log,
    expected_routine_dates,
    get_habit,
    get_habit_log,
    habit_history,
    habit_log_history,
    list_habit_logs,
    list_habits,
    missed_routine_report,
    search_habits,
    update_habit,
    update_habit_log,
    weekly_adjustment_report,
    weekly_habit_report,
)
from src.identity import ensure_account
from src.life_graph import LifeGraphConflict, LifeGraphError, LifeGraphNotFound


class _IdentityAuthority:
    def __init__(self, *usernames: str):
        self._config_lock = threading.Lock()
        self._identity_migrations: set[str] = set()
        self.retired_usernames: set[str] = set()
        self.users = {name: {} for name in usernames}

    @property
    def is_configured(self) -> bool:
        return bool(self.users)


@pytest.fixture()
def habit_env(tmp_path):
    db_path = tmp_path / "habits.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.state.auth_manager = _IdentityAuthority("alice", "bob")

    @app.middleware("http")
    async def inject_identity(request, call_next):
        request.state.api_token = False
        request.state.current_user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(setup_life_routes(session_factory=factory))
    yield SimpleNamespace(app=app, Session=factory, engine=engine)
    engine.dispose()


def _account(db, username: str):
    account = ensure_account(db, username)
    db.flush()
    return account


def _habit_payload(routine_type: str = "morning", **overrides):
    payload = {
        "title": f"{routine_type.title()} routine",
        "routine_type": routine_type,
        "schedule": {
            "cadence": "daily",
            "start_date": date(2026, 7, 6),
            "time_of_day": "08:00",
            "timezone": "Asia/Kolkata",
            "grace_minutes": 15,
        },
        "triggers": [{"kind": "time", "cue": "Morning alarm", "at": "08:00"}],
        "checklist": [
            {"id": "core", "label": "Complete the core step", "required": True},
            {"id": "optional", "label": "Optional extension", "required": False},
        ],
        "contexts": ["home", "low_energy"],
        "duration_minutes": 30,
        "minimum_viable": {
            "duration_minutes": 5,
            "checklist_item_ids": ["core"],
            "description": "Do the smallest useful version.",
        },
        "recovery_rules": {
            "strategy": "next_available",
            "window_hours": 24,
            "minimum_duration_minutes": 5,
            "note": "Recover without doubling the next routine.",
        },
        "note": "Private routine definition",
        "provenance": {"capture": "manual"},
    }
    payload.update(overrides)
    return payload


def _completed_log(habit_id: str, scheduled: date, **overrides):
    payload = {
        "habit_id": habit_id,
        "result": "completed",
        "logged_at": datetime.combine(scheduled, datetime.min.time()).replace(hour=9),
        "scheduled_for": scheduled,
        "quality": 80,
        "friction": 20,
        "failure_causes": [],
        "duration_minutes": 30,
        "checklist_evidence": [
            {"item_id": "core", "status": "completed", "note": "Observed"}
        ],
        "source": {"kind": "manual", "label": "User check-in"},
        "note": "Routine evidence",
        "provenance": {"capture": "manual"},
    }
    payload.update(overrides)
    return payload


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, **kwargs)


@pytest.mark.parametrize("routine_type", sorted(ROUTINE_TYPES))
def test_all_routine_types_use_owner_scoped_canonical_habit_entities(
    habit_env, routine_type
):
    db = habit_env.Session()
    try:
        alice = _account(db, "alice")
        entity, created = create_habit(
            db, account=alice, **_habit_payload(routine_type)
        )
        db.commit()

        assert created is True
        assert entity.entity_type == "habit"
        assert entity.owner_id == alice.id
        assert entity.properties["habit_schema_version"] == 1
        assert entity.properties["routine_type"] == routine_type
        assert entity.properties["schedule"]["start_date"] == "2026-07-06"
        assert entity.properties["minimum_viable"]["checklist_item_ids"] == ["core"]
        assert entity.properties["recovery_rules"]["strategy"] == "next_available"
        assert db.query(LifeEntity).filter_by(id=entity.id).one().id == entity.id
    finally:
        db.close()


def test_schedule_trigger_minimum_and_recovery_validation_are_bounded(habit_env):
    db = habit_env.Session()
    try:
        alice = _account(db, "alice")
        with pytest.raises(LifeGraphError, match="weekly schedules require days_of_week"):
            create_habit(
                db,
                account=alice,
                **_habit_payload(schedule={
                    "cadence": "weekly", "start_date": date(2026, 7, 6)
                }),
            )
        with pytest.raises(LifeGraphError, match="valid IANA timezone"):
            create_habit(
                db,
                account=alice,
                **_habit_payload(schedule={
                    "cadence": "daily", "start_date": date(2026, 7, 6),
                    "timezone": "Mars/Olympus_Mons",
                }),
            )
        with pytest.raises(LifeGraphError, match="time triggers require at"):
            create_habit(
                db, account=alice,
                **_habit_payload(triggers=[{"kind": "time", "cue": "Alarm"}]),
            )
        with pytest.raises(LifeGraphError, match="do not exist"):
            create_habit(
                db, account=alice,
                **_habit_payload(minimum_viable={
                    "duration_minutes": 5,
                    "checklist_item_ids": ["missing"],
                }),
            )
        with pytest.raises(LifeGraphError, match="cannot exceed duration_minutes"):
            create_habit(
                db, account=alice,
                **_habit_payload(recovery_rules={
                    "strategy": "next_available", "window_hours": 24,
                    "minimum_duration_minutes": 90,
                }),
            )
        with pytest.raises(LifeGraphError, match="autonomous or external actions"):
            create_habit(
                db, account=alice,
                **_habit_payload(provenance={"execute": "send a message"}),
            )
        with pytest.raises(LifeGraphError, match="credentials or secrets"):
            create_habit(
                db, account=alice,
                **_habit_payload(provenance={"api_token": "do-not-store"}),
            )
        weekly, _ = create_habit(
            db,
            account=alice,
            **_habit_payload(
                title="Monday routine",
                schedule={
                    "cadence": "weekly", "start_date": date(2026, 7, 6),
                    "days_of_week": [0],
                },
            ),
        )
        with pytest.raises(LifeGraphError, match="occurrence in the habit schedule"):
            create_habit_log(
                db, account=alice,
                **_completed_log(weekly.id, date(2026, 7, 7)),
            )
    finally:
        db.close()


@pytest.mark.parametrize(
    ("schedule", "expected"),
    [
        (
            {"cadence": "weekdays", "start_date": date(2026, 7, 6)},
            [6, 7, 8, 9, 10],
        ),
        (
            {
                "cadence": "weekly", "start_date": date(2026, 7, 6),
                "days_of_week": [0, 2],
            },
            [6, 8],
        ),
        (
            {
                "cadence": "interval", "start_date": date(2026, 7, 6),
                "interval_days": 3,
            },
            [6, 9, 12],
        ),
        (
            {
                "cadence": "custom", "start_date": date(2026, 7, 6),
                "days_of_week": [1, 4],
            },
            [7, 10],
        ),
    ],
)
def test_schedule_cadences_produce_deterministic_expected_dates(
    habit_env, schedule, expected
):
    db = habit_env.Session()
    try:
        alice = _account(db, "alice")
        habit, _ = create_habit(
            db, account=alice, **_habit_payload(schedule=schedule)
        )
        assert [day.day for day in expected_routine_dates(
            habit, from_date=date(2026, 7, 6), to_date=date(2026, 7, 12)
        )] == expected
    finally:
        db.close()


def test_habit_owner_isolation_idempotency_cas_audit_and_history(habit_env):
    db = habit_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        payload = _habit_payload(idempotency_key="morning-routine-v1")
        entity, created = create_habit(db, account=alice, **payload)
        replay, replay_created = create_habit(db, account=alice, **payload)
        assert entity.id == replay.id
        assert created is True and replay_created is False
        updated = update_habit(
            db, account=alice, entity_id=entity.id, expected_version=1,
            changes={"note": "Updated after weekly review", "status": "paused"},
        )
        with pytest.raises(LifeGraphConflict, match="another client"):
            update_habit(
                db, account=alice, entity_id=entity.id, expected_version=1,
                changes={"note": "Stale overwrite"},
            )
        db.commit()

        assert updated.version == 2
        assert updated.status == "paused"
        with pytest.raises(LifeGraphNotFound, match="Habit not found"):
            get_habit(db, owner_id=bob.id, entity_id=entity.id)
        rows, _ = list_habits(db, owner_id=bob.id)
        assert rows == []
        history, truncated = habit_history(
            db, owner_id=alice.id, entity_id=entity.id
        )
        assert truncated is False
        assert [row["version"] for row in history] == [2, 1]
        assert set(history[0]["changed_fields"]) == {"summary", "status"}
        assert db.query(LifeEntityVersion).filter_by(
            owner_id=alice.id, entity_id=entity.id
        ).count() == 2
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id, entity_id=entity.id
        ).count() == 2
    finally:
        db.close()


@pytest.mark.parametrize("result", ["completed", "partial", "skipped", "missed"])
def test_typed_habit_logs_capture_result_quality_friction_failure_and_evidence(
    habit_env, result
):
    db = habit_env.Session()
    try:
        alice = _account(db, "alice")
        habit, _ = create_habit(db, account=alice, **_habit_payload())
        payload = _completed_log(habit.id, date(2026, 7, 6), result=result)
        if result == "partial":
            payload.update({
                "quality": 55, "friction": 70, "duration_minutes": 10,
                "failure_causes": ["Time pressure"],
                "checklist_evidence": [{
                    "item_id": "core", "status": "partial", "note": "Started"
                }],
            })
        elif result in {"skipped", "missed"}:
            payload.update({
                "quality": None, "friction": 90, "duration_minutes": 0,
                "failure_causes": ["Unexpected travel"],
                "checklist_evidence": [],
            })
        entity, created = create_habit_log(db, account=alice, **payload)
        db.commit()

        assert created is True
        assert entity.entity_type == "metric"
        assert entity.owner_id == alice.id
        assert entity.properties["habit_log_schema_version"] == 1
        record = get_habit_log(db, owner_id=alice.id, entity_id=entity.id)
        assert record["result"] == result
        assert record["friction"] == payload["friction"]
        assert record["failure_causes"] == payload["failure_causes"]
        assert record["checklist_evidence"] == payload["checklist_evidence"]
    finally:
        db.close()


def test_recovery_log_requires_owned_matching_missed_log_and_log_cas(habit_env):
    db = habit_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        habit, _ = create_habit(db, account=alice, **_habit_payload())
        missed, _ = create_habit_log(
            db, account=alice,
            **_completed_log(
                habit.id, date(2026, 7, 7), result="missed", quality=None,
                friction=85, duration_minutes=0,
                failure_causes=["Late meeting"], checklist_evidence=[],
            ),
        )
        bob_habit, _ = create_habit(
            db, account=bob, **_habit_payload(title="Bob routine")
        )
        bob_missed, _ = create_habit_log(
            db, account=bob,
            **_completed_log(
                bob_habit.id, date(2026, 7, 7), result="missed", quality=None,
                friction=80, duration_minutes=0,
                failure_causes=["Private cause"], checklist_evidence=[],
            ),
        )
        with pytest.raises(LifeGraphNotFound, match="not found"):
            create_habit_log(
                db, account=alice,
                **_completed_log(
                    habit.id, date(2026, 7, 7), result="recovered",
                    recovery_of_log_id=bob_missed.id,
                ),
            )
        recovered, _ = create_habit_log(
            db, account=alice,
            **_completed_log(
                habit.id, date(2026, 7, 7), result="recovered",
                recovery_of_log_id=missed.id,
            ),
        )
        corrected = update_habit_log(
            db, account=alice, entity_id=recovered.id, expected_version=1,
            changes={"quality": 90, "note": "Recovery evidence corrected"},
        )
        with pytest.raises(LifeGraphConflict, match="another client"):
            update_habit_log(
                db, account=alice, entity_id=recovered.id, expected_version=1,
                changes={"quality": 50},
            )
        db.commit()

        assert corrected.version == 2
        history, _ = habit_log_history(
            db, owner_id=alice.id, entity_id=recovered.id
        )
        assert [row["version"] for row in history] == [2, 1]
        assert set(history[0]["changed_fields"]) == {"summary", "quality"}
    finally:
        db.close()


def test_weekly_consistency_streak_recovery_missed_and_adjustment_are_deterministic(
    habit_env,
):
    db = habit_env.Session()
    try:
        alice = _account(db, "alice")
        habit, _ = create_habit(db, account=alice, **_habit_payload())
        create_habit_log(
            db, account=alice,
            **_completed_log(habit.id, date(2026, 7, 6)),
        )
        create_habit_log(
            db, account=alice,
            **_completed_log(
                habit.id, date(2026, 7, 7), result="partial", quality=60,
                friction=70, duration_minutes=10,
                failure_causes=["Time pressure"],
                checklist_evidence=[{
                    "item_id": "core", "status": "partial", "note": "Started"
                }],
            ),
        )
        missed, _ = create_habit_log(
            db, account=alice,
            **_completed_log(
                habit.id, date(2026, 7, 8), result="missed", quality=None,
                friction=80, duration_minutes=0,
                failure_causes=["Travel"], checklist_evidence=[],
            ),
        )
        create_habit_log(
            db, account=alice,
            **_completed_log(
                habit.id, date(2026, 7, 8), result="recovered", quality=75,
                friction=40, duration_minutes=20,
                recovery_of_log_id=missed.id,
            ),
        )
        create_habit_log(
            db, account=alice,
            **_completed_log(habit.id, date(2026, 7, 9)),
        )
        db.commit()

        report = weekly_habit_report(
            db, owner_id=alice.id, week_start=date(2026, 7, 6), habit_id=habit.id
        )
        stats = report["items"][0]
        assert stats == {
            "habit_id": habit.id,
            "title": "Morning routine",
            "routine_type": "morning",
            "expected_count": 7,
            "completed_count": 2,
            "partial_count": 1,
            "skipped_count": 0,
            "missed_count": 3,
            "recovered_count": 1,
            "consistency_percent": 50.0,
            "current_streak": 0,
            "longest_streak": 2,
            "recovery_percent": 25.0,
            "average_quality": 73.8,
            "average_friction": 37.5,
            "failure_causes": [{"cause": "Time pressure", "count": 1}],
            "scan_truncated": False,
        }
        missed_report = missed_routine_report(
            db, owner_id=alice.id,
            as_of=datetime(2026, 7, 10, 6, 30, tzinfo=timezone.utc),
            lookback_days=7, habit_id=habit.id,
        )
        assert [(row["scheduled_for"], row["state"]) for row in missed_report["items"]] == [
            ("2026-07-10", "missing_log")
        ]
        assert "IANA schedule timezone" in missed_report["time_basis"]
        with pytest.raises(LifeGraphError, match="must include a UTC offset"):
            missed_routine_report(
                db, owner_id=alice.id, as_of=datetime(2026, 7, 10, 12),
                lookback_days=7, habit_id=habit.id,
            )
        adjustment = weekly_adjustment_report(
            db, owner_id=alice.id, week_start=date(2026, 7, 6),
            habit_id=habit.id,
        )
        assert adjustment["execution_policy"] == {
            "record_only": True, "can_apply_automatically": False,
        }
        assert adjustment["items"][0]["evidence"]["consistency_percent"] == 50.0
    finally:
        db.close()


def test_habit_search_and_log_filters_never_cross_owner(habit_env):
    db = habit_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        habit, _ = create_habit(
            db, account=alice,
            **_habit_payload(title="Calibrated morning mobility"),
        )
        bob_habit, _ = create_habit(
            db, account=bob, **_habit_payload(title="Bob private routine")
        )
        historical_payload = _completed_log(
            habit.id, date(2026, 7, 6), note="Mobility evidence",
            idempotency_key="historical-mobility-log",
        )
        historical_log, _ = create_habit_log(
            db, account=alice, **historical_payload,
        )
        create_habit_log(
            db, account=bob,
            **_completed_log(bob_habit.id, date(2026, 7, 6), note="Bob evidence"),
        )
        update_habit(
            db, account=alice, entity_id=habit.id, expected_version=1,
            changes={
                "checklist": [{
                    "id": "new_core", "label": "Revised core step", "required": True
                }],
                "minimum_viable": {
                    "duration_minutes": 5,
                    "checklist_item_ids": ["new_core"],
                    "description": "Revised smallest version",
                },
            },
        )
        replayed_log, replay_created = create_habit_log(
            db, account=alice, **historical_payload,
        )
        assert replayed_log.id == historical_log.id
        assert replay_created is False
        db.commit()

        found = search_habits(
            db, owner_id=alice.id, query_text="calibrated"
        )
        assert [row["habit"]["title"] for row in found["items"]] == [
            "Calibrated morning mobility"
        ]
        logs, _ = list_habit_logs(db, owner_id=alice.id, habit_id=habit.id)
        assert [row["note"] for row in logs] == ["Mobility evidence"]
        historical = get_habit_log(
            db, owner_id=alice.id, entity_id=historical_log.id
        )
        assert historical["checklist_evidence"][0]["item_id"] == "core"
        assert historical["checklist_snapshot"][0]["label"] == "Complete the core step"
        with pytest.raises(LifeGraphNotFound, match="Habit not found"):
            list_habit_logs(db, owner_id=bob.id, habit_id=habit.id)
    finally:
        db.close()


@pytest.mark.anyio
async def test_habit_routes_crud_reports_and_generic_bypass(habit_env):
    payload = _habit_payload(idempotency_key="api-morning-v1")
    payload["schedule"]["start_date"] = "2026-07-06"
    created = await _call(habit_env, "POST", "/api/life/habits", json=payload)
    assert created.status_code == 201, created.text
    habit = created.json()["habit"]
    assert habit["execution_policy"] == {
        "record_only": True, "can_execute_external_action": False,
    }
    replay = await _call(habit_env, "POST", "/api/life/habits", json=payload)
    assert replay.status_code == 201, replay.text
    assert replay.json()["created"] is False

    hidden = await _call(
        habit_env, "GET", f"/api/life/habits/{habit['id']}", user="bob"
    )
    assert hidden.status_code == 404
    updated = await _call(
        habit_env, "PATCH", f"/api/life/habits/{habit['id']}",
        json={"version": 1, "note": "API weekly review"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["habit"]["version"] == 2

    log_payload = _completed_log(habit["id"], date(2026, 7, 6))
    log_payload["logged_at"] = log_payload["logged_at"].isoformat() + "Z"
    log_payload["scheduled_for"] = log_payload["scheduled_for"].isoformat()
    log_payload["idempotency_key"] = "api-log-1"
    log_created = await _call(
        habit_env, "POST", "/api/life/habits/logs", json=log_payload
    )
    assert log_created.status_code == 201, log_created.text
    log = log_created.json()["log"]
    assert log["result"] == "completed"
    assert (await _call(
        habit_env, "GET", f"/api/life/habits/logs/{log['id']}"
    )).status_code == 200

    legacy_untyped = await _call(
        habit_env, "POST", "/api/life/entities", json={
            "entity_type": "habit", "title": "Legacy untyped habit",
            "properties": {"legacy_capture": True},
        },
    )
    assert legacy_untyped.status_code == 201, legacy_untyped.text
    generic_habit_create = await _call(
        habit_env, "POST", "/api/life/entities", json={
            "entity_type": "habit", "title": "Bypass",
            "properties": {
                "habit_schema_version": 1,
                "routine_type": "morning",
                "schedule": {"cadence": "daily", "start_date": "2026-07-06"},
            },
        },
    )
    assert generic_habit_create.status_code == 400
    assert "/api/life/habits" in generic_habit_create.text
    generic_log_create = await _call(
        habit_env, "POST", "/api/life/entities", json={
            "entity_type": "metric", "title": "Bypass",
            "properties": {"habit_log_schema_version": 1, "habit_id": habit["id"]},
        },
    )
    assert generic_log_create.status_code == 400
    for entity_id, version in ((habit["id"], 2), (log["id"], 1)):
        generic_update = await _call(
            habit_env, "PATCH", f"/api/life/entities/{entity_id}",
            json={"version": version, "summary": "Bypass"},
        )
        assert generic_update.status_code == 400
        generic_delete = await _call(
            habit_env, "DELETE", f"/api/life/entities/{entity_id}",
            json={"version": version},
        )
        assert generic_delete.status_code == 400

    for path in (
        "/api/life/habits",
        "/api/life/habits/search?q=morning",
        f"/api/life/habits/logs?habit_id={habit['id']}",
        "/api/life/habits/logs/search?q=evidence",
        f"/api/life/habits/reports/weekly?week_start=2026-07-06&habit_id={habit['id']}",
        f"/api/life/habits/reports/missed?as_of=2026-07-07T06:30:00Z&habit_id={habit['id']}",
        f"/api/life/habits/reports/weekly-adjustment?week_start=2026-07-06&habit_id={habit['id']}",
        f"/api/life/habits/{habit['id']}/history",
        f"/api/life/habits/logs/{log['id']}/history",
    ):
        response = await _call(habit_env, "GET", path)
        assert response.status_code == 200, (path, response.text)

    removed_log = await _call(
        habit_env, "DELETE", f"/api/life/habits/logs/{log['id']}",
        json={"version": 1, "reason": "Duplicate observation"},
    )
    assert removed_log.status_code == 200, removed_log.text
    assert removed_log.json()["log"]["status"] == "deleted"
    removed_habit = await _call(
        habit_env, "DELETE", f"/api/life/habits/{habit['id']}",
        json={"version": 2, "reason": "Routine retired"},
    )
    assert removed_habit.status_code == 200, removed_habit.text
    assert removed_habit.json()["habit"]["status"] == "deleted"
