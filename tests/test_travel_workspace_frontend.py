"""Travel context in the existing read-only Life workspace."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "lifeWorkspace.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_typed_travel_context_is_bounded_offline_and_record_only():
    source = f"""
      import {{ normalizeLifeEntity, normalizeTravelProperties }}
        from {json.dumps(MODULE.as_uri())};
      const entity = normalizeLifeEntity({{
        id: 'trip-1', entity_type: 'trip', title: 'Tokyo workshop',
        properties: {{
          travel_schema_version: 1, record_kind: 'trip', trip_id: null,
          starts_at: '2026-07-20T00:00:00Z', ends_at: '2026-07-24T09:00:00Z',
          offline_available: true,
          details: {{ destination: 'Tokyo', trip_timezone: 'Asia/Tokyo' }},
          related_entity_ids: ['project-1'], source_ids: ['source-1'],
          calendar_event_ids: ['event-1'],
        }},
      }});
      console.log(JSON.stringify(normalizeTravelProperties(entity)));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source], cwd=ROOT,
        check=True, capture_output=True, text=True, timeout=20,
    )
    assert json.loads(result.stdout) == {
        "recordKind": "trip",
        "tripId": "",
        "startsAt": "2026-07-20T00:00:00Z",
        "endsAt": "2026-07-24T09:00:00Z",
        "offlineAvailable": True,
        "descriptor": "Tokyo",
        "referenceCount": 3,
    }


def test_travel_card_does_not_add_sidebar_or_claim_execution():
    module = MODULE.read_text(encoding="utf-8")
    assert "normalizeTravelProperties" in module
    assert "Travel record context" in module
    assert "Record only · no booking or purchase" in module
    assert "life-travel-context" in module
    assert "travel: Object.freeze" not in module
    assert ".innerHTML" not in module
