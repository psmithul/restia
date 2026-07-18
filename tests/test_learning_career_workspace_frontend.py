"""Learning/Career context in the existing read-only Life workspace."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "lifeWorkspace.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_typed_learning_context_is_source_backed_and_bounded():
    source = f"""
      import {{ normalizeLifeEntity, normalizeLearningCareerProperties }}
        from {json.dumps(MODULE.as_uri())};
      const entity = normalizeLifeEntity({{
        id: 'practice-1', entity_type: 'learning_record', title: 'Practice',
        properties: {{
          learning_career_schema_version: 1, domain: 'learning',
          record_kind: 'practice', details: {{ method: 'bench experiment' }},
          source_links: [{{ source_id: 'source-1', relation: 'supports' }}],
          entity_links: [{{ entity_id: 'portfolio-1', relation: 'related_to' }}],
          weekly_action: {{ week_start: '2026-07-20', status: 'planned',
            estimated_minutes: 120 }},
        }},
      }});
      console.log(JSON.stringify(normalizeLearningCareerProperties(entity)));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source], cwd=ROOT,
        check=True, capture_output=True, text=True, timeout=20,
    )
    assert json.loads(result.stdout) == {
        "domain": "learning",
        "recordKind": "practice",
        "descriptor": "bench experiment",
        "sourceCount": 1,
        "linkCount": 1,
        "weekStart": "2026-07-20",
        "weeklyStatus": "planned",
        "weeklyMinutes": 120,
    }


def test_learning_filter_and_card_share_existing_navigation():
    module = MODULE.read_text(encoding="utf-8")
    assert "learning: Object.freeze(['learning_record', 'career_item'])" in module
    assert "normalizeLearningCareerProperties" in module
    assert "Learning and career context" in module
    assert "Record and plan only · no submission" in module
    assert "life-learning-context" in module
    assert ".innerHTML" not in module
