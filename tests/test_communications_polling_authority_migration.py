from __future__ import annotations

import json
import uuid

import pytest
from alembic import command
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from migrations.versions.communications_polling_authority_20260802_0018 import (
    COMMUNICATION_POLLING_REQUIRED_COLUMNS,
    COMMUNICATION_POLLING_REQUIRED_TABLES,
)
from src.database_migrations import (
    SCHEMA_HEAD_REVISION,
    SchemaRevisionError,
    _alembic_config,
    schema_revision_status,
    validate_head_schema,
)
from src.identity import ensure_account
from src.profile_configuration_service import put_configuration


PREVIOUS_REVISION = "20260801_0017"


def _run(engine, operation, revision: str) -> None:
    config = _alembic_config(str(engine.url))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        operation(config, revision)


def test_communications_polling_is_current_head_and_round_trips(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'communications-polling.db'}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, PREVIOUS_REVISION)
    assert not (
        COMMUNICATION_POLLING_REQUIRED_TABLES & set(inspect(engine).get_table_names())
    )

    _run(engine, command.upgrade, SCHEMA_HEAD_REVISION)
    inspector = inspect(engine)
    assert SCHEMA_HEAD_REVISION == "20260802_0018"
    assert COMMUNICATION_POLLING_REQUIRED_TABLES <= set(inspector.get_table_names())
    for table_name, required in COMMUNICATION_POLLING_REQUIRED_COLUMNS.items():
        assert required <= {
            str(column["name"]) for column in inspector.get_columns(table_name)
        }
    validate_head_schema(engine)
    assert schema_revision_status(engine).current_revisions == (SCHEMA_HEAD_REVISION,)

    _run(engine, command.downgrade, PREVIOUS_REVISION)
    assert not (
        COMMUNICATION_POLLING_REQUIRED_TABLES & set(inspect(engine).get_table_names())
    )
    engine.dispose()


def test_head_validation_rejects_plaintext_provider_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(secret_storage, "_digest_key", None)
    engine = create_engine(f"sqlite:///{tmp_path / 'plaintext-cursor.db'}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, SCHEMA_HEAD_REVISION)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        account = ensure_account(db, "owner")
        configuration = put_configuration(
            db,
            account=account,
            namespace="integration",
            key="slack",
            value={
                "id": "slack",
                "preset": "slack",
                "name": "Slack",
                "base_url": "https://slack.com",
                "auth_type": "bearer",
                "api_key": "private-token",
                "enabled": True,
                "permissions": {
                    "allowed_methods": ["GET"],
                    "allowed_path_prefixes": ["/api"],
                    "require_action_approval_for_writes": True,
                },
            },
            source="domain_service",
        ).record
        db.commit()
        owner_id = account.id
        configuration_id = configuration.id
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO communication_poll_states "
            "(id, owner_id, configuration_id, provider, cursor, state, "
            "consecutive_failures, imported_count, version, created_at, updated_at) "
            "VALUES (?, ?, ?, 'slack', ?, 'healthy', 0, 0, 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (
                str(uuid.uuid4()), owner_id, configuration_id,
                json.dumps({"channels": {"private-channel": "1.0"}}),
            ),
        )
    with pytest.raises(SchemaRevisionError, match="cursor is plaintext"):
        validate_head_schema(engine)
    engine.dispose()
