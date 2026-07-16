import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_calendar_poller_never_prompts_for_notification_permission_on_startup():
    source = (ROOT / "static/js/calendar/reminders.js").read_text(encoding="utf-8")

    assert "Notification.requestPermission" not in source


def test_calendar_poller_unwraps_the_notes_api_envelope_executably():
    helper = (ROOT / "static/js/calendar/reminderPayload.js").as_uri()
    script = f"""
      import {{ notesFromPayload }} from {json.dumps(helper)};
      console.log(JSON.stringify({{
        envelope: notesFromPayload({{notes: [{{id: 'due-1'}}]}}),
        legacy: notesFromPayload([{{id: 'due-2'}}]),
        malformed: notesFromPayload({{items: [{{id: 'wrong'}}]}}),
      }}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    payload = json.loads(result.stdout)
    assert payload == {
        "envelope": [{"id": "due-1"}],
        "legacy": [{"id": "due-2"}],
        "malformed": [],
    }

    poller = (ROOT / "static/js/calendar/reminders.js").read_text(encoding="utf-8")
    assert "const notes = notesFromPayload(payload);" in poller


def test_background_patterns_do_not_use_scroll_linked_fixed_painting():
    source = (ROOT / "static/style.css").read_text(encoding="utf-8")

    assert "background-attachment: fixed" not in source
