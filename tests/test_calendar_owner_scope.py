"""Account-id ownership and bounded calendar read-model regressions."""

from __future__ import annotations

import ast
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Account, Base, CalendarCal, CalendarEvent
from routes import calendar_routes


def test_get_upcoming_events_is_owner_scoped():
    source = Path("core/database.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "get_upcoming_events"
    )
    body = ast.unparse(fn)
    assert "join(CalendarCal)" in body
    assert "find_account(db, owner_alias)" in body
    assert "CalendarCal.owner_id == account.id" in body
    assert "CalendarEvent.owner_id == account.id" in body


def _calendar_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'owner-scope.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    alice = Account(id=str(uuid.uuid4()), username="alice")
    bob = Account(id=str(uuid.uuid4()), username="bob")
    alice_cal = CalendarCal(
        id=str(uuid.uuid4()), owner_id=alice.id, owner="alice",
        name="Alice", source="local",
    )
    bob_cal = CalendarCal(
        id=str(uuid.uuid4()), owner_id=bob.id, owner="bob",
        name="Bob", source="local",
    )
    db.add_all((alice, bob, alice_cal, bob_cal))
    db.flush()
    return engine, db, alice, bob, alice_cal, bob_cal


def _event(owner, calendar, uid, start, *, rrule=""):
    return CalendarEvent(
        uid=uid,
        owner_id=owner.id,
        calendar_id=calendar.id,
        summary=uid,
        dtstart=start,
        dtend=start + timedelta(hours=1),
        rrule=rrule,
        version=1,
    )


def test_list_read_model_filters_by_immutable_owner_id(tmp_path):
    engine, db, alice, bob, alice_cal, bob_cal = _calendar_db(tmp_path)
    try:
        start = datetime(2026, 7, 20, 10)
        db.add_all((
            _event(alice, alice_cal, "alice-event", start),
            _event(bob, bob_cal, "bob-event", start),
        ))
        db.flush()
        result = calendar_routes._list_events_for_owner(
            db,
            owner_id=alice.id,
            start_dt=datetime(2026, 7, 20),
            end_dt=datetime(2026, 7, 21),
        )
        assert [row["uid"] for row in result["events"]] == ["alice-event"]
        assert result["events"][0]["version"] == 1
    finally:
        db.close()
        engine.dispose()


def test_direct_events_are_not_starved_by_recurring_candidate_cap(
    tmp_path, monkeypatch
):
    engine, db, alice, _bob, alice_cal, _bob_cal = _calendar_db(tmp_path)
    try:
        monkeypatch.setattr(calendar_routes, "_CALENDAR_LIST_CANDIDATE_LIMIT", 2)
        monkeypatch.setattr(calendar_routes, "_CALENDAR_LIST_OUTPUT_LIMIT", 3)
        now = datetime(2026, 7, 20, 10)
        db.add(_event(alice, alice_cal, "current-direct", now))
        for index in range(5):
            db.add(_event(
                alice,
                alice_cal,
                f"old-series-{index}",
                datetime(2020, 1, index + 1, 9),
                rrule="FREQ=DAILY",
            ))
        db.flush()
        result = calendar_routes._list_events_for_owner(
            db,
            owner_id=alice.id,
            start_dt=datetime(2026, 7, 20),
            end_dt=datetime(2026, 7, 21),
        )
        assert "current-direct" in {row["uid"] for row in result["events"]}
        assert len(result["events"]) <= 3
        assert result["truncated"] is True
    finally:
        db.close()
        engine.dispose()


def test_export_filename_sanitizer_rejects_header_metacharacters():
    assert calendar_routes._safe_ics_filename(
        'Team\r\nX-Evil: yes/../"calendar"'
    ) == "Team__X-Evil__yes_..__calendar.ics"


def test_routes_no_longer_filter_calendar_authority_by_mutable_username():
    source = Path("routes/calendar_routes.py").read_text(encoding="utf-8")
    assert "CalendarEvent.owner_id == owner_id" in source
    assert "CalendarCal.owner_id == owner_id" in source
    assert "with request_account_transaction" in source
