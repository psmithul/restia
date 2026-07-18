"""Contracts for the permanent Search surface across chats and Life."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "static" / "js" / "search-chat.js"


def test_global_search_normalizes_chat_and_owner_scoped_life_results():
    source = MODULE.read_text(encoding="utf-8")

    assert "function chatResults(data)" in source
    assert "session_id" in source
    assert "content_snippet" in source
    assert "function lifeResults(payload)" in source
    assert "const entity = row?.entity || {};" in source
    assert "entity.entity_type" in source
    assert "entity.summary" in source


def test_global_search_queries_both_authorities_and_is_accessible():
    source = MODULE.read_text(encoding="utf-8")
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "style.css").read_text(encoding="utf-8")

    assert "Promise.allSettled" in source
    assert "`${API_BASE}/api/search?q=${encoded}&limit=20`" in source
    assert "`${API_BASE}/api/life/search?q=${encoded}&limit=20`" in source
    assert "credentials: 'same-origin'" in source
    assert "window.activateNavigationItem('life')" in source
    assert "window.lifeWorkspaceModule.revealLifeEntity(entityId)" in source
    assert 'role="dialog" aria-modal="true"' in html
    assert 'placeholder="Search chats and Life…"' in html
    assert 'aria-live="polite"' in html
    assert "min-height: 44px" in css

    life_workspace = (ROOT / "static" / "js" / "lifeWorkspace.js").read_text(
        encoding="utf-8"
    )
    assert "export async function getLifeEntity(entityId" in life_workspace
    assert "export async function revealLifeEntity(entityId)" in life_workspace
    assert "'data-life-entity-id': item.id" in life_workspace
