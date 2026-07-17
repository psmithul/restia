"""Database-backed authentication primitives for the V3 identity cutover.

This is the portable repository used by the production database auth manager.
The legacy JSON files are accepted only by the one-time, backed-up importer and
never remain a concurrent writable authority.

Raw browser/native session tokens are never stored. New tokens use a keyed
HMAC-SHA256 digest, while the resolver can also read the one-way SHA-256 keys
already persisted by ``sessions.json``. API-token resolution similarly accepts
existing bcrypt hashes and the new indexed HMAC form.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Mapping

import bcrypt

from core.database import (
    Account,
    AccountCapability,
    AccountRole,
    ApiToken,
    AuthIdentity,
    AuthSession,
    LocalCredential,
    utcnow_naive,
)


LOCAL_PROVIDER = "local"
LOCAL_ISSUER = "restia-local"
ACTIVE_ACCOUNT = "active"
ACTIVE_IDENTITY = "active"

HMAC_SHA256_V1 = "hmac_sha256_v1"
LEGACY_SESSION_SHA256 = "sha256_legacy"
LEGACY_API_BCRYPT = "bcrypt_legacy"
SESSION_TOKEN_PREFIX = "rst_s_"
_SESSION_TOKEN_RE = re.compile(r"^rst_s_[A-Za-z0-9_-]{32,96}$")
_LEGACY_SESSION_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_API_TOKEN_RE = re.compile(r"^ody_[A-Za-z0-9_-]{16,96}$")
# A fixed valid bcrypt hash makes a missing/unsupported credential perform the
# same expensive verification step as an ordinary wrong password. The source
# plaintext is not a usable Restia password and this row is never persisted.
_DUMMY_BCRYPT_HASH = (
    "$2b$12$IldarhwYgAuUb/3njMvPCuuguogPq0WhZDZHSQ83Oe6vVCnHcBGTu"
)


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _normalized_local_subject(value: object) -> str:
    return str(value or "").strip().lower()


@dataclass(frozen=True)
class DatabasePrincipal:
    """Resolved immutable account plus its current authorization context."""

    account_id: str
    username: str
    roles: tuple[str, ...]
    capabilities: dict[str, Any]
    credential_type: str
    credential_id: str
    scopes: tuple[str, ...] = ()

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


@dataclass(frozen=True)
class IssuedSession:
    """One newly issued raw secret; callers must reveal it only once."""

    session_id: str
    token: str
    expires_at: datetime


class DatabaseAuthRepository:
    """Pure SQLAlchemy auth repository with explicit key and session factory."""

    def __init__(
        self,
        session_factory,
        *,
        token_hmac_key: bytes,
        default_capabilities: Mapping[str, Any] | None = None,
        admin_capabilities: Mapping[str, Any] | None = None,
        locked_capabilities: Mapping[str, Any] | None = None,
        now: Callable[[], datetime] = utcnow_naive,
    ) -> None:
        if not isinstance(token_hmac_key, bytes) or len(token_hmac_key) < 32:
            raise ValueError("token_hmac_key must contain at least 32 bytes")
        self._session_factory = session_factory
        self._token_hmac_key = token_hmac_key
        self._default_capabilities = dict(default_capabilities or {})
        self._admin_capabilities = dict(admin_capabilities or {})
        self._locked_capabilities = dict(locked_capabilities or {})
        self._now = now

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[Any]:
        db = self._session_factory()
        try:
            yield db
            if write:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _time(self) -> datetime:
        return _naive_utc(self._now())

    def _hmac_digest(self, purpose: bytes, raw_token: str) -> str:
        material = purpose + b"\x00" + raw_token.encode("utf-8")
        return hmac.new(
            self._token_hmac_key,
            material,
            hashlib.sha256,
        ).hexdigest()

    def session_token_digest(self, raw_token: str) -> str:
        return self._hmac_digest(b"restia-auth-session-v1", raw_token)

    def api_token_digest(self, raw_token: str) -> str:
        return self._hmac_digest(b"restia-api-token-v1", raw_token)

    @staticmethod
    def legacy_session_digest(raw_token: str) -> str:
        return "sha256:" + hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    def _account_for_local_subject(self, db, username: str) -> Account | None:
        subject = _normalized_local_subject(username)
        if not subject:
            return None
        identity = (
            db.query(AuthIdentity)
            .filter(
                AuthIdentity.provider == LOCAL_PROVIDER,
                AuthIdentity.issuer == LOCAL_ISSUER,
                AuthIdentity.subject == subject,
                AuthIdentity.state == ACTIVE_IDENTITY,
            )
            .first()
        )
        if identity is None:
            return None
        return (
            db.query(Account)
            .filter(
                Account.id == identity.account_id,
                Account.status == ACTIVE_ACCOUNT,
            )
            .first()
        )

    def find_account(self, account_id: str) -> Account | None:
        with self._db() as db:
            return (
                db.query(Account)
                .filter(Account.id == str(account_id or ""))
                .first()
            )

    def find_local_account(self, username: str) -> Account | None:
        with self._db() as db:
            return self._account_for_local_subject(db, username)

    def _roles(self, db, account_id: str) -> tuple[str, ...]:
        values = (
            db.query(AccountRole.role)
            .filter(AccountRole.account_id == account_id)
            .all()
        )
        return tuple(sorted({str(row[0]) for row in values if str(row[0] or "")}))

    def _capabilities(
        self,
        db,
        account_id: str,
        roles: tuple[str, ...],
    ) -> dict[str, Any]:
        if "admin" in roles:
            effective = dict(self._default_capabilities)
            effective.update(self._admin_capabilities)
            return effective
        row = (
            db.query(AccountCapability)
            .filter(AccountCapability.account_id == account_id)
            .first()
        )
        stored = getattr(row, "capabilities", None)
        if not isinstance(stored, dict):
            return dict(self._locked_capabilities)
        effective = dict(self._default_capabilities)
        for key, value in stored.items():
            if key not in self._default_capabilities:
                continue
            default = self._default_capabilities[key]
            valid = (
                isinstance(value, bool)
                if isinstance(default, bool)
                else (
                    isinstance(value, int) and not isinstance(value, bool) and value >= 0
                    if isinstance(default, int)
                    else (
                        isinstance(value, list)
                        and len(value) <= 256
                        and all(
                            isinstance(item, str)
                            and bool(item)
                            and len(item) <= 255
                            for item in value
                        )
                        if isinstance(default, list)
                        else False
                    )
                )
            )
            if not valid:
                return dict(self._locked_capabilities)
            effective[key] = value
        return effective

    def effective_authorization(
        self,
        account_id: str,
    ) -> tuple[tuple[str, ...], dict[str, Any]]:
        with self._db() as db:
            account = (
                db.query(Account)
                .filter(Account.id == account_id, Account.status == ACTIVE_ACCOUNT)
                .first()
            )
            if account is None:
                return (), {}
            roles = self._roles(db, account.id)
            return roles, self._capabilities(db, account.id, roles)

    def _principal(
        self,
        db,
        account: Account,
        *,
        credential_type: str,
        credential_id: str,
        scopes: tuple[str, ...] = (),
    ) -> DatabasePrincipal:
        roles = self._roles(db, account.id)
        return DatabasePrincipal(
            account_id=account.id,
            username=account.username,
            roles=roles,
            capabilities=self._capabilities(db, account.id, roles),
            credential_type=credential_type,
            credential_id=credential_id,
            scopes=scopes,
        )

    def verify_local_credential(
        self,
        username: str,
        password: str,
    ) -> DatabasePrincipal | None:
        """Verify a bcrypt local credential without issuing a session."""

        with self._db() as db:
            account = self._account_for_local_subject(db, username)
            credential = None
            if account is not None:
                credential = (
                    db.query(LocalCredential)
                    .filter(LocalCredential.account_id == account.id)
                    .first()
                )
            protected = (
                credential.password_hash
                if credential is not None and credential.algorithm == "bcrypt"
                else _DUMMY_BCRYPT_HASH
            )
            try:
                valid = bcrypt.checkpw(
                    str(password or "").encode("utf-8"),
                    protected.encode("utf-8"),
                )
            except (TypeError, ValueError):
                valid = False
            if account is None or credential is None or not valid:
                return None
            return self._principal(
                db,
                account,
                credential_type="local_password",
                credential_id=credential.id,
            )

    def create_session(
        self,
        account_id: str,
        *,
        ttl_seconds: int,
        interface: str = "web",
        auth_method: str = "local",
        source_identity_id: str | None = None,
        external_session_id: str | None = None,
    ) -> IssuedSession:
        if int(ttl_seconds) <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = self._time()
        raw_token = SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=int(ttl_seconds))
        session_id = str(uuid.uuid4())
        with self._db(write=True) as db:
            account = (
                db.query(Account)
                .filter(
                    Account.id == account_id,
                    Account.status == ACTIVE_ACCOUNT,
                )
                .first()
            )
            if account is None:
                raise ValueError("active account not found")
            account.last_login_at = now
            if source_identity_id:
                identity = (
                    db.query(AuthIdentity)
                    .filter(
                        AuthIdentity.id == source_identity_id,
                        AuthIdentity.account_id == account.id,
                        AuthIdentity.state == ACTIVE_IDENTITY,
                    )
                    .first()
                )
                if identity is None:
                    raise ValueError("source identity does not belong to account")
            db.add(AuthSession(
                id=session_id,
                account_id=account.id,
                token_digest=self.session_token_digest(raw_token),
                digest_scheme=HMAC_SHA256_V1,
                auth_epoch=account.auth_epoch,
                expires_at=expires_at,
                last_seen_at=now,
                interface=str(interface or "web")[:32],
                auth_method=str(auth_method or "local")[:32],
                source_identity_id=source_identity_id,
                external_session_id=(
                    str(external_session_id)[:255]
                    if external_session_id else None
                ),
            ))
        return IssuedSession(
            session_id=session_id,
            token=raw_token,
            expires_at=expires_at,
        )

    def _session_candidates(self, raw_token: str) -> tuple[str, ...]:
        legacy = self.legacy_session_digest(raw_token)
        return (
            self.session_token_digest(raw_token),
            legacy,
            legacy.removeprefix("sha256:"),
        )

    @staticmethod
    def _valid_session_token(raw_token: object) -> bool:
        return isinstance(raw_token, str) and bool(
            _SESSION_TOKEN_RE.fullmatch(raw_token)
            or _LEGACY_SESSION_TOKEN_RE.fullmatch(raw_token)
        )

    @staticmethod
    def _valid_api_token(raw_token: object) -> bool:
        return isinstance(raw_token, str) and bool(_API_TOKEN_RE.fullmatch(raw_token))

    def _session_matches(self, row: AuthSession, raw_token: str) -> bool:
        scheme = str(row.digest_scheme or "")
        if scheme == HMAC_SHA256_V1:
            return hmac.compare_digest(
                row.token_digest,
                self.session_token_digest(raw_token),
            )
        if scheme == LEGACY_SESSION_SHA256:
            legacy = self.legacy_session_digest(raw_token)
            return hmac.compare_digest(row.token_digest, legacy) or hmac.compare_digest(
                row.token_digest,
                legacy.removeprefix("sha256:"),
            )
        return False

    def resolve_session(self, raw_token: str | None) -> DatabasePrincipal | None:
        if not self._valid_session_token(raw_token):
            return None
        now = self._time()
        with self._db() as db:
            rows = (
                db.query(AuthSession)
                .filter(AuthSession.token_digest.in_(self._session_candidates(raw_token)))
                .all()
            )
            for row in rows:
                if not self._session_matches(row, raw_token):
                    continue
                if row.revoked_at is not None or row.expires_at <= now:
                    return None
                account = (
                    db.query(Account)
                    .filter(
                        Account.id == row.account_id,
                        Account.status == ACTIVE_ACCOUNT,
                    )
                    .first()
                )
                if account is None or row.auth_epoch != account.auth_epoch:
                    return None
                return self._principal(
                    db,
                    account,
                    credential_type="session",
                    credential_id=row.id,
                )
        return None

    def revoke_session(self, raw_token: str | None) -> bool:
        if not self._valid_session_token(raw_token):
            return False
        now = self._time()
        with self._db(write=True) as db:
            rows = (
                db.query(AuthSession)
                .filter(AuthSession.token_digest.in_(self._session_candidates(raw_token)))
                .all()
            )
            for row in rows:
                if self._session_matches(row, raw_token) and row.revoked_at is None:
                    row.revoked_at = now
                    return True
        return False

    def revoke_account_sessions(
        self,
        account_id: str,
        *,
        except_session_id: str | None = None,
    ) -> int:
        now = self._time()
        with self._db(write=True) as db:
            query = db.query(AuthSession).filter(
                AuthSession.account_id == account_id,
                AuthSession.revoked_at.is_(None),
            )
            if except_session_id:
                query = query.filter(AuthSession.id != except_session_id)
            return int(query.update(
                {AuthSession.revoked_at: now},
                synchronize_session=False,
            ))

    def increment_auth_epoch(
        self,
        account_id: str,
        *,
        preserve_session_id: str | None = None,
    ) -> int:
        """Invalidate every old epoch, optionally advancing one live session."""

        now = self._time()
        with self._db(write=True) as db:
            account = (
                db.query(Account)
                .filter(Account.id == account_id)
                .with_for_update()
                .first()
            )
            if account is None:
                raise ValueError("account not found")
            account.auth_epoch = int(account.auth_epoch or 1) + 1
            sessions = db.query(AuthSession).filter(
                AuthSession.account_id == account.id,
                AuthSession.revoked_at.is_(None),
            ).all()
            for row in sessions:
                if preserve_session_id and row.id == preserve_session_id:
                    row.auth_epoch = account.auth_epoch
                else:
                    row.revoked_at = now
            new_epoch = account.auth_epoch
        return new_epoch

    def _api_token_matches(self, token: ApiToken, raw_token: str) -> bool:
        scheme = str(getattr(token, "digest_scheme", None) or LEGACY_API_BCRYPT)
        protected = str(token.token_hash or "")
        if scheme == HMAC_SHA256_V1:
            return hmac.compare_digest(protected, self.api_token_digest(raw_token))
        if scheme == LEGACY_API_BCRYPT:
            try:
                return bcrypt.checkpw(
                    raw_token.encode("utf-8"),
                    protected.encode("utf-8"),
                )
            except (TypeError, ValueError):
                return False
        return False

    def resolve_api_token(self, raw_token: str | None) -> DatabasePrincipal | None:
        """Resolve only account-linked tokens; legacy owner strings are inert."""

        if not self._valid_api_token(raw_token):
            return None
        now = self._time()
        prefix = raw_token[:8]
        with self._db() as db:
            candidates = (
                db.query(ApiToken)
                .filter(
                    ApiToken.token_prefix == prefix,
                    ApiToken.is_active.is_(True),
                    ApiToken.revoked_at.is_(None),
                )
                .all()
            )
            for token in candidates:
                if token.expires_at is not None and token.expires_at <= now:
                    continue
                if not self._api_token_matches(token, raw_token):
                    continue
                if not token.account_id:
                    return None
                account = (
                    db.query(Account)
                    .filter(
                        Account.id == token.account_id,
                        Account.status == ACTIVE_ACCOUNT,
                    )
                    .first()
                )
                if account is None:
                    return None
                scopes = tuple(dict.fromkeys(
                    scope.strip()
                    for scope in str(token.scopes or "chat").split(",")
                    if scope.strip()
                ))
                return self._principal(
                    db,
                    account,
                    credential_type="api_token",
                    credential_id=token.id,
                    scopes=scopes,
                )
        return None
