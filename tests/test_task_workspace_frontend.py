"""Structured human Task context in the existing read-only Life workspace."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "lifeWorkspace.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_typed_task_context_keeps_human_commitment_fields():
    source = f"""
      import {{ normalizeLifeEntity, normalizeTaskProperties }}
        from {json.dumps(MODULE.as_uri())};
      const entity = normalizeLifeEntity({{
        id: 'task-1', entity_type: 'task', title: 'Verify release',
        properties: {{
          task_schema_version: 1, definition_of_done: 'All gates pass',
          priority: 'critical', effort_minutes: 90, energy: 'high',
          contexts: ['laptop', 'lab'], project_id: 'project-1',
          people_ids: ['person-1'], dependency_ids: ['task-0'],
          document_ids: ['file-1'], next_action: 'Run migration smoke',
          waiting_on: '', completion_evidence: [{{ note: 'Focused tests green' }}],
        }},
      }});
      console.log(JSON.stringify(normalizeTaskProperties(entity)));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source], cwd=ROOT,
        check=True, capture_output=True, text=True, timeout=20,
    )
    assert json.loads(result.stdout) == {
        "definitionOfDone": "All gates pass",
        "priority": "critical",
        "effortMinutes": 90,
        "energy": "high",
        "contexts": ["laptop", "lab"],
        "nextAction": "Run migration smoke",
        "waitingOn": "",
        "referenceCount": 4,
        "evidenceCount": 1,
    }


def test_task_card_never_conflates_human_tasks_with_scheduler():
    module = MODULE.read_text(encoding="utf-8")
    assert "normalizeTaskProperties" in module
    assert "Structured task context" in module
    assert "Human commitment · not Scheduled Tasks" in module
    assert "life-task-context" in module
    assert ".innerHTML" not in module
