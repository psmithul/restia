"""Bounded, text-only previews for validated OOXML project attachments.

Project uploads already verify that DOCX/XLSX/PPTX files are ZIP containers
with the expected package marker. Previewing is a separate trust boundary:
compressed XML is untrusted, so this module never extracts files to disk,
rejects entity declarations, bounds every XML member, and caps the rendered
shape before it can reach either the local browser or a Home Link proxy.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from dataclasses import dataclass
from typing import Any, BinaryIO
from xml.etree import ElementTree as ET


OFFICE_PREVIEW_VERSION = 1
OFFICE_PREVIEW_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
OFFICE_PREVIEW_MIME_BY_EXTENSION = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

_MAX_ARCHIVE_MEMBERS = 10_000
_MAX_XML_MEMBER_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_XML_BYTES = 32 * 1024 * 1024
_MAX_XML_COMPRESSION_RATIO = 200
_MAX_OUTPUT_CHARS = 384 * 1024
_MAX_DOCX_PARAGRAPHS = 3_000
_MAX_SHEETS = 12
_MAX_ROWS_PER_SHEET = 250
_MAX_COLUMNS_PER_SHEET = 50
_MAX_TOTAL_CELLS = 10_000
_MAX_SHARED_STRINGS = 20_000
_MAX_SLIDES = 80
_MAX_CELL_CHARS = 1_000
_MAX_SECTION_TITLE_CHARS = 160
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_FORBIDDEN_XML_RE = re.compile(br"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
_CELL_REF_RE = re.compile(r"^([A-Za-z]+)")


class OfficePreviewError(ValueError):
    """The package cannot be rendered within the safe preview boundary."""


@dataclass
class _PreviewBudget:
    remaining: int = _MAX_OUTPUT_CHARS
    truncated: bool = False

    def take(self, value: object, *, per_value: int = 12_000) -> str:
        text = _clean_text(value)
        if len(text) > per_value:
            text = text[:per_value]
            self.truncated = True
        if len(text) > self.remaining:
            text = text[: max(0, self.remaining)]
            self.truncated = True
        self.remaining -= len(text)
        return text


def _clean_text(value: object) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def _local_name(tag: object) -> str:
    return str(tag or "").rsplit("}", 1)[-1]


def _attribute(element: ET.Element, name: str) -> str:
    for key, value in element.attrib.items():
        if _local_name(key).lower() == name.lower():
            return str(value or "")
    return ""


def _first_descendant(element: ET.Element, name: str) -> ET.Element | None:
    for child in element.iter():
        if _local_name(child.tag) == name:
            return child
    return None


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise OfficePreviewError("Office package is missing required content") from exc
    if info.is_dir() or info.flag_bits & 0x1:
        raise OfficePreviewError("Office package content cannot be previewed")
    if info.file_size < 0 or info.file_size > _MAX_XML_MEMBER_BYTES:
        raise OfficePreviewError("Office package content exceeds the preview limit")
    already_read = int(getattr(archive, "_restia_preview_xml_bytes", 0) or 0)
    if already_read + info.file_size > _MAX_TOTAL_XML_BYTES:
        raise OfficePreviewError("Office package content exceeds the total preview limit")
    if (
        info.file_size > 1024 * 1024
        and (
            info.compress_size <= 0
            or info.file_size > info.compress_size * _MAX_XML_COMPRESSION_RATIO
        )
    ):
        raise OfficePreviewError("Office package compression is unsafe to preview")
    try:
        with archive.open(info, "r") as handle:
            content = handle.read(_MAX_XML_MEMBER_BYTES + 1)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise OfficePreviewError("Office package content could not be read") from exc
    if len(content) > _MAX_XML_MEMBER_BYTES:
        raise OfficePreviewError("Office package content exceeds the preview limit")
    archive._restia_preview_xml_bytes = already_read + len(content)
    return content


def _parse_xml_member(archive: zipfile.ZipFile, name: str) -> ET.Element:
    content = _read_member(archive, name)
    # OOXML normally uses UTF-8. Reject UTF-16/32 preview input instead of
    # letting its interleaved NUL bytes hide a DOCTYPE/ENTITY declaration from
    # the byte-level fail-closed check below. The original remains downloadable.
    if b"\x00" in content[:512] or content.startswith(
        (b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")
    ):
        raise OfficePreviewError("Office package uses an unsafe XML encoding")
    if _FORBIDDEN_XML_RE.search(content):
        raise OfficePreviewError("Office package contains unsafe XML")
    try:
        return ET.fromstring(content)
    except (ET.ParseError, RecursionError) as exc:
        raise OfficePreviewError("Office package contains invalid XML") from exc


def _archive(source: BinaryIO) -> zipfile.ZipFile:
    try:
        source.seek(0)
        archive = zipfile.ZipFile(source, "r")
        if len(archive.infolist()) > _MAX_ARCHIVE_MEMBERS:
            archive.close()
            raise OfficePreviewError("Office package contains too many entries")
        return archive
    except OfficePreviewError:
        raise
    except (AttributeError, OSError, ValueError, zipfile.BadZipFile) as exc:
        raise OfficePreviewError("Office package is not a readable ZIP document") from exc


def _docx_preview(archive: zipfile.ZipFile, budget: _PreviewBudget) -> list[dict[str, Any]]:
    root = _parse_xml_member(archive, "word/document.xml")
    paragraphs: list[str] = []
    paragraph_count = 0
    for paragraph in root.iter():
        if _local_name(paragraph.tag) != "p":
            continue
        paragraph_count += 1
        if paragraph_count > _MAX_DOCX_PARAGRAPHS:
            budget.truncated = True
            break
        pieces: list[str] = []
        for node in paragraph.iter():
            local = _local_name(node.tag)
            if local == "t" and node.text:
                pieces.append(node.text)
            elif local == "tab":
                pieces.append("\t")
            elif local in {"br", "cr"}:
                pieces.append("\n")
        line = budget.take("".join(pieces))
        if line:
            paragraphs.append(line)
        if budget.remaining <= 0:
            budget.truncated = True
            break
    text = "\n\n".join(paragraphs)
    if not text:
        raise OfficePreviewError("No readable Word document text was found")
    return [{"kind": "text", "title": "Document", "text": text}]


def _shared_strings(archive: zipfile.ZipFile) -> tuple[list[str], bool]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return [], False
    root = _parse_xml_member(archive, "xl/sharedStrings.xml")
    values: list[str] = []
    truncated = False
    for item in root.iter():
        if _local_name(item.tag) != "si":
            continue
        if len(values) >= _MAX_SHARED_STRINGS:
            truncated = True
            break
        value = "".join(
            node.text or ""
            for node in item.iter()
            if _local_name(node.tag) == "t"
        )
        cleaned = _clean_text(value)
        if len(cleaned) > _MAX_CELL_CHARS:
            cleaned = cleaned[:_MAX_CELL_CHARS]
            truncated = True
        values.append(cleaned)
    return values, truncated


def _xlsx_sheet_targets(
    archive: zipfile.ZipFile,
) -> list[tuple[str, str]]:
    workbook = _parse_xml_member(archive, "xl/workbook.xml")
    relationships: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in archive.namelist():
        rels = _parse_xml_member(archive, "xl/_rels/workbook.xml.rels")
        for relation in rels.iter():
            if _local_name(relation.tag) != "Relationship":
                continue
            rel_id = _attribute(relation, "Id")
            target = _attribute(relation, "Target")
            if rel_id and target:
                relationships[rel_id] = target

    sheets: list[tuple[str, str]] = []
    for sheet in workbook.iter():
        if _local_name(sheet.tag) != "sheet":
            continue
        title = _clean_text(_attribute(sheet, "name"))[:_MAX_SECTION_TITLE_CHARS]
        relation_id = _attribute(sheet, "id")
        target = relationships.get(relation_id, "")
        if target.startswith("/"):
            member = posixpath.normpath(target.lstrip("/"))
        elif target:
            member = posixpath.normpath(posixpath.join("xl", target))
        else:
            sheet_id = _attribute(sheet, "sheetId")
            member = f"xl/worksheets/sheet{sheet_id}.xml" if sheet_id.isdigit() else ""
        if not member.startswith("xl/worksheets/") or member not in archive.namelist():
            continue
        sheets.append((title or f"Sheet {len(sheets) + 1}", member))
    return sheets


def _column_index(reference: object) -> int | None:
    match = _CELL_REF_RE.match(str(reference or ""))
    if not match:
        return None
    value = 0
    for character in match.group(1).upper():
        value = value * 26 + (ord(character) - 64)
        if value > _MAX_COLUMNS_PER_SHEET:
            return -1
    return value - 1


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    cell_type = _attribute(cell, "t")
    if cell_type == "inlineStr":
        return "".join(
            node.text or ""
            for node in cell.iter()
            if _local_name(node.tag) == "t"
        )
    value_node = next(
        (node for node in cell if _local_name(node.tag) == "v"),
        None,
    )
    raw = value_node.text if value_node is not None and value_node.text is not None else ""
    if cell_type == "s":
        try:
            index = int(raw)
            return shared[index] if 0 <= index < len(shared) else ""
        except (TypeError, ValueError):
            return ""
    if cell_type == "b":
        return "TRUE" if raw == "1" else "FALSE"
    if raw:
        return raw
    formula = next(
        (node.text or "" for node in cell if _local_name(node.tag) == "f"),
        "",
    )
    return f"={formula}" if formula else ""


def _xlsx_preview(archive: zipfile.ZipFile, budget: _PreviewBudget) -> list[dict[str, Any]]:
    shared, shared_truncated = _shared_strings(archive)
    budget.truncated = budget.truncated or shared_truncated
    targets = _xlsx_sheet_targets(archive)
    if not targets:
        raise OfficePreviewError("No readable workbook sheets were found")
    if len(targets) > _MAX_SHEETS:
        targets = targets[:_MAX_SHEETS]
        budget.truncated = True

    sections: list[dict[str, Any]] = []
    total_cells = 0
    for title, member in targets:
        root = _parse_xml_member(archive, member)
        rows: list[list[str]] = []
        row_count = 0
        for row in root.iter():
            if _local_name(row.tag) != "row":
                continue
            row_count += 1
            if row_count > _MAX_ROWS_PER_SHEET or total_cells >= _MAX_TOTAL_CELLS:
                budget.truncated = True
                break
            values: dict[int, str] = {}
            sequential_column = 0
            for cell in row:
                if _local_name(cell.tag) != "c":
                    continue
                column = _column_index(_attribute(cell, "r"))
                if column is None:
                    column = sequential_column
                sequential_column = column + 1
                if column < 0 or column >= _MAX_COLUMNS_PER_SHEET:
                    budget.truncated = True
                    continue
                value = budget.take(
                    _cell_value(cell, shared),
                    per_value=_MAX_CELL_CHARS,
                )
                values[column] = value
                total_cells += 1
                if total_cells >= _MAX_TOTAL_CELLS or budget.remaining <= 0:
                    budget.truncated = True
                    break
            if values:
                width = min(max(values) + 1, _MAX_COLUMNS_PER_SHEET)
                rows.append([values.get(index, "") for index in range(width)])
            if total_cells >= _MAX_TOTAL_CELLS or budget.remaining <= 0:
                break
        if rows:
            sections.append({"kind": "table", "title": title, "rows": rows})
        if total_cells >= _MAX_TOTAL_CELLS or budget.remaining <= 0:
            break
    if not sections:
        raise OfficePreviewError("No readable workbook cells were found")
    return sections


def _pptx_preview(archive: zipfile.ZipFile, budget: _PreviewBudget) -> list[dict[str, Any]]:
    # Parse the package marker as well as slide parts so a corrupt marker does
    # not get a more permissive path merely because slide filenames exist.
    presentation = _parse_xml_member(archive, "ppt/presentation.xml")
    relationships: dict[str, str] = {}
    if "ppt/_rels/presentation.xml.rels" in archive.namelist():
        rels = _parse_xml_member(archive, "ppt/_rels/presentation.xml.rels")
        for relation in rels.iter():
            if _local_name(relation.tag) != "Relationship":
                continue
            rel_id = _attribute(relation, "Id")
            target = _attribute(relation, "Target")
            if rel_id and target:
                relationships[rel_id] = target

    ordered_members: list[str] = []
    for slide_id in presentation.iter():
        if _local_name(slide_id.tag) != "sldId":
            continue
        relation_id = next(
            (
                str(value or "")
                for key, value in slide_id.attrib.items()
                if _local_name(key) == "id" and str(value or "") in relationships
            ),
            "",
        )
        target = relationships.get(relation_id, "")
        if target.startswith("/"):
            member = posixpath.normpath(target.lstrip("/"))
        elif target:
            member = posixpath.normpath(posixpath.join("ppt", target))
        else:
            member = ""
        if _SLIDE_RE.fullmatch(member) and member in archive.namelist():
            ordered_members.append(member)
    if not ordered_members:
        fallback_slides: list[tuple[int, str]] = []
        for name in archive.namelist():
            match = _SLIDE_RE.fullmatch(name)
            if match is not None:
                fallback_slides.append((int(match.group(1)), name))
        ordered_members = [
            name for _, name in sorted(fallback_slides, key=lambda entry: entry[0])
        ]
    slides = list(enumerate(ordered_members, start=1))
    if not slides:
        raise OfficePreviewError("No readable presentation slides were found")
    if len(slides) > _MAX_SLIDES:
        slides = slides[:_MAX_SLIDES]
        budget.truncated = True

    sections: list[dict[str, Any]] = []
    for number, member in slides:
        root = _parse_xml_member(archive, member)
        lines: list[str] = []
        for paragraph in root.iter():
            if _local_name(paragraph.tag) != "p":
                continue
            line = budget.take(
                "".join(
                    node.text or ""
                    for node in paragraph.iter()
                    if _local_name(node.tag) == "t"
                )
            )
            if line:
                lines.append(line)
            if budget.remaining <= 0:
                budget.truncated = True
                break
        sections.append(
            {
                "kind": "text",
                "title": f"Slide {number}",
                "text": "\n".join(lines) or "No readable text on this slide.",
            }
        )
        if budget.remaining <= 0:
            break
    return sections


def sanitize_office_preview_payload(value: object) -> dict[str, Any]:
    """Validate and bound an Office preview, including data from a remote hub."""

    if not isinstance(value, dict) or value.get("version") != OFFICE_PREVIEW_VERSION:
        raise OfficePreviewError("Office preview has an unsupported shape")
    format_name = str(value.get("format") or "").lower()
    if format_name not in {"docx", "xlsx", "pptx"}:
        raise OfficePreviewError("Office preview has an unsupported format")
    raw_sections = value.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections or len(raw_sections) > _MAX_SLIDES:
        raise OfficePreviewError("Office preview has invalid sections")

    budget = _PreviewBudget()
    sections: list[dict[str, Any]] = []
    total_cells = 0
    for raw in raw_sections:
        if not isinstance(raw, dict):
            raise OfficePreviewError("Office preview has an invalid section")
        kind = str(raw.get("kind") or "")
        title = _clean_text(raw.get("title"))[:_MAX_SECTION_TITLE_CHARS] or "Section"
        if kind == "text":
            text = budget.take(raw.get("text"), per_value=_MAX_OUTPUT_CHARS)
            sections.append({"kind": "text", "title": title, "text": text})
        elif kind == "table":
            raw_rows = raw.get("rows")
            if not isinstance(raw_rows, list) or len(raw_rows) > _MAX_ROWS_PER_SHEET:
                raise OfficePreviewError("Office preview has invalid table rows")
            rows: list[list[str]] = []
            for raw_row in raw_rows:
                if not isinstance(raw_row, list) or len(raw_row) > _MAX_COLUMNS_PER_SHEET:
                    raise OfficePreviewError("Office preview has invalid table cells")
                row: list[str] = []
                for raw_cell in raw_row:
                    total_cells += 1
                    if total_cells > _MAX_TOTAL_CELLS:
                        raise OfficePreviewError("Office preview has too many cells")
                    row.append(budget.take(raw_cell, per_value=_MAX_CELL_CHARS))
                rows.append(row)
            sections.append({"kind": "table", "title": title, "rows": rows})
        else:
            raise OfficePreviewError("Office preview has an unsupported section")
    return {
        "version": OFFICE_PREVIEW_VERSION,
        "format": format_name,
        "sections": sections,
        "truncated": bool(value.get("truncated")) or budget.truncated,
    }


def extract_office_preview(source: BinaryIO, extension: str) -> dict[str, Any]:
    """Extract a bounded preview from one verified immutable attachment snapshot."""

    normalized_extension = str(extension or "").lower()
    if normalized_extension not in OFFICE_PREVIEW_MIME_BY_EXTENSION:
        raise OfficePreviewError("This attachment is not a supported Office document")
    budget = _PreviewBudget()
    archive = _archive(source)
    try:
        if normalized_extension == ".docx":
            sections = _docx_preview(archive, budget)
        elif normalized_extension == ".xlsx":
            sections = _xlsx_preview(archive, budget)
        else:
            sections = _pptx_preview(archive, budget)
    finally:
        archive.close()
    return sanitize_office_preview_payload(
        {
            "version": OFFICE_PREVIEW_VERSION,
            "format": normalized_extension[1:],
            "sections": sections,
            "truncated": budget.truncated,
        }
    )


__all__ = [
    "OFFICE_PREVIEW_MAX_RESPONSE_BYTES",
    "OFFICE_PREVIEW_MIME_BY_EXTENSION",
    "OfficePreviewError",
    "extract_office_preview",
    "sanitize_office_preview_payload",
]
