// static/js/messaging.js

/**
 * Direct messages — a WhatsApp-style chat between user accounts.
 *
 * Two-pane modal: a conversation list on the left, the active thread on the
 * right. On phones it collapses to one pane at a time (list → thread → back).
 * Polls the open thread for new messages and the sidebar badge for unread
 * counts. Backend: routes/messaging_routes.py (strictly pair-scoped).
 *
 * All user-controlled strings (usernames, message bodies) are escaped before
 * being inserted as HTML.
 */

import uiModule from './ui.js';
import * as Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';

const API = '';
const esc = uiModule.esc;

let _open = false;
let _me = null;
let _activeOther = null;            // username of the open conversation, or null
let _lastMsgId = 0;                 // highest message id shown in the open thread
let _threadPollTimer = null;
let _listPollTimer = null;
let _badgePollTimer = null;
let _escHandler = null;
let _conversations = [];           // cached list for re-render
let _linkRequests = [];            // pending Home Link requests (hub admins)
let _pendingCheckTimer = null;     // re-poll while the "waiting" card is up
let _sending = false;

const THREAD_POLL_MS = 1000;
const LIST_POLL_MS = 3000;
const BADGE_POLL_MS = 10000;

// ── Small helpers ──────────────────────────────────────────────────────────

async function _api(path, opts) {
  const res = await fetch(API + path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) {
    let msg = `Request failed (${res.status})`;
    try { const j = await res.json(); msg = j.detail || j.error || msg; } catch (_) {}
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function _initial(name) {
  return (name || '?').trim().charAt(0).toUpperCase() || '?';
}

// Deterministic avatar color from the username.
function _avatarColor(name) {
  let hash = 0;
  const s = name || '';
  for (let i = 0; i < s.length; i++) hash = (hash * 31 + s.charCodeAt(i)) & 0xffffffff;
  const hue = Math.abs(hash) % 360;
  return `hsl(${hue}, 45%, 45%)`;
}

function _fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const yst = new Date(now); yst.setDate(now.getDate() - 1);
  if (d.toDateString() === yst.toDateString()) return 'Yesterday';
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function _avatarHtml(name, cls) {
  return `<span class="msg-avatar ${cls || ''}" style="background:${_avatarColor(name)}">${esc(_initial(name))}</span>`;
}

// ── Sidebar unread badge ────────────────────────────────────────────────────

function _setNavBadge(count) {
  const btn = document.getElementById('tool-messages-btn');
  if (!btn) return;
  let badge = btn.querySelector('.msg-nav-badge');
  if (count > 0) {
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'msg-nav-badge';
      btn.appendChild(badge);
    }
    badge.textContent = count > 99 ? '99+' : String(count);
  } else if (badge) {
    badge.remove();
  }
  // Also mirror on the icon-rail button, if present.
  const rail = document.getElementById('rail-messages');
  if (rail) {
    let rb = rail.querySelector('.rail-notes-badge');
    if (count > 0) {
      if (!rb) { rb = document.createElement('span'); rb.className = 'rail-notes-badge fired'; rail.appendChild(rb); }
      rb.textContent = count > 99 ? '99+' : String(count);
    } else if (rb) { rb.remove(); }
  }
}

async function _pollBadge() {
  try {
    const data = await _api('/api/messages/unread');
    _setNavBadge(data.total || 0);
  } catch (_) { /* not logged in / unavailable — leave badge as-is */ }
}

export function startBadgePolling() {
  _pollBadge();
  if (_badgePollTimer) clearInterval(_badgePollTimer);
  _badgePollTimer = setInterval(_pollBadge, BADGE_POLL_MS);
}

// ── Conversation list ───────────────────────────────────────────────────────

async function _loadConversations() {
  const data = await _api('/api/messages/conversations');
  _me = data.me;
  _conversations = data.conversations || [];
  _linkRequests = data.link_requests || [];
  _renderConversationList();
}

// Approve / block a pending Home Link request (hub admins only — the
// endpoint itself re-checks admin server-side).
async function _actOnLinkRequest(handle, action) {
  try {
    await _api(`/api/link/admin/guests/${encodeURIComponent(handle)}`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    });
  } catch (e) {
    uiModule.showError && uiModule.showError(`Could not ${action} ${handle}: ${e.message}`);
  }
  _loadConversations().catch(() => {});
}

function _linkRequestsHtml() {
  if (!_linkRequests.length) return '';
  const rows = _linkRequests.map(r => `
    <div class="msg-link-req" data-handle="${esc(r.handle)}">
      ${_avatarHtml(r.guest, '')}
      <div class="msg-link-req-mid">
        <span class="msg-link-req-name">${esc(r.guest)}</span>
        <span class="msg-link-req-hint">wants to chat</span>
      </div>
      <button type="button" class="msg-req-btn msg-req-approve" title="Approve">✓</button>
      <button type="button" class="msg-req-btn msg-req-block" title="Block">✕</button>
    </div>`).join('');
  return `<div class="msg-link-reqs"><div class="msg-link-reqs-title">Chat requests</div>${rows}</div>`;
}

function _renderConversationList() {
  const list = document.getElementById('msg-convo-list');
  if (!list) return;
  const reqs = _linkRequestsHtml();
  if (!_conversations.length && !reqs) {
    list.innerHTML = `<div class="msg-empty-list">No conversations yet.<br><span>Start one with the ✎ button.</span></div>`;
    return;
  }
  list.innerHTML = reqs + _conversations.map(c => {
    const active = c.username === _activeOther ? ' active' : '';
    const unread = c.unread > 0
      ? `<span class="msg-convo-unread">${c.unread > 99 ? '99+' : c.unread}</span>` : '';
    const preview = c.last_mine ? `You: ${c.last_body || ''}` : (c.last_body || '');
    return `
      <div class="msg-convo-item${active}" data-user="${esc(c.username)}" role="button" tabindex="0">
        ${_avatarHtml(c.username, 'msg-avatar-lg')}
        <div class="msg-convo-mid">
          <div class="msg-convo-top">
            <span class="msg-convo-name">${esc(c.username)}${c.home ? ' <span class="msg-admin-tag msg-dev-tag">dev</span>' : (c.is_admin ? ' <span class="msg-admin-tag">admin</span>' : '')}</span>
            <span class="msg-convo-time">${esc(_fmtTime(c.last_at))}</span>
          </div>
          <div class="msg-convo-preview">${esc(preview)}</div>
        </div>
        ${unread}
      </div>`;
  }).join('');

  list.querySelectorAll('.msg-convo-item').forEach(el => {
    const open = () => openConversation(el.dataset.user);
    el.addEventListener('click', open);
    el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } });
  });
  list.querySelectorAll('.msg-link-req').forEach(el => {
    const handle = el.dataset.handle;
    el.querySelector('.msg-req-approve')?.addEventListener('click', () => _actOnLinkRequest(handle, 'approve'));
    el.querySelector('.msg-req-block')?.addEventListener('click', () => _actOnLinkRequest(handle, 'block'));
  });
}

// ── Active thread ───────────────────────────────────────────────────────────

export async function openConversation(other) {
  if (!other) return;
  _activeOther = other;
  _lastMsgId = 0;
  _stopPendingCheck();
  const modal = document.getElementById('messages-modal');
  if (modal) modal.classList.add('msg-thread-view'); // mobile: show thread pane
  _renderThreadHeader(other);
  _renderConversationList(); // reflect active highlight

  const body = document.getElementById('msg-thread-body');
  if (body) body.innerHTML = `<div class="msg-thread-loading">Loading…</div>`;

  try {
    const data = await _api(`/api/messages/conversations/${encodeURIComponent(other)}`);
    _me = data.me;
    _renderThreadHeader(other, data.other);
    _renderMessages(data.messages, true);
    _startThreadPolling();
    // Opening marks read server-side; refresh list + badge to clear the count.
    _loadConversations().catch(() => {});
    _pollBadge();
  } catch (e) {
    if (e.status === 409 && e.message === 'link_not_connected') {
      _renderConnectCard(other);
      return;
    }
    if (e.status === 403 && e.message === 'link_pending') {
      _renderPendingCard(other);
      return;
    }
    if (body) body.innerHTML = `<div class="msg-thread-loading">${esc(e.message)}</div>`;
  }
  const input = document.getElementById('msg-composer-input');
  if (input) setTimeout(() => input.focus(), 50);
}

function _renderThreadHeader(other, meta) {
  const host = document.getElementById('msg-thread-header');
  if (!host) return;
  host.innerHTML = `
    <button type="button" class="msg-back-btn" id="msg-back-btn" title="Back" aria-label="Back to conversations">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
    </button>
    ${_avatarHtml(other, '')}
    <div class="msg-thread-who">
      <span class="msg-thread-name">${esc(other)}${meta && meta.home ? ' <span class="msg-admin-tag msg-dev-tag">dev</span>' : (meta && meta.is_admin ? ' <span class="msg-admin-tag">admin</span>' : '')}</span>
    </div>`;
  const back = document.getElementById('msg-back-btn');
  if (back) back.addEventListener('click', _closeThread);
}

function _closeThread() {
  _activeOther = null;
  _stopThreadPolling();
  _stopPendingCheck();
  const modal = document.getElementById('messages-modal');
  if (modal) modal.classList.remove('msg-thread-view');
  _renderConversationList();
}

function _renderMessages(messages, replace) {
  const body = document.getElementById('msg-thread-body');
  if (!body) return;
  if (replace) body.innerHTML = '';
  const empty = body.querySelector('.msg-thread-loading');
  if (empty && messages.length) empty.remove();

  if (replace && !messages.length) {
    body.innerHTML = `<div class="msg-thread-empty">No messages yet — say hi 👋</div>`;
    return;
  }
  const emptyHint = body.querySelector('.msg-thread-empty');
  if (emptyHint && messages.length) emptyHint.remove();

  const frag = document.createDocumentFragment();
  for (const m of messages) {
    if (m.id && m.id <= _lastMsgId) continue;
    if (m.id) _lastMsgId = Math.max(_lastMsgId, m.id);
    const row = document.createElement('div');
    row.className = 'msg-bubble-row ' + (m.mine ? 'mine' : 'theirs');
    row.innerHTML = `
      <div class="msg-bubble">
        <div class="msg-bubble-text">${esc(m.body).replace(/\n/g, '<br>')}</div>
        <div class="msg-bubble-meta">${esc(_fmtTime(m.created_at))}${m.mine ? _tick(m.read) : ''}</div>
      </div>`;
    frag.appendChild(row);
  }
  const wasNearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 120;
  body.appendChild(frag);
  if (replace || wasNearBottom) body.scrollTop = body.scrollHeight;
}

function _tick(read) {
  // Double-check tick; blue-ish when read.
  const color = read ? 'var(--accent, #34b7f1)' : 'currentColor';
  return `<span class="msg-tick" style="color:${color}" title="${read ? 'Read' : 'Sent'}">
    <svg width="15" height="11" viewBox="0 0 18 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 6.5 4.5 10 11 3"/><polyline points="7 10 13.5 3"/></svg></span>`;
}

// ── Home Link connect card ─────────────────────────────────────────────────
// Shown in place of the thread when the special home contact (Home Link,
// routes/link_routes.py) is opened before this instance has registered a
// handle with the home server.

function _renderConnectCard(other, errText) {
  const body = document.getElementById('msg-thread-body');
  if (!body) return;
  body.innerHTML = `
    <div class="msg-connect-card">
      <div class="msg-connect-icon">🔗</div>
      <h4>Say hi to the developer</h4>
      <p>Pick a handle to register this instance with <strong>${esc(other)}</strong>.
         Messages you send here go straight to the developer's inbox, and replies
         show up right in this thread.</p>
      <div class="msg-connect-row">
        <input type="text" id="msg-connect-handle" maxlength="32" autocomplete="off"
               placeholder="your-handle" aria-label="Handle" />
        <button type="button" id="msg-connect-btn">Connect</button>
      </div>
      <div class="msg-connect-err" id="msg-connect-err">${errText ? esc(errText) : ''}</div>
    </div>`;
  const input = document.getElementById('msg-connect-handle');
  const btn = document.getElementById('msg-connect-btn');
  const go = async () => {
    const handle = (input.value || '').trim().toLowerCase();
    if (!handle) { input.focus(); return; }
    btn.disabled = true;
    btn.textContent = 'Connecting…';
    try {
      await _api('/api/homelink/connect', { method: 'POST', body: JSON.stringify({ handle }) });
      openConversation(other);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Connect';
      const err = document.getElementById('msg-connect-err');
      if (err) err.textContent = e.message;
    }
  };
  btn.addEventListener('click', go);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); go(); } });
  setTimeout(() => input.focus(), 50);
}

// Registered but not yet approved by the hub owner. Re-checks quietly every
// few seconds so the thread comes alive the moment the owner approves.
function _renderPendingCard(other) {
  const body = document.getElementById('msg-thread-body');
  if (!body) return;
  body.innerHTML = `
    <div class="msg-connect-card">
      <div class="msg-connect-icon">⏳</div>
      <h4>Waiting for approval</h4>
      <p>Your request to chat with <strong>${esc(other)}</strong> has been sent.
         The conversation will open automatically once the developer accepts.</p>
    </div>`;
  _stopPendingCheck();
  _pendingCheckTimer = setInterval(async () => {
    if (!_open || _activeOther !== other) { _stopPendingCheck(); return; }
    try {
      // Silent probe — only rebuild the thread once we're approved, so the
      // waiting card doesn't flicker through a loading state every tick.
      await _api(`/api/messages/conversations/${encodeURIComponent(other)}?after_id=0`);
      _stopPendingCheck();
      openConversation(other);
    } catch (_) { /* still pending / offline — keep waiting */ }
  }, 8000);
}

function _stopPendingCheck() {
  if (_pendingCheckTimer) { clearInterval(_pendingCheckTimer); _pendingCheckTimer = null; }
}

async function _pollThread() {
  if (!_activeOther || !_open) return;
  try {
    const data = await _api(`/api/messages/conversations/${encodeURIComponent(_activeOther)}?after_id=${_lastMsgId}`);
    if (data.messages && data.messages.length) {
      _renderMessages(data.messages, false);
      _loadConversations().catch(() => {});
    }
  } catch (_) { /* transient */ }
}

function _startThreadPolling() {
  _stopThreadPolling();
  _threadPollTimer = setInterval(_pollThread, THREAD_POLL_MS);
}
function _stopThreadPolling() {
  if (_threadPollTimer) { clearInterval(_threadPollTimer); _threadPollTimer = null; }
}

async function _sendCurrent() {
  const input = document.getElementById('msg-composer-input');
  if (!input || !_activeOther || _sending) return;
  const body = input.value.trim();
  if (!body) return;
  _sending = true;
  input.value = '';
  input.style.height = 'auto';
  try {
    const data = await _api(`/api/messages/conversations/${encodeURIComponent(_activeOther)}`, {
      method: 'POST',
      body: JSON.stringify({ body }),
    });
    _renderMessages([data.message], false);
    _loadConversations().catch(() => {});
  } catch (e) {
    input.value = body; // restore so the user doesn't lose their text
    uiModule.showError && uiModule.showError('Message failed: ' + e.message);
  } finally {
    _sending = false;
    input.focus();
  }
}

// ── New-conversation picker ─────────────────────────────────────────────────

async function _openNewChatPicker() {
  const overlay = document.getElementById('msg-newchat-overlay');
  const list = document.getElementById('msg-newchat-list');
  if (!overlay || !list) return;
  overlay.classList.remove('hidden');
  list.innerHTML = `<div class="msg-thread-loading">Loading users…</div>`;
  try {
    const data = await _api('/api/messages/users');
    const users = data.users || [];
    if (!users.length) {
      list.innerHTML = `<div class="msg-empty-list">Only your account exists on this instance.<br><span>Create another account in Settings → Users to start a direct message.</span></div>`;
      return;
    }
    list.innerHTML = users.map(u => {
      const tag = u.home ? ' <span class="msg-admin-tag msg-dev-tag">dev</span>'
        : u.remote ? ' <span class="msg-admin-tag">remote</span>'
        : u.is_admin ? ' <span class="msg-admin-tag">admin</span>' : '';
      const hint = u.home ? '<span class="msg-newchat-hint">Chat with the developer</span>' : '';
      return `
      <div class="msg-newchat-item" data-user="${esc(u.username)}" role="button" tabindex="0">
        ${_avatarHtml(u.username, 'msg-avatar-lg')}
        <span class="msg-newchat-name">${esc(u.username)}${tag}${hint}</span>
      </div>`;
    }).join('');
    list.querySelectorAll('.msg-newchat-item').forEach(el => {
      const go = () => { overlay.classList.add('hidden'); openConversation(el.dataset.user); };
      el.addEventListener('click', go);
      el.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
    });
  } catch (e) {
    list.innerHTML = `<div class="msg-thread-loading">${esc(e.message)}</div>`;
  }
}

// ── Modal lifecycle ─────────────────────────────────────────────────────────

function _buildModal() {
  const modal = document.createElement('div');
  modal.className = 'modal';
  modal.id = 'messages-modal';
  modal.innerHTML = `
    <div class="modal-content messages-modal-content">
      <div class="messages-layout">
        <aside class="msg-list-pane">
          <div class="msg-list-header">
            <h4>Messages</h4>
            <span style="flex:1"></span>
            <button type="button" class="msg-newchat-btn" id="msg-newchat-btn" title="New message" aria-label="New message">
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>
            </button>
            <button type="button" class="close-btn msg-list-close" id="messages-close" title="Close">✖</button>
          </div>
          <div class="msg-convo-list" id="msg-convo-list"></div>
        </aside>
        <section class="msg-thread-pane">
          <div class="msg-thread-header" id="msg-thread-header"></div>
          <div class="msg-thread-body" id="msg-thread-body">
            <div class="msg-thread-placeholder">
              <svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" style="opacity:0.3"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
              <p>Select a conversation, or start a new one.</p>
            </div>
          </div>
          <div class="msg-composer" id="msg-composer">
            <textarea id="msg-composer-input" rows="1" placeholder="Type a message…" aria-label="Message"></textarea>
            <button type="button" class="msg-send-btn" id="msg-send-btn" title="Send" aria-label="Send">
              <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" stroke="none"><path d="M3 20.5v-17l19 8.5-19 8.5zM5 9.3l7.5 2.7L5 14.7V9.3z" opacity="0"/><path d="M2.5 21 22 12 2.5 3 2.5 10l13 2-13 2z"/></svg>
            </button>
          </div>
        </section>
      </div>
      <div class="msg-newchat-overlay hidden" id="msg-newchat-overlay">
        <div class="msg-newchat-card">
          <div class="msg-newchat-header">
            <span>New message</span>
            <button type="button" class="close-btn" id="msg-newchat-cancel" title="Cancel">✖</button>
          </div>
          <div class="msg-newchat-list" id="msg-newchat-list"></div>
        </div>
      </div>
    </div>`;
  document.body.appendChild(modal);

  // Draggable (desktop) via shared helper.
  const content = modal.querySelector('.modal-content');
  const header = modal.querySelector('.msg-list-header');
  if (content && header) makeWindowDraggable(modal, { content, header });

  // Wiring
  document.getElementById('messages-close').addEventListener('click', close);
  document.getElementById('msg-newchat-btn').addEventListener('click', _openNewChatPicker);
  document.getElementById('msg-newchat-cancel').addEventListener('click', () => {
    document.getElementById('msg-newchat-overlay').classList.add('hidden');
  });
  document.getElementById('msg-send-btn').addEventListener('click', _sendCurrent);

  const input = document.getElementById('msg-composer-input');
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        _sendCurrent();
      }
    });
    input.addEventListener('input', () => {
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    });
  }

  modal.addEventListener('click', (e) => {
    if (uiModule.isTouchInsideModal && uiModule.isTouchInsideModal()) return;
    if (e.target === modal) close();
  });

  _escHandler = (e) => {
    if (e.key !== 'Escape') return;
    const overlay = document.getElementById('msg-newchat-overlay');
    if (overlay && !overlay.classList.contains('hidden')) { overlay.classList.add('hidden'); return; }
    const modalEl = document.getElementById('messages-modal');
    if (modalEl && modalEl.classList.contains('msg-thread-view') && window.innerWidth <= 768) {
      _closeThread();
      return;
    }
    close();
  };
  document.addEventListener('keydown', _escHandler);

  Modals.register && Modals.register('messages-modal', {
    railBtnId: 'tool-messages-btn',
    restoreFn: () => { const m = document.getElementById('messages-modal'); if (m) m.classList.remove('hidden'); },
    closeFn: close,
  });

  return modal;
}

export function open() {
  if (_open) return;
  _open = true;
  _buildModal();
  document.getElementById('tool-messages-btn')?.classList.add('active');
  _loadConversations().catch((e) => {
    const list = document.getElementById('msg-convo-list');
    if (list) list.innerHTML = `<div class="msg-thread-loading">${esc(e.message)}</div>`;
  });
  if (_listPollTimer) clearInterval(_listPollTimer);
  _listPollTimer = setInterval(() => { if (_open) _loadConversations().catch(() => {}); }, LIST_POLL_MS);
}

export function close() {
  if (!_open) return;
  _open = false;
  _stopThreadPolling();
  _stopPendingCheck();
  if (_listPollTimer) { clearInterval(_listPollTimer); _listPollTimer = null; }
  _activeOther = null;
  if (_escHandler) { document.removeEventListener('keydown', _escHandler); _escHandler = null; }
  Modals.unregister && Modals.unregister('messages-modal');
  document.getElementById('tool-messages-btn')?.classList.remove('active');
  const modal = document.getElementById('messages-modal');
  if (modal) {
    const content = modal.querySelector('.modal-content');
    if (content) {
      content.classList.add('modal-closing');
      content.addEventListener('animationend', () => modal.remove(), { once: true });
      setTimeout(() => { if (modal.parentElement) modal.remove(); }, 250);
    } else {
      modal.remove();
    }
  }
  _pollBadge();
}

export function toggle() {
  _open ? close() : open();
}

export function isOpen() { return _open; }

export function init() {
  startBadgePolling();
}

const messagingModule = { init, open, close, toggle, isOpen, openConversation, startBadgePolling };
export default messagingModule;
