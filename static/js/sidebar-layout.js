// ============================================
// Sidebar Layout — icon rail, hamburger cycling, mobile backdrop & swipe
// ============================================

import { setSidebarSectionCollapsed } from './section-management.js';

let _syncRailSideFn = null;
let _toggleSidebarFn = null;
let _setSidebarStateFn = null;
const MOBILE_BREAKPOINT = 768;
export const SIDEBAR_STATES = Object.freeze({ FULL: 'full', MINI: 'mini', OFF: 'off' });

export function toggleSidebarFromControl(event = null) {
  if (_toggleSidebarFn) {
    _toggleSidebarFn(event || { stopPropagation() {} });
  }
}

/** Apply one authoritative sidebar state and reconcile rail, scrim and ARIA. */
export function setSidebarState(state, options = {}) {
  if (!_setSidebarStateFn) return false;
  return _setSidebarStateFn(state, options);
}

export function openSidebar(options = {}) {
  return setSidebarState(SIDEBAR_STATES.FULL, options);
}

export function closeSidebar(options = {}) {
  return setSidebarState(
    options.state || (_isMobileViewport() ? SIDEBAR_STATES.OFF : SIDEBAR_STATES.MINI),
    options,
  );
}

function _isMobileViewport() {
  return window.innerWidth <= MOBILE_BREAKPOINT;
}

/**
 * Get the current syncRailSide function reference.
 * Needed because it gets patched after initial setup.
 */
export function syncRailSide() {
  if (_syncRailSideFn) _syncRailSideFn();
}

/**
 * Initialize sidebar layout: icon rail, hamburger cycling, mobile backdrop, swipe gestures.
 * @param {Object} Storage - Storage module
 * @param {Object} opts
 * @param {Object} opts.documentModule - Document module (for swapSide)
 * @param {Function} opts._closeCompareIfActive
 * @param {Function} opts._deactivateIncognito
 * @param {Object} opts.presetsModule
 * @param {Object} opts.sessionModule
 * @param {Function} opts.el - Element lookup helper
 * @param {*} opts._defaultChat - Default chat config
 * @param {Function} opts._syncResearchIndicator
 */
export function initSidebarLayout(Storage, opts) {
  const {
    documentModule, _closeCompareIfActive, _deactivateIncognito,
    presetsModule, sessionModule, el, _defaultChat, _syncResearchIndicator
  } = opts;

  // ── Icon rail + sidebar toggle ──
  const iconRail = document.getElementById('icon-rail');
  const hamburgerBtn = document.getElementById('hamburger-btn');
  const sidebarToggleBtn = document.getElementById('sidebar-toggle-btn');
  const sidebar = document.getElementById('sidebar');
  let mobileBackdrop = document.getElementById('sidebar-backdrop');
  if (!mobileBackdrop) {
    mobileBackdrop = document.createElement('div');
    mobileBackdrop.id = 'sidebar-backdrop';
    document.body.appendChild(mobileBackdrop);
  }
  let _temporaryMobileRightSide = false;
  let _wasMobileViewport = _isMobileViewport();
  let _sidebarWasVisible = Boolean(sidebar && !sidebar.classList.contains('hidden'));
  let _userToggledSidebar = false;
  let _wasAutoCollapsed = false;
  let _autoCollapsedFromState = null;
  let _pendingMobileOpenTimer = null;
  let _sidebarTransitionGeneration = 0;
  const stateStorageKey = Storage.KEYS.SIDEBAR_STATE || 'sidebar-layout-state';
  const savedDesktopState = Storage.get(stateStorageKey);
  let _desktopPreferredState = Object.values(SIDEBAR_STATES).includes(savedDesktopState)
    ? savedDesktopState
    : SIDEBAR_STATES.FULL;

  function _syncSidebarAccessibility(sidebarHidden) {
    if (!sidebar) return;
    const activeWasInside = sidebar.contains(document.activeElement);
    sidebar.toggleAttribute('inert', sidebarHidden);
    if (sidebarHidden) sidebar.setAttribute('aria-hidden', 'true');
    else sidebar.removeAttribute('aria-hidden');

    // Every mobile dismissal path (toggle, backdrop, swipe, outside click,
    // tool launch, or Escape) converges here. Never leave keyboard focus in an
    // off-canvas inert drawer.
    if (_isMobileViewport() && _sidebarWasVisible && sidebarHidden && activeWasInside) {
      const returnTarget = document.querySelector('[data-mobile-nav="more"]') || hamburgerBtn;
      try { returnTarget?.focus({ preventScroll: true }); } catch (_) { try { returnTarget?.focus(); } catch (_) {} }
    }
    _sidebarWasVisible = !sidebarHidden;
  }

  function _setSidebarRightSide(wantRight, { persist = false, syncDocument = true } = {}) {
    if (!sidebar) return false;
    const changed = sidebar.classList.contains('right-side') !== wantRight;
    sidebar.classList.toggle('right-side', wantRight);
    if (persist) {
      try { Storage.set(Storage.KEYS.SIDEBAR_SIDE, wantRight ? 'right' : 'left'); } catch (_) {}
    }
    if (changed && syncDocument && documentModule?.swapSide) {
      try { documentModule.swapSide(); } catch (_) {}
    }
    return changed;
  }

  function _syncRailSideCore() {
    if (!iconRail || !sidebar) return;
    const isRight = sidebar.classList.contains('right-side');
    const sidebarHidden = sidebar.classList.contains('hidden');
    _syncSidebarAccessibility(sidebarHidden);
    const railHidden = iconRail.classList.contains('rail-hidden');
    const isMobileMini = iconRail.classList.contains('mobile-mini');
    iconRail.classList.toggle('right-side', isRight);
    // On mobile mini mode, JS already set inline styles — don't touch
    if (isMobileMini) {
      // Just update side positioning
      if (isRight) {
        iconRail.style.left = 'auto';
        iconRail.style.right = '0';
      } else {
        iconRail.style.left = '0';
        iconRail.style.right = 'auto';
      }
    } else {
      iconRail.style.display = (sidebarHidden && !railHidden) ? '' : 'none';
    }
    // Hamburger is always visible — just update body classes for CSS layout adjustments
    if (hamburgerBtn) {
      document.body.classList.toggle('hamburger-right', isRight);
      document.body.classList.toggle('hamburger-left', !isRight);
      document.body.classList.toggle('hamburger-only', sidebarHidden && railHidden);
      document.body.classList.toggle('sidebar-collapsed', sidebarHidden);
    }
    [hamburgerBtn, sidebarToggleBtn].forEach((btn) => {
      if (!btn) return;
      btn.setAttribute('aria-expanded', sidebarHidden ? 'false' : 'true');
      const action = _isMobileViewport()
        ? (sidebarHidden ? 'Open more tools' : 'Close more tools')
        : (!sidebarHidden
          ? 'Collapse sidebar to navigation rail'
          : (railHidden ? 'Show sidebar' : 'Hide navigation rail'));
      btn.setAttribute('aria-label', action);
      btn.title = action;
    });
    const mobileMore = document.querySelector('[data-mobile-nav="more"]');
    mobileMore?.setAttribute('aria-expanded', String(!sidebarHidden));
    // Keep incognito button clear of hamburger
    const incogBtn = document.getElementById('incognito-btn');
    if (incogBtn) {
      if (isRight && sidebarHidden) {
        incogBtn.style.right = '48px';
      } else {
        incogBtn.style.right = '';
      }
    }
  }

  function updateMobileBackdrop() {
    if (!_isMobileViewport()) { mobileBackdrop.classList.remove('visible'); return; }
    const sidebarOpen = sidebar && !sidebar.classList.contains('hidden');
    const miniOpen = iconRail && iconRail.classList.contains('mobile-mini');
    mobileBackdrop.classList.toggle('visible', Boolean(sidebarOpen || miniOpen));
  }

  function _currentSidebarState() {
    if (!sidebar || !iconRail) return SIDEBAR_STATES.OFF;
    if (!sidebar.classList.contains('hidden')) return SIDEBAR_STATES.FULL;
    if (_isMobileViewport()) {
      return iconRail.classList.contains('mobile-mini') ? SIDEBAR_STATES.MINI : SIDEBAR_STATES.OFF;
    }
    return iconRail.classList.contains('rail-hidden') ? SIDEBAR_STATES.OFF : SIDEBAR_STATES.MINI;
  }

  function _cancelPendingMobileOpen() {
    _sidebarTransitionGeneration += 1;
    if (_pendingMobileOpenTimer !== null) {
      clearTimeout(_pendingMobileOpenTimer);
      _pendingMobileOpenTimer = null;
      return true;
    }
    return false;
  }

  function _applySidebarState(state, { persist = false, userInitiated = false } = {}) {
    if (!sidebar || !iconRail || !Object.values(SIDEBAR_STATES).includes(state)) return false;
    _cancelPendingMobileOpen();
    if (userInitiated) _userToggledSidebar = true;
    const mobile = _isMobileViewport();
    if (mobile) {
      sidebar.classList.toggle('hidden', state !== SIDEBAR_STATES.FULL);
      iconRail.classList.toggle('mobile-mini', state === SIDEBAR_STATES.MINI);
      if (state !== SIDEBAR_STATES.MINI) iconRail.style.cssText = '';
    } else if (state === SIDEBAR_STATES.FULL) {
      iconRail.classList.remove('mobile-mini');
      iconRail.style.cssText = '';
      sidebar.classList.remove('hidden');
      iconRail.classList.remove('rail-hidden');
    } else if (state === SIDEBAR_STATES.MINI) {
      iconRail.classList.remove('mobile-mini');
      iconRail.style.cssText = '';
      sidebar.classList.add('hidden');
      iconRail.classList.remove('rail-hidden');
    } else {
      iconRail.classList.remove('mobile-mini');
      iconRail.style.cssText = '';
      sidebar.classList.add('hidden');
      iconRail.classList.add('rail-hidden');
    }

    if (persist && !mobile) {
      _desktopPreferredState = state;
      delete document.body.dataset.routeCollapsedSidebar;
      try { Storage.set(stateStorageKey, state); } catch (_) {}
    }
    syncRailSide();
    return true;
  }

  function _scheduleMobileOpen() {
    _cancelPendingMobileOpen();
    const generation = _sidebarTransitionGeneration;
    _pendingMobileOpenTimer = setTimeout(() => {
      if (generation !== _sidebarTransitionGeneration) return;
      _pendingMobileOpenTimer = null;
      _applySidebarState(SIDEBAR_STATES.FULL);
    }, 250);
  }

  // Set initial reference and expose globally
  _syncRailSideFn = function() { _syncRailSideCore(); updateMobileBackdrop(); };
  window.syncRailSide = syncRailSide;
  _setSidebarStateFn = _applySidebarState;
  window.setSidebarState = setSidebarState;
  window.openSidebar = openSidebar;
  window.closeSidebar = closeSidebar;

  // Restore sidebar side preference
  if (Storage.get(Storage.KEYS.SIDEBAR_SIDE) === 'right') {
    _setSidebarRightSide(true, { syncDocument: false });
  }
  if (_isMobileViewport()) {
    _wasAutoCollapsed = true;
    _applySidebarState(SIDEBAR_STATES.OFF);
  } else {
    _applySidebarState(_desktopPreferredState);
  }

  // Header-only new-chat aliases delegate to the one canonical sidebar action.
  // #sidebar-new-chat-btn is wired in app.js because it needs the full
  // default-model/pending-chat flow; wiring that action here as well caused
  // duplicate click handling and occasional no-op/race behavior.
  const chatNewBtn = document.getElementById('chat-new-btn');
  [chatNewBtn].forEach(btn => {
    if (btn) btn.addEventListener('click', () => {
      document.getElementById('sidebar-new-chat-btn')?.click();
    });
  });

  // Hamburger cycles: full sidebar → mini → off → full.

  // Deliberate "open the sidebar" used by the mobile swipe gesture (wired at
  // module scope). It MUST set _userToggledSidebar so the auto-collapse
  // MutationObserver doesn't immediately re-hide it (the swipe was opening it,
  // then checkSidebarAutoCollapse re-added .hidden because this flag was unset
  // — looked like nothing happened). Mirrors the hamburger's mobile-open path.
  window._odyOpenSidebar = function(side) {
    if (!sidebar) return;
    // On mobile, never open the sidebar while Compare is running — the panes
    // own the screen and stray gestures (swipe, dragging a dock chip to the X)
    // were popping it open. Blocking the open helper covers every path.
    const cc = document.getElementById('chat-container');
    if (_isMobileViewport() && cc && cc.classList.contains('compare-active')) return;
    _userToggledSidebar = true;
    // Optionally place the sidebar on a specific edge (the swipe gesture passes
    // the direction). Persist it + re-anchor the doc panel.
    if (side === 'left' || side === 'right') {
      const wantRight = side === 'right';
      _setSidebarRightSide(wantRight, { persist: true });
      _temporaryMobileRightSide = false;
    } else if (_isMobileViewport() && !sidebar.classList.contains('right-side')) {
      // The mobile hamburger and V2 More entry both open from the right without
      // overwriting the user's persisted desktop side.
      _setSidebarRightSide(true);
      _temporaryMobileRightSide = Storage.get(Storage.KEYS.SIDEBAR_SIDE) !== 'right';
    }
    _wasAutoCollapsed = false;
    _applySidebarState(SIDEBAR_STATES.FULL, { persist: !_isMobileViewport() });
  };

  function _toggleSidebarFromControl(e) {
      e?.stopPropagation?.();
      if (!sidebar) return;

      _userToggledSidebar = true;

      if (e?.shiftKey && !_isMobileViewport()) {
        _cancelPendingMobileOpen();
        _setSidebarRightSide(!sidebar.classList.contains('right-side'), { persist: true });
        syncRailSide();
        return;
      }

      if (_isMobileViewport()) {
        // Mobile: full sidebar ↔ hidden — simple toggle, no mini rail
        const isSidebarVisible = !sidebar.classList.contains('hidden');
        if (_pendingMobileOpenTimer !== null) {
          // A second click while keyboard dismissal is pending is the inverse
          // action: cancel the stale open instead of scheduling another one.
          _cancelPendingMobileOpen();
          _applySidebarState(SIDEBAR_STATES.OFF);
        } else if (isSidebarVisible) {
          _applySidebarState(SIDEBAR_STATES.OFF);
        } else {
          // Mobile: the hamburger always opens the sidebar from the RIGHT.
          // (Not persisted — keeps the desktop side preference untouched.)
          if (!sidebar.classList.contains('right-side')) {
            _setSidebarRightSide(true);
            _temporaryMobileRightSide = Storage.get(Storage.KEYS.SIDEBAR_SIDE) !== 'right';
          }
          // Opening sidebar — blur keyboard first, then open after layout settles
          if (document.activeElement && document.activeElement !== document.body
              && (document.activeElement.tagName === 'INPUT' || document.activeElement.tagName === 'TEXTAREA')) {
            document.activeElement.blur();
            _scheduleMobileOpen();
          } else {
            _applySidebarState(SIDEBAR_STATES.FULL);
          }
        }
        return;
      }

      const current = _currentSidebarState();
      const next = current === SIDEBAR_STATES.FULL
        ? SIDEBAR_STATES.MINI
        : (current === SIDEBAR_STATES.MINI ? SIDEBAR_STATES.OFF : SIDEBAR_STATES.FULL);
      _wasAutoCollapsed = false;
      _autoCollapsedFromState = null;
      _applySidebarState(next, { persist: true });
  }

  _toggleSidebarFn = _toggleSidebarFromControl;
  window.toggleSidebarFromControl = toggleSidebarFromControl;

  if (hamburgerBtn) {
    hamburgerBtn.addEventListener('click', _toggleSidebarFromControl);
  }
  if (sidebarToggleBtn) {
    sidebarToggleBtn.addEventListener('click', _toggleSidebarFromControl);
  }

  // Icon rail section clicks — open sidebar and scroll to section
  if (iconRail) {
    iconRail.addEventListener('click', (e) => {
      const btn = e.target.closest('.icon-rail-btn');
      if (!btn || btn.id === 'rail-new-session' || btn.id === 'rail-delete-session' || btn.id === 'rail-search-btn' || btn.id === 'rail-settings' || btn.id === 'rail-admin') return;
      const sectionId = btn.dataset.section;
      if (!sectionId) return;
      window._odyOpenSidebar?.();
      const section = document.getElementById(sectionId);
      if (section) {
        section.scrollIntoView({ behavior: 'smooth', block: 'start' });
        setSidebarSectionCollapsed(Storage, section, false);
      }
    });
  }

  // Auto-collapse sidebar when window gets small or chat area is squeezed
  const MIN_CHAT_WIDTH = 380; // collapse sidebar if chat gets narrower than this

  function checkSidebarAutoCollapse() {
    if (_userToggledSidebar) return;
    if (!sidebar) return;
    const currentState = _currentSidebarState();
    const isHidden = currentState !== SIDEBAR_STATES.FULL;

    // Check if chat area is too narrow (e.g. sidebar + doc panel both open).
    // BUT — if a tile-snapped modal exists, IT is what's making chat narrow,
    // and that's the user's explicit choice. Don't auto-collapse the sidebar
    // in response, or we get a reactive loop: snap → narrow chat → hide
    // sidebar → safe-rect changes → reclamp modal → new chat width → ...
    const chatContainer = document.querySelector('.chat-container');
    const hasTileSnapped = document.querySelector('.modal-content[data-_tile-zone], .research-pane[data-_tile-zone]');
    const chatTooNarrow = chatContainer && chatContainer.offsetWidth < MIN_CHAT_WIDTH && !isHidden && !hasTileSnapped;

    if ((_isMobileViewport() || chatTooNarrow) && !isHidden) {
      _autoCollapsedFromState = currentState;
      _wasAutoCollapsed = true;
      _applySidebarState(_isMobileViewport() ? SIDEBAR_STATES.OFF : SIDEBAR_STATES.MINI);
    } else if (!_isMobileViewport() && isHidden && _wasAutoCollapsed) {
      // Only restore if chat won't be too narrow
      const restoreState = _autoCollapsedFromState || _desktopPreferredState;
      _applySidebarState(restoreState);
      void document.body.offsetWidth; // reflow
      if (restoreState === SIDEBAR_STATES.FULL && chatContainer && chatContainer.offsetWidth < MIN_CHAT_WIDTH) {
        _applySidebarState(SIDEBAR_STATES.MINI);
      } else {
        _wasAutoCollapsed = false;
        _autoCollapsedFromState = null;
      }
    }
  }

  window.addEventListener('resize', () => {
    const mobile = _isMobileViewport();
    if (_wasMobileViewport && !mobile) {
      if (_temporaryMobileRightSide) {
        _setSidebarRightSide(Storage.get(Storage.KEYS.SIDEBAR_SIDE) === 'right');
        _temporaryMobileRightSide = false;
      }
      _wasAutoCollapsed = false;
      _autoCollapsedFromState = null;
      _applySidebarState(_desktopPreferredState);
    } else if (!_wasMobileViewport && mobile) {
      _autoCollapsedFromState = _currentSidebarState();
      _wasAutoCollapsed = true;
      _applySidebarState(SIDEBAR_STATES.OFF);
    }
    _wasMobileViewport = mobile;
    _userToggledSidebar = false; // allow auto-collapse on actual resize
    requestAnimationFrame(checkSidebarAutoCollapse);
  });
  // Also re-check when doc panel toggles
  new MutationObserver(() => requestAnimationFrame(checkSidebarAutoCollapse))
    .observe(document.body, { attributes: true, attributeFilter: ['class'] });

  // ── Mobile sidebar backdrop + swipe-to-close ──
  // Backdrop overlay: tapping it closes the sidebar

  // Suppress sidebar close briefly after dropdown actions
  window._suppressSidebarClose = false;
  mobileBackdrop.addEventListener('click', (e) => {
    // The scrim owns this gesture. Keep it from reaching document-level
    // navigation/modal handlers after the drawer state changes underneath it.
    e.preventDefault();
    e.stopPropagation();
    if (window._suppressSidebarClose) return;
    // Don't close while a session is being renamed inline — the rename input
    // lives inside the sidebar, and a backdrop tap (e.g. to dismiss the
    // keyboard) would otherwise kick the user out mid-rename.
    if (document.querySelector('.session-rename-input')) return;
    // Don't close if a dropdown or submenu is visible
    const openDD = document.querySelector('.session-dropdown-menu[style*="display: block"], .session-dropdown-menu[style*="display:block"]');
    const openSub = document.querySelector('.session-folder-submenu[style*="display: block"], .session-folder-submenu[style*="display:block"]');
    if (openDD || openSub) {
      if (openSub) openSub.style.display = 'none';
      if (openDD) openDD.style.display = 'none';
      return;
    }
    _applySidebarState(SIDEBAR_STATES.OFF);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || !_isMobileViewport()) return;
    const hasPendingOpen = _pendingMobileOpenTimer !== null;
    if ((!sidebar || sidebar.classList.contains('hidden')) && !hasPendingOpen) return;
    e.preventDefault();
    e.stopPropagation();
    _applySidebarState(SIDEBAR_STATES.OFF);
  });

  // Swipe sidebar toward edge to close
  if (sidebar && 'ontouchstart' in window) {
    let _swStartX = 0, _swStartY = 0, _swSwiping = false;
    sidebar.addEventListener('touchstart', (e) => {
      if (e.target.closest('.list-item')) { _swSwiping = false; return; }
      _swStartX = e.touches[0].clientX;
      _swStartY = e.touches[0].clientY;
      _swSwiping = true;
    }, { passive: true });
    sidebar.addEventListener('touchmove', (e) => {
      if (!_swSwiping) return;
      const dx = e.touches[0].clientX - _swStartX;
      const dy = Math.abs(e.touches[0].clientY - _swStartY);
      if (dy > 40) { _swSwiping = false; return; }
      const isRight = sidebar.classList.contains('right-side');
      if ((!isRight && dx < -60) || (isRight && dx > 60)) {
        _swSwiping = false;
        _applySidebarState(SIDEBAR_STATES.OFF);
      }
    }, { passive: true });
    sidebar.addEventListener('touchend', () => { _swSwiping = false; }, { passive: true });
  }

  // ── Click outside sidebar / icon rail to close (mobile only) ──
  document.addEventListener('click', (e) => {
    if (!_isMobileViewport()) return; // desktop keeps sidebar open
    const sb = document.getElementById('sidebar');
    const rail = document.getElementById('icon-rail');
    // Ignore clicks on elements removed from DOM (e.g. session list re-render during folder toggle)
    if (!e.target.isConnected) return;
    // Ignore clicks on the sidebar, icon rail, or hamburger button itself
    if (e.target.closest('#sidebar') || e.target.closest('#icon-rail') || e.target.closest('#v2-mobile-nav') || e.target.closest('#hamburger-btn') || e.target.closest('#sidebar-toggle-btn')) return;
    // Ignore clicks inside modals or the chat input area
    if (e.target.closest('.modal') || e.target.closest('.input-bar') || e.target.closest('#message')) return;
    // Ignore clicks on session/folder dropdowns and the styled prompt
    // overlay — they're body-level elements logically tied to a sidebar
    // action (e.g. "Move to folder → New Folder…"), so closing the
    // sidebar when the user clicks one yanks the action mid-flight.
    if (e.target.closest('.session-dropdown, .folder-submenu, #styled-prompt-overlay, #styled-confirm-overlay')) return;
    // Close full sidebar if open (with animation)
    if (sb && !sb.classList.contains('hidden')) {
      _applySidebarState(SIDEBAR_STATES.OFF);
      return;
    }
    // Close mobile-mini icon rail overlay if open
    if (rail && rail.classList.contains('mobile-mini')) {
      _applySidebarState(SIDEBAR_STATES.OFF);
    }
  });

  // ── Mobile: close sidebar/rail when a tool button is tapped ──
  // The user expects the sidebar to get out of the way the moment a tool
  // window opens — otherwise the modal lands behind the sidebar on phones.
  // We remember whether the sidebar was open at the moment the tool was
  // tapped so we can re-open it when the tool's modal is dismissed; that
  // way clicking around the app doesn't leave the sidebar permanently
  // shut.
  let _sidebarWasOpenBeforeTool = false;
  let _railWasOpenBeforeTool = false;
  document.addEventListener('click', (e) => {
    if (!_isMobileViewport()) return;
    const btn = e.target.closest('[id^="tool-"], [id^="rail-"]');
    if (!btn) return;
    // Capture runs before the button's own handler (Notes used to hide the
    // drawer synchronously), so retain the true pre-launch state.
    if (sidebar && !sidebar.classList.contains('hidden')) _sidebarWasOpenBeforeTool = true;
    if (iconRail && iconRail.classList.contains('mobile-mini')) _railWasOpenBeforeTool = true;
    setTimeout(() => {
      if (_sidebarWasOpenBeforeTool || _railWasOpenBeforeTool || _currentSidebarState() !== SIDEBAR_STATES.OFF) {
        _applySidebarState(SIDEBAR_STATES.OFF);
      }
    }, 0);
  }, true);

  // When a tool is dismissed by swiping it down (ui.js fires `modal-dismissed`),
  // don't bounce the sidebar back open — the swipe should just dismiss the tool.
  // Button-close still restores the prior sidebar state (no event fired there).
  window.addEventListener('modal-dismissed', () => {
    _sidebarWasOpenBeforeTool = false;
    _railWasOpenBeforeTool = false;
  });

  // ── Mobile: when a tool modal closes, restore the sidebar/rail to
  // whatever state it was in before the tool was opened. ──
  // We watch every .modal for the .hidden class going on, and if our
  // remembered "sidebar-was-open" flag is set, undo the auto-close.
  if (_isMobileViewport()) {
    const _restoreSidebar = () => {
      // Skip if any modal is still visible (.modal without .hidden) — we only
      // restore once the user is back to bare chat. A tool swiped DOWN to a
      // dock chip is minimized (display:none via .modal-minimized), not closed
      // — it's still "around", so don't bounce the sidebar open behind it. Only
      // a full close (no minimized modal, no dock chips) should restore.
      const anyOpen = [...document.querySelectorAll('.modal')]
        .some(m => (!m.classList.contains('hidden') && getComputedStyle(m).display !== 'none')
                   || m.classList.contains('modal-minimized'));
      const anyDocked = document.querySelectorAll('.minimized-dock-chip').length > 0;
      if (anyOpen || anyDocked) {
        // A tool is still minimized/docked. The user has left the "launched
        // from the sidebar" context — drop the restore intent so that later
        // FULLY closing the tool (e.g. dragging its chip to the trash) doesn't
        // bounce the sidebar open. (The modal-dismissed listener that normally
        // clears these gets blocked by modalManager's stopImmediatePropagation.)
        _sidebarWasOpenBeforeTool = false;
        _railWasOpenBeforeTool = false;
        return;
      }
      const shouldSync = _sidebarWasOpenBeforeTool || _railWasOpenBeforeTool;
      const restoreState = _sidebarWasOpenBeforeTool
        ? SIDEBAR_STATES.FULL
        : (_railWasOpenBeforeTool ? SIDEBAR_STATES.MINI : SIDEBAR_STATES.OFF);
      _sidebarWasOpenBeforeTool = false;
      _railWasOpenBeforeTool = false;
      if (shouldSync) _applySidebarState(restoreState);
    };
    const _modalObs = new MutationObserver((muts) => {
      let triggered = false;
      for (const m of muts) {
        if (m.type !== 'attributes' || m.attributeName !== 'class') continue;
        const t = m.target;
        if (!(t instanceof HTMLElement) || !t.classList) continue;
        if (t.classList.contains('modal')) { triggered = true; break; }
      }
      if (triggered) setTimeout(_restoreSidebar, 50);
    });
    _modalObs.observe(document.body, { subtree: true, attributes: true, attributeFilter: ['class'] });
    window.addEventListener('sidebar-tool-closed', _restoreSidebar);
  }

  // (Mobile swipe-to-open-sidebar is wired at MODULE scope — see
  // _initChatSwipeToOpenSidebar() at the bottom of this file — so it attaches
  // independently of this init function completing.)
}

// ── Mobile: swipe horizontally on the splash/chat to open the sidebar ──
// Wired at MODULE scope (not inside initSidebarLayout) so a throw anywhere in
// that init can't drop this listener. Bound on `document` so it catches the
// touch regardless of which child element is under the finger. touchmove is
// NON-passive and calls preventDefault() once the gesture is locked
// horizontal — without that, Firefox (and others) treat the horizontal swipe
// as their own scroll/navigation gesture and our handler never gets to act.
function _initChatSwipeToOpenSidebar() {
  if (window.__odySwipeWired) return;
  window.__odySwipeWired = true;

  // Areas where a horizontal drag means something else (their own scroll/drag).
  const EXCLUDE = [
    '#sidebar', '#icon-rail', '.modal', '.input-bar', '#message',
    '#minimized-dock', '.minimized-dock-chip', '#dock-trash-zone',
    'pre', 'table', '.agent-tool-output', '.agent-thread-cmd',
    'input', 'textarea', 'select',
  ].join(', ');

  let sx = 0, sy = 0, track = false, decided = false;

  const reset = () => { track = false; decided = false; };

  document.addEventListener('touchstart', (e) => {
    reset();
    if (!_isMobileViewport()) return;
    if (!e.touches || e.touches.length !== 1) return;
    if (window._chipDragging) return;
    const sb = document.getElementById('sidebar');
    if (sb && !sb.classList.contains('hidden')) return; // already open
    // Only in the chat / empty-chat view. Not when a document or PDF is open
    // (body.doc-view), notes is open (body.notes-view), or a tool modal is up.
    if (document.body.classList.contains('doc-view') ||
        document.body.classList.contains('notes-view')) return;
    // Not while Compare is running — it takes over #chat-container with its own
    // panes/scroll, and the swipe-to-open-sidebar gesture gets in the way there.
    const cc = document.getElementById('chat-container');
    if (cc && cc.classList.contains('compare-active')) return;
    const anyModalOpen = [...document.querySelectorAll('.modal')].some(
      m => !m.classList.contains('hidden') && getComputedStyle(m).display !== 'none');
    if (anyModalOpen) return;
    const t = e.target;
    if (t && t.closest && t.closest(EXCLUDE)) return;
    // The gesture must start within the chat area itself.
    if (!(t && t.closest && t.closest('#chat-container'))) return;
    sx = e.touches[0].clientX;
    sy = e.touches[0].clientY;
    track = true;
  }, { passive: true, capture: true });

  document.addEventListener('touchmove', (e) => {
    if (!track) return;
    if (window._chipDragging) { track = false; return; }
    if (!e.touches || !e.touches.length) return;
    const dx = e.touches[0].clientX - sx;
    const dy = e.touches[0].clientY - sy;
    const adx = Math.abs(dx), ady = Math.abs(dy);
    if (!decided) {
      if (adx < 10 && ady < 10) return;          // not enough travel to judge
      if (ady > adx) { track = false; return; }   // vertical-dominant → let it scroll
      decided = true;                             // locked into a horizontal swipe
    }
    // Claim the gesture from the browser so it doesn't scroll/navigate instead.
    if (e.cancelable) e.preventDefault();
    if (adx >= 40) {
      track = false;
      // Direction picks the side (per user preference): swipe LEFT → sidebar
      // on the left, swipe RIGHT → sidebar on the right. dx<0 is a leftward
      // finger motion; mapping it to 'right' (and dx>0 to 'left') is what makes
      // it feel correct in practice.
      const side = dx < 0 ? 'right' : 'left';
      // Use the deliberate-open helper (sets _userToggledSidebar so the
      // auto-collapse observer doesn't instantly re-hide it). Fall back to a
      // plain unhide if the helper isn't wired yet.
      if (typeof window._odyOpenSidebar === 'function') {
        window._odyOpenSidebar(side);
      } else {
        openSidebar();
      }
    }
  }, { passive: false, capture: true });

  document.addEventListener('touchend', reset, { passive: true, capture: true });
  document.addEventListener('touchcancel', reset, { passive: true, capture: true });
}

if (typeof document !== 'undefined' && typeof window !== 'undefined') {
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', _initChatSwipeToOpenSidebar);
  } else {
    _initChatSwipeToOpenSidebar();
  }
}
