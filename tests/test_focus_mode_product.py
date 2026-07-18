"""Product contracts connecting V3 Focus Mode to Today and Life."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path

import pytest

import routes.mission_control_routes as mission


ROOT = Path(__file__).resolve().parents[1]
MISSION_MODULE = ROOT / "static" / "js" / "missionControl.js"
LIFE_MODULE = ROOT / "static" / "js" / "lifeWorkspace.js"


def _source(items):
    return {
        "status": "ok",
        "items": items,
        "count": len(items),
        "truncated": False,
    }


def test_today_actual_recommendations_carry_canonical_focus_targets():
    planning = [{
        "id": "plan-1",
        "title": "Ship Focus Mode",
        "details": "",
        "status": "open",
        "priority": "high",
        "due_date": "2026-07-17",
        "due_today": True,
        "overdue": False,
        "version": 7,
    }]
    sources = {
        "calendar": _source([]),
        "project_work": _source([]),
        "planning": _source(planning),
        "goals": _source([]),
        "tasks": _source([]),
        "study_reviews": _source([]),
        "important_mail": _source([]),
        "notes_today": _source([]),
        "health": {"status": "ok", "overall": "ok", "services": []},
    }
    actions = mission._build_next_actions(sources)
    sections = mission._build_today_sections(
        sources,
        next_actions=actions,
        local_date=date(2026, 7, 17),
        local_now=datetime(2026, 7, 17, 9, 0),
        utc_offset_minutes=330,
    )

    expected = {"kind": "planning_item", "id": "plan-1", "version": 7}
    assert sections["primary_outcome"]["focus_target"] == expected
    assert sections["top_three_actions"][0]["focus_target"] == expected
    assert sections["must_do_tasks"][0]["focus_target"] == expected
    assert sections["suggested_schedule"][0]["focus_target"] == expected


def test_non_versioned_or_non_actionable_today_rows_are_not_focusable():
    assert mission._planning_focus_target({
        "id": "plan", "status": "open", "version": 0,
    }) is None
    assert mission._planning_focus_target({
        "id": "plan", "status": "completed", "version": 2,
    }) is None


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_focus_timer_and_target_normalization_are_dom_free_and_recoverable():
    source = f"""
      import mission from {json.dumps(MISSION_MODULE.as_uri())};
      const active = mission.__test.normalizeFocusSession({{
        id: 'focus-1', state: 'active', elapsed_seconds: 120, version: 3,
        entity: {{ id: 'entity-1', entity_type: 'task', title: 'Test', version: 2 }},
      }}, 1000);
      const paused = {{ ...active, state: 'paused' }};
      console.log(JSON.stringify({{
        active: mission.__test.focusDisplayElapsed(active, 6100),
        paused: mission.__test.focusDisplayElapsed(paused, 6100),
        direct: mission.__test.normalizeFocusTarget({{ kind: 'life_entity', id: 'e', version: 2 }}),
        canonical: mission.__test.normalizeFocusTarget({{ kind: 'planning_item', id: 'p', version: 4 }}),
        invalid: mission.__test.normalizeFocusTarget({{ kind: 'calendar', id: 'c', version: 1 }}),
      }}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    payload = json.loads(result.stdout)
    assert payload == {
        "active": 125,
        "paused": 120,
        "direct": {"kind": "life_entity", "id": "e", "version": 2},
        "canonical": {"kind": "planning_item", "id": "p", "version": 4},
        "invalid": None,
    }


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_life_focusability_matches_backend_contract():
    source = f"""
      import life from {json.dumps(LIFE_MODULE.as_uri())};
      console.log(JSON.stringify({{
        task: life.__test.isFocusableLifeEntity({{ id: '1', type: 'task', status: 'open', version: 1 }}),
        milestone: life.__test.isFocusableLifeEntity({{ id: '2', type: 'milestone', status: 'in_progress', version: 2 }}),
        note: life.__test.isFocusableLifeEntity({{ id: '3', type: 'note', status: 'active', version: 1 }}),
        completed: life.__test.isFocusableLifeEntity({{ id: '4', type: 'action', status: 'completed', version: 1 }}),
      }}));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert json.loads(result.stdout) == {
        "task": True,
        "milestone": True,
        "note": False,
        "completed": False,
    }


def test_focus_surface_is_today_integrated_accessible_and_server_authoritative():
    mission_js = MISSION_MODULE.read_text(encoding="utf-8")
    life_js = LIFE_MODULE.read_text(encoding="utf-8")
    css = (ROOT / "static" / "mission-control.css").read_text(encoding="utf-8")

    assert "recommendationCard(item" in mission_js
    assert "focus-prepare" in mission_js
    assert "focusRecommendationId" in mission_js
    assert "/api/life/focus/current" not in mission_js  # constructed from the canonical prefix
    assert "focusRequest('/current', { method: 'GET'" in mission_js
    for action in ("pause", "resume", "interruptions", "progress", "evidence", "complete", "abandon"):
        assert action in mission_js
    assert "visibilitychange" in mission_js and "window.addEventListener('online'" in mission_js
    assert "data-focus-elapsed" in mission_js
    assert "Definition of done" in mission_js
    assert "Linked context" in mission_js
    assert "Follow-up actions created" in mission_js
    assert "Return to Today" in mission_js and "Return to Focus" in mission_js
    assert "localStorage" not in mission_js and "sessionStorage" not in mission_js
    assert "window.missionControlModule" in life_js and "prepareFocus" in life_js
    assert "data-life-focus-id" in life_js
    assert "POST" not in life_js

    for selector in (
        ".mission-focus-host", ".mission-focus-clock", ".mission-focus-layout",
        ".mission-focus-context-list", ".mission-focus-capture-form",
    ):
        assert selector in css
    assert "font-variant-numeric: tabular-nums" in css
    assert "min-height: 44px" in css
    assert "@media (max-width: 768px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
