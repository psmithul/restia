// static/js/commandPalette.js
//
// A global command palette (⌘K / Ctrl+K) — the keyboard-first launcher that
// ties every tool and quick action together, the way a desktop OS does.
//
// Commands are data-driven. A command either clicks an existing trigger button
// (so it rides whatever wiring that button already has) or runs a function.
// Commands whose trigger button isn't in the DOM are filtered out, so the
// palette always reflects exactly what this instance actually offers.

import uiModule from './ui.js';
import { topPortalZ } from './toolWindowZOrder.js';

const RECENTS_KEY = 'restia.palette.recents';
const MAX_RECENTS = 6;

let _open = false;
let _overlay = null;
let _items = [];          // current filtered command list
let _active = 0;          // highlighted index
let _commands = null;     // built lazily on first open

// ── Command registry ────────────────────────────────────────────────────────
// btn: id of a button to click. run: custom action. after: id to click shortly
// after opening (for "open tool → do thing" composites).

// Each command lists candidate trigger button ids in priority order. The rail
// buttons (visible when the sidebar is collapsed) and the sidebar tool buttons
// (visible when expanded) are BOTH listed; run() clicks whichever is currently
// visible, so the palette works at any width.
function _spec() {
  return [
    // Tools
    { id: 'messages', icon: '💬', title: 'Messages', hint: 'Open chats', btns: ['tool-messages-btn', 'rail-messages'], keys: 'dm chat talk' },
    { id: 'notes', icon: '📝', title: 'Notes', hint: 'Open notes', btns: ['tool-notes-btn', 'rail-notes'], keys: 'note memo' },
    { id: 'tasks', icon: '✅', title: 'Tasks', hint: 'Open tasks', btns: ['tool-tasks-btn', 'rail-tasks'], keys: 'task agent scheduled' },
    { id: 'todos', icon: '☑️', title: 'Reminders & Todos', hint: 'Open todos', btns: ['tool-todos-btn', 'rail-todos'], keys: 'todo reminder' },
    { id: 'calendar', icon: '📅', title: 'Calendar', hint: 'Open calendar', btns: ['tool-calendar-btn', 'rail-calendar'], keys: 'events schedule caldav' },
    { id: 'documents', icon: '📄', title: 'Documents', hint: 'Open library', btns: ['tool-library-btn', 'rail-documents'], keys: 'docs editor writing' },
    { id: 'gallery', icon: '🖼️', title: 'Gallery', hint: 'Open gallery', btns: ['tool-gallery-btn', 'rail-gallery'], keys: 'images photos pictures' },
    { id: 'research', icon: '🔬', title: 'Deep Research', hint: 'Open research', btns: ['tool-research-btn', 'rail-research'], keys: 'research web report' },
    { id: 'compare', icon: '⚖️', title: 'Compare Models', hint: 'Open compare', btns: ['tool-compare-btn', 'rail-compare'], keys: 'compare models test' },
    { id: 'cookbook', icon: '📓', title: 'Cookbook', hint: 'Models & serving', btns: ['tool-cookbook-btn', 'rail-cookbook'], keys: 'cookbook models download serve' },
    { id: 'memory', icon: '🧠', title: 'Memory', hint: 'Open memory', btns: ['tool-memory-btn', 'rail-memory'], keys: 'memory facts recall' },
    { id: 'email', icon: '✉️', title: 'Email', hint: 'Open inbox', btns: ['rail-email', 'email-section-title'], keys: 'mail inbox imap' },
    { id: 'settings', icon: '⚙️', title: 'Settings', hint: 'Open settings', btns: ['rail-settings', 'user-bar-settings'], keys: 'settings preferences config' },
    { id: 'search', icon: '🔎', title: 'Search Chats', hint: 'Search your chats', btns: ['rail-search-btn'], keys: 'find search history' },
    // Actions
    { id: 'new-chat', icon: '✨', title: 'New Chat', hint: 'Start a fresh session', btns: ['rail-new-session'], keys: 'new session conversation' },
    { id: 'new-message', icon: '📨', title: 'New Message', hint: 'Message someone', btns: ['tool-messages-btn', 'rail-messages'], after: 'msg-newchat-btn', keys: 'dm new message compose' },
    { id: 'share-moment', icon: '📸', title: 'Share a Moment', hint: 'Post a status photo', btns: ['tool-messages-btn', 'rail-messages'], after: 'msg-moment-add', keys: 'moment status photo bereal' },
    { id: 'theme', icon: '🌗', title: 'Toggle Theme', hint: 'Light / dark', btns: ['tool-theme-btn', 'rail-theme'], keys: 'theme dark light appearance' },
  ];
}

function _isClickable(el) {
  if (!el) return false;
  if (el.disabled || el.closest('.hidden,[hidden]')) return false;
  const cs = getComputedStyle(el);
  if (cs.display === 'none' || cs.visibility === 'hidden' || cs.pointerEvents === 'none') return false;
  return el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0;
}

// The trigger to click: the first VISIBLE candidate, else the first that
// merely exists (best effort). Returns null if none exist.
function _pickTrigger(ids) {
  const present = ids.map(id => document.getElementById(id)).filter(Boolean);
  return present.find(_isClickable) || present[0] || null;
}

function _buildCommands() {
  const out = [];
  for (const c of _spec()) {
    const ids = c.btns || [];
    if (!ids.some(id => document.getElementById(id)) && !c.run) continue;  // unavailable here
    out.push({
      ...c,
      run: c.run || (() => {
        const trigger = _pickTrigger(ids);
        if (trigger) trigger.click();
        if (c.after) {
          // Give the tool a beat to render, then fire the follow-up control.
          [120, 320, 600].forEach(t => setTimeout(() => {
            const a = document.getElementById(c.after);
            if (a && !a._paletteFired) {
              a._paletteFired = true; a.click();
              setTimeout(() => { a._paletteFired = false; }, 800);
            }
          }, t));
        }
      }),
    });
  }
  return out;
}

// ── Fuzzy match ─────────────────────────────────────────────────────────────
// Subsequence match with light scoring: exact/prefix hits rank first.

function _score(cmd, q) {
  if (!q) return 0;
  const hay = (cmd.title + ' ' + (cmd.keys || '')).toLowerCase();
  const title = cmd.title.toLowerCase();
  if (title.startsWith(q)) return 1000 - title.length;
  if (title.includes(q)) return 700 - title.indexOf(q);
  if (hay.includes(q)) return 400 - hay.indexOf(q);
  // subsequence
  let i = 0;
  for (const ch of hay) { if (ch === q[i]) i++; if (i === q.length) break; }
  return i === q.length ? 100 : -1;
}

function _filter(q) {
  q = (q || '').trim().toLowerCase();
  if (!q) {
    const recents = _loadRecents();
    const byId = Object.fromEntries(_commands.map(c => [c.id, c]));
    const top = recents.map(id => byId[id]).filter(Boolean);
    const rest = _commands.filter(c => !recents.includes(c.id));
    return [...top, ...rest];
  }
  return _commands
    .map(c => ({ c, s: _score(c, q) }))
    .filter(x => x.s >= 0)
    .sort((a, b) => b.s - a.s)
    .map(x => x.c);
}

function _loadRecents() {
  try { return JSON.parse(localStorage.getItem(RECENTS_KEY) || '[]'); } catch (_) { return []; }
}
function _pushRecent(id) {
  try {
    const r = _loadRecents().filter(x => x !== id);
    r.unshift(id);
    localStorage.setItem(RECENTS_KEY, JSON.stringify(r.slice(0, MAX_RECENTS)));
  } catch (_) {}
}

// ── Render ──────────────────────────────────────────────────────────────────

const esc = uiModule.esc;

function _render(q) {
  _items = _filter(q);
  if (_active >= _items.length) _active = Math.max(0, _items.length - 1);
  const list = _overlay.querySelector('.cmdp-list');
  if (!_items.length) {
    list.innerHTML = `<div class="cmdp-empty">No commands match “${esc(q)}”.</div>`;
    return;
  }
  const showingRecents = !q.trim() && _loadRecents().length;
  list.innerHTML = (showingRecents ? '<div class="cmdp-section">Recent</div>' : '') +
    _items.map((c, i) => `
      <div class="cmdp-item${i === _active ? ' active' : ''}" data-i="${i}" role="option" aria-selected="${i === _active}">
        <span class="cmdp-icon">${c.icon || '•'}</span>
        <span class="cmdp-title">${esc(c.title)}</span>
        <span class="cmdp-hint">${esc(c.hint || '')}</span>
      </div>`).join('');
  list.querySelectorAll('.cmdp-item').forEach(el => {
    el.addEventListener('mousemove', () => { _active = Number(el.dataset.i); _paintActive(); });
    el.addEventListener('click', () => _exec(Number(el.dataset.i)));
  });
  _scrollActiveIntoView();
}

function _paintActive() {
  _overlay.querySelectorAll('.cmdp-item').forEach(el => {
    const on = Number(el.dataset.i) === _active;
    el.classList.toggle('active', on);
    el.setAttribute('aria-selected', on ? 'true' : 'false');
  });
}

function _scrollActiveIntoView() {
  _overlay.querySelector('.cmdp-item.active')?.scrollIntoView({ block: 'nearest' });
}

function _exec(i) {
  const cmd = _items[i];
  if (!cmd) return;
  _pushRecent(cmd.id);
  close();
  try { cmd.run(); } catch (e) { uiModule.showError && uiModule.showError('Command failed: ' + e.message); }
}

// ── Lifecycle ────────────────────────────────────────────────────────────────

export function open() {
  if (_open) return;
  _open = true;
  _commands = _buildCommands();
  _active = 0;
  _overlay = document.createElement('div');
  _overlay.className = 'cmdp-overlay';
  _overlay.style.zIndex = String(topPortalZ());
  _overlay.innerHTML = `
    <div class="cmdp-backdrop"></div>
    <div class="cmdp-panel" role="combobox" aria-expanded="true" aria-haspopup="listbox">
      <input type="text" class="cmdp-input" id="cmdp-input" placeholder="Type a command or search…"
             autocomplete="off" spellcheck="false" aria-label="Command palette" />
      <div class="cmdp-list" role="listbox"></div>
      <div class="cmdp-footer"><span><kbd>↑</kbd><kbd>↓</kbd> navigate</span><span><kbd>↵</kbd> run</span><span><kbd>esc</kbd> close</span></div>
    </div>`;
  document.body.appendChild(_overlay);
  const input = _overlay.querySelector('#cmdp-input');
  _render('');
  input.addEventListener('input', () => { _active = 0; _render(input.value); });
  input.addEventListener('keydown', _onKey);
  _overlay.querySelector('.cmdp-backdrop').addEventListener('click', close);
  setTimeout(() => input.focus(), 20);
}

function _onKey(e) {
  if (e.key === 'ArrowDown') { e.preventDefault(); _active = Math.min(_items.length - 1, _active + 1); _paintActive(); _scrollActiveIntoView(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); _active = Math.max(0, _active - 1); _paintActive(); _scrollActiveIntoView(); }
  else if (e.key === 'Enter') { e.preventDefault(); _exec(_active); }
  else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
  else if (e.key === 'Tab') { e.preventDefault(); const d = e.shiftKey ? -1 : 1;
    _active = (_active + d + _items.length) % _items.length; _paintActive(); _scrollActiveIntoView(); }
}

export function close() {
  if (!_open) return;
  _open = false;
  _overlay?.remove();
  _overlay = null;
}

export function toggle() { _open ? close() : open(); }
export function isOpen() { return _open; }

export function init() {
  document.addEventListener('keydown', (e) => {
    // ⌘K / Ctrl+K anywhere. Ignore the browser's own combos with shift/alt.
    if ((e.metaKey || e.ctrlKey) && !e.shiftKey && !e.altKey && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      toggle();
    }
  });
}

const commandPaletteModule = { init, open, close, toggle, isOpen };
export default commandPaletteModule;
