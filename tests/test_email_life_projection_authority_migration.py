from __future__ import annotations

from alembic import command
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from core.database import Account, EmailLifeProjection, EmailLifeProjectionImportRun
from migrations.versions.email_life_projection_authority_20260726_0011 import (
    EMAIL_LIFE_PROJECTION_REQUIRED_COLUMNS,
    EMAIL_LIFE_PROJECTION_REQUIRED_TABLES,
)
from src.database_migrations import (
    SCHEMA_HEAD_REVISION,
    SchemaRevisionError,
    _alembic_config,
    schema_revision_status,
    upgrade_schema,
    validate_head_schema,
)
from src.email_life_projection_ledger import enqueue_email_life_headers


PREVIOUS_REVISION = "20260725_0010"


def _run(engine, operation, revision: str) -> None:
    config = _alembic_config(str(engine.url))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        operation(config, revision)


def test_0011_upgrade_is_executable_from_notification_head_and_downgrades_empty(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'projection-migration.db'}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, PREVIOUS_REVISION)
    assert schema_revision_status(engine).current_revisions == (
        PREVIOUS_REVISION,
    )
    assert not EMAIL_LIFE_PROJECTION_REQUIRED_TABLES.intersection(
        inspect(engine).get_table_names()
    )

    result = upgrade_schema(engine)
    assert result.current_revisions == (SCHEMA_HEAD_REVISION,)
    inspector = inspect(engine)
    assert EMAIL_LIFE_PROJECTION_REQUIRED_TABLES <= set(
        inspector.get_table_names()
    )
    for table_name, required in EMAIL_LIFE_PROJECTION_REQUIRED_COLUMNS.items():
        assert required <= {
            str(column["name"])
            for column in inspector.get_columns(table_name)
        }
    validate_head_schema(engine)

    _run(engine, command.downgrade, PREVIOUS_REVISION)
    assert schema_revision_status(engine).current_revisions == (
        PREVIOUS_REVISION,
    )
    assert not EMAIL_LIFE_PROJECTION_REQUIRED_TABLES.intersection(
        inspect(engine).get_table_names()
    )
    engine.dispose()


def test_head_validation_rejects_plaintext_projection_payload(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tampered-projection.db'}")
    upgrade_schema(engine)
    now = "2026-07-17 09:00:00"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO accounts(id, username, status, auth_epoch, "
            "created_at, updated_at) VALUES ("
            "'00000000-0000-4000-8000-000000001101', 'tamper-projection', "
            "'active', 1, :now, :now)"
        ), {"now": now})
        connection.execute(text(
            "INSERT INTO email_life_projection_ledger("
            "id, owner_id, account_key, folder, message_uid, header_sha256, "
            "payload, state, attempt_count, version, created_at, updated_at) "
            "VALUES ("
            "'00000000-0000-4000-8000-000000001102', "
            "'00000000-0000-4000-8000-000000001101', 'mail-a', 'INBOX', "
            "'1', :digest, :payload, 'pending', 0, 1, :now, :now)"
        ), {
            "digest": "a" * 64,
            "payload": '{"subject":"plaintext private subject"}',
            "now": now,
        })

    try:
        validate_head_schema(engine)
    except SchemaRevisionError as exc:
        assert "email_life_projection_ledger.payload" in str(exc)
    else:  # pragma: no cover - explicit privacy contract
        raise AssertionError("plaintext projection payload passed validation")
    engine.dispose()


def test_encrypted_runtime_row_validates_and_blocks_lossy_downgrade(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'retained-projection.db'}")
    upgrade_schema(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = factory()
    db.add(Account(
        id="00000000-0000-4000-8000-000000001111",
        username="projection-owner",
        status="active",
    ))
    db.commit()
    db.close()
    enqueue_email_life_headers(
        owner="projection-owner",
        account_key="mail-a",
        folder="INBOX",
        emails=[{
            "uid": "1",
            "message_id": "<private-thread@example.test>",
            "references": ["<private-root@example.test>"],
            "subject": "Private migration subject",
        }],
        session_factory=factory,
    )
    validate_head_schema(engine)

    try:
        _run(engine, command.downgrade, PREVIOUS_REVISION)
    except RuntimeError as exc:
        assert "projection authority contains durable handoff" in str(exc)
    else:  # pragma: no cover - explicit loss-averse contract
        raise AssertionError("retained projection was discarded by downgrade")
    # SQLite DDL is non-transactional: newer empty revisions are removed
    # before the loss-averse 0011 guard rejects its own downgrade. The durable
    # projection remains intact and the database can be upgraded back to head.
    assert schema_revision_status(engine).current_revisions == (
        "20260726_0011",
    )
    assert upgrade_schema(engine).current_revisions == (SCHEMA_HEAD_REVISION,)
    validate_head_schema(engine)
    engine.dispose()


def test_projection_models_compile_for_postgresql():
    ledger_ddl = str(CreateTable(EmailLifeProjection.__table__).compile(
        dialect=postgresql.dialect()
    ))
    import_ddl = str(CreateTable(EmailLifeProjectionImportRun.__table__).compile(
        dialect=postgresql.dialect()
    ))
    assert "FOREIGN KEY(owner_id) REFERENCES accounts (id) ON DELETE CASCADE" in ledger_ddl
    assert "UNIQUE (owner_id, account_key, folder, message_uid, header_sha256)" in ledger_ddl
    assert "FOREIGN KEY(owner_id) REFERENCES accounts (id) ON DELETE CASCADE" in import_ddl
    assert "UNIQUE (owner_id, source_kind, source_sha256)" in import_ddl
