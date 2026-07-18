"""Work/Business context in the existing read-only Life workspace."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "lifeWorkspace.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_typed_business_record_keeps_exact_workspace_context():
    source = f"""
      import {{ normalizeLifeEntity, normalizeWorkBusinessProperties }}
        from {json.dumps(MODULE.as_uri())};
      const entity = normalizeLifeEntity({{
        id: 'revenue-1', entity_type: 'transaction', title: 'Pilot revenue',
        properties: {{
          work_business_record_schema_version: 1, workspace_id: 'business-1',
          workspace_kind: 'business', record_kind: 'revenue',
          details: {{ category: 'pilot' }},
          source_links: [{{ source_id: 'source-1', relation: 'supports' }}],
        }},
      }});
      console.log(JSON.stringify(normalizeWorkBusinessProperties(entity)));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source], cwd=ROOT,
        check=True, capture_output=True, text=True, timeout=20,
    )
    assert json.loads(result.stdout) == {
        "isWorkspace": False,
        "workspaceKind": "business",
        "recordKind": "revenue",
        "workspaceId": "business-1",
        "purpose": "",
        "descriptor": "pilot",
        "sourceCount": 1,
    }


def test_work_filter_includes_typed_records_without_new_navigation():
    module = MODULE.read_text(encoding="utf-8")
    assert "normalizeWorkBusinessProperties" in module
    assert "filter === 'work' && normalizeWorkBusinessProperties(item)" in module
    assert "Work and business workspace context" in module
    assert "Record only · no outreach, submission, or payment" in module
    assert "life-work-context" in module
    assert "business: Object.freeze" not in module
    assert ".innerHTML" not in module
