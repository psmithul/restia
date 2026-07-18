"""Read-only contextual Finance rendering in the existing Life workspace."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "lifeWorkspace.js"


@pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")
def test_typed_finance_context_is_bounded_and_record_only():
    source = f"""
      import {{ normalizeLifeEntity, normalizeFinanceProperties }}
        from {json.dumps(MODULE.as_uri())};
      const entity = normalizeLifeEntity({{
        id: 'finance-1', entity_type: 'finance_record', title: 'Bus ticket',
        properties: {{
          finance_schema_version: 1, record_type: 'expense', scope: 'personal',
          amount: '249.50', currency: 'inr', effective_at: '2026-07-16T19:43:00Z',
          details: {{ merchant: 'Bus operator' }},
          source: {{ kind: 'email', label: 'Receipt email' }},
        }},
      }});
      console.log(JSON.stringify(normalizeFinanceProperties(entity)));
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    finance = json.loads(result.stdout)
    assert finance == {
        "recordType": "expense",
        "scope": "personal",
        "amount": "249.50",
        "currency": "INR",
        "party": "Bus operator",
        "sourceLabel": "Receipt email",
        "effectiveAt": "2026-07-16T19:43:00Z",
    }


def test_money_filter_and_finance_card_share_canonical_authority():
    module = MODULE.read_text(encoding="utf-8")
    assert "money: Object.freeze(['finance_record', 'transaction', 'asset'])" in module
    assert "normalizeFinanceProperties" in module
    assert "Finance record context" in module
    assert "Record analysis only" in module
    assert "life-finance-context" in module
    assert "textContent" in module
    assert ".innerHTML" not in module
