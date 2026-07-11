// static/js/messaging.js

/**
 * Direct messages — a WhatsApp-style chat between user accounts.
 *
 * Two-pane modal: a conversation list on the left, the active thread on the
 * right. On phones it collapses to one pane at a time (list → thread → back).
 * While the modal is open, live updates ride a single SSE stream
 * (/api/messages/stream): new messages, edits/deletes/reactions, typing and
 * read receipts. Polling is the fallback when the stream drops — and always
 * the transport for Home Link threads, whose messages live on a remote hub
 * and never cross the local SSE bus. The sidebar badge keeps its own slow
 * poll for when the modal is closed. Backend: routes/messaging_routes.py
 * (strictly pair-scoped).
 *
 * All user-controlled strings (usernames, bodies, reaction emoji, reply
 * snippets) are escaped before being inserted as HTML; message bodies are
 * linkified through the same escape-first helper the email library uses
 * (_escLinkify, covered by tests/test_email_linkify_security_js.py).
 */

import uiModule from './ui.js';
import * as Modals from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';
import { bindMenuDismiss } from './escMenuStack.js';
import { topPortalZ } from './toolWindowZOrder.js';
import { _escLinkify } from './emailLibrary/utils.js';
import e2ee from './e2ee.js';
import callModule from './call.js';

const API = '';
const esc = uiModule.esc;

let _open = false;
let _me = null;
let _meDisplay = null;              // my own display name (profile), or null
let _activeOther = null;            // username of the open conversation, or null
let _activeOtherMeta = null;        // "other" object from the thread GET ({home, remote, is_admin})
let _threadReady = false;           // thread GET succeeded (vs connect/pending card showing)
let _messages = new Map();          // id → message for the open thread
let _lastMsgId = 0;                 // highest message id shown in the open thread
let _lastRenderedMsg = null;        // tail message of the thread DOM (grouping/day-sep anchor)
let _threadPollTimer = null;
let _listPollTimer = null;
let _badgePollTimer = null;
let _escHandler = null;
let _conversations = [];           // cached list for re-render
let _linkRequests = [];            // pending Home Link requests (hub admins)
let _pendingCheckTimer = null;     // re-poll while the "waiting" card is up
let _sending = false;
let _listFilter = '';              // client-side conversation search

// SSE stream state. _sseHealthy gates the poll timers: while the stream is
// live, local conversations don't poll at all.
let _es = null;
let _sseHealthy = false;
let _sseRetryMs = 5000;
let _sseRetryTimer = null;
let _listRefreshTimer = null;      // coalesces list/badge refreshes off SSE bursts

// Composer state: reply and edit are mutually exclusive.
let _replyTo = null;               // message object being replied to
let _editingId = null;             // id of own message being edited
let _composerEscUnreg = null;      // escMenuStack entry while reply/edit is active

// Typing
let _lastTypingSentAt = 0;
let _typingHideTimer = null;
let _typingPeers = new Map();      // username → expiry ts for the "typing…" list preview

// Message context menu (⋯ / long-press)
let _menuEl = null;

// ── End-to-end encryption (static/js/e2ee.js) ───────────────────────────────
// _e2ee holds this session's identity: `published` once an identity exists on
// the server, `unlocked` once the private key is in memory (needs the
// passphrase). Shared per-conversation AES keys are cached by peer username.
// Missing-peer-key usernames are remembered so we don't refetch every render.
let _e2ee = { published: false, unlocked: false, privateJwk: null, bundle: null };
let _sharedKeys = new Map();     // peer username → derived CryptoKey
let _peerKeyMiss = new Set();    // peers with no published key (send plaintext)

const THREAD_POLL_FALLBACK_MS = 1500;  // SSE down
const THREAD_POLL_HOME_MS = 2000;      // Home Link thread — remote hub, no local SSE
const LIST_POLL_FALLBACK_MS = 5000;    // SSE down
const BADGE_POLL_MS = 10000;
const SSE_RETRY_MIN_MS = 5000;
const SSE_RETRY_MAX_MS = 30000;
const TYPING_SEND_EVERY_MS = 2500;
const TYPING_SHOW_MS = 4000;
const GROUP_WINDOW_MS = 5 * 60 * 1000; // consecutive same-sender messages group inside this
const NEAR_BOTTOM_PX = 120;            // only autoscroll when already reading the newest messages
const QUICK_REACTIONS = ['👍', '❤️', '😂', '😮', '😢'];

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

// In-thread bubbles always show a clock time — the day separator carries the
// date context, so "Yesterday"/"Jun 3" per bubble would be redundant.
function _fmtClock(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function _dayKey(iso) {
  const d = new Date(iso || '');
  return isNaN(d) ? '' : d.toDateString();
}

function _dayLabel(iso) {
  const d = new Date(iso || '');
  if (isNaN(d)) return '';
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return 'Today';
  const yst = new Date(now); yst.setDate(now.getDate() - 1);
  if (d.toDateString() === yst.toDateString()) return 'Yesterday';
  const opts = { month: 'short', day: 'numeric' };
  if (d.getFullYear() !== now.getFullYear()) opts.year = 'numeric';
  return d.toLocaleDateString([], opts);
}

function _truncate(s, n) {
  s = s || '';
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

function _avatarHtml(name, cls) {
  return `<span class="msg-avatar ${cls || ''}" style="background:${_avatarColor(name)}">${esc(_initial(name))}</span>`;
}

function _isHomeThread() {
  return !!(_activeOtherMeta && _activeOtherMeta.home);
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

// ── SSE stream ──────────────────────────────────────────────────────────────
// One EventSource per open modal. While it's healthy, all poll timers for
// local conversations stop; on error we close it, fall back to polling and
// retry with doubling backoff so a down server isn't hammered.

function _connectSSE() {
  if (_es || !_open) return;
  let es;
  try {
    es = new EventSource(API + '/api/messages/stream');
  } catch (_) {
    _scheduleSseRetry();
    return;
  }
  _es = es;

  es.addEventListener('open', () => {
    if (_es !== es) return;
    _sseHealthy = true;
    _sseRetryMs = SSE_RETRY_MIN_MS;
    _reconfigurePolling();
  });

  es.addEventListener('message', (e) => _onSseMessage(e, false));
  es.addEventListener('update', (e) => _onSseMessage(e, true));
  es.addEventListener('typing', _onSseTyping);
  es.addEventListener('read', _onSseRead);

  es.onerror = () => {
    if (_es !== es) return;
    try { es.close(); } catch (_) {}
    _es = null;
    _sseHealthy = false;
    if (!_open) return;
    _reconfigurePolling();
    _scheduleSseRetry();
  };
}

function _scheduleSseRetry() {
  if (_sseRetryTimer) clearTimeout(_sseRetryTimer);
  _sseRetryTimer = setTimeout(() => {
    _sseRetryTimer = null;
    if (_open && !_es) _connectSSE();
  }, _sseRetryMs);
  _sseRetryMs = Math.min(_sseRetryMs * 2, SSE_RETRY_MAX_MS);
}

function _disconnectSSE() {
  if (_sseRetryTimer) { clearTimeout(_sseRetryTimer); _sseRetryTimer = null; }
  if (_es) { try { _es.close(); } catch (_) {} _es = null; }
  _sseHealthy = false;
  _sseRetryMs = SSE_RETRY_MIN_MS;
}

function _onSseMessage(e, isUpdate) {
  let data;
  try { data = JSON.parse(e.data); } catch (_) { return; }
  const m = data && data.message;
  if (!m || m.id == null) return;
  // Home Link threads don't ride the local bus; their poll owns the thread.
  const isActive = _open && _activeOther && data.with === _activeOther && !_isHomeThread();

  if (isUpdate) {
    if (isActive) _updateRowInPlace(m);
  } else if (isActive && _threadReady) {
    if (!m.mine) _hideTypingIndicator();
    // Dedupe by id: own sends were already rendered from the POST response.
    if (_appendMessages([m]) && !m.mine) {
      // The GET marks the thread read server-side (it's what flips the
      // sender's ticks); SSE delivery alone doesn't. after_id is already
      // caught up, so the response body is empty — this is purely the
      // read-marking side effect.
      _api(`/api/messages/conversations/${encodeURIComponent(_activeOther)}?after_id=${_lastMsgId}`).catch(() => {});
    }
  }
  _scheduleListRefresh();
}

function _onSseTyping(e) {
  let data;
  try { data = JSON.parse(e.data); } catch (_) { return; }
  const from = data && data.from;
  if (!from) return;
  _typingPeers.set(from, Date.now() + TYPING_SHOW_MS);
  if (_open && from === _activeOther && !_isHomeThread()) _showTypingIndicator();
  _renderConversationList();
  // Restore the real preview once this signal goes stale (a fresher typing
  // event pushes the expiry forward and schedules its own restore).
  setTimeout(() => {
    if (_open && (_typingPeers.get(from) || 0) <= Date.now()) _renderConversationList();
  }, TYPING_SHOW_MS + 150);
}

function _onSseRead(e) {
  let data;
  try { data = JSON.parse(e.data); } catch (_) { return; }
  if (!data || data.from !== _activeOther) return;
  for (const m of _messages.values()) if (m.mine) m.read = true;
  const body = document.getElementById('msg-thread-body');
  body?.querySelectorAll('.msg-bubble-row.mine .msg-tick:not(.read)').forEach(t => {
    t.classList.add('read');
    t.title = 'Read';
  });
}

// Coalesce the list + badge refresh SSE events ask for, so a burst of
// messages doesn't turn into a burst of /conversations calls.
function _scheduleListRefresh() {
  if (_listRefreshTimer) return;
  _listRefreshTimer = setTimeout(() => {
    _listRefreshTimer = null;
    if (!_open) return;
    _loadConversations().catch(() => {});
    _pollBadge();
  }, 250);
}

// ── End-to-end encryption session ───────────────────────────────────────────
// Encryption is applied to LOCAL conversations. Home Link / remote-guest
// threads still send plaintext for now — their payload E2EE rides the
// cross-instance identity exchange and is a separate step. Every failure path
// degrades to plaintext + a clear indicator; E2EE never blocks chatting.

async function _e2eeRefresh() {
  try {
    const me = await _api('/api/e2ee/me');
    _e2ee.published = !!me.exists;
    _e2ee.bundle = me.exists ? me : null;
  } catch (_) { /* keep prior state — E2EE stays optional */ }
}

function _e2eeEligible() {
  return !!_activeOther && !_isHomeThread()
    && !(_activeOtherMeta && _activeOtherMeta.remote);
}

async function _e2eeSetup() {
  const pass = await uiModule.styledPrompt(
    'Choose an encryption passphrase. It never leaves this device and unlocks your encrypted messages anywhere you sign in. If you forget it, those messages can’t be recovered.',
    { title: 'Set up encryption', placeholder: 'passphrase', confirmText: 'Enable', maxLength: 128 });
  if (!pass) return false;
  try {
    const id = await e2ee.generateIdentity();
    const w = await e2ee.wrapPrivateKey(id.privateJwk, pass);
    await _api('/api/e2ee/me', { method: 'POST', body: JSON.stringify({
      public_jwk: JSON.stringify(id.publicJwk),
      wrapped_private: JSON.stringify(w.wrapped),
      kdf_salt: w.kdf_salt, kdf_iterations: w.kdf_iterations,
    })});
    _e2ee = { published: true, unlocked: true, privateJwk: id.privateJwk, bundle: null };
    _sharedKeys.clear(); _peerKeyMiss.clear();
    uiModule.showToast && uiModule.showToast('Encryption enabled');
    return true;
  } catch (e) {
    uiModule.showError && uiModule.showError('Could not enable encryption: ' + e.message);
    return false;
  }
}

async function _e2eeUnlock() {
  const me = _e2ee.bundle || (await _api('/api/e2ee/me').catch(() => null));
  if (!me || !me.exists) return false;
  const pass = await uiModule.styledPrompt(
    'Enter your encryption passphrase to unlock encrypted messages on this device.',
    { title: 'Unlock encryption', placeholder: 'passphrase', confirmText: 'Unlock', maxLength: 128 });
  if (!pass) return false;
  try {
    _e2ee.privateJwk = await e2ee.unwrapPrivateKey(
      JSON.parse(me.wrapped_private), me.kdf_salt, me.kdf_iterations, pass);
    _e2ee.unlocked = true; _e2ee.bundle = me; _sharedKeys.clear();
    return true;
  } catch (_) {
    uiModule.showError && uiModule.showError('Wrong passphrase');
    return false;
  }
}

// Set up an identity if none exists, else unlock the existing one.
async function _e2eeEnsure() {
  if (_e2ee.unlocked) return true;
  await _e2eeRefresh();
  const ok = _e2ee.published ? await _e2eeUnlock() : await _e2eeSetup();
  if (ok) { _renderThreadHeader(_activeOther, _activeOtherMeta); _decryptThread(); }
  return ok;
}

async function _sharedKeyFor(user) {
  if (!_e2ee.unlocked || !user) return null;
  if (_sharedKeys.has(user)) return _sharedKeys.get(user);
  if (_peerKeyMiss.has(user)) return null;
  try {
    const r = await _api(`/api/e2ee/key/${encodeURIComponent(user)}`);
    if (!r.exists) { _peerKeyMiss.add(user); return null; }
    const key = await e2ee.deriveSharedKey(_e2ee.privateJwk, JSON.parse(r.public_jwk));
    _sharedKeys.set(user, key);
    return key;
  } catch (_) { return null; }
}

// Decrypt any still-locked envelopes in the open thread and patch them in
// place. Safe to call repeatedly; only undecrypted envelopes do work.
async function _decryptThread() {
  if (!_e2ee.unlocked || !_activeOther) return;
  const key = await _sharedKeyFor(_activeOther);
  if (!key) return;
  const patched = [];
  for (const m of _messages.values()) {
    if (m.deleted || m._plain != null || !e2ee.isEnvelope(m.body)) continue;
    try { m._plain = await e2ee.decryptMessage(m.body, key); patched.push(m); }
    catch (_) { /* not for this key — leave it locked */ }
  }
  for (const m of patched) _updateRowInPlace(m);
}

// ── Conversation list ───────────────────────────────────────────────────────

async function _loadConversations() {
  const data = await _api('/api/messages/conversations');
  _me = data.me;
  if (data.me_display !== undefined) _meDisplay = data.me_display;
  _conversations = data.conversations || [];
  _linkRequests = data.link_requests || [];
  _renderConversationList();
  _renderProfileBar();
}

// ── Your profile (the name shown in chat) ───────────────────────────────────

async function _loadProfile() {
  try {
    const p = await _api('/api/profile');
    _me = _me || p.username;
    _meDisplay = p.display_name;
  } catch (_) { /* not signed in — leave defaults */ }
  _renderProfileBar();
}

function _renderProfileBar() {
  const host = document.getElementById('msg-profile-bar');
  if (!host) return;
  const name = _meDisplay || _me || 'You';
  host.innerHTML = `
    ${_avatarHtml(name, 'msg-avatar-lg')}
    <div class="msg-profile-mid">
      <span class="msg-profile-you">You</span>
      <span class="msg-profile-name">${esc(name)}</span>
    </div>
    <button type="button" class="msg-profile-edit" id="msg-profile-edit" title="Set your display name" aria-label="Set your name">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>
    </button>`;
  document.getElementById('msg-profile-edit')?.addEventListener('click', _editProfile);
}

async function _editProfile() {
  const name = await uiModule.styledPrompt(
    'This is the name people see when you chat with them.',
    { title: 'Your name', defaultValue: _meDisplay || '', placeholder: 'Your name', confirmText: 'Save', maxLength: 48 });
  if (name === null) return;
  try {
    await _api('/api/profile', { method: 'POST', body: JSON.stringify({ display_name: name }) });
    _meDisplay = name || null;
    _renderProfileBar();
    _loadConversations().catch(() => {});
    uiModule.showToast && uiModule.showToast('Name saved');
  } catch (e) {
    uiModule.showError && uiModule.showError('Could not save name: ' + e.message);
  }
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
  const filter = _listFilter;
  const convos = filter
    ? _conversations.filter(c =>
        (c.username || '').toLowerCase().includes(filter) ||
        (c.last_body || '').toLowerCase().includes(filter))
    : _conversations;
  // Link-request cards hide while filtering — they aren't searchable rows.
  const reqs = filter ? '' : _linkRequestsHtml();
  if (!convos.length && !reqs) {
    list.innerHTML = filter
      ? `<div class="msg-empty-list">No matches.</div>`
      : `<div class="msg-empty-list">No conversations yet.<br><span>Start one with the ✎ button.</span></div>`;
    return;
  }
  const now = Date.now();
  list.innerHTML = reqs + convos.map(c => {
    const active = c.username === _activeOther ? ' active' : '';
    const unread = c.unread > 0
      ? `<span class="msg-convo-unread">${c.unread > 99 ? '99+' : c.unread}</span>` : '';
    const preview = c.last_mine ? `You: ${c.last_body || ''}` : (c.last_body || '');
    const typing = (_typingPeers.get(c.username) || 0) > now;
    const previewHtml = typing ? '<em class="msg-preview-typing">typing…</em>'
      : e2ee.isEnvelope(c.last_body) ? '<em class="msg-locked">🔒 Encrypted message</em>'
      : esc(preview);
    return `
      <div class="msg-convo-item${active}" data-user="${esc(c.username)}" role="button" tabindex="0">
        ${_avatarHtml(c.username, 'msg-avatar-lg')}
        <div class="msg-convo-mid">
          <div class="msg-convo-top">
            <span class="msg-convo-name">${esc(c.display || c.username)}${c.home ? ' <span class="msg-admin-tag msg-dev-tag">dev</span>' : (c.is_admin ? ' <span class="msg-admin-tag">admin</span>' : '')}</span>
            <span class="msg-convo-time">${esc(_fmtTime(c.last_at))}</span>
          </div>
          <div class="msg-convo-preview">${previewHtml}</div>
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
  _activeOtherMeta = null;
  _threadReady = false;
  _lastMsgId = 0;
  _messages.clear();
  _lastRenderedMsg = null;
  _cancelComposerState();
  _closeMessageMenu();
  _hideTypingIndicator();
  _stopPendingCheck();
  _reconfigurePolling(); // stop the previous thread's poll while we load
  const modal = document.getElementById('messages-modal');
  if (modal) modal.classList.add('msg-thread-view'); // mobile: show thread pane
  _renderThreadHeader(other);
  _renderConversationList(); // reflect active highlight

  const body = document.getElementById('msg-thread-body');
  if (body) body.innerHTML = `<div class="msg-thread-loading">Loading…</div>`;

  try {
    const data = await _api(`/api/messages/conversations/${encodeURIComponent(other)}`);
    if (_activeOther !== other) return; // user opened another thread mid-flight
    _me = data.me;
    if (data.me_display !== undefined) _meDisplay = data.me_display;
    _activeOtherMeta = data.other || null;
    _renderThreadHeader(other, data.other);
    _renderThread(data.messages || []);
    _threadReady = true;
    _reconfigurePolling();
    // Sync E2EE state and decrypt any encrypted history (if already unlocked);
    // re-render the header so the lock reflects this thread's eligibility.
    _e2eeRefresh().then(() => {
      if (_activeOther !== other) return;
      _renderThreadHeader(other, data.other);
      _decryptThread();
    });
    // Opening marks read server-side; refresh list + badge to clear the count.
    _loadConversations().catch(() => {});
    _pollBadge();
  } catch (e) {
    if (_activeOther !== other) return;
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
    ${_avatarHtml((meta && meta.display) || other, '')}
    <div class="msg-thread-who">
      <span class="msg-thread-name">${esc((meta && meta.display) || other)}${meta && meta.home ? ' <span class="msg-admin-tag msg-dev-tag">dev</span>' : (meta && meta.is_admin ? ' <span class="msg-admin-tag">admin</span>' : '')}</span>
    </div>
    ${_callBtnsHtml(meta)}
    ${_lockBtnHtml(meta)}`;
  const back = document.getElementById('msg-back-btn');
  if (back) back.addEventListener('click', _closeThread);
  const lb = document.getElementById('msg-lock-btn');
  if (lb) lb.addEventListener('click', () => { _e2eeEnsure(); });
  document.getElementById('msg-call-voice')?.addEventListener('click', () => _startCallSafe(other, false));
  document.getElementById('msg-call-video')?.addEventListener('click', () => _startCallSafe(other, true));
}

// Calls run over the same-instance signaling bus. Contacts on another instance
// (Home Link / '@remote') can't be dialed yet — say so plainly instead of
// failing silently.
function _startCallSafe(other, video) {
  if (_activeOtherMeta && (_activeOtherMeta.remote || _activeOtherMeta.home)) {
    uiModule.showToast && uiModule.showToast(
      'Calling contacts on another instance is coming soon — calls between accounts here work now.');
    return;
  }
  callModule.startCall(other, video);
}

// Voice/video call buttons — shown whenever calling is enabled so the feature
// is discoverable on every thread. The action itself is gated in
// _startCallSafe (same-instance calls connect; remote shows a clear message).
function _callBtnsHtml(meta) {
  const eligible = meta && callModule.isEnabled && callModule.isEnabled();
  if (!eligible) return '';
  return `
    <button type="button" class="msg-call-btn" id="msg-call-voice" title="Voice call" aria-label="Voice call">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.9.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z"/></svg>
    </button>
    <button type="button" class="msg-call-btn" id="msg-call-video" title="Video call" aria-label="Video call">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 7l-7 5 7 5V7z"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
    </button>`;
}

// Header encryption control. Only local threads are E2EE-eligible for now.
function _lockBtnHtml(meta) {
  const eligible = meta && !meta.home && !meta.remote;
  if (!eligible) return '';
  const on = _e2ee.unlocked;
  const title = on
    ? 'End-to-end encrypted. Messages you send here are encrypted to the recipient.'
    : (_e2ee.published ? 'Unlock end-to-end encryption on this device' : 'Enable end-to-end encryption');
  return `<button type="button" class="msg-lock-btn${on ? ' on' : ''}" id="msg-lock-btn"
            title="${esc(title)}" aria-label="Encryption">${on ? '🔒' : '🔓'}</button>`;
}

function _closeThread() {
  _activeOther = null;
  _activeOtherMeta = null;
  _threadReady = false;
  _cancelComposerState();
  _closeMessageMenu();
  _hideTypingIndicator();
  _stopPendingCheck();
  _reconfigurePolling();
  const modal = document.getElementById('messages-modal');
  if (modal) modal.classList.remove('msg-thread-view');
  _renderConversationList();
}

// ── Thread rendering ────────────────────────────────────────────────────────
// Messages render as grouped bubbles: consecutive same-sender messages within
// 5 minutes share a group, and only the group's last row shows the
// timestamp/ticks line (CSS hides it on non-.grp-tail rows). Day separators
// carry the date so bubble meta only needs the clock time.

function _sameGroup(a, b) {
  if (!a || !b || a.sender !== b.sender) return false;
  if (_dayKey(a.created_at) !== _dayKey(b.created_at)) return false;
  const gap = new Date(b.created_at) - new Date(a.created_at);
  return isFinite(gap) && Math.abs(gap) < GROUP_WINDOW_MS;
}

function _tickHtml(read) {
  // Double-check tick; class-driven color so a 'read' SSE event can flip
  // every sent bubble without re-rendering them.
  return `<span class="msg-tick${read ? ' read' : ''}" title="${read ? 'Read' : 'Sent'}">
    <svg width="15" height="11" viewBox="0 0 18 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 6.5 4.5 10 11 3"/><polyline points="7 10 13.5 3"/></svg></span>`;
}

function _quoteHtml(rt) {
  if (!rt) return '';
  const bodyHtml = rt.body
    ? (e2ee.isEnvelope(rt.body) ? '<em class="msg-locked">🔒 Encrypted</em>' : esc(_truncate(rt.body, 110)))
    : '<em class="msg-deleted">Message deleted</em>';
  return `
    <div class="msg-quote" data-quote-id="${Number(rt.id) || 0}" role="button" tabindex="0" title="Go to message">
      <span class="msg-quote-sender">${esc(rt.sender || '')}</span>
      <span class="msg-quote-body">${bodyHtml}</span>
    </div>`;
}

function _reactionsHtml(m) {
  const rx = m.reactions || {}; // may be absent on old messages
  const groups = new Map();     // emoji → [users]
  for (const [user, emoji] of Object.entries(rx)) {
    if (!emoji) continue;
    if (!groups.has(emoji)) groups.set(emoji, []);
    groups.get(emoji).push(user);
  }
  if (!groups.size) return '';
  const myEmoji = rx[_me];
  const chips = [...groups.entries()].map(([emoji, users]) => `
    <button type="button" class="msg-react-chip${emoji === myEmoji ? ' mine' : ''}"
            data-emoji="${esc(emoji)}" title="${esc(users.join(', '))}">
      ${esc(emoji)}<span class="msg-react-count">${users.length}</span>
    </button>`).join('');
  return `<div class="msg-reactions">${chips}</div>`;
}

// Locked-envelope placeholder: we have ciphertext but not (yet) the key.
function _lockHtml() {
  return '<em class="msg-locked">🔒 Encrypted — unlock to read</em>';
}

function _bubbleInnerHtml(m) {
  const encrypted = !m.deleted && e2ee.isEnvelope(m.body);
  const locked = encrypted && m._plain == null;
  const bodyHtml = m.deleted
    ? '<em class="msg-deleted">Message deleted</em>'
    : locked
      ? _lockHtml()
      : _escLinkify((m._plain != null ? m._plain : m.body) || '');
  const edited = (m.edited && !m.deleted) ? '<span class="msg-edited">(edited)</span>' : '';
  // A small lock marks bubbles that travelled encrypted, once readable.
  const lockBadge = (encrypted && !locked)
    ? '<span class="msg-enc-badge" title="End-to-end encrypted">🔒</span>' : '';
  return `
    ${_quoteHtml(m.reply_to)}
    <div class="msg-bubble-text">${bodyHtml}</div>
    <div class="msg-bubble-meta">${edited}${lockBadge}${esc(_fmtClock(m.created_at))}${m.mine ? _tickHtml(m.read) : ''}</div>`;
}

function _colInnerHtml(m) {
  return `<div class="msg-bubble">${_bubbleInnerHtml(m)}</div>${_reactionsHtml(m)}`;
}

function _rowFor(m, cont) {
  const row = document.createElement('div');
  row.className = 'msg-bubble-row ' + (m.mine ? 'mine' : 'theirs')
    + (cont ? ' grp-cont' : '') + ' grp-tail' + (m.deleted ? ' deleted' : '');
  row.dataset.id = String(m.id);
  row.innerHTML = `
    <div class="msg-bubble-col">${_colInnerHtml(m)}</div>
    <button type="button" class="msg-act-btn" title="Message actions" aria-label="Message actions">⋯</button>`;
  return row;
}

function _daySepEl(iso) {
  const el = document.createElement('div');
  el.className = 'msg-day-sep';
  el.innerHTML = `<span>${esc(_dayLabel(iso))}</span>`;
  return el;
}

// Full re-render of the open thread from _messages (sorted by id). Used for
// the initial load and the rare out-of-order arrival; scroll position is
// preserved unless the reader was already at the bottom.
function _rebuildThread(toBottom) {
  const body = document.getElementById('msg-thread-body');
  if (!body) return;
  const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < NEAR_BOTTOM_PX;
  const prevScroll = body.scrollTop;
  body.innerHTML = '';
  _lastRenderedMsg = null;
  const sorted = [..._messages.values()].sort((a, b) => a.id - b.id);
  if (!sorted.length) {
    body.innerHTML = `<div class="msg-thread-empty">No messages yet — say hi 👋</div>`;
    return;
  }
  const frag = document.createDocumentFragment();
  let prev = null;
  let prevRow = null;
  for (const m of sorted) {
    if (!prev || _dayKey(m.created_at) !== _dayKey(prev.created_at)) frag.appendChild(_daySepEl(m.created_at));
    const cont = _sameGroup(prev, m);
    if (cont && prevRow) prevRow.classList.remove('grp-tail');
    const row = _rowFor(m, cont);
    frag.appendChild(row);
    prev = m; prevRow = row;
  }
  body.appendChild(frag);
  _lastRenderedMsg = prev;
  body.scrollTop = (toBottom || nearBottom) ? body.scrollHeight : prevScroll;
}

function _renderThread(messages) {
  _messages.clear();
  _lastMsgId = 0;
  for (const m of messages) {
    if (!m || m.id == null) continue;
    _messages.set(m.id, m);
    _lastMsgId = Math.max(_lastMsgId, m.id);
  }
  _rebuildThread(true);
}

// Append new messages (poll batch, SSE event, or own POST echo). Dedupes by
// id. Returns true when something new was rendered.
function _appendMessages(list) {
  const body = document.getElementById('msg-thread-body');
  if (!body) return false;
  const fresh = [];
  for (const m of list || []) {
    if (!m || m.id == null || _messages.has(m.id)) continue;
    fresh.push(m);
  }
  if (!fresh.length) return false;
  fresh.sort((a, b) => a.id - b.id);
  const outOfOrder = _lastMsgId > 0 && fresh[0].id < _lastMsgId;
  for (const m of fresh) {
    _messages.set(m.id, m);
    _lastMsgId = Math.max(_lastMsgId, m.id);
  }
  if (outOfOrder) { _rebuildThread(false); return true; }

  body.querySelector('.msg-thread-empty')?.remove();
  body.querySelector('.msg-thread-loading')?.remove();
  body.querySelector('.msg-thread-placeholder')?.remove();

  const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < NEAR_BOTTOM_PX;
  const frag = document.createDocumentFragment();
  for (const m of fresh) {
    if (!_lastRenderedMsg || _dayKey(m.created_at) !== _dayKey(_lastRenderedMsg.created_at)) {
      frag.appendChild(_daySepEl(m.created_at));
    }
    const cont = _sameGroup(_lastRenderedMsg, m);
    if (cont && _lastRenderedMsg) {
      // Extending a group: the previous row hands its meta line to this one.
      const sel = `.msg-bubble-row[data-id="${_lastRenderedMsg.id}"]`;
      (frag.querySelector(sel) || body.querySelector(sel))?.classList.remove('grp-tail');
    }
    frag.appendChild(_rowFor(m, cont));
    _lastRenderedMsg = m;
  }
  // Keep the typing dots pinned below the newest message.
  const ind = document.getElementById('msg-typing-ind');
  if (ind && ind.parentElement === body) body.insertBefore(frag, ind);
  else body.appendChild(frag);
  if (nearBottom) body.scrollTop = body.scrollHeight;
  // Decrypt any newly-arrived encrypted messages, then patch them in place.
  if (fresh.some(m => e2ee.isEnvelope(m.body) && m._plain == null)) _decryptThread();
  return true;
}

// Re-render one bubble where it stands (edit / delete / reaction change) —
// scroll position and grouping classes are untouched.
function _updateRowInPlace(m) {
  if (!m || m.id == null || !_messages.has(m.id)) return;
  _messages.set(m.id, m);
  if (_lastRenderedMsg && _lastRenderedMsg.id === m.id) _lastRenderedMsg = m;
  const body = document.getElementById('msg-thread-body');
  const row = body?.querySelector(`.msg-bubble-row[data-id="${m.id}"]`);
  if (!row) return;
  row.classList.toggle('deleted', !!m.deleted);
  const col = row.querySelector('.msg-bubble-col');
  if (col) col.innerHTML = _colInnerHtml(m);
}

function _jumpToMessage(id) {
  const body = document.getElementById('msg-thread-body');
  const row = body?.querySelector(`.msg-bubble-row[data-id="${id}"]`);
  if (!row) return;
  row.scrollIntoView({ behavior: 'smooth', block: 'center' });
  row.classList.remove('msg-flash');
  void row.offsetWidth; // restart the flash animation on repeat clicks
  row.classList.add('msg-flash');
  setTimeout(() => row.classList.remove('msg-flash'), 1300);
}

// ── Typing indicator ────────────────────────────────────────────────────────

function _showTypingIndicator() {
  const body = document.getElementById('msg-thread-body');
  if (!body || !_threadReady) return;
  let el = document.getElementById('msg-typing-ind');
  if (!el) {
    el = document.createElement('div');
    el.id = 'msg-typing-ind';
    el.className = 'msg-typing-ind';
    el.innerHTML = '<div class="msg-typing-bubble"><span></span><span></span><span></span></div>';
    const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < NEAR_BOTTOM_PX;
    body.appendChild(el);
    if (nearBottom) body.scrollTop = body.scrollHeight;
  }
  if (_typingHideTimer) clearTimeout(_typingHideTimer);
  _typingHideTimer = setTimeout(_hideTypingIndicator, TYPING_SHOW_MS);
}

function _hideTypingIndicator() {
  if (_typingHideTimer) { clearTimeout(_typingHideTimer); _typingHideTimer = null; }
  document.getElementById('msg-typing-ind')?.remove();
}

function _maybeSendTyping() {
  // Fire-and-forget, throttled. Home Link threads skip it — the remote hub
  // has no typing channel.
  if (!_activeOther || !_threadReady || _isHomeThread()) return;
  const now = Date.now();
  if (now - _lastTypingSentAt < TYPING_SEND_EVERY_MS) return;
  _lastTypingSentAt = now;
  _api(`/api/messages/conversations/${encodeURIComponent(_activeOther)}/typing`, { method: 'POST' })
    .catch(() => {});
}

// ── Message actions (context menu, reactions, edit, delete, reply) ────────

function _closeMessageMenu() {
  if (!_menuEl) return;
  const el = _menuEl;
  _menuEl = null;
  if (typeof el._dismiss === 'function') el._dismiss();
  else el.remove();
}

function _openMessageMenu(m, x, y) {
  _closeMessageMenu();
  if (!m || m.deleted) return;
  const isHome = _isHomeThread();
  const menu = document.createElement('div');
  menu.className = 'msg-ctx-menu';

  const parts = [];
  if (!isHome) {
    // Quick reactions; the backend keeps one reaction per user, so tapping a
    // different emoji switches, tapping the current one removes.
    const rx = m.reactions || {};
    parts.push(`<div class="msg-ctx-reacts">${QUICK_REACTIONS.map(em =>
      `<button type="button" class="msg-ctx-react${rx[_me] === em ? ' mine' : ''}" data-emoji="${esc(em)}">${esc(em)}</button>`
    ).join('')}</div>`);
  }
  const item = (act, label, danger) =>
    `<button type="button" class="msg-ctx-item${danger ? ' danger' : ''}" data-act="${act}">${label}</button>`;
  if (!isHome) parts.push(item('reply', 'Reply'));
  parts.push(item('copy', 'Copy'));
  // Edit/delete only for own messages, never in Home Link threads (the hub
  // rejects them — messages already left this instance).
  if (!isHome && m.mine) {
    parts.push(item('edit', 'Edit'));
    parts.push(item('delete', 'Delete', true));
  }
  menu.innerHTML = parts.join('');
  document.body.appendChild(menu);

  menu.style.position = 'fixed';
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
  menu.style.zIndex = String(topPortalZ());
  requestAnimationFrame(() => {
    const r = menu.getBoundingClientRect();
    if (r.right > window.innerWidth - 8) menu.style.left = Math.max(8, window.innerWidth - r.width - 8) + 'px';
    if (r.bottom > window.innerHeight - 8) menu.style.top = Math.max(8, window.innerHeight - r.height - 8) + 'px';
  });

  // Outside click + Escape via the shared menu stack, so Esc closes the menu
  // before it touches reply/edit state, the overlay, or the modal.
  bindMenuDismiss(menu, () => {
    if (_menuEl === menu) _menuEl = null;
    menu.remove();
  });
  _menuEl = menu;

  menu.addEventListener('click', (ev) => {
    const rb = ev.target.closest('.msg-ctx-react');
    if (rb) { _closeMessageMenu(); _toggleReaction(m, rb.dataset.emoji); return; }
    const it = ev.target.closest('.msg-ctx-item');
    if (!it) return;
    _closeMessageMenu();
    switch (it.dataset.act) {
      case 'reply': _enterReply(m); break;
      case 'copy': uiModule.copyToClipboard((m._plain != null ? m._plain : m.body) || ''); break;
      case 'edit': _enterEdit(m); break;
      case 'delete': _confirmDelete(m); break;
    }
  });
}

async function _toggleReaction(m, emoji) {
  if (!emoji || m.deleted) return;
  const mine = (m.reactions || {})[_me];
  const next = mine === emoji ? '' : emoji; // empty string removes my reaction
  try {
    const data = await _api(`/api/messages/msg/${m.id}/react`, {
      method: 'POST',
      body: JSON.stringify({ emoji: next }),
    });
    if (data.message) _updateRowInPlace(data.message);
  } catch (e) {
    uiModule.showError && uiModule.showError('Reaction failed: ' + e.message);
  }
}

async function _confirmDelete(m) {
  const ok = await uiModule.styledConfirm('Delete this message?', { confirmText: 'Delete', danger: true });
  if (!ok) return;
  try {
    const data = await _api(`/api/messages/msg/${m.id}`, { method: 'DELETE' });
    if (data.message) _updateRowInPlace(data.message);
    _scheduleListRefresh();
  } catch (e) {
    uiModule.showError && uiModule.showError('Delete failed: ' + e.message);
  }
}

// ── Composer reply/edit state ───────────────────────────────────────────────

function _registerComposerEsc() {
  if (_composerEscUnreg) return;
  // The reply/edit bar joins the shared Escape stack so the ui.js arbiter
  // cancels it before falling through to close the modal.
  _composerEscUnreg = uiModule.registerMenuDismiss(() => {
    _composerEscUnreg = null;
    _cancelComposerState();
  });
}

function _unregisterComposerEsc() {
  if (!_composerEscUnreg) return;
  const unreg = _composerEscUnreg;
  _composerEscUnreg = null;
  unreg();
}

function _renderComposerBar() {
  const bar = document.getElementById('msg-composer-bar');
  if (!bar) return;
  if (_editingId == null && !_replyTo) {
    bar.classList.add('hidden');
    bar.innerHTML = '';
    return;
  }
  const m = _editingId != null ? _messages.get(_editingId) : _replyTo;
  const title = _editingId != null ? 'Editing message' : `Replying to ${esc((m && m.sender) || '')}`;
  bar.innerHTML = `
    <div class="msg-cbar-main">
      <span class="msg-cbar-title">${title}</span>
      <span class="msg-cbar-snippet">${esc(_truncate((m && m.body) || '', 90))}</span>
    </div>
    <button type="button" class="msg-cbar-cancel" title="Cancel" aria-label="Cancel">✕</button>`;
  bar.classList.remove('hidden');
  bar.querySelector('.msg-cbar-cancel')?.addEventListener('click', _cancelComposerState);
}

function _enterReply(m) {
  if (!m) return;
  if (_editingId != null) _cancelComposerState(); // edit owned the input text
  _editingId = null;
  _replyTo = m;
  _renderComposerBar();
  _registerComposerEsc();
  document.getElementById('msg-composer-input')?.focus();
}

function _enterEdit(m) {
  if (!m || !m.mine || m.deleted) return;
  // Can't edit an encrypted message we haven't unlocked — the raw envelope
  // isn't editable text.
  if (e2ee.isEnvelope(m.body) && m._plain == null) {
    uiModule.showError && uiModule.showError('Unlock encryption to edit this message');
    return;
  }
  _replyTo = null;
  _editingId = m.id;
  const input = document.getElementById('msg-composer-input');
  if (input) {
    input.value = (m._plain != null ? m._plain : m.body) || '';
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 120) + 'px';
    input.focus();
  }
  _renderComposerBar();
  _registerComposerEsc();
}

function _cancelComposerState() {
  if (_editingId != null) {
    // The input held the message being edited, not a draft — clear it.
    const input = document.getElementById('msg-composer-input');
    if (input) { input.value = ''; input.style.height = 'auto'; }
  }
  _editingId = null;
  _replyTo = null;
  _unregisterComposerEsc();
  _renderComposerBar();
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
      <h4>Connect to ${esc(other)}</h4>
      <p>Pick a handle to register this instance with <strong>${esc(other)}</strong>.
         Have an <strong>invite code</strong>? Enter it and you're approved
         instantly — otherwise your request waits for the owner to accept it.</p>
      <div class="msg-connect-row">
        <input type="text" id="msg-connect-handle" maxlength="32" autocomplete="off"
               placeholder="your-handle" aria-label="Handle" />
      </div>
      <div class="msg-connect-row">
        <input type="text" id="msg-connect-code" maxlength="64" autocomplete="off"
               placeholder="invite code (optional)" aria-label="Invite code" />
        <button type="button" id="msg-connect-btn">Connect</button>
      </div>
      <div class="msg-connect-err" id="msg-connect-err">${errText ? esc(errText) : ''}</div>
    </div>`;
  const input = document.getElementById('msg-connect-handle');
  const codeInput = document.getElementById('msg-connect-code');
  const btn = document.getElementById('msg-connect-btn');
  const go = async () => {
    const handle = (input.value || '').trim().toLowerCase();
    if (!handle) { input.focus(); return; }
    const code = (codeInput.value || '').trim();
    btn.disabled = true;
    btn.textContent = code ? 'Joining…' : 'Connecting…';
    try {
      // A code redeems to instant approval; no code falls back to the classic
      // register-and-wait flow.
      if (code) {
        await _api('/api/homelink/redeem', { method: 'POST', body: JSON.stringify({ handle, code }) });
      } else {
        await _api('/api/homelink/connect', { method: 'POST', body: JSON.stringify({ handle }) });
      }
      openConversation(other);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Connect';
      const err = document.getElementById('msg-connect-err');
      if (err) err.textContent = e.message;
    }
  };
  btn.addEventListener('click', go);
  const onEnter = (e) => { if (e.key === 'Enter') { e.preventDefault(); go(); } };
  input.addEventListener('keydown', onEnter);
  codeInput.addEventListener('keydown', onEnter);
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

// ── Polling ─────────────────────────────────────────────────────────────────
// One reconfigure point instead of scattered start/stop calls. The matrix:
//   SSE healthy + local thread → no polls at all (the stream carries it)
//   Home Link thread           → thread poll at 2s regardless of SSE
//   SSE down                   → thread 1.5s + list 5s until the retry lands

async function _pollThread() {
  if (!_activeOther || !_open || !_threadReady) return;
  try {
    const data = await _api(`/api/messages/conversations/${encodeURIComponent(_activeOther)}?after_id=${_lastMsgId}`);
    if (data.messages && data.messages.length) {
      if (_appendMessages(data.messages)) _scheduleListRefresh();
    }
  } catch (_) { /* transient */ }
}

function _reconfigurePolling() {
  if (_threadPollTimer) { clearInterval(_threadPollTimer); _threadPollTimer = null; }
  if (_listPollTimer) { clearInterval(_listPollTimer); _listPollTimer = null; }
  if (!_open) return;
  if (_activeOther && _threadReady) {
    const ms = _isHomeThread() ? THREAD_POLL_HOME_MS : (_sseHealthy ? 0 : THREAD_POLL_FALLBACK_MS);
    if (ms) _threadPollTimer = setInterval(_pollThread, ms);
  }
  if (!_sseHealthy) {
    _listPollTimer = setInterval(() => { if (_open) _loadConversations().catch(() => {}); }, LIST_POLL_FALLBACK_MS);
  }
}

// ── Sending / editing ───────────────────────────────────────────────────────

async function _sendCurrent() {
  const input = document.getElementById('msg-composer-input');
  if (!input || !_activeOther || _sending) return;
  const body = input.value.trim();
  if (!body) return;
  if (_editingId != null) { _submitEdit(body); return; }
  _sending = true;
  input.value = '';
  input.style.height = 'auto';
  const replyTo = _replyTo;
  try {
    // Encrypt to the peer when the thread supports E2EE and both sides have
    // keys; otherwise send plaintext (the composer footer shows which).
    let outBody = body;
    let encKey = null;
    if (_e2eeEligible() && _e2ee.unlocked) {
      encKey = await _sharedKeyFor(_activeOther);
      if (encKey) outBody = await e2ee.encryptMessage(body, encKey);
    }
    const payload = { body: outBody };
    if (replyTo && replyTo.id != null) payload.reply_to_id = replyTo.id;
    const data = await _api(`/api/messages/conversations/${encodeURIComponent(_activeOther)}`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    // Show my own message as plaintext immediately (the body I hold is the
    // envelope; stash the cleartext so the bubble renders unlocked).
    if (data.message) { if (encKey) data.message._plain = body; _appendMessages([data.message]); }
    if (_replyTo === replyTo) _cancelComposerState();
    _lastTypingSentAt = 0; // a fresh keystroke after sending signals typing again
    _scheduleListRefresh();
  } catch (e) {
    input.value = body; // restore so the user doesn't lose their text
    uiModule.showError && uiModule.showError('Message failed: ' + e.message);
  } finally {
    _sending = false;
    input.focus();
  }
}

async function _submitEdit(body) {
  const id = _editingId;
  if (id == null || _sending) return;
  _sending = true;
  try {
    // Keep an edited message encrypted if the original was.
    let outBody = body;
    const orig = _messages.get(id);
    let encKey = null;
    if (_e2eeEligible() && _e2ee.unlocked && orig && e2ee.isEnvelope(orig.body)) {
      encKey = await _sharedKeyFor(_activeOther);
      if (encKey) outBody = await e2ee.encryptMessage(body, encKey);
    }
    const data = await _api(`/api/messages/msg/${id}`, {
      method: 'PUT',
      body: JSON.stringify({ body: outBody }),
    });
    if (data.message) { if (encKey) data.message._plain = body; _updateRowInPlace(data.message); }
    _cancelComposerState(); // clears the input too — it held the edit text
    _scheduleListRefresh();
  } catch (e) {
    // Keep edit mode and the text so the user can retry or bail with Esc.
    uiModule.showError && uiModule.showError('Edit failed: ' + e.message);
  } finally {
    _sending = false;
    document.getElementById('msg-composer-input')?.focus();
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

// ── Status photos ("Moments") ───────────────────────────────────────────────
// A strip of contacts' latest "what I'm doing" photos above the chat list.
// Backend: routes/status_routes.py (visible only to your DM contacts).

let _moments = [];
let _statusPollTimer = null;
let _viewer = null;                    // {author, posts, idx, mine} while open
const STATUS_POLL_MS = 30000;
const STATUS_MAX_BYTES = 8 * 1024 * 1024;

async function _loadMoments() {
  try {
    const data = await _api('/api/status/feed');
    _moments = data.statuses || [];
    _renderMoments();
  } catch (_) { /* not available — hide the strip */ }
}

function _renderMoments() {
  const host = document.getElementById('msg-moments');
  if (!host) return;
  const add = `<button type="button" class="msg-moment msg-moment-add" id="msg-moment-add"
      title="Share a moment" aria-label="Share a moment">
      <span class="msg-moment-plus">＋</span><span class="msg-moment-name">Add</span></button>`;
  const tiles = _moments.map(a => {
    const ring = a.mine ? 'mine' : (a.has_unseen ? 'unseen' : 'seen');
    const cnt = a.posts.length > 1 ? `<span class="msg-moment-count">${a.posts.length}</span>` : '';
    return `<button type="button" class="msg-moment ${ring}" data-author="${esc(a.author)}" title="${esc(a.author)}">
        <span class="msg-moment-ring">${_avatarHtml(a.author, 'msg-avatar-lg')}</span>
        <span class="msg-moment-name">${a.mine ? 'You' : esc(a.author)}</span>${cnt}
      </button>`;
  }).join('');
  host.innerHTML = add + tiles;
  document.getElementById('msg-moment-add')?.addEventListener('click', _openStatusComposer);
  host.querySelectorAll('.msg-moment[data-author]').forEach(el =>
    el.addEventListener('click', () => _openStatusViewer(el.dataset.author)));
}

function _readDataUrl(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result);
    r.onerror = () => rej(new Error('read failed'));
    r.readAsDataURL(file);
  });
}

function _openStatusComposer() {
  let inp = document.getElementById('msg-status-file');
  if (!inp) {
    inp = document.createElement('input');
    inp.type = 'file'; inp.accept = 'image/png,image/jpeg,image/webp,image/gif';
    inp.capture = 'environment'; inp.id = 'msg-status-file'; inp.style.display = 'none';
    document.body.appendChild(inp);
    inp.addEventListener('change', async () => {
      const f = inp.files && inp.files[0];
      inp.value = '';
      if (!f) return;
      if (f.size > STATUS_MAX_BYTES) {
        uiModule.showError && uiModule.showError('Image too large (max 8 MB)');
        return;
      }
      let dataUrl;
      try { dataUrl = await _readDataUrl(f); } catch (_) { return; }
      const caption = await uiModule.styledPrompt(
        'Add a caption — what are you up to? (optional)',
        { title: 'Share a moment', placeholder: 'caption', confirmText: 'Share', maxLength: 280 });
      if (caption === null) return;   // cancelled the whole post
      try {
        await _api('/api/status/post', { method: 'POST',
          body: JSON.stringify({ image: dataUrl, caption }) });
        uiModule.showToast && uiModule.showToast('Moment shared');
        _loadMoments();
      } catch (e) {
        uiModule.showError && uiModule.showError('Could not share: ' + e.message);
      }
    });
  }
  inp.click();
}

function _openStatusViewer(author) {
  const a = _moments.find(x => x.author === author);
  if (!a || !a.posts.length) return;
  _viewer = { author, posts: a.posts.slice(), idx: 0, mine: a.mine };
  _renderStatusViewer();
}

async function _renderStatusViewer() {
  const s = _viewer;
  if (!s) return;
  let ov = document.getElementById('msg-status-viewer');
  if (!ov) {
    ov = document.createElement('div');
    ov.id = 'msg-status-viewer';
    ov.className = 'msg-status-viewer';
    document.body.appendChild(ov);
  }
  ov.style.zIndex = String(topPortalZ());
  const post = s.posts[s.idx];
  ov.innerHTML = `
    <div class="msg-sv-backdrop"></div>
    <div class="msg-sv-card">
      <div class="msg-sv-progress">${s.posts.map((p, i) =>
        `<span class="${i < s.idx ? 'done' : (i === s.idx ? 'on' : '')}"></span>`).join('')}</div>
      <div class="msg-sv-head">
        ${_avatarHtml(s.author, '')}
        <span class="msg-sv-author">${s.mine ? 'You' : esc(s.author)}</span>
        <span class="msg-sv-time">${esc(_fmtTime(post.created_at))}</span>
        <span style="flex:1"></span>
        ${s.mine ? '<button type="button" class="msg-sv-del" title="Delete">🗑</button>' : ''}
        <button type="button" class="msg-sv-close" title="Close" aria-label="Close">✕</button>
      </div>
      <div class="msg-sv-imgwrap">
        <div class="msg-sv-loading">Loading…</div>
        <img class="msg-sv-img" alt="" style="display:none" />
      </div>
      ${post.caption ? `<div class="msg-sv-caption">${esc(post.caption)}</div>` : ''}
      <button type="button" class="msg-sv-nav msg-sv-prev" aria-label="Previous">‹</button>
      <button type="button" class="msg-sv-nav msg-sv-next" aria-label="Next">›</button>
    </div>`;
  ov.querySelector('.msg-sv-close')?.addEventListener('click', _closeStatusViewer);
  ov.querySelector('.msg-sv-backdrop')?.addEventListener('click', _closeStatusViewer);
  ov.querySelector('.msg-sv-prev')?.addEventListener('click', () => _stepViewer(-1));
  ov.querySelector('.msg-sv-next')?.addEventListener('click', () => _stepViewer(1));
  ov.querySelector('.msg-sv-del')?.addEventListener('click', () => _deleteStatus(post.id));

  try {
    const data = await _api(`/api/status/${post.id}/image`);
    // Ignore if the viewer moved on while we were fetching.
    if (!_viewer || _viewer.posts[_viewer.idx].id !== post.id) return;
    const img = ov.querySelector('.msg-sv-img');
    if (img) { img.src = data.image; img.style.display = ''; ov.querySelector('.msg-sv-loading')?.remove(); }
  } catch (_) {
    const l = ov.querySelector('.msg-sv-loading');
    if (l) l.textContent = 'Could not load';
  }
  if (!s.mine) {
    _api(`/api/status/${post.id}/seen`, { method: 'POST' })
      .then(() => { post.seen = true; }).catch(() => {});
  }
}

function _stepViewer(d) {
  const s = _viewer;
  if (!s) return;
  const n = s.idx + d;
  if (n < 0) { _closeStatusViewer(); return; }
  if (n >= s.posts.length) { _closeStatusViewer(); return; }
  s.idx = n;
  _renderStatusViewer();
}

function _closeStatusViewer() {
  _viewer = null;
  document.getElementById('msg-status-viewer')?.remove();
  _loadMoments();   // refresh unseen rings
}

async function _deleteStatus(id) {
  const ok = await uiModule.styledConfirm('Delete this moment?', { confirmText: 'Delete', danger: true });
  if (!ok) return;
  try { await _api(`/api/status/${id}`, { method: 'DELETE' }); } catch (_) {}
  _closeStatusViewer();
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
          <div class="msg-profile-bar" id="msg-profile-bar"></div>
          <div class="msg-moments-section">
            <div class="msg-moments-label">Moments <span>· share what you're up to</span></div>
            <div class="msg-moments" id="msg-moments"></div>
          </div>
          <div class="msg-list-search-wrap">
            <input type="text" id="msg-list-search" placeholder="Search chats…" autocomplete="off" aria-label="Search conversations" />
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
          <div class="msg-composer-bar hidden" id="msg-composer-bar"></div>
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

  const search = document.getElementById('msg-list-search');
  if (search) {
    search.addEventListener('input', () => {
      _listFilter = search.value.trim().toLowerCase();
      _renderConversationList();
    });
  }

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
      _maybeSendTyping();
    });
  }

  // Per-message actions are delegated on the thread body so they survive
  // re-renders: click (⋯ button, reaction chips, reply quotes), contextmenu
  // (desktop right-click + Android long-press), and an explicit long-press
  // timer for iOS Safari, which never fires contextmenu.
  const threadBody = document.getElementById('msg-thread-body');
  const msgFromNode = (node) => {
    const row = node.closest('.msg-bubble-row');
    return row ? _messages.get(Number(row.dataset.id)) : null;
  };
  threadBody.addEventListener('click', (e) => {
    const chip = e.target.closest('.msg-react-chip');
    if (chip) {
      const m = msgFromNode(chip);
      if (m && !_isHomeThread()) _toggleReaction(m, chip.dataset.emoji);
      return;
    }
    const quote = e.target.closest('.msg-quote');
    if (quote) { _jumpToMessage(Number(quote.dataset.quoteId)); return; }
    const act = e.target.closest('.msg-act-btn');
    if (act) {
      const m = msgFromNode(act);
      if (m) {
        const r = act.getBoundingClientRect();
        _openMessageMenu(m, r.left, r.bottom + 4);
      }
    }
  });
  threadBody.addEventListener('contextmenu', (e) => {
    const row = e.target.closest('.msg-bubble-row');
    if (!row) return;
    // A live text selection means the user wants the native copy menu.
    if (String(window.getSelection ? window.getSelection() : '')) return;
    const m = msgFromNode(row);
    if (!m || m.deleted) return;
    e.preventDefault();
    _openMessageMenu(m, e.clientX, e.clientY);
  });
  let lpTimer = null;
  threadBody.addEventListener('touchstart', (e) => {
    const row = e.target.closest('.msg-bubble-row');
    if (!row || e.touches.length !== 1) return;
    const t = e.touches[0];
    const sx = t.clientX, sy = t.clientY;
    const cleanup = () => {
      threadBody.removeEventListener('touchmove', onMove);
      threadBody.removeEventListener('touchend', cancel);
      threadBody.removeEventListener('touchcancel', cancel);
    };
    const cancel = () => {
      if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; }
      cleanup();
    };
    const onMove = (ev) => {
      const tt = ev.touches[0];
      if (Math.abs(tt.clientX - sx) > 10 || Math.abs(tt.clientY - sy) > 10) cancel();
    };
    lpTimer = setTimeout(() => {
      lpTimer = null;
      const m = msgFromNode(row);
      if (m && !m.deleted) _openMessageMenu(m, sx, sy);
    }, 500);
    threadBody.addEventListener('touchmove', onMove, { passive: true });
    threadBody.addEventListener('touchend', cancel);
    threadBody.addEventListener('touchcancel', cancel);
  }, { passive: true });

  modal.addEventListener('click', (e) => {
    if (uiModule.isTouchInsideModal && uiModule.isTouchInsideModal()) return;
    if (e.target === modal) close();
  });

  _escHandler = (e) => {
    if (e.key !== 'Escape') return;
    // Escape peels one layer at a time: context menu → reply/edit state →
    // new-chat overlay → mobile thread → whole modal. The first two live in
    // the shared escMenuStack, so the global arbiter in ui.js usually
    // consumes them before this handler runs; the re-checks here keep the
    // order intact when the event reaches us directly (the arbiter defers
    // to focused text inputs, i.e. exactly when the composer has focus).
    if (_viewer) { _closeStatusViewer(); return; }
    if (_menuEl) { _closeMessageMenu(); return; }
    if (_editingId != null || _replyTo) { _cancelComposerState(); return; }
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
  _listFilter = '';
  _typingPeers.clear();
  _buildModal();
  document.getElementById('tool-messages-btn')?.classList.add('active');
  _loadProfile();
  _loadConversations().catch((e) => {
    const list = document.getElementById('msg-convo-list');
    if (list) list.innerHTML = `<div class="msg-thread-loading">${esc(e.message)}</div>`;
  });
  // Polling starts in fallback mode and stands down when the stream opens.
  _reconfigurePolling();
  _connectSSE();
  // Status photos strip: load now, refresh slowly (they're ephemeral, not live).
  _loadMoments();
  if (_statusPollTimer) clearInterval(_statusPollTimer);
  _statusPollTimer = setInterval(() => { if (_open && !_viewer) _loadMoments(); }, STATUS_POLL_MS);
}

export function close() {
  if (!_open) return;
  _open = false;
  _disconnectSSE();
  _closeMessageMenu();
  _cancelComposerState();
  _hideTypingIndicator();
  _stopPendingCheck();
  if (_threadPollTimer) { clearInterval(_threadPollTimer); _threadPollTimer = null; }
  if (_listPollTimer) { clearInterval(_listPollTimer); _listPollTimer = null; }
  if (_listRefreshTimer) { clearTimeout(_listRefreshTimer); _listRefreshTimer = null; }
  if (_statusPollTimer) { clearInterval(_statusPollTimer); _statusPollTimer = null; }
  _viewer = null;
  document.getElementById('msg-status-viewer')?.remove();
  _typingPeers.clear();
  _messages.clear();
  _lastRenderedMsg = null;
  _activeOther = null;
  _activeOtherMeta = null;
  _threadReady = false;
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
