"""Exercise Study Mode's real browser module with a small deterministic DOM.

These tests intentionally use the shipped JavaScript instead of duplicating its
state machine in Python.  The fake DOM only supplies the browser primitives the
module needs, while fetch is a tiny in-memory Study API.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


DOM_HARNESS = r"""
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
    constructor() { this.values = new Map(); this.width = ''; this.height = ''; }
    setProperty(name, value) { this.values.set(name, String(value)); }
    removeProperty(name) { this.values.delete(name); }
    getPropertyValue(name) { return this.values.get(name) || ''; }
  }

  class FakeElement extends EventTarget {
    constructor(id = '', tagName = 'div') {
      super();
      this.id = id;
      this.tagName = tagName.toUpperCase();
      this.attributes = new Map();
      this.classList = new FakeClassList();
      this.style = new FakeStyle();
      this.textContent = '';
      this.value = '';
      this.disabled = false;
      this.hidden = false;
      this.title = '';
      this.placeholder = '';
      this.dataset = {};
      this.children = [];
      this.parentNode = null;
      this.inert = false;
      this.queryResults = [];
      this.firstElementChild = null;
      this.isConnected = true;
      this.focusCount = 0;
    }
    get parentElement() { return this.parentNode; }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
    hasAttribute(name) { return this.attributes.has(name); }
    removeAttribute(name) { this.attributes.delete(name); }
    appendChild(child) {
      child.parentNode = this;
      this.children.push(child);
      if (!this.firstElementChild) this.firstElementChild = child;
      return child;
    }
    querySelector(selector) {
      if (selector === '.msg') return null;
      return this.querySelectorAll(selector)[0] || null;
    }
    querySelectorAll(selector) {
      if (selector === '[data-study-modal-close]') return this.modalCloseResults || [];
      if (selector.includes('button:not([disabled])')) return this.focusableResults || [];
      return this.queryResults;
    }
    focus() { document.activeElement = this; this.focusCount += 1; }
    blur() { if (document.activeElement === this) document.activeElement = document.body; }
    click() { this.dispatchEvent(new Event('click', { bubbles: true, cancelable: true })); }
    getBoundingClientRect() { return { top: 0, right: 0, bottom: 0, left: 0, width: 0, height: 0 }; }
  }

  const ids = [
    'study-panel', 'study-panel-body', 'study-panel-title',
    'study-panel-collapse', 'study-panel-close', 'study-timer',
    'study-timer-status', 'study-timer-pause',
    'study-timer-finish', 'study-session-total', 'study-goal-form',
    'study-goal-text', 'study-target-hours', 'study-target-date',
    'study-goal-save', 'study-progress-bar', 'study-progress-value',
    'study-progress-copy', 'study-deadline-copy', 'study-error',
    'study-live-status', 'study-method', 'tool-study-btn', 'rail-study',
    'study-quick-actions', 'study-review-status', 'study-review-due',
    'study-review-actions', 'study-workspace-switcher', 'study-workspace-count',
    'study-new-workspace', 'study-rename-workspace', 'study-chat-launcher',
    'study-controls-open', 'study-controls-close', 'study-control-modal',
    'study-control-dialog', 'study-control-loading', 'study-chat-workspace-name',
    'study-tracker-workspace-name', 'study-tracker-workspace-count',
    'study-tracker-alert', 'study-goal-summary', 'study-mastery-distance',
    'study-control-focus-copy', 'message', 'welcome-screen', 'chat-container',
    'chat-history',
  ];
  const elements = new Map(ids.map(id => [id, new FakeElement(id)]));
  elements.get('study-progress-bar').firstElementChild = new FakeElement('progress-fill');
  elements.get('study-control-modal').hidden = true;
  elements.get('study-error').hidden = true;
  elements.get('study-tracker-alert').hidden = true;

  const recallButton = new FakeElement('recall-button', 'button');
  recallButton.textContent = 'Recall sprint';
  recallButton.dataset.studyPrompt = 'Run a closed-book recall sprint.';
  elements.get('study-quick-actions').queryResults = [recallButton];
  const cleanButton = new FakeElement('clean-button', 'button');
  cleanButton.dataset.studyResult = 'clean';
  elements.get('study-review-actions').queryResults = [cleanButton];
  const backdrop = new FakeElement('study-backdrop', 'button');
  backdrop.dataset.studyModalClose = '';
  elements.get('study-control-modal').modalCloseResults = [backdrop];

  const focusable = [
    elements.get('study-controls-close'), elements.get('study-workspace-switcher'),
    elements.get('study-rename-workspace'), elements.get('study-new-workspace'),
    elements.get('study-timer-pause'), elements.get('study-timer-finish'),
    elements.get('study-goal-text'),
    elements.get('study-target-hours'), elements.get('study-target-date'),
    elements.get('study-goal-save'), recallButton, cleanButton,
  ];
  elements.get('study-control-dialog').focusableResults = focusable;

  const composerBar = new FakeElement('composer-bar');
  composerBar.getBoundingClientRect = () => ({ top: 700, bottom: 790, width: 600, height: 90 });

  class FakeDocument extends EventTarget {
    constructor() {
      super();
      this.body = new FakeElement('body', 'body');
      this.documentElement = { clientHeight: 800 };
      this.visibilityState = 'visible';
      this.activeElement = this.body;
    }
    getElementById(id) { return elements.get(id) || null; }
    createElement(tag) { return new FakeElement('', tag); }
    querySelector(selector) {
      if (selector === '.chat-input-bar') return composerBar;
      if (selector === '.study-control-backdrop' || selector === '[data-study-modal-close]') return backdrop;
      return null;
    }
  }
  class FakeWindow extends EventTarget {}
  class FakeKeyboardEvent extends Event {
    constructor(type, options = {}) {
      super(type, { bubbles: true, cancelable: true });
      this.key = options.key || '';
      this.shiftKey = Boolean(options.shiftKey);
    }
  }
  class FakeCustomEvent extends Event {
    constructor(type, options = {}) {
      super(type, { bubbles: Boolean(options.bubbles), cancelable: Boolean(options.cancelable) });
      this.detail = options.detail;
    }
  }

  const document = new FakeDocument();
  const mainShell = new FakeElement('main-shell', 'main');
  const backgroundSurface = new FakeElement('background-surface', 'section');
  mainShell.appendChild(backgroundSurface);
  mainShell.appendChild(elements.get('study-control-modal'));
  document.body.appendChild(mainShell);
  document.body.appendChild(elements.get('study-panel'));
  const window = new FakeWindow();
  window.document = document;
  window.innerWidth = 900;
  window.innerHeight = 800;
  window.location = { origin: 'http://study.test', pathname: '/study', search: '', hash: '' };
  window.history = { state: null, replaceState() { window.location.pathname = '/'; } };
  window.confirm = () => true;
  window.prompt = () => 'Study workspace';
  window.__odysseusSetChatMode = () => {};
  window.getComputedStyle = () => ({ display: 'block', visibility: 'visible' });

  globalThis.document = document;
  globalThis.window = window;
  globalThis.KeyboardEvent = FakeKeyboardEvent;
  globalThis.CustomEvent = FakeCustomEvent;
  globalThis.getComputedStyle = window.getComputedStyle;
  let monotonicMs = 1000;
  globalThis.performance = { now: () => monotonicMs };
  globalThis.requestAnimationFrame = callback => { callback(); return 1; };
  globalThis.cancelAnimationFrame = () => {};
  globalThis.ResizeObserver = class { constructor(callback) { this.callback = callback; } observe() {} };

  const response = (payload, status = 200) => ({
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(payload),
    json: async () => payload,
  });
  const flush = () => new Promise(resolve => setTimeout(resolve, 0));
  async function waitFor(predicate, message = 'condition', attempts = 80) {
    for (let index = 0; index < attempts; index += 1) {
      if (predicate()) return;
      await flush();
    }
    throw new Error(`Timed out waiting for ${message}`);
  }
"""


def _run_node(body: str) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", DOM_HARNESS + body],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_prompt_only_timer_controls_dialog_and_drawer_clearance():
    values = _run_node(
        r"""
          const events = [];
          const server = {
            session_id: 'study-a', workspace_name: 'Dynamics', goal_initialized: true,
            goal_text: 'Build deep mastery', target_minutes: 60, target_date: null,
            timer_running: false, timer_seconds: 0, total_seconds: 0,
            last_prompt_at: null, idle_pause_at: null, idle_seconds_remaining: null,
            review: { level: 0, count: 0, due: false, due_in_seconds: null, status: 'not_scheduled' },
          };
          const snapshot = () => ({
            ...server,
            studied_seconds: server.total_seconds + server.timer_seconds,
            remaining_seconds: Math.max(0, 3600 - server.total_seconds - server.timer_seconds),
            progress_percent: (server.total_seconds + server.timer_seconds) / 36,
            tracker: {
              active_workspace: { session_id: 'study-a', title: 'Dynamics', mode: 'study' },
              focus_block: { running: server.timer_running, elapsed_seconds: server.timer_seconds, completed_seconds: server.total_seconds },
              learning_goal: { text: server.goal_text, target_minutes: 60, target_date: null, source: 'prompt' },
              effort: { studied_seconds: server.total_seconds + server.timer_seconds, target_seconds: 3600, remaining_seconds: 3600, progress_percent: 0 },
              mastery: { status: 'not_started', next_evidence: 'Complete one hint-free recall check.', review_level: 0, review_count: 0 },
              review_due: server.review,
            },
          });
          globalThis.fetch = async (url, options = {}) => {
            const path = new URL(url).pathname;
            if (path.endsWith('/initialize')) {
              const prompt = JSON.parse(options.body || '{}').prompt || '';
              events.push(`initialize:${prompt}`);
              return response(snapshot());
            }
            if (path.endsWith('/state')) {
              events.push('state');
              return response(snapshot());
            }
            if (path.endsWith('/timer/pause')) {
              events.push('pause');
              server.timer_running = false;
              return response(snapshot());
            }
            if (path.endsWith('/review')) {
              events.push('review:clean');
              server.review = { level: 1, count: 1, last_result: 'clean', due: false, due_in_seconds: 86400, status: 'scheduled' };
              return response(snapshot());
            }
            throw new Error(`Unexpected Study request: ${options.method || 'GET'} ${path}`);
          };

          const study = await import('./static/js/study.js');
          study.init('http://study.test', {
            getCurrentSessionId: () => 'study-a',
            getSessions: () => [{ id: 'study-a', name: 'Dynamics', mode: 'study' }],
          });
          await study.open({ focus: false });

          const pause = elements.get('study-timer-pause');
          const goal = elements.get('study-goal-text');
          const stayedPausedOnEntry = !server.timer_running;
          const promptOnlyStatus = elements.get('study-timer-status').textContent;
          const launcherVisible = !elements.get('study-chat-launcher').hidden;
          const drawerBottom = elements.get('study-panel').style.getPropertyValue('--study-drawer-bottom');

          goal.value = 'Unsaved replacement goal';
          goal.dispatchEvent(new Event('input'));
          goal.value = server.goal_text;
          goal.dispatchEvent(new Event('input'));

          cleanButton.click();
          await waitFor(() => server.review.count === 1, 'review mutation');
          const reviewStatus = elements.get('study-review-status').textContent;

          const prepared = await study.prepareFirstPrompt('Continue control theory', { sessionId: 'study-a' });
          const preflightStayedPaused = !server.timer_running;
          const acceptedPrompt = snapshot();
          acceptedPrompt.timer_running = true;
          acceptedPrompt.last_prompt_at = '2026-07-15T10:00:00';
          acceptedPrompt.idle_pause_at = '2026-07-15T10:10:00';
          acceptedPrompt.idle_seconds_remaining = 600;
          server.timer_running = true;
          Object.assign(server, acceptedPrompt);
          await study.applyServerInitialization(acceptedPrompt, 'study-a');
          const startedAfterAcceptedPrompt = server.timer_running && !pause.disabled;

          pause.click();
          await waitFor(() => !server.timer_running, 'manual pause');
          const closed = await study.close({ manual: false, startFresh: false });
          const reopened = await study.open({ focus: false });
          const stayedPausedOnReopen = !server.timer_running;

          const trigger = elements.get('study-controls-open');
          trigger.focus();
          trigger.click();
          const modalOpened = !elements.get('study-control-modal').hidden
            && elements.get('study-control-modal').getAttribute('aria-hidden') === 'false';
          const backgroundInertWhileOpen = backgroundSurface.inert
            && elements.get('study-panel').inert
            && trigger.getAttribute('aria-expanded') === 'true';
          cleanButton.focus();
          document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab' }));
          const tabWrapped = document.activeElement === elements.get('study-controls-close');
          document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
          const escapeClosed = elements.get('study-control-modal').hidden;
          const triggerRestored = document.activeElement === trigger;
          const backgroundRestored = !backgroundSurface.inert
            && !elements.get('study-panel').inert
            && trigger.getAttribute('aria-expanded') === 'false';

          await study.close({ manual: false, startFresh: false });
          console.log(JSON.stringify({
            stayedPausedOnEntry, promptOnlyStatus, launcherVisible, prepared,
            preflightStayedPaused, startedAfterAcceptedPrompt, drawerBottom,
            reviewStatus, events, closed, reopened, stayedPausedOnReopen,
            modalOpened, tabWrapped, escapeClosed, triggerRestored,
            backgroundInertWhileOpen, backgroundRestored,
          }));
        """
    )

    assert values["stayedPausedOnEntry"] is True
    assert values["promptOnlyStatus"] == "Send a Study prompt to start"
    assert values["launcherVisible"] is True
    assert values["prepared"] is True
    assert values["preflightStayedPaused"] is True
    assert values["startedAfterAcceptedPrompt"] is True
    assert values["drawerBottom"] == "112px"
    assert values["reviewStatus"] == "Level 1 · 1 evidence check"
    assert values["closed"] is True
    assert values["reopened"] is True
    assert values["stayedPausedOnReopen"] is True
    assert values["modalOpened"] is True
    assert values["tabWrapped"] is True
    assert values["escapeClosed"] is True
    assert values["triggerRestored"] is True
    assert values["backgroundInertWhileOpen"] is True
    assert values["backgroundRestored"] is True
    assert values["events"] == [
        "initialize:",
        "review:clean",
        "initialize:Continue control theory",
        "pause",
        "pause",
        "initialize:",
        "pause",
    ]


def test_inactivity_deadline_clamps_and_reconciles_without_blind_pause():
    values = _run_node(
        r"""
          let stateReads = 0;
          let pauseCalls = 0;
          const server = {
            running: true,
            seconds: 40,
            lastPromptAt: '2026-07-15T10:00:00',
            idlePauseAt: new Date(Date.now() + 250).toISOString(),
            idleRemaining: 5,
          };
          const review = { level: 0, count: 0, due: false, due_in_seconds: null, status: 'not_scheduled' };
          const snapshot = () => ({
            session_id: 'study-a', workspace_name: 'Dynamics', goal_initialized: true,
            goal_text: 'Build deep mastery', target_minutes: 60, target_date: null,
            timer_running: server.running, timer_seconds: server.seconds, total_seconds: 0,
            last_prompt_at: server.lastPromptAt, idle_pause_at: server.idlePauseAt,
            idle_seconds_remaining: server.running ? server.idleRemaining : null,
            studied_seconds: server.seconds, remaining_seconds: Math.max(0, 3600 - server.seconds),
            progress_percent: server.seconds / 36, review,
            tracker: {
              active_workspace: { session_id: 'study-a', title: 'Dynamics', mode: 'study' },
              focus_block: { running: server.running, elapsed_seconds: server.seconds, completed_seconds: 0 },
              learning_goal: { text: 'Build deep mastery', target_minutes: 60, target_date: null, source: 'prompt' },
              effort: { studied_seconds: server.seconds, target_seconds: 3600, remaining_seconds: 3600 - server.seconds, progress_percent: server.seconds / 36 },
              mastery: { status: 'building', next_evidence: 'Complete one hint-free recall check.', review_level: 0, review_count: 0 },
              review_due: review,
            },
          });
          globalThis.fetch = async (url, options = {}) => {
            const path = new URL(url).pathname;
            if (path.endsWith('/initialize')) return response(snapshot());
            if (path.endsWith('/state')) {
              stateReads += 1;
              if (stateReads === 1) {
                // Another tab accepted a newer prompt just before this tab's
                // stale local deadline fired. A blind pause would erase it.
                server.running = true;
                server.lastPromptAt = '2026-07-15T10:09:59';
                server.idlePauseAt = new Date(Date.now() + 600000).toISOString();
                server.idleRemaining = 600;
              } else {
                server.running = false;
                server.seconds = 640;
                server.idlePauseAt = null;
                server.idleRemaining = null;
              }
              return response(snapshot());
            }
            if (path.endsWith('/timer/pause')) {
              pauseCalls += 1;
              server.running = false;
              server.idleRemaining = null;
              return response(snapshot());
            }
            throw new Error(`Unexpected Study request: ${options.method || 'GET'} ${path}`);
          };

          const study = await import('./static/js/study.js');
          study.init('http://study.test', {
            getCurrentSessionId: () => 'study-a',
            getSessions: () => [{ id: 'study-a', name: 'Dynamics', mode: 'study' }],
          });
          await study.open({ focus: false });
          await new Promise(resolve => setTimeout(resolve, 300));
          await waitFor(() => stateReads === 1, 'cross-tab lease refresh');
          const newerPromptPreserved = server.running
            && elements.get('study-timer-status').textContent.includes('Focus timer running');

          const clamped = study.deriveLiveProgress({
            ...snapshot(), timer_seconds: 40, idle_seconds_remaining: 600,
          }, 700);
          server.idleRemaining = 5;
          server.idlePauseAt = new Date(Date.now() + 100).toISOString();
          await study.applyServerInitialization(snapshot(), 'study-a');
          await new Promise(resolve => setTimeout(resolve, 150));
          await waitFor(() => stateReads === 2, 'authoritative idle pause');
          const pausedAtDeadline = !server.running
            && elements.get('study-timer-status').textContent.includes('send a Study prompt to resume');
          const pauseCallsBeforeClose = pauseCalls;
          await study.close({ manual: false, startFresh: false });

          console.log(JSON.stringify({
            newerPromptPreserved,
            clampedRunning: clamped.timer_running,
            clampedSeconds: clamped.timer_seconds,
            clampedIdleRemaining: clamped.idle_seconds_remaining,
            pausedAtDeadline,
            stateReads,
            pauseCallsBeforeClose,
          }));
        """
    )

    assert values == {
        "newerPromptPreserved": True,
        "clampedRunning": False,
        "clampedSeconds": 640,
        "clampedIdleRemaining": 0,
        "pausedAtDeadline": True,
        "stateReads": 2,
        "pauseCallsBeforeClose": 0,
    }


def test_workspace_switch_create_first_prompt_and_stream_refresh():
    values = _run_node(
        r"""
          const events = [];
          let currentId = 'study-a';
          let reloadCount = 0;
          let study;
          const sessions = [
            { id: 'study-a', name: 'Dynamics', mode: 'study' },
            { id: 'study-b', name: 'Signals', mode: 'study' },
          ];
          const records = new Map(sessions.map(session => [session.id, {
            title: session.name, goal: 'Choose a substantive Study prompt', goalInitialized: false,
            running: false, seconds: 0, total: 0, lastPromptAt: null,
          }]));
          const snapshot = id => {
            const record = records.get(id);
            return {
              session_id: id, workspace_name: record.title,
              goal_initialized: record.goalInitialized,
              goal_text: record.goal, target_minutes: 180, target_date: null,
              timer_running: record.running, timer_seconds: record.seconds,
              last_prompt_at: record.lastPromptAt,
              idle_pause_at: record.running ? '2026-07-15T10:10:00' : null,
              idle_seconds_remaining: record.running ? 600 : null,
              total_seconds: record.total, studied_seconds: record.total + record.seconds,
              remaining_seconds: 10800, progress_percent: 0,
              review: { level: 0, count: 0, due: false, due_in_seconds: null, status: 'not_scheduled' },
              tracker: {
                active_workspace: { session_id: id, title: record.title, mode: 'study' },
                focus_block: { running: record.running, elapsed_seconds: record.seconds, completed_seconds: record.total },
                learning_goal: { text: record.goal, target_minutes: 180, target_date: null, source: record.goalInitialized ? 'prompt' : 'starter' },
                effort: { studied_seconds: 0, target_seconds: 10800, remaining_seconds: 10800, progress_percent: 0 },
                mastery: { status: record.goalInitialized ? 'building' : 'not_started', next_evidence: 'Solve one novel transfer problem without hints.', review_level: 0, review_count: 0 },
                review_due: { level: 0, count: 0, due: false, due_in_seconds: null, status: 'not_scheduled' },
              },
            };
          };
          globalThis.fetch = async (url, options = {}) => {
            const parsed = new URL(url);
            const path = parsed.pathname;
            const id = parsed.searchParams.get('session_id');
            const record = records.get(id);
            if (path.endsWith('/initialize')) {
              const prompt = JSON.parse(options.body || '{}').prompt || '';
              events.push(`initialize:${id}:${prompt}`);
              for (const [otherId, other] of records) if (otherId !== id) other.running = false;
              if (prompt.trim() && !record.goalInitialized) {
                record.goalInitialized = true;
                record.title = 'PID Control Mastery';
                record.goal = 'Explain PID control from first principles and solve a novel tuning problem without hints.';
                const session = sessions.find(item => item.id === id);
                if (session) session.name = record.title;
                const payload = snapshot(id);
                payload.title_initialized = true;
                payload.goal_initialized = true;
                return response(payload);
              }
              return response(snapshot(id));
            }
            if (path.endsWith('/timer/pause')) {
              events.push(`pause:${id}`);
              record.running = false;
              return response(snapshot(id));
            }
            throw new Error(`Unexpected Study request: ${options.method || 'GET'} ${path}`);
          };

          async function selectSession(id) {
            if (study?.isActive()) await study.beforeSessionSwitch(id, 'study');
            currentId = id;
            return true;
          }
          study = await import('./static/js/study.js');
          study.init('http://study.test', {
            getCurrentSessionId: () => currentId,
            getSessions: () => sessions,
            selectSession,
            styledPrompt: async () => 'Control Systems',
            createStudySession: async name => {
              const id = 'study-c';
              sessions.push({ id, name, mode: 'study' });
              records.set(id, { title: name, goal: 'Choose a substantive Study prompt', goalInitialized: false, running: false, seconds: 0, total: 0, lastPromptAt: null });
              return { id, name, mode: 'study' };
            },
            reloadSessions: async () => { reloadCount += 1; },
          });

          await study.enter({ focus: false });
          const enteredAPaused = !records.get('study-a').running;
          const trainingMovesDisabledBeforeGoal = recallButton.disabled && cleanButton.disabled;
          elements.get('study-workspace-switcher').value = 'study-b';
          elements.get('study-workspace-switcher').dispatchEvent(new Event('change'));
          await waitFor(() => events.some(item => item === 'initialize:study-b:'), 'workspace switch initialization');
          const switched = currentId === 'study-b';
          const enteredBPaused = !records.get('study-b').running && !records.get('study-a').running;

          elements.get('study-controls-open').click();
          elements.get('study-new-workspace').click();
          await waitFor(() => events.some(item => item === 'initialize:study-c:'), 'new workspace initialization');
          const createdAndPaused = currentId === 'study-c'
            && !records.get('study-c').running
            && !records.get('study-b').running;

          const prepared = await study.prepareFirstPrompt('Master PID control from scratch', { sessionId: 'study-c' });
          const promptSaved = records.get('study-c').goalInitialized;
          const preflightStayedPaused = !records.get('study-c').running;
          const trainingMovesEnabledAfterGoal = !recallButton.disabled && !cleanButton.disabled;
          const derivedTitle = elements.get('study-tracker-workspace-name').textContent;
          const derivedGoal = elements.get('study-goal-summary').textContent;

          records.get('study-c').running = true;
          records.get('study-c').lastPromptAt = '2026-07-15T10:00:00';
          const accepted = snapshot('study-c');
          await study.applyServerInitialization(accepted, 'study-c');
          const startedAfterAcceptedPrompt = records.get('study-c').running
            && elements.get('study-timer-status').textContent.includes('Focus timer running');

          const streamed = snapshot('study-c');
          streamed.workspace_name = 'PID Control — Transfer';
          streamed.tracker.active_workspace.title = 'PID Control — Transfer';
          streamed.tracker.mastery.next_evidence = 'Complete a closed-book transfer check.';
          streamed.title_initialized = true;
          const streamApplied = await study.applyServerInitialization(streamed, 'study-c');
          const streamedTitle = elements.get('study-tracker-workspace-name').textContent;
          const masteryDistance = elements.get('study-mastery-distance').textContent;

          recallButton.click();
          const quickPrompt = elements.get('message').value;
          const quickClosedDialog = elements.get('study-control-modal').hidden;
          const quickFocusedComposer = document.activeElement === elements.get('message');

          await study.close({ manual: false, startFresh: false });
          console.log(JSON.stringify({
            enteredAPaused, switched, enteredBPaused, createdAndPaused, prepared,
            promptSaved, preflightStayedPaused, startedAfterAcceptedPrompt,
            trainingMovesDisabledBeforeGoal, trainingMovesEnabledAfterGoal,
            derivedTitle, derivedGoal, streamApplied, streamedTitle, masteryDistance,
            quickPrompt, quickClosedDialog, quickFocusedComposer, reloadCount, events,
          }));
        """
    )

    assert values["enteredAPaused"] is True
    assert values["switched"] is True
    assert values["enteredBPaused"] is True
    assert values["createdAndPaused"] is True
    assert values["prepared"] is True
    assert values["promptSaved"] is True
    assert values["preflightStayedPaused"] is True
    assert values["startedAfterAcceptedPrompt"] is True
    assert values["trainingMovesDisabledBeforeGoal"] is True
    assert values["trainingMovesEnabledAfterGoal"] is True
    assert values["derivedTitle"] == "PID Control Mastery"
    assert values["derivedGoal"].startswith("Explain PID control from first principles")
    assert values["streamApplied"] is True
    assert values["streamedTitle"] == "PID Control — Transfer"
    assert values["masteryDistance"] == "Next mastery evidence: Complete a closed-book transfer check."
    assert values["quickPrompt"] == "Run a closed-book recall sprint."
    assert values["quickClosedDialog"] is True
    assert values["quickFocusedComposer"] is True
    assert values["reloadCount"] >= 2
    assert "initialize:study-a:" in values["events"]
    assert "pause:study-a" in values["events"]
    assert "initialize:study-b:" in values["events"]
    assert "pause:study-b" in values["events"]
    assert "initialize:study-c:" in values["events"]
    assert "initialize:study-c:Master PID control from scratch" in values["events"]


def test_workspace_creation_failure_is_visible_and_retryable():
    values = _run_node(
        r"""
          let attempts = 0;
          let currentId = null;
          let study;
          const sessions = [];
          const record = { running: false };
          const snapshot = () => ({
            session_id: 'study-retry', workspace_name: 'Retry Mastery', goal_initialized: false,
            goal_text: 'Choose a substantive Study prompt', target_minutes: 180, target_date: null,
            timer_running: record.running, timer_seconds: 0, total_seconds: 0,
            studied_seconds: 0, remaining_seconds: 10800, progress_percent: 0,
            review: { level: 0, count: 0, due: false, due_in_seconds: null, status: 'not_scheduled' },
            tracker: {
              active_workspace: { session_id: 'study-retry', title: 'Retry Mastery', mode: 'study' },
              focus_block: { running: record.running, elapsed_seconds: 0, completed_seconds: 0 },
              learning_goal: { text: 'Choose a substantive Study prompt', target_minutes: 180, target_date: null, source: 'starter' },
              effort: { studied_seconds: 0, target_seconds: 10800, remaining_seconds: 10800, progress_percent: 0 },
              mastery: { status: 'not_started', next_evidence: 'Send a real Study prompt.', review_level: 0, review_count: 0 },
              review_due: { level: 0, count: 0, due: false, due_in_seconds: null, status: 'not_scheduled' },
            },
          });
          globalThis.fetch = async (url, options = {}) => {
            const path = new URL(url).pathname;
            if (path.endsWith('/initialize')) { return response(snapshot()); }
            if (path.endsWith('/timer/pause')) { record.running = false; return response(snapshot()); }
            throw new Error(`Unexpected request ${options.method || 'GET'} ${path}`);
          };

          study = await import('./static/js/study.js');
          study.init('http://study.test', {
            getCurrentSessionId: () => currentId,
            getSessions: () => sessions,
            styledPrompt: async () => 'Retry Mastery',
            createStudySession: async name => {
              attempts += 1;
              if (attempts === 1) throw new Error('Workspace storage is temporarily unavailable.');
              const created = { id: 'study-retry', name, mode: 'study' };
              sessions.push(created);
              return created;
            },
            selectSession: async id => { currentId = id; return true; },
          });

          const firstEnter = await study.enter({ focus: false });
          const modalVisible = !elements.get('study-control-modal').hidden;
          const modalErrorVisible = !elements.get('study-error').hidden;
          const trackerErrorVisible = !elements.get('study-tracker-alert').hidden;
          const specificError = elements.get('study-error').textContent;
          const retryEnabled = !elements.get('study-new-workspace').disabled;

          elements.get('study-new-workspace').click();
          await waitFor(() => study.isActive() && currentId === 'study-retry', 'retry workspace activation');
          const recovered = study.isActive() && currentId === 'study-retry' && !record.running;
          const errorCleared = elements.get('study-error').hidden && elements.get('study-tracker-alert').hidden;
          await study.close({ manual: false, startFresh: false });

          console.log(JSON.stringify({
            firstEnter, modalVisible, modalErrorVisible, trackerErrorVisible,
            specificError, retryEnabled, attempts, recovered, errorCleared,
          }));
        """
    )

    assert values == {
        "firstEnter": False,
        "modalVisible": True,
        "modalErrorVisible": True,
        "trackerErrorVisible": True,
        "specificError": "Workspace storage is temporarily unavailable.",
        "retryEnabled": True,
        "attempts": 2,
        "recovered": True,
        "errorCleared": True,
    }


def test_closing_during_workspace_creation_never_reopens_study():
    values = _run_node(
        r"""
          let currentId = 'study-a';
          let releaseCreate;
          let createStarted = false;
          let createFinished = false;
          let selectCalls = 0;
          const createGate = new Promise(resolve => { releaseCreate = resolve; });
          const sessions = [{ id: 'study-a', name: 'Dynamics', mode: 'study' }];
          const records = new Map([['study-a', { running: false }]]);
          const snapshot = id => ({
            session_id: id, workspace_name: id === 'study-a' ? 'Dynamics' : 'Signals',
            goal_initialized: false, goal_text: 'Choose a substantive Study prompt',
            target_minutes: 180, target_date: null,
            timer_running: Boolean(records.get(id)?.running), timer_seconds: 0,
            total_seconds: 0, studied_seconds: 0, remaining_seconds: 10800,
            progress_percent: 0,
            review: { level: 0, count: 0, due: false, due_in_seconds: null, status: 'not_scheduled' },
            tracker: {
              active_workspace: { session_id: id, title: id === 'study-a' ? 'Dynamics' : 'Signals', mode: 'study' },
              focus_block: { running: Boolean(records.get(id)?.running), elapsed_seconds: 0, completed_seconds: 0 },
              learning_goal: { text: 'Choose a substantive Study prompt', target_minutes: 180, target_date: null, source: 'starter' },
              effort: { studied_seconds: 0, target_seconds: 10800, remaining_seconds: 10800, progress_percent: 0 },
              mastery: { status: 'not_started', next_evidence: 'Send a real Study prompt.', review_level: 0, review_count: 0 },
              review_due: { level: 0, count: 0, due: false, due_in_seconds: null, status: 'not_scheduled' },
            },
          });
          globalThis.fetch = async (url, options = {}) => {
            const parsed = new URL(url);
            const id = parsed.searchParams.get('session_id');
            if (parsed.pathname.endsWith('/initialize')) {
              return response(snapshot(id));
            }
            if (parsed.pathname.endsWith('/timer/pause')) {
              records.get(id).running = false;
              return response(snapshot(id));
            }
            throw new Error(`Unexpected request ${options.method || 'GET'} ${parsed.pathname}`);
          };

          const study = await import('./static/js/study.js');
          study.init('http://study.test', {
            getCurrentSessionId: () => currentId,
            getSessions: () => sessions,
            styledPrompt: async () => 'Signals',
            createStudySession: async name => {
              createStarted = true;
              await createGate;
              const created = { id: 'study-b', name, mode: 'study' };
              sessions.push(created);
              records.set('study-b', { running: false });
              createFinished = true;
              return created;
            },
            selectSession: async id => {
              selectCalls += 1;
              currentId = id;
              return true;
            },
          });

          await study.enter({ focus: false });
          elements.get('study-controls-open').click();
          elements.get('study-new-workspace').click();
          await waitFor(() => createStarted, 'workspace creation to begin');
          const closed = await study.close({ manual: false, startFresh: false });
          releaseCreate();
          await waitFor(() => createFinished, 'workspace creation to settle');
          await flush();
          await flush();

          console.log(JSON.stringify({
            closed,
            inactive: !study.isActive(),
            currentId,
            selectCalls,
            panelHidden: elements.get('study-panel').hidden,
            modalHidden: elements.get('study-control-modal').hidden,
            newWorkspaceExists: sessions.some(session => session.id === 'study-b'),
            newTimerStayedStopped: !records.get('study-b').running,
          }));
        """
    )

    assert values == {
        "closed": True,
        "inactive": True,
        "currentId": "study-a",
        "selectCalls": 0,
        "panelHidden": True,
        "modalHidden": True,
        "newWorkspaceExists": True,
        "newTimerStayedStopped": True,
    }
