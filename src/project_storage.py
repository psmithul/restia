"""Durable, path-confined storage for project task attachments.

Project files are intentionally separate from chat uploads: chat uploads have a
retention window, while project deliverables are first-class user data.  The DB
stores an opaque relative ``storage_key``; original filenames are display-only
metadata and never participate in filesystem resolution.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import codecs
import os
import re
import shutil
import tempfile
import time
import warnings
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fastapi import HTTPException, UploadFile

from src.upload_limits import PROJECT_ATTACHMENT_MAX_BYTES, format_byte_limit, read_upload_limited


logger = logging.getLogger(__name__)

_SAFE_SEGMENT_RE = re.compile(r"^[0-9A-Za-z_-]{1,80}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_OOXML_MARKERS = {
    ".docx": ("word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".xlsx": ("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".pptx": ("ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
}
_IMAGE_FORMATS = {
    ".png": ("PNG", "image/png"),
    ".jpg": ("JPEG", "image/jpeg"),
    ".jpeg": ("JPEG", "image/jpeg"),
    ".webp": ("WEBP", "image/webp"),
    ".gif": ("GIF", "image/gif"),
}
_TEXT_FORMATS = {
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
}
_CAD_FORMATS = {".stl", ".step", ".stp", ".iges", ".igs"}
_ALLOWED_EXTENSIONS = {
    ".pdf", ".zip", *_OOXML_MARKERS, *_IMAGE_FORMATS, *_TEXT_FORMATS, *_CAD_FORMATS,
}
_MAX_IMAGE_PIXELS = 40_000_000
_JSON_MAX_BYTES = 10 * 1024 * 1024
_NON_ASCII_CAD_RE = re.compile(rb"[^\x09\x0a\x0d\x20-\x7e]")
PROJECT_RECONCILE_GRACE_SECONDS = 60 * 60
PROJECT_RECONCILE_MAX_ENTRIES = 10_000
PROJECT_RECONCILE_TIME_BUDGET_SECONDS = 1.0
PROJECT_RECONCILE_MAX_MISSING_LOGS = 100


@dataclass(frozen=True)
class ValidatedProjectUpload:
    display_name: str
    extension: str
    mime: str
    size: int
    sha256: str
    data: bytes


def safe_display_filename(value: object) -> str:
    """Validate a display filename without reducing Unicode to ASCII.

    Browsers may send a full fake path. Treat separators and controls as an
    invalid request instead of silently accepting a path-shaped filename; the
    actual stored name is opaque regardless.
    """

    name = str(value or "").strip()
    if not name:
        raise HTTPException(400, "Attachment filename is required")
    if "/" in name or "\\" in name or _CONTROL_RE.search(name):
        raise HTTPException(400, "Attachment filename is invalid")
    name = name.lstrip(".").strip()
    if not name or name in {".", ".."}:
        raise HTTPException(400, "Attachment filename is invalid")
    # Bound header/DB size while retaining the verified extension.
    suffix = Path(name).suffix
    if len(name) > 240:
        stem_budget = max(1, 240 - len(suffix))
        name = name[:stem_budget].rstrip() + suffix
    return name


def _validate_zip(data: bytes, extension: str) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            # Reading the central directory is enough: project attachments are
            # never extracted server-side, so member paths cannot traverse the
            # host filesystem. Bound absurd member counts as a cheap abuse gate.
            names = archive.namelist()
            if len(names) > 100_000:
                raise HTTPException(413, "Archive contains too many files")
            if extension in _OOXML_MARKERS:
                marker, mime = _OOXML_MARKERS[extension]
                if marker not in names:
                    raise HTTPException(400, f"File is not a valid {extension[1:].upper()} document")
                return mime
    except HTTPException:
        raise
    except (zipfile.BadZipFile, OSError, ValueError):
        raise HTTPException(400, "File is not a valid ZIP archive")
    return "application/zip"


def _validate_image(data: bytes, extension: str) -> str:
    try:
        from PIL import Image, ImageFile

        ImageFile.LOAD_TRUNCATED_IMAGES = False
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = int(image.width or 0), int(image.height or 0)
                if width < 1 or height < 1 or width * height > _MAX_IMAGE_PIXELS:
                    raise HTTPException(413, "Image dimensions are too large")
                expected, mime = _IMAGE_FORMATS[extension]
                if str(image.format or "").upper() != expected:
                    raise HTTPException(400, "Image content does not match its filename")
                image.verify()
                return mime
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "File is not a valid image")


def _validate_text(data: bytes, extension: str) -> str:
    if b"\x00" in data:
        raise HTTPException(400, "Text attachment contains binary data")
    try:
        decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
        for offset in range(0, len(data), 64 * 1024):
            decoder.decode(data[offset : offset + 64 * 1024], final=False)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise HTTPException(400, "Text attachments must use UTF-8 encoding")
    if extension == ".json":
        if len(data) > _JSON_MAX_BYTES:
            raise HTTPException(413, "JSON attachments are limited to 10 MB")
        try:
            json.loads(data)
        except (json.JSONDecodeError, RecursionError):
            raise HTTPException(400, "File is not valid JSON")
    return _TEXT_FORMATS[extension]


def _validate_cad(data: bytes, extension: str) -> str:
    """Cheap structural validation for inert CAD exchange deliverables.

    These files are never parsed or rendered server-side and downloads always
    carry ``Content-Disposition: attachment`` plus ``nosniff``. Validation is
    deliberately structural rather than a full CAD parser.
    """

    if extension == ".stl":
        # Binary STL: 80-byte header, uint32 little-endian triangle count, then
        # exactly 50 bytes per triangle. ASCII STL has explicit solid/facet/end.
        if len(data) >= 84:
            triangle_count = int.from_bytes(data[80:84], "little")
            if triangle_count <= 20_000_000 and len(data) == 84 + triangle_count * 50:
                return "application/octet-stream"
        try:
            prefix = data[:4096].lstrip().lower()
            suffix = data[-4096:].rstrip().lower()
        except (AttributeError, ValueError):
            raise HTTPException(400, "File is not a valid STL model")
        if not (
            prefix.startswith(b"solid")
            and re.search(rb"facet\s+normal", data, flags=re.IGNORECASE)
            and b"endsolid" in suffix
            and not _NON_ASCII_CAD_RE.search(data)
        ):
            raise HTTPException(400, "File is not a valid STL model")
        return "application/octet-stream"

    if b"\x00" in data:
        raise HTTPException(400, "CAD exchange file contains invalid binary data")
    if _NON_ASCII_CAD_RE.search(data):
        raise HTTPException(400, "CAD exchange files must use ASCII encoding")
    if extension in {".step", ".stp"}:
        if b"ISO-10303-21;" not in data[:4096].upper() or b"END-ISO-10303-21;" not in data[-4096:].upper():
            raise HTTPException(400, "File is not a valid STEP exchange model")
    else:
        # Only the first/last 50 80-column records can contain the Start and
        # Terminate sentinels. Bound splitting to small windows instead of
        # duplicating a 50 MB model into millions of Python line objects.
        first_lines = [line for line in data[:8192].splitlines()[:50] if line.strip()]
        last_lines = [line for line in data[-8192:].splitlines()[-50:] if line.strip()]
        if not first_lines or not any(len(line) >= 73 and line[72:73].upper() == b"S" for line in first_lines):
            raise HTTPException(400, "File is not a valid IGES exchange model")
        if not any(len(line) >= 73 and line[72:73].upper() == b"T" for line in last_lines):
            raise HTTPException(400, "File is not a valid IGES exchange model")
    return "application/octet-stream"


def validate_project_attachment_bytes(filename: object, data: bytes) -> ValidatedProjectUpload:
    display_name = safe_display_filename(filename)
    extension = Path(display_name).suffix.lower()
    if extension not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            400,
            "Unsupported attachment type. Use PDF, Office, image, ZIP, text, data, or CAD exchange files.",
        )
    if not data:
        raise HTTPException(400, "Attachment is empty")
    if len(data) > PROJECT_ATTACHMENT_MAX_BYTES:
        raise HTTPException(
            413,
            f"Attachment exceeds {format_byte_limit(PROJECT_ATTACHMENT_MAX_BYTES)} limit",
        )

    if extension == ".pdf":
        if not data.lstrip().startswith(b"%PDF-"):
            raise HTTPException(400, "File is not a valid PDF")
        mime = "application/pdf"
    elif extension == ".zip" or extension in _OOXML_MARKERS:
        mime = _validate_zip(data, extension)
    elif extension in _IMAGE_FORMATS:
        mime = _validate_image(data, extension)
    elif extension in _TEXT_FORMATS:
        mime = _validate_text(data, extension)
    else:
        mime = _validate_cad(data, extension)

    return ValidatedProjectUpload(
        display_name=display_name,
        extension=extension,
        mime=mime,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        data=data,
    )


async def read_project_attachment(upload: UploadFile) -> ValidatedProjectUpload:
    data = await read_upload_limited(
        upload,
        PROJECT_ATTACHMENT_MAX_BYTES,
        label="Project attachment",
    )
    return validate_project_attachment_bytes(upload.filename, data)


async def write_project_attachment_cancellation_safe(
    store: "ProjectFileStore",
    storage_key: str,
    data: bytes,
) -> Path:
    """Run the atomic writer without leaving a file behind on cancellation.

    Cancelling ``asyncio.to_thread`` only cancels its asyncio waiter; the worker
    thread keeps running. Shielding the task and then waiting for it to settle
    ensures cleanup cannot race a late ``os.replace`` from that worker.
    """

    write_task = asyncio.create_task(asyncio.to_thread(store.write, storage_key, data))
    try:
        return await asyncio.shield(write_task)
    except asyncio.CancelledError:
        # A task may be cancelled repeatedly (for example timeout followed by
        # connection teardown). Each wait must remain shielded so a later
        # cancellation cannot cancel the asyncio waiter while its worker thread
        # keeps running and recreates the file after cleanup.
        while not write_task.done():
            try:
                await asyncio.shield(write_task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        try:
            write_task.result()
        except BaseException:
            # Preserve cancellation as the request outcome. The writer's own
            # atomic-finally path removes its temp file; delete below covers a
            # final file created before a late write/fsync error.
            logger.warning(
                "Project attachment writer failed while a cancelled request was settling key %r",
                storage_key,
                exc_info=True,
            )
        store.delete(storage_key)
        raise


class ProjectFileStore:
    """Atomic durable file operations beneath one private storage root."""

    def __init__(self, root: str | os.PathLike[str] | None = None):
        if root is None:
            from src.blob_store import resolve_blob_store_config

            root = resolve_blob_store_config(create=True).project_root
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass

    @staticmethod
    def _segment(value: object) -> str:
        segment = str(value or "")
        if not _SAFE_SEGMENT_RE.fullmatch(segment):
            raise HTTPException(400, "Invalid project file identifier")
        return segment

    def storage_key(
        self,
        project_id: object,
        work_item_id: object,
        attachment_id: object,
        extension: str,
    ) -> str:
        project = self._segment(project_id)
        item = self._segment(work_item_id)
        attachment = self._segment(attachment_id)
        if extension not in _ALLOWED_EXTENSIONS:
            raise HTTPException(400, "Invalid attachment extension")
        return f"{project}/{item}/{attachment}{extension}"

    def resolve(self, storage_key: object, *, must_exist: bool = True) -> Path:
        raw = str(storage_key or "")
        pure = PurePosixPath(raw)
        if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
            raise HTTPException(404, "Attachment not found")
        if len(pure.parts) != 3:
            raise HTTPException(404, "Attachment not found")
        for part in pure.parts[:2]:
            self._segment(part)
        filename = pure.parts[-1]
        stem, extension = os.path.splitext(filename)
        self._segment(stem)
        if extension.lower() not in _ALLOWED_EXTENSIONS:
            raise HTTPException(404, "Attachment not found")

        candidate = self.root.joinpath(*pure.parts)
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise HTTPException(404, "Attachment not found")
        if must_exist and (not resolved.is_file() or candidate.is_symlink()):
            raise HTTPException(404, "Attachment not found")
        return resolved

    def write(self, storage_key: str, data: bytes) -> Path:
        path = self.resolve(storage_key, must_exist=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        fd, temporary = tempfile.mkstemp(prefix=".project-file-", dir=path.parent)
        try:
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                pass
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
            return path
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def delete(self, storage_key: str) -> None:
        try:
            path = self.resolve(storage_key, must_exist=False)
            path.unlink(missing_ok=True)
            for parent in (path.parent, path.parent.parent):
                try:
                    parent.rmdir()
                except OSError:
                    break
        except HTTPException:
            # A malformed DB value must never turn deletion into an arbitrary
            # filesystem operation. Leave it for an operator-visible orphan audit.
            logger.error("Refused unsafe project attachment cleanup key %r", storage_key)
        except OSError:
            # The route commits metadata deletion first. Do not report a false
            # 500 after that irreversible commit; retain a loud operator log so
            # the opaque orphan can be retried/audited without exposing a path.
            logger.exception("Project attachment cleanup failed for key %r", storage_key)

    def delete_project(self, project_id: object) -> None:
        project = self._segment(project_id)
        target = (self.root / project).resolve(strict=False)
        try:
            target.relative_to(self.root)
        except ValueError:
            return
        if target.is_dir() and not (self.root / project).is_symlink():
            try:
                shutil.rmtree(target)
            except OSError:
                logger.exception("Project file-tree cleanup failed for project %s", project)

    def reconcile(
        self,
        referenced_storage_keys: Iterable[str],
        *,
        delete_durable_orphans: bool = False,
        grace_seconds: float = PROJECT_RECONCILE_GRACE_SECONDS,
        max_entries: int = PROJECT_RECONCILE_MAX_ENTRIES,
        time_budget_seconds: float = PROJECT_RECONCILE_TIME_BUDGET_SECONDS,
    ) -> dict[str, int | bool]:
        """Remove stale private temp files and optionally durable orphans.

        Filesystem traversal is entry- and time-bounded, never follows
        symlinks, and recognizes only this store's three-segment attachment
        paths or its private atomic-temp prefix. Unknown files are left alone.

        Durable attachment files are preserved by default. A caller may be
        connected to a different or incomplete database while sharing this
        storage root, so absence from one reference scan is not proof that a
        durable file is safe to delete. Explicit maintenance may opt in only
        after establishing that the reference set is authoritative.
        """

        if grace_seconds < 0:
            raise ValueError("grace_seconds must not be negative")
        if max_entries < 1:
            raise ValueError("max_entries must be greater than 0")
        if time_budget_seconds <= 0:
            raise ValueError("time_budget_seconds must be greater than 0")

        started = time.monotonic()
        referenced_paths: set[Path] = set()
        referenced_count = 0
        missing_count = 0
        reference_scan_truncated = False
        for raw_key in referenced_storage_keys:
            if time.monotonic() - started >= time_budget_seconds:
                reference_scan_truncated = True
                break
            storage_key = str(raw_key or "")
            try:
                path = self.resolve(storage_key, must_exist=False)
            except HTTPException:
                logger.error(
                    "Project attachment metadata contains an unsafe storage key %r",
                    storage_key,
                )
                continue
            if path not in referenced_paths:
                referenced_paths.add(path)
                referenced_count += 1
            try:
                self.resolve(storage_key, must_exist=True)
            except HTTPException:
                missing_count += 1
                if missing_count <= PROJECT_RECONCILE_MAX_MISSING_LOGS:
                    logger.warning(
                        "Project attachment metadata references a missing file for key %r",
                        storage_key,
                    )
        if missing_count > PROJECT_RECONCILE_MAX_MISSING_LOGS:
            logger.warning(
                "%d additional project attachment references are missing files",
                missing_count - PROJECT_RECONCILE_MAX_MISSING_LOGS,
            )

        # An incomplete reference set must never be used to declare anything
        # orphaned. Abort deletion entirely if enumerating DB keys consumed the
        # startup budget; the next restart can make another bounded pass.
        if reference_scan_truncated:
            logger.info(
                "Project attachment reconciliation stopped while reading references at its %.2fs bound",
                time_budget_seconds,
            )
            return {
                "referenced": referenced_count,
                "missing": missing_count,
                "scanned_entries": 0,
                "deleted_orphans": 0,
                "deleted_temps": 0,
                "skipped_recent": 0,
                "truncated": True,
            }

        cutoff = time.time() - float(grace_seconds)
        scanned_entries = 0
        deleted_orphans = 0
        deleted_temps = 0
        skipped_recent = 0
        truncated = False
        stack = [self.root]

        while stack and not truncated:
            directory = stack.pop()
            try:
                entries = os.scandir(directory)
            except OSError:
                logger.warning(
                    "Could not scan project attachment directory %s",
                    directory,
                    exc_info=True,
                )
                continue
            with entries:
                for entry in entries:
                    if (
                        scanned_entries >= max_entries
                        or time.monotonic() - started >= time_budget_seconds
                    ):
                        truncated = True
                        break
                    scanned_entries += 1
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        candidate = Path(entry.path)
                        resolved = candidate.resolve(strict=False)
                        relative = resolved.relative_to(self.root)
                    except (OSError, ValueError):
                        logger.warning(
                            "Skipped unsafe project attachment reconciliation entry %r",
                            entry.path,
                        )
                        continue

                    if resolved in referenced_paths:
                        continue
                    parts = relative.parts
                    if len(parts) != 3:
                        continue
                    try:
                        self._segment(parts[0])
                        self._segment(parts[1])
                    except HTTPException:
                        continue

                    is_temp = parts[2].startswith(".project-file-")
                    if not is_temp:
                        try:
                            expected = self.resolve(relative.as_posix(), must_exist=False)
                        except HTTPException:
                            continue
                        if expected != resolved:
                            continue
                        if not delete_durable_orphans:
                            continue
                    try:
                        modified_at = entry.stat(follow_symlinks=False).st_mtime
                    except (FileNotFoundError, OSError):
                        continue
                    if modified_at > cutoff:
                        skipped_recent += 1
                        continue
                    try:
                        candidate.unlink()
                    except FileNotFoundError:
                        continue
                    except OSError:
                        logger.warning(
                            "Could not remove orphan project attachment entry %s",
                            relative.as_posix(),
                            exc_info=True,
                        )
                        continue
                    if is_temp:
                        deleted_temps += 1
                    else:
                        deleted_orphans += 1

        if truncated:
            logger.info(
                "Project attachment reconciliation stopped at its bound after %d entries",
                scanned_entries,
            )
        return {
            "referenced": referenced_count,
            "missing": missing_count,
            "scanned_entries": scanned_entries,
            "deleted_orphans": deleted_orphans,
            "deleted_temps": deleted_temps,
            "skipped_recent": skipped_recent,
            "truncated": truncated,
        }
