"""Focused contracts for V3 Home and Personal Administration records."""

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
    Document,
    LifeEntity,
    LifeEntityVersion,
)
from routes.home_routes import setup_home_routes
from routes.life_routes import setup_life_routes
from src.home_service import (
    HOME_RECORD_TYPES,
    create_home_record,
    delete_home_record,
    get_home_record,
    home_alert_report,
    home_record_history,
    list_home_records,
    update_home_record,
)
from src.identity import ensure_account
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    create_life_entity,
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
def home_env(tmp_path):
    db_path = tmp_path / "home.db"
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
    app.include_router(setup_home_routes(session_factory=factory))
    yield SimpleNamespace(app=app, Session=factory, engine=engine)
    engine.dispose()


def _account(db, username: str):
    account = ensure_account(db, username)
    db.flush()
    return account


def _details(record_type: str) -> dict:
    details = {
        "identity_document": {
            "document_kind": "national_id", "issuer": "Authority",
            "identifier_last4": "A123",
        },
        "insurance": {
            "policy_kind": "health", "provider_name": "Insurer",
            "coverage_summary": "Private policy summary",
        },
        "warranty": {
            "item_name": "Laptop", "provider_name": "Manufacturer",
        },
        "renewal": {
            "renewal_kind": "membership", "provider_name": "Association",
            "cadence": "annual",
        },
        "inventory_item": {
            "category": "electronics", "location": "Desk", "quantity": 1,
        },
        "repair": {
            "item_name": "Air conditioner", "repair_kind": "maintenance",
        },
        "purchase": {"merchant": "Local shop", "category": "household"},
        "delivery": {"carrier": "Parcel carrier"},
        "vehicle": {
            "vehicle_kind": "car", "make": "Example", "model": "City",
            "year": 2024, "vin_last6": "12AB34",
        },
        "travel_document": {
            "document_kind": "passport", "issuer": "Government",
            "country": "India", "identifier_last4": "P123",
        },
        "form": {"form_kind": "application", "organization": "University"},
        "provider": {
            "provider_kind": "plumber", "provider_name": "Home Services",
        },
        "household_routine": {
            "routine_kind": "filter_change", "cadence": "quarterly",
            "instructions": "Inspect the filter and record its condition.",
        },
        "emergency_information": {
            "information_kind": "evacuation", "contact_label": "Family",
            "instructions": "Use the east exit and meet at the marked point.",
        },
    }
    return details[record_type]


def _payload(record_type: str, **overrides) -> dict:
    payload = {
        "record_type": record_type,
        "title": record_type.replace("_", " ").title(),
        "effective_at": datetime(2026, 7, 1, 8),
        "source": {"kind": "manual", "label": "User entry"},
        "details": _details(record_type),
        "references": {},
        "note": "Private home administration record",
        "provenance": {"capture": "manual"},
    }
    if record_type in {"insurance", "warranty", "travel_document"}:
        payload["expires_at"] = datetime(2026, 12, 31, 23, 59)
    if record_type in {"renewal", "delivery", "form", "household_routine"}:
        payload["due_at"] = datetime(2026, 8, 1, 9)
    payload.update(overrides)
    return payload


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.request(method, path, headers=headers, **kwargs)


@pytest.mark.parametrize("record_type", sorted(HOME_RECORD_TYPES))
def test_every_home_type_uses_encrypted_account_scoped_life_entity(
    home_env, record_type
):
    db = home_env.Session()
    try:
        alice = _account(db, "alice")
        entity, created = create_home_record(
            db, account=alice, **_payload(record_type)
        )
        db.commit()

        assert created is True
        assert entity.entity_type == "home_record"
        assert entity.owner_id == alice.id
        assert entity.properties["home_schema_version"] == 1
        assert entity.properties["record_type"] == record_type
        assert entity.sensitivity == "private"
        assert entity.properties["source"]["label"] == "User entry"
        assert type(LifeEntity.__table__.c.properties.type).__name__ == "EncryptedJSON"
    finally:
        db.close()


def test_home_rejects_secrets_payment_numbers_and_executor_payloads(home_env):
    db = home_env.Session()
    try:
        alice = _account(db, "alice")
        unsafe = [
            (
                {"provenance": {"password": "never-store-this"}},
                "credentials, secrets",
            ),
            (
                {"provenance": {"action_payload": {"submit_form": True}}},
                "autonomous executor",
            ),
            (
                {"note": "Use card 4111 1111 1111 1111"},
                "payment-card number",
            ),
            (
                {"source": {
                    "kind": "manual", "label": "Unsafe",
                    "reference": "https://user:password@example.test/private",
                }},
                "embedded credentials",
            ),
        ]
        for changes, message in unsafe:
            with pytest.raises(LifeGraphError, match=message):
                create_home_record(
                    db, account=alice, **_payload("identity_document", **changes)
                )

        with pytest.raises(LifeGraphError, match="Unsupported identity_document"):
            create_home_record(
                db,
                account=alice,
                **_payload(
                    "identity_document",
                    details={
                        **_details("identity_document"),
                        "full_document_number": "AA12345678",
                    },
                ),
            )
    finally:
        db.close()


def test_home_validates_source_file_document_and_entity_ownership(home_env):
    db = home_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        alice_source, _ = create_life_source(
            db, account=alice, source_type="document", title="Alice source"
        )
        bob_source, _ = create_life_source(
            db, account=bob, source_type="document", title="Bob source"
        )
        alice_file, _ = create_life_entity(
            db, account=alice, entity_type="file", title="Alice file"
        )
        bob_file, _ = create_life_entity(
            db, account=bob, entity_type="file", title="Bob file"
        )
        alice_asset, _ = create_life_entity(
            db, account=alice, entity_type="asset", title="Alice asset"
        )
        alice_document = Document(
            id="alice-doc", owner="alice", title="Alice document",
            current_content="private", language="text",
        )
        bob_document = Document(
            id="bob-doc", owner="bob", title="Bob document",
            current_content="private", language="text",
        )
        db.add_all([alice_document, bob_document])
        db.flush()

        valid, _ = create_home_record(
            db,
            account=alice,
            **_payload(
                "inventory_item",
                source={
                    "kind": "document", "label": "Inventory sheet",
                    "source_id": alice_source.id,
                },
                references={
                    "file_entity_ids": [alice_file.id],
                    "document_ids": [alice_document.id],
                    "entity_ids": [alice_asset.id],
                },
            ),
        )
        assert valid.properties["references"]["entity_ids"] == [alice_asset.id]

        bad_cases = [
            ({"source": {
                "kind": "document", "label": "Wrong source",
                "source_id": bob_source.id,
            }}, "Home source not found"),
            ({"references": {"file_entity_ids": [bob_file.id]}},
             "Referenced Life entity not found"),
            ({"references": {"file_entity_ids": [alice_asset.id]}},
             "Referenced file entity not found"),
            ({"references": {"document_ids": [bob_document.id]}},
             "Referenced document not found"),
        ]
        for changes, message in bad_cases:
            with pytest.raises(LifeGraphNotFound, match=message):
                create_home_record(
                    db, account=alice,
                    **_payload("inventory_item", **changes),
                )
    finally:
        db.close()


def test_home_owner_isolation_cas_history_and_delete(home_env):
    db = home_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        entity, _ = create_home_record(
            db, account=alice, **_payload("repair")
        )
        db.commit()

        assert get_home_record(
            db, owner_id=alice.id, entity_id=entity.id
        )["record_type"] == "repair"
        with pytest.raises(LifeGraphNotFound):
            get_home_record(db, owner_id=bob.id, entity_id=entity.id)
        bob_rows, _ = list_home_records(db, owner_id=bob.id)
        assert bob_rows == []

        updated = update_home_record(
            db,
            account=alice,
            entity_id=entity.id,
            expected_version=1,
            changes={"note": "Technician visit recorded"},
        )
        db.commit()
        assert updated.version == 2
        with pytest.raises(LifeGraphConflict):
            update_home_record(
                db,
                account=alice,
                entity_id=entity.id,
                expected_version=1,
                changes={"note": "stale"},
            )

        history, truncated = home_record_history(
            db, owner_id=alice.id, entity_id=entity.id
        )
        assert truncated is False
        assert [item["version"] for item in history] == [2, 1]
        assert history[0]["changed_fields"] == ["summary"]

        deleted = delete_home_record(
            db,
            owner_id=alice.id,
            entity_id=entity.id,
            expected_version=2,
            reason="Duplicate repair record",
        )
        db.commit()
        assert deleted.status == "deleted"
        assert deleted.version == 3
        with pytest.raises(LifeGraphNotFound):
            get_home_record(db, owner_id=alice.id, entity_id=entity.id)
        assert db.query(LifeEntityVersion).filter_by(entity_id=entity.id).count() == 3
        assert db.query(ActionAudit).filter_by(entity_id=entity.id).count() == 3
    finally:
        db.close()


def test_home_alerts_use_explicit_as_of_inclusive_boundaries_and_sources(home_env):
    db = home_env.Session()
    try:
        alice = _account(db, "alice")
        as_of = datetime(2026, 7, 10, 9)
        create_home_record(
            db,
            account=alice,
            **_payload("renewal", due_at=as_of),
        )
        create_home_record(
            db,
            account=alice,
            **_payload("insurance", expires_at=datetime(2026, 7, 20, 9)),
        )
        create_home_record(
            db,
            account=alice,
            **_payload(
                "form",
                title="Overdue form",
                effective_at=datetime(2026, 7, 1),
                due_at=datetime(2026, 7, 9, 9),
            ),
        )
        create_home_record(
            db,
            account=alice,
            **_payload(
                "delivery",
                due_at=datetime(2026, 7, 15),
                details={**_details("delivery"), "record_status": "delivered"},
            ),
        )
        create_home_record(
            db,
            account=alice,
            **_payload("warranty", expires_at=datetime(2026, 7, 21, 9)),
        )
        db.commit()

        report = home_alert_report(
            db, owner_id=alice.id, as_of=as_of, horizon_days=10
        )
        assert report["as_of"] == "2026-07-10T09:00:00Z"
        assert report["window_end"] == "2026-07-20T09:00:00Z"
        assert [item["deadline_at"] for item in report["items"]] == [
            "2026-07-09T09:00:00Z",
            "2026-07-10T09:00:00Z",
            "2026-07-20T09:00:00Z",
        ]
        assert report["items"][0]["overdue"] is True
        assert report["items"][1]["overdue"] is False
        assert all(item["source"]["label"] == "User entry" for item in report["items"])

        future_only = home_alert_report(
            db,
            owner_id=alice.id,
            as_of=as_of,
            horizon_days=10,
            include_overdue=False,
        )
        assert all(not item["overdue"] for item in future_only["items"])
        assert len(future_only["items"]) == 2
        with pytest.raises(LifeGraphError, match="horizon_days"):
            home_alert_report(
                db, owner_id=alice.id, as_of=as_of, horizon_days=366
            )
        with pytest.raises(LifeGraphError, match="as_of is required"):
            home_alert_report(db, owner_id=alice.id, as_of=None)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_home_api_is_strict_owner_scoped_and_blocks_generic_bypass(home_env):
    payload = _payload("renewal")
    payload["effective_at"] = payload["effective_at"].isoformat()
    payload["due_at"] = payload["due_at"].isoformat()
    created = await _call(
        home_env, "POST", "/api/life/home/records", json=payload
    )
    assert created.status_code == 201, created.text
    record = created.json()["record"]
    assert record["record_type"] == "renewal"
    assert record["execution_policy"]["can_execute_external_action"] is False

    alice = await _call(home_env, "GET", "/api/life/home/records")
    bob = await _call(
        home_env, "GET", "/api/life/home/records", user="bob"
    )
    assert alice.status_code == 200
    assert alice.json()["count"] == 1
    assert bob.status_code == 200
    assert bob.json()["items"] == []

    forbidden = await _call(
        home_env,
        "POST",
        "/api/life/home/records",
        json={**payload, "execute": {"renew_now": True}},
    )
    assert forbidden.status_code == 422

    missing_as_of = await _call(
        home_env, "GET", "/api/life/home/alerts"
    )
    assert missing_as_of.status_code == 422
    alerts = await _call(
        home_env,
        "GET",
        "/api/life/home/alerts?as_of=2026-07-01T00:00:00Z&horizon_days=32",
    )
    assert alerts.status_code == 200, alerts.text
    assert alerts.json()["count"] == 1

    hidden = await _call(
        home_env,
        "GET",
        f"/api/life/home/records/{record['id']}",
        user="bob",
    )
    assert hidden.status_code == 404

    generic = await _call(
        home_env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "home_record",
            "title": "Bypass",
            "properties": {"home_schema_version": 1},
        },
    )
    assert generic.status_code == 400
    assert "typed Home" in generic.text
