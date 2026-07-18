from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routes.calendar_routes as calendar_routes
from core.database import (
    Account,
    ActionAudit,
    Base,
    CalendarCal,
    CalendarDelivery,
    CalendarEvent,
    LifeEntity,
)


class _Upload:
    def __init__(self, content: bytes, filename: str = "events.ics"):
        self.content = content
        self.filename = filename

    async def read(self, size: int = -1) -> bytes:
        return self.content if size < 0 else self.content[:size]


def _request(username: str):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=username),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
        headers={},
    )


class _JsonRequest:
    def __init__(self, username: str, payload: dict):
        self.state = SimpleNamespace(current_user=username)
        self.app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
        self.headers = {}
        self._payload = payload

    async def json(self):
        return dict(self._payload)


def _endpoint(path: str, method: str):
    router = calendar_routes.setup_calendar_routes()
    for route in router.routes:
        if route.path == f"/api/calendar{path}" and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


@pytest.fixture()
def route_env(tmp_path, monkeypatch):
    from src import secret_storage

    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(secret_storage, "_digest_key", None)
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar-routes.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(calendar_routes, "SessionLocal", factory)
    yield SimpleNamespace(Session=factory, engine=engine)
    engine.dispose()


def _establish_account_and_calendar(username: str):
    listed = asyncio.run(_endpoint("/calendars", "GET")(_request(username)))
    return listed["calendars"][0]["href"]


def test_event_crud_uses_owner_versions_projection_and_durable_delivery(route_env):
    _establish_account_and_calendar("alice")
    with route_env.Session() as db:
        alice = db.query(Account).filter(Account.username == "alice").one()
        remote = CalendarCal(
            id=str(uuid.uuid4()),
            owner_id=alice.id,
            owner=alice.username,
            name="Remote",
            source="caldav",
            account_id=str(uuid.uuid4()),
            config_version=2,
        )
        db.add(remote)
        db.commit()
        remote_id = remote.id

    created = asyncio.run(_endpoint("/events", "POST")(
        _request("alice"),
        calendar_routes.EventCreate(
            summary="Private meeting",
            dtstart="2026-07-20T10:00:00Z",
            dtend="2026-07-20T11:00:00Z",
            calendar_href=remote_id,
            idempotency_key="web-create-1",
        ),
    ))
    assert created["created"] is True
    assert created["version"] == created["event"]["version"] == 1
    uid = created["uid"]

    # Changing only RRULE must round-trip the existing Z representation. A
    # UTC-naive ORM datetime passed directly would silently flip is_utc false.
    updated = asyncio.run(_endpoint("/events/{uid}", "PUT")(
        _request("alice"),
        uid,
        calendar_routes.EventUpdate(version=1, rrule="FREQ=DAILY"),
    ))
    assert updated["version"] == 2
    assert updated["event"]["dtstart"].endswith("Z")
    assert updated["event"]["is_utc"] is True

    with pytest.raises(HTTPException) as stale:
        asyncio.run(_endpoint("/events/{uid}", "PUT")(
            _request("alice"),
            uid,
            calendar_routes.EventUpdate(version=1, summary="Stale overwrite"),
        ))
    assert stale.value.status_code == 409

    with pytest.raises(HTTPException) as unsafe_scope:
        asyncio.run(_endpoint("/events/{uid}", "DELETE")(
            _request("alice"), uid, scope="occurrence", version=2
        ))
    assert unsafe_scope.value.status_code == 400

    occurrence_uid = f"{uid}::2026-07-21T10:00"
    excluded = asyncio.run(_endpoint("/events/{uid}", "DELETE")(
        _request("alice"), occurrence_uid, scope="occurrence", version=2
    ))
    assert excluded["scope"] == "occurrence"
    assert excluded["version"] == 3
    assert excluded["event"]["recurrence_exdates"] == ["2026-07-21T10:00"]

    cancelled = asyncio.run(_endpoint("/events/{uid}", "DELETE")(
        _request("alice"), uid, scope="series", version=3
    ))
    assert cancelled["version"] == 4
    assert cancelled["event"]["version"] == 4

    with route_env.Session() as db:
        alice = db.query(Account).filter(Account.username == "alice").one()
        event = db.query(CalendarEvent).filter_by(
            owner_id=alice.id, uid=uid
        ).one()
        projection = db.query(LifeEntity).filter_by(
            owner_id=alice.id,
            domain_ref_type="calendar_event",
            domain_ref_id=uid,
        ).one()
        delivery = db.query(CalendarDelivery).filter_by(
            owner_id=alice.id, event_uid=uid
        ).one()
        assert event.status == "cancelled"
        assert event.version == 4
        assert projection.status == "cancelled"
        # Create/update/delete coalesce before any worker claim; no network is
        # performed by the request and the never-created remote work is closed.
        assert delivery.operation == "create"
        assert delivery.state == "cancelled"
        assert delivery.expected_event_version == 4


def test_owner_id_reads_exports_and_nonempty_calendar_delete_fail_closed(route_env):
    alice_calendar = _establish_account_and_calendar("alice")
    created = asyncio.run(_endpoint("/events", "POST")(
        _request("alice"),
        calendar_routes.EventCreate(
            summary="Alice only",
            dtstart="2026-07-20T10:00:00",
            calendar_href=alice_calendar,
        ),
    ))
    _establish_account_and_calendar("bob")

    bob_events = asyncio.run(_endpoint("/events", "GET")(
        _request("bob"),
        start="2026-07-20T00:00:00",
        end="2026-07-21T00:00:00",
    ))
    assert bob_events == {"events": []}

    with pytest.raises(HTTPException) as hidden_export:
        asyncio.run(_endpoint("/export/{cal_id}", "GET")(
            _request("bob"), alice_calendar
        ))
    assert hidden_export.value.status_code == 404

    renamed = asyncio.run(_endpoint("/calendars/{cal_id}", "PUT")(
        _request("alice"), alice_calendar, version=1, name="Alice private"
    ))
    assert renamed == {"ok": True, "version": 2}
    with pytest.raises(HTTPException) as stale_calendar:
        asyncio.run(_endpoint("/calendars/{cal_id}", "PUT")(
            _request("alice"), alice_calendar, version=1, name="Stale"
        ))
    assert stale_calendar.value.status_code == 409

    with pytest.raises(HTTPException) as nonempty:
        asyncio.run(_endpoint("/calendars/{cal_id}", "DELETE")(
            _request("alice"), alice_calendar, version=2
        ))
    assert nonempty.value.status_code == 409

    with route_env.Session() as db:
        alice = db.query(Account).filter(Account.username == "alice").one()
        assert db.query(CalendarCal).filter_by(
            id=alice_calendar, owner_id=alice.id
        ).one()
        event = db.query(CalendarEvent).filter_by(
            uid=created["uid"], owner_id=alice.id
        ).one()
        projection = db.query(LifeEntity).filter_by(
            owner_id=alice.id, domain_ref_id=event.uid
        ).one()
        assert event.status == "confirmed"
        assert projection.status == "active"


def test_ics_import_is_owner_id_scoped_idempotent_and_projects(route_env):
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//route-test//EN\r\n"
        "BEGIN:VEVENT\r\nUID:shared-source@example.test\r\n"
        "SUMMARY:Imported event\r\nDTSTART:20260720T100000Z\r\n"
        "DTEND:20260720T110000Z\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()
    import_endpoint = _endpoint("/import", "POST")

    first = asyncio.run(import_endpoint(
        _request("alice"), file=_Upload(ics), calendar_name="Imported"
    ))
    again = asyncio.run(import_endpoint(
        _request("alice"), file=_Upload(ics), calendar_name="Imported"
    ))
    other_owner = asyncio.run(import_endpoint(
        _request("bob"), file=_Upload(ics), calendar_name="Imported"
    ))
    assert first["imported"] == 1
    assert again["imported"] == 0 and again["skipped"] == 1
    assert other_owner["imported"] == 1

    with route_env.Session() as db:
        alice = db.query(Account).filter(Account.username == "alice").one()
        bob = db.query(Account).filter(Account.username == "bob").one()
        assert db.query(CalendarCal).filter_by(
            id=first["calendar_id"], owner_id=alice.id
        ).one().owner == "alice"
        alice_events = db.query(CalendarEvent).filter_by(owner_id=alice.id).all()
        bob_events = db.query(CalendarEvent).filter_by(owner_id=bob.id).all()
        assert len(alice_events) == len(bob_events) == 1
        assert alice_events[0].uid != bob_events[0].uid
        assert alice_events[0].is_utc is True
        assert db.query(LifeEntity).filter_by(
            owner_id=alice.id, domain_ref_type="calendar_event"
        ).count() == 1


def test_reimport_repairs_legacy_zero_duration_only_through_service(route_env):
    _establish_account_and_calendar("alice")
    with route_env.Session() as db:
        alice = db.query(Account).filter(Account.username == "alice").one()
        calendar = CalendarCal(
            id=str(uuid.uuid4()),
            owner_id=alice.id,
            owner=alice.username,
            name="Legacy",
            source="import",
            config_version=1,
        )
        event = CalendarEvent(
            uid="legacy-zero",
            owner_id=alice.id,
            calendar_id=calendar.id,
            summary="Public Holiday",
            dtstart=datetime(2026, 8, 1),
            dtend=datetime(2026, 8, 1),
            all_day=True,
            version=1,
        )
        db.add_all((calendar, event))
        db.commit()

    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//route-test//EN\r\n"
        "BEGIN:VEVENT\r\nUID:holiday-1\r\nSUMMARY:Public Holiday\r\n"
        "DTSTART;VALUE=DATE:20260801\r\nDTEND;VALUE=DATE:20260801\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    ).encode()
    result = asyncio.run(_endpoint("/import", "POST")(
        _request("alice"), file=_Upload(ics), calendar_name="Legacy"
    ))
    assert result["imported"] == 0
    assert result["skipped"] == 1
    assert result["repaired"] == 1

    with route_env.Session() as db:
        alice = db.query(Account).filter(Account.username == "alice").one()
        event = db.query(CalendarEvent).filter_by(
            owner_id=alice.id, uid="legacy-zero"
        ).one()
        assert event.dtend == datetime(2026, 8, 2)
        assert event.version == 2
        assert db.query(LifeEntity).filter_by(
            owner_id=alice.id,
            domain_ref_type="calendar_event",
            domain_ref_id=event.uid,
        ).count() == 1


def test_caldav_credential_change_fences_queued_collection_before_save(
    route_env, monkeypatch
):
    from routes import prefs_routes

    _establish_account_and_calendar("alice")
    with route_env.Session() as db:
        alice = db.query(Account).filter(Account.username == "alice").one()
        remote = CalendarCal(
            id=str(uuid.uuid4()),
            owner_id=alice.id,
            owner=alice.username,
            name="Remote",
            source="caldav",
            account_id="connector-1",
            caldav_base_url="https://dav.example.test/alice/",
            config_version=3,
        )
        db.add(remote)
        db.commit()
        remote_id = remote.id

    state = {
        "caldav_accounts": [{
            "id": "connector-1",
            "label": "Remote",
            "url": "https://dav.example.test/",
            "username": "alice-old",
            "password": "enc:c1:old",
        }]
    }
    observed_versions: list[int] = []

    monkeypatch.setattr(
        prefs_routes,
        "_load_for_user",
        lambda _owner: json.loads(json.dumps(state)),
    )

    def save(_owner, prefs):
        with route_env.Session() as db:
            observed_versions.append(
                db.query(CalendarCal.config_version).filter_by(id=remote_id).scalar()
            )
        state.clear()
        state.update(json.loads(json.dumps(prefs)))

    monkeypatch.setattr(prefs_routes, "_save_for_user", save)
    endpoint = _endpoint("/config/accounts/{account_id}", "PUT")
    result = asyncio.run(endpoint(
        "connector-1",
        _JsonRequest("alice", {"username": "alice-new"}),
    ))
    assert result == {"ok": True}
    assert observed_versions == [4]

    with route_env.Session() as db:
        calendar = db.query(CalendarCal).filter_by(id=remote_id).one()
        assert calendar.config_version == 4
        audit = db.query(ActionAudit).filter_by(
            owner_id=calendar.owner_id,
            action="calendar.caldav.configuration_changed",
            entity_id=remote_id,
        ).one()
        assert audit.before_state == {"config_version": 3}
        assert audit.after_state == {"config_version": 4}
        assert "alice" not in json.dumps(audit.details)


def test_empty_calendar_delete_is_versioned_and_audited(route_env):
    created = asyncio.run(_endpoint("/calendars", "POST")(
        _request("alice"), name="Disposable", color="#123456"
    ))
    assert created["version"] == 1
    deleted = asyncio.run(_endpoint("/calendars/{cal_id}", "DELETE")(
        _request("alice"), created["id"], version=created["version"]
    ))
    assert deleted == {"ok": True}

    with route_env.Session() as db:
        alice = db.query(Account).filter(Account.username == "alice").one()
        assert db.query(CalendarCal).filter_by(
            id=created["id"], owner_id=alice.id
        ).count() == 0
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id,
            action="calendar.deleted",
            entity_id=created["id"],
        ).count() == 1


def test_update_requires_version_and_frontend_round_trips_it():
    with pytest.raises(ValidationError):
        calendar_routes.EventUpdate(summary="Missing CAS")

    source = Path("static/js/calendar.js").read_text(encoding="utf-8")
    assert "body: JSON.stringify({ ...data, version: expectedVersion })" in source
    assert "new URLSearchParams({ version: String(expectedVersion) })" in source
    assert "version: String(expectedVersion)" in source
    assert "?version=${expectedVersion}" in source
    assert "const result = await _calendarApiPayload" in source
    route_source = Path("routes/calendar_routes.py").read_text(encoding="utf-8")
    assert "_push_caldav_event_after_commit" not in route_source
    assert "_record_caldav_delete_tombstone" not in route_source
