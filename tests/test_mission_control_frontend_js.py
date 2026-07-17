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


def test_degraded_today_sources_never_render_as_confirmed_zeroes():
    result = _node_eval(
        """
        const sources = {
          calendar: { status: 'error' },
          planning: { status: 'empty' },
          tasks: { status: 'unavailable' },
          health: { status: 'degraded' },
          progression: { status: 'error' },
        };
        console.log(JSON.stringify({
          unavailable: mission.__test.unavailableSourceNames(
            sources,
            ['calendar', 'planning', 'tasks', 'health', 'progression'],
          ),
          failedCalendar: mission.__test.summaryMetricValue(0, 'calendar', sources.calendar),
          emptyPlan: mission.__test.summaryMetricValue(0, 'planning', sources.planning),
          degradedHealth: mission.__test.sourceValueUnavailable('health', sources.health),
        }));
        """
    )

    assert result == {
        "unavailable": ["calendar", "tasks", "progression"],
        "failedCalendar": "Unavailable",
        "emptyPlan": 0,
        "degradedHealth": False,
    }


def test_today_degraded_sections_suppress_affirmative_empty_claims():
    source = MODULE.read_text(encoding="utf-8")

    assert "function degradedSourceState(sourceNames)" in source
    assert "Unavailable sources: ${labels.join(', ')}." in source
    assert "Retry before treating this section as clear." in source
    assert "if (unavailableSources.length) body.appendChild(degradedSourceState(unavailableSources));" in source
    assert "if (!items.length && !unavailableSources.length) body.appendChild(emptyState(emptyMessage));" in source
    assert "currentUnavailableSourceNames(PRIORITY_SOURCE_NAMES)" in source
    assert "currentUnavailableSourceNames(sourceNames)" in source


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


def test_today_renders_the_v3_execution_contract_without_repeating_legacy_panels():
    source = MODULE.read_text(encoding="utf-8")
    css = (ROOT / "static" / "mission-control.css").read_text(encoding="utf-8")

    for key in (
        "primary_outcome",
        "top_three_actions",
        "events",
        "must_do_tasks",
        "people_awaiting_responses",
        "health_routine_commitments",
        "risks_conflicts",
        "suggested_schedule",
        "restia_owned_work",
    ):
        assert key in source
    for label in ("Why now", "If delayed", "Restia can", "Supports", "Sources"):
        assert label in source
    assert "function renderTodayExecution()" in source
    home_render = source[source.index("  renderSummary();"):source.index("async function activateInboxNavigation")]
    assert "...renderTodayExecution(), renderInboxAttention()" in home_render
    for repeated_panel in (
        "renderPlanning()",
        "renderCalendar()",
        "renderProjectWork()",
        "renderImportantMail()",
        "renderTasks()",
    ):
        assert repeated_panel not in home_render
    for selector in (
        ".mission-recommendation",
        ".mission-primary-panel",
        ".mission-recommendation-details",
    ):
        assert selector in css


def test_today_settings_target_uses_a_real_trigger_and_missing_targets_fail():
    result = _node_eval(
        """
        const calls = [];
        globalThis.document = {
          getElementById(id) {
            return id === 'rail-settings' ? { click() { calls.push(id); } } : null;
          },
        };
        console.log(JSON.stringify({
          opened: mission.__test.triggerTarget('settings'),
          missing: mission.__test.triggerTarget('missing-target'),
          calls,
        }));
        """
    )

    assert result == {
        "opened": True,
        "missing": False,
        "calls": ["rail-settings"],
    }
    source = MODULE.read_text(encoding="utf-8")
    assert "settings: ['user-bar-settings', 'rail-settings']" in source
    assert "notify(`Could not open ${target.replace(/[-_]+/g, ' ')}`, true);" in source


def test_inbox_attention_normalization_is_bounded_and_content_free():
    result = _node_eval(
        """
        const items = Array.from({ length: 7 }, (_, index) => ({
          id: `capture-${index}`,
          title: `Capture ${index}`,
          kind: index % 2 ? 'project_information' : 'task',
          confidence: index === 0 ? undefined : index === 1 ? 'unknown' : 72 + index,
          reason: `Reason ${index}`,
          content: `PRIVATE_CONTENT_${index}`,
          metadata: { token: `PRIVATE_METADATA_${index}` },
          source_ref: `PRIVATE_SOURCE_REF_${index}`,
        }));
        console.log(JSON.stringify(mission.__test.normalizeInboxAttention({
          status: 'ok',
          unprocessed_count: 7,
          kinds: { task: 4, project_information: 3, empty: 0 },
          oldest_at: '2026-07-01T00:00:00Z',
          truncated: true,
          items,
          content: 'PRIVATE_SOURCE_CONTENT',
          metadata: { password: 'PRIVATE_SOURCE_METADATA' },
          source_ref: 'PRIVATE_SOURCE_REFERENCE',
        })));
        """
    )

    assert result["unprocessedCount"] == 7
    assert result["oldestAt"] == "2026-07-01T00:00:00Z"
    assert result["truncated"] is True
    assert [(row["kind"], row["count"]) for row in result["kinds"]] == [
        ("task", 4),
        ("project_information", 3),
    ]
    assert len(result["items"]) == 5
    assert set(result["items"][0]) == {
        "title", "kind", "kindLabel", "confidence", "reason"
    }
    assert result["items"][0]["confidence"] is None
    assert result["items"][1]["confidence"] is None
    rendered = json.dumps(result, sort_keys=True)
    for private_value in (
        "PRIVATE_CONTENT",
        "PRIVATE_METADATA",
        "PRIVATE_SOURCE_REF",
        "PRIVATE_SOURCE_CONTENT",
        "PRIVATE_SOURCE_REFERENCE",
    ):
        assert private_value not in rendered


def test_inbox_attention_uses_the_canonical_navigation_activator():
    result = _node_eval(
        """
        const calls = [];
        const opened = await mission.__test.activateInboxNavigation(async (id) => {
          calls.push(id);
          return true;
        });
        const rejected = await mission.__test.activateInboxNavigation(async (id) => {
          calls.push(id);
          return false;
        });
        console.log(JSON.stringify({ opened, rejected, calls }));
        """
    )

    assert result == {
        "opened": True,
        "rejected": False,
        "calls": ["inbox", "inbox"],
    }


def test_today_renders_accessible_inbox_attention_via_canonical_navigation():
    source = MODULE.read_text(encoding="utf-8")
    css = (ROOT / "static" / "mission-control.css").read_text(encoding="utf-8")

    assert "function renderInboxAttention()" in source
    assert "const value = source('inbox');" in source
    assert "safeValue.unprocessed_count ?? safeValue.count" in source
    assert "Inbox classification breakdown" in source
    assert "Oldest Inbox captures awaiting attention" in source
    assert "Oldest capture ${formatDate(inbox.oldestAt, { time: true })}" in source
    assert "...renderTodayExecution(), renderInboxAttention()" in source
    assert "activateInboxNavigation(window.activateNavigationItem)" in source
    assert "return (await handler('inbox')) !== false;" in source
    assert "target: 'inbox', title: 'Open Universal Inbox'" in source
    assert "Confidence unavailable" in source
    assert "window.location" not in source[source.index("function triggerTarget"):source.index("async function loadCurrentView")]
    for selector in (
        ".mission-inbox-attention",
        ".mission-inbox-breakdown",
        ".mission-inbox-previews",
        ".mission-inbox-preview-reason",
        ".mission-inbox-age",
    ):
        assert selector in css
