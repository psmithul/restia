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


class _DomParser(HTMLParser):
    """Record just enough ancestry/attributes for cross-surface contracts."""

    _VOID = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__()
        self.stack = []
        self.nodes = {}
        self.data_attributes = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        element_id = values.get("id")
        ancestors = tuple(frame[1] for frame in self.stack if frame[1])
        if element_id:
            self.nodes[element_id] = {
                "tag": tag,
                "attrs": values,
                "ancestors": ancestors,
            }
        if any(name.startswith("data-study-") for name in values):
            self.data_attributes.append((tag, values, ancestors))
        if tag not in self._VOID:
            self.stack.append((tag, element_id))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


def _html_dom():
    parser = _DomParser()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))
    return parser


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


def test_study_rail_tracker_and_chat_control_dialog_dom_contract_is_complete():
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
        "study-workspace-switcher",
        "study-workspace-count",
        "study-new-workspace",
        "study-rename-workspace",
        "study-quick-actions",
        "study-review-status",
        "study-review-due",
        "study-review-actions",
        "study-review-help",
        "study-chat-launcher",
        "study-controls-open",
        "study-controls-close",
        "study-control-modal",
        "study-control-dialog",
        "study-control-title",
        "study-control-description",
        "study-control-loading",
        "study-chat-workspace-name",
        "study-tracker-workspace-name",
        "study-tracker-workspace-count",
        "study-tracker-alert",
        "study-goal-summary",
        "study-mastery-distance",
        "study-control-focus-copy",
    }
    assert required <= ids, f"missing Study Mode DOM ids: {sorted(required - ids)}"

    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'data-ui-key="tool-study"' in html
    assert 'data-wipe-kind="study"' in html
    for move in ("Mastery map", "Recall sprint", "Mixed drill", "Transfer test"):
        assert f">{move}</button>" in html
    for outcome in ("missed", "hinted", "clean", "transfer"):
        assert f'data-study-result="{outcome}"' in html

    dom = _html_dom()
    dialog = dom.nodes["study-control-dialog"]
    assert dialog["tag"] == "section"
    assert dialog["attrs"]["role"] == "dialog"
    assert dialog["attrs"]["aria-modal"] == "true"
    assert dialog["attrs"]["aria-labelledby"] == "study-control-title"
    assert dialog["attrs"]["aria-describedby"] == "study-control-description"
    assert dialog["attrs"]["tabindex"] == "-1"

    launcher = dom.nodes["study-controls-open"]
    assert launcher["tag"] == "button"
    assert launcher["attrs"]["aria-haspopup"] == "dialog"
    assert launcher["attrs"]["aria-controls"] == "study-control-dialog"
    assert launcher["attrs"]["aria-expanded"] == "false"
    assert dom.nodes["study-control-modal"]["attrs"]["aria-hidden"] == "true"
    assert any(
        tag == "button"
        and "data-study-modal-close" in attrs
        and attrs.get("aria-label")
        for tag, attrs, _ in dom.data_attributes
    )


def test_right_study_panel_is_read_only_and_mutations_live_in_chat_dialog():
    dom = _html_dom()
    mutations = {
        "study-workspace-switcher",
        "study-new-workspace",
        "study-rename-workspace",
        "study-timer-start",
        "study-timer-pause",
        "study-timer-finish",
        "study-goal-form",
        "study-goal-text",
        "study-target-hours",
        "study-target-date",
        "study-goal-save",
        "study-quick-actions",
        "study-review-actions",
    }
    for element_id in mutations:
        ancestors = dom.nodes[element_id]["ancestors"]
        assert "study-control-dialog" in ancestors, f"{element_id} escaped the chat control dialog"
        assert "study-panel" not in ancestors, f"{element_id} made the right tracker interactive"

    tracker_outputs = {
        "study-tracker-workspace-name",
        "study-tracker-workspace-count",
        "study-timer",
        "study-timer-status",
        "study-session-total",
        "study-goal-summary",
        "study-progress-value",
        "study-progress-bar",
        "study-progress-copy",
        "study-mastery-distance",
        "study-review-status",
        "study-review-due",
    }
    for element_id in tracker_outputs:
        assert "study-panel" in dom.nodes[element_id]["ancestors"], f"{element_id} is not in the tracker"


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
    assert re.search(r"\bstudyModule\.init\(\s*API_BASE\s*,", source)
    assert re.search(
        r"['\"]/study['\"]\s*:\s*\(\)\s*=>\s*document\.getElementById\(['\"]tool-study-btn['\"]\)",
        source,
    )
    assert re.search(r"['\"]rail-study['\"]\s*:\s*['\"]tool-study-btn['\"]", source)
    assert re.search(r"toolStudyBtn\.addEventListener\(['\"]click['\"]", source)
    assert "await studyModule.enter()" in source
    assert "await studyModule.close({ startFresh: false })" in source
    assert "'#tool-study-btn, #rail-study'" in source

    module = STUDY_JS.read_text(encoding="utf-8")
    for export in (
        "init",
        "enter",
        "open",
        "close",
        "beforeSessionSwitch",
        "prepareFirstPrompt",
        "applyServerInitialization",
        "isActive",
        "focus",
    ):
        assert re.search(rf"export\s+(?:async\s+)?function\s+{export}\b", module)
    assert "export default studyModule" in module
    assert "_initializeWorkspace({ sessionId: requestedId" in module
    assert "_openControls({ trigger: _controlsTriggerTarget(), focusError: true })" in module
    assert "document.addEventListener('keydown', _handleControlsKeydown)" in module
    assert "event.key === 'Escape'" in module
    assert "event.key !== 'Tab'" in module
    assert "_setControlsBackgroundInert(true)" in module
    assert "_setControlsBackgroundInert(false)" in module
    assert "setAttribute('aria-expanded', 'true')" in module
    assert "setAttribute('aria-expanded', 'false')" in module
    assert "REQUEST_TIMEOUT_MS" in module and "new AbortController()" in module


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
    assert "session_id=${encodeURIComponent(workspaceId)}" in STUDY_JS.read_text(encoding="utf-8")
    assert re.search(
        r"if\s*\(\s*!isStudyMode\s*&&\s*presetsModule\.getSelectedPreset\(\)\s*\)",
        source,
    )


def test_first_study_prompt_is_saved_before_chat_and_stream_refreshes_tracker():
    source = CHAT_JS.read_text(encoding="utf-8")
    preflight = source.index("window.studyModule.prepareFirstPrompt(msg")
    optimistic_bubble = source.index("const userDisplay = _displayOverride || msg", preflight)
    assert preflight < optimistic_bubble
    assert "{ sessionId: preparingSessionId }" in source[preflight : preflight + 300]
    revalidation = source.index(
        "sessionModule.getCurrentSessionId() !== preparingSessionId", preflight
    )
    stream_capture = source.index(
        "const streamSessionId = sessionModule.getCurrentSessionId()", preflight
    )
    assert preflight < revalidation < stream_capture
    assert "_releaseSendFlag();" in source[revalidation : stream_capture]

    stream_hook = source.index("json.type === 'study_initialized'")
    assert "applyServerInitialization?.(json.data, streamSessionId)" in source[
        stream_hook : stream_hook + 500
    ]

    module = STUDY_JS.read_text(encoding="utf-8")
    prepare = module[module.index("export async function prepareFirstPrompt") :]
    assert "_initializeWorkspace({ prompt, sessionId: workspaceId" in prepare[:1200]
    apply_event = module[module.index("export async function applyServerInitialization") :]
    assert "_applyState(payload" in apply_event[:1200]
    assert "reloadSessions" in apply_event[:1200]


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


def test_study_workspace_creation_is_bounded_and_updates_the_local_list():
    source = SESSIONS_JS.read_text(encoding="utf-8")
    assert "const STUDY_REQUEST_TIMEOUT_MS = 12000" in source

    helper = source[
        source.index("async function _fetchStudyWorkspace") :
        source.index("export async function createStudySession")
    ]
    for contract in (
        "new AbortController()",
        "setTimeout(",
        "controller.abort()",
        "clearTimeout(timeout)",
        "Study workspace request timed out",
    ):
        assert contract in helper

    creation = source[
        source.index("export async function createStudySession") :
        source.index("export async function materializePendingSession")
    ]
    assert creation.count("_fetchStudyWorkspace(") == 2
    assert "`${API_BASE}/api/default-chat`" in creation
    assert "`${API_BASE}/api/session`" in creation
    assert "await loadSessions()" not in creation
    assert "sessions.unshift(localSession)" in creation
    assert "renderSessionList()" in creation
    assert "sessionsSection.classList.remove('hidden')" in creation


def test_service_worker_precaches_study_asset_with_cache_bump():
    source = SW_JS.read_text(encoding="utf-8")
    version = re.search(r"const\s+CACHE_NAME\s*=\s*['\"]restia-v(\d+)['\"]", source)
    assert version, "versioned service-worker cache name is required"
    assert int(version.group(1)) >= 354

    precache = re.search(r"const\s+PRECACHE\s*=\s*\[(?P<body>.*?)\];", source, re.DOTALL)
    assert precache
    assert "'/static/js/study.js'" in precache.group("body") or '"/static/js/study.js"' in precache.group("body")
    assert STUDY_JS.is_file()


def test_study_release_assets_use_one_cache_key_and_exclude_local_backups():
    html = INDEX_HTML.read_text(encoding="utf-8")
    app_urls = re.findall(r"/static/app\.js\?v=([A-Za-z0-9_-]+)", html)
    assert app_urls and len(set(app_urls)) == 1
    style_urls = re.findall(r"/static/style\.css\?v=([A-Za-z0-9_-]+)", html)
    assert style_urls and len(set(style_urls)) == 1
    assert app_urls[0] == style_urls[0]

    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    assert "/backups/" in dockerignore

    css = STYLE_CSS.read_text(encoding="utf-8")
    study_css = css[css.index("/* ── Study Mode") :]
    tracker_css = css.index("/* Study tracker + chat-centered control sheet")
    legacy_study_css = css.index("/* ── Study Mode")
    assert tracker_css > legacy_study_css, (
        "chat-control overrides must follow legacy Study rules so global ID selectors "
        "cannot shrink or restyle dialog controls"
    )
    assert re.search(r"#study-panel\s*\{[^}]*height:\s*100%;", study_css, re.DOTALL)
    assert "@media (max-width: 1100px)" in study_css
    assert "--study-drawer-bottom" in study_css
    assert ".study-control-modal" in css
    assert ".study-control-dialog" in css
    assert "max-height: calc(100dvh" in css
    assert "grid-template-columns: minmax(0, 1fr)" in css
    assert "@media (max-width: 390px)" in css
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert re.search(r"\.study-controls-open\s*\{[^}]*min-height:\s*44px", css, re.DOTALL)
    assert re.search(r"\.study-control-close,[^{]+\{[^}]*min-width:\s*44px", css, re.DOTALL)

    module = STUDY_JS.read_text(encoding="utf-8")
    assert "_queueMutation" in module
    assert "() => _request('/timer/pause', { method: 'POST' }, previousId)" in module
    assert "() => _request('/timer/pause', { method: 'POST' }, closingSessionId)" in module
    assert "ResizeObserver(_syncDrawerClearance)" in module
    assert "_request('/review'" in module
    assert "querySelectorAll('[data-study-result]')" in module
    assert "_request('/initialize'" in module
    assert "_closeControls({ restoreFocus: false })" in module

    admin = ADMIN_JS.read_text(encoding="utf-8")
    assert re.search(r"\bstudy:\s*['\"]study goals and timer progress", admin)
