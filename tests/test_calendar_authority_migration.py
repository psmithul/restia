from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import bcrypt
from alembic import command
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError, OperationalError, StatementError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateIndex, CreateTable

from core.database import (
    Account,
    ActionProposal,
    Base,
    CalendarActionUndo,
    CalendarCal,
    CalendarDelivery,
    CalendarEvent,
    PlanningItem,
)
from migrations.versions.calendar_authority_20260720_0005 import (
    CALENDAR_REQUIRED_TABLES,
)
from src.database_migrations import (
    SCHEMA_HEAD_REVISION,
    SchemaRevisionError,
    _alembic_config,
    _stored_encrypted_json_envelope,
    schema_revision_status,
    upgrade_schema,
    validate_head_schema,
)
from src import database_runtime


ROOT = Path(__file__).resolve().parent.parent
CALENDAR_TABLES = tuple(sorted(CALENDAR_REQUIRED_TABLES))
PREVIOUS_REVISION = "20260719_0004"


def _set_encryption_key(monkeypatch) -> None:
    from src import secret_storage

    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)


def _run_alembic(engine, operation, revision: str) -> None:
    config = _alembic_config(str(engine.url))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        operation(config, revision)


def _upgrade_empty_to_previous(engine) -> None:
    _run_alembic(engine, command.stamp, "20260716_0001")
    _run_alembic(engine, command.upgrade, PREVIOUS_REVISION)
    assert schema_revision_status(engine).current_revisions == (
        PREVIOUS_REVISION,
    )


def _insert_legacy_account(connection, *, account_id: str, username: str) -> None:
    connection.execute(text(
        "INSERT INTO accounts "
        "(id, username, status, auth_epoch, created_at, updated_at) "
        "VALUES (:id, :username, 'active', 1, CURRENT_TIMESTAMP, "
        "CURRENT_TIMESTAMP)"
    ), {"id": account_id, "username": username})


def _insert_legacy_calendar(
    connection,
    *,
    calendar_id: str,
    owner: str | None,
    source: str = "local",
    account_id: str | None = None,
) -> None:
    connection.execute(text(
        "INSERT INTO calendars "
        "(id, owner, name, color, source, account_id, created_at, updated_at) "
        "VALUES (:id, :owner, 'Legacy calendar', '#123456', :source, "
        ":account_id, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    ), {
        "id": calendar_id,
        "owner": owner,
        "source": source,
        "account_id": account_id,
    })


def _insert_legacy_event(
    connection, *, uid: str, calendar_id: str
) -> None:
    connection.execute(text(
        "INSERT INTO calendar_events "
        "(uid, calendar_id, summary, dtstart, dtend, is_utc, created_at, "
        "updated_at) VALUES (:uid, :calendar_id, 'Legacy event', "
        "'2026-07-20 09:00:00', '2026-07-20 10:00:00', 0, "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    ), {"uid": uid, "calendar_id": calendar_id})


def _orm_foreign_keys(table):
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


def _seed_authority_rows(engine, *, include_undo: bool = True) -> dict[str, str]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        account = Account(
            id="calendar-owner-id",
            username="calendar-owner",
            status="active",
            auth_epoch=1,
        )
        proposal = ActionProposal(
            id="calendar-proposal-id",
            owner_id=account.id,
            domain="calendar",
            action="update_event",
            autonomy_level=4,
            state="completed",
            target_type="event",
            target_id="calendar-event-id",
            payload={"summary": "Private meeting"},
            reason="Private calendar action",
            sources={},
            external=False,
            requires_confirmation=False,
            result={"ok": True},
            undo_ref="calendar-action-undo:calendar-undo-id",
            version=1,
        )
        calendar = CalendarCal(
            id="calendar-id",
            owner_id=account.id,
            owner=account.username,
            name="Private calendar",
            source="caldav",
            account_id="caldav-connector-id",
            config_version=1,
        )
        event = CalendarEvent(
            uid="calendar-event-id",
            owner_id=account.id,
            calendar_id=calendar.id,
            summary="Private meeting",
            description="Private event description",
            dtstart=datetime(2026, 7, 20, 9, 0),
            dtend=datetime(2026, 7, 20, 10, 0),
            version=1,
        )
        db.add_all((account, proposal, calendar, event))
        db.flush()
        delivery = CalendarDelivery(
            id="calendar-delivery-id",
            owner_id=account.id,
            calendar_id=calendar.id,
            event_uid=event.uid,
            proposal_id=proposal.id,
            operation="update",
            idempotency_key="calendar-delivery-key",
            payload={
                "ical": "BEGIN:VCALENDAR\nSUMMARY:Private meeting\nEND:VCALENDAR",
                "etag": '"private-etag"',
            },
            expected_event_version=1,
            expected_config_version=1,
            state="pending",
            attempts=0,
            version=1,
        )
        db.add(delivery)
        if include_undo:
            db.add(CalendarActionUndo(
                id="calendar-undo-id",
                owner_id=account.id,
                proposal_id=proposal.id,
                event_uid=event.uid,
                operation="update",
                before_state={
                    "summary": "Private meeting before update",
                    "description": "Private before-state description",
                },
                result_event_version=1,
                life_entity_id=None,
                result_graph_version=None,
                created_link_ids={"ids": ["private-link-id"]},
                state="ready",
                version=1,
            ))
        db.commit()
        return {
            "account_id": account.id,
            "calendar_id": calendar.id,
            "event_uid": event.uid,
            "proposal_id": proposal.id,
        }
    finally:
        db.close()


def test_sqlite_upgrade_backfills_normalized_legacy_calendar_owners(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar-backfill.db'}")
    _upgrade_empty_to_previous(engine)
    with engine.begin() as connection:
        _insert_legacy_account(
            connection, account_id="stable-account-id", username="alice"
        )
        _insert_legacy_calendar(
            connection, calendar_id="legacy-calendar", owner="  ALICE  "
        )
        _insert_legacy_event(
            connection, uid="legacy-event", calendar_id="legacy-calendar"
        )
        _insert_legacy_calendar(
            connection, calendar_id="ownerless-calendar", owner=None
        )
        _insert_legacy_event(
            connection, uid="ownerless-event", calendar_id="ownerless-calendar"
        )

    upgrade_schema(engine)
    assert schema_revision_status(engine).current_revisions == (
        SCHEMA_HEAD_REVISION,
    )
    with engine.connect() as connection:
        calendar = connection.execute(text(
            "SELECT owner_id, owner, account_id, config_version "
            "FROM calendars WHERE id='legacy-calendar'"
        )).one()
        event = connection.execute(text(
            "SELECT owner_id, version FROM calendar_events "
            "WHERE uid='legacy-event'"
        )).one()
        ownerless = connection.execute(text(
            "SELECT c.owner_id, e.owner_id FROM calendars AS c "
            "JOIN calendar_events AS e ON e.calendar_id=c.id "
            "WHERE c.id='ownerless-calendar'"
        )).one()
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    assert calendar == ("stable-account-id", "  ALICE  ", None, 1)
    assert event == ("stable-account-id", 1)
    assert ownerless == ("stable-account-id", "stable-account-id")
    engine.dispose()


@pytest.mark.parametrize(
    ("accounts", "legacy_owner"),
    (
        ((("account-a", "alice"),), "missing-owner"),
        (
            (("account-a", "alice"), ("account-b", "ALICE")),
            "Alice",
        ),
        ((), None),
        ((("account-a", "alice"), ("account-b", "bob")), None),
    ),
)
def test_calendar_owner_backfill_fails_closed_before_schema_mutation(
    tmp_path, monkeypatch, accounts, legacy_owner
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(
        f"sqlite:///{tmp_path / ('calendar-owner-' + str(len(accounts)) + '-' + str(legacy_owner) + '.db')}"
    )
    _upgrade_empty_to_previous(engine)
    with engine.begin() as connection:
        for account_id, username in accounts:
            _insert_legacy_account(
                connection, account_id=account_id, username=username
            )
        _insert_legacy_calendar(
            connection, calendar_id="legacy-calendar", owner=legacy_owner
        )
    with pytest.raises(RuntimeError, match="exactly one Account.id"):
        upgrade_schema(engine)

    assert schema_revision_status(engine).current_revisions == (
        PREVIOUS_REVISION,
    )
    columns = {
        str(value["name"])
        for value in inspect(engine).get_columns("calendars")
    }
    assert "owner_id" not in columns
    assert "config_version" not in columns
    engine.dispose()


def test_calendar_migration_matches_orm_and_postgresql_ddl(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar-parity.db'}")
    upgrade_schema(engine)
    schema = inspect(engine)

    for table_name in CALENDAR_TABLES:
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
            column.name: column.type.compile(dialect=engine.dialect).upper()
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

        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "JSON" in ddl or table_name in {"calendars", "calendar_events"}
        for index in table.indexes:
            str(CreateIndex(index).compile(dialect=postgresql.dialect()))

    proposal_indexes = {
        (
            str(index.get("name") or ""),
            tuple(index.get("column_names") or ()),
            bool(index.get("unique")),
        )
        for index in schema.get_indexes("action_proposals")
    }
    assert (
        "uq_action_proposals_id_owner", ("id", "owner_id"), True
    ) in proposal_indexes
    assert tuple(
        schema.get_pk_constraint("calendar_events").get(
            "constrained_columns"
        ) or ()
    ) == ("uid", "owner_id")
    assert (
        ("calendar_event_uid", "calendar_id"),
        "calendar_events",
        ("uid", "calendar_id"),
        "SET NULL",
    ) in _reflected_foreign_keys(schema, "planning_items")
    assert ("calendar_event_uid", "calendar_id") in {
        tuple(value.get("column_names") or ())
        for value in schema.get_unique_constraints("planning_items")
    }
    str(CreateTable(PlanningItem.__table__).compile(
        dialect=postgresql.dialect()
    ))
    engine.dispose()


def test_calendar_migration_refuses_unsafe_offline_postgresql_sql(monkeypatch):
    _set_encryption_key(monkeypatch)
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
            f"{PREVIOUS_REVISION}:{SCHEMA_HEAD_REVISION}",
            "--sql",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert compiled.returncode != 0
    assert "requires an online migration" in compiled.stderr


def test_calendar_owner_composite_fk_rejects_cross_account_event(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar-owner-fk.db'}")
    upgrade_schema(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        db.add_all((
            Account(id="account-a", username="alice", status="active", auth_epoch=1),
            Account(id="account-b", username="bob", status="active", auth_epoch=1),
        ))
        db.add(CalendarCal(
            id="alice-calendar", owner_id="account-a", owner="alice", name="A"
        ))
        db.flush()
        db.add(CalendarEvent(
            uid="cross-owner-event",
            owner_id="account-b",
            calendar_id="alice-calendar",
            summary="Must fail",
            dtstart=datetime.utcnow(),
            dtend=datetime.utcnow() + timedelta(hours=1),
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()
        engine.dispose()


def test_calendar_event_uid_identity_is_scoped_per_owner(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar-shared-uid.db'}")
    upgrade_schema(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        db.add_all((
            Account(id="account-a", username="alice", status="active", auth_epoch=1),
            Account(id="account-b", username="bob", status="active", auth_epoch=1),
            CalendarCal(
                id="alice-calendar", owner_id="account-a", owner="alice", name="A"
            ),
            CalendarCal(
                id="bob-calendar", owner_id="account-b", owner="bob", name="B"
            ),
        ))
        db.flush()
        for owner_id, calendar_id in (
            ("account-a", "alice-calendar"),
            ("account-b", "bob-calendar"),
        ):
            db.add(CalendarEvent(
                uid="shared-rfc-uid@example.test",
                owner_id=owner_id,
                calendar_id=calendar_id,
                summary=f"Owned by {owner_id}",
                dtstart=datetime(2026, 7, 20, 9, 0),
                dtend=datetime(2026, 7, 20, 10, 0),
            ))
        db.commit()
        rows = db.query(CalendarEvent).filter(
            CalendarEvent.uid == "shared-rfc-uid@example.test"
        ).all()
        assert {row.owner_id for row in rows} == {"account-a", "account-b"}
    finally:
        db.close()
        engine.dispose()


def test_calendar_private_json_is_physically_encrypted_and_validated(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar-encryption.db'}")
    upgrade_schema(engine)
    _seed_authority_rows(engine)

    with engine.connect() as connection:
        before_state, created_link_ids = connection.execute(text(
            "SELECT before_state, created_link_ids FROM calendar_action_undos"
        )).one()
        (payload,) = connection.execute(text(
            "SELECT payload FROM calendar_deliveries"
        )).one()
    for stored in (before_state, created_link_ids, payload):
        envelope = _stored_encrypted_json_envelope(stored, dialect="sqlite")
        assert envelope is not None
        assert envelope.startswith("enc:c1:")
        assert "Private" not in str(stored)
    validate_head_schema(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE calendar_deliveries SET payload=:payload "
            "WHERE id='calendar-delivery-id'"
        ), {"payload": json.dumps({"ical": "plaintext-private-calendar"})})
    with pytest.raises(
        SchemaRevisionError, match="calendar_deliveries.payload"
    ):
        upgrade_schema(engine)
    engine.dispose()


def test_calendar_undo_created_link_ids_must_be_an_encrypted_object(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar-link-shape.db'}")
    upgrade_schema(engine)
    _seed_authority_rows(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        undo = db.query(CalendarActionUndo).one()
        # EncryptedJSON itself enforces the object-only contract, so the
        # calendar executor must use {"ids": [...]} rather than a raw list.
        undo.created_link_ids = ["raw-list-is-not-the-contract"]
        with pytest.raises(
            StatementError, match="encrypted JSON value must be an object"
        ):
            db.commit()
        db.rollback()
    finally:
        db.close()
    validate_head_schema(engine)
    engine.dispose()


def test_guarded_legacy_adoption_repairs_calendar_authority_before_stamp(
    tmp_path, monkeypatch
):
    import core.database as database

    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar-adoption.db'}")
    _upgrade_empty_to_previous(engine)
    with engine.begin() as connection:
        _insert_legacy_account(
            connection, account_id="legacy-account", username="legacy-owner"
        )
        _insert_legacy_calendar(
            connection, calendar_id="legacy-calendar", owner="legacy-owner"
        )
        _insert_legacy_event(
            connection, uid="legacy-event", calendar_id="legacy-calendar"
        )

    # This is the ordering used by guarded pre-Alembic adoption: create_all
    # adds missing new tables but cannot alter the two existing calendar tables.
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", engine)
    database._migrate_calendar_authority()
    _run_alembic(engine, command.stamp, SCHEMA_HEAD_REVISION)
    validate_head_schema(engine)

    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT owner_id FROM calendars WHERE id='legacy-calendar'"
        )).scalar_one() == "legacy-account"
        assert connection.execute(text(
            "SELECT owner_id FROM calendar_events WHERE uid='legacy-event'"
        )).scalar_one() == "legacy-account"
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_true_pre_alembic_calendar_shape_is_repaired_without_data_loss(
    tmp_path, monkeypatch
):
    import core.database as database

    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'true-legacy-calendar.db'}")
    Account.__table__.create(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("""
            CREATE TABLE calendars (
                id VARCHAR NOT NULL PRIMARY KEY,
                owner VARCHAR,
                name VARCHAR NOT NULL,
                color VARCHAR,
                source VARCHAR,
                account_id VARCHAR,
                caldav_base_url VARCHAR,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """)
        connection.exec_driver_sql("""
            CREATE TABLE calendar_events (
                uid VARCHAR NOT NULL PRIMARY KEY,
                calendar_id VARCHAR NOT NULL,
                summary VARCHAR NOT NULL,
                description TEXT,
                location VARCHAR,
                dtstart DATETIME NOT NULL,
                dtend DATETIME NOT NULL,
                all_day BOOLEAN,
                is_utc BOOLEAN NOT NULL,
                rrule VARCHAR,
                recurrence_exdates TEXT,
                color VARCHAR,
                status VARCHAR,
                importance VARCHAR,
                event_type VARCHAR,
                last_pinged DATETIME,
                origin VARCHAR,
                remote_href VARCHAR,
                remote_etag VARCHAR,
                caldav_sync_pending VARCHAR,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(calendar_id) REFERENCES calendars(id)
            )
        """)
        _insert_legacy_account(
            connection, account_id="only-active-account", username="only-owner"
        )
        _insert_legacy_calendar(
            connection, calendar_id="ownerless-calendar", owner=None
        )
        _insert_legacy_event(
            connection, uid="legacy-event", calendar_id="ownerless-calendar"
        )

    # Match pre-Alembic initialization exactly for this domain: create_all adds
    # missing V3 tables but leaves these two legacy tables untouched.
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", engine)
    database._migrate_calendar_authority()

    schema = inspect(engine)
    assert {
        "owner_id", "config_version"
    } <= {str(column["name"]) for column in schema.get_columns("calendars")}
    assert {
        "owner_id", "version"
    } <= {
        str(column["name"])
        for column in schema.get_columns("calendar_events")
    }
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT owner_id FROM calendars WHERE id='ownerless-calendar'"
        )).scalar_one() == "only-active-account"
        assert connection.execute(text(
            "SELECT summary, owner_id FROM calendar_events "
            "WHERE uid='legacy-event'"
        )).one() == ("Legacy event", "only-active-account")
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_unstamped_v21_calendar_retry_imports_auth_before_owner_backfill(
    tmp_path, monkeypatch
):
    """A partial create_all retry must adopt the preserved principal first."""

    import core.database as database
    from src import constants as runtime_constants
    from src import secret_storage

    _set_encryption_key(monkeypatch)
    database_path = tmp_path / "unstamped-v21-calendar.db"
    engine = create_engine(f"sqlite:///{database_path}")
    _upgrade_empty_to_previous(engine)
    with engine.begin() as connection:
        _insert_legacy_calendar(
            connection,
            calendar_id="legacy-calendar",
            owner="remote@example.test",
            source="caldav",
            account_id="caldav-connector-id",
        )
        _insert_legacy_event(
            connection, uid="legacy-event", calendar_id="legacy-calendar"
        )
        connection.execute(text("DROP TABLE alembic_version"))
        assert connection.execute(text("SELECT COUNT(*) FROM accounts")).scalar() == 0

    # Reproduce the state left by the old failed adoption: create_all added V3
    # composite-FK children but could not alter the V2.1 calendar parents.
    Base.metadata.create_all(engine)
    with engine.connect() as connection:
        with pytest.raises(OperationalError, match="foreign key mismatch"):
            connection.exec_driver_sql("PRAGMA foreign_key_check").all()

    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    auth_raw = json.dumps({
        "users": {
            "legacy-owner": {
                "password_hash": bcrypt.hashpw(
                    b"legacy-password", bcrypt.gensalt(rounds=4)
                ).decode("ascii"),
                "created": 1_700_000_000,
                "is_admin": True,
            }
        },
        "signup_enabled": False,
        "retired_usernames": [],
    }).encode("utf-8")
    auth_path.write_bytes(auth_raw)
    preferences_path = tmp_path / "user_prefs.json"
    preferences_path.write_text(json.dumps({
        "_users": {
            "legacy-owner": {
                "caldav_accounts": [{
                    "id": "caldav-connector-id",
                    "username": "remote@example.test",
                    "password": "preserved-but-not-read-by-owner-migration",
                }],
            }
        }
    }), encoding="utf-8")

    monkeypatch.setattr(runtime_constants, "AUTH_FILE", str(auth_path))
    monkeypatch.setattr(runtime_constants, "SESSIONS_FILE", str(sessions_path))
    monkeypatch.setattr(
        runtime_constants, "USER_PREFS_FILE", str(preferences_path)
    )
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setattr(
        database, "SETTINGS_FILE", str(tmp_path / "missing-settings.json")
    )
    monkeypatch.setattr(database_runtime, "_initialized_binding", None)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "local-single")
    monkeypatch.setattr(secret_storage, "_fernet", None)

    database_runtime.initialize_database()

    assert schema_revision_status(engine).current_revisions == (
        SCHEMA_HEAD_REVISION,
    )
    schema = inspect(engine)
    assert {
        "calendar_action_undos",
        "calendar_deliveries",
        "email_outbound_drafts",
        "email_outbound_deliveries",
    } <= set(schema.get_table_names())
    with engine.connect() as connection:
        account = connection.execute(text(
            "SELECT a.id, a.username, i.subject "
            "FROM accounts AS a JOIN auth_identities AS i ON i.account_id=a.id"
        )).one()
        assert account[1:] == ("legacy-owner", "legacy-owner")
        assert connection.execute(text(
            "SELECT owner_id FROM calendars WHERE id='legacy-calendar'"
        )).scalar_one() == account.id
        assert connection.execute(text(
            "SELECT owner_id FROM calendar_events WHERE uid='legacy-event'"
        )).scalar_one() == account.id
        assert connection.execute(text(
            "SELECT state FROM auth_import_runs "
            "WHERE source_kind='legacy-auth-json-v1'"
        )).scalar_one() == "completed"
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    assert auth_path.read_bytes() == auth_raw
    assert len(list((tmp_path / "legacy-auth-backups").glob("auth.*.json.bak"))) == 1
    validate_head_schema(engine)
    engine.dispose()


def test_legacy_repair_restores_sqlite_foreign_keys_after_failure(
    tmp_path, monkeypatch
):
    import core.database as database
    from migrations.versions import calendar_authority_20260720_0005 as revision

    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-fk-restore.db'}")
    _upgrade_empty_to_previous(engine)
    with engine.begin() as connection:
        _insert_legacy_account(
            connection, account_id="legacy-account", username="legacy-owner"
        )
        _insert_legacy_calendar(
            connection, calendar_id="legacy-calendar", owner="legacy-owner"
        )
    Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "engine", engine)

    def injected_failure(*, manage_sqlite_foreign_keys=True):
        assert manage_sqlite_foreign_keys is False
        assert revision.op.get_bind().exec_driver_sql(
            "PRAGMA foreign_keys"
        ).scalar() == 0
        raise RuntimeError("injected calendar batch failure")

    monkeypatch.setattr(revision, "_upgrade_existing_tables", injected_failure)
    with pytest.raises(RuntimeError, match="injected calendar batch failure"):
        database._migrate_calendar_authority()
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    engine.dispose()


def test_calendar_downgrade_refuses_authority_data_before_destructive_ddl(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar-downgrade-data.db'}")
    upgrade_schema(engine)
    _seed_authority_rows(engine)

    with pytest.raises(RuntimeError, match="export or resolve"):
        _run_alembic(engine, command.downgrade, PREVIOUS_REVISION)

    assert schema_revision_status(engine).current_revisions == (
        "20260720_0005",
    )
    assert CALENDAR_REQUIRED_TABLES <= set(inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM calendar_action_undos"
        )).scalar_one() == 1
    engine.dispose()


def test_calendar_downgrade_refuses_new_only_versions(tmp_path, monkeypatch):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar-downgrade-version.db'}")
    upgrade_schema(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    try:
        db.add(Account(
            id="calendar-owner-id", username="calendar-owner",
            status="active", auth_epoch=1,
        ))
        db.add(CalendarCal(
            id="calendar-id", owner_id="calendar-owner-id",
            owner="calendar-owner", name="Calendar", config_version=2,
        ))
        db.commit()
    finally:
        db.close()

    with pytest.raises(RuntimeError, match="cannot be represented safely"):
        _run_alembic(engine, command.downgrade, PREVIOUS_REVISION)
    assert schema_revision_status(engine).current_revisions == (
        "20260720_0005",
    )
    engine.dispose()


def test_empty_calendar_authority_can_downgrade_without_partial_schema(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar-downgrade-empty.db'}")
    upgrade_schema(engine)
    _run_alembic(engine, command.downgrade, PREVIOUS_REVISION)

    assert schema_revision_status(engine).current_revisions == (
        PREVIOUS_REVISION,
    )
    schema = inspect(engine)
    assert "calendar_action_undos" not in schema.get_table_names()
    assert "calendar_deliveries" not in schema.get_table_names()
    assert "owner_id" not in {
        str(column["name"]) for column in schema.get_columns("calendars")
    }
    assert "version" not in {
        str(column["name"])
        for column in schema.get_columns("calendar_events")
    }
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_calendar_upgrade_downgrade_roundtrip_preserves_legacy_rows(
    tmp_path, monkeypatch
):
    _set_encryption_key(monkeypatch)
    engine = create_engine(f"sqlite:///{tmp_path / 'calendar-roundtrip.db'}")
    _upgrade_empty_to_previous(engine)
    with engine.begin() as connection:
        _insert_legacy_account(
            connection, account_id="stable-account", username="alice"
        )
        _insert_legacy_calendar(
            connection, calendar_id="legacy-calendar", owner="Alice"
        )
        _insert_legacy_event(
            connection, uid="legacy-event", calendar_id="legacy-calendar"
        )
        before_calendar = connection.execute(text(
            "SELECT id, owner, name, color, source, account_id, caldav_base_url "
            "FROM calendars WHERE id='legacy-calendar'"
        )).one()
        before_event = connection.execute(text(
            "SELECT uid, calendar_id, summary, dtstart, dtend, is_utc "
            "FROM calendar_events WHERE uid='legacy-event'"
        )).one()

    upgrade_schema(engine)
    _run_alembic(engine, command.downgrade, PREVIOUS_REVISION)
    with engine.connect() as connection:
        after_calendar = connection.execute(text(
            "SELECT id, owner, name, color, source, account_id, caldav_base_url "
            "FROM calendars WHERE id='legacy-calendar'"
        )).one()
        after_event = connection.execute(text(
            "SELECT uid, calendar_id, summary, dtstart, dtend, is_utc "
            "FROM calendar_events WHERE uid='legacy-event'"
        )).one()
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    assert after_calendar == before_calendar
    assert after_event == before_event
    engine.dispose()
