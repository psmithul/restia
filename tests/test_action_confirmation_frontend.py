"""Safety contracts for Today action review and confirmation flows."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "missionControl.js"
CSS = ROOT / "static" / "mission-control.css"
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _node_eval(body: str):
    source = f"""
      import mission from {json.dumps(MODULE.as_uri())};
      {body}
    """
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return json.loads(result.stdout)


def test_action_normalization_is_bounded_explicit_and_secret_free():
    result = _node_eval(
        """
        const action = mission.__test.normalizeActionProposal({
          id: 'proposal-1', domain: 'calendar', action: 'cancel_event',
          autonomy_level: 5, state: 'prepared', target_type: 'event',
          target_id: 'event-uid', version: 7, requires_confirmation: true,
          reviewed_server_executor: true, reviewed_server_reversal: false,
          reason: 'The source email cancelled the appointment.',
          payload: { expected_event_version: 3, access_token: 'PAYLOAD_SECRET' },
          sources: { message_id: 'mail-42', password: 'SOURCE_SECRET' },
          result: { status: 'waiting', confirmation_digest: 'RESULT_SECRET' },
          confirmation_digest: 'TOP_SECRET', confirmation_token: 'TOP_TOKEN',
        });
        console.log(JSON.stringify(action));
        """
    )

    assert result["riskLabel"] == "External or consequential"
    assert result["reviewedExecutor"] is True
    assert result["changes"] == [
        {"label": "Operation", "value": "Cancel this calendar event"},
        {"label": "Event UID", "value": "event-uid"},
        {"label": "Expected event version", "value": "3"},
    ]
    assert result["evidence"] == [{"label": "Message Id", "value": "mail-42"}]
    rendered = json.dumps(result, sort_keys=True)
    for secret in ("PAYLOAD_SECRET", "SOURCE_SECRET", "RESULT_SECRET", "TOP_SECRET", "TOP_TOKEN"):
        assert secret not in rendered


def test_cancel_approval_executes_with_fresh_confirmation_and_current_versions():
    result = _node_eval(
        """
        const calls = [];
        const base = {
          id: 'cancel-1', domain: 'calendar', action: 'cancel_event',
          autonomy_level: 5, state: 'prepared', target_type: 'event',
          target_id: 'uid-1', payload: { expected_event_version: 2 },
          requires_confirmation: true, reviewed_server_executor: true,
          reviewed_server_reversal: false, version: 4,
        };
        const request = async (path, options) => {
          calls.push({ path, body: options.body });
          if (path.endsWith('/confirmation')) return {
            action: { ...base, version: 5 }, confirmation_token: 'rac_single_use',
          };
          if (path.endsWith('/approve')) return { action: { ...base, state: 'approved', version: 6 } };
          return { action: {
            ...base, state: 'completed', version: 8,
            reviewed_server_reversal: true, result: { event_version: 3 },
          } };
        };
        // This is the real card-click path: list responses are normalized for
        // rendering, then the handler passes that bounded object back through
        // the execution boundary.
        const normalized = mission.__test.normalizeActionProposal(base);
        const completed = await mission.__test.executeReviewedAction(normalized, request);
        let genericError = '';
        try {
          await mission.__test.executeReviewedAction(mission.__test.normalizeActionProposal({
            ...base, id: 'generic', domain: 'email', action: 'send_email',
            target_type: 'message', reviewed_server_executor: false,
          }), request);
        } catch (error) { genericError = error.message; }
        console.log(JSON.stringify({ calls, completed, genericError }));
        """
    )

    assert result["calls"] == [
        {"path": "/actions/cancel-1/confirmation", "body": {"version": 4, "purpose": "approve"}},
        {"path": "/actions/cancel-1/approve", "body": {"version": 5, "confirmation_token": "rac_single_use"}},
        {"path": "/actions/cancel-1/execute", "body": {"version": 6}},
    ]
    assert result["completed"]["state"] == "completed"
    assert "rac_single_use" not in json.dumps(result["completed"])
    assert result["genericError"] == "Action has no reviewed server executor."


def test_high_risk_reversal_always_uses_a_fresh_confirmation():
    result = _node_eval(
        """
        const calls = [];
        const completed = {
          id: 'cancel-2', domain: 'calendar', action: 'cancel_event',
          autonomy_level: 5, state: 'completed', target_type: 'event',
          target_id: 'uid-2', payload: { expected_event_version: 3 },
          requires_confirmation: true, reviewed_server_executor: true,
          reviewed_server_reversal: true, version: 9,
        };
        const request = async (path, options) => {
          calls.push({ path, body: options.body });
          if (path.endsWith('/confirmation')) return {
            action: { ...completed, version: 10 }, confirmation_token: 'rac_reverse_once',
          };
          return { action: { ...completed, state: 'reversed', version: 12 } };
        };
        const reversed = await mission.__test.reverseReviewedAction(
          mission.__test.normalizeActionProposal(completed), request,
        );
        let unfenced = '';
        try {
          await mission.__test.reverseReviewedAction({ ...completed, requires_confirmation: false }, request);
        } catch (error) { unfenced = error.message; }
        console.log(JSON.stringify({ calls, reversed, unfenced }));
        """
    )

    assert result["calls"] == [
        {"path": "/actions/cancel-2/confirmation", "body": {"version": 9, "purpose": "reverse"}},
        {"path": "/actions/cancel-2/reverse", "body": {"version": 10, "confirmation_token": "rac_reverse_once"}},
    ]
    assert result["reversed"]["state"] == "reversed"
    assert "missing its required confirmation fence" in result["unfenced"]


def test_today_action_center_dom_accessibility_mobile_and_recovery_contracts():
    source = MODULE.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")

    for phrase in (
        "Action review", "Review action", "Approve & execute", "Reject",
        "Exactly what will change", "Source evidence", "Recent verified outcomes",
        "No reviewed server executor exists", "Review reversal", "Confirm & reverse",
    ):
        assert phrase in source
    assert "renderActionCenter(), ...renderTodayExecution()" in source
    assert "reviewed_server_executor" in source and "reviewed_server_reversal" in source
    assert "aria-expanded" in source and "aria-controls" in source
    assert "'aria-live': 'polite'" in source and "'aria-busy'" in source
    assert "Action changed or expired. The queue was refreshed" in source
    assert "Promise.allSettled" in source
    assert "localStorage" not in source and "sessionStorage" not in source

    for selector in (
        ".mission-action-center", ".mission-action-card", ".mission-action-review",
        ".mission-action-controls", ".mission-action-error", ".mission-action-risk-high",
    ):
        assert selector in css
    assert ".mission-workspace :focus-visible" in css
    assert "min-height: 44px" in css
    assert "gap: 8px" in css
    assert "overflow-wrap: anywhere" in css
    assert "@media (max-width: 768px)" in css
    mobile = css[css.index("@media (max-width: 768px)"):]
    assert ".mission-action-list" in mobile and "grid-template-columns: 1fr" in mobile
    assert "flex-direction: column" in mobile
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert ".mission-action-outcome { transition: none; }" in css
