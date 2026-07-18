from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine, inspect

from migrations.versions.upload_attachment_authority_20260729_0014 import (
    UPLOAD_METADATA_REQUIRED_COLUMNS,
    UPLOAD_METADATA_REQUIRED_TABLES,
)
from src.database_migrations import (
    SCHEMA_HEAD_REVISION,
    SchemaRevisionError,
    _alembic_config,
    schema_revision_status,
    validate_head_schema,
)


PREVIOUS_REVISION = "20260728_0013"


def _run(engine, operation, revision: str) -> None:
    config = _alembic_config(str(engine.url))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        operation(config, revision)


def test_upload_authority_migration_upgrades_from_0013_and_downgrades(tmp_path):
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    engine = create_engine(url)
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, PREVIOUS_REVISION)
    assert not (UPLOAD_METADATA_REQUIRED_TABLES & set(inspect(engine).get_table_names()))

    _run(engine, command.upgrade, "20260729_0014")
    inspector = inspect(engine)
    assert UPLOAD_METADATA_REQUIRED_TABLES <= set(inspector.get_table_names())
    for table, required in UPLOAD_METADATA_REQUIRED_COLUMNS.items():
        present = {column["name"] for column in inspector.get_columns(table)}
        assert required <= present
    assert schema_revision_status(engine).current_revisions == ("20260729_0014",)
    assert schema_revision_status(engine).state == "behind"
    assert SCHEMA_HEAD_REVISION == "20260731_0016"

    _run(engine, command.downgrade, PREVIOUS_REVISION)
    assert not (UPLOAD_METADATA_REQUIRED_TABLES & set(inspect(engine).get_table_names()))
    engine.dispose()


def _insert_plaintext_upload(engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO accounts "
            "(id, username, status, auth_epoch, created_at, updated_at) "
            "VALUES (?, ?, 'active', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("owner-alice", "alice"),
        )
        connection.exec_driver_sql(
            "INSERT INTO chat_upload_metadata "
            "(id, owner_id, content_digest, blob_key, payload, state, "
            "last_accessed_at, retention_until, version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            ("a" * 32 + ".txt", "owner-alice", "b" * 64, "2026/07/file", "{}"),
        )


def test_head_validation_rejects_plaintext_upload_metadata(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'plaintext.db'}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, SCHEMA_HEAD_REVISION)
    _insert_plaintext_upload(engine)

    with pytest.raises(
        SchemaRevisionError,
        match=r"chat_upload_metadata\.payload is plaintext",
    ):
        validate_head_schema(engine)
    engine.dispose()


def test_0014_downgrade_refuses_retained_upload_metadata(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'retained.db'}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, SCHEMA_HEAD_REVISION)
    _insert_plaintext_upload(engine)

    with pytest.raises(RuntimeError, match="contains retained upload or import"):
        _run(engine, command.downgrade, PREVIOUS_REVISION)
    # SQLite DDL is non-transactional: 0015's rebuildable lease table is
    # removed before 0014's loss-averse user-metadata downgrade refuses.
    assert schema_revision_status(engine).current_revisions == (
        "20260729_0014",
    )

    with engine.begin() as connection:
        connection.exec_driver_sql("DELETE FROM chat_upload_metadata")
    _run(engine, command.downgrade, PREVIOUS_REVISION)
    engine.dispose()
