// Restia V2 adaptive navigation shell.
//
// The existing launch controls remain the behavioural source of truth: this
// module moves those exact nodes into a grouped information architecture
// before app.js binds listeners. That keeps every legacy integration working
// while giving desktop, rail, mobile, and command surfaces one registry.

import missionControlModule from './missionControl.js';
import {
  NAVIGATION_GROUPS,
  NAVIGATION_ITEMS,
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
  'tool-notes-btn', 'tool-todos-btn', 'tool-tasks-btn', 'tool-theme-btn',
]);

const RAIL_PRIMARY = new Set([
  'rail-home', 'rail-search-btn', 'rail-new-session', 'rail-chats',
  'rail-documents', 'rail-projects', 'rail-tasks', 'rail-calendar',
  'rail-messages', 'rail-study', 'rail-activity', 'rail-settings',
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
  let home = document.getElementById('v2-home-nav');
  if (!home) {
    home = destinationButton('v2-home-nav', 'Home', icon([
      { d: 'M3 11.5 12 4l9 7.5' }, { d: 'M5.5 10v10h13V10' }, { d: 'M9.5 20v-6h5v6' },
    ]));
    container.appendChild(home);
  }
  let activity = document.getElementById('v2-activity-nav');
  if (!activity) {
    activity = destinationButton('v2-activity-nav', 'Activity', icon([
      { tag: 'path', d: 'M4 19V9' }, { tag: 'path', d: 'M10 19V5' },
      { tag: 'path', d: 'M16 19v-7' }, { tag: 'path', d: 'M22 19H2' },
    ]));
    container.appendChild(activity);
  }
  return { home, activity };
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

function nodesForGroup(groupId) {
  if (groupId === 'home') {
    return ['v2-home-nav', 'sessions-section'].map((id) => document.getElementById(id)).filter(Boolean);
  }
  if (groupId === 'system') {
    return [document.getElementById('v2-activity-nav')].filter(Boolean);
  }
  const nodes = NAVIGATION_ITEMS
    .filter((item) => item.group === groupId && item.surfaces.includes('sidebar'))
    .filter((item) => !['new-chat', 'search', 'toggle-sidebar', 'theme', 'settings', 'profile'].includes(item.id))
    .map((item) => {
      if (item.id === 'email') return document.getElementById('email-section');
      if (item.id === 'library') return document.getElementById('tool-library-row');
      return item.legacyIds.sidebar.map((id) => document.getElementById(id)).find(Boolean);
    })
    .filter(Boolean);
  if (groupId === 'ai-lab') nodes.unshift(document.getElementById('models-section'));
  return Array.from(new Set(nodes.filter(Boolean)));
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
  const search = document.getElementById('rail-search-btn');
  if (!document.getElementById('rail-home')) {
    rail.insertBefore(railButton('rail-home', 'Home', icon([
      { d: 'M3 11.5 12 4l9 7.5' }, { d: 'M5.5 10v10h13V10' },
    ])), search || rail.firstChild);
  }
  if (!document.getElementById('rail-activity')) {
    const activity = railButton('rail-activity', 'Activity', icon([
      { d: 'M4 19V9' }, { d: 'M10 19V5' }, { d: 'M16 19v-7' }, { d: 'M22 19H2' },
    ]));
    const spacer = Array.from(rail.children).find((node) => node instanceof HTMLElement && node.style.flex === '1');
    rail.insertBefore(activity, spacer || document.getElementById('rail-settings'));
  }
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
    mobileButton('home', 'Home', icon([{ d: 'M3 11.5 12 4l9 7.5' }, { d: 'M5.5 10v10h13V10' }])),
    mobileButton('chat', 'Chat', icon([{ d: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z' }])),
    mobileButton('projects', 'Projects', icon([{ tag: 'rect', x: '3', y: '4', width: '18', height: '16', rx: '2' }, { d: 'M9 4v16M15 4v16' }])),
    mobileButton('calendar', 'Calendar', icon([{ tag: 'rect', x: '3', y: '4', width: '18', height: '17', rx: '2' }, { d: 'M8 2v4M16 2v4M3 10h18' }])),
    mobileButton('more', 'More', icon([{ tag: 'circle', cx: '5', cy: '12', r: '1' }, { tag: 'circle', cx: '12', cy: '12', r: '1' }, { tag: 'circle', cx: '19', cy: '12', r: '1' }])),
  );
  const more = nav.querySelector('[data-mobile-nav="more"]');
  more?.setAttribute('aria-controls', 'sidebar');
  more?.setAttribute('aria-expanded', 'false');
  document.body.appendChild(nav);
}

function hideMobileSidebar() {
  if (!window.matchMedia('(max-width: 768px)').matches) return;
  document.getElementById('sidebar')?.classList.add('hidden');
  document.getElementById('sidebar-backdrop')?.classList.remove('visible');
  window.syncRailSide?.();
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
  if (id === 'home' || id === 'activity') {
    // Reflect the requested destination immediately while the bounded Today
    // aggregation loads. If a dirty Projects close is cancelled, reconcile
    // back to the still-visible workspace below.
    setActiveNavigationItem(id);
    const opened = await missionControlModule.open();
    if (opened === false) {
      syncActiveNavigationFromVisibleSurface();
      return false;
    }
    setActiveNavigationItem(id);
    hideMobileSidebar();
    if (id === 'activity') {
      requestAnimationFrame(() => document.getElementById('mission-health-title')?.scrollIntoView({ behavior: 'smooth', block: 'start' }));
    }
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
      document.getElementById('sidebar-brand-btn')?.click();
    }
    setActiveNavigationItem('chat');
    hideMobileSidebar();
    return true;
  }

  const item = getNavigationItem(id);
  if (!item) return false;
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
  trigger.click();
  setActiveNavigationItem(item.id);
  hideMobileSidebar();
  return true;
}

export function prepareV2NavigationShell() {
  const sidebar = document.getElementById('sidebar');
  const inner = sidebar?.querySelector('.sidebar-inner');
  if (!sidebar || !inner) return false;
  if (sidebar.dataset.navigationVersion === '2') return true;

  ensureStylesheet();
  SEMANTIC_BUTTON_IDS.forEach(replaceWithButton);
  // Attach new V2-only destinations before resolving groups. Detached nodes
  // are intentionally absent from document.getElementById(), which otherwise
  // leaves Home and Activity out of the expanded sidebar on a fresh load.
  ensureNewDestinations(inner);
  moveThemeToSettings();
  prepareRail();

  const root = document.createElement('div');
  root.id = 'v2-sidebar-navigation';
  root.className = 'v2-sidebar-navigation';
  const savedState = readGroupState();
  NAVIGATION_GROUPS.filter((group) => !group.hidden).forEach((group) => {
    const nodes = nodesForGroup(group.id);
    if (nodes.length) root.appendChild(groupElement(group, nodes, savedState));
  });
  document.getElementById('sidebar-search-btn')?.after(root);
  document.getElementById('tools-section')?.remove();

  sidebar.dataset.navigationVersion = '2';
  document.body.classList.add('v2-navigation-ready');
  annotateRegistryControls();
  prepareMobileNav();
  return true;
}

export function initV2NavigationShell() {
  prepareV2NavigationShell();
  if (initialized) return true;
  initialized = true;
  missionControlModule.init(window.location.origin);
  initNavigationModalObserver();

  document.getElementById('v2-home-nav')?.addEventListener('click', () => activateNavigationItem('home'));
  document.getElementById('rail-home')?.addEventListener('click', () => activateNavigationItem('home'));
  document.getElementById('v2-activity-nav')?.addEventListener('click', () => activateNavigationItem('activity'));
  document.getElementById('rail-activity')?.addEventListener('click', () => activateNavigationItem('activity'));
  document.getElementById('v2-mobile-nav')?.addEventListener('click', (event) => {
    const control = event.target.closest?.('[data-mobile-nav]');
    if (control) activateNavigationItem(control.dataset.mobileNav, { surface: 'mobile' });
  });
  document.addEventListener('restia:mission-control-closed', () => {
    setActiveNavigationItem('chat');
  });
  document.addEventListener('restia:projects-closed', () => {
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

export const __test = Object.freeze({ RAIL_PRIMARY });

export default { prepareV2NavigationShell, initV2NavigationShell, activateNavigationItem, setActiveNavigationItem };
