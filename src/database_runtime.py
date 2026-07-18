"""Database deployment-mode validation and explicit process initialization.

Restia uses an explicit Alembic baseline for empty databases and a verified,
one-time adoption path for pre-Alembic SQLite installations. V3 keeps shared
PostgreSQL startup is enabled only after every runtime state store and singleton
worker role has a shared authority and database-fenced leadership.

``local-single`` (the default)
    One private Restia process using SQLite and the Alembic schema authority.

``shared``
    Multi-interface and multi-device deployment using PostgreSQL, shared blob
    storage, one encryption key, and database-time worker leases.
"""

from __future__ import annotations

import importlib
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from cryptography.fernet import Fernet
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


LOCAL_SINGLE_MODE = "local-single"
SHARED_MODE = "shared"
SUPPORTED_DATABASE_MODES = frozenset({LOCAL_SINGLE_MODE, SHARED_MODE})

# This must only become True in the same change that removes every remaining
# SQLite/local-file runtime authority from the shared startup path.
SHARED_SCHEMA_AUTHORITY_READY = True
SCHEMA_AUTHORITY = "alembic-20260730"

# These are executable release blockers, not prose documentation.  Shared-mode
# startup must remain disabled while any entry is present.  Keep the codes
# stable so deployment diagnostics and CI can compare them without parsing an
# exception message.
SHARED_RUNTIME_BLOCKERS: tuple[dict[str, str], ...] = ()

_initialization_lock = threading.Lock()
_initialized_binding: tuple[int, str] | None = None


class DatabaseConfigurationError(RuntimeError):
    """Raised when the selected database deployment mode is unsafe."""


@dataclass(frozen=True)
class DatabaseRuntimeConfig:
    mode: str
    database_url: str
    dialect: str
    driver: str
    auth_enabled: bool
    localhost_bypass: bool
    encryption_key_source: str | None
    blob_store_kind: str
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


def shared_runtime_blockers() -> list[dict[str, str]]:
    """Return a mutable copy of the current shared-runtime release blockers."""

    return [dict(blocker) for blocker in SHARED_RUNTIME_BLOCKERS]


def _database_url_contract(database_url: str) -> tuple[str, str, URL]:
    """Parse one SQLAlchemy URL without ever rendering its credentials."""

    try:
        parsed = make_url(database_url)
    except (ArgumentError, TypeError, ValueError) as exc:
        raise DatabaseConfigurationError(
            "DATABASE_URL is not a valid SQLAlchemy database URL"
        ) from exc
    drivername = str(parsed.drivername or "").lower()
    backend = drivername.split("+", 1)[0]
    driver = drivername.split("+", 1)[1] if "+" in drivername else ""
    if backend == "postgres":
        backend = "postgresql"
    return backend or "unknown", driver, parsed


def _validate_postgresql_contract(driver: str, parsed: URL) -> None:
    """Require the one installed/supported PostgreSQL driver and a usable DSN."""

    if driver != "psycopg":
        raise DatabaseConfigurationError(
            "shared database mode requires an explicit "
            "postgresql+psycopg:// DATABASE_URL"
        )
    if not str(parsed.database or "").strip():
        raise DatabaseConfigurationError(
            "shared database mode requires DATABASE_URL to name a PostgreSQL database"
        )
    try:
        importlib.import_module("psycopg")
    except (ImportError, ModuleNotFoundError) as exc:
        raise DatabaseConfigurationError(
            "PostgreSQL support requires psycopg 3; install Restia's core requirements"
        ) from exc


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
    dialect, driver, parsed_url = _database_url_contract(url)
    auth_enabled = _parse_bool(env, "AUTH_ENABLED", default=True)
    localhost_bypass = _parse_bool(env, "LOCALHOST_BYPASS", default=False)

    if mode == LOCAL_SINGLE_MODE:
        if dialect != "sqlite":
            raise DatabaseConfigurationError(
                "local-single database mode currently requires SQLite; "
                "PostgreSQL belongs to guarded shared mode"
            )
        from src.blob_store import (
            BlobStoreConfigurationError,
            resolve_blob_store_config,
        )
        try:
            blob_config = resolve_blob_store_config(
                environ=env, database_mode=mode, create=False,
            )
        except BlobStoreConfigurationError as exc:
            raise DatabaseConfigurationError(str(exc)) from exc
        return DatabaseRuntimeConfig(
            mode=mode,
            database_url=url,
            dialect=dialect,
            driver=driver or "sqlite3",
            auth_enabled=auth_enabled,
            localhost_bypass=localhost_bypass,
            encryption_key_source=None,
            blob_store_kind=blob_config.kind,
        )

    if dialect != "postgresql":
        raise DatabaseConfigurationError(
            "shared database mode requires a PostgreSQL DATABASE_URL"
        )
    _validate_postgresql_contract(driver, parsed_url)
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
    from src.blob_store import (
        BlobStoreConfigurationError,
        resolve_blob_store_config,
    )
    try:
        blob_config = resolve_blob_store_config(
            environ=env, database_mode=mode, create=False,
        )
    except BlobStoreConfigurationError as exc:
        raise DatabaseConfigurationError(str(exc)) from exc
    if require_schema_authority and (
        not SHARED_SCHEMA_AUTHORITY_READY or SHARED_RUNTIME_BLOCKERS
    ):
        raise DatabaseConfigurationError(
            "shared database mode is not available yet; run "
            "scripts/odysseus-db shared-gate --pretty for the machine-readable "
            "runtime blockers"
        )

    return DatabaseRuntimeConfig(
        mode=mode,
        database_url=url,
        dialect=dialect,
        driver=driver,
        auth_enabled=auth_enabled,
        localhost_bypass=localhost_bypass,
        encryption_key_source=key_source,
        blob_store_kind=blob_config.kind,
    )


def _normalize_legacy_caldav_calendar_owners(
    target_engine,
    *,
    preferences_path: str | os.PathLike[str],
) -> int:
    """Resolve only connector-backed legacy CalDAV owner aliases.

    Older CalDAV discovery sometimes stored the remote login address in
    ``calendars.owner`` instead of Restia's profile username.  During local
    pre-Alembic adoption, the preserved ``user_prefs.json`` still provides a
    stronger relationship: each CalDAV connector id lives under exactly one
    ``_users/<username>`` profile.  Normalize only that exact, unique mapping;
    local calendars and missing/ambiguous connector ids remain untouched so
    the reviewed calendar preflight fails closed.
    """

    if target_engine.dialect.name != "sqlite":
        return 0

    from sqlalchemy import inspect, text

    schema = inspect(target_engine)
    if not schema.has_table("accounts") or not schema.has_table("calendars"):
        return 0
    calendar_columns = {
        str(column["name"]) for column in schema.get_columns("calendars")
    }
    if not {"id", "owner", "source", "account_id"} <= calendar_columns:
        return 0

    from src.profile_configuration_import import _read_bounded_json

    snapshot = _read_bounded_json(Path(preferences_path))
    if snapshot is None:
        return 0
    payload, _source_digest = snapshot
    if not isinstance(payload, Mapping):
        return 0
    users = payload.get("_users")
    if not isinstance(users, Mapping):
        return 0

    with target_engine.begin() as connection:
        account_rows = connection.execute(text(
            "SELECT id, username FROM accounts"
        )).mappings().all()
        accounts_by_username: dict[str, set[str]] = {}
        canonical_usernames: dict[str, str] = {}
        for row in account_rows:
            username = str(row["username"] or "").strip()
            normalized = username.lower()
            if not normalized:
                continue
            accounts_by_username.setdefault(normalized, set()).add(str(row["id"]))
            canonical_usernames[normalized] = username

        connector_owners: dict[str, set[str]] = {}
        for raw_username, raw_preferences in users.items():
            normalized = str(raw_username or "").strip().lower()
            if (
                len(accounts_by_username.get(normalized, ())) != 1
                or not isinstance(raw_preferences, Mapping)
            ):
                continue
            connectors = raw_preferences.get("caldav_accounts", ())
            if not isinstance(connectors, list):
                continue
            for connector in connectors:
                if not isinstance(connector, Mapping):
                    continue
                connector_id = str(connector.get("id") or "").strip()
                if connector_id:
                    connector_owners.setdefault(connector_id, set()).add(
                        normalized
                    )

        candidates = connection.execute(text("""
            SELECT c.id, c.account_id, c.owner
            FROM calendars AS c
            LEFT JOIN accounts AS a
              ON lower(trim(a.username)) = lower(trim(COALESCE(c.owner, '')))
            WHERE lower(trim(COALESCE(c.source, ''))) = 'caldav'
              AND trim(COALESCE(c.account_id, '')) <> ''
              AND a.id IS NULL
        """)).mappings().all()
        updated = 0
        for calendar in candidates:
            connector_id = str(calendar["account_id"] or "").strip()
            owners = connector_owners.get(connector_id, set())
            if len(owners) != 1:
                continue
            normalized = next(iter(owners))
            username = canonical_usernames[normalized]
            result = connection.execute(text("""
                UPDATE calendars
                SET owner = :username
                WHERE id = :calendar_id
                  AND account_id = :connector_id
                  AND (
                    owner = :previous_owner
                    OR (owner IS NULL AND :previous_owner IS NULL)
                  )
            """), {
                "username": username,
                "calendar_id": calendar["id"],
                "connector_id": connector_id,
                "previous_owner": calendar["owner"],
            })
            if int(result.rowcount or 0) != 1:
                raise RuntimeError(
                    "Legacy CalDAV owner mapping changed during adoption"
                )
            updated += 1
        return updated


def initialize_database() -> DatabaseRuntimeConfig:
    """Validate configuration and bring the selected database to Alembic head.

    Empty local databases run the explicit baseline. Existing pre-Alembic
    SQLite databases first run the final compatibility bootstrap and are then
    stamped only after the frozen baseline manifest verifies them. A database
    with an unknown revision or unrecognized partial schema is never mutated.
    """

    config = validate_database_mode()
    from core.database import engine, harden_database_permissions, init_db
    from src.database_migrations import (
        SchemaRevisionError,
        application_table_names,
        backup_legacy_sqlite_database,
        schema_revision_status,
        stamp_legacy_baseline,
        upgrade_schema,
        validate_head_schema,
        validate_legacy_adoption_preflight,
    )

    binding = (id(engine), str(engine.url))
    global _initialized_binding
    if _initialized_binding == binding:
        return config
    with _initialization_lock:
        if _initialized_binding != binding:
            try:
                status = schema_revision_status(engine)
                if status.state == "unknown":
                    current = ", ".join(status.current_revisions) or "unknown"
                    raise SchemaRevisionError(
                        f"Database schema revision is unknown ({current}); expected "
                        f"{status.expected_revision}"
                    )
                application_tables = application_table_names(engine)
                if status.matches_expected:
                    # A revision row is not proof of a usable database. This
                    # also catches databases stamped by the pushed 0001
                    # foundation before the immutable 0002 repair existed.
                    validate_head_schema(engine)
                elif not application_tables:
                    # Alembic's immutable 0001 is stamp-only. Empty databases
                    # record it without executing code, then run explicit 0002.
                    upgrade_schema(engine)
                elif status.current_revisions in {
                    ("20260717_0002",),
                    ("20260718_0003",),
                    ("20260719_0004",),
                    ("20260720_0005",),
                    ("20260721_0006",),
                    ("20260722_0007",),
                    ("20260723_0008",),
                    ("20260724_0009",),
                    ("20260725_0010",),
                    ("20260726_0011",),
                    ("20260727_0012",),
                    ("20260728_0013",),
                    ("20260729_0014",),
                }:
                    # 0002 is a real executable Alembic baseline.  Later
                    # revisions may upgrade it normally, but private SQLite
                    # installs still receive a consistent recovery copy first.
                    if config.mode == LOCAL_SINGLE_MODE:
                        backup_legacy_sqlite_database(engine)
                    upgrade_schema(engine)
                elif config.mode == LOCAL_SINGLE_MODE:
                    if status.state == "unstamped":
                        # No version table is the normal pre-Alembic case. An
                        # existing-but-empty version table alongside app data is
                        # instead a failed downgrade/fake authority marker.
                        from sqlalchemy import inspect

                        if inspect(engine).has_table("alembic_version"):
                            raise SchemaRevisionError(
                                "Refusing to adopt application tables with an "
                                "empty Alembic version table"
                            )
                    validate_legacy_adoption_preflight(engine)
                    backup_legacy_sqlite_database(engine)

                    def initialize_legacy_principals() -> None:
                        """Import the preserved auth authority before owner backfill."""

                        from sqlalchemy.orm import sessionmaker

                        from src import constants as runtime_constants
                        from src.legacy_auth_import import (
                            LegacyAuthImportError,
                            import_legacy_auth,
                        )

                        auth_path = Path(runtime_constants.AUTH_FILE)
                        if not (auth_path.exists() or auth_path.is_symlink()):
                            return
                        factory = sessionmaker(
                            bind=engine,
                            autoflush=False,
                            expire_on_commit=False,
                        )
                        try:
                            import_legacy_auth(
                                factory,
                                auth_path=auth_path,
                                sessions_path=Path(runtime_constants.SESSIONS_FILE),
                            )
                            _normalize_legacy_caldav_calendar_owners(
                                engine,
                                preferences_path=runtime_constants.USER_PREFS_FILE,
                            )
                        except LegacyAuthImportError as exc:
                            raise SchemaRevisionError(
                                "Legacy authentication could not establish calendar "
                                f"principals before ownership repair ({exc.code})"
                            ) from exc

                    init_db(
                        legacy_principal_initializer=initialize_legacy_principals
                    )
                    stamp_legacy_baseline()
                else:
                    raise SchemaRevisionError(
                        "Shared databases must be empty or at a recognized "
                        "Alembic revision"
                    )
                harden_database_permissions()
            except SchemaRevisionError as exc:
                raise DatabaseConfigurationError(str(exc)) from exc
            _initialized_binding = binding
    return config
