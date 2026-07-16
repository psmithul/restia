"""Read-only schema status plus a conservative legacy-baseline stamp command."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from src.database_runtime import validate_database_mode


LEGACY_BASELINE_REVISION = "20260716_0001"
LEGACY_BASELINE_REQUIRED_TABLES = frozenset(
    {
        "accounts",
        "auth_identities",
        "inbox_items",
        "action_audit",
        "sessions",
        "chat_messages",
        "model_endpoints",
        "scheduled_tasks",
    }
)


class SchemaRevisionError(RuntimeError):
    """Raised when schema revision state is absent, ambiguous, or stale."""


@dataclass(frozen=True)
class SchemaRevisionStatus:
    expected_revision: str
    current_revisions: tuple[str, ...]
    state: str
    matches_expected: bool

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["current_revisions"] = list(self.current_revisions)
        return value


def schema_revision_status(engine: Engine) -> SchemaRevisionStatus:
    """Inspect Alembic state without creating tables or stamping revisions."""

    if engine.dialect.name == "sqlite":
        database = str(engine.url.database or "")
        if database not in ("", ":memory:") and not Path(database).exists():
            return SchemaRevisionStatus(
                expected_revision=LEGACY_BASELINE_REVISION,
                current_revisions=(),
                state="unstamped",
                matches_expected=False,
            )
    if not inspect(engine).has_table("alembic_version"):
        return SchemaRevisionStatus(
            expected_revision=LEGACY_BASELINE_REVISION,
            current_revisions=(),
            state="unstamped",
            matches_expected=False,
        )
    with engine.connect() as connection:
        revisions = tuple(
            sorted(
                str(row[0])
                for row in connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).all()
            )
        )
    matches = revisions == (LEGACY_BASELINE_REVISION,)
    return SchemaRevisionStatus(
        expected_revision=LEGACY_BASELINE_REVISION,
        current_revisions=revisions,
        state="current" if matches else "mismatch",
        matches_expected=matches,
    )


def assert_schema_revision(engine: Engine) -> SchemaRevisionStatus:
    status = schema_revision_status(engine)
    if not status.matches_expected:
        current = ", ".join(status.current_revisions) or "unstamped"
        raise SchemaRevisionError(
            f"Database schema revision is {current}; expected "
            f"{status.expected_revision}"
        )
    return status


def database_status() -> dict[str, Any]:
    """Return deployment-mode and Alembic status without schema mutation."""

    from core.database import engine

    mode_error = None
    try:
        config = validate_database_mode(require_schema_authority=False)
        mode = config.mode
        dialect = config.dialect
        schema_authority = config.schema_authority
    except Exception as exc:
        mode_error = str(exc)
        mode = "invalid"
        dialect = engine.dialect.name
        schema_authority = "legacy-sqlite-bootstrap"

    revision_error = None
    try:
        revision = schema_revision_status(engine).as_dict()
    except Exception as exc:
        # Status must remain useful when a configured remote database is down,
        # without echoing a connection string or credentials into logs/JSON.
        revision_error = f"{type(exc).__name__}: database revision query failed"
        revision = {
            "expected_revision": LEGACY_BASELINE_REVISION,
            "current_revisions": [],
            "state": "unavailable",
            "matches_expected": False,
        }
    return {
        "database_mode": mode,
        "dialect": dialect,
        "schema_authority": schema_authority,
        "shared_schema_ready": False,
        "configuration_error": mode_error,
        "revision_error": revision_error,
        "revision": revision,
    }


def _alembic_config(database_url: str):
    try:
        from alembic.config import Config
    except ImportError as exc:
        raise SchemaRevisionError(
            "Alembic is not installed; install Restia's requirements first"
        ) from exc

    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def stamp_legacy_baseline() -> SchemaRevisionStatus:
    """Stamp a fully bootstrapped local SQLite database at the frozen baseline.

    This never builds schema from ``Base.metadata``. It verifies a fixed set of
    V3-foundation tables, then uses Alembic's stamp operation (which runs no
    revision upgrade code). Empty or partial databases are refused.
    """

    config = validate_database_mode(require_schema_authority=False)
    if config.mode != "local-single" or config.dialect != "sqlite":
        raise SchemaRevisionError(
            "The legacy baseline may only stamp a local-single SQLite database"
        )

    from core.database import engine

    existing = set(inspect(engine).get_table_names())
    missing = sorted(LEGACY_BASELINE_REQUIRED_TABLES - existing)
    if missing:
        raise SchemaRevisionError(
            "Refusing to stamp a partial legacy schema; run the current Restia "
            f"bootstrap first. Missing sentinel tables: {', '.join(missing)}"
        )

    before = schema_revision_status(engine)
    if before.state == "mismatch":
        current = ", ".join(before.current_revisions) or "unknown"
        raise SchemaRevisionError(
            f"Refusing to replace existing Alembic revision state: {current}"
        )
    if before.matches_expected:
        return before

    from alembic import command

    command.stamp(_alembic_config(config.database_url), LEGACY_BASELINE_REVISION)
    return assert_schema_revision(engine)
