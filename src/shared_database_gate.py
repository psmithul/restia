"""Executable PostgreSQL integration and shared-runtime readiness gate.

The default check is read-only.  ``migration_smoke=True`` creates a uniquely
named temporary PostgreSQL schema, runs the full Alembic chain there, validates
the reviewed head contract, and drops the schema in ``finally``.  It never
changes Restia's production schema. The separately enumerated runtime-authority
blockers remain authoritative if a future regression reintroduces one.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from src.database_runtime import (
    SHARED_SCHEMA_AUTHORITY_READY,
    DatabaseConfigurationError,
    shared_runtime_blockers,
    validate_database_mode,
)


EngineFactory = Callable[..., Engine]


def _safe_failure(stage: str, exc: BaseException) -> dict[str, object]:
    """Return a credential-free failure suitable for CLI/readiness JSON."""

    return {
        "ok": False,
        "code": f"{stage}_{type(exc).__name__.lower()}",
    }


def _engine_kwargs(*, search_path: str | None = None) -> dict[str, object]:
    connect_args: dict[str, object] = {"connect_timeout": 5}
    if search_path:
        # The schema identifier is generated internally and contains only
        # lowercase ASCII plus underscores. Supplying search_path at connect
        # time also covers every new connection Alembic or validation opens.
        connect_args["options"] = f"-csearch_path={search_path}"
    return {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "connect_args": connect_args,
    }


def _postgresql_connection_check(engine: Engine) -> dict[str, object]:
    if str(engine.dialect.name) != "postgresql":
        return {
            "ok": False,
            "code": "connection_dialect_mismatch",
        }
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            raw_version = connection.execute(
                text("SHOW server_version_num")
            ).scalar_one()
        return {
            "ok": True,
            "server_version_num": int(raw_version),
        }
    except Exception as exc:
        return _safe_failure("connection", exc)


def _current_schema_check(engine: Engine) -> dict[str, object]:
    from src.database_migrations import (
        schema_revision_status,
        validate_head_schema,
    )

    try:
        revision = schema_revision_status(engine)
        if not revision.matches_expected:
            return {
                "ok": False,
                "code": "schema_revision_mismatch",
                "revision": revision.as_dict(),
            }
        validate_head_schema(engine)
        return {
            "ok": True,
            "revision": revision.as_dict(),
        }
    except Exception as exc:
        return _safe_failure("schema_contract", exc)


def _temporary_schema_migration_smoke(
    config,
    *,
    base_engine: Engine,
    engine_factory: EngineFactory,
) -> dict[str, object]:
    """Run and clean up one isolated, full Alembic PostgreSQL migration."""

    from src.database_migrations import upgrade_schema

    schema_name = f"restia_gate_{uuid.uuid4().hex}"
    created = False
    removed = False
    gate_engine: Engine | None = None
    result: dict[str, object] = {
        "ok": False,
        "code": "migration_smoke_interrupted",
    }
    try:
        with base_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
        created = True
        gate_engine = engine_factory(
            config.database_url,
            **_engine_kwargs(search_path=schema_name),
        )
        revision = upgrade_schema(gate_engine)
        result = {
            "ok": True,
            "revision": revision.as_dict(),
        }
    except Exception as exc:
        result = _safe_failure("migration_smoke", exc)
    finally:
        if gate_engine is not None:
            gate_engine.dispose()
        if created:
            try:
                with base_engine.begin() as connection:
                    connection.execute(
                        text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
                    )
                removed = True
            except Exception as cleanup_exc:
                result = _safe_failure("migration_smoke_cleanup", cleanup_exc)
    result["temporary_schema_removed"] = removed
    return result


def check_shared_database_gate(
    *,
    environ: Mapping[str, str] | None = None,
    migration_smoke: bool = False,
    engine_factory: EngineFactory = create_engine,
) -> dict[str, object]:
    """Return a secret-free shared database integration/readiness report.

    ``passed`` means the requested gate passed.  With ``migration_smoke`` it
    means PostgreSQL connectivity plus a disposable full migration succeeded.
    Without it, ``passed`` means the configured schema and the complete shared
    runtime are ready. Any enumerated blocker makes that result fail closed.
    """

    env = os.environ if environ is None else environ
    blockers = shared_runtime_blockers()
    report: dict[str, object] = {
        "gate": "postgresql_migration_smoke" if migration_smoke else "shared_runtime",
        "passed": False,
        "configuration": {"ok": False},
        "connection": {"ok": False, "code": "connection_not_attempted"},
        "schema": {"ok": False, "code": "schema_not_checked"},
        "migration_smoke": {
            "requested": migration_smoke,
            "ok": False if migration_smoke else None,
        },
        "postgresql_integration_passed": False,
        "runtime_ready": False,
        "runtime": {
            "schema_authority_ready": SHARED_SCHEMA_AUTHORITY_READY,
            "blockers": blockers,
        },
    }

    try:
        config = validate_database_mode(
            environ=env,
            require_schema_authority=False,
        )
    except DatabaseConfigurationError as exc:
        report["configuration"] = {
            "ok": False,
            "code": "configuration_invalid",
            # DatabaseConfigurationError messages are controlled strings that
            # never render the supplied URL, key, or key-file path.
            "message": str(exc),
        }
        return report
    if not config.shared:
        report["configuration"] = {
            "ok": False,
            "code": "configuration_not_shared_mode",
        }
        return report
    report["configuration"] = {
        "ok": True,
        "mode": config.mode,
        "dialect": config.dialect,
        "driver": config.driver,
        "blob_store": config.blob_store_kind,
        "schema_authority": config.schema_authority,
    }

    base_engine: Engine | None = None
    try:
        base_engine = engine_factory(config.database_url, **_engine_kwargs())
        connection = _postgresql_connection_check(base_engine)
        report["connection"] = connection
        if not bool(connection.get("ok")):
            return report

        if migration_smoke:
            smoke = _temporary_schema_migration_smoke(
                config,
                base_engine=base_engine,
                engine_factory=engine_factory,
            )
            report["migration_smoke"] = {
                "requested": True,
                **smoke,
            }
            integration_passed = bool(smoke.get("ok"))
        else:
            schema = _current_schema_check(base_engine)
            report["schema"] = schema
            integration_passed = bool(schema.get("ok"))

        report["postgresql_integration_passed"] = integration_passed
        runtime_ready = bool(
            integration_passed
            and SHARED_SCHEMA_AUTHORITY_READY
            and not blockers
        )
        report["runtime_ready"] = runtime_ready
        report["passed"] = integration_passed if migration_smoke else runtime_ready
        return report
    except Exception as exc:
        report["connection"] = _safe_failure("engine", exc)
        return report
    finally:
        if base_engine is not None:
            base_engine.dispose()


__all__ = ["check_shared_database_gate"]
