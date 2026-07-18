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
  money: Object.freeze(['finance_record', 'transaction', 'asset']),
  learning: Object.freeze(['learning_record', 'career_item']),
  work: Object.freeze(['workspace', 'task', 'action', 'decision', 'automation']),
  home: Object.freeze(['home_record', 'place']),
  journal: Object.freeze(['journal_entry', 'period_review', 'note']),
  files: Object.freeze(['file', 'source']),
});
const FILTERS = new Set(Object.keys(FILTER_TYPES));
const FOCUSABLE_TYPES = new Set(['task', 'action', 'milestone']);
const FOCUSABLE_STATUSES = new Set(['active', 'open', 'in_progress']);

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
  const properties = object(raw.properties);
  return {
    id: text(raw.id),
    type: entityType(raw.entity_type ?? raw.type),
    title: text(raw.title ?? raw.name, 'Untitled entity').slice(0, 240),
    summary: text(raw.summary ?? raw.description).slice(0, 20_000),
    status: text(raw.status, 'active').toLowerCase(),
    properties,
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
    memoryKind: text(raw.memory_kind ?? properties.memory_kind).toLowerCase(),
    epistemicStatus: text(
      raw.epistemic_status ?? properties.epistemic_status,
    ).toLowerCase(),
    effectiveEpistemicStatus: text(
      raw.effective_epistemic_status ?? raw.epistemic_status
        ?? properties.epistemic_status,
    ).toLowerCase(),
    claimOrigin: text(raw.claim_origin ?? properties.claim_origin).toLowerCase(),
    staleReason: text(raw.stale_reason),
    citations: Array.isArray(raw.citations)
      ? raw.citations.slice(0, 30).map((row) => object(row))
      : (Array.isArray(properties.citations)
        ? properties.citations.slice(0, 30).map((row) => object(row)) : []),
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

function knowledgeApiUrl() {
  return `${String(API_BASE || '').replace(/\/$/, '')}/api/life/knowledge/records?limit=${MAX_ENTITIES}`;
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
  const requestPage = async (url) => {
    const response = await fetch(url, {
      method: 'GET', credentials: 'same-origin', headers: { Accept: 'application/json' }, signal,
    });
    const raw = await response.text();
    let payload = null;
    if (raw) {
      try { payload = JSON.parse(raw); } catch (_) { payload = { detail: raw }; }
    }
    if (!response.ok) throw new Error(errorMessage(payload, response.status));
    return unwrapLifePage(payload);
  };
  const [life, knowledge] = await Promise.all([
    requestPage(apiUrl()), requestPage(knowledgeApiUrl()),
  ]);
  const combined = [...life.items, ...knowledge.items]
    .sort((left, right) => (parsedTime(right.updatedAt) ?? 0) - (parsedTime(left.updatedAt) ?? 0));
  return {
    items: combined.slice(0, MAX_ENTITIES),
    count: life.count + knowledge.count,
    truncated: life.truncated || knowledge.truncated || combined.length > MAX_ENTITIES,
  };
}

export async function getLifeEntity(entityId, { signal } = {}) {
  const id = text(entityId);
  if (!id) throw new Error('Life entity id is required.');
  const response = await fetch(
    `${String(API_BASE || '').replace(/\/$/, '')}/api/life/entities/${encodeURIComponent(id)}`,
    {
      method: 'GET', credentials: 'same-origin', headers: { Accept: 'application/json' }, signal,
    },
  );
  const raw = await response.text();
  let payload = null;
  if (raw) {
    try { payload = JSON.parse(raw); } catch (_) { payload = { detail: raw }; }
  }
  if (!response.ok) throw new Error(errorMessage(payload, response.status));
  return normalizeLifeEntity(object(payload).entity);
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

function boundedStrings(value, limit = 30) {
  if (!Array.isArray(value)) return [];
  return value.slice(0, limit).map((entry) => text(entry)).filter(Boolean);
}

function parsedTime(value) {
  if (!value) return null;
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : null;
}

export function normalizeDecisionProperties(item, now = new Date()) {
  const entity = object(item);
  const properties = object(entity.properties);
  if (entityType(entity.type ?? entity.entity_type) !== 'decision'
      || number(properties.decision_schema_version, 0) !== 1) return null;
  const options = (Array.isArray(properties.options) ? properties.options : [])
    .slice(0, 20)
    .map((entry) => {
      const option = object(entry);
      return { id: text(option.id), label: text(option.label), details: text(option.details) };
    })
    .filter((entry) => entry.id && entry.label);
  const chosenId = text(properties.chosen_option);
  const chosen = options.find((entry) => entry.id === chosenId);
  if (!chosen) return null;
  const assumptions = (Array.isArray(properties.assumptions) ? properties.assumptions : [])
    .slice(0, 40)
    .map((entry) => {
      const assumption = object(entry);
      return {
        id: text(assumption.id),
        text: text(assumption.text),
        status: text(assumption.status, 'unverified').toLowerCase(),
        reviewAt: text(assumption.review_at),
        recordedAt: text(assumption.recorded_at),
        lastReviewedAt: text(assumption.last_reviewed_at),
      };
    })
    .filter((entry) => entry.id && entry.text);
  const nowMs = now instanceof Date ? now.getTime() : new Date(now).getTime();
  const safeNow = Number.isFinite(nowMs) ? nowMs : Date.now();
  const staleCutoff = safeNow - (30 * 24 * 60 * 60 * 1000);
  const occurredAt = parsedTime(entity.occurredAt ?? entity.occurred_at);
  const activeAssumptions = assumptions.filter(
    (entry) => entry.status === 'unverified' || entry.status === 'valid',
  );
  const dueAssumptions = activeAssumptions.filter((entry) => {
    const dueAt = parsedTime(entry.reviewAt);
    return dueAt !== null && dueAt <= safeNow;
  });
  const staleAssumptions = activeAssumptions.filter((entry) => {
    const reviewedAt = parsedTime(entry.lastReviewedAt);
    const recordedAt = parsedTime(entry.recordedAt);
    const freshness = reviewedAt ?? recordedAt ?? occurredAt;
    return !entry.reviewAt && freshness !== null && freshness <= staleCutoff;
  });
  const decisionReviewAt = parsedTime(entity.reviewAt ?? entity.review_at);
  const reviewDue = (decisionReviewAt !== null && decisionReviewAt <= safeNow)
    || dueAssumptions.length > 0;
  const outcome = object(properties.outcome);
  return {
    chosenId,
    chosenLabel: chosen.label,
    reasons: boundedStrings(properties.reasons),
    risks: boundedStrings(properties.risks),
    people: boundedStrings(properties.people),
    evidenceCount: Array.isArray(properties.evidence) ? Math.min(40, properties.evidence.length) : 0,
    assumptionCount: assumptions.length,
    dueAssumptionCount: dueAssumptions.length,
    staleAssumptionCount: staleAssumptions.length,
    outcomeStatus: text(outcome.status, 'pending').toLowerCase(),
    outcomeSummary: text(outcome.summary),
    reviewLabel: reviewDue
      ? 'Review due'
      : (staleAssumptions.length ? 'Assumptions stale' : 'Review on track'),
    reviewDue,
  };
}

export function normalizeFinanceProperties(item) {
  const entity = object(item);
  const properties = object(entity.properties);
  if (entityType(entity.type ?? entity.entity_type) !== 'finance_record'
      || number(properties.finance_schema_version, 0) !== 1) return null;
  const details = object(properties.details);
  const source = object(properties.source);
  const recordType = entityType(properties.record_type);
  const scope = entityType(properties.scope);
  if (!recordType || !scope) return null;
  const amount = properties.amount === null || properties.amount === undefined
    ? '' : text(properties.amount).slice(0, 80);
  const currency = text(properties.currency).toUpperCase().slice(0, 3);
  const party = text(
    details.merchant ?? details.counterparty ?? details.provider ?? details.payee
      ?? details.payer ?? details.institution ?? details.lender,
  ).slice(0, 240);
  return {
    recordType,
    scope,
    amount,
    currency,
    party,
    sourceLabel: text(source.label ?? source.kind).slice(0, 240),
    effectiveAt: text(properties.effective_at),
  };
}

export function normalizeHabitProperties(item) {
  const entity = object(item);
  const properties = object(entity.properties);
  if (entityType(entity.type ?? entity.entity_type) !== 'habit'
      || number(properties.habit_schema_version, 0) !== 1) return null;
  const schedule = object(properties.schedule);
  const minimum = object(properties.minimum_viable);
  const recovery = object(properties.recovery_rules);
  const routineType = entityType(properties.routine_type);
  const cadence = entityType(schedule.cadence);
  if (!routineType || !cadence) return null;
  const checklist = (Array.isArray(properties.checklist) ? properties.checklist : [])
    .slice(0, 50)
    .map((entry) => object(entry))
    .filter((entry) => text(entry.id) && text(entry.label));
  return {
    routineType,
    cadence,
    timeOfDay: text(schedule.time_of_day).slice(0, 5),
    timezone: text(schedule.timezone).slice(0, 100),
    durationMinutes: Math.max(1, Math.min(1440, Math.trunc(number(properties.duration_minutes, 1)))),
    checklistCount: checklist.length,
    requiredCount: checklist.filter((entry) => Boolean(entry.required)).length,
    minimumMinutes: Math.max(1, Math.min(1440, Math.trunc(number(minimum.duration_minutes, 1)))),
    recoveryStrategy: entityType(recovery.strategy),
  };
}

export function normalizeRelationshipProperties(item) {
  const entity = object(item);
  const properties = object(entity.properties);
  if (entityType(entity.type ?? entity.entity_type) !== 'person'
      || number(properties.relationship_schema_version, 0) !== 1
      || text(properties.relationship_record_kind) !== 'profile') return null;
  const origin = object(properties.contact_origin);
  const care = object(properties.care_plan);
  const dates = (Array.isArray(properties.important_dates) ? properties.important_dates : [])
    .slice(0, 30).map((entry) => object(entry)).filter((entry) => text(entry.label));
  const preferences = (Array.isArray(properties.preferences) ? properties.preferences : [])
    .slice(0, 50).map((entry) => object(entry)).filter((entry) => text(entry.key));
  return {
    subjectKind: entityType(properties.subject_kind),
    relationshipType: entityType(properties.relationship_type),
    originLabel: text(origin.label ?? origin.kind).slice(0, 240),
    importantDateCount: dates.length,
    preferenceCount: preferences.length,
    careDueAt: text(care.next_due_at),
    careIntervalDays: Math.max(0, Math.min(3650, Math.trunc(number(care.interval_days, 0)))),
  };
}

export function normalizeJournalProperties(item) {
  const entity = object(item);
  const properties = object(entity.properties);
  if (entityType(entity.type ?? entity.entity_type) !== 'journal_entry'
      || number(properties.journal_schema_version, 0) !== 1) return null;
  const mood = object(properties.mood);
  const promises = (Array.isArray(properties.promises) ? properties.promises : [])
    .slice(0, 50).map((entry) => object(entry));
  return {
    entryDate: text(properties.entry_date),
    moodLabel: text(mood.label).slice(0, 100),
    moodScore: mood.score === null || mood.score === undefined
      ? null : Math.max(0, Math.min(10, Math.trunc(number(mood.score, 0)))),
    energy: mood.energy === null || mood.energy === undefined
      ? null : Math.max(0, Math.min(10, Math.trunc(number(mood.energy, 0)))),
    winCount: boundedStrings(properties.wins, 50).length,
    lessonCount: boundedStrings(properties.lessons, 50).length,
    openPromiseCount: promises.filter((entry) => text(entry.status) === 'open').length,
    nextChangeCount: boundedStrings(properties.next_changes, 50).length,
  };
}

export function normalizeHomeProperties(item) {
  const entity = object(item);
  const properties = object(entity.properties);
  if (entityType(entity.type ?? entity.entity_type) !== 'home_record'
      || number(properties.home_schema_version, 0) !== 1) return null;
  const source = object(properties.source);
  const details = object(properties.details);
  const recordType = entityType(properties.record_type);
  if (!recordType) return null;
  return {
    recordType,
    effectiveAt: text(properties.effective_at),
    expiresAt: text(properties.expires_at),
    dueAt: text(properties.due_at),
    sourceLabel: text(source.label ?? source.kind).slice(0, 240),
    recordStatus: entityType(details.record_status || 'active'),
    referenceCount: [
      ...boundedStrings(object(properties.references).file_entity_ids, 50),
      ...boundedStrings(object(properties.references).document_ids, 50),
      ...boundedStrings(object(properties.references).entity_ids, 50),
    ].length,
  };
}

export function normalizeTravelProperties(item) {
  const entity = object(item);
  const properties = object(entity.properties);
  if (entityType(entity.type ?? entity.entity_type) !== 'trip'
      || number(properties.travel_schema_version, 0) !== 1) return null;
  const details = object(properties.details);
  const recordKind = entityType(properties.record_kind);
  if (!recordKind) return null;
  const descriptor = text(
    details.destination ?? details.name ?? details.activity ?? details.topic
      ?? details.item ?? details.provider ?? details.mode ?? details.document_kind
      ?? details.label,
  ).slice(0, 240);
  return {
    recordKind,
    tripId: text(properties.trip_id),
    startsAt: text(properties.starts_at),
    endsAt: text(properties.ends_at),
    offlineAvailable: Boolean(properties.offline_available),
    descriptor,
    referenceCount: [
      ...boundedStrings(properties.related_entity_ids, 50),
      ...boundedStrings(properties.source_ids, 50),
      ...boundedStrings(properties.calendar_event_ids, 50),
    ].length,
  };
}

export function normalizeLearningCareerProperties(item) {
  const entity = object(item);
  const type = entityType(entity.type ?? entity.entity_type);
  const properties = object(entity.properties);
  if (!['learning_record', 'career_item'].includes(type)
      || number(properties.learning_career_schema_version, 0) !== 1) return null;
  const domain = entityType(properties.domain);
  const recordKind = entityType(properties.record_kind);
  if (!['learning', 'career'].includes(domain) || !recordKind) return null;
  const weekly = object(properties.weekly_action);
  const details = object(properties.details);
  const descriptor = text(
    details.provider ?? details.organization ?? details.program ?? details.category
      ?? details.topic ?? details.metric ?? details.method ?? details.application_state
      ?? details.current_level,
  ).slice(0, 240);
  return {
    domain,
    recordKind,
    descriptor,
    sourceCount: Array.isArray(properties.source_links)
      ? Math.min(50, properties.source_links.length) : 0,
    linkCount: Array.isArray(properties.entity_links)
      ? Math.min(50, properties.entity_links.length) : 0,
    weekStart: text(weekly.week_start),
    weeklyStatus: entityType(weekly.status),
    weeklyMinutes: Math.max(0, Math.min(10080,
      Math.trunc(number(weekly.estimated_minutes, 0)))),
  };
}

export function normalizeWorkBusinessProperties(item) {
  const entity = object(item);
  const properties = object(entity.properties);
  const workspaceSchema = number(
    properties.work_business_workspace_schema_version, 0,
  ) === 1;
  const recordSchema = number(properties.work_business_record_schema_version, 0) === 1;
  if (!workspaceSchema && !recordSchema) return null;
  const details = object(properties.details);
  const workspaceKind = entityType(properties.workspace_kind);
  if (!['work', 'business'].includes(workspaceKind)) return null;
  const recordKind = recordSchema ? entityType(properties.record_kind) : 'workspace';
  const descriptor = text(
    details.owner ?? details.organization ?? details.customer ?? details.metric
      ?? details.stage ?? details.category ?? details.process ?? details.channel,
  ).slice(0, 240);
  return {
    isWorkspace: workspaceSchema,
    workspaceKind,
    recordKind,
    workspaceId: text(properties.workspace_id),
    purpose: workspaceSchema ? text(properties.purpose).slice(0, 600) : '',
    descriptor,
    sourceCount: Array.isArray(properties.source_links)
      ? Math.min(50, properties.source_links.length) : 0,
  };
}

export function normalizeTaskProperties(item) {
  const entity = object(item);
  const properties = object(entity.properties);
  if (entityType(entity.type ?? entity.entity_type) !== 'task'
      || number(properties.task_schema_version, 0) !== 1) return null;
  return {
    definitionOfDone: text(properties.definition_of_done).slice(0, 2_000),
    priority: entityType(properties.priority),
    effortMinutes: Math.max(1, Math.min(10080,
      Math.trunc(number(properties.effort_minutes, 1)))),
    energy: entityType(properties.energy),
    contexts: boundedStrings(properties.contexts, 20),
    nextAction: text(properties.next_action).slice(0, 1_000),
    waitingOn: text(properties.waiting_on).slice(0, 1_000),
    referenceCount: [
      text(properties.project_id),
      ...boundedStrings(properties.people_ids, 50),
      ...boundedStrings(properties.dependency_ids, 50),
      ...boundedStrings(properties.document_ids, 50),
    ].filter(Boolean).length,
    evidenceCount: Array.isArray(properties.completion_evidence)
      ? Math.min(50, properties.completion_evidence.length) : 0,
  };
}

export function normalizeAutomationProperties(item) {
  const entity = object(item);
  const properties = object(entity.properties);
  if (entityType(entity.type ?? entity.entity_type) !== 'automation'
      || number(properties.schema_version, 0) !== 1
      || text(properties.record_kind) !== 'automation_definition') return null;
  const trigger = object(properties.trigger);
  const actions = (Array.isArray(properties.actions) ? properties.actions : [])
    .slice(0, 32).map((entry) => object(entry));
  const actionTypes = [
    ...new Set(actions.map((entry) => entityType(entry.type)).filter(Boolean)),
  ];
  return {
    enabled: Boolean(properties.enabled),
    triggerType: entityType(trigger.type),
    actionCount: actions.length,
    actionTypes,
    externalReviewCount: actions.filter((entry) => Boolean(entry.external)).length,
    sourceCount: boundedStrings(object(entity.provenance).source_ids, 64).length,
  };
}

export function normalizePersonalKnowledgeProperties(item) {
  const entity = object(item);
  const properties = object(entity.properties);
  if (entityType(entity.type ?? entity.entity_type) !== 'source'
      || number(properties.personal_knowledge_schema_version, 0) !== 1
      || text(properties.authority) !== 'personal_knowledge_v1') return null;
  const memoryKind = entityType(entity.memoryKind ?? properties.memory_kind);
  const epistemicStatus = entityType(
    entity.epistemicStatus ?? properties.epistemic_status,
  );
  const effectiveStatus = entityType(
    entity.effectiveEpistemicStatus ?? epistemicStatus,
  );
  const claimOrigin = entityType(entity.claimOrigin ?? properties.claim_origin);
  if (!memoryKind || !epistemicStatus || !claimOrigin) return null;
  const citations = Array.isArray(entity.citations)
    ? entity.citations.slice(0, 30) : [];
  return {
    memoryKind,
    epistemicStatus,
    effectiveStatus,
    claimOrigin,
    citationCount: citations.length,
    availableCitationCount: citations.filter((row) => row.available !== false).length,
    contradictionCount: citations.filter((row) => text(row.relation) === 'contradicts').length,
    tagCount: boundedStrings(properties.tags, 40).length,
    staleReason: text(entity.staleReason),
  };
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
  return state.items.filter((item) => matchesFilter(item, state.filter));
}

function matchesFilter(item, filter) {
  const types = FILTER_TYPES[filter] || [];
  if (filter === 'work' && normalizeWorkBusinessProperties(item)) return true;
  return types.includes(item.type);
}

function typeCounts() {
  return Object.fromEntries(Object.entries(FILTER_TYPES).map(([filter, types]) => [
    filter,
    filter === 'all'
      ? state.items.length
      : state.items.filter((item) => matchesFilter(item, filter)).length,
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

function decisionContext(item) {
  const decision = normalizeDecisionProperties(item);
  if (!decision) return null;
  const rows = [
    ['Chosen', decision.chosenLabel],
    ['Review', decision.reviewLabel],
  ];
  if (decision.outcomeStatus !== 'pending' || decision.outcomeSummary) {
    rows.push([
      'Outcome',
      decision.outcomeSummary
        ? `${humanize(decision.outcomeStatus)} — ${decision.outcomeSummary}`
        : humanize(decision.outcomeStatus),
    ]);
  }
  if (decision.reasons.length) rows.push(['Reasons', decision.reasons.slice(0, 2).join(' · ')]);
  if (decision.risks.length) rows.push(['Risks', decision.risks.slice(0, 2).join(' · ')]);
  if (decision.people.length) rows.push(['People', decision.people.slice(0, 4).join(', ')]);
  const assumptionSummary = [
    `${decision.assumptionCount} recorded`,
    decision.dueAssumptionCount ? `${decision.dueAssumptionCount} due` : '',
    decision.staleAssumptionCount ? `${decision.staleAssumptionCount} stale` : '',
  ].filter(Boolean).join(' · ');
  if (decision.assumptionCount) rows.push(['Assumptions', assumptionSummary]);
  if (decision.evidenceCount) rows.push(['Evidence', `${decision.evidenceCount} linked item${decision.evidenceCount === 1 ? '' : 's'}`]);
  return make('section', {
    className: `life-decision-context${decision.reviewDue || decision.staleAssumptionCount ? ' is-review-needed' : ''}`,
    attrs: { 'aria-label': 'Decision context' },
  }, [
    make('h4', { text: 'Decision record' }),
    make('dl', {}, rows.flatMap(([label, value]) => [
      make('div', { className: 'life-decision-row' }, [
        make('dt', { text: label }), make('dd', { text: value }),
      ]),
    ])),
  ]);
}

function financeContext(item) {
  const finance = normalizeFinanceProperties(item);
  if (!finance) return null;
  const effective = formatDate(finance.effectiveAt);
  const rows = [
    ['Type', humanize(finance.recordType)],
    ['Scope', humanize(finance.scope)],
    ['Amount', finance.amount
      ? [finance.currency, finance.amount].filter(Boolean).join(' ')
      : 'Non-monetary record'],
  ];
  if (finance.party) rows.push(['Party', finance.party]);
  if (finance.sourceLabel) rows.push(['Source', finance.sourceLabel]);
  if (effective) rows.push(['Effective', effective.label]);
  rows.push(['Policy', 'Record analysis only']);
  return make('section', {
    className: 'life-decision-context life-finance-context',
    attrs: { 'aria-label': 'Finance record context' },
  }, [
    make('h4', { text: 'Finance record' }),
    make('dl', {}, rows.flatMap(([label, value]) => [
      make('div', { className: 'life-decision-row life-finance-row' }, [
        make('dt', { text: label }), make('dd', { text: value }),
      ]),
    ])),
  ]);
}

function habitContext(item) {
  const habit = normalizeHabitProperties(item);
  if (!habit) return null;
  const schedule = [humanize(habit.cadence), habit.timeOfDay, habit.timezone]
    .filter(Boolean).join(' · ');
  const rows = [
    ['Routine', humanize(habit.routineType)],
    ['Schedule', schedule],
    ['Duration', `${habit.durationMinutes} min`],
    ['Checklist', `${habit.requiredCount} required · ${habit.checklistCount} total`],
    ['Minimum', `${habit.minimumMinutes} min`],
  ];
  if (habit.recoveryStrategy) rows.push(['Recovery', humanize(habit.recoveryStrategy)]);
  rows.push(['Policy', 'Tracked evidence only']);
  return make('section', {
    className: 'life-decision-context life-habit-context',
    attrs: { 'aria-label': 'Habit routine context' },
  }, [
    make('h4', { text: 'Habit routine' }),
    make('dl', {}, rows.flatMap(([label, value]) => [
      make('div', { className: 'life-decision-row life-habit-row' }, [
        make('dt', { text: label }), make('dd', { text: value }),
      ]),
    ])),
  ]);
}

function relationshipContext(item) {
  const relationship = normalizeRelationshipProperties(item);
  if (!relationship) return null;
  const careDue = formatDate(relationship.careDueAt);
  const rows = [
    ['Profile', humanize(relationship.subjectKind)],
    ['Relationship', humanize(relationship.relationshipType)],
  ];
  if (relationship.originLabel) rows.push(['Origin', relationship.originLabel]);
  if (relationship.importantDateCount) rows.push([
    'Important dates', String(relationship.importantDateCount),
  ]);
  if (relationship.preferenceCount) rows.push([
    'Preferences', String(relationship.preferenceCount),
  ]);
  if (careDue) rows.push([
    'Care review', `${careDue.label}${relationship.careIntervalDays
      ? ` · every ${relationship.careIntervalDays} days` : ''}`,
  ]);
  rows.push(['Messaging', 'Separate confirmation required']);
  return make('section', {
    className: 'life-decision-context life-relationship-context',
    attrs: { 'aria-label': 'Relationship profile context' },
  }, [
    make('h4', { text: 'Relationship profile' }),
    make('dl', {}, rows.flatMap(([label, value]) => [
      make('div', { className: 'life-decision-row life-relationship-row' }, [
        make('dt', { text: label }), make('dd', { text: value }),
      ]),
    ])),
  ]);
}

function journalContext(item) {
  const journal = normalizeJournalProperties(item);
  if (!journal) return null;
  const entryDate = formatDate(journal.entryDate);
  const rows = [];
  if (entryDate) rows.push(['Entry', entryDate.label]);
  if (journal.moodLabel || journal.moodScore !== null) rows.push([
    'Mood', [journal.moodLabel, journal.moodScore === null ? '' : `${journal.moodScore}/10`]
      .filter(Boolean).join(' · '),
  ]);
  if (journal.energy !== null) rows.push(['Energy', `${journal.energy}/10`]);
  rows.push(['Captured', `${journal.winCount} wins · ${journal.lessonCount} lessons`]);
  if (journal.openPromiseCount) rows.push(['Open promises', String(journal.openPromiseCount)]);
  if (journal.nextChangeCount) rows.push(['Next changes', String(journal.nextChangeCount)]);
  rows.push(['Review', 'Explicit evidence only']);
  return make('section', {
    className: 'life-decision-context life-journal-context',
    attrs: { 'aria-label': 'Journal entry context' },
  }, [
    make('h4', { text: 'Journal entry' }),
    make('dl', {}, rows.flatMap(([label, value]) => [
      make('div', { className: 'life-decision-row life-journal-row' }, [
        make('dt', { text: label }), make('dd', { text: value }),
      ]),
    ])),
  ]);
}

function homeContext(item) {
  const home = normalizeHomeProperties(item);
  if (!home) return null;
  const effective = formatDate(home.effectiveAt);
  const expiry = formatDate(home.expiresAt);
  const due = formatDate(home.dueAt);
  const rows = [
    ['Record', humanize(home.recordType)],
    ['Status', humanize(home.recordStatus)],
  ];
  if (effective) rows.push(['Effective', effective.label]);
  if (expiry) rows.push(['Expires', expiry.label]);
  if (due) rows.push(['Due', due.label]);
  if (home.sourceLabel) rows.push(['Source', home.sourceLabel]);
  if (home.referenceCount) rows.push(['Linked references', String(home.referenceCount)]);
  rows.push(['Policy', 'Record and alert only']);
  return make('section', {
    className: 'life-decision-context life-home-context',
    attrs: { 'aria-label': 'Home administration context' },
  }, [
    make('h4', { text: 'Home & administration' }),
    make('dl', {}, rows.flatMap(([label, value]) => [
      make('div', { className: 'life-decision-row life-home-row' }, [
        make('dt', { text: label }), make('dd', { text: value }),
      ]),
    ])),
  ]);
}

function travelContext(item) {
  const travel = normalizeTravelProperties(item);
  if (!travel) return null;
  const starts = formatDate(travel.startsAt);
  const ends = formatDate(travel.endsAt);
  const rows = [
    ['Record', humanize(travel.recordKind)],
    ['Offline', travel.offlineAvailable ? 'Available' : 'Online reference only'],
  ];
  if (travel.descriptor) rows.push(['Context', travel.descriptor]);
  if (starts) rows.push(['Starts', starts.label]);
  if (ends) rows.push(['Ends', ends.label]);
  if (travel.tripId) rows.push(['Trip', 'Linked to owner-scoped trip']);
  if (travel.referenceCount) rows.push(['Linked evidence', String(travel.referenceCount)]);
  rows.push(['Policy', 'Record only · no booking or purchase']);
  return make('section', {
    className: 'life-decision-context life-travel-context',
    attrs: { 'aria-label': 'Travel record context' },
  }, [
    make('h4', { text: 'Travel record' }),
    make('dl', {}, rows.flatMap(([label, value]) => [
      make('div', { className: 'life-decision-row life-travel-row' }, [
        make('dt', { text: label }), make('dd', { text: value }),
      ]),
    ])),
  ]);
}

function learningCareerContext(item) {
  const learning = normalizeLearningCareerProperties(item);
  if (!learning) return null;
  const week = formatDate(learning.weekStart);
  const rows = [
    ['Domain', humanize(learning.domain)],
    ['Record', humanize(learning.recordKind)],
  ];
  if (learning.descriptor) rows.push(['Context', learning.descriptor]);
  if (learning.sourceCount) rows.push(['Sources', String(learning.sourceCount)]);
  if (learning.linkCount) rows.push(['Graph links', String(learning.linkCount)]);
  if (week) rows.push([
    'Weekly action',
    [week.label, learning.weeklyStatus ? humanize(learning.weeklyStatus) : '',
      learning.weeklyMinutes ? `${learning.weeklyMinutes} min` : '']
      .filter(Boolean).join(' · '),
  ]);
  rows.push(['Policy', 'Record and plan only · no submission']);
  return make('section', {
    className: 'life-decision-context life-learning-context',
    attrs: { 'aria-label': 'Learning and career context' },
  }, [
    make('h4', { text: 'Learning & career' }),
    make('dl', {}, rows.flatMap(([label, value]) => [
      make('div', { className: 'life-decision-row life-learning-row' }, [
        make('dt', { text: label }), make('dd', { text: value }),
      ]),
    ])),
  ]);
}

function workBusinessContext(item) {
  const workspace = normalizeWorkBusinessProperties(item);
  if (!workspace) return null;
  const rows = [
    ['Workspace', humanize(workspace.workspaceKind)],
    ['Record', humanize(workspace.recordKind)],
  ];
  if (workspace.purpose) rows.push(['Purpose', workspace.purpose]);
  if (workspace.descriptor) rows.push(['Context', workspace.descriptor]);
  if (workspace.workspaceId) rows.push(['Isolation', 'Exact owner-scoped workspace']);
  if (workspace.sourceCount) rows.push(['Sources', String(workspace.sourceCount)]);
  rows.push(['Policy', 'Record only · no outreach, submission, or payment']);
  return make('section', {
    className: 'life-decision-context life-work-context',
    attrs: { 'aria-label': 'Work and business workspace context' },
  }, [
    make('h4', { text: 'Work & business' }),
    make('dl', {}, rows.flatMap(([label, value]) => [
      make('div', { className: 'life-decision-row life-work-row' }, [
        make('dt', { text: label }), make('dd', { text: value }),
      ]),
    ])),
  ]);
}

function taskContext(item) {
  const task = normalizeTaskProperties(item);
  if (!task) return null;
  const rows = [
    ['Done means', task.definitionOfDone],
    ['Plan', `${humanize(task.priority)} · ${task.effortMinutes} min · ${humanize(task.energy)} energy`],
  ];
  if (task.contexts.length) rows.push(['Context', task.contexts.slice(0, 5).join(', ')]);
  if (task.nextAction) rows.push(['Next action', task.nextAction]);
  if (task.waitingOn) rows.push(['Waiting on', task.waitingOn]);
  if (task.referenceCount) rows.push(['Linked context', String(task.referenceCount)]);
  if (task.evidenceCount) rows.push(['Completion evidence', String(task.evidenceCount)]);
  rows.push(['Authority', 'Human commitment · not Scheduled Tasks']);
  return make('section', {
    className: 'life-decision-context life-task-context',
    attrs: { 'aria-label': 'Structured task context' },
  }, [
    make('h4', { text: 'Structured task' }),
    make('dl', {}, rows.flatMap(([label, value]) => [
      make('div', { className: 'life-decision-row life-task-row' }, [
        make('dt', { text: label }), make('dd', { text: value }),
      ]),
    ])),
  ]);
}

function automationContext(item) {
  const automation = normalizeAutomationProperties(item);
  if (!automation) return null;
  const rows = [
    ['State', automation.enabled ? 'Enabled' : 'Paused'],
    ['Trigger', humanize(automation.triggerType)],
    ['Typed actions', `${automation.actionCount}${automation.actionTypes.length
      ? ` · ${automation.actionTypes.map(humanize).join(', ')}` : ''}`],
  ];
  if (automation.externalReviewCount) rows.push([
    'Human review', `${automation.externalReviewCount} external action(s)`,
  ]);
  if (automation.sourceCount) rows.push(['Sources', String(automation.sourceCount)]);
  rows.push(['Safety', 'Prepared only · no execution or send']);
  return make('section', {
    className: 'life-decision-context life-automation-context',
    attrs: { 'aria-label': 'Automation definition context' },
  }, [
    make('h4', { text: 'Automation' }),
    make('dl', {}, rows.flatMap(([label, value]) => [
      make('div', { className: 'life-decision-row life-automation-row' }, [
        make('dt', { text: label }), make('dd', { text: value }),
      ]),
    ])),
  ]);
}

function personalKnowledgeContext(item) {
  const memory = normalizePersonalKnowledgeProperties(item);
  if (!memory) return null;
  const rows = [
    ['Memory', humanize(memory.memoryKind)],
    ['Epistemic status', humanize(memory.effectiveStatus)],
    ['Claim origin', humanize(memory.claimOrigin)],
    ['Citations', `${memory.availableCitationCount} available · ${memory.citationCount} recorded`],
  ];
  if (memory.contradictionCount) {
    rows.push(['Contradictions', `${memory.contradictionCount} explicit`]);
  }
  if (memory.tagCount) rows.push(['Tags', String(memory.tagCount)]);
  if (memory.staleReason) rows.push(['Staleness', humanize(memory.staleReason)]);
  rows.push(['Policy', 'Citation-backed · no inferred fact promotion']);
  return make('section', {
    className: 'life-decision-context life-knowledge-context',
    attrs: { 'aria-label': 'Personal knowledge context' },
  }, [
    make('h4', { text: 'Personal knowledge' }),
    make('dl', {}, rows.flatMap(([label, value]) => [
      make('div', { className: 'life-decision-row life-knowledge-row' }, [
        make('dt', { text: label }), make('dd', { text: value }),
      ]),
    ])),
  ]);
}

export function isFocusableLifeEntity(item) {
  return Boolean(
    text(item?.id)
    && FOCUSABLE_TYPES.has(entityType(item?.type ?? item?.entity_type))
    && FOCUSABLE_STATUSES.has(text(item?.status).toLowerCase())
    && number(item?.version, 0) >= 1
  );
}

function entityCard(item) {
  const headingId = `life-entity-${item.id || Math.random().toString(36).slice(2)}`;
  return make('article', {
    className: 'life-entity-card',
    attrs: {
      role: 'listitem', tabindex: '0', 'aria-labelledby': headingId,
      'data-life-entity-type': item.type,
      'data-life-entity-id': item.id,
    },
  }, [
    make('header', { className: 'life-entity-header' }, [
      make('span', { className: `life-entity-type life-type-${item.type}`, text: humanize(item.type) }),
      make('span', { className: `life-entity-status life-status-${item.status}`, text: humanize(item.status) }),
    ]),
    make('h3', { id: headingId, text: item.title }),
    item.summary ? make('p', { className: 'life-entity-summary', text: item.summary.slice(0, 600) }) : null,
    decisionContext(item),
    financeContext(item),
    habitContext(item),
    relationshipContext(item),
    journalContext(item),
    homeContext(item),
    travelContext(item),
    learningCareerContext(item),
    workBusinessContext(item),
    personalKnowledgeContext(item),
    taskContext(item),
    automationContext(item),
    make('div', { className: 'life-entity-meta' }, entityMeta(item)),
    ...(isFocusableLifeEntity(item) ? [make('div', { className: 'life-entity-actions' }, [
      make('button', {
        type: 'button', className: 'life-button life-focus-button', text: 'Focus',
        attrs: {
          'data-life-focus-id': item.id,
          'aria-label': `Focus on ${item.title}`,
        },
      }),
    ])] : []),
  ]);
}

async function startLifeFocus(item) {
  if (!isFocusableLifeEntity(item)) return false;
  const focus = typeof window !== 'undefined' ? window.missionControlModule : null;
  if (typeof focus?.prepareFocus !== 'function') {
    announce('Focus is unavailable. Open Today and retry.');
    return false;
  }
  const prepared = await focus.prepareFocus({
    target: { kind: 'life_entity', id: item.id, version: item.version },
    title: item.title,
    summary: item.summary,
    properties: item.properties,
  });
  if (!prepared) announce('Focus could not be opened.');
  return prepared;
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

export async function revealLifeEntity(entityId) {
  const id = text(entityId);
  if (!id) return false;
  if (!state.open && !(await open())) return false;
  if (state.loading || !state.items.length) await loadLifeEntities();
  let item = state.items.find((row) => row.id === id);
  if (!item) {
    try {
      item = await getLifeEntity(id);
      state.items = [item, ...state.items.filter((row) => row.id !== id)].slice(0, MAX_ENTITIES);
      state.count = Math.max(state.count, state.items.length);
    } catch (error) {
      announce(text(error?.message, 'Could not open the Life result.'));
      return false;
    }
  }
  state.filter = 'all';
  render();
  const card = [...refs.list.querySelectorAll('[data-life-entity-id]')]
    .find((node) => node.getAttribute('data-life-entity-id') === id);
  if (!card) return false;
  card.scrollIntoView?.({ block: 'center', behavior: 'auto' });
  try { card.focus({ preventScroll: true }); } catch (_) { card.focus?.(); }
  announce(`Opened ${item.title}.`);
  return true;
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
  refs.list.addEventListener('click', (event) => {
    const control = event.target.closest?.('[data-life-focus-id]');
    if (!control || !refs.list.contains(control)) return;
    const item = state.items.find((row) => row.id === control.getAttribute('data-life-focus-id'));
    if (item) void startLifeFocus(item);
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

export const __test = Object.freeze({
  MAX_ENTITIES, FILTERS, FILTER_TYPES, FOCUSABLE_TYPES, FOCUSABLE_STATUSES,
  humanize, formatDate, isFocusableLifeEntity, normalizeDecisionProperties,
  normalizeAutomationProperties, normalizePersonalKnowledgeProperties,
});

const lifeWorkspaceModule = {
  init, open, close, isOpen, focus, loadLifeEntities, listLifeEntities,
  getLifeEntity, revealLifeEntity, __test,
};

export default lifeWorkspaceModule;

if (typeof window !== 'undefined') window.lifeWorkspaceModule = lifeWorkspaceModule;
