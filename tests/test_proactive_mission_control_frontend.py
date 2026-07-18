"""Static contracts for the bounded proactive Mission Control surface."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mission_control_renders_interruptions_without_repeating_digest_items():
    source = (ROOT / "static/js/missionControl.js").read_text(encoding="utf-8")

    assert "'proactive'," in source
    assert "sourceSummaryCard('Signals', 'proactive_interruptions', 'proactive'" in source
    assert "function renderProactiveInterruptions()" in source
    assert "rows(value.interruptions).slice(0, 8)" in source
    assert "rows(value.digest)" not in source
    assert "deterministic signal" in source
    assert "record-only" in source
    assert "renderProactiveInterruptions()" in source.split("function render()", 1)[1]


def test_proactive_surface_has_no_execution_or_notification_controls():
    source = (ROOT / "static/js/missionControl.js").read_text(encoding="utf-8")
    function_body = source.split("function renderProactiveInterruptions()", 1)[1].split(
        "\nfunction ", 1
    )[0]

    assert "open-target" not in function_body
    assert "fetch(" not in function_body
    assert "notify(" not in function_body
    assert "send" not in function_body.lower()
