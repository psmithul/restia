from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    ActionAudit,
    Base,
    CalendarCal,
    CalendarDelivery,
    CalendarEvent,
    EntityLink,
    LifeEntity,
)
from src import calendar_service
from src.calendar_service import (
    CalendarConflict,
    CalendarNotFound,
    CalendarServiceError,
    cancel_calendar_event,
    create_calendar_event,
    ensure_default_calendar,
    reschedule_calendar_event,
    restore_calendar_event,
    restore_calendar_event_snapshot,
    update_calendar_event,
)
from src.life_graph import create_life_entity


@pytest.fixture()
def calendar_env(tmp_path, monkeypatch):
    from src import secret_storage

    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(secret_storage, "_digest_key", None)
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar-service.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        alice = Account(
            id=str(uuid.uuid4()), username="alice", status="active", auth_epoch=1
        )
        bob = Account(
            id=str(uuid.uuid4()), username="bob", status="active", auth_epoch=1
        )
        db.add_all((alice, bob))
        db.commit()
        alice_id, bob_id = alice.id, bob.id
    yield SimpleNamespace(
        Session=factory, engine=engine, alice_id=alice_id, bob_id=bob_id
    )
    engine.dispose()


def _accounts(db, env):
    return (
        db.query(Account).filter(Account.id == env.alice_id).one(),
        db.query(Account).filter(Account.id == env.bob_id).one(),
    )


def _caldav_calendar(db, account, *, name="CalDAV"):
    calendar = CalendarCal(
        id=str(uuid.uuid4()),
        owner_id=account.id,
        owner=account.username,
        name=name,
        source="caldav",
        account_id=str(uuid.uuid4()),
        config_version=3,
    )
    db.add(calendar)
    db.flush()
    return calendar


def _target(db, account, *, entity_type="project", title="Target"):
    entity, _ = create_life_entity(
        db,
        account=account,
        entity_type=entity_type,
        title=title,
        idempotency_key=f"target:{account.id}:{entity_type}:{title}",
    )
    return entity


def test_default_calendar_is_deterministic_and_owner_scoped(calendar_env):
    with calendar_env.Session() as db:
        alice, bob = _accounts(db, calendar_env)
        first = ensure_default_calendar(db, account=alice)
        again = ensure_default_calendar(db, account=alice)
        other = ensure_default_calendar(db, account=bob)

        assert first.id == again.id
        assert first.owner_id == alice.id
        assert other.owner_id == bob.id
        assert other.id != first.id
        assert db.query(CalendarCal).count() == 2
        assert db.query(ActionAudit).filter(
            ActionAudit.action == "calendar.created"
        ).count() == 2


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"dtstart": "2026-07-20", "dtend": "2026-07-21"},
            "include a time",
        ),
        (
            {
                "dtstart": "2026-07-20T10:00:00Z",
                "dtend": "2026-07-20T09:00:00Z",
            },
            "later",
        ),
        (
            {
                "dtstart": "2026-07-20T10:00:00Z",
                "dtend": "2026-07-20T11:00:00Z",
                "rrule": "FREQ=MINUTELY",
            },
            "frequency",
        ),
        (
            {
                "dtstart": "2026-07-20T10:00:00Z",
                "dtend": "2026-07-20T11:00:00Z",
                "rrule": "FREQ=DAILY;COUNT=3;UNTIL=20260730T100000Z",
            },
            "COUNT and UNTIL",
        ),
    ],
)
def test_strict_datetime_and_recurrence_validation(calendar_env, kwargs, message):
    with calendar_env.Session() as db:
        alice, _ = _accounts(db, calendar_env)
        with pytest.raises(CalendarServiceError, match=message):
            create_calendar_event(db, account=alice, summary="Private", **kwargs)


def test_all_day_dates_and_aware_datetimes_are_normalized(calendar_env):
    with calendar_env.Session() as db:
        alice, _ = _accounts(db, calendar_env)
        all_day = create_calendar_event(
            db,
            account=alice,
            summary="All day",
            dtstart=date(2026, 7, 20),
            all_day=True,
        )
        timed = create_calendar_event(
            db,
            account=alice,
            summary="Timed",
            dtstart="2026-07-20T15:30:00+05:30",
            dtend="2026-07-20T16:30:00+05:30",
        )

        assert all_day.event.dtend == datetime(2026, 7, 21)
        assert all_day.event.is_utc is False
        assert timed.event.dtstart == datetime(2026, 7, 20, 10)
        assert timed.event.is_utc is True


def test_create_is_idempotent_and_projects_one_graph_entity(calendar_env):
    with calendar_env.Session() as db:
        alice, _ = _accounts(db, calendar_env)
        first = create_calendar_event(
            db,
            account=alice,
            summary="One private meeting",
            dtstart="2026-07-20T10:00:00Z",
            idempotency_key="capture-1",
        )
        again = create_calendar_event(
            db,
            account=alice,
            summary="One private meeting",
            dtstart="2026-07-20T10:00:00Z",
            idempotency_key="capture-1",
        )

        assert first.created is True
        assert again.created is False
        assert first.event.uid == again.event.uid
        assert first.event_version == again.event_version == 1
        assert first.graph_entity.id == again.graph_entity.id
        assert first.graph_version == again.graph_version == 1
        assert db.query(CalendarEvent).count() == 1
        assert db.query(LifeEntity).filter(
            LifeEntity.domain_ref_type == "calendar_event"
        ).count() == 1
        assert db.query(ActionAudit).filter(
            ActionAudit.action == "calendar.event.created"
        ).count() == 1

        with pytest.raises(CalendarConflict):
            create_calendar_event(
                db,
                account=alice,
                summary="Different content",
                dtstart="2026-07-20T10:00:00Z",
                idempotency_key="capture-1",
            )


def test_caldav_delivery_is_enqueued_in_the_caller_transaction(calendar_env):
    with calendar_env.Session() as db:
        alice, _ = _accounts(db, calendar_env)
        calendar = _caldav_calendar(db, alice)
        result = create_calendar_event(
            db,
            account=alice,
            calendar_id=calendar.id,
            summary="Remote private meeting",
            dtstart="2026-07-20T10:00:00Z",
            idempotency_key="remote-create",
        )

        assert result.delivery is not None
        assert result.delivery.operation == "create"
        assert result.delivery.expected_event_version == 1
        assert result.delivery.expected_config_version == 3
        assert result.delivery_version == 1
        assert result.delivery.payload["event"]["summary"] == "Remote private meeting"
        assert db.query(CalendarDelivery).count() == 1

        db.rollback()
        assert db.query(CalendarEvent).count() == 0
        assert db.query(CalendarDelivery).count() == 0
        assert db.query(LifeEntity).filter(
            LifeEntity.domain_ref_type == "calendar_event"
        ).count() == 0


def test_pending_caldav_create_coalesces_later_updates(calendar_env):
    with calendar_env.Session() as db:
        alice, _ = _accounts(db, calendar_env)
        calendar = _caldav_calendar(db, alice)
        created = create_calendar_event(
            db,
            account=alice,
            calendar_id=calendar.id,
            summary="First payload",
            dtstart="2026-07-20T10:00:00Z",
        )
        delivery_id = created.delivery.id

        updated = update_calendar_event(
            db,
            account=alice,
            uid=created.event.uid,
            expected_version=1,
            changes={"summary": "Latest payload"},
        )
        rescheduled = reschedule_calendar_event(
            db,
            account=alice,
            uid=created.event.uid,
            expected_version=2,
            dtstart="2026-07-21T12:00:00Z",
            dtend="2026-07-21T13:00:00Z",
        )

        assert db.query(CalendarDelivery).count() == 1
        delivery = db.query(CalendarDelivery).one()
        assert delivery.id == delivery_id
        assert delivery.operation == "create"
        assert delivery.state == "pending"
        assert delivery.version == 3
        assert delivery.expected_event_version == 3
        assert delivery.payload["event"]["summary"] == "Latest payload"
        assert delivery.payload["event"]["dtstart"] == "2026-07-21T12:00:00Z"
        assert updated.delivery.id == delivery_id
        assert rescheduled.delivery.id == delivery_id


def test_cancel_cancels_an_unclaimed_create_without_remote_delete(calendar_env):
    with calendar_env.Session() as db:
        alice, _ = _accounts(db, calendar_env)
        calendar = _caldav_calendar(db, alice)
        created = create_calendar_event(
            db,
            account=alice,
            calendar_id=calendar.id,
            summary="Never reaches remote",
            dtstart="2026-07-20T10:00:00Z",
        )
        cancelled = cancel_calendar_event(
            db,
            account=alice,
            uid=created.event.uid,
            expected_version=1,
        )

        assert db.query(CalendarDelivery).count() == 1
        delivery = db.query(CalendarDelivery).one()
        assert delivery.id == created.delivery.id == cancelled.delivery.id
        assert delivery.operation == "create"
        assert delivery.state == "cancelled"
        assert delivery.version == 2
        assert delivery.expected_event_version == 2
        assert delivery.completed_at is not None


def test_claimed_create_forces_a_fifo_update_instead_of_coalescing(calendar_env):
    with calendar_env.Session() as db:
        alice, _ = _accounts(db, calendar_env)
        calendar = _caldav_calendar(db, alice)
        created = create_calendar_event(
            db,
            account=alice,
            calendar_id=calendar.id,
            summary="Create in flight",
            dtstart="2026-07-20T10:00:00Z",
        )
        created.delivery.state = "processing"
        created.delivery.claim_token = str(uuid.uuid4())
        created.delivery.claimed_at = datetime.utcnow()
        db.flush()

        updated = update_calendar_event(
            db,
            account=alice,
            uid=created.event.uid,
            expected_version=1,
            changes={"summary": "Queued after create"},
        )

        rows = db.query(CalendarDelivery).order_by(
            CalendarDelivery.created_at.asc(), CalendarDelivery.id.asc()
        ).all()
        assert len(rows) == 2
        assert {row.operation for row in rows} == {"create", "update"}
        assert updated.delivery.operation == "update"
        assert updated.delivery.expected_event_version == 2


def test_wrong_owner_stale_version_and_occurrence_ids_fail_closed(calendar_env):
    with calendar_env.Session() as db:
        alice, bob = _accounts(db, calendar_env)
        result = create_calendar_event(
            db,
            account=alice,
            summary="Owned event",
            dtstart="2026-07-20T10:00:00",
        )
        db.commit()

        with pytest.raises(CalendarNotFound):
            update_calendar_event(
                db,
                account=bob,
                uid=result.event.uid,
                expected_version=1,
                changes={"summary": "Cross-owner"},
            )
        with pytest.raises(CalendarServiceError, match="occurrence"):
            update_calendar_event(
                db,
                account=alice,
                uid=f"{result.event.uid}::2026-07-21T10:00:00",
                expected_version=1,
                changes={"summary": "Occurrence"},
            )

        updated = update_calendar_event(
            db,
            account=alice,
            uid=result.event.uid,
            expected_version=1,
            changes={"summary": "Version two"},
        )
        assert updated.event_version == 2
        with pytest.raises(CalendarConflict):
            update_calendar_event(
                db,
                account=alice,
                uid=result.event.uid,
                expected_version=1,
                changes={"summary": "Stale"},
            )
        db.refresh(updated.event)
        assert updated.event.summary == "Version two"


def test_links_are_allowlisted_idempotent_and_preserved(calendar_env):
    with calendar_env.Session() as db:
        alice, bob = _accounts(db, calendar_env)
        first_target = _target(db, alice, entity_type="project", title="Project")
        second_target = _target(db, alice, entity_type="person", title="Person")
        goal = _target(db, alice, entity_type="goal", title="Goal")
        cross_owner = _target(db, bob, entity_type="project", title="Bob project")

        created = create_calendar_event(
            db,
            account=alice,
            summary="Linked event",
            dtstart="2026-07-20T10:00:00",
            linked_entity_ids=[first_target.id],
            idempotency_key="linked-create",
        )
        assert len(created.links_created) == 1
        retry = create_calendar_event(
            db,
            account=alice,
            summary="Linked event",
            dtstart="2026-07-20T10:00:00",
            linked_entity_ids=[first_target.id],
            idempotency_key="linked-create",
        )
        assert retry.links_created == ()

        updated = update_calendar_event(
            db,
            account=alice,
            uid=created.event.uid,
            expected_version=1,
            changes={},
            linked_entity_ids=[second_target.id],
        )
        assert len(updated.links_created) == 1
        targets = {
            row.target_id for row in db.query(EntityLink).filter(
                EntityLink.source_id == created.graph_entity.id,
                EntityLink.deleted_at.is_(None),
            )
        }
        assert targets == {first_target.id, second_target.id}

        goal_link = update_calendar_event(
            db,
            account=alice,
            uid=created.event.uid,
            expected_version=1,
            changes={},
            linked_entity_ids=[goal.id],
        )
        assert [row.target_id for row in goal_link.links_created] == [goal.id]
        with pytest.raises(CalendarNotFound):
            update_calendar_event(
                db,
                account=alice,
                uid=created.event.uid,
                expected_version=1,
                changes={},
                linked_entity_ids=[cross_owner.id],
            )


def test_reschedule_cancel_restore_and_snapshot_restore_each_use_one_cas(
    calendar_env,
):
    with calendar_env.Session() as db:
        alice, _ = _accounts(db, calendar_env)
        calendar = _caldav_calendar(db, alice)
        created = create_calendar_event(
            db,
            account=alice,
            calendar_id=calendar.id,
            summary="Original",
            description="Before",
            dtstart="2026-07-20T10:00:00Z",
            dtend="2026-07-20T11:00:00Z",
        )
        original = created.after_snapshot
        rescheduled = reschedule_calendar_event(
            db,
            account=alice,
            uid=created.event.uid,
            expected_version=1,
            dtstart="2026-07-21T12:00:00Z",
            dtend="2026-07-21T13:30:00Z",
            rrule="FREQ=WEEKLY;COUNT=3",
        )
        assert rescheduled.event_version == 2
        cancelled = cancel_calendar_event(
            db,
            account=alice,
            uid=created.event.uid,
            expected_version=2,
        )
        assert cancelled.event_version == 3
        assert cancelled.delivery.operation == "create"
        assert cancelled.delivery.state == "cancelled"
        restored = restore_calendar_event(
            db,
            account=alice,
            uid=created.event.uid,
            expected_version=3,
        )
        assert restored.event_version == 4

        updated = update_calendar_event(
            db,
            account=alice,
            uid=created.event.uid,
            expected_version=4,
            changes={"summary": "Changed", "description": "After"},
        )
        replayed = restore_calendar_event_snapshot(
            db,
            account=alice,
            uid=created.event.uid,
            expected_version=5,
            snapshot=original,
        )
        assert replayed.event_version == 6
        assert replayed.event.summary == "Original"
        assert replayed.event.description == "Before"
        assert replayed.event.dtstart == datetime(2026, 7, 20, 10)
        assert replayed.event.rrule == ""
        assert replayed.delivery is not None
        assert replayed.delivery.expected_event_version == 6
        assert updated.before_snapshot["summary"] == "Original"


def test_snapshot_shape_and_identity_are_strict(calendar_env):
    with calendar_env.Session() as db:
        alice, _ = _accounts(db, calendar_env)
        created = create_calendar_event(
            db,
            account=alice,
            summary="Snapshot",
            dtstart="2026-07-20T10:00:00",
        )
        malformed = dict(created.after_snapshot)
        malformed["extra"] = "not allowed"
        with pytest.raises(CalendarServiceError, match="shape"):
            restore_calendar_event_snapshot(
                db,
                account=alice,
                uid=created.event.uid,
                expected_version=1,
                snapshot=malformed,
            )
        wrong_uid = dict(created.after_snapshot)
        wrong_uid["uid"] = str(uuid.uuid4())
        with pytest.raises(CalendarServiceError, match="UID"):
            restore_calendar_event_snapshot(
                db,
                account=alice,
                uid=created.event.uid,
                expected_version=1,
                snapshot=wrong_uid,
            )


def test_snapshot_restore_preserves_legacy_whitespace_exactly(calendar_env):
    with calendar_env.Session() as db:
        alice, _ = _accounts(db, calendar_env)
        calendar = ensure_default_calendar(db, account=alice)
        event = CalendarEvent(
            uid="legacy-whitespace-event",
            owner_id=alice.id,
            calendar_id=calendar.id,
            summary="  Legacy   title  ",
            description="\n  exact description  \n",
            location="  Room   42  ",
            dtstart=datetime(2026, 7, 20, 10),
            dtend=datetime(2026, 7, 20, 11),
            recurrence_exdates='["2026-07-27T10:00:00"]',
            color="  #abc  ",
            version=1,
        )
        db.add(event)
        db.flush()
        before = calendar_service.snapshot_calendar_event(event)

        changed = update_calendar_event(
            db,
            account=alice,
            uid=event.uid,
            expected_version=1,
            changes={
                "summary": "Changed",
                "description": "Changed description",
                "location": "Changed location",
                "color": "#000",
            },
        )
        restored = restore_calendar_event_snapshot(
            db,
            account=alice,
            uid=event.uid,
            expected_version=changed.event_version,
            snapshot=before,
        )

        assert restored.event.summary == "  Legacy   title  "
        assert restored.event.description == "\n  exact description  \n"
        assert restored.event.location == "  Room   42  "
        assert restored.event.color == "  #abc  "
        assert restored.event.recurrence_exdates == '["2026-07-27T10:00:00"]'


def test_projection_failure_can_roll_back_the_entire_mutation(
    calendar_env, monkeypatch
):
    with calendar_env.Session() as db:
        alice, _ = _accounts(db, calendar_env)

        def fail_projection(*_args, **_kwargs):
            raise RuntimeError("injected projection failure")

        monkeypatch.setattr(calendar_service, "_project_event", fail_projection)
        with pytest.raises(RuntimeError, match="injected"):
            create_calendar_event(
                db,
                account=alice,
                summary="Must roll back",
                dtstart="2026-07-20T10:00:00",
            )
        db.rollback()
        assert db.query(CalendarEvent).count() == 0
        assert db.query(LifeEntity).filter(
            LifeEntity.domain_ref_type == "calendar_event"
        ).count() == 0
        assert db.query(ActionAudit).filter(
            ActionAudit.action.like("calendar.%")
        ).count() == 0


def test_delivery_failure_rolls_back_event_and_graph_versions(
    calendar_env, monkeypatch
):
    with calendar_env.Session() as db:
        alice, _ = _accounts(db, calendar_env)
        calendar = _caldav_calendar(db, alice)
        created = create_calendar_event(
            db,
            account=alice,
            calendar_id=calendar.id,
            summary="Before failure",
            dtstart="2026-07-20T10:00:00",
        )
        db.commit()
        original_event_version = created.event_version
        original_graph_version = created.graph_version
        original_delivery_count = db.query(CalendarDelivery).count()

        def fail_delivery(*_args, **_kwargs):
            raise RuntimeError("injected delivery failure")

        monkeypatch.setattr(calendar_service, "_enqueue_delivery", fail_delivery)
        with pytest.raises(RuntimeError, match="injected"):
            update_calendar_event(
                db,
                account=alice,
                uid=created.event.uid,
                expected_version=original_event_version,
                changes={"summary": "Should not persist"},
            )
        db.rollback()
        event = db.query(CalendarEvent).filter(
            CalendarEvent.uid == created.event.uid
        ).one()
        graph = db.query(LifeEntity).filter(
            LifeEntity.domain_ref_type == "calendar_event",
            LifeEntity.domain_ref_id == created.event.uid,
        ).one()
        assert event.summary == "Before failure"
        assert event.version == original_event_version
        assert graph.version == original_graph_version
        assert db.query(CalendarDelivery).count() == original_delivery_count


def test_calendar_audits_are_structural_and_do_not_contain_event_pii(calendar_env):
    with calendar_env.Session() as db:
        alice, _ = _accounts(db, calendar_env)
        private_values = (
            "Board compensation discussion",
            "Private agenda with acquisition terms",
            "Home address room",
        )
        result = create_calendar_event(
            db,
            account=alice,
            summary=private_values[0],
            description=private_values[1],
            location=private_values[2],
            dtstart="2026-07-20T10:00:00",
        )
        update_calendar_event(
            db,
            account=alice,
            uid=result.event.uid,
            expected_version=1,
            changes={"summary": "Changed private title"},
        )
        rows = db.query(ActionAudit).filter(
            ActionAudit.owner_id == alice.id
        ).all()
        serialized = json.dumps([
            {
                "action": row.action,
                "before": row.before_state,
                "after": row.after_state,
                "details": row.details,
            }
            for row in rows
        ])
        for value in (*private_values, "Changed private title"):
            assert value not in serialized
