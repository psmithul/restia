// Restia Projects — local-first project/workflow workspace.
//
// The shell owns navigation and imports this module. This file deliberately
// owns no chat composer state: project deliverables use a task-scoped queue so
// files can never leak into chat attachments or another task draft.

const PROJECT_VIEWS = new Set(['board', 'list', 'activity']);
const ATTACHMENT_KINDS = new Set(['reference', 'draft', 'deliverable']);
const ITEM_TYPES = Object.freeze(['task', 'story', 'bug', 'epic', 'subtask']);
const PRIORITIES = Object.freeze(['lowest', 'low', 'medium', 'high', 'highest', 'critical']);
const PROJECT_TEMPLATES = Object.freeze(['general', 'personal', 'research', 'software', 'content', 'coursework', 'gtm']);
const STAGE_CATEGORIES = Object.freeze(['backlog', 'todo', 'in_progress', 'review', 'done']);
const ACCEPTED_ATTACHMENT_EXTENSIONS = new Set([
  'pdf', 'docx', 'xlsx', 'pptx', 'zip',
  'png', 'jpg', 'jpeg', 'webp', 'gif',
  'txt', 'md', 'csv', 'json', 'stl', 'step', 'stp', 'iges', 'igs',
]);
const ACCEPT_STRING = [
  '.pdf', '.docx', '.xlsx', '.pptx', '.zip',
  '.png', '.jpg', '.jpeg', '.webp', '.gif',
  '.txt', '.md', '.csv', '.json', '.stl', '.step', '.stp', '.iges', '.igs',
].join(',');

const DEFAULT_FILTERS = Object.freeze({
  search: '',
  priority: '',
  type: '',
  label: '',
  assignee: '',
  due: '',
  attachments: '',
});

let API_BASE = typeof window !== 'undefined' ? window.location.origin : '';
let dependencies = {};
let refs = {};
let lifecycleCleanups = [];

function createRequestGate() {
  let generation = 0;
  return {
    next() { generation += 1; return generation; },
    current(token) { return token === generation; },
    invalidate() { generation += 1; },
    value() { return generation; },
  };
}

class AttachmentQueueStore {
  constructor(revoke = null) {
    this.queues = new Map();
    this.revoke = revoke || ((url) => {
      try { URL.revokeObjectURL(url); } catch (_) {}
    });
  }

  key(projectId, itemId) {
    return `${String(projectId || 'none')}::${String(itemId || 'draft')}`;
  }

  get(projectId, itemId) {
    const key = this.key(projectId, itemId);
    if (!this.queues.has(key)) this.queues.set(key, []);
    return this.queues.get(key);
  }

  add(projectId, itemId, entry) {
    this.get(projectId, itemId).push(entry);
    return entry;
  }

  remove(projectId, itemId, queueId) {
    const queue = this.get(projectId, itemId);
    const index = queue.findIndex((entry) => entry.queueId === queueId);
    if (index < 0) return null;
    const [removed] = queue.splice(index, 1);
    if (removed.previewUrl) this.revoke(removed.previewUrl);
    if (removed.xhr && typeof removed.xhr.abort === 'function') {
      try { removed.xhr.abort(); } catch (_) {}
    }
    return removed;
  }

  clear(projectId, itemId) {
    const key = this.key(projectId, itemId);
    const queue = this.queues.get(key) || [];
    queue.forEach((entry) => {
      if (entry.previewUrl) this.revoke(entry.previewUrl);
      if (entry.xhr && typeof entry.xhr.abort === 'function') {
        try { entry.xhr.abort(); } catch (_) {}
      }
    });
    this.queues.delete(key);
  }

  clearAll() {
    for (const key of [...this.queues.keys()]) {
      const [projectId, itemId] = key.split('::');
      this.clear(projectId, itemId === 'draft' ? null : itemId);
    }
    this.queues.clear();
  }
}

const attachmentQueues = new AttachmentQueueStore();

const state = {
  initialized: false,
  open: false,
  loadingProjects: false,
  loadingBoard: false,
  loadError: '',
  projects: [],
  globalOverview: null,
  project: null,
  activeProjectId: null,
  stages: [],
  items: [],
  members: [],
  overview: null,
  activity: [],
  activityLoaded: false,
  activityLoading: false,
  activityNextBefore: null,
  activeView: 'board',
  filters: { ...DEFAULT_FILTERS },
  mobileStageId: null,
  quickStageId: null,
  selectedItem: null,
  drawerLoading: false,
  drawerDirty: false,
  drawerDraft: null,
  dialogClose: null,
  previousFocus: null,
  detailPreviousFocus: null,
  drag: null,
  preserved: new Map(),
  projectGate: createRequestGate(),
  boardGate: createRequestGate(),
  detailGate: createRequestGate(),
  activityGate: createRequestGate(),
  projectController: null,
  boardController: null,
  detailController: null,
  activityController: null,
  moveVersions: new Map(),
  movingTasks: new Set(),
  submittingTasks: new Set(),
  taskActivityLoading: new Set(),
};

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function asId(value) {
  if (value === null || value === undefined) return '';
  return String(value);
}

function asNumber(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function taskScopeKey(projectId, itemId) {
  return `${asId(projectId)}::${asId(itemId)}`;
}

function captureSelectedTaskContext() {
  const projectId = asId(state.activeProjectId);
  const itemId = asId(state.selectedItem?.id);
  return projectId && itemId ? { projectId, itemId, key: taskScopeKey(projectId, itemId) } : null;
}

function taskContextMatches(
  context,
  projectId = state.activeProjectId,
  itemId = state.selectedItem?.id,
  isOpen = state.open,
) {
  return Boolean(
    isOpen &&
    context &&
    context.projectId === asId(projectId) &&
    context.itemId === asId(itemId)
  );
}

function mergeUniqueRows(existing, incoming) {
  const seen = new Set();
  return [...asArray(existing), ...asArray(incoming)].filter((row) => {
    const id = asId(row?.id);
    if (!id || seen.has(id)) return false;
    seen.add(id);
    return true;
  });
}

function activityCursorFromEntries(entries) {
  const rows = asArray(entries);
  const oldest = rows[rows.length - 1];
  return oldest?.created_at && oldest?.id ? `${oldest.created_at}|${oldest.id}` : null;
}

function safeHexColor(value, fallback = '#7f849c') {
  const candidate = String(value || '').trim();
  return /^#[0-9a-fA-F]{6}$/.test(candidate) ? candidate : fallback;
}

function normalizeProject(raw = {}) {
  const overview = raw.overview && typeof raw.overview === 'object' ? raw.overview : {};
  return {
    ...raw,
    id: asId(raw.id ?? raw.project_id),
    name: String(raw.name || raw.title || 'Untitled project'),
    key: String(raw.key || raw.project_key || 'PROJECT').toUpperCase(),
    description: String(raw.description || ''),
    template: PROJECT_TEMPLATES.includes(raw.template) ? raw.template : 'general',
    color: safeHexColor(raw.color, '#e06c75'),
    icon: String(raw.icon || ''),
    archived: Boolean(raw.archived || raw.is_archived || raw.archived_at),
    role: String(raw.role || 'owner').toLowerCase(),
    owner: String(raw.owner_username || raw.owner || ''),
    overview,
    item_count: asNumber(raw.item_count ?? raw.items_count ?? raw.task_count ?? overview.total_items),
    done_count: asNumber(raw.done_count ?? raw.completed_count ?? overview.done_items),
    overdue_count: asNumber(raw.overdue_count ?? overview.overdue_items),
    due_soon_count: asNumber(raw.due_soon_count ?? overview.due_soon_items),
    blocked_count: asNumber(raw.blocked_count ?? overview.blocked_items),
    updated_at: raw.updated_at || null,
    version: asNumber(raw.version, 0),
  };
}

function normalizeMember(member = {}) {
  return {
    ...member,
    id: asId(member.id ?? member.member_id ?? member.username),
    username: String(member.username || member.name || member.display_name || ''),
    name: String(member.name || member.display_name || member.username || 'Member'),
    role: String(member.role || 'viewer').toLowerCase(),
  };
}

function normalizeStage(raw = {}, index = 0) {
  return {
    ...raw,
    id: asId(raw.id ?? raw.stage_id ?? raw.status_id),
    name: String(raw.name || raw.title || 'Stage'),
    category: String(raw.category || raw.stage_category || 'todo'),
    color: safeHexColor(raw.color, '#7f849c'),
    position: asNumber(raw.position ?? raw.order ?? index, index),
    wip_limit: raw.wip_limit === null || raw.wip_limit === undefined || raw.wip_limit === ''
      ? null
      : Math.max(1, asNumber(raw.wip_limit, 1)),
  };
}

function normalizeChecklistItem(raw = {}, index = 0) {
  return {
    ...raw,
    id: asId(raw.id ?? raw.checklist_item_id),
    text: String(raw.text || raw.title || ''),
    done: Boolean(raw.done ?? raw.completed),
    position: asNumber(raw.position ?? index, index),
  };
}

function normalizeComment(raw = {}) {
  return {
    ...raw,
    id: asId(raw.id ?? raw.comment_id),
    body: String(raw.body || raw.text || ''),
    author: String(raw.author_name || raw.author || raw.created_by || 'You'),
    created_at: raw.created_at || null,
    updated_at: raw.updated_at || null,
  };
}

function normalizeAttachment(raw = {}) {
  return {
    ...raw,
    id: asId(raw.id ?? raw.attachment_id),
    name: String(raw.name || raw.filename || 'Attachment'),
    mime: String(raw.mime || raw.mime_type || raw.content_type || ''),
    size: asNumber(raw.size ?? raw.size_bytes),
    kind: ATTACHMENT_KINDS.has(raw.kind) ? raw.kind : 'reference',
    description: String(raw.description || raw.note || ''),
    created_at: raw.created_at || raw.uploaded_at || null,
    download_url: String(raw.download_url || raw.url || ''),
  };
}

function normalizeActivity(raw = {}) {
  return {
    ...raw,
    id: asId(raw.id ?? raw.activity_id ?? `${raw.created_at || ''}-${raw.type || ''}`),
    type: String(raw.type || raw.event_type || raw.action || 'updated'),
    text: String(raw.text || raw.summary || raw.message || raw.description || 'Project updated'),
    actor: String(raw.actor_name || raw.actor || raw.created_by || 'You'),
    created_at: raw.created_at || null,
    item_id: asId(raw.item_id ?? raw.work_item_id ?? raw.task_id),
    item_key: String(raw.item_key || raw.task_key || ''),
  };
}

function normalizeItem(raw = {}, index = 0) {
  const checklist = asArray(raw.checklist || raw.subtasks).map(normalizeChecklistItem);
  const doneChecklist = checklist.filter((entry) => entry.done).length;
  return {
    ...raw,
    id: asId(raw.id ?? raw.item_id ?? raw.task_id),
    key: String(raw.key || raw.item_key || raw.task_key || ''),
    title: String(raw.title || raw.name || 'Untitled task'),
    description: String(raw.description || ''),
    stage_id: asId(raw.stage_id ?? raw.status_id ?? raw.stage?.id ?? raw.status?.id),
    position: asNumber(raw.position ?? raw.rank ?? index, index),
    version: asNumber(raw.version, 0),
    type: String(raw.type || raw.item_type || 'task'),
    priority: String(raw.priority || 'medium').toLowerCase(),
    labels: asArray(raw.labels).map(String),
    assignee_id: asId(raw.assignee_id ?? raw.assignee?.id),
    assignee_name: String(raw.assignee_name || (typeof raw.assignee === 'string' ? raw.assignee : raw.assignee?.name) || ''),
    start_date: raw.start_date || '',
    due_date: raw.due_date || '',
    estimate_minutes: asNumber(raw.estimate_minutes ?? raw.estimate),
    logged_minutes: asNumber(raw.logged_minutes ?? raw.logged),
    parent_id: asId(raw.parent_id ?? raw.parent?.id),
    blocker_id: asId(raw.blocker_id ?? raw.blocked_by_id ?? raw.blocked_by_item_id ?? raw.blocker?.id),
    blocked: Boolean(raw.blocked || raw.is_blocked || raw.blocker_id || raw.blocked_by_id || raw.blocked_by_item_id),
    archived: Boolean(raw.archived || raw.is_archived || raw.archived_at),
    checklist,
    checklist_count: asNumber(raw.checklist_count, checklist.length),
    checklist_done: asNumber(raw.checklist_done, doneChecklist),
    checklist_total: asNumber(raw.checklist_total, checklist.length),
    checklist_truncated: Boolean(raw.checklist_truncated),
    comments: asArray(raw.comments).map(normalizeComment),
    comments_total: asNumber(raw.comments_total, asArray(raw.comments).length),
    comments_truncated: Boolean(raw.comments_truncated),
    comments_next_before: raw.comments_next_before || raw.next_before || null,
    attachments: asArray(raw.attachments || raw.deliverables).map(normalizeAttachment),
    attachment_count: asNumber(raw.attachment_count, asArray(raw.attachments || raw.deliverables).length),
    activity: asArray(raw.activity).map(normalizeActivity),
    activity_next_before: raw.activity_next_before || null,
    created_at: raw.created_at || null,
    updated_at: raw.updated_at || null,
  };
}

function mergeItemPayload(current = {}, raw = {}) {
  const patch = raw && typeof raw === 'object' ? raw : {};
  const has = (key) => Object.prototype.hasOwnProperty.call(patch, key);
  const assigneeChanged = has('assignee') || has('assignee_id') || has('assignee_name');
  const blockerChanged = has('blocked_by_id') || has('blocker_id');
  const assigneeValue = patch.assignee_name ?? patch.assignee ?? patch.assignee_id ?? '';
  const blockerValue = patch.blocked_by_id ?? patch.blocker_id ?? '';
  return normalizeItem({
    ...current,
    ...patch,
    type: patch.item_type ?? patch.type ?? current.type,
    assignee_id: assigneeChanged ? asId(patch.assignee_id ?? patch.assignee) : current.assignee_id,
    assignee_name: assigneeChanged ? String(assigneeValue || '') : current.assignee_name,
    blocker_id: blockerChanged ? asId(blockerValue) : current.blocker_id,
    blocked: blockerChanged ? Boolean(blockerValue) : current.blocked,
    checklist: patch.checklist ?? current.checklist,
    comments: patch.comments ?? current.comments,
    attachments: patch.attachments ?? patch.deliverables ?? current.attachments,
    activity: patch.activity ?? current.activity,
  });
}

function normalizeBoard(payload = {}) {
  const stages = asArray(payload.stages || payload.statuses)
    .map(normalizeStage)
    .sort((a, b) => a.position - b.position);
  const items = asArray(payload.items || payload.tasks || payload.issues)
    .map(normalizeItem)
    .sort((a, b) => a.position - b.position);
  return {
    project: payload.project ? normalizeProject(payload.project) : null,
    stages,
    items,
    members: asArray(payload.members).map(normalizeMember),
    overview: payload.overview || null,
    activity: asArray(payload.activity).map(normalizeActivity),
  };
}

function projectRole() {
  return String(state.project?.role || 'owner').toLowerCase();
}

function canEditProject() {
  return !state.project?.archived && (projectRole() === 'owner' || projectRole() === 'editor');
}

function canManageProject() {
  return !state.project?.archived && projectRole() === 'owner';
}

function canManageSpecificProject(project) {
  return String(project?.role || 'owner').toLowerCase() === 'owner';
}

function currentUsername() {
  const source = dependencies.currentUsername;
  let value = '';
  try { value = typeof source === 'function' ? source() : source; } catch (_) {}
  return String(value || '').trim().toLowerCase();
}

function canDeleteOwnedResource(role, actor, resourceOwner) {
  if (String(role || '').toLowerCase() === 'owner') return true;
  const normalizedActor = String(actor || '').trim().toLowerCase();
  const normalizedOwner = String(resourceOwner || '').trim().toLowerCase();
  return Boolean(normalizedActor && normalizedOwner && normalizedActor === normalizedOwner);
}

function canDeleteComment(comment) {
  return canDeleteOwnedResource(projectRole(), currentUsername(), comment?.author);
}

function canDeleteAttachment(attachment) {
  return canDeleteOwnedResource(projectRole(), currentUsername(), attachment?.uploader);
}

function taskDraftFromItem(item = {}) {
  return {
    projectId: asId(item.project_id ?? state.activeProjectId),
    itemId: asId(item.id),
    values: {
      title: String(item.title || ''),
      description: String(item.description || ''),
      item_type: String(item.type || item.item_type || 'task'),
      priority: String(item.priority || 'medium'),
      stage_id: asId(item.stage_id),
      assignee: String(item.assignee_id || item.assignee_name || ''),
      labels: asArray(item.labels).join(', '),
      start_date: String(item.start_date || ''),
      due_date: String(item.due_date || ''),
      estimate_minutes: String(asNumber(item.estimate_minutes)),
      logged_minutes: String(asNumber(item.logged_minutes)),
      parent_id: asId(item.parent_id),
      blocked_by_id: asId(item.blocker_id ?? item.blocked_by_id),
    },
  };
}

function activeDrawerDraft(item = state.selectedItem) {
  if (!item) return null;
  const matches = state.drawerDraft &&
    state.drawerDraft.projectId === asId(state.activeProjectId) &&
    state.drawerDraft.itemId === asId(item.id);
  if (!matches) state.drawerDraft = taskDraftFromItem(item);
  return state.drawerDraft;
}

function updateDrawerDraftField(control) {
  const fieldName = control?.dataset?.taskField;
  const draft = activeDrawerDraft();
  if (!fieldName || !draft || !Object.prototype.hasOwnProperty.call(draft.values, fieldName)) return false;
  draft.values[fieldName] = control.value;
  state.drawerDirty = true;
  return true;
}

function syncDrawerDraftStage(previousStageId, nextStageId) {
  const draft = state.drawerDraft;
  if (!draft || draft.itemId !== asId(state.selectedItem?.id)) return;
  if (asId(draft.values.stage_id) === asId(previousStageId)) draft.values.stage_id = asId(nextStageId);
}

function normalizeDateOnly(value) {
  if (!value) return null;
  const date = new Date(`${String(value).slice(0, 10)}T12:00:00`);
  return Number.isNaN(date.getTime()) ? null : date;
}

function dueBucket(item, now = new Date()) {
  const due = normalizeDateOnly(item.due_date);
  if (!due) return 'none';
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 12);
  const days = Math.ceil((due - today) / 86400000);
  if (days < 0) return 'overdue';
  if (days <= 7) return 'soon';
  return 'later';
}

function computeProjectHealth(items, stages, now = new Date()) {
  const activeItems = asArray(items).filter((item) => !item.archived);
  const doneStageIds = new Set(
    asArray(stages).filter((stage) => stage.category === 'done').map((stage) => asId(stage.id)),
  );
  let done = 0;
  let overdue = 0;
  let dueSoon = 0;
  let blocked = 0;
  activeItems.forEach((item) => {
    if (doneStageIds.has(asId(item.stage_id))) {
      done += 1;
      return;
    }
    const bucket = dueBucket(item, now);
    if (bucket === 'overdue') overdue += 1;
    if (bucket === 'soon') dueSoon += 1;
    if (item.blocked || item.blocker_id || item.blocked_by_id) blocked += 1;
  });
  return { total: activeItems.length, done, overdue, dueSoon, blocked };
}

function taskMatchesFilters(item, filters = DEFAULT_FILTERS, now = new Date()) {
  const search = String(filters.search || '').trim().toLowerCase();
  if (search) {
    const haystack = [item.key, item.title, item.description, ...item.labels]
      .join(' ')
      .toLowerCase();
    if (!haystack.includes(search)) return false;
  }
  if (filters.priority && item.priority !== filters.priority) return false;
  if (filters.type && item.type !== filters.type) return false;
  if (filters.label && !item.labels.some((label) => label.toLowerCase().includes(filters.label.toLowerCase()))) return false;
  if (filters.assignee) {
    const assignee = `${item.assignee_name} ${item.assignee_id}`.toLowerCase();
    if (!assignee.includes(filters.assignee.toLowerCase())) return false;
  }
  if (filters.due && dueBucket(item, now) !== filters.due) return false;
  if (filters.attachments === 'with' && item.attachment_count <= 0) return false;
  if (filters.attachments === 'without' && item.attachment_count > 0) return false;
  return !item.archived;
}

function applyTaskFilters(items, filters = DEFAULT_FILTERS, now = new Date()) {
  return asArray(items).filter((item) => taskMatchesFilters(item, filters, now));
}

function optimisticMove(items, taskId, stageId, position = null) {
  const taskKey = asId(taskId);
  const targetStage = asId(stageId);
  const source = items.map((item) => ({ ...item }));
  const moving = source.find((item) => item.id === taskKey);
  if (!moving || moving.archived || !targetStage) return source;
  const archived = source.filter((item) => item.id !== taskKey && item.archived);
  const remaining = source.filter((item) => item.id !== taskKey && !item.archived);
  moving.stage_id = targetStage;
  const targetItems = remaining.filter((item) => item.stage_id === targetStage);
  const targetPosition = position === null || position === undefined
    ? targetItems.length
    : Math.max(0, Math.min(asNumber(position), targetItems.length));
  const before = targetItems[targetPosition];
  if (before) {
    const globalIndex = remaining.findIndex((item) => item.id === before.id);
    remaining.splice(globalIndex, 0, moving);
  } else {
    const lastIndex = remaining.reduce((found, item, index) => item.stage_id === targetStage ? index : found, -1);
    remaining.splice(lastIndex + 1, 0, moving);
  }
  const counters = new Map();
  remaining.forEach((item) => {
    const next = counters.get(item.stage_id) || 0;
    item.position = next;
    counters.set(item.stage_id, next + 1);
  });
  return [...remaining, ...archived];
}

async function runMoveTransaction(items, taskId, stageId, position, persist) {
  const snapshot = items.map((item) => ({ ...item }));
  const optimistic = optimisticMove(items, taskId, stageId, position);
  try {
    const result = await persist();
    return { ok: true, items: optimistic, result, snapshot };
  } catch (error) {
    return { ok: false, items: snapshot, error, snapshot };
  }
}

function isMobileViewport() {
  if (typeof window === 'undefined') return false;
  if (window.matchMedia) return window.matchMedia('(max-width: 768px)').matches;
  return window.innerWidth <= 768;
}

function visibleStagesForViewport(stages, mobile, selectedStageId) {
  if (!mobile) return [...stages];
  const selected = stages.find((stage) => stage.id === asId(selectedStageId));
  return selected ? [selected] : stages.slice(0, 1);
}

function projectPath(projectId, suffix = '') {
  return `/api/projects/${encodeURIComponent(asId(projectId))}${suffix}`;
}

async function request(path, { method = 'GET', body, signal, headers = {} } = {}) {
  const fetchFn = dependencies.fetch || (typeof window !== 'undefined' ? window.fetch.bind(window) : null);
  if (!fetchFn) throw new Error('Fetch is unavailable');
  const init = { method, signal, headers: { ...headers } };
  if (body !== undefined) {
    if (typeof FormData !== 'undefined' && body instanceof FormData) {
      init.body = body;
    } else {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
  }
  const response = await fetchFn(`${API_BASE}${path}`, init);
  let payload = null;
  if (response.status !== 204) {
    try { payload = await response.json(); } catch (_) { payload = null; }
  }
  if (!response.ok) {
    const detail = payload?.detail || payload?.error || `HTTP ${response.status}`;
    const error = new Error(String(detail));
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload || {};
}

async function optionalRequest(path, options) {
  try { return await request(path, options); } catch (error) {
    if (error?.name === 'AbortError') throw error;
    return null;
  }
}

function showToast(message, kind = 'info') {
  const fn = dependencies.showToast || dependencies.uiModule?.showToast ||
    (typeof window !== 'undefined' ? window.showToast : null);
  if (typeof fn === 'function') {
    try { fn(String(message), kind); return; } catch (_) {}
  }
  announce(message, kind === 'error' ? 'assertive' : 'polite');
}

async function confirmAction(message, options = {}) {
  const fn = dependencies.styledConfirm || dependencies.uiModule?.styledConfirm ||
    (typeof window !== 'undefined' ? window.styledConfirm : null);
  if (typeof fn !== 'function') {
    showToast('Confirmation dialog is unavailable. No changes were made.', 'error');
    return false;
  }
  return Boolean(await fn(String(message), options));
}

function announce(message, politeness = 'polite') {
  if (!refs.live) return;
  refs.live.setAttribute('aria-live', politeness);
  refs.live.textContent = '';
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => { if (refs.live) refs.live.textContent = String(message); });
  } else {
    refs.live.textContent = String(message);
  }
}

function make(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.id) element.id = options.id;
  if (options.className) element.className = options.className;
  if (options.text !== undefined) element.textContent = String(options.text);
  if (options.type) element.type = options.type;
  if (options.value !== undefined) element.value = String(options.value);
  if (options.name) element.name = options.name;
  if (options.placeholder) element.placeholder = options.placeholder;
  if (options.title) element.title = options.title;
  if (options.hidden) element.hidden = true;
  if (options.disabled) element.disabled = true;
  if (options.checked !== undefined) element.checked = Boolean(options.checked);
  if (options.draggable !== undefined) element.draggable = Boolean(options.draggable);
  if (options.dataset) {
    Object.entries(options.dataset).forEach(([key, value]) => { element.dataset[key] = String(value); });
  }
  if (options.attrs) {
    Object.entries(options.attrs).forEach(([key, value]) => {
      if (value !== null && value !== undefined) element.setAttribute(key, String(value));
    });
  }
  const childList = Array.isArray(children) ? children : [children];
  childList.filter(Boolean).forEach((child) => element.appendChild(
    typeof child === 'string' ? document.createTextNode(child) : child,
  ));
  return element;
}

function actionButton(label, action, { className = '', title = '', dataset = {}, disabled = false } = {}) {
  return make('button', {
    type: 'button',
    className,
    text: label,
    title: title || label,
    disabled,
    dataset: { action, ...dataset },
  });
}

function clear(element) {
  if (element) element.replaceChildren();
}

function listen(target, event, handler, options) {
  target.addEventListener(event, handler, options);
  lifecycleCleanups.push(() => target.removeEventListener(event, handler, options));
}

function formatDate(value, { includeTime = false } = {}) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 10);
  return new Intl.DateTimeFormat(undefined, includeTime
    ? { dateStyle: 'medium', timeStyle: 'short' }
    : { dateStyle: 'medium' }).format(date);
}

function formatBytes(value) {
  const bytes = asNumber(value);
  if (bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / (1024 ** index)).toFixed(index ? 1 : 0)} ${units[index]}`;
}

function formatMinutes(value) {
  const minutes = asNumber(value);
  if (!minutes) return '0m';
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return hours ? `${hours}h${remainder ? ` ${remainder}m` : ''}` : `${remainder}m`;
}

function abortController(name) {
  try { state[name]?.abort(); } catch (_) {}
  state[name] = typeof AbortController !== 'undefined' ? new AbortController() : null;
  return state[name]?.signal;
}

function saveProjectUiState() {
  if (!state.activeProjectId) return;
  const columnScroll = {};
  refs.view?.querySelectorAll?.('[data-stage-scroll]').forEach((element) => {
    columnScroll[element.dataset.stageScroll] = element.scrollTop;
  });
  state.preserved.set(state.activeProjectId, {
    view: state.activeView,
    filters: { ...state.filters },
    mobileStageId: state.mobileStageId,
    viewScroll: refs.view?.scrollTop || 0,
    columnScroll,
  });
}

function restoreProjectUiState(projectId) {
  const saved = state.preserved.get(asId(projectId));
  state.activeView = PROJECT_VIEWS.has(saved?.view) ? saved.view : 'board';
  state.filters = { ...DEFAULT_FILTERS, ...(saved?.filters || {}) };
  state.mobileStageId = saved?.mobileStageId || null;
  return saved || null;
}

function restoreScroll(saved) {
  if (!saved || typeof requestAnimationFrame !== 'function') return;
  requestAnimationFrame(() => {
    if (refs.view) refs.view.scrollTop = saved.viewScroll || 0;
    Object.entries(saved.columnScroll || {}).forEach(([stageId, top]) => {
      const scroller = [...(refs.view?.querySelectorAll?.('[data-stage-scroll]') || [])]
        .find((element) => element.dataset.stageScroll === stageId);
      if (scroller) scroller.scrollTop = top;
    });
  });
}

function buildWorkspace() {
  if (refs.root?.isConnected) return refs.root;
  const root = make('section', {
    id: 'projects-workspace',
    className: 'projects-workspace',
    attrs: {
      tabindex: '-1',
      'aria-labelledby': 'projects-workspace-title',
      'data-no-swipe-dismiss': 'true',
    },
  });

  const topbar = make('header', { className: 'projects-topbar' });
  const headingGroup = make('div', { className: 'projects-topbar__heading' }, [
    make('p', { className: 'projects-eyebrow', text: 'Workflow command center' }),
    make('h1', { id: 'projects-workspace-title', text: 'Projects' }),
  ]);
  const activeSummary = make('div', { className: 'projects-topbar__summary', attrs: { 'aria-live': 'polite' } });
  const topActions = make('div', { className: 'projects-topbar__actions' }, [
    actionButton('New project', 'new-project', { className: 'projects-btn projects-btn--primary' }),
    actionButton('Close', 'close-projects', { className: 'projects-btn projects-btn--quiet', title: 'Close Projects workspace' }),
  ]);
  topbar.append(headingGroup, activeSummary, topActions);

  const layout = make('div', { className: 'projects-layout' });
  const navigator = make('aside', { className: 'projects-navigator', attrs: { 'aria-label': 'Projects' } });
  const main = make('main', { className: 'projects-main', attrs: { 'aria-label': 'Project workspace' } });
  const mobileControls = make('div', { className: 'projects-mobile-controls' });
  const toolbar = make('div', { className: 'projects-toolbar' });
  const filters = make('div', { className: 'projects-filters', attrs: { 'aria-label': 'Task filters' } });
  const view = make('div', { className: 'projects-view', attrs: { tabindex: '-1' } });
  main.append(mobileControls, toolbar, filters, view);

  const drawer = make('aside', {
    className: 'projects-drawer',
    hidden: true,
    attrs: { 'aria-labelledby': 'projects-drawer-title', tabindex: '-1' },
  });
  const live = make('div', {
    className: 'projects-sr-only',
    attrs: { role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' },
  });
  const dialogHost = make('div', { className: 'projects-dialog-host' });
  layout.append(navigator, main, drawer);
  root.append(topbar, layout, live, dialogHost);
  document.body.appendChild(root);

  refs = { root, topbar, activeSummary, navigator, main, mobileControls, toolbar, filters, view, drawer, live, dialogHost };
  bindWorkspaceEvents();
  return root;
}

function bindWorkspaceEvents() {
  listen(refs.root, 'click', onWorkspaceClick);
  listen(refs.root, 'submit', onWorkspaceSubmit);
  listen(refs.root, 'change', onWorkspaceChange);
  listen(refs.root, 'input', onWorkspaceInput);
  listen(refs.root, 'dragstart', onDragStart);
  listen(refs.root, 'dragover', onDragOver);
  listen(refs.root, 'dragleave', onDragLeave);
  listen(refs.root, 'drop', onDrop);
  listen(refs.root, 'dragend', onDragEnd);
  listen(document, 'keydown', onGlobalKeydown, true);
}

function renderAll() {
  if (!refs.root) return;
  renderTopSummary();
  renderNavigator();
  renderMobileControls();
  renderToolbar();
  renderFilters();
  renderCurrentView();
  renderTaskDrawer();
}

function renderTopSummary() {
  clear(refs.activeSummary);
  if (!state.project) {
    refs.activeSummary.appendChild(make('span', { text: state.projects.length ? 'Choose a project' : 'Create your first project' }));
    return;
  }
  const { total, done } = computeProjectHealth(state.items, state.stages);
  const open = Math.max(0, total - done);
  const progress = total ? Math.round((done / total) * 100) : 0;
  refs.activeSummary.append(
    make('strong', { text: `${state.project.key} · ${state.project.name}` }),
    make('span', { text: `${progress}% complete · ${open} open task${open === 1 ? '' : 's'}` }),
  );
}

function overviewMetric(label, value, tone = '') {
  return make('div', { className: `projects-health-card${tone ? ` projects-health-card--${tone}` : ''}` }, [
    make('strong', { text: asNumber(value) }),
    make('span', { text: label }),
  ]);
}

function projectHealth(project) {
  if (project.id === state.activeProjectId && state.project) {
    return computeProjectHealth(state.items, state.stages);
  }
  const overviewRows = asArray(state.globalOverview?.projects || state.globalOverview?.project_overviews);
  const matching = overviewRows.find((row) => asId(row.project_id ?? row.project?.id ?? row.id) === project.id) || {};
  const overview = matching.overview && typeof matching.overview === 'object' ? matching.overview : matching;
  const dueSoonRows = Array.isArray(state.globalOverview?.due_soon) ? state.globalOverview.due_soon : null;
  const dueSoonFromGlobalList = dueSoonRows
    ? dueSoonRows.filter((item) => asId(item.project_id) === project.id).length
    : null;
  return {
    total: asNumber(overview.item_count ?? overview.total_items ?? overview.total ?? project.item_count),
    done: asNumber(overview.done_count ?? overview.done_items ?? overview.completed ?? project.done_count),
    overdue: asNumber(overview.overdue_count ?? overview.overdue_items ?? overview.overdue ?? project.overdue_count),
    dueSoon: asNumber(
      overview.due_soon_count ?? overview.due_soon_items ?? overview.due_soon
      ?? dueSoonFromGlobalList ?? project.due_soon_count,
    ),
    blocked: asNumber(overview.blocked_count ?? overview.blocked_items ?? overview.blocked ?? project.blocked_count),
  };
}

function renderNavigator() {
  clear(refs.navigator);
  const header = make('div', { className: 'projects-navigator__header' }, [
    make('div', {}, [
      make('h2', { text: 'My work' }),
      make('p', { text: 'Across every project' }),
    ]),
  ]);
  const aggregate = state.projects.filter((project) => !project.archived).reduce((sum, project) => {
    const health = projectHealth(project);
    sum.overdue += health.overdue;
    sum.dueSoon += health.dueSoon;
    sum.blocked += health.blocked;
    return sum;
  }, { overdue: 0, dueSoon: 0, blocked: 0 });
  const healthGrid = make('div', { className: 'projects-health-grid' }, [
    // `globalOverview` is only refreshed when the project list is loaded. The
    // aggregate combines that snapshot for inactive projects with live board
    // state for the active project, so risk counters update immediately.
    overviewMetric('Overdue', aggregate.overdue, 'danger'),
    overviewMetric('Due soon', aggregate.dueSoon, 'warning'),
    overviewMetric('Blocked', aggregate.blocked, 'muted'),
  ]);
  const listHeading = make('div', { className: 'projects-navigator__section-heading' }, [
    make('h2', { text: 'Projects' }),
    actionButton('Add', 'new-project', { className: 'projects-text-btn', title: 'Create project' }),
  ]);
  const list = make('div', { className: 'projects-project-list', attrs: { role: 'list' } });
  const activeProjects = state.projects.filter((project) => !project.archived);
  if (!activeProjects.length) {
    list.appendChild(make('p', { className: 'projects-empty-inline', text: 'No active projects yet.' }));
  }
  activeProjects.forEach((project) => list.appendChild(renderProjectRow(project)));

  refs.navigator.append(header, healthGrid, listHeading, list);
  const archived = state.projects.filter((project) => project.archived);
  if (archived.length) {
    const details = make('details', { className: 'projects-archived-projects' });
    details.appendChild(make('summary', { text: `Archived (${archived.length})` }));
    const archivedList = make('div', { className: 'projects-project-list', attrs: { role: 'list' } });
    archived.forEach((project) => archivedList.appendChild(renderProjectRow(project)));
    details.appendChild(archivedList);
    refs.navigator.appendChild(details);
  }
}

function renderProjectRow(project) {
  const row = make('div', {
    className: `projects-project-row${project.id === state.activeProjectId ? ' is-active' : ''}`,
    attrs: { role: 'listitem' },
  });
  const health = projectHealth(project);
  const completion = health.total ? Math.round((health.done / health.total) * 100) : 0;
  const open = actionButton('', 'select-project', {
    className: 'projects-project-row__open',
    title: `Open ${project.name}`,
    dataset: { projectId: project.id },
  });
  const swatch = make('span', { className: 'projects-project-swatch', attrs: { 'aria-hidden': 'true' } });
  swatch.style.setProperty('--project-color', project.color);
  const copy = make('span', { className: 'projects-project-row__copy' }, [
    make('strong', { text: project.name }),
    make('small', { text: `${project.key} · ${completion}% · ${health.overdue} overdue` }),
  ]);
  open.append(swatch, copy);
  row.appendChild(open);
  if (project.archived && canManageSpecificProject(project)) {
    row.appendChild(actionButton('Restore', 'restore-project', {
      className: 'projects-row-action', dataset: { projectId: project.id },
    }));
  } else if (!project.archived && canManageSpecificProject(project)) {
    row.append(
      actionButton('Edit', 'edit-project', {
        className: 'projects-row-action', dataset: { projectId: project.id },
      }),
      actionButton('Archive', 'archive-project', {
        className: 'projects-row-action', dataset: { projectId: project.id },
      }),
    );
  }
  return row;
}

function renderMobileControls() {
  clear(refs.mobileControls);
  const projectLabel = make('label', { className: 'projects-field projects-field--compact' }, [
    make('span', { text: 'Project' }),
  ]);
  const projectSelect = make('select', {
    attrs: { 'aria-label': 'Current project' },
    dataset: { action: 'mobile-project' },
  });
  state.projects.filter((project) => !project.archived).forEach((project) => {
    const option = make('option', { value: project.id, text: `${project.key} · ${project.name}` });
    option.selected = project.id === state.activeProjectId;
    projectSelect.appendChild(option);
  });
  projectLabel.appendChild(projectSelect);

  const stageLabel = make('label', { className: 'projects-field projects-field--compact' }, [
    make('span', { text: 'Stage' }),
  ]);
  const stageSelect = make('select', {
    attrs: { 'aria-label': 'Visible board stage' },
    dataset: { action: 'mobile-stage' },
    disabled: state.activeView !== 'board',
  });
  state.stages.forEach((stage) => {
    const option = make('option', { value: stage.id, text: stage.name });
    option.selected = stage.id === state.mobileStageId;
    stageSelect.appendChild(option);
  });
  stageLabel.appendChild(stageSelect);
  refs.mobileControls.append(projectLabel, stageLabel);
}

function renderToolbar() {
  clear(refs.toolbar);
  const title = make('div', { className: 'projects-toolbar__title' });
  if (state.project) {
    title.append(
      make('h2', { text: state.project.name }),
      make('p', { text: state.project.description || 'Plan, execute, review, and ship.' }),
    );
  }
  const tabs = make('div', { className: 'projects-view-tabs', attrs: { role: 'tablist', 'aria-label': 'Project view' } });
  [['board', 'Board'], ['list', 'List'], ['activity', 'Activity']].forEach(([view, label]) => {
    const tab = actionButton(label, 'set-view', {
      className: `projects-view-tab${state.activeView === view ? ' is-active' : ''}`,
      dataset: { view },
    });
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-selected', String(state.activeView === view));
    tabs.appendChild(tab);
  });
  const archivedCount = state.items.filter((item) => item.archived).length;
  const actions = make('div', { className: 'projects-toolbar__actions' }, [
    actionButton(`Archived (${archivedCount})`, 'show-archived-tasks', {
      className: 'projects-btn projects-btn--quiet', disabled: !state.project || archivedCount === 0,
    }),
    actionButton('Members', 'manage-members', { className: 'projects-btn projects-btn--quiet', disabled: !state.project || !canManageProject() }),
    actionButton('Stages', 'manage-stages', { className: 'projects-btn projects-btn--quiet', disabled: !state.project || !canManageProject() }),
    actionButton('New task', 'new-task', {
      className: 'projects-btn projects-btn--primary',
      disabled: !state.project || !state.stages.length || !canEditProject(),
    }),
  ]);
  refs.toolbar.append(title, tabs, actions);
}

function selectOption(value, label, selected) {
  const option = make('option', { value, text: label });
  option.selected = value === selected;
  return option;
}

function renderFilters() {
  clear(refs.filters);
  if (!state.project || state.activeView === 'activity') {
    refs.filters.hidden = true;
    return;
  }
  refs.filters.hidden = false;
  const search = make('input', {
    type: 'search',
    value: state.filters.search,
    placeholder: 'Search key, title, labels',
    attrs: { 'aria-label': 'Search tasks' },
    dataset: { filter: 'search' },
  });
  const priority = make('select', { attrs: { 'aria-label': 'Filter by priority' }, dataset: { filter: 'priority' } }, [
    selectOption('', 'All priorities', state.filters.priority),
    ...PRIORITIES.slice().reverse().map((value) => selectOption(value, value[0].toUpperCase() + value.slice(1), state.filters.priority)),
  ]);
  const type = make('select', { attrs: { 'aria-label': 'Filter by task type' }, dataset: { filter: 'type' } }, [
    selectOption('', 'All types', state.filters.type),
    ...ITEM_TYPES.map((value) => selectOption(value, value[0].toUpperCase() + value.slice(1), state.filters.type)),
  ]);
  const due = make('select', { attrs: { 'aria-label': 'Filter by due date' }, dataset: { filter: 'due' } }, [
    selectOption('', 'Any due date', state.filters.due),
    selectOption('overdue', 'Overdue', state.filters.due),
    selectOption('soon', 'Due in 7 days', state.filters.due),
    selectOption('none', 'No due date', state.filters.due),
  ]);
  const attachments = make('select', { attrs: { 'aria-label': 'Filter by attachments' }, dataset: { filter: 'attachments' } }, [
    selectOption('', 'Any deliverables', state.filters.attachments),
    selectOption('with', 'Has files', state.filters.attachments),
    selectOption('without', 'No files', state.filters.attachments),
  ]);
  const label = make('input', {
    type: 'search', value: state.filters.label, placeholder: 'Label',
    attrs: { 'aria-label': 'Filter by label' }, dataset: { filter: 'label' },
  });
  const assignee = make('input', {
    type: 'search', value: state.filters.assignee, placeholder: 'Assignee',
    attrs: { 'aria-label': 'Filter by assignee' }, dataset: { filter: 'assignee' },
  });
  const clearFilters = actionButton('Clear', 'clear-filters', { className: 'projects-text-btn' });
  refs.filters.append(search, priority, type, due, attachments, label, assignee, clearFilters);
}

function renderCurrentView() {
  clear(refs.view);
  refs.view.dataset.view = state.activeView;
  if (state.loadingProjects || state.loadingBoard) {
    refs.view.appendChild(renderLoadingState());
    return;
  }
  if (state.loadError) {
    refs.view.appendChild(renderErrorState(state.loadError));
    return;
  }
  if (!state.projects.length) {
    refs.view.appendChild(renderFirstProjectState());
    return;
  }
  if (!state.project) {
    refs.view.appendChild(make('div', { className: 'projects-empty-state' }, [
      make('h2', { text: 'Choose a project' }),
      make('p', { text: 'Select a project from the navigator to open its workflow.' }),
    ]));
    return;
  }
  if (state.activeView === 'list') renderListView();
  else if (state.activeView === 'activity') renderActivityView();
  else renderBoardView();
}

function renderLoadingState() {
  return make('div', { className: 'projects-loading', attrs: { role: 'status' } }, [
    make('span', { className: 'projects-spinner', attrs: { 'aria-hidden': 'true' } }),
    make('p', { text: state.loadingProjects ? 'Loading projects…' : 'Loading project board…' }),
  ]);
}

function renderErrorState(message) {
  return make('div', { className: 'projects-empty-state projects-empty-state--error', attrs: { role: 'alert' } }, [
    make('h2', { text: 'Projects could not load' }),
    make('p', { text: message }),
    actionButton('Try again', 'retry-load', { className: 'projects-btn projects-btn--primary' }),
  ]);
}

function renderFirstProjectState() {
  return make('div', { className: 'projects-empty-state' }, [
    make('p', { className: 'projects-eyebrow', text: 'Build momentum with a visible workflow' }),
    make('h2', { text: 'Create your first project' }),
    make('p', { text: 'Start from General, Personal, Research, Software, Content, Coursework, or GTM.' }),
    actionButton('Create project', 'new-project', { className: 'projects-btn projects-btn--primary' }),
  ]);
}

function stageSelectForItem(item, { compact = false, deferMove = false } = {}) {
  const dataset = { taskId: item.id };
  if (!deferMove) dataset.action = 'move-task-select';
  const select = make('select', {
    className: `projects-status-select${compact ? ' projects-status-select--compact' : ''}`,
    attrs: { 'aria-label': `Move ${item.key || item.title} to stage` },
    dataset,
  });
  select.disabled = !canEditProject() || state.movingTasks.has(item.id) ||
    state.submittingTasks.has(taskScopeKey(state.activeProjectId, item.id));
  state.stages.forEach((stage) => {
    const option = selectOption(stage.id, stage.name, item.stage_id);
    select.appendChild(option);
  });
  return select;
}

function renderBoardView() {
  if (!state.stages.length) {
    refs.view.appendChild(make('div', { className: 'projects-empty-state' }, [
      make('h2', { text: 'This workflow has no stages' }),
      make('p', { text: 'Create stages before adding tasks.' }),
      actionButton('Manage stages', 'manage-stages', { className: 'projects-btn projects-btn--primary' }),
    ]));
    return;
  }
  if (!state.mobileStageId || !state.stages.some((stage) => stage.id === state.mobileStageId)) {
    state.mobileStageId = state.stages[0].id;
  }
  const filtered = applyTaskFilters(state.items, state.filters);
  const visibleStages = visibleStagesForViewport(state.stages, isMobileViewport(), state.mobileStageId);
  const board = make('div', {
    className: 'projects-board',
    attrs: { 'aria-label': `${state.project.name} task board` },
  });
  visibleStages.forEach((stage) => board.appendChild(renderStageColumn(stage, filtered)));
  refs.view.appendChild(board);
  restoreColumnScrollSoon();
}

function renderStageColumn(stage, filteredItems) {
  const stageItems = filteredItems
    .filter((item) => item.stage_id === stage.id)
    .sort((a, b) => a.position - b.position);
  const allActiveCount = state.items.filter((item) => !item.archived && item.stage_id === stage.id).length;
  const overLimit = stage.wip_limit !== null && allActiveCount > stage.wip_limit;
  const column = make('section', {
    className: `projects-column${overLimit ? ' is-over-limit' : ''}`,
    dataset: { stageId: stage.id },
    attrs: { 'aria-labelledby': `projects-stage-${stage.id}` },
  });
  const swatch = make('span', { className: 'projects-stage-swatch', attrs: { 'aria-hidden': 'true' } });
  swatch.style.setProperty('--stage-color', stage.color);
  const heading = make('div', { className: 'projects-column__heading' }, [
    swatch,
    make('h3', { id: `projects-stage-${stage.id}`, text: stage.name }),
    make('span', {
      className: `projects-stage-count${overLimit ? ' is-over-limit' : ''}`,
      text: stage.wip_limit === null ? allActiveCount : `${allActiveCount}/${stage.wip_limit}`,
      attrs: { title: overLimit ? 'Work-in-progress limit exceeded' : 'Task count' },
    }),
    actionButton('Add', 'quick-add', {
      className: 'projects-column__add',
      title: `Add task to ${stage.name}`,
      dataset: { stageId: stage.id }, disabled: !canEditProject(),
    }),
  ]);
  const list = make('div', {
    className: 'projects-column__cards',
    dataset: { dropStage: stage.id, stageScroll: stage.id },
    attrs: { role: 'list', 'aria-label': `${stage.name} tasks` },
  });
  if (state.quickStageId === stage.id) list.appendChild(renderQuickTaskForm(stage));
  stageItems.forEach((item) => list.appendChild(renderTaskCard(item)));
  if (!stageItems.length && state.quickStageId !== stage.id) {
    list.appendChild(make('p', { className: 'projects-column__empty', text: 'No matching tasks' }));
  }
  column.append(heading, list);
  return column;
}

function renderQuickTaskForm(stage) {
  const form = make('form', {
    className: 'projects-quick-task',
    dataset: { form: 'quick-task', stageId: stage.id },
  });
  const input = make('input', {
    name: 'title',
    placeholder: `Task title for ${stage.name}`,
    attrs: { required: 'true', maxlength: '240', 'aria-label': 'Task title' },
  });
  form.append(input, make('div', { className: 'projects-quick-task__actions' }, [
    make('button', { type: 'submit', className: 'projects-btn projects-btn--primary', text: 'Add task' }),
    actionButton('Cancel', 'cancel-quick-add', { className: 'projects-btn projects-btn--quiet' }),
  ]));
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => input.focus());
  return form;
}

function taskIndicator(text, tone = '') {
  return make('span', { className: `projects-indicator${tone ? ` projects-indicator--${tone}` : ''}`, text });
}

function renderTaskCard(item) {
  const card = make('article', {
    className: `projects-card projects-card--priority-${item.priority}${item.blocked ? ' is-blocked' : ''}`,
    draggable: !isMobileViewport() && canEditProject() && !state.movingTasks.has(item.id),
    dataset: { taskId: item.id, stageId: item.stage_id },
    attrs: { role: 'listitem' },
  });
  const top = make('div', { className: 'projects-card__top' }, [
    taskIndicator(item.type),
    make('span', { className: 'projects-card__key', text: item.key || 'NEW' }),
    taskIndicator(item.priority, item.priority === 'critical' || item.priority === 'highest' ? 'danger' : ''),
  ]);
  const open = actionButton(item.title, 'open-task', {
    className: 'projects-card__title',
    title: `Open ${item.key || item.title}`,
    dataset: { taskId: item.id },
  });
  const indicators = make('div', { className: 'projects-card__indicators' });
  if (item.due_date) {
    const bucket = dueBucket(item);
    indicators.appendChild(taskIndicator(`Due ${formatDate(item.due_date)}`, bucket === 'overdue' ? 'danger' : bucket === 'soon' ? 'warning' : ''));
  }
  if (item.checklist_count) indicators.appendChild(taskIndicator(`Checklist ${item.checklist_done}/${item.checklist_count}`));
  if (item.blocked) indicators.appendChild(taskIndicator('Blocked', 'danger'));
  if (item.attachment_count) indicators.appendChild(taskIndicator(`${item.attachment_count} file${item.attachment_count === 1 ? '' : 's'}`));
  if (item.assignee_name) indicators.appendChild(taskIndicator(item.assignee_name));
  card.append(top, open, indicators, stageSelectForItem(item, { compact: true }));
  return card;
}

function renderListView() {
  const filtered = applyTaskFilters(state.items, state.filters);
  const list = make('div', { className: 'projects-task-table', attrs: { role: 'table', 'aria-label': `${state.project.name} tasks` } });
  const header = make('div', { className: 'projects-task-row projects-task-row--header', attrs: { role: 'row' } }, [
    make('span', { text: 'Task', attrs: { role: 'columnheader' } }),
    make('span', { text: 'Priority', attrs: { role: 'columnheader' } }),
    make('span', { text: 'Due', attrs: { role: 'columnheader' } }),
    make('span', { text: 'Status', attrs: { role: 'columnheader' } }),
  ]);
  list.appendChild(header);
  filtered.forEach((item) => {
    const taskCell = make('div', { className: 'projects-task-row__task', attrs: { role: 'cell' } }, [
      make('small', { text: `${item.key || 'NEW'} · ${item.type}` }),
      actionButton(item.title, 'open-task', { className: 'projects-list-task-title', dataset: { taskId: item.id } }),
    ]);
    const due = make('span', {
      className: dueBucket(item) === 'overdue' ? 'is-danger' : '',
      text: item.due_date ? formatDate(item.due_date) : '—',
      attrs: { role: 'cell' },
    });
    const row = make('div', { className: 'projects-task-row', attrs: { role: 'row' } }, [
      taskCell,
      make('span', { text: item.priority, attrs: { role: 'cell' } }),
      due,
      make('span', { attrs: { role: 'cell' } }, [stageSelectForItem(item)]),
    ]);
    list.appendChild(row);
  });
  if (!filtered.length) list.appendChild(make('p', { className: 'projects-empty-inline', text: 'No tasks match these filters.' }));
  refs.view.appendChild(list);
}

function renderActivityView() {
  if (!state.activityLoaded) {
    refs.view.appendChild(renderLoadingState());
    if (!state.activityLoading) loadActivity();
    return;
  }
  const timeline = make('ol', { className: 'projects-activity-list', attrs: { 'aria-label': 'Project activity' } });
  if (!state.activity.length) {
    timeline.appendChild(make('li', { className: 'projects-empty-inline', text: 'No activity recorded yet.' }));
  }
  state.activity.forEach((entry) => {
    const item = make('li', { className: 'projects-activity-entry' });
    const marker = make('span', { className: 'projects-activity-entry__marker', attrs: { 'aria-hidden': 'true' } });
    const copy = make('div', {}, [
      make('p', { text: entry.text }),
      make('small', { text: `${entry.actor}${entry.created_at ? ` · ${formatDate(entry.created_at, { includeTime: true })}` : ''}` }),
    ]);
    if (entry.item_id) {
      copy.appendChild(actionButton(entry.item_key || 'Open task', 'open-task', {
        className: 'projects-text-btn', dataset: { taskId: entry.item_id },
      }));
    }
    item.append(marker, copy);
    timeline.appendChild(item);
  });
  refs.view.appendChild(timeline);
  if (state.activityNextBefore) {
    refs.view.appendChild(actionButton(
      state.activityLoading ? 'Loading earlier activity…' : 'Load earlier activity',
      'load-earlier-activity',
      { className: 'projects-btn projects-btn--quiet', disabled: state.activityLoading },
    ));
  }
}

function restoreColumnScrollSoon() {
  const saved = state.preserved.get(state.activeProjectId);
  if (!saved || typeof requestAnimationFrame !== 'function') return;
  requestAnimationFrame(() => {
    Object.entries(saved.columnScroll || {}).forEach(([stageId, top]) => {
      const element = [...refs.view.querySelectorAll('[data-stage-scroll]')]
        .find((candidate) => candidate.dataset.stageScroll === stageId);
      if (element) element.scrollTop = top;
    });
  });
}

async function loadProjects({ preserveProject = true } = {}) {
  state.loadingProjects = true;
  state.loadError = '';
  renderCurrentView();
  const token = state.projectGate.next();
  const signal = abortController('projectController');
  try {
    const [projectsPayload, overviewPayload] = await Promise.all([
      request('/api/projects?include_archived=true', { signal }),
      optionalRequest('/api/projects/overview', { signal }),
    ]);
    if (!state.projectGate.current(token) || !state.open) return;
    state.projects = asArray(projectsPayload.projects || projectsPayload.items || projectsPayload)
      .map(normalizeProject);
    state.globalOverview = overviewPayload?.overview || overviewPayload || null;
    state.loadingProjects = false;
    renderNavigator();
    renderMobileControls();
    const savedId = preserveProject ? state.activeProjectId : null;
    let desired = state.projects.find((project) => project.id === savedId && !project.archived)?.id;
    if (!desired && typeof localStorage !== 'undefined') {
      try {
        const remembered = localStorage.getItem('restia-projects-active');
        desired = state.projects.find((project) => project.id === remembered && !project.archived)?.id;
      } catch (_) {}
    }
    desired ||= state.projects.find((project) => !project.archived)?.id || null;
    if (desired) await selectProject(desired, { force: true });
    else {
      state.project = null;
      state.activeProjectId = null;
      renderAll();
    }
  } catch (error) {
    if (error?.name === 'AbortError') return;
    if (!state.projectGate.current(token)) return;
    state.loadingProjects = false;
    state.loadError = error?.message || 'Unknown error';
    renderAll();
  }
}

async function selectProject(projectId, { force = false } = {}) {
  const id = asId(projectId);
  if (!id || (!force && id === state.activeProjectId && state.project)) return;
  if (state.selectedItem && id !== state.activeProjectId) {
    const closed = await closeTaskDetail();
    if (!closed) return;
  }
  saveProjectUiState();
  state.activeProjectId = id;
  const saved = restoreProjectUiState(id);
  state.loadingBoard = true;
  state.loadError = '';
  state.selectedItem = null;
  state.drawerDirty = false;
  state.activity = [];
  state.activityLoaded = false;
  state.activityLoading = false;
  state.activityNextBefore = null;
  state.drawerDraft = null;
  renderAll();
  try { localStorage.setItem('restia-projects-active', id); } catch (_) {}
  const token = state.boardGate.next();
  const signal = abortController('boardController');
  try {
    const payload = await request(projectPath(id, '/board?include_archived=true'), { signal });
    if (!state.boardGate.current(token) || state.activeProjectId !== id || !state.open) return;
    const board = normalizeBoard(payload);
    state.project = board.project || state.projects.find((project) => project.id === id) || null;
    state.stages = board.stages;
    state.items = board.items;
    state.members = board.members;
    state.overview = board.overview;
    state.activity = board.activity;
    state.activityLoaded = false;
    if (!state.mobileStageId || !state.stages.some((stage) => stage.id === state.mobileStageId)) {
      state.mobileStageId = state.stages[0]?.id || null;
    }
    state.loadingBoard = false;
    renderAll();
    restoreScroll(saved);
  } catch (error) {
    if (error?.name === 'AbortError') return;
    if (!state.boardGate.current(token)) return;
    state.loadingBoard = false;
    state.loadError = error?.message || 'Could not load project';
    renderAll();
  }
}

async function loadActivity({ append = false } = {}) {
  if (!state.activeProjectId || state.activityLoading) return;
  if (append && !state.activityNextBefore) return;
  const projectId = state.activeProjectId;
  const before = append ? state.activityNextBefore : null;
  state.activityLoading = true;
  if (append && state.activeView === 'activity') renderCurrentView();
  const token = state.activityGate.next();
  const signal = abortController('activityController');
  try {
    const suffix = `/activity?limit=100${before ? `&before=${encodeURIComponent(before)}` : ''}`;
    const payload = await request(projectPath(projectId, suffix), { signal });
    if (!state.activityGate.current(token) || state.activeProjectId !== projectId) return;
    const page = asArray(payload.activity || payload.items).map(normalizeActivity);
    state.activity = append ? mergeUniqueRows(state.activity, page) : page;
    state.activityNextBefore = payload.next_before || null;
    state.activityLoaded = true;
    state.activityLoading = false;
    if (state.activeView === 'activity') renderCurrentView();
  } catch (error) {
    if (error?.name === 'AbortError') return;
    if (!state.activityGate.current(token)) return;
    state.activityLoading = false;
    state.activityLoaded = true;
    showToast(`Activity failed to load: ${error.message}`, 'error');
    if (state.activeView === 'activity') renderCurrentView();
  }
}

export async function moveTask(taskId, stageId, position = null, { source = 'status' } = {}) {
  if (!canEditProject()) {
    announce('You have view-only access to this project.', 'assertive');
    return false;
  }
  const item = state.items.find((candidate) => candidate.id === asId(taskId));
  const stage = state.stages.find((candidate) => candidate.id === asId(stageId));
  if (!item || !stage || !state.activeProjectId) return false;
  if (state.submittingTasks.has(taskScopeKey(state.activeProjectId, item.id))) {
    announce(`${item.key || item.title} is submitting work. Wait for it to finish before moving it.`, 'assertive');
    return false;
  }
  if (item.stage_id === stage.id && position === null) return true;
  const taskKey = item.id;
  if (state.movingTasks.has(taskKey)) {
    announce(`${item.key || item.title} is already moving.`, 'assertive');
    return false;
  }
  state.movingTasks.add(taskKey);
  const moveVersion = (state.moveVersions.get(taskKey) || 0) + 1;
  state.moveVersions.set(taskKey, moveVersion);
  const projectId = state.activeProjectId;
  const snapshot = state.items.map((candidate) => ({ ...candidate }));
  state.items = optimisticMove(state.items, taskKey, stage.id, position);
  renderTopSummary();
  renderNavigator();
  renderCurrentView();
  announce(`${item.key || item.title} moving to ${stage.name}`);
  try {
    const payload = await request(projectPath(projectId, `/items/${encodeURIComponent(taskKey)}/move`), {
      method: 'POST',
      body: {
        stage_id: stage.id,
        ...(position === null || position === undefined ? {} : { position }),
        version: item.version,
      },
    });
    if (state.moveVersions.get(taskKey) !== moveVersion || state.activeProjectId !== projectId || !state.open) {
      state.movingTasks.delete(taskKey);
      return true;
    }
    state.movingTasks.delete(taskKey);
    if (payload.item) {
      const current = state.items.find((candidate) => candidate.id === taskKey) || item;
      const normalized = mergeItemPayload(current, payload.item);
      state.items = state.items.map((candidate) => candidate.id === taskKey ? { ...candidate, ...normalized } : candidate);
      if (state.selectedItem?.id === taskKey) {
        const previousStageId = state.selectedItem.stage_id;
        state.selectedItem = mergeItemPayload(state.selectedItem, payload.item);
        syncDrawerDraftStage(previousStageId, state.selectedItem.stage_id);
      }
    }
    renderTopSummary();
    renderNavigator();
    renderCurrentView();
    if (state.selectedItem?.id === taskKey) renderTaskDrawer();
    announce(`${item.key || item.title} moved to ${stage.name}`);
    if (source === 'drag') showToast(`Moved ${item.key || item.title} to ${stage.name}`);
    return true;
  } catch (error) {
    state.movingTasks.delete(taskKey);
    if (state.moveVersions.get(taskKey) === moveVersion && state.activeProjectId === projectId && state.open) {
      state.items = snapshot;
      if (state.selectedItem?.id === taskKey) {
        const restored = snapshot.find((candidate) => candidate.id === taskKey);
        if (restored) state.selectedItem = { ...state.selectedItem, ...restored };
      }
      renderTopSummary();
      renderNavigator();
      renderCurrentView();
      if (state.selectedItem?.id === taskKey) renderTaskDrawer();
      announce(`Move failed. ${item.key || item.title} returned to its previous stage.`, 'assertive');
      showToast(`Could not move task: ${error.message}`, 'error');
    }
    return false;
  }
}

function renderTaskDrawer() {
  clear(refs.drawer);
  if (!state.selectedItem) {
    refs.drawer.hidden = true;
    refs.root?.classList.remove('has-task-drawer');
    return;
  }
  refs.drawer.hidden = false;
  refs.root?.classList.add('has-task-drawer');
  const item = state.selectedItem;
  const editable = canEditProject() && !item.archived;
  const submitting = state.submittingTasks.has(taskScopeKey(state.activeProjectId, item.id));
  const header = make('header', { className: 'projects-drawer__header' }, [
    make('div', {}, [
      make('p', { className: 'projects-eyebrow', text: `${item.key || 'Task'} · ${item.type}` }),
      make('h2', { id: 'projects-drawer-title', text: item.title }),
    ]),
    actionButton('Close', 'drawer-close', { className: 'projects-btn projects-btn--quiet', title: 'Close task details' }),
  ]);
  refs.drawer.appendChild(header);
  if (state.drawerLoading) {
    refs.drawer.appendChild(renderLoadingState());
    return;
  }

  // Keep task details and the checklist/comment/upload forms as siblings. Nested
  // forms are invalid HTML and can bypass the delegated submit handler, causing
  // a full-page navigation when an inline action is submitted.
  const form = make('form', {
    id: 'projects-task-details-form',
    className: 'projects-task-form',
    dataset: { form: 'task-details' },
  });
  form.appendChild(taskCoreFields(item, editable && !submitting));
  refs.drawer.append(
    form,
    renderChecklistSection(item, editable && !submitting),
    renderDeliverablesSection(item, editable),
    renderCommentsSection(item, editable && !submitting),
    renderTaskActivitySection(item),
  );

  const footer = make('footer', { className: 'projects-drawer__footer' });
  if (editable) {
    footer.appendChild(make('button', {
      type: 'submit', disabled: submitting, className: 'projects-btn projects-btn--primary',
      attrs: { form: 'projects-task-details-form' },
      text: submitting ? 'Submission in progress…' : 'Save changes',
    }));
  }
  if (item.archived && canEditProject()) {
    footer.appendChild(actionButton('Restore task', 'restore-task', { className: 'projects-btn projects-btn--quiet', disabled: submitting }));
  } else if (canEditProject()) {
    footer.appendChild(actionButton('Archive task', 'archive-task', { className: 'projects-btn projects-btn--danger', disabled: submitting }));
  }
  refs.drawer.appendChild(footer);
}

function field(label, control, { hint = '', wide = false } = {}) {
  const wrapper = make('label', { className: `projects-field${wide ? ' projects-field--wide' : ''}` }, [
    make('span', { text: label }),
  ]);
  wrapper.appendChild(control);
  if (hint) wrapper.appendChild(make('small', { text: hint }));
  return wrapper;
}

function taskCoreFields(item, editable) {
  const draft = activeDrawerDraft(item)?.values || taskDraftFromItem(item).values;
  const section = make('section', { className: 'projects-drawer-section' }, [make('h3', { text: 'Task details' })]);
  const title = make('input', {
    name: 'title', value: draft.title, disabled: !editable,
    attrs: { required: 'true', maxlength: '240' }, dataset: { taskField: 'title' },
  });
  const description = make('textarea', {
    name: 'description', value: draft.description, disabled: !editable,
    attrs: { rows: '6', maxlength: '20000' }, dataset: { taskField: 'description' },
  });
  description.value = draft.description;
  const grid = make('div', { className: 'projects-field-grid' });
  const type = make('select', { name: 'item_type', disabled: !editable, dataset: { taskField: 'item_type' } },
    ITEM_TYPES.map((value) => selectOption(value, value[0].toUpperCase() + value.slice(1), draft.item_type)));
  const priority = make('select', { name: 'priority', disabled: !editable, dataset: { taskField: 'priority' } },
    PRIORITIES.slice().reverse().map((value) => selectOption(value, value[0].toUpperCase() + value.slice(1), draft.priority)));
  const stage = stageSelectForItem({ ...item, stage_id: draft.stage_id }, { deferMove: true });
  stage.name = 'stage_id';
  stage.disabled = !editable;
  stage.dataset.taskField = 'stage_id';
  const assignee = make('select', { name: 'assignee', disabled: !editable, dataset: { taskField: 'assignee' } }, [
    selectOption('', 'Unassigned', draft.assignee),
  ]);
  const assignableMembers = state.members.filter((member) => member.role === 'owner' || member.role === 'editor');
  assignableMembers.forEach((member) => assignee.appendChild(selectOption(member.id || member.name, member.name, draft.assignee)));
  if (draft.assignee && !assignableMembers.some((member) => member.id === draft.assignee || member.name === draft.assignee)) {
    assignee.appendChild(selectOption(draft.assignee, draft.assignee, draft.assignee));
  }
  const labels = make('input', {
    name: 'labels', value: draft.labels, disabled: !editable,
    placeholder: 'design, launch, customer', dataset: { taskField: 'labels' },
  });
  const startDate = make('input', { type: 'date', name: 'start_date', value: draft.start_date, disabled: !editable, dataset: { taskField: 'start_date' } });
  const dueDate = make('input', { type: 'date', name: 'due_date', value: draft.due_date, disabled: !editable, dataset: { taskField: 'due_date' } });
  const estimate = make('input', { type: 'number', name: 'estimate_minutes', value: draft.estimate_minutes, disabled: !editable, attrs: { min: '0', step: '15' }, dataset: { taskField: 'estimate_minutes' } });
  const logged = make('input', { type: 'number', name: 'logged_minutes', value: draft.logged_minutes, disabled: !editable, attrs: { min: '0', step: '15' }, dataset: { taskField: 'logged_minutes' } });
  const parent = make('select', { name: 'parent_id', disabled: !editable, dataset: { taskField: 'parent_id' } }, [selectOption('', 'No parent', draft.parent_id)]);
  const blocker = make('select', { name: 'blocked_by_id', disabled: !editable, dataset: { taskField: 'blocked_by_id' } }, [selectOption('', 'Not blocked', draft.blocked_by_id)]);
  state.items.filter((candidate) => candidate.id !== item.id && !candidate.archived).forEach((candidate) => {
    const label = `${candidate.key || 'Task'} · ${candidate.title}`;
    if (candidate.type !== 'subtask') parent.appendChild(selectOption(candidate.id, label, draft.parent_id));
    blocker.appendChild(selectOption(candidate.id, label, draft.blocked_by_id));
  });
  grid.append(
    field('Type', type), field('Priority', priority), field('Stage', stage), field('Assignee', assignee),
    field('Start date', startDate), field('Due date', dueDate),
    field('Estimate (minutes)', estimate, { hint: formatMinutes(draft.estimate_minutes) }),
    field('Logged (minutes)', logged, { hint: formatMinutes(draft.logged_minutes) }),
    field('Parent task', parent), field('Blocked by', blocker),
  );
  section.append(field('Title', title, { wide: true }), field('Description', description, { wide: true }), field('Labels', labels, { wide: true }), grid);
  return section;
}

function renderChecklistSection(item, editable) {
  const section = make('section', { className: 'projects-drawer-section' }, [
    make('div', { className: 'projects-section-heading' }, [
      make('h3', { text: 'Checklist' }),
      make('span', { text: `${item.checklist.filter((entry) => entry.done).length}/${item.checklist.length}` }),
    ]),
  ]);
  const list = make('ul', { className: 'projects-checklist' });
  item.checklist.slice().sort((a, b) => a.position - b.position).forEach((entry) => {
    const checkbox = make('input', {
      type: 'checkbox', checked: entry.done, disabled: !editable,
      dataset: { action: 'toggle-checklist', checklistId: entry.id },
      attrs: { 'aria-label': `Mark ${entry.text} ${entry.done ? 'incomplete' : 'complete'}` },
    });
    const row = make('li', { className: entry.done ? 'is-complete' : '' }, [
      checkbox,
      make('span', { text: entry.text }),
    ]);
    if (editable) row.appendChild(actionButton('Remove', 'delete-checklist', {
      className: 'projects-row-action', dataset: { checklistId: entry.id },
    }));
    list.appendChild(row);
  });
  section.appendChild(list);
  if (item.checklist_truncated) {
    section.appendChild(make('p', {
      className: 'projects-empty-inline',
      text: `Showing ${item.checklist.length} of ${item.checklist_total} checklist steps.`,
    }));
  }
  if (editable) {
    const form = make('form', { className: 'projects-inline-form', dataset: { form: 'checklist' } }, [
      make('input', { name: 'text', placeholder: 'Add a checklist step', attrs: { required: 'true', maxlength: '500' } }),
      make('button', { type: 'submit', className: 'projects-btn projects-btn--quiet', text: 'Add' }),
    ]);
    section.appendChild(form);
  }
  return section;
}

function renderDeliverablesSection(item, editable) {
  const submitting = state.submittingTasks.has(taskScopeKey(state.activeProjectId, item.id));
  const mutationsAllowed = editable && !submitting;
  const section = make('section', { className: 'projects-drawer-section projects-deliverables' }, [
    make('div', { className: 'projects-section-heading' }, [
      make('h3', { text: 'Deliverables' }),
      make('span', { text: `${item.attachments.length} uploaded` }),
    ]),
  ]);
  const existing = make('ul', { className: 'projects-attachment-list' });
  item.attachments.forEach((attachment) => existing.appendChild(renderExistingAttachment(attachment, mutationsAllowed)));
  if (!item.attachments.length) existing.appendChild(make('li', { className: 'projects-empty-inline', text: 'No uploaded files yet.' }));
  section.appendChild(existing);
  if (!editable) return section;

  const inputId = `projects-attachment-input-${item.id}`;
  const dropzone = make('div', {
    className: 'projects-dropzone',
    dataset: { attachmentDropzone: item.id },
    attrs: { role: 'group', 'aria-label': 'Add task deliverables' },
  }, [
    make('p', { text: 'Drop work here, or choose files' }),
    make('small', { text: 'PDF, Office, images, text/data, ZIP, STL, STEP, or IGES' }),
    make('label', { className: 'projects-btn projects-btn--quiet', text: 'Choose files', attrs: { for: inputId } }),
    make('input', {
      id: inputId, type: 'file', hidden: true,
      disabled: submitting,
      attrs: { multiple: 'true', accept: ACCEPT_STRING },
      dataset: { action: 'attachment-files', itemId: item.id },
    }),
  ]);
  section.appendChild(dropzone);

  const queue = attachmentQueues.get(state.activeProjectId, item.id);
  const queueList = make('ul', { className: 'projects-upload-queue', attrs: { 'aria-label': 'Pending uploads' } });
  queue.forEach((entry) => queueList.appendChild(renderQueueEntry(entry)));
  section.appendChild(queueList);
  if (queue.length) section.appendChild(renderSubmitWorkForm(item));
  return section;
}

function attachmentKindLabel(kind) {
  if (kind === 'deliverable') return 'Final deliverable';
  return kind === 'draft' ? 'Draft' : 'Reference';
}

function attachmentDownloadUrl(itemId, attachment) {
  if (attachment.download_url) return attachment.download_url;
  return `/api/projects/attachments/${encodeURIComponent(attachment.id)}/download`;
}

function renderExistingAttachment(attachment, editable) {
  const row = make('li', { className: 'projects-attachment-row' });
  const copy = make('div', { className: 'projects-attachment-row__copy' }, [
    make('strong', { text: attachment.name }),
    make('small', { text: `${attachmentKindLabel(attachment.kind)} · ${formatBytes(attachment.size)}${attachment.description ? ` · ${attachment.description}` : ''}` }),
  ]);
  const link = make('a', {
    className: 'projects-row-action', text: 'Download',
    attrs: {
      href: attachmentDownloadUrl(state.selectedItem.id, attachment),
      download: attachment.name,
      rel: 'noopener',
    },
  });
  row.append(copy, link);
  if (editable && canDeleteAttachment(attachment)) row.appendChild(actionButton('Delete', 'delete-attachment', {
    className: 'projects-row-action projects-row-action--danger', dataset: { attachmentId: attachment.id },
  }));
  return row;
}

function renderQueueEntry(entry) {
  const submitting = state.submittingTasks.has(taskScopeKey(state.activeProjectId, state.selectedItem?.id));
  const row = make('li', { className: `projects-upload-row is-${entry.status}`, dataset: { queueId: entry.queueId } });
  const copy = make('div', { className: 'projects-upload-row__copy' }, [
    make('strong', { text: entry.file.name || 'File' }),
    make('small', { text: `${formatBytes(entry.file.size)} · ${entry.status === 'error' ? entry.error : entry.status}` }),
  ]);
  const kind = make('select', {
    disabled: submitting,
    attrs: { 'aria-label': `File kind for ${entry.file.name}` },
    dataset: { action: 'queue-kind', queueId: entry.queueId },
  }, [
    selectOption('reference', 'Reference', entry.kind),
    selectOption('draft', 'Draft', entry.kind),
    selectOption('deliverable', 'Final deliverable', entry.kind),
  ]);
  const note = make('input', {
    value: entry.description,
    disabled: submitting,
    placeholder: 'File note',
    attrs: { 'aria-label': `Note for ${entry.file.name}`, maxlength: '500' },
    dataset: { action: 'queue-note', queueId: entry.queueId },
  });
  const progress = make('progress', { attrs: { max: '100', value: String(entry.progress || 0), 'aria-label': `Upload progress for ${entry.file.name}` } });
  progress.value = entry.progress || 0;
  row.append(copy, kind, note, progress);
  if (entry.status === 'error') row.appendChild(actionButton('Retry', 'retry-upload', {
    className: 'projects-row-action', dataset: { queueId: entry.queueId }, disabled: submitting,
  }));
  row.appendChild(actionButton(entry.status === 'uploading' ? 'Cancel' : 'Remove', 'remove-queued-file', {
    className: 'projects-row-action', dataset: { queueId: entry.queueId }, disabled: submitting,
  }));
  return row;
}

function renderSubmitWorkForm(item) {
  const submitting = state.submittingTasks.has(taskScopeKey(state.activeProjectId, item.id));
  const form = make('form', { className: 'projects-submit-work', dataset: { form: 'submit-work' } });
  const note = make('textarea', { name: 'submission_note', disabled: submitting, placeholder: 'Submission note', attrs: { rows: '2', maxlength: '2000' } });
  const transition = make('select', { name: 'transition_stage_id', disabled: submitting, attrs: { 'aria-label': 'Stage after submission' } }, [
    selectOption('', 'Keep current stage', ''),
  ]);
  state.stages.filter((stage) => stage.id !== item.stage_id).forEach((stage) => {
    const preferred = stage.category === 'review' || /review/i.test(stage.name);
    const option = selectOption(stage.id, `Move to ${stage.name}`, preferred ? stage.id : '');
    if (preferred) option.selected = true;
    transition.appendChild(option);
  });
  form.append(
    field('Submission note', note, { wide: true }),
    field('After submitting', transition, { wide: true }),
    make('button', {
      type: 'submit', disabled: submitting, className: 'projects-btn projects-btn--primary',
      text: submitting ? 'Submitting…' : 'Submit work',
    }),
  );
  return form;
}

function renderCommentsSection(item, editable) {
  const section = make('section', { className: 'projects-drawer-section' }, [
    make('h3', { text: 'Comments' }),
  ]);
  const list = make('ol', { className: 'projects-comment-list' });
  item.comments.forEach((comment) => {
    const row = make('li', {}, [
      make('p', { text: comment.body }),
      make('small', { text: `${comment.author}${comment.created_at ? ` · ${formatDate(comment.created_at, { includeTime: true })}` : ''}` }),
    ]);
    if (editable && canDeleteComment(comment)) row.appendChild(actionButton('Delete', 'delete-comment', {
      className: 'projects-row-action projects-row-action--danger', dataset: { commentId: comment.id },
    }));
    list.appendChild(row);
  });
  if (!item.comments.length) list.appendChild(make('li', { className: 'projects-empty-inline', text: 'No comments yet.' }));
  section.appendChild(list);
  if (item.comments_truncated) {
    const remaining = Math.max(0, item.comments_total - item.comments.length);
    const pagination = make('div', { className: 'projects-inline-actions' }, [
      make('p', {
        className: 'projects-empty-inline',
        text: `Showing ${item.comments.length} of ${item.comments_total} comments.`,
      }),
    ]);
    if (item.comments_next_before) {
      pagination.appendChild(actionButton(
        `Load earlier comments${remaining ? ` (${remaining} remaining)` : ''}`,
        'load-earlier-comments',
        { className: 'projects-btn projects-btn--quiet' },
      ));
    }
    section.appendChild(pagination);
  }
  if (editable) {
    section.appendChild(make('form', { className: 'projects-inline-form projects-inline-form--stacked', dataset: { form: 'comment' } }, [
      make('textarea', { name: 'body', placeholder: 'Add context, a decision, or an update', attrs: { required: 'true', rows: '3', maxlength: '5000' } }),
      make('button', { type: 'submit', className: 'projects-btn projects-btn--quiet', text: 'Add comment' }),
    ]));
  }
  return section;
}

function renderTaskActivitySection(item) {
  const section = make('section', { className: 'projects-drawer-section' }, [make('h3', { text: 'Task activity' })]);
  const list = make('ol', { className: 'projects-mini-activity' });
  item.activity.forEach((entry) => list.appendChild(make('li', {}, [
    make('p', { text: entry.text }),
    make('small', { text: `${entry.actor}${entry.created_at ? ` · ${formatDate(entry.created_at, { includeTime: true })}` : ''}` }),
  ])));
  if (!item.activity.length) list.appendChild(make('li', { className: 'projects-empty-inline', text: 'No activity yet.' }));
  section.appendChild(list);
  if (item.activity_next_before) {
    const loading = state.taskActivityLoading.has(taskScopeKey(state.activeProjectId, item.id));
    section.appendChild(actionButton(
      loading ? 'Loading earlier activity…' : 'Load earlier task activity',
      'load-earlier-task-activity',
      { className: 'projects-btn projects-btn--quiet', disabled: loading },
    ));
  }
  return section;
}

async function openTaskDetail(taskId, trigger = null) {
  const id = asId(taskId);
  const summary = state.items.find((item) => item.id === id);
  if (!summary || !state.activeProjectId) return;
  if (state.selectedItem && state.selectedItem.id !== id) {
    const closed = await closeTaskDetail();
    if (!closed) return;
  }
  state.detailPreviousFocus = trigger || document.activeElement;
  state.selectedItem = { ...summary };
  state.drawerLoading = true;
  state.drawerDirty = false;
  state.drawerDraft = null;
  renderTaskDrawer();
  refs.drawer?.focus();
  const projectId = state.activeProjectId;
  const token = state.detailGate.next();
  const signal = abortController('detailController');
  try {
    const payload = await request(projectPath(projectId, `/items/${encodeURIComponent(id)}`), { signal });
    if (!state.detailGate.current(token) || state.activeProjectId !== projectId || state.selectedItem?.id !== id) return;
    const activity = asArray(payload.activity || payload.item?.activity).map(normalizeActivity);
    const detail = normalizeItem({
      ...summary,
      ...(payload.item || {}),
      checklist: payload.checklist || payload.item?.checklist,
      checklist_total: payload.checklist_total,
      checklist_truncated: payload.checklist_truncated,
      comments: payload.comments || payload.item?.comments,
      comments_total: payload.comments_total,
      comments_truncated: payload.comments_truncated,
      comments_next_before: payload.comments_next_before,
      attachments: payload.attachments || payload.item?.attachments,
      activity,
      activity_next_before: payload.activity_next_before || (activity.length >= 100 ? activityCursorFromEntries(activity) : null),
    });
    state.selectedItem = detail;
    state.drawerLoading = false;
    state.drawerDraft = taskDraftFromItem(detail);
    state.items = state.items.map((item) => item.id === detail.id ? { ...item, ...detail } : item);
    renderTaskDrawer();
  } catch (error) {
    if (error?.name === 'AbortError') return;
    if (!state.detailGate.current(token)) return;
    state.drawerLoading = false;
    showToast(`Task details failed to load: ${error.message}`, 'error');
    renderTaskDrawer();
  }
}

async function closeTaskDetail({ force = false } = {}) {
  if (!state.selectedItem) return true;
  if (!force && state.drawerDirty) {
    const discard = await confirmAction('Discard unsaved task changes?', { confirmText: 'Discard', danger: true });
    if (!discard) return false;
  }
  state.detailGate.invalidate();
  try { state.detailController?.abort(); } catch (_) {}
  const previous = state.detailPreviousFocus;
  state.selectedItem = null;
  state.drawerDirty = false;
  state.drawerDraft = null;
  renderTaskDrawer();
  try { previous?.focus?.(); } catch (_) {}
  return true;
}

function taskFormPayload(form) {
  const value = (name) => form.elements?.namedItem(name)?.value ?? '';
  const assignee = String(value('assignee'));
  const startDate = String(value('start_date'));
  const dueDate = String(value('due_date'));
  const parentId = asId(value('parent_id'));
  const blockedById = asId(value('blocked_by_id'));
  return {
    title: String(value('title')).trim(),
    description: String(value('description')),
    item_type: String(value('item_type')),
    priority: String(value('priority')),
    stage_id: asId(value('stage_id')),
    assignee: assignee || null,
    clear_assignee: !assignee,
    labels: String(value('labels')).split(',').map((entry) => entry.trim()).filter(Boolean),
    start_date: startDate || null,
    clear_start_date: !startDate,
    due_date: dueDate || null,
    clear_due_date: !dueDate,
    estimate_minutes: Math.max(0, asNumber(value('estimate_minutes'))),
    logged_minutes: Math.max(0, asNumber(value('logged_minutes'))),
    parent_id: parentId || null,
    clear_parent: !parentId,
    blocked_by_id: blockedById || null,
    clear_blocked_by: !blockedById,
  };
}

async function saveTaskDetails(form) {
  if (!state.selectedItem || !canEditProject()) return;
  const context = captureSelectedTaskContext();
  if (!context) return;
  const payload = taskFormPayload(form);
  if (!payload.title) {
    showToast('Task title is required.', 'error');
    form.elements?.namedItem('title')?.focus();
    return;
  }
  const itemId = context.itemId;
  const projectId = context.projectId;
  const expectedVersion = state.selectedItem.version;
  const targetStageId = payload.stage_id;
  delete payload.stage_id;
  // The Save button lives in the drawer footer and is associated through its
  // `form` attribute, so it is not a descendant of the details form.
  const submit = refs.drawer?.querySelector(
    'button[type="submit"][form="projects-task-details-form"]',
  );
  if (submit) submit.disabled = true;
  try {
    const result = await request(projectPath(projectId, `/items/${encodeURIComponent(itemId)}`), {
      method: 'PATCH', body: { ...payload, version: expectedVersion },
    });
    const current = taskContextMatches(context)
      ? state.selectedItem
      : state.items.find((item) => item.id === itemId);
    const updated = mergeItemPayload(current || { id: itemId }, result.item || payload);
    state.items = state.items.map((item) => item.id === itemId ? { ...item, ...updated } : item);
    if (targetStageId && targetStageId !== updated.stage_id) {
      if (state.activeProjectId === projectId) {
        await moveTask(itemId, targetStageId, null, { source: 'detail' });
      } else {
        await request(projectPath(projectId, `/items/${encodeURIComponent(itemId)}/move`), {
          method: 'POST', body: { stage_id: targetStageId, version: updated.version },
        });
      }
    }
    if (taskContextMatches(context)) {
      const moved = state.items.find((item) => item.id === itemId) || updated;
      state.selectedItem = { ...state.selectedItem, ...moved };
      state.drawerDirty = false;
      state.drawerDraft = taskDraftFromItem(state.selectedItem);
      renderAll();
    }
    showToast(`${updated.key || 'Task'} saved`);
  } catch (error) {
    showToast(`Could not save task: ${error.message}`, 'error');
    if (submit) submit.disabled = false;
  }
}

function openDialog(title, content, { labelledBy = '', onClose = null } = {}) {
  closeDialog();
  const previous = document.activeElement;
  const titleId = labelledBy || `projects-dialog-title-${Date.now()}`;
  const backdrop = make('div', { className: 'projects-dialog-backdrop' });
  const dialog = make('section', {
    className: 'projects-dialog',
    attrs: { role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId },
  });
  const header = make('header', { className: 'projects-dialog__header' }, [
    make('h2', { id: titleId, text: title }),
    actionButton('Close', 'close-dialog', { className: 'projects-btn projects-btn--quiet', title: `Close ${title}` }),
  ]);
  dialog.append(header, content);
  backdrop.appendChild(dialog);
  refs.dialogHost.appendChild(backdrop);
  const close = () => {
    if (!backdrop.isConnected) return;
    backdrop.remove();
    state.dialogClose = null;
    try { onClose?.(); } catch (_) {}
    try { previous?.focus?.(); } catch (_) {}
  };
  backdrop.addEventListener('mousedown', (event) => { if (event.target === backdrop) close(); });
  state.dialogClose = close;
  const first = dialog.querySelector('input:not([disabled]), select:not([disabled]), textarea:not([disabled]), button:not([disabled])');
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => first?.focus?.());
  else first?.focus?.();
  return { backdrop, dialog, close };
}

function closeDialog() {
  if (typeof state.dialogClose === 'function') state.dialogClose();
  else clear(refs.dialogHost);
  state.dialogClose = null;
}

function trapDialogTab(event) {
  const dialog = refs.dialogHost?.querySelector?.('[role="dialog"]');
  if (!dialog) return false;
  const focusable = [...dialog.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')]
    .filter((element) => !element.hidden);
  if (!focusable.length) return false;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault(); last.focus(); return true;
  }
  if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault(); first.focus(); return true;
  }
  return false;
}

function openProjectEditor(project = null) {
  if (project && !canManageSpecificProject(project)) return;
  const editing = Boolean(project);
  const form = make('form', { className: 'projects-dialog-form', dataset: { form: 'project-editor', projectId: project?.id || '' } });
  const name = make('input', { name: 'name', value: project?.name || '', placeholder: 'Project name', attrs: { required: 'true', maxlength: '120' } });
  const key = make('input', { name: 'key', value: project?.key || '', placeholder: 'REST', attrs: { maxlength: '12', pattern: '[A-Za-z][A-Za-z0-9_-]{1,11}' } });
  const description = make('textarea', { name: 'description', placeholder: 'What outcome does this project exist to create?', attrs: { rows: '4', maxlength: '2000' } });
  description.value = project?.description || '';
  const template = make('select', { name: 'template', disabled: editing }, [
    ...PROJECT_TEMPLATES
      .map((value) => selectOption(value, value[0].toUpperCase() + value.slice(1), project?.template || 'general')),
  ]);
  const color = make('input', { type: 'color', name: 'color', value: /^#[0-9a-f]{6}$/i.test(project?.color || '') ? project.color : '#e06c75' });
  form.append(
    field('Name', name, { wide: true }),
    field('Key', key, { hint: 'Short task prefix, for example REST', wide: true }),
    field('Description', description, { wide: true }),
    field('Template', template, { hint: editing ? 'Template is fixed after creation.' : 'Creates a useful starting workflow.', wide: true }),
    field('Color', color, { wide: true }),
    make('footer', { className: 'projects-dialog__footer' }, [
      actionButton('Cancel', 'close-dialog', { className: 'projects-btn projects-btn--quiet' }),
      make('button', { type: 'submit', className: 'projects-btn projects-btn--primary', text: editing ? 'Save project' : 'Create project' }),
    ]),
  );
  openDialog(editing ? 'Edit project' : 'Create project', form);
}

async function submitProjectEditor(form) {
  const id = asId(form.dataset.projectId);
  const values = form.elements;
  const payload = {
    name: String(values.namedItem('name')?.value || '').trim(),
    key: String(values.namedItem('key')?.value || '').trim().toUpperCase() || undefined,
    description: String(values.namedItem('description')?.value || '').trim(),
    template: String(values.namedItem('template')?.value || 'general'),
    color: String(values.namedItem('color')?.value || '#e06c75'),
  };
  if (id) {
    delete payload.template;
    payload.version = Math.max(1, state.projects.find((candidate) => candidate.id === id)?.version ?? 1);
  }
  if (!payload.name) return;
  const submit = form.querySelector('button[type="submit"]');
  if (submit) submit.disabled = true;
  try {
    const result = await request(id ? projectPath(id) : '/api/projects', { method: id ? 'PATCH' : 'POST', body: payload });
    const project = normalizeProject(result.project || payload);
    if (id) state.projects = state.projects.map((candidate) => candidate.id === id ? { ...candidate, ...project } : candidate);
    else state.projects.unshift(project);
    closeDialog();
    await loadProjects({ preserveProject: Boolean(id) });
    if (!id && project.id) await selectProject(project.id, { force: true });
    showToast(id ? 'Project updated' : 'Project created');
  } catch (error) {
    showToast(`Could not ${id ? 'update' : 'create'} project: ${error.message}`, 'error');
    if (submit) submit.disabled = false;
  }
}

async function archiveProject(projectId) {
  const project = state.projects.find((candidate) => candidate.id === asId(projectId));
  if (!project || !canManageSpecificProject(project)) return;
  const accepted = await confirmAction(`Archive “${project.name}”? Its tasks and files remain available for restore.`, {
    confirmText: 'Archive', danger: true,
  });
  if (!accepted) return;
  try {
    await request(projectPath(project.id, '/archive'), { method: 'POST', body: { version: project.version } });
    if (state.activeProjectId === project.id) {
      saveProjectUiState();
      state.activeProjectId = null;
      state.project = null;
    }
    await loadProjects({ preserveProject: true });
    showToast('Project archived');
  } catch (error) {
    showToast(`Could not archive project: ${error.message}`, 'error');
  }
}

async function restoreProject(projectId) {
  const project = state.projects.find((candidate) => candidate.id === asId(projectId));
  if (!project || !canManageSpecificProject(project)) return;
  try {
    await request(projectPath(project.id, '/restore'), { method: 'POST', body: { version: project.version } });
    await loadProjects({ preserveProject: false });
    await selectProject(project.id, { force: true });
    showToast('Project restored');
  } catch (error) {
    showToast(`Could not restore project: ${error.message}`, 'error');
  }
}

function openArchivedTasksDialog() {
  if (!state.project) return;
  const archived = state.items
    .filter((item) => item.archived)
    .sort((a, b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
  const content = make('div', { className: 'projects-archived-tasks' }, [
    make('p', { text: 'Archived tasks stay out of the active board while their history and deliverables remain available.' }),
  ]);
  const list = make('ul', { className: 'projects-archived-tasks__list' });
  archived.forEach((item) => {
    const stage = state.stages.find((candidate) => candidate.id === item.stage_id);
    const copy = make('div', { className: 'projects-archived-tasks__copy' }, [
      make('strong', { text: item.title }),
      make('small', {
        text: `${item.key || 'Task'}${stage ? ` · ${stage.name}` : ''}${item.updated_at ? ` · Updated ${formatDate(item.updated_at)}` : ''}`,
      }),
    ]);
    const actions = make('div', { className: 'projects-archived-tasks__actions' }, [
      actionButton('View', 'open-archived-task', {
        className: 'projects-row-action', dataset: { taskId: item.id },
      }),
    ]);
    if (canEditProject()) actions.appendChild(actionButton('Restore', 'restore-archived-task', {
      className: 'projects-row-action', dataset: { taskId: item.id },
    }));
    list.appendChild(make('li', {}, [copy, actions]));
  });
  if (!archived.length) list.appendChild(make('li', { className: 'projects-empty-inline', text: 'No archived tasks.' }));
  content.append(list, make('footer', { className: 'projects-dialog__footer' }, [
    actionButton('Close', 'close-dialog', { className: 'projects-btn projects-btn--quiet' }),
  ]));
  openDialog(`Archived tasks (${archived.length})`, content);
}

async function restoreArchivedTask(taskId, control = null) {
  if (!canEditProject()) return false;
  const item = state.items.find((candidate) => candidate.id === asId(taskId) && candidate.archived);
  if (!item) return false;
  const projectId = state.activeProjectId;
  if (!projectId) return false;
  if (control) control.disabled = true;
  try {
    const result = await request(projectPath(projectId, `/items/${encodeURIComponent(item.id)}/restore`), {
      method: 'POST', body: { version: item.version },
    });
    if (!state.open || state.activeProjectId !== projectId) {
      if (control?.isConnected) control.disabled = false;
      return false;
    }
    const updated = mergeItemPayload(item, { ...(result.item || {}), archived: false });
    state.items = state.items.map((candidate) => candidate.id === item.id ? { ...candidate, ...updated } : candidate);
    if (state.selectedItem?.id === item.id) state.selectedItem = updated;
    renderAll();
    announce(`${item.key || item.title} restored`);
    showToast(`${item.key || item.title} restored`);
    return true;
  } catch (error) {
    showToast(`Could not restore task: ${error.message}`, 'error');
    if (control?.isConnected) control.disabled = false;
    return false;
  }
}

function openTaskCreateDialog(defaultStageId = null) {
  if (!canEditProject()) return;
  if (!state.stages.length) {
    showToast('Create a workflow stage before adding tasks.', 'error');
    return;
  }
  const form = make('form', { className: 'projects-dialog-form', dataset: { form: 'task-create' } });
  const title = make('input', { name: 'title', placeholder: 'What needs to be done?', attrs: { required: 'true', maxlength: '240' } });
  const stage = make('select', { name: 'stage_id' });
  state.stages.forEach((entry) => stage.appendChild(selectOption(entry.id, entry.name, asId(defaultStageId) || state.mobileStageId || state.stages[0]?.id)));
  const type = make('select', { name: 'item_type', dataset: { action: 'task-create-type' } }, ITEM_TYPES.map((value) => selectOption(value, value[0].toUpperCase() + value.slice(1), 'task')));
  const priority = make('select', { name: 'priority' }, PRIORITIES.slice().reverse().map((value) => selectOption(value, value[0].toUpperCase() + value.slice(1), 'medium')));
  const parent = make('select', { name: 'parent_id', disabled: true }, [selectOption('', 'Choose a parent task', '')]);
  state.items
    .filter((item) => !item.archived && item.type !== 'subtask')
    .forEach((item) => parent.appendChild(selectOption(item.id, `${item.key || 'Task'} · ${item.title}`, '')));
  const parentField = field('Parent task', parent, { hint: 'Required for subtasks.', wide: true });
  parentField.hidden = true;
  parentField.dataset.taskCreateParent = 'true';
  form.append(
    field('Task title', title, { wide: true }), field('Stage', stage), field('Type', type), field('Priority', priority),
    parentField,
    make('footer', { className: 'projects-dialog__footer' }, [
      actionButton('Cancel', 'close-dialog', { className: 'projects-btn projects-btn--quiet' }),
      make('button', { type: 'submit', className: 'projects-btn projects-btn--primary', text: 'Create task' }),
    ]),
  );
  openDialog('Create task', form);
}

async function createTask(payload, { openAfter = true } = {}) {
  if (!state.activeProjectId || !canEditProject()) return null;
  const projectId = state.activeProjectId;
  const fallbackIndex = state.items.length;
  try {
    const result = await request(projectPath(projectId, '/items'), { method: 'POST', body: payload });
    const item = normalizeItem(result.item || payload, fallbackIndex);
    if (!state.open || state.activeProjectId !== projectId) {
      announce(`${item.key || item.title} created`);
      return item;
    }
    state.items.push(item);
    state.quickStageId = null;
    closeDialog();
    renderAll();
    announce(`${item.key || item.title} created`);
    if (openAfter && item.id) await openTaskDetail(item.id);
    return item;
  } catch (error) {
    showToast(`Could not create task: ${error.message}`, 'error');
    return null;
  }
}

function openStageManager() {
  if (!canManageProject()) return;
  const content = make('div', { className: 'projects-stage-manager' });
  const intro = make('p', { text: 'Stages are project-specific. Categories drive completion reporting; WIP limits expose bottlenecks.' });
  const list = make('ol', { className: 'projects-stage-manager__list' });
  state.stages.forEach((stage, index) => list.appendChild(renderStageManagerRow(stage, index)));
  const form = make('form', { className: 'projects-stage-create', dataset: { form: 'stage-create' } });
  const name = make('input', { name: 'name', placeholder: 'New stage name', attrs: { required: 'true', maxlength: '80' } });
  const category = make('select', { name: 'category' }, [
    ...STAGE_CATEGORIES.map((value) => selectOption(value, value.replace('_', ' ').replace(/^./, (letter) => letter.toUpperCase()), 'todo')),
  ]);
  const color = make('input', { type: 'color', name: 'color', value: '#7f849c', attrs: { 'aria-label': 'Stage color' } });
  const wip = make('input', { type: 'number', name: 'wip_limit', placeholder: 'No WIP limit', attrs: { min: '1', max: '10000', 'aria-label': 'Work in progress limit' } });
  form.append(name, category, color, wip, make('button', { type: 'submit', className: 'projects-btn projects-btn--primary', text: 'Add stage' }));
  content.append(intro, list, form);
  openDialog('Manage workflow stages', content);
}

function renderStageManagerRow(stage, index) {
  const row = make('li', { className: 'projects-stage-manager__row', dataset: { stageId: stage.id } });
  const name = make('input', { value: stage.name, attrs: { 'aria-label': 'Stage name', maxlength: '80' }, dataset: { stageField: 'name' } });
  const category = make('select', { attrs: { 'aria-label': 'Stage category' }, dataset: { stageField: 'category' } }, [
    ...STAGE_CATEGORIES.map((value) => selectOption(value, value.replace('_', ' ').replace(/^./, (letter) => letter.toUpperCase()), stage.category)),
  ]);
  const color = make('input', { type: 'color', value: /^#[0-9a-f]{6}$/i.test(stage.color) ? stage.color : '#7f849c', attrs: { 'aria-label': 'Stage color' }, dataset: { stageField: 'color' } });
  const wip = make('input', { type: 'number', value: stage.wip_limit ?? '', placeholder: 'No WIP', attrs: { min: '1', max: '10000', 'aria-label': 'Work in progress limit' }, dataset: { stageField: 'wip_limit' } });
  row.append(
    make('span', { className: 'projects-stage-manager__order', text: index + 1 }), name, category, color, wip,
    actionButton('Save', 'save-stage', { className: 'projects-row-action', dataset: { stageId: stage.id } }),
    actionButton('Up', 'move-stage-up', { className: 'projects-row-action', dataset: { stageId: stage.id }, disabled: index === 0 }),
    actionButton('Down', 'move-stage-down', { className: 'projects-row-action', dataset: { stageId: stage.id }, disabled: index === state.stages.length - 1 }),
    actionButton('Delete', 'delete-stage', { className: 'projects-row-action projects-row-action--danger', dataset: { stageId: stage.id }, disabled: state.stages.length <= 1 }),
  );
  return row;
}

async function createStage(form) {
  if (!canManageProject()) return;
  const values = form.elements;
  const payload = {
    name: String(values.namedItem('name')?.value || '').trim(),
    category: String(values.namedItem('category')?.value || 'todo'),
    color: String(values.namedItem('color')?.value || '#7f849c'),
    wip_limit: values.namedItem('wip_limit')?.value === '' ? null : Math.max(1, asNumber(values.namedItem('wip_limit')?.value, 1)),
  };
  if (!payload.name) return;
  try {
    const result = await request(projectPath(state.activeProjectId, '/stages'), { method: 'POST', body: payload });
    state.stages.push(normalizeStage(result.stage || payload, state.stages.length));
    closeDialog(); openStageManager(); renderAll();
    announce(`${payload.name} stage created`);
  } catch (error) { showToast(`Could not create stage: ${error.message}`, 'error'); }
}

async function saveStage(stageId, row) {
  if (!canManageProject() || !row) return;
  const get = (name) => row.querySelector(`[data-stage-field="${name}"]`);
  const clearWipLimit = get('wip_limit')?.value === '';
  const payload = {
    name: String(get('name')?.value || '').trim(),
    category: String(get('category')?.value || 'todo'),
    color: String(get('color')?.value || '#7f849c'),
    wip_limit: clearWipLimit ? null : Math.max(1, asNumber(get('wip_limit')?.value, 1)),
    clear_wip_limit: clearWipLimit,
  };
  if (!payload.name) return;
  try {
    const result = await request(projectPath(state.activeProjectId, `/stages/${encodeURIComponent(stageId)}`), { method: 'PATCH', body: payload });
    const previous = state.stages.find((stage) => stage.id === asId(stageId));
    const updated = normalizeStage(result.stage || { ...payload, id: stageId });
    state.stages = state.stages.map((stage) => stage.id === stageId ? { ...stage, ...updated } : stage);
    if (previous && previous.category !== updated.category) {
      const advance = (item) => item.stage_id === updated.id && !item.archived
        ? { ...item, version: item.version + 1 }
        : item;
      state.items = state.items.map(advance);
      if (state.selectedItem) state.selectedItem = advance(state.selectedItem);
    }
    closeDialog(); openStageManager(); renderAll();
    announce(`${updated.name} stage updated`);
  } catch (error) { showToast(`Could not update stage: ${error.message}`, 'error'); }
}

async function reorderStage(stageId, direction) {
  if (!canManageProject()) return;
  const index = state.stages.findIndex((stage) => stage.id === asId(stageId));
  const target = index + direction;
  if (index < 0 || target < 0 || target >= state.stages.length) return;
  const snapshot = state.stages.map((stage) => ({ ...stage }));
  const next = state.stages.map((stage) => ({ ...stage }));
  [next[index], next[target]] = [next[target], next[index]];
  next.forEach((stage, position) => { stage.position = position; });
  state.stages = next;
  closeDialog(); openStageManager(); renderAll();
  try {
    const result = await request(projectPath(state.activeProjectId, '/stages/order'), {
      method: 'PUT', body: { stage_ids: next.map((stage) => stage.id) },
    });
    if (result.stages) state.stages = result.stages.map(normalizeStage).sort((a, b) => a.position - b.position);
    announce('Stage order updated');
  } catch (error) {
    state.stages = snapshot;
    closeDialog(); openStageManager(); renderAll();
    showToast(`Could not reorder stages: ${error.message}`, 'error');
  }
}

async function deleteStage(stageId) {
  if (!canManageProject()) return;
  const stage = state.stages.find((candidate) => candidate.id === asId(stageId));
  if (!stage || state.stages.length <= 1) return;
  const fallback = state.stages.find((candidate) => candidate.id !== stage.id);
  const count = state.items.filter((item) => !item.archived && item.stage_id === stage.id).length;
  const accepted = await confirmAction(
    `Delete “${stage.name}”? ${count} task${count === 1 ? '' : 's'} will move to “${fallback.name}”.`,
    { confirmText: 'Delete stage', danger: true },
  );
  if (!accepted) return;
  try {
    const moveTarget = encodeURIComponent(fallback.id);
    const result = await request(projectPath(state.activeProjectId, `/stages/${encodeURIComponent(stage.id)}?move_to_stage_id=${moveTarget}`), {
      method: 'DELETE',
    });
    state.stages = result.stages ? result.stages.map(normalizeStage) : state.stages.filter((candidate) => candidate.id !== stage.id);
    const moveDeletedStageItem = (item) => item.stage_id === stage.id
      ? { ...item, stage_id: fallback.id, version: item.version + 1 }
      : item;
    state.items = state.items.map(moveDeletedStageItem);
    if (state.selectedItem) state.selectedItem = moveDeletedStageItem(state.selectedItem);
    state.mobileStageId = fallback.id;
    closeDialog(); openStageManager(); renderAll();
    announce(`${stage.name} deleted; tasks moved to ${fallback.name}`);
  } catch (error) { showToast(`Could not delete stage: ${error.message}`, 'error'); }
}

function openMembersDialog() {
  if (!canManageProject()) return;
  const content = make('div', { className: 'projects-members' });
  content.appendChild(make('p', {
    text: 'Editors can create and update work. Viewers can follow progress and download deliverables. Only the owner can manage the project workflow and membership.',
  }));
  const list = make('ul', { className: 'projects-members__list' });
  const ownerName = state.project.owner || 'Project owner';
  list.appendChild(make('li', { className: 'projects-member-row projects-member-row--owner' }, [
    make('div', {}, [make('strong', { text: ownerName }), make('small', { text: 'Owner' })]),
    make('span', { className: 'projects-indicator', text: 'Owner' }),
  ]));
  state.members
    .filter((member) => member.role !== 'owner' && member.username !== state.project.owner)
    .forEach((member) => list.appendChild(renderMemberRow(member)));
  content.appendChild(list);
  const form = make('form', { className: 'projects-member-add', dataset: { form: 'member-add' } });
  const username = make('input', {
    name: 'username', placeholder: 'Local Restia username',
    attrs: { required: 'true', maxlength: '120', autocomplete: 'off' },
  });
  const role = make('select', { name: 'role', attrs: { 'aria-label': 'New member role' } }, [
    selectOption('viewer', 'Viewer', 'viewer'), selectOption('editor', 'Editor', 'viewer'),
  ]);
  form.append(username, role, make('button', { type: 'submit', className: 'projects-btn projects-btn--primary', text: 'Add member' }));
  content.appendChild(form);
  openDialog('Project members', content);
}

function renderMemberRow(member) {
  const username = member.username || member.name;
  const role = make('select', {
    attrs: { 'aria-label': `Role for ${username}` },
    dataset: { action: 'member-role', username },
  }, [
    selectOption('viewer', 'Viewer', member.role), selectOption('editor', 'Editor', member.role),
  ]);
  return make('li', { className: 'projects-member-row' }, [
    make('div', {}, [make('strong', { text: member.name || username }), make('small', { text: username })]),
    role,
    actionButton('Transfer ownership', 'transfer-project', {
      className: 'projects-row-action', dataset: { username },
    }),
    actionButton('Remove', 'remove-member', {
      className: 'projects-row-action projects-row-action--danger', dataset: { username },
    }),
  ]);
}

async function addProjectMember(form) {
  if (!canManageProject()) return;
  const username = String(form.elements.namedItem('username')?.value || '').trim();
  const role = String(form.elements.namedItem('role')?.value || 'viewer');
  if (!username) return;
  try {
    const result = await request(projectPath(state.activeProjectId, '/members'), {
      method: 'POST', body: { username, role: role === 'editor' ? 'editor' : 'viewer' },
    });
    const member = normalizeMember(result.member || { username, role });
    state.members = [...state.members.filter((candidate) => candidate.username !== member.username), member];
    closeDialog(); openMembersDialog(); renderAll();
    announce(`${member.name} added as ${member.role}`);
  } catch (error) { showToast(`Could not add member: ${error.message}`, 'error'); }
}

function unassignMemberLocally(username) {
  const normalized = String(username || '').trim().toLowerCase();
  if (!normalized) return;
  const clear = (item) => {
    const assigned = String(item.assignee_name || item.assignee_id || '').trim().toLowerCase();
    return assigned === normalized
      ? { ...item, assignee: null, assignee_id: '', assignee_name: '', version: item.version + 1 }
      : item;
  };
  state.items = state.items.map(clear);
  if (state.selectedItem) state.selectedItem = clear(state.selectedItem);
}

async function updateProjectMember(username, role, control) {
  if (!canManageProject()) return;
  const current = state.members.find((member) => member.username === username);
  const previousRole = current?.role || 'viewer';
  try {
    const result = await request(projectPath(state.activeProjectId, `/members/${encodeURIComponent(username)}`), {
      method: 'PATCH', body: { role: role === 'editor' ? 'editor' : 'viewer' },
    });
    const member = normalizeMember(result.member || { ...current, username, role });
    state.members = state.members.map((candidate) => candidate.username === username ? member : candidate);
    if (member.role === 'viewer') unassignMemberLocally(username);
    renderAll();
    announce(`${member.name} is now ${member.role}`);
  } catch (error) {
    if (control) control.value = previousRole;
    showToast(`Could not change member role: ${error.message}`, 'error');
  }
}

async function removeProjectMember(username) {
  if (!canManageProject()) return;
  const member = state.members.find((candidate) => candidate.username === username);
  if (!member) return;
  const accepted = await confirmAction(`Remove ${member.name || username} from “${state.project.name}”?`, {
    confirmText: 'Remove member', danger: true,
  });
  if (!accepted) return;
  try {
    await request(projectPath(state.activeProjectId, `/members/${encodeURIComponent(username)}`), { method: 'DELETE' });
    state.members = state.members.filter((candidate) => candidate.username !== username);
    unassignMemberLocally(username);
    closeDialog(); openMembersDialog(); renderAll();
    announce(`${member.name || username} removed`);
  } catch (error) { showToast(`Could not remove member: ${error.message}`, 'error'); }
}

async function transferProjectOwnership(username) {
  if (!canManageProject()) return;
  const member = state.members.find((candidate) => candidate.username === username);
  if (!member) return;
  const projectId = state.activeProjectId;
  const projectVersion = state.project.version;
  const projectName = state.project.name;
  const accepted = await confirmAction(
    `Transfer ownership of “${projectName}” to ${member.name || username}? You will become an editor and only the new owner can reverse this.`,
    { confirmText: 'Transfer ownership', danger: true },
  );
  if (!accepted) return;
  try {
    const result = await request(projectPath(projectId, '/transfer'), {
      method: 'POST', body: { username, version: projectVersion },
    });
    const board = normalizeBoard(result);
    const transferredProject = board.project || {
      ...state.projects.find((project) => project.id === projectId),
      owner: username,
      role: 'editor',
    };
    if (!transferredProject.role || transferredProject.role === 'owner') {
      transferredProject.role = 'editor';
    }
    state.projects = state.projects.map((project) => (
      project.id === projectId ? { ...project, ...transferredProject } : project
    ));
    // The owner may switch projects while the confirmation/request is in
    // flight. Keep the completed transfer in the navigator, but never replace
    // whichever board is active now with the stale response.
    if (!state.open || state.activeProjectId !== projectId) {
      if (state.open) renderNavigator();
      announce(`Ownership of ${projectName} transferred to ${member.name || username}`);
      showToast(`Ownership of ${projectName} transferred. You are now an editor.`);
      return;
    }
    state.project = transferredProject;
    state.stages = board.stages.length ? board.stages : state.stages;
    state.members = board.members.length ? board.members : state.members;
    state.overview = board.overview || state.overview;
    closeDialog(); renderAll();
    announce(`Ownership transferred to ${member.name || username}`);
    showToast('Ownership transferred. You are now an editor.');
  } catch (error) { showToast(`Could not transfer ownership: ${error.message}`, 'error'); }
}

async function addChecklistItem(form) {
  if (!state.selectedItem || !canEditProject()) return;
  const context = captureSelectedTaskContext();
  if (!context) return;
  const input = form.elements.namedItem('text');
  const text = String(input?.value || '').trim();
  if (!text) return;
  try {
    const result = await request(projectPath(context.projectId, `/items/${encodeURIComponent(context.itemId)}/checklist`), {
      method: 'POST', body: { text, position: state.selectedItem.checklist.length },
    });
    const entry = normalizeChecklistItem(result.checklist_item || { id: `local-${Date.now()}`, text });
    if (!taskContextMatches(context)) return;
    state.selectedItem.checklist.push(entry);
    syncSelectedItemIntoBoard();
    renderTaskDrawer();
    announce('Checklist item added');
  } catch (error) { showToast(`Could not add checklist item: ${error.message}`, 'error'); }
}

async function toggleChecklistItem(checklistId, done, control) {
  if (!state.selectedItem || !canEditProject()) return;
  const context = captureSelectedTaskContext();
  if (!context) return;
  const entry = state.selectedItem.checklist.find((candidate) => candidate.id === asId(checklistId));
  if (!entry) return;
  const before = entry.done;
  entry.done = done;
  syncSelectedItemIntoBoard();
  renderTaskDrawer();
  try {
    const result = await request(projectPath(context.projectId, `/items/${encodeURIComponent(context.itemId)}/checklist/${encodeURIComponent(entry.id)}`), {
      method: 'PATCH', body: { done },
    });
    Object.assign(entry, normalizeChecklistItem(result.checklist_item || entry));
    if (taskContextMatches(context)) {
      syncSelectedItemIntoBoard();
      announce(done ? 'Checklist item completed' : 'Checklist item reopened');
    }
  } catch (error) {
    entry.done = before;
    if (taskContextMatches(context)) {
      if (control) control.checked = before;
      syncSelectedItemIntoBoard(); renderTaskDrawer();
    }
    showToast(`Could not update checklist: ${error.message}`, 'error');
  }
}

async function deleteChecklistItem(checklistId) {
  if (!state.selectedItem || !canEditProject()) return;
  const context = captureSelectedTaskContext();
  if (!context) return;
  try {
    await request(projectPath(context.projectId, `/items/${encodeURIComponent(context.itemId)}/checklist/${encodeURIComponent(checklistId)}`), { method: 'DELETE' });
    if (!taskContextMatches(context)) return;
    state.selectedItem.checklist = state.selectedItem.checklist.filter((entry) => entry.id !== asId(checklistId));
    syncSelectedItemIntoBoard(); renderTaskDrawer();
    announce('Checklist item removed');
  } catch (error) { showToast(`Could not remove checklist item: ${error.message}`, 'error'); }
}

async function addComment(form) {
  if (!state.selectedItem || !canEditProject()) return;
  const context = captureSelectedTaskContext();
  if (!context) return;
  const input = form.elements.namedItem('body');
  const body = String(input?.value || '').trim();
  if (!body) return;
  try {
    const result = await request(projectPath(context.projectId, `/items/${encodeURIComponent(context.itemId)}/comments`), {
      method: 'POST', body: { body },
    });
    if (!taskContextMatches(context)) return;
    state.selectedItem.comments.push(normalizeComment(result.comment || { id: `local-${Date.now()}`, body, author: 'You' }));
    state.selectedItem.comments_total += 1;
    syncSelectedItemIntoBoard(); renderTaskDrawer(); announce('Comment added');
  } catch (error) { showToast(`Could not add comment: ${error.message}`, 'error'); }
}

async function loadEarlierComments(control = null) {
  const selected = state.selectedItem;
  const cursor = selected?.comments_next_before;
  const projectId = state.activeProjectId;
  if (!selected || !cursor || !projectId) return;
  const itemId = selected.id;
  if (control) control.disabled = true;
  try {
    const payload = await request(projectPath(
      projectId,
      `/items/${encodeURIComponent(itemId)}/comments?limit=200&before=${encodeURIComponent(cursor)}`,
    ));
    if (state.activeProjectId !== projectId || state.selectedItem?.id !== itemId) return;
    const existingIds = new Set(state.selectedItem.comments.map((comment) => comment.id));
    const earlier = asArray(payload.comments)
      .map(normalizeComment)
      .filter((comment) => !existingIds.has(comment.id));
    state.selectedItem.comments = [...earlier, ...state.selectedItem.comments];
    state.selectedItem.comments_next_before = payload.next_before || null;
    state.selectedItem.comments_truncated = Boolean(payload.next_before);
    state.selectedItem.comments_total = Math.max(
      state.selectedItem.comments_total,
      state.selectedItem.comments.length,
    );
    syncSelectedItemIntoBoard(); renderTaskDrawer();
    announce(earlier.length ? `Loaded ${earlier.length} earlier comments` : 'All comments loaded');
  } catch (error) {
    showToast(`Could not load earlier comments: ${error.message}`, 'error');
    if (control?.isConnected) control.disabled = false;
  }
}

async function loadEarlierTaskActivity() {
  const context = captureSelectedTaskContext();
  const before = state.selectedItem?.activity_next_before;
  if (!context || !before || state.taskActivityLoading.has(context.key)) return;
  state.taskActivityLoading.add(context.key);
  renderTaskDrawer();
  try {
    const suffix = `/activity?work_item_id=${encodeURIComponent(context.itemId)}&limit=100&before=${encodeURIComponent(before)}`;
    const payload = await request(projectPath(context.projectId, suffix));
    if (!taskContextMatches(context)) return;
    const page = asArray(payload.activity || payload.items).map(normalizeActivity);
    state.selectedItem.activity = mergeUniqueRows(state.selectedItem.activity, page);
    state.selectedItem.activity_next_before = payload.next_before || null;
    syncSelectedItemIntoBoard();
    announce(page.length ? `Loaded ${page.length} earlier task events` : 'All task activity loaded');
  } catch (error) {
    showToast(`Could not load earlier task activity: ${error.message}`, 'error');
  } finally {
    state.taskActivityLoading.delete(context.key);
    if (taskContextMatches(context)) renderTaskDrawer();
  }
}

async function deleteComment(commentId) {
  if (!state.selectedItem || !canEditProject()) return;
  const context = captureSelectedTaskContext();
  if (!context) return;
  const comment = state.selectedItem.comments.find((entry) => entry.id === asId(commentId));
  if (!comment || !canDeleteComment(comment)) return;
  const accepted = await confirmAction('Delete this comment?', { confirmText: 'Delete', danger: true });
  if (!accepted) return;
  try {
    await request(projectPath(context.projectId, `/items/${encodeURIComponent(context.itemId)}/comments/${encodeURIComponent(commentId)}`), { method: 'DELETE' });
    if (!taskContextMatches(context)) return;
    state.selectedItem.comments = state.selectedItem.comments.filter((comment) => comment.id !== asId(commentId));
    state.selectedItem.comments_total = Math.max(0, state.selectedItem.comments_total - 1);
    syncSelectedItemIntoBoard(); renderTaskDrawer(); announce('Comment deleted');
  } catch (error) { showToast(`Could not delete comment: ${error.message}`, 'error'); }
}

function syncSelectedItemIntoBoard() {
  if (!state.selectedItem) return;
  state.selectedItem.checklist_count = state.selectedItem.checklist.length;
  state.selectedItem.checklist_done = state.selectedItem.checklist.filter((entry) => entry.done).length;
  state.selectedItem.attachment_count = state.selectedItem.attachments.length;
  state.items = state.items.map((item) => item.id === state.selectedItem.id ? { ...item, ...state.selectedItem } : item);
  renderTopSummary();
  renderNavigator();
  renderCurrentView();
}

async function setTaskArchived(archived) {
  if (!state.selectedItem || !canEditProject()) return;
  const context = captureSelectedTaskContext();
  if (!context) return;
  const item = state.selectedItem;
  if (archived) {
    const accepted = await confirmAction(`Archive ${item.key || item.title}?`, { confirmText: 'Archive', danger: true });
    if (!accepted) return;
  }
  try {
    const result = await request(projectPath(context.projectId, `/items/${encodeURIComponent(context.itemId)}/${archived ? 'archive' : 'restore'}`), {
      method: 'POST', body: { version: item.version },
    });
    if (!taskContextMatches(context)) return;
    const updated = mergeItemPayload(item, { ...(result.item || {}), archived });
    state.items = state.items.map((candidate) => candidate.id === item.id ? { ...candidate, ...updated } : candidate);
    if (archived) {
      await closeTaskDetail({ force: true });
      renderAll();
    } else {
      state.selectedItem = updated;
      state.drawerDraft = taskDraftFromItem(updated);
      renderAll();
    }
    announce(`${item.key || item.title} ${archived ? 'archived' : 'restored'}`);
  } catch (error) { showToast(`Could not ${archived ? 'archive' : 'restore'} task: ${error.message}`, 'error'); }
}

function queueId() {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return `queue-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function attachmentExtension(file) {
  const parts = String(file?.name || '').toLowerCase().split('.');
  return parts.length > 1 ? parts.pop() : '';
}

function isAcceptedAttachment(file) {
  if (!file) return false;
  if (String(file.type || '').startsWith('image/')) return true;
  return ACCEPTED_ATTACHMENT_EXTENSIONS.has(attachmentExtension(file));
}

function queueAttachmentFiles(files) {
  if (!state.selectedItem || !canEditProject()) return;
  const context = captureSelectedTaskContext();
  if (!context) return;
  if (state.submittingTasks.has(context.key)) {
    showToast('Wait for the current work submission to finish before adding files.', 'error');
    return;
  }
  const rejected = [];
  asArray(Array.from(files || [])).forEach((file) => {
    if (!isAcceptedAttachment(file)) { rejected.push(file.name || 'Unnamed file'); return; }
    let previewUrl = '';
    if (String(file.type || '').startsWith('image/') && typeof URL !== 'undefined' && URL.createObjectURL) {
      try { previewUrl = URL.createObjectURL(file); } catch (_) {}
    }
    attachmentQueues.add(context.projectId, context.itemId, {
      queueId: queueId(), file, previewUrl, kind: 'deliverable', description: '',
      progress: 0, status: 'pending', error: '', xhr: null,
    });
  });
  if (rejected.length) showToast(`Unsupported file${rejected.length === 1 ? '' : 's'}: ${rejected.join(', ')}`, 'error');
  renderTaskDrawer();
  announce(`${asArray(Array.from(files || [])).length - rejected.length} file${files?.length === 1 ? '' : 's'} queued`);
}

function findQueueEntry(queueIdValue) {
  if (!state.selectedItem) return null;
  return attachmentQueues.get(state.activeProjectId, state.selectedItem.id)
    .find((entry) => entry.queueId === queueIdValue) || null;
}

function uploadWithXhr(url, formData, entry) {
  const custom = dependencies.uploadAttachment;
  if (typeof custom === 'function') return custom(url, formData, entry, (progress) => {
    entry.progress = progress;
    updateQueueProgress(entry);
  });
  if (typeof XMLHttpRequest === 'undefined') {
    return request(url.replace(API_BASE, ''), { method: 'POST', body: formData });
  }
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    entry.xhr = xhr;
    xhr.open('POST', url, true);
    xhr.responseType = 'json';
    xhr.upload.addEventListener('progress', (event) => {
      if (!event.lengthComputable) return;
      entry.progress = Math.round((event.loaded / event.total) * 100);
      updateQueueProgress(entry);
    });
    xhr.addEventListener('load', () => {
      entry.xhr = null;
      const payload = xhr.response || (() => { try { return JSON.parse(xhr.responseText); } catch (_) { return {}; } })();
      if (xhr.status >= 200 && xhr.status < 300) resolve(payload || {});
      else reject(new Error(payload?.detail || payload?.error || `HTTP ${xhr.status}`));
    });
    xhr.addEventListener('error', () => { entry.xhr = null; reject(new Error('Network error')); });
    xhr.addEventListener('abort', () => { entry.xhr = null; reject(Object.assign(new Error('Upload cancelled'), { name: 'AbortError' })); });
    xhr.send(formData);
  });
}

function updateQueueProgress(entry) {
  const row = refs.drawer?.querySelector?.(`[data-queue-id="${entry.queueId}"]`);
  const progress = row?.querySelector?.('progress');
  if (progress) progress.value = entry.progress;
  const status = row?.querySelector?.('.projects-upload-row__copy small');
  if (status) status.textContent = `${formatBytes(entry.file.size)} · ${entry.progress}%`;
}

async function uploadQueueEntry(entry, {
  submissionNote = '', transitionStageId = '', context = captureSelectedTaskContext(), version = null,
} = {}) {
  if (!context || !entry || entry.status === 'uploading') return null;
  const scopedItem = taskContextMatches(context)
    ? state.selectedItem
    : state.activeProjectId === context.projectId
      ? state.items.find((item) => item.id === context.itemId)
      : null;
  const expectedVersion = version ?? scopedItem?.version ?? 0;
  entry.status = 'uploading'; entry.error = ''; entry.progress = 0;
  if (taskContextMatches(context)) renderTaskDrawer();
  const formData = new FormData();
  formData.append('file', entry.file, entry.file.name || 'attachment');
  formData.append('kind', ATTACHMENT_KINDS.has(entry.kind) ? entry.kind : 'reference');
  if (entry.description) formData.append('description', entry.description);
  if (submissionNote) formData.append('submission_note', submissionNote);
  if (transitionStageId) formData.append('transition_stage_id', transitionStageId);
  formData.append('version', String(expectedVersion));
  const url = `${API_BASE}${projectPath(context.projectId, `/items/${encodeURIComponent(context.itemId)}/attachments`)}`;
  try {
    const result = await uploadWithXhr(url, formData, entry);
    const attachment = normalizeAttachment(result.attachment || { name: entry.file.name, size: entry.file.size, kind: entry.kind, description: entry.description });
    attachmentQueues.remove(context.projectId, context.itemId, entry.queueId);
    if (taskContextMatches(context)) {
      const previousStageId = state.selectedItem.stage_id;
      if (!state.selectedItem.attachments.some((current) => current.id === attachment.id)) {
        state.selectedItem.attachments.push(attachment);
      }
      if (result.item) state.selectedItem = mergeItemPayload(state.selectedItem, result.item);
      syncDrawerDraftStage(previousStageId, state.selectedItem.stage_id);
      syncSelectedItemIntoBoard();
      renderTaskDrawer();
    } else if (state.activeProjectId === context.projectId) {
      state.items = state.items.map((item) => {
        if (item.id !== context.itemId) return item;
        const updated = result.item ? mergeItemPayload(item, result.item) : item;
        return { ...updated, attachment_count: Math.max(updated.attachment_count + 1, 1) };
      });
      renderTopSummary();
      renderNavigator();
      renderCurrentView();
    }
    announce(`${attachment.name} uploaded`);
    return attachment;
  } catch (error) {
    if (error?.name === 'AbortError') {
      entry.status = 'pending'; entry.error = ''; entry.progress = 0;
      announce(`${entry.file.name} upload cancelled`);
    } else {
      entry.status = 'error'; entry.error = error?.message || 'Upload failed';
      announce(`${entry.file.name} failed to upload`, 'assertive');
      showToast(`Upload failed: ${entry.error}`, 'error');
    }
    entry.xhr = null;
    if (taskContextMatches(context)) renderTaskDrawer();
    return null;
  }
}

async function submitWork(form) {
  if (!state.selectedItem || !canEditProject()) return;
  const context = captureSelectedTaskContext();
  if (!context) return;
  if (state.submittingTasks.has(context.key)) {
    announce('This task is already submitting work.', 'assertive');
    return;
  }
  const queue = [...attachmentQueues.get(context.projectId, context.itemId)];
  if (!queue.length) { showToast('Choose at least one file to submit.', 'error'); return; }
  const note = String(form.elements.namedItem('submission_note')?.value || '').trim();
  const transitionStageId = asId(form.elements.namedItem('transition_stage_id')?.value);
  const version = state.selectedItem.version;
  state.submittingTasks.add(context.key);
  renderTaskDrawer();
  let uploaded = 0;
  try {
    for (let index = 0; index < queue.length; index += 1) {
      if (!taskContextMatches(context) || !state.submittingTasks.has(context.key)) break;
      const isFinalUpload = index === queue.length - 1;
      const allEarlierUploadsSucceeded = uploaded === index;
      const attachment = await uploadQueueEntry(queue[index], {
        context,
        version,
        submissionNote: isFinalUpload && allEarlierUploadsSucceeded ? note : '',
        transitionStageId: isFinalUpload && allEarlierUploadsSucceeded ? transitionStageId : '',
      });
      if (attachment) uploaded += 1;
    }
  } finally {
    state.submittingTasks.delete(context.key);
    if (taskContextMatches(context)) renderTaskDrawer();
  }
  if (uploaded === queue.length) showToast(`Submitted ${uploaded} file${uploaded === 1 ? '' : 's'}`);
}

async function deleteAttachment(attachmentId) {
  if (!state.selectedItem || !canEditProject()) return;
  const context = captureSelectedTaskContext();
  if (!context) return;
  const attachment = state.selectedItem.attachments.find((entry) => entry.id === asId(attachmentId));
  if (!attachment || !canDeleteAttachment(attachment)) return;
  const accepted = await confirmAction(`Delete “${attachment.name}”?`, { confirmText: 'Delete', danger: true });
  if (!accepted) return;
  try {
    await request(projectPath(context.projectId, `/attachments/${encodeURIComponent(attachment.id)}`), { method: 'DELETE' });
    if (!taskContextMatches(context)) return;
    state.selectedItem.attachments = state.selectedItem.attachments.filter((entry) => entry.id !== attachment.id);
    syncSelectedItemIntoBoard(); renderTaskDrawer(); announce(`${attachment.name} deleted`);
  } catch (error) { showToast(`Could not delete attachment: ${error.message}`, 'error'); }
}

async function onWorkspaceClick(event) {
  const control = event.target.closest?.('[data-action]');
  if (!control || !refs.root?.contains(control)) return;
  const action = control.dataset.action;
  if (!action || action === 'move-task-select' || action === 'attachment-files' || action === 'toggle-checklist') return;
  switch (action) {
    case 'close-projects': await close(); break;
    case 'new-project': openProjectEditor(); break;
    case 'select-project': await selectProject(control.dataset.projectId); break;
    case 'edit-project': {
      const project = state.projects.find((candidate) => candidate.id === control.dataset.projectId);
      if (project) openProjectEditor(project);
      break;
    }
    case 'archive-project': await archiveProject(control.dataset.projectId); break;
    case 'restore-project': await restoreProject(control.dataset.projectId); break;
    case 'set-view': {
      const view = control.dataset.view;
      if (PROJECT_VIEWS.has(view)) {
        state.activeView = view;
        renderToolbar(); renderFilters(); renderCurrentView();
      }
      break;
    }
    case 'clear-filters':
      state.filters = { ...DEFAULT_FILTERS };
      renderFilters(); renderCurrentView();
      break;
    case 'retry-load':
      state.loadError = '';
      if (state.projects.length && state.activeProjectId) await selectProject(state.activeProjectId, { force: true });
      else await loadProjects({ preserveProject: true });
      break;
    case 'manage-members': openMembersDialog(); break;
    case 'manage-stages': openStageManager(); break;
    case 'show-archived-tasks': openArchivedTasksDialog(); break;
    case 'open-archived-task': {
      const taskId = control.dataset.taskId;
      closeDialog();
      await openTaskDetail(taskId, control);
      break;
    }
    case 'restore-archived-task': {
      const restored = await restoreArchivedTask(control.dataset.taskId, control);
      if (restored) {
        closeDialog();
        if (state.items.some((item) => item.archived)) openArchivedTasksDialog();
      }
      break;
    }
    case 'new-task': openTaskCreateDialog(); break;
    case 'quick-add':
      state.quickStageId = control.dataset.stageId;
      renderCurrentView();
      break;
    case 'cancel-quick-add':
      state.quickStageId = null;
      renderCurrentView();
      break;
    case 'open-task': await openTaskDetail(control.dataset.taskId, control); break;
    case 'drawer-close': await closeTaskDetail(); break;
    case 'archive-task': await setTaskArchived(true); break;
    case 'restore-task': await setTaskArchived(false); break;
    case 'delete-checklist': await deleteChecklistItem(control.dataset.checklistId); break;
    case 'delete-comment': await deleteComment(control.dataset.commentId); break;
    case 'load-earlier-comments': await loadEarlierComments(control); break;
    case 'load-earlier-activity': await loadActivity({ append: true }); break;
    case 'load-earlier-task-activity': await loadEarlierTaskActivity(); break;
    case 'close-dialog': closeDialog(); break;
    case 'save-stage': await saveStage(control.dataset.stageId, control.closest('[data-stage-id]')); break;
    case 'move-stage-up': await reorderStage(control.dataset.stageId, -1); break;
    case 'move-stage-down': await reorderStage(control.dataset.stageId, 1); break;
    case 'delete-stage': await deleteStage(control.dataset.stageId); break;
    case 'remove-member': await removeProjectMember(control.dataset.username); break;
    case 'transfer-project': await transferProjectOwnership(control.dataset.username); break;
    case 'remove-queued-file': {
      const context = captureSelectedTaskContext();
      if (!context || state.submittingTasks.has(context.key)) break;
      attachmentQueues.remove(state.activeProjectId, state.selectedItem?.id, control.dataset.queueId);
      renderTaskDrawer();
      break;
    }
    case 'retry-upload': {
      const context = captureSelectedTaskContext();
      if (!context || state.submittingTasks.has(context.key)) break;
      const entry = findQueueEntry(control.dataset.queueId);
      if (entry) await uploadQueueEntry(entry, { context });
      break;
    }
    case 'delete-attachment': await deleteAttachment(control.dataset.attachmentId); break;
    default: break;
  }
}

async function onWorkspaceSubmit(event) {
  const form = event.target.closest?.('form[data-form]');
  if (!form || !refs.root?.contains(form)) return;
  event.preventDefault();
  switch (form.dataset.form) {
    case 'project-editor': await submitProjectEditor(form); break;
    case 'task-create': {
      const values = form.elements;
      const itemType = String(values.namedItem('item_type')?.value || 'task');
      const parentId = asId(values.namedItem('parent_id')?.value);
      if (itemType === 'subtask' && !parentId) {
        showToast('Choose a parent before creating a subtask.', 'error');
        values.namedItem('parent_id')?.focus();
        break;
      }
      await createTask({
        title: String(values.namedItem('title')?.value || '').trim(),
        stage_id: asId(values.namedItem('stage_id')?.value),
        item_type: itemType,
        priority: String(values.namedItem('priority')?.value || 'medium'),
        ...(itemType === 'subtask' ? { parent_id: parentId } : {}),
      });
      break;
    }
    case 'quick-task':
      await createTask({ title: String(form.elements.namedItem('title')?.value || '').trim(), stage_id: form.dataset.stageId }, { openAfter: false });
      break;
    case 'task-details': await saveTaskDetails(form); break;
    case 'stage-create': await createStage(form); break;
    case 'member-add': await addProjectMember(form); break;
    case 'checklist': await addChecklistItem(form); break;
    case 'comment': await addComment(form); break;
    case 'submit-work': await submitWork(form); break;
    default: break;
  }
}

async function onWorkspaceChange(event) {
  const target = event.target;
  if (!(target instanceof Element)) return;
  if (target.dataset.filter) {
    state.filters[target.dataset.filter] = target.value;
    renderCurrentView();
    return;
  }
  switch (target.dataset.action) {
    case 'mobile-project': await selectProject(target.value); break;
    case 'mobile-stage': state.mobileStageId = target.value; renderCurrentView(); break;
    case 'task-create-type': {
      const form = target.closest('form[data-form="task-create"]');
      const parent = form?.elements?.namedItem('parent_id');
      const parentField = form?.querySelector('[data-task-create-parent]');
      const needsParent = target.value === 'subtask';
      if (parent) {
        parent.disabled = !needsParent;
        parent.required = needsParent;
        if (!needsParent) parent.value = '';
      }
      if (parentField) parentField.hidden = !needsParent;
      break;
    }
    case 'move-task-select': {
      target.disabled = true;
      await moveTask(target.dataset.taskId, target.value, null, { source: 'status' });
      break;
    }
    case 'toggle-checklist': await toggleChecklistItem(target.dataset.checklistId, target.checked, target); break;
    case 'attachment-files':
      queueAttachmentFiles(target.files);
      target.value = '';
      break;
    case 'queue-kind': {
      const entry = findQueueEntry(target.dataset.queueId);
      if (entry && ATTACHMENT_KINDS.has(target.value)) entry.kind = target.value;
      break;
    }
    case 'queue-note': {
      const entry = findQueueEntry(target.dataset.queueId);
      if (entry) entry.description = target.value;
      break;
    }
    case 'member-role': await updateProjectMember(target.dataset.username, target.value, target); break;
    default:
      if (target.closest('.projects-task-form')) updateDrawerDraftField(target);
      break;
  }
}

function onWorkspaceInput(event) {
  const target = event.target;
  if (!(target instanceof Element)) return;
  if (target.dataset.filter) {
    state.filters[target.dataset.filter] = target.value;
    renderCurrentView();
    return;
  }
  if (target.dataset.action === 'queue-note') {
    const entry = findQueueEntry(target.dataset.queueId);
    if (entry) entry.description = target.value;
    return;
  }
  if (target.closest('.projects-task-form')) updateDrawerDraftField(target);
}

function onDragStart(event) {
  const card = event.target.closest?.('.projects-card[data-task-id]');
  if (!card || isMobileViewport() || !canEditProject()) return;
  if (state.submittingTasks.has(taskScopeKey(state.activeProjectId, card.dataset.taskId))) {
    event.preventDefault();
    announce('Wait for the work submission to finish before moving this task.', 'assertive');
    return;
  }
  if (event.target.closest?.('button, input, select, textarea, a')) {
    event.preventDefault(); return;
  }
  state.drag = { taskId: card.dataset.taskId, fromStageId: card.dataset.stageId };
  card.classList.add('is-dragging');
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', card.dataset.taskId);
  }
}

function onDragOver(event) {
  const dropzone = event.target.closest?.('[data-attachment-dropzone]');
  if (dropzone && event.dataTransfer && [...(event.dataTransfer.types || [])].includes('Files')) {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    dropzone.classList.add('is-drag-over');
    return;
  }
  const stage = event.target.closest?.('[data-drop-stage]');
  if (!stage || !state.drag || isMobileViewport()) return;
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
  stage.classList.add('is-drag-over');
}

function onDragLeave(event) {
  const zone = event.target.closest?.('[data-drop-stage], [data-attachment-dropzone]');
  if (zone && !zone.contains(event.relatedTarget)) zone.classList.remove('is-drag-over');
}

async function onDrop(event) {
  const attachmentZone = event.target.closest?.('[data-attachment-dropzone]');
  if (attachmentZone && event.dataTransfer?.files?.length) {
    event.preventDefault(); event.stopPropagation();
    attachmentZone.classList.remove('is-drag-over');
    queueAttachmentFiles(event.dataTransfer.files);
    return;
  }
  const stageZone = event.target.closest?.('[data-drop-stage]');
  if (!stageZone || !state.drag) return;
  event.preventDefault();
  stageZone.classList.remove('is-drag-over');
  const stageId = stageZone.dataset.dropStage;
  const targetCard = event.target.closest?.('.projects-card[data-task-id]');
  const cards = [...stageZone.querySelectorAll('.projects-card[data-task-id]')]
    .filter((card) => card.dataset.taskId !== state.drag.taskId);
  const position = targetCard ? Math.max(0, cards.findIndex((card) => card.dataset.taskId === targetCard.dataset.taskId)) : cards.length;
  const taskId = state.drag.taskId;
  state.drag = null;
  await moveTask(taskId, stageId, position < 0 ? cards.length : position, { source: 'drag' });
}

function onDragEnd() {
  refs.root?.querySelectorAll?.('.is-dragging, .is-drag-over').forEach((element) => element.classList.remove('is-dragging', 'is-drag-over'));
  state.drag = null;
}

function onGlobalKeydown(event) {
  if (!state.open) return;
  if (event.key === 'Tab' && state.dialogClose) {
    trapDialogTab(event);
    return;
  }
  if (event.key !== 'Escape') return;
  event.preventDefault();
  event.stopPropagation();
  event.stopImmediatePropagation?.();
  if (state.dialogClose) { closeDialog(); return; }
  if (state.selectedItem) { closeTaskDetail(); return; }
  close();
}

function ensureStylesheet() {
  if (typeof document === 'undefined' || document.getElementById('projects-workspace-css') ||
      document.querySelector('link[href^="/static/projects.css"]')) return;
  const link = make('link', { id: 'projects-workspace-css', attrs: { rel: 'stylesheet', href: '/static/projects.css' } });
  document.head.appendChild(link);
}

export function init(apiBase = '', deps = {}) {
  API_BASE = String(apiBase || (typeof window !== 'undefined' ? window.location.origin : '')).replace(/\/$/, '');
  dependencies = { ...dependencies, ...(deps || {}) };
  state.initialized = true;
  ensureStylesheet();
  return projectsModule;
}

export async function open() {
  if (state.open) { focus(); return true; }
  if (!state.initialized) init();
  state.previousFocus = typeof document !== 'undefined' ? document.activeElement : null;
  state.open = true;
  state.loadError = '';
  buildWorkspace();
  refs.root.hidden = false;
  document.body.classList.add('projects-view');
  renderAll();
  focus();
  await loadProjects({ preserveProject: true });
  return true;
}

export async function close({ force = false } = {}) {
  if (!state.open) return true;
  if (!force && state.drawerDirty) {
    const discard = await confirmAction('Discard unsaved task changes and close Projects?', { confirmText: 'Close Projects', danger: true });
    if (!discard) return false;
  }
  saveProjectUiState();
  state.open = false;
  state.projectGate.invalidate(); state.boardGate.invalidate(); state.detailGate.invalidate(); state.activityGate.invalidate();
  ['projectController', 'boardController', 'detailController', 'activityController'].forEach((name) => {
    try { state[name]?.abort(); } catch (_) {}
    state[name] = null;
  });
  closeDialog();
  attachmentQueues.clearAll();
  state.movingTasks.clear();
  state.submittingTasks.clear();
  state.taskActivityLoading.clear();
  lifecycleCleanups.splice(0).forEach((cleanup) => { try { cleanup(); } catch (_) {} });
  refs.root?.remove();
  document.body.classList.remove('projects-view');
  const previous = state.previousFocus;
  state.selectedItem = null;
  state.drawerDirty = false;
  state.drawerDraft = null;
  state.activityLoading = false;
  refs = {};
  try {
    if (typeof dependencies.restoreSidebar === 'function') dependencies.restoreSidebar();
    else window._restoreSidebarIfRouteCollapsed?.();
  } catch (_) {}
  try { previous?.focus?.(); } catch (_) {}
  return true;
}

export async function toggle() {
  return state.open ? close() : open();
}

export function isOpen() {
  return state.open;
}

export function focus() {
  if (!state.open || !refs.root) return false;
  const target = refs.root.querySelector('.projects-card__title, .projects-view-tab, button:not([disabled])') || refs.root;
  try { target.focus({ preventScroll: true }); } catch (_) { try { target.focus(); } catch (_) {} }
  return true;
}

export const __test = Object.freeze({
  AttachmentQueueStore,
  createRequestGate,
  taskScopeKey,
  taskContextMatches,
  taskDraftFromItem,
  mergeUniqueRows,
  activityCursorFromEntries,
  canDeleteOwnedResource,
  normalizeProject,
  normalizeStage,
  normalizeItem,
  mergeItemPayload,
  normalizeBoard,
  normalizeAttachment,
  taskMatchesFilters,
  applyTaskFilters,
  optimisticMove,
  runMoveTransaction,
  visibleStagesForViewport,
  computeProjectHealth,
  isAcceptedAttachment,
  attachmentKindLabel,
});

const projectsModule = { init, open, close, toggle, isOpen, focus, moveTask, __test };
export default projectsModule;

if (typeof window !== 'undefined') window.projectsModule = projectsModule;
