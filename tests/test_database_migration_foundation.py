from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect, text

from src import database_runtime
from src.database_migrations import (
    LEGACY_BASELINE_REVISION,
    SchemaRevisionError,
    assert_schema_revision,
    schema_revision_status,
    stamp_legacy_baseline,
)
from src.database_runtime import DatabaseConfigurationError, validate_database_mode


ROOT = Path(__file__).resolve().parent.parent


def _shared_env(**overrides: str) -> dict[str, str]:
    env = {
        "RESTIA_DATABASE_MODE": "shared",
        "DATABASE_URL": "postgresql+psycopg://restia@db/restia",
        "AUTH_ENABLED": "true",
        "LOCALHOST_BYPASS": "false",
        "RESTIA_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
    }
    env.update(overrides)
    return env


def test_importing_core_database_does_not_create_or_migrate_schema(tmp_path):
    db_path = tmp_path / "import-only.db"
    data_dir = tmp_path / "data"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_path}",
            "RESTIA_DATA_DIR": str(data_dir),
            "RESTIA_DATABASE_MODE": "local-single",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import core.database; print('imported')",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("imported")
    assert not db_path.exists()


def test_initialize_database_runs_bootstrap_once_per_engine(monkeypatch):
    import core.database as database

    calls = []
    monkeypatch.setattr(database, "init_db", lambda: calls.append("init"))
    monkeypatch.setattr(database_runtime, "_initialized_binding", None)
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "local-single")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")

    database_runtime.initialize_database()
    database_runtime.initialize_database()

    assert calls == ["init"]


def test_app_bootstrap_precedes_component_initialization():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert source.index("initialize_database()") < source.index("initialize_managers(")


def test_fresh_data_import_app_creates_schema_before_consumers(tmp_path):
    db_path = tmp_path / "fresh-app.db"
    data_dir = tmp_path / "data"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_path}",
            "RESTIA_DATA_DIR": str(data_dir),
            "RESTIA_DATABASE_MODE": "local-single",
            "AUTH_ENABLED": "false",
            "LOCALHOST_BYPASS": "false",
            "RESTIA_STARTUP_WARMUPS": "0",
            "RESTIA_MODEL_KEEPALIVE": "0",
            "RESTIA_INPROCESS_TASKS": "0",
            "RESTIA_INPROCESS_POLLERS": "0",
            "RESTIA_INPROCESS_TELEGRAM": "0",
            "FASTEMBED_CACHE_PATH": str(tmp_path / "fastembed"),
            "MNEMOSYNE_DATA_DIR": str(tmp_path / "mnemosyne"),
        }
    )
    code = (
        "import sqlite3; import app; "
        f"c=sqlite3.connect({str(db_path)!r}); "
        "names={r[0] for r in c.execute(\"SELECT name FROM sqlite_master "
        "WHERE type='table'\")}; c.close(); "
        "assert {'sessions','accounts','inbox_items'} <= names; print('ready')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ready")


@pytest.mark.parametrize(
    ("env", "message"),
    [
        (
            _shared_env(DATABASE_URL="sqlite:///shared.db"),
            "requires a PostgreSQL DATABASE_URL",
        ),
        (
            _shared_env(AUTH_ENABLED="false"),
            "requires AUTH_ENABLED=true",
        ),
        (
            _shared_env(LOCALHOST_BYPASS="true"),
            "requires LOCALHOST_BYPASS=false",
        ),
        (
            {
                key: value
                for key, value in _shared_env().items()
                if key != "RESTIA_ENCRYPTION_KEY"
            },
            "requires RESTIA_ENCRYPTION_KEY",
        ),
    ],
)
def test_shared_mode_security_prerequisites_fail_closed(env, message):
    with pytest.raises(DatabaseConfigurationError, match=message):
        validate_database_mode(environ=env)


def test_shared_mode_refuses_startup_until_alembic_is_authoritative():
    with pytest.raises(DatabaseConfigurationError, match="not available yet"):
        validate_database_mode(environ=_shared_env())


def test_shared_mode_diagnostic_validation_can_report_prerequisites():
    config = validate_database_mode(
        environ=_shared_env(),
        require_schema_authority=False,
    )
    assert config.mode == "shared"
    assert config.dialect == "postgresql"
    assert config.encryption_key_source == "RESTIA_ENCRYPTION_KEY"


def test_shared_mode_wraps_non_ascii_inline_key_as_configuration_error():
    with pytest.raises(DatabaseConfigurationError, match="valid Fernet"):
        validate_database_mode(
            environ=_shared_env(RESTIA_ENCRYPTION_KEY="not-ascii-é"),
            require_schema_authority=False,
        )


def test_local_single_mode_rejects_non_sqlite_database():
    with pytest.raises(DatabaseConfigurationError, match="requires SQLite"):
        validate_database_mode(
            environ={
                "RESTIA_DATABASE_MODE": "local-single",
                "DATABASE_URL": "postgresql+psycopg://restia@db/restia",
            }
        )


def test_revision_mismatch_is_detected_without_mutating_schema():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('old_revision')")
        )

    status = schema_revision_status(engine)
    assert status.state == "mismatch"
    assert status.current_revisions == ("old_revision",)
    with pytest.raises(SchemaRevisionError, match=LEGACY_BASELINE_REVISION):
        assert_schema_revision(engine)


def test_unstamped_revision_status_does_not_create_version_table():
    engine = create_engine("sqlite:///:memory:")
    status = schema_revision_status(engine)
    assert status.state == "unstamped"
    assert status.current_revisions == ()
    assert not inspect(engine).has_table("alembic_version")


def test_legacy_stamp_refuses_partial_schema_before_loading_alembic(monkeypatch):
    import core.database as database

    partial = create_engine("sqlite:///:memory:")
    with partial.begin() as connection:
        connection.execute(text("CREATE TABLE sessions (id VARCHAR PRIMARY KEY)"))
    monkeypatch.setattr(database, "engine", partial)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "local-single")

    with pytest.raises(SchemaRevisionError, match="partial legacy schema"):
        stamp_legacy_baseline()


def test_legacy_stamp_succeeds_and_is_idempotent(tmp_path, monkeypatch):
    import core.database as database
    from src.database_migrations import LEGACY_BASELINE_REQUIRED_TABLES

    db_path = tmp_path / "legacy-complete.db"
    legacy = create_engine(f"sqlite:///{db_path}")
    with legacy.begin() as connection:
        for table_name in sorted(LEGACY_BASELINE_REQUIRED_TABLES):
            connection.execute(
                text(f'CREATE TABLE "{table_name}" (id VARCHAR PRIMARY KEY)')
            )

    monkeypatch.setattr(database, "engine", legacy)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "local-single")
    application_logger = logging.getLogger("restia.tests.alembic-preserves-loggers")
    application_logger.disabled = False

    first = stamp_legacy_baseline()
    second = stamp_legacy_baseline()

    assert first.matches_expected is True
    assert first.current_revisions == (LEGACY_BASELINE_REVISION,)
    assert second == first
    assert application_logger.disabled is False


def test_baseline_revision_is_stamp_only_and_not_dynamic_metadata():
    revision = (
        ROOT / "migrations" / "versions" / "20260716_0001_legacy_baseline.py"
    ).read_text(encoding="utf-8")
    assert "stamp-only" in revision
    assert "Base.metadata" not in revision
    assert "create_all" not in revision


def test_database_backed_cli_entrypoints_opt_into_explicit_initialization():
    names = (
        "calendar",
        "docs",
        "gallery",
        "mail",
        "mcp",
        "notes",
        "sessions",
        "signature",
        "tasks",
        "webhook",
    )
    for name in names:
        source = (ROOT / "scripts" / f"odysseus-{name}").read_text(encoding="utf-8")
        assert "initialize_db=True" in source, name


def test_secret_storage_uses_explicit_shared_key_without_writing_local_file(
    tmp_path,
    monkeypatch,
):
    from src import secret_storage

    key_path = tmp_path / ".app_key"
    monkeypatch.setattr(secret_storage, "_KEY_PATH", key_path)
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("RESTIA_ENCRYPTION_KEY_FILE", raising=False)

    encrypted = secret_storage.encrypt("shared secret")
    assert secret_storage.decrypt(encrypted) == "shared secret"
    assert not key_path.exists()
