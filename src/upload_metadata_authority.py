"""Canonical Account.id-owned SQL authority for assistant-chat uploads.

Only opaque routing fields are searchable.  Original names, client origin,
content hash, dimensions, and media metadata live in one encrypted ORM payload.
The filesystem blob key is path-confined by :mod:`src.blob_store` and is never
accepted from a request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from core.database import Account, SessionLocal
from src.auth_helpers import DEFAULT_LOCAL_OWNER, resolved_runtime_owner
from src.blob_store import BlobKeyError, FileSystemBlobStore
from src.identity import ensure_account, find_account
from src.secret_storage import private_digest
from src.upload_metadata_models import (
    ChatUploadMetadata,
    ChatUploadMetadataImportRun,
)


logger = logging.getLogger(__name__)

LEGACY_UPLOAD_INDEX_MAX_BYTES = 8 * 1024 * 1024
LEGACY_UPLOAD_INDEX_MAX_ROWS = 10_000
UPLOAD_IMPORT_SOURCE_KIND = "uploads_json"
_UPLOAD_ID_RE = re.compile(r"^[0-9a-fA-F]{32}(?:\.[A-Za-z0-9]+)?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UploadMetadataAuthorityError(RuntimeError):
    """Canonical upload metadata could not be accessed safely."""


@dataclass(frozen=True)
class LegacyUploadImportResult:
    attempted: bool
    recovered_from_backup: bool
    imported: int
    skipped: int
    failed: int
    source_preserved: bool = True


def _normalize_db_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is not None:
        result = result.astimezone(timezone.utc).replace(tzinfo=None)
    return result


def _db_now(db) -> datetime:
    return _normalize_db_time(
        db.execute(select(func.current_timestamp())).scalar_one()
    )


def _begin_write(db) -> None:
    if db.get_bind().dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _account(db, owner: object, *, create: bool) -> Account | None:
    raw = str(owner or "").strip()
    if not raw:
        raw = DEFAULT_LOCAL_OWNER
    direct = db.query(Account).filter(
        Account.id == raw,
        Account.status == "active",
    ).one_or_none()
    if direct is not None:
        return direct
    return ensure_account(db, raw) if create else find_account(db, raw)


def _content_digest(owner_id: str, content_sha256: str) -> str:
    normalized = str(content_sha256 or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise UploadMetadataAuthorityError("Upload content digest is invalid")
    return private_digest(f"chat-upload-content-v1:{owner_id}", normalized)


def _safe_int(value: object, *, minimum: int = 0) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= minimum else None


def _parse_timestamp(value: object, fallback: datetime) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return fallback
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _hash_file(path: Path, *, max_bytes: int) -> str | None:
    try:
        if path.stat().st_size > max_bytes:
            return None
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


class SQLUploadMetadataAuthority:
    """Small transactional facade used by UploadHandler and its consumers."""

    mode = "sql"

    def __init__(
        self,
        blob_store: FileSystemBlobStore,
        *,
        session_factory=SessionLocal,
        retention_days: int = 30,
    ):
        if retention_days < 1:
            raise ValueError("retention_days must be positive")
        self.blob_store = blob_store
        self.session_factory = session_factory
        self.retention_days = int(retention_days)

    def _row_info(
        self,
        db,
        row: ChatUploadMetadata,
        *,
        require_blob: bool,
    ) -> dict[str, Any] | None:
        if row.state != "active":
            return None
        payload = row.payload if isinstance(row.payload, dict) else {}
        try:
            path = self.blob_store.resolve(
                row.blob_key, must_exist=require_blob
            )
        except (BlobKeyError, FileNotFoundError, OSError):
            return None
        owner = db.query(Account).filter(Account.id == row.owner_id).one_or_none()
        if owner is None or owner.status != "active":
            return None
        content_sha256 = str(payload.get("content_sha256") or "").lower()
        if not _SHA256_RE.fullmatch(content_sha256):
            logger.error("Upload metadata payload failed integrity validation")
            return None
        name = str(payload.get("name") or row.id)
        original_name = str(payload.get("original_name") or name)
        mime = str(payload.get("mime") or "application/octet-stream")
        size = _safe_int(payload.get("size"), minimum=0)
        if size is None:
            return None
        result = {
            "id": row.id,
            "path": str(path),
            "blob_key": row.blob_key,
            "mime": mime,
            "size": size,
            "name": name,
            "hash": content_sha256,
            "original_name": original_name,
            "uploaded_at": str(
                payload.get("uploaded_at")
                or row.created_at.isoformat()
            ),
            "last_accessed": row.last_accessed_at.isoformat(),
            "client_ip": str(payload.get("client_ip") or ""),
            "owner": owner.username,
            "owner_id": row.owner_id,
            "width": _safe_int(payload.get("width"), minimum=1),
            "height": _safe_int(payload.get("height"), minimum=1),
            "_version": row.version,
        }
        return result

    def get(
        self,
        upload_id: object,
        *,
        owner: object | None = None,
        unscoped: bool = False,
        require_blob: bool = True,
    ) -> dict[str, Any] | None:
        value = str(upload_id or "")
        if not _UPLOAD_ID_RE.fullmatch(value):
            return None
        db = self.session_factory()
        try:
            query = db.query(ChatUploadMetadata).filter(
                ChatUploadMetadata.id == value,
                ChatUploadMetadata.state == "active",
            )
            if not unscoped:
                account = _account(db, owner, create=False)
                if account is None:
                    return None
                query = query.filter(ChatUploadMetadata.owner_id == account.id)
            row = query.one_or_none()
            return (
                self._row_info(db, row, require_blob=require_blob)
                if row is not None else None
            )
        finally:
            db.close()

    def resolve_for_access(
        self,
        upload_id: object,
        *,
        owner: object | None = None,
        unscoped: bool = False,
    ) -> dict[str, Any] | None:
        """Atomically authorize a read and renew its retention lease.

        Cleanup fences on ``version``. Locking the row while it is validated
        and renewed means a cleanup replica either tombstones first (and this
        read fails) or observes the incremented version and cannot delete the
        blob being returned.
        """

        value = str(upload_id or "")
        if not _UPLOAD_ID_RE.fullmatch(value):
            return None
        db = self.session_factory()
        try:
            _begin_write(db)
            query = db.query(ChatUploadMetadata).filter(
                ChatUploadMetadata.id == value,
                ChatUploadMetadata.state == "active",
            )
            if not unscoped:
                account = _account(db, owner, create=False)
                if account is None:
                    db.rollback()
                    return None
                query = query.filter(ChatUploadMetadata.owner_id == account.id)
            if db.get_bind().dialect.name == "postgresql":
                query = query.with_for_update()
            row = query.one_or_none()
            if row is None:
                db.rollback()
                return None
            info = self._row_info(db, row, require_blob=True)
            if info is None:
                db.rollback()
                return None
            clock = _db_now(db)
            row.last_accessed_at = clock
            row.retention_until = clock + timedelta(days=self.retention_days)
            row.version = int(row.version) + 1
            row.updated_at = clock
            db.commit()
            info["last_accessed"] = clock.isoformat()
            info["_version"] = row.version
            return info
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def find_duplicate(
        self,
        owner: object,
        content_sha256: str,
        *,
        require_blob: bool,
    ) -> dict[str, Any] | None:
        db = self.session_factory()
        try:
            account = _account(db, owner, create=False)
            if account is None:
                return None
            digest = _content_digest(account.id, content_sha256)
            row = db.query(ChatUploadMetadata).filter(
                ChatUploadMetadata.owner_id == account.id,
                ChatUploadMetadata.content_digest == digest,
                ChatUploadMetadata.state == "active",
            ).one_or_none()
            return (
                self._row_info(db, row, require_blob=require_blob)
                if row is not None else None
            )
        finally:
            db.close()

    def find_content_row(
        self,
        owner: object,
        content_sha256: str,
    ) -> dict[str, Any] | None:
        """Return the minimal CAS descriptor for active or retired content."""

        db = self.session_factory()
        try:
            account = _account(db, owner, create=False)
            if account is None:
                return None
            digest = _content_digest(account.id, content_sha256)
            row = db.query(ChatUploadMetadata).filter(
                ChatUploadMetadata.owner_id == account.id,
                ChatUploadMetadata.content_digest == digest,
            ).one_or_none()
            if row is None:
                return None
            info = (
                self._row_info(db, row, require_blob=False)
                if row.state == "active" else None
            )
            return {
                "id": row.id,
                "blob_key": row.blob_key,
                "state": row.state,
                "version": row.version,
                "info": info,
                "blob_exists": self.blob_store.exists(row.blob_key),
            }
        finally:
            db.close()

    @staticmethod
    def _encrypted_payload(metadata: dict[str, Any]) -> dict[str, Any]:
        return {
            "content_sha256": str(metadata["hash"]).lower(),
            "name": str(metadata.get("name") or metadata["id"]),
            "original_name": str(
                metadata.get("original_name")
                or metadata.get("name")
                or metadata["id"]
            ),
            "mime": str(metadata.get("mime") or "application/octet-stream"),
            "size": int(metadata.get("size") or 0),
            "uploaded_at": str(
                metadata.get("uploaded_at") or datetime.now().isoformat()
            ),
            "client_ip": str(metadata.get("client_ip") or ""),
            "width": _safe_int(metadata.get("width"), minimum=1),
            "height": _safe_int(metadata.get("height"), minimum=1),
        }

    def insert(
        self,
        owner: object,
        metadata: dict[str, Any],
        *,
        blob_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Insert one row or return the concurrent owner/hash winner."""

        normalized_key = self.blob_store.validate_key(blob_key)
        db = self.session_factory()
        try:
            _begin_write(db)
            account = _account(db, owner, create=True)
            assert account is not None
            digest = _content_digest(account.id, str(metadata.get("hash") or ""))
            clock = _db_now(db)
            row = ChatUploadMetadata(
                id=str(metadata["id"]),
                owner_id=account.id,
                content_digest=digest,
                blob_key=normalized_key,
                payload=self._encrypted_payload(metadata),
                state="active",
                last_accessed_at=clock,
                retention_until=clock + timedelta(days=self.retention_days),
                version=1,
            )
            db.add(row)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                winner = db.query(ChatUploadMetadata).filter(
                    ChatUploadMetadata.owner_id == account.id,
                    ChatUploadMetadata.content_digest == digest,
                    ChatUploadMetadata.state == "active",
                ).one_or_none()
                if winner is None:
                    raise
                info = self._row_info(db, winner, require_blob=True)
                if info is None:
                    raise UploadMetadataAuthorityError(
                        "Concurrent upload metadata winner has no valid blob"
                    )
                return info, False
            db.refresh(row)
            info = self._row_info(db, row, require_blob=True)
            if info is None:
                raise UploadMetadataAuthorityError(
                    "Created upload metadata failed validation"
                )
            return info, True
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def restore_metadata(
        self,
        owner: object,
        upload_id: str,
        metadata: dict[str, Any],
        *,
        blob_key: str,
        expected_version: int,
    ) -> dict[str, Any]:
        """CAS-reactivate or refresh metadata after restoring blob bytes."""

        normalized_key = self.blob_store.validate_key(blob_key)
        db = self.session_factory()
        try:
            _begin_write(db)
            account = _account(db, owner, create=False)
            if account is None:
                raise UploadMetadataAuthorityError("Upload owner is unavailable")
            clock = _db_now(db)
            result = db.execute(
                update(ChatUploadMetadata)
                .where(
                    ChatUploadMetadata.id == upload_id,
                    ChatUploadMetadata.owner_id == account.id,
                    ChatUploadMetadata.version == int(expected_version),
                )
                .values(
                    blob_key=normalized_key,
                    payload=self._encrypted_payload(metadata),
                    state="active",
                    last_accessed_at=clock,
                    retention_until=clock + timedelta(days=self.retention_days),
                    deleted_at=None,
                    version=ChatUploadMetadata.version + 1,
                    updated_at=clock,
                )
            )
            if result.rowcount != 1:
                db.rollback()
                current = self.get(upload_id, owner=owner, require_blob=True)
                if current is None:
                    raise UploadMetadataAuthorityError(
                        "Concurrent upload metadata update conflict"
                    )
                return current
            db.commit()
            current = self.get(upload_id, owner=owner, require_blob=True)
            if current is None:
                raise UploadMetadataAuthorityError("Restored upload is unavailable")
            return current
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def touch(self, upload_id: str, *, owner: object) -> None:
        db = self.session_factory()
        try:
            _begin_write(db)
            account = _account(db, owner, create=False)
            if account is None:
                return
            clock = _db_now(db)
            db.execute(
                update(ChatUploadMetadata)
                .where(
                    ChatUploadMetadata.id == upload_id,
                    ChatUploadMetadata.owner_id == account.id,
                    ChatUploadMetadata.state == "active",
                )
                .values(
                    last_accessed_at=clock,
                    retention_until=clock + timedelta(days=self.retention_days),
                    version=ChatUploadMetadata.version + 1,
                    updated_at=clock,
                )
            )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def list_index(self) -> dict[str, dict[str, Any]]:
        db = self.session_factory()
        try:
            rows = db.query(ChatUploadMetadata).filter(
                ChatUploadMetadata.state == "active"
            ).order_by(ChatUploadMetadata.created_at.asc()).all()
            result: dict[str, dict[str, Any]] = {}
            for row in rows:
                info = self._row_info(db, row, require_blob=True)
                if info is not None:
                    result[f"{row.owner_id}:{row.id}"] = info
            return result
        finally:
            db.close()

    def cleanup_expired(self, *, limit: int = 500) -> int:
        """Tombstone SQL first, then best-effort delete the retired blobs."""

        if limit < 1 or limit > 5_000:
            raise ValueError("cleanup limit must be between 1 and 5000")
        db = self.session_factory()
        retired: list[str] = []
        try:
            _begin_write(db)
            clock = _db_now(db)
            rows = db.query(ChatUploadMetadata).filter(
                ChatUploadMetadata.state == "active",
                ChatUploadMetadata.retention_until <= clock,
            ).order_by(ChatUploadMetadata.retention_until.asc()).limit(limit).all()
            for row in rows:
                result = db.execute(
                    update(ChatUploadMetadata)
                    .where(
                        ChatUploadMetadata.id == row.id,
                        ChatUploadMetadata.version == row.version,
                        ChatUploadMetadata.state == "active",
                    )
                    .values(
                        state="tombstoned",
                        payload={},
                        deleted_at=clock,
                        version=ChatUploadMetadata.version + 1,
                        updated_at=clock,
                    )
                )
                if result.rowcount == 1:
                    retired.append(row.blob_key)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        for key in retired:
            try:
                self.blob_store.delete(key)
            except (BlobKeyError, OSError):
                logger.exception("Failed to delete tombstoned upload blob")
        return len(retired)

    def stats(self) -> dict[str, Any]:
        index = self.list_index()
        total_size = sum(int(row.get("size") or 0) for row in index.values())
        types: dict[str, int] = {}
        for row in index.values():
            mime = str(row.get("mime") or "unknown")
            types[mime] = types.get(mime, 0) + 1
        return {
            "total_files": len(index),
            "total_size": total_size,
            "total_size_mb": round(total_size / (1024 * 1024), 2),
            "file_types": types,
            "cleanup_days": self.retention_days,
        }

    def _failed_import_marker(
        self,
        raw: bytes,
        *,
        details: dict[str, Any],
    ) -> None:
        db = self.session_factory()
        try:
            _begin_write(db)
            account = _account(db, DEFAULT_LOCAL_OWNER, create=True)
            assert account is not None
            source_digest = private_digest(
                f"chat-upload-import-v1:{account.id}",
                hashlib.sha256(raw).hexdigest(),
            )
            marker = db.query(ChatUploadMetadataImportRun).filter(
                ChatUploadMetadataImportRun.owner_id == account.id,
                ChatUploadMetadataImportRun.source_kind == UPLOAD_IMPORT_SOURCE_KIND,
                ChatUploadMetadataImportRun.source_sha256 == source_digest,
            ).one_or_none()
            clock = _db_now(db)
            if marker is None:
                db.add(ChatUploadMetadataImportRun(
                    id=str(uuid.uuid4()),
                    owner_id=account.id,
                    source_kind=UPLOAD_IMPORT_SOURCE_KIND,
                    source_sha256=source_digest,
                    state="failed",
                    imported_count=0,
                    skipped_count=0,
                    details=details,
                    completed_at=clock,
                    version=1,
                ))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Could not persist upload legacy-import failure marker")
        finally:
            db.close()

    @staticmethod
    def _read_legacy(path: Path) -> tuple[bytes, dict[str, Any]]:
        try:
            info = path.lstat()
        except OSError as exc:
            raise UploadMetadataAuthorityError("legacy_source_unavailable") from exc
        if path.is_symlink() or not path.is_file():
            raise UploadMetadataAuthorityError("legacy_source_not_regular")
        if info.st_size > LEGACY_UPLOAD_INDEX_MAX_BYTES:
            raise UploadMetadataAuthorityError("legacy_source_too_large")
        try:
            raw = path.read_bytes()
            decoded = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UploadMetadataAuthorityError("legacy_source_malformed") from exc
        if not isinstance(decoded, dict):
            raise UploadMetadataAuthorityError("legacy_source_not_object")
        if len(decoded) > LEGACY_UPLOAD_INDEX_MAX_ROWS:
            raise UploadMetadataAuthorityError("legacy_source_too_many_rows")
        return raw, decoded

    def _legacy_metadata(
        self,
        info: object,
        *,
        now: datetime,
        max_blob_bytes: int,
    ) -> tuple[str, dict[str, Any], str] | None:
        if not isinstance(info, dict):
            return None
        upload_id = str(info.get("id") or "")
        if not _UPLOAD_ID_RE.fullmatch(upload_id):
            return None
        raw_path = str(info.get("path") or "")
        supplied = Path(raw_path)
        candidates = [
            supplied if supplied.is_absolute() else self.blob_store.root / supplied
        ]
        # uploads.json historically stored absolute paths. After an operator
        # copies data/uploads into a shared mount, those paths still name the
        # old host directory. Accept only the deterministic YYYY/MM/DD/id
        # suffix at the new confined root; never read bytes from the old path.
        parts = supplied.parts
        if len(parts) >= 4:
            year, month, day, filename = parts[-4:]
            if (
                filename == upload_id
                and len(year) == 4 and year.isdigit()
                and month.isdigit() and 1 <= int(month) <= 12
                and day.isdigit() and 1 <= int(day) <= 31
            ):
                candidates.append(
                    self.blob_store.root / year / month / day / filename
                )

        path: Path | None = None
        key = ""
        for candidate in candidates:
            try:
                if candidate.is_symlink():
                    continue
                resolved = candidate.resolve(strict=True)
                relative = resolved.relative_to(self.blob_store.root).as_posix()
                normalized = self.blob_store.validate_key(relative)
            except (OSError, ValueError, BlobKeyError):
                continue
            if resolved.is_file() and resolved.name == upload_id:
                path = resolved
                key = normalized
                break
        if path is None:
            return None
        size = _safe_int(info.get("size"), minimum=1)
        try:
            actual_size = path.stat().st_size
        except OSError:
            return None
        if actual_size < 1 or actual_size > max_blob_bytes:
            return None
        if size != actual_size:
            size = actual_size
        # The retained index is a migration hint, never an integrity
        # authority. Recompute the bounded blob digest so a stale or tampered
        # `hash` field cannot bind the wrong bytes to an idempotency key.
        content_sha256 = _hash_file(path, max_bytes=max_blob_bytes) or ""
        if not _SHA256_RE.fullmatch(content_sha256):
            return None
        uploaded_at = _parse_timestamp(info.get("uploaded_at"), now)
        owner = resolved_runtime_owner(info.get("owner"))
        metadata = {
            "id": upload_id,
            "hash": content_sha256,
            "name": str(info.get("name") or upload_id),
            "original_name": str(
                info.get("original_name") or info.get("name") or upload_id
            ),
            "mime": str(info.get("mime") or "application/octet-stream"),
            "size": int(size),
            "uploaded_at": uploaded_at.isoformat(),
            "client_ip": str(info.get("client_ip") or ""),
            "width": _safe_int(info.get("width"), minimum=1),
            "height": _safe_int(info.get("height"), minimum=1),
        }
        return owner, metadata, key

    def import_legacy_index(
        self,
        path: str | os.PathLike[str],
        *,
        max_blob_bytes: int,
    ) -> LegacyUploadImportResult:
        """Bounded, source-preserving, idempotent uploads.json adoption."""

        source = Path(path)
        if not source.exists() and not Path(str(source) + ".bak").exists():
            return LegacyUploadImportResult(False, False, 0, 0, 0)

        live_error: str | None = None
        recovered = False
        try:
            raw, decoded = self._read_legacy(source)
        except UploadMetadataAuthorityError as exc:
            live_error = str(exc)
            backup = Path(str(source) + ".bak")
            try:
                raw, decoded = self._read_legacy(backup)
                recovered = True
            except UploadMetadataAuthorityError as backup_exc:
                failure_raw = b""
                try:
                    if source.exists() and source.stat().st_size <= LEGACY_UPLOAD_INDEX_MAX_BYTES:
                        failure_raw = source.read_bytes()
                except OSError:
                    pass
                self._failed_import_marker(
                    failure_raw,
                    details={
                        "live_error": live_error,
                        "backup_error": str(backup_exc),
                        "source_preserved": True,
                    },
                )
                return LegacyUploadImportResult(True, False, 0, 0, 1)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        grouped: dict[str, list[tuple[dict[str, Any], str]]] = {}
        invalid = 0
        for info in decoded.values():
            parsed = self._legacy_metadata(
                info, now=now, max_blob_bytes=max_blob_bytes
            )
            if parsed is None:
                invalid += 1
                continue
            owner, metadata, key = parsed
            grouped.setdefault(owner, []).append((metadata, key))

        # Even an empty/all-invalid but syntactically valid source receives a
        # durable checkpoint, so startup does not rescan it forever. The source
        # remains untouched and a future repaired file has a different digest.
        if not grouped:
            grouped[DEFAULT_LOCAL_OWNER] = []

        imported = 0
        skipped = invalid
        for owner, rows in grouped.items():
            db = self.session_factory()
            try:
                _begin_write(db)
                account = _account(db, owner, create=True)
                assert account is not None
                source_digest = private_digest(
                    f"chat-upload-import-v1:{account.id}",
                    hashlib.sha256(raw).hexdigest(),
                )
                marker = db.query(ChatUploadMetadataImportRun).filter(
                    ChatUploadMetadataImportRun.owner_id == account.id,
                    ChatUploadMetadataImportRun.source_kind == UPLOAD_IMPORT_SOURCE_KIND,
                    ChatUploadMetadataImportRun.source_sha256 == source_digest,
                ).one_or_none()
                if marker is not None and marker.state == "completed":
                    skipped += len(rows)
                    db.rollback()
                    continue
                if marker is None:
                    marker = ChatUploadMetadataImportRun(
                        id=str(uuid.uuid4()),
                        owner_id=account.id,
                        source_kind=UPLOAD_IMPORT_SOURCE_KIND,
                        source_sha256=source_digest,
                        state="pending",
                        imported_count=0,
                        skipped_count=0,
                        details={},
                        version=1,
                    )
                    db.add(marker)
                    db.flush()

                owner_imported = 0
                owner_skipped = 0
                for metadata, key in rows:
                    digest = _content_digest(account.id, metadata["hash"])
                    existing = db.query(ChatUploadMetadata).filter(
                        or_(
                            ChatUploadMetadata.id == metadata["id"],
                            (
                                (ChatUploadMetadata.owner_id == account.id)
                                & (ChatUploadMetadata.content_digest == digest)
                            ),
                        )
                    ).first()
                    if existing is not None:
                        owner_skipped += 1
                        continue
                    uploaded_at = _parse_timestamp(metadata["uploaded_at"], now)
                    db.add(ChatUploadMetadata(
                        id=metadata["id"],
                        owner_id=account.id,
                        content_digest=digest,
                        blob_key=key,
                        payload=self._encrypted_payload(metadata),
                        state="active",
                        last_accessed_at=uploaded_at,
                        retention_until=uploaded_at + timedelta(
                            days=self.retention_days
                        ),
                        version=1,
                        created_at=uploaded_at,
                        updated_at=uploaded_at,
                    ))
                    db.flush()
                    owner_imported += 1

                clock = _db_now(db)
                marker.state = "completed"
                marker.imported_count = owner_imported
                marker.skipped_count = owner_skipped
                marker.details = {
                    "recovered_from_backup": recovered,
                    "live_error": live_error,
                    "invalid_count": invalid,
                    "source_preserved": True,
                }
                marker.completed_at = clock
                marker.version = int(marker.version or 0) + 1
                db.commit()
                imported += owner_imported
                skipped += owner_skipped
            except IntegrityError:
                db.rollback()
                logger.warning("Concurrent upload legacy import conflict")
                skipped += len(rows)
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

        return LegacyUploadImportResult(
            attempted=True,
            recovered_from_backup=recovered,
            imported=imported,
            skipped=skipped,
            failed=0,
        )
