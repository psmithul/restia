"""Bounded, text-only OOXML extraction for Projects attachment previews."""

from __future__ import annotations

import io
import zipfile

import pytest

import src.project_office_preview as office_preview
from src.project_office_preview import (
    OfficePreviewError,
    extract_office_preview,
    sanitize_office_preview_payload,
)


def _package(entries: dict[str, str]) -> io.BytesIO:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        for name, value in entries.items():
            archive.writestr(name, value)
    output.seek(0)
    return output


def test_docx_preview_extracts_text_without_turning_markup_into_html():
    source = _package(
        {
            "word/document.xml": """
                <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                  <w:body>
                    <w:p><w:r><w:t>Release plan</w:t></w:r></w:p>
                    <w:p><w:r><w:t>&lt;script&gt;alert(1)&lt;/script&gt;</w:t></w:r></w:p>
                    <w:p><w:r><w:t>safe controls</w:t></w:r></w:p>
                  </w:body>
                </w:document>
            """,
        }
    )
    preview = extract_office_preview(source, ".docx")
    assert preview["format"] == "docx"
    assert preview["sections"] == [
        {
            "kind": "text",
            "title": "Document",
            "text": "Release plan\n\n<script>alert(1)</script>\n\nsafe controls",
        }
    ]


def test_xlsx_preview_preserves_cells_and_formula_as_inert_text():
    source = _package(
        {
            "xl/workbook.xml": """
                <workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
                  <sheets><sheet name="Inputs" sheetId="1" r:id="rId1"/></sheets>
                </workbook>
            """,
            "xl/_rels/workbook.xml.rels": """
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rId1" Target="worksheets/sheet1.xml"/>
                </Relationships>
            """,
            "xl/sharedStrings.xml": """
                <sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                  <si><t>&lt;img src=x onerror=alert(1)&gt;</t></si>
                </sst>
            """,
            "xl/worksheets/sheet1.xml": """
                <worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
                  <sheetData>
                    <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c></row>
                    <row r="2"><c r="A2"><f>HYPERLINK(&quot;javascript:alert(1)&quot;)</f></c></row>
                  </sheetData>
                </worksheet>
            """,
        }
    )
    preview = extract_office_preview(source, ".xlsx")
    assert preview["format"] == "xlsx"
    assert preview["sections"][0]["title"] == "Inputs"
    assert preview["sections"][0]["rows"] == [
        ["<img src=x onerror=alert(1)>", "42"],
        ['=HYPERLINK("javascript:alert(1)")'],
    ]


def test_pptx_preview_extracts_ordered_slide_text():
    source = _package(
        {
            "ppt/presentation.xml": """
                <p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>
            """,
            "ppt/slides/slide2.xml": """
                <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
                  <a:p><a:r><a:t>Second slide</a:t></a:r></a:p>
                </p:sld>
            """,
            "ppt/slides/slide1.xml": """
                <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
                  <a:p><a:r><a:t>First slide</a:t></a:r></a:p>
                </p:sld>
            """,
        }
    )
    preview = extract_office_preview(source, ".pptx")
    assert [(section["title"], section["text"]) for section in preview["sections"]] == [
        ("Slide 1", "First slide"),
        ("Slide 2", "Second slide"),
    ]


def test_pptx_preview_uses_presentation_relationship_order_after_reordering():
    source = _package(
        {
            "ppt/presentation.xml": """
                <p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
                  <p:sldIdLst><p:sldId id="2" r:id="rSecond"/><p:sldId id="1" r:id="rFirst"/></p:sldIdLst>
                </p:presentation>
            """,
            "ppt/_rels/presentation.xml.rels": """
                <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
                  <Relationship Id="rFirst" Target="slides/slide1.xml"/>
                  <Relationship Id="rSecond" Target="slides/slide2.xml"/>
                </Relationships>
            """,
            "ppt/slides/slide1.xml": """
                <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
                  <a:p><a:r><a:t>Originally first</a:t></a:r></a:p>
                </p:sld>
            """,
            "ppt/slides/slide2.xml": """
                <p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
                  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
                  <a:p><a:r><a:t>Moved to front</a:t></a:r></a:p>
                </p:sld>
            """,
        }
    )
    preview = extract_office_preview(source, ".pptx")
    assert [section["text"] for section in preview["sections"]] == [
        "Moved to front",
        "Originally first",
    ]


@pytest.mark.parametrize(
    "source",
    [
        _package({"word/document.xml": "<w:document>"}),
        _package(
            {
                "word/document.xml": (
                    '<!DOCTYPE x [<!ENTITY boom "unsafe">]><document><p>&boom;</p></document>'
                )
            }
        ),
    ],
)
def test_corrupt_or_entity_bearing_office_xml_fails_closed(source):
    with pytest.raises(OfficePreviewError):
        extract_office_preview(source, ".docx")


def test_link_payload_sanitizer_rejects_unbounded_or_active_shapes():
    with pytest.raises(OfficePreviewError):
        sanitize_office_preview_payload(
            {
                "version": 1,
                "format": "xlsx",
                "sections": [
                    {"kind": "html", "title": "Unsafe", "html": "<script />"}
                ],
            }
        )
    with pytest.raises(OfficePreviewError):
        sanitize_office_preview_payload(
            {
                "version": 1,
                "format": "xlsx",
                "sections": [
                    {"kind": "table", "title": "Wide", "rows": [[""] * 51]}
                ],
            }
        )

    cleaned = sanitize_office_preview_payload(
        {
            "version": 1,
            "format": "docx",
            "sections": [
                {"kind": "text", "title": "Doc\u0007", "text": "safe\u0007 text"}
            ],
        }
    )
    assert cleaned["sections"] == [
        {"kind": "text", "title": "Doc", "text": "safe text"}
    ]


def test_archive_uses_one_cumulative_uncompressed_xml_budget(monkeypatch):
    source = _package(
        {
            "ppt/presentation.xml": (
                '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                + (" " * 120)
                + "</p:presentation>"
            ),
            "ppt/slides/slide1.xml": (
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                '<a:p><a:r><a:t>'
                + ("x" * 120)
                + "</a:t></a:r></a:p></p:sld>"
            ),
        }
    )
    monkeypatch.setattr(office_preview, "_MAX_TOTAL_XML_BYTES", 300)
    with pytest.raises(OfficePreviewError, match="total preview limit"):
        extract_office_preview(source, ".pptx")


def test_utf16_xml_cannot_hide_entity_declarations():
    xml = (
        '<?xml version="1.0" encoding="utf-16"?>'
        '<!DOCTYPE x [<!ENTITY boom "unsafe">]>'
        '<document><p><t>&boom;</t></p></document>'
    ).encode("utf-16")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types />")
        archive.writestr("word/document.xml", xml)
    output.seek(0)
    with pytest.raises(OfficePreviewError, match="unsafe XML encoding"):
        extract_office_preview(output, ".docx")
