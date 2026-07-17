"""Offline, idempotent import of Restia's legacy JSON authentication stores.

The importer deliberately has no runtime wiring.  Callers provide a SQLAlchemy
session factory plus explicit source/backup paths.  Source files are never
modified, byte-identical owner-only backups are durable before the first SQL
mutation, and all auth-domain changes commit in one transaction.
"""

from __future__ import annotations

import bcrypt
import hashlib
import hmac
import json
import math
import os
import re
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy.exc import IntegrityError, OperationalError

from core.auth import ADMIN_PRIVILEGES, DEFAULT_PRIVILEGES, username_is_reserved
from core.database import (
    Account,
    AccountCapability,
    AccountRole,
    ApiToken,
    AuthIdentity,
    AuthImportRun,
    AuthPolicy,
    AuthSession,
    LocalCredential,
    MfaFactor,
    MfaRecoveryCode,
    RetiredAuthSubject,
    utcnow_naive,
)


LOCAL_PROVIDER = "local"
LOCAL_ISSUER = "restia-local"
IMPORT_SOURCE_KIND = "legacy-auth-json-v1"
LEGACY_SESSION_DIGEST_SCHEME = "sha256_legacy"
LEGACY_RECOVERY_SHA256 = "sha256_legacy"
LEGACY_RECOVERY_BCRYPT = "bcrypt_legacy"

MAX_AUTH_SOURCE_BYTES = 4 * 1024 * 1024
MAX_SESSIONS_SOURCE_BYTES = 16 * 1024 * 1024
MAX_USERS = 10_000
MAX_SESSIONS = 100_000
MAX_RECOVERY_CODES_PER_USER = 64
MAX_PLAINTEXT_RECOVERY_CODES = 64
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 500_000

_IMPORT_RETRY_INITIAL_SECONDS = 0.005
_IMPORT_RETRY_MAX_SECONDS = 0.100
_POSTGRES_TRANSACTION_RETRY_STATES = frozenset({"40001", "40P01", "55P03"})

_BCRYPT_RE = re.compile(r"^\$2[aby]\$(?:0[4-9]|1[0-6])\$[./A-Za-z0-9]{53}$")
_SHA256_PROTECTED_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SESSION_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RAW_LEGACY_SESSION_RE = re.compile(r"^[0-9a-f]{64}$")

_ERROR_MESSAGES = {
    "source_missing": "Legacy authentication source is missing",
    "source_not_regular": "Legacy authentication source is not a regular file",
    "source_too_large": "Legacy authentication source exceeds the size limit",
    "source_changed_during_read": "Legacy authentication source changed during import",
    "backup_failed": "Legacy authentication backup could not be created safely",
    "invalid_json": "Legacy authentication source is not valid bounded JSON",
    "invalid_schema": "Legacy authentication source has an invalid schema",
    "canonical_collision": "Legacy authentication subjects have a canonical collision",
    "reserved_subject": "Legacy authentication source contains a reserved subject",
    "invalid_credential": "Legacy authentication source contains an invalid credential",
    "identity_conflict": "Database identity state conflicts with the legacy source",
    "credential_conflict": "Database credential state conflicts with the legacy source",
    "authorization_conflict": "Database authorization state conflicts with the legacy source",
    "policy_conflict": "Database authentication policy conflicts with the legacy source",
    "mfa_conflict": "Database MFA state conflicts with the legacy source",
    "session_conflict": "Database session state conflicts with the legacy source",
    "orphan_api_token": "An API token cannot be linked to an imported account",
    "source_changed": "Legacy authentication sources changed after a completed import",
    "import_in_progress": "A legacy authentication import is already pending",
    "database_error": "Legacy authentication import failed at the database boundary",
}


class LegacyAuthImportError(RuntimeError):
    """Sanitized importer failure; source values are never included."""

    def __init__(self, code: str, *, record_failure: bool = True):
        self.code = code if code in _ERROR_MESSAGES else "database_error"
        self.record_failure = record_failure
        super().__init__(_ERROR_MESSAGES[self.code])


@dataclass(frozen=True, slots=True)
class LegacyAuthImportResult:
    run_id: str
    state: str
    auth_sha256: str
    sessions_sha256: str | None
    backup_auth_path: str
    backup_sessions_path: str | None
    accounts: int
    sessions: int
    retired_subjects: int
    api_tokens_backfilled: int
    idempotent: bool


@dataclass(frozen=True, slots=True)
class _RecoveryCode:
    source_value: str
    source_kind: str


@dataclass(frozen=True, slots=True)
class _LegacyUser:
    username: str
    password_hash: str
    is_admin: bool
    capabilities: dict[str, Any]
    created_at: datetime
    totp_state: str | None
    totp_secret: str | None
    totp_pending_secret: str | None
    recovery_codes: tuple[_RecoveryCode, ...]


@dataclass(frozen=True, slots=True)
class _LegacySession:
    token_digest: str
    username: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _LegacySnapshot:
    users: tuple[_LegacyUser, ...]
    retired_subjects: tuple[str, ...]
    signup_enabled: bool
    sessions: tuple[_LegacySession, ...]


class _DuplicateJSONKey(ValueError):
    pass


def _raise(code: str, *, record_failure: bool = True) -> None:
    raise LegacyAuthImportError(code, record_failure=record_failure)


def _normalized_subject(value: object) -> str:
    return str(value or "").strip().lower()


def _valid_subject(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 160
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _is_reserved_subject(subject: str) -> bool:
    return username_is_reserved(subject) or subject.endswith("@remote")


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _epoch_datetime(value: object, *, fallback: datetime | None = None) -> datetime:
    if value is None and fallback is not None:
        return fallback
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _raise("invalid_schema")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        _raise("invalid_schema")
    try:
        return datetime.fromtimestamp(numeric, timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        _raise("invalid_schema")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_source(path: Path, *, limit: int, required: bool) -> bytes | None:
    try:
        source_stat = path.lstat()
    except FileNotFoundError:
        if required:
            _raise("source_missing", record_failure=False)
        return None
    except OSError:
        _raise("source_not_regular", record_failure=False)
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
        _raise("source_not_regular", record_failure=False)
    if source_stat.st_size > limit:
        _raise("source_too_large", record_failure=False)

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        _raise("source_not_regular", record_failure=False)
    try:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_size > limit:
            _raise("source_too_large", record_failure=False)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                _raise("source_too_large", record_failure=False)
        final_stat = os.fstat(fd)
        if (
            final_stat.st_dev != opened_stat.st_dev
            or final_stat.st_ino != opened_stat.st_ino
            or final_stat.st_size != opened_stat.st_size
            or final_stat.st_mtime_ns != opened_stat.st_mtime_ns
        ):
            _raise("source_changed_during_read", record_failure=False)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        # Some Windows/network filesystems do not support directory fsync. The
        # file itself was fsynced before replace; preserve cross-platform use.
        if os.name != "nt":
            _raise("backup_failed", record_failure=False)


def _atomic_backup(raw: bytes, destination: Path) -> Path:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(destination.parent, 0o700)
        except OSError:
            pass
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_file():
                _raise("backup_failed", record_failure=False)
            if destination.stat().st_size != len(raw):
                _raise("backup_failed", record_failure=False)
            existing = destination.read_bytes()
            if not hmac.compare_digest(existing, raw):
                _raise("backup_failed", record_failure=False)
            return destination

        fd, temporary_name = tempfile.mkstemp(
            prefix=destination.name + ".tmp.",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            with os.fdopen(fd, "wb") as handle:
                fd = -1
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
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        if not hmac.compare_digest(destination.read_bytes(), raw):
            _raise("backup_failed", record_failure=False)
        return destination
    except LegacyAuthImportError:
        raise
    except OSError:
        _raise("backup_failed", record_failure=False)


def _backup_snapshot(
    *,
    auth_raw: bytes,
    sessions_raw: bytes | None,
    backup_dir: Path,
) -> tuple[str, str | None, Path, Path | None]:
    auth_digest = _sha256(auth_raw)
    sessions_digest = _sha256(sessions_raw) if sessions_raw is not None else None
    auth_backup = _atomic_backup(
        auth_raw,
        backup_dir / f"auth.{auth_digest}.json.bak",
    )
    sessions_backup = None
    if sessions_raw is not None:
        sessions_backup = _atomic_backup(
            sessions_raw,
            backup_dir / f"sessions.{sessions_digest}.json.bak",
        )
    return auth_digest, sessions_digest, auth_backup, sessions_backup


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateJSONKey
        value[key] = item
    return value


def _validate_json_bounds(value: Any) -> None:
    pending: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            _raise("invalid_json")
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)


def _parse_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJSONKey,
        RecursionError,
        MemoryError,
    ):
        _raise("invalid_json")
    _validate_json_bounds(value)
    return value


def _validate_bcrypt_hash(value: object) -> str:
    if not isinstance(value, str) or not _BCRYPT_RE.fullmatch(value):
        _raise("invalid_credential")
    return value


def _validate_capability_value(key: str, value: Any) -> Any:
    default = DEFAULT_PRIVILEGES[key]
    if isinstance(default, bool):
        if not isinstance(value, bool):
            _raise("invalid_schema")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _raise("invalid_schema")
        return value
    if isinstance(default, list):
        if not isinstance(value, list) or len(value) > 1_000:
            _raise("invalid_schema")
        cleaned: list[str] = []
        for item in value:
            if (
                not isinstance(item, str)
                or not item
                or len(item) > 255
                or any(ord(char) < 32 or ord(char) == 127 for char in item)
            ):
                _raise("invalid_schema")
            if item not in cleaned:
                cleaned.append(item)
        return cleaned
    _raise("invalid_schema")


def _capabilities(user: Mapping[str, Any], *, is_admin: bool) -> dict[str, Any]:
    if is_admin:
        return {
            key: list(value) if isinstance(value, list) else value
            for key, value in ADMIN_PRIVILEGES.items()
        }
    stored = user.get("privileges", {})
    if stored is None:
        stored = {}
    if not isinstance(stored, dict):
        _raise("invalid_schema")
    effective = {
        key: list(value) if isinstance(value, list) else value
        for key, value in DEFAULT_PRIVILEGES.items()
    }
    for key, value in stored.items():
        # Legacy AuthManager ignores unknown keys; preserve its effective
        # authorization instead of promoting inert data into capabilities.
        if key in DEFAULT_PRIVILEGES:
            effective[key] = _validate_capability_value(key, value)
    return effective


def _totp_secret(value: object) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 4096:
        _raise("invalid_schema")
    if value.startswith("enc:"):
        from src.secret_storage import decrypt

        plaintext = decrypt(value)
        if not plaintext:
            _raise("invalid_credential")
    else:
        plaintext = value
    if (
        not plaintext
        or len(plaintext) > 256
        or plaintext != plaintext.strip()
        or any(ord(char) < 33 or ord(char) == 127 for char in plaintext)
    ):
        _raise("invalid_credential")
    return plaintext


def _recovery_codes(value: object) -> tuple[_RecoveryCode, ...]:
    if value in (None, []):
        return ()
    if not isinstance(value, list) or len(value) > MAX_RECOVERY_CODES_PER_USER:
        _raise("invalid_schema")
    result: list[_RecoveryCode] = []
    seen: set[str] = set()
    for raw in value:
        if (
            not isinstance(raw, str)
            or not raw
            or len(raw) > 255
            or not raw.isascii()
            or any(ord(char) < 33 or ord(char) == 127 for char in raw)
        ):
            _raise("invalid_credential")
        if raw in seen:
            _raise("canonical_collision")
        seen.add(raw)
        if raw.startswith("sha256:"):
            if not _SHA256_PROTECTED_RE.fullmatch(raw):
                _raise("invalid_credential")
            kind = LEGACY_RECOVERY_SHA256
        elif raw.startswith(("$2a$", "$2b$", "$2y$")):
            _validate_bcrypt_hash(raw)
            kind = LEGACY_RECOVERY_BCRYPT
        else:
            if len(raw.encode("ascii")) > 72:
                _raise("invalid_credential")
            kind = "plaintext"
        result.append(_RecoveryCode(source_value=raw, source_kind=kind))
    return tuple(result)


def _parse_user(
    raw_username: object,
    raw_user: object,
    *,
    now: datetime,
    force_admin: bool = False,
) -> _LegacyUser:
    if not isinstance(raw_username, str) or not isinstance(raw_user, dict):
        _raise("invalid_schema")
    username = _normalized_subject(raw_username)
    if not _valid_subject(username):
        _raise("invalid_schema")
    if _is_reserved_subject(username):
        _raise("reserved_subject")
    password_hash = _validate_bcrypt_hash(raw_user.get("password_hash"))
    raw_admin = raw_user.get("is_admin")
    if raw_admin is not None and not isinstance(raw_admin, bool):
        _raise("invalid_schema")
    legacy_role = raw_user.get("role")
    if legacy_role is not None and not isinstance(legacy_role, str):
        _raise("invalid_schema")
    # Match AuthManager's legacy-role migration without allowing a stale
    # ``role=admin`` marker to override an explicit ``is_admin=false``.
    is_admin = force_admin or (
        bool(raw_admin)
        if raw_admin is not None
        else legacy_role == "admin"
    )
    created_at = _epoch_datetime(raw_user.get("created"), fallback=now)

    totp_enabled = raw_user.get("totp_enabled", False)
    if not isinstance(totp_enabled, bool):
        _raise("invalid_schema")
    secret = _totp_secret(raw_user.get("totp_secret"))
    pending = _totp_secret(raw_user.get("totp_secret_pending"))
    codes = _recovery_codes(raw_user.get("totp_backup_codes"))
    if totp_enabled and not secret:
        _raise("invalid_credential")
    if codes and not secret:
        _raise("invalid_credential")
    if totp_enabled:
        totp_state = "active"
    elif pending:
        totp_state = "pending"
    elif secret:
        totp_state = "disabled"
    else:
        totp_state = None

    return _LegacyUser(
        username=username,
        password_hash=password_hash,
        is_admin=is_admin,
        capabilities=_capabilities(raw_user, is_admin=is_admin),
        created_at=created_at,
        totp_state=totp_state,
        totp_secret=secret,
        totp_pending_secret=pending,
        recovery_codes=codes,
    )


def _parse_auth(raw: bytes, *, now: datetime) -> tuple[
    tuple[_LegacyUser, ...], tuple[str, ...], bool
]:
    value = _parse_json(raw)
    if not isinstance(value, dict):
        _raise("invalid_schema")
    if "users" in value and "password_hash" in value:
        _raise("invalid_schema")

    users: list[_LegacyUser] = []
    canonical_sources: dict[str, str] = {}
    if "users" in value:
        raw_users = value.get("users")
        if not isinstance(raw_users, dict) or not raw_users or len(raw_users) > MAX_USERS:
            _raise("invalid_schema")
        for raw_username, raw_user in raw_users.items():
            parsed = _parse_user(raw_username, raw_user, now=now)
            if parsed.username in canonical_sources:
                _raise("canonical_collision")
            canonical_sources[parsed.username] = raw_username
            users.append(parsed)
    else:
        if "password_hash" not in value:
            _raise("invalid_schema")
        username = value.get("username", "admin")
        single_user = {
            "password_hash": value.get("password_hash"),
            "created": value.get("created"),
            "is_admin": True,
            "privileges": value.get("privileges", {}),
            "totp_enabled": value.get("totp_enabled", False),
            "totp_secret": value.get("totp_secret"),
            "totp_secret_pending": value.get("totp_secret_pending"),
            "totp_backup_codes": value.get("totp_backup_codes"),
        }
        parsed = _parse_user(username, single_user, now=now, force_admin=True)
        canonical_sources[parsed.username] = str(username)
        users.append(parsed)

    # Restia's setup and role-management contracts always preserve at least
    # one administrator. Importing a manually damaged all-member store would
    # make the database authority impossible to administer without an unsafe
    # out-of-band privilege edit.
    if not any(user.is_admin for user in users):
        _raise("authorization_conflict")

    signup_enabled = value.get("signup_enabled", False)
    if not isinstance(signup_enabled, bool):
        _raise("invalid_schema")

    raw_retired = value.get("retired_usernames", [])
    if not isinstance(raw_retired, list) or len(raw_retired) > MAX_USERS:
        _raise("invalid_schema")
    retired: list[str] = []
    retired_seen: set[str] = set()
    for raw_subject in raw_retired:
        if not isinstance(raw_subject, str):
            _raise("invalid_schema")
        subject = _normalized_subject(raw_subject)
        if not _valid_subject(subject):
            _raise("invalid_schema")
        if _is_reserved_subject(subject):
            _raise("reserved_subject")
        if subject in retired_seen:
            _raise("canonical_collision")
        if subject in canonical_sources:
            _raise("identity_conflict")
        retired_seen.add(subject)
        retired.append(subject)
    return tuple(users), tuple(sorted(retired)), signup_enabled


def _session_digest(raw_key: object) -> str:
    if not isinstance(raw_key, str):
        _raise("invalid_schema")
    if raw_key.startswith("sha256:"):
        if not _SESSION_SHA256_RE.fullmatch(raw_key):
            _raise("invalid_credential")
        return raw_key
    if not _RAW_LEGACY_SESSION_RE.fullmatch(raw_key):
        _raise("invalid_credential")
    return "sha256:" + hashlib.sha256(raw_key.encode("ascii")).hexdigest()


def _parse_sessions(
    raw: bytes | None,
    *,
    users: Mapping[str, _LegacyUser],
    now: datetime,
) -> tuple[_LegacySession, ...]:
    if raw is None:
        return ()
    value = _parse_json(raw)
    if not isinstance(value, dict) or len(value) > MAX_SESSIONS:
        _raise("invalid_schema")
    result: list[_LegacySession] = []
    seen_digests: set[str] = set()
    now_timestamp = now.replace(tzinfo=timezone.utc).timestamp()
    for raw_key, raw_session in value.items():
        if not isinstance(raw_session, dict):
            _raise("invalid_schema")
        digest = _session_digest(raw_key)
        expiry = raw_session.get("expiry")
        if isinstance(expiry, bool) or not isinstance(expiry, (int, float)):
            _raise("invalid_schema")
        expiry_value = float(expiry)
        if not math.isfinite(expiry_value) or expiry_value < 0:
            _raise("invalid_schema")
        username = _normalized_subject(raw_session.get("username"))
        if not _valid_subject(username) or _is_reserved_subject(username):
            _raise("invalid_schema")
        # Expired sessions are intentionally not migrated, even when their
        # former account has since been removed from auth.json.
        if expiry_value <= now_timestamp:
            continue
        if username not in users:
            _raise("session_conflict")
        if digest in seen_digests:
            _raise("canonical_collision")
        seen_digests.add(digest)
        result.append(_LegacySession(
            token_digest=digest,
            username=username,
            expires_at=_epoch_datetime(expiry_value),
        ))
    return tuple(result)


def _parse_snapshot(
    auth_raw: bytes,
    sessions_raw: bytes | None,
    *,
    now: datetime,
) -> _LegacySnapshot:
    users, retired, signup_enabled = _parse_auth(auth_raw, now=now)
    if sum(
        code.source_kind == "plaintext"
        for user in users
        for code in user.recovery_codes
    ) > MAX_PLAINTEXT_RECOVERY_CODES:
        _raise("invalid_schema")
    user_map = {user.username: user for user in users}
    sessions = _parse_sessions(sessions_raw, users=user_map, now=now)
    return _LegacySnapshot(
        users=users,
        retired_subjects=retired,
        signup_enabled=signup_enabled,
        sessions=sessions,
    )


def _same_datetime(left: datetime | None, right: datetime | None) -> bool:
    if left is None or right is None:
        return left is right
    return _naive_utc(left) == _naive_utc(right)


def _canonical_account_maps(
    db,
) -> tuple[
    dict[str, Account],
    dict[str, AuthIdentity],
    dict[str, RetiredAuthSubject],
]:
    accounts: dict[str, Account] = {}
    for account in db.query(Account).all():
        subject = _normalized_subject(account.username)
        if not _valid_subject(subject) or _is_reserved_subject(subject) or subject in accounts:
            _raise("canonical_collision")
        accounts[subject] = account

    identities: dict[str, AuthIdentity] = {}
    rows = db.query(AuthIdentity).filter(
        AuthIdentity.provider == LOCAL_PROVIDER,
        AuthIdentity.issuer == LOCAL_ISSUER,
    ).all()
    for identity in rows:
        subject = _normalized_subject(identity.subject)
        if not _valid_subject(subject) or _is_reserved_subject(subject) or subject in identities:
            _raise("canonical_collision")
        identities[subject] = identity
    retired: dict[str, RetiredAuthSubject] = {}
    rows = db.query(RetiredAuthSubject).filter(
        RetiredAuthSubject.provider == LOCAL_PROVIDER,
        RetiredAuthSubject.issuer == LOCAL_ISSUER,
    ).all()
    for row in rows:
        subject = _normalized_subject(row.subject)
        if not _valid_subject(subject) or _is_reserved_subject(subject) or subject in retired:
            _raise("canonical_collision")
        retired[subject] = row
    return accounts, identities, retired


def _reconcile_account(
    db,
    user: _LegacyUser,
    *,
    account_map: dict[str, Account],
    identity_map: dict[str, AuthIdentity],
    retired_map: Mapping[str, RetiredAuthSubject],
    now: datetime,
) -> tuple[Account, AuthIdentity]:
    account_by_name = account_map.get(user.username)
    identity = identity_map.get(user.username)
    account_by_identity = db.query(Account).filter(
        Account.id == identity.account_id
    ).first() if identity is not None else None

    if identity is not None and account_by_identity is None:
        _raise("identity_conflict")
    if (
        account_by_name is not None
        and account_by_identity is not None
        and account_by_name.id != account_by_identity.id
    ):
        _raise("identity_conflict")
    account = account_by_identity or account_by_name
    if account is None:
        account = Account(
            id=str(uuid.uuid4()),
            username=user.username,
            status="active",
            auth_epoch=1,
            created_at=user.created_at,
            updated_at=user.created_at,
        )
        db.add(account)
        db.flush()
        account_map[user.username] = account
    elif (
        account.username != user.username
        or account.status != "active"
        or int(account.auth_epoch or 0) < 1
    ):
        _raise("identity_conflict")

    if user.username in retired_map:
        _raise("identity_conflict")

    if identity is None:
        # Never infer linkage through email or metadata. Only the exact local
        # canonical subject and exact Account.username are considered.
        other_local = db.query(AuthIdentity).filter(
            AuthIdentity.account_id == account.id,
            AuthIdentity.provider == LOCAL_PROVIDER,
            AuthIdentity.issuer == LOCAL_ISSUER,
        ).first()
        if other_local is not None:
            _raise("identity_conflict")
        identity = AuthIdentity(
            id=str(uuid.uuid4()),
            account_id=account.id,
            provider=LOCAL_PROVIDER,
            issuer=LOCAL_ISSUER,
            subject=user.username,
            state="active",
            linked_at=now,
            last_verified_at=now,
        )
        db.add(identity)
        db.flush()
        identity_map[user.username] = identity
    elif (
        identity.account_id != account.id
        or identity.subject != user.username
        or identity.state != "active"
    ):
        _raise("identity_conflict")
    return account, identity


def _reconcile_credential(db, account: Account, user: _LegacyUser) -> None:
    credential = db.query(LocalCredential).filter(
        LocalCredential.account_id == account.id
    ).first()
    if credential is None:
        db.add(LocalCredential(
            id=str(uuid.uuid4()),
            account_id=account.id,
            password_hash=user.password_hash,
            algorithm="bcrypt",
            version=1,
            password_changed_at=user.created_at,
        ))
        return
    if (
        credential.algorithm != "bcrypt"
        or not hmac.compare_digest(credential.password_hash, user.password_hash)
    ):
        _raise("credential_conflict")


def _reconcile_authorization(db, account: Account, user: _LegacyUser, *, now: datetime) -> None:
    expected_roles = {"member"}
    if user.is_admin:
        expected_roles.add("admin")
    existing_rows = db.query(AccountRole).filter(
        AccountRole.account_id == account.id
    ).all()
    existing_roles = {str(row.role) for row in existing_rows}
    if existing_roles - expected_roles:
        _raise("authorization_conflict")
    if "admin" in existing_roles and not user.is_admin:
        _raise("authorization_conflict")
    for role in sorted(expected_roles - existing_roles):
        db.add(AccountRole(
            id=str(uuid.uuid4()),
            account_id=account.id,
            role=role,
            granted_at=now,
        ))

    capability = db.query(AccountCapability).filter(
        AccountCapability.account_id == account.id
    ).first()
    if capability is None:
        db.add(AccountCapability(
            account_id=account.id,
            capabilities=user.capabilities,
        ))
    elif capability.capabilities != user.capabilities:
        _raise("authorization_conflict")


def _recovery_matches(source: _RecoveryCode, row: MfaRecoveryCode) -> bool:
    protected = str(row.code_hash or "")
    if source.source_kind == "plaintext":
        if row.digest_scheme != LEGACY_RECOVERY_BCRYPT:
            return False
        try:
            return bcrypt.checkpw(
                source.source_value.encode("utf-8"),
                protected.encode("utf-8"),
            )
        except (TypeError, ValueError):
            return False
    return (
        row.digest_scheme == source.source_kind
        and hmac.compare_digest(protected, source.source_value)
    )


def _new_recovery_protection(source: _RecoveryCode) -> tuple[str, str]:
    if source.source_kind == "plaintext":
        return (
            bcrypt.hashpw(
                source.source_value.encode("utf-8"), bcrypt.gensalt(rounds=10)
            ).decode("ascii"),
            LEGACY_RECOVERY_BCRYPT,
        )
    return source.source_value, source.source_kind


def _reconcile_mfa(
    db,
    account: Account,
    user: _LegacyUser,
    *,
    now: datetime,
) -> None:
    factor = db.query(MfaFactor).filter(
        MfaFactor.account_id == account.id,
        MfaFactor.kind == "totp",
    ).first()
    if user.totp_state is None:
        if factor is not None:
            _raise("mfa_conflict")
        return
    if factor is None:
        factor = MfaFactor(
            id=str(uuid.uuid4()),
            account_id=account.id,
            kind="totp",
            state=user.totp_state,
            secret=user.totp_secret,
            pending_secret=user.totp_pending_secret,
            confirmed_at=now if user.totp_state == "active" else None,
        )
        db.add(factor)
        db.flush()
    elif (
        factor.state != user.totp_state
        or factor.secret != user.totp_secret
        or factor.pending_secret != user.totp_pending_secret
    ):
        _raise("mfa_conflict")

    existing = db.query(MfaRecoveryCode).filter(
        MfaRecoveryCode.factor_id == factor.id
    ).all()
    if existing:
        unmatched = list(existing)
        for source in user.recovery_codes:
            match_index = next(
                (index for index, row in enumerate(unmatched) if _recovery_matches(source, row)),
                None,
            )
            if match_index is None:
                _raise("mfa_conflict")
            unmatched.pop(match_index)
        if unmatched:
            _raise("mfa_conflict")
        return
    for source in user.recovery_codes:
        protected, scheme = _new_recovery_protection(source)
        db.add(MfaRecoveryCode(
            id=str(uuid.uuid4()),
            factor_id=factor.id,
            code_hash=protected,
            digest_scheme=scheme,
        ))


def _reconcile_retired(
    db,
    subjects: Sequence[str],
    *,
    account_map: Mapping[str, Account],
    identity_map: Mapping[str, AuthIdentity],
    retired_map: dict[str, RetiredAuthSubject],
    now: datetime,
) -> None:
    for subject in subjects:
        if subject in identity_map or subject in account_map:
            _raise("identity_conflict")
        existing = retired_map.get(subject)
        if existing is None:
            existing = RetiredAuthSubject(
                id=str(uuid.uuid4()),
                provider=LOCAL_PROVIDER,
                issuer=LOCAL_ISSUER,
                subject=subject,
                account_id=None,
                reason="legacy_retired",
                retired_at=now,
            )
            db.add(existing)
            retired_map[subject] = existing
        elif existing.reason not in {"legacy_retired", "retired", "renamed", "deleted"}:
            _raise("identity_conflict")


def _reconcile_policy(db, signup_enabled: bool) -> None:
    policy = db.query(AuthPolicy).filter(AuthPolicy.id == "global").first()
    if policy is None:
        db.add(AuthPolicy(
            id="global",
            signup_enabled=signup_enabled,
            bootstrap_completed=True,
            version=1,
        ))
    elif bool(policy.signup_enabled) != signup_enabled:
        _raise("policy_conflict")
    else:
        policy.bootstrap_completed = True


def _reconcile_sessions(
    db,
    sessions: Sequence[_LegacySession],
    *,
    accounts: Mapping[str, Account],
    identities: Mapping[str, AuthIdentity],
) -> None:
    for source in sessions:
        account = accounts[source.username]
        identity = identities[source.username]
        existing = db.query(AuthSession).filter(
            AuthSession.token_digest == source.token_digest
        ).first()
        if existing is None:
            db.add(AuthSession(
                id=str(uuid.uuid4()),
                account_id=account.id,
                token_digest=source.token_digest,
                digest_scheme=LEGACY_SESSION_DIGEST_SCHEME,
                auth_epoch=account.auth_epoch,
                expires_at=source.expires_at,
                interface="web",
                auth_method="local",
                source_identity_id=identity.id,
            ))
        elif (
            existing.account_id != account.id
            or existing.digest_scheme != LEGACY_SESSION_DIGEST_SCHEME
            or int(existing.auth_epoch or 0) != int(account.auth_epoch or 0)
            or not _same_datetime(existing.expires_at, source.expires_at)
            or existing.revoked_at is not None
        ):
            _raise("session_conflict")
        elif existing.source_identity_id is None:
            existing.source_identity_id = identity.id
        elif existing.source_identity_id != identity.id:
            _raise("session_conflict")


def _backfill_api_tokens(db, accounts: Mapping[str, Account]) -> int:
    backfilled = 0
    for token in db.query(ApiToken).all():
        owner = _normalized_subject(token.owner)
        account = accounts.get(owner)
        if not owner or account is None:
            _raise("orphan_api_token")
        if token.account_id is None:
            token.account_id = account.id
            backfilled += 1
        elif token.account_id != account.id:
            _raise("orphan_api_token")
    return backfilled


def _redacted_details(
    *,
    accounts: int = 0,
    sessions: int = 0,
    retired_subjects: int = 0,
    api_tokens_backfilled: int = 0,
    error_code: str | None = None,
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "schema_version": 1,
        "accounts": int(accounts),
        "sessions": int(sessions),
        "retired_subjects": int(retired_subjects),
        "api_tokens_backfilled": int(api_tokens_backfilled),
    }
    if error_code:
        details["error_code"] = error_code
    return details


def _completed_result(
    run: AuthImportRun,
    *,
    auth_digest: str,
    sessions_digest: str | None,
    auth_backup: Path,
    sessions_backup: Path | None,
    details: Mapping[str, Any],
    idempotent: bool,
) -> LegacyAuthImportResult:
    return LegacyAuthImportResult(
        run_id=run.id,
        state="completed",
        auth_sha256=auth_digest,
        sessions_sha256=sessions_digest,
        backup_auth_path=str(auth_backup),
        backup_sessions_path=str(sessions_backup) if sessions_backup else None,
        accounts=int(details.get("accounts", 0)),
        sessions=int(details.get("sessions", 0)),
        retired_subjects=int(details.get("retired_subjects", 0)),
        api_tokens_backfilled=int(details.get("api_tokens_backfilled", 0)),
        idempotent=idempotent,
    )


def _open_session(session_factory):
    try:
        return session_factory()
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise LegacyAuthImportError("database_error") from None


def _is_transient_transaction_conflict(exc: OperationalError) -> bool:
    """Return whether a rolled-back transaction is safe to retry.

    PostgreSQL reports serialization failures, deadlocks, and lock timeouts by
    SQLSTATE. SQLite reports its single-writer contention as a message instead.
    Keep this deliberately narrow so unavailable/corrupt databases still fail
    loudly rather than being mistaken for an importer race.
    """

    original = exc.orig
    sqlstate = (
        getattr(original, "sqlstate", None)
        or getattr(original, "pgcode", None)
    )
    if sqlstate in _POSTGRES_TRANSACTION_RETRY_STATES:
        return True
    message = str(original).lower()
    return any(
        marker in message
        for marker in (
            "database is locked",
            "database table is locked",
            "database is busy",
        )
    )


def _next_retry_delay(delay: float) -> float:
    time.sleep(delay)
    return min(delay * 2, _IMPORT_RETRY_MAX_SECONDS)


def _apply_import_once(
    session_factory,
    *,
    snapshot: _LegacySnapshot,
    auth_digest: str,
    sessions_digest: str | None,
    auth_backup: Path,
    sessions_backup: Path | None,
    now: datetime,
) -> LegacyAuthImportResult | None:
    """Apply one transaction, returning ``None`` when another starter won."""

    db = _open_session(session_factory)
    try:
        run = db.query(AuthImportRun).filter(
            AuthImportRun.source_kind == IMPORT_SOURCE_KIND
        ).with_for_update().first()
        idempotent = False
        if run is not None and run.state == "completed":
            if (
                run.auth_sha256 != auth_digest
                or run.sessions_sha256 != sessions_digest
            ):
                _raise("source_changed", record_failure=False)
            # The database became the sole authority when this run completed.
            # Never reconcile the preserved, intentionally stale JSON snapshot
            # again: doing so would reject legitimate post-cutover password,
            # role, MFA, policy, and account changes or silently turn the file
            # back into a second source of truth.
            details = run.details if isinstance(run.details, dict) else {}
            result = _completed_result(
                run,
                auth_digest=auth_digest,
                sessions_digest=sessions_digest,
                auth_backup=auth_backup,
                sessions_backup=sessions_backup,
                details=details,
                idempotent=True,
            )
            db.rollback()
            return result
        elif run is not None and run.state == "pending":
            # A pending row from an older/manual workflow is not a validation
            # failure and must never be rewritten to ``failed`` by a waiter.
            _raise("import_in_progress", record_failure=False)
        elif run is None:
            run = AuthImportRun(
                id=str(uuid.uuid4()),
                source_kind=IMPORT_SOURCE_KIND,
                state="pending",
                auth_sha256=auth_digest,
                sessions_sha256=sessions_digest,
                backup_auth_path=str(auth_backup),
                backup_sessions_path=str(sessions_backup) if sessions_backup else None,
                details=_redacted_details(),
            )
            db.add(run)
            try:
                # The unique source_kind is the portable first-starter claim.
                # PostgreSQL blocks a competing insert until this transaction
                # commits; SQLite may instead report a busy writer. In either
                # case the loser rolls back and rereads the winner's result.
                db.flush()
            except IntegrityError:
                db.rollback()
                return None
        else:
            run.state = "pending"
            run.auth_sha256 = auth_digest
            run.sessions_sha256 = sessions_digest
            run.backup_auth_path = str(auth_backup)
            run.backup_sessions_path = str(sessions_backup) if sessions_backup else None
            run.details = _redacted_details()
            run.completed_at = None

        account_map, identity_map, retired_map = _canonical_account_maps(db)
        imported_accounts: dict[str, Account] = {}
        imported_identities: dict[str, AuthIdentity] = {}
        for user in snapshot.users:
            account, identity = _reconcile_account(
                db,
                user,
                account_map=account_map,
                identity_map=identity_map,
                retired_map=retired_map,
                now=now,
            )
            _reconcile_credential(db, account, user)
            _reconcile_authorization(db, account, user, now=now)
            _reconcile_mfa(db, account, user, now=now)
            imported_accounts[user.username] = account
            imported_identities[user.username] = identity

        _reconcile_retired(
            db,
            snapshot.retired_subjects,
            account_map=account_map,
            identity_map=identity_map,
            retired_map=retired_map,
            now=now,
        )
        _reconcile_policy(db, snapshot.signup_enabled)
        _reconcile_sessions(
            db,
            snapshot.sessions,
            accounts=imported_accounts,
            identities=imported_identities,
        )
        api_tokens_backfilled = _backfill_api_tokens(db, imported_accounts)
        details = _redacted_details(
            accounts=len(snapshot.users),
            sessions=len(snapshot.sessions),
            retired_subjects=len(snapshot.retired_subjects),
            api_tokens_backfilled=api_tokens_backfilled,
        )
        if not idempotent:
            run.state = "completed"
            run.details = details
            run.completed_at = now
        db.commit()
        return _completed_result(
            run,
            auth_digest=auth_digest,
            sessions_digest=sessions_digest,
            auth_backup=auth_backup,
            sessions_backup=sessions_backup,
            details=details,
            idempotent=False,
        )
    except OperationalError as exc:
        db.rollback()
        if _is_transient_transaction_conflict(exc):
            return None
        raise
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def _apply_import(
    session_factory,
    *,
    snapshot: _LegacySnapshot,
    auth_digest: str,
    sessions_digest: str | None,
    auth_backup: Path,
    sessions_backup: Path | None,
    now: datetime,
) -> LegacyAuthImportResult:
    """Apply or join the single transaction for this immutable snapshot."""

    delay = _IMPORT_RETRY_INITIAL_SECONDS
    while True:
        result = _apply_import_once(
            session_factory,
            snapshot=snapshot,
            auth_digest=auth_digest,
            sessions_digest=sessions_digest,
            auth_backup=auth_backup,
            sessions_backup=sessions_backup,
            now=now,
        )
        if result is not None:
            return result
        delay = _next_retry_delay(delay)


def _record_failure_once(
    session_factory,
    *,
    error_code: str,
    auth_digest: str,
    sessions_digest: str | None,
    auth_backup: Path,
    sessions_backup: Path | None,
) -> bool:
    db = _open_session(session_factory)
    try:
        run = db.query(AuthImportRun).filter(
            AuthImportRun.source_kind == IMPORT_SOURCE_KIND
        ).with_for_update().first()
        if run is not None and run.state == "completed":
            db.rollback()
            return True
        if run is None:
            run = AuthImportRun(
                id=str(uuid.uuid4()),
                source_kind=IMPORT_SOURCE_KIND,
            )
            db.add(run)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                return False
        run.state = "failed"
        run.auth_sha256 = auth_digest
        run.sessions_sha256 = sessions_digest
        run.backup_auth_path = str(auth_backup)
        run.backup_sessions_path = str(sessions_backup) if sessions_backup else None
        run.details = _redacted_details(error_code=error_code)
        run.completed_at = None
        db.commit()
        return True
    except OperationalError as exc:
        db.rollback()
        if _is_transient_transaction_conflict(exc):
            return False
        raise LegacyAuthImportError("database_error") from None
    except BaseException:
        db.rollback()
        raise LegacyAuthImportError("database_error") from None
    finally:
        db.close()


def _record_failure(
    session_factory,
    *,
    error_code: str,
    auth_digest: str,
    sessions_digest: str | None,
    auth_backup: Path,
    sessions_backup: Path | None,
) -> None:
    """Record one redacted failure without racing a completing importer."""

    delay = _IMPORT_RETRY_INITIAL_SECONDS
    while not _record_failure_once(
        session_factory,
        error_code=error_code,
        auth_digest=auth_digest,
        sessions_digest=sessions_digest,
        auth_backup=auth_backup,
        sessions_backup=sessions_backup,
    ):
        delay = _next_retry_delay(delay)


def _completed_digest_check_once(
    session_factory,
    *,
    auth_digest: str,
    sessions_digest: str | None,
) -> bool:
    db = _open_session(session_factory)
    try:
        run = db.query(AuthImportRun).filter(
            AuthImportRun.source_kind == IMPORT_SOURCE_KIND
        ).first()
        if run is not None and run.state == "completed" and (
            run.auth_sha256 != auth_digest
            or run.sessions_sha256 != sessions_digest
        ):
            _raise("source_changed", record_failure=False)
        db.rollback()
        return True
    except LegacyAuthImportError:
        db.rollback()
        raise
    except OperationalError as exc:
        db.rollback()
        if _is_transient_transaction_conflict(exc):
            return False
        raise LegacyAuthImportError("database_error") from None
    except BaseException as exc:
        db.rollback()
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise LegacyAuthImportError("database_error") from None
    finally:
        db.close()


def _completed_digest_check(
    session_factory,
    *,
    auth_digest: str,
    sessions_digest: str | None,
) -> None:
    delay = _IMPORT_RETRY_INITIAL_SECONDS
    while not _completed_digest_check_once(
        session_factory,
        auth_digest=auth_digest,
        sessions_digest=sessions_digest,
    ):
        delay = _next_retry_delay(delay)


def import_legacy_auth(
    session_factory,
    *,
    auth_path: str | os.PathLike[str],
    sessions_path: str | os.PathLike[str] | None = None,
    backup_dir: str | os.PathLike[str] | None = None,
    now: Callable[[], datetime] = utcnow_naive,
) -> LegacyAuthImportResult:
    """Import an immutable legacy snapshot into the unified auth tables.

    This function performs no cutover and never writes either source file.
    Exact source digests make a completed rerun idempotent; any later source
    change is a loud operator error rather than a second authority.
    """

    auth_source = Path(auth_path)
    sessions_source = (
        Path(sessions_path)
        if sessions_path is not None
        else auth_source.with_name("sessions.json")
    )
    backup_root = (
        Path(backup_dir)
        if backup_dir is not None
        else auth_source.parent / "legacy-auth-backups"
    )
    auth_raw = _read_source(
        auth_source,
        limit=MAX_AUTH_SOURCE_BYTES,
        required=True,
    )
    assert auth_raw is not None
    sessions_raw = _read_source(
        sessions_source,
        limit=MAX_SESSIONS_SOURCE_BYTES,
        required=False,
    )
    auth_digest, sessions_digest, auth_backup, sessions_backup = _backup_snapshot(
        auth_raw=auth_raw,
        sessions_raw=sessions_raw,
        backup_dir=backup_root,
    )

    # Ensure the paths still name the exact bytes that were backed up before
    # opening a database transaction.
    if _read_source(
        auth_source, limit=MAX_AUTH_SOURCE_BYTES, required=True
    ) != auth_raw:
        _raise("source_changed_during_read", record_failure=False)
    if _read_source(
        sessions_source, limit=MAX_SESSIONS_SOURCE_BYTES, required=False
    ) != sessions_raw:
        _raise("source_changed_during_read", record_failure=False)

    _completed_digest_check(
        session_factory,
        auth_digest=auth_digest,
        sessions_digest=sessions_digest,
    )
    try:
        current_time = _naive_utc(now())
        snapshot = _parse_snapshot(
            auth_raw,
            sessions_raw,
            now=current_time,
        )
        return _apply_import(
            session_factory,
            snapshot=snapshot,
            auth_digest=auth_digest,
            sessions_digest=sessions_digest,
            auth_backup=auth_backup,
            sessions_backup=sessions_backup,
            now=current_time,
        )
    except LegacyAuthImportError as exc:
        if exc.record_failure:
            _record_failure(
                session_factory,
                error_code=exc.code,
                auth_digest=auth_digest,
                sessions_digest=sessions_digest,
                auth_backup=auth_backup,
                sessions_backup=sessions_backup,
            )
        raise
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        _record_failure(
            session_factory,
            error_code="database_error",
            auth_digest=auth_digest,
            sessions_digest=sessions_digest,
            auth_backup=auth_backup,
            sessions_backup=sessions_backup,
        )
        raise LegacyAuthImportError("database_error") from None


__all__ = [
    "IMPORT_SOURCE_KIND",
    "LegacyAuthImportError",
    "LegacyAuthImportResult",
    "MAX_AUTH_SOURCE_BYTES",
    "MAX_SESSIONS_SOURCE_BYTES",
    "import_legacy_auth",
]
