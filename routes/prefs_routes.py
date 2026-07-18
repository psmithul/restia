"""Profile preferences API backed by canonical Account.id-owned SQL rows."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from core.database import Account, SessionLocal
from src.auth_helpers import resolved_runtime_owner
from src.identity import ensure_account, find_account, request_account_transaction
from src.profile_configuration_service import (
    ProfileConfigurationError,
    delete_configuration,
    list_configurations,
    put_configuration,
    serialize_configuration,
)


def _account(db, user: Optional[str], *, write: bool) -> Account | None:
    username = resolved_runtime_owner(user)
    return ensure_account(db, username) if write else find_account(db, username)


def _load_for_account(db, account: Account | None) -> dict[str, Any]:
    if account is None:
        return {}
    rows, truncated = list_configurations(
        db,
        owner_id=account.id,
        namespace="preference",
        limit=500,
    )
    if truncated:
        raise RuntimeError("Profile preference count exceeds the compatibility limit")
    return {row.key: serialize_configuration(row)["value"] for row in rows}


def _replace_for_account(db, account: Account, prefs: dict[str, Any]) -> None:
    if not isinstance(prefs, dict):
        raise ProfileConfigurationError("preferences must be an object")
    rows, truncated = list_configurations(
        db,
        owner_id=account.id,
        namespace="preference",
        limit=500,
    )
    if truncated or len(prefs) > 500:
        raise ProfileConfigurationError("preferences exceed the entry limit")
    existing = {row.key: row for row in rows}
    for key, value in prefs.items():
        current = existing.pop(str(key), None)
        put_configuration(
            db,
            account=account,
            namespace="preference",
            key=key,
            value=value,
            expected_version=int(current.version) if current is not None else None,
            source="domain_service",
        )
    for key, current in existing.items():
        delete_configuration(
            db,
            account=account,
            namespace="preference",
            key=key,
            expected_version=int(current.version),
            source="domain_service",
        )


def _load() -> dict[str, Any]:
    """Compatibility snapshot for admin maintenance; never reads JSON files."""

    db = SessionLocal()
    try:
        accounts = db.query(Account).filter(Account.status == "active").order_by(
            Account.username.asc(),
        ).limit(1_001).all()
        if len(accounts) > 1_000:
            raise RuntimeError("Profile count exceeds the preference snapshot limit")
        users = {
            account.username: _load_for_account(db, account)
            for account in accounts
        }
        db.rollback()
        return {"_users": users}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _save(prefs: dict[str, Any]) -> None:
    """Compatibility bulk writer used by bounded admin maintenance paths."""

    if not isinstance(prefs, dict):
        raise ProfileConfigurationError("preferences must be an object")
    users = prefs.get("_users")
    db = SessionLocal()
    try:
        if isinstance(users, dict):
            if len(users) > 1_000:
                raise ProfileConfigurationError("profile preference snapshot is too large")
            for username, values in users.items():
                if not isinstance(values, dict):
                    raise ProfileConfigurationError("profile preferences must be objects")
                account = _account(db, str(username), write=True)
                _replace_for_account(db, account, values)
        else:
            account = _account(db, None, write=True)
            _replace_for_account(db, account, prefs)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _load_for_user(user: Optional[str] = None) -> dict[str, Any]:
    db = SessionLocal()
    try:
        result = _load_for_account(db, _account(db, user, write=False))
        db.rollback()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _save_for_user(user: Optional[str], prefs: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        account = _account(db, user, write=True)
        _replace_for_account(db, account, prefs)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def setup_prefs_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(prefix="/api/prefs", tags=["preferences"])

    @router.get("")
    def get_all_prefs(request: Request):
        db = session_factory()
        try:
            with request_account_transaction(db, request, write=False) as account:
                return _load_for_account(db, account)
        finally:
            db.close()

    @router.get("/{key}")
    def get_pref(request: Request, key: str):
        db = session_factory()
        try:
            with request_account_transaction(db, request, write=False) as account:
                prefs = _load_for_account(db, account)
                return {"key": key, "value": prefs.get(key)}
        finally:
            db.close()

    @router.put("/{key}")
    def set_pref(request: Request, key: str, body: dict):
        db = session_factory()
        try:
            with request_account_transaction(db, request, write=True) as account:
                current = {
                    row.key: row
                    for row in list_configurations(
                        db,
                        owner_id=account.id,
                        namespace="preference",
                        limit=500,
                    )[0]
                }.get(key)
                result = put_configuration(
                    db,
                    account=account,
                    namespace="preference",
                    key=key,
                    value=body.get("value"),
                    expected_version=(
                        int(body["version"])
                        if body.get("version") is not None else
                        (int(current.version) if current is not None else None)
                    ),
                    source="browser",
                    idempotency_key=body.get("idempotency_key"),
                )
                serialized = serialize_configuration(result.record)
                return {
                    "key": key,
                    "value": serialized["value"],
                    "version": serialized["version"],
                }
        except ProfileConfigurationError as exc:
            db.rollback()
            raise HTTPException(400, str(exc)) from exc
        finally:
            db.close()

    return router


__all__ = [
    "_load",
    "_load_for_user",
    "_save",
    "_save_for_user",
    "setup_prefs_routes",
]
