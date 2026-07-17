"""Static cross-file contracts for the V3 Inbox frontend slice."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_inbox_workspace_has_labeled_capture_and_explicit_states():
    html = _read("static/index.html")

    assert 'id="inbox-workspace"' in html
    assert 'aria-labelledby="inbox-title"' in html
    assert '<label id="inbox-capture-label" for="inbox-capture-input">Quick capture</label>' in html
    assert 'id="inbox-capture-input" name="content"' in html
    assert "No destination needed." in html
    assert 'id="inbox-loading" class="inbox-state" role="status"' in html
    assert 'id="inbox-error" class="inbox-state" role="alert"' in html
    assert 'id="inbox-empty" class="inbox-state" role="status"' in html
    assert 'id="inbox-pagination" class="inbox-pagination" hidden' in html
    assert 'id="inbox-pagination-error" class="inbox-pagination-error" role="alert"' in html
    assert 'id="inbox-load-more" class="inbox-button"' in html
    assert 'aria-describedby="inbox-pagination-error"' in html
    assert 'id="inbox-live-region"' in html
    assert 'aria-live="polite" aria-atomic="true"' in html
    for status in ("inbox", "processed", "archived", "all"):
        assert f'data-inbox-filter="{status}"' in html


def test_inbox_navigation_and_history_are_wired_without_replacing_sidebar():
    app_py = _read("app.py")
    html = _read("static/index.html")
    registry = _read("static/js/navigation-registry.js")
    shell = _read("static/js/v2NavigationShell.js")
    app = _read("static/app.js")

    assert 'id="tool-inbox-btn"' in html
    assert 'id="rail-inbox"' in html
    assert '@app.get("/inbox")' in app_py
    assert "async def serve_inbox" in app_py
    assert "id: 'inbox'" in registry
    assert "route: '/inbox'" in registry
    assert "containerId: 'inbox-workspace'" in registry
    assert "legacyIds: { rail: ['rail-inbox'], sidebar: ['tool-inbox-btn'] }" in registry
    assert "import inboxModule from './js/inbox.js';" in app
    assert "inboxModule.init(API_BASE);" in app
    assert "'/inbox':    () =>" in app
    assert "window.addEventListener('popstate'" in app
    assert "leaveInboxFor" in shell
    assert "window.inboxModule?.isOpen?.()" in shell
    assert "document.getElementById('tools-section')?.remove()" in shell


def test_inbox_client_uses_versioned_mutations_and_never_claims_unsupported_success():
    source = _read("static/js/inbox.js")

    assert "method: 'POST'" in source
    assert "method: 'PATCH'" in source
    assert "idempotency_key: key" in source
    assert "body: { version:" in source
    for action in ("classify", "process", "archive"):
        assert f"'{action}'" in source
    assert "const DIRECT_PROCESS_KINDS = new Set([" in source
    for kind in (
        "task", "event", "note", "person_update", "decision",
        "reference_material", "expense", "goal", "habit", "someday_idea",
    ):
        assert f"'{kind}'" in source
    assert "Use Archive below." in source
    assert "Processing project information needs a project destination" in source
    assert "No safe automatic processor is available" in source
    assert "if (!response.ok)" in source
    assert "throw error;" in source
    assert "query.set('cursor', text(cursor))" in source
    assert "nextCursor: text(row?.next_cursor) || null" in source
    assert "mergeInboxItems(state.items, page.items)" in source
    assert "state.items = [];" in source
    assert "refs.loadMore.hidden = !state.nextCursor" in source


def test_inbox_cards_expose_text_metadata_and_mobile_targets():
    source = _read("static/js/inbox.js")
    css = _read("static/inbox.css")

    for label in ("Source:", "Classification", "Reason", "Updated", "Status:"):
        assert label in source
    assert "classificationCopy(item)" in source
    assert "formatConfidence" in source
    assert "min-height: 44px" in css
    assert "max-width: 768px" in css
    assert "max-width: 390px" in css
    assert "prefers-reduced-motion" in css
    assert ":focus-visible" in css
    assert "var(--font-family" in css
    assert "var(--accent, var(--red))" in css


def test_inbox_assets_are_preloaded_and_precached_with_cache_bump():
    html = _read("static/index.html")
    sw = _read("static/sw.js")

    assert '/static/inbox.css?v=20260716v21' in html
    assert '<link rel="modulepreload" href="/static/js/inbox.js">' in html
    assert "'/static/inbox.css'" in sw
    assert "'/static/js/inbox.js'" in sw
    assert "const CACHE_NAME = 'restia-v371'" in sw
