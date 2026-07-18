"""Read-only contextual Decision rendering in the existing Life workspace."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "lifeWorkspace.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_typed_decision_context_is_bounded_and_marks_due_and_stale_assumptions():
    source = f"""
      import {{ normalizeLifeEntity, normalizeDecisionProperties, isFocusableLifeEntity }}
        from {json.dumps(MODULE.as_uri())};
      const raw = {{
        id: 'decision-1', entity_type: 'decision', title: 'Choose architecture',
        occurred_at: '2026-05-01T00:00:00Z', review_at: '2026-07-01T00:00:00Z',
        version: 3,
        properties: {{
          decision_schema_version: 1,
          options: [
            {{ id: 'single', label: 'Single authority' }},
            {{ id: 'split', label: 'Split stores' }},
          ],
          chosen_option: 'single', reasons: ['Avoid drift'], risks: ['Migration'],
          people: ['Mits'], evidence: [{{ label: 'Memo' }}],
          assumptions: [
            {{ id: 'due', text: 'Due check', status: 'unverified', review_at: '2026-07-10T00:00:00Z' }},
            {{ id: 'stale', text: 'Old check', status: 'valid' }},
          ],
          outcome: {{ status: 'pending', summary: '' }},
        }},
      }};
      const entity = normalizeLifeEntity(raw);
      const decision = normalizeDecisionProperties(entity, new Date('2026-07-17T00:00:00Z'));
      console.log(JSON.stringify({{ decision, focusable: isFocusableLifeEntity(entity) }}));
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
    decision = payload["decision"]
    assert decision["chosenLabel"] == "Single authority"
    assert decision["reviewLabel"] == "Review due"
    assert decision["dueAssumptionCount"] == 1
    assert decision["staleAssumptionCount"] == 1
    assert decision["evidenceCount"] == 1
    assert payload["focusable"] is False


def test_decision_ui_is_contextual_read_only_semantic_and_focus_safe():
    module = MODULE.read_text(encoding="utf-8")
    css = (ROOT / "static" / "life-workspace.css").read_text(encoding="utf-8")

    assert "normalizeDecisionProperties" in module
    assert "life-decision-context" in module
    assert "make('dl'" in module and "make('dt'" in module and "make('dd'" in module
    assert "Review due" in module and "Assumptions stale" in module
    assert "method: 'POST'" not in module
    assert "method: 'PATCH'" not in module
    assert "localStorage" not in module
    assert "isFocusableLifeEntity" in module
    assert "data-life-focus-id" in module
    assert "life-entity-actions" in module
    assert ".life-decision-row dd" in css
    assert "overflow-wrap: anywhere" in css
    assert "@media (max-width: 480px)" in css
