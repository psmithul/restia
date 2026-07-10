"""Regression coverage for notification popover placement on either rail side."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "notificationPanelPosition.js"

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _position(anchor_rect, viewport_width=1280, panel_width=360):
    script = f"""
    import {{ calculateNotificationPanelHorizontalPosition }} from '{_HELPER.as_posix()}';
    console.log(JSON.stringify(calculateNotificationPanelHorizontalPosition(
      {json.dumps(anchor_rect)}, {viewport_width}, {panel_width}
    )));
    """
    result = subprocess.run(
        ["node", "--input-type=module"],
        input=script,
        capture_output=True,
        text=True,
        cwd=_REPO,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_notification_panel_opens_left_of_a_right_hand_rail():
    position = _position({"left": 1238, "right": 1272})

    assert position == {"left": "auto", "right": "50px"}


def test_notification_panel_stays_inside_a_narrow_viewport():
    position = _position({"left": 600, "right": 634}, viewport_width=641, panel_width=625)

    assert position == {"left": "auto", "right": "8px"}
