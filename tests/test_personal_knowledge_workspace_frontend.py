"""Personal knowledge context in the existing read-only Life workspace."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "lifeWorkspace.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_typed_personal_knowledge_context_preserves_epistemic_labels():
    source = f"""
      import {{ normalizeLifeEntity, normalizePersonalKnowledgeProperties }}
        from {json.dumps(MODULE.as_uri())};
      const entity = normalizeLifeEntity({{
        id: 'memory-1', entity_type: 'source', title: 'Damping claim',
        memory_kind: 'semantic', epistemic_status: 'assumption',
        effective_epistemic_status: 'stale', claim_origin: 'model',
        stale_reason: 'stale_after_elapsed',
        citations: [
          {{ relation: 'supports', available: true }},
          {{ relation: 'contradicts', available: true }},
        ],
        properties: {{
          personal_knowledge_schema_version: 1,
          authority: 'personal_knowledge_v1', memory_kind: 'semantic',
          epistemic_status: 'assumption', claim_origin: 'model',
          tags: ['controls', 'review'],
        }},
      }});
      console.log(JSON.stringify(normalizePersonalKnowledgeProperties(entity)));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source], cwd=ROOT,
        check=True, capture_output=True, text=True, timeout=20,
    )
    assert json.loads(result.stdout) == {
        "memoryKind": "semantic",
        "epistemicStatus": "assumption",
        "effectiveStatus": "stale",
        "claimOrigin": "model",
        "citationCount": 2,
        "availableCitationCount": 2,
        "contradictionCount": 1,
        "tagCount": 2,
        "staleReason": "stale_after_elapsed",
    }


def test_personal_knowledge_uses_typed_read_api_and_safe_card_policy():
    module = MODULE.read_text(encoding="utf-8")
    assert "/api/life/knowledge/records?limit=${MAX_ENTITIES}" in module
    assert "Promise.all" in module
    assert "Personal knowledge context" in module
    assert "Citation-backed · no inferred fact promotion" in module
    assert "life-knowledge-context" in module
    assert ".innerHTML" not in module
