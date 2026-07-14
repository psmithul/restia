"""Execute Study Mode's pure clock/progress helpers in the real JS module."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _node_eval(source: str):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(result.stdout)


def test_duration_clock_and_running_progress_are_exact():
    values = _node_eval(
        """
        import { formatStudyDuration, computeLiveProgress } from './static/js/study.js';
        const source = {
          timer_running: true,
          timer_seconds: 10,
          total_seconds: 50,
          target_minutes: 2
        };
        const live = computeLiveProgress(source, 20);
        console.log(JSON.stringify({
          clock: formatStudyDuration(3661),
          timer: live.timer_seconds,
          studied: live.studied_seconds,
          remaining: live.remaining_seconds,
          percent: live.progress_percent,
          sourceTimer: source.timer_seconds
        }));
        """
    )

    assert values == {
        "clock": "01:01:01",
        "timer": 30,
        "studied": 80,
        "remaining": 40,
        "percent": 66.7,
        "sourceTimer": 10,
    }


def test_progress_clamps_and_paused_timer_does_not_tick():
    values = _node_eval(
        """
        import { formatStudyDuration, computeLiveProgress } from './static/js/study.js';
        const paused = computeLiveProgress({
          timer_running: false,
          timer_seconds: 15,
          total_seconds: 100,
          target_minutes: 1
        }, 999);
        const empty = computeLiveProgress({
          timer_running: true,
          timer_seconds: -20,
          total_seconds: -30,
          target_minutes: 0
        }, -10);
        console.log(JSON.stringify({
          pausedTimer: paused.timer_seconds,
          pausedRemaining: paused.remaining_seconds,
          pausedPercent: paused.progress_percent,
          emptyTimer: empty.timer_seconds,
          emptyStudied: empty.studied_seconds,
          emptyPercent: empty.progress_percent,
          negativeClock: formatStudyDuration(-10)
        }));
        """
    )

    assert values == {
        "pausedTimer": 15,
        "pausedRemaining": 0,
        "pausedPercent": 100,
        "emptyTimer": 0,
        "emptyStudied": 0,
        "emptyPercent": 0,
        "negativeClock": "00:00:00",
    }
