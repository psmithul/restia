"""CalDAV UID lookup is scoped by immutable Account.id and collection."""
import tempfile
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import Account, CalendarEvent, CalendarCal
from src.caldav_sync import _find_existing_event

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(f"sqlite:///{_TMPDB.name}", connect_args={"check_same_thread": False}, poolclass=NullPool)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)
_ALICE_ID = "10000000-0000-0000-0000-000000000001"
_BOB_ID = "10000000-0000-0000-0000-000000000002"


def _setup():
    db = _TS()
    try:
        db.query(CalendarEvent).delete()
        db.query(CalendarCal).delete()
        db.query(Account).delete()
        db.add_all([
            Account(id=_ALICE_ID, username="alice"),
            Account(id=_BOB_ID, username="bob"),
        ])
        db.add(CalendarCal(
            id="calA", owner_id=_ALICE_ID, owner="alice", name="A",
        ))
        db.add(CalendarCal(
            id="calB", owner_id=_BOB_ID, owner="bob", name="B",
        ))
        # dtstart/dtend are NOT NULL in the schema, so seed valid values.
        db.add_all([
            CalendarEvent(
                uid="shared@svc", owner_id=_ALICE_ID, calendar_id="calA",
                summary="Alice event", dtstart=datetime(2026, 6, 4, 9, 0),
                dtend=datetime(2026, 6, 4, 10, 0),
            ),
            CalendarEvent(
                uid="shared@svc", owner_id=_BOB_ID, calendar_id="calB",
                summary="Bob event", dtstart=datetime(2026, 6, 4, 11, 0),
                dtend=datetime(2026, 6, 4, 12, 0),
            ),
        ])
        db.commit()
    finally:
        db.close()


def test_same_uid_resolves_independently_for_each_account():
    _setup()
    db = _TS()
    try:
        alice = _find_existing_event(
            db, {}, "shared@svc", "calA", _ALICE_ID,
        )
        bob = _find_existing_event(
            db, {}, "shared@svc", "calB", _BOB_ID,
        )
        assert alice is not None and alice.summary == "Alice event"
        assert bob is not None and bob.summary == "Bob event"
        assert alice.owner_id != bob.owner_id
    finally:
        db.close()


def test_cross_account_calendar_lookup_cannot_hijack_uid():
    _setup()
    db = _TS()
    try:
        assert _find_existing_event(
            db, {}, "shared@svc", "calB", _ALICE_ID,
        ) is None
        alice = db.query(CalendarEvent).filter(
            CalendarEvent.uid == "shared@svc",
            CalendarEvent.owner_id == _ALICE_ID,
        ).one()
        assert alice.calendar_id == "calA"
    finally:
        db.close()


def test_pending_takes_precedence():
    _setup()
    db = _TS()
    try:
        sentinel = object()
        assert _find_existing_event(
            db,
            {(_BOB_ID, "shared@svc"): sentinel},
            "shared@svc",
            "calB",
            _BOB_ID,
        ) is sentinel
    finally:
        db.close()
