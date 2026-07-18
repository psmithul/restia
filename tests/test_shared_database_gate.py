from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from src import database_runtime
from src.database_runtime import (
    DatabaseConfigurationError,
    shared_runtime_blockers,
    validate_database_mode,
)
from src.database_migrations import database_status
from src.shared_database_gate import (
    _temporary_schema_migration_smoke,
    check_shared_database_gate,
)


def _shared_env(**overrides: str) -> dict[str, str]:
    values = {
        "RESTIA_DATABASE_MODE": "shared",
        "DATABASE_URL": "postgresql+psycopg://restia:private@db/restia",
        "AUTH_ENABLED": "true",
        "LOCALHOST_BYPASS": "false",
        "RESTIA_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "RESTIA_BLOB_STORE": "shared-filesystem",
        "RESTIA_BLOB_ROOT": "/mnt/restia-blobs",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://restia@db/restia",
        "postgresql+psycopg2://restia@db/restia",
        "postgres://restia@db/restia",
    ],
)
def test_shared_mode_requires_explicit_psycopg3_driver(database_url):
    with pytest.raises(DatabaseConfigurationError, match=r"postgresql\+psycopg"):
        validate_database_mode(
            environ=_shared_env(DATABASE_URL=database_url),
            require_schema_authority=False,
        )


def test_shared_mode_requires_named_postgresql_database():
    with pytest.raises(DatabaseConfigurationError, match="name a PostgreSQL database"):
        validate_database_mode(
            environ=_shared_env(DATABASE_URL="postgresql+psycopg://restia@db"),
            require_schema_authority=False,
        )


def test_shared_mode_fails_before_engine_creation_when_driver_is_missing(monkeypatch):
    real_import = database_runtime.importlib.import_module

    def missing_driver(name):
        if name == "psycopg":
            raise ModuleNotFoundError("private import detail")
        return real_import(name)

    monkeypatch.setattr(database_runtime.importlib, "import_module", missing_driver)
    with pytest.raises(DatabaseConfigurationError, match="requires psycopg 3"):
        validate_database_mode(
            environ=_shared_env(),
            require_schema_authority=False,
        )


def test_shared_startup_cannot_enable_while_runtime_blockers_remain(monkeypatch):
    monkeypatch.setattr(database_runtime, "SHARED_SCHEMA_AUTHORITY_READY", True)
    monkeypatch.setattr(
        database_runtime,
        "SHARED_RUNTIME_BLOCKERS",
        ({"code": "test-blocker", "authority": "test", "required_change": "test"},),
    )

    with pytest.raises(DatabaseConfigurationError, match="not available yet"):
        validate_database_mode(environ=_shared_env())


def test_reviewed_shared_runtime_configuration_is_admitted():
    config = validate_database_mode(environ=_shared_env())
    assert config.shared is True
    assert config.dialect == "postgresql"
    assert config.driver == "psycopg"


class _Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one(self):
        return self.value


class _Connection:
    def execute(self, statement):
        rendered = str(statement)
        if "server_version_num" in rendered:
            return _Result("160004")
        return _Result(1)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Engine:
    class _Dialect:
        name = "postgresql"

    dialect = _Dialect()

    def __init__(self):
        self.disposed = False

    def connect(self):
        return _Connection()

    def dispose(self):
        self.disposed = True


def test_temporary_schema_smoke_always_uses_and_removes_generated_schema(monkeypatch):
    statements = []
    created_engines = []

    class BaseEngine(_Engine):
        def begin(self):
            class Transaction(_Connection):
                def execute(self, statement):
                    statements.append(str(statement))
                    return _Result(1)

            return Transaction()

    class Revision:
        def as_dict(self):
            return {"matches_expected": True}

    monkeypatch.setattr(
        "src.database_migrations.upgrade_schema",
        lambda _engine: Revision(),
    )

    def make_engine(*_args, **kwargs):
        engine = _Engine()
        created_engines.append((engine, kwargs))
        return engine

    result = _temporary_schema_migration_smoke(
        SimpleNamespace(database_url="postgresql+psycopg://db/restia"),
        base_engine=BaseEngine(),
        engine_factory=make_engine,
    )

    assert result == {
        "ok": True,
        "revision": {"matches_expected": True},
        "temporary_schema_removed": True,
    }
    assert len(statements) == 2
    assert statements[0].startswith('CREATE SCHEMA "restia_gate_')
    assert statements[1].startswith('DROP SCHEMA IF EXISTS "restia_gate_')
    assert statements[1].endswith('" CASCADE')
    assert created_engines[0][0].disposed is True
    assert "search_path=restia_gate_" in str(
        created_engines[0][1]["connect_args"]["options"]
    )


def test_read_only_gate_reports_integrated_runtime_ready(monkeypatch):
    engine = _Engine()
    monkeypatch.setattr(
        "src.shared_database_gate._current_schema_check",
        lambda _engine: {"ok": True, "revision": {"matches_expected": True}},
    )

    report = check_shared_database_gate(
        environ=_shared_env(),
        engine_factory=lambda *_args, **_kwargs: engine,
    )

    assert report["configuration"] == {
        "ok": True,
        "mode": "shared",
        "dialect": "postgresql",
        "driver": "psycopg",
        "blob_store": "shared-filesystem",
        "schema_authority": database_runtime.SCHEMA_AUTHORITY,
    }
    assert report["connection"] == {
        "ok": True,
        "server_version_num": 160004,
    }
    assert report["postgresql_integration_passed"] is True
    assert report["runtime_ready"] is True
    assert report["passed"] is True
    assert engine.disposed is True
    assert "private" not in json.dumps(report)


def test_migration_smoke_exit_contract_is_independent_from_runtime_gate(monkeypatch):
    engine = _Engine()
    monkeypatch.setattr(
        "src.shared_database_gate._temporary_schema_migration_smoke",
        lambda *_args, **_kwargs: {
            "ok": True,
            "temporary_schema_removed": True,
            "revision": {"matches_expected": True},
        },
    )

    report = check_shared_database_gate(
        environ=_shared_env(),
        migration_smoke=True,
        engine_factory=lambda *_args, **_kwargs: engine,
    )

    assert report["gate"] == "postgresql_migration_smoke"
    assert report["postgresql_integration_passed"] is True
    assert report["migration_smoke"]["temporary_schema_removed"] is True
    assert report["runtime_ready"] is True
    assert report["passed"] is True


def test_gate_connection_failure_is_secret_free():
    class BrokenEngine(_Engine):
        def connect(self):
            raise RuntimeError("postgresql://restia:private@db/restia")

    report = check_shared_database_gate(
        environ=_shared_env(),
        engine_factory=lambda *_args, **_kwargs: BrokenEngine(),
    )

    assert report["passed"] is False
    assert report["connection"]["ok"] is False
    assert report["connection"]["code"] == "connection_runtimeerror"
    assert "private" not in json.dumps(report)


def test_shared_runtime_blocker_codes_are_unique_and_stable():
    codes = [blocker["code"] for blocker in shared_runtime_blockers()]
    assert len(codes) == len(set(codes))
    assert codes == []


def test_shared_database_mode_requires_explicit_shared_blob_contract():
    missing_store = _shared_env()
    missing_store.pop("RESTIA_BLOB_STORE")
    with pytest.raises(DatabaseConfigurationError, match="shared-filesystem"):
        validate_database_mode(
            environ=missing_store, require_schema_authority=False,
        )

    with pytest.raises(DatabaseConfigurationError, match="absolute"):
        validate_database_mode(
            environ=_shared_env(RESTIA_BLOB_ROOT="relative/blobs"),
            require_schema_authority=False,
        )


def test_database_status_reports_invalid_driver_without_opening_revision(monkeypatch):
    for key, value in _shared_env(
        DATABASE_URL="postgresql://restia:private@db/restia"
    ).items():
        monkeypatch.setenv(key, value)

    status = database_status()

    assert status["database_mode"] == "invalid"
    assert status["dialect"] == "unknown"
    assert "postgresql+psycopg" in status["configuration_error"]
    assert status["revision"]["state"] == "unavailable"
    assert "private" not in json.dumps(status)
