"""Migration contract for the canonical Finance LifeEntity discriminator."""

from __future__ import annotations

from alembic import command
from cryptography.fernet import Fernet
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from core.database import LIFE_ENTITY_TYPES
from migrations.versions.finance_record_authority_20260723_0008 import (
    CURRENT_LIFE_ENTITY_TYPES,
    FINANCE_LIFE_ENTITY_TYPE,
    PREVIOUS_LIFE_ENTITY_TYPES,
)
from src.database_migrations import (
    SCHEMA_HEAD_REVISION,
    _alembic_config,
    schema_revision_status,
    validate_head_schema,
)


PREVIOUS_REVISION = "20260722_0007"


def _run(engine, operation, revision: str) -> None:
    config = _alembic_config(str(engine.url))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        operation(config, revision)


def _insert_life_entity(connection, *, entity_id: str, entity_type: str) -> None:
    connection.execute(text(
        "INSERT INTO life_entities "
        "(id, owner_id, entity_type, title, summary, status, properties, "
        "provenance, confidence, sensitivity, version, created_at, updated_at) "
        "VALUES (:id, 'finance-owner', :entity_type, 'encrypted-title', '', "
        "'active', '{}', '{}', 100, 'private', 1, CURRENT_TIMESTAMP, "
        "CURRENT_TIMESTAMP)"
    ), {"id": entity_id, "entity_type": entity_type})


def test_0008_widens_only_the_finance_discriminator_and_downgrade_is_loss_averse(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(f"sqlite:///{tmp_path / 'finance-migration.db'}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, PREVIOUS_REVISION)
    assert schema_revision_status(engine).current_revisions == (PREVIOUS_REVISION,)

    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO accounts "
            "(id, username, status, auth_epoch, created_at, updated_at) "
            "VALUES ('finance-owner', 'alice', 'active', 1, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)"
        ))
        _insert_life_entity(
            connection, entity_id="existing-person", entity_type="person"
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_life_entity(
                connection, entity_id="too-early", entity_type="finance_record"
            )

    _run(engine, command.upgrade, SCHEMA_HEAD_REVISION)
    assert schema_revision_status(engine).current_revisions == (SCHEMA_HEAD_REVISION,)
    validate_head_schema(engine)
    with engine.begin() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM life_entities WHERE id = 'existing-person'"
        )).scalar_one() == 1
        _insert_life_entity(
            connection, entity_id="finance-row", entity_type="finance_record"
        )

    with pytest.raises(RuntimeError, match="finance_record rows exist"):
        _run(engine, command.downgrade, PREVIOUS_REVISION)
    # SQLite DDL is non-transactional: newer empty revisions are removed before
    # the loss-averse 0008 guard rejects its own downgrade.
    assert schema_revision_status(engine).current_revisions == ("20260723_0008",)

    with engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM life_entities WHERE id = 'finance-row'"
        ))
    _run(engine, command.downgrade, PREVIOUS_REVISION)
    assert schema_revision_status(engine).current_revisions == (PREVIOUS_REVISION,)
    assert inspect(engine).has_table("life_entities")
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_life_entity(
                connection, entity_id="after-downgrade", entity_type="finance_record"
            )
    engine.dispose()


def test_finance_migration_entity_type_manifest_matches_orm():
    assert FINANCE_LIFE_ENTITY_TYPE == "finance_record"
    assert CURRENT_LIFE_ENTITY_TYPES == tuple(LIFE_ENTITY_TYPES)
    assert FINANCE_LIFE_ENTITY_TYPE not in PREVIOUS_LIFE_ENTITY_TYPES
