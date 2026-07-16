// Restia V2 Mission Control — one owner-scoped view of today's work.
// The backend owns aggregation; this module owns a resilient, accessible
// workspace that remains useful when any individual source is unavailable.

const SOURCE_KEYS = Object.freeze([
  'calendar', 'project_work', 'inbox', 'goals', 'tasks', 'study_reviews', 'health',
  'important_mail', 'notes_today', 'daily_brief', 'planning', 'progression',
]);

let API_BASE = typeof window !== 'undefined' ? window.location.origin : '';
let refs = {};
let sequence = 0;
let controller = null;
let activityMoreController = null;
let previousFocus = null;

const state = {
  initialized: false,
  open: false,
  loading: false,
  error: '',
  data: null,
  view: 'home',
  activityLoadingMore: false,
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
  const overall = text(health.overall, healthUnavailable(health) ? 'Unavailable' : 'Ready');
  refs.summary.append(
    summaryCard('Calendar', number(summary.calendar), 'today'),
    summaryCard('Project work', number(summary.project_work), 'open or due'),
    summaryCard('Plan', number(summary.planning), 'open commitments'),
    summaryCard('To Do', number(summary.notes_today), 'next steps'),
    summaryCard('Rank', text(profile.rank_name, 'E-Rank'), `Level ${number(profile.level) || 1}`),
    summaryCard('Automations', number(summary.tasks), 'scheduled or running'),
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
  const actions = state.view === 'activity'
    ? [['Home', 'home'], ['Projects', 'projects'], ['Automations', 'tasks'], ['Calendar', 'calendar']]
    : [['New chat', 'new-chat'], ['Projects', 'projects'], ['Automations', 'tasks'], ['Calendar', 'calendar'], ['To Do', 'todos'], ['Study', 'study']];
  actions.forEach(([label, target]) => refs.quickActions.appendChild(button(label, 'open-target', { className: 'mission-quick-action', target })));
}

function render() {
  refs.loading.hidden = !state.loading;
  refs.error.hidden = !state.error;
  refs.error.textContent = state.error;
  refs.content.hidden = state.loading || Boolean(state.error) || !state.data;
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
  refs.grid.append(
    renderFocus(), renderInboxAttention(), renderPlanning(), renderProgression(), renderCalendar(),
    renderProjectWork(), renderNotesToday(), renderImportantMail(), renderDailyBrief(),
    renderTasks(), renderGoals(), renderReviews(), renderHealth(),
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
    title: heading.querySelector('#mission-title'),
    eyebrow: heading.querySelector('.mission-eyebrow'),
    date: heading.querySelector('#mission-date'), asOf: heading.querySelector('#mission-as-of'),
  };
  root.addEventListener('click', (event) => {
    const control = event.target.closest?.('[data-action]');
    if (!control || !root.contains(control)) return;
    if (control.dataset.action === 'close') close();
    else if (control.dataset.action === 'refresh') loadCurrentView();
    else if (control.dataset.action === 'activity-more') void loadMoreActivity();
    else if (control.dataset.action === 'open-target') triggerTarget(control.dataset.target);
    else if (control.dataset.action.startsWith('planning-')) void mutatePlanningItem(control);
  });
  root.addEventListener('submit', (event) => {
    const form = event.target.closest?.('.mission-plan-form');
    if (!form || !root.contains(form)) return;
    event.preventDefault();
    void createPlanningItem(form);
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
  state.activityLoadingMore = false;
  state.open = false;
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
  sourceMessage, sourceUnavailable, healthUnavailable,
  normalizeInboxAttention, activateInboxNavigation,
});

const missionControlModule = { init, open, close, toggle, isOpen, refresh, __test };
export default missionControlModule;

if (typeof window !== 'undefined') window.missionControlModule = missionControlModule;
