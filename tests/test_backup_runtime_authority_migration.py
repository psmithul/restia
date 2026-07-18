from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from migrations.versions.backup_runtime_authority_20260801_0017 import (
    BACKUP_RUNTIME_REQUIRED_COLUMNS,
    BACKUP_RUNTIME_REQUIRED_TABLES,
)
from src.database_migrations import _alembic_config


PREVIOUS_REVISION = "20260731_0016"
BACKUP_REVISION = "20260801_0017"


def _run(engine, operation, revision: str) -> None:
    config = _alembic_config(str(engine.url))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        operation(config, revision)


def test_backup_runtime_authority_round_trips(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backup-runtime.db'}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, PREVIOUS_REVISION)
    assert not (BACKUP_RUNTIME_REQUIRED_TABLES & set(inspect(engine).get_table_names()))

    _run(engine, command.upgrade, BACKUP_REVISION)
    inspector = inspect(engine)
    assert BACKUP_RUNTIME_REQUIRED_TABLES <= set(inspector.get_table_names())
    for table_name, required in BACKUP_RUNTIME_REQUIRED_COLUMNS.items():
        assert required <= {
            str(column["name"]) for column in inspector.get_columns(table_name)
        }

    _run(engine, command.downgrade, PREVIOUS_REVISION)
    assert not (BACKUP_RUNTIME_REQUIRED_TABLES & set(inspect(engine).get_table_names()))
    engine.dispose()


def test_backup_success_requires_encrypted_verified_evidence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'backup-checks.db'}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, BACKUP_REVISION)
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO backup_runs "
                "(id, run_key, trigger, database_mode, state, archive_name, "
                "archive_bytes, encrypted, verified, started_at, completed_at, "
                "created_at, updated_at) VALUES "
                "('bad', 'bad', 'scheduled', 'local-single', 'completed', "
                "'bad.restia', 10, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
    engine.dispose()
