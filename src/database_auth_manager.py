"""Database authority implementing Restia's existing authentication contract.

The public methods intentionally mirror :class:`core.auth.AuthManager` so the
HTTP routes and policy helpers can move from JSON files to one transactional
authority without a flag day.  Usernames remain compatibility aliases; every
credential, session, role, and external identity resolves to ``Account.id``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit

import bcrypt
import pyotp
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from core.auth import (
    ADMIN_PRIVILEGES,
    DEFAULT_PRIVILEGES,
    LOCKED_PRIVILEGES,
    RESERVED_USERNAMES,
    SetAdminResult,
    TOKEN_TTL,
    username_is_reserved,
)
from core.database import (
    Account,
    AccountCapability,
    AccountRole,
    ApiToken,
    AuthIdentity,
    AuthPolicy,
    AuthSession,
    LocalCredential,
    MfaFactor,
    MfaRecoveryCode,
    RetiredAuthSubject,
    SessionLocal,
    utcnow_naive,
)
from src.auth_keyring import load_auth_token_hmac_key
from src.constants import PASSWORD_MIN_LENGTH
from src.database_auth import (
    ACTIVE_ACCOUNT,
    ACTIVE_IDENTITY,
    DatabaseAuthRepository,
    DatabasePrincipal,
    HMAC_SHA256_V1,
    LOCAL_ISSUER,
    LOCAL_PROVIDER,
    SESSION_TOKEN_PREFIX,
)


_POLICY_ID = "global"
_MEMBER_ROLE = "member"
_ADMIN_ROLE = "admin"
_RECOVERY_HMAC_SCHEME = "hmac_sha256_v1"
_RECOVERY_SHA256_LEGACY = "sha256_legacy"
_RECOVERY_BCRYPT_LEGACY = "bcrypt_legacy"
_DUMMY_BCRYPT_HASH = (
    "$2b$12$IldarhwYgAuUb/3njMvPCuuguogPq0WhZDZHSQ83Oe6vVCnHcBGTu"
)
_EXTERNAL_IDENTITY_PROVIDERS = frozenset({"supabase"})


@dataclass(frozen=True, slots=True)
class DatabaseLoginResult:
    token: str | None = None
    username: str | None = None
    account_id: str | None = None
    requires_totp: bool = False


def _normal_username(value: object) -> str:
    return str(value or "").strip().lower()


def _valid_username(value: object) -> str | None:
    username = _normal_username(value)
    if (
        not username
        or len(username) > 160
        or username_is_reserved(username)
        or username.endswith("@remote")
        or any(ord(char) < 32 or ord(char) == 127 for char in username)
    ):
        return None
    return username


def _copy_privileges(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: list(item) if isinstance(item, list) else item
        for key, item in value.items()
    }


def _valid_external_identity(
    provider: object,
    issuer: object,
    subject: object,
) -> tuple[str, str, str] | None:
    """Validate an already-verified external identity without canonicalizing it."""

    if provider not in _EXTERNAL_IDENTITY_PROVIDERS:
        return None
    if (
        not isinstance(issuer, str)
        or not issuer
        or issuer != issuer.strip()
        or len(issuer) > 500
        or any(unicodedata.category(char) == "Cc" for char in issuer)
    ):
        return None
    try:
        parsed = urlsplit(issuer)
        parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    if (
        not isinstance(subject, str)
        or not subject
        or len(subject) > 255
        or any(unicodedata.category(char) == "Cc" for char in subject)
    ):
        return None
    return provider, issuer, subject


def _sanitize_privileges(
    incoming: Mapping[str, Any] | None,
    *,
    base: Mapping[str, Any] = DEFAULT_PRIVILEGES,
) -> dict[str, Any]:
    result = _copy_privileges(base)
    if not isinstance(incoming, Mapping):
        return result
    for key, default in DEFAULT_PRIVILEGES.items():
        if key not in incoming:
            continue
        value = incoming[key]
        if isinstance(default, bool):
            if isinstance(value, bool):
                result[key] = value
        elif isinstance(default, int):
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[key] = min(value, 1_000_000)
        elif isinstance(default, list) and isinstance(value, list):
            cleaned: list[str] = []
            for item in value[:256]:
                text = str(item or "").strip()
                if text and len(text) <= 255 and text not in cleaned:
                    cleaned.append(text)
            result[key] = cleaned
    return result


class DatabaseAuthManager:
    """Transactional authentication manager backed by the canonical database."""

    def __init__(
        self,
        session_factory=SessionLocal,
        *,
        token_hmac_key: bytes | None = None,
        now: Callable[[], datetime] = utcnow_naive,
        wall_clock: Callable[[], float] = time.time,
        store_error: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._now = now
        self._wall_clock = wall_clock
        self._auth_load_failed = bool(store_error)
        self._config_lock = threading.RLock()
        self._setup_lock = threading.Lock()
        self._identity_migrations: set[str] = set()
        self._token_hmac_key = (
            load_auth_token_hmac_key()
            if token_hmac_key is None else token_hmac_key
        )
        self._repository = DatabaseAuthRepository(
            session_factory,
            token_hmac_key=self._token_hmac_key,
            default_capabilities=DEFAULT_PRIVILEGES,
            admin_capabilities=ADMIN_PRIVILEGES,
            locked_capabilities=LOCKED_PRIVILEGES,
            now=now,
        )

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
        value = self._now()
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    @staticmethod
    def _local_account_query(db, username: str):
        return (
            db.query(Account)
            .join(AuthIdentity, AuthIdentity.account_id == Account.id)
            .filter(
                Account.status == ACTIVE_ACCOUNT,
                AuthIdentity.provider == LOCAL_PROVIDER,
                AuthIdentity.issuer == LOCAL_ISSUER,
                AuthIdentity.subject == username,
                AuthIdentity.state == ACTIVE_IDENTITY,
            )
        )

    @classmethod
    def _local_account(cls, db, username: object) -> Account | None:
        normalized = _normal_username(username)
        if not normalized:
            return None
        return cls._local_account_query(db, normalized).first()

    @staticmethod
    def _has_role(db, account_id: str, role: str) -> bool:
        return db.query(AccountRole.id).filter(
            AccountRole.account_id == account_id,
            AccountRole.role == role,
        ).first() is not None

    @staticmethod
    def _policy_row(db, *, lock: bool = False) -> AuthPolicy:
        query = db.query(AuthPolicy).filter(AuthPolicy.id == _POLICY_ID)
        if lock:
            query = query.with_for_update()
        row = query.first()
        if row is None:
            row = AuthPolicy(
                id=_POLICY_ID,
                signup_enabled=False,
                bootstrap_completed=False,
                version=1,
            )
            db.add(row)
            db.flush()
        return row

    @property
    def auth_store_error(self) -> bool:
        return self._auth_load_failed

    @property
    def users(self) -> dict[str, dict[str, Any]]:
        if self._auth_load_failed:
            return {}
        with self._db() as db:
            accounts = (
                db.query(Account)
                .join(AuthIdentity, AuthIdentity.account_id == Account.id)
                .filter(
                    Account.status == ACTIVE_ACCOUNT,
                    AuthIdentity.provider == LOCAL_PROVIDER,
                    AuthIdentity.issuer == LOCAL_ISSUER,
                    AuthIdentity.state == ACTIVE_IDENTITY,
                )
                .order_by(Account.username.asc())
                .all()
            )
            result: dict[str, dict[str, Any]] = {}
            for account in accounts:
                roles = {
                    str(row[0])
                    for row in db.query(AccountRole.role).filter(
                        AccountRole.account_id == account.id
                    ).all()
                }
                result[account.username] = {
                    "account_id": account.id,
                    "created": (
                        account.created_at.timestamp()
                        if account.created_at is not None else None
                    ),
                    "is_admin": _ADMIN_ROLE in roles,
                    "privileges": self._privileges_for_account(db, account, roles),
                }
            return result

    @property
    def retired_usernames(self) -> set[str]:
        with self._db() as db:
            return {
                str(row[0])
                for row in db.query(RetiredAuthSubject.subject).filter(
                    RetiredAuthSubject.provider == LOCAL_PROVIDER,
                    RetiredAuthSubject.issuer == LOCAL_ISSUER,
                ).all()
            }

    def retire_username(self, username: str) -> None:
        if self._auth_load_failed:
            return
        normalized = _normal_username(username)
        if not normalized:
            return
        with self._config_lock, self._db(write=True) as db:
            if self._local_account(db, normalized) is not None:
                return
            exists = db.query(RetiredAuthSubject.id).filter(
                RetiredAuthSubject.provider == LOCAL_PROVIDER,
                RetiredAuthSubject.issuer == LOCAL_ISSUER,
                RetiredAuthSubject.subject == normalized,
            ).first()
            if exists is None:
                db.add(RetiredAuthSubject(
                    id=str(uuid.uuid4()),
                    provider=LOCAL_PROVIDER,
                    issuer=LOCAL_ISSUER,
                    subject=normalized,
                    reason="retired",
                ))

    @property
    def signup_enabled(self) -> bool:
        if self._auth_load_failed:
            return False
        with self._db() as db:
            row = db.query(AuthPolicy).filter(AuthPolicy.id == _POLICY_ID).first()
            return bool(row.signup_enabled) if row is not None else False

    @signup_enabled.setter
    def signup_enabled(self, value: bool) -> None:
        if self._auth_load_failed:
            return
        with self._config_lock, self._db(write=True) as db:
            row = self._policy_row(db, lock=True)
            row.signup_enabled = bool(value)
            row.version = int(row.version or 1) + 1

    def set_signup_enabled(self, value: bool, requesting_user: str) -> bool:
        """Change open signup while serializing and rechecking the admin."""

        if self._auth_load_failed:
            return False
        with self._config_lock, self._db(write=True) as db:
            row = self._policy_row(db, lock=True)
            requester = self._local_account(db, requesting_user)
            if requester is None or not self._has_role(
                db, requester.id, _ADMIN_ROLE
            ):
                return False
            row.signup_enabled = bool(value)
            row.version = int(row.version or 1) + 1
            return True

    @property
    def is_configured(self) -> bool:
        if self._auth_load_failed:
            return True
        with self._db() as db:
            policy = db.query(AuthPolicy).filter(
                AuthPolicy.id == _POLICY_ID
            ).first()
            if policy is not None and bool(policy.bootstrap_completed):
                return True
            return (
                db.query(LocalCredential.id)
                .join(Account, Account.id == LocalCredential.account_id)
                .join(AuthIdentity, AuthIdentity.account_id == Account.id)
                .filter(
                    Account.status == ACTIVE_ACCOUNT,
                    LocalCredential.algorithm == "bcrypt",
                    AuthIdentity.provider == LOCAL_PROVIDER,
                    AuthIdentity.issuer == LOCAL_ISSUER,
                    AuthIdentity.state == ACTIVE_IDENTITY,
                )
                .first()
                is not None
            )

    def policy(self) -> dict[str, Any]:
        return {
            "password_min_length": PASSWORD_MIN_LENGTH,
            "reserved_usernames": sorted(RESERVED_USERNAMES),
            "signup_enabled": self.signup_enabled,
            "session_days": TOKEN_TTL // 86400,
        }

    def _is_retired(self, db, username: str) -> bool:
        return db.query(RetiredAuthSubject.id).filter(
            RetiredAuthSubject.provider == LOCAL_PROVIDER,
            RetiredAuthSubject.issuer == LOCAL_ISSUER,
            RetiredAuthSubject.subject == username,
        ).first() is not None

    def _create_user_in_db(
        self,
        db,
        username: str,
        password: str,
        *,
        is_admin: bool,
        adopt_existing: bool = False,
    ) -> Account | None:
        if self._is_retired(db, username):
            return None
        account = self._local_account(db, username)
        if account is not None:
            if not adopt_existing or db.query(LocalCredential.id).filter(
                LocalCredential.account_id == account.id
            ).first() is not None:
                return None
        else:
            account = Account(
                id=str(uuid.uuid4()),
                username=username,
                display_name=username,
                status=ACTIVE_ACCOUNT,
                auth_epoch=1,
            )
            identity = AuthIdentity(
                id=str(uuid.uuid4()),
                account_id=account.id,
                provider=LOCAL_PROVIDER,
                issuer=LOCAL_ISSUER,
                subject=username,
                state=ACTIVE_IDENTITY,
            )
            db.add_all([account, identity])
        credential = LocalCredential(
            id=str(uuid.uuid4()),
            account_id=account.id,
            password_hash=bcrypt.hashpw(
                password.encode("utf-8"), bcrypt.gensalt()
            ).decode("ascii"),
            algorithm="bcrypt",
            version=1,
            password_changed_at=self._time(),
        )
        db.add(credential)
        if not self._has_role(db, account.id, _MEMBER_ROLE):
            db.add(AccountRole(
                id=str(uuid.uuid4()), account_id=account.id, role=_MEMBER_ROLE
            ))
        if is_admin and not self._has_role(db, account.id, _ADMIN_ROLE):
            db.add(AccountRole(
                id=str(uuid.uuid4()), account_id=account.id, role=_ADMIN_ROLE
            ))
        if db.query(AccountCapability.account_id).filter(
            AccountCapability.account_id == account.id
        ).first() is None:
            db.add(AccountCapability(
                account_id=account.id,
                capabilities=_copy_privileges(DEFAULT_PRIVILEGES),
            ))
        db.flush()
        return account

    def setup(self, username: str, password: str) -> bool:
        if self._auth_load_failed:
            return False
        normalized = _valid_username(username)
        if normalized is None or len(password or "") < PASSWORD_MIN_LENGTH:
            return False
        with self._setup_lock, self._config_lock:
            try:
                with self._db(write=True) as db:
                    policy = self._policy_row(db, lock=True)
                    # Bootstrap is a one-way security boundary.  An install
                    # that has completed setup must never reopen first-admin
                    # creation merely because local credentials were removed
                    # (for example after moving to an external identity-only
                    # account).
                    if bool(policy.bootstrap_completed):
                        return False
                    if db.query(LocalCredential.id).join(
                        Account, Account.id == LocalCredential.account_id
                    ).filter(Account.status == ACTIVE_ACCOUNT).first() is not None:
                        return False
                    return self._create_user_in_db(
                        db,
                        normalized,
                        password,
                        is_admin=True,
                        adopt_existing=True,
                    ) is not None and self._mark_bootstrap_completed(db)
            except IntegrityError:
                return False

    @staticmethod
    def _mark_bootstrap_completed(db) -> bool:
        policy = db.query(AuthPolicy).filter(
            AuthPolicy.id == _POLICY_ID
        ).one()
        policy.bootstrap_completed = True
        policy.version = int(policy.version or 1) + 1
        return True

    def create_user(
        self,
        username: str,
        password: str,
        is_admin: bool = False,
        requesting_user: str | None = None,
    ) -> bool:
        if self._auth_load_failed:
            return False
        normalized = _valid_username(username)
        if normalized is None or len(password or "") < PASSWORD_MIN_LENGTH:
            return False
        with self._config_lock:
            try:
                with self._db(write=True) as db:
                    policy = self._policy_row(db, lock=True)
                    requested_admin = bool(is_admin)
                    if requesting_user:
                        requester = self._local_account(db, requesting_user)
                        if requester is None or not self._has_role(
                            db, requester.id, _ADMIN_ROLE
                        ):
                            return False
                    elif not bool(policy.signup_enabled):
                        return False
                    else:
                        # Open signup may create members only.  The optional
                        # ``is_admin`` argument exists for the authenticated
                        # admin route and must not become an escalation path
                        # for another interface calling the manager directly.
                        requested_admin = False
                    return self._create_user_in_db(
                        db, normalized, password, is_admin=requested_admin
                    ) is not None
            except IntegrityError:
                return False

    def delete_user(self, username: str, requesting_user: str) -> bool:
        if self._auth_load_failed:
            return False
        target_name = _normal_username(username)
        requester_name = _normal_username(requesting_user)
        if not target_name or target_name == requester_name:
            return False
        with self._config_lock, self._db(write=True) as db:
            self._policy_row(db, lock=True)
            requester = self._local_account(db, requester_name)
            target = self._local_account(db, target_name)
            if (
                requester is None
                or target is None
                or not self._has_role(db, requester.id, _ADMIN_ROLE)
            ):
                return False
            if self._is_retired(db, target_name):
                return False
            db.add(RetiredAuthSubject(
                id=str(uuid.uuid4()),
                provider=LOCAL_PROVIDER,
                issuer=LOCAL_ISSUER,
                subject=target_name,
                account_id=target.id,
                reason="account_deleted",
            ))
            target.status = "deleted"
            target.auth_epoch = int(target.auth_epoch or 1) + 1
            db.query(AuthIdentity).filter(
                AuthIdentity.account_id == target.id
            ).update({AuthIdentity.state: "unlinked"}, synchronize_session=False)
            db.query(AuthSession).filter(
                AuthSession.account_id == target.id,
                AuthSession.revoked_at.is_(None),
            ).update({AuthSession.revoked_at: self._time()}, synchronize_session=False)
            db.query(ApiToken).filter(or_(
                ApiToken.account_id == target.id,
                ApiToken.owner == target_name,
            )
            ).update({
                ApiToken.is_active: False,
                ApiToken.revoked_at: self._time(),
            }, synchronize_session=False)
            db.query(LocalCredential).filter(
                LocalCredential.account_id == target.id
            ).delete(synchronize_session=False)
            factors = [
                row[0] for row in db.query(MfaFactor.id).filter(
                    MfaFactor.account_id == target.id
                ).all()
            ]
            if factors:
                db.query(MfaRecoveryCode).filter(
                    MfaRecoveryCode.factor_id.in_(factors)
                ).delete(synchronize_session=False)
                db.query(MfaFactor).filter(
                    MfaFactor.id.in_(factors)
                ).delete(synchronize_session=False)
            db.query(AccountRole).filter(
                AccountRole.account_id == target.id
            ).delete(synchronize_session=False)
            db.query(AccountCapability).filter(
                AccountCapability.account_id == target.id
            ).delete(synchronize_session=False)
            return True

    def rename_user(
        self,
        old_username: str,
        new_username: str,
        requesting_user: str,
    ) -> bool:
        if self._auth_load_failed:
            return False
        old_name = _normal_username(old_username)
        new_name = _valid_username(new_username)
        requester_name = _normal_username(requesting_user)
        if not old_name or new_name is None or old_name == new_name:
            return False
        with self._config_lock:
            try:
                with self._db(write=True) as db:
                    self._policy_row(db, lock=True)
                    requester = self._local_account(db, requester_name)
                    target = self._local_account(db, old_name)
                    if (
                        requester is None
                        or target is None
                        or not self._has_role(db, requester.id, _ADMIN_ROLE)
                        or self._local_account(db, new_name) is not None
                        or self._is_retired(db, new_name)
                    ):
                        return False
                    local_identity = db.query(AuthIdentity).filter(
                        AuthIdentity.account_id == target.id,
                        AuthIdentity.provider == LOCAL_PROVIDER,
                        AuthIdentity.issuer == LOCAL_ISSUER,
                        AuthIdentity.state == ACTIVE_IDENTITY,
                    ).one()
                    target.username = new_name
                    local_identity.subject = new_name
                    db.add(RetiredAuthSubject(
                        id=str(uuid.uuid4()),
                        provider=LOCAL_PROVIDER,
                        issuer=LOCAL_ISSUER,
                        subject=old_name,
                        account_id=target.id,
                        reason="renamed",
                    ))
                    db.query(ApiToken).filter(or_(
                        ApiToken.account_id == target.id,
                        ApiToken.owner == old_name,
                    )
                    ).update({ApiToken.owner: new_name}, synchronize_session=False)
                    db.flush()
                    return True
            except IntegrityError:
                return False

    def rollback_user_rename(
        self,
        current_username: str,
        former_username: str,
        requesting_user: str,
    ) -> bool:
        if self._auth_load_failed:
            return False
        current = _normal_username(current_username)
        former = _normal_username(former_username)
        requester_name = _normal_username(requesting_user)
        if not current or not former:
            return False
        with self._config_lock, self._db(write=True) as db:
            self._policy_row(db, lock=True)
            requester = self._local_account(db, requester_name)
            target = self._local_account(db, current)
            retired = db.query(RetiredAuthSubject).filter(
                RetiredAuthSubject.provider == LOCAL_PROVIDER,
                RetiredAuthSubject.issuer == LOCAL_ISSUER,
                RetiredAuthSubject.subject == former,
                RetiredAuthSubject.reason == "renamed",
                RetiredAuthSubject.account_id == (
                    target.id if target is not None else ""
                ),
            ).first()
            if (
                requester is None
                or target is None
                or retired is None
                or self._local_account(db, former) is not None
                or not self._has_role(db, requester.id, _ADMIN_ROLE)
            ):
                return False
            identity = db.query(AuthIdentity).filter(
                AuthIdentity.account_id == target.id,
                AuthIdentity.provider == LOCAL_PROVIDER,
                AuthIdentity.issuer == LOCAL_ISSUER,
                AuthIdentity.state == ACTIVE_IDENTITY,
            ).one()
            target.username = former
            identity.subject = former
            db.delete(retired)
            db.query(ApiToken).filter(or_(
                ApiToken.account_id == target.id,
                ApiToken.owner == current,
            )
            ).update({ApiToken.owner: former}, synchronize_session=False)
            return True

    def is_admin(self, username: str | None) -> bool:
        if self._auth_load_failed:
            return False
        with self._db() as db:
            account = self._local_account(db, username)
            return bool(
                account is not None
                and self._has_role(db, account.id, _ADMIN_ROLE)
            )

    def list_users(self) -> list[dict[str, Any]]:
        return [
            {
                "username": username,
                "is_admin": bool(value.get("is_admin")),
                "privileges": value.get("privileges", {}),
            }
            for username, value in self.users.items()
        ]

    def _privileges_for_account(
        self,
        db,
        account: Account,
        roles: set[str] | None = None,
    ) -> dict[str, Any]:
        roles = roles or {
            str(row[0])
            for row in db.query(AccountRole.role).filter(
                AccountRole.account_id == account.id
            ).all()
        }
        if _ADMIN_ROLE in roles:
            return _copy_privileges(ADMIN_PRIVILEGES)
        row = db.query(AccountCapability).filter(
            AccountCapability.account_id == account.id
        ).first()
        stored = row.capabilities if row is not None else None
        if not isinstance(stored, dict):
            return _copy_privileges(LOCKED_PRIVILEGES)
        for key, value in stored.items():
            if key not in DEFAULT_PRIVILEGES:
                continue
            default = DEFAULT_PRIVILEGES[key]
            if (
                (isinstance(default, bool) and not isinstance(value, bool))
                or (
                    isinstance(default, int)
                    and not isinstance(default, bool)
                    and (
                        not isinstance(value, int)
                        or isinstance(value, bool)
                        or value < 0
                    )
                )
                or (
                    isinstance(default, list)
                    and (
                        not isinstance(value, list)
                        or len(value) > 256
                        or any(
                            not isinstance(item, str)
                            or not item
                            or len(item) > 255
                            for item in value
                        )
                    )
                )
            ):
                return _copy_privileges(LOCKED_PRIVILEGES)
        return _sanitize_privileges(stored)

    def get_privileges(self, username: str | None) -> dict[str, Any]:
        if self._auth_load_failed:
            return _copy_privileges(LOCKED_PRIVILEGES)
        with self._db() as db:
            account = self._local_account(db, username)
            if account is None:
                return _copy_privileges(LOCKED_PRIVILEGES)
            return self._privileges_for_account(db, account)

    def set_privileges(
        self,
        username: str,
        privileges: Mapping[str, Any],
        requesting_user: str | None = None,
    ) -> bool:
        if self._auth_load_failed:
            return False
        with self._config_lock, self._db(write=True) as db:
            self._policy_row(db, lock=True)
            requester = self._local_account(db, requesting_user)
            if requester is None or not self._has_role(
                db, requester.id, _ADMIN_ROLE
            ):
                return False
            account = self._local_account(db, username)
            if account is None or self._has_role(db, account.id, _ADMIN_ROLE):
                return False
            row = db.query(AccountCapability).filter(
                AccountCapability.account_id == account.id
            ).first()
            current = self._privileges_for_account(db, account)
            updated = _sanitize_privileges(privileges, base=current)
            if row is None:
                db.add(AccountCapability(
                    account_id=account.id, capabilities=updated
                ))
            else:
                row.capabilities = updated
            return True

    def set_admin(
        self,
        username: str,
        is_admin: bool,
        requesting_user: str,
    ) -> SetAdminResult:
        if self._auth_load_failed:
            return SetAdminResult.NOT_AUTHORIZED
        with self._config_lock, self._db(write=True) as db:
            self._policy_row(db, lock=True)
            requester = self._local_account(db, requesting_user)
            if requester is None or not self._has_role(
                db, requester.id, _ADMIN_ROLE
            ):
                return SetAdminResult.NOT_AUTHORIZED
            target = self._local_account(db, username)
            if target is None:
                return SetAdminResult.USER_NOT_FOUND
            existing = db.query(AccountRole).filter(
                AccountRole.account_id == target.id,
                AccountRole.role == _ADMIN_ROLE,
            ).first()
            if bool(is_admin) == (existing is not None):
                return SetAdminResult.OK
            if is_admin:
                db.add(AccountRole(
                    id=str(uuid.uuid4()),
                    account_id=target.id,
                    role=_ADMIN_ROLE,
                    granted_by_account_id=requester.id,
                ))
                return SetAdminResult.OK
            admin_count = db.query(AccountRole.id).filter(
                AccountRole.role == _ADMIN_ROLE
            ).count()
            if admin_count <= 1:
                return SetAdminResult.LAST_ADMIN
            db.delete(existing)
            # API tokens are minted only by administrators and may carry
            # host-control scopes such as cookbook:launch or email:send.
            # Demotion must withdraw those durable credentials in the same
            # transaction; checking a cached scope alone after the role change
            # would otherwise preserve the old authority indefinitely.
            now = self._time()
            db.query(ApiToken).filter(or_(
                ApiToken.account_id == target.id,
                ApiToken.owner == target.username,
            )).update({
                ApiToken.is_active: False,
                ApiToken.revoked_at: now,
            }, synchronize_session=False)
            return SetAdminResult.OK

    def verify_password(self, username: str, password: str) -> bool:
        if self._auth_load_failed:
            return False
        return self._repository.verify_local_credential(username, password) is not None

    def change_password(
        self,
        username: str,
        current_password: str,
        new_password: str,
        preserve_token: str | None = None,
    ) -> bool:
        if (
            self._auth_load_failed
            or len(new_password or "") < PASSWORD_MIN_LENGTH
        ):
            return False
        now = self._time()
        with self._config_lock, self._db(write=True) as db:
            account = self._local_account_query(
                db, _normal_username(username)
            ).with_for_update().first()
            credential = db.query(LocalCredential).filter(
                LocalCredential.account_id == (
                    account.id if account is not None else ""
                ),
                LocalCredential.algorithm == "bcrypt",
            ).with_for_update().first()
            protected = (
                credential.password_hash
                if credential is not None else _DUMMY_BCRYPT_HASH
            )
            try:
                valid = bcrypt.checkpw(
                    str(current_password or "").encode("utf-8"),
                    protected.encode("ascii"),
                )
            except (TypeError, ValueError):
                valid = False
            if account is None or credential is None or not valid:
                return False
            credential.password_hash = bcrypt.hashpw(
                new_password.encode("utf-8"), bcrypt.gensalt()
            ).decode("ascii")
            credential.version = int(credential.version or 1) + 1
            credential.password_changed_at = now
            self._rotate_auth_epoch(
                db,
                account,
                preserve_token=preserve_token,
                now=now,
            )
            # Password rotation is also the recovery boundary for a mistakenly
            # linked external login. Require an explicit password/MFA step-up
            # before any Supabase subject can authenticate this account again.
            db.query(AuthIdentity).filter(
                AuthIdentity.account_id == account.id,
                AuthIdentity.provider != LOCAL_PROVIDER,
            ).update(
                {AuthIdentity.state: "unlinked"},
                synchronize_session=False,
            )
            return True

    def _rotate_auth_epoch(
        self,
        db,
        account: Account,
        *,
        preserve_token: str | None,
        now: datetime,
    ) -> None:
        """Revoke existing sessions while optionally preserving this request.

        Token matching and the epoch update happen under the same account and
        session-row transaction so another process cannot swap a stale token
        into the preserved slot.
        """

        previous_epoch = int(account.auth_epoch or 1)
        preserve_id = None
        if self._repository._valid_session_token(preserve_token):
            rows = db.query(AuthSession).filter(
                AuthSession.token_digest.in_(
                    self._repository._session_candidates(preserve_token)
                ),
                AuthSession.account_id == account.id,
            ).with_for_update().all()
            preserved = next(
                (
                    row for row in rows
                    if self._repository._session_matches(row, preserve_token)
                ),
                None,
            )
            if (
                preserved is not None
                and preserved.revoked_at is None
                and preserved.expires_at > now
                and int(preserved.auth_epoch or 0) == previous_epoch
            ):
                preserve_id = preserved.id

        account.auth_epoch = previous_epoch + 1
        sessions = db.query(AuthSession).filter(
            AuthSession.account_id == account.id,
            AuthSession.revoked_at.is_(None),
        ).with_for_update().all()
        for row in sessions:
            if preserve_id and row.id == preserve_id:
                row.auth_epoch = account.auth_epoch
            else:
                row.revoked_at = now

    def _active_factor(self, db, account_id: str) -> MfaFactor | None:
        return db.query(MfaFactor).filter(
            MfaFactor.account_id == account_id,
            MfaFactor.kind == "totp",
            MfaFactor.state == "active",
        ).first()

    def totp_enabled(self, username: str) -> bool:
        if self._auth_load_failed:
            return False
        with self._db() as db:
            account = self._local_account(db, username)
            return bool(
                account is not None
                and self._active_factor(db, account.id) is not None
            )

    def totp_generate_secret(self, username: str) -> str | None:
        if self._auth_load_failed:
            return None
        with self._config_lock, self._db(write=True) as db:
            account = self._local_account_query(
                db, _normal_username(username)
            ).with_for_update().first()
            if account is None or self._active_factor(db, account.id) is not None:
                return None
            factor = db.query(MfaFactor).filter(
                MfaFactor.account_id == account.id,
                MfaFactor.kind == "totp",
            ).with_for_update().first()
            if (
                factor is not None
                and factor.state == "pending"
                and factor.pending_secret
            ):
                return str(factor.pending_secret)
            secret = pyotp.random_base32()
            if factor is None:
                factor = MfaFactor(
                    id=str(uuid.uuid4()),
                    account_id=account.id,
                    kind="totp",
                    state="pending",
                )
                db.add(factor)
            factor.state = "pending"
            factor.secret = None
            factor.pending_secret = secret
            factor.confirmed_at = None
            factor.last_used_step = None
        return secret

    @staticmethod
    def totp_get_provisioning_uri(username: str, secret: str) -> str:
        return pyotp.TOTP(secret).provisioning_uri(
            name=_normal_username(username), issuer_name="Restia"
        )

    def _recovery_digest(self, code: str) -> str:
        material = b"restia-mfa-recovery-v1\x00" + code.encode("utf-8")
        return hmac.new(
            self._token_hmac_key, material, hashlib.sha256
        ).hexdigest()

    def totp_confirm_enable(
        self,
        username: str,
        code: str,
        password: str,
        preserve_token: str | None = None,
    ) -> list[str] | None:
        if self._auth_load_failed:
            return None
        with self._config_lock, self._db(write=True) as db:
            account = self._local_account_query(
                db, _normal_username(username)
            ).with_for_update().first()
            credential = db.query(LocalCredential).filter(
                LocalCredential.account_id == (
                    account.id if account is not None else ""
                ),
                LocalCredential.algorithm == "bcrypt",
            ).with_for_update().first()
            protected = (
                credential.password_hash
                if credential is not None else _DUMMY_BCRYPT_HASH
            )
            try:
                valid_password = bcrypt.checkpw(
                    str(password or "").encode("utf-8"),
                    protected.encode("ascii"),
                )
            except (TypeError, ValueError):
                valid_password = False
            if account is None or credential is None or not valid_password:
                return None
            factor = db.query(MfaFactor).filter(
                MfaFactor.account_id == account.id,
                MfaFactor.kind == "totp",
                MfaFactor.state == "pending",
            ).with_for_update().first()
            secret = str(factor.pending_secret or "") if factor else ""
            if not secret:
                return None
            matched_step = self._matching_totp_step(secret, str(code or ""))
            if matched_step is None:
                return None
            factor.secret = secret
            factor.pending_secret = None
            factor.state = "active"
            factor.confirmed_at = self._time()
            factor.last_used_step = matched_step
            db.query(MfaRecoveryCode).filter(
                MfaRecoveryCode.factor_id == factor.id
            ).delete(synchronize_session=False)
            plaintext = [secrets.token_urlsafe(9) for _ in range(8)]
            for value in plaintext:
                db.add(MfaRecoveryCode(
                    id=str(uuid.uuid4()),
                    factor_id=factor.id,
                    code_hash=self._recovery_digest(value),
                    digest_scheme=_RECOVERY_HMAC_SCHEME,
                ))
            self._rotate_auth_epoch(
                db,
                account,
                preserve_token=preserve_token,
                now=self._time(),
            )
            return plaintext

    def _matching_totp_step(self, secret: str, code: str) -> int | None:
        step = int(self._wall_clock() // 30)
        totp = pyotp.TOTP(secret)
        candidate_code = str(code or "")
        for candidate in (step - 1, step, step + 1):
            if candidate >= 0 and secrets.compare_digest(
                totp.at(candidate * 30), candidate_code
            ):
                return candidate
        return None

    def _verify_recovery_code(self, db, factor: MfaFactor, code: str) -> bool:
        now = self._time()
        digest = self._recovery_digest(code)
        direct = db.query(MfaRecoveryCode).filter(
            MfaRecoveryCode.factor_id == factor.id,
            MfaRecoveryCode.digest_scheme == _RECOVERY_HMAC_SCHEME,
            MfaRecoveryCode.code_hash == digest,
            MfaRecoveryCode.used_at.is_(None),
        ).first()
        if direct is not None:
            updated = db.query(MfaRecoveryCode).filter(
                MfaRecoveryCode.id == direct.id,
                MfaRecoveryCode.used_at.is_(None),
            ).update({MfaRecoveryCode.used_at: now}, synchronize_session=False)
            return updated == 1
        for row in db.query(MfaRecoveryCode).filter(
            MfaRecoveryCode.factor_id == factor.id,
            MfaRecoveryCode.used_at.is_(None),
            MfaRecoveryCode.digest_scheme.in_([
                _RECOVERY_SHA256_LEGACY,
                _RECOVERY_BCRYPT_LEGACY,
            ]),
        ).all():
            matched = False
            if row.digest_scheme == _RECOVERY_SHA256_LEGACY:
                expected = "sha256:" + hashlib.sha256(
                    code.encode("utf-8")
                ).hexdigest()
                matched = hmac.compare_digest(str(row.code_hash), expected)
            elif row.digest_scheme == _RECOVERY_BCRYPT_LEGACY:
                try:
                    matched = bcrypt.checkpw(
                        code.encode("utf-8"), str(row.code_hash).encode("ascii")
                    )
                except (TypeError, ValueError):
                    matched = False
            if matched:
                updated = db.query(MfaRecoveryCode).filter(
                    MfaRecoveryCode.id == row.id,
                    MfaRecoveryCode.used_at.is_(None),
                ).update({MfaRecoveryCode.used_at: now}, synchronize_session=False)
                return updated == 1
        return False

    def _verify_factor_code(self, db, factor: MfaFactor, code: str) -> bool:
        secret = str(factor.secret or "")
        if not secret:
            return False
        step = self._matching_totp_step(secret, str(code or ""))
        if step is not None:
            updated = db.query(MfaFactor).filter(
                MfaFactor.id == factor.id,
                or_(
                    MfaFactor.last_used_step.is_(None),
                    MfaFactor.last_used_step < step,
                ),
            ).update(
                {MfaFactor.last_used_step: step},
                synchronize_session=False,
            )
            return updated == 1
        return self._verify_recovery_code(db, factor, str(code or ""))

    def totp_verify(self, username: str, code: str) -> bool:
        if self._auth_load_failed:
            return False
        with self._db(write=True) as db:
            account = self._local_account(db, username)
            if account is None:
                return False
            factor = self._active_factor(db, account.id)
            if factor is None:
                return True
            return self._verify_factor_code(db, factor, str(code or ""))

    def totp_disable(
        self,
        username: str,
        password: str,
        preserve_token: str | None = None,
    ) -> bool:
        with self._config_lock, self._db(write=True) as db:
            account = self._local_account_query(
                db, _normal_username(username)
            ).with_for_update().first()
            credential = db.query(LocalCredential).filter(
                LocalCredential.account_id == (
                    account.id if account is not None else ""
                ),
                LocalCredential.algorithm == "bcrypt",
            ).with_for_update().first()
            protected = (
                credential.password_hash
                if credential is not None else _DUMMY_BCRYPT_HASH
            )
            try:
                valid = bcrypt.checkpw(
                    str(password or "").encode("utf-8"),
                    protected.encode("ascii"),
                )
            except (TypeError, ValueError):
                valid = False
            if account is None or credential is None or not valid:
                return False
            factors = db.query(MfaFactor).filter(
                MfaFactor.account_id == account.id,
                MfaFactor.kind == "totp",
            ).all()
            for factor in factors:
                db.query(MfaRecoveryCode).filter(
                    MfaRecoveryCode.factor_id == factor.id
                ).delete(synchronize_session=False)
                db.delete(factor)
            self._rotate_auth_epoch(
                db,
                account,
                preserve_token=preserve_token,
                now=self._time(),
            )
            return True

    def authenticate_session(
        self,
        username: str,
        password: str,
        *,
        totp_code: str | None = None,
        interface: str = "web",
    ) -> DatabaseLoginResult:
        """Atomically verify every factor and issue one epoch-bound session."""

        if self._auth_load_failed:
            return DatabaseLoginResult()
        normalized = _normal_username(username)
        with self._db(write=True) as db:
            account = self._local_account_query(
                db, normalized
            ).with_for_update().first()
            credential = db.query(LocalCredential).filter(
                LocalCredential.account_id == (
                    account.id if account is not None else ""
                ),
                LocalCredential.algorithm == "bcrypt",
            ).with_for_update().first()
            protected = (
                credential.password_hash
                if credential is not None else _DUMMY_BCRYPT_HASH
            )
            try:
                valid_password = bcrypt.checkpw(
                    str(password or "").encode("utf-8"),
                    protected.encode("ascii"),
                )
            except (TypeError, ValueError):
                valid_password = False
            if account is None or credential is None or not valid_password:
                return DatabaseLoginResult()

            factor = self._active_factor(db, account.id)
            if factor is not None:
                if not totp_code:
                    return DatabaseLoginResult(
                        username=account.username,
                        account_id=account.id,
                        requires_totp=True,
                    )
                if not self._verify_factor_code(db, factor, totp_code):
                    return DatabaseLoginResult()

            now = self._time()
            raw_token = SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
            db.add(AuthSession(
                id=str(uuid.uuid4()),
                account_id=account.id,
                token_digest=self._repository.session_token_digest(raw_token),
                digest_scheme=HMAC_SHA256_V1,
                auth_epoch=account.auth_epoch,
                expires_at=now + timedelta(seconds=TOKEN_TTL),
                last_seen_at=now,
                interface=str(interface or "web")[:32],
                auth_method="local",
                source_identity_id=(
                    db.query(AuthIdentity.id).filter(
                        AuthIdentity.account_id == account.id,
                        AuthIdentity.provider == LOCAL_PROVIDER,
                        AuthIdentity.issuer == LOCAL_ISSUER,
                        AuthIdentity.state == ACTIVE_IDENTITY,
                    ).scalar()
                ),
            ))
            account.last_login_at = now
            return DatabaseLoginResult(
                token=raw_token,
                username=account.username,
                account_id=account.id,
            )

    def link_external_identity(
        self,
        session_token: str,
        *,
        current_password: str,
        totp_code: str | None = None,
        provider: str,
        issuer: str,
        subject: str,
    ) -> bool:
        """Link one cryptographically verified subject to an existing account.

        The caller must present its current Restia session plus a local password
        and, when enabled, a fresh Restia factor. The session and every factor
        are rechecked and row-locked in the same transaction as the link so a
        stolen cookie or concurrent logout/password rotation cannot install a
        durable login credential.
        No email/username matching is performed; identical-looking strings
        from different issuers remain different identities.
        """

        if self._auth_load_failed:
            return False
        external = _valid_external_identity(provider, issuer, subject)
        if external is None:
            return False
        provider, issuer, subject = external
        try:
            with self._db(write=True) as db:
                if not self._repository._valid_session_token(session_token):
                    return False
                session_rows = db.query(AuthSession).filter(
                    AuthSession.token_digest.in_(
                        self._repository._session_candidates(session_token)
                    )
                ).with_for_update().all()
                current_session = next(
                    (
                        row for row in session_rows
                        if self._repository._session_matches(row, session_token)
                    ),
                    None,
                )
                now = self._time()
                if (
                    current_session is None
                    or current_session.revoked_at is not None
                    or current_session.expires_at <= now
                ):
                    return False
                account = db.query(Account).filter(
                    Account.id == current_session.account_id,
                    Account.status == ACTIVE_ACCOUNT,
                ).with_for_update().first()
                if (
                    account is None
                    or current_session.auth_epoch != account.auth_epoch
                ):
                    return False
                credential = db.query(LocalCredential).filter(
                    LocalCredential.account_id == account.id,
                    LocalCredential.algorithm == "bcrypt",
                ).with_for_update().first()
                protected = (
                    credential.password_hash
                    if credential is not None else _DUMMY_BCRYPT_HASH
                )
                try:
                    valid_password = bcrypt.checkpw(
                        str(current_password or "").encode("utf-8"),
                        protected.encode("ascii"),
                    )
                except (TypeError, ValueError):
                    valid_password = False
                if credential is None or not valid_password:
                    return False
                factor = db.query(MfaFactor).filter(
                    MfaFactor.account_id == account.id,
                    MfaFactor.kind == "totp",
                    MfaFactor.state == "active",
                ).with_for_update().first()
                if factor is not None and (
                    not totp_code
                    or not self._verify_factor_code(db, factor, totp_code)
                ):
                    return False
                existing = db.query(AuthIdentity).filter(
                    AuthIdentity.provider == provider,
                    AuthIdentity.issuer == issuer,
                    AuthIdentity.subject == subject,
                ).with_for_update().first()
                if existing is not None:
                    if (
                        existing.account_id != account.id
                        or existing.state not in (ACTIVE_IDENTITY, "unlinked")
                    ):
                        return False
                    existing.state = ACTIVE_IDENTITY
                    existing.last_verified_at = self._time()
                    return True
                db.add(AuthIdentity(
                    id=str(uuid.uuid4()),
                    account_id=account.id,
                    provider=provider,
                    issuer=issuer,
                    subject=subject,
                    state=ACTIVE_IDENTITY,
                    linked_at=self._time(),
                    last_verified_at=self._time(),
                ))
                db.flush()
                return True
        except IntegrityError:
            # Another replica may have linked the subject concurrently.  Do
            # not guess which account won; the caller can retry and the exact
            # ownership check above will decide safely.
            return False

    def authenticate_external_identity(
        self,
        *,
        provider: str,
        issuer: str,
        subject: str,
        totp_code: str | None = None,
        interface: str = "web",
    ) -> DatabaseLoginResult:
        """Issue a Restia session for one previously linked verified subject."""

        if self._auth_load_failed:
            return DatabaseLoginResult()
        external = _valid_external_identity(provider, issuer, subject)
        if external is None:
            return DatabaseLoginResult()
        provider, issuer, subject = external
        with self._db(write=True) as db:
            identity = db.query(AuthIdentity).filter(
                AuthIdentity.provider == provider,
                AuthIdentity.issuer == issuer,
                AuthIdentity.subject == subject,
                AuthIdentity.state == ACTIVE_IDENTITY,
            ).with_for_update().first()
            if identity is None:
                return DatabaseLoginResult()
            account = db.query(Account).filter(
                Account.id == identity.account_id,
                Account.status == ACTIVE_ACCOUNT,
            ).with_for_update().first()
            if account is None:
                return DatabaseLoginResult()
            factor = db.query(MfaFactor).filter(
                MfaFactor.account_id == account.id,
                MfaFactor.kind == "totp",
                MfaFactor.state == "active",
            ).with_for_update().first()
            if factor is not None:
                if not totp_code:
                    return DatabaseLoginResult(requires_totp=True)
                if not self._verify_factor_code(db, factor, totp_code):
                    return DatabaseLoginResult()
            now = self._time()
            raw_token = SESSION_TOKEN_PREFIX + secrets.token_urlsafe(32)
            db.add(AuthSession(
                id=str(uuid.uuid4()),
                account_id=account.id,
                token_digest=self._repository.session_token_digest(raw_token),
                digest_scheme=HMAC_SHA256_V1,
                auth_epoch=account.auth_epoch,
                expires_at=now + timedelta(seconds=TOKEN_TTL),
                last_seen_at=now,
                interface=str(interface or "web")[:32],
                auth_method=provider,
                source_identity_id=identity.id,
            ))
            identity.last_verified_at = now
            account.last_login_at = now
            return DatabaseLoginResult(
                token=raw_token,
                username=account.username,
                account_id=account.id,
            )

    def create_session(self, username: str, password: str) -> str | None:
        return self.authenticate_session(username, password).token

    def create_session_trusted(self, username: str) -> str | None:
        if self._auth_load_failed:
            return None
        with self._db() as db:
            account = self._local_account(db, username)
            if (
                account is None
                or self._active_factor(db, account.id) is not None
            ):
                return None
            account_id = account.id
        issued = self._repository.create_session(
            account_id,
            ttl_seconds=TOKEN_TTL,
            interface="web",
            auth_method="local",
        )
        return issued.token

    def resolve_session(self, token: str | None) -> DatabasePrincipal | None:
        if self._auth_load_failed:
            return None
        return self._repository.resolve_session(token)

    def validate_token(self, token: str | None) -> bool:
        return self.resolve_session(token) is not None

    def get_username_for_token(self, token: str | None) -> str | None:
        principal = self.resolve_session(token)
        return principal.username if principal is not None else None

    def revoke_token(self, token: str | None) -> None:
        if not self._auth_load_failed:
            self._repository.revoke_session(token)

    def revoke_user_sessions(
        self,
        username: str,
        except_token: str | None = None,
    ) -> int:
        if self._auth_load_failed:
            return 0
        preserved = self.resolve_session(except_token) if except_token else None
        now = self._time()
        with self._db(write=True) as db:
            account = self._local_account_query(
                db, _normal_username(username)
            ).with_for_update().first()
            if account is None:
                return 0
            preserve_id = (
                preserved.credential_id
                if preserved is not None and preserved.account_id == account.id
                else None
            )
            account.auth_epoch = int(account.auth_epoch or 1) + 1
            sessions = db.query(AuthSession).filter(
                AuthSession.account_id == account.id,
                AuthSession.revoked_at.is_(None),
            ).all()
            revoked = 0
            for row in sessions:
                if preserve_id and row.id == preserve_id:
                    row.auth_epoch = account.auth_epoch
                else:
                    row.revoked_at = now
                    revoked += 1
            return revoked

    def resolve_api_token(self, token: str | None) -> DatabasePrincipal | None:
        if self._auth_load_failed:
            return None
        return self._repository.resolve_api_token(token)

    def account_id_for_username(self, username: str | None) -> str | None:
        if self._auth_load_failed:
            return None
        with self._db() as db:
            account = self._local_account(db, username)
            return account.id if account is not None else None

    def issue_api_token(
        self,
        username: str,
        *,
        name: str,
        scopes: list[str] | tuple[str, ...],
    ) -> dict[str, Any] | None:
        """Mint one account-bound API token and reveal its secret once."""

        if self._auth_load_failed:
            return None
        raw_token = "ody_" + secrets.token_urlsafe(32)
        token_id = str(uuid.uuid4())
        with self._db(write=True) as db:
            self._policy_row(db, lock=True)
            account = self._local_account(db, username)
            if (
                account is None
                or not self._has_role(db, account.id, _ADMIN_ROLE)
            ):
                return None
            normalized_scopes = tuple(dict.fromkeys(
                str(scope).strip()
                for scope in scopes
                if str(scope).strip()
            ))
            db.add(ApiToken(
                id=token_id,
                owner=account.username,
                account_id=account.id,
                name=str(name or "")[:100],
                token_hash=self._repository.api_token_digest(raw_token),
                token_prefix=raw_token[:8],
                digest_scheme=HMAC_SHA256_V1,
                scopes=",".join(normalized_scopes) or "chat",
                is_active=True,
            ))
        return {
            "id": token_id,
            "owner": _normal_username(username),
            "token": raw_token,
            "token_prefix": raw_token[:8],
            "scopes": list(normalized_scopes) or ["chat"],
        }

    def touch_api_token(self, token_id: str) -> None:
        if self._auth_load_failed:
            return
        with self._db(write=True) as db:
            db.query(ApiToken).filter(
                ApiToken.id == str(token_id or ""),
                ApiToken.is_active.is_(True),
                ApiToken.revoked_at.is_(None),
            ).update(
                {ApiToken.last_used_at: self._time()},
                synchronize_session=False,
            )

    def status(self, token: str | None) -> dict[str, Any]:
        principal = self.resolve_session(token)
        return {
            "configured": self.is_configured,
            "authenticated": principal is not None,
            "username": principal.username if principal else None,
            "account_id": principal.account_id if principal else None,
            "is_admin": principal.is_admin if principal else False,
            "auth_store_error": self._auth_load_failed,
            **(
                {"privileges": principal.capabilities}
                if principal is not None else {}
            ),
        }


__all__ = [
    "DatabaseAuthManager",
    "DatabaseLoginResult",
    "_sanitize_privileges",
]
