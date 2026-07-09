from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_EMAIL_LIBRARY = _REPO / "static" / "js" / "emailLibrary.js"
_EMAIL_INBOX = _REPO / "static" / "js" / "emailInbox.js"
_APP_JS = _REPO / "static" / "app.js"
_EMAIL_ROUTES = _REPO / "routes" / "email_routes.py"


def test_email_first_page_limits_stay_small():
    library = _EMAIL_LIBRARY.read_text(encoding="utf-8")
    inbox = _EMAIL_INBOX.read_text(encoding="utf-8")

    assert "const EMAIL_LIST_PAGE_SIZE = 40" in library
    assert "const EMAIL_PREWARM_PAGE_SIZE = 30" in library
    assert "const EMAIL_SIDEBAR_PAGE_SIZE = 30" in inbox
    assert "limit=100" not in library


def test_bulk_done_uses_single_provider_call():
    src = _EMAIL_LIBRARY.read_text(encoding="utf-8")

    assert "/api/email/mark-done/" in src
    bulk_start = src.index("async function _bulkAction(action)")
    bulk_end = src.index("\n}\n\n// _extractName", bulk_start)
    bulk_src = src[bulk_start:bulk_end]
    assert "/api/email/mark-done/" in bulk_src
    assert "/api/email/mark-answered/" not in bulk_src


def test_sidebar_done_uses_single_provider_call():
    src = _EMAIL_INBOX.read_text(encoding="utf-8")
    toggle_start = src.index("async function _toggleDone(")
    toggle_end = src.index("\n}\n\nasync function _createEmailChat", toggle_start)
    toggle_src = src[toggle_start:toggle_end]

    assert "/api/email/mark-done/" in toggle_src
    assert "/api/email/mark-answered/" not in toggle_src
    assert "/api/email/mark-read/" not in toggle_src


def test_remote_email_images_load_by_default():
    src = _EMAIL_LIBRARY.read_text(encoding="utf-8")
    prep_start = src.index("function _prepareEmailInlineImages(html)")
    prep_end = src.index("\n}\n\nfunction _wireEmailInlineImages", prep_start)
    prep_src = src[prep_start:prep_end]

    assert "src = `https:${src}`" in prep_src
    assert "if (isHttp) {" in prep_src
    assert "img.setAttribute('loading', 'eager')" in prep_src
    assert "img.replaceWith(ph)" in prep_src
    assert "Remote image blocked" not in prep_src


def test_email_flag_routes_offload_imap_work():
    src = _EMAIL_ROUTES.read_text(encoding="utf-8")

    assert 'async def mark_done(' in src
    for route in ("mark_unread", "mark_read", "mark_answered", "clear_answered"):
        start = src.index(f"async def {route}(")
        end = src.index("\n    @router.", start)
        body = src[start:end]
        assert "asyncio.to_thread(_mutate_email_flags_sync" in body
        assert "with _imap(" not in body


def test_app_loader_has_fast_fallback():
    src = _APP_JS.read_text(encoding="utf-8")

    assert "setTimeout(hideAppLoader, 900)" in src
    assert "Session restore can be slow" in src


def test_email_index_first_paint_refreshes_silently():
    routes = _EMAIL_ROUTES.read_text(encoding="utf-8")
    library = _EMAIL_LIBRARY.read_text(encoding="utf-8")
    inbox = _EMAIL_INBOX.read_text(encoding="utf-8")

    assert "def _email_index_page(" in routes
    assert '"source": "index"' in routes
    assert '"refreshing": True' in routes
    assert "_asyncio.create_task(_refresh_from_imap())" in routes
    assert "sync.source === 'index' && sync.refreshing" in library
    assert "sync.source !== 'index'" in library
    assert "loadEmails(false, { force: true })" in inbox


def test_cookbook_does_not_autoscan_when_no_cache():
    src = (_REPO / "static" / "js" / "cookbook-hwfit.js").read_text(encoding="utf-8")
    empty_start = src.index("} else if (!allowNetwork) {")
    empty_end = src.index("\n    }\n    if (!canKeepPrevious)", empty_start)
    empty_src = src[empty_start:empty_end]

    assert "_hwfitFetch(true, { autoFromEmpty: true })" not in empty_src
    assert "Click Rescan when you need fresh model recommendations." in empty_src
