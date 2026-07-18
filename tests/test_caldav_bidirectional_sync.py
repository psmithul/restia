"""Regression coverage for CalDAV serialization and durable delivery plumbing."""

from datetime import datetime
from pathlib import Path

from src.caldav_writeback import build_event_ical


def test_event_to_ical_serializes_core_fields_and_rrule():
    ical = build_event_ical({
        "uid": "evt-123",
        "summary": "Planning",
        "description": "Bring notes",
        "location": "HQ",
        "dtstart": datetime(2026, 6, 5, 9, 0),
        "dtend": datetime(2026, 6, 5, 10, 0),
        "all_day": False,
        "is_utc": False,
        "rrule": "FREQ=WEEKLY;COUNT=2",
    })
    assert "UID:evt-123" in ical
    assert "SUMMARY:Planning" in ical
    assert "DESCRIPTION:Bring notes" in ical
    assert "LOCATION:HQ" in ical
    assert "RRULE:FREQ=WEEKLY;COUNT=2" in ical


def test_caldav_pull_prune_only_soft_cancels_owned_remote_rows():
    source = Path("src/caldav_sync.py").read_text(encoding="utf-8")
    assert 'CalendarEvent.origin == "caldav"' in source
    assert "CalendarEvent.owner_id == account.id" in source
    assert "CalendarEvent.remote_href.isnot(None)" in source
    assert "cancel_missing_remote_calendar_event(" in source
    assert "CalendarRemoteWritePending" in source


def test_http_and_agent_writes_share_server_owned_action_and_outbox_paths():
    http_source = Path("routes/calendar_routes.py").read_text(encoding="utf-8")
    tool_source = Path("src/tools/calendar.py").read_text(encoding="utf-8")
    service_source = Path("src/calendar_service.py").read_text(encoding="utf-8")
    assert "request_account_transaction" in http_source
    assert "create_calendar_event(" in http_source
    assert "_execute_level_four(" in tool_source
    assert "execute_calendar_action(" in tool_source
    assert "CalendarDelivery(" in service_source
    for source in (http_source, tool_source):
        assert "_push_caldav_event_after_commit" not in source
        assert "push_event_create" not in source


def test_database_declares_versioned_encrypted_caldav_delivery():
    source = Path("core/database.py").read_text(encoding="utf-8")
    for needle in [
        "class CalendarDelivery",
        "payload = Column(EncryptedJSON",
        "expected_event_version = Column(Integer, nullable=False)",
        "expected_config_version = Column(Integer, nullable=False)",
        "idempotency_key = Column(String(96), nullable=False)",
        "lease_expires_at = Column(DateTime",
        "ix_calendar_deliveries_event_order",
    ]:
        assert needle in source
