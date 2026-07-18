from __future__ import annotations

from alembic import command
import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from core.database import (
    EmailAutomationRule,
    EmailAutomationRun,
    EmailRuntimeImportRun,
    EmailScheduledDelivery,
    EmailTagState,
)
from migrations.versions.email_runtime_authority_20260727_0012 import (
    EMAIL_RUNTIME_REQUIRED_COLUMNS,
    EMAIL_RUNTIME_REQUIRED_TABLES,
)
from src.database_migrations import _alembic_config


PREVIOUS_REVISION = "20260726_0011"
REVISION = "20260727_0012"


def _run(engine, operation, revision: str) -> None:
    config = _alembic_config(str(engine.url))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        operation(config, revision)


def test_0012_upgrade_and_downgrade_are_executable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'email-runtime-migration.db'}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, PREVIOUS_REVISION)
    assert not EMAIL_RUNTIME_REQUIRED_TABLES.intersection(
        inspect(engine).get_table_names()
    )
    _run(engine, command.upgrade, REVISION)
    inspector = inspect(engine)
    assert EMAIL_RUNTIME_REQUIRED_TABLES <= set(inspector.get_table_names())
    for table_name, required in EMAIL_RUNTIME_REQUIRED_COLUMNS.items():
        assert required <= {
            str(column["name"])
            for column in inspector.get_columns(table_name)
        }
    _run(engine, command.downgrade, PREVIOUS_REVISION)
    assert not EMAIL_RUNTIME_REQUIRED_TABLES.intersection(
        inspect(engine).get_table_names()
    )
    engine.dispose()


def test_email_runtime_models_compile_for_postgresql():
    ddls = {
        model.__tablename__: str(CreateTable(model.__table__).compile(
            dialect=postgresql.dialect()
        ))
        for model in (
            EmailTagState,
            EmailAutomationRule,
            EmailScheduledDelivery,
            EmailAutomationRun,
            EmailRuntimeImportRun,
        )
    }
    for name, ddl in ddls.items():
        assert "FOREIGN KEY(owner_id) REFERENCES accounts (id) ON DELETE CASCADE" in ddl, name
    assert (
        "FOREIGN KEY(email_account_id) REFERENCES email_accounts (id) ON DELETE RESTRICT"
        in ddls["email_scheduled_deliveries"]
    )
    assert "UNIQUE (owner_id, account_key, message_digest)" in ddls["email_tag_states"]
    assert (
        "UNIQUE (owner_id, account_key, operation, message_digest)"
        in ddls["email_automation_runs"]
    )


def test_0012_downgrade_refuses_retained_runtime_state(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'email-runtime-retained.db'}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, REVISION)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO accounts "
            "(id, username, status, auth_epoch, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("owner-alice", "alice", "active"),
        )
        connection.exec_driver_sql(
            "INSERT INTO email_tag_states "
            "(id, owner_id, account_key, message_digest, location_digest, "
            "payload, version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (
                "tag-1", "owner-alice", "mail-alice", "a" * 64,
                "b" * 64, "{}",
            ),
        )

    with pytest.raises(RuntimeError, match="contains tag, rule, schedule"):
        _run(engine, command.downgrade, PREVIOUS_REVISION)

    with engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM email_tag_states")
    _run(engine, command.downgrade, PREVIOUS_REVISION)
    engine.dispose()
