"""Home/admin context in the existing read-only Life workspace."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "lifeWorkspace.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_typed_home_context_is_bounded_and_record_only():
    source = f"""
      import {{ normalizeLifeEntity, normalizeHomeProperties }}
        from {json.dumps(MODULE.as_uri())};
      const entity = normalizeLifeEntity({{
        id: 'home-1', entity_type: 'home_record', title: 'Warranty',
        properties: {{
          home_schema_version: 1, record_type: 'warranty',
          effective_at: '2026-07-01T08:00:00Z', expires_at: '2026-12-31T23:59:00Z',
          due_at: null, source: {{ kind: 'manual', label: 'User entry' }},
          details: {{ record_status: 'active' }},
          references: {{ file_entity_ids: ['file-1'], document_ids: [], entity_ids: [] }},
        }},
      }});
      console.log(JSON.stringify(normalizeHomeProperties(entity)));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source], cwd=ROOT,
        check=True, capture_output=True, text=True, timeout=20,
    )
    assert json.loads(result.stdout) == {
        "recordType": "warranty",
        "effectiveAt": "2026-07-01T08:00:00Z",
        "expiresAt": "2026-12-31T23:59:00Z",
        "dueAt": "",
        "sourceLabel": "User entry",
        "recordStatus": "active",
        "referenceCount": 1,
    }


def test_home_filter_uses_typed_record_and_never_claims_execution():
    module = MODULE.read_text(encoding="utf-8")
    assert "home: Object.freeze(['home_record', 'place'])" in module
    assert "normalizeHomeProperties" in module
    assert "Home administration context" in module
    assert "Record and alert only" in module
    assert "life-home-context" in module
    assert ".innerHTML" not in module
