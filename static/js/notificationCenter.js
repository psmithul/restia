// Notification command center — a bell in the icon rail with a dropdown
// aggregating everything that used to hide behind scattered red dots:
// emails needing reply, direct messages, due to-dos, calendar events, AI
// suggestions, and finished long-running jobs. Data comes from
// GET /api/notifications/center (routes/notification_center_routes.py).

import uiModule from './ui.js';
import messagingModule from './messaging.js';
import { calculateNotificationPanelHorizontalPosition } from './notificationPanelPosition.js';

const API_BASE = '';
const POLL_MS = 90 * 1000;

let _panel = null;
let _data = null;
let _pollTimer = null;
let _lastSeenCount = 0;

const _esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

const _ICONS = {
  bell: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>',
  email: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg>',
  message: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  todo: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>',
  event: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
  spark: '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0L14.59 8.41L23 12L14.59 15.59L12 24L9.41 15.59L1 12L9.41 8.41Z"/></svg>',
  job: '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
};

async function _fetchCenter() {
  try {
    const res = await fetch(`${API_BASE}/api/notifications/center`, { credentials: 'same-origin' });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

function _updateBadge() {
  const btn = document.getElementById('rail-notif-center');
  if (!btn) return;
  let badge = btn.querySelector('.rail-notes-badge');
  const count = _data?.count || 0;
  if (count > 0) {
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'rail-notes-badge fired';
      btn.appendChild(badge);
    }
    badge.textContent = count > 99 ? '99+' : String(count);
  } else if (badge) {
    badge.remove();
  }
}

function _section(title, icon, items, renderItem) {
  if (!items || !items.length) return '';
  return `
    <div class="notif-center-section">
      <div class="notif-center-section-title">${icon} ${_esc(title)}<span class="notif-center-section-count">${items.length}</span></div>
      ${items.map(renderItem).join('')}
    </div>`;
}

function _render() {
  if (!_panel) return;
  const d = _data || { emails: [], messages: [], todos: [], events: [], suggestions: [], jobs: [], count: 0 };
  const empty = !d.emails.length && !(d.messages || []).length && !d.todos.length && !d.events.length && !d.suggestions.length && !d.jobs.length;
  _panel.querySelector('.notif-center-body').innerHTML = empty
    ? '<div class="notif-center-empty">All clear — nothing needs you right now.</div>'
    : (
      _section('Needs a reply', _ICONS.email, d.emails, (e) => `
        <button class="notif-center-item" data-kind="email" data-hash="${_esc(e.open_hash)}">
          <span class="notif-center-item-main">${_esc(e.subject)}</span>
          <span class="notif-center-item-sub">${_esc(e.from)}${e.reason ? ' · ' + _esc(e.reason) : ''}</span>
        </button>`)
      + _section('Messages', _ICONS.message, d.messages || [], (m) => `
        <button class="notif-center-item" data-kind="message" data-user="${_esc(m.sender)}">
          <span class="notif-center-item-main">${_esc(m.sender)}${m.unread > 1 ? ` <span class="notif-center-section-count">${m.unread}</span>` : ''}</span>
          <span class="notif-center-item-sub">${_esc(m.preview || 'New message')}</span>
        </button>`)
      + _section('To-dos due', _ICONS.todo, d.todos, (t) => `
        <button class="notif-center-item" data-kind="todo" data-hash="${_esc(t.open_hash)}">
          <span class="notif-center-item-main${t.overdue ? ' overdue' : ''}">${_esc(t.title)}</span>
          <span class="notif-center-item-sub">${t.overdue ? 'overdue · ' : ''}${_esc(t.due_label)}${t.repeat && t.repeat !== 'none' ? ' · repeats' : ''}</span>
        </button>`)
      + _section('Calendar', _ICONS.event, d.events, (ev) => `
        <button class="notif-center-item" data-kind="event">
          <span class="notif-center-item-main">${_esc(ev.summary)}</span>
          <span class="notif-center-item-sub">${_esc(ev.start_label)}</span>
        </button>`)
      + _section('Suggestions', _ICONS.spark, d.suggestions, (s) => `
        <button class="notif-center-item notif-center-suggestion" data-kind="suggestion" data-prompt="${_esc(s.prompt)}">
          <span class="notif-center-item-main">${_esc(s.text)}</span>
          <span class="notif-center-item-sub">Click to ask the assistant</span>
        </button>`)
      + _section('Jobs finished', _ICONS.job, d.jobs, (j) => `
        <button class="notif-center-item" data-kind="job">
          <span class="notif-center-item-main">${_esc(j.task_name || 'Task')}<span class="notif-center-job-status ${j.status === 'success' ? 'ok' : 'err'}">${_esc(j.status || '')}</span></span>
          ${j.body ? `<span class="notif-center-item-sub">${_esc(String(j.body).slice(0, 140))}</span>` : ''}
        </button>`)
    );

  _panel.querySelectorAll('.notif-center-item').forEach((item) => {
    item.addEventListener('click', () => {
      const kind = item.dataset.kind;
      _closePanel();
      if (kind === 'message') {
        const username = item.dataset.user || '';
        messagingModule.open();
        if (username) messagingModule.openConversation(username);
      } else if (kind === 'email' || kind === 'todo') {
        const hash = item.dataset.hash || '';
        if (hash) {
          // Re-assigning the same hash doesn't fire hashchange — clear first.
          if (window.location.hash === hash) window.location.hash = '';
          window.location.hash = hash;
        }
      } else if (kind === 'event') {
        document.getElementById('rail-calendar')?.click();
      } else if (kind === 'job') {
        document.getElementById('rail-tasks')?.click();
      } else if (kind === 'suggestion') {
        const composer = document.getElementById('message');
        if (composer) {
          composer.value = item.dataset.prompt || '';
          composer.dispatchEvent(new Event('input', { bubbles: true }));
          composer.focus();
        }
      }
    });
  });
}

function _closePanel() {
  if (_panel) {
    _panel.remove();
    _panel = null;
    document.removeEventListener('click', _onDocClick, true);
    document.removeEventListener('keydown', _onKeyDown, true);
  }
}

function _onDocClick(e) {
  if (_panel && !_panel.contains(e.target) && e.target.id !== 'rail-notif-center' && !e.target.closest('#rail-notif-center')) {
    _closePanel();
  }
}

function _onKeyDown(e) {
  if (e.key === 'Escape') _closePanel();
}

async function _openPanel(anchorBtn) {
  if (_panel) { _closePanel(); return; }
  _panel = document.createElement('div');
  _panel.className = 'notif-center-panel';
  _panel.innerHTML = `
    <div class="notif-center-header">
      ${_ICONS.bell}
      <span>Notifications</span>
      <span style="flex:1"></span>
      <button class="notif-center-refresh" title="Refresh">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7"/><polyline points="3 4 3 10 9 10"/></svg>
      </button>
    </div>
    <div class="notif-center-body"><div class="notif-center-empty">Loading…</div></div>
  `;
  document.body.appendChild(_panel);
  // Anchor beside the trigger. The icon rail can live on either viewport edge,
  // so opening the panel to its right would put it off-screen when the sidebar
  // is collapsed on the right.
  const r = anchorBtn.getBoundingClientRect();
  if (window.innerWidth > 640) {
    const panelWidth = _panel.getBoundingClientRect().width;
    const position = calculateNotificationPanelHorizontalPosition(r, window.innerWidth, panelWidth);
    _panel.style.left = position.left;
    _panel.style.right = position.right;
    _panel.style.top = `${Math.max(8, Math.round(r.top - 4))}px`;
  } else {
    _panel.style.left = '8px';
    _panel.style.right = '8px';
    _panel.style.top = `${Math.round(r.bottom + 8)}px`;
  }
  document.addEventListener('click', _onDocClick, true);
  document.addEventListener('keydown', _onKeyDown, true);
  _panel.querySelector('.notif-center-refresh').addEventListener('click', async (e) => {
    e.stopPropagation();
    _data = (await _fetchCenter()) || _data;
    _updateBadge();
    _render();
  });
  _render();
  _data = (await _fetchCenter()) || _data;
  _lastSeenCount = _data?.count || 0;
  _updateBadge();
  _render();
}

function _injectBellButton() {
  if (document.getElementById('rail-notif-center')) return;
  const rail = document.getElementById('rail-search-btn')?.parentElement;
  if (!rail) return;
  const btn = document.createElement('button');
  btn.className = 'icon-rail-btn';
  btn.id = 'rail-notif-center';
  btn.title = 'Notifications';
  btn.style.position = 'relative';
  btn.innerHTML = _ICONS.bell;
  const anchor = document.getElementById('rail-search-btn');
  rail.insertBefore(btn, anchor);
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    _openPanel(btn);
  });
}

async function _poll() {
  const d = await _fetchCenter();
  if (d) {
    _data = d;
    _updateBadge();
    if (_panel) _render();
    // Gentle toast when NEW items appeared since the last poll (not on
    // first load, and never spamming — one line, auto-dismisses).
    if (_lastSeenCount && d.count > _lastSeenCount) {
      try { uiModule.showToast(`${d.count - _lastSeenCount} new notification${d.count - _lastSeenCount === 1 ? '' : 's'}`); } catch {}
    }
    _lastSeenCount = d.count;
  }
}

function init() {
  _injectBellButton();
  _poll();
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(_poll, POLL_MS);
}

export default { init };
