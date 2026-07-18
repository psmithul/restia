"""Deterministic one-time adoption of legacy contacts JSON state.

The importer is the only code allowed to read ``settings.json`` contact keys
or ``contacts.json``.  It never modifies those files, creates byte-identical
owner-only backups before SQL mutation, binds the snapshot only to the primary
admin (or ``DEFAULT_LOCAL_OWNER`` in auth-disabled mode), and records a
database cutover marker so runtime code cannot fall back to dual authority.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError, OperationalError

from core.database import Account, ContactImportRun, SessionLocal, utcnow_naive
from src.auth_helpers import DEFAULT_LOCAL_OWNER
from src.carddav_contacts import normalize_contact, validate_carddav_url
from src.constants import CONTACTS_FILE, DATA_DIR, SETTINGS_FILE
from src.contact_service import import_legacy_snapshot
from src.identity import ensure_account, find_account


IMPORT_SOURCE_KIND = "legacy-contacts-json-v1"
MAX_SETTINGS_SOURCE_BYTES = 8 * 1024 * 1024
MAX_CONTACTS_SOURCE_BYTES = 64 * 1024 * 1024
MAX_CONTACTS = 100_000
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 1_000_000


_ERRORS = {
    "owner_ambiguous": "Legacy contacts cannot be assigned to an unambiguous owner",
    "owner_missing": "The deterministic legacy contacts owner does not exist",
    "source_not_regular": "Legacy contacts source is not a regular file",
    "source_too_large": "Legacy contacts source exceeds the size limit",
    "source_changed_during_read": "Legacy contacts source changed during import",
    "invalid_json": "Legacy contacts source is not valid bounded JSON",
    "invalid_schema": "Legacy contacts source has an invalid schema",
    "invalid_credential": "Legacy CardDAV credential cannot be decrypted",
    "invalid_url": "Legacy CardDAV URL is unsafe or invalid",
    "duplicate_uid": "Legacy contacts contain a duplicate UID",
    "backup_failed": "Legacy contacts backup could not be created safely",
    "backup_missing": "Completed legacy contacts backup is missing or changed",
    "source_changed": "Legacy contacts changed after database cutover",
    "database_error": "Legacy contacts import failed at the database boundary",
}


class ContactLegacyImportError(RuntimeError):
    def __init__(self, code: str):
        self.code = code if code in _ERRORS else "database_error"
        super().__init__(_ERRORS[self.code])


@dataclass(frozen=True, slots=True)
class ContactLegacyImportResult:
    run_id: str
    owner_id: str
    state: str
    contacts: int
    carddav_configured: bool
    idempotent: bool


@dataclass(frozen=True, slots=True)
class _Snapshot:
    settings_raw: bytes | None
    contacts_raw: bytes | None
    settings_sha256: str | None
    contacts_sha256: str | None
    settings_backup: Path | None
    contacts_backup: Path | None
    config_digest: str
    config: dict[str, str] | None
    config_origin: str
    contacts: list[dict[str, Any]]
    settings_exists: bool
    contacts_exists: bool


class _DuplicateKey(ValueError):
    pass


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_source(path: Path, *, limit: int) -> bytes | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ContactLegacyImportError("source_not_regular") from exc
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ContactLegacyImportError("source_not_regular")
    if before.st_size > limit:
        raise ContactLegacyImportError("source_too_large")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContactLegacyImportError("source_not_regular") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > limit:
            raise ContactLegacyImportError("source_too_large")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise ContactLegacyImportError("source_too_large")
        after = os.fstat(descriptor)
        if (
            after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or after.st_size != opened.st_size
            or after.st_mtime_ns != opened.st_mtime_ns
        ):
            raise ContactLegacyImportError("source_changed_during_read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _bounded_json(raw: bytes) -> Any:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateKey,
        RecursionError,
        MemoryError,
    ) as exc:
        raise ContactLegacyImportError("invalid_json") from exc
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ContactLegacyImportError("invalid_json")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return value


def _config_from_settings(
    settings: dict[str, Any] | None,
    *,
    environ: dict[str, str],
) -> tuple[dict[str, str] | None, bool, str]:
    values = settings or {}
    keys_present = any(
        key in values
        for key in ("carddav_url", "carddav_username", "carddav_password")
    )
    raw_url = values.get("carddav_url") if "carddav_url" in values else environ.get("CARDDAV_URL", "")
    raw_username = (
        values.get("carddav_username")
        if "carddav_username" in values else environ.get("CARDDAV_USERNAME", "")
    )
    from_settings_password = "carddav_password" in values
    raw_password = (
        values.get("carddav_password")
        if from_settings_password else environ.get("CARDDAV_PASSWORD", "")
    )
    url = str(raw_url or "").strip()
    username = str(raw_username or "")
    password = str(raw_password or "")
    if from_settings_password and password.startswith("enc:"):
        from src.secret_storage import decrypt, is_decryptable, is_encrypted

        if not is_encrypted(password) or not is_decryptable(password):
            raise ContactLegacyImportError("invalid_credential")
        password = decrypt(password)
    if url:
        try:
            url = validate_carddav_url(url)
        except ValueError as exc:
            raise ContactLegacyImportError("invalid_url") from exc
    configured = bool(url)
    has_env = bool(
        str(environ.get("CARDDAV_URL") or "").strip()
        or str(environ.get("CARDDAV_USERNAME") or "").strip()
        or str(environ.get("CARDDAV_PASSWORD") or "")
    )
    if not keys_present and not has_env:
        return None, False, "none"
    return (
        {"url": url, "username": username, "password": password},
        configured,
        "settings" if keys_present else "env",
    )


def _config_digest(config: dict[str, str] | None) -> str:
    canonical = json.dumps(
        config or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return _sha256(canonical)


def _parse_contacts(raw: bytes | None) -> list[dict[str, Any]]:
    if raw is None:
        return []
    value = _bounded_json(raw)
    rows = value.get("contacts") if isinstance(value, dict) else value
    if not isinstance(rows, list) or len(rows) > MAX_CONTACTS:
        raise ContactLegacyImportError("invalid_schema")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ContactLegacyImportError("invalid_schema")
        normalized = normalize_contact(row)
        uid = str(normalized.get("uid") or "")
        if len(uid) > 512:
            raise ContactLegacyImportError("invalid_schema")
        if uid in seen:
            raise ContactLegacyImportError("duplicate_uid")
        seen.add(uid)
        result.append(normalized)
    return result


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ContactLegacyImportError("backup_failed") from exc


def _backup(raw: bytes | None, destination: Path | None) -> Path | None:
    if raw is None or destination is None:
        return None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(destination.parent, 0o700)
        except OSError:
            pass
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file():
                raise ContactLegacyImportError("backup_failed")
            if not hmac.compare_digest(destination.read_bytes(), raw):
                raise ContactLegacyImportError("backup_failed")
            return destination
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=destination.name + ".tmp.", dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            try:
                os.fchmod(descriptor, 0o600)
            except OSError:
                pass
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass
            _fsync_directory(destination.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        if not hmac.compare_digest(destination.read_bytes(), raw):
            raise ContactLegacyImportError("backup_failed")
        return destination
    except ContactLegacyImportError:
        raise
    except OSError as exc:
        raise ContactLegacyImportError("backup_failed") from exc


def _prepare_snapshot(
    *,
    settings_path: Path,
    contacts_path: Path,
    backup_dir: Path,
    environ: dict[str, str],
    force_existing_cutover_check: bool = False,
) -> _Snapshot | None:
    settings_raw = _read_source(settings_path, limit=MAX_SETTINGS_SOURCE_BYTES)
    contacts_raw = _read_source(contacts_path, limit=MAX_CONTACTS_SOURCE_BYTES)
    settings = None
    if settings_raw is not None:
        settings = _bounded_json(settings_raw)
        if not isinstance(settings, dict):
            raise ContactLegacyImportError("invalid_schema")
    config, _, config_origin = _config_from_settings(settings, environ=environ)
    if (
        config is None
        and contacts_raw is None
        and not (force_existing_cutover_check and settings_raw is not None)
    ):
        return None
    contacts = _parse_contacts(contacts_raw)
    settings_digest = _sha256(settings_raw) if settings_raw is not None else None
    contacts_digest = _sha256(contacts_raw) if contacts_raw is not None else None
    settings_backup = _backup(
        settings_raw,
        backup_dir / f"settings.{settings_digest}.json.bak" if settings_digest else None,
    )
    contacts_backup = _backup(
        contacts_raw,
        backup_dir / f"contacts.{contacts_digest}.json.bak" if contacts_digest else None,
    )
    return _Snapshot(
        settings_raw=settings_raw,
        contacts_raw=contacts_raw,
        settings_sha256=settings_digest,
        contacts_sha256=contacts_digest,
        settings_backup=settings_backup,
        contacts_backup=contacts_backup,
        config_digest=_config_digest(config),
        config=config,
        config_origin=config_origin,
        contacts=contacts,
        settings_exists=settings_raw is not None,
        contacts_exists=contacts_raw is not None,
    )


def _deterministic_owner(
    db,
    *,
    auth_enabled: bool,
    primary_admin_resolver: Callable[[], str | None],
):
    if not auth_enabled:
        return ensure_account(db, DEFAULT_LOCAL_OWNER)
    username = str(primary_admin_resolver() or "").strip().lower()
    if not username:
        raise ContactLegacyImportError("owner_ambiguous")
    account = find_account(db, username)
    if account is None:
        raise ContactLegacyImportError("owner_missing")
    return account


def _backup_matches(path_value: object, digest: object, *, limit: int) -> bool:
    if not path_value and digest is None:
        return True
    if not isinstance(path_value, str) or not isinstance(digest, str):
        return False
    try:
        path = Path(path_value)
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return False
        return hmac.compare_digest(_sha256(path.read_bytes()), digest)
    except OSError:
        return False


def _completed_result(
    run: ContactImportRun,
    *,
    snapshot: _Snapshot | None,
) -> ContactLegacyImportResult:
    if not _backup_matches(
        run.backup_settings_path,
        run.settings_sha256,
        limit=MAX_SETTINGS_SOURCE_BYTES,
    ) or not _backup_matches(
        run.backup_contacts_path,
        run.contacts_sha256,
        limit=MAX_CONTACTS_SOURCE_BYTES,
    ):
        raise ContactLegacyImportError("backup_missing")
    details = dict(run.details or {})
    if snapshot is not None:
        original_origin = str(details.get("config_origin") or "none")
        compare_config = (
            original_origin == "settings" and snapshot.settings_exists
        ) or (
            original_origin == "env" and snapshot.config_origin == "env"
        )
        if compare_config and details.get("config_sha256") != snapshot.config_digest:
            raise ContactLegacyImportError("source_changed")
        if snapshot.contacts_exists and run.contacts_sha256 != snapshot.contacts_sha256:
            raise ContactLegacyImportError("source_changed")
    return ContactLegacyImportResult(
        run_id=run.id,
        owner_id=run.owner_id,
        state=run.state,
        contacts=int(details.get("contacts", 0)),
        carddav_configured=bool(details.get("carddav_configured")),
        idempotent=True,
    )


def _apply_once(
    session_factory,
    *,
    snapshot: _Snapshot,
    auth_enabled: bool,
    primary_admin_resolver: Callable[[], str | None],
) -> ContactLegacyImportResult:
    db = session_factory()
    try:
        run = db.query(ContactImportRun).filter(
            ContactImportRun.source_kind == IMPORT_SOURCE_KIND
        ).first()
        if run is not None and run.state == "completed":
            if db.query(Account.id).filter(Account.id == run.owner_id).first() is None:
                raise ContactLegacyImportError("owner_missing")
            result = _completed_result(run, snapshot=snapshot)
            db.rollback()
            return result
        if run is not None:
            account = db.query(Account).filter(Account.id == run.owner_id).first()
            if account is None:
                raise ContactLegacyImportError("owner_missing")
        else:
            account = _deterministic_owner(
                db,
                auth_enabled=auth_enabled,
                primary_admin_resolver=primary_admin_resolver,
            )
        if run is None:
            run = ContactImportRun(
                id=str(uuid.uuid4()),
                owner_id=account.id,
                source_kind=IMPORT_SOURCE_KIND,
                state="pending",
                details={},
            )
            db.add(run)
            db.flush()
        elif run.owner_id != account.id:
            raise ContactLegacyImportError("owner_ambiguous")

        imported, configured = import_legacy_snapshot(
            db,
            owner_id=account.id,
            contacts=snapshot.contacts,
            carddav_config=snapshot.config,
        )
        run.state = "completed"
        run.settings_sha256 = snapshot.settings_sha256
        run.contacts_sha256 = snapshot.contacts_sha256
        run.backup_settings_path = (
            str(snapshot.settings_backup) if snapshot.settings_backup else None
        )
        run.backup_contacts_path = (
            str(snapshot.contacts_backup) if snapshot.contacts_backup else None
        )
        run.details = {
            "contacts": imported,
            "carddav_configured": configured,
            "config_sha256": snapshot.config_digest,
            "config_origin": snapshot.config_origin,
        }
        run.completed_at = utcnow_naive()
        db.commit()
        return ContactLegacyImportResult(
            run_id=run.id,
            owner_id=account.id,
            state=run.state,
            contacts=imported,
            carddav_configured=configured,
            idempotent=False,
        )
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _completed_without_snapshot(
    session_factory,
    *,
    auth_enabled: bool,
    primary_admin_resolver: Callable[[], str | None],
) -> ContactLegacyImportResult | None:
    db = session_factory()
    try:
        run = db.query(ContactImportRun).filter(
            ContactImportRun.source_kind == IMPORT_SOURCE_KIND
        ).first()
        if run is None:
            return None
        if db.query(Account.id).filter(Account.id == run.owner_id).first() is None:
            raise ContactLegacyImportError("owner_missing")
        if run.state != "completed":
            raise ContactLegacyImportError("database_error")
        return _completed_result(run, snapshot=None)
    finally:
        db.close()


def _import_run_state(session_factory) -> str | None:
    db = session_factory()
    try:
        run = db.query(ContactImportRun).filter(
            ContactImportRun.source_kind == IMPORT_SOURCE_KIND
        ).first()
        return str(run.state) if run is not None else None
    finally:
        db.close()


def adopt_legacy_contacts(
    session_factory=SessionLocal,
    *,
    settings_path: str | os.PathLike[str] = SETTINGS_FILE,
    contacts_path: str | os.PathLike[str] = CONTACTS_FILE,
    backup_dir: str | os.PathLike[str] | None = None,
    auth_enabled: bool | None = None,
    primary_admin_resolver: Callable[[], str | None] | None = None,
    environ: dict[str, str] | None = None,
) -> ContactLegacyImportResult | None:
    """Adopt legacy contact state once and return the durable cutover result."""

    if primary_admin_resolver is None:
        from src.auth_runtime import unambiguous_contact_import_owner

        primary_admin_resolver = unambiguous_contact_import_owner
    enabled = (
        os.getenv("AUTH_ENABLED", "true").lower() != "false"
        if auth_enabled is None else bool(auth_enabled)
    )
    env = dict(os.environ if environ is None else environ)
    settings = Path(settings_path)
    contacts = Path(contacts_path)
    destination = Path(backup_dir) if backup_dir is not None else Path(DATA_DIR) / "legacy-contact-backups"
    import_state = _import_run_state(session_factory)
    # CARDDAV_* is a one-time bootstrap input, never a second runtime
    # authority. Once SQL records the completed cutover, an environment change
    # must not relock the contacts domain or replace the stored configuration.
    snapshot_environ = {} if import_state == "completed" else env
    snapshot = _prepare_snapshot(
        settings_path=settings,
        contacts_path=contacts,
        backup_dir=destination,
        environ=snapshot_environ,
        force_existing_cutover_check=import_state is not None,
    )
    if snapshot is None:
        return _completed_without_snapshot(
            session_factory,
            auth_enabled=enabled,
            primary_admin_resolver=primary_admin_resolver,
        )

    delay = 0.005
    for attempt in range(6):
        try:
            return _apply_once(
                session_factory,
                snapshot=snapshot,
                auth_enabled=enabled,
                primary_admin_resolver=primary_admin_resolver,
            )
        except (IntegrityError, OperationalError) as exc:
            if attempt == 5:
                raise ContactLegacyImportError("database_error") from exc
            time.sleep(delay)
            delay = min(delay * 2, 0.1)
    raise ContactLegacyImportError("database_error")


__all__ = [
    "ContactLegacyImportError",
    "ContactLegacyImportResult",
    "IMPORT_SOURCE_KIND",
    "MAX_CONTACTS_SOURCE_BYTES",
    "MAX_SETTINGS_SOURCE_BYTES",
    "adopt_legacy_contacts",
]
