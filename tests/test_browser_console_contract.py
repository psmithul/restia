from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_calendar_poller_never_prompts_for_notification_permission_on_startup():
    source = (ROOT / "static/js/calendar/reminders.js").read_text(encoding="utf-8")

    assert "Notification.requestPermission" not in source


def test_background_patterns_do_not_use_scroll_linked_fixed_painting():
    source = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert "background-attachment: fixed" not in source
