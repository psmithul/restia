// Restia V2 adaptive navigation shell.
//
// The existing launch controls remain the behavioural source of truth: this
// module moves those exact nodes into a grouped information architecture
// before app.js binds listeners. That keeps every legacy integration working
// while giving desktop, rail, mobile, and command surfaces one registry.

import missionControlModule from './missionControl.js';
import lifeWorkspaceModule from './lifeWorkspace.js';
import { closeSidebar, SIDEBAR_STATES } from './sidebar-layout.js';
import {
  NAVIGATION_ITEMS,
  PRIMARY_NAVIGATION_IDS,
  findNavigationItemByLegacyId,
  findNavigationItemByModalId,
  findNavigationItemByRoute,
  getLegacyTriggerIds,
  getNavigationItem,
} from './navigation-registry.js';

const GROUP_STATE_KEY = 'restia.v2.navigation.groups';
const SEMANTIC_BUTTON_IDS = Object.freeze([
  'sidebar-new-chat-btn', 'sidebar-search-btn', 'tool-memory-btn',
  'tool-messages-btn', 'tool-calendar-btn', 'tool-compare-btn',
  'tool-cookbook-btn', 'tool-research-btn', 'tool-gallery-btn',
  'tool-notes-btn', 'tool-inbox-btn', 'tool-todos-btn', 'tool-tasks-btn', 'tool-theme-btn',
]);

const RAIL_PRIMARY = new Set([
  'rail-restia', 'rail-home', 'rail-inbox', 'rail-life', 'rail-search-btn',
]);

let initialized = false;
const replayingControls = new WeakSet();
const SHELL_ONLY_ACTIONS = new Set(['toggle-sidebar']);
const navigationModalObservers = new WeakMap();
let modalDomObserver = null;
let modalSyncFrame = 0;

function readGroupState() {
  try {
    const value = JSON.parse(localStorage.getItem(GROUP_STATE_KEY) || '{}');
    return value && typeof value === 'object' ? value : {};
  } catch (_) {
    return {};
  }
}

function writeGroupState(value) {
  try { localStorage.setItem(GROUP_STATE_KEY, JSON.stringify(value)); } catch (_) {}
}

function ensureStylesheet() {
  if (document.getElementById('v2-shell-css')) return;
  const link = document.createElement('link');
  link.id = 'v2-shell-css';
  link.rel = 'stylesheet';
  link.href = '/static/v2-shell.css';
  document.head.appendChild(link);
}

function replaceWithButton(id) {
  const current = document.getElementById(id);
  if (!current || current.tagName === 'BUTTON') return current;
  const button = document.createElement('button');
  Array.from(current.attributes).forEach((attribute) => {
    button.setAttribute(attribute.name, attribute.value);
  });
  button.type = 'button';
  while (current.firstChild) button.appendChild(current.firstChild);
  current.replaceWith(button);
  return button;
}

function icon(path) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '16');
  svg.setAttribute('height', '16');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '2');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  svg.setAttribute('aria-hidden', 'true');
  path.forEach((definition) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', definition.tag || 'path');
    Object.entries(definition).forEach(([key, value]) => {
      if (key !== 'tag') node.setAttribute(key, value);
    });
    svg.appendChild(node);
  });
  return svg;
}

function destinationButton(id, label, iconNodes) {
  const button = document.createElement('button');
  button.type = 'button';
  button.id = id;
  button.className = 'list-item v2-destination';
  button.append(iconNodes, Object.assign(document.createElement('span'), {
    className: 'grow', textContent: label,
  }));
  return button;
}

function railButton(id, label, iconNodes) {
  const button = document.createElement('button');
  button.type = 'button';
  button.id = id;
  button.className = 'icon-rail-btn v2-rail-destination';
  button.title = label;
  button.setAttribute('aria-label', label);
  button.appendChild(iconNodes);
  return button;
}

function ensureNewDestinations(container) {
  const restiaItem = getNavigationItem('chat');
  let restia = document.getElementById('v3-restia-nav');
  if (!restia) {
    restia = destinationButton('v3-restia-nav', restiaItem?.label || 'Restia', icon([
      { d: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z' },
    ]));
    container.appendChild(restia);
  }
  const homeItem = getNavigationItem('home');
  let home = document.getElementById('v2-home-nav');
  if (!home) {
    home = destinationButton('v2-home-nav', homeItem?.label || 'Today', icon([
      { d: 'M3 11.5 12 4l9 7.5' }, { d: 'M5.5 10v10h13V10' }, { d: 'M9.5 20v-6h5v6' },
    ]));
    container.appendChild(home);
  }
  const lifeItem = getNavigationItem('life');
  let life = document.getElementById('v3-life-nav');
  if (!life) {
    life = destinationButton('v3-life-nav', lifeItem?.label || 'Life', icon([
      { tag: 'circle', cx: '12', cy: '5', r: '2' },
      { tag: 'circle', cx: '6', cy: '17', r: '2' },
      { tag: 'circle', cx: '18', cy: '17', r: '2' },
      { d: 'M12 7v4M12 11 6 15M12 11l6 4' },
    ]));
    container.appendChild(life);
  }
  let activity = document.getElementById('v2-activity-nav');
  if (!activity) {
    activity = destinationButton('v2-activity-nav', 'Activity', icon([
      { tag: 'path', d: 'M4 19V9' }, { tag: 'path', d: 'M10 19V5' },
      { tag: 'path', d: 'M16 19v-7' }, { tag: 'path', d: 'M22 19H2' },
    ]));
    container.appendChild(activity);
  }
  return { restia, home, life, activity };
}

function moveThemeToSettings() {
  const themeButton = document.getElementById('tool-theme-btn');
  const appearance = document.querySelector('[data-settings-panel="appearance"]');
  if (!themeButton || !appearance || document.getElementById('v2-theme-settings-card')) return;
  const card = document.createElement('div');
  card.id = 'v2-theme-settings-card';
  card.className = 'admin-card v2-theme-settings-card';
  const heading = document.createElement('h2');
  heading.textContent = 'Theme';
  const copy = document.createElement('p');
  copy.className = 'admin-toggle-sub';
  copy.textContent = 'Browse themes, tune colors, typography, density, and backgrounds.';
  themeButton.classList.add('v2-open-theme-settings');
  themeButton.querySelector('.grow')?.replaceChildren('Open theme studio');
  card.append(heading, copy, themeButton);
  appearance.prepend(card);
}

function groupElement(group, nodes, savedState) {
  const wrapper = document.createElement('section');
  wrapper.className = 'v2-nav-group';
  wrapper.dataset.navGroup = group.id;
  const heading = document.createElement('button');
  heading.type = 'button';
  heading.className = 'v2-nav-group-heading';
  heading.setAttribute('aria-expanded', savedState[group.id] === false ? 'false' : 'true');
  heading.innerHTML = `<span>${group.label}</span><svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true"><path d="m8 10 4 4 4-4" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  const panel = document.createElement('div');
  panel.className = 'v2-nav-group-panel';
  panel.id = `v2-nav-group-${group.id}`;
  heading.setAttribute('aria-controls', panel.id);
  const expanded = savedState[group.id] !== false;
  wrapper.classList.toggle('is-collapsed', !expanded);
  panel.hidden = !expanded;
  nodes.filter(Boolean).forEach((node) => {
    node.setAttribute('draggable', 'false');
    if (node.classList.contains('section')) node.classList.add('v2-nested-section');
    panel.appendChild(node);
  });
  heading.addEventListener('click', () => {
    const next = heading.getAttribute('aria-expanded') !== 'true';
    heading.setAttribute('aria-expanded', String(next));
    panel.hidden = !next;
    wrapper.classList.toggle('is-collapsed', !next);
    const state = readGroupState();
    state[group.id] = next;
    writeGroupState(state);
  });
  wrapper.append(heading, panel);
  return wrapper;
}

function sidebarNodeForItem(item) {
  if (item.id === 'email') return document.getElementById('email-section');
  if (item.id === 'library') return document.getElementById('tool-library-row');
  return item.legacyIds.sidebar.map((id) => document.getElementById(id)).find(Boolean) || null;
}

function primaryNavigationNodes() {
  return PRIMARY_NAVIGATION_IDS
    .map((id) => getNavigationItem(id))
    .map((item) => item && sidebarNodeForItem(item))
    .filter(Boolean);
}

function secondaryNavigationNodes() {
  const excluded = new Set([
    ...PRIMARY_NAVIGATION_IDS,
    'new-chat', 'toggle-sidebar', 'theme', 'settings', 'profile',
  ]);
  const specialistNodes = NAVIGATION_ITEMS
    .filter((item) => item.surfaces.includes('sidebar'))
    .filter((item) => item.group !== 'quick-actions' && !excluded.has(item.id))
    .map(sidebarNodeForItem)
    .filter(Boolean);
  return Array.from(new Set([
    document.getElementById('sessions-section'),
    document.getElementById('models-section'),
    ...specialistNodes,
    document.getElementById('v2-activity-nav'),
  ].filter(Boolean)));
}

function primaryNavigationElement(nodes) {
  const primary = document.createElement('div');
  primary.className = 'v3-primary-navigation';
  primary.setAttribute('role', 'group');
  primary.setAttribute('aria-label', 'Primary navigation');
  nodes.forEach((node) => {
    node.dataset.primaryNavigation = 'true';
    node.setAttribute('draggable', 'false');
    primary.appendChild(node);
  });
  return primary;
}

function existingSidebarNavigationRoots(sidebar) {
  if (!sidebar) return [];
  // Attribute matching intentionally returns every duplicate id. Using
  // getElementById() here would hide the exact partial-reinitialization fault
  // this reconciliation protects against.
  return Array.from(sidebar.querySelectorAll('[id="v2-sidebar-navigation"]'));
}

function annotateRegistryControls() {
  NAVIGATION_ITEMS.forEach((item) => {
    getLegacyTriggerIds(item).forEach((id) => {
      const control = document.getElementById(id);
      if (!control) return;
      control.dataset.navId = item.id;
      if (!control.getAttribute('aria-label') && control.classList.contains('icon-rail-btn')) {
        control.setAttribute('aria-label', item.label);
      }
    });
  });
}

function prepareRail() {
  const rail = document.getElementById('icon-rail');
  if (!rail) return;
  rail.setAttribute('aria-label', 'Primary navigation');
  if (!document.getElementById('rail-restia')) {
    rail.appendChild(railButton('rail-restia', 'Restia', icon([
      { d: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z' },
    ])));
  }
  if (!document.getElementById('rail-home')) {
    rail.appendChild(railButton('rail-home', 'Today', icon([
      { d: 'M3 11.5 12 4l9 7.5' }, { d: 'M5.5 10v10h13V10' },
    ])));
  }
  if (!document.getElementById('rail-life')) {
    rail.appendChild(railButton('rail-life', 'Life', icon([
      { tag: 'circle', cx: '12', cy: '5', r: '2' },
      { tag: 'circle', cx: '6', cy: '17', r: '2' },
      { tag: 'circle', cx: '18', cy: '17', r: '2' },
      { d: 'M12 7v4M12 11 6 15M12 11l6 4' },
    ])));
  }
  if (!document.getElementById('rail-activity')) {
    const activity = railButton('rail-activity', 'Activity', icon([
      { d: 'M4 19V9' }, { d: 'M10 19V5' }, { d: 'M16 19v-7' }, { d: 'M22 19H2' },
    ]));
    const spacer = Array.from(rail.children).find((node) => node instanceof HTMLElement && node.style.flex === '1');
    rail.insertBefore(activity, spacer || document.getElementById('rail-settings'));
  }
  const resizeHandle = rail.querySelector('.rail-resize-handle');
  let previous = resizeHandle;
  ['rail-restia', 'rail-home', 'rail-inbox', 'rail-life', 'rail-search-btn']
    .map((id) => document.getElementById(id))
    .filter(Boolean)
    .forEach((button) => {
      rail.insertBefore(button, previous?.nextSibling || rail.firstChild);
      previous = button;
    });
  rail.querySelectorAll('.icon-rail-btn').forEach((button) => {
    button.classList.toggle('v2-rail-secondary', !RAIL_PRIMARY.has(button.id));
  });
}

function mobileButton(id, label, iconNodes) {
  const button = document.createElement('button');
  button.type = 'button';
  button.dataset.mobileNav = id;
  button.setAttribute('aria-label', label);
  button.append(iconNodes, Object.assign(document.createElement('span'), { textContent: label }));
  return button;
}

function prepareMobileNav() {
  if (document.getElementById('v2-mobile-nav')) return;
  const nav = document.createElement('nav');
  nav.id = 'v2-mobile-nav';
  nav.className = 'v2-mobile-nav';
  nav.setAttribute('aria-label', 'Primary navigation');
  nav.append(
    mobileButton('chat', 'Restia', icon([{ d: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z' }])),
    mobileButton('home', 'Today', icon([{ d: 'M3 11.5 12 4l9 7.5' }, { d: 'M5.5 10v10h13V10' }])),
    mobileButton('inbox', 'Inbox', icon([{ d: 'M4 4h16l2 9v6a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-6z' }, { d: 'M2 13h5l2 3h6l2-3h5' }])),
    mobileButton('life', 'Life', icon([
      { tag: 'circle', cx: '12', cy: '5', r: '2' },
      { tag: 'circle', cx: '6', cy: '17', r: '2' },
      { tag: 'circle', cx: '18', cy: '17', r: '2' },
      { d: 'M12 7v4M12 11 6 15M12 11l6 4' },
    ])),
    mobileButton('search', 'Search', icon([{ tag: 'circle', cx: '10', cy: '10', r: '7' }, { d: 'M21 21l-4.35-4.35' }])),
  );
  document.body.appendChild(nav);
}

function hideMobileSidebar() {
  if (!window.matchMedia('(max-width: 768px)').matches) return;
  closeSidebar({ state: SIDEBAR_STATES.OFF, persist: false });
}

function visibleOrExisting(ids) {
  const controls = ids.map((id) => document.getElementById(id)).filter(Boolean);
  return controls.find((control) => {
    const style = getComputedStyle(control);
    return style.display !== 'none' && style.visibility !== 'hidden';
  }) || controls[0] || null;
}

export function setActiveNavigationItem(id) {
  document.querySelectorAll('[data-nav-id], [data-mobile-nav]').forEach((control) => {
    const active = (control.dataset.navId || control.dataset.mobileNav) === id;
    control.classList.toggle('is-active', active);
    if (active) control.setAttribute('aria-current', 'page');
    else control.removeAttribute('aria-current');
  });
}

function leaveInboxFor(id) {
  if (id === 'inbox' || !window.inboxModule?.isOpen?.()) return false;
  const item = getNavigationItem(id);
  if (item?.kind === 'action' && !['new-chat', 'search'].includes(id)) return false;
  const route = item?.route || (['chat', 'new-chat', 'search'].includes(id) ? '/' : null);
  const wasInboxRoute = window.location.pathname === '/inbox';
  window.inboxModule.close({ restoreFocus: false });
  if (wasInboxRoute && route && window.location.pathname !== route) {
    window.history.pushState({ restiaNavigation: item?.id || 'chat' }, '', route);
    document.title = item?.label ? `${item.label} — Restia` : 'Restia';
  }
  return true;
}

function commitPrimaryRoute(id, { fallbackRoute = null } = {}) {
  const item = getNavigationItem(id);
  const route = item?.route || fallbackRoute;
  if (!route || typeof window === 'undefined') return false;
  if (window.location.pathname !== route) {
    window.history.pushState({ restiaNavigation: id }, '', route);
  }
  document.title = route === '/'
    ? 'Restia'
    : `${item?.label || 'Restia'} — Restia`;
  return true;
}

function topVisibleModalNavigationItem(excludedModalId = '') {
  const rows = NAVIGATION_ITEMS.flatMap((item) => asModalRows(item, excludedModalId));
  rows.sort((left, right) => left.zIndex - right.zIndex);
  return rows.at(-1)?.item || null;
}

function asModalRows(item, excludedModalId = '') {
  return (item.modal?.ids || []).flatMap((id) => {
    if (id === excludedModalId) return [];
    const modal = document.getElementById(id);
    if (!modal?.isConnected || modal.hidden || modal.classList.contains('hidden') || modal.classList.contains('modal-minimized')) return [];
    const style = getComputedStyle(modal);
    if (style.display === 'none' || style.visibility === 'hidden') return [];
    const zIndex = Number.parseInt(style.zIndex, 10);
    return [{ item, zIndex: Number.isFinite(zIndex) ? zIndex : 0 }];
  });
}

function syncActiveNavigationFromVisibleSurface(excludedModalId = '') {
  const searchOverlay = document.getElementById('search-overlay');
  if (searchOverlay && !searchOverlay.classList.contains('hidden')) {
    setActiveNavigationItem('search');
    return;
  }
  if (window.inboxModule?.isOpen?.()) {
    setActiveNavigationItem('inbox');
    return;
  }
  if (lifeWorkspaceModule.isOpen()) {
    setActiveNavigationItem('life');
    return;
  }
  if (missionControlModule.isOpen()) {
    const activityIsActive = document.querySelector('[data-nav-id="activity"][aria-current="page"], [data-mobile-nav="activity"][aria-current="page"]');
    setActiveNavigationItem(activityIsActive ? 'activity' : 'home');
    return;
  }
  if (window.projectsModule?.isOpen?.()) {
    setActiveNavigationItem('projects');
    return;
  }
  const modalItem = topVisibleModalNavigationItem(excludedModalId);
  if (modalItem) {
    setActiveNavigationItem(modalItem.id);
    return;
  }
  if (window.studyModule?.isActive?.()) {
    setActiveNavigationItem('study');
    return;
  }
  setActiveNavigationItem('chat');
}

function scheduleNavigationSurfaceSync(excludedModalId = '') {
  const cancelFrame = typeof cancelAnimationFrame === 'function' ? cancelAnimationFrame : clearTimeout;
  const nextFrame = typeof requestAnimationFrame === 'function'
    ? requestAnimationFrame
    : (callback) => setTimeout(callback, 0);
  if (modalSyncFrame) cancelFrame(modalSyncFrame);
  modalSyncFrame = nextFrame(() => {
    modalSyncFrame = 0;
    syncActiveNavigationFromVisibleSurface(excludedModalId);
  });
}

function navigationModalIds() {
  return new Set(NAVIGATION_ITEMS.flatMap((item) => item.modal?.ids || []));
}

function watchNavigationModal(modal, ids) {
  if (!(modal instanceof HTMLElement) || !ids.has(modal.id) || navigationModalObservers.has(modal)) return;
  const observer = new MutationObserver(() => scheduleNavigationSurfaceSync());
  observer.observe(modal, { attributes: true, attributeFilter: ['class', 'hidden', 'style'] });
  navigationModalObservers.set(modal, observer);
}

function unwatchNavigationModal(modal) {
  const observer = navigationModalObservers.get(modal);
  if (!observer) return;
  observer.disconnect();
  navigationModalObservers.delete(modal);
}

function matchingNavigationModals(node, ids) {
  if (!(node instanceof HTMLElement)) return [];
  return [node, ...node.querySelectorAll('[id]')].filter((candidate) => ids.has(candidate.id));
}

function initNavigationModalObserver() {
  if (modalDomObserver || typeof MutationObserver === 'undefined') return;
  const ids = navigationModalIds();
  ids.forEach((id) => watchNavigationModal(document.getElementById(id), ids));
  modalDomObserver = new MutationObserver((mutations) => {
    let changed = false;
    mutations.forEach((mutation) => {
      mutation.addedNodes.forEach((node) => {
        const modals = matchingNavigationModals(node, ids);
        modals.forEach((modal) => watchNavigationModal(modal, ids));
        if (modals.length) changed = true;
      });
      mutation.removedNodes.forEach((node) => {
        const modals = matchingNavigationModals(node, ids);
        modals.forEach(unwatchNavigationModal);
        if (modals.length) changed = true;
      });
    });
    if (changed) scheduleNavigationSurfaceSync();
  });
  modalDomObserver.observe(document.body, { childList: true, subtree: true });
}

export async function activateNavigationItem(id, options = {}) {
  // Notes is a fullscreen mobile sheet rather than a conventional `.modal`.
  // Primary navigation must dismiss it before opening another destination;
  // otherwise the new workspace changes behind an still-interactive Notes
  // surface and the active tab no longer matches what the user can see.
  if (!['notes', 'todos'].includes(id) && window.notesModule?.isPanelOpen?.()) {
    window.notesModule.closePanel?.();
  }

  // More is a contextual drawer, not a destination. Keep the current Life
  // workspace (and its valid /life route) open behind it. Every real primary
  // switch closes Life and commits its own route only after opening succeeds.
  if (!['life', 'more'].includes(id) && lifeWorkspaceModule.isOpen()) {
    lifeWorkspaceModule.close({ restoreFocus: false });
  }

  if (id === 'more') {
    if (typeof window._odyOpenSidebar === 'function') window._odyOpenSidebar();
    else document.getElementById('mobile-menu-btn')?.click();
    const sidebar = document.getElementById('sidebar');
    const more = document.querySelector('[data-mobile-nav="more"]');
    more?.setAttribute('aria-expanded', String(Boolean(sidebar && !sidebar.classList.contains('hidden'))));
    requestAnimationFrame(() => {
      const target = sidebar?.querySelector('button:not([disabled]), [href], input:not([disabled])');
      try { target?.focus({ preventScroll: true }); } catch (_) { try { target?.focus(); } catch (_) {} }
    });
    return true;
  }
  leaveInboxFor(id);
  if (id === 'life') {
    setActiveNavigationItem('life');
    const opened = await lifeWorkspaceModule.open();
    if (opened === false) {
      syncActiveNavigationFromVisibleSurface();
      return false;
    }
    setActiveNavigationItem('life');
    hideMobileSidebar();
    return true;
  }
  if (id === 'home' || id === 'activity') {
    // Reflect the requested destination immediately while the bounded Today
    // aggregation loads. If a dirty Projects close is cancelled, reconcile
    // back to the still-visible workspace below.
    setActiveNavigationItem(id);
    const opened = await missionControlModule.open(id);
    if (opened === false) {
      syncActiveNavigationFromVisibleSurface();
      return false;
    }
    setActiveNavigationItem(id);
    commitPrimaryRoute(id);
    hideMobileSidebar();
    return true;
  }
  if (id === 'chat') {
    missionControlModule.close();
    let leftStudy = false;
    if (window.studyModule?.isActive?.()) {
      // Chat is a distinct primary destination. A manual Study exit starts a
      // normal chat instead of immediately re-selecting the Study session and
      // reopening its panel.
      const closed = await window.studyModule.close({ manual: true });
      if (!closed && window.studyModule?.isActive?.()) return false;
      leftStudy = Boolean(closed);
    }
    if (window.projectsModule?.isOpen?.()) {
      const closed = await window.projectsModule.close();
      if (!closed) return false;
    }
    const sessionId = window.sessionModule?.getCurrentSessionId?.();
    const sessionMode = window.sessionModule?.getSessions?.()
      ?.find((session) => String(session.id) === String(sessionId))?.mode;
    if (sessionId && String(sessionMode || 'chat').toLowerCase() !== 'study') {
      await window.sessionModule.selectSession?.(sessionId);
    } else if (sessionId) {
      document.getElementById('sidebar-new-chat-btn')?.click();
    } else if (!leftStudy) {
      document.getElementById('sidebar-new-chat-btn')?.click();
    }
    setActiveNavigationItem('chat');
    commitPrimaryRoute('chat');
    hideMobileSidebar();
    return true;
  }

  const item = getNavigationItem(id);
  if (!item) return false;
  if (id === 'inbox' && window.inboxModule?.isOpen?.()) {
    window.inboxModule.focus?.();
    setActiveNavigationItem('inbox');
    hideMobileSidebar();
    return true;
  }
  if (id === 'projects' && window.projectsModule?.isOpen?.()) {
    window.projectsModule.focus?.();
    setActiveNavigationItem('projects');
    hideMobileSidebar();
    return true;
  }
  missionControlModule.close();
  const preferred = options.surface === 'rail'
    ? ['rail', 'sidebar', 'auxiliary']
    : ['sidebar', 'rail', 'auxiliary'];
  const trigger = visibleOrExisting(getLegacyTriggerIds(item, preferred));
  if (!trigger) return false;
  // Search is an overlay on the Restia surface. Commit the underlying route
  // before invoking legacy search handlers so a synchronous focus/modal error
  // cannot leave a closed Life workspace paired with a stale `/life` URL.
  if (item.id === 'search') commitPrimaryRoute('search', { fallbackRoute: '/' });
  trigger.click();
  setActiveNavigationItem(item.id);
  hideMobileSidebar();
  return true;
}

export function prepareV2NavigationShell() {
  const sidebar = document.getElementById('sidebar');
  const inner = sidebar?.querySelector('.sidebar-inner');
  if (!sidebar || !inner) return false;
  const existingRoots = existingSidebarNavigationRoots(sidebar);
  if (sidebar.dataset.navigationVersion === '2') {
    if (existingRoots.length === 1) return true;
    // The legacy controls were moved into the root during the first build.
    // If that root vanished, silently constructing a partial shell would
    // create unbound Home/Activity clones. Fail loudly to the caller instead.
    if (existingRoots.length === 0) return false;
  }

  ensureStylesheet();
  SEMANTIC_BUTTON_IDS.forEach(replaceWithButton);
  // Attach shell-owned destinations before resolving the hierarchy. Detached
  // nodes are intentionally absent from document.getElementById(), which
  // would otherwise leave them out on a fresh load.
  ensureNewDestinations(inner);
  moveThemeToSettings();
  prepareRail();

  // Reuse one canonical root when a partial re-init left shell markup behind.
  // Build the replacement groups before clearing it: the registry-owned
  // controls may currently live inside that root and must be moved, not cloned.
  const root = existingRoots.shift() || document.createElement('div');
  root.id = 'v2-sidebar-navigation';
  root.className = 'v2-sidebar-navigation';
  const savedState = readGroupState();
  const claimedNodes = new Set();
  const primaryNodes = primaryNavigationNodes().filter((node) => {
    if (claimedNodes.has(node)) return false;
    claimedNodes.add(node);
    return true;
  });
  const secondaryNodes = secondaryNavigationNodes().filter((node) => {
    // A legacy control has exactly one registry owner. Keep the first claim
    // if malformed extension metadata points two destinations at one node.
    if (claimedNodes.has(node)) return false;
    claimedNodes.add(node);
    return true;
  });
  const moreState = Object.prototype.hasOwnProperty.call(savedState, 'more')
    ? savedState
    : { ...savedState, more: false };
  const moreGroup = secondaryNodes.length
    ? groupElement({ id: 'more', label: 'More' }, secondaryNodes, moreState)
    : null;
  root.replaceChildren(
    primaryNavigationElement(primaryNodes),
    ...(moreGroup ? [moreGroup] : []),
  );
  document.getElementById('sidebar-new-chat-btn')?.after(root);
  existingRoots.forEach((duplicate) => duplicate.remove());
  document.getElementById('tools-section')?.remove();

  sidebar.dataset.navigationVersion = '2';
  document.body.classList.add('v2-navigation-ready');
  annotateRegistryControls();
  prepareMobileNav();
  return true;
}

export function initV2NavigationShell() {
  if (!prepareV2NavigationShell()) {
    console.error('V2 navigation shell could not be prepared; initialization deferred.');
    return false;
  }
  if (initialized) return true;
  initialized = true;
  missionControlModule.init(window.location.origin);
  lifeWorkspaceModule.init(window.location.origin);
  initNavigationModalObserver();

  document.getElementById('v3-restia-nav')?.addEventListener('click', () => activateNavigationItem('chat'));
  document.getElementById('rail-restia')?.addEventListener('click', () => activateNavigationItem('chat'));
  document.getElementById('v2-home-nav')?.addEventListener('click', () => activateNavigationItem('home'));
  document.getElementById('rail-home')?.addEventListener('click', () => activateNavigationItem('home'));
  document.getElementById('v3-life-nav')?.addEventListener('click', () => activateNavigationItem('life'));
  document.getElementById('rail-life')?.addEventListener('click', () => activateNavigationItem('life'));
  document.getElementById('v2-activity-nav')?.addEventListener('click', () => activateNavigationItem('activity'));
  document.getElementById('rail-activity')?.addEventListener('click', () => activateNavigationItem('activity'));
  document.getElementById('v2-mobile-nav')?.addEventListener('click', (event) => {
    const control = event.target.closest?.('[data-mobile-nav]');
    if (control) activateNavigationItem(control.dataset.mobileNav, { surface: 'mobile' });
  });
  document.addEventListener('restia:mission-control-closed', () => {
    scheduleNavigationSurfaceSync();
  });
  document.addEventListener('restia:search-opened', () => {
    setActiveNavigationItem('search');
  });
  document.addEventListener('restia:search-closed', () => {
    scheduleNavigationSurfaceSync();
  });
  document.addEventListener('restia:projects-closed', () => {
    scheduleNavigationSurfaceSync();
  });
  document.addEventListener('restia:inbox-opened', () => {
    setActiveNavigationItem('inbox');
  });
  document.addEventListener('restia:inbox-closed', () => {
    scheduleNavigationSurfaceSync();
  });
  document.addEventListener('restia:life-opened', () => {
    setActiveNavigationItem('life');
  });
  document.addEventListener('restia:life-closed', () => {
    scheduleNavigationSurfaceSync();
  });
  document.addEventListener('restia:study-opened', () => {
    setActiveNavigationItem('study');
  });
  document.addEventListener('restia:study-closed', () => {
    scheduleNavigationSurfaceSync();
  });
  window.addEventListener('odysseus:modal-opened', (event) => {
    const item = findNavigationItemByModalId(event.detail?.id);
    if (item) setActiveNavigationItem(item.id);
  });
  ['odysseus:modal-closed', 'odysseus:modal-minimized'].forEach((eventName) => {
    window.addEventListener(eventName, (event) => {
      const excludedModalId = event.detail?.id || '';
      scheduleNavigationSurfaceSync(excludedModalId);
    });
  });
  document.addEventListener('click', (event) => {
    const control = event.target.closest?.('[data-nav-id]');
    if (!control) return;
    const item = findNavigationItemByLegacyId(control.id);
    if (item && !['home', 'activity'].includes(item.id)) {
      if (SHELL_ONLY_ACTIONS.has(item.id)) return;
      if (item.id !== 'inbox') leaveInboxFor(item.id);
      if (item.id !== 'life' && lifeWorkspaceModule.isOpen()) {
        lifeWorkspaceModule.close({ restoreFocus: false });
      }
      if (item.id === 'search') {
        commitPrimaryRoute('search', { fallbackRoute: '/' });
      }
      if (
        item.id !== 'projects' &&
        window.projectsModule?.isOpen?.() &&
        !replayingControls.has(control)
      ) {
        event.preventDefault();
        event.stopImmediatePropagation();
        void window.projectsModule.close().then((closed) => {
          if (!closed || !control.isConnected) return;
          replayingControls.add(control);
          try { control.click(); } finally { replayingControls.delete(control); }
        }).catch((error) => {
          console.error('V2 navigation could not close Projects:', error);
        });
        return;
      }
      if (missionControlModule.isOpen()) missionControlModule.close();
      if (item.kind !== 'action') setActiveNavigationItem(item.id);
    }
  }, true);

  window.activateNavigationItem = activateNavigationItem;
  setActiveNavigationItem(findNavigationItemByRoute(window.location.href)?.id || 'chat');
  return true;
}

export const __test = Object.freeze({ RAIL_PRIMARY, PRIMARY_NAVIGATION_IDS });

export default { prepareV2NavigationShell, initV2NavigationShell, activateNavigationItem, setActiveNavigationItem };
