from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    ActionAudit,
    Base,
    CalendarCal,
    CalendarDelivery,
    CalendarEvent,
    LifeEntity,
)
from src.calendar_service import (
    CalendarConflict,
    CalendarRemoteWritePending,
    adopt_legacy_caldav_delivery,
    cancel_missing_remote_calendar_event,
    create_calendar_event,
    ingest_remote_calendar_event,
    upsert_caldav_calendar,
)


@pytest.fixture()
def pull_env(tmp_path, monkeypatch):
    from src import secret_storage

    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(secret_storage, "_digest_key", None)
    engine = create_engine(f"sqlite:///{tmp_path / 'caldav-pull.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        alice = Account(id=str(uuid.uuid4()), username="pull-alice")
        bob = Account(id=str(uuid.uuid4()), username="pull-bob")
        db.add_all((alice, bob))
        db.flush()
        alice_cal = CalendarCal(
            id="alice-remote",
            owner_id=alice.id,
            owner=alice.username,
            name="Alice remote",
            source="caldav",
            account_id="alice-connector",
            caldav_base_url="https://dav.example.test/alice",
            config_version=1,
        )
        bob_cal = CalendarCal(
            id="bob-remote",
            owner_id=bob.id,
            owner=bob.username,
            name="Bob remote",
            source="caldav",
            account_id="bob-connector",
            caldav_base_url="https://dav.example.test/bob",
            config_version=1,
        )
        db.add_all((alice_cal, bob_cal))
        db.commit()
        ids = SimpleNamespace(
            alice=alice.id,
            bob=bob.id,
            alice_cal=alice_cal.id,
            bob_cal=bob_cal.id,
        )
    yield SimpleNamespace(Session=factory, engine=engine, **vars(ids))
    engine.dispose()


def _account(db, owner_id):
    return db.query(Account).filter(Account.id == owner_id).one()


def _ingest(db, *, account, calendar_id, expected_version=None, **overrides):
    values = {
        "uid": "shared-remote-uid",
        "summary": "Remote meeting",
        "description": "Remote private description",
        "location": "Remote room",
        "dtstart": "2026-07-20T09:00:00Z",
        "dtend": "2026-07-20T10:00:00Z",
        "all_day": False,
        "is_utc": True,
        "rrule": "",
        "recurrence_exdates": [],
        "remote_href": f"https://dav.example.test/{account.id}/event.ics",
        "remote_etag": '"etag-1"',
    }
    values.update(overrides)
    return ingest_remote_calendar_event(
        db,
        account=account,
        calendar_id=calendar_id,
        expected_version=expected_version,
        **values,
    )


def test_same_remote_uid_is_independent_across_accounts(pull_env):
    with pull_env.Session() as db:
        alice = _account(db, pull_env.alice)
        bob = _account(db, pull_env.bob)

        alice_result = _ingest(
            db, account=alice, calendar_id=pull_env.alice_cal,
        )
        bob_result = _ingest(
            db, account=bob, calendar_id=pull_env.bob_cal,
        )

        rows = db.query(CalendarEvent).filter(
            CalendarEvent.uid == "shared-remote-uid"
        ).all()
        assert len(rows) == 2
        assert {row.owner_id for row in rows} == {alice.id, bob.id}
        assert alice_result.event.owner_id == alice.id
        assert bob_result.event.owner_id == bob.id
        assert db.query(LifeEntity).filter(
            LifeEntity.domain_ref_type == "calendar_event"
        ).count() == 2
        assert db.query(CalendarDelivery).count() == 0


def test_remote_refresh_cancel_and_restore_are_cas_projected_without_echo(pull_env):
    with pull_env.Session() as db:
        alice = _account(db, pull_env.alice)
        created = _ingest(
            db, account=alice, calendar_id=pull_env.alice_cal,
        )
        refreshed = _ingest(
            db,
            account=alice,
            calendar_id=pull_env.alice_cal,
            expected_version=1,
            summary="Remote meeting moved",
            remote_etag='"etag-2"',
        )
        assert created.created is True
        assert refreshed.event_version == 2
        assert refreshed.event.summary == "Remote meeting moved"
        assert refreshed.graph_entity.properties["event_version"] == 2
        assert db.query(CalendarDelivery).count() == 0

        with pytest.raises(CalendarConflict, match="changed"):
            _ingest(
                db,
                account=alice,
                calendar_id=pull_env.alice_cal,
                expected_version=1,
                summary="Stale remote overwrite",
            )

        cancelled = cancel_missing_remote_calendar_event(
            db,
            account=alice,
            calendar_id=pull_env.alice_cal,
            uid=created.event.uid,
            expected_version=2,
        )
        assert cancelled.event_version == 3
        assert cancelled.event.status == "cancelled"
        assert cancelled.event.remote_href is None
        assert cancelled.graph_entity.status == "cancelled"

        restored = _ingest(
            db,
            account=alice,
            calendar_id=pull_env.alice_cal,
            expected_version=3,
            summary="Remote meeting restored",
            remote_etag='"etag-3"',
        )
        assert restored.event_version == 4
        assert restored.event.status == "confirmed"
        assert restored.graph_entity.status == "active"
        actions = {
            row.action for row in db.query(ActionAudit).filter(
                ActionAudit.owner_id == alice.id
            ).all()
        }
        assert {
            "calendar.event.remote_ingested",
            "calendar.event.remote_refreshed",
            "calendar.event.remote_disappeared",
        }.issubset(actions)


def test_open_delivery_and_legacy_marker_refuse_remote_overwrite(pull_env):
    with pull_env.Session() as db:
        alice = _account(db, pull_env.alice)
        local = create_calendar_event(
            db,
            account=alice,
            calendar_id=pull_env.alice_cal,
            uid="local-generation",
            summary="Local wins",
            dtstart="2026-07-20T09:00:00Z",
        )
        assert local.delivery is not None
        with pytest.raises(CalendarRemoteWritePending, match="awaiting delivery"):
            _ingest(
                db,
                account=alice,
                calendar_id=pull_env.alice_cal,
                uid="local-generation",
                expected_version=1,
                summary="Remote stale value",
            )
        assert local.event.summary == "Local wins"

        legacy = CalendarEvent(
            uid="legacy-generation",
            owner_id=alice.id,
            calendar_id=pull_env.alice_cal,
            summary="Legacy local wins",
            dtstart=local.event.dtstart,
            dtend=local.event.dtend,
            origin="local",
            caldav_sync_pending="update",
            version=1,
        )
        db.add(legacy)
        db.flush()
        with pytest.raises(CalendarRemoteWritePending, match="legacy"):
            _ingest(
                db,
                account=alice,
                calendar_id=pull_env.alice_cal,
                uid=legacy.uid,
                expected_version=1,
            )


def test_legacy_marker_is_atomically_adopted_into_encrypted_outbox(pull_env):
    with pull_env.Session() as db:
        alice = _account(db, pull_env.alice)
        event = CalendarEvent(
            uid="legacy-private-event",
            owner_id=alice.id,
            calendar_id=pull_env.alice_cal,
            summary="Private legacy summary",
            description="Private legacy body",
            dtstart=datetime(2026, 7, 20, 9),
            dtend=datetime(2026, 7, 20, 10),
            origin="local",
            caldav_sync_pending="create",
            version=1,
        )
        db.add(event)
        db.flush()

        delivery = adopt_legacy_caldav_delivery(
            db, account=alice, uid=event.uid, expected_version=1,
        )
        db.flush()
        db.refresh(event)

        assert delivery is not None
        assert delivery.operation == "create"
        assert delivery.expected_event_version == 1
        assert delivery.payload["event"]["summary"] == "Private legacy summary"
        assert event.caldav_sync_pending is None
        assert event.version == 1
        raw_payload = db.execute(text(
            "SELECT payload FROM calendar_deliveries WHERE id = :id"
        ), {"id": delivery.id}).scalar_one()
        assert "Private legacy summary" not in str(raw_payload)


def test_legacy_adoption_does_not_mistake_old_conflict_for_current_generation(
    pull_env,
):
    with pull_env.Session() as db:
        alice = _account(db, pull_env.alice)
        event = CalendarEvent(
            uid="legacy-new-generation",
            owner_id=alice.id,
            calendar_id=pull_env.alice_cal,
            summary="Current private generation",
            dtstart=datetime(2026, 7, 20, 9),
            dtend=datetime(2026, 7, 20, 10),
            origin="local",
            remote_href="https://dav.example.test/alice/legacy.ics",
            remote_etag='"old"',
            caldav_sync_pending="update",
            version=2,
        )
        old = CalendarDelivery(
            id=str(uuid.uuid4()),
            owner_id=alice.id,
            calendar_id=pull_env.alice_cal,
            event_uid=event.uid,
            operation="update",
            idempotency_key="old-conflicted-generation",
            payload={},
            expected_event_version=1,
            expected_config_version=1,
            state="conflict",
            attempts=1,
            version=1,
        )
        db.add_all((event, old))
        db.flush()

        current = adopt_legacy_caldav_delivery(
            db, account=alice, uid=event.uid, expected_version=2,
        )
        db.flush()
        db.refresh(event)

        assert current is not None
        assert current.id != old.id
        assert current.expected_event_version == 2
        assert current.state == "pending"
        assert event.caldav_sync_pending is None


def test_caldav_binding_uses_owner_id_and_versions_connector_changes(pull_env):
    with pull_env.Session() as db:
        alice = _account(db, pull_env.alice)
        created, was_created = upsert_caldav_calendar(
            db,
            account=alice,
            calendar_id="new-discovered-calendar",
            name="Discovered",
            connector_account_id="connector-a",
            remote_url="https://dav.example.test/a",
        )
        same, changed = upsert_caldav_calendar(
            db,
            account=alice,
            calendar_id=created.id,
            name="Discovered",
            connector_account_id="connector-a",
            remote_url="https://dav.example.test/a",
        )
        rebound, _ = upsert_caldav_calendar(
            db,
            account=alice,
            calendar_id=created.id,
            name="Discovered renamed",
            connector_account_id="connector-b",
            remote_url="https://dav.example.test/b",
        )

        assert was_created is True
        assert changed is False
        assert same.owner_id == alice.id
        assert rebound.config_version == 2
        assert rebound.account_id == "connector-b"


def test_push_direction_adopts_then_drains_durable_delivery(monkeypatch):
    from src import caldav_sync, calendar_delivery

    calls = []
    monkeypatch.setattr(
        caldav_sync,
        "_adopt_legacy_pending",
        lambda owner: {
            "owner_id": "durable-owner-id",
            "adopted": 2,
            "skipped": 0,
            "errors": [],
        },
    )

    def fake_drain(session_factory, *, owner_id, limit):
        calls.append((session_factory, owner_id, limit))
        return {"completed": 3, "retried": 1, "conflicts": 0}

    monkeypatch.setattr(calendar_delivery, "drain_calendar_deliveries", fake_drain)

    result = asyncio.run(caldav_sync.push_pending_events("legacy-alias"))

    assert result == {
        "events": 3,
        "completed": 3,
        "retried": 1,
        "conflicts": 0,
        "legacy_adopted": 2,
        "errors": [],
    }
    assert calls[0][1:] == ("durable-owner-id", 500)


def test_discovered_resource_href_cannot_change_origin_or_embed_credentials():
    from src.caldav_sync import _safe_remote_href

    base = "https://dav.example.test/calendars/private"
    assert _safe_remote_href(base, "event.ics") == (
        "https://dav.example.test/calendars/private/event.ics"
    )
    with pytest.raises(RuntimeError, match="changed origin"):
        _safe_remote_href(base, "https://internal.example.test/event.ics")
    with pytest.raises(ValueError, match="not allowed"):
        _safe_remote_href(base, "https://secret@dav.example.test/event.ics")


def test_window_absence_requires_authoritative_uid_not_found():
    from caldav.lib.error import NotFoundError
    from src.caldav_sync import _remote_uid_presence

    class Present:
        def event_by_uid(self, uid):
            return {"uid": uid}

    class Missing:
        def event_by_uid(self, _uid):
            raise NotFoundError("absent")

    class Unknown:
        def event_by_uid(self, _uid):
            raise RuntimeError("private server detail")

    assert _remote_uid_presence(Present(), "moved-outside-window") == (
        "present", None,
    )
    assert _remote_uid_presence(Missing(), "deleted") == ("missing", None)
    assert _remote_uid_presence(Unknown(), "uncertain") == (
        "unknown", "RuntimeError",
    )
