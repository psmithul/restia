"""Focused contracts for the bounded, owner-scoped Life workspace."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "lifeWorkspace.js"


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_life_page_normalization_is_dom_free_and_hard_bounded():
    source = f"""
      import {{ normalizeLifeEntity, unwrapLifePage }} from {json.dumps(MODULE.as_uri())};
      const rows = Array.from({{ length: 60 }}, (_, index) => ({{
        id: String(index), entity_type: index ? 'task' : 'Life Goal',
        title: index ? `Task ${{index}}` : '', confidence: index ? 85.4 : 140,
        properties: index ? {{ effort: 2 }} : null,
      }}));
      const normalized = normalizeLifeEntity(rows[0]);
      const page = unwrapLifePage({{ items: rows, count: 60, truncated: false }});
      console.log(JSON.stringify({{ normalized, length: page.items.length, page }}));
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

    assert payload["normalized"]["type"] == "life_goal"
    assert payload["normalized"]["title"] == "Untitled entity"
    assert payload["normalized"]["confidence"] == 100
    assert payload["normalized"]["properties"] == {}
    assert payload["length"] == 50
    assert payload["page"]["count"] == 60
    assert payload["page"]["truncated"] is True


def test_life_workspace_reads_only_owner_scoped_life_api_and_renders_safely():
    module = _read("static/js/lifeWorkspace.js")

    assert "/api/life/entities?limit=${MAX_ENTITIES}" in module
    assert "const MAX_ENTITIES = 50" in module
    assert "credentials: 'same-origin'" in module
    assert "method: 'GET'" in module
    assert "textContent" in module
    assert ".innerHTML" not in module
    assert "localStorage" not in module
    assert "POST" not in module
    assert "PATCH" not in module
    assert "DELETE" not in module


def test_life_workspace_owns_every_required_contextual_destination():
    html = _read("static/index.html")
    module = _read("static/js/lifeWorkspace.js")

    for key, label in (
        ("goal", "Goals"), ("project", "Projects"), ("people", "People"),
        ("health", "Health"), ("money", "Money"),
        ("learning", "Learning"), ("work", "Work"), ("home", "Home"),
        ("journal", "Journal"), ("files", "Files"),
    ):
        assert f'data-life-filter="{key}"' in html
        assert f">{label} <" in html
        assert f"{key}: Object.freeze(" in module
    assert "money: Object.freeze(['finance_record', 'transaction', 'asset'])" in module


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_automation_context_is_bounded_read_only_and_definition_specific():
    source = f"""
      import {{ normalizeAutomationProperties, __test }} from {json.dumps(MODULE.as_uri())};
      const definition = normalizeAutomationProperties({{
        type: 'automation',
        provenance: {{ source_ids: ['source-1', 'source-2'] }},
        properties: {{
          schema_version: 1,
          record_kind: 'automation_definition',
          enabled: true,
          trigger: {{ type: 'calendar', config: {{ event: 'meeting_ended' }} }},
          actions: [
            {{ type: 'briefing', external: false }},
            {{ type: 'approved_send', external: true }},
          ],
        }},
      }});
      const run = normalizeAutomationProperties({{
        type: 'action',
        properties: {{ schema_version: 1, record_kind: 'automation_preparation' }},
      }});
      console.log(JSON.stringify({{
        definition, run, workTypes: __test.FILTER_TYPES.work,
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

    assert payload["definition"] == {
        "enabled": True,
        "triggerType": "calendar",
        "actionCount": 2,
        "actionTypes": ["briefing", "approved_send"],
        "externalReviewCount": 1,
        "sourceCount": 2,
    }
    assert payload["run"] is None
    assert "automation" in payload["workTypes"]

    module = _read("static/js/lifeWorkspace.js")
    assert "Automation definition context" in module
    assert "Prepared only · no execution or send" in module
    assert "/api/life/automations" not in module


def test_life_workspace_shell_assets_and_accessible_states_are_registered():
    html = _read("static/index.html")
    css = _read("static/life-workspace.css")
    sw = _read("static/sw.js")

    for element_id in (
        "life-workspace", "life-title", "life-refresh", "life-close",
        "life-filters", "life-loading", "life-error", "life-retry",
        "life-empty", "life-list", "life-count", "life-live-region",
    ):
        assert f'id="{element_id}"' in html
    assert 'aria-labelledby="life-title"' in html
    assert 'aria-label="Filter Life entities by type"' in html
    assert ".life-workspace[hidden]" in css
    assert "@media (max-width: 768px)" in css
    assert "prefers-reduced-motion" in css
    assert "/static/life-workspace.css" in sw
    assert "/static/js/lifeWorkspace.js" in sw


def test_life_navigation_route_and_workspace_lifecycle_are_connected():
    app = _read("static/app.js")
    shell = _read("static/js/v2NavigationShell.js")
    registry = _read("static/js/navigation-registry.js")
    module = _read("static/js/lifeWorkspace.js")

    assert "'/life':     () => activateNavigationItem('life')" in app
    assert "lifeWorkspaceModule.init(window.location.origin)" in shell
    assert "lifeWorkspaceModule.open()" in shell
    assert "restia:life-opened" in shell and "restia:life-closed" in shell
    assert "route: '/life'" in registry
    assert "window.inboxModule?.close?.({ restoreFocus: false })" in module
    assert "window.missionControlModule?.close?.()" in module
    assert "window.projectsModule?.isOpen?.()" in module
    assert "window.studyModule?.isActive?.()" in module
