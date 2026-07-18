"""Immutable account authority for legacy calendar read helpers."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import database as cdb
from src.builtin_actions import _telegram_digest_calendar_candidates


def _calendar_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-readers.db'}")
    cdb.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    alice = cdb.Account(id=str(uuid.uuid4()), username="alice")
    bob = cdb.Account(id=str(uuid.uuid4()), username="bob")
    alice_cal = cdb.CalendarCal(
        id=str(uuid.uuid4()), owner_id=alice.id, owner="legacy-alice",
        name="Alice", source="local",
    )
    bob_cal = cdb.CalendarCal(
        id=str(uuid.uuid4()), owner_id=bob.id, owner="alice",
        name="Bob", source="local",
    )
    db.add_all((alice, bob, alice_cal, bob_cal))
    db.flush()
    start = cdb.utcnow_naive() + timedelta(days=1)
    db.add_all((
        cdb.CalendarEvent(
            uid="same-rfc-uid", owner_id=alice.id, calendar_id=alice_cal.id,
            summary="Alice event", dtstart=start,
            dtend=start + timedelta(hours=1), version=4,
        ),
        cdb.CalendarEvent(
            uid="same-rfc-uid", owner_id=bob.id, calendar_id=bob_cal.id,
            summary="Bob event", dtstart=start,
            dtend=start + timedelta(hours=1), version=9,
        ),
    ))
    db.commit()
    return engine, factory, db


def test_get_upcoming_events_resolves_account_and_returns_version(tmp_path, monkeypatch):
    engine, factory, db = _calendar_db(tmp_path)
    try:
        monkeypatch.setattr(cdb, "SessionLocal", factory)

        assert cdb.get_upcoming_events("alice") == [{
            "uid": "same-rfc-uid",
            "title": "Alice event",
            "start": db.query(cdb.CalendarEvent).filter(
                cdb.CalendarEvent.owner_id == db.query(cdb.Account).filter_by(
                    username="alice"
                ).one().id
            ).one().dtstart.isoformat(),
            "version": 4,
        }]
        assert cdb.get_upcoming_events("unknown") == []
        assert cdb.get_upcoming_events(None) == []
    finally:
        db.close()
        engine.dispose()


def test_telegram_digest_candidates_use_both_immutable_owner_filters(tmp_path):
    engine, _factory, db = _calendar_db(tmp_path)
    try:
        rows = _telegram_digest_calendar_candidates(db, "alice")
        assert [(row.summary, row.version) for row in rows] == [("Alice event", 4)]
        assert _telegram_digest_calendar_candidates(db, "unknown") == []
        assert _telegram_digest_calendar_candidates(db, "") == []
    finally:
        db.close()
        engine.dispose()
