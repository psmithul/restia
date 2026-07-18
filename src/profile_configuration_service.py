"""Account-owned, versioned profile configuration service.

This module is the canonical SQL boundary for profile preferences, mutable app
settings, feature preferences, and user-created integrations.  It never reads
environment variables or accepts deployment-control keys; database URLs,
encryption material, auth secrets, and process-level security configuration
remain deployment authority only.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.exc import IntegrityError

from core.database import Account, utcnow_naive
from src.profile_configuration_models import (
    ProfileConfiguration,
    ProfileConfigurationImportRun,
    ProfileConfigurationMutation,
)
from src.secret_storage import decrypt, is_decryptable, is_encrypted, private_digest
from src.integration_permissions import (
    IntegrationPermissionError,
    normalize_integration_permissions,
)


NAMESPACES = frozenset({"setting", "preference", "feature", "integration"})
SOURCE_KINDS = frozenset({
    "settings_json", "user_prefs_json", "features_json", "integrations_json",
})
WRITE_SOURCES = frozenset({
    "api", "browser", "cli", "telegram", "internal_tool", "domain_service",
    "legacy_import",
})
# Audit interfaces describe the trusted adapter that performed a write, while
# ``WRITE_SOURCES`` describes the caller-supplied provenance of the value.
# Keep these separate: an HTTP request may legitimately persist a value whose
# source is ``browser`` while its server-bound audit interface is ``web``.
CONFIGURATION_INTERFACES = frozenset({
    "web",
    "api",
    "cli",
    "email",
    "telegram",
    "voice",
    "automation",
    "home_link",
    "internal_tool",
    "domain_service",
})
PUBLIC_NAMESPACES = frozenset({"feature"})
MAX_VALUE_BYTES = 128 * 1024
MAX_VALUE_NODES = 20_000
MAX_VALUE_DEPTH = 32
MAX_IMPORT_ENTRIES = 1_000
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Email automation has its own canonical rules table and must not acquire a
# second writer through general settings import.
EMAIL_AUTOMATION_SETTING_KEYS = frozenset({
    "email_auto_summarize",
    "email_auto_reply",
    "email_auto_tag",
    "email_auto_spam",
    "email_auto_calendar",
})

# These names control the deployment/process trust boundary.  They are never
# valid profile configuration, even if a malformed legacy JSON file contains
# them. Provider credentials configured by a user (for example Brave or a
# custom integration token) are private profile values and are encrypted; the
# application encryption key, database credentials, auth secrets, and process
# policy remain environment/deployment only.
DEPLOYMENT_ONLY_KEYS = frozenset({
    "allowed_origins",
    "app_key",
    "auth_enabled",
    "auth_secret",
    "database_mode",
    "database_url",
    "encryption_key",
    "encryption_key_file",
    "localhost_bypass",
    "restia_database_mode",
    "restia_encryption_key",
    "restia_encryption_key_file",
    "session_secret",
    "shared_schema_authority_ready",
    "supabase_jwt_secret",
    "supabase_service_role_key",
})


class ProfileConfigurationError(ValueError):
    pass


class ProfileConfigurationNotFound(ProfileConfigurationError):
    pass


class ProfileConfigurationConflict(ProfileConfigurationError):
    pass


@dataclass(frozen=True, slots=True)
class ConfigurationWriteResult:
    record: ProfileConfiguration
    created: bool
    changed: bool
    idempotent: bool


@dataclass(frozen=True, slots=True)
class ConfigurationImportResult:
    run_id: str
    owner_id: str
    source_kind: str
    imported: int
    skipped: int
    idempotent: bool


def _canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ProfileConfigurationError("Configuration value must be bounded JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_VALUE_BYTES:
        raise ProfileConfigurationError("Configuration value exceeds the size limit")
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > MAX_VALUE_NODES or depth > MAX_VALUE_DEPTH:
            raise ProfileConfigurationError("Configuration value exceeds structural limits")
        if isinstance(item, Mapping):
            if any(not isinstance(key, str) for key in item):
                raise ProfileConfigurationError("Configuration object keys must be strings")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            raise ProfileConfigurationError("Configuration numbers must be finite")
    return encoded


def _json_copy(value: Any) -> Any:
    return json.loads(_canonical_json(value))


def _namespace(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in NAMESPACES:
        raise ProfileConfigurationError(
            "namespace must be setting, preference, feature, or integration"
        )
    return normalized


def _key(value: object) -> str:
    normalized = str(value or "").strip()
    if not _KEY_RE.fullmatch(normalized):
        raise ProfileConfigurationError(
            "Configuration key must use letters, numbers, ., _, :, or -"
        )
    return normalized


def _source(value: object) -> str:
    normalized = str(value or "domain_service").strip().lower()
    if normalized not in WRITE_SOURCES:
        raise ProfileConfigurationError("Unknown configuration write source")
    return normalized


def _interface(db, source: str) -> str:
    context = db.info.get("restia_action_audit_context") or {}
    value = str(context.get("interface") or source or "domain_service").strip().lower()
    return value if value in CONFIGURATION_INTERFACES else "domain_service"


def _assert_profile_key(namespace: str, key: str) -> None:
    normalized = key.strip().lower().replace("-", "_")
    if normalized in DEPLOYMENT_ONLY_KEYS or normalized.startswith((
        "database_", "restia_encryption_", "odysseus_encryption_",
    )):
        raise ProfileConfigurationError(
            "deployment configuration must remain in the environment"
        )
    if namespace == "setting" and normalized in EMAIL_AUTOMATION_SETTING_KEYS:
        raise ProfileConfigurationError(
            "Email automation settings are owned by email automation rules"
        )


def _setting_defaults() -> Mapping[str, Any]:
    # Local import avoids making the SQL service a dependency of legacy
    # settings compatibility during the cutover.
    from src.settings import DEFAULT_SETTINGS

    return DEFAULT_SETTINGS


def _feature_defaults() -> Mapping[str, Any]:
    from src.settings import DEFAULT_FEATURES

    return DEFAULT_FEATURES


def _same_shape(value: Any, default: Any, *, key: str) -> Any:
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ProfileConfigurationError(f"{key} must be true or false")
    elif isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProfileConfigurationError(f"{key} must be an integer")
    elif isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ProfileConfigurationError(f"{key} must be numeric")
    elif isinstance(default, str):
        if not isinstance(value, str):
            raise ProfileConfigurationError(f"{key} must be text")
    elif isinstance(default, list):
        if not isinstance(value, list):
            raise ProfileConfigurationError(f"{key} must be a list")
    elif isinstance(default, dict):
        if not isinstance(value, dict):
            raise ProfileConfigurationError(f"{key} must be an object")
    return _json_copy(value)


def _normalize_url(value: object) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text or len(text) > 4_096:
        raise ProfileConfigurationError("Integration base_url is required")
    parsed = urlsplit(text)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ProfileConfigurationError(
            "Integration base_url must be a credential-free HTTP(S) URL without query or fragment"
        )
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _bounded_text(value: object, *, field: str, limit: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ProfileConfigurationError(f"{field} must be text")
    text = value.strip()
    if required and not text:
        raise ProfileConfigurationError(f"{field} is required")
    if len(text) > limit:
        raise ProfileConfigurationError(f"{field} exceeds the length limit")
    return text


def _normalize_integration(key: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ProfileConfigurationError("Integration value must be an object")
    allowed = {
        "id", "preset", "name", "auth_type", "auth_header", "auth_param",
        "description", "api_key", "base_url", "enabled",
        "permissions",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ProfileConfigurationError("Integration contains unsupported fields")
    integration_id = _key(value.get("id") or key)
    if integration_id != key:
        raise ProfileConfigurationError("Integration id must match its configuration key")
    auth_type = str(value.get("auth_type") or "none").strip().lower()
    if auth_type not in {"none", "bearer", "header", "query", "basic"}:
        raise ProfileConfigurationError("Unsupported integration auth_type")
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ProfileConfigurationError("Integration enabled must be true or false")
    try:
        permissions = normalize_integration_permissions(value.get("permissions"))
    except IntegrationPermissionError as exc:
        raise ProfileConfigurationError(str(exc)) from exc
    result = {
        "id": integration_id,
        "preset": _bounded_text(
            str(value.get("preset") or ""), field="Integration preset", limit=80,
        ),
        "name": _bounded_text(
            value.get("name") if isinstance(value.get("name"), str) else "",
            field="Integration name", limit=240, required=True,
        ),
        "auth_type": auth_type,
        "auth_header": _bounded_text(
            str(value.get("auth_header") or ""),
            field="Integration auth_header", limit=160,
        ),
        "auth_param": _bounded_text(
            str(value.get("auth_param") or ""),
            field="Integration auth_param", limit=160,
        ),
        "description": _bounded_text(
            str(value.get("description") or ""),
            field="Integration description", limit=24_000,
        ),
        "api_key": _bounded_text(
            str(value.get("api_key") or ""),
            field="Integration credential", limit=16_000,
        ),
        "base_url": _normalize_url(value.get("base_url")),
        "enabled": enabled,
        "permissions": permissions,
    }
    return _json_copy(result)


def normalize_configuration_value(namespace: object, key: object, value: Any) -> tuple[str, str, Any, str]:
    normalized_namespace = _namespace(namespace)
    normalized_key = _key(key)
    _assert_profile_key(normalized_namespace, normalized_key)
    if normalized_namespace == "setting":
        defaults = _setting_defaults()
        if normalized_key not in defaults:
            raise ProfileConfigurationError("Unknown mutable application setting")
        normalized_value = _same_shape(
            value, defaults[normalized_key], key=normalized_key,
        )
    elif normalized_namespace == "feature":
        defaults = _feature_defaults()
        if normalized_key not in defaults:
            raise ProfileConfigurationError("Unknown feature preference")
        normalized_value = _same_shape(
            value, defaults[normalized_key], key=normalized_key,
        )
    elif normalized_namespace == "integration":
        normalized_value = _normalize_integration(normalized_key, value)
    else:
        normalized_value = _json_copy(value)
    visibility = "public" if normalized_namespace in PUBLIC_NAMESPACES else "private"
    return normalized_namespace, normalized_key, normalized_value, visibility


def _stored_value(record: ProfileConfiguration) -> Any:
    payload = record.public_value if record.visibility == "public" else record.private_value
    if not isinstance(payload, Mapping) or set(payload) != {"value"}:
        raise ProfileConfigurationError("Stored profile configuration is malformed")
    return _json_copy(payload["value"])


def serialize_configuration(record: ProfileConfiguration, *, include_deleted: bool = False) -> dict[str, Any]:
    if record.state == "deleted" and not include_deleted:
        raise ProfileConfigurationNotFound("Profile configuration not found")
    value = _stored_value(record)
    return {
        "id": record.id,
        "namespace": record.namespace,
        "key": record.key,
        "value": value,
        "private": record.visibility == "private",
        "state": record.state,
        "source": record.source,
        "updated_interface": record.updated_interface,
        "version": int(record.version),
        "created_at": record.created_at.isoformat() + "Z" if record.created_at else None,
        "updated_at": record.updated_at.isoformat() + "Z" if record.updated_at else None,
        "deleted_at": record.deleted_at.isoformat() + "Z" if record.deleted_at else None,
    }


def get_configuration(
    db,
    *,
    owner_id: str,
    namespace: object,
    key: object,
    include_deleted: bool = False,
) -> ProfileConfiguration:
    normalized_namespace = _namespace(namespace)
    normalized_key = _key(key)
    record = db.query(ProfileConfiguration).filter(
        ProfileConfiguration.owner_id == str(owner_id),
        ProfileConfiguration.namespace == normalized_namespace,
        ProfileConfiguration.key == normalized_key,
    ).first()
    if record is None or (record.state == "deleted" and not include_deleted):
        raise ProfileConfigurationNotFound("Profile configuration not found")
    _stored_value(record)
    return record


def list_configurations(
    db,
    *,
    owner_id: str,
    namespace: object | None = None,
    include_deleted: bool = False,
    limit: int = 200,
) -> tuple[list[ProfileConfiguration], bool]:
    bounded = max(1, min(500, int(limit)))
    query = db.query(ProfileConfiguration).filter(
        ProfileConfiguration.owner_id == str(owner_id),
    )
    if namespace is not None:
        query = query.filter(ProfileConfiguration.namespace == _namespace(namespace))
    if not include_deleted:
        query = query.filter(ProfileConfiguration.state == "active")
    rows = query.order_by(
        ProfileConfiguration.namespace.asc(), ProfileConfiguration.key.asc(),
    ).limit(bounded + 1).all()
    for row in rows[:bounded]:
        _stored_value(row)
    return rows[:bounded], len(rows) > bounded


def _idempotency_parts(
    *, owner_id: str, idempotency_key: object | None, request: Mapping[str, Any],
) -> tuple[str, str] | None:
    if idempotency_key is None:
        return None
    raw = str(idempotency_key).strip()
    if not raw or len(raw) > 256:
        raise ProfileConfigurationError("idempotency_key must be 1 to 256 characters")
    canonical = _canonical_json(request)
    return (
        private_digest("profile_configuration_idempotency", f"{owner_id}\0{raw}"),
        private_digest("profile_configuration_request", f"{owner_id}\0{canonical}"),
    )


def _replay(
    db,
    *,
    owner_id: str,
    parts: tuple[str, str] | None,
) -> ProfileConfiguration | None:
    if parts is None:
        return None
    idempotency_digest, request_digest = parts
    mutation = db.query(ProfileConfigurationMutation).filter(
        ProfileConfigurationMutation.owner_id == owner_id,
        ProfileConfigurationMutation.idempotency_digest == idempotency_digest,
    ).first()
    if mutation is None:
        return None
    if mutation.request_digest != request_digest:
        raise ProfileConfigurationConflict(
            "idempotency_key was already used for a different request"
        )
    record = db.query(ProfileConfiguration).filter(
        ProfileConfiguration.id == mutation.configuration_id,
        ProfileConfiguration.owner_id == owner_id,
    ).first()
    if record is None:
        raise ProfileConfigurationError("Stored configuration replay state is malformed")
    if int(record.version) != int(mutation.result_version):
        # The mutation ledger intentionally stores only digests and a version,
        # not a second copy of the private response payload. Returning the
        # record after a later update would make an old idempotency key appear
        # to have produced a value it never wrote, so fail explicitly instead.
        raise ProfileConfigurationConflict(
            "Idempotent configuration result has been superseded"
        )
    return record


def _record_mutation(
    db,
    *,
    owner_id: str,
    record: ProfileConfiguration,
    operation: str,
    parts: tuple[str, str] | None,
) -> None:
    if parts is None:
        return
    idempotency_digest, request_digest = parts
    db.add(ProfileConfigurationMutation(
        id=str(uuid.uuid4()),
        owner_id=owner_id,
        configuration_id=record.id,
        idempotency_digest=idempotency_digest,
        request_digest=request_digest,
        operation=operation,
        result_version=int(record.version),
        created_at=utcnow_naive(),
    ))
    try:
        db.flush()
    except IntegrityError as exc:
        raise ProfileConfigurationConflict("Concurrent idempotent write conflict") from exc


def put_configuration(
    db,
    *,
    account: Account,
    namespace: object,
    key: object,
    value: Any,
    expected_version: int | None = None,
    source: object = "domain_service",
    idempotency_key: object | None = None,
) -> ConfigurationWriteResult:
    normalized_namespace, normalized_key, normalized_value, visibility = (
        normalize_configuration_value(namespace, key, value)
    )
    normalized_source = _source(source)
    request = {
        "operation": "put",
        "namespace": normalized_namespace,
        "key": normalized_key,
        "value": normalized_value,
        "expected_version": expected_version,
    }
    parts = _idempotency_parts(
        owner_id=account.id, idempotency_key=idempotency_key, request=request,
    )
    replay = _replay(db, owner_id=account.id, parts=parts)
    if replay is not None:
        return ConfigurationWriteResult(replay, False, False, True)

    record = db.query(ProfileConfiguration).filter(
        ProfileConfiguration.owner_id == account.id,
        ProfileConfiguration.namespace == normalized_namespace,
        ProfileConfiguration.key == normalized_key,
    ).first()
    payload = {"value": normalized_value}
    now = utcnow_naive()
    if record is None:
        if expected_version not in (None, 0):
            raise ProfileConfigurationConflict("Configuration version does not match")
        record = ProfileConfiguration(
            id=str(uuid.uuid4()),
            owner_id=account.id,
            namespace=normalized_namespace,
            key=normalized_key,
            visibility=visibility,
            public_value=payload if visibility == "public" else None,
            private_value=payload if visibility == "private" else None,
            state="active",
            source=normalized_source,
            updated_interface=_interface(db, normalized_source),
            version=1,
            deleted_at=None,
        )
        db.add(record)
        try:
            db.flush()
        except IntegrityError as exc:
            raise ProfileConfigurationConflict("Concurrent configuration create conflict") from exc
        _record_mutation(
            db, owner_id=account.id, record=record, operation="put", parts=parts,
        )
        return ConfigurationWriteResult(record, True, True, False)

    current_value = _stored_value(record)
    if expected_version is None:
        if record.state == "active" and current_value == normalized_value:
            _record_mutation(
                db, owner_id=account.id, record=record, operation="put", parts=parts,
            )
            return ConfigurationWriteResult(record, False, False, False)
        raise ProfileConfigurationConflict("expected_version is required for updates")
    if int(expected_version) != int(record.version):
        raise ProfileConfigurationConflict("Configuration version does not match")
    updated = db.query(ProfileConfiguration).filter(
        ProfileConfiguration.id == record.id,
        ProfileConfiguration.owner_id == account.id,
        ProfileConfiguration.version == int(expected_version),
    ).update({
        ProfileConfiguration.visibility: visibility,
        ProfileConfiguration.public_value: payload if visibility == "public" else None,
        ProfileConfiguration.private_value: payload if visibility == "private" else None,
        ProfileConfiguration.state: "active",
        ProfileConfiguration.source: normalized_source,
        ProfileConfiguration.updated_interface: _interface(db, normalized_source),
        ProfileConfiguration.version: int(expected_version) + 1,
        ProfileConfiguration.deleted_at: None,
        ProfileConfiguration.updated_at: now,
    }, synchronize_session=False)
    if updated != 1:
        raise ProfileConfigurationConflict("Concurrent configuration update conflict")
    db.flush()
    db.expire(record)
    record = db.query(ProfileConfiguration).filter(
        ProfileConfiguration.id == record.id,
        ProfileConfiguration.owner_id == account.id,
    ).first()
    if record is None:
        raise ProfileConfigurationError("Updated configuration disappeared")
    _record_mutation(
        db, owner_id=account.id, record=record, operation="put", parts=parts,
    )
    return ConfigurationWriteResult(record, False, True, False)


def delete_configuration(
    db,
    *,
    account: Account,
    namespace: object,
    key: object,
    expected_version: int,
    source: object = "domain_service",
    idempotency_key: object | None = None,
) -> ConfigurationWriteResult:
    normalized_namespace = _namespace(namespace)
    normalized_key = _key(key)
    _assert_profile_key(normalized_namespace, normalized_key)
    normalized_source = _source(source)
    request = {
        "operation": "delete",
        "namespace": normalized_namespace,
        "key": normalized_key,
        "expected_version": expected_version,
    }
    parts = _idempotency_parts(
        owner_id=account.id, idempotency_key=idempotency_key, request=request,
    )
    replay = _replay(db, owner_id=account.id, parts=parts)
    if replay is not None:
        return ConfigurationWriteResult(replay, False, False, True)
    record = get_configuration(
        db,
        owner_id=account.id,
        namespace=normalized_namespace,
        key=normalized_key,
        include_deleted=True,
    )
    if record.state == "deleted":
        raise ProfileConfigurationNotFound("Profile configuration not found")
    if int(record.version) != int(expected_version):
        raise ProfileConfigurationConflict("Configuration version does not match")
    now = utcnow_naive()
    updated = db.query(ProfileConfiguration).filter(
        ProfileConfiguration.id == record.id,
        ProfileConfiguration.owner_id == account.id,
        ProfileConfiguration.version == int(expected_version),
        ProfileConfiguration.state == "active",
    ).update({
        ProfileConfiguration.state: "deleted",
        ProfileConfiguration.source: normalized_source,
        ProfileConfiguration.updated_interface: _interface(db, normalized_source),
        ProfileConfiguration.version: int(expected_version) + 1,
        ProfileConfiguration.deleted_at: now,
        ProfileConfiguration.updated_at: now,
    }, synchronize_session=False)
    if updated != 1:
        raise ProfileConfigurationConflict("Concurrent configuration delete conflict")
    db.flush()
    db.expire(record)
    record = db.query(ProfileConfiguration).filter(
        ProfileConfiguration.id == record.id,
        ProfileConfiguration.owner_id == account.id,
    ).first()
    if record is None:
        raise ProfileConfigurationError("Deleted configuration disappeared")
    _record_mutation(
        db, owner_id=account.id, record=record, operation="delete", parts=parts,
    )
    return ConfigurationWriteResult(record, False, True, False)


def _open_legacy_secret(value: Any) -> Any:
    if not isinstance(value, str) or not value.startswith("enc:"):
        return value
    if not is_encrypted(value) or not is_decryptable(value):
        raise ProfileConfigurationError("Legacy configuration credential cannot be decrypted")
    return decrypt(value)


def _legacy_entries(
    *,
    account: Account,
    source_kind: str,
    payload: Any,
) -> tuple[list[tuple[str, str, Any]], int]:
    entries: list[tuple[str, str, Any]] = []
    skipped = 0
    if source_kind == "settings_json":
        if not isinstance(payload, Mapping):
            raise ProfileConfigurationError("Legacy settings must be an object")
        from src.settings import _is_secret_setting

        defaults = _setting_defaults()
        miniflux_url = payload.get("miniflux_url")
        miniflux_key = payload.get("miniflux_api_key")
        has_legacy_miniflux = (
            isinstance(miniflux_url, str)
            and bool(miniflux_url.strip())
            and isinstance(miniflux_key, str)
            and bool(miniflux_key.strip())
        )
        for raw_key, raw_value in payload.items():
            key = str(raw_key or "").strip()
            if has_legacy_miniflux and key in {"miniflux_url", "miniflux_api_key"}:
                # These retired fields are adopted below as one canonical
                # integration. The source file remains unchanged.
                continue
            if (
                key not in defaults
                or key.lower() in EMAIL_AUTOMATION_SETTING_KEYS
                or key.lower() in DEPLOYMENT_ONLY_KEYS
            ):
                skipped += 1
                continue
            value = (
                _open_legacy_secret(raw_value)
                if _is_secret_setting(key, raw_value) else raw_value
            )
            entries.append(("setting", key, value))
        if has_legacy_miniflux:
            entries.append(("integration", "miniflux", {
                "id": "miniflux",
                "preset": "miniflux",
                "name": "Miniflux",
                "auth_type": "header",
                "auth_header": "X-Auth-Token",
                "auth_param": "",
                "description": "",
                "api_key": _open_legacy_secret(miniflux_key),
                "base_url": miniflux_url,
                "enabled": True,
            }))
    elif source_kind == "features_json":
        if not isinstance(payload, Mapping):
            raise ProfileConfigurationError("Legacy features must be an object")
        defaults = _feature_defaults()
        for raw_key, raw_value in payload.items():
            key = str(raw_key or "").strip()
            if key not in defaults:
                skipped += 1
                continue
            entries.append(("feature", key, raw_value))
    elif source_kind == "user_prefs_json":
        if not isinstance(payload, Mapping):
            raise ProfileConfigurationError("Legacy preferences must be an object")
        selected: Any = payload
        if "_users" in payload:
            users = payload.get("_users")
            if not isinstance(users, Mapping):
                raise ProfileConfigurationError("Legacy _users preferences must be an object")
            matched = {
                str(name or "").strip().lower(): value
                for name, value in users.items()
            }.get(str(account.username or "").strip().lower(), {})
            selected = matched
        if not isinstance(selected, Mapping):
            raise ProfileConfigurationError("Legacy profile preferences must be an object")
        for raw_key, raw_value in selected.items():
            key = str(raw_key or "").strip()
            try:
                _assert_profile_key("preference", _key(key))
            except ProfileConfigurationError:
                skipped += 1
                continue
            entries.append(("preference", key, raw_value))
    elif source_kind == "integrations_json":
        if not isinstance(payload, list):
            raise ProfileConfigurationError("Legacy integrations must be a list")
        seen: set[str] = set()
        for raw in payload:
            if not isinstance(raw, Mapping):
                raise ProfileConfigurationError("Legacy integration rows must be objects")
            integration = dict(raw)
            key = _key(integration.get("id"))
            if key in seen:
                raise ProfileConfigurationError("Legacy integrations contain a duplicate id")
            seen.add(key)
            if "api_key" in integration:
                integration["api_key"] = _open_legacy_secret(integration.get("api_key"))
            entries.append(("integration", key, integration))
    else:
        raise ProfileConfigurationError("Unknown legacy configuration source")
    if len(entries) > MAX_IMPORT_ENTRIES:
        raise ProfileConfigurationError("Legacy configuration exceeds the entry limit")
    return entries, skipped


def import_legacy_configuration(
    db,
    *,
    account: Account,
    source_kind: object,
    source_sha256: object,
    payload: Any,
) -> ConfigurationImportResult:
    normalized_kind = str(source_kind or "").strip().lower()
    digest = str(source_sha256 or "").strip().lower()
    if normalized_kind not in SOURCE_KINDS:
        raise ProfileConfigurationError("Unknown legacy configuration source")
    if not _SHA256_RE.fullmatch(digest):
        raise ProfileConfigurationError("source_sha256 must be a SHA-256 digest")
    existing_run = db.query(ProfileConfigurationImportRun).filter(
        ProfileConfigurationImportRun.owner_id == account.id,
        ProfileConfigurationImportRun.source_kind == normalized_kind,
        ProfileConfigurationImportRun.source_sha256 == digest,
    ).first()
    if existing_run is not None and existing_run.state == "completed":
        return ConfigurationImportResult(
            run_id=existing_run.id,
            owner_id=existing_run.owner_id,
            source_kind=existing_run.source_kind,
            imported=int(existing_run.imported_count),
            skipped=int(existing_run.skipped_count),
            idempotent=True,
        )
    entries, rejected = _legacy_entries(
        account=account, source_kind=normalized_kind, payload=payload,
    )
    run = existing_run or ProfileConfigurationImportRun(
        id=str(uuid.uuid4()),
        owner_id=account.id,
        source_kind=normalized_kind,
        source_sha256=digest,
        state="pending",
        imported_count=0,
        skipped_count=0,
        details={},
        version=1,
    )
    if existing_run is None:
        db.add(run)
        try:
            db.flush()
        except IntegrityError as exc:
            raise ProfileConfigurationConflict("Concurrent legacy import conflict") from exc
    imported = 0
    skipped = rejected
    for namespace, key, value in entries:
        normalized_namespace, normalized_key, normalized_value, visibility = (
            normalize_configuration_value(namespace, key, value)
        )
        present = db.query(ProfileConfiguration.id).filter(
            ProfileConfiguration.owner_id == account.id,
            ProfileConfiguration.namespace == normalized_namespace,
            ProfileConfiguration.key == normalized_key,
        ).first()
        if present is not None:
            skipped += 1
            continue
        if normalized_namespace == "integration":
            preset = str(normalized_value.get("preset") or "").strip().lower()
            if preset:
                # The retired settings.json Miniflux fields used a generated
                # integration id. Avoid creating a semantic duplicate when an
                # integrations.json row or an earlier canonical row already
                # represents the same preset.
                candidates = db.query(ProfileConfiguration).filter(
                    ProfileConfiguration.owner_id == account.id,
                    ProfileConfiguration.namespace == "integration",
                ).limit(501).all()
                if len(candidates) > 500:
                    raise ProfileConfigurationError(
                        "Canonical integration count exceeds the import limit"
                    )
                if any(
                    str((_stored_value(candidate) or {}).get("preset") or "")
                    .strip().lower() == preset
                    for candidate in candidates
                ):
                    skipped += 1
                    continue
        wrapped = {"value": normalized_value}
        db.add(ProfileConfiguration(
            id=str(uuid.uuid4()),
            owner_id=account.id,
            namespace=normalized_namespace,
            key=normalized_key,
            visibility=visibility,
            public_value=wrapped if visibility == "public" else None,
            private_value=wrapped if visibility == "private" else None,
            state="active",
            source="legacy_import",
            updated_interface="domain_service",
            version=1,
            deleted_at=None,
        ))
        try:
            db.flush()
        except IntegrityError as exc:
            raise ProfileConfigurationConflict("Concurrent legacy import conflict") from exc
        imported += 1
    run.state = "completed"
    run.imported_count = imported
    run.skipped_count = skipped
    run.details = {
        "bounded": True,
        "non_destructive": True,
        "source_preserved": True,
        "entries_considered": len(entries),
        "excluded_or_existing": skipped,
    }
    run.completed_at = utcnow_naive()
    run.version = int(run.version or 1) + (1 if existing_run is not None else 0)
    db.flush()
    return ConfigurationImportResult(
        run_id=run.id,
        owner_id=account.id,
        source_kind=normalized_kind,
        imported=imported,
        skipped=skipped,
        idempotent=False,
    )


def payload_sha256(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


__all__ = [
    "ConfigurationImportResult",
    "ConfigurationWriteResult",
    "DEPLOYMENT_ONLY_KEYS",
    "EMAIL_AUTOMATION_SETTING_KEYS",
    "MAX_IMPORT_ENTRIES",
    "ProfileConfigurationConflict",
    "ProfileConfigurationError",
    "ProfileConfigurationNotFound",
    "delete_configuration",
    "get_configuration",
    "import_legacy_configuration",
    "list_configurations",
    "normalize_configuration_value",
    "payload_sha256",
    "put_configuration",
    "serialize_configuration",
]
