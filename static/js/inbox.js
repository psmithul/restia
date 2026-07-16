// Restia V3 universal inbox workspace.
//
// Capture stays intentionally destination-free. Classification and processing
// are explicit review actions backed by the owner-scoped /api/inbox contract.

const STATUS_FILTERS = new Set(['all', 'inbox', 'processed', 'archived']);
const DIRECT_PROCESS_KINDS = new Set(['task']);

let API_BASE = typeof window !== 'undefined' ? window.location.origin : '';
let refs = {};
let loadController = null;
let loadSequence = 0;

const state = {
  initialized: false,
  open: false,
  loading: false,
  loadingMore: false,
  captureBusy: false,
  error: '',
  paginationError: '',
  filter: 'inbox',
  items: [],
  nextCursor: null,
  previousFocus: null,
  pendingCapture: null,
};

function text(value, fallback = '') {
  const normalized = String(value ?? '').trim();
  return normalized || fallback;
}

function number(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function object(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
}

export function unwrapInboxItems(payload) {
  if (Array.isArray(payload)) return payload;
  const row = object(payload);
  if (!row) return [];
  if (Array.isArray(row.items)) return row.items;
  if (object(row.item)) return [row.item];
  return row.id || row.item_id ? [row] : [];
}

export function unwrapInboxItem(payload) {
  if (Array.isArray(payload)) return object(payload[0]);
  const row = object(payload);
  if (!row) return null;
  if (object(row.item)) return row.item;
  if (Array.isArray(row.items)) return object(row.items[0]);
  return row.id || row.item_id ? row : null;
}

export function unwrapInboxPage(payload) {
  const row = object(payload);
  return {
    items: unwrapInboxItems(payload).map(normalizeInboxItem),
    count: Math.max(0, Math.trunc(number(row?.count, unwrapInboxItems(payload).length))),
    truncated: Boolean(row?.truncated),
    nextCursor: text(row?.next_cursor) || null,
  };
}

export function mergeInboxItems(current = [], incoming = []) {
  const merged = [...current];
  const seen = new Set(current.map((item) => text(item?.id)).filter(Boolean));
  incoming.forEach((item) => {
    const id = text(item?.id);
    if (!id || seen.has(id)) return;
    seen.add(id);
    merged.push(item);
  });
  return merged;
}

function classificationValue(raw, key, aliases = []) {
  const classification = object(raw.classification) || {};
  for (const name of [key, ...aliases]) {
    if (raw[name] !== undefined && raw[name] !== null) return raw[name];
    if (classification[name] !== undefined && classification[name] !== null) return classification[name];
  }
  return null;
}

export function normalizeInboxItem(value) {
  const raw = object(value) || {};
  const content = text(raw.content ?? raw.text ?? raw.body);
  const firstLine = content.split(/\r?\n/, 1)[0].slice(0, 160);
  const status = text(raw.status, 'inbox').toLowerCase();
  return {
    id: text(raw.id ?? raw.item_id),
    title: text(raw.title ?? raw.name, firstLine || 'Untitled capture'),
    content,
    kind: text(classificationValue(raw, 'kind', ['label', 'classification_label']), 'unclassified'),
    status,
    sourceType: text(raw.source_type ?? raw.source, 'user'),
    sourceRef: text(raw.source_ref ?? raw.source_reference),
    confidence: classificationValue(raw, 'classification_confidence', ['confidence']),
    reason: text(classificationValue(raw, 'classification_reason', ['reason'])),
    processedTargetType: text(raw.processed_target_type ?? raw.target_type),
    processedTargetId: text(raw.processed_target_id ?? raw.target_id),
    version: Math.max(1, Math.trunc(number(raw.version, 1))),
    createdAt: text(raw.created_at ?? raw.createdAt),
    updatedAt: text(raw.updated_at ?? raw.updatedAt ?? raw.created_at ?? raw.createdAt),
    raw,
  };
}

export function inboxErrorMessage(payload, fallback = 'Inbox request failed.') {
  const root = object(payload) || {};
  const detail = root.detail ?? root.error ?? root.message ?? payload;
  if (typeof detail === 'string') return text(detail, fallback);
  const structured = object(detail);
  if (structured) {
    const message = text(structured.message ?? structured.detail ?? structured.error);
    const kind = text(structured.kind);
    if (message && kind) return `${message} (${humanize(kind)})`;
    return message || kind || fallback;
  }
  return fallback;
}

function apiUrl(path = '') {
  return `${String(API_BASE || '').replace(/\/$/, '')}/api/inbox${path}`;
}

async function requestJSON(path = '', options = {}) {
  const init = {
    credentials: 'same-origin',
    headers: { Accept: 'application/json', ...(options.headers || {}) },
    ...options,
  };
  if (Object.prototype.hasOwnProperty.call(options, 'body') && options.body !== undefined) {
    init.headers = { ...init.headers, 'Content-Type': 'application/json' };
    init.body = typeof options.body === 'string' ? options.body : JSON.stringify(options.body);
  }
  const response = await fetch(apiUrl(path), init);
  const raw = await response.text();
  let payload = null;
  if (raw) {
    try { payload = JSON.parse(raw); } catch (_) { payload = { detail: raw }; }
  }
  if (!response.ok) {
    const error = new Error(inboxErrorMessage(payload, `Inbox request failed (${response.status}).`));
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

export async function listInboxItems(status = 'inbox', { signal, cursor = '', limit = 100 } = {}) {
  const filter = STATUS_FILTERS.has(status) ? status : 'inbox';
  const boundedLimit = Math.max(1, Math.min(100, Math.trunc(number(limit, 100))));
  const query = new URLSearchParams({ status: filter, limit: String(boundedLimit) });
  if (text(cursor)) query.set('cursor', text(cursor));
  const payload = await requestJSON(`?${query.toString()}`, { method: 'GET', signal });
  return unwrapInboxPage(payload);
}

function captureKey() {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID();
    }
  } catch (_) {}
  return `inbox-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export async function createInboxCapture(content, { idempotencyKey = '' } = {}) {
  const value = text(content);
  if (!value) throw new Error('Enter something to capture.');
  const key = text(idempotencyKey) || captureKey();
  // Give cross-surface summaries a useful, bounded preview without exposing
  // the full encrypted capture body. The API keeps content as the authority;
  // title is only the first non-empty line used by Today/search/card headers.
  const title = value.split(/\r?\n/).find((line) => text(line))?.trim().slice(0, 240) || '';
  const payload = await requestJSON('', {
    method: 'POST',
    body: {
      title, content: value, source_type: 'user', idempotency_key: key,
    },
  });
  const item = unwrapInboxItem(payload);
  return item ? normalizeInboxItem(item) : null;
}

export async function patchInboxItem(itemId, version, changes = {}) {
  const payload = await requestJSON(`/${encodeURIComponent(text(itemId))}`, {
    method: 'PATCH',
    body: { ...changes, version: Math.max(1, Math.trunc(number(version, 1))) },
  });
  const item = unwrapInboxItem(payload);
  return item ? normalizeInboxItem(item) : null;
}

export async function mutateInboxItem(itemId, action, version, extra = {}) {
  if (!['classify', 'process', 'archive'].includes(action)) {
    throw new Error(`Unsupported inbox action: ${action}`);
  }
  const payload = await requestJSON(
    `/${encodeURIComponent(text(itemId))}/${action}`,
    {
      method: 'POST',
      body: { version: Math.max(1, Math.trunc(number(version, 1))), ...extra },
    },
  );
  const item = unwrapInboxItem(payload);
  return item ? normalizeInboxItem(item) : null;
}

function humanize(value) {
  return text(value, 'Unknown')
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatConfidence(value) {
  if (value === null || value === undefined || value === '') return 'Confidence not available';
  const parsed = number(value, Number.NaN);
  if (!Number.isFinite(parsed)) return `Confidence ${text(value)}`;
  const percentage = parsed >= 0 && parsed <= 1 ? parsed * 100 : parsed;
  return `Confidence ${Math.max(0, Math.min(100, Math.round(percentage)))}%`;
}

function formatUpdated(value) {
  if (!value) return { display: 'Update time unavailable', iso: '' };
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return { display: text(value), iso: '' };
  try {
    return {
      display: new Intl.DateTimeFormat(undefined, {
        dateStyle: 'medium', timeStyle: 'short',
      }).format(parsed),
      iso: parsed.toISOString(),
    };
  } catch (_) {
    return { display: parsed.toLocaleString(), iso: parsed.toISOString() };
  }
}

function make(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.id) element.id = options.id;
  if (options.className) element.className = options.className;
  if (options.text !== undefined) element.textContent = String(options.text);
  if (options.type) element.type = options.type;
  if (options.hidden) element.hidden = true;
  Object.entries(options.attrs || {}).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== '') element.setAttribute(key, String(value));
  });
  Object.entries(options.dataset || {}).forEach(([key, value]) => {
    if (value !== null && value !== undefined) element.dataset[key] = String(value);
  });
  const rows = Array.isArray(children) ? children : [children];
  rows.filter(Boolean).forEach((child) => element.appendChild(
    typeof Node !== 'undefined' && child instanceof Node
      ? child : document.createTextNode(String(child)),
  ));
  return element;
}

function classificationCopy(item) {
  return [formatConfidence(item.confidence), item.reason || 'No classification reason yet.'].join(' · ');
}

function sourceCopy(item) {
  const source = item.sourceType === 'user' ? 'Captured here' : humanize(item.sourceType);
  return [source, item.sourceRef].filter(Boolean).join(' · ');
}

function statusCopy(item) {
  if (item.status === 'processed' && item.processedTargetType) {
    return `Processed to ${humanize(item.processedTargetType)}`;
  }
  return humanize(item.status);
}

function processAvailability(item) {
  if (item.status === 'processed') {
    return { enabled: false, message: 'Already processed.' };
  }
  if (item.status === 'archived') {
    return { enabled: false, message: 'Archived items cannot be processed.' };
  }
  if (item.kind === 'project_information') {
    return {
      enabled: false,
      message: 'Processing project information needs a project destination; leave it in Inbox or archive it for now.',
    };
  }
  if (item.kind === 'archive') {
    return {
      enabled: false,
      message: 'This capture is classified for archiving. Use Archive below.',
    };
  }
  if (!DIRECT_PROCESS_KINDS.has(item.kind)) {
    return {
      enabled: false,
      message: `No safe automatic processor is available for ${humanize(item.kind)}; keep it classified or archive it.`,
    };
  }
  return { enabled: true, message: 'Process will create a To Do item.' };
}

function metaRow(label, value, { time = '' } = {}) {
  const content = time
    ? make('time', { text: value, attrs: { datetime: time } })
    : make('span', { text: value });
  return make('div', { className: 'inbox-meta-row' }, [
    make('dt', { text: label }),
    make('dd', {}, [content]),
  ]);
}

function actionButton(item, action, label, disabled = false, describedBy = '') {
  const button = make('button', {
    type: 'button',
    className: `inbox-action inbox-action-${action}`,
    text: label,
    dataset: { inboxAction: action, itemId: item.id },
    attrs: {
      'aria-label': `${label} ${item.title}`,
      'aria-describedby': describedBy,
    },
  });
  button.disabled = disabled;
  return button;
}

function renderCard(item) {
  const headingId = `inbox-item-${item.id || Math.random().toString(36).slice(2)}`;
  const updated = formatUpdated(item.updatedAt);
  const final = item.status === 'archived';
  const processed = item.status === 'processed';
  const process = processAvailability(item);
  const processHelpId = `${headingId}-process-help`;
  const card = make('article', {
    className: 'inbox-card',
    attrs: {
      role: 'listitem', tabindex: '-1', 'aria-labelledby': headingId,
      'data-inbox-item-id': item.id,
    },
  });
  const header = make('header', { className: 'inbox-card-header' }, [
    make('div', { className: 'inbox-card-title-wrap' }, [
      make('h3', { id: headingId, text: item.title }),
      make('p', { className: 'inbox-card-source', text: `Source: ${sourceCopy(item)}` }),
    ]),
    make('span', { className: `inbox-status inbox-status-${item.status}`, text: `Status: ${statusCopy(item)}` }),
  ]);
  const body = item.content && item.content !== item.title
    ? make('p', { className: 'inbox-card-content', text: item.content })
    : null;
  const meta = make('dl', { className: 'inbox-card-meta' }, [
    metaRow('Classification', humanize(item.kind)),
    metaRow('Reason', classificationCopy(item)),
    metaRow('Updated', updated.display, { time: updated.iso }),
  ]);
  const actions = make('div', { className: 'inbox-card-actions', attrs: { 'aria-label': `Actions for ${item.title}` } }, [
    actionButton(item, 'classify', 'Classify', final || processed),
    actionButton(item, 'process', 'Process', !process.enabled, processHelpId),
    actionButton(item, 'archive', 'Archive', final),
  ]);
  const processHelp = make('p', {
    id: processHelpId,
    className: 'inbox-process-guidance',
    text: process.message,
  });
  const error = make('p', {
    className: 'inbox-card-error', hidden: true,
    attrs: { role: 'alert', 'data-inbox-action-error': item.id },
  });
  card.append(header);
  if (body) card.append(body);
  card.append(meta, processHelp, actions, error);
  return card;
}

function visibleItems() {
  if (state.filter === 'all') return state.items;
  return state.items.filter((item) => item.status === state.filter);
}

function syncFilterControls() {
  refs.filters?.querySelectorAll('[data-inbox-filter]').forEach((button) => {
    const active = button.dataset.inboxFilter === state.filter;
    button.setAttribute('aria-pressed', String(active));
    button.classList.toggle('is-active', active);
  });
}

function render() {
  if (!state.initialized) return;
  syncFilterControls();
  const items = visibleItems();
  const hasItems = items.length > 0;
  refs.loading.hidden = !state.loading || hasItems;
  refs.error.hidden = !state.error || state.loading || hasItems;
  refs.errorMessage.textContent = state.error;
  refs.empty.hidden = state.loading || Boolean(state.error) || hasItems;
  refs.list.hidden = !hasItems;
  refs.list.replaceChildren(...items.map(renderCard));
  const countCopy = `${items.length} item${items.length === 1 ? '' : 's'} loaded`;
  refs.count.textContent = state.loading && hasItems ? `${countCopy} · Refreshing…` : countCopy;
  refs.pagination.hidden = !state.nextCursor && !state.paginationError;
  refs.loadMore.hidden = !state.nextCursor;
  refs.loadMore.disabled = state.loadingMore;
  refs.loadMore.textContent = state.loadingMore ? 'Loading more…' : 'Load more';
  refs.loadMore.setAttribute('aria-busy', String(state.loadingMore));
  refs.paginationError.hidden = !state.paginationError;
  refs.paginationError.textContent = state.paginationError;
}

function announce(message, error = false) {
  if (!refs.live) return;
  refs.live.textContent = '';
  requestAnimationFrame(() => { refs.live.textContent = text(message); });
  if (error) refs.live.dataset.tone = 'error';
  else delete refs.live.dataset.tone;
}

export async function loadInboxItems({ focusList = false, append = false } = {}) {
  if (!state.initialized) return [];
  if (append && (!state.nextCursor || state.loadingMore)) return state.items;
  const sequence = ++loadSequence;
  loadController?.abort();
  loadController = new AbortController();
  const requestedCursor = append ? state.nextCursor : null;
  if (!append) state.nextCursor = null;
  state.loading = !append;
  state.loadingMore = append;
  state.error = '';
  state.paginationError = '';
  render();
  try {
    const page = await listInboxItems(state.filter, {
      signal: loadController.signal,
      cursor: requestedCursor || '',
    });
    if (sequence !== loadSequence) return state.items;
    state.items = append ? mergeInboxItems(state.items, page.items) : page.items;
    state.nextCursor = page.nextCursor;
    state.loading = false;
    state.loadingMore = false;
    render();
    if (focusList && state.items.length) refs.list.querySelector('.inbox-card')?.focus({ preventScroll: true });
    if (append) announce(`${page.items.length} more Inbox item${page.items.length === 1 ? '' : 's'} loaded.`);
    return state.items;
  } catch (error) {
    if (error?.name === 'AbortError' || sequence !== loadSequence) return state.items;
    state.loading = false;
    state.loadingMore = false;
    const message = text(error?.message, 'Could not load Inbox.');
    if (state.items.length) state.paginationError = message;
    else state.error = message;
    render();
    announce(message, true);
    return state.items;
  }
}

async function handleCapture(event) {
  event.preventDefault();
  if (state.captureBusy) return;
  const content = text(refs.captureInput.value);
  if (!content) {
    refs.captureInput.setCustomValidity('Enter something to capture.');
    refs.captureInput.reportValidity();
    return;
  }
  refs.captureInput.setCustomValidity('');
  state.captureBusy = true;
  refs.captureForm.setAttribute('aria-busy', 'true');
  refs.captureInput.disabled = true;
  refs.captureButton.disabled = true;
  refs.captureButton.textContent = 'Capturing…';
  refs.captureStatus.textContent = 'Saving your capture…';
  try {
    if (!state.pendingCapture || state.pendingCapture.content !== content) {
      state.pendingCapture = { content, idempotencyKey: captureKey() };
    }
    const item = await createInboxCapture(content, state.pendingCapture);
    refs.captureInput.value = '';
    state.pendingCapture = null;
    state.filter = 'inbox';
    if (item) state.items = [item, ...state.items.filter((current) => current.id !== item.id)];
    render();
    await loadInboxItems();
    refs.captureStatus.textContent = 'Captured to Inbox.';
    announce('Captured to Inbox.');
  } catch (error) {
    refs.captureStatus.textContent = text(error?.message, 'Could not capture this item.');
    announce(refs.captureStatus.textContent, true);
  } finally {
    state.captureBusy = false;
    refs.captureForm.removeAttribute('aria-busy');
    refs.captureInput.disabled = false;
    refs.captureButton.disabled = false;
    refs.captureButton.textContent = 'Capture';
    refs.captureInput.focus();
  }
}

function setCardBusy(card, control, action) {
  card.setAttribute('aria-busy', 'true');
  card.querySelectorAll('.inbox-action').forEach((button) => { button.disabled = true; });
  const labels = { classify: 'Classifying…', process: 'Processing…', archive: 'Archiving…' };
  control.textContent = labels[action] || 'Working…';
  const error = card.querySelector('.inbox-card-error');
  if (error) { error.hidden = true; error.textContent = ''; }
}

function restoreCardActions(card, item) {
  card.removeAttribute('aria-busy');
  const final = item.status === 'archived';
  const processed = item.status === 'processed';
  const process = processAvailability(item);
  card.querySelectorAll('[data-inbox-action]').forEach((button) => {
    const action = button.dataset.inboxAction;
    button.textContent = humanize(action);
    if (action === 'process') button.disabled = !process.enabled;
    else button.disabled = final || (processed && action !== 'archive');
  });
}

async function handleItemAction(control) {
  const itemId = text(control.dataset.itemId);
  const action = text(control.dataset.inboxAction).toLowerCase();
  const item = state.items.find((row) => row.id === itemId);
  const card = control.closest('.inbox-card');
  if (!item || !card || !['classify', 'process', 'archive'].includes(action)) return;
  setCardBusy(card, control, action);
  announce(`${humanize(action)} started for ${item.title}.`);
  try {
    const updated = await mutateInboxItem(item.id, action, item.version);
    if (updated) {
      state.items = state.items.map((row) => row.id === updated.id ? updated : row);
      render();
      const nextCard = Array.from(refs.list.querySelectorAll('[data-inbox-item-id]'))
        .find((candidate) => candidate.dataset.inboxItemId === updated.id);
      nextCard?.focus({ preventScroll: true });
    } else {
      await loadInboxItems({ focusList: true });
    }
    announce(`${humanize(action)} complete for ${item.title}.`);
  } catch (error) {
    restoreCardActions(card, item);
    const message = text(error?.message, `Could not ${action} this item.`);
    const errorNode = card.querySelector('.inbox-card-error');
    if (errorNode) { errorNode.textContent = message; errorNode.hidden = false; }
    announce(message, true);
    control.focus();
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

function commitInboxRoute(mode) {
  if (typeof window === 'undefined' || mode === 'none' || window.location.pathname === '/inbox') return;
  const method = mode === 'replace' ? 'replaceState' : 'pushState';
  window.history?.[method]?.({ restiaNavigation: 'inbox' }, '', '/inbox');
  document.title = 'Inbox — Restia';
}

async function leaveToChat() {
  if (typeof window !== 'undefined' && typeof window.activateNavigationItem === 'function') {
    await window.activateNavigationItem('chat');
    return;
  }
  close({ restoreFocus: false });
  if (typeof window !== 'undefined' && window.location.pathname === '/inbox') {
    window.history?.pushState?.({ restiaNavigation: 'chat' }, '', '/');
  }
}

function bindEvents() {
  refs.captureForm.addEventListener('submit', handleCapture);
  refs.refresh.addEventListener('click', () => void loadInboxItems({ focusList: true }));
  refs.retry.addEventListener('click', () => void loadInboxItems({ focusList: true }));
  refs.loadMore.addEventListener('click', () => void loadInboxItems({ append: true }));
  refs.close.addEventListener('click', () => void leaveToChat());
  refs.filters.addEventListener('click', (event) => {
    const control = event.target.closest?.('[data-inbox-filter]');
    if (!control || !STATUS_FILTERS.has(control.dataset.inboxFilter)) return;
    state.filter = control.dataset.inboxFilter;
    state.items = [];
    state.nextCursor = null;
    state.paginationError = '';
    void loadInboxItems({ focusList: true });
  });
  refs.list.addEventListener('click', (event) => {
    const control = event.target.closest?.('[data-inbox-action]');
    if (control && !control.disabled) void handleItemAction(control);
  });
  refs.root.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    event.preventDefault();
    void leaveToChat();
  });
}

export function init(apiBase = '') {
  if (apiBase) API_BASE = String(apiBase).replace(/\/$/, '');
  if (typeof document === 'undefined') return inboxModule;
  const root = document.getElementById('inbox-workspace');
  if (!root) return inboxModule;
  refs = {
    root,
    captureForm: document.getElementById('inbox-capture-form'),
    captureInput: document.getElementById('inbox-capture-input'),
    captureButton: document.getElementById('inbox-capture-submit'),
    captureStatus: document.getElementById('inbox-capture-status'),
    refresh: document.getElementById('inbox-refresh'),
    close: document.getElementById('inbox-close'),
    filters: document.getElementById('inbox-status-filters'),
    loading: document.getElementById('inbox-loading'),
    error: document.getElementById('inbox-error'),
    errorMessage: document.getElementById('inbox-error-message'),
    retry: document.getElementById('inbox-retry'),
    empty: document.getElementById('inbox-empty'),
    list: document.getElementById('inbox-list'),
    count: document.getElementById('inbox-count'),
    pagination: document.getElementById('inbox-pagination'),
    loadMore: document.getElementById('inbox-load-more'),
    paginationError: document.getElementById('inbox-pagination-error'),
    live: document.getElementById('inbox-live-region'),
  };
  if (Object.values(refs).some((element) => !element)) return inboxModule;
  if (!state.initialized) bindEvents();
  state.initialized = true;
  render();
  return inboxModule;
}

export async function open({ historyMode = 'push' } = {}) {
  if (!state.initialized) init();
  if (!state.initialized) return false;
  if (state.open) {
    commitInboxRoute(historyMode);
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
  if (typeof window !== 'undefined') window.missionControlModule?.close?.();
  await minimizeWorkspaceModals();
  state.previousFocus = document.activeElement;
  state.open = true;
  refs.root.hidden = false;
  document.body.classList.add('inbox-view');
  commitInboxRoute(historyMode);
  document.dispatchEvent(new CustomEvent('restia:inbox-opened'));
  focus();
  void loadInboxItems();
  return true;
}

export function close({ restoreFocus = true } = {}) {
  if (!state.open) return true;
  loadSequence += 1;
  loadController?.abort();
  loadController = null;
  state.open = false;
  state.loading = false;
  state.loadingMore = false;
  document.body.classList.remove('inbox-view');
  refs.root.hidden = true;
  document.dispatchEvent(new CustomEvent('restia:inbox-closed'));
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
  const target = refs.captureInput || refs.root;
  try { target.focus({ preventScroll: true }); } catch (_) { try { target.focus(); } catch (_) {} }
  return true;
}

export const __test = Object.freeze({
  formatConfidence,
  formatUpdated,
  humanize,
  sourceCopy,
  statusCopy,
  processAvailability,
});

const inboxModule = {
  init, open, close, isOpen, focus, loadInboxItems,
  createInboxCapture, patchInboxItem, mutateInboxItem, __test,
};

export default inboxModule;

if (typeof window !== 'undefined') window.inboxModule = inboxModule;
