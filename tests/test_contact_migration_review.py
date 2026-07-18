from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateIndex, CreateTable

from core.database import Base, ContactImportRun, ContactRecord, ContactSource
from migrations.versions.contact_authority_20260719_0004 import (
    CONTACT_REQUIRED_TABLES,
)
from src.contact_legacy_import import (
    ContactLegacyImportError,
    adopt_legacy_contacts,
)
from src.database_migrations import (
    SCHEMA_HEAD_REVISION,
    SchemaRevisionError,
    _alembic_config,
    _contact_kind_predicate_matches,
    schema_revision_status,
    upgrade_schema,
)
from src.identity import ensure_account


ROOT = Path(__file__).resolve().parent.parent
CONTACT_TABLES = tuple(sorted(CONTACT_REQUIRED_TABLES))


def _set_encryption_key(monkeypatch) -> None:
    from src import secret_storage

    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)


def _orm_foreign_keys(table) -> set[tuple[tuple[str, ...], str, tuple[str, ...], str]]:
    values = set()
    for constraint in table.foreign_key_constraints:
        elements = list(constraint.elements)
        values.add((
            tuple(element.parent.name for element in elements),
            elements[0].column.table.name,
            tuple(element.column.name for element in elements),
            str(constraint.ondelete or "").upper(),
        ))
    return values


def _reflected_foreign_keys(inspector, table_name: str):
    return {
        (
            tuple(value.get("constrained_columns") or ()),
            str(value.get("referred_table") or ""),
            tuple(value.get("referred_columns") or ()),
            str((value.get("options") or {}).get("ondelete") or "").upper(),
        )
        for value in inspector.get_foreign_keys(table_name)
    }


def _seed_contact_record(engine) -> str:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        account = ensure_account(db, "contact-owner")
        source = ContactSource(
            id="contact-source",
            owner_id=account.id,
            kind="local",
            label="Local contacts",
            enabled=True,
            sync_state="ready",
        )
        db.add(source)
        db.flush()
        record = ContactRecord(
            id="contact-record",
            owner_id=account.id,
            source_id=source.id,
            remote_uid="private-uid",
            remote_uid_digest="derived-by-listener",
            remote_href="/private/addressbook/private-uid.vcf",
            remote_etag='"private-etag"',
            payload={"name": "Private contact", "emails": ["p@example.test"]},
            raw_vcard="BEGIN:VCARD\nFN:Private contact\nEND:VCARD",
        )
        db.add(record)
        db.commit()
        return record.id
    finally:
        db.close()


def test_contact_partial_index_predicate_accepts_postgresql_reflection_only():
    accepted = (
        ("kind = 'local'", "local", "sqlite"),
        (text("kind = 'local'"), "local", "sqlite"),
        ("(kind = 'carddav')", "carddav", "sqlite"),
        ("((kind)::text = 'local'::text)", "local", "postgresql"),
        (
            "((kind)::text = ('carddav'::character varying(24))::text)",
            "carddav",
            "postgresql",
        ),
    )
    for predicate, kind, dialect in accepted:
        assert _contact_kind_predicate_matches(
            predicate, expected_kind=kind, dialect=dialect
        )

    rejected = (
        "kind <> 'local'",
        "kind = 'carddav'",
        "kind = 'local' OR true",
        "NOT (kind = 'local')",
        "kind IN ('local', 'carddav')",
    )
    for predicate in rejected:
        assert not _contact_kind_predicate_matches(
            predicate, expected_kind="local", dialect="postgresql"
        )


def test_contact_migration_matches_orm_and_compiles_for_postgresql(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'contact-parity.db'}")
    upgrade_schema(engine)
    schema = inspect(engine)

    for table_name in CONTACT_TABLES:
        table = Base.metadata.tables[table_name]
        reflected_columns = {
            str(column["name"]): bool(column["nullable"])
            for column in schema.get_columns(table_name)
        }
        orm_columns = {
            column.name: bool(column.nullable) for column in table.columns
        }
        assert reflected_columns == orm_columns
        reflected_types = {
            str(column["name"]): str(column["type"]).upper()
            for column in schema.get_columns(table_name)
        }
        orm_types = {
            column.name: column.type.compile(
                dialect=engine.dialect
            ).upper()
            for column in table.columns
        }
        assert reflected_types == orm_types

        reflected_indexes = {
            (
                str(index.get("name") or ""),
                tuple(index.get("column_names") or ()),
                bool(index.get("unique")),
            )
            for index in schema.get_indexes(table_name)
        }
        orm_indexes = {
            (
                str(index.name or ""),
                tuple(column.name for column in index.columns),
                bool(index.unique),
            )
            for index in table.indexes
        }
        assert reflected_indexes == orm_indexes

        reflected_uniques = {
            tuple(value.get("column_names") or ())
            for value in schema.get_unique_constraints(table_name)
        }
        orm_uniques = {
            tuple(column.name for column in value.columns)
            for value in table.constraints
            if value.__class__.__name__ == "UniqueConstraint"
        }
        assert reflected_uniques == orm_uniques

        reflected_checks = {
            str(value.get("name") or "")
            for value in schema.get_check_constraints(table_name)
        }
        orm_checks = {
            str(value.name or "")
            for value in table.constraints
            if value.__class__.__name__ == "CheckConstraint"
        }
        assert reflected_checks == orm_checks
        assert _reflected_foreign_keys(schema, table_name) == _orm_foreign_keys(table)

        str(CreateTable(table).compile(dialect=postgresql.dialect()))
        for index in table.indexes:
            str(CreateIndex(index).compile(dialect=postgresql.dialect()))

    environment = os.environ.copy()
    environment["DATABASE_URL"] = (
        "postgresql+psycopg://restia:unused@localhost/restia"
    )
    compiled = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            "20260718_0003:20260719_0004",
            "--sql",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr
    assert "CREATE TABLE contact_deliveries" in compiled.stdout
    assert "label TEXT" in compiled.stdout
    assert "label VARCHAR(160)" not in compiled.stdout
    assert "remote_etag TEXT" in compiled.stdout
    assert "remote_etag VARCHAR(512)" not in compiled.stdout
    assert (
        "CREATE UNIQUE INDEX uq_contact_sources_owner_local"
        in compiled.stdout
    )
    engine.dispose()


def test_head_validation_rejects_inverted_contact_partial_index(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'inverted-contact-index.db'}")
    upgrade_schema(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP INDEX uq_contact_sources_owner_local"))
        connection.execute(text(
            "CREATE UNIQUE INDEX uq_contact_sources_owner_local "
            "ON contact_sources(owner_id) WHERE kind = 'carddav'"
        ))
    with pytest.raises(
        SchemaRevisionError,
        match="one-local-source-per-owner uniqueness",
    ):
        upgrade_schema(engine)
    engine.dispose()


@pytest.mark.parametrize(
    ("column", "plaintext"),
    (
        ("remote_uid", "plaintext-private-uid"),
        ("remote_etag", '"plaintext-private-etag"'),
        ("payload", json.dumps({"private": "plaintext contact"})),
    ),
)
def test_head_validation_rejects_plaintext_contact_fields(
    tmp_path, monkeypatch, column, plaintext
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(
        f"sqlite:///{tmp_path / ('plaintext-' + column + '.db')}"
    )
    upgrade_schema(engine)
    record_id = _seed_contact_record(engine)
    assert column in {"remote_uid", "remote_etag", "payload"}
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE contact_records SET {column}=:value WHERE id=:id"),
            {"value": plaintext, "id": record_id},
        )
    with pytest.raises(SchemaRevisionError, match=column):
        upgrade_schema(engine)
    engine.dispose()


def test_head_validation_rejects_plaintext_contact_source_label(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'plaintext-label.db'}")
    upgrade_schema(engine)
    _seed_contact_record(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE contact_sources SET label='Private family address book' "
            "WHERE id='contact-source'"
        ))
    with pytest.raises(SchemaRevisionError, match="contact_sources.label"):
        upgrade_schema(engine)
    engine.dispose()


def test_head_validation_rejects_inconsistent_contact_uid_digest(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'bad-contact-digest.db'}")
    upgrade_schema(engine)
    record_id = _seed_contact_record(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE contact_records SET remote_uid_digest=:digest WHERE id=:id"
        ), {"digest": "0" * 64, "id": record_id})
    with pytest.raises(SchemaRevisionError, match="missing or inconsistent"):
        upgrade_schema(engine)
    engine.dispose()


def test_contact_downgrade_refuses_data_before_destructive_ddl(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    database_url = f"sqlite:///{tmp_path / 'contact-downgrade.db'}"
    engine = create_engine(database_url)
    upgrade_schema(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO accounts "
            "(id, username, status, auth_epoch, created_at, updated_at) "
            "VALUES ('owner', 'owner', 'active', 1, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO contact_sources "
            "(id, owner_id, kind, label, enabled, sync_state, "
            "config_version, version, created_at, updated_at) VALUES "
            "('source', 'owner', 'local', 'Local contacts', 1, 'ready', "
            "1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))

    config = _alembic_config(database_url)
    with pytest.raises(RuntimeError, match="export or remove"):
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "20260718_0003")

    assert schema_revision_status(engine).current_revisions == (
        "20260720_0005",
    )
    assert CONTACT_REQUIRED_TABLES <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM contact_sources WHERE id='source'"
        )).scalar_one() == 1
    engine.dispose()


def test_completed_legacy_import_pins_owner_and_ignores_bootstrap_env(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    import src.contact_legacy_import as legacy
    import src.carddav_contacts as carddav

    validated: list[str] = []

    def validate_url(value: object) -> str:
        cleaned = str(value)
        validated.append(cleaned)
        if cleaned == "invalid-after-cutover":
            raise ValueError("invalid")
        return cleaned.rstrip("/")

    monkeypatch.setattr(legacy, "validate_carddav_url", validate_url)
    monkeypatch.setattr(carddav, "validate_carddav_url", validate_url)
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-owner.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    alice = ensure_account(db, "alice")
    bob = ensure_account(db, "bob")
    db.commit()
    db.close()

    settings = tmp_path / "settings.json"
    contacts = tmp_path / "contacts.json"
    backups = tmp_path / "backups"
    settings.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    contacts.write_text(json.dumps({"contacts": [{
        "uid": "legacy", "name": "Legacy", "emails": [],
    }]}), encoding="utf-8")

    first = adopt_legacy_contacts(
        factory,
        settings_path=settings,
        contacts_path=contacts,
        backup_dir=backups,
        auth_enabled=True,
        primary_admin_resolver=lambda: "alice",
        environ={"CARDDAV_URL": "https://bootstrap.example/dav"},
    )
    second = adopt_legacy_contacts(
        factory,
        settings_path=settings,
        contacts_path=contacts,
        backup_dir=backups,
        auth_enabled=True,
        primary_admin_resolver=lambda: "bob",
        environ={"CARDDAV_URL": "invalid-after-cutover"},
    )
    contacts.unlink()
    third = adopt_legacy_contacts(
        factory,
        settings_path=settings,
        contacts_path=contacts,
        backup_dir=backups,
        auth_enabled=True,
        primary_admin_resolver=lambda: None,
        environ={"CARDDAV_URL": "invalid-after-cutover"},
    )

    assert first.owner_id == alice.id
    assert second.owner_id == alice.id
    assert third.owner_id == alice.id
    assert second.idempotent is True
    assert third.idempotent is True
    assert validated == [
        "https://bootstrap.example/dav",
        "https://bootstrap.example/dav",
    ]

    db = factory()
    try:
        assert db.query(ContactImportRun).one().owner_id == alice.id
        assert db.query(ContactRecord).filter(
            ContactRecord.owner_id == bob.id
        ).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_legacy_import_rejects_malformed_reserved_credential(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'malformed-credential.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    ensure_account(db, "alice")
    db.commit()
    db.close()

    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "carddav_password": "enc:not-a-fernet-envelope",
    }), encoding="utf-8")
    with pytest.raises(
        ContactLegacyImportError,
        match="credential cannot be decrypted",
    ):
        adopt_legacy_contacts(
            factory,
            settings_path=settings,
            contacts_path=tmp_path / "missing-contacts.json",
            backup_dir=tmp_path / "backups",
            auth_enabled=True,
            primary_admin_resolver=lambda: "alice",
            environ={},
        )

    db = factory()
    try:
        assert db.query(ContactImportRun).count() == 0
    finally:
        db.close()
        engine.dispose()
