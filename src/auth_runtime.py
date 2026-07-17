"""Process-wide construction of Restia's single database auth authority."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import threading
from pathlib import Path

from core.database import AuthImportRun, SessionLocal
from src.constants import AUTH_FILE, SESSIONS_FILE
from src.database_auth_manager import DatabaseAuthManager
from src.legacy_auth_import import (
    IMPORT_SOURCE_KIND,
    LegacyAuthImportError,
    MAX_AUTH_SOURCE_BYTES,
    MAX_SESSIONS_SOURCE_BYTES,
    import_legacy_auth,
)


logger = logging.getLogger(__name__)
_manager_lock = threading.Lock()
_manager: DatabaseAuthManager | None = None


def _safe_backup_matches(path_value: object, digest: object, *, limit: int) -> bool:
    if not isinstance(path_value, str) or not path_value:
        return False
    if not isinstance(digest, str) or len(digest) != 64:
        return False
    path = Path(path_value)
    try:
        info = path.lstat()
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size > limit
        ):
            return False
        raw = path.read_bytes()
    except OSError:
        return False
    return hashlib.sha256(raw).hexdigest() == digest


def _import_run(session_factory) -> AuthImportRun | None:
    db = session_factory()
    try:
        return db.query(AuthImportRun).filter(
            AuthImportRun.source_kind == IMPORT_SOURCE_KIND
        ).first()
    finally:
        db.close()


def _completed_backups_are_durable(run: AuthImportRun) -> bool:
    if run.state != "completed" or not _safe_backup_matches(
        run.backup_auth_path,
        run.auth_sha256,
        limit=MAX_AUTH_SOURCE_BYTES,
    ):
        return False
    if run.sessions_sha256 is None:
        return run.backup_sessions_path in (None, "")
    return _safe_backup_matches(
        run.backup_sessions_path,
        run.sessions_sha256,
        limit=MAX_SESSIONS_SOURCE_BYTES,
    )


def build_auth_manager(
    session_factory=SessionLocal,
    *,
    auth_path: str | os.PathLike[str] = AUTH_FILE,
    sessions_path: str | os.PathLike[str] = SESSIONS_FILE,
    backup_dir: str | os.PathLike[str] | None = None,
    token_hmac_key: bytes | None = None,
) -> DatabaseAuthManager:
    """Import once, then construct the sole database-backed authority.

    Corrupt, conflicting, changed, or half-imported legacy state produces a
    locked manager so setup, localhost bypass, cookies, and API tokens all fail
    closed while explicit recovery tooling remains available. A missing source
    after a completed cutover is accepted only when its immutable backup still
    matches the recorded digest.
    """

    auth_source = Path(auth_path)
    sessions_source = Path(sessions_path)
    store_error = False
    try:
        if auth_source.exists() or auth_source.is_symlink():
            import_legacy_auth(
                session_factory,
                auth_path=auth_source,
                sessions_path=sessions_source,
                backup_dir=backup_dir,
            )
        else:
            run = _import_run(session_factory)
            if run is not None:
                store_error = not _completed_backups_are_durable(run)
            elif sessions_source.exists() or sessions_source.is_symlink():
                # A session file without its credential source cannot be
                # proven safe to discard or attach to a new first-run admin.
                store_error = True
    except LegacyAuthImportError as exc:
        store_error = True
        logger.error("Legacy authentication import locked: %s", exc.code)

    return DatabaseAuthManager(
        session_factory,
        token_hmac_key=token_hmac_key,
        store_error=store_error,
    )


def get_auth_manager() -> DatabaseAuthManager:
    """Return the one process-wide database auth manager."""

    global _manager
    if _manager is not None:
        return _manager
    with _manager_lock:
        if _manager is None:
            from src.database_runtime import initialize_database

            initialize_database()
            _manager = build_auth_manager()
    return _manager


def configure_auth_manager(manager: DatabaseAuthManager) -> None:
    """Install an already-built manager for the current application process."""

    global _manager
    with _manager_lock:
        if _manager is not None and _manager is not manager:
            raise RuntimeError("A different database auth manager is already configured")
        _manager = manager


def active_auth_usernames() -> tuple[str, ...]:
    """Return deterministic active profile aliases from the database authority."""

    profiles = get_auth_manager().list_users()
    return tuple(sorted({
        str(profile.get("username") or "").strip().lower()
        for profile in profiles
        if str(profile.get("username") or "").strip()
    }))


def primary_admin_username() -> str | None:
    """Return the deterministic primary admin, or fail closed with no owner."""

    profiles = get_auth_manager().list_users()
    admins = sorted({
        str(profile.get("username") or "").strip().lower()
        for profile in profiles
        if bool(profile.get("is_admin"))
        and str(profile.get("username") or "").strip()
    })
    return admins[0] if admins else None


def _reset_auth_manager_for_tests() -> None:
    global _manager
    with _manager_lock:
        _manager = None


__all__ = [
    "active_auth_usernames",
    "build_auth_manager",
    "configure_auth_manager",
    "get_auth_manager",
    "primary_admin_username",
]
