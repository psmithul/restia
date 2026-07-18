"""Calendar reminder requests must never become hidden Note side effects.

Calendar action undo currently covers the event, Life projection, links, and
connector outbox only. Until reminders have their own reviewed proposal and
reversal path, ``manage_calendar`` fails loudly and directs the caller to an
explicit ``manage_notes`` action.
"""

import json
import sys
import uuid

import pytest

from tests.helpers.import_state import clear_fake_database_modules
from tests.helpers.sqlite_db import make_temp_sqlite

clear_fake_database_modules()

import core.database as cdb
from core.database import Account, Note

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)


@pytest.fixture(autouse=True)
def _bind_temp_db(monkeypatch):
    monkeypatch.setitem(sys.modules, "core.database", cdb)
    parent = sys.modules.get("core")
    if parent is not None:
        monkeypatch.setattr(parent, "database", cdb, raising=False)
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    yield


async def _create_with_reminder(reminder, owner):
    from src.tool_implementations import do_manage_calendar

    payload = {
        "action": "create_event",
        "summary": "Dentist",
        # Far-future so the reminder is never "already passed".
        "dtstart": "2030-01-01T10:00:00",
        "reminder_minutes": reminder,
    }
    return await do_manage_calendar(json.dumps(payload), owner=owner)


@pytest.mark.parametrize("reminder,expected", [
    ("5 mins", 5),
    ("10 mins", 10),
    ("2 hrs", 120),
    ("1 hr", 60),
    ("15 minutes", 15),   # regression: long form still works
    ("30m", 30),          # regression: bare unit still works
])
async def test_reminder_minutes_requires_separate_explicit_action(reminder, expected):
    owner = "tester-" + uuid.uuid4().hex[:6]
    res = await _create_with_reminder(reminder, owner)
    assert expected > 0  # Keep every historical accepted spelling in the matrix.
    assert res.get("exit_code") == 1, res
    assert res.get("reminder_requires_separate_action") is True
    assert "manage_notes" in res.get("error", "")

    db = _TS()
    try:
        assert db.query(Note).filter(Note.owner == owner).count() == 0
        assert db.query(Account).filter(Account.username == owner).count() == 0
    finally:
        db.close()


async def test_no_reminder_when_offset_absent():
    owner = "tester-" + uuid.uuid4().hex[:6]
    from src.tool_implementations import do_manage_calendar

    payload = {
        "action": "create_event",
        "summary": "No Reminder Event",
        "dtstart": "2030-02-01T10:00:00",
    }
    res = await do_manage_calendar(json.dumps(payload), owner=owner)
    assert res.get("exit_code") == 0, res
    assert "reminder set" not in res.get("response", ""), res
