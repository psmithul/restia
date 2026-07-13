"""Group chat must not discard files or block attachment-only turns."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_group_submit_uploads_pending_files_before_sending():
    src = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    block = src[src.index("// Group chat: route to group module"):src.index("return originalSubmit.call", src.index("// Group chat: route to group module"))]
    assert "getPendingCount" in block
    assert "uploadPending" in block
    assert "if (!msg && !pendingCount)" in block
    assert "groupModule.sendMessage(msg, attachmentIds, attachments)" in block
    assert "setTimeout(() => { _submitting = false; }, 300)" not in src
    assert "finally {\n      _submitting = false;\n    }" in src


def test_group_streams_same_authorized_attachment_ids_to_each_participant():
    src = (ROOT / "static" / "js" / "group.js").read_text(encoding="utf-8")
    assert "sendMessage(msg, attachmentIds = [], attachments = [])" in src
    assert "fd.append('attachments', JSON.stringify(attachmentIds))" in src
    assert "metadata: attachments.length ? { attachments } : undefined" in src
    assert "export function getParentSessionId()" in src


def test_global_paste_does_not_stage_files_from_other_composers():
    src = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    paste = src[src.index("// Paste handler"):src.index("// Message count", src.index("// Paste handler"))]
    assert "#messages-modal" in paste
    assert "if (!mainComposer && otherComposer) return" in paste


def test_compare_uploads_once_and_forwards_attachment_ids_to_every_pane():
    index = (ROOT / "static" / "js" / "compare" / "index.js").read_text(encoding="utf-8")
    stream = (ROOT / "static" / "js" / "compare" / "stream.js").read_text(encoding="utf-8")
    assert "await fileHandlerModule.uploadPending()" in index
    assert "attachments: attachmentIds" in index
    assert "if (!message && !pendingCount) return" in index
    assert "fd.append('attachments', JSON.stringify(opts.attachments))" in stream
    app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "return await compareModule.handleCompareSubmit(e)" in app
