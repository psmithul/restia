// static/js/study.js
// Study Mode goal, effort timer, and panel lifecycle.

const EMPTY_STATE = Object.freeze({
  goal_text: '',
  target_minutes: 0,
  target_date: null,
  timer_running: false,
  timer_seconds: 0,
  total_seconds: 0,
  studied_seconds: 0,
  remaining_seconds: 0,
  progress_percent: 0,
});

let API_BASE = '';
let _initialized = false;
let _active = false;
let _collapsed = false;
let _state = { ...EMPTY_STATE };
let _syncClockMs = 0;
let _ticker = null;
let _requestController = null;
let _busy = false;
let _elements = {};
let _previousChatUi = null;
let _options = {};

const ids = [
  'study-panel', 'study-panel-body', 'study-panel-title',
  'study-panel-collapse', 'study-panel-close',
  'study-timer', 'study-timer-status', 'study-timer-start',
  'study-timer-pause', 'study-timer-finish', 'study-session-total',
  'study-goal-form', 'study-goal-text', 'study-target-hours',
  'study-target-date', 'study-goal-save', 'study-progress-bar',
  'study-progress-value', 'study-progress-copy', 'study-deadline-copy',
  'study-error', 'study-live-status', 'study-method',
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
    goal_text: String(state.goal_text || ''),
    target_minutes: targetMinutes,
    target_date: state.target_date || null,
    timer_running: Boolean(state.timer_running),
    timer_seconds: timerSeconds,
    total_seconds: totalSeconds,
    studied_seconds: studiedSeconds,
    remaining_seconds: remainingSeconds,
    progress_percent: Math.round(_percent(progressPercent) * 10) / 10,
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
  const error = _elements['study-error'];
  if (!error) return;
  const text = String(message || '').trim();
  error.textContent = text;
  error.hidden = !text;
  error.setAttribute('role', 'alert');
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
  if (start) start.disabled = _busy || live.timer_running || !live.goal_text || !live.target_minutes;
  if (pause) pause.disabled = _busy || !live.timer_running;
  if (finish) finish.disabled = _busy || live.timer_seconds <= 0;
  if (save) save.disabled = _busy;
}

function _setBusy(value) {
  _busy = Boolean(value);
  const panel = _elements['study-panel'];
  if (panel) panel.setAttribute('aria-busy', String(_busy));
  _syncControls();
}

function _renderLive() {
  const live = _liveState();
  _setText(_elements['study-timer'], formatDuration(live.timer_seconds));
  _setText(
    _elements['study-timer-status'],
    !live.goal_text
      ? 'Set a learning goal to start'
      : (live.timer_running ? 'Focus timer running' : (live.timer_seconds ? 'Focus timer paused' : 'Ready to study')),
  );
  _setText(_elements['study-session-total'], _humanTime(live.total_seconds));

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
  _syncControls(live);
}

function _renderForm() {
  const goal = _elements['study-goal-text'];
  const hours = _elements['study-target-hours'];
  const date = _elements['study-target-date'];
  if (goal) goal.value = _state.goal_text || '';
  if (hours) hours.value = _hoursValue(_state.target_minutes);
  if (date) date.value = _state.target_date || '';
}

function _applyState(payload, { syncForm = false } = {}) {
  _state = deriveLiveProgress(payload || EMPTY_STATE, 0);
  _syncClockMs = _monotonicNow();
  if (syncForm) _renderForm();
  _renderLive();
  _syncTicker();
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

async function _request(path, options = {}) {
  if (_requestController) _requestController.abort();
  _requestController = new AbortController();
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'same-origin',
    ...options,
    signal: _requestController.signal,
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
}

async function _loadState() {
  _setBusy(true);
  _showError('');
  try {
    const state = await _request('/state');
    _applyState(state, { syncForm: true });
  } catch (error) {
    if (error && error.name !== 'AbortError') _showError(error.message || 'Could not load Study Mode.');
  } finally {
    _setBusy(false);
  }
}

async function _timerAction(action) {
  if (_busy) return;
  const before = _liveState();
  _setBusy(true);
  _showError('');
  try {
    const state = await _request(`/timer/${action}`, { method: 'POST' });
    _applyState(state, { syncForm: false });
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

async function _saveGoal(event) {
  if (event) event.preventDefault();
  if (_busy) return;
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
    const state = await _request('/goal', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        goal_text: goal,
        target_minutes: targetMinutes,
        target_date: targetDate,
        reset_progress: resetProgress,
      }),
    });
    _applyState(state, { syncForm: true });
    _announce(resetProgress
      ? 'New study goal saved. Previous goal effort was reset.'
      : 'Study goal saved. Progress is based on focused time, not assumed mastery.');
  } catch (error) {
    if (error && error.name !== 'AbortError') _showError(error.message || 'Could not save the study goal.');
  } finally {
    _setBusy(false);
  }
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
  if (mode === 'study') {
    void open({ focus: false, refresh: true, fromSession: true });
  } else if (mode && _active) {
    close({ manual: false, startFresh: false });
  }
}

function _wireEvents() {
  _elements['study-panel-collapse']?.addEventListener('click', () => _setCollapsed(!_collapsed));
  _elements['study-panel-close']?.addEventListener('click', () => close({ manual: true }));
  _elements['study-timer-start']?.addEventListener('click', () => void _timerAction('start'));
  _elements['study-timer-pause']?.addEventListener('click', () => void _timerAction('pause'));
  _elements['study-timer-finish']?.addEventListener('click', () => void _timerAction('finish'));
  _elements['study-goal-form']?.addEventListener('submit', event => void _saveGoal(event));
  window.addEventListener('restia:session-selected', _onSessionSelected);
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
  _initialized = true;
  window.studyModule = studyModule;
  return true;
}

export async function open(options = {}) {
  if (!_initialized && !init(options.apiBase, options)) return false;
  if (_active) {
    if (options.refresh) await _loadState();
    if (options.focus !== false) focus();
    return true;
  }

  _active = true;
  window.__restiaStudyModeActive = true;
  document.body.classList.add('study-view');
  const panel = _elements['study-panel'];
  panel.hidden = false;
  panel.setAttribute('aria-hidden', 'false');
  _activateNavigation(true);
  _enterChatShell();
  _forceChatMode();
  _setCollapsed(_collapsed);
  await _loadState();
  if (options.focus !== false) focus();
  return true;
}

export function close(options = {}) {
  if (typeof options === 'boolean') options = { manual: options };
  const manual = Boolean(options.manual);
  const startFresh = manual
    && options.startFresh !== false
    && _options.startFreshOnManualClose !== false;
  if (!_active) return false;

  _active = false;
  if (typeof window !== 'undefined') window.__restiaStudyModeActive = false;
  document.body.classList.remove('study-view');
  const panel = _elements['study-panel'];
  if (panel) {
    panel.hidden = true;
    panel.setAttribute('aria-hidden', 'true');
    panel.setAttribute('aria-busy', 'false');
  }
  _stopTicker();
  if (_requestController) {
    _requestController.abort();
    _requestController = null;
  }
  _busy = false;
  _activateNavigation(false);
  _restoreChatShell({ manual });
  _leaveStudyRoute();
  if (startFresh) _startFreshNormalChat();
  return true;
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

const studyModule = {
  init,
  open,
  close,
  isActive,
  focus,
};

export default studyModule;
