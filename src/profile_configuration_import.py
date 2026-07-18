"""Bounded, non-destructive adoption of legacy profile configuration files."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from core.database import Account, SessionLocal
from src.auth_helpers import DEFAULT_LOCAL_OWNER
from src.constants import (
    FEATURES_FILE,
    INTEGRATIONS_FILE,
    SETTINGS_FILE,
    USER_PREFS_FILE,
)
from src.identity import ensure_account, find_account
from src.profile_configuration_service import (
    ConfigurationImportResult,
    ProfileConfigurationError,
    import_legacy_configuration,
)


MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 100_000


class ProfileConfigurationImportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LegacyConfigurationAdoptionResult:
    imported: int
    skipped: int
    runs: int
    idempotent_runs: int
    source_preserved: bool


class _DuplicateKey(ValueError):
    pass


def _pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _read_bounded_json(path: Path) -> tuple[Any, str] | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProfileConfigurationImportError("Legacy configuration source is unreadable") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > MAX_SOURCE_BYTES
    ):
        raise ProfileConfigurationImportError("Legacy configuration source is not a bounded regular file")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ProfileConfigurationImportError("Legacy configuration source is unreadable") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_SOURCE_BYTES:
            raise ProfileConfigurationImportError("Legacy configuration source exceeds the size limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, MAX_SOURCE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_SOURCE_BYTES:
                raise ProfileConfigurationImportError("Legacy configuration source exceeds the size limit")
        after = os.fstat(descriptor)
        if (
            opened.st_dev != after.st_dev
            or opened.st_ino != after.st_ino
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
        ):
            raise ProfileConfigurationImportError("Legacy configuration source changed during import")
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (
        UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey,
        RecursionError, MemoryError,
    ) as exc:
        raise ProfileConfigurationImportError("Legacy configuration is not valid bounded JSON") from exc
    pending = [(payload, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ProfileConfigurationImportError("Legacy configuration exceeds structural limits")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return payload, hashlib.sha256(raw).hexdigest()


def _global_owner(
    db,
    *,
    auth_enabled: bool,
    primary_admin_resolver: Callable[[], str | None],
) -> Account:
    if not auth_enabled:
        return ensure_account(db, DEFAULT_LOCAL_OWNER)
    username = str(primary_admin_resolver() or "").strip().lower()
    if not username:
        raise ProfileConfigurationImportError(
            "Legacy installation configuration has no primary admin owner"
        )
    account = find_account(db, username)
    if account is None:
        # The auth profile already exists; this creates only its immutable SQL
        # principal and local identity in the same import transaction.
        account = ensure_account(db, username)
    return account


def adopt_legacy_profile_configuration(
    *,
    session_factory=SessionLocal,
    auth_enabled: bool,
    primary_admin_resolver: Callable[[], str | None],
    profile_usernames: Iterable[str] = (),
    settings_path: str | os.PathLike[str] = SETTINGS_FILE,
    preferences_path: str | os.PathLike[str] = USER_PREFS_FILE,
    features_path: str | os.PathLike[str] = FEATURES_FILE,
    integrations_path: str | os.PathLike[str] = INTEGRATIONS_FILE,
) -> LegacyConfigurationAdoptionResult:
    snapshots = {
        "settings_json": _read_bounded_json(Path(settings_path)),
        "user_prefs_json": _read_bounded_json(Path(preferences_path)),
        "features_json": _read_bounded_json(Path(features_path)),
        "integrations_json": _read_bounded_json(Path(integrations_path)),
    }
    if not any(value is not None for value in snapshots.values()):
        return LegacyConfigurationAdoptionResult(0, 0, 0, 0, True)
    db = session_factory()
    try:
        owner = _global_owner(
            db,
            auth_enabled=auth_enabled,
            primary_admin_resolver=primary_admin_resolver,
        )
        accounts: dict[str, Account] = {
            str(owner.username or "").strip().lower(): owner,
        }
        for raw_username in profile_usernames:
            username = str(raw_username or "").strip().lower()
            if not username or username in accounts:
                continue
            account = find_account(db, username) or ensure_account(db, username)
            accounts[username] = account

        results: list[ConfigurationImportResult] = []
        for source_kind in ("settings_json", "features_json", "integrations_json"):
            snapshot = snapshots[source_kind]
            if snapshot is None:
                continue
            payload, digest = snapshot
            results.append(import_legacy_configuration(
                db,
                account=owner,
                source_kind=source_kind,
                source_sha256=digest,
                payload=payload,
            ))
        preferences = snapshots["user_prefs_json"]
        if preferences is not None:
            payload, digest = preferences
            for account in accounts.values():
                results.append(import_legacy_configuration(
                    db,
                    account=account,
                    source_kind="user_prefs_json",
                    source_sha256=digest,
                    payload=payload,
                ))
        db.commit()
        return LegacyConfigurationAdoptionResult(
            imported=sum(result.imported for result in results),
            skipped=sum(result.skipped for result in results),
            runs=len(results),
            idempotent_runs=sum(1 for result in results if result.idempotent),
            source_preserved=True,
        )
    except ProfileConfigurationImportError:
        db.rollback()
        raise
    except ProfileConfigurationError as exc:
        db.rollback()
        raise ProfileConfigurationImportError(str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


__all__ = [
    "LegacyConfigurationAdoptionResult",
    "ProfileConfigurationImportError",
    "adopt_legacy_profile_configuration",
]
