"""Cross-interface adapters for canonical profile configuration.

HTTP routes normally use ``request_account_transaction`` directly.  Trusted
CLI, Telegram, and internal-tool boundaries use these adapters so every
interface resolves the same immutable ``Account.id`` and writes through the
same optimistic/idempotent service instead of reopening JSON files.
"""

from __future__ import annotations

from typing import Any

from core.database import Account, SessionLocal
from src.audit_context import bind_service_audit_context
from src.identity import find_account
from src.profile_configuration_service import (
    ConfigurationWriteResult,
    ProfileConfigurationNotFound,
    delete_configuration,
    get_configuration,
    list_configurations,
    put_configuration,
    serialize_configuration,
)


def configuration_values(
    db,
    *,
    owner_id: str,
    namespace: str,
    defaults: dict[str, Any] | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    rows, truncated = list_configurations(
        db, owner_id=owner_id, namespace=namespace, limit=limit,
    )
    if truncated:
        raise RuntimeError("Canonical profile configuration exceeds adapter limit")
    result = dict(defaults or {})
    for row in rows:
        result[row.key] = serialize_configuration(row)["value"]
    return result


def profile_settings(db, *, owner_id: str) -> dict[str, Any]:
    from src.settings import DEFAULT_SETTINGS

    return configuration_values(
        db, owner_id=owner_id, namespace="setting", defaults=DEFAULT_SETTINGS,
    )


def profile_features(db, *, owner_id: str) -> dict[str, Any]:
    from src.settings import DEFAULT_FEATURES

    return configuration_values(
        db, owner_id=owner_id, namespace="feature", defaults=DEFAULT_FEATURES,
    )


def profile_preferences(db, *, owner_id: str) -> dict[str, Any]:
    return configuration_values(db, owner_id=owner_id, namespace="preference")


def profile_integrations(db, *, owner_id: str) -> list[dict[str, Any]]:
    values = configuration_values(db, owner_id=owner_id, namespace="integration")
    return [dict(values[key]) for key in sorted(values)]


def get_profile_configuration_for_username(
    owner: str,
    *,
    namespace: str,
    key: str,
    session_factory=SessionLocal,
) -> dict[str, Any] | None:
    db = session_factory()
    try:
        account = find_account(db, owner)
        if account is None:
            db.rollback()
            return None
        try:
            record = get_configuration(
                db, owner_id=account.id, namespace=namespace, key=key,
            )
        except ProfileConfigurationNotFound:
            db.rollback()
            return None
        result = serialize_configuration(record)
        db.rollback()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def put_profile_configuration_for_username(
    owner: str,
    *,
    namespace: str,
    key: str,
    value: Any,
    expected_version: int | None,
    interface: str,
    idempotency_key: str | None = None,
    session_factory=SessionLocal,
) -> dict[str, Any]:
    db = session_factory()
    try:
        account = find_account(db, owner)
        if account is None:
            raise ProfileConfigurationNotFound("Profile account not found")
        bind_service_audit_context(
            db,
            account_id=account.id,
            interface=interface,
            actor_type="account",
            credential_type="internal",
        )
        result = put_configuration(
            db,
            account=account,
            namespace=namespace,
            key=key,
            value=value,
            expected_version=expected_version,
            source=interface,
            idempotency_key=idempotency_key,
        )
        db.commit()
        return {
            "configuration": serialize_configuration(result.record),
            "created": result.created,
            "changed": result.changed,
            "idempotent": result.idempotent,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def delete_profile_configuration_for_username(
    owner: str,
    *,
    namespace: str,
    key: str,
    expected_version: int,
    interface: str,
    idempotency_key: str | None = None,
    session_factory=SessionLocal,
) -> dict[str, Any]:
    db = session_factory()
    try:
        account = find_account(db, owner)
        if account is None:
            raise ProfileConfigurationNotFound("Profile account not found")
        bind_service_audit_context(
            db,
            account_id=account.id,
            interface=interface,
            actor_type="account",
            credential_type="internal",
        )
        result = delete_configuration(
            db,
            account=account,
            namespace=namespace,
            key=key,
            expected_version=expected_version,
            source=interface,
            idempotency_key=idempotency_key,
        )
        db.commit()
        return {
            "configuration": serialize_configuration(
                result.record, include_deleted=True,
            ),
            "changed": result.changed,
            "idempotent": result.idempotent,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


__all__ = [
    "configuration_values",
    "delete_profile_configuration_for_username",
    "get_profile_configuration_for_username",
    "profile_features",
    "profile_integrations",
    "profile_preferences",
    "profile_settings",
    "put_profile_configuration_for_username",
]
