"""Cross-file browser-shell contract for Study Mode.

The browser modules depend on the full Restia DOM and cannot be imported under
Python.  Source checks here are deliberately limited to cross-file wiring
(module import, route maps, FormData flags, and service-worker assets); DOM
structure itself is parsed with the standard HTML parser.
"""

from __future__ import annotations

import ast
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "app.js"
CHAT_JS = ROOT / "static" / "js" / "chat.js"
INDEX_HTML = ROOT / "static" / "index.html"
SESSIONS_JS = ROOT / "static" / "js" / "sessions.js"
STUDY_JS = ROOT / "static" / "js" / "study.js"
SW_JS = ROOT / "static" / "sw.js"
STYLE_CSS = ROOT / "static" / "style.css"
ADMIN_JS = ROOT / "static" / "js" / "admin.js"
DOCKERIGNORE = ROOT / ".dockerignore"


class _IdParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "id" and value:
                self.ids.append(value)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)


def _html_ids():
    parser = _IdParser()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))
    return parser.ids


def test_study_deep_link_serves_the_spa_and_has_browser_metadata():
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"), filename=str(APP_PY))
    route = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "serve_study"
    )
    assert any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "get"
        and decorator.args
        and isinstance(decorator.args[0], ast.Constant)
        and decorator.args[0].value == "/study"
        for decorator in route.decorator_list
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "serve_index"
        for node in ast.walk(route)
    )

    html = INDEX_HTML.read_text(encoding="utf-8")
    assert html.count("'/study'") >= 2  # favicon map and title map
    assert re.search(r"['\"]?/study['\"]?\s*:\s*['\"]Study Mode", html)


def test_study_rail_tool_and_panel_dom_contract_is_complete():
    ids = set(_html_ids())
    required = {
        "rail-study",
        "tool-study-btn",
        "study-panel",
        "study-panel-title",
        "study-panel-body",
        "study-panel-collapse",
        "study-panel-close",
        "study-timer",
        "study-timer-status",
        "study-timer-start",
        "study-timer-pause",
        "study-timer-finish",
        "study-session-total",
        "study-goal-form",
        "study-goal-text",
        "study-target-hours",
        "study-target-date",
        "study-goal-save",
        "study-progress-bar",
        "study-progress-value",
        "study-progress-copy",
        "study-deadline-copy",
        "study-error",
        "study-live-status",
        "study-method",
    }
    assert required <= ids, f"missing Study Mode DOM ids: {sorted(required - ids)}"

    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'data-ui-key="tool-study"' in html
    assert 'data-wipe-kind="study"' in html


def test_index_has_no_duplicate_dom_ids():
    counts = Counter(_html_ids())
    duplicates = {name: count for name, count in counts.items() if count > 1}
    assert not duplicates, f"duplicate DOM ids: {duplicates}"


def test_app_imports_initializes_and_routes_the_study_module():
    source = APP_JS.read_text(encoding="utf-8")
    assert STUDY_JS.is_file()
    assert re.search(
        r"import\s+studyModule\s+from\s+['\"]\./js/study\.js['\"]",
        source,
    )
    assert re.search(r"\bstudyModule\.init\(\s*API_BASE\s*\)", source)
    assert re.search(
        r"['\"]/study['\"]\s*:\s*\(\)\s*=>\s*document\.getElementById\(['\"]tool-study-btn['\"]\)",
        source,
    )
    assert re.search(r"['\"]rail-study['\"]\s*:\s*['\"]tool-study-btn['\"]", source)
    assert re.search(r"toolStudyBtn\.addEventListener\(['\"]click['\"]", source)
    assert "await studyModule.open()" in source
    assert "await studyModule.close({ startFresh: false })" in source
    assert "'#tool-study-btn, #rail-study'" in source

    module = STUDY_JS.read_text(encoding="utf-8")
    for export in ("init", "open", "close", "isActive", "focus"):
        assert re.search(rf"export\s+(?:async\s+)?function\s+{export}\b", module)
    assert "export default studyModule" in module


def test_chat_formdata_marks_study_and_disables_conflicting_modes():
    """Source contract is necessary because send() requires the full browser."""

    source = CHAT_JS.read_text(encoding="utf-8")
    assert re.search(
        r"const\s+isStudyMode\s*=\s*window\.__restiaStudyModeActive\s*===\s*true",
        source,
    )
    assert re.search(r"let\s+isAgentMode\s*=\s*!isStudyMode\s*&&", source)
    assert re.search(
        r"if\s*\(\s*isStudyMode\s*\)\s*\{\s*fd\.append\(\s*['\"]study_mode['\"]\s*,\s*['\"]true['\"]\s*\)",
        source,
    )
    assert re.search(
        r"if\s*\(\s*!isStudyMode\s*&&\s*!isIncognito\s*&&\s*!isAgentMode",
        source,
    )
    assert re.search(
        r"if\s*\(\s*!isStudyMode\s*&&\s*el\(['\"]research-toggle['\"]\)\.checked\s*\)",
        source,
    )
    assert re.search(
        r"if\s*\(\s*isStudyMode\s*\)\s*\{\s*fd\.append\(\s*['\"]allow_bash['\"]\s*,\s*['\"]false['\"]",
        source,
    )
    assert "allow_bash', el('bash-toggle').checked ? 'true' : 'false'" in source
    assert "reset_progress: resetProgress" in STUDY_JS.read_text(encoding="utf-8")
    assert re.search(
        r"if\s*\(\s*!isStudyMode\s*&&\s*presetsModule\.getSelectedPreset\(\)\s*\)",
        source,
    )


def test_study_sessions_have_an_icon_and_restore_event():
    source = SESSIONS_JS.read_text(encoding="utf-8")
    icon_branch = re.search(
        r"else\s+if\s*\(\s*s\.mode\s*===\s*['\"]study['\"]\s*\)\s*\{(?P<body>.{0,800}?)\n\s*\}",
        source,
        re.DOTALL,
    )
    assert icon_branch and "icon.innerHTML" in icon_branch.group("body")
    assert "restia:session-selected" in source
    assert re.search(
        r"detail\s*:\s*\{\s*id\s*,\s*mode\s*:\s*\(meta\s*&&\s*meta\.mode\)\s*\|\|\s*['\"]chat['\"]",
        source,
    )
    select_body = source[source.index("export async function selectSession") :]
    assert "await window.studyModule.close({ manual: false, startFresh: false })" in select_body
    assert select_body.index("await window.studyModule.close") < select_body.index("currentSessionId = id")


def test_service_worker_precaches_study_asset_with_cache_bump():
    source = SW_JS.read_text(encoding="utf-8")
    version = re.search(r"const\s+CACHE_NAME\s*=\s*['\"]restia-v(\d+)['\"]", source)
    assert version, "versioned service-worker cache name is required"
    assert int(version.group(1)) >= 350

    precache = re.search(r"const\s+PRECACHE\s*=\s*\[(?P<body>.*?)\];", source, re.DOTALL)
    assert precache
    assert "'/static/js/study.js'" in precache.group("body") or '"/static/js/study.js"' in precache.group("body")
    assert STUDY_JS.is_file()


def test_study_release_assets_use_one_cache_key_and_exclude_local_backups():
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_urls = re.findall(r"/static/app\.js\?v=([A-Za-z0-9_-]+)", html)
    assert app_urls and len(set(app_urls)) == 1

    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    assert "/backups/" in dockerignore

    css = STYLE_CSS.read_text(encoding="utf-8")
    study_css = css[css.index("/* ── Study Mode") :]
    assert re.search(r"#study-panel\s*\{[^}]*height:\s*100%;", study_css, re.DOTALL)
    assert "@media (max-width: 1100px)" in study_css
    assert "--study-drawer-bottom" in study_css

    module = STUDY_JS.read_text(encoding="utf-8")
    assert "_queueMutation" in module
    assert "() => _request('/timer/pause', { method: 'POST' })" in module
    assert "ResizeObserver(_syncDrawerClearance)" in module

    admin = ADMIN_JS.read_text(encoding="utf-8")
    assert re.search(r"\bstudy:\s*['\"]study goals and timer progress", admin)
