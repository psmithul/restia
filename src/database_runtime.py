"""Database deployment-mode validation and explicit process initialization.

Restia currently has one production-safe schema path: the legacy SQLite
bootstrap in :mod:`core.database`.  V3 introduces an explicit boundary so a
future shared PostgreSQL deployment cannot accidentally run that SQLite-only
bootstrap or start with anonymous access and per-node encryption keys.

``local-single`` (the default)
    One private Restia process using SQLite and the existing idempotent schema
    bootstrap.

``shared``
    A future multi-interface/multi-device deployment using PostgreSQL.  Its
    security prerequisites are validated now, but startup remains blocked
    until a deterministic Alembic baseline replaces the legacy migrations.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from cryptography.fernet import Fernet


LOCAL_SINGLE_MODE = "local-single"
SHARED_MODE = "shared"
SUPPORTED_DATABASE_MODES = frozenset({LOCAL_SINGLE_MODE, SHARED_MODE})

# This must only become True in the same change that supplies a deterministic
# PostgreSQL baseline and removes the SQLite-specific startup migrations.
SHARED_SCHEMA_AUTHORITY_READY = False
SCHEMA_AUTHORITY = "legacy-sqlite-bootstrap"

_initialization_lock = threading.Lock()
_initialized_binding: tuple[int, str] | None = None


class DatabaseConfigurationError(RuntimeError):
    """Raised when the selected database deployment mode is unsafe."""


@dataclass(frozen=True)
class DatabaseRuntimeConfig:
    mode: str
    database_url: str
    dialect: str
    auth_enabled: bool
    localhost_bypass: bool
    encryption_key_source: str | None
    schema_authority: str = SCHEMA_AUTHORITY

    @property
    def shared(self) -> bool:
        return self.mode == SHARED_MODE


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def _env_value(environ: Mapping[str, str], primary: str, legacy: str) -> str:
    return str(environ.get(primary) or environ.get(legacy) or "").strip()


def _parse_bool(
    environ: Mapping[str, str],
    name: str,
    *,
    default: bool,
) -> bool:
    raw = str(environ.get(name, "")).strip().lower()
    if not raw:
        return default
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise DatabaseConfigurationError(
        f"{name} must be an explicit boolean (true/false), got an invalid value"
    )


def _database_dialect(database_url: str) -> str:
    scheme = database_url.split(":", 1)[0].strip().lower()
    if scheme == "postgres" or scheme.startswith("postgresql"):
        return "postgresql"
    if scheme.startswith("sqlite"):
        return "sqlite"
    return scheme or "unknown"


def _validate_fernet_key(raw_key: bytes, *, source: str) -> str:
    try:
        Fernet(raw_key.strip())
    except Exception as exc:
        raise DatabaseConfigurationError(
            f"The configured {source} is not a valid Fernet encryption key"
        ) from exc
    return source


def _shared_encryption_key_source(environ: Mapping[str, str]) -> str | None:
    inline = _env_value(environ, "RESTIA_ENCRYPTION_KEY", "ODYSSEUS_ENCRYPTION_KEY")
    key_file = _env_value(
        environ,
        "RESTIA_ENCRYPTION_KEY_FILE",
        "ODYSSEUS_ENCRYPTION_KEY_FILE",
    )
    if inline and key_file:
        raise DatabaseConfigurationError(
            "Configure exactly one of RESTIA_ENCRYPTION_KEY or "
            "RESTIA_ENCRYPTION_KEY_FILE"
        )
    if inline:
        try:
            raw_inline = inline.encode("ascii", errors="strict")
        except UnicodeEncodeError as exc:
            raise DatabaseConfigurationError(
                "The configured RESTIA_ENCRYPTION_KEY is not a valid Fernet "
                "encryption key"
            ) from exc
        return _validate_fernet_key(
            raw_inline,
            source="RESTIA_ENCRYPTION_KEY",
        )
    if key_file:
        path = Path(key_file).expanduser()
        try:
            raw_key = path.read_bytes()
        except OSError as exc:
            raise DatabaseConfigurationError(
                "RESTIA_ENCRYPTION_KEY_FILE must name a readable existing file"
            ) from exc
        return _validate_fernet_key(raw_key, source="RESTIA_ENCRYPTION_KEY_FILE")
    return None


def validate_database_mode(
    *,
    environ: Mapping[str, str] | None = None,
    database_url: str | None = None,
    require_schema_authority: bool = True,
) -> DatabaseRuntimeConfig:
    """Validate the selected database deployment mode without mutating schema.

    ``require_schema_authority=False`` exists only for diagnostics and the
    baseline-stamp command.  Production startup always uses the default and
    therefore cannot opt into shared mode prematurely.
    """

    env = os.environ if environ is None else environ
    mode = _env_value(env, "RESTIA_DATABASE_MODE", "ODYSSEUS_DATABASE_MODE")
    mode = mode or LOCAL_SINGLE_MODE
    if mode not in SUPPORTED_DATABASE_MODES:
        choices = ", ".join(sorted(SUPPORTED_DATABASE_MODES))
        raise DatabaseConfigurationError(
            f"RESTIA_DATABASE_MODE must be one of: {choices}"
        )

    url = str(database_url or env.get("DATABASE_URL") or "").strip()
    if not url:
        from src.constants import DATA_DIR

        url = f"sqlite:///{Path(DATA_DIR) / 'app.db'}"
    dialect = _database_dialect(url)
    auth_enabled = _parse_bool(env, "AUTH_ENABLED", default=True)
    localhost_bypass = _parse_bool(env, "LOCALHOST_BYPASS", default=False)

    if mode == LOCAL_SINGLE_MODE:
        if dialect != "sqlite":
            raise DatabaseConfigurationError(
                "local-single database mode currently requires SQLite; "
                "PostgreSQL belongs to guarded shared mode"
            )
        return DatabaseRuntimeConfig(
            mode=mode,
            database_url=url,
            dialect=dialect,
            auth_enabled=auth_enabled,
            localhost_bypass=localhost_bypass,
            encryption_key_source=None,
        )

    if dialect != "postgresql":
        raise DatabaseConfigurationError(
            "shared database mode requires a PostgreSQL DATABASE_URL"
        )
    if not auth_enabled:
        raise DatabaseConfigurationError(
            "shared database mode requires AUTH_ENABLED=true"
        )
    if localhost_bypass:
        raise DatabaseConfigurationError(
            "shared database mode requires LOCALHOST_BYPASS=false"
        )
    key_source = _shared_encryption_key_source(env)
    if not key_source:
        raise DatabaseConfigurationError(
            "shared database mode requires RESTIA_ENCRYPTION_KEY or "
            "RESTIA_ENCRYPTION_KEY_FILE so every process uses the same key"
        )
    if require_schema_authority and not SHARED_SCHEMA_AUTHORITY_READY:
        raise DatabaseConfigurationError(
            "shared database mode is not available yet: the legacy schema "
            "bootstrap contains SQLite-specific migrations and Alembic is not "
            "yet the authoritative PostgreSQL schema path"
        )

    return DatabaseRuntimeConfig(
        mode=mode,
        database_url=url,
        dialect=dialect,
        auth_enabled=auth_enabled,
        localhost_bypass=localhost_bypass,
        encryption_key_source=key_source,
    )


def initialize_database() -> DatabaseRuntimeConfig:
    """Validate configuration and run the current explicit schema bootstrap."""

    config = validate_database_mode()
    from core.database import engine, init_db

    binding = (id(engine), str(engine.url))
    global _initialized_binding
    if _initialized_binding == binding:
        return config
    with _initialization_lock:
        if _initialized_binding != binding:
            init_db()
            _initialized_binding = binding
    return config
