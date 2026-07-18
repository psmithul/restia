"""Focused contracts for V3 Health & Fitness typed records."""

from __future__ import annotations

import threading
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    ActionAudit,
    Base,
    LifeEntity,
    LifeEntityVersion,
)
from routes.life_routes import setup_life_routes
from src.health_service import (
    HEALTH_RECORD_TYPES,
    create_health_record,
    get_health_record,
    health_record_history,
    health_trends,
    list_health_records,
    search_health_records,
    serialize_health_record,
    update_health_record,
)
from src.identity import ensure_account
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    create_life_source,
)


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
def health_env(tmp_path):
    db_path = tmp_path / "health.db"
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


def _record_payload(record_type: str, **overrides):
    payload = {
        "record_type": record_type,
        "title": record_type.replace("_", " ").title(),
        "recorded_at": datetime(2026, 7, 1, 8),
        "metrics": [],
        "details": {},
        "source": {"kind": "manual", "label": "User entry"},
        "note": "Private health record",
        "private_metadata": {"context": "morning"},
        "provenance": {"capture": "manual"},
    }
    typed = {
        "weight": {"metrics": [{"name": "weight", "value": 72.5, "unit": "kg"}]},
        "measurement": {"metrics": [{"name": "waist", "value": 82, "unit": "cm"}]},
        "sleep": {
            "metrics": [{"name": "duration", "value": 7.5, "unit": "h"}],
            "ended_at": datetime(2026, 7, 1, 15, 30),
        },
        "exercise": {
            "metrics": [{"name": "duration", "value": 45, "unit": "min"}],
            "details": {"activity": "Cycling"},
        },
        "nutrition": {"metrics": [{"name": "energy", "value": 650, "unit": "kcal"}]},
        "steps": {"metrics": [{"name": "steps", "value": 9000, "unit": "steps"}]},
        "recovery": {"metrics": [{"name": "hrv", "value": 58, "unit": "ms"}]},
        "water": {"metrics": [{"name": "volume", "value": 500, "unit": "ml"}]},
        "medication_reminder": {
            "due_at": datetime(2026, 7, 1, 20),
            "source": {
                "kind": "prescription",
                "label": "Clinic prescription",
                "reference": "prescription-2026-07-01",
            },
            "details": {
                "externally_prescribed": True,
                "medication_name": "Recorded medication",
                "dosage": "As written by clinician",
                "schedule": "At 20:00",
                "prescribed_by": "Treating clinician",
            },
        },
        "appointment": {"details": {"provider": "Clinic"}},
        "report": {"details": {"report_type": "Laboratory report"}},
        "symptom": {"details": {"description": "Mild headache", "red_flags": []}},
        "mood_stress": {"metrics": [{"name": "stress", "value": 4, "unit": "score_0_10"}]},
        "wearable_observation": {
            "metrics": [{"name": "oxygen_saturation", "value": 98, "unit": "percent"}],
            "details": {"device": "User wearable"},
            "source": {"kind": "wearable", "label": "User wearable"},
        },
    }
    payload.update(typed[record_type])
    payload.update(overrides)
    return payload


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, **kwargs)


@pytest.mark.parametrize("record_type", sorted(HEALTH_RECORD_TYPES))
def test_all_bounded_health_record_types_use_canonical_life_entity(
    health_env, record_type
):
    db = health_env.Session()
    try:
        alice = _account(db, "alice")
        entity, created = create_health_record(
            db, account=alice, **_record_payload(record_type)
        )
        db.commit()

        assert created is True
        assert entity.entity_type == "health_record"
        assert entity.owner_id == alice.id
        assert entity.properties["health_schema_version"] == 1
        assert entity.properties["record_type"] == record_type
        assert entity.occurred_at == datetime(2026, 7, 1, 8)
        assert entity.sensitivity == "private"
        assert db.query(LifeEntity).filter_by(id=entity.id).one().id == entity.id
    finally:
        db.close()


def test_owner_scope_source_provenance_and_private_metadata(health_env):
    db = health_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        source, _ = create_life_source(
            db, account=alice, source_type="wearable", title="Alice device"
        )
        payload = _record_payload(
            "weight",
            source={
                "kind": "wearable",
                "label": "Alice device",
                "source_id": source.id,
                "external_id": "weight-1",
            },
            provenance={"source_id": source.id, "capture": "sync"},
            private_metadata={"device_location": "private"},
        )
        entity, _ = create_health_record(db, account=alice, **payload)
        db.commit()

        record = get_health_record(db, owner_id=alice.id, entity_id=entity.id)
        assert record["source"]["source_id"] == source.id
        assert record["provenance"]["source_kind"] == "wearable"
        assert record["private_metadata"] == {"device_location": "private"}
        with pytest.raises(LifeGraphNotFound, match="not found"):
            get_health_record(db, owner_id=bob.id, entity_id=entity.id)
        rows, _ = list_health_records(db, owner_id=bob.id)
        assert rows == []
    finally:
        db.close()


def test_health_validation_enforces_dates_units_sources_and_no_medical_advice_fields(
    health_env,
):
    db = health_env.Session()
    try:
        alice = _account(db, "alice")
        with pytest.raises(LifeGraphError, match="weight.weight must use"):
            create_health_record(
                db,
                account=alice,
                **_record_payload(
                    "weight",
                    metrics=[{"name": "weight", "value": 72, "unit": "cm"}],
                ),
            )
        with pytest.raises(LifeGraphError, match="whole numbers"):
            create_health_record(
                db,
                account=alice,
                **_record_payload(
                    "steps",
                    metrics=[{"name": "steps", "value": 12.5, "unit": "steps"}],
                ),
            )
        with pytest.raises(LifeGraphError, match="recorded_at is required"):
            create_health_record(
                db, account=alice, **_record_payload("weight", recorded_at=None)
            )
        with pytest.raises(LifeGraphError, match="record_type must be one of"):
            unsupported = _record_payload("weight")
            unsupported["record_type"] = "diagnosis"
            create_health_record(
                db, account=alice, **unsupported
            )
        with pytest.raises(LifeGraphError, match="cannot diagnose"):
            create_health_record(
                db,
                account=alice,
                **_record_payload(
                    "report",
                    details={"report_type": "External", "diagnosis": "Model guess"},
                ),
            )
        with pytest.raises(LifeGraphError, match="credentials or secrets"):
            create_health_record(
                db,
                account=alice,
                **_record_payload("weight", private_metadata={"api_token": "no"}),
            )
    finally:
        db.close()


def test_health_optimistic_version_audit_and_history(health_env):
    db = health_env.Session()
    try:
        alice = _account(db, "alice")
        entity, _ = create_health_record(db, account=alice, **_record_payload("weight"))
        updated = update_health_record(
            db,
            account=alice,
            entity_id=entity.id,
            expected_version=1,
            changes={
                "metrics": [{"name": "weight", "value": 71.8, "unit": "kg"}],
                "note": "Updated after a calibrated measurement",
            },
        )
        with pytest.raises(LifeGraphConflict, match="another client"):
            update_health_record(
                db,
                account=alice,
                entity_id=entity.id,
                expected_version=1,
                changes={"note": "Stale overwrite"},
            )
        db.commit()

        assert updated.version == 2
        history, truncated = health_record_history(
            db, owner_id=alice.id, entity_id=entity.id
        )
        assert truncated is False
        assert [row["version"] for row in history] == [2, 1]
        assert set(history[0]["changed_fields"]) == {"summary", "metrics"}
        assert db.query(LifeEntityVersion).filter_by(
            owner_id=alice.id, entity_id=entity.id
        ).count() == 2
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id, entity_id=entity.id
        ).count() == 2
    finally:
        db.close()


def test_health_search_and_trends_group_by_date_unit_and_owner(health_env):
    db = health_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        values = (
            ("Morning calibrated weight", datetime(2026, 7, 1, 8), 70.0),
            ("Evening weight", datetime(2026, 7, 1, 20), 72.0),
            ("Next day weight", datetime(2026, 7, 2, 8), 71.0),
        )
        for title, when, value in values:
            create_health_record(
                db,
                account=alice,
                **_record_payload(
                    "weight",
                    title=title,
                    recorded_at=when,
                    metrics=[{"name": "weight", "value": value, "unit": "kg"}],
                ),
            )
        create_health_record(
            db,
            account=bob,
            **_record_payload(
                "weight",
                title="Bob private weight",
                metrics=[{"name": "weight", "value": 999, "unit": "kg"}],
            ),
        )
        db.commit()

        found = search_health_records(
            db, owner_id=alice.id, query_text="calibrated", record_type="weight"
        )
        assert [row["record"]["title"] for row in found["items"]] == [
            "Morning calibrated weight"
        ]
        trend = health_trends(
            db,
            owner_id=alice.id,
            record_type="weight",
            metric="weight",
            group_by="day",
            unit="kg",
        )
        assert trend["count"] == 2
        assert trend["buckets"][0] == {
            "period_start": "2026-07-01",
            "unit": "kg",
            "count": 2,
            "minimum": 70.0,
            "maximum": 72.0,
            "average": 71.0,
            "sum": 142.0,
            "latest": 72.0,
        }
        assert max(bucket["maximum"] for bucket in trend["buckets"]) < 999
    finally:
        db.close()


def test_urgent_symptom_language_is_bounded_and_non_diagnostic(health_env):
    db = health_env.Session()
    try:
        alice = _account(db, "alice")
        entity, _ = create_health_record(
            db,
            account=alice,
            **_record_payload(
                "symptom",
                details={
                    "description": "Chest pain and difficulty breathing",
                    "red_flags": ["chest pain"],
                },
            ),
        )
        record = serialize_health_record(entity)
        assert record["safety"]["urgent"] is True
        assert {"chest_pain", "breathing_difficulty", "reported_red_flag"}.issubset(
            set(record["safety"]["signals"])
        )
        message = record["safety"]["message"].lower()
        assert "emergency services" in message
        assert "urgent in-person medical help" in message
        assert "cannot diagnose" in message
        assert "take " not in message
    finally:
        db.close()


def test_medication_reminders_only_record_external_facts_and_never_execute_changes(
    health_env,
):
    db = health_env.Session()
    try:
        alice = _account(db, "alice")
        with pytest.raises(LifeGraphError, match="prescription or provider source"):
            create_health_record(
                db,
                account=alice,
                **_record_payload(
                    "medication_reminder",
                    source={"kind": "manual", "label": "Model-generated"},
                ),
            )
        with pytest.raises(LifeGraphError, match="externally prescribed facts"):
            create_health_record(
                db,
                account=alice,
                **_record_payload(
                    "medication_reminder",
                    details={
                        "externally_prescribed": False,
                        "medication_name": "Unsafe",
                        "schedule": "Now",
                        "prescribed_by": "Nobody",
                    },
                ),
            )
        entity, _ = create_health_record(
            db, account=alice, **_record_payload("medication_reminder")
        )
        changed_details = dict(entity.properties["details"])
        changed_details["dosage"] = "A different recorded dose"
        with pytest.raises(LifeGraphError, match="explicit external prescription source"):
            update_health_record(
                db,
                account=alice,
                entity_id=entity.id,
                expected_version=1,
                changes={"details": changed_details},
            )
        updated = update_health_record(
            db,
            account=alice,
            entity_id=entity.id,
            expected_version=1,
            changes={
                "details": changed_details,
                "source": entity.properties["source"],
            },
        )
        assert updated.version == 2
        assert updated.properties["details"]["externally_prescribed"] is True
        assert "executor" not in updated.properties
    finally:
        db.close()


@pytest.mark.asyncio
async def test_health_routes_block_generic_bypass_and_support_import_filters(
    health_env,
):
    db = health_env.Session()
    try:
        _account(db, "alice")
        _account(db, "bob")
        db.commit()
    finally:
        db.close()

    payload = _record_payload("weight")
    payload["recorded_at"] = payload["recorded_at"].isoformat() + "Z"
    generic = await _call(
        health_env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "health_record",
            "title": "Bypass",
            "properties": {"health_schema_version": 1, "record_type": "weight"},
        },
    )
    assert generic.status_code == 400
    assert "/api/life/health/records" in generic.json()["detail"]

    created = await _call(
        health_env, "POST", "/api/life/health/records", json=payload
    )
    assert created.status_code == 201, created.text
    record = created.json()["record"]
    hidden = await _call(
        health_env, "GET", f"/api/life/health/records/{record['id']}", user="bob"
    )
    assert hidden.status_code == 404
    generic_update = await _call(
        health_env,
        "PATCH",
        f"/api/life/entities/{record['id']}",
        json={"version": 1, "summary": "Bypass typed validation"},
    )
    assert generic_update.status_code == 400
    generic_delete = await _call(
        health_env,
        "DELETE",
        f"/api/life/entities/{record['id']}",
        json={"version": 1},
    )
    assert generic_delete.status_code == 400

    imported = _record_payload(
        "steps",
        source={
            "kind": "wearable",
            "label": "Watch",
            "external_id": "watch-steps-2026-07-01",
        },
        idempotency_key="watch-steps-2026-07-01",
    )
    imported["recorded_at"] = imported["recorded_at"].isoformat() + "Z"
    response = await _call(
        health_env,
        "POST",
        "/api/life/health/import",
        json={"records": [imported]},
    )
    assert response.status_code == 201, response.text
    assert response.json()["created_count"] == 1
    filtered = await _call(
        health_env,
        "GET",
        "/api/life/health/records",
        params={"record_type": "steps", "source_kind": "wearable"},
    )
    assert filtered.status_code == 200, filtered.text
    assert [item["record_type"] for item in filtered.json()["items"]] == ["steps"]
