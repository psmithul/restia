"""Node-level contracts for Mission Control's safe frontend normalization."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "missionControl.js"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _node_eval(body: str):
    source = f"""
      import mission from {json.dumps(MODULE.as_uri())};
      {body}
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def test_backend_next_actions_are_bounded_and_toned():
    result = _node_eval(
        """
        console.log(JSON.stringify(mission.__test.mapBackendActions([
          { title: 'Repair task', detail: 'Failed', target: 'tasks', urgency: 'critical' },
          { title: 'Reply', detail: 'Mail', target: 'email', urgency: 'attention' },
          { title: 'Review', detail: 'Study', target: 'study', urgency: 'normal' },
          { title: 'Never rendered', target: 'projects', urgency: 'critical' },
        ])));
        """
    )

    assert len(result) == 3
    assert [row["tone"] for row in result] == ["danger", "attention", ""]
    assert [row["target"] for row in result] == ["tasks", "email", "study"]


def test_health_loaded_states_are_not_rendered_as_source_failures():
    result = _node_eval(
        """
        console.log(JSON.stringify(Object.fromEntries(
          ['ok', 'healthy', 'degraded', 'down', 'empty', 'error', 'unavailable', 'unknown']
            .map(status => [status, mission.__test.healthUnavailable({ status })])
        )));
        """
    )

    assert result == {
        "ok": False,
        "healthy": False,
        "degraded": False,
        "down": False,
        "empty": False,
        "error": True,
        "unavailable": True,
        "unknown": True,
    }


def test_mission_control_renders_new_v2_sources_without_html_injection():
    source = MODULE.read_text(encoding="utf-8")
    css = (ROOT / "static" / "mission-control.css").read_text(encoding="utf-8")

    for key in ("important_mail", "notes_today", "daily_brief"):
        assert key in source
    assert "state.data?.next_actions" in source
    assert "element.textContent" in source
    assert ".innerHTML" not in source
    assert ".mission-topbar-actions .mission-btn:first-child { display: none; }" not in css


def test_mission_control_bounds_long_item_metadata_in_narrow_cards():
    css = (ROOT / "static" / "mission-control.css").read_text(encoding="utf-8")
    source = MODULE.read_text(encoding="utf-8")

    assert ".mission-item > small" in css
    assert "text-overflow: ellipsis" in css
    assert "white-space: nowrap" in css
    assert "attrs: { title: meta }" in source
