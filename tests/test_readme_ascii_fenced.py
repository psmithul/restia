"""Regression guard for the README title presentation.

Originally (#1390) the README opened with an ASCII-art banner that had to live
inside a ``` code fence, otherwise GitHub's markdown collapsed its leading
whitespace and box-drawing rules and rendered it misaligned. The V2 refresh uses
a text title so it cannot accidentally reintroduce an obsolete branded image,
while this guard still catches the original failure mode if an un-fenced ASCII
banner is ever reintroduced.
"""
from pathlib import Path

README = Path(__file__).resolve().parent.parent / "README.md"

# Box-drawing rule from the legacy ASCII banner (the #1390 failure mode).
_RULE = "─" * 10


def _fenced_segments(text: str):
    """Return the segments of *text* that sit INSIDE ``` fences."""
    parts = text.split("```")
    # parts[0] is before the first fence, parts[1] is inside the first fence, ...
    return parts[1::2]


def test_readme_opens_with_restia_title_and_has_no_stale_brand_visuals():
    head = "\n".join(README.read_text(encoding="utf-8").splitlines()[:15])
    assert '<h1 align="center">Restia</h1>' in head
    text = README.read_text(encoding="utf-8")
    assert "docs/restia-wordmark.png" not in text
    assert "docs/restia-browser.jpg" not in text
    assert "docs/restia-v2.jpg" in text
    assert (README.parent / "docs" / "restia-v2.jpg").exists()
    assert not (README.parent / "docs" / "restia-wordmark.png").exists()
    assert not (README.parent / "docs" / "restia-browser.jpg").exists()


def test_reintroduced_ascii_banner_stays_fenced():
    # Defensive: if a box-drawing banner is ever added back, it must be fenced so
    # GitHub renders it monospace-as-typed (the original #1390 regression).
    text = README.read_text(encoding="utf-8")
    if _RULE not in text:
        return
    inside = "\n".join(_fenced_segments(text))
    assert _RULE in inside, "ASCII banner rule must be inside a ``` code fence"


def test_readme_is_restia_v2_first_and_keeps_legacy_brand_out_of_product_copy():
    text = README.read_text(encoding="utf-8")
    lowered = text.lower()
    assert "what's new in v2" in lowered
    assert "today + activity" in lowered
    assert "projects and home link" in lowered
    assert "messages and invitations" in lowered
    assert "invite another restia" in lowered
    assert "connect another restia" in lowered
    assert "chat with the developer" not in lowered
    assert "app.restia.dev" not in lowered
    assert "projectattachmentviewer" not in lowered
    assert "odysseus" not in lowered
    assert "ghcr.io/psmithul/restia:latest" in text
    assert "Word, Excel, and PowerPoint" in text
    assert "supporting services such as STUN and update checks" in text
    assert "Restia only contacts providers" not in text
