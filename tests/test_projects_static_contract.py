"""Cross-file shell contract for the Projects workflow workspace."""

from __future__ import annotations

import ast
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_PY = ROOT / "app.py"
APP_JS = ROOT / "static" / "app.js"
INDEX_HTML = ROOT / "static" / "index.html"
PROJECTS_JS = ROOT / "static" / "js" / "projects.js"
PROJECTS_CSS = ROOT / "static" / "projects.css"
SW_JS = ROOT / "static" / "sw.js"


class _ElementParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements: dict[str, tuple[str, dict[str, str | None]]] = {}
        self.ids: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        element_id = values.get("id")
        if element_id:
            self.ids.append(element_id)
            self.elements[element_id] = (tag, values)


def _parsed_html() -> _ElementParser:
    parser = _ElementParser()
    parser.feed(INDEX_HTML.read_text(encoding="utf-8"))
    return parser


def test_projects_deep_link_serves_spa_and_registers_api_router():
    source = APP_PY.read_text(encoding="utf-8")
    bootstrap = (ROOT / "src" / "v2" / "bootstrap.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(APP_PY))
    route = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "serve_projects"
    )
    assert any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == "get"
        and decorator.args
        and isinstance(decorator.args[0], ast.Constant)
        and decorator.args[0].value == "/projects"
        for decorator in route.decorator_list
    )
    assert "v2_feature_registry.install(app)" in source
    assert "from routes.project_routes import setup_project_routes" in bootstrap
    assert 'registry.register(FeatureSpec("projects"' in bootstrap


def test_projects_launchers_and_customization_controls_are_semantic_and_unique():
    parsed = _parsed_html()
    counts = Counter(parsed.ids)
    assert not [element_id for element_id, count in counts.items() if count > 1]
    for element_id in ("rail-projects", "tool-projects-btn"):
        assert element_id in parsed.elements
        assert parsed.elements[element_id][0] == "button"

    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'data-ui-key="tool-projects"' in html
    assert "'/projects': 'Projects — Restia'" in html
    assert "'/projects':" in html
    assert '/static/projects.css?v=20260715v2' in html


def test_projects_module_is_wired_as_a_full_workspace():
    app_js = APP_JS.read_text(encoding="utf-8")
    projects_js = PROJECTS_JS.read_text(encoding="utf-8")

    assert "import projectsModule from './js/projects.js';" in app_js
    assert "projectsModule.init(API_BASE" in app_js
    assert "currentUsername: () => window._currentUsername || ''" in app_js
    assert "window._currentUsername = String(d.username || '').trim().toLowerCase()" in app_js
    navigation = (ROOT / "static" / "js" / "navigation-registry.js").read_text(encoding="utf-8")
    assert "NAVIGATION_ITEMS.flatMap" in app_js
    assert "legacyIds: { rail: ['rail-projects'], sidebar: ['tool-projects-btn'] }" in navigation
    assert "'tool-projects':       '#tool-projects-btn, #rail-projects'" in app_js
    assert "'/projects': () =>" in app_js

    for public_method in ("init", "open", "close", "toggle", "isOpen", "focus"):
        assert public_method in projects_js
    assert "projects-workspace" in projects_js
    assert "projects-view" in projects_js
    assert "load-earlier-comments" in projects_js
    assert "comments_next_before" in projects_js


def test_projects_frontend_wires_closed_home_link_federation_surfaces():
    projects_js = PROJECTS_JS.read_text(encoding="utf-8")
    css = PROJECTS_CSS.read_text(encoding="utf-8")

    for endpoint in (
        "/linked-instances",
        "/pairing-invitations",
        "/remote-invitations",
        "/remote-grants/",
        "/api/homelink/projects?include_archived=true",
        "/api/homelink/projects/invitations",
        "/api/homelink/projects/attachments/",
    ):
        assert endpoint in projects_js
    assert "transport === PROJECT_SOURCES.HOME ? '/api/homelink/projects' : '/api/projects'" in projects_js
    assert "On this Restia" in projects_js
    assert "Linked projects" in projects_js
    assert "Owning Restia" in projects_js
    assert "kind: 'instance'" in projects_js
    assert ".projects-invitations" in css
    assert ".projects-linked-members" in css
    assert ".projects-member-status--active" in css
    assert ".projects-member-status--pending" in css
    assert ".projects-pairing" in css


def test_projects_assets_are_precached_and_respect_responsive_accessibility():
    service_worker = SW_JS.read_text(encoding="utf-8")
    css = PROJECTS_CSS.read_text(encoding="utf-8")
    assert "'/static/projects.css'" in service_worker
    assert "'/static/js/projects.js'" in service_worker
    assert "@media" in css
    assert "max-width: 768px" in css
    assert "prefers-reduced-motion" in css
    assert ":focus-visible" in css


def test_projects_drawer_toolbar_commands_cannot_overlap():
    css = PROJECTS_CSS.read_text(encoding="utf-8")
    assert ".projects-workspace.has-task-drawer .projects-toolbar" in css
    assert "grid-template-columns: minmax(0, 1fr);" in css
    assert ".projects-workspace.has-task-drawer .projects-toolbar__actions" in css
    assert "overflow-x: auto;" in css


def test_profile_json_transfer_does_not_promise_project_file_backup():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "Profile Transfer" in html
    assert "./scripts/odysseus-backup snapshot" in html
    assert "For Projects and deliverable files" in html
