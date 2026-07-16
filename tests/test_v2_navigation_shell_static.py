"""Static integration contracts for the registry-owned Restia V2 shell."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_v2_shell_prepares_before_legacy_event_binding():
    app = _read("static/app.js")
    start = app.index("function startRestiaApp()")
    prepare = app.index("initV2NavigationShell();", start)
    listeners = app.index("initializeEventListeners();", start)

    assert prepare < listeners
    assert "findNavigationItemByLegacyId(btn.id)" in app
    assert "NAVIGATION_ITEMS.flatMap" in app
    assert "dataset.navigationVersion !== '2'" in app


def test_shell_uses_registry_and_preserves_existing_controls():
    shell = _read("static/js/v2NavigationShell.js")
    section_management = _read("static/js/section-management.js")
    html = _read("static/index.html")

    assert "NAVIGATION_ITEMS" in shell
    assert "item.group === groupId" in shell
    assert "getLegacyTriggerIds" in shell
    assert "replaceWithButton" in shell
    assert "while (current.firstChild) button.appendChild(current.firstChild)" in shell
    assert "ensureNewDestinations(inner)" in shell
    assert "container.appendChild(home)" in shell
    assert "container.appendChild(activity)" in shell
    assert "document.getElementById('tools-section')?.remove()" in shell
    assert "sidebar?.dataset?.navigationVersion === '2'" in section_management
    assert '<button type="button" class="section-title" id="email-section-title"' in html
    assert '<button type="button" class="tool-library-main" id="tool-library-btn"' in html
    assert "document.getElementById('tool-library-row')" in shell


def test_expanded_sidebar_deduplicates_home_and_new_chat_controls():
    shell = _read("static/js/v2NavigationShell.js")
    layout = _read("static/js/sidebar-layout.js")
    registry = _read("static/js/navigation-registry.js")
    app = _read("static/app.js")
    html = _read("static/index.html")

    # The Home group already supplies the group label, so its destination uses
    # the registry's distinct compact label ("Today") instead of saying Home
    # twice on adjacent rows.
    assert "const homeItem = getNavigationItem('home');" in shell
    assert "homeItem?.shortLabel || homeItem?.label || 'Today'" in shell

    # Branding remains readable/semantic but is no longer a second New Chat
    # trigger beside the explicit row.
    assert '<div class="sidebar-brand" id="sidebar-brand-btn">' in html
    assert '<span class="sidebar-brand-title">Restia</span>' in html
    assert 'class="sidebar-brand-title" role="heading"' not in html
    assert 'id="sidebar-brand-btn" style="cursor:pointer;"' not in html
    assert "const brandBtn = el('sidebar-brand-btn');" not in app
    assert "document.getElementById('sidebar-brand-btn')?.click()" not in shell
    assert "auxiliary: ['sidebar-brand-btn']" not in registry
    assert "document.getElementById('sidebar-new-chat-btn')?.click();" in layout
    assert "document.getElementById('sidebar-new-chat-btn')?.click();" in shell


def test_rail_hover_label_replaces_native_tooltip_accessibly():
    app = _read("static/app.js")

    assert "btn.setAttribute('aria-label', cleanLabel)" in app
    assert "btn.removeAttribute('title')" in app
    assert "span.setAttribute('aria-hidden', 'true')" in app


def test_adaptive_shell_has_five_item_mobile_nav_and_accessible_states():
    shell = _read("static/js/v2NavigationShell.js")
    mission = _read("static/js/missionControl.js")
    modal_manager = _read("static/js/modalManager.js")
    projects = _read("static/js/projects.js")
    css = _read("static/v2-shell.css")

    assert shell.count("mobileButton(") == 6  # declaration plus exactly five calls
    for item in ("home", "chat", "projects", "calendar", "more"):
        assert f"mobileButton('{item}'" in shell
    assert "aria-current" in shell
    assert "aria-expanded" in shell
    assert "restia:mission-control-closed" in shell
    assert "restia:mission-control-closed" in mission
    assert "restia:projects-closed" in shell
    assert "restia:projects-closed" in projects
    assert "setActiveNavigationItem('chat')" in shell
    assert "findNavigationItemByRoute(window.location.href)?.id || 'chat'" in shell
    assert "if (missionControlModule.isOpen()) missionControlModule.close();" in shell
    assert "window.projectsModule?.isOpen?.()" in shell
    assert "event.stopImmediatePropagation();" in shell
    assert "replayingControls.add(control);" in shell
    assert "findNavigationItemByModalId(event.detail?.id)" in shell
    assert "odysseus:modal-closed" in shell
    assert "odysseus:modal-minimized" in shell
    assert "SHELL_ONLY_ACTIONS.has(item.id)" in shell
    assert "if (item.kind !== 'action')" in shell
    assert "_emitModalState('odysseus:modal-closed'" in modal_manager
    assert "_emitModalState('odysseus:modal-minimized'" in modal_manager
    assert "syncActiveNavigationFromVisibleSurface(excludedModalId)" in shell
    assert "if (id === excludedModalId) return [];" in shell
    assert "prefers-reduced-motion" in css
    assert "repeat(5, minmax(0, 1fr))" in css


def test_theme_is_under_settings_and_secondary_rail_is_decluttered():
    shell = _read("static/js/v2NavigationShell.js")
    css = _read("static/v2-shell.css")

    assert "moveThemeToSettings" in shell
    assert "[data-settings-panel=\"appearance\"]" in shell
    assert "tool-theme-btn" in shell
    assert "#icon-rail #rail-delete-session" in css
    assert "#icon-rail #rail-theme" in css


def test_v2_assets_are_versioned_and_available_offline():
    html = _read("static/index.html")
    sw = _read("static/sw.js")

    for asset in (
        "/static/v2-shell.css",
        "/static/mission-control.css",
        "/static/js/navigation-registry.js",
        "/static/js/v2NavigationShell.js",
        "/static/js/missionControl.js",
        "/static/js/calendar/reminderPayload.js",
    ):
        assert asset in (html + sw)
    assert "const CACHE_NAME = 'restia-v366'" in sw


def test_mobile_v2_bar_replaces_legacy_floating_hamburger():
    css = _read("static/v2-shell.css")

    assert "body.v2-navigation-ready .hamburger-btn" in css
    assert "display: none !important" in css
    assert "body:has(.projects-dialog) .v2-mobile-nav" in css


def test_primary_navigation_closes_fullscreen_notes_before_switching_views():
    shell = _read("static/js/v2NavigationShell.js")

    close_notes = "window.notesModule?.isPanelOpen?.()"
    assert close_notes in shell
    assert "window.notesModule.closePanel?.()" in shell
    assert shell.index(close_notes) < shell.index("if (id === 'more')")


def test_home_activity_and_planning_are_first_class_workspaces():
    app_js = _read("static/app.js")
    shell = _read("static/js/v2NavigationShell.js")
    mission = _read("static/js/missionControl.js")
    modals = _read("static/js/modalManager.js")

    assert "urlPath === '/today' || urlPath === '/activity'" in app_js
    assert "queueMicrotask" in app_js
    assert "missionControlModule.open(id)" in shell
    assert "/api/mission-control/activity?limit=30" in mission
    assert "before_id: String(current.next_before_id)" in mission
    assert "'activity-more'" in mission
    assert "void loadCurrentView()" in mission
    assert "/api/planning" in mission
    assert "planning-complete" in mission
    assert "System progression" in mission
    assert "minimizeVisibleModals" in modals


def test_command_palette_is_derived_from_navigation_registry():
    palette = _read("static/js/commandPalette.js")

    assert "getNavigationItems({ surface: 'command-palette'" in palette
    assert "command.triggerIds" in palette
    assert "quick-capture:note" in palette
    assert "quick-capture:todo" in palette


def test_mission_control_deep_link_serves_and_opens_v2_home():
    app_py = _read("app.py")
    app_js = _read("static/app.js")

    assert '@app.get("/today")' in app_py
    assert "async def serve_today" in app_py
    assert "'/today':    () => activateNavigationItem('home')" in app_js
    html = _read("static/index.html")
    assert "'/today': 'Today — Restia'" in html


def test_activity_deep_link_serves_and_opens_maintainer_center():
    app_py = _read("app.py")
    app_js = _read("static/app.js")
    html = _read("static/index.html")

    assert '@app.get("/activity")' in app_py
    assert "async def serve_activity" in app_py
    assert "'/activity': () => activateNavigationItem('activity')" in app_js
    assert "'/activity': 'Activity — Restia'" in html


def test_mission_control_reserves_the_live_sidebar_or_rail_width():
    css = _read("static/mission-control.css")

    assert "calc(var(--icon-rail-w, 0px) + var(--sidebar-w, 0px))" in css


def test_primary_workspaces_close_competing_surfaces_before_opening():
    shell = _read("static/js/v2NavigationShell.js")
    mission = _read("static/js/missionControl.js")
    projects = _read("static/js/projects.js")
    app = _read("static/app.js")

    assert "window.studyModule.close({ startFresh: false })" in mission
    assert "window.studyModule.close({ startFresh: false })" in projects
    assert "window.missionControlModule?.close?.()" in projects
    assert "window.studyModule.close({ manual: true })" in shell
    assert "String(sessionMode || 'chat').toLowerCase() !== 'study'" in shell
    assert "const closed = await projectsModule.close()" in app


def test_projects_destination_is_idempotent_across_sidebar_rail_and_commands():
    app = _read("static/app.js")

    assert "if (projectsModule.isOpen()) projectsModule.focus();" in app
    assert "if (projectsModule.isOpen()) projectsModule.close();" not in app
