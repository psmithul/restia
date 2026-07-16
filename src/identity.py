"""Stable V3 account resolution over Restia's existing authentication.

This module deliberately does not replace ``auth.json`` or ``sessions.json``.
The current AuthManager remains the credential/session authority; this is the
compatibility seam that maps its authenticated username (including the real
owner behind an API token) to a durable SQL ``Account.id`` for new domains.
Credential migration and remote identity-provider validation are separate
release gates.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from typing import Iterator

from fastapi import HTTPException, Request
from sqlalchemy.exc import IntegrityError

from core.database import Account, AuthIdentity
from src.audit_context import bind_request_audit_context
from src.auth_helpers import effective_user, require_user, resolved_request_owner


LOCAL_IDENTITY_PROVIDER = "local"
_IDENTITY_CHANGED = "Profile identity changed; refresh and sign in again"


def normalize_identity(value: object) -> str:
    return str(value or "").strip().lower()


def ensure_account(
    db,
    username: str,
    *,
    provider: str = LOCAL_IDENTITY_PROVIDER,
    subject: str | None = None,
) -> Account:
    """Return the stable account for one already-authenticated principal.

    The first resolution lazily creates both rows in the caller's transaction.
    Subsequent cookie and API-token requests converge on the provider/subject
    identity and therefore the same ``Account.id``.
    """

    normalized_username = normalize_identity(username)
    normalized_provider = normalize_identity(provider)
    normalized_subject = normalize_identity(subject or normalized_username)
    if not normalized_username or not normalized_provider or not normalized_subject:
        raise ValueError("A concrete authenticated identity is required")

    identity = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.provider == normalized_provider,
            AuthIdentity.subject == normalized_subject,
        )
        .first()
    )
    if identity is not None:
        account = db.query(Account).filter(Account.id == identity.account_id).first()
        if account is None:
            raise RuntimeError("Authenticated identity points to a missing account")
        return account

    # A verified external subject may look exactly like a local username or
    # email address without belonging to that local profile.  External login
    # flows must perform an explicit, authenticated link before resolving here;
    # never infer account ownership from a matching string.
    if normalized_provider != LOCAL_IDENTITY_PROVIDER:
        raise ValueError("External identities require explicit account linking")

    account = (
        db.query(Account)
        .filter(Account.username == normalized_username)
        .first()
    )
    try:
        # The savepoint turns a concurrent first-touch unique conflict into a
        # recoverable lookup without rolling back the caller's whole request.
        with db.begin_nested():
            if account is None:
                account = Account(id=str(uuid.uuid4()), username=normalized_username)
                db.add(account)
                db.flush()
            db.add(AuthIdentity(
                id=str(uuid.uuid4()),
                account_id=account.id,
                provider=normalized_provider,
                subject=normalized_subject,
            ))
            db.flush()
    except IntegrityError:
        identity = (
            db.query(AuthIdentity)
            .filter(
                AuthIdentity.provider == normalized_provider,
                AuthIdentity.subject == normalized_subject,
            )
            .first()
        )
        if identity is None:
            raise
        account = db.query(Account).filter(Account.id == identity.account_id).first()
        if account is None:
            raise RuntimeError("Authenticated identity points to a missing account")
    return account


def find_account(db, username: str) -> Account | None:
    """Resolve an existing local account without changing persistent state."""

    subject = normalize_identity(username)
    if not subject:
        raise ValueError("A concrete authenticated identity is required")
    identity = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.provider == LOCAL_IDENTITY_PROVIDER,
            AuthIdentity.subject == subject,
        )
        .first()
    )
    if identity is not None:
        account = db.query(Account).filter(Account.id == identity.account_id).first()
        if account is None:
            raise RuntimeError("Authenticated identity points to a missing account")
        return account
    # Account.username is itself a local alias. Older/manual V3 data can have
    # that row before the AuthIdentity backfill; reads may use it, but must not
    # repair it implicitly.
    return db.query(Account).filter(Account.username == subject).first()


def rename_local_identity(db, old_username: str, new_username: str) -> Account | None:
    """Move one local login subject while preserving its durable account id.

    This helper belongs inside the existing profile owner-migration
    transaction.  It intentionally updates only the ``local`` provider:
    externally verified provider subjects are immutable unless their own
    explicit linking flow changes them.
    """

    old_subject = normalize_identity(old_username)
    new_subject = normalize_identity(new_username)
    if not old_subject or not new_subject:
        raise ValueError("Both local usernames are required")
    if old_subject == new_subject:
        identity = (
            db.query(AuthIdentity)
            .filter(
                AuthIdentity.provider == LOCAL_IDENTITY_PROVIDER,
                AuthIdentity.subject == old_subject,
            )
            .first()
        )
        return (
            db.query(Account).filter(Account.id == identity.account_id).first()
            if identity is not None else
            db.query(Account).filter(Account.username == old_subject).first()
        )

    old_identity = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.provider == LOCAL_IDENTITY_PROVIDER,
            AuthIdentity.subject == old_subject,
        )
        .first()
    )
    account = None
    if old_identity is not None:
        account = db.query(Account).filter(Account.id == old_identity.account_id).first()
        if account is None:
            raise RuntimeError("Local identity points to a missing account")
    if account is None:
        account = db.query(Account).filter(Account.username == old_subject).first()
    # Accounts are created lazily.  A profile that never used a V3 owner-scoped
    # domain has no identity row to migrate and will be created under its new
    # authenticated username on first use.
    if account is None:
        return None

    destination_account = (
        db.query(Account).filter(Account.username == new_subject).first()
    )
    if destination_account is not None and destination_account.id != account.id:
        raise ValueError("Destination username is already linked to another account")
    destination_identity = (
        db.query(AuthIdentity)
        .filter(
            AuthIdentity.provider == LOCAL_IDENTITY_PROVIDER,
            AuthIdentity.subject == new_subject,
        )
        .first()
    )
    if (
        destination_identity is not None
        and destination_identity.account_id != account.id
    ):
        raise ValueError("Destination local identity is already linked")

    account.username = new_subject
    if old_identity is not None:
        if destination_identity is None:
            old_identity.subject = new_subject
        elif destination_identity.id != old_identity.id:
            db.delete(old_identity)
    elif destination_identity is None:
        db.add(AuthIdentity(
            id=str(uuid.uuid4()),
            account_id=account.id,
            provider=LOCAL_IDENTITY_PROVIDER,
            subject=new_subject,
        ))
    db.flush()
    return account


def _require_api_scopes(request: Request, required_scopes: Iterable[str]) -> None:
    required = {str(scope).strip() for scope in required_scopes if str(scope).strip()}
    if not required or not bool(getattr(request.state, "api_token", False)):
        return
    raw = getattr(request.state, "api_token_scopes", None) or []
    if isinstance(raw, str):
        raw = raw.split(",")
    granted = {str(scope).strip() for scope in raw if str(scope).strip()}
    missing = sorted(required - granted)
    if missing:
        raise HTTPException(403, f"API token requires scope: {', '.join(missing)}")


def resolved_request_username(
    request: Request,
    *,
    required_scopes: Iterable[str] = (),
) -> str:
    """Resolve the same legacy owner for cookies and attributed API tokens."""

    if bool(getattr(request.state, "api_token", False)):
        _require_api_scopes(request, required_scopes)
        owner = normalize_identity(effective_user(request))
        if not owner or owner == "api":
            raise HTTPException(401, "API token owner is unavailable")
        return owner

    admitted = require_user(request)
    return normalize_identity(
        resolved_request_owner(request, admitted_user=admitted)
    )


def resolve_request_account(
    db,
    request: Request,
    *,
    required_scopes: Iterable[str] = (),
) -> Account | None:
    """Read an admitted request's existing account without lazy mutation.

    Request mutations must use :func:`request_account_transaction`, which
    serializes first-touch account creation with profile rename/deletion.
    """

    username = resolved_request_username(request, required_scopes=required_scopes)
    return find_account(db, username)


def _request_auth_manager(request: Request):
    app = getattr(request, "app", None)
    state = getattr(app, "state", None)
    return getattr(state, "auth_manager", None)


def _validate_live_identity(manager, username: str, *, auth_disabled: bool) -> None:
    active = {
        normalize_identity(value)
        for value in (getattr(manager, "_identity_migrations", set()) or set())
        if normalize_identity(value)
    }
    if username in active:
        raise HTTPException(409, _IDENTITY_CHANGED)

    retired = {
        normalize_identity(value)
        for value in (getattr(manager, "retired_usernames", set()) or set())
        if normalize_identity(value)
    }
    if username in retired:
        raise HTTPException(409, _IDENTITY_CHANGED)

    users = getattr(manager, "users", None)
    if not isinstance(users, dict):
        raise HTTPException(503, "Profile identity authority is unavailable")
    known = {
        normalize_identity(value)
        for value in users
        if normalize_identity(value)
    }
    # AUTH_ENABLED=false intentionally supports an installation with no
    # profile rows by resolving DEFAULT_LOCAL_OWNER. In every authenticated
    # deployment, a principal missing from the live auth store is a stale
    # cookie/token owner and must never be rebound to a fresh Account UUID.
    if not auth_disabled and bool(getattr(manager, "is_configured", False)):
        if username not in known:
            raise HTTPException(409, _IDENTITY_CHANGED)
    elif known and username not in known and not auth_disabled:
        raise HTTPException(409, _IDENTITY_CHANGED)


@contextmanager
def request_account_transaction(
    db,
    request: Request,
    *,
    required_scopes: Iterable[str] = (),
    write: bool,
) -> Iterator[Account | None]:
    """Run one V3 owner-scoped operation behind the auth identity barrier.

    Lock ordering matches existing Projects writes: AuthManager config lock,
    then the SQL transaction. Profile rename/deletion reserves identities
    under that same lock before touching SQL, so an old cookie or cached API
    owner either finishes its transaction first or fails closed. This context
    owns commit/rollback so the auth lock is never released while SQL work is
    still pending. Reads roll back their snapshot and never lazily create an
    Account/AuthIdentity pair.
    """

    username = resolved_request_username(request, required_scopes=required_scopes)
    auth_disabled = os.getenv("AUTH_ENABLED", "true").lower() == "false"
    manager = _request_auth_manager(request)
    if manager is None:
        if not auth_disabled:
            raise HTTPException(503, "Profile identity authority is unavailable")
        lock = None
    else:
        lock = getattr(manager, "_config_lock", None)
        if lock is None and not auth_disabled:
            raise HTTPException(503, "Profile identity authority is unavailable")

    if lock is not None:
        lock.acquire()
    try:
        try:
            if manager is not None:
                _validate_live_identity(manager, username, auth_disabled=auth_disabled)
            account = ensure_account(db, username) if write else find_account(db, username)
            if account is not None:
                bind_request_audit_context(db, request, account)
            yield account
            if write:
                db.commit()
            else:
                # SQLAlchemy opens a transaction for SELECTs. End that read
                # snapshot before the auth lock is released.
                db.rollback()
        except BaseException:
            db.rollback()
            raise
    finally:
        if lock is not None:
            lock.release()
