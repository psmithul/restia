// Restia V3 owner-scoped Life workspace.
//
// This first slice is intentionally read-only. The Life graph remains the
// authority; the UI requests a bounded page and never mirrors identity or
// entity state into browser storage.

const MAX_ENTITIES = 50;
const FILTER_TYPES = Object.freeze({
  all: Object.freeze([]),
  goal: Object.freeze(['goal']),
  project: Object.freeze(['project', 'milestone']),
  people: Object.freeze(['person', 'interaction', 'commitment', 'communication_thread', 'message']),
  health: Object.freeze(['health_record', 'habit', 'metric']),
  money: Object.freeze(['transaction', 'asset']),
  learning: Object.freeze(['learning_record', 'career_item']),
  work: Object.freeze(['workspace', 'task', 'action', 'decision']),
  home: Object.freeze(['home_record', 'place']),
  journal: Object.freeze(['journal_entry', 'period_review', 'note']),
  files: Object.freeze(['file', 'source']),
});
const FILTERS = new Set(Object.keys(FILTER_TYPES));

let API_BASE = typeof window !== 'undefined' ? window.location.origin : '';
let refs = {};
let loadController = null;
let loadSequence = 0;

const state = {
  initialized: false,
  open: false,
  loading: false,
  error: '',
  filter: 'all',
  items: [],
  count: 0,
  truncated: false,
  previousFocus: null,
};

function text(value, fallback = '') {
  const normalized = String(value ?? '').trim();
  return normalized || fallback;
}

function object(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

function number(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function entityType(value) {
  return text(value, 'other').toLowerCase().replace(/[\s-]+/g, '_');
}

export function normalizeLifeEntity(value) {
  const raw = object(value);
  return {
    id: text(raw.id),
    type: entityType(raw.entity_type ?? raw.type),
    title: text(raw.title ?? raw.name, 'Untitled entity').slice(0, 240),
    summary: text(raw.summary ?? raw.description).slice(0, 20_000),
    status: text(raw.status, 'active').toLowerCase(),
    properties: object(raw.properties),
    provenance: object(raw.provenance),
    confidence: Math.max(0, Math.min(100, Math.round(number(raw.confidence, 100)))),
    sensitivity: text(raw.sensitivity, 'private'),
    domainRefType: text(raw.domain_ref_type),
    domainRefId: text(raw.domain_ref_id),
    occurredAt: text(raw.occurred_at),
    dueAt: text(raw.due_at),
    reviewAt: text(raw.review_at),
    updatedAt: text(raw.updated_at ?? raw.created_at),
    version: Math.max(1, Math.trunc(number(raw.version, 1))),
  };
}

export function unwrapLifePage(payload) {
  const root = object(payload);
  const source = Array.isArray(payload) ? payload : (Array.isArray(root.items) ? root.items : []);
  const items = source.slice(0, MAX_ENTITIES).map(normalizeLifeEntity);
  return {
    items,
    count: Math.max(0, Math.trunc(number(root.count, source.length))),
    truncated: Boolean(root.truncated || source.length > MAX_ENTITIES),
  };
}

function apiUrl() {
  return `${String(API_BASE || '').replace(/\/$/, '')}/api/life/entities?limit=${MAX_ENTITIES}`;
}

function errorMessage(payload, status) {
  const root = object(payload);
  const detail = root.detail ?? root.error ?? root.message;
  if (typeof detail === 'string' && detail.trim()) return detail.trim();
  const structured = object(detail);
  return text(structured.message ?? structured.detail ?? structured.error,
    `Life request failed (${status}).`);
}

export async function listLifeEntities({ signal } = {}) {
  const response = await fetch(apiUrl(), {
    method: 'GET', credentials: 'same-origin', headers: { Accept: 'application/json' }, signal,
  });
  const raw = await response.text();
  let payload = null;
  if (raw) {
    try { payload = JSON.parse(raw); } catch (_) { payload = { detail: raw }; }
  }
  if (!response.ok) throw new Error(errorMessage(payload, response.status));
  return unwrapLifePage(payload);
}

function humanize(value) {
  return text(value, 'Other')
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatDate(value) {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return { label: text(value), iso: '' };
  try {
    return {
      label: new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' }).format(parsed),
      iso: parsed.toISOString(),
    };
  } catch (_) {
    return { label: parsed.toLocaleDateString(), iso: parsed.toISOString() };
  }
}

function make(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.id) element.id = options.id;
  if (options.className) element.className = options.className;
  if (options.text !== undefined) element.textContent = String(options.text);
  if (options.type) element.type = options.type;
  Object.entries(options.attrs || {}).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') element.setAttribute(key, String(value));
  });
  const rows = Array.isArray(children) ? children : [children];
  rows.filter(Boolean).forEach((child) => element.appendChild(
    typeof Node !== 'undefined' && child instanceof Node
      ? child : document.createTextNode(String(child)),
  ));
  return element;
}

function visibleItems() {
  if (state.filter === 'all') return state.items;
  const types = FILTER_TYPES[state.filter] || [];
  return state.items.filter((item) => types.includes(item.type));
}

function typeCounts() {
  return Object.fromEntries(Object.entries(FILTER_TYPES).map(([filter, types]) => [
    filter,
    filter === 'all'
      ? state.items.length
      : state.items.filter((item) => types.includes(item.type)).length,
  ]));
}

function syncFilters() {
  const counts = typeCounts();
  refs.filters?.querySelectorAll('[data-life-filter]').forEach((button) => {
    const filter = button.dataset.lifeFilter;
    const active = filter === state.filter;
    const count = filter === 'all' ? state.items.length : (counts[filter] || 0);
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-pressed', String(active));
    const countNode = button.querySelector('[data-life-filter-count]');
    if (countNode) countNode.textContent = String(count);
  });
}

function entityMeta(item) {
  const rows = [];
  const due = formatDate(item.dueAt);
  const review = formatDate(item.reviewAt);
  if (due) rows.push(make('span', {}, ['Due ', make('time', { text: due.label, attrs: { datetime: due.iso } })]));
  if (review) rows.push(make('span', {}, ['Review ', make('time', { text: review.label, attrs: { datetime: review.iso } })]));
  if (item.domainRefType) rows.push(make('span', { text: `Linked to ${humanize(item.domainRefType)}` }));
  rows.push(make('span', { text: `${item.confidence}% confidence` }));
  return rows;
}

function entityCard(item) {
  const headingId = `life-entity-${item.id || Math.random().toString(36).slice(2)}`;
  return make('article', {
    className: 'life-entity-card',
    attrs: {
      role: 'listitem', tabindex: '0', 'aria-labelledby': headingId,
      'data-life-entity-type': item.type,
    },
  }, [
    make('header', { className: 'life-entity-header' }, [
      make('span', { className: `life-entity-type life-type-${item.type}`, text: humanize(item.type) }),
      make('span', { className: `life-entity-status life-status-${item.status}`, text: humanize(item.status) }),
    ]),
    make('h3', { id: headingId, text: item.title }),
    item.summary ? make('p', { className: 'life-entity-summary', text: item.summary.slice(0, 600) }) : null,
    make('div', { className: 'life-entity-meta' }, entityMeta(item)),
  ]);
}

function render() {
  if (!state.initialized) return;
  syncFilters();
  const items = visibleItems();
  const hasItems = items.length > 0;
  refs.loading.hidden = !state.loading;
  refs.error.hidden = !state.error || state.loading;
  refs.errorMessage.textContent = state.error;
  refs.empty.hidden = state.loading || Boolean(state.error) || hasItems;
  refs.list.hidden = state.loading || Boolean(state.error) || !hasItems;
  refs.list.replaceChildren(...items.map(entityCard));
  const suffix = state.truncated ? ' · showing the latest 50' : '';
  refs.count.textContent = `${items.length} of ${state.count} ${items.length === 1 ? 'entity' : 'entities'}${suffix}`;
}

function announce(message) {
  if (!refs.live) return;
  refs.live.textContent = '';
  requestAnimationFrame(() => { refs.live.textContent = text(message); });
}

export async function loadLifeEntities({ focusList = false } = {}) {
  if (!state.initialized) return [];
  const sequence = ++loadSequence;
  loadController?.abort();
  loadController = new AbortController();
  state.loading = true;
  state.error = '';
  render();
  try {
    const page = await listLifeEntities({ signal: loadController.signal });
    if (sequence !== loadSequence) return state.items;
    state.items = page.items;
    state.count = page.count;
    state.truncated = page.truncated;
    state.loading = false;
    render();
    if (focusList && state.items.length) refs.list.querySelector('.life-entity-card')?.focus?.({ preventScroll: true });
    announce(`${state.items.length} Life entities loaded.`);
    return state.items;
  } catch (error) {
    if (error?.name === 'AbortError' || sequence !== loadSequence) return state.items;
    state.loading = false;
    state.error = text(error?.message, 'Could not load Life.');
    render();
    announce(state.error);
    return state.items;
  }
}

async function minimizeWorkspaceModals() {
  try {
    const manager = await import('./modalManager.js');
    return manager.minimizeVisibleModals?.() || 0;
  } catch (_) {
    return 0;
  }
}

function commitLifeRoute(mode) {
  if (typeof window === 'undefined' || mode === 'none' || window.location.pathname === '/life') return;
  const method = mode === 'replace' ? 'replaceState' : 'pushState';
  window.history?.[method]?.({ restiaNavigation: 'life' }, '', '/life');
  document.title = 'Life — Restia';
}

async function leaveToRestia() {
  if (typeof window !== 'undefined' && typeof window.activateNavigationItem === 'function') {
    await window.activateNavigationItem('chat');
    return;
  }
  close({ restoreFocus: false });
}

function bindEvents() {
  refs.refresh.addEventListener('click', () => void loadLifeEntities({ focusList: true }));
  refs.retry.addEventListener('click', () => void loadLifeEntities({ focusList: true }));
  refs.close.addEventListener('click', () => void leaveToRestia());
  refs.filters.addEventListener('click', (event) => {
    const control = event.target.closest?.('[data-life-filter]');
    if (!control || !FILTERS.has(control.dataset.lifeFilter)) return;
    state.filter = control.dataset.lifeFilter;
    render();
    refs.list.querySelector('.life-entity-card')?.focus?.({ preventScroll: true });
  });
  refs.root.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    event.preventDefault();
    void leaveToRestia();
  });
}

export function init(apiBase = '') {
  if (apiBase) API_BASE = String(apiBase).replace(/\/$/, '');
  if (typeof document === 'undefined') return lifeWorkspaceModule;
  refs = {
    root: document.getElementById('life-workspace'),
    refresh: document.getElementById('life-refresh'),
    close: document.getElementById('life-close'),
    filters: document.getElementById('life-filters'),
    loading: document.getElementById('life-loading'),
    error: document.getElementById('life-error'),
    errorMessage: document.getElementById('life-error-message'),
    retry: document.getElementById('life-retry'),
    empty: document.getElementById('life-empty'),
    list: document.getElementById('life-list'),
    count: document.getElementById('life-count'),
    live: document.getElementById('life-live-region'),
  };
  if (Object.values(refs).some((element) => !element)) return lifeWorkspaceModule;
  if (!state.initialized) bindEvents();
  state.initialized = true;
  render();
  return lifeWorkspaceModule;
}

export async function open({ historyMode = 'push' } = {}) {
  if (!state.initialized) init();
  if (!state.initialized) return false;
  if (state.open) {
    commitLifeRoute(historyMode);
    focus();
    return true;
  }
  if (typeof window !== 'undefined' && window.projectsModule?.isOpen?.()) {
    const closed = await window.projectsModule.close();
    if (!closed) return false;
  }
  if (typeof window !== 'undefined' && window.studyModule?.isActive?.()) {
    const closed = await window.studyModule.close({ startFresh: false });
    if (!closed && window.studyModule?.isActive?.()) return false;
  }
  if (typeof window !== 'undefined') {
    window.inboxModule?.close?.({ restoreFocus: false });
    window.missionControlModule?.close?.();
  }
  await minimizeWorkspaceModals();
  state.previousFocus = document.activeElement;
  state.open = true;
  refs.root.hidden = false;
  document.body.classList.add('life-view');
  commitLifeRoute(historyMode);
  document.dispatchEvent(new CustomEvent('restia:life-opened'));
  focus();
  void loadLifeEntities();
  return true;
}

export function close({ restoreFocus = true } = {}) {
  if (!state.open) return true;
  loadSequence += 1;
  loadController?.abort();
  loadController = null;
  state.open = false;
  state.loading = false;
  document.body.classList.remove('life-view');
  refs.root.hidden = true;
  document.dispatchEvent(new CustomEvent('restia:life-closed'));
  if (restoreFocus) {
    try { state.previousFocus?.focus?.({ preventScroll: true }); } catch (_) {}
  }
  state.previousFocus = null;
  return true;
}

export function isOpen() {
  return state.open;
}

export function focus() {
  if (!state.open || !refs.root) return false;
  const target = refs.filters.querySelector('[aria-pressed="true"]') || refs.root;
  try { target.focus({ preventScroll: true }); } catch (_) { try { target.focus(); } catch (_) {} }
  return true;
}

export const __test = Object.freeze({ MAX_ENTITIES, FILTERS, FILTER_TYPES, humanize, formatDate });

const lifeWorkspaceModule = {
  init, open, close, isOpen, focus, loadLifeEntities, listLifeEntities, __test,
};

export default lifeWorkspaceModule;

if (typeof window !== 'undefined') window.lifeWorkspaceModule = lifeWorkspaceModule;
