from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import (
    Base,
    CalendarCal,
    CalendarDelivery,
    CalendarEvent,
    utcnow_naive,
)
from src.identity import ensure_account


@pytest.fixture()
def calendar_delivery_env(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(secret_storage, "_digest_key", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'calendar-delivery.db'}",
        connect_args={"check_same_thread": False, "timeout": 0.1},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        account = ensure_account(db, "calendar-delivery-owner")
        calendar = CalendarCal(
            id="calendar-delivery-calendar",
            owner_id=account.id,
            owner=account.username,
            name="Private calendar",
            source="caldav",
            account_id="calendar-account",
            caldav_base_url="https://dav.example.test/calendars/private",
            config_version=1,
        )
        event = CalendarEvent(
            uid="private-event-uid",
            owner_id=account.id,
            calendar_id=calendar.id,
            summary="Private meeting",
            description="Private description",
            dtstart=datetime(2026, 7, 20, 9, 0),
            dtend=datetime(2026, 7, 20, 10, 0),
            version=1,
        )
        db.add(calendar)
        db.flush()
        db.add(event)
        db.commit()
        owner_id = account.id
    finally:
        db.close()
    try:
        yield SimpleNamespace(
            engine=engine,
            Session=factory,
            owner_id=owner_id,
            calendar_id=calendar.id,
            event_uid=event.uid,
        )
    finally:
        engine.dispose()


def _event_payload(*, version=1, summary="Private meeting"):
    return {
        "schema_version": 1,
        "event": {
            "snapshot_type": "restia.calendar_event",
            "snapshot_version": 1,
            "uid": "private-event-uid",
            "calendar_id": "calendar-delivery-calendar",
            "event_version": version,
            "summary": summary,
            "description": "Private description",
            "location": "",
            "dtstart": "2026-07-20T09:00:00",
            "dtend": "2026-07-20T10:00:00",
            "all_day": False,
            "is_utc": False,
            "rrule": "",
            "recurrence_exdates": [],
            "color": None,
            "status": "confirmed",
            "importance": "normal",
            "event_type": None,
            "origin": "local",
            "remote_href": None,
            "remote_etag": None,
        },
    }


def test_delivery_snapshot_decodes_canonical_recurrence_json():
    from src.calendar_delivery import _snapshot_ical

    payload = _event_payload()
    payload["event"]["rrule"] = "FREQ=DAILY;COUNT=2"
    payload["event"]["recurrence_exdates"] = '["2026-07-21T09:00"]'

    raw = _snapshot_ical(payload, uid="private-event-uid")

    assert "RRULE:FREQ=DAILY;COUNT=2" in raw
    assert "EXDATE:20260721T090000" in raw


def _queue(
    env,
    *,
    delivery_id="calendar-delivery-1",
    operation="create",
    expected_version=1,
    summary="Private meeting",
    state="pending",
    attempts=0,
    created_at=None,
):
    db = env.Session()
    try:
        row = CalendarDelivery(
            id=delivery_id,
            owner_id=env.owner_id,
            calendar_id=env.calendar_id,
            event_uid=env.event_uid,
            operation=operation,
            idempotency_key=f"opaque-{delivery_id}",
            payload=_event_payload(version=expected_version, summary=summary),
            expected_event_version=expected_version,
            expected_config_version=1,
            state=state,
            attempts=attempts,
            version=1,
        )
        if created_at is not None:
            row.created_at = created_at
        db.add(row)
        db.commit()
    finally:
        db.close()


def _stub_config(monkeypatch, delivery):
    monkeypatch.setattr(
        delivery,
        "_resolve_account_config",
        lambda _snapshot: {
            "url": "https://dav.example.test",
            "collection_url": "https://dav.example.test/calendars/private",
            "username": "private-user",
            "password": "private-password",
            "access_token": "",
            "config_marker": "stable-marker",
        },
    )
    monkeypatch.setattr(
        delivery, "_current_account_marker", lambda _snapshot: "stable-marker"
    )


def test_payload_is_encrypted_and_network_phase_holds_no_db_transaction(
    calendar_delivery_env, monkeypatch
):
    import src.calendar_delivery as delivery

    _queue(calendar_delivery_env)
    with calendar_delivery_env.engine.connect() as connection:
        raw = connection.execute(text(
            "SELECT payload FROM calendar_deliveries WHERE id='calendar-delivery-1'"
        )).scalar_one()
    assert str(raw).startswith('"enc:c1:')
    assert "Private meeting" not in str(raw)
    assert "private-event-uid" not in str(raw)

    _stub_config(monkeypatch, delivery)

    def put_event(_config, **_kwargs):
        other = calendar_delivery_env.Session()
        try:
            other.query(CalendarCal).filter_by(
                id=calendar_delivery_env.calendar_id
            ).update({CalendarCal.name: "Concurrent calendar label"})
            other.commit()
        finally:
            other.close()
        return (
            "https://dav.example.test/calendars/private/private-event-uid.ics",
            '"etag-1"',
        )

    monkeypatch.setattr(delivery.caldav, "put_calendar_event", put_event)
    assert delivery.drain_calendar_deliveries(
        calendar_delivery_env.Session,
        owner_id=calendar_delivery_env.owner_id,
        limit=1,
    ) == {"completed": 1, "retried": 0, "conflicts": 0}

    db = calendar_delivery_env.Session()
    try:
        row = db.query(CalendarDelivery).filter_by(
            id="calendar-delivery-1"
        ).one()
        event = db.query(CalendarEvent).filter_by(
            uid=calendar_delivery_env.event_uid
        ).one()
        calendar = db.query(CalendarCal).filter_by(
            id=calendar_delivery_env.calendar_id
        ).one()
        assert row.state == "completed"
        assert row.payload == {}
        assert row.claim_token is None and row.lease_expires_at is None
        assert event.remote_etag == '"etag-1"'
        assert calendar.name == "Concurrent calendar label"
    finally:
        db.close()


def test_stale_lease_is_reclaimed_before_later_fifo_work(calendar_delivery_env):
    import src.calendar_delivery as delivery

    base = datetime(2026, 1, 1)
    _queue(
        calendar_delivery_env,
        delivery_id="first",
        state="processing",
        attempts=1,
        created_at=base,
    )
    _queue(
        calendar_delivery_env,
        delivery_id="second",
        created_at=base + timedelta(seconds=1),
    )
    db = calendar_delivery_env.Session()
    try:
        first = db.query(CalendarDelivery).filter_by(id="first").one()
        first.claim_token = "abandoned"
        first.claimed_at = utcnow_naive() - timedelta(
            seconds=delivery.CLAIM_LEASE_SECONDS + 1
        )
        first.lease_expires_at = utcnow_naive() - timedelta(seconds=1)
        db.commit()
        claimed = delivery._claim_one(
            db, owner_id=calendar_delivery_env.owner_id
        )
        assert claimed.id == "first"
        assert claimed.claim_token != "abandoned"
        assert claimed.attempts == 2
    finally:
        db.rollback()
        db.close()


def test_fifo_chained_create_then_edit_uses_new_etag(
    calendar_delivery_env, monkeypatch
):
    import src.calendar_delivery as delivery

    base = datetime(2026, 1, 1)
    _queue(
        calendar_delivery_env,
        delivery_id="create-v1",
        expected_version=1,
        summary="Version one",
        created_at=base,
    )
    _queue(
        calendar_delivery_env,
        delivery_id="edit-v2",
        expected_version=2,
        summary="Version two",
        created_at=base + timedelta(seconds=1),
    )
    db = calendar_delivery_env.Session()
    try:
        event = db.query(CalendarEvent).filter_by(
            uid=calendar_delivery_env.event_uid
        ).one()
        event.summary = "Version two"
        event.version = 2
        db.commit()
    finally:
        db.close()
    _stub_config(monkeypatch, delivery)
    calls = []

    def put_event(_config, **kwargs):
        calls.append(dict(kwargs))
        return (
            "https://dav.example.test/calendars/private/private-event-uid.ics",
            f'"etag-{len(calls)}"',
        )

    monkeypatch.setattr(delivery.caldav, "put_calendar_event", put_event)
    result = delivery.drain_calendar_deliveries(
        calendar_delivery_env.Session,
        owner_id=calendar_delivery_env.owner_id,
        limit=2,
    )
    assert result == {"completed": 2, "retried": 0, "conflicts": 0}
    assert [call["operation"] for call in calls] == ["create", "update"]
    assert calls[1]["etag"] == '"etag-1"'
    assert "SUMMARY:Version one" in calls[0]["raw_ical"]
    assert "SUMMARY:Version two" in calls[1]["raw_ical"]


def test_config_rebind_after_remote_success_becomes_visible_conflict(
    calendar_delivery_env, monkeypatch
):
    import src.calendar_delivery as delivery

    _queue(calendar_delivery_env)
    _stub_config(monkeypatch, delivery)
    monkeypatch.setattr(
        delivery.caldav,
        "put_calendar_event",
        lambda _config, **_kwargs: (
            "https://dav.example.test/calendars/private/private-event-uid.ics",
            '"etag-rebound"',
        ),
    )
    monkeypatch.setattr(
        delivery, "_current_account_marker", lambda _snapshot: "new-marker"
    )
    result = delivery.drain_calendar_deliveries(
        calendar_delivery_env.Session,
        owner_id=calendar_delivery_env.owner_id,
        limit=1,
    )
    assert result == {"completed": 0, "retried": 0, "conflicts": 1}
    db = calendar_delivery_env.Session()
    try:
        row = db.query(CalendarDelivery).filter_by(
            id="calendar-delivery-1"
        ).one()
        event = db.query(CalendarEvent).filter_by(
            uid=calendar_delivery_env.event_uid
        ).one()
        assert row.state == "conflict"
        assert row.last_error_code == "config_changed"
        assert event.remote_etag is None
    finally:
        db.close()


def test_auth_failure_retries_then_stops_at_bound(calendar_delivery_env, monkeypatch):
    import src.calendar_delivery as delivery

    _queue(
        calendar_delivery_env,
        attempts=delivery.MAX_DELIVERY_ATTEMPTS - 1,
    )
    _stub_config(monkeypatch, delivery)
    monkeypatch.setattr(
        delivery.caldav,
        "put_calendar_event",
        lambda _config, **_kwargs: (_ for _ in ()).throw(
            delivery.caldav.CalDAVAuthError("private server message")
        ),
    )
    result = delivery.drain_calendar_deliveries(
        calendar_delivery_env.Session,
        owner_id=calendar_delivery_env.owner_id,
        limit=1,
    )
    assert result["conflicts"] == 1
    db = calendar_delivery_env.Session()
    try:
        row = db.query(CalendarDelivery).filter_by(
            id="calendar-delivery-1"
        ).one()
        assert row.state == "conflict"
        assert row.last_error_code == "attempts_exhausted"
        assert "private server message" not in str(row.last_error_code)
    finally:
        db.close()


def test_protocol_create_update_delete_preconditions(monkeypatch):
    import src.caldav_writeback as caldav

    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(
            204 if method == "DELETE" else 201,
            headers={"etag": '"new"'},
        )

    monkeypatch.setattr(caldav, "_secure_request", request)
    config = {"username": "u", "password": "p"}
    collection = "https://dav.example.test/cal/private"
    created = caldav.put_calendar_event(
        config,
        collection_url=collection,
        uid="uid/with space",
        raw_ical=caldav.build_event_ical({
            "uid": "uid/with space",
            "summary": "One",
            "dtstart": datetime(2026, 1, 1, 9),
            "dtend": datetime(2026, 1, 1, 10),
        }),
        operation="create",
    )
    assert created[0].endswith("/uid%2Fwith%20space.ics")
    assert calls[-1][2]["headers"]["If-None-Match"] == "*"

    caldav.put_calendar_event(
        config,
        collection_url=collection,
        uid="uid",
        raw_ical=caldav.build_event_ical({
            "uid": "uid",
            "summary": "Two",
            "dtstart": datetime(2026, 1, 1, 9),
            "dtend": datetime(2026, 1, 1, 10),
        }),
        operation="update",
        href=collection + "/uid.ics",
        etag='"old"',
    )
    assert calls[-1][2]["headers"]["If-Match"] == '"old"'

    caldav.delete_calendar_event(
        config,
        collection_url=collection,
        href=collection + "/uid.ics",
        etag='"delete-old"',
    )
    assert calls[-1][0] == "DELETE"
    assert calls[-1][2]["headers"]["If-Match"] == '"delete-old"'


def test_protocol_412_crash_replay_uses_exact_get_and_semantic_ical(monkeypatch):
    import src.caldav_writeback as caldav

    desired = caldav.build_event_ical({
        "uid": "crash-uid",
        "summary": "Crash safe",
        "dtstart": datetime(2026, 1, 1, 9),
        "dtend": datetime(2026, 1, 1, 10),
    })
    server = desired.replace(
        "BEGIN:VEVENT\r\n",
        "BEGIN:VEVENT\r\nDTSTAMP:20260101T000000Z\r\n",
    )
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "PUT":
            return httpx.Response(412)
        return httpx.Response(200, headers={"etag": '"remote"'}, text=server)

    monkeypatch.setattr(caldav, "_secure_request", request)
    href, etag = caldav.put_calendar_event(
        {"username": "u", "password": "p"},
        collection_url="https://dav.example.test/cal/private",
        uid="crash-uid",
        raw_ical=desired,
        operation="create",
    )
    assert [item[0] for item in calls] == ["PUT", "GET"]
    assert calls[0][1] == calls[1][1] == href
    assert etag == '"remote"'


def test_protocol_distinguishes_404_auth_and_delete_absence(monkeypatch):
    import src.caldav_writeback as caldav

    collection = "https://dav.example.test/cal/private"
    config = {"username": "u", "password": "p"}
    monkeypatch.setattr(
        caldav, "_secure_request", lambda *_args, **_kwargs: httpx.Response(404)
    )
    with pytest.raises(caldav.CalDAVNotFound):
        caldav.get_calendar_resource(
            config, collection_url=collection, href=collection + "/uid.ics"
        )
    caldav.delete_calendar_event(
        config,
        collection_url=collection,
        href=collection + "/uid.ics",
        etag='"old"',
    )

    monkeypatch.setattr(
        caldav, "_secure_request", lambda *_args, **_kwargs: httpx.Response(401)
    )
    with pytest.raises(caldav.CalDAVAuthError):
        caldav.get_calendar_resource(
            config, collection_url=collection, href=collection + "/uid.ics"
        )


def test_caldav_pinned_transport_rejects_oversized_responses():
    import src.caldav_writeback as caldav

    class CoreResponse:
        status = 200
        headers = []
        extensions = {}

        def __init__(self):
            self.closed = False

        def iter_stream(self):
            yield b"x" * caldav.MAX_CALDAV_RESPONSE_BYTES
            yield b"y"

        def close(self):
            self.closed = True

    response = CoreResponse()

    class Pool:
        def handle_request(self, _request):
            return response

    transport = object.__new__(caldav._PinnedSyncTransport)
    transport._pool = Pool()
    with pytest.raises(caldav.CalDAVTransportError, match="size limit"):
        transport.handle_request(
            httpx.Request("GET", "https://dav.example.test/calendar")
        )
    assert response.closed is True


def test_secure_request_disables_proxies_redirects_and_pins_dns(monkeypatch):
    import src.caldav_sync as sync
    import src.caldav_writeback as caldav

    captured = {}

    monkeypatch.setattr(sync, "validate_caldav_url", lambda value: str(value))
    monkeypatch.setattr(
        sync, "_validate_caldav_address", lambda value: captured.setdefault(
            "validated_ip", str(value)
        )
    )
    monkeypatch.setattr(
        caldav, "_resolved_ips", lambda _host: [caldav.ipaddress.ip_address("93.184.216.34")]
    )
    monkeypatch.setattr(
        caldav, "_PinnedSyncTransport", lambda ip: ("pinned", str(ip))
    )

    class Client:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def request(self, method, url, **kwargs):
            captured["request"] = (method, url, kwargs)
            return httpx.Response(204)

    monkeypatch.setattr(caldav.httpx, "Client", Client)
    response = caldav._secure_request(
        "DELETE", "https://dav.example.test/calendar/item.ics"
    )
    assert response.status_code == 204
    assert captured["validated_ip"] == "93.184.216.34"
    assert captured["transport"] == ("pinned", "93.184.216.34")
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False


@pytest.mark.asyncio
async def test_calendar_worker_gate_is_independent_from_tasks(
    calendar_delivery_env, monkeypatch
):
    import src.calendar_delivery as delivery

    monkeypatch.setenv("RESTIA_INPROCESS_TASKS", "0")
    monkeypatch.setenv("RESTIA_INPROCESS_POLLERS", "0")
    monkeypatch.setenv("RESTIA_INPROCESS_TELEGRAM", "0")
    monkeypatch.setenv("RESTIA_INPROCESS_CONTACT_DELIVERY", "0")
    monkeypatch.delenv("RESTIA_INPROCESS_CALENDAR_DELIVERY", raising=False)
    assert delivery.inprocess_calendar_delivery_enabled() is True
    monkeypatch.setenv("RESTIA_INPROCESS_CALENDAR_DELIVERY", "0")
    assert delivery.inprocess_calendar_delivery_enabled() is False


def test_lifespan_owns_calendar_worker_and_all_compose_variants_expose_gate():
    root = Path(__file__).resolve().parents[1]
    app_source = (root / "app.py").read_text(encoding="utf-8")
    assert "if inprocess_calendar_delivery_enabled():" in app_source
    assert "calendar_delivery_loop()," in app_source
    assert 'name="restia-calendar-delivery"' in app_source
    assert "_startup_tasks.append(asyncio.create_task(" in app_source

    gate = (
        "RESTIA_INPROCESS_CALENDAR_DELIVERY="
        "${RESTIA_INPROCESS_CALENDAR_DELIVERY:-1}"
    )
    interval = (
        "RESTIA_CALENDAR_DELIVERY_INTERVAL_SECONDS="
        "${RESTIA_CALENDAR_DELIVERY_INTERVAL_SECONDS:-}"
    )
    batch = (
        "RESTIA_CALENDAR_DELIVERY_BATCH_SIZE="
        "${RESTIA_CALENDAR_DELIVERY_BATCH_SIZE:-}"
    )
    for name in (
        "docker-compose.yml",
        "docker-compose.gpu-nvidia.yml",
        "docker-compose.gpu-amd.yml",
    ):
        source = (root / name).read_text(encoding="utf-8")
        assert gate in source
        assert interval in source
        assert batch in source

    env_source = (root / ".env.example").read_text(encoding="utf-8")
    assert "# RESTIA_INPROCESS_CALENDAR_DELIVERY=1" in env_source
    assert "# RESTIA_CALENDAR_DELIVERY_INTERVAL_SECONDS=2" in env_source
    assert "# RESTIA_CALENDAR_DELIVERY_BATCH_SIZE=5" in env_source
