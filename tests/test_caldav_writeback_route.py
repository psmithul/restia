"""Calendar HTTP writes use the transactional CalDAV outbox, never I/O."""

from pathlib import Path


def test_http_calendar_writes_use_canonical_service_and_no_network_helper():
    source = Path("routes/calendar_routes.py").read_text(encoding="utf-8")
    assert "create_calendar_event(" in source
    assert "update_calendar_event(" in source
    assert "reschedule_calendar_event(" in source
    assert "cancel_calendar_event(" in source
    assert "exclude_calendar_occurrence(" in source
    assert "_push_caldav_event_after_commit" not in source
    assert "push_event_create" not in source
    assert "push_event_update" not in source
    assert "push_event_delete" not in source


def test_calendar_service_commits_delivery_with_local_state():
    source = Path("src/calendar_service.py").read_text(encoding="utf-8")
    assert "def _enqueue_delivery(" in source
    assert "CalendarDelivery(" in source
    assert "db.flush()" in source
    assert "db.commit()" not in source


def test_http_mutations_return_server_versions_for_client_cas():
    route_source = Path("routes/calendar_routes.py").read_text(encoding="utf-8")
    client_source = Path("static/js/calendar.js").read_text(encoding="utf-8")
    assert '"version": result.event_version' in route_source
    assert "version: expectedVersion" in client_source
    assert "version: String(expectedVersion)" in client_source
