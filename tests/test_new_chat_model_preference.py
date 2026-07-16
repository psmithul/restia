from pathlib import Path


APP_JS = Path("static/app.js")


def _slice(source, start_marker, end_marker):
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_new_chat_prefers_pending_and_current_model_before_default():
    source = APP_JS.read_text(encoding="utf-8")
    helper = _slice(
        source,
        "async function _createDirectChatFromPreferredModel()",
        "// ============================================",
    )

    default_pos = helper.index("const dc = await _refreshDefaultChat();")
    assert helper.index("sessionModule.getPendingChat") < default_pos
    assert helper.index("current.endpoint_url") < default_pos
    assert default_pos < helper.index("const withModel = sessions.filter")


def test_desktop_new_chat_actions_use_shared_preference_helper_without_brand_alias():
    source = APP_JS.read_text(encoding="utf-8")

    shared_handler = _slice(
        source,
        "async function _handleNewChatAction",
        "// New session button on icon rail",
    )
    rail_handler = _slice(
        source,
        "// New session button on icon rail",
        "// Mobile new chat button",
    )
    sidebar_handler = _slice(
        source,
        "// The Restia brand is display-only.",
        "// Delete session button on icon rail",
    )

    assert "if (preferModel && await _createDirectChatFromPreferredModel()) return;" in shared_handler
    assert "await _handleNewChatAction();" in rail_handler
    assert "await _handleNewChatAction();" in sidebar_handler
    assert "const dc = await _refreshDefaultChat();" not in rail_handler
    assert "const dc = await _refreshDefaultChat();" not in sidebar_handler
    assert "const brandBtn = el('sidebar-brand-btn');" not in source
