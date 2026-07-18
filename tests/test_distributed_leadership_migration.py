from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from migrations.versions.distributed_worker_leadership_20260730_0015 import (
    RUNTIME_LEADERSHIP_REQUIRED_COLUMNS,
    RUNTIME_LEADERSHIP_REQUIRED_TABLES,
)
from src.database_migrations import (
    SCHEMA_HEAD_REVISION,
    _alembic_config,
    schema_revision_status,
    upgrade_schema,
    validate_head_schema,
)


PREVIOUS_REVISION = "20260729_0014"


def _run(engine, operation, revision: str) -> None:
    config = _alembic_config(str(engine.url))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        operation(config, revision)


def test_leadership_migration_is_the_reviewed_head_and_round_trips(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'leadership-migration.db'}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, PREVIOUS_REVISION)
    assert not (
        RUNTIME_LEADERSHIP_REQUIRED_TABLES
        & set(inspect(engine).get_table_names())
    )

    # The supported runtime upgrader must treat 0014 as an executable known-
    # behind revision; existing shared/local installs cannot require stamping.
    upgrade_schema(engine)
    inspector = inspect(engine)
    assert SCHEMA_HEAD_REVISION == "20260730_0015"
    assert RUNTIME_LEADERSHIP_REQUIRED_TABLES <= set(
        inspector.get_table_names()
    )
    for table_name, required in RUNTIME_LEADERSHIP_REQUIRED_COLUMNS.items():
        present = {
            str(column["name"])
            for column in inspector.get_columns(table_name)
        }
        assert required <= present
    assert schema_revision_status(engine).current_revisions == (
        SCHEMA_HEAD_REVISION,
    )
    validate_head_schema(engine)

    _run(engine, command.downgrade, PREVIOUS_REVISION)
    assert not (
        RUNTIME_LEADERSHIP_REQUIRED_TABLES
        & set(inspect(engine).get_table_names())
    )
    engine.dispose()


@pytest.mark.parametrize(
    "values",
    [
        ("bad-fence", "replica-a", -1, "2099-01-01 00:00:00", 1),
        ("bad-version", "replica-a", 1, "2099-01-01 00:00:00", 0),
        ("missing-holder", None, 1, "2099-01-01 00:00:00", 1),
        ("missing-expiry", "replica-a", 1, None, 1),
    ],
)
def test_leadership_database_checks_reject_malformed_state(tmp_path, values):
    engine = create_engine(f"sqlite:///{tmp_path / (values[0] + '.db')}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, SCHEMA_HEAD_REVISION)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO runtime_worker_leases "
                "(lease_name, holder_id, fencing_token, lease_expires_at, "
                "heartbeat_at, version, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)",
                values,
            )
    engine.dispose()


def test_released_and_held_leadership_rows_both_satisfy_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'valid-leases.db'}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, SCHEMA_HEAD_REVISION)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO runtime_worker_leases "
            "(lease_name, holder_id, fencing_token, lease_expires_at, "
            "heartbeat_at, version, created_at, updated_at) VALUES "
            "('released', NULL, 2, NULL, CURRENT_TIMESTAMP, 3, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP), "
            "('held', 'replica-a', 4, '2099-01-01 00:00:00', "
            "CURRENT_TIMESTAMP, 5, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
    validate_head_schema(engine)
    engine.dispose()
