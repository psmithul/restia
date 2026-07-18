"""Read-only contextual Habit rendering in the existing Life workspace."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "lifeWorkspace.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_typed_habit_context_is_bounded_and_timezone_explicit():
    source = f"""
      import {{ normalizeLifeEntity, normalizeHabitProperties }}
        from {json.dumps(MODULE.as_uri())};
      const entity = normalizeLifeEntity({{
        id: 'habit-1', entity_type: 'habit', title: 'Morning reset',
        properties: {{
          habit_schema_version: 1, routine_type: 'morning', duration_minutes: 10,
          schedule: {{ cadence: 'daily', time_of_day: '07:00', timezone: 'Asia/Kolkata' }},
          checklist: [
            {{ id: 'water', label: 'Drink water', required: true }},
            {{ id: 'stretch', label: 'Stretch', required: false }},
          ],
          minimum_viable: {{ duration_minutes: 3 }},
          recovery_rules: {{ strategy: 'next_available' }},
        }},
      }});
      console.log(JSON.stringify(normalizeHabitProperties(entity)));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert json.loads(result.stdout) == {
        "routineType": "morning",
        "cadence": "daily",
        "timeOfDay": "07:00",
        "timezone": "Asia/Kolkata",
        "durationMinutes": 10,
        "checklistCount": 2,
        "requiredCount": 1,
        "minimumMinutes": 3,
        "recoveryStrategy": "next_available",
    }


def test_health_filter_and_habit_card_share_canonical_authority():
    module = MODULE.read_text(encoding="utf-8")
    assert "health: Object.freeze(['health_record', 'habit', 'metric'])" in module
    assert "normalizeHabitProperties" in module
    assert "Habit routine context" in module
    assert "Tracked evidence only" in module
    assert "life-habit-context" in module
    assert "textContent" in module
    assert ".innerHTML" not in module
