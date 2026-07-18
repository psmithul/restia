"""Focused contracts for V3 typed Travel and deterministic Travel Mode."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    ActionAudit,
    Base,
    CalendarCal,
    CalendarEvent,
    LifeEntity,
    LifeEntityVersion,
)
from routes.life_routes import setup_life_routes
from routes.travel_routes import setup_travel_routes
from src.identity import ensure_account
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    create_life_entity,
    create_life_source,
)
from src.travel_service import (
    TRAVEL_RECORD_KINDS,
    create_travel_record,
    delete_travel_record,
    get_travel_record,
    list_travel_records,
    travel_mode,
    travel_record_history,
    update_travel_record,
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
def travel_env(tmp_path):
    db_path = tmp_path / "travel.db"
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
    app.include_router(setup_travel_routes(session_factory=factory))
    yield SimpleNamespace(app=app, Session=factory, engine=engine, db_path=db_path)
    engine.dispose()


def _account(db, username: str):
    account = ensure_account(db, username)
    db.flush()
    return account


def _trip(db, account, *, title="Bengaluru to Tokyo", **overrides):
    payload = {
        "record_kind": "trip",
        "title": title,
        "summary": "Private trip context",
        "starts_at": "2026-07-20T09:00:00+05:30",
        "ends_at": "2026-07-25T18:00:00+09:00",
        "details": {
            "destination": "Tokyo, Japan",
            "trip_timezone": "Asia/Tokyo",
            "purpose": "Conference",
            "country_codes": ["jp"],
        },
        "offline_available": True,
        "provenance": {"capture": "manual"},
    }
    payload.update(overrides)
    return create_travel_record(db, account=account, **payload)[0]


_CHILD_DETAILS = {
    "research": {"topic": "Rail passes", "finding": "Regional pass is enough"},
    "budget": {"category": "trip_total", "amount": "1200", "currency": "USD"},
    "transport": {
        "mode": "flight", "carrier": "Example Air", "service_number": "EA42",
        "origin": "BLR", "destination": "NRT", "reference": "TRIP-REF-42",
    },
    "lodging": {"name": "Example Hotel", "address": "Shinjuku, Tokyo"},
    "visa": {"jurisdiction": "Japan", "visa_status": "approved"},
    "itinerary": {"day_label": "Day 1", "activity": "Conference registration"},
    "packing": {"category": "documents", "item": "Passport", "quantity": 1, "packed": True},
    "reservation": {"reservation_type": "restaurant", "provider": "Example Bistro"},
    "local_transport": {"mode": "train", "origin": "NRT", "destination": "Shinjuku"},
    "document": {"document_kind": "insurance", "storage_ref": "vault://travel/insurance.pdf"},
    "contact": {"name": "Conference Desk", "role": "organizer", "email": "desk@example.test"},
    "expense": {"category": "transport", "amount": "24.50", "currency": "USD"},
    "calendar_reference": {"label": "Trip calendar event"},
}


def _calendar_event(db, account, *, uid: str):
    calendar = CalendarCal(
        id=f"calendar-{uid}", owner_id=account.id, owner=account.username,
        name="Travel", source="local",
    )
    event = CalendarEvent(
        uid=uid,
        owner_id=account.id,
        calendar_id=calendar.id,
        summary="Travel event",
        dtstart=datetime(2026, 7, 20, 3, 30),
        dtend=datetime(2026, 7, 20, 5, 30),
        is_utc=True,
    )
    db.add_all([calendar, event])
    db.flush()
    return event


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, **kwargs)


def test_all_required_travel_kinds_use_encrypted_account_owned_life_entities(travel_env):
    private_phrase = "unique-private-travel-phrase-6f1ac"
    db = travel_env.Session()
    try:
        alice = _account(db, "alice")
        source, _ = create_life_source(
            db, account=alice, source_type="travel_research", title="Trip research"
        )
        related, _ = create_life_entity(
            db, account=alice, entity_type="note", title="Travel note"
        )
        event = _calendar_event(db, alice, uid="travel-event")
        trip = _trip(db, alice, summary=private_phrase)
        created_kinds = {"trip"}
        for kind, details in _CHILD_DETAILS.items():
            entity, created = create_travel_record(
                db,
                account=alice,
                record_kind=kind,
                trip_id=trip.id,
                title=f"{kind} record",
                details=details,
                offline_available=kind in {"packing", "document", "contact"},
                related_entity_ids=[related.id],
                source_ids=[source.id],
                calendar_event_ids=[event.uid] if kind == "calendar_reference" else [],
                provenance={"source_id": source.id, "capture": "manual"},
            )
            assert created is True
            assert entity.owner_id == alice.id
            assert entity.entity_type == "trip"
            assert entity.properties["travel_schema_version"] == 1
            created_kinds.add(entity.properties["record_kind"])
        db.commit()

        assert created_kinds == TRAVEL_RECORD_KINDS
        items, truncated = list_travel_records(db, owner_id=alice.id, limit=100)
        assert truncated is False
        assert {item["record_kind"] for item in items} == TRAVEL_RECORD_KINDS
        assert all(item["execution_policy"]["can_book"] is False for item in items)
        assert all(item["execution_policy"]["uses_network"] is False for item in items)
    finally:
        db.close()

    raw = travel_env.db_path.read_bytes()
    assert private_phrase.encode() not in raw
    assert b"Tokyo, Japan" not in raw


def test_reference_authority_and_owner_isolation_fail_closed(travel_env):
    db = travel_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        alice_trip = _trip(db, alice)
        bob_trip = _trip(db, bob, title="Bob private trip")
        bob_source, _ = create_life_source(
            db, account=bob, source_type="note", title="Bob source"
        )
        bob_entity, _ = create_life_entity(
            db, account=bob, entity_type="note", title="Bob note"
        )
        bob_event = _calendar_event(db, bob, uid="bob-event")
        db.flush()

        with pytest.raises(LifeGraphNotFound, match="trip context"):
            create_travel_record(
                db, account=alice, record_kind="research", trip_id=bob_trip.id,
                title="Cross-owner trip", details=_CHILD_DETAILS["research"],
            )
        with pytest.raises(LifeGraphNotFound, match="LifeSource"):
            create_travel_record(
                db, account=alice, record_kind="research", trip_id=alice_trip.id,
                title="Cross-owner source", details=_CHILD_DETAILS["research"],
                source_ids=[bob_source.id],
            )
        with pytest.raises(LifeGraphNotFound, match="LifeEntity"):
            create_travel_record(
                db, account=alice, record_kind="research", trip_id=alice_trip.id,
                title="Cross-owner entity", details=_CHILD_DETAILS["research"],
                related_entity_ids=[bob_entity.id],
            )
        with pytest.raises(LifeGraphNotFound, match="calendar event"):
            create_travel_record(
                db, account=alice, record_kind="calendar_reference",
                trip_id=alice_trip.id, title="Cross-owner event",
                details=_CHILD_DETAILS["calendar_reference"],
                calendar_event_ids=[bob_event.uid],
            )
        with pytest.raises(LifeGraphNotFound, match="Domain record"):
            create_travel_record(
                db, account=alice, record_kind="research", trip_id=alice_trip.id,
                title="Cross-owner domain ref", details=_CHILD_DETAILS["research"],
                domain_ref_type="life_source", domain_ref_id=bob_source.id,
            )

        alice_source, _ = create_life_source(
            db, account=alice, source_type="note", title="Alice domain source"
        )
        domain_record, _ = create_travel_record(
            db, account=alice, record_kind="research", trip_id=alice_trip.id,
            title="Owned domain ref", details=_CHILD_DETAILS["research"],
            domain_ref_type="life_source", domain_ref_id=alice_source.id,
        )
        assert get_travel_record(
            db, owner_id=alice.id, entity_id=domain_record.id
        )["domain_ref_id"] == alice_source.id
        # A later authority drift is re-checked on reads instead of exposing a
        # stale pointer that no longer belongs to the Travel record owner.
        alice_source.owner_id = bob.id
        db.flush()
        with pytest.raises(LifeGraphNotFound, match="Travel domain record"):
            get_travel_record(db, owner_id=alice.id, entity_id=domain_record.id)

        with pytest.raises(LifeGraphNotFound):
            get_travel_record(db, owner_id=bob.id, entity_id=alice_trip.id)
        assert list_travel_records(
            db, owner_id=bob.id, record_kind="trip"
        )[0][0]["id"] == bob_trip.id
        assert travel_mode(
            db, owner_id=bob.id, as_of="2026-07-21T00:00:00+09:00"
        )["current_trips"][0]["id"] == bob_trip.id
    finally:
        db.close()


def test_travel_update_is_cas_guarded_audited_historic_and_soft_deleted(travel_env):
    db = travel_env.Session()
    try:
        alice = _account(db, "alice")
        trip = _trip(db, alice)
        record, _ = create_travel_record(
            db, account=alice, record_kind="research", trip_id=trip.id,
            title="Rail research", details=_CHILD_DETAILS["research"],
        )
        updated = update_travel_record(
            db,
            account=alice,
            entity_id=record.id,
            expected_version=1,
            changes={
                "details": {"topic": "Rail passes", "finding": "Buy locally"},
                "offline_available": True,
            },
        )
        assert updated.version == 2
        with pytest.raises(LifeGraphConflict):
            update_travel_record(
                db, account=alice, entity_id=record.id, expected_version=1,
                changes={"title": "Stale update"},
            )
        items, truncated = travel_record_history(
            db, owner_id=alice.id, entity_id=record.id
        )
        assert truncated is False
        assert [item["version"] for item in items] == [2, 1]
        assert db.query(LifeEntityVersion).filter_by(
            owner_id=alice.id, entity_id=record.id
        ).count() == 2
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id, entity_id=record.id
        ).count() >= 2

        deleted = delete_travel_record(
            db, owner_id=alice.id, entity_id=record.id,
            expected_version=2, reason="No longer relevant",
        )
        assert deleted.version == 3
        assert deleted.deleted_at is not None
        with pytest.raises(LifeGraphNotFound):
            get_travel_record(db, owner_id=alice.id, entity_id=record.id)
        assert travel_record_history(
            db, owner_id=alice.id, entity_id=record.id
        )[0][0]["version"] == 3
    finally:
        db.close()


def test_travel_mode_uses_explicit_instant_and_handles_timezone_boundaries(travel_env):
    db = travel_env.Session()
    try:
        alice = _account(db, "alice")
        current = _trip(
            db,
            alice,
            title="Boundary trip",
            starts_at="2026-07-20T00:00:00+05:30",
            ends_at="2026-07-21T00:00:00+05:30",
        )
        upcoming = _trip(
            db,
            alice,
            title="Next trip",
            starts_at="2026-08-01T08:00:00-04:00",
            ends_at="2026-08-02T08:00:00-04:00",
            details={
                "destination": "New York", "trip_timezone": "America/New_York",
            },
        )

        at_start = travel_mode(
            db, owner_id=alice.id, as_of="2026-07-19T18:30:00Z"
        )
        assert [row["id"] for row in at_start["current_trips"]] == [current.id]
        assert [row["id"] for row in at_start["next_trips"]] == [upcoming.id]

        at_end = travel_mode(
            db, owner_id=alice.id, as_of="2026-07-20T18:30:00+00:00"
        )
        assert at_end["current_trips"] == []
        assert [row["id"] for row in at_end["next_trips"]] == [upcoming.id]
        with pytest.raises(LifeGraphError, match="explicit UTC offset"):
            travel_mode(db, owner_id=alice.id, as_of=datetime(2026, 7, 20, 12, 0))
    finally:
        db.close()


def test_travel_mode_offline_filter_is_bounded_and_reports_document_availability(travel_env):
    db = travel_env.Session()
    try:
        alice = _account(db, "alice")
        trip = _trip(db, alice)
        create_travel_record(
            db, account=alice, record_kind="research", trip_id=trip.id,
            title="Online-only research", details=_CHILD_DETAILS["research"],
            offline_available=False,
        )
        create_travel_record(
            db, account=alice, record_kind="packing", trip_id=trip.id,
            title="Offline packing", details=_CHILD_DETAILS["packing"],
            offline_available=True,
        )
        offline_doc, _ = create_travel_record(
            db, account=alice, record_kind="document", trip_id=trip.id,
            title="Offline insurance", details=_CHILD_DETAILS["document"],
            offline_available=True,
        )
        create_travel_record(
            db, account=alice, record_kind="document", trip_id=trip.id,
            title="Cloud-only visa", details={"document_kind": "visa"},
            offline_available=False,
        )

        offline = travel_mode(
            db,
            owner_id=alice.id,
            as_of="2026-07-21T12:00:00+09:00",
            offline_only=True,
            fact_limit=100,
        )
        assert {fact["record_kind"] for fact in offline["facts"]} == {
            "packing", "document",
        }
        assert [document["id"] for document in offline["documents"]] == [offline_doc.id]
        assert offline["document_availability"] == {
            "total": 2, "available_offline": 1, "unavailable_offline": 1,
        }
        assert offline["execution_policy"]["uses_network"] is False

        all_facts = travel_mode(
            db,
            owner_id=alice.id,
            as_of="2026-07-21T12:00:00+09:00",
            offline_only=False,
            fact_limit=1,
        )
        assert len(all_facts["facts"]) == 1
        assert all_facts["bounds"]["truncated"] is True
        assert len(all_facts["documents"]) == 1  # same fact_limit bound
    finally:
        db.close()


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("details", {"topic": "Rail", "api_token": "secret"}, "credentials"),
        ("details", {"topic": "Rail", "execute": "book a ticket"}, "action payloads"),
        ("provenance", {"send_email": {"to": "agent@example.test"}}, "action payloads"),
        (
            "summary",
            "Charge card 4111 1111 1111 1111",
            "payment-card data",
        ),
    ],
)
def test_travel_rejects_secrets_payment_data_and_action_payloads(
    travel_env, field, value, error
):
    db = travel_env.Session()
    try:
        alice = _account(db, "alice")
        trip = _trip(db, alice)
        payload = {
            "record_kind": "research",
            "trip_id": trip.id,
            "title": "Unsafe record",
            "details": _CHILD_DETAILS["research"],
        }
        payload[field] = value
        with pytest.raises(LifeGraphError, match=error):
            create_travel_record(db, account=alice, **payload)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_travel_api_owner_scope_strict_models_and_generic_bypass(travel_env):
    trip_payload = {
        "record_kind": "trip",
        "title": "API trip",
        "starts_at": "2026-07-20T09:00:00+05:30",
        "ends_at": "2026-07-25T18:00:00+09:00",
        "details": {
            "destination": "Tokyo", "trip_timezone": "Asia/Tokyo",
        },
        "offline_available": True,
    }
    response = await _call(travel_env, "POST", "/api/life/travel/records", json=trip_payload)
    assert response.status_code == 201, response.text
    trip = response.json()["record"]

    assert (await _call(
        travel_env,
        "GET",
        "/api/life/travel/mode",
        params={"as_of": "2026-07-21T12:00:00+09:00"},
    )).status_code == 200
    assert (await _call(
        travel_env,
        "GET",
        "/api/life/travel/mode",
        params={"as_of": "2026-07-21T12:00:00"},
    )).status_code == 400
    assert (await _call(
        travel_env, "GET", f"/api/life/travel/records/{trip['id']}", user="bob"
    )).status_code == 404
    assert (await _call(
        travel_env,
        "POST",
        "/api/life/travel/records",
        json={**trip_payload, "unknown": True},
    )).status_code == 422

    generic_create = await _call(
        travel_env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "trip",
            "title": "Bypass",
            "properties": {"travel_schema_version": 1},
        },
    )
    assert generic_create.status_code == 400
    assert "typed Travel" in generic_create.text

    generic_patch = await _call(
        travel_env,
        "PATCH",
        f"/api/life/entities/{trip['id']}",
        json={"version": 1, "title": "Bypass update"},
    )
    assert generic_patch.status_code == 400
    generic_delete = await _call(
        travel_env,
        "DELETE",
        f"/api/life/entities/{trip['id']}",
        json={"version": 1, "reason": "Bypass delete"},
    )
    assert generic_delete.status_code == 400
