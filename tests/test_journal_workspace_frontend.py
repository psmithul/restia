"""Private Journal context in the existing read-only Life workspace."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "lifeWorkspace.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_typed_journal_context_is_bounded_and_explicit_only():
    source = f"""
      import {{ normalizeLifeEntity, normalizeJournalProperties }}
        from {json.dumps(MODULE.as_uri())};
      const entity = normalizeLifeEntity({{
        id: 'journal-1', entity_type: 'journal_entry', title: 'Reflection',
        properties: {{
          journal_schema_version: 1, entry_date: '2026-07-17',
          mood: {{ label: 'focused', score: 8, energy: 7 }},
          wins: ['Finished model'], lessons: ['Protect deep work'],
          promises: [{{ status: 'open' }}, {{ status: 'kept' }}],
          next_changes: ['Move phone'],
        }},
      }});
      console.log(JSON.stringify(normalizeJournalProperties(entity)));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source], cwd=ROOT,
        check=True, capture_output=True, text=True, timeout=20,
    )
    assert json.loads(result.stdout) == {
        "entryDate": "2026-07-17",
        "moodLabel": "focused",
        "moodScore": 8,
        "energy": 7,
        "winCount": 1,
        "lessonCount": 1,
        "openPromiseCount": 1,
        "nextChangeCount": 1,
    }


def test_journal_filter_uses_typed_entry_and_review_claim_is_bounded():
    module = MODULE.read_text(encoding="utf-8")
    assert "journal: Object.freeze(['journal_entry', 'period_review', 'note'])" in module
    assert "normalizeJournalProperties" in module
    assert "Journal entry context" in module
    assert "Explicit evidence only" in module
    assert "life-journal-context" in module
    assert ".innerHTML" not in module
