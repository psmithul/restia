// Restia V2 Mission Control — one owner-scoped view of today's work.
// The backend owns aggregation; this module owns a resilient, accessible
// workspace that remains useful when any individual source is unavailable.

const SOURCE_KEYS = Object.freeze([
  'calendar', 'project_work', 'goals', 'tasks', 'study_reviews', 'health',
  'important_mail', 'notes_today', 'daily_brief',
]);

let API_BASE = typeof window !== 'undefined' ? window.location.origin : '';
let refs = {};
let sequence = 0;
let controller = null;
let previousFocus = null;

const state = {
  initialized: false,
  open: false,
  loading: false,
  error: '',
  data: null,
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

function button(label, action, { className = '', target = '', title = '' } = {}) {
  return make('button', {
    type: 'button',
    className: className || 'mission-btn',
    text: label,
    attrs: { title: title || label },
    dataset: { action, target },
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

function source(name) {
  const value = state.data?.sources?.[name];
  return value && typeof value === 'object'
    ? { ...value, status: text(value.status, 'ok'), items: rows(value.items) }
    : { status: 'empty', items: [], count: 0, truncated: false };
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
  const overall = text(health.overall, healthUnavailable(health) ? 'Unavailable' : 'Ready');
  refs.summary.append(
    summaryCard('Calendar', number(summary.calendar), 'today'),
    summaryCard('Project work', number(summary.project_work), 'open or due'),
    summaryCard('Important mail', number(summary.important_mail), 'needs attention'),
    summaryCard('Notes Today', number(summary.notes_today), 'active plans'),
    summaryCard('Goals', number(summary.goals), 'active'),
    summaryCard('Tasks', number(summary.tasks), 'scheduled or running'),
    summaryCard('Reviews', number(summary.study_reviews), 'due'),
    summaryCard('System', overall, `${rows(health.services).length} services`, ['ok', 'healthy'].includes(overall.toLowerCase()) ? 'good' : 'attention'),
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

function renderNotesToday() {
  const { section, body, value } = sectionShell('Notes Today', 'notes_today', 'notes');
  if (sourceUnavailable(value)) body.appendChild(sourceError(value));
  else if (!value.items.length) body.appendChild(emptyState('Pin a note or add a due step to bring it into Today.'));
  else value.items.forEach((note) => {
    const progress = `${number(note.completed_steps)}/${number(note.total_steps)} steps`;
    const row = itemRow(text(note.title, 'Untitled note'), progress, {
      target: 'notes',
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

function renderQuickActions() {
  clear(refs.quickActions);
  [
    ['New chat', 'new-chat'], ['Projects', 'projects'], ['Tasks', 'tasks'],
    ['Calendar', 'calendar'], ['Study', 'study'],
  ].forEach(([label, target]) => refs.quickActions.appendChild(button(label, 'open-target', { className: 'mission-quick-action', target })));
}

function render() {
  refs.loading.hidden = !state.loading;
  refs.error.hidden = !state.error;
  refs.error.textContent = state.error;
  refs.content.hidden = state.loading || Boolean(state.error) || !state.data;
  if (!state.data || state.loading || state.error) return;
  refs.date.textContent = formatDate(`${state.data.date}T12:00:00`) || text(state.data.date, 'Today');
  refs.asOf.textContent = state.data.as_of ? `Updated ${formatDate(state.data.as_of, { time: true })}` : '';
  renderSummary();
  renderQuickActions();
  clear(refs.grid);
  refs.grid.append(
    renderFocus(), renderDailyBrief(), renderCalendar(), renderProjectWork(),
    renderImportantMail(), renderNotesToday(), renderTasks(), renderGoals(),
    renderReviews(), renderHealth(),
  );
}

function triggerTarget(target) {
  const ids = {
    'new-chat': ['sidebar-new-chat-btn', 'rail-new-session'],
    projects: ['tool-projects-btn', 'rail-projects'],
    tasks: ['tool-tasks-btn', 'rail-tasks'],
    calendar: ['tool-calendar-btn', 'rail-calendar'],
    study: ['tool-study-btn', 'rail-study'],
    email: ['email-section-title', 'rail-email'],
    notes: ['tool-notes-btn', 'rail-notes'],
    activity: ['v2-activity-nav', 'rail-activity'],
  }[target] || [];
  const trigger = ids.map((id) => document.getElementById(id)).find(Boolean);
  if (!trigger) return false;
  close();
  trigger.click();
  return true;
}

async function loadToday() {
  const token = ++sequence;
  controller?.abort();
  controller = new AbortController();
  state.loading = true;
  state.error = '';
  render();
  try {
    const response = await fetch(`${API_BASE}/api/mission-control/today?utc_offset_minutes=${localOffsetMinutes()}`, {
      credentials: 'same-origin', signal: controller.signal,
    });
    if (!response.ok) {
      let detail = '';
      try { detail = text((await response.json()).detail); } catch (_) {}
      throw new Error(detail || `Mission Control request failed (HTTP ${response.status})`);
    }
    const payload = await response.json();
    if (token !== sequence || !state.open) return;
    state.data = payload && typeof payload === 'object' ? payload : null;
    if (!state.data?.sources) throw new Error('Mission Control returned an invalid response');
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
    button('Refresh', 'refresh', { className: 'mission-btn' }),
    button('Close', 'close', { className: 'mission-btn mission-btn-quiet' }),
  ]);
  header.append(heading, actions);
  const summary = make('div', { className: 'mission-summary', attrs: { 'aria-label': 'Today summary' } });
  const quickActions = make('nav', { className: 'mission-quick-actions', attrs: { 'aria-label': 'Quick actions' } });
  const loading = make('div', { className: 'mission-loading', text: 'Building today’s view…', attrs: { role: 'status' } });
  const error = make('div', { className: 'mission-load-error', hidden: true, attrs: { role: 'alert' } });
  const content = make('div', { className: 'mission-content', hidden: true });
  const grid = make('div', { className: 'mission-grid' });
  content.append(summary, quickActions, grid);
  root.append(header, loading, error, content);
  document.body.appendChild(root);
  refs = {
    root, summary, quickActions, loading, error, content, grid,
    date: heading.querySelector('#mission-date'), asOf: heading.querySelector('#mission-as-of'),
  };
  root.addEventListener('click', (event) => {
    const control = event.target.closest?.('[data-action]');
    if (!control || !root.contains(control)) return;
    if (control.dataset.action === 'close') close();
    else if (control.dataset.action === 'refresh') loadToday();
    else if (control.dataset.action === 'open-target') triggerTarget(control.dataset.target);
  });
  root.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { event.preventDefault(); close(); }
  });
  return root;
}

export function init(apiBase = '') {
  if (apiBase) API_BASE = String(apiBase).replace(/\/$/, '');
  state.initialized = true;
  return missionControlModule;
}

export async function open() {
  if (!state.initialized) init();
  if (window.studyModule?.isActive?.()) {
    const closed = await window.studyModule.close({ startFresh: false });
    if (!closed && window.studyModule?.isActive?.()) return false;
  }
  if (window.projectsModule?.isOpen?.()) {
    const closed = await window.projectsModule.close();
    if (!closed) return false;
  }
  buildWorkspace();
  previousFocus = document.activeElement;
  state.open = true;
  document.body.classList.add('mission-control-view');
  refs.root.hidden = false;
  refs.root.focus();
  await loadToday();
  return true;
}

export function close() {
  if (!state.open) return true;
  sequence += 1;
  controller?.abort();
  controller = null;
  state.open = false;
  document.body.classList.remove('mission-control-view');
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
  return state.open ? loadToday() : Promise.resolve(false);
}

export const __test = Object.freeze({
  localOffsetMinutes, focusCandidates, mapBackendActions,
  sourceMessage, sourceUnavailable, healthUnavailable,
});

const missionControlModule = { init, open, close, toggle, isOpen, refresh, __test };
export default missionControlModule;

if (typeof window !== 'undefined') window.missionControlModule = missionControlModule;
