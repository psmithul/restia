from __future__ import annotations

import uuid

from alembic import command
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from core.database import (
    Account,
    EmailAccount,
    EmailOutboundDelivery,
    EmailOutboundDraft,
)
from migrations.versions.email_outbound_authority_20260722_0007 import (
    EMAIL_OUTBOUND_REQUIRED_TABLES,
)
from src.database_migrations import (
    SCHEMA_HEAD_REVISION,
    _alembic_config,
    schema_revision_status,
    upgrade_schema,
    validate_head_schema,
)
from src.email_outbound import prepare_agent_email_action


PREVIOUS_REVISION = "20260721_0006"


def _run(engine, operation, revision: str) -> None:
    config = _alembic_config(str(engine.url))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        operation(config, revision)


def test_0007_upgrade_is_executable_from_telegram_head_and_downgrades(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    _run(engine, command.stamp, "20260716_0001")
    _run(engine, command.upgrade, PREVIOUS_REVISION)
    assert schema_revision_status(engine).current_revisions == (
        PREVIOUS_REVISION,
    )
    assert not EMAIL_OUTBOUND_REQUIRED_TABLES.intersection(
        inspect(engine).get_table_names()
    )

    result = upgrade_schema(engine)
    assert result.current_revisions == (SCHEMA_HEAD_REVISION,)
    assert EMAIL_OUTBOUND_REQUIRED_TABLES.issubset(
        set(inspect(engine).get_table_names())
    )

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        owner = Account(id=str(uuid.uuid4()), username="alice")
        mailbox = EmailAccount(
            id="mail-alice",
            owner="alice",
            name="Alice Mail",
            enabled=True,
            is_default=True,
            imap_user="alice@example.test",
            smtp_user="alice@example.test",
            from_address="alice@example.test",
        )
        db.add_all([owner, mailbox])
        db.commit()
        prepare_agent_email_action(
            db,
            owner_username="alice",
            email_account_id="mail-alice",
            to="ada@example.test",
            subject="Migration review",
            body="Encrypted exact body",
            idempotency_key="migration-email-action",
        )
        db.commit()
    finally:
        db.close()

    validate_head_schema(engine)
    _run(engine, command.downgrade, PREVIOUS_REVISION)
    assert schema_revision_status(engine).current_revisions == (
        PREVIOUS_REVISION,
    )
    assert not EMAIL_OUTBOUND_REQUIRED_TABLES.intersection(
        inspect(engine).get_table_names()
    )
    engine.dispose()


def test_email_outbound_models_compile_for_postgresql():
    draft_ddl = str(CreateTable(EmailOutboundDraft.__table__).compile(
        dialect=postgresql.dialect()
    ))
    delivery_ddl = str(CreateTable(EmailOutboundDelivery.__table__).compile(
        dialect=postgresql.dialect()
    ))
    assert "FOREIGN KEY(proposal_id, owner_id)" in draft_ddl
    assert "REFERENCES action_proposals (id, owner_id)" in draft_ddl
    assert "FOREIGN KEY(draft_id, owner_id)" in delivery_ddl
    assert "REFERENCES email_outbound_drafts (id, owner_id)" in delivery_ddl
    assert "FOREIGN KEY(proposal_id, owner_id)" in delivery_ddl
    assert "REFERENCES action_proposals (id, owner_id)" in delivery_ddl
