"""Focused contracts for the shared sidebar collapse and viewport state."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node binary not on PATH")


def _node_eval(source: str):
    result = subprocess.run(
        ["node", "--input-type=module", "-e", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return json.loads(result.stdout)


def test_rail_section_expansion_uses_canonical_state_and_updates_aria():
    values = _node_eval(
        """
        import { setSidebarSectionCollapsed } from './static/js/section-management.js';

        class FakeClassList {
          constructor(...names) { this.values = new Set(names); }
          add(...names) { names.forEach(name => this.values.add(name)); }
          remove(...names) { names.forEach(name => this.values.delete(name)); }
          contains(name) { return this.values.has(name); }
          toggle(name, force) {
            const enabled = force === undefined ? !this.values.has(name) : Boolean(force);
            if (enabled) this.values.add(name); else this.values.delete(name);
            return enabled;
          }
        }
        const attributes = new Map();
        const button = {
          title: '',
          setAttribute(name, value) { attributes.set(name, String(value)); },
        };
        const title = { textContent: 'Chats' };
        const section = {
          id: 'sessions-section',
          classList: new FakeClassList('collapsed', 'section-just-collapsing'),
          querySelector(selector) {
            return selector === '.section-collapse-btn' ? button : title;
          },
        };
        const values = new Map([['section-collapsed', { 'sessions-section': true }]]);
        const Storage = {
          KEYS: { SIDEBAR_COLLAPSED: 'sidebar-collapsed' },
          getJSON(key, fallback) { return values.has(key) ? values.get(key) : fallback; },
          setJSON(key, value) { values.set(key, value); },
        };

        const changed = setSidebarSectionCollapsed(Storage, section, false);
        console.log(JSON.stringify({
          changed,
          collapsed: section.classList.contains('collapsed'),
          animating: section.classList.contains('section-just-collapsing'),
          canonical: values.get('sidebar-collapsed'),
          legacy: values.get('section-collapsed'),
          expanded: attributes.get('aria-expanded'),
          label: attributes.get('aria-label'),
          title: button.title,
        }));
        """
    )

    assert values == {
        "changed": True,
        "collapsed": False,
        "animating": False,
        "canonical": {"sessions-section": False},
        "legacy": {"sessions-section": True},
        "expanded": "true",
        "label": "Collapse Chats",
        "title": "Collapse Chats",
    }


def test_restored_collapsed_section_reconciles_accessible_toggle_name():
    values = _node_eval(
        """
        import { syncSidebarSectionCollapseControl } from './static/js/section-management.js';

        const attributes = new Map();
        const button = {
          title: '',
          setAttribute(name, value) { attributes.set(name, String(value)); },
        };
        const title = { textContent: 'Chats' };
        const section = {
          classList: { contains(name) { return name === 'collapsed'; } },
          querySelector(selector) {
            return selector === '.section-collapse-btn' ? button : title;
          },
        };

        const changed = syncSidebarSectionCollapseControl(section);
        console.log(JSON.stringify({
          changed,
          expanded: attributes.get('aria-expanded'),
          label: attributes.get('aria-label'),
          title: button.title,
        }));
        """
    )

    assert values == {
        "changed": True,
        "expanded": "false",
        "label": "Expand Chats",
        "title": "Expand Chats",
    }


def test_sidebar_uses_one_inclusive_mobile_breakpoint_and_migrates_legacy_state():
    layout = (ROOT / "static/js/sidebar-layout.js").read_text(encoding="utf-8")
    init = (ROOT / "static/js/init.js").read_text(encoding="utf-8")

    assert "const MOBILE_BREAKPOINT = 768" in layout
    assert "window.innerWidth <= MOBILE_BREAKPOINT" in layout
    assert not re.search(r"window\.innerWidth\s*(?:<|>=|<=|>)\s*(?:700|768)", layout)
    assert "_temporaryMobileRightSide" in layout
    assert "e.target.closest('#v2-mobile-nav')" in layout
    assert "Storage.get(Storage.KEYS.SIDEBAR_SIDE) === 'right'" in layout
    assert "sidebar.toggleAttribute('inert', sidebarHidden)" in layout
    assert "sidebar.setAttribute('aria-hidden', 'true')" in layout
    assert "_sidebarWasVisible && sidebarHidden && activeWasInside" in layout
    assert "document.querySelector('[data-mobile-nav=\"more\"]') || hamburgerBtn" in layout
    assert "Storage.getJSON('section-collapsed', null)" in init
    assert "Storage.setJSON(KEY, saved)" in init


def test_mobile_backdrop_click_is_an_isolated_sidebar_dismissal():
    values = _node_eval(
        r"""
        import { readFileSync } from 'node:fs';

        const source = readFileSync('./static/js/sidebar-layout.js', 'utf8');
        const match = source.match(
          /mobileBackdrop\.addEventListener\('click', \(e\) => \{([\s\S]*?)\n  \}\);\n\n  document\.addEventListener\('keydown'/,
        );
        if (!match) throw new Error('mobile backdrop click handler not found');

        const names = new Set(['visible']);
        const sidebar = {
          classList: {
            contains(name) { return names.has(name); },
            add(name) { names.add(name); },
          },
        };
        const backdropNames = new Set(['visible']);
        const mobileBackdrop = {
          classList: { remove(name) { backdropNames.delete(name); } },
        };
        const document = {
          getElementById(id) { return id === 'sidebar' ? sidebar : null; },
          querySelector() { return null; },
        };
        const window = { _suppressSidebarClose: false };
        let syncCalls = 0;
        const syncRailSide = () => { syncCalls += 1; };
        const event = {
          defaultPrevented: false,
          propagationStopped: false,
          preventDefault() { this.defaultPrevented = true; },
          stopPropagation() { this.propagationStopped = true; },
        };
        let missionControlOpen = true;
        let tasksOpened = false;

        const handler = new Function(
          'e', 'window', 'document', 'mobileBackdrop', 'syncRailSide',
          match[1],
        );
        handler(event, window, document, mobileBackdrop, syncRailSide);

        console.log(JSON.stringify({
          sidebarHidden: names.has('hidden'),
          backdropVisible: backdropNames.has('visible'),
          defaultPrevented: event.defaultPrevented,
          propagationStopped: event.propagationStopped,
          syncCalls,
          missionControlOpen,
          tasksOpened,
        }));
        """
    )

    assert values == {
        "sidebarHidden": True,
        "backdropVisible": False,
        "defaultPrevented": True,
        "propagationStopped": True,
        "syncCalls": 1,
        "missionControlOpen": True,
        "tasksOpened": False,
    }


def test_chats_collapse_control_stays_visible_beside_header_actions():
    """Manage/sort controls must not hide the keyboard collapse target."""

    css = (ROOT / "static/style.css").read_text(encoding="utf-8")
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    service_worker = (ROOT / "static/sw.js").read_text(encoding="utf-8")

    generic_hide = ".section-header-flex:has(.section-header-btn) .section-collapse-btn { display: none; }"
    chats_override = "#sessions-section .section-collapse-btn {"
    assert generic_hide in css
    assert chats_override in css
    assert css.index(chats_override) > css.index(generic_hide)
    assert "flex: 0 0 28px" in css
    assert "flex-basis: 44px" in css
    assert "#sessions-section .section-collapse-btn:focus-visible" in css
    # Any shell release that changes a full workspace/sidebar CSS must advance both cache
    # layers; otherwise an installed app can combine new markup with old rules.
    assert "/static/style.css?v=20260715v2" in html
    assert "/static/projects.css?v=20260715v2" in html
    assert "/static/v2-shell.css?v=20260715v2" in html
    assert "/static/mission-control.css?v=20260715v2" in html
    assert "const CACHE_NAME = 'restia-v358'" in service_worker
