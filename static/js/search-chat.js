// Global Search — Ctrl+K across conversations and the owner-scoped Life graph.

import uiModule from './ui.js';
import sessionModule from './sessions.js';

let API_BASE = '';
let debounceTimer = null;
let selectedIndex = -1;
let results = [];
let searchSequence = 0;

function el(id) { return document.getElementById(id); }

export function openSearch() {
  const overlay = el('search-overlay');
  if (!overlay) return;
  const wasOpen = !overlay.classList.contains('hidden');
  overlay.classList.remove('hidden');
  const input = el('search-input');
  if (input) {
    input.value = '';
    input.focus();
  }
  selectedIndex = -1;
  results = [];
  searchSequence++;
  if (el('search-results')) el('search-results').innerHTML = '';
  if (!wasOpen) document.dispatchEvent(new CustomEvent('restia:search-opened'));
}

export function closeSearch() {
  const overlay = el('search-overlay');
  if (!overlay) return;
  const wasOpen = !overlay.classList.contains('hidden');
  overlay.classList.add('hidden');
  searchSequence++;
  if (el('search-results')) el('search-results').innerHTML = '';
  selectedIndex = -1;
  results = [];
  if (wasOpen) document.dispatchEvent(new CustomEvent('restia:search-closed'));
}

export function isOpen() {
  const overlay = el('search-overlay');
  return overlay && !overlay.classList.contains('hidden');
}

var escapeHtml = uiModule.esc;

function highlightMatch(text, query) {
  if (!query) return escapeHtml(text);
  const escaped = escapeHtml(text);
  const regex = new RegExp('(' + query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
  return escaped.replace(regex, '<mark class="search-highlight">$1</mark>');
}

function formatTimestamp(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const diff = now - d;
  if (diff < 86400000) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  if (diff < 604800000) {
    return d.toLocaleDateString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  }
  return d.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
}

function lifeResults(payload) {
  return (Array.isArray(payload?.items) ? payload.items : []).flatMap((row) => {
    const entity = row?.entity || {};
    const id = String(entity.id || '').trim();
    if (!id) return [];
    return [{
      kind: 'life',
      id,
      title: String(entity.title || entity.entity_type || 'Life record'),
      contentSnippet: String(entity.summary || entity.entity_type || 'Life record'),
      label: String(entity.entity_type || 'record').replace(/_/g, ' '),
      timestamp: entity.updated_at || entity.occurred_at || '',
    }];
  });
}

function chatResults(data) {
  return (Array.isArray(data) ? data : []).flatMap((row) => {
    const id = String(row?.session_id || '').trim();
    if (!id) return [];
    return [{
      kind: 'chat',
      id,
      title: String(row.session_name || 'Conversation'),
      contentSnippet: String(row.content_snippet || ''),
      label: row.role === 'user' ? 'You' : 'AI',
      timestamp: row.timestamp || '',
    }];
  });
}

function renderResults(chatData, lifeData, query, { failed = false } = {}) {
  const chats = chatResults(chatData);
  const life = lifeResults(lifeData);
  results = [...chats, ...life];
  selectedIndex = -1;
  const container = el('search-results');
  if (!container) return;

  if (results.length === 0) {
    container.innerHTML = query
      ? `<div class="search-empty" role="status">${failed ? 'Search is temporarily unavailable' : 'No results found'}</div>`
      : '';
    return;
  }

  // Keep chat hits grouped by session, then show canonical Life records.
  const grouped = {};
  for (const item of chats) {
    if (!grouped[item.id]) {
      grouped[item.id] = { name: item.title, items: [] };
    }
    grouped[item.id].items.push(item);
  }

  let html = '';
  let idx = 0;
  for (const [sessionId, group] of Object.entries(grouped)) {
    html += `<div class="search-group-header">Chats · ${escapeHtml(group.name)}</div>`;
    for (const item of group.items) {
      html += `<button type="button" class="search-result-item" data-index="${idx}" data-result-kind="chat" data-result-id="${escapeHtml(sessionId)}">
        <span class="search-result-role">${escapeHtml(item.label)}</span>
        <span class="search-result-snippet">${highlightMatch(item.contentSnippet, query)}</span>
        <span class="search-result-time">${formatTimestamp(item.timestamp)}</span>
      </button>`;
      idx++;
    }
  }
  if (life.length) html += '<div class="search-group-header">Life</div>';
  for (const item of life) {
    html += `<button type="button" class="search-result-item" data-index="${idx}" data-result-kind="life" data-result-id="${escapeHtml(item.id)}">
      <span class="search-result-role">${escapeHtml(item.label)}</span>
      <span class="search-result-snippet"><strong>${highlightMatch(item.title, query)}</strong>${item.contentSnippet && item.contentSnippet !== item.title ? ` · ${highlightMatch(item.contentSnippet, query)}` : ''}</span>
      <span class="search-result-time">${formatTimestamp(item.timestamp)}</span>
    </button>`;
    idx++;
  }
  container.innerHTML = html;

  // Click handlers
  container.querySelectorAll('.search-result-item').forEach(item => {
    item.addEventListener('click', () => activateResult(item));
  });
}

function navigateToSession(sessionId) {
  closeSearch();
  if (sessionModule && sessionModule.selectSession) {
    sessionModule.selectSession(sessionId);
  }
}

async function navigateToLife(entityId) {
  closeSearch();
  if (typeof window !== 'undefined' && typeof window.activateNavigationItem === 'function') {
    await window.activateNavigationItem('life');
  }
  if (typeof window !== 'undefined'
      && typeof window.lifeWorkspaceModule?.revealLifeEntity === 'function') {
    await window.lifeWorkspaceModule.revealLifeEntity(entityId);
    return;
  }
  document.dispatchEvent(new CustomEvent('restia:life-search-result-selected', {
    detail: { entityId },
  }));
}

function activateResult(item) {
  if (!item) return;
  if (item.dataset.resultKind === 'life') void navigateToLife(item.dataset.resultId);
  else navigateToSession(item.dataset.resultId);
}

function updateSelection() {
  const container = el('search-results');
  if (!container) return;
  const items = container.querySelectorAll('.search-result-item');
  items.forEach((item, i) => {
    item.classList.toggle('selected', i === selectedIndex);
  });
  // Scroll selected into view
  if (selectedIndex >= 0 && items[selectedIndex]) {
    items[selectedIndex].scrollIntoView({ block: 'nearest' });
  }
}

function handleKeydown(e) {
  if (!isOpen()) return;

  const container = el('search-results');
  const items = container ? container.querySelectorAll('.search-result-item') : [];
  const count = items.length;

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    selectedIndex = count > 0 ? Math.min(selectedIndex + 1, count - 1) : -1;
    updateSelection();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    selectedIndex = Math.max(selectedIndex - 1, 0);
    updateSelection();
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (selectedIndex >= 0 && items[selectedIndex]) {
      activateResult(items[selectedIndex]);
    }
  }
}

function handleInput(e) {
  const query = e.target.value.trim();
  if (debounceTimer) clearTimeout(debounceTimer);

  if (!query) {
    searchSequence++;
    renderResults([], { items: [] }, '');
    return;
  }

  debounceTimer = setTimeout(async () => {
    const sequence = ++searchSequence;
    try {
      const readJson = async (url) => {
        const response = await fetch(url, { credentials: 'same-origin' });
        if (!response.ok) throw new Error(`Search request failed (${response.status})`);
        return response.json();
      };
      const encoded = encodeURIComponent(query);
      const [chatResponse, lifeResponse] = await Promise.allSettled([
        readJson(`${API_BASE}/api/search?q=${encoded}&limit=20`),
        readJson(`${API_BASE}/api/life/search?q=${encoded}&limit=20`),
      ]);
      if (sequence !== searchSequence) return;
      const chatData = chatResponse.status === 'fulfilled' ? chatResponse.value : [];
      const lifeData = lifeResponse.status === 'fulfilled' ? lifeResponse.value : { items: [] };
      renderResults(chatData, lifeData, query, {
        failed: chatResponse.status === 'rejected' && lifeResponse.status === 'rejected',
      });
    } catch (err) {
      console.error('Search error:', err);
      if (sequence === searchSequence) renderResults([], { items: [] }, query, { failed: true });
    }
  }, 300);
}

export function init(apiBase) {
  API_BASE = apiBase || '';

  const input = el('search-input');
  if (input) {
    input.addEventListener('input', handleInput);
    input.addEventListener('keydown', handleKeydown);
  }

  // Close on overlay click (not popup click)
  const overlay = el('search-overlay');
  if (overlay) {
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) closeSearch();
    });
  }
}

const searchChatModule = {
  init,
  openSearch,
  closeSearch,
  isOpen,
  __test: { chatResults, lifeResults },
};

export default searchChatModule;
