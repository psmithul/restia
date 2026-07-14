// static/js/study.js
// Study Mode goal, effort timer, and panel lifecycle.

const EMPTY_REVIEW = Object.freeze({
  level: 0,
  count: 0,
  last_result: null,
  last_reviewed_at: null,
  next_review_at: null,
  due: false,
  due_in_seconds: null,
  status: 'not_scheduled',
});

const EMPTY_TRACKER = Object.freeze({
  active_workspace: Object.freeze({ session_id: null, title: 'Study workspace', mode: 'study' }),
  focus_block: Object.freeze({ running: false, elapsed_seconds: 0, completed_seconds: 0 }),
  learning_goal: Object.freeze({ text: '', target_minutes: 0, target_date: null, source: 'starter' }),
  effort: Object.freeze({ studied_seconds: 0, target_seconds: 0, remaining_seconds: 0, progress_percent: 0 }),
  mastery: Object.freeze({ status: 'not_started', next_evidence: '', review_level: 0, review_count: 0 }),
  review_due: EMPTY_REVIEW,
});

const EMPTY_STATE = Object.freeze({
  session_id: null,
  workspace_name: 'Study workspace',
  goal_initialized: false,
  goal_text: '',
  target_minutes: 0,
  target_date: null,
  timer_running: false,
  timer_seconds: 0,
  total_seconds: 0,
  studied_seconds: 0,
  remaining_seconds: 0,
  progress_percent: 0,
  review: EMPTY_REVIEW,
  tracker: EMPTY_TRACKER,
});

let API_BASE = '';
const REQUEST_TIMEOUT_MS = 12000;
let _initialized = false;
let _active = false;
let _activeSessionId = null;
let _collapsed = false;
let _state = { ...EMPTY_STATE };
let _syncClockMs = 0;
let _ticker = null;
let _stateController = null;
let _mutationChain = Promise.resolve();
let _busy = false;
let _busyDepth = 0;
let _busyMessage = '';
let _closing = false;
let _closePromise = null;
let _goalDirty = false;
let _lastResyncMs = 0;
let _composerObserver = null;
let _elements = {};
let _previousChatUi = null;
let _options = {};
let _controlsOpen = false;
let _controlsTrigger = null;
let _lifecycleGeneration = 0;
let _pendingWorkspaceTransition = null;
const _suppressedWorkspaceOpens = new Map();
let _inertBackground = [];
const _initializingPrompt = new Map();

const ids = [
  'study-panel', 'study-panel-body', 'study-panel-title',
  'study-panel-collapse', 'study-panel-close',
  'study-timer', 'study-timer-status', 'study-timer-start',
  'study-timer-pause', 'study-timer-finish', 'study-session-total',
  'study-goal-form', 'study-goal-text', 'study-target-hours',
  'study-target-date', 'study-goal-save', 'study-progress-bar',
  'study-progress-value', 'study-progress-copy', 'study-deadline-copy',
  'study-error', 'study-live-status', 'study-method',
  'study-workspace-switcher', 'study-workspace-count',
  'study-new-workspace', 'study-rename-workspace', 'study-quick-actions',
  'study-review-status', 'study-review-due', 'study-review-actions',
  'study-chat-launcher', 'study-controls-open', 'study-controls-close',
  'study-control-modal', 'study-control-dialog', 'study-control-loading',
  'study-chat-workspace-name', 'study-tracker-workspace-name',
  'study-tracker-workspace-count', 'study-tracker-alert', 'study-goal-summary',
  'study-mastery-distance', 'study-control-focus-copy',
];

function _number(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function _seconds(value) {
  return Math.max(0, Math.floor(_number(value)));
}

function _percent(value) {
  return Math.min(100, Math.max(0, _number(value)));
}

function _review(value) {
  const review = value && typeof value === 'object' ? value : EMPTY_REVIEW;
  return {
    ...EMPTY_REVIEW,
    ...review,
    level: Math.max(0, Math.floor(_number(review.level))),
    count: Math.max(0, Math.floor(_number(review.count))),
    last_result: review.last_result || null,
    last_reviewed_at: review.last_reviewed_at || null,
    next_review_at: review.next_review_at || null,
    due: Boolean(review.due),
    due_in_seconds: review.due_in_seconds == null ? null : Math.floor(_number(review.due_in_seconds)),
    status: String(review.status || 'not_scheduled'),
  };
}

/** Format seconds as a stable HH:MM:SS clock value. */
export function formatDuration(value) {
  const seconds = _seconds(value);
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remainder = seconds % 60;
  return [hours, minutes, remainder].map(part => String(part).padStart(2, '0')).join(':');
}

/**
 * Derive the live timer and effort progress without mutating server state.
 * `elapsedSeconds` is time elapsed since the state response was received.
 */
export function deriveLiveProgress(state = EMPTY_STATE, elapsedSeconds = 0) {
  const runningDelta = state.timer_running ? _seconds(elapsedSeconds) : 0;
  const timerSeconds = _seconds(state.timer_seconds) + runningDelta;
  const totalSeconds = _seconds(state.total_seconds);
  const studiedSeconds = totalSeconds + timerSeconds;
  const targetMinutes = Math.max(0, Math.floor(_number(state.target_minutes)));
  const targetSeconds = targetMinutes * 60;
  const remainingSeconds = Math.max(0, targetSeconds - studiedSeconds);
  const progressPercent = targetSeconds
    ? Math.min(100, Math.max(0, (studiedSeconds / targetSeconds) * 100))
    : 0;

  return {
    ...EMPTY_STATE,
    ...state,
    session_id: state.session_id || null,
    goal_text: String(state.goal_text || ''),
    target_minutes: targetMinutes,
    target_date: state.target_date || null,
    timer_running: Boolean(state.timer_running),
    timer_seconds: timerSeconds,
    total_seconds: totalSeconds,
    studied_seconds: studiedSeconds,
    remaining_seconds: remainingSeconds,
    progress_percent: Math.round(_percent(progressPercent) * 10) / 10,
    review: _review(state.review),
  };
}

// Descriptive aliases keep the helpers easy to discover at call sites/tests.
export const formatStudyDuration = formatDuration;
export const computeLiveProgress = deriveLiveProgress;

function _monotonicNow() {
  try {
    if (typeof performance !== 'undefined' && typeof performance.now === 'function') {
      return performance.now();
    }
  } catch (_) {}
  return Date.now();
}

function _normalizeApiBase(value) {
  let base = String(value || '').replace(/\/+$/, '');
  if (!base && typeof window !== 'undefined') base = window.location.origin;
  if (/\/api\/study$/i.test(base)) return base;
  if (/\/api$/i.test(base)) return `${base}/study`;
  return `${base}/api/study`;
}

function _sessionId(value = _activeSessionId) {
  return String(value || '').trim();
}

function _studySessions() {
  try {
    const sessions = typeof _options.getSessions === 'function' ? _options.getSessions() : [];
    return (Array.isArray(sessions) ? sessions : []).filter(
      session => String(session?.mode || '').toLowerCase() === 'study',
    );
  } catch (_) {
    return [];
  }
}

function _currentSessionId() {
  try {
    return _sessionId(typeof _options.getCurrentSessionId === 'function'
      ? _options.getCurrentSessionId()
      : null);
  } catch (_) {
    return '';
  }
}

function _workspaceName(sessionId = _activeSessionId) {
  const match = _studySessions().find(session => String(session.id) === _sessionId(sessionId));
  return String(match?.name || 'Study workspace');
}

function _setActiveSessionId(value) {
  _activeSessionId = _sessionId(value) || null;
  if (typeof window !== 'undefined') {
    window.__restiaStudyWorkspaceId = _activeSessionId;
  }
  const switcher = _elements['study-workspace-switcher'];
  if (switcher && _activeSessionId) switcher.value = _activeSessionId;
}

function _get(id) {
  return typeof document === 'undefined' ? null : document.getElementById(id);
}

function _cacheElements() {
  _elements = {};
  ids.forEach(id => { _elements[id] = _get(id); });
  return Boolean(_elements['study-panel']);
}

function _humanTime(value) {
  const seconds = _seconds(value);
  if (seconds < 60) return `${seconds}s`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (!hours) return `${minutes}m`;
  return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
}

function _reviewTimestamp(value) {
  const raw = String(value || '').trim();
  if (!raw) return null;
  const timestamp = /(?:Z|[+-]\d\d:\d\d)$/i.test(raw) ? raw : `${raw}Z`;
  const parsed = new Date(timestamp);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function _reviewDueSeconds(review) {
  const next = _reviewTimestamp(review?.next_review_at);
  if (next) return Math.ceil((next.getTime() - Date.now()) / 1000);
  if (review?.due_in_seconds != null) return Math.floor(_number(review.due_in_seconds));
  return null;
}

function _reviewDueText(review) {
  const dueSeconds = _reviewDueSeconds(review);
  if (dueSeconds == null) return 'After a closed-book check, record the evidence here.';
  if (dueSeconds <= 0 || review?.due) return 'Review due now. Use Recall sprint, then record the result.';
  const next = _reviewTimestamp(review?.next_review_at);
  if (dueSeconds < 3600) return `Next retrieval in ${Math.max(1, Math.ceil(dueSeconds / 60))}m.`;
  if (dueSeconds < 86400) return `Next retrieval in ${Math.max(1, Math.ceil(dueSeconds / 3600))}h.`;
  if (next) {
    const label = new Intl.DateTimeFormat(undefined, {
      month: 'short', day: 'numeric',
      year: next.getFullYear() !== new Date().getFullYear() ? 'numeric' : undefined,
    }).format(next);
    return `Next closed-book retrieval ${label}.`;
  }
  return `Next retrieval in ${Math.max(1, Math.ceil(dueSeconds / 86400))}d.`;
}

function _renderReview(live = _liveState()) {
  const review = _review(live.review);
  const status = review.count
    ? `Level ${review.level} · ${review.count} evidence check${review.count === 1 ? '' : 's'}`
    : 'No mastery evidence recorded yet';
  _setText(_elements['study-review-status'], status);
  _setText(_elements['study-review-due'], _reviewDueText(review));
}

function _hoursValue(minutes) {
  if (!_number(minutes)) return '';
  return String(Number((_number(minutes) / 60).toFixed(2)));
}

function _deadlineText(dateValue) {
  if (!dateValue) return 'No target date set.';
  const target = new Date(`${dateValue}T00:00:00`);
  if (Number.isNaN(target.getTime())) return 'Target date unavailable.';
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Math.round((target.getTime() - today.getTime()) / 86400000);
  const label = new Intl.DateTimeFormat(undefined, {
    month: 'short', day: 'numeric', year: target.getFullYear() !== today.getFullYear() ? 'numeric' : undefined,
  }).format(target);
  if (days > 1) return `Target ${label} · ${days} days left`;
  if (days === 1) return `Target ${label} · 1 day left`;
  if (days === 0) return `Target ${label} · due today`;
  const overdue = Math.abs(days);
  return `Target ${label} · ${overdue} day${overdue === 1 ? '' : 's'} past`;
}

function _elapsedSinceSync() {
  if (!_state.timer_running || !_syncClockMs) return 0;
  return Math.max(0, Math.floor((_monotonicNow() - _syncClockMs) / 1000));
}

function _liveState() {
  return deriveLiveProgress(_state, _elapsedSinceSync());
}

function _setText(element, value) {
  if (element) element.textContent = value;
}

function _showError(message) {
  const text = String(message || '').trim();
  for (const error of [_elements['study-error'], _elements['study-tracker-alert']]) {
    if (!error) continue;
    error.textContent = text;
    error.hidden = !text;
    error.setAttribute('role', 'alert');
  }
}

function _controlsTriggerTarget() {
  const launcher = _elements['study-controls-open'];
  if (launcher && !launcher.hidden && !launcher.closest?.('[hidden]')) return launcher;
  return document.activeElement || _get('tool-study-btn') || _get('rail-study');
}

function _announce(message) {
  const status = _elements['study-live-status'];
  if (!status) return;
  status.textContent = '';
  // A fresh text node makes repeated messages announce reliably without
  // putting the once-per-second clock in a live region.
  setTimeout(() => { if (status) status.textContent = String(message || ''); }, 0);
}

function _syncControls(live = _liveState()) {
  const start = _elements['study-timer-start'];
  const pause = _elements['study-timer-pause'];
  const finish = _elements['study-timer-finish'];
  const save = _elements['study-goal-save'];
  const switcher = _elements['study-workspace-switcher'];
  const create = _elements['study-new-workspace'];
  const rename = _elements['study-rename-workspace'];
  const hasLearningGoal = Boolean(live.goal_initialized && String(live.goal_text || '').trim());
  if (start) start.disabled = _busy || _closing || live.timer_running || _goalDirty;
  if (pause) pause.disabled = _busy || _closing || !live.timer_running;
  if (finish) finish.disabled = _busy || _closing || live.timer_seconds <= 0;
  if (save) save.disabled = _busy || _closing;
  if (switcher) switcher.disabled = _busy || _closing || _studySessions().length < 2;
  if (create) create.disabled = _busy || _closing;
  if (rename) rename.disabled = _busy || _closing || !_activeSessionId;
  const reviewActions = _elements['study-review-actions'];
  if (reviewActions?.querySelectorAll) {
    reviewActions.querySelectorAll('[data-study-result]').forEach(button => {
      button.disabled = _busy || _closing || !_activeSessionId || !hasLearningGoal;
    });
  }
  const quickActions = _elements['study-quick-actions'];
  if (quickActions?.querySelectorAll) {
    quickActions.querySelectorAll('[data-study-prompt]').forEach(button => {
      button.disabled = _busy || _closing || !_activeSessionId || !hasLearningGoal;
    });
  }
}

function _setBusy(value) {
  if (value) _busyDepth += 1;
  else _busyDepth = Math.max(0, _busyDepth - 1);
  _busy = _busyDepth > 0;
  const panel = _elements['study-panel'];
  if (panel) panel.setAttribute('aria-busy', String(_busy));
  const dialog = _elements['study-control-dialog'];
  if (dialog) dialog.setAttribute('aria-busy', String(_busy));
  _setText(_elements['study-control-loading'], _busy ? (_busyMessage || 'Updating Study workspace…') : '');
  _syncControls();
}

function _trackerFor(live) {
  const tracker = live?.tracker && typeof live.tracker === 'object' ? live.tracker : EMPTY_TRACKER;
  return {
    ...EMPTY_TRACKER,
    ...tracker,
    active_workspace: { ...EMPTY_TRACKER.active_workspace, ...(tracker.active_workspace || {}) },
    focus_block: { ...EMPTY_TRACKER.focus_block, ...(tracker.focus_block || {}) },
    learning_goal: { ...EMPTY_TRACKER.learning_goal, ...(tracker.learning_goal || {}) },
    effort: { ...EMPTY_TRACKER.effort, ...(tracker.effort || {}) },
    mastery: { ...EMPTY_TRACKER.mastery, ...(tracker.mastery || {}) },
    review_due: _review(tracker.review_due || live?.review),
  };
}

function _renderTracker(live = _liveState()) {
  const tracker = _trackerFor(live);
  const stateTitle = String(live.workspace_name || '').trim();
  const trackerTitle = String(tracker.active_workspace.title || '').trim();
  const title = (stateTitle && stateTitle !== 'Study workspace')
    ? stateTitle
    : ((trackerTitle && trackerTitle !== 'Study workspace')
      ? trackerTitle
      : _workspaceName());
  const workspaceCount = _studySessions().length;
  const countLabel = `${workspaceCount} workspace${workspaceCount === 1 ? '' : 's'}`;
  _setText(_elements['study-chat-workspace-name'], title);
  _setText(_elements['study-tracker-workspace-name'], title);
  _setText(_elements['study-tracker-workspace-count'], countLabel);

  const goalText = String(live.goal_text || tracker.learning_goal.text || '').trim();
  const hasServerTracker = Boolean(live?.tracker && live.tracker !== EMPTY_TRACKER);
  const goalIsStarter = !goalText || (hasServerTracker && tracker.learning_goal.source === 'starter');
  _setText(
    _elements['study-goal-summary'],
    goalIsStarter
      ? 'Send your first real Study prompt. Restia will turn it into a measurable goal and save it here.'
      : goalText,
  );

  const nextEvidence = String(tracker.mastery.next_evidence || '').trim();
  _setText(
    _elements['study-mastery-distance'],
    nextEvidence
      ? `Next mastery evidence: ${nextEvidence}`
      : 'Mastery is measured by closed-book recall and transfer, not time alone.',
  );
  _setText(
    _elements['study-control-focus-copy'],
    live.timer_running
      ? `Running now · ${formatDuration(live.timer_seconds)} in this focus block.`
      : (live.timer_seconds
        ? `Paused at ${formatDuration(live.timer_seconds)}. Resume when you are ready.`
        : 'The timer starts automatically when this workspace opens.'),
  );
}

function _renderLive() {
  const live = _liveState();
  _setText(_elements['study-timer'], formatDuration(live.timer_seconds));
  _setText(
    _elements['study-timer-status'],
    _goalDirty && !live.timer_running
      ? 'Save goal changes before starting'
      : !live.goal_text
      ? 'Set a learning goal to start'
      : (live.timer_running ? 'Focus timer running' : (live.timer_seconds ? 'Focus timer paused' : 'Ready to study')),
  );
  _setText(_elements['study-session-total'], _humanTime(live.studied_seconds));

  const progress = _percent(live.progress_percent);
  const bar = _elements['study-progress-bar'];
  const value = _elements['study-progress-value'];
  if (bar) {
    bar.setAttribute('role', 'progressbar');
    bar.setAttribute('aria-valuemin', '0');
    bar.setAttribute('aria-valuemax', '100');
    bar.setAttribute('aria-valuenow', String(Math.round(progress)));
    bar.setAttribute('aria-valuetext', live.target_minutes
      ? `${progress.toFixed(1)} percent of study-time target`
      : 'No study-time target set');
  }
  const fill = bar?.firstElementChild;
  if (fill) fill.style.width = `${progress}%`;
  if (value) {
    const label = Number.isInteger(progress) ? progress.toFixed(0) : progress.toFixed(1);
    value.textContent = `${label}%`;
  }

  if (live.target_minutes) {
    const targetSeconds = live.target_minutes * 60;
    _setText(
      _elements['study-progress-copy'],
      `${_humanTime(live.studied_seconds)} of ${_humanTime(targetSeconds)} study-time target · ${_humanTime(live.remaining_seconds)} remaining`,
    );
  } else {
    _setText(_elements['study-progress-copy'], 'Set a study-time target to measure effort.');
  }
  _setText(_elements['study-deadline-copy'], _deadlineText(live.target_date));
  _renderReview(live);
  _renderTracker(live);
  _syncControls(live);
}

function _renderForm() {
  const goal = _elements['study-goal-text'];
  const hours = _elements['study-target-hours'];
  const date = _elements['study-target-date'];
  if (goal) goal.value = _state.goal_text || '';
  if (hours) hours.value = _hoursValue(_state.target_minutes);
  if (date) date.value = _state.target_date || '';
  _goalDirty = false;
}

function _renderWorkspaces() {
  const sessions = _studySessions();
  const switcher = _elements['study-workspace-switcher'];
  const count = _elements['study-workspace-count'];
  const countLabel = `${sessions.length} workspace${sessions.length === 1 ? '' : 's'}`;
  if (count) count.textContent = countLabel;
  _setText(_elements['study-tracker-workspace-count'], countLabel);
  if (!switcher) return;

  const previous = _sessionId(_activeSessionId || switcher.value);
  switcher.textContent = '';
  if (typeof document.createElement === 'function') {
    sessions.forEach(session => {
      const option = document.createElement('option');
      option.value = String(session.id || '');
      option.textContent = String(session.name || 'Untitled Study');
      switcher.appendChild(option);
    });
  }
  if (previous) switcher.value = previous;
  switcher.setAttribute('aria-label', sessions.length
    ? `Current Study workspace: ${_workspaceName(previous)}`
    : 'Current Study workspace');
  _renderTracker();
  _syncControls();
}

function _applyState(payload, { syncForm = false, sessionId = _activeSessionId } = {}) {
  if (_sessionId(sessionId) && _sessionId(sessionId) !== _sessionId(_activeSessionId)) return false;
  _state = deriveLiveProgress(payload || EMPTY_STATE, 0);
  _syncClockMs = _monotonicNow();
  if (syncForm) _renderForm();
  _renderWorkspaces();
  _renderLive();
  _syncTicker();
  return true;
}

function _stopTicker() {
  if (_ticker !== null) {
    clearInterval(_ticker);
    _ticker = null;
  }
}

function _syncTicker() {
  _stopTicker();
  if (!_active || !_state.timer_running) return;
  _ticker = setInterval(_renderLive, 1000);
}

async function _request(path, options = {}, sessionId = _activeSessionId) {
  const workspaceId = _sessionId(sessionId);
  if (!workspaceId) throw new Error('Choose or create a Study workspace first.');
  const separator = String(path).includes('?') ? '&' : '?';
  const externalSignal = options.signal;
  const controller = new AbortController();
  let timedOut = false;
  const abortFromExternal = () => controller.abort();
  if (externalSignal?.aborted) controller.abort();
  else externalSignal?.addEventListener?.('abort', abortFromExternal, { once: true });
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(`${API_BASE}${path}${separator}session_id=${encodeURIComponent(workspaceId)}`, {
      credentials: 'same-origin',
      ...options,
      signal: controller.signal,
    });
    const text = await response.text();
    let payload = {};
    if (text) {
      try { payload = JSON.parse(text); }
      catch (_) { payload = { detail: text }; }
    }
    if (!response.ok) {
      throw new Error(payload.detail || payload.error || `Study request failed (${response.status})`);
    }
    return payload;
  } catch (error) {
    if (timedOut) throw new Error('Study request timed out. Check the connection and try again.');
    throw error;
  } finally {
    clearTimeout(timeout);
    externalSignal?.removeEventListener?.('abort', abortFromExternal);
  }
}

function _withTimeout(operation, message, timeoutMs = REQUEST_TIMEOUT_MS) {
  let timeout = null;
  const promise = typeof operation === 'function'
    ? Promise.resolve().then(operation)
    : Promise.resolve(operation);
  const deadline = new Promise((_, reject) => {
    timeout = setTimeout(() => reject(new Error(message)), timeoutMs);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timeout));
}

async function _fetchWithTimeout(url, options = {}, message = 'Study request timed out. Try again.') {
  const controller = new AbortController();
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, REQUEST_TIMEOUT_MS);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } catch (error) {
    if (timedOut) throw new Error(message);
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

async function _refreshSessionList() {
  if (typeof _options.reloadSessions !== 'function') return;
  await _withTimeout(
    () => _options.reloadSessions(),
    'The Study workspace was saved, but refreshing the workspace list timed out. Reload the page to update it.',
  );
  _renderWorkspaces();
}

async function _initializeWorkspace({ prompt = '', sessionId = _activeSessionId, syncForm = true } = {}) {
  const workspaceId = _sessionId(sessionId);
  if (!workspaceId) {
    _showError('Choose or create a Study workspace first.');
    return null;
  }
  const cleanPrompt = String(prompt || '').trim();
  const requestKey = `${workspaceId}\u0000${cleanPrompt}`;
  if (_initializingPrompt.has(requestKey)) return _initializingPrompt.get(requestKey);

  const operation = (async () => {
    _busyMessage = cleanPrompt ? 'Saving your learning goal…' : 'Starting your focus timer…';
    _setBusy(true);
    _showError('');
    try {
      const state = await _queueMutation(() => _request('/initialize', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: cleanPrompt }),
      }, workspaceId));
      if (_sessionId(_activeSessionId) === workspaceId) {
        _applyState(state, { syncForm, sessionId: workspaceId });
        _lastResyncMs = _monotonicNow();
      }
      if (state.title_initialized && typeof _options.reloadSessions === 'function') {
        try {
          await _refreshSessionList();
        } catch (error) {
          console.error('Study workspace saved, but the session list could not refresh:', error);
          _showError('Your Study goal was saved, but the workspace list could not refresh. Reload the page to update it.');
        }
      }
      if (state.goal_initialized) {
        _announce(`Learning goal saved for ${state.workspace_name || 'this Study workspace'}.`);
      }
      return state;
    } catch (error) {
      if (error?.name !== 'AbortError') {
        _showError(error?.message || 'Could not activate this Study workspace.');
        _openControls({ trigger: _controlsTriggerTarget(), focusError: true });
      }
      return null;
    } finally {
      _busyMessage = '';
      _setBusy(false);
    }
  })();
  _initializingPrompt.set(requestKey, operation);
  try {
    return await operation;
  } finally {
    if (_initializingPrompt.get(requestKey) === operation) _initializingPrompt.delete(requestKey);
  }
}

function _queueMutation(operation) {
  const queued = _mutationChain.then(operation, operation);
  _mutationChain = queued.catch(() => {});
  return queued;
}

function _lifecycleIsCurrent(generation, { requireActive = false } = {}) {
  return generation === _lifecycleGeneration && !_closing && (!requireActive || _active);
}

function _invalidatePendingWorkspaceTransition() {
  const transition = _pendingWorkspaceTransition;
  if (!transition?.sessionId) return;
  _suppressedWorkspaceOpens.set(String(transition.sessionId), transition.generation);
  _pendingWorkspaceTransition = null;
  setTimeout(() => {
    if (_suppressedWorkspaceOpens.get(String(transition.sessionId)) === transition.generation) {
      _suppressedWorkspaceOpens.delete(String(transition.sessionId));
    }
  }, REQUEST_TIMEOUT_MS * 3);
}

async function _loadState({ syncForm = true, sessionId = _activeSessionId } = {}) {
  if (_busy || _closing) return false;
  const workspaceId = _sessionId(sessionId);
  if (!workspaceId) {
    _showError('Choose or create a Study workspace first.');
    return false;
  }
  if (_stateController) _stateController.abort();
  const controller = new AbortController();
  _stateController = controller;
  _setBusy(true);
  _showError('');
  try {
    const state = await _request('/state', { signal: controller.signal }, workspaceId);
    if (!_applyState(state, { syncForm, sessionId: workspaceId })) return false;
    _lastResyncMs = _monotonicNow();
    return true;
  } catch (error) {
    if (error && error.name !== 'AbortError') _showError(error.message || 'Could not load Study Mode.');
    return false;
  } finally {
    if (_stateController === controller) _stateController = null;
    _setBusy(false);
  }
}

async function _timerAction(action) {
  if (_busy || _closing) return;
  const workspaceId = _sessionId();
  if (!workspaceId) return;
  const before = _liveState();
  _setBusy(true);
  _showError('');
  try {
    const state = await _queueMutation(() => _request(`/timer/${action}`, { method: 'POST' }, workspaceId));
    if (!_applyState(state, { syncForm: false, sessionId: workspaceId })) return;
    if (action === 'start') _announce('Focus timer started.');
    if (action === 'pause') _announce('Focus timer paused.');
    if (action === 'finish') {
      const added = Math.max(0, _seconds(state.total_seconds) - _seconds(before.total_seconds));
      _announce(added ? `Study block finished. ${_humanTime(added)} added to your effort total.` : 'Study block finished.');
    }
  } catch (error) {
    if (error && error.name !== 'AbortError') _showError(error.message || `Could not ${action} the timer.`);
  } finally {
    _setBusy(false);
  }
}

async function _recordReview(outcome) {
  const result = String(outcome || '').trim().toLowerCase();
  if (_busy || _closing || !['missed', 'hinted', 'clean', 'transfer'].includes(result)) return;
  const workspaceId = _sessionId();
  if (!workspaceId) return;
  _setBusy(true);
  _showError('');
  try {
    const state = await _queueMutation(() => _request('/review', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ outcome: result }),
    }, workspaceId));
    if (!_applyState(state, { syncForm: false, sessionId: workspaceId })) return;
    const labels = {
      missed: 'Missed recorded. Restia will bring this back soon.',
      hinted: 'Hint-assisted success recorded. A near-term review is scheduled.',
      clean: 'Hint-free success recorded. The retrieval interval expanded.',
      transfer: 'Transfer evidence recorded. The retrieval interval expanded.',
    };
    _announce(labels[result]);
  } catch (error) {
    if (error && error.name !== 'AbortError') {
      _showError(error.message || 'Could not record mastery evidence.');
    }
  } finally {
    _setBusy(false);
  }
}

async function _saveGoal(event) {
  if (event) event.preventDefault();
  if (_busy || _closing) return;
  const workspaceId = _sessionId();
  if (!workspaceId) return;
  const goal = String(_elements['study-goal-text']?.value || '').trim();
  const hours = _number(_elements['study-target-hours']?.value, NaN);
  const targetMinutes = Math.round(hours * 60);
  const targetDate = String(_elements['study-target-date']?.value || '').trim() || null;
  const liveBeforeSave = _liveState();
  const goalChanged = String(_state.goal_text || '').trim() !== goal;
  let resetProgress = false;

  if (!goal) {
    _showError('Describe the learning goal before saving.');
    _elements['study-goal-text']?.focus();
    return;
  }
  if (!Number.isFinite(hours) || targetMinutes < 15 || targetMinutes > 525600) {
    _showError('Set a study-time target between 15 minutes and 8,760 hours.');
    _elements['study-target-hours']?.focus();
    return;
  }
  if (goalChanged && (liveBeforeSave.studied_seconds > 0 || liveBeforeSave.timer_running)) {
    const confirmed = window.confirm(
      'Starting a new learning goal will reset the study time logged for the current goal. Continue?',
    );
    if (!confirmed) return;
    resetProgress = true;
  }

  _setBusy(true);
  _showError('');
  try {
    const state = await _queueMutation(() => _request('/goal', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        goal_text: goal,
        target_minutes: targetMinutes,
        target_date: targetDate,
        reset_progress: resetProgress,
      }),
    }, workspaceId));
    if (!_applyState(state, { syncForm: true, sessionId: workspaceId })) return;
    _announce(resetProgress
      ? 'New study goal saved. Previous goal effort was reset.'
      : 'Study goal saved. Progress is based on focused time, not assumed mastery.');
  } catch (error) {
    if (error && error.name !== 'AbortError') _showError(error.message || 'Could not save the study goal.');
  } finally {
    _setBusy(false);
  }
}

function _syncGoalDirty() {
  const goal = String(_elements['study-goal-text']?.value || '').trim();
  const hours = _number(_elements['study-target-hours']?.value, NaN);
  const targetMinutes = Number.isFinite(hours) ? Math.round(hours * 60) : NaN;
  const targetDate = String(_elements['study-target-date']?.value || '').trim() || null;
  _goalDirty = (
    goal !== String(_state.goal_text || '').trim()
    || targetMinutes !== _seconds(_state.target_minutes)
    || targetDate !== (_state.target_date || null)
  );
  _renderLive();
}

async function _askWorkspaceName({ current = '', create = false } = {}) {
  const fallback = current || `Study ${Math.max(1, _studySessions().length + 1)}`;
  if (typeof _options.styledPrompt === 'function') {
    return _options.styledPrompt(
      create
        ? 'Use a short subject or outcome so this workspace is easy to return to.'
        : 'Rename this Study workspace without changing its goal or progress.',
      {
        title: create ? 'New Study workspace' : 'Rename Study workspace',
        defaultValue: fallback,
        placeholder: 'e.g. Controls interview prep',
        confirmText: create ? 'Create' : 'Rename',
        maxLength: 80,
      },
    );
  }
  if (typeof window.prompt === 'function') return window.prompt('Study workspace name', fallback);
  return fallback;
}

async function _createWorkspace({ askName = true } = {}) {
  if (_busy || _closing || typeof _options.createStudySession !== 'function') return null;
  const generation = _lifecycleGeneration;
  const startedWhileActive = _active;
  const suggested = `Study ${Math.max(1, _studySessions().length + 1)}`;
  const name = askName ? await _askWorkspaceName({ current: suggested, create: true }) : suggested;
  if (name == null || !String(name).trim()) return null;
  if (!_lifecycleIsCurrent(generation, { requireActive: startedWhileActive })) return null;

  _setBusy(true);
  _showError('');
  let created = null;
  let transition = null;
  try {
    created = await _withTimeout(
      () => _options.createStudySession(String(name).trim()),
      'Creating the Study workspace timed out. Check the connection and try again.',
    );
    if (!_lifecycleIsCurrent(generation, { requireActive: startedWhileActive })) return null;
    _renderWorkspaces();
    if (created?.id && typeof _options.selectSession === 'function') {
      transition = { generation, sessionId: String(created.id) };
      _pendingWorkspaceTransition = transition;
      const selected = await _withTimeout(
        () => _options.selectSession(created.id, { keepSidebar: true, showLoading: false }),
        'The workspace was created, but opening it timed out. Choose it from the workspace list to continue.',
      );
      if (!_lifecycleIsCurrent(generation, { requireActive: startedWhileActive })) return null;
      if (selected === false) throw new Error('The new workspace was created but could not be opened.');
      const opened = await open({ sessionId: created.id, focus: false, refresh: true, fromSession: true });
      if (!_lifecycleIsCurrent(generation, { requireActive: true })) return null;
      if (!opened) throw new Error('The new workspace was created but could not be activated.');
    }
    if (created?.id) _announce(`${String(name).trim()} created with separate chat, goal, and progress.`);
    return created;
  } catch (error) {
    _showError(error?.message || 'Could not create a Study workspace.');
    if (_lifecycleIsCurrent(generation)) {
      _openControls({ trigger: _controlsTriggerTarget(), focusError: true });
    }
    return null;
  } finally {
    if (_pendingWorkspaceTransition === transition) _pendingWorkspaceTransition = null;
    _setBusy(false);
  }
}

async function _renameWorkspace() {
  const workspaceId = _sessionId();
  if (!workspaceId || _busy || _closing) return;
  const generation = _lifecycleGeneration;
  const current = _workspaceName(workspaceId);
  const name = await _askWorkspaceName({ current, create: false });
  const cleaned = String(name || '').trim();
  if (!cleaned || cleaned === current) return;
  if (!_lifecycleIsCurrent(generation, { requireActive: true })) return;

  _setBusy(true);
  _showError('');
  try {
    const fd = new FormData();
    fd.append('name', cleaned);
    const origin = typeof window !== 'undefined' ? window.location.origin : '';
    const response = await _fetchWithTimeout(`${origin}/api/session/${encodeURIComponent(workspaceId)}`, {
      method: 'PATCH', body: fd, credentials: 'same-origin',
    }, 'Renaming the Study workspace timed out. Check the connection and try again.');
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || `Could not rename workspace (${response.status})`);
    }
    if (!_lifecycleIsCurrent(generation, { requireActive: true })) return;
    _state = {
      ..._state,
      workspace_name: cleaned,
      tracker: {
        ...(_state.tracker || {}),
        active_workspace: {
          ...(_state.tracker?.active_workspace || {}),
          session_id: workspaceId,
          title: cleaned,
          mode: 'study',
        },
      },
    };
    _renderWorkspaces();
    _renderLive();
    _announce(`Study workspace renamed to ${cleaned}.`);
    try {
      await _refreshSessionList();
    } catch (error) {
      console.error('Study workspace renamed, but the session list could not refresh:', error);
      _showError('The workspace was renamed, but refreshing the workspace list timed out. Reload the page to update it.');
    }
  } catch (error) {
    _showError(error?.message || 'Could not rename the Study workspace.');
  } finally {
    _setBusy(false);
  }
}

async function _switchWorkspace(event) {
  const workspaceId = _sessionId(event?.target?.value);
  if (!workspaceId || workspaceId === _sessionId() || _busy || _closing) return;
  if (typeof _options.selectSession !== 'function') return;
  const generation = _lifecycleGeneration;
  const transition = { generation, sessionId: workspaceId };
  _pendingWorkspaceTransition = transition;
  _setBusy(true);
  _showError('');
  try {
    const selected = await _withTimeout(
      () => _options.selectSession(workspaceId, { keepSidebar: true }),
      'Switching Study workspaces timed out. Check the connection and try again.',
    );
    if (!_lifecycleIsCurrent(generation, { requireActive: true })) return;
    if (selected === false) {
      _renderWorkspaces();
      throw new Error('Could not switch Study workspaces.');
    }
    await open({ sessionId: workspaceId, focus: false, refresh: true, fromSession: true });
  } catch (error) {
    _showError(error?.message || 'Could not switch Study workspaces.');
  } finally {
    if (_pendingWorkspaceTransition === transition) _pendingWorkspaceTransition = null;
    _setBusy(false);
  }
}

function _fillStudyPrompt(button) {
  const prompt = String(button?.dataset?.studyPrompt || '').trim();
  const composer = _get('message');
  if (!prompt || !composer) return;
  composer.value = prompt;
  composer.dispatchEvent(new Event('input', { bubbles: true }));
  _closeControls({ restoreFocus: false });
  try { composer.focus({ preventScroll: false }); } catch (_) { try { composer.focus(); } catch (_) {} }
  _announce(`${String(button.textContent || 'Study move').trim()} ready. Edit it or send when ready.`);
}

function _focusableControls() {
  const dialog = _elements['study-control-dialog'];
  if (!dialog?.querySelectorAll) return [];
  return Array.from(dialog.querySelectorAll(
    'button:not([disabled]), select:not([disabled]), textarea:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
  )).filter(element => !element.hidden && element.getAttribute?.('aria-hidden') !== 'true');
}

function _setControlsBackgroundInert(enabled) {
  if (!enabled) {
    _inertBackground.forEach(({ element, inert }) => {
      if (element) element.inert = inert;
    });
    _inertBackground = [];
    return;
  }
  if (_inertBackground.length) return;
  const modal = _elements['study-control-modal'];
  let branch = modal;
  while (branch?.parentElement) {
    const parent = branch.parentElement;
    Array.from(parent.children || []).forEach(element => {
      if (element === branch || element === modal) return;
      _inertBackground.push({ element, inert: Boolean(element.inert) });
      element.inert = true;
    });
    if (parent === document.body) break;
    branch = parent;
  }
}

function _openControls({ trigger = null, focusError = false } = {}) {
  const modal = _elements['study-control-modal'];
  const dialog = _elements['study-control-dialog'];
  if (!modal || !dialog) return false;
  if (!_controlsOpen) {
    _controlsTrigger = trigger || document.activeElement || _elements['study-controls-open'];
  }
  _controlsOpen = true;
  modal.hidden = false;
  modal.setAttribute('aria-hidden', 'false');
  _elements['study-controls-open']?.setAttribute('aria-expanded', 'true');
  _setControlsBackgroundInert(true);
  document.body?.classList?.add('study-controls-open');
  if (!_goalDirty) _renderForm();
  _renderWorkspaces();
  _renderLive();
  const focusTarget = focusError && !_elements['study-error']?.hidden
    ? _elements['study-error']
    : (_focusableControls()[0] || dialog);
  const focusDialog = () => {
    if (!_controlsOpen) return;
    if (focusTarget === _elements['study-error'] && !focusTarget.hasAttribute?.('tabindex')) {
      focusTarget.setAttribute?.('tabindex', '-1');
    }
    try { focusTarget?.focus({ preventScroll: true }); }
    catch (_) { try { dialog.focus(); } catch (_) {} }
  };
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(focusDialog);
  else focusDialog();
  return true;
}

function _closeControls({ restoreFocus = true } = {}) {
  const modal = _elements['study-control-modal'];
  if (!modal || !_controlsOpen) return false;
  _controlsOpen = false;
  _setControlsBackgroundInert(false);
  modal.hidden = true;
  modal.setAttribute('aria-hidden', 'true');
  _elements['study-controls-open']?.setAttribute('aria-expanded', 'false');
  document.body?.classList?.remove('study-controls-open');
  if (restoreFocus) {
    const target = _controlsTrigger?.isConnected === false
      ? _elements['study-controls-open']
      : (_controlsTrigger || _elements['study-controls-open']);
    try { target?.focus({ preventScroll: true }); } catch (_) { try { target?.focus(); } catch (_) {} }
  }
  _controlsTrigger = null;
  return true;
}

function _handleControlsKeydown(event) {
  if (!_controlsOpen) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    _closeControls();
    return;
  }
  if (event.key !== 'Tab') return;
  const focusable = _focusableControls();
  if (!focusable.length) {
    event.preventDefault();
    _elements['study-control-dialog']?.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function _syncDrawerClearance() {
  const panel = _elements['study-panel'];
  if (!panel || !_active) return;
  const viewportWidth = _number(window.innerWidth, 0);
  if (viewportWidth > 1100) {
    panel.style.removeProperty('--study-drawer-bottom');
    panel.style.removeProperty('--study-drawer-right');
    return;
  }
  let rightClearance = 12;
  for (const navigation of [_get('sidebar'), _get('icon-rail')]) {
    if (!navigation?.classList?.contains('right-side')) continue;
    if (navigation.classList.contains('hidden') || navigation.classList.contains('rail-hidden')) continue;
    const navRect = navigation.getBoundingClientRect?.();
    if (navRect && navRect.width > 0 && navRect.right >= viewportWidth - 2) {
      rightClearance = Math.max(rightClearance, Math.ceil(navRect.width + 12));
    }
  }
  panel.style.setProperty('--study-drawer-right', `${rightClearance}px`);
  const composer = document.querySelector('.chat-input-bar');
  const viewportHeight = _number(window.innerHeight, document.documentElement?.clientHeight || 0);
  const rect = composer?.getBoundingClientRect?.();
  if (!rect || rect.top <= 0 || rect.top >= viewportHeight) {
    panel.style.removeProperty('--study-drawer-bottom');
    return;
  }
  const clearance = Math.max(12, Math.ceil(viewportHeight - rect.top + 12));
  panel.style.setProperty('--study-drawer-bottom', `${clearance}px`);
}

function _refreshAuthoritativeState() {
  if (!_active || _busy || _closing || document.visibilityState === 'hidden') return;
  const now = _monotonicNow();
  if (_lastResyncMs && now - _lastResyncMs < 1000) return;
  void _loadState({ syncForm: !_goalDirty });
}

function _setCollapsed(value) {
  _collapsed = Boolean(value);
  const panel = _elements['study-panel'];
  const body = _elements['study-panel-body'];
  const button = _elements['study-panel-collapse'];
  if (panel) panel.classList.toggle('study-panel-collapsed', _collapsed);
  if (body) body.setAttribute('aria-hidden', String(_collapsed));
  if (button) {
    button.setAttribute('aria-expanded', String(!_collapsed));
    button.setAttribute('aria-label', _collapsed ? 'Expand Study panel' : 'Collapse Study panel');
    button.title = _collapsed ? 'Expand Study panel' : 'Collapse Study panel';
  }
}

function _forceChatMode() {
  try {
    if (typeof window.__odysseusSetChatMode === 'function') window.__odysseusSetChatMode('chat');
    else document.getElementById('mode-chat-btn')?.click();
  } catch (_) {}
  try {
    if (typeof window._syncResearchIndicator === 'function') window._syncResearchIndicator(false);
    else {
      const research = document.getElementById('research-toggle');
      if (research) research.checked = false;
      document.getElementById('research-toggle-btn')?.classList.remove('active');
      document.getElementById('overflow-research-btn')?.classList.remove('active');
      document.getElementById('tool-research-btn')?.classList.remove('active');
    }
  } catch (_) {}
}

function _activateNavigation(active) {
  const tool = _get('tool-study-btn');
  const rail = _get('rail-study');
  if (tool) {
    tool.classList.toggle('active', active);
    tool.setAttribute('aria-pressed', String(active));
  }
  if (rail) {
    rail.classList.toggle('active-section', active);
    rail.setAttribute('aria-pressed', String(active));
  }
}

function _enterChatShell() {
  const composer = _get('message');
  const welcome = _get('welcome-screen');
  const chat = _get('chat-container');
  if (!_previousChatUi) {
    _previousChatUi = {
      placeholder: composer?.getAttribute('placeholder'),
      ariaLabel: composer?.getAttribute('aria-label'),
      welcomeHidden: welcome?.classList.contains('hidden') || false,
      welcomeAriaHidden: welcome?.getAttribute('aria-hidden'),
      welcomeActive: chat?.classList.contains('welcome-active') || false,
    };
  }
  if (composer) {
    composer.placeholder = 'Ask for a derivation, explain your reasoning, or say “quiz me”.';
    composer.setAttribute('aria-label', 'Study question');
  }
  if (welcome) {
    welcome.classList.add('hidden');
    welcome.setAttribute('aria-hidden', 'true');
  }
  chat?.classList.remove('welcome-active');
}

function _restoreChatShell({ manual = false } = {}) {
  const composer = _get('message');
  const welcome = _get('welcome-screen');
  const chat = _get('chat-container');
  const historyHasMessages = Boolean(_get('chat-history')?.querySelector('.msg'));
  if (composer) {
    if (_previousChatUi?.placeholder == null) composer.removeAttribute('placeholder');
    else composer.setAttribute('placeholder', _previousChatUi.placeholder);
    if (_previousChatUi?.ariaLabel == null) composer.removeAttribute('aria-label');
    else composer.setAttribute('aria-label', _previousChatUi.ariaLabel);
  }
  if (welcome) {
    const shouldRestoreWelcome = manual || !historyHasMessages;
    welcome.classList.toggle('hidden', shouldRestoreWelcome ? Boolean(_previousChatUi?.welcomeHidden) : true);
    if (shouldRestoreWelcome && _previousChatUi?.welcomeAriaHidden != null) {
      welcome.setAttribute('aria-hidden', _previousChatUi.welcomeAriaHidden);
    } else if (shouldRestoreWelcome) {
      welcome.removeAttribute('aria-hidden');
    } else {
      welcome.setAttribute('aria-hidden', 'true');
    }
  }
  if (chat) {
    const shouldUseWelcomeLayout = (manual || !historyHasMessages) && Boolean(_previousChatUi?.welcomeActive);
    chat.classList.toggle('welcome-active', shouldUseWelcomeLayout);
  }
  _previousChatUi = null;
}

function _startFreshNormalChat() {
  const callback = _options.startFreshChat || _options.onManualClose;
  if (typeof callback === 'function') {
    try { callback(); } catch (error) { console.error('Study Mode fresh-chat callback failed:', error); }
    return;
  }
  const button = _get('rail-new-session') || _get('sidebar-new-chat-btn');
  if (button) button.click();
}

function _leaveStudyRoute() {
  if (typeof window === 'undefined' || window.location.pathname !== '/study') return;
  const nextUrl = `/${window.location.search || ''}${window.location.hash || ''}`;
  try {
    window.history.replaceState(window.history.state, '', nextUrl);
  } catch (error) {
    console.warn('Study Mode could not restore the normal chat URL:', error);
  }
}

function _sessionMode(detail) {
  const raw = detail || {};
  const session = raw.session || raw;
  let mode = raw.mode || raw.sessionMode || session.mode;
  const id = raw.sessionId || raw.id || session.id;
  if (!mode && id && typeof window !== 'undefined') {
    try {
      mode = window.sessionModule?.getSessions?.().find(item => String(item.id) === String(id))?.mode;
    } catch (_) {}
  }
  return typeof mode === 'string' ? mode.toLowerCase() : '';
}

function _onSessionSelected(event) {
  const mode = _sessionMode(event?.detail);
  const raw = event?.detail || {};
  const session = raw.session || raw;
  const sessionId = _sessionId(raw.sessionId || raw.id || session.id);
  const suppressedGeneration = _suppressedWorkspaceOpens.get(sessionId);
  if (suppressedGeneration != null && suppressedGeneration !== _lifecycleGeneration) {
    _suppressedWorkspaceOpens.delete(sessionId);
    return;
  }
  if (mode === 'study') {
    void open({ focus: false, refresh: true, fromSession: true, sessionId });
  } else if (mode && _active) {
    void close({ manual: false, startFresh: false });
  }
}

function _wireEvents() {
  _elements['study-panel-collapse']?.addEventListener('click', () => _setCollapsed(!_collapsed));
  _elements['study-panel-close']?.addEventListener('click', () => void close({ manual: true }));
  _elements['study-timer-start']?.addEventListener('click', () => void _timerAction('start'));
  _elements['study-timer-pause']?.addEventListener('click', () => void _timerAction('pause'));
  _elements['study-timer-finish']?.addEventListener('click', () => void _timerAction('finish'));
  _elements['study-workspace-switcher']?.addEventListener('change', event => void _switchWorkspace(event));
  _elements['study-new-workspace']?.addEventListener('click', () => void _createWorkspace({ askName: true }));
  _elements['study-rename-workspace']?.addEventListener('click', () => void _renameWorkspace());
  _elements['study-controls-open']?.addEventListener('click', event => {
    _openControls({ trigger: event.currentTarget });
  });
  _elements['study-controls-close']?.addEventListener('click', () => _closeControls());
  _elements['study-control-modal']?.querySelectorAll?.('[data-study-modal-close]').forEach(element => {
    element.addEventListener('click', () => _closeControls());
  });
  const quickActions = _elements['study-quick-actions'];
  if (quickActions?.querySelectorAll) {
    quickActions.querySelectorAll('[data-study-prompt]').forEach(button => {
      button.addEventListener('click', () => _fillStudyPrompt(button));
    });
  }
  const reviewActions = _elements['study-review-actions'];
  if (reviewActions?.querySelectorAll) {
    reviewActions.querySelectorAll('[data-study-result]').forEach(button => {
      button.addEventListener('click', () => void _recordReview(button.dataset.studyResult));
    });
  }
  _elements['study-goal-form']?.addEventListener('submit', event => void _saveGoal(event));
  ['study-goal-text', 'study-target-hours', 'study-target-date'].forEach(id => {
    _elements[id]?.addEventListener('input', _syncGoalDirty);
    _elements[id]?.addEventListener('change', _syncGoalDirty);
  });
  window.addEventListener('restia:session-selected', _onSessionSelected);
  window.addEventListener('focus', _refreshAuthoritativeState);
  window.addEventListener('resize', _syncDrawerClearance);
  document.addEventListener('visibilitychange', _refreshAuthoritativeState);
  document.addEventListener('keydown', _handleControlsKeydown);
  if (typeof ResizeObserver !== 'undefined') {
    const composer = document.querySelector('.chat-input-bar');
    if (composer) {
      _composerObserver = new ResizeObserver(_syncDrawerClearance);
      _composerObserver.observe(composer);
    }
  }
}

export function init(apiBase, options = {}) {
  if (apiBase && typeof apiBase === 'object') {
    options = apiBase;
    apiBase = options.apiBase;
  }
  API_BASE = _normalizeApiBase(apiBase);
  _options = { ..._options, ...(options || {}) };
  if (_initialized) return true;
  if (typeof document === 'undefined' || typeof window === 'undefined') return false;
  if (!_cacheElements()) {
    console.error('Study Mode could not initialize: #study-panel is missing.');
    return false;
  }

  const panel = _elements['study-panel'];
  panel.setAttribute('aria-labelledby', 'study-panel-title');
  panel.setAttribute('aria-hidden', 'true');
  const modal = _elements['study-control-modal'];
  if (modal) {
    modal.hidden = true;
    modal.setAttribute('aria-hidden', 'true');
  }
  if (_elements['study-chat-launcher']) _elements['study-chat-launcher'].hidden = true;
  _elements['study-controls-open']?.setAttribute('aria-expanded', 'false');
  _elements['study-timer']?.setAttribute('aria-live', 'off');
  const live = _elements['study-live-status'];
  if (live) {
    live.setAttribute('role', 'status');
    live.setAttribute('aria-live', 'polite');
    live.setAttribute('aria-atomic', 'true');
  }
  if (_elements['study-method'] && !_elements['study-method'].textContent.trim()) {
    _elements['study-method'].textContent = 'Feynman loop: explain simply, retrieve from memory, expose the gap, rebuild from first principles, then apply.';
  }
  _wireEvents();
  _setCollapsed(false);
  _applyState(EMPTY_STATE, { syncForm: true });
  _renderWorkspaces();
  _initialized = true;
  window.studyModule = studyModule;
  return true;
}

export async function open(options = {}) {
  if (!_initialized && !init(options.apiBase, options)) return false;
  if (_closePromise) await _closePromise;
  const requestedId = _sessionId(options.sessionId || _currentSessionId() || _activeSessionId);
  if (!requestedId) {
    _showError('Create a Study workspace before opening Study Mode.');
    _openControls({ trigger: _controlsTriggerTarget(), focusError: true });
    return false;
  }
  if (_active) {
    const changed = requestedId !== _sessionId(_activeSessionId);
    if (changed) {
      if (_stateController) _stateController.abort();
      _setActiveSessionId(requestedId);
      _applyState(EMPTY_STATE, { syncForm: true, sessionId: requestedId });
    }
    const initialized = await _initializeWorkspace({ sessionId: requestedId, syncForm: !_goalDirty });
    if (options.focus !== false) focus();
    return Boolean(initialized);
  }

  _setActiveSessionId(requestedId);
  _active = true;
  window.__restiaStudyModeActive = true;
  document.body.classList.add('study-view');
  const panel = _elements['study-panel'];
  panel.hidden = false;
  panel.setAttribute('aria-hidden', 'false');
  if (_elements['study-chat-launcher']) _elements['study-chat-launcher'].hidden = false;
  _activateNavigation(true);
  _enterChatShell();
  _forceChatMode();
  _setCollapsed(_collapsed);
  _syncDrawerClearance();
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(_syncDrawerClearance);
  _renderWorkspaces();
  const initialized = await _initializeWorkspace({ sessionId: requestedId, syncForm: true });
  if (options.focus !== false) focus();
  return Boolean(initialized);
}

/** Enter the current/recent Study workspace, creating the first one if needed. */
export async function enter(options = {}) {
  if (!_initialized && !init(options.apiBase, options)) return false;
  const currentId = _currentSessionId();
  const current = _studySessions().find(session => String(session.id) === currentId);
  let workspaceId = current ? currentId : _sessionId(_studySessions()[0]?.id);
  if (!workspaceId) {
    const created = await _createWorkspace({ askName: false });
    workspaceId = _sessionId(created?.id);
    if (!workspaceId) return false;
  } else if (currentId !== workspaceId && typeof _options.selectSession === 'function') {
    const selected = await _withTimeout(
      () => _options.selectSession(workspaceId, { keepSidebar: true, showLoading: false }),
      'Opening the Study workspace timed out. Check the connection and try again.',
    );
    if (selected === false) return false;
  }
  return open({ ...options, sessionId: workspaceId });
}

/** Pause the old workspace before sessions.js commits a navigation. */
export async function beforeSessionSwitch(targetId, targetMode) {
  if (!_active) return true;
  const previousId = _sessionId(_activeSessionId);
  if (!previousId || previousId === _sessionId(targetId)) return true;
  if (String(targetMode || '').toLowerCase() !== 'study') {
    return close({ manual: false, startFresh: false });
  }

  _setBusy(true);
  _showError('');
  try {
    const paused = await _queueMutation(
      () => _request('/timer/pause', { method: 'POST' }, previousId),
    );
    _applyState(paused, { syncForm: false, sessionId: previousId });
    return true;
  } catch (error) {
    _showError(error?.message || 'Could not pause the current Study workspace.');
    // Navigation must remain recoverable. Initializing the destination also
    // pauses every other owned Study timer on the server.
    console.warn('Study Mode could not pause before switching; continuing safely:', error);
    return true;
  } finally {
    _setBusy(false);
  }
}

async function _close(options = {}) {
  if (typeof options === 'boolean') options = { manual: options };
  const manual = Boolean(options.manual);
  const startFresh = manual
    && options.startFresh !== false
    && _options.startFreshOnManualClose !== false;
  _lifecycleGeneration += 1;
  _invalidatePendingWorkspaceTransition();
  _closing = true;
  const closingSessionId = _sessionId(_activeSessionId);
  if (_stateController) {
    _stateController.abort();
    _stateController = null;
  }
  _setBusy(true);
  _showError('');

  // Mutations are deliberately serialized. If Close races with Start, this
  // pause runs only after Start has resolved, so the server can never be left
  // with an invisible timer that continues counting outside Study Mode.
  try {
    const paused = closingSessionId
      ? await _queueMutation(
        () => _request('/timer/pause', { method: 'POST' }, closingSessionId),
      )
      : null;
    if (paused) _applyState(paused, { syncForm: false, sessionId: closingSessionId });
  } catch (error) {
    const message = error?.message || 'Could not pause the focus timer while leaving Study Mode.';
    console.error('Study Mode close failed to pause the timer:', error);
    _showError(message);
    try {
      window.uiModule?.showToast?.(
        `${message} Restia will reconcile the timer when Study Mode opens again.`,
        6000,
      );
    } catch (_) {}
    // Do not trap the user in Study Mode. The request is bounded, and the
    // next Study initialization reconciles timers by pausing all others.
  }

  _active = false;
  if (typeof window !== 'undefined') window.__restiaStudyModeActive = false;
  _setActiveSessionId(null);
  document.body.classList.remove('study-view');
  const panel = _elements['study-panel'];
  if (panel) {
    panel.hidden = true;
    panel.setAttribute('aria-hidden', 'true');
    panel.setAttribute('aria-busy', 'false');
  }
  if (_elements['study-chat-launcher']) _elements['study-chat-launcher'].hidden = true;
  _closeControls({ restoreFocus: false });
  _stopTicker();
  _closing = false;
  _busyDepth = 1;
  _setBusy(false);
  _activateNavigation(false);
  _restoreChatShell({ manual });
  _leaveStudyRoute();
  if (startFresh) _startFreshNormalChat();
  return true;
}

export function close(options = {}) {
  if (_closePromise) return _closePromise;
  if (!_active) return Promise.resolve(false);
  _closePromise = _close(options).finally(() => {
    _closePromise = null;
  });
  return _closePromise;
}

export function isActive() {
  return _active;
}

export function focus() {
  if (!_active) return false;
  if (_collapsed) _setCollapsed(false);
  const target = _state.goal_text ? _get('message') : _elements['study-goal-text'];
  try { target?.focus({ preventScroll: false }); } catch (_) { try { target?.focus(); } catch (_) {} }
  return Boolean(target);
}

/** Save the first substantive prompt before the chat request builds tutor context. */
export async function prepareFirstPrompt(prompt, { sessionId = null } = {}) {
  const workspaceId = _sessionId(sessionId || _currentSessionId() || _activeSessionId);
  const isStudyWorkspace = _studySessions().some(session => String(session.id) === workspaceId);
  if (!workspaceId || (!isStudyWorkspace && workspaceId !== _sessionId(_activeSessionId))) return true;
  const state = await _initializeWorkspace({ prompt, sessionId: workspaceId, syncForm: !_goalDirty });
  return Boolean(state);
}

/** Apply the backend's stream-time initialization event without touching another open chat. */
export async function applyServerInitialization(payload, sessionId = null) {
  const workspaceId = _sessionId(sessionId || payload?.session_id);
  if (!workspaceId || !payload || typeof payload !== 'object') return false;
  if (workspaceId === _sessionId(_activeSessionId)) {
    _applyState(payload, { syncForm: !_goalDirty, sessionId: workspaceId });
    _lastResyncMs = _monotonicNow();
  }
  if (payload.title_initialized && typeof _options.reloadSessions === 'function') {
    try {
      await _refreshSessionList();
    } catch (error) {
      console.error('Study goal saved, but the session list could not refresh:', error);
      _showError('Your Study goal was saved, but refreshing the workspace list timed out. Reload the page to update it.');
    }
  }
  return true;
}

const studyModule = {
  init,
  enter,
  open,
  close,
  beforeSessionSwitch,
  prepareFirstPrompt,
  applyServerInitialization,
  isActive,
  focus,
};

export default studyModule;
