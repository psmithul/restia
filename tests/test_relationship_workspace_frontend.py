"""Read-only relationship context in the existing Life workspace."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "lifeWorkspace.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_typed_relationship_profile_context_is_bounded_and_non_sending():
    source = f"""
      import {{ normalizeLifeEntity, normalizeRelationshipProperties }}
        from {json.dumps(MODULE.as_uri())};
      const entity = normalizeLifeEntity({{
        id: 'person-1', entity_type: 'person', title: 'Alex Rivera',
        properties: {{
          relationship_schema_version: 1, relationship_record_kind: 'profile',
          subject_kind: 'person', relationship_type: 'friend',
          contact_origin: {{ kind: 'manual', label: 'User statement' }},
          important_dates: [{{ label: 'Birthday', date: '1999-08-12' }}],
          preferences: [{{ key: 'coffee', value: 'Filter coffee' }}],
          care_plan: {{ next_due_at: '2026-07-20T09:00:00Z', interval_days: 30 }},
        }},
      }});
      console.log(JSON.stringify(normalizeRelationshipProperties(entity)));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source], cwd=ROOT,
        check=True, capture_output=True, text=True, timeout=20,
    )
    assert json.loads(result.stdout) == {
        "subjectKind": "person",
        "relationshipType": "friend",
        "originLabel": "User statement",
        "importantDateCount": 1,
        "preferenceCount": 1,
        "careDueAt": "2026-07-20T09:00:00Z",
        "careIntervalDays": 30,
    }


def test_people_filter_uses_typed_profile_and_card_has_no_send_control():
    module = MODULE.read_text(encoding="utf-8")
    assert "people: Object.freeze(['person', 'interaction', 'commitment'" in module
    assert "normalizeRelationshipProperties" in module
    assert "Relationship profile context" in module
    assert "Separate confirmation required" in module
    assert "life-relationship-context" in module
    assert ".innerHTML" not in module
