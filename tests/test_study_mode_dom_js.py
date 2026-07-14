"""Exercise Study Mode's browser lifecycle in Node with a minimal DOM."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def test_start_close_race_goal_guard_and_drawer_clearance():
    script = r"""
      class FakeClassList {
        constructor() { this.values = new Set(); }
        add(...names) { names.forEach(name => this.values.add(name)); }
        remove(...names) { names.forEach(name => this.values.delete(name)); }
        contains(name) { return this.values.has(name); }
        toggle(name, force) {
          const enabled = force === undefined ? !this.values.has(name) : Boolean(force);
          if (enabled) this.values.add(name); else this.values.delete(name);
          return enabled;
        }
      }

      class FakeStyle {
        constructor() { this.values = new Map(); }
        setProperty(name, value) { this.values.set(name, String(value)); }
        removeProperty(name) { this.values.delete(name); }
        getPropertyValue(name) { return this.values.get(name) || ''; }
      }

      class FakeElement extends EventTarget {
        constructor(id = '') {
          super();
          this.id = id;
          this.attributes = new Map();
          this.classList = new FakeClassList();
          this.style = new FakeStyle();
          this.textContent = '';
          this.value = '';
          this.disabled = false;
          this.hidden = false;
          this.title = '';
          this.placeholder = '';
          this.firstElementChild = null;
        }
        setAttribute(name, value) { this.attributes.set(name, String(value)); }
        getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
        removeAttribute(name) { this.attributes.delete(name); }
        querySelector() { return null; }
        focus() { this.focused = true; }
        click() { this.dispatchEvent(new Event('click')); }
      }

      const ids = [
        'study-panel', 'study-panel-body', 'study-panel-title',
        'study-panel-collapse', 'study-panel-close', 'study-timer',
        'study-timer-status', 'study-timer-start', 'study-timer-pause',
        'study-timer-finish', 'study-session-total', 'study-goal-form',
        'study-goal-text', 'study-target-hours', 'study-target-date',
        'study-goal-save', 'study-progress-bar', 'study-progress-value',
        'study-progress-copy', 'study-deadline-copy', 'study-error',
        'study-live-status', 'study-method', 'tool-study-btn', 'rail-study',
        'message', 'welcome-screen', 'chat-container', 'chat-history',
      ];
      const elements = new Map(ids.map(id => [id, new FakeElement(id)]));
      elements.get('study-progress-bar').firstElementChild = new FakeElement('progress-fill');

      const composerBar = new FakeElement('composer-bar');
      composerBar.getBoundingClientRect = () => ({ top: 700, bottom: 790 });

      class FakeDocument extends EventTarget {
        constructor() {
          super();
          this.body = new FakeElement('body');
          this.documentElement = { clientHeight: 800 };
          this.visibilityState = 'visible';
        }
        getElementById(id) { return elements.get(id) || null; }
        querySelector(selector) { return selector === '.chat-input-bar' ? composerBar : null; }
      }
      class FakeWindow extends EventTarget {}

      const document = new FakeDocument();
      const window = new FakeWindow();
      window.document = document;
      window.innerWidth = 900;
      window.innerHeight = 800;
      window.location = { origin: 'http://study.test', pathname: '/study', search: '', hash: '' };
      window.history = { state: null, replaceState() { window.location.pathname = '/'; } };
      window.confirm = () => true;
      window.__odysseusSetChatMode = () => {};

      globalThis.document = document;
      globalThis.window = window;
      let monotonicMs = 1000;
      globalThis.performance = { now: () => monotonicMs };
      globalThis.requestAnimationFrame = callback => { callback(); return 1; };
      globalThis.ResizeObserver = class { constructor(callback) { this.callback = callback; } observe() {} };

      const events = [];
      let releaseStart;
      const startGate = new Promise(resolve => { releaseStart = resolve; });
      const server = {
        goal_text: 'Build deep mastery',
        target_minutes: 60,
        target_date: null,
        timer_running: false,
        timer_seconds: 0,
        total_seconds: 0,
      };
      const response = payload => ({
        ok: true,
        status: 200,
        text: async () => JSON.stringify(payload),
      });
      globalThis.fetch = async (url, options = {}) => {
        const path = new URL(url).pathname;
        if (path.endsWith('/state')) {
          events.push('state');
          return response({ ...server });
        }
        if (path.endsWith('/timer/start')) {
          events.push('start');
          await startGate;
          server.timer_running = true;
          return response({ ...server });
        }
        if (path.endsWith('/timer/pause')) {
          events.push('pause');
          server.timer_running = false;
          return response({ ...server });
        }
        throw new Error(`Unexpected Study request: ${options.method || 'GET'} ${path}`);
      };

      const study = await import('./static/js/study.js');
      study.init('http://study.test');
      await study.open({ focus: false });

      const start = elements.get('study-timer-start');
      const goal = elements.get('study-goal-text');
      const initialStartEnabled = !start.disabled;
      const drawerBottom = elements.get('study-panel').style.getPropertyValue('--study-drawer-bottom');

      server.total_seconds = 120;
      monotonicMs = 2501;
      window.dispatchEvent(new Event('focus'));
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      const refreshedTotal = elements.get('study-session-total').textContent;

      goal.value = 'Unsaved replacement goal';
      goal.dispatchEvent(new Event('input'));
      const dirtyDisablesStart = start.disabled;
      goal.value = server.goal_text;
      goal.dispatchEvent(new Event('input'));

      start.click();
      await Promise.resolve();
      await Promise.resolve();
      const closePromise = study.close({ manual: false, startFresh: false });
      const reopenPromise = study.open({ focus: false });
      releaseStart();
      const closed = await closePromise;
      const reopened = await reopenPromise;

      console.log(JSON.stringify({
        initialStartEnabled,
        dirtyDisablesStart,
        drawerBottom,
        refreshedTotal,
        events,
        closed,
        reopened,
        timerRunning: server.timer_running,
        active: study.isActive(),
      }));
    """

    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    values = json.loads(result.stdout)

    assert values == {
        "initialStartEnabled": True,
        "dirtyDisablesStart": True,
        "drawerBottom": "112px",
        "refreshedTotal": "2m",
        "events": ["state", "state", "start", "pause", "state"],
        "closed": True,
        "reopened": True,
        "timerRunning": False,
        "active": True,
    }
