// Restia V2 Mission Control — one owner-scoped view of today's work.
// The backend owns aggregation; this module owns a resilient, accessible
// workspace that remains useful when any individual source is unavailable.

const SOURCE_KEYS = Object.freeze([
  'calendar', 'project_work', 'inbox', 'goals', 'tasks', 'study_reviews', 'health',
  'important_mail', 'notes_today', 'daily_brief', 'planning', 'progression',
  'proactive', 'recent_activity',
]);

let API_BASE = typeof window !== 'undefined' ? window.location.origin : '';
let refs = {};
let sequence = 0;
let controller = null;
let activityMoreController = null;
let focusController = null;
let actionController = null;
let focusTimer = null;
let previousFocus = null;
let focusRecoveryBound = false;

const state = {
  initialized: false,
  open: false,
  loading: false,
  error: '',
  data: null,
  view: 'home',
  activityLoadingMore: false,
  focusSession: null,
  focusLoading: false,
  focusError: '',
  focusView: false,
  focusReturnedToToday: false,
  focusSetup: null,
  focusResult: null,
  focusBusy: false,
  actionCenter: {
    loading: false,
    error: '',
    partial: false,
    pending: [],
    recent: [],
    reviewedKey: '',
    busyId: '',
    announcement: '',
  },
};

function text(value, fallback = '') {
  const normalized = String(value ?? '').trim();
  return normalized || fallback;
}

function number(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function rows(value) {
  return Array.isArray(value) ? value : [];
}

function object(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function make(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.className) element.className = options.className;
  if (options.text !== undefined) element.textContent = String(options.text);
  if (options.id) element.id = options.id;
  if (options.type) element.type = options.type;
  if (options.hidden) element.hidden = true;
  Object.entries(options.attrs || {}).forEach(([key, value]) => {
    if (value !== null && value !== undefined) element.setAttribute(key, String(value));
  });
  Object.entries(options.dataset || {}).forEach(([key, value]) => {
    if (value !== null && value !== undefined) element.dataset[key] = String(value);
  });
  const childRows = Array.isArray(children) ? children : [children];
  childRows.filter(Boolean).forEach((child) => element.appendChild(
    child instanceof Node ? child : document.createTextNode(String(child)),
  ));
  return element;
}

function button(label, action, {
  className = '', target = '', title = '', dataset = {}, attrs = {}, type = 'button',
} = {}) {
  return make('button', {
    type,
    className: className || 'mission-btn',
    text: label,
    attrs: { title: title || label, ...attrs },
    dataset: { action, target, ...dataset },
  });
}

function clear(element) {
  if (element) element.replaceChildren();
}

function ensureStylesheet() {
  if (document.getElementById('mission-control-css')) return;
  document.head.appendChild(make('link', {
    id: 'mission-control-css',
    attrs: { rel: 'stylesheet', href: '/static/mission-control.css' },
  }));
}

async function minimizeWorkspaceModals() {
  try {
    const manager = await import('./modalManager.js');
    return manager.minimizeVisibleModals?.() || 0;
  } catch (error) {
    console.warn('Could not minimize floating tools for workspace:', error);
    return 0;
  }
}

function localOffsetMinutes() {
  return Math.max(-840, Math.min(840, -new Date().getTimezoneOffset()));
}

function formatDate(value, options = {}) {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return text(value);
  try {
    return new Intl.DateTimeFormat(undefined, {
      month: 'short', day: 'numeric',
      ...(options.time ? { hour: 'numeric', minute: '2-digit' } : {}),
    }).format(parsed);
  } catch (_) {
    return parsed.toLocaleString();
  }
}

function formatClock(value) {
  if (!value) return '';
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return text(value);
  try {
    return new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' }).format(parsed);
  } catch (_) {
    return parsed.toLocaleTimeString();
  }
}

function formatDuration(value) {
  const total = Math.max(0, Math.floor(number(value)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return [hours, minutes, seconds]
    .map((part) => String(part).padStart(2, '0'))
    .join(':');
}

export function normalizeFocusTarget(value) {
  const raw = object(value);
  const kind = text(raw.kind);
  const id = text(raw.id);
  const version = Math.max(0, Math.trunc(number(raw.version)));
  if (!id || version < 1 || !['life_entity', 'planning_item'].includes(kind)) return null;
  return { kind, id, version };
}

export function normalizeFocusSession(value, receivedAt = Date.now()) {
  const raw = object(value);
  if (!text(raw.id)) return null;
  const entity = object(raw.entity);
  return {
    id: text(raw.id),
    entityId: text(raw.entity_id),
    state: text(raw.state, 'active'),
    definitionOfDone: text(raw.definition_of_done),
    startedAt: text(raw.started_at),
    activeSince: text(raw.active_since),
    pausedAt: text(raw.paused_at),
    completedAt: text(raw.completed_at),
    elapsedSeconds: Math.max(0, Math.floor(number(raw.elapsed_seconds))),
    interruptions: rows(raw.interruptions).slice(-100),
    progress: rows(raw.progress).slice(-100),
    evidence: rows(raw.evidence).slice(-100),
    followUpEntityIds: rows(raw.follow_up_entity_ids).map((id) => text(id)).filter(Boolean),
    context: rows(raw.context).slice(0, 12).map((item) => ({
      id: text(item?.id),
      type: text(item?.entity_type, 'context'),
      title: text(item?.title, 'Untitled context'),
      summary: text(item?.summary),
      relation: text(item?.relation, 'linked'),
      direction: text(item?.direction),
    })),
    entity: {
      id: text(entity.id ?? raw.entity_id),
      type: text(entity.entity_type, 'task'),
      title: text(entity.title, 'Focused work'),
      summary: text(entity.summary),
      status: text(entity.status, 'active'),
      properties: object(entity.properties),
      version: Math.max(1, Math.trunc(number(entity.version, 1))),
    },
    version: Math.max(1, Math.trunc(number(raw.version, 1))),
    receivedAt: Math.max(0, number(receivedAt, Date.now())),
  };
}

export function focusDisplayElapsed(session, now = Date.now()) {
  const normalized = object(session);
  const persisted = Math.max(0, Math.floor(number(normalized.elapsedSeconds)));
  if (text(normalized.state) !== 'active') return persisted;
  const receivedAt = Math.max(0, number(normalized.receivedAt, now));
  return persisted + Math.max(0, Math.floor((number(now, receivedAt) - receivedAt) / 1000));
}

const ACTION_PENDING_STATES = Object.freeze(['prepared', 'approved']);
const ACTION_RECENT_STATES = Object.freeze(['completed', 'reversed']);
const ACTION_SENSITIVE_KEY = /(^|_)(confirmation|token|secret|password|credential|cookie|authorization|digest|session)(_|$)/i;

function humanizeActionLabel(value, fallback = 'Unknown') {
  const clean = text(value, fallback).replace(/[_-]+/g, ' ').replace(/\s+/g, ' ').trim();
  return clean.replace(/\b\w/g, (letter) => letter.toUpperCase()).slice(0, 120);
}

function safeActionValue(value, depth = 0) {
  if (value === null || value === undefined) return '';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '';
  if (typeof value === 'string') return value.trim().slice(0, 800);
  if (depth >= 2) return '[Nested details]';
  if (Array.isArray(value)) {
    return value.slice(0, 12)
      .map((item) => safeActionValue(item, depth + 1))
      .filter(Boolean)
      .join(', ')
      .slice(0, 1200);
  }
  if (typeof value === 'object') {
    return Object.entries(value)
      .filter(([key]) => !ACTION_SENSITIVE_KEY.test(String(key)))
      .slice(0, 16)
      .map(([key, item]) => {
        const rendered = safeActionValue(item, depth + 1);
        return rendered ? `${humanizeActionLabel(key)}: ${rendered}` : '';
      })
      .filter(Boolean)
      .join(' · ')
      .slice(0, 1600);
  }
  return '';
}

function safeActionRows(value, limit = 12) {
  return Object.entries(object(value))
    .filter(([key]) => !ACTION_SENSITIVE_KEY.test(String(key)))
    .slice(0, limit)
    .map(([key, item]) => ({
      label: humanizeActionLabel(key),
      value: safeActionValue(item),
    }))
    .filter((item) => item.value);
}

function safeActionDetailRows(value, limit = 12) {
  return rows(value)
    .slice(0, limit)
    .map((item) => object(item))
    .filter((item) => {
      const label = text(item.label);
      return label && !ACTION_SENSITIVE_KEY.test(label);
    })
    .map((item) => ({
      label: text(item.label).slice(0, 120),
      value: safeActionValue(item.value),
    }))
    .filter((item) => item.value);
}

function actionChangeRows(raw) {
  const payload = object(raw.payload);
  const action = text(raw.action).toLowerCase();
  const targetId = text(raw.target_id);
  if (action === 'cancel_event') {
    return [
      { label: 'Operation', value: 'Cancel this calendar event' },
      ...(targetId ? [{ label: 'Event UID', value: targetId.slice(0, 255) }] : []),
      ...(Number(payload.expected_event_version) > 0 ? [{
        label: 'Expected event version',
        value: String(Math.trunc(Number(payload.expected_event_version))),
      }] : []),
    ];
  }
  const changes = action === 'update_event' ? object(payload.changes) : payload;
  const rendered = safeActionRows(changes, 20);
  if (targetId && action !== 'create_event') rendered.unshift({
    label: humanizeActionLabel(raw.target_type, 'Target'), value: targetId.slice(0, 255),
  });
  if (Number(payload.expected_event_version) > 0 && action !== 'update_event') rendered.push({
    label: 'Expected event version',
    value: String(Math.trunc(Number(payload.expected_event_version))),
  });
  return rendered.length ? rendered : [{
    label: 'Operation', value: humanizeActionLabel(action, 'Execute reviewed action'),
  }];
}

function actionRisk(autonomyLevel) {
  const requested = Number(autonomyLevel);
  const level = Number.isFinite(requested)
    ? Math.max(1, Math.min(6, Math.trunc(requested))) : 6;
  return ({
    1: { label: 'Observe only', tone: 'low' },
    2: { label: 'Suggestion only', tone: 'low' },
    3: { label: 'Prepared draft', tone: 'medium' },
    4: { label: 'Reversible change', tone: 'medium' },
    5: { label: 'External or consequential', tone: 'high' },
    6: { label: 'Critical or irreversible', tone: 'critical' },
  })[level];
}

export function normalizeActionProposal(value) {
  const raw = object(value);
  const id = text(raw.id).slice(0, 255);
  const version = Math.trunc(number(raw.version));
  if (!id || version < 1) return null;
  // The action center stores normalized proposals in memory.  Event handlers
  // pass those objects back through this boundary, so accept both the server's
  // snake_case wire shape and our bounded camelCase shape without dropping
  // server-reviewed capability flags on the second pass.
  const requestedAutonomy = Number(raw.autonomy_level ?? raw.autonomyLevel);
  const autonomyLevel = Number.isFinite(requestedAutonomy)
    ? Math.max(1, Math.min(6, Math.trunc(requestedAutonomy))) : 6;
  const risk = actionRisk(autonomyLevel);
  return {
    id,
    domain: text(raw.domain, 'unknown').slice(0, 48),
    action: text(raw.action, 'unknown_action').slice(0, 80),
    state: text(raw.state, 'unknown').toLowerCase().slice(0, 24),
    autonomyLevel,
    riskLabel: risk.label,
    riskTone: risk.tone,
    targetType: text(raw.target_type ?? raw.targetType, 'target').slice(0, 48),
    targetId: text(raw.target_id ?? raw.targetId).slice(0, 255),
    reason: text(raw.reason, 'No reason was provided.').slice(0, 4000),
    evidence: Array.isArray(raw.evidence)
      ? safeActionDetailRows(raw.evidence, 12) : safeActionRows(raw.sources, 12),
    changes: Array.isArray(raw.changes)
      ? safeActionDetailRows(raw.changes, 20) : actionChangeRows(raw),
    outcome: Array.isArray(raw.outcome)
      ? safeActionDetailRows(raw.outcome, 12) : safeActionRows(raw.result, 12),
    external: Boolean(raw.external),
    requiresConfirmation: (raw.requires_confirmation ?? raw.requiresConfirmation) === true,
    reviewedExecutor: (raw.reviewed_server_executor ?? raw.reviewedExecutor) === true,
    reviewedReversal: (raw.reviewed_server_reversal ?? raw.reviewedReversal) === true,
    expiresAt: text(raw.expires_at ?? raw.expiresAt),
    executedAt: text(raw.executed_at ?? raw.executedAt),
    updatedAt: text(raw.updated_at ?? raw.updatedAt),
    version,
  };
}

function normalizeActionRows(value, allowedStates) {
  const seen = new Set();
  return rows(value).map(normalizeActionProposal).filter((item) => {
    if (!item || !allowedStates.includes(item.state) || seen.has(item.id)) return false;
    seen.add(item.id);
    return true;
  });
}

function actionApiError(payload, status) {
  const detail = object(payload).detail ?? object(payload).error ?? object(payload).message;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  return text(object(detail).message ?? object(detail).detail, `Action review request failed (HTTP ${status})`);
}

async function actionRequest(path, { method = 'GET', body = null, signal = null } = {}) {
  const response = await fetch(`${API_BASE}/api/life${path}`, {
    method,
    credentials: 'same-origin',
    headers: body
      ? { 'Content-Type': 'application/json', Accept: 'application/json' }
      : { Accept: 'application/json' },
    body: body ? JSON.stringify(body) : null,
    signal,
  });
  const raw = await response.text();
  let payload = {};
  if (raw) {
    try { payload = JSON.parse(raw); } catch (_) { payload = { detail: raw }; }
  }
  if (!response.ok) {
    const error = new Error(actionApiError(payload, response.status));
    error.status = response.status;
    throw error;
  }
  return object(payload);
}

export async function executeReviewedAction(value, requester = actionRequest) {
  let current = normalizeActionProposal(value);
  if (!current) throw new Error('Action proposal is invalid; reload before continuing.');
  if (!current.reviewedExecutor) throw new Error('Action has no reviewed server executor.');
  if (!ACTION_PENDING_STATES.includes(current.state)) {
    throw new Error('Action is no longer pending; reload before continuing.');
  }
  let confirmationToken = '';
  try {
    if (current.state === 'prepared' && current.requiresConfirmation) {
      const challenge = await requester(`/actions/${encodeURIComponent(current.id)}/confirmation`, {
        method: 'POST', body: { version: current.version, purpose: 'approve' },
      });
      confirmationToken = text(challenge.confirmation_token);
      const challenged = normalizeActionProposal(challenge.action);
      if (!challenged || !confirmationToken) throw new Error('Fresh confirmation could not be issued.');
      const approved = await requester(`/actions/${encodeURIComponent(current.id)}/approve`, {
        method: 'POST',
        body: { version: challenged.version, confirmation_token: confirmationToken },
      });
      confirmationToken = '';
      current = normalizeActionProposal(approved.action);
      if (!current || current.state !== 'approved') throw new Error('Action approval was not recorded.');
    }
    const executed = await requester(`/actions/${encodeURIComponent(current.id)}/execute`, {
      method: 'POST', body: { version: current.version },
    });
    const result = normalizeActionProposal(executed.action);
    if (!result || result.state !== 'completed') throw new Error('Server did not confirm action completion.');
    return result;
  } finally {
    confirmationToken = '';
  }
}

export async function reverseReviewedAction(value, requester = actionRequest) {
  let current = normalizeActionProposal(value);
  if (!current) throw new Error('Action proposal is invalid; reload before continuing.');
  if (current.state !== 'completed') throw new Error('Only completed actions can be reversed.');
  if (!current.reviewedReversal) throw new Error('Action has no reviewed server reversal.');
  if (current.autonomyLevel >= 5 && !current.requiresConfirmation) {
    throw new Error('High-risk reversal is missing its required confirmation fence.');
  }
  let confirmationToken = '';
  try {
    if (current.requiresConfirmation) {
      const challenge = await requester(`/actions/${encodeURIComponent(current.id)}/confirmation`, {
        method: 'POST', body: { version: current.version, purpose: 'reverse' },
      });
      confirmationToken = text(challenge.confirmation_token);
      current = normalizeActionProposal(challenge.action);
      if (!current || !confirmationToken) throw new Error('Fresh reversal confirmation could not be issued.');
    }
    const reversed = await requester(`/actions/${encodeURIComponent(current.id)}/reverse`, {
      method: 'POST',
      body: {
        version: current.version,
        ...(confirmationToken ? { confirmation_token: confirmationToken } : {}),
      },
    });
    const result = normalizeActionProposal(reversed.action);
    if (!result || result.state !== 'reversed') throw new Error('Server did not confirm the reversal.');
    return result;
  } finally {
    confirmationToken = '';
  }
}

async function fetchActionState(actionState, signal) {
  const params = new URLSearchParams({ state: actionState, limit: '20' });
  const payload = await actionRequest(`/actions?${params}`, { signal });
  if (!Array.isArray(payload.actions)) throw new Error(`Invalid ${actionState} action response`);
  return payload.actions;
}

async function loadActionCenter({ signal = null } = {}) {
  state.actionCenter.loading = true;
  state.actionCenter.error = '';
  state.actionCenter.partial = false;
  state.actionCenter.pending = [];
  state.actionCenter.recent = [];
  state.actionCenter.reviewedKey = '';
  const requestedStates = [...ACTION_PENDING_STATES, ...ACTION_RECENT_STATES];
  try {
    const settled = await Promise.allSettled(
      requestedStates.map((actionState) => fetchActionState(actionState, signal)),
    );
    if (signal?.aborted) return false;
    const failures = settled.filter((item) => item.status === 'rejected');
    const firstFailure = failures[0]?.reason;
    if (failures.length === settled.length) {
      state.actionCenter.error = text(
        firstFailure?.message,
        'Action review is temporarily unavailable. Today remains usable.',
      );
      return false;
    }
    const byState = Object.fromEntries(requestedStates.map((name, index) => [
      name,
      settled[index].status === 'fulfilled' ? settled[index].value : [],
    ]));
    state.actionCenter.pending = normalizeActionRows(
      [...rows(byState.prepared), ...rows(byState.approved)], ACTION_PENDING_STATES,
    );
    state.actionCenter.recent = normalizeActionRows(
      [...rows(byState.completed), ...rows(byState.reversed)], ACTION_RECENT_STATES,
    ).sort((left, right) => text(right.updatedAt).localeCompare(text(left.updatedAt))).slice(0, 12);
    if (failures.length) {
      state.actionCenter.partial = true;
      state.actionCenter.error = 'Some action states could not be refreshed. Retry before treating this queue as complete.';
    }
    return true;
  } finally {
    if (!signal?.aborted) state.actionCenter.loading = false;
  }
}

async function refreshActionCenter() {
  actionController?.abort();
  actionController = new AbortController();
  try {
    return await loadActionCenter({ signal: actionController.signal });
  } catch (error) {
    if (error?.name !== 'AbortError') {
      state.actionCenter.error = text(error?.message, 'Action review could not be refreshed.');
      state.actionCenter.loading = false;
    }
    return false;
  } finally {
    actionController = null;
    if (state.open && state.view === 'home') render();
  }
}

function findActionProposal(actionId) {
  const wanted = text(actionId);
  return [...state.actionCenter.pending, ...state.actionCenter.recent]
    .find((item) => item.id === wanted) || null;
}

async function rejectReviewedAction(value) {
  const current = normalizeActionProposal(value);
  if (!current || !ACTION_PENDING_STATES.includes(current.state)) {
    throw new Error('Action is no longer pending; reload before continuing.');
  }
  const rejected = await actionRequest(`/actions/${encodeURIComponent(current.id)}/reject`, {
    method: 'POST',
    body: { version: current.version, reason: 'Rejected by the owner in Today action review.' },
  });
  const result = normalizeActionProposal(rejected.action);
  if (!result || result.state !== 'rejected') throw new Error('Server did not confirm rejection.');
  return result;
}

async function mutateReviewedAction(kind, actionId) {
  const current = findActionProposal(actionId);
  if (!current || state.actionCenter.busyId) return false;
  state.actionCenter.busyId = current.id;
  state.actionCenter.announcement = kind === 'reject'
    ? 'Rejecting action…'
    : kind === 'reverse' ? 'Confirming and reversing action…' : 'Approving and executing action…';
  render();
  try {
    if (kind === 'reject') await rejectReviewedAction(current);
    else if (kind === 'reverse') await reverseReviewedAction(current);
    else await executeReviewedAction(current);
    state.actionCenter.reviewedKey = '';
    state.actionCenter.announcement = kind === 'reject'
      ? 'Action rejected.' : kind === 'reverse' ? 'Action reversed.' : 'Action completed.';
    notify(state.actionCenter.announcement);
    await loadActionCenter();
    return true;
  } catch (error) {
    const stale = [409, 410].includes(Number(error?.status));
    const recovery = stale
      ? 'Action changed or expired. The queue was refreshed; review it again.'
      : text(error?.message, 'Action could not be updated. Retry after reviewing the current state.');
    state.actionCenter.announcement = recovery;
    notify(recovery, true);
    await loadActionCenter().catch(() => false);
    return false;
  } finally {
    state.actionCenter.busyId = '';
    if (state.open && state.view === 'home') render();
  }
}

function source(name) {
  const value = state.data?.sources?.[name];
  return value && typeof value === 'object'
    ? { ...value, status: text(value.status, 'ok'), items: rows(value.items) }
    : { status: 'empty', items: [], count: 0, truncated: false };
}

function inboxKindLabel(value) {
  const normalized = text(value, 'Unclassified')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .slice(0, 80);
  return normalized.replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function normalizeInboxAttention(value) {
  const safeValue = value && typeof value === 'object' ? value : {};
  const unprocessedCount = Math.max(0, Math.floor(number(
    safeValue.unprocessed_count ?? safeValue.count,
  )));
  const rawKinds = safeValue.kinds && typeof safeValue.kinds === 'object' && !Array.isArray(safeValue.kinds)
    ? safeValue.kinds
    : {};
  const kinds = Object.entries(rawKinds)
    .map(([kind, count]) => ({
      kind: text(kind, 'unclassified').slice(0, 80),
      label: inboxKindLabel(kind),
      count: Math.max(0, Math.floor(number(count))),
    }))
    .filter((item) => item.count > 0)
    .sort((left, right) => right.count - left.count || left.label.localeCompare(right.label))
    .slice(0, 12);
  const items = rows(safeValue.items).slice(0, 5).map((item) => {
    const rawConfidence = item?.confidence;
    const confidence = rawConfidence === null
      || rawConfidence === undefined
      || String(rawConfidence).trim() === ''
      ? Number.NaN
      : Number(rawConfidence);
    return {
      title: text(item?.title, 'Untitled capture').slice(0, 180),
      kind: text(item?.kind, 'unclassified').slice(0, 80),
      kindLabel: inboxKindLabel(item?.kind),
      confidence: Number.isFinite(confidence)
        ? Math.max(0, Math.min(100, Math.round(confidence)))
        : null,
      reason: text(item?.reason, 'Classification reason not available.').slice(0, 240),
    };
  });
  return {
    unavailable: sourceUnavailable(safeValue),
    unprocessedCount,
    kinds,
    items,
    oldestAt: text(safeValue.oldest_at),
    truncated: Boolean(safeValue.truncated || unprocessedCount > items.length),
  };
}

function sourceMessage(value) {
  const raw = value?.error ?? value?.message ?? value?.detail;
  if (raw && typeof raw === 'object') return text(raw.message ?? raw.code, 'Source unavailable');
  return text(raw, 'Source unavailable');
}

function sourceUnavailable(value) {
  return !['ok', 'empty'].includes(text(value?.status, 'empty').toLowerCase());
}

function healthUnavailable(value) {
  return !['ok', 'healthy', 'degraded', 'down', 'empty'].includes(
    text(value?.status, 'empty').toLowerCase(),
  );
}

function sourceValueUnavailable(name, value) {
  return name === 'health' ? healthUnavailable(value) : sourceUnavailable(value);
}

function unavailableSourceNames(sourceValues, names) {
  const values = sourceValues && typeof sourceValues === 'object' ? sourceValues : {};
  return rows(names).filter((name) => sourceValueUnavailable(name, values[name]));
}

function currentUnavailableSourceNames(names) {
  return unavailableSourceNames(state.data?.sources, names);
}

function summaryMetricValue(value, sourceName, sourceValue) {
  return sourceValueUnavailable(sourceName, sourceValue) ? 'Unavailable' : number(value);
}

function summaryCard(label, value, hint = '', tone = '') {
  return make('article', {
    className: `mission-summary-card${tone ? ` mission-tone-${tone}` : ''}`,
  }, [
    make('span', { text: label }),
    make('strong', { text: value }),
    make('small', { text: hint }),
  ]);
}

function renderSummary() {
  clear(refs.summary);
  const summary = state.data?.summary || {};
  const health = source('health');
  const progression = source('progression');
  const profile = progression.profile || {};
  const progressionIsUnavailable = sourceValueUnavailable('progression', progression);
  const healthIsUnavailable = sourceValueUnavailable('health', health);
  const overall = healthIsUnavailable ? 'Unavailable' : text(health.overall, 'Ready');
  const sourceSummaryCard = (label, summaryKey, sourceName, hint) => {
    const value = source(sourceName);
    const unavailable = sourceValueUnavailable(sourceName, value);
    return summaryCard(
      label,
      summaryMetricValue(summary[summaryKey], sourceName, value),
      unavailable ? `${label} source` : hint,
      unavailable ? 'attention' : '',
    );
  };
  refs.summary.append(
    sourceSummaryCard('Calendar', 'calendar', 'calendar', 'today'),
    sourceSummaryCard('Project work', 'project_work', 'project_work', 'open or due'),
    sourceSummaryCard('Plan', 'planning', 'planning', 'open commitments'),
    sourceSummaryCard('To Do', 'notes_today', 'notes_today', 'next steps'),
    summaryCard(
      'Rank',
      progressionIsUnavailable ? 'Unavailable' : text(profile.rank_name, 'E-Rank'),
      progressionIsUnavailable ? 'Progression source' : `Level ${number(profile.level) || 1}`,
      progressionIsUnavailable ? 'attention' : '',
    ),
    sourceSummaryCard('Automations', 'tasks', 'tasks', 'scheduled or running'),
    sourceSummaryCard('Signals', 'proactive_interruptions', 'proactive', 'need attention'),
    summaryCard(
      'System',
      overall,
      healthIsUnavailable ? 'Health source' : `${rows(health.services).length} services`,
      ['ok', 'healthy'].includes(overall.toLowerCase()) ? 'good' : 'attention',
    ),
  );
}

function sectionShell(title, sourceName, actionTarget = '') {
  const value = source(sourceName);
  const count = number(value.count ?? value.items.length);
  const section = make('section', {
    className: 'mission-panel',
    attrs: { 'aria-labelledby': `mission-${sourceName}-title` },
  });
  const heading = make('header', { className: 'mission-panel-heading' }, [
    make('div', {}, [
      make('h2', { id: `mission-${sourceName}-title`, text: title }),
      make('p', { text: sourceUnavailable(value) ? sourceMessage(value) : `${count} item${count === 1 ? '' : 's'}${value.truncated ? ' · showing top results' : ''}` }),
    ]),
    ...(actionTarget ? [button('Open', 'open-target', { className: 'mission-text-btn', target: actionTarget, title: `Open ${title}` })] : []),
  ]);
  const body = make('div', { className: 'mission-panel-body' });
  section.append(heading, body);
  return { section, body, value };
}

function emptyState(message) {
  return make('p', { className: 'mission-empty', text: message });
}

function sourceError(value) {
  return make('div', { className: 'mission-source-error', attrs: { role: 'status' } }, [
    make('strong', { text: 'Unavailable' }),
    make('span', { text: sourceMessage(value) }),
  ]);
}

function itemRow(title, meta = '', { tone = '', target = '', detail = '' } = {}) {
  const content = [
    make('span', { className: 'mission-item-copy' }, [
      make('strong', { text: title }),
      ...(detail ? [make('span', { text: detail })] : []),
    ]),
    ...(meta ? [make('small', { text: meta, attrs: { title: meta } })] : []),
  ];
  return target
    ? make('button', {
      type: 'button',
      className: `mission-item${tone ? ` mission-tone-${tone}` : ''}`,
      dataset: { action: 'open-target', target },
    }, content)
    : make('div', { className: `mission-item${tone ? ` mission-tone-${tone}` : ''}` }, content);
}

function renderCalendar() {
  const { section, body, value } = sectionShell('Calendar', 'calendar', 'calendar');
  if (sourceUnavailable(value)) body.appendChild(sourceError(value));
  else if (!value.items.length) body.appendChild(emptyState('No calendar events today.'));
  else value.items.forEach((event) => {
    const timing = event.all_day ? 'All day' : [formatClock(event.start), formatClock(event.end)].filter(Boolean).join('–');
    body.appendChild(itemRow(text(event.title, 'Untitled event'), timing, {
      target: 'calendar',
      detail: [text(event.calendar), text(event.location)].filter(Boolean).join(' · '),
    }));
  });
  return section;
}

function renderProjectWork() {
  const { section, body, value } = sectionShell('Project work', 'project_work', 'projects');
  if (sourceUnavailable(value)) body.appendChild(sourceError(value));
  else if (!value.items.length) body.appendChild(emptyState('No open project work needs attention today.'));
  else value.items.forEach((item) => {
    const due = item.overdue ? `Overdue · ${text(item.due_date)}` : item.due_today ? 'Due today' : text(item.due_date);
    body.appendChild(itemRow(text(item.title, item.key || 'Untitled task'), [text(item.key), due].filter(Boolean).join(' · '), {
      target: 'projects',
      tone: item.overdue ? 'danger' : item.due_today ? 'attention' : '',
      detail: [text(item.project_name), text(item.stage)].filter(Boolean).join(' · '),
    }));
  });
  return section;
}

function renderTasks() {
  const { section, body, value } = sectionShell('Automations', 'tasks', 'tasks');
  if (sourceUnavailable(value)) body.appendChild(sourceError(value));
  else if (!value.items.length) body.appendChild(emptyState('No failed, running, or scheduled automations.'));
  else value.items.forEach((item) => {
    const failed = item.kind === 'failed_run' || text(item.status).toLowerCase() === 'failed';
    const timestamp = item.scheduled_for || item.started_at || item.finished_at;
    body.appendChild(itemRow(text(item.task_name, 'Automation'), text(item.status, item.kind), {
      target: 'tasks', tone: failed ? 'danger' : item.kind === 'running_run' ? 'attention' : '',
      detail: timestamp ? formatDate(timestamp, { time: true }) : '',
    }));
  });
  return section;
}

function progressBar(percent) {
  const clamped = Math.max(0, Math.min(100, number(percent)));
  const wrapper = make('div', { className: 'mission-progress', attrs: { role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': clamped } });
  const fill = make('span');
  fill.style.width = `${clamped}%`;
  wrapper.appendChild(fill);
  return wrapper;
}

function renderGoals() {
  const { section, body, value } = sectionShell('Goals & mastery', 'goals', 'study');
  if (sourceUnavailable(value)) body.appendChild(sourceError(value));
  else if (!value.items.length) body.appendChild(emptyState('Set a Study goal to track mastery here.'));
  else value.items.forEach((goal) => {
    const row = itemRow(text(goal.goal, goal.workspace_name || 'Study goal'), `${number(goal.progress_percent)}% · ${text(goal.mastery_status, 'in progress')}`, {
      target: 'study', detail: text(goal.next_step),
    });
    row.querySelector('.mission-item-copy')?.appendChild(progressBar(goal.progress_percent));
    body.appendChild(row);
  });
  return section;
}

function renderReviews() {
  const { section, body, value } = sectionShell('Study reviews', 'study_reviews', 'study');
  if (sourceUnavailable(value)) body.appendChild(sourceError(value));
  else if (!value.items.length) body.appendChild(emptyState('No reviews are due.'));
  else value.items.forEach((review) => {
    body.appendChild(itemRow(text(review.goal, review.workspace_name || 'Study review'), formatDate(review.due_at, { time: true }), {
      target: 'study', tone: 'attention', detail: text(review.next_step, `Level ${number(review.review_level)}`),
    }));
  });
  return section;
}

function renderImportantMail() {
  const { section, body, value } = sectionShell('Important mail', 'important_mail', 'email');
  if (sourceUnavailable(value)) body.appendChild(sourceError(value));
  else if (!value.items.length) body.appendChild(emptyState('No important unread mail needs attention.'));
  else value.items.forEach((message) => {
    body.appendChild(itemRow(text(message.subject, 'Untitled message'), text(message.sender, 'Unknown sender'), {
      target: 'email',
      tone: number(message.score) >= 2 ? 'attention' : '',
      detail: text(message.reason),
    }));
  });
  return section;
}

function renderInboxAttention() {
  const value = source('inbox');
  const inbox = normalizeInboxAttention(value);
  const section = make('section', {
    className: 'mission-panel mission-inbox-attention',
    attrs: { 'aria-labelledby': 'mission-inbox-title' },
  });
  const headingStatus = inbox.unavailable
    ? sourceMessage(value)
    : inbox.unprocessedCount
      ? `${inbox.unprocessedCount} unprocessed capture${inbox.unprocessedCount === 1 ? '' : 's'}`
      : 'Inbox is clear';
  const heading = make('header', { className: 'mission-panel-heading' }, [
    make('div', {}, [
      make('h2', { id: 'mission-inbox-title', text: 'Inbox attention' }),
      make('p', { text: headingStatus }),
    ]),
    button('Open Inbox', 'open-target', {
      className: 'mission-text-btn', target: 'inbox', title: 'Open Universal Inbox',
    }),
  ]);
  const body = make('div', { className: 'mission-panel-body' });

  if (inbox.unavailable) {
    body.appendChild(sourceError(value));
  } else if (!inbox.unprocessedCount) {
    body.appendChild(emptyState('Nothing needs classification or processing.'));
  } else {
    if (inbox.kinds.length) {
      const breakdown = make('dl', {
        className: 'mission-inbox-breakdown',
        attrs: { 'aria-label': 'Inbox classification breakdown' },
      });
      inbox.kinds.forEach((kind) => breakdown.appendChild(make('div', {}, [
        make('dt', { text: kind.label }),
        make('dd', { text: kind.count }),
      ])));
      body.appendChild(breakdown);
    } else {
      body.appendChild(make('p', {
        className: 'mission-inbox-breakdown-empty',
        text: 'Classification breakdown unavailable.',
      }));
    }

    if (inbox.items.length) {
      const previews = make('ol', {
        className: 'mission-inbox-previews',
        attrs: { 'aria-label': 'Oldest Inbox captures awaiting attention' },
      });
      inbox.items.forEach((item) => previews.appendChild(make('li', {
        className: 'mission-inbox-preview',
      }, [
        make('div', { className: 'mission-inbox-preview-heading' }, [
          make('strong', { text: item.title }),
          make('span', { text: item.kindLabel }),
        ]),
        make('p', {
          className: 'mission-inbox-preview-meta',
          text: item.confidence === null ? 'Confidence unavailable' : `${item.confidence}% confidence`,
        }),
        make('p', { className: 'mission-inbox-preview-reason', text: `Reason: ${item.reason}` }),
      ])));
      body.appendChild(previews);
    } else {
      body.appendChild(emptyState('Previews are unavailable. Open Inbox to review the captures.'));
    }

    body.appendChild(make('p', {
      className: 'mission-inbox-age',
      text: inbox.oldestAt
        ? `Oldest capture ${formatDate(inbox.oldestAt, { time: true })}${inbox.truncated ? ' · showing up to 5' : ''}`
        : `Oldest capture time unavailable${inbox.truncated ? ' · showing up to 5' : ''}`,
    }));
  }

  section.append(heading, body);
  return section;
}

function renderProactiveInterruptions() {
  const value = source('proactive');
  const interruptions = rows(value.interruptions).slice(0, 8);
  if (!sourceUnavailable(value) && !interruptions.length) return null;

  const section = make('section', {
    className: 'mission-panel mission-proactive-attention',
    attrs: { 'aria-labelledby': 'mission-proactive-title' },
  });
  const body = make('div', { className: 'mission-panel-body' });
  section.append(
    make('header', { className: 'mission-panel-heading' }, [
      make('div', {}, [
        make('h2', { id: 'mission-proactive-title', text: 'Needs attention' }),
        make('p', {
          text: sourceUnavailable(value)
            ? sourceMessage(value)
            : `${interruptions.length} deterministic signal${interruptions.length === 1 ? '' : 's'} · record-only`,
        }),
      ]),
    ]),
    body,
  );

  if (sourceUnavailable(value)) {
    body.appendChild(sourceError(value));
    return section;
  }

  interruptions.forEach((signal) => {
    const severity = text(signal?.severity, 'attention').toLowerCase();
    const domain = inboxKindLabel(signal?.domain || 'Life');
    const due = signal?.due_at ? ` · due ${formatDate(signal.due_at, { time: true })}` : '';
    body.appendChild(itemRow(
      text(signal?.title, 'Life signal'),
      `${domain} · ${inboxKindLabel(severity)}${due}`,
      {
        tone: ['critical', 'high', 'medium'].includes(severity) ? 'attention' : '',
        detail: text(signal?.summary, 'Review the cited record before acting.'),
      },
    ));
  });
  if (value.truncated || rows(value.interruptions).length > interruptions.length) {
    body.appendChild(make('p', {
      className: 'mission-inbox-age',
      text: 'Showing the highest-priority interruption signals.',
    }));
  }
  return section;
}

function renderPlanning() {
  const { section, body, value } = sectionShell('Plan', 'planning');
  section.classList.add('mission-plan-panel');
  const form = make('form', {
    className: 'mission-plan-form',
    attrs: { 'aria-label': 'Add a planning item' },
  }, [
    make('label', { className: 'mission-sr-only', text: 'Planning item' , attrs: { for: 'mission-plan-title' } }),
    make('input', {
      id: 'mission-plan-title',
      attrs: { name: 'title', type: 'text', maxlength: '240', required: 'required', placeholder: 'What needs to happen?' },
    }),
    make('label', { className: 'mission-sr-only', text: 'Due date', attrs: { for: 'mission-plan-due' } }),
    make('input', { id: 'mission-plan-due', attrs: { name: 'due_date', type: 'date' } }),
    make('button', { type: 'submit', className: 'mission-btn', text: 'Add' }),
  ]);
  body.appendChild(form);
  if (sourceUnavailable(value)) {
    body.appendChild(sourceError(value));
    return section;
  }
  if (!value.items.length) {
    body.appendChild(emptyState('Add your next concrete commitment here.'));
    return section;
  }
  value.items.forEach((item) => {
    const completed = item.status === 'completed';
    const dateInput = make('input', {
      className: 'mission-plan-date',
      attrs: {
        type: 'date',
        value: text(item.due_date),
        'aria-label': `Schedule ${text(item.title, 'planning item')}`,
      },
    });
    const row = make('article', {
      className: `mission-planning-item${completed ? ' is-complete' : ''}${item.overdue ? ' mission-tone-danger' : ''}`,
      dataset: { planningId: item.id, version: item.version },
    }, [
      make('div', { className: 'mission-item-copy' }, [
        make('strong', { text: text(item.title, 'Untitled item') }),
        make('span', {
          text: completed
            ? `Completed ${formatDate(item.completed_at, { time: true })}`
            : item.scheduled_start
              ? `Scheduled ${formatDate(item.scheduled_start, { time: true })}`
              : item.overdue ? `Overdue · ${text(item.due_date)}` : text(item.due_date, 'No due date'),
        }),
      ]),
      make('div', { className: 'mission-plan-actions' }, [
        ...(!completed ? [dateInput, button('Schedule', 'planning-schedule', { className: 'mission-text-btn' })] : []),
        button(completed ? 'Reopen' : 'Complete', completed ? 'planning-reopen' : 'planning-complete', { className: 'mission-text-btn' }),
      ]),
    ]);
    body.appendChild(row);
  });
  return section;
}

function renderProgression() {
  const { section, body, value } = sectionShell('System progression', 'progression');
  section.classList.add('mission-system-panel');
  if (sourceUnavailable(value)) {
    body.appendChild(sourceError(value));
    return section;
  }
  const profile = value.profile || {};
  const streak = value.streak || {};
  const header = make('div', { className: 'mission-rank' }, [
    make('span', { className: 'mission-rank-mark', text: text(profile.rank, 'E'), attrs: { 'aria-hidden': 'true' } }),
    make('div', {}, [
      make('strong', { text: `${text(profile.rank_name, 'E-Rank')} · Level ${number(profile.level) || 1}` }),
      make('span', { text: `${number(profile.total_xp)} XP · ${number(streak.current_days)} day streak` }),
      progressBar(profile.progress_percent),
      make('small', { text: `${number(profile.xp_to_next_level)} XP to next level` }),
    ]),
  ]);
  body.appendChild(header);
  rows(value.today?.quests).forEach((quest) => {
    const target = Math.max(1, number(quest.target));
    const current = Math.min(target, number(quest.current));
    const row = make('div', { className: `mission-quest${quest.complete ? ' is-complete' : ''}` }, [
      make('div', {}, [
        make('strong', { text: text(quest.title, 'Daily objective') }),
        make('small', { text: `${current}/${target}` }),
      ]),
      progressBar((current / target) * 100),
    ]);
    body.appendChild(row);
  });
  return section;
}

function renderNotesToday() {
  const { section, body, value } = sectionShell('To Do & goal steps', 'notes_today', 'todos');
  if (sourceUnavailable(value)) body.appendChild(sourceError(value));
  else if (!value.items.length) body.appendChild(emptyState('Add a checklist item or goal step to bring it into Home.'));
  else value.items.forEach((note) => {
    const progress = `${number(note.completed_steps)}/${number(note.total_steps)} steps`;
    const row = itemRow(text(note.title, 'Untitled note'), progress, {
      target: note.kind === 'todo' ? 'todos' : 'notes',
      tone: note.due_date ? 'attention' : '',
      detail: text(note.next_step),
    });
    if (number(note.total_steps)) row.querySelector('.mission-item-copy')?.appendChild(progressBar(note.progress_percent));
    body.appendChild(row);
  });
  return section;
}

function renderDailyBrief() {
  const { section, body, value } = sectionShell('Daily brief', 'daily_brief', 'tasks');
  if (sourceUnavailable(value)) body.appendChild(sourceError(value));
  else if (!value.items.length) body.appendChild(emptyState('No Daily Brief has run yet today.'));
  else value.items.forEach((brief) => {
    body.appendChild(make('article', { className: 'mission-brief' }, [
      make('header', {}, [
        make('strong', { text: text(brief.task_name, 'Daily Brief') }),
        make('small', { text: formatDate(brief.generated_at, { time: true }) }),
      ]),
      make('p', { text: text(brief.content, 'Brief completed without a text summary.') }),
      ...(brief.content_truncated ? [make('small', { text: 'Summary shortened in Mission Control.' })] : []),
    ]));
  });
  return section;
}

function renderHealth() {
  const { section, body, value } = sectionShell('Maintainer center', 'health', 'activity');
  const unavailable = healthUnavailable(value);
  const sectionStatus = section.querySelector('.mission-panel-heading p');
  if (sectionStatus && !unavailable) {
    const count = rows(value.services).length;
    sectionStatus.textContent = `${count} service${count === 1 ? '' : 's'} · ${text(value.overall, value.status)}`;
  }
  if (unavailable) body.appendChild(sourceError(value));
  const services = rows(value.services);
  if (!services.length && !unavailable) body.appendChild(emptyState('No service health data available.'));
  services.forEach((service) => {
    const status = text(service.status, 'unknown').toLowerCase();
    body.appendChild(itemRow(text(service.name, 'Service'), status, {
      tone: ['ok', 'healthy', 'ready', 'online'].includes(status) ? 'good' : 'attention',
      target: 'activity',
    }));
  });
  return section;
}

function renderActivityFeed() {
  const value = state.data?.activity || { status: 'empty', items: [] };
  const section = make('section', {
    className: 'mission-panel mission-activity-feed',
    attrs: { 'aria-labelledby': 'mission-activity-feed-title' },
  });
  const count = number(value.count ?? rows(value.items).length);
  const heading = make('header', { className: 'mission-panel-heading' }, [
    make('div', {}, [
      make('h2', { id: 'mission-activity-feed-title', text: 'Recent activity' }),
      make('p', { text: `${count} event${count === 1 ? '' : 's'}${value.truncated ? ' · more available' : ''}` }),
    ]),
    ...(value.truncated && value.next_before && value.next_before_id ? [
      button(state.activityLoadingMore ? 'Loading…' : 'Load more', 'activity-more', {
        className: 'mission-text-btn',
        title: 'Load older activity',
      }),
    ] : []),
  ]);
  const moreButton = heading.querySelector('[data-action="activity-more"]');
  if (moreButton) moreButton.disabled = state.activityLoadingMore;
  const body = make('div', { className: 'mission-panel-body' });
  if (sourceUnavailable(value)) body.appendChild(sourceError(value));
  else if (!rows(value.items).length) body.appendChild(emptyState('No task, project, or progression activity yet.'));
  else rows(value.items).forEach((item) => {
    body.appendChild(itemRow(text(item.title, 'Activity'), formatDate(item.occurred_at, { time: true }), {
      target: text(item.target, 'home'),
      tone: item.status === 'error' ? 'danger' : item.xp ? 'good' : '',
      detail: text(item.detail),
    }));
  });
  section.append(heading, body);
  return section;
}

function renderWhatChanged() {
  const { section, body, value } = sectionShell(
    'What changed?', 'recent_activity', 'activity',
  );
  if (sourceUnavailable(value)) body.appendChild(sourceError(value));
  else if (!value.items.length) body.appendChild(emptyState('No verified task, project, or progression changes yet.'));
  else value.items.slice(0, 8).forEach((item) => {
    body.appendChild(itemRow(
      text(item.title, 'Activity'),
      formatDate(item.occurred_at, { time: true }),
      {
        target: text(item.target, 'home'),
        tone: item.status === 'error' ? 'danger' : item.xp ? 'good' : '',
        detail: text(item.detail),
      },
    ));
  });
  return section;
}

function mapBackendActions(value) {
  return rows(value).slice(0, 3).map((item) => ({
    title: text(item?.title, 'Next action'),
    meta: text(item?.detail),
    tone: item?.urgency === 'critical' ? 'danger' : item?.urgency === 'attention' ? 'attention' : '',
    target: text(item?.target, 'home'),
    rank: 0,
  }));
}

function focusCandidates() {
  const backend = mapBackendActions(state.data?.next_actions);
  if (backend.length) return backend;
  const candidates = [];
  source('tasks').items.filter((item) => item.kind === 'failed_run').forEach((item) => candidates.push({
    title: `Fix ${text(item.task_name, 'failed automation')}`, meta: 'Automation failed', tone: 'danger', target: 'tasks', rank: 0,
  }));
  source('project_work').items.forEach((item) => candidates.push({
    title: text(item.title, item.key || 'Project task'),
    meta: item.overdue ? `Overdue · ${text(item.project_name)}` : item.due_today ? `Due today · ${text(item.project_name)}` : text(item.project_name),
    tone: item.overdue ? 'danger' : item.due_today ? 'attention' : '', target: 'projects', rank: item.overdue ? 1 : item.due_today ? 2 : 6,
  }));
  source('study_reviews').items.forEach((item) => candidates.push({
    title: `Review ${text(item.goal, item.workspace_name || 'study goal')}`, meta: text(item.next_step, 'Review due'), tone: 'attention', target: 'study', rank: 3,
  }));
  source('calendar').items.slice(0, 2).forEach((item) => candidates.push({
    title: text(item.title, 'Calendar event'), meta: item.all_day ? 'All day' : formatClock(item.start), target: 'calendar', rank: 4,
  }));
  source('goals').items.forEach((item) => {
    if (!text(item.next_step)) return;
    candidates.push({ title: text(item.next_step), meta: text(item.goal, 'Goal next step'), target: 'study', rank: 5 });
  });
  return candidates.sort((a, b) => a.rank - b.rank).slice(0, 3);
}

function renderFocus() {
  const section = make('section', { className: 'mission-panel mission-focus', attrs: { 'aria-labelledby': 'mission-focus-title' } });
  const heading = make('header', { className: 'mission-panel-heading' }, [
    make('div', {}, [make('h2', { id: 'mission-focus-title', text: 'Focus now' }), make('p', { text: 'Highest-leverage next actions across Restia' })]),
  ]);
  const body = make('div', { className: 'mission-panel-body' });
  const candidates = focusCandidates();
  if (!candidates.length) body.appendChild(emptyState('You are clear for now. Pick a goal or start a focused project block.'));
  candidates.forEach((item) => body.appendChild(itemRow(item.title, item.meta, item)));
  section.append(heading, body);
  return section;
}

const SOURCE_LABELS = Object.freeze({
  calendar: 'Calendar',
  project_work: 'Project work',
  planning: 'Plan',
  study_reviews: 'Study reviews',
  notes_today: 'To Do',
  important_mail: 'Important mail',
  tasks: 'Automations',
  goals: 'Study goals',
  health: 'System health',
  recent_activity: 'Recent changes',
});

const PRIORITY_SOURCE_NAMES = Object.freeze([
  'tasks', 'project_work', 'important_mail', 'study_reviews', 'planning', 'notes_today', 'goals',
]);

const SCHEDULE_SOURCE_NAMES = Object.freeze(['calendar', ...PRIORITY_SOURCE_NAMES]);

const TODAY_EXECUTION_SECTIONS = Object.freeze([
  ['events', 'What is happening today?', 'Known calendar commitments.', 'No calendar commitments today.', ['calendar']],
  ['must_do_tasks', 'What must be completed?', 'Due, overdue, and review work.', 'No must-do work is due today.', ['project_work', 'planning', 'study_reviews', 'notes_today']],
  ['people_awaiting_responses', 'Who needs a response?', 'People and threads awaiting attention.', 'No source-backed responses are waiting.', ['important_mail']],
  ['health_routine_commitments', 'Health and routines', 'Commitments worth protecting today.', 'No health or routine commitment is due.', ['planning', 'notes_today', 'calendar']],
  ['risks_conflicts', 'What needs attention?', 'Overlaps, failures, overdue work, and degraded services.', 'No current conflict or overdue risk was found.', ['calendar', 'tasks', 'project_work', 'planning', 'health', 'important_mail']],
  ['suggested_schedule', 'Suggested schedule', 'Free blocks for the highest-priority actions.', 'No additional focus block fits the known schedule.', SCHEDULE_SOURCE_NAMES],
  ['restia_owned_work', 'What is Restia handling?', 'Scheduled, running, and supervised automated work.', 'Restia has no owned work scheduled today.', ['tasks']],
]);

function recommendationSupport(item) {
  const values = [];
  if (item?.supported_goal?.title) values.push(`Goal: ${text(item.supported_goal.title)}`);
  if (item?.supported_project?.title) values.push(`Project: ${text(item.supported_project.title)}`);
  return values.join(' · ');
}

function recommendationEvidence(item) {
  return rows(item?.source_evidence)
    .slice(0, 2)
    .map((evidence) => text(evidence?.label, text(evidence?.source)))
    .filter(Boolean)
    .join(' · ');
}

function recommendationCard(item, { primary = false } = {}) {
  const duration = Math.max(0, Math.round(number(item?.estimated_minutes)));
  const timing = item?.start && item?.end
    ? `${formatClock(item.start)}–${formatClock(item.end)}`
    : duration ? `${duration} min` : 'Restia-owned';
  const tone = item?.urgency === 'critical'
    ? ' mission-tone-danger'
    : item?.urgency === 'attention' ? ' mission-tone-attention' : '';
  const support = recommendationSupport(item);
  const evidence = recommendationEvidence(item);
  const focusTarget = normalizeFocusTarget(item?.focus_target);
  const recommendationId = text(item?.id);
  const headingActions = make('div', { className: 'mission-recommendation-actions' });
  if (focusTarget && recommendationId) {
    headingActions.appendChild(button('Focus', 'focus-prepare', {
      className: 'mission-text-btn mission-focus-start-btn',
      title: `Focus on ${text(item?.title, 'this item')}`,
      dataset: { focusRecommendationId: recommendationId },
    }));
  }
  if (text(item?.target)) {
    headingActions.appendChild(button('Open', 'open-target', {
      className: 'mission-text-btn', target: text(item.target),
      title: `Open ${text(item.title, 'recommendation')}`,
    }));
  }
  const card = make('article', {
    className: `mission-recommendation${primary ? ' is-primary' : ''}${tone}`,
  }, [
    make('header', { className: 'mission-recommendation-heading' }, [
      make('div', {}, [
        ...(primary ? [make('span', { className: 'mission-primary-label', text: 'Primary outcome' })] : []),
        make('strong', { text: text(item?.title, 'Untitled recommendation') }),
        make('small', { text: [timing, text(item?.detail)].filter(Boolean).join(' · ') }),
      ]),
      ...(headingActions.childElementCount ? [headingActions] : []),
    ]),
    make('dl', { className: 'mission-recommendation-details' }, [
      make('div', {}, [make('dt', { text: 'Why now' }), make('dd', { text: text(item?.why_now, 'Reason unavailable.') })]),
      make('div', {}, [make('dt', { text: 'If delayed' }), make('dd', { text: text(item?.delay_cost, 'Delay impact unavailable.') })]),
      make('div', {}, [make('dt', { text: 'Restia can' }), make('dd', { text: text(item?.what_restia_can_handle, 'Open the source context.') })]),
      ...(support ? [make('div', {}, [make('dt', { text: 'Supports' }), make('dd', { text: support })])] : []),
      ...(evidence ? [make('div', {}, [make('dt', { text: 'Sources' }), make('dd', { text: evidence })])] : []),
    ]),
  ]);
  return card;
}

function degradedSourceState(sourceNames) {
  const labels = rows(sourceNames).map((name) => SOURCE_LABELS[name] || name);
  return make('div', { className: 'mission-source-error', attrs: { role: 'status' } }, [
    make('strong', { text: 'Incomplete data' }),
    make('span', {
      text: `Unavailable sources: ${labels.join(', ')}. Retry before treating this section as clear.`,
    }),
  ]);
}

function executionSection(
  title,
  subtitle,
  items,
  emptyMessage,
  { primaryId = '', unavailableSources = [] } = {},
) {
  const section = make('section', {
    className: `mission-panel mission-execution-panel${primaryId ? ' mission-primary-panel' : ''}`,
    attrs: { 'aria-labelledby': `mission-execution-${title.toLowerCase().replace(/[^a-z0-9]+/g, '-')}` },
  });
  const headingId = section.getAttribute('aria-labelledby');
  const heading = make('header', { className: 'mission-panel-heading' }, [
    make('div', {}, [
      make('h2', { id: headingId, text: title }),
      make('p', { text: subtitle }),
    ]),
  ]);
  const body = make('div', { className: 'mission-panel-body mission-execution-body' });
  if (unavailableSources.length) body.appendChild(degradedSourceState(unavailableSources));
  if (!items.length && !unavailableSources.length) body.appendChild(emptyState(emptyMessage));
  items.forEach((item) => body.appendChild(recommendationCard(item, {
    primary: Boolean(primaryId && text(item?.id) === primaryId),
  })));
  section.append(heading, body);
  return section;
}

function renderTodayExecution() {
  const primary = state.data?.primary_outcome;
  const primaryId = text(primary?.id);
  const seen = new Set();
  const topActions = rows(state.data?.top_three_actions).filter((item) => {
    const id = text(item?.id);
    if (!id || seen.has(id)) return false;
    seen.add(id);
    return true;
  }).slice(0, 3);
  if (primaryId && !seen.has(primaryId)) topActions.unshift(primary);
  const sections = [executionSection(
    'What matters now?',
    'Primary outcome and the three highest-priority actions.',
    topActions.slice(0, 3),
    'No urgent action is currently recommended.',
    {
      primaryId: primaryId || text(topActions[0]?.id),
      unavailableSources: currentUnavailableSourceNames(PRIORITY_SOURCE_NAMES),
    },
  )];
  TODAY_EXECUTION_SECTIONS.forEach(([key, title, subtitle, emptyMessage, sourceNames]) => {
    sections.push(executionSection(
      title, subtitle, rows(state.data?.[key]), emptyMessage,
      { unavailableSources: currentUnavailableSourceNames(sourceNames) },
    ));
  });
  return sections;
}

function actionDetailSection(title, items, emptyMessage) {
  const section = make('section', { className: 'mission-action-detail-section' });
  section.appendChild(make('h4', { text: title }));
  if (!items.length) {
    section.appendChild(make('p', { className: 'mission-action-detail-empty', text: emptyMessage }));
    return section;
  }
  const list = make('dl', { className: 'mission-action-detail-list' });
  items.forEach((item) => {
    list.append(
      make('dt', { text: item.label }),
      make('dd', { text: item.value }),
    );
  });
  section.appendChild(list);
  return section;
}

function actionReviewId(item, purpose) {
  return `mission-action-review-${purpose}-${item.id.replace(/[^a-zA-Z0-9_-]/g, '-')}`;
}

function actionReviewDetails(item, purpose) {
  const reviewId = actionReviewId(item, purpose);
  const review = make('div', {
    id: reviewId,
    className: 'mission-action-review',
    attrs: { tabindex: '-1', 'aria-label': purpose === 'reverse' ? 'Reversal review' : 'Action review' },
  });
  const target = [{
    label: humanizeActionLabel(item.targetType, 'Target'),
    value: item.targetId || 'Server assigns the target during execution.',
  }];
  review.append(
    actionDetailSection('Reason', [{ label: 'Why Restia prepared this', value: item.reason }], ''),
    actionDetailSection('Target', target, 'No target was supplied.'),
    actionDetailSection(
      purpose === 'reverse' ? 'Change that will be reversed' : 'Exactly what will change',
      item.changes,
      'No mutation details were supplied. Execution stays unavailable until a reviewed server executor exists.',
    ),
    actionDetailSection('Source evidence', item.evidence, 'No source evidence was recorded.'),
  );
  if (purpose === 'reverse') {
    review.appendChild(make('p', {
      className: 'mission-action-safety-note',
      text: 'Restia will restore the server-recorded pre-action state. High-risk reversals require a new, single-use human confirmation.',
    }));
  } else if (!item.reviewedExecutor) {
    review.appendChild(make('p', {
      className: 'mission-action-safety-note', attrs: { role: 'status' },
      text: 'No reviewed server executor exists for this action. Restia will not attempt it or claim it ran.',
    }));
  }
  const busy = state.actionCenter.busyId === item.id;
  const controls = make('div', { className: 'mission-action-controls' });
  if (purpose === 'reverse') {
    controls.appendChild(button(
      busy ? 'Reversing…' : (item.requiresConfirmation ? 'Confirm & reverse' : 'Reverse reviewed action'),
      'action-reverse', {
        className: 'mission-btn mission-action-danger',
        dataset: { actionId: item.id },
        attrs: { disabled: busy ? 'disabled' : null, 'aria-busy': busy ? 'true' : 'false' },
      },
    ));
  } else {
    controls.appendChild(button(busy ? 'Rejecting…' : 'Reject', 'action-reject', {
      className: 'mission-btn mission-btn-quiet mission-action-reject',
      dataset: { actionId: item.id },
      attrs: { disabled: busy ? 'disabled' : null, 'aria-busy': busy ? 'true' : 'false' },
    }));
    if (item.reviewedExecutor) {
      const executeLabel = item.state === 'approved'
        ? 'Execute approved action'
        : item.requiresConfirmation ? 'Approve & execute' : 'Execute reviewed action';
      controls.appendChild(button(busy ? 'Working…' : executeLabel, 'action-execute', {
        className: 'mission-btn mission-action-primary',
        dataset: { actionId: item.id },
        attrs: { disabled: busy ? 'disabled' : null, 'aria-busy': busy ? 'true' : 'false' },
      }));
    }
  }
  review.appendChild(controls);
  return review;
}

function actionChips(item) {
  return make('div', { className: 'mission-action-chips', attrs: { 'aria-label': 'Action risk and state' } }, [
    make('span', { className: 'mission-action-chip', text: humanizeActionLabel(item.domain) }),
    make('span', {
      className: `mission-action-chip mission-action-risk-${item.riskTone}`,
      text: `Level ${item.autonomyLevel} · ${item.riskLabel}`,
    }),
    make('span', { className: 'mission-action-chip', text: humanizeActionLabel(item.state) }),
  ]);
}

function pendingActionCard(item) {
  const key = `execute:${item.id}`;
  const expanded = state.actionCenter.reviewedKey === key;
  const reviewId = actionReviewId(item, 'execute');
  const card = make('article', {
    className: 'mission-action-card',
    attrs: { 'aria-labelledby': `${reviewId}-title` },
    dataset: { actionId: item.id },
  });
  const heading = make('div', { className: 'mission-action-card-heading' }, [
    make('div', { className: 'mission-action-card-copy' }, [
      make('h3', { id: `${reviewId}-title`, text: humanizeActionLabel(item.action) }),
      make('p', { text: item.reason }),
    ]),
    button(expanded ? 'Close review' : 'Review action', 'action-review', {
      className: 'mission-btn mission-btn-quiet',
      dataset: { actionId: item.id, actionPurpose: 'execute' },
      attrs: { 'aria-expanded': expanded ? 'true' : 'false', 'aria-controls': reviewId },
    }),
  ]);
  card.append(heading, actionChips(item));
  if (item.expiresAt) card.appendChild(make('p', {
    className: 'mission-action-expiry',
    text: `${item.state === 'approved' ? 'Approval' : 'Review'} expires ${formatDate(item.expiresAt, { time: true })}`,
  }));
  if (expanded) card.appendChild(actionReviewDetails(item, 'execute'));
  return card;
}

function recentActionCard(item) {
  const key = `reverse:${item.id}`;
  const expanded = state.actionCenter.reviewedKey === key;
  const reviewId = actionReviewId(item, 'reverse');
  const card = make('article', { className: 'mission-action-outcome', dataset: { actionId: item.id } });
  const copy = make('div', { className: 'mission-action-card-copy' }, [
    make('h3', { text: humanizeActionLabel(item.action) }),
    make('p', {
      text: `${humanizeActionLabel(item.state)}${item.executedAt ? ` · ${formatDate(item.executedAt, { time: true })}` : ''}`,
    }),
  ]);
  const children = [copy];
  if (item.state === 'completed' && item.reviewedReversal) {
    children.push(button(expanded ? 'Close reversal review' : 'Review reversal', 'action-review', {
      className: 'mission-btn mission-btn-quiet',
      dataset: { actionId: item.id, actionPurpose: 'reverse' },
      attrs: { 'aria-expanded': expanded ? 'true' : 'false', 'aria-controls': reviewId },
    }));
  }
  card.appendChild(make('div', { className: 'mission-action-outcome-heading' }, children));
  if (item.outcome.length) card.appendChild(actionDetailSection('Outcome', item.outcome, ''));
  if (expanded) card.appendChild(actionReviewDetails(item, 'reverse'));
  return card;
}

function renderActionCenter() {
  const center = state.actionCenter;
  const section = make('section', {
    className: 'mission-panel mission-primary-panel mission-action-center',
    attrs: { 'aria-labelledby': 'mission-action-center-title' },
  });
  const title = make('div', {}, [
    make('h2', { id: 'mission-action-center-title', text: 'Action review' }),
    make('p', { text: 'Human approval for prepared changes and their server-verified outcomes.' }),
  ]);
  const badge = make('span', {
    className: 'mission-action-count',
    text: center.loading ? 'Loading' : `${center.pending.length} pending`,
    attrs: { 'aria-label': center.loading ? 'Loading action review' : `${center.pending.length} actions pending review` },
  });
  section.appendChild(make('div', { className: 'mission-panel-heading' }, [title, badge]));
  const body = make('div', { className: 'mission-panel-body mission-action-body' });
  body.appendChild(make('p', {
    className: 'mission-action-announcement',
    text: center.announcement,
    attrs: { role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' },
  }));
  if (center.error) {
    body.appendChild(make('div', {
      className: 'mission-action-error', attrs: { role: center.partial ? 'status' : 'alert' },
    }, [
      make('p', { text: center.error }),
      button(center.loading ? 'Retrying…' : 'Retry action review', 'action-retry', {
        className: 'mission-btn mission-btn-quiet',
        attrs: { disabled: center.loading ? 'disabled' : null, 'aria-busy': center.loading ? 'true' : 'false' },
      }),
    ]));
  }
  if (center.loading && !center.pending.length && !center.recent.length) {
    body.appendChild(make('p', { className: 'mission-action-loading', text: 'Loading owner-scoped action proposals…', attrs: { role: 'status' } }));
  } else {
    const pending = make('div', { className: 'mission-action-list', attrs: { 'aria-label': 'Pending actions' } });
    if (!center.pending.length && !center.error) pending.appendChild(make('p', {
      className: 'mission-action-empty', text: 'No actions need approval right now.',
    }));
    center.pending.forEach((item) => pending.appendChild(pendingActionCard(item)));
    body.appendChild(pending);
    if (center.recent.length) {
      body.appendChild(make('h3', { className: 'mission-action-recent-title', text: 'Recent verified outcomes' }));
      const recent = make('div', { className: 'mission-action-outcomes', attrs: { 'aria-label': 'Recent action outcomes' } });
      center.recent.forEach((item) => recent.appendChild(recentActionCard(item)));
      body.appendChild(recent);
    }
  }
  section.appendChild(body);
  return section;
}

const FOCUS_RECOMMENDATION_KEYS = Object.freeze([
  'top_three_actions',
  ...TODAY_EXECUTION_SECTIONS.map(([key]) => key),
]);

function findFocusRecommendation(recommendationId) {
  const wanted = text(recommendationId);
  if (!wanted) return null;
  for (const key of FOCUS_RECOMMENDATION_KEYS) {
    const match = rows(state.data?.[key]).find((item) => text(item?.id) === wanted);
    if (match) return match;
  }
  return null;
}

export function normalizeFocusCandidate(value) {
  const raw = object(value);
  const target = normalizeFocusTarget(raw.target ?? raw.focus_target);
  if (!target) return null;
  const properties = object(raw.properties ?? raw.entity?.properties);
  return {
    target,
    title: text(raw.title ?? raw.entity?.title, 'Focused work').slice(0, 240),
    summary: text(raw.summary ?? raw.detail ?? raw.entity?.summary).slice(0, 20_000),
    definitionOfDone: text(
      raw.definition_of_done ?? raw.definitionOfDone ?? properties.definition_of_done,
    ).slice(0, 20_000),
  };
}

function focusCandidateFromRecommendation(item) {
  return normalizeFocusCandidate({
    focus_target: item?.focus_target,
    title: item?.title,
    summary: item?.detail,
    definition_of_done: item?.definition_of_done,
  });
}

function focusApiError(payload, status) {
  const detail = object(payload).detail ?? object(payload).error ?? object(payload).message;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  return text(object(detail).message ?? object(detail).detail, `Focus request failed (HTTP ${status})`);
}

async function focusRequest(path, { method = 'POST', body = null, signal = null } = {}) {
  const response = await fetch(`${API_BASE}/api/life/focus${path}`, {
    method,
    credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json', Accept: 'application/json' } : { Accept: 'application/json' },
    body: body ? JSON.stringify(body) : null,
    signal,
  });
  const raw = await response.text();
  let payload = {};
  if (raw) {
    try { payload = JSON.parse(raw); } catch (_) { payload = { detail: raw }; }
  }
  if (!response.ok) throw new Error(focusApiError(payload, response.status));
  return object(payload);
}

async function loadCurrentFocus({ signal = null, autoEnter = true } = {}) {
  state.focusLoading = true;
  state.focusError = '';
  try {
    const payload = await focusRequest('/current', { method: 'GET', signal });
    const session = normalizeFocusSession(payload.session);
    state.focusSession = session;
    if (session) {
      state.focusSetup = null;
      state.focusResult = null;
      if (autoEnter && !state.focusReturnedToToday) state.focusView = true;
    } else if (!state.focusSetup && !state.focusResult) {
      state.focusView = false;
    }
    return session;
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    state.focusError = text(error?.message, 'Focus state could not be restored.');
    return state.focusSession;
  } finally {
    state.focusLoading = false;
  }
}

function syncFocusTimer() {
  if (focusTimer) {
    clearInterval(focusTimer);
    focusTimer = null;
  }
  const update = () => {
    const elapsed = formatDuration(focusDisplayElapsed(state.focusSession));
    refs.root?.querySelectorAll?.('[data-focus-elapsed]').forEach((element) => {
      element.textContent = elapsed;
      element.setAttribute('datetime', `PT${focusDisplayElapsed(state.focusSession)}S`);
    });
  };
  update();
  if (state.open && state.focusSession?.state === 'active') {
    focusTimer = setInterval(update, 1000);
  }
}

function focusJournal(entries, label) {
  const list = make('ol', { className: 'mission-focus-journal-list' });
  const recent = rows(entries).slice(-4).reverse();
  if (!recent.length) {
    list.appendChild(make('li', { className: 'mission-focus-journal-empty', text: `No ${label.toLowerCase()} yet.` }));
    return list;
  }
  recent.forEach((entry) => list.appendChild(make('li', {}, [
    make('span', { text: text(entry?.text, 'Untitled entry') }),
    make('time', { text: formatDate(entry?.at, { time: true }), attrs: { datetime: text(entry?.at) } }),
  ])));
  return list;
}

function focusContextCard(item) {
  return make('li', { className: 'mission-focus-context-item' }, [
    make('span', { className: 'mission-focus-context-type', text: text(item?.type, 'Context').replace(/[_-]+/g, ' ') }),
    make('strong', { text: text(item?.title, 'Untitled context') }),
    ...(text(item?.summary) ? [make('p', { text: text(item.summary).slice(0, 400) })] : []),
    make('small', { text: text(item?.relation, 'linked').replace(/[_-]+/g, ' ') }),
  ]);
}

function focusSetupSurface(candidate) {
  const form = make('form', { className: 'mission-focus-setup-form' });
  const definitionId = 'mission-focus-definition';
  const definition = make('textarea', {
    attrs: {
      id: definitionId,
      name: 'definition_of_done',
      required: 'required',
      maxlength: '20000',
      rows: '4',
      placeholder: 'What observable result means this work is done?',
    },
  });
  definition.value = text(candidate?.definitionOfDone);
  form.append(
    make('div', { className: 'mission-focus-setup-copy' }, [
      make('p', { className: 'mission-focus-kicker', text: 'Prepare focus' }),
      make('h2', { id: 'mission-focus-surface-title', text: text(candidate?.title, 'Focused work') }),
      ...(text(candidate?.summary) ? [make('p', { text: text(candidate.summary) })] : []),
    ]),
    make('label', { className: 'mission-focus-field' }, [
      make('span', { text: 'Definition of done' }),
      definition,
      make('small', { text: 'Use an observable result so completion can be verified.' }),
    ]),
    make('div', { className: 'mission-focus-setup-actions' }, [
      button('Start focus', 'focus-start', {
        className: 'mission-btn mission-focus-primary', type: 'submit',
      }),
      button('Cancel', 'focus-cancel-setup', { className: 'mission-btn mission-btn-quiet' }),
    ]),
  );
  return make('section', {
    className: 'mission-focus-surface mission-focus-setup',
    attrs: { 'aria-labelledby': 'mission-focus-surface-title' },
  }, [form]);
}

function focusCaptureForm() {
  const selectId = 'mission-focus-entry-type';
  const textId = 'mission-focus-entry-text';
  const select = make('select', { attrs: { id: selectId, name: 'journal' } }, [
    make('option', { text: 'Progress', attrs: { value: 'progress' } }),
    make('option', { text: 'Interruption', attrs: { value: 'interruptions' } }),
    make('option', { text: 'Evidence', attrs: { value: 'evidence' } }),
  ]);
  return make('form', { className: 'mission-focus-capture-form' }, [
    make('label', { className: 'mission-focus-field' }, [make('span', { text: 'Capture' }), select]),
    make('label', { className: 'mission-focus-field mission-focus-capture-text' }, [
      make('span', { className: 'mission-sr-only', text: 'Capture text' }),
      make('textarea', { attrs: {
        id: textId, name: 'text', rows: '2', maxlength: '4000', required: 'required',
        placeholder: 'Record what changed, interrupted you, or proves progress.',
      } }),
    ]),
    button('Save entry', 'focus-save-entry', { className: 'mission-btn', type: 'submit' }),
  ]);
}

function focusFinishForm() {
  return make('form', { className: 'mission-focus-finish-form' }, [
    make('label', { className: 'mission-focus-field' }, [
      make('span', { text: 'Follow-up actions' }),
      make('textarea', { attrs: {
        name: 'follow_ups', rows: '3', maxlength: '4800',
        placeholder: 'Optional — one action per line. Restia will create durable Planning tasks.',
      } }),
      make('small', { text: 'Up to 20 actions. They are created by the backend when this session ends.' }),
    ]),
    make('div', { className: 'mission-focus-finish-actions' }, [
      button('Complete focus', 'focus-complete', {
        className: 'mission-btn mission-focus-primary', type: 'submit',
      }),
      button('Abandon', 'focus-abandon', { className: 'mission-btn mission-focus-danger' }),
    ]),
  ]);
}

function focusActiveSurface(session) {
  const status = text(session?.state, 'active');
  const context = rows(session?.context);
  const controls = make('div', { className: 'mission-focus-controls' }, [
    status === 'active'
      ? button('Pause', 'focus-pause', { className: 'mission-btn' })
      : button('Resume', 'focus-resume', { className: 'mission-btn mission-focus-primary' }),
  ]);
  const hero = make('header', { className: 'mission-focus-hero' }, [
    make('div', { className: 'mission-focus-hero-copy' }, [
      make('p', { className: 'mission-focus-kicker', text: `${status} focus` }),
      make('h2', { id: 'mission-focus-surface-title', text: text(session?.entity?.title, 'Focused work') }),
      ...(text(session?.entity?.summary) ? [make('p', { text: text(session.entity.summary) })] : []),
    ]),
    make('div', { className: 'mission-focus-clock' }, [
      make('span', { text: 'Elapsed' }),
      make('time', { text: formatDuration(focusDisplayElapsed(session)), attrs: {
        'data-focus-elapsed': 'true', 'aria-label': 'Elapsed focus time',
      } }),
    ]),
    controls,
  ]);
  const done = make('section', { className: 'mission-focus-card mission-focus-done' }, [
    make('h3', { text: 'Definition of done' }),
    make('p', { text: text(session?.definitionOfDone, 'No definition was recorded.') }),
  ]);
  const contextSection = make('section', { className: 'mission-focus-card' }, [
    make('h3', { text: 'Linked context' }),
    context.length
      ? make('ul', { className: 'mission-focus-context-list' }, context.map(focusContextCard))
      : make('p', { className: 'mission-focus-empty-copy', text: 'No linked notes, files, or projects are available for this item.' }),
  ]);
  const journal = make('section', { className: 'mission-focus-card mission-focus-journal' }, [
    make('h3', { text: 'Progress and evidence' }),
    focusCaptureForm(),
    make('div', { className: 'mission-focus-journal-grid' }, [
      make('section', {}, [make('h4', { text: 'Progress' }), focusJournal(session?.progress, 'Progress')]),
      make('section', {}, [make('h4', { text: 'Interruptions' }), focusJournal(session?.interruptions, 'Interruptions')]),
      make('section', {}, [make('h4', { text: 'Evidence' }), focusJournal(session?.evidence, 'Evidence')]),
    ]),
  ]);
  const finish = make('section', { className: 'mission-focus-card mission-focus-finish' }, [
    make('h3', { text: 'Finish this block' }),
    focusFinishForm(),
  ]);
  return make('section', {
    className: `mission-focus-surface is-${status}`,
    attrs: { 'aria-labelledby': 'mission-focus-surface-title' },
  }, [hero, make('div', { className: 'mission-focus-layout' }, [done, contextSection, journal, finish])]);
}

function focusResultSurface(result) {
  const finalSession = result?.session;
  const followUps = rows(result?.followUps);
  const completed = text(finalSession?.state) === 'completed';
  return make('section', {
    className: 'mission-focus-surface mission-focus-result',
    attrs: { 'aria-labelledby': 'mission-focus-surface-title' },
  }, [
    make('p', { className: 'mission-focus-kicker', text: completed ? 'Focus completed' : 'Focus ended' }),
    make('h2', { id: 'mission-focus-surface-title', text: text(finalSession?.entity?.title, 'Focused work') }),
    make('p', { className: 'mission-focus-result-time' }, [
      'Recorded time ', make('strong', { text: formatDuration(finalSession?.elapsedSeconds) }),
    ]),
    make('section', { className: 'mission-focus-card' }, [
      make('h3', { text: 'Follow-up actions created' }),
      followUps.length
        ? make('ul', { className: 'mission-focus-followups' }, followUps.map((item) => make('li', {}, [
          make('strong', { text: text(item?.title, 'Untitled action') }),
          ...(text(item?.summary) ? [make('span', { text: text(item.summary) })] : []),
        ])))
        : make('p', { className: 'mission-focus-empty-copy', text: 'No follow-up actions were created.' }),
    ]),
    button('Return to Today', 'focus-return-today', { className: 'mission-btn mission-focus-primary' }),
  ]);
}

function renderFocusWorkspace() {
  clear(refs.focusHost);
  if (state.focusSession) refs.focusHost.appendChild(focusActiveSurface(state.focusSession));
  else if (state.focusResult) refs.focusHost.appendChild(focusResultSurface(state.focusResult));
  else if (state.focusSetup) refs.focusHost.appendChild(focusSetupSurface(state.focusSetup));
  if (state.focusError) refs.focusHost.prepend(make('div', {
    className: 'mission-focus-error', text: state.focusError, attrs: { role: 'alert' },
  }));
  refs.focusHost.querySelectorAll('button, textarea, select').forEach((control) => {
    control.disabled = state.focusBusy;
  });
  syncFocusTimer();
}

function renderFocusStatus() {
  clear(refs.focusStatus);
  refs.focusStatus.hidden = true;
  if (state.focusError && !state.focusSession) {
    refs.focusStatus.append(
      make('span', { text: state.focusError }),
      button('Retry Focus', 'focus-retry', { className: 'mission-text-btn' }),
    );
  } else if (state.focusSession && !state.focusView) {
    refs.focusStatus.append(
      make('div', {}, [
        make('strong', { text: text(state.focusSession.entity?.title, 'Focused work') }),
        make('span', {}, [
          `${text(state.focusSession.state, 'active')} · `,
          make('time', { text: formatDuration(focusDisplayElapsed(state.focusSession)), attrs: { 'data-focus-elapsed': 'true' } }),
        ]),
      ]),
      button('Return to Focus', 'focus-return', { className: 'mission-btn mission-focus-primary' }),
    );
  }
  refs.focusStatus.hidden = !refs.focusStatus.childElementCount;
  syncFocusTimer();
}

function focusStartBody(candidate, definitionOfDone) {
  const target = candidate?.target;
  const body = { definition_of_done: definitionOfDone };
  if (target?.kind === 'life_entity') {
    body.entity_id = target.id;
    body.entity_version = target.version;
  } else if (target?.kind === 'planning_item') {
    body.domain_ref_type = 'planning_item';
    body.domain_ref_id = target.id;
    body.domain_ref_version = target.version;
  }
  return body;
}

async function startPreparedFocus(form) {
  const candidate = state.focusSetup;
  const definition = text(new FormData(form).get('definition_of_done'));
  if (!candidate || !definition || state.focusBusy) return false;
  state.focusBusy = true;
  state.focusError = '';
  render();
  try {
    const payload = await focusRequest('/start', {
      body: focusStartBody(candidate, definition),
    });
    state.focusSession = normalizeFocusSession(payload.session);
    state.focusSetup = null;
    state.focusResult = null;
    state.focusView = true;
    state.focusReturnedToToday = false;
    notify('Focus started');
    return true;
  } catch (error) {
    state.focusError = text(error?.message, 'Could not start Focus.');
    notify(state.focusError, true);
    return false;
  } finally {
    state.focusBusy = false;
    render();
  }
}

async function mutateFocus(action, extra = {}) {
  const session = state.focusSession;
  if (!session || state.focusBusy) return null;
  state.focusBusy = true;
  state.focusError = '';
  render();
  try {
    const payload = await focusRequest(`/${encodeURIComponent(session.id)}/${action}`, {
      body: { version: session.version, ...extra },
    });
    state.focusSession = normalizeFocusSession(payload.session);
    return payload;
  } catch (error) {
    state.focusError = text(error?.message, 'Focus could not be updated.');
    notify(state.focusError, true);
    return null;
  } finally {
    state.focusBusy = false;
    render();
  }
}

async function saveFocusEntry(form) {
  const values = new FormData(form);
  const journal = text(values.get('journal'));
  const entryText = text(values.get('text'));
  if (!entryText || !['progress', 'interruptions', 'evidence'].includes(journal)) return;
  const payload = await mutateFocus(journal, { text: entryText, metadata: {} });
  if (payload) form.reset();
}

function followUpsFromForm(form) {
  return text(new FormData(form).get('follow_ups'))
    .split(/\r?\n/)
    .map((title) => title.trim())
    .filter(Boolean)
    .slice(0, 20)
    .map((title) => ({ title: title.slice(0, 240) }));
}

async function finishFocus(action, form) {
  const session = state.focusSession;
  if (!session || state.focusBusy) return false;
  if (action === 'abandon' && typeof window !== 'undefined'
    && !window.confirm('Abandon this focus block? Recorded time and notes will be kept.')) return false;
  state.focusBusy = true;
  state.focusError = '';
  render();
  try {
    const payload = await focusRequest(`/${encodeURIComponent(session.id)}/${action}`, {
      body: { version: session.version, follow_ups: followUpsFromForm(form) },
    });
    state.focusSession = null;
    state.focusResult = {
      session: normalizeFocusSession(payload.session),
      followUps: rows(payload.follow_ups).slice(0, 20),
    };
    state.focusView = true;
    notify(action === 'complete' ? 'Focus completed' : 'Focus abandoned');
    void loadCurrentView();
    return true;
  } catch (error) {
    state.focusError = text(error?.message, 'Focus could not be finished.');
    notify(state.focusError, true);
    return false;
  } finally {
    state.focusBusy = false;
    render();
  }
}

export async function prepareFocus(value) {
  const candidate = normalizeFocusCandidate(value);
  if (!candidate) return false;
  if (window.lifeWorkspaceModule?.isOpen?.()) {
    window.lifeWorkspaceModule.close({ restoreFocus: false });
  }
  const opened = await open('home');
  if (!opened) return false;
  if (state.focusSession) {
    state.focusView = true;
    state.focusReturnedToToday = false;
    render();
    return true;
  }
  state.focusSetup = candidate;
  state.focusResult = null;
  state.focusView = true;
  state.focusReturnedToToday = false;
  state.focusError = '';
  render();
  refs.focusHost.querySelector('textarea[name="definition_of_done"]')?.focus?.();
  return true;
}

function renderQuickActions() {
  clear(refs.quickActions);
  const actions = state.view === 'activity'
    ? [['Home', 'home'], ['Projects', 'projects'], ['Automations', 'tasks'], ['Calendar', 'calendar']]
    : [['New chat', 'new-chat'], ['Projects', 'projects'], ['Automations', 'tasks'], ['Calendar', 'calendar'], ['To Do', 'todos'], ['Study', 'study']];
  actions.forEach(([label, target]) => refs.quickActions.appendChild(button(label, 'open-target', { className: 'mission-quick-action', target })));
}

function render() {
  const focusSurface = state.view === 'home' && state.focusView
    && Boolean(state.focusSession || state.focusResult || state.focusSetup);
  refs.loading.hidden = focusSurface || !state.loading;
  refs.error.hidden = focusSurface || !state.error;
  refs.error.textContent = state.error;
  refs.content.hidden = focusSurface || state.loading || Boolean(state.error) || !state.data;
  refs.askRestia.hidden = focusSurface || state.view !== 'home';
  refs.focusHost.hidden = !focusSurface;
  refs.focusToggle.hidden = !focusSurface;
  refs.focusToggle.textContent = 'Return to Today';
  if (focusSurface) {
    refs.title.textContent = 'Focus';
    refs.eyebrow.textContent = 'Protected work block';
    refs.date.textContent = text(state.focusSession?.state, state.focusResult ? 'Finished' : 'Ready');
    refs.asOf.textContent = state.focusSession?.startedAt
      ? `Started ${formatDate(state.focusSession.startedAt, { time: true })}` : '';
    renderFocusWorkspace();
    return;
  }
  if (!state.data || state.loading || state.error) return;
  refs.title.textContent = state.view === 'activity' ? 'Activity' : 'Today';
  refs.eyebrow.textContent = state.view === 'activity' ? 'Owner-scoped history' : 'Personal Mission Control';
  refs.date.textContent = state.view === 'activity'
    ? 'Tasks, projects, and verified clears'
    : formatDate(`${state.data.date}T12:00:00`) || text(state.data.date, 'Today');
  refs.asOf.textContent = state.data.as_of ? `Updated ${formatDate(state.data.as_of, { time: true })}` : '';
  renderQuickActions();
  clear(refs.grid);
  if (state.view === 'activity') {
    clear(refs.focusStatus);
    refs.focusStatus.hidden = true;
    clear(refs.summary);
    const activity = state.data.activity || {};
    const counts = activity.source_counts || {};
    const health = source('health');
    refs.summary.append(
      summaryCard('Events', number(activity.count), activity.truncated ? 'latest results' : 'recent'),
      summaryCard('Automations', number(counts.automation), 'runs'),
      summaryCard('Projects', number(counts.project), 'changes'),
      summaryCard('XP clears', number(counts.progression), 'verified'),
      summaryCard('System', text(health.overall, 'Unknown'), health.cached ? (health.stale ? 'cached · stale' : 'cached') : 'fresh'),
    );
    refs.grid.append(renderActivityFeed(), renderHealth());
    return;
  }
  renderSummary();
  renderFocusStatus();
  refs.grid.append(
    renderActionCenter(), ...renderTodayExecution(), renderInboxAttention(),
    ...[renderProactiveInterruptions()].filter(Boolean),
    renderWhatChanged(),
  );
}

async function activateInboxNavigation(activate = null) {
  const handler = activate || (typeof window !== 'undefined' ? window.activateNavigationItem : null);
  if (typeof handler !== 'function') return false;
  return (await handler('inbox')) !== false;
}

function triggerTarget(target) {
  if (target === 'inbox') {
    if (typeof window.activateNavigationItem !== 'function') {
      notify('Inbox navigation is unavailable', true);
      return false;
    }
    void activateInboxNavigation(window.activateNavigationItem).then((opened) => {
      if (!opened) notify('Inbox could not be opened', true);
    }).catch((error) => {
      notify(text(error?.message, 'Inbox could not be opened'), true);
    });
    return true;
  }
  const ids = {
    'new-chat': ['sidebar-new-chat-btn', 'rail-new-session'],
    projects: ['tool-projects-btn', 'rail-projects'],
    tasks: ['tool-tasks-btn', 'rail-tasks'],
    calendar: ['tool-calendar-btn', 'rail-calendar'],
    study: ['tool-study-btn', 'rail-study'],
    email: ['email-section-title', 'rail-email'],
    notes: ['tool-notes-btn', 'rail-notes'],
    todos: ['tool-todos-btn', 'rail-todos'],
    settings: ['user-bar-settings', 'rail-settings'],
    home: ['v2-home-nav', 'rail-home'],
    activity: ['v2-activity-nav', 'rail-activity'],
  }[target] || [];
  const trigger = ids.map((id) => document.getElementById(id)).find(Boolean);
  if (!trigger) return false;
  close();
  trigger.click();
  return true;
}

async function loadCurrentView() {
  const token = ++sequence;
  const requestedView = state.view;
  controller?.abort();
  activityMoreController?.abort();
  activityMoreController = null;
  state.activityLoadingMore = false;
  controller = new AbortController();
  state.loading = true;
  state.error = '';
  render();
  const focusPromise = requestedView === 'home'
    ? loadCurrentFocus({ signal: controller.signal, autoEnter: true })
      .then((session) => {
        if (token === sequence && state.open && requestedView === state.view) render();
        return session;
      })
      .catch((error) => {
        if (error?.name !== 'AbortError') state.focusError = text(error?.message, 'Focus state could not be restored.');
        return null;
      })
    : Promise.resolve(null);
  const actionPromise = requestedView === 'home'
    ? loadActionCenter({ signal: controller.signal })
      .catch((error) => {
        if (error?.name !== 'AbortError') {
          state.actionCenter.error = text(error?.message, 'Action review could not be loaded.');
          state.actionCenter.loading = false;
        }
        return false;
      })
      .then((loaded) => {
        if (token === sequence && state.open && requestedView === state.view) render();
        return loaded;
      })
    : Promise.resolve(false);
  try {
    const endpoint = requestedView === 'activity'
      ? `${API_BASE}/api/mission-control/activity?limit=30`
      : `${API_BASE}/api/mission-control/today?utc_offset_minutes=${localOffsetMinutes()}`;
    const response = await fetch(endpoint, {
      credentials: 'same-origin', signal: controller.signal,
    });
    if (!response.ok) {
      let detail = '';
      try { detail = text((await response.json()).detail); } catch (_) {}
      throw new Error(detail || `Mission Control request failed (HTTP ${response.status})`);
    }
    const payload = await response.json();
    await focusPromise;
    void actionPromise;
    if (token !== sequence || !state.open || requestedView !== state.view) return;
    if (requestedView === 'activity') {
      if (!payload?.feed || !payload?.health) throw new Error('Activity returned an invalid response');
      state.data = {
        as_of: payload.as_of,
        activity: payload.feed,
        sources: { health: payload.health },
      };
    } else {
      state.data = payload && typeof payload === 'object' ? payload : null;
      if (!state.data?.sources) throw new Error('Mission Control returned an invalid response');
    }
  } catch (error) {
    if (error?.name === 'AbortError' || token !== sequence) return;
    state.error = text(error?.message, 'Mission Control could not load');
  } finally {
    if (token === sequence) {
      state.loading = false;
      render();
    }
  }
}

async function recoverCurrentFocus({ autoEnter = false } = {}) {
  if (!state.open || state.view !== 'home') return null;
  focusController?.abort();
  focusController = new AbortController();
  try {
    return await loadCurrentFocus({ signal: focusController.signal, autoEnter });
  } catch (error) {
    if (error?.name !== 'AbortError') state.focusError = text(error?.message, 'Focus state could not be restored.');
    return state.focusSession;
  } finally {
    focusController = null;
    render();
  }
}

function activityCounts(items) {
  return rows(items).reduce((counts, item) => {
    const key = text(item?.source, 'unknown');
    counts[key] = number(counts[key]) + 1;
    return counts;
  }, {});
}

async function loadMoreActivity() {
  const current = state.data?.activity;
  if (
    state.view !== 'activity'
    || state.activityLoadingMore
    || !current?.truncated
    || !current?.next_before
    || !current?.next_before_id
  ) return false;

  const requestedView = state.view;
  const token = sequence;
  const params = new URLSearchParams({
    limit: '30',
    before: String(current.next_before),
    before_id: String(current.next_before_id),
  });
  activityMoreController?.abort();
  activityMoreController = new AbortController();
  state.activityLoadingMore = true;
  render();
  try {
    const response = await fetch(`${API_BASE}/api/mission-control/activity?${params}`, {
      credentials: 'same-origin', signal: activityMoreController.signal,
    });
    if (!response.ok) {
      let detail = '';
      try { detail = text((await response.json()).detail); } catch (_) {}
      throw new Error(detail || `Activity request failed (HTTP ${response.status})`);
    }
    const payload = await response.json();
    if (token !== sequence || !state.open || requestedView !== state.view) return false;
    if (!payload?.feed || !Array.isArray(payload.feed.items)) {
      throw new Error('Activity returned an invalid response');
    }
    const seen = new Set(rows(current.items).map((item) => text(item?.id)).filter(Boolean));
    const appended = rows(payload.feed.items).filter((item) => {
      const id = text(item?.id);
      if (!id || seen.has(id)) return false;
      seen.add(id);
      return true;
    });
    const items = [...rows(current.items), ...appended];
    state.data.activity = {
      ...payload.feed,
      items,
      count: items.length,
      source_counts: activityCounts(items),
    };
    if (payload.health) state.data.sources.health = payload.health;
    return true;
  } catch (error) {
    if (error?.name !== 'AbortError') {
      notify(text(error?.message, 'Could not load older activity'), true);
    }
    return false;
  } finally {
    if (token === sequence) {
      state.activityLoadingMore = false;
      activityMoreController = null;
      render();
    }
  }
}

function notify(message, error = false) {
  if (window.uiModule?.showToast && !error) window.uiModule.showToast(message);
  else if (window.uiModule?.showError && error) window.uiModule.showError(message);
  else if (error) console.error(message);
}

async function planningRequest(path, body) {
  const response = await fetch(`${API_BASE}/api/planning${path}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = '';
    try { detail = text((await response.json()).detail); } catch (_) {}
    throw new Error(detail || `Planning request failed (HTTP ${response.status})`);
  }
  return response.json();
}

async function createPlanningItem(form) {
  const values = new FormData(form);
  const title = text(values.get('title'));
  if (!title) return;
  const submit = form.querySelector('button[type="submit"]');
  if (submit) submit.disabled = true;
  try {
    await planningRequest('', {
      title,
      due_date: text(values.get('due_date')) || null,
    });
    form.reset();
    notify('Planning item added');
    await loadCurrentView();
  } catch (error) {
    notify(text(error?.message, 'Could not add planning item'), true);
  } finally {
    if (submit?.isConnected) submit.disabled = false;
  }
}

async function mutatePlanningItem(control) {
  const row = control.closest?.('[data-planning-id]');
  if (!row) return;
  const itemId = text(row.dataset.planningId);
  const version = number(row.dataset.version);
  if (!itemId || !version) return;
  const action = control.dataset.action;
  let suffix = '';
  let payload = { version };
  if (action === 'planning-complete') suffix = `/${encodeURIComponent(itemId)}/complete`;
  else if (action === 'planning-reopen') suffix = `/${encodeURIComponent(itemId)}/reopen`;
  else if (action === 'planning-schedule') {
    const due = text(row.querySelector('.mission-plan-date')?.value);
    if (!due) {
      notify('Choose a date before scheduling', true);
      return;
    }
    const start = new Date(`${due}T09:00:00`);
    if (Number.isNaN(start.getTime())) {
      notify('Choose a valid schedule date', true);
      return;
    }
    const end = new Date(start.getTime() + 30 * 60 * 1000);
    suffix = `/${encodeURIComponent(itemId)}/schedule`;
    payload = {
      version,
      start: start.toISOString(),
      end: end.toISOString(),
      due_date: due,
      add_to_calendar: true,
    };
  } else return;
  control.disabled = true;
  try {
    await planningRequest(suffix, payload);
    notify(action === 'planning-schedule' ? 'Added to Calendar' : action === 'planning-reopen' ? 'Planning item reopened' : 'Objective cleared');
    await loadCurrentView();
  } catch (error) {
    notify(text(error?.message, 'Could not update planning item'), true);
  } finally {
    if (control.isConnected) control.disabled = false;
  }
}

function buildWorkspace() {
  if (refs.root?.isConnected) return refs.root;
  ensureStylesheet();
  const root = make('section', {
    id: 'mission-control-workspace', className: 'mission-workspace',
    attrs: { tabindex: '-1', 'aria-labelledby': 'mission-title', 'data-no-swipe-dismiss': 'true' },
  });
  const header = make('header', { className: 'mission-topbar' });
  const heading = make('div', {}, [
    make('p', { className: 'mission-eyebrow', text: 'Personal Mission Control' }),
    make('h1', { id: 'mission-title', text: 'Today' }),
    make('p', { className: 'mission-date-line' }, [
      make('span', { id: 'mission-date', text: 'Today' }),
      make('span', { id: 'mission-as-of' }),
    ]),
  ]);
  const actions = make('div', { className: 'mission-topbar-actions' }, [
    button('Return to Today', 'focus-return-today', {
      className: 'mission-btn mission-btn-quiet', attrs: { hidden: 'hidden' },
    }),
    button('Refresh', 'refresh', { className: 'mission-btn' }),
    button('Close', 'close', { className: 'mission-btn mission-btn-quiet' }),
  ]);
  header.append(heading, actions);
  const summary = make('div', { className: 'mission-summary', attrs: { 'aria-label': 'Today summary' } });
  const quickActions = make('nav', { className: 'mission-quick-actions', attrs: { 'aria-label': 'Quick actions' } });
  const loading = make('div', { className: 'mission-loading', text: 'Building today’s view…', attrs: { role: 'status' } });
  const error = make('div', { className: 'mission-load-error', hidden: true, attrs: { role: 'alert' } });
  const focusHost = make('div', {
    className: 'mission-focus-host', hidden: true,
    attrs: { tabindex: '-1' },
  });
  const content = make('div', { className: 'mission-content', hidden: true });
  const askRestia = make('section', {
    className: 'mission-ask-restia',
    attrs: { 'aria-label': 'Ask Restia' },
  }, [
    make('div', {}, [
      make('strong', { text: 'Ask Restia anything…' }),
      make('span', { text: 'Plan the day, recall a decision, find a document, or prepare a safe action.' }),
    ]),
    button('Start with Restia', 'open-target', {
      className: 'mission-ask-restia-button', target: 'new-chat',
      title: 'Open a new Restia conversation',
    }),
  ]);
  const focusStatus = make('aside', {
    className: 'mission-focus-status', hidden: true,
    attrs: { 'aria-label': 'Current Focus status' },
  });
  const grid = make('div', { className: 'mission-grid' });
  content.append(focusStatus, summary, quickActions, grid, askRestia);
  root.append(header, loading, error, focusHost, content);
  document.body.appendChild(root);
  refs = {
    root, summary, quickActions, loading, error, focusHost, focusStatus,
    askRestia, content, grid,
    focusToggle: actions.querySelector('[data-action="focus-return-today"]'),
    title: heading.querySelector('#mission-title'),
    eyebrow: heading.querySelector('.mission-eyebrow'),
    date: heading.querySelector('#mission-date'), asOf: heading.querySelector('#mission-as-of'),
  };
  root.addEventListener('click', (event) => {
    const control = event.target.closest?.('[data-action]');
    if (!control || !root.contains(control)) return;
    if (control.dataset.action === 'close') close();
    else if (control.dataset.action === 'refresh') loadCurrentView();
    else if (control.dataset.action === 'focus-return-today') {
      state.focusView = false;
      state.focusReturnedToToday = Boolean(state.focusSession);
      state.focusSetup = null;
      state.focusResult = null;
      render();
      refs.root?.focus?.();
    }
    else if (control.dataset.action === 'focus-return') {
      state.focusView = true;
      state.focusReturnedToToday = false;
      render();
      refs.focusHost?.focus?.();
    }
    else if (control.dataset.action === 'focus-cancel-setup') {
      state.focusSetup = null;
      state.focusView = false;
      render();
    }
    else if (control.dataset.action === 'focus-retry') {
      void loadCurrentFocus({ autoEnter: false }).then(() => render());
    }
    else if (control.dataset.action === 'focus-prepare') {
      const item = findFocusRecommendation(control.dataset.focusRecommendationId);
      const candidate = focusCandidateFromRecommendation(item);
      if (candidate) void prepareFocus(candidate);
    }
    else if (control.dataset.action === 'focus-pause') void mutateFocus('pause');
    else if (control.dataset.action === 'focus-resume') void mutateFocus('resume');
    else if (control.dataset.action === 'focus-abandon') {
      const form = control.closest?.('.mission-focus-finish-form');
      if (form) void finishFocus('abandon', form);
    }
    else if (control.dataset.action === 'activity-more') void loadMoreActivity();
    else if (control.dataset.action === 'action-retry') void refreshActionCenter();
    else if (control.dataset.action === 'action-review') {
      const actionId = text(control.dataset.actionId);
      const purpose = control.dataset.actionPurpose === 'reverse' ? 'reverse' : 'execute';
      const key = `${purpose}:${actionId}`;
      state.actionCenter.reviewedKey = state.actionCenter.reviewedKey === key ? '' : key;
      render();
      if (state.actionCenter.reviewedKey) {
        refs.root?.querySelector(`#${actionReviewId({ id: actionId }, purpose)}`)?.focus?.();
      }
    }
    else if (control.dataset.action === 'action-execute') {
      void mutateReviewedAction('execute', control.dataset.actionId);
    }
    else if (control.dataset.action === 'action-reject') {
      void mutateReviewedAction('reject', control.dataset.actionId);
    }
    else if (control.dataset.action === 'action-reverse') {
      void mutateReviewedAction('reverse', control.dataset.actionId);
    }
    else if (control.dataset.action === 'open-target') {
      const target = text(control.dataset.target, 'destination');
      if (!triggerTarget(target)) {
        notify(`Could not open ${target.replace(/[-_]+/g, ' ')}`, true);
      }
    }
    else if (control.dataset.action.startsWith('planning-')) void mutatePlanningItem(control);
  });
  root.addEventListener('submit', (event) => {
    const focusSetupForm = event.target.closest?.('.mission-focus-setup-form');
    if (focusSetupForm && root.contains(focusSetupForm)) {
      event.preventDefault();
      void startPreparedFocus(focusSetupForm);
      return;
    }
    const focusCapture = event.target.closest?.('.mission-focus-capture-form');
    if (focusCapture && root.contains(focusCapture)) {
      event.preventDefault();
      void saveFocusEntry(focusCapture);
      return;
    }
    const focusFinish = event.target.closest?.('.mission-focus-finish-form');
    if (focusFinish && root.contains(focusFinish)) {
      event.preventDefault();
      void finishFocus('complete', focusFinish);
      return;
    }
    const form = event.target.closest?.('.mission-plan-form');
    if (!form || !root.contains(form)) return;
    event.preventDefault();
    void createPlanningItem(form);
  });
  root.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      if (state.focusView) {
        state.focusView = false;
        state.focusReturnedToToday = Boolean(state.focusSession);
        state.focusSetup = null;
        state.focusResult = null;
        render();
      } else close();
    }
  });
  return root;
}

export function init(apiBase = '') {
  if (apiBase) API_BASE = String(apiBase).replace(/\/$/, '');
  state.initialized = true;
  if (typeof document !== 'undefined' && !focusRecoveryBound) {
    focusRecoveryBound = true;
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') void recoverCurrentFocus({ autoEnter: false });
    });
    window.addEventListener('online', () => void recoverCurrentFocus({ autoEnter: false }));
  }
  return missionControlModule;
}

export async function open(view = 'home') {
  if (!state.initialized) init();
  const nextView = view === 'activity' ? 'activity' : 'home';
  if (!state.open) {
    if (window.studyModule?.isActive?.()) {
      const closed = await window.studyModule.close({ startFresh: false });
      if (!closed && window.studyModule?.isActive?.()) return false;
    }
    if (window.projectsModule?.isOpen?.()) {
      const closed = await window.projectsModule.close();
      if (!closed) return false;
    }
    // Registered floating tools have z-indexes above full workspaces. Preserve
    // their state in the dock instead of leaving Home hidden behind them.
    await minimizeWorkspaceModals();
    buildWorkspace();
    previousFocus = document.activeElement;
    state.open = true;
    state.focusReturnedToToday = false;
    document.body.classList.add('mission-control-view');
    refs.root.hidden = false;
    refs.root.focus();
  }
  state.view = nextView;
  state.data = null;
  state.error = '';
  state.loading = true;
  refs.loading.textContent = nextView === 'activity' ? 'Loading recent activity…' : 'Building today’s view…';
  document.body.dataset.missionView = nextView;
  render();
  // First paint is independent from session restoration and backend
  // aggregation. Navigation becomes visible now; data fills in asynchronously.
  void loadCurrentView();
  document.dispatchEvent(new CustomEvent('restia:mission-control-opened', { detail: { view: nextView } }));
  return true;
}

export function close() {
  if (!state.open) return true;
  sequence += 1;
  controller?.abort();
  controller = null;
  activityMoreController?.abort();
  activityMoreController = null;
  focusController?.abort();
  focusController = null;
  actionController?.abort();
  actionController = null;
  if (focusTimer) clearInterval(focusTimer);
  focusTimer = null;
  state.activityLoadingMore = false;
  state.open = false;
  state.focusView = false;
  state.focusReturnedToToday = false;
  state.focusSetup = null;
  state.focusResult = null;
  state.actionCenter.reviewedKey = '';
  state.actionCenter.busyId = '';
  document.body.classList.remove('mission-control-view');
  delete document.body.dataset.missionView;
  if (refs.root) refs.root.hidden = true;
  document.dispatchEvent(new CustomEvent('restia:mission-control-closed'));
  try { previousFocus?.focus?.(); } catch (_) {}
  previousFocus = null;
  return true;
}

export function toggle() {
  return state.open ? close() : open();
}

export function isOpen() {
  return state.open;
}

export function refresh() {
  return state.open ? loadCurrentView() : Promise.resolve(false);
}

export const __test = Object.freeze({
  localOffsetMinutes, focusCandidates, mapBackendActions,
  sourceMessage, sourceUnavailable, healthUnavailable, sourceValueUnavailable,
  unavailableSourceNames, summaryMetricValue,
  normalizeInboxAttention, activateInboxNavigation, triggerTarget,
  normalizeFocusTarget, normalizeFocusSession, normalizeFocusCandidate,
  focusDisplayElapsed, focusStartBody, followUpsFromForm,
  normalizeActionProposal, executeReviewedAction, reverseReviewedAction,
  actionRisk,
});

const missionControlModule = {
  init, open, close, toggle, isOpen, refresh, prepareFocus, __test,
};
export default missionControlModule;

if (typeof window !== 'undefined') window.missionControlModule = missionControlModule;
