"""Only owner-scoped, previously pulled resources may disappear remotely.

The production pull path feeds these candidates to the canonical soft-cancel
service; it never deletes ``CalendarEvent`` rows directly.
"""
import tempfile
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import Account, CalendarEvent, CalendarCal

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)

_NOW = datetime(2026, 6, 4, 12, 0)
_START = _NOW - timedelta(days=90)
_END = _NOW + timedelta(days=365)
_OWNER_ID = "20000000-0000-0000-0000-000000000001"


def _stale_candidates(db, calendar_id, owner_id, seen_uids):
    return db.query(CalendarEvent).filter(
        CalendarEvent.calendar_id == calendar_id,
        CalendarEvent.owner_id == owner_id,
        CalendarEvent.origin == "caldav",
        CalendarEvent.dtstart >= _START,
        CalendarEvent.dtstart <= _END,
        CalendarEvent.remote_href.isnot(None),
        ~CalendarEvent.uid.in_(seen_uids) if seen_uids else CalendarEvent.uid.isnot(None),
    ).all()


def _seed():
    db = _TS()
    try:
        db.query(CalendarEvent).delete()
        db.query(CalendarCal).delete()
        db.query(Account).delete()
        db.add(Account(id=_OWNER_ID, username="alice"))
        db.add(CalendarCal(
            id="cal1", owner_id=_OWNER_ID, owner="alice", name="Work",
            source="caldav",
        ))
        # A server-synced event whose UID is NO LONGER returned (deleted upstream).
        db.add(CalendarEvent(
            uid="server-gone@svc", owner_id=_OWNER_ID, calendar_id="cal1",
            summary="Old server event",
            dtstart=_NOW + timedelta(days=1), dtend=_NOW + timedelta(days=1, hours=1),
            origin="caldav",
            remote_href="https://dav.example.test/cal1/server-gone.ics",
        ))
        # A locally-created event (agent / triage / failed write-back) — origin NULL.
        db.add(CalendarEvent(
            uid="local-uuid", owner_id=_OWNER_ID, calendar_id="cal1",
            summary="Dentist",
            dtstart=_NOW + timedelta(days=2), dtend=_NOW + timedelta(days=2, hours=1),
            origin=None,
        ))
        db.commit()
    finally:
        db.close()


def test_local_event_is_not_a_remote_disappearance_candidate():
    _seed()
    db = _TS()
    try:
        # Server returned nothing (both UIDs absent from seen_uids).
        stale = _stale_candidates(
            db, "cal1", _OWNER_ID, seen_uids={"some-other-uid"},
        )
        assert [row.uid for row in stale] == ["server-gone@svc"]
        assert db.query(CalendarEvent).count() == 2
    finally:
        db.close()


def test_synced_event_still_returned_is_not_a_disappearance_candidate():
    _seed()
    db = _TS()
    try:
        # The server still returns the synced event → it must be kept.
        stale = _stale_candidates(
            db, "cal1", _OWNER_ID, seen_uids={"server-gone@svc"},
        )
        assert stale == []
        assert db.query(CalendarEvent).filter_by(uid="server-gone@svc").first() is not None
        assert db.query(CalendarEvent).filter_by(uid="local-uuid").first() is not None
    finally:
        db.close()
