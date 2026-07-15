"""Security and format validation for durable project attachments."""

from __future__ import annotations

import io
import os
import zipfile

import pytest
from fastapi import HTTPException

import src.project_storage as project_storage
from src.project_storage import (
    ProjectFileStore,
    safe_display_filename,
    validate_project_attachment_bytes,
)


def _raises_status(status: int, function, *args):
    with pytest.raises(HTTPException) as exc:
        function(*args)
    assert exc.value.status_code == status


def _office(marker: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr(marker, "<document />")
    return output.getvalue()


def test_supported_document_text_and_cad_formats(monkeypatch):
    pdf = validate_project_attachment_bytes("analysis.pdf", b"\n%PDF-1.7\n%%EOF")
    assert pdf.mime == "application/pdf"
    assert len(pdf.sha256) == 64

    assert validate_project_attachment_bytes(
        "report.docx", _office("word/document.xml")
    ).mime.endswith("wordprocessingml.document")
    assert validate_project_attachment_bytes(
        "data.xlsx", _office("xl/workbook.xml")
    ).mime.endswith("spreadsheetml.sheet")
    assert validate_project_attachment_bytes(
        "slides.pptx", _office("ppt/presentation.xml")
    ).mime.endswith("presentationml.presentation")

    assert validate_project_attachment_bytes("notes.md", "Δ controls\n".encode()).mime.startswith("text/markdown")
    assert validate_project_attachment_bytes("results.csv", b"x,y\n1,2\n").mime.startswith("text/csv")
    assert validate_project_attachment_bytes("model.json", b'{"ok": true}').mime == "application/json"
    monkeypatch.setattr(project_storage, "_JSON_MAX_BYTES", 2)
    _raises_status(413, validate_project_attachment_bytes, "large.json", b"{}\n")

    step = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
    assert validate_project_attachment_bytes("part.step", step).mime == "application/octet-stream"
    stl = b"solid x\nfacet normal 0 0 1\nouter loop\nendloop\nendfacet\nendsolid x\n"
    assert validate_project_attachment_bytes("part.stl", stl).mime == "application/octet-stream"
    start = b" " * 72 + b"S" + b"      1"
    terminate = b" " * 72 + b"T" + b"      1"
    assert validate_project_attachment_bytes("part.iges", start + b"\n" + terminate + b"\n").mime == "application/octet-stream"


def test_invalid_content_names_extensions_and_size(monkeypatch):
    assert safe_display_filename("résumé final.pdf") == "résumé final.pdf"
    for name in ("../secret.pdf", "folder/file.pdf", "folder\\file.pdf", "bad\x00.pdf", ".."):
        _raises_status(400, safe_display_filename, name)

    _raises_status(400, validate_project_attachment_bytes, "payload.html", b"<script />")
    _raises_status(400, validate_project_attachment_bytes, "fake.pdf", b"not a pdf")
    _raises_status(400, validate_project_attachment_bytes, "fake.docx", b"not a zip")
    _raises_status(400, validate_project_attachment_bytes, "bad.json", b"{]")
    _raises_status(400, validate_project_attachment_bytes, "bad.step", b"hello")
    _raises_status(400, validate_project_attachment_bytes, "empty.txt", b"")

    monkeypatch.setattr(project_storage, "PROJECT_ATTACHMENT_MAX_BYTES", 4)
    _raises_status(413, validate_project_attachment_bytes, "large.txt", b"12345")


def test_store_uses_opaque_confined_atomic_paths_and_rejects_symlinks(tmp_path):
    store = ProjectFileStore(tmp_path / "project-files")
    key = store.storage_key("project-1", "item-1", "attachment-1", ".pdf")
    assert key == "project-1/item-1/attachment-1.pdf"
    path = store.write(key, b"%PDF-1.4\n%%EOF")
    assert store.resolve(key) == path
    assert path.read_bytes().startswith(b"%PDF")
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0

    _raises_status(404, store.resolve, "../outside/item/file.pdf")
    _raises_status(404, store.resolve, "/absolute/item/file.pdf")
    _raises_status(400, store.storage_key, "project/escape", "item", "file", ".pdf")

    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"secret")
    link_key = store.storage_key("project-2", "item-2", "attachment-2", ".pdf")
    link_path = store.resolve(link_key, must_exist=False)
    link_path.parent.mkdir(parents=True)
    try:
        link_path.symlink_to(outside)
    except (OSError, NotImplementedError):
        pass
    else:
        _raises_status(404, store.resolve, link_key)

    store.delete(key)
    assert not path.exists()


def test_binary_stl_structure_and_image_signature():
    binary_stl = b"header".ljust(80, b"\x00") + (1).to_bytes(4, "little") + b"\x00" * 50
    assert validate_project_attachment_bytes("part.stl", binary_stl).size == 134
    _raises_status(
        400,
        validate_project_attachment_bytes,
        "part.stl",
        b"header".ljust(80, b"\x00") + (2).to_bytes(4, "little") + b"\x00" * 50,
    )

    from PIL import Image

    output = io.BytesIO()
    Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(output, format="PNG")
    png = output.getvalue()
    assert validate_project_attachment_bytes("preview.png", png).mime == "image/png"
    _raises_status(400, validate_project_attachment_bytes, "preview.jpg", png)
