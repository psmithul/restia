from __future__ import annotations

from alembic import command
from sqlalchemy import create_engine, inspect

from migrations.versions.passkey_authority_20260731_0016 import (
    PASSKEY_REQUIRED_COLUMNS,
    PASSKEY_REQUIRED_TABLES,
)
from src.database_migrations import (
    SCHEMA_HEAD_REVISION,
    _alembic_config,
    schema_revision_status,
    upgrade_schema,
    validate_head_schema,
)


PREVIOUS_REVISION = "20260730_0015"
PASSKEY_REVISION = "20260731_0016"


def _run(engine, operation, revision: str) -> None:
    config = _alembic_config(str(engine.url))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        operation(config, revision)


def test_passkey_authority_round_trips_and_remains_in_current_head(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'passkey-authority.db'}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, PREVIOUS_REVISION)
    before = inspect(engine)
    assert not (PASSKEY_REQUIRED_TABLES & set(before.get_table_names()))
    assert not (
        PASSKEY_REQUIRED_COLUMNS["auth_sessions"]
        & {column["name"] for column in before.get_columns("auth_sessions")}
    )

    _run(engine, command.upgrade, PASSKEY_REVISION)
    inspector = inspect(engine)
    assert PASSKEY_REQUIRED_TABLES <= set(inspector.get_table_names())
    for table_name, required in PASSKEY_REQUIRED_COLUMNS.items():
        assert required <= {
            str(column["name"])
            for column in inspector.get_columns(table_name)
        }
    upgrade_schema(engine)
    assert SCHEMA_HEAD_REVISION == "20260802_0018"
    validate_head_schema(engine)
    assert schema_revision_status(engine).current_revisions == (SCHEMA_HEAD_REVISION,)

    _run(engine, command.downgrade, PREVIOUS_REVISION)
    after = inspect(engine)
    assert not (PASSKEY_REQUIRED_TABLES & set(after.get_table_names()))
    assert not (
        PASSKEY_REQUIRED_COLUMNS["auth_sessions"]
        & {column["name"] for column in after.get_columns("auth_sessions")}
    )
    engine.dispose()
