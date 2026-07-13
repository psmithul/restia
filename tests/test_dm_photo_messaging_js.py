"""Frontend contract for one-photo-per-message direct messaging."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static/js/messaging.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/style.css").read_text(encoding="utf-8")


def test_photo_picker_is_single_file_and_supports_photo_only_send():
    assert 'id="msg-photo-input"' in JS
    assert 'accept="image/png,image/jpeg,image/webp' in JS
    assert 'id="msg-photo-input"' in JS and 'multiple' not in JS.split('id="msg-photo-input"', 1)[1].split('/>', 1)[0]
    assert "if (!body && !photo) return;" in JS
    assert "payload.attachments = [{" in JS


def test_photo_draft_is_pair_bound_and_paste_does_not_leak_to_global_handlers():
    assert "let _photoDrafts = new Map()" in JS
    assert "const peer = _activeOther;" in JS
    assert "_photoDrafts.get(peer)" in JS
    assert "e.stopImmediatePropagation();" in JS


def test_photo_rendering_uses_only_opaque_ids_and_pair_scoped_endpoint():
    assert "const PHOTO_ID_RE = /^[0-9a-f]{32}$/;" in JS
    assert "`/api/messages/media/${encodeURIComponent(id)}`" in JS
    assert 'rel="noopener noreferrer"' in JS
    assert 'referrerpolicy="no-referrer"' in JS
    assert ".msg-photo img" in CSS


def test_ui_discloses_photo_encryption_boundary_and_federation_cap():
    assert "encrypted at rest, not end-to-end" in JS
    assert "const FEDERATED_PHOTO_MAX = 2 * 1024 * 1024;" in JS
    assert "One photo per message" in JS
