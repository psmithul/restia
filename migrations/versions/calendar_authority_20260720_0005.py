"""Add principal-owned calendar authority, undo, and delivery outbox.

Revision ID: 20260720_0005
Revises: 20260719_0004

The revision is online-only because legacy username ownership must resolve to
exactly one immutable Account.id before any table is rebuilt. Private undo and
delivery content is represented physically as JSON and is encrypted by the ORM
content envelope before insertion.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260720_0005"
down_revision: Union[str, Sequence[str], None] = "20260719_0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Calendar routes used this reserved pre-profile owner before unified accounts
# existed. It can never be a real login subject. Treat it like an ownerless row
# only when the preserved auth authority resolves to exactly one active account.
LEGACY_LOCAL_CALENDAR_OWNER = "owner@localhost"


CALENDAR_REQUIRED_TABLES = frozenset({
    "calendars", "calendar_events", "calendar_action_undos",
    "calendar_deliveries",
})

CALENDAR_REQUIRED_COLUMNS = {
    "calendars": frozenset({
        "id", "owner_id", "owner", "name", "color", "source",
        "account_id", "caldav_base_url", "config_version", "created_at",
        "updated_at",
    }),
    "calendar_events": frozenset({
        "uid", "owner_id", "calendar_id", "summary", "description",
        "location", "dtstart", "dtend", "all_day", "is_utc", "rrule",
        "recurrence_exdates", "color", "status", "importance",
        "event_type", "last_pinged", "origin", "remote_href",
        "remote_etag", "caldav_sync_pending", "version", "created_at",
        "updated_at",
    }),
    "calendar_action_undos": frozenset({
        "id", "owner_id", "proposal_id", "event_uid", "operation",
        "before_state", "result_event_version", "life_entity_id",
        "result_graph_version", "created_link_ids", "state", "used_at",
        "version", "created_at", "updated_at",
    }),
    "calendar_deliveries": frozenset({
        "id", "owner_id", "calendar_id", "event_uid", "proposal_id",
        "operation", "idempotency_key", "payload",
        "expected_event_version", "expected_config_version", "state",
        "attempts", "next_attempt_at", "claim_token", "claimed_at",
        "lease_expires_at", "completed_at", "last_error_code", "version",
        "created_at", "updated_at",
    }),
}


_SQLITE_NAMING_CONVENTION = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
}


def _require_online_execution() -> None:
    if bool(getattr(op.get_context(), "as_sql", False)):
        raise RuntimeError(
            "Revision 20260720_0005 requires an online migration so legacy "
            "calendar owners can be resolved and verified before backfill"
        )


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def _preflight_owner_mapping() -> None:
    """Refuse missing, ambiguous, or dangling legacy calendar ownership."""

    bind = op.get_bind()
    invalid_calendar = bind.execute(sa.text("""
        SELECT c.id
        FROM calendars AS c
        LEFT JOIN accounts AS a
          ON (
              trim(COALESCE(c.owner, '')) <> ''
              AND lower(trim(c.owner)) <> :legacy_local_owner
              AND lower(trim(a.username)) = lower(trim(c.owner))
          ) OR (
              (
                  trim(COALESCE(c.owner, '')) = ''
                  OR lower(trim(c.owner)) = :legacy_local_owner
              )
              AND a.status = 'active'
          )
        GROUP BY c.id, c.owner
        HAVING COUNT(a.id) <> 1
        LIMIT 1
    """), {"legacy_local_owner": LEGACY_LOCAL_CALENDAR_OWNER}).first()
    if invalid_calendar is not None:
        raise RuntimeError(
            "Every legacy calendar owner must resolve to exactly one Account.id"
        )

    dangling_event = bind.execute(sa.text("""
        SELECT e.uid
        FROM calendar_events AS e
        LEFT JOIN calendars AS c ON c.id = e.calendar_id
        WHERE c.id IS NULL
        LIMIT 1
    """)).first()
    if dangling_event is not None:
        raise RuntimeError(
            "Every calendar event must reference an existing owned calendar"
        )

    invalid_planning_link = bind.execute(sa.text("""
        SELECT p.id
        FROM planning_items AS p
        LEFT JOIN calendar_events AS e ON e.uid = p.calendar_event_uid
        WHERE p.calendar_event_uid IS NOT NULL
          AND (
              e.uid IS NULL
              OR p.calendar_id IS NULL
              OR p.calendar_id <> e.calendar_id
          )
        LIMIT 1
    """)).first()
    if invalid_planning_link is not None:
        raise RuntimeError(
            "Every linked planning item must reference its event calendar"
        )


def _backfill_owner_ids() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("""
        UPDATE calendars
        SET owner_id = (
            SELECT MIN(a.id)
            FROM accounts AS a
            WHERE (
                trim(COALESCE(calendars.owner, '')) <> ''
                AND lower(trim(calendars.owner)) <> :legacy_local_owner
                AND lower(trim(a.username)) = lower(trim(calendars.owner))
            ) OR (
                (
                    trim(COALESCE(calendars.owner, '')) = ''
                    OR lower(trim(calendars.owner)) = :legacy_local_owner
                )
                AND a.status = 'active'
            )
        )
    """), {"legacy_local_owner": LEGACY_LOCAL_CALENDAR_OWNER})
    bind.execute(sa.text("""
        UPDATE calendar_events
        SET owner_id = (
            SELECT c.owner_id
            FROM calendars AS c
            WHERE c.id = calendar_events.calendar_id
        )
    """))
    missing = bind.execute(sa.text("""
        SELECT 1 FROM calendars WHERE owner_id IS NULL
        UNION ALL
        SELECT 1 FROM calendar_events WHERE owner_id IS NULL
        LIMIT 1
    """)).first()
    if missing is not None:
        raise RuntimeError("Calendar Account.id ownership backfill was incomplete")


def _existing_calendar_event_fk_name() -> str:
    foreign_keys = sa.inspect(op.get_bind()).get_foreign_keys("calendar_events")
    matches = [
        value for value in foreign_keys
        if tuple(value.get("constrained_columns") or ()) == ("calendar_id",)
        and str(value.get("referred_table") or "") == "calendars"
        and tuple(value.get("referred_columns") or ()) == ("id",)
    ]
    if len(matches) != 1 or not matches[0].get("name"):
        raise RuntimeError(
            "Expected exactly one named legacy calendar_events calendar foreign key"
        )
    return str(matches[0]["name"])


def _enable_sqlite_foreign_keys(enabled: bool) -> None:
    context = op.get_context()
    with context.autocommit_block():
        op.get_bind().exec_driver_sql(
            "PRAGMA foreign_keys=" + ("ON" if enabled else "OFF")
        )


def _upgrade_existing_tables(*, manage_sqlite_foreign_keys: bool = True) -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        planning_already_current = any(
            tuple(value.get("constrained_columns") or ())
            == ("calendar_event_uid", "calendar_id")
            and str(value.get("referred_table") or "") == "calendar_events"
            and tuple(value.get("referred_columns") or ())
            == ("uid", "calendar_id")
            for value in sa.inspect(bind).get_foreign_keys("planning_items")
        )
        if manage_sqlite_foreign_keys:
            _enable_sqlite_foreign_keys(False)
        try:
            with op.batch_alter_table(
                "calendars", recreate="always",
                naming_convention=_SQLITE_NAMING_CONVENTION,
            ) as batch:
                batch.alter_column(
                    "owner_id", existing_type=sa.String(length=36),
                    nullable=False,
                )
                batch.create_foreign_key(
                    "fk_calendars_owner_account", "accounts",
                    ["owner_id"], ["id"], ondelete="CASCADE",
                )
                batch.create_unique_constraint(
                    "uq_calendars_id_owner", ["id", "owner_id"],
                )
                batch.create_check_constraint(
                    "ck_calendars_config_version", "config_version >= 1",
                )

            with op.batch_alter_table(
                "calendar_events", recreate="always",
                naming_convention=_SQLITE_NAMING_CONVENTION,
            ) as batch:
                batch.drop_constraint(
                    "pk_calendar_events", type_="primary",
                )
                batch.drop_constraint(
                    "fk_calendar_events_calendar_id_calendars",
                    type_="foreignkey",
                )
                batch.alter_column(
                    "owner_id", existing_type=sa.String(length=36),
                    nullable=False,
                )
                batch.create_foreign_key(
                    "fk_calendar_events_owner_account", "accounts",
                    ["owner_id"], ["id"], ondelete="CASCADE",
                )
                batch.create_foreign_key(
                    "fk_calendar_events_calendar_owner", "calendars",
                    ["calendar_id", "owner_id"], ["id", "owner_id"],
                    ondelete="CASCADE",
                )
                batch.create_unique_constraint(
                    "uq_calendar_events_uid_owner", ["uid", "owner_id"],
                )
                batch.create_unique_constraint(
                    "uq_calendar_events_uid_calendar", ["uid", "calendar_id"],
                )
                batch.create_primary_key(
                    "pk_calendar_events", ["uid", "owner_id"],
                )
                batch.create_check_constraint(
                    "ck_calendar_events_version", "version >= 1",
                )

            if not planning_already_current:
                with op.batch_alter_table(
                    "planning_items", recreate="always",
                    naming_convention=_SQLITE_NAMING_CONVENTION,
                ) as batch:
                    batch.drop_constraint(
                        "fk_planning_items_calendar_event_uid_calendar_events",
                        type_="foreignkey",
                    )
                    batch.drop_constraint(
                        "uq_planning_items_calendar_event_uid", type_="unique",
                    )
                    batch.create_foreign_key(
                        "fk_planning_items_calendar_event", "calendar_events",
                        ["calendar_event_uid", "calendar_id"],
                        ["uid", "calendar_id"], ondelete="SET NULL",
                    )
                    batch.create_unique_constraint(
                        "uq_planning_items_calendar_event",
                        ["calendar_event_uid", "calendar_id"],
                    )
        finally:
            if manage_sqlite_foreign_keys:
                _enable_sqlite_foreign_keys(True)
        violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("Calendar authority migration violated a foreign key")
    elif dialect == "postgresql":
        inspector = sa.inspect(bind)
        old_calendar_fk = _existing_calendar_event_fk_name()
        old_event_pk = str(
            inspector.get_pk_constraint("calendar_events").get("name") or ""
        )
        old_planning_fks = [
            value for value in inspector.get_foreign_keys("planning_items")
            if tuple(value.get("constrained_columns") or ())
            == ("calendar_event_uid",)
            and str(value.get("referred_table") or "") == "calendar_events"
            and tuple(value.get("referred_columns") or ()) == ("uid",)
        ]
        old_planning_uniques = [
            value for value in inspector.get_unique_constraints("planning_items")
            if tuple(value.get("column_names") or ()) == ("calendar_event_uid",)
        ]
        if (
            not old_event_pk
            or len(old_planning_fks) != 1
            or not old_planning_fks[0].get("name")
            or len(old_planning_uniques) != 1
            or not old_planning_uniques[0].get("name")
        ):
            raise RuntimeError(
                "Expected legacy calendar-event and planning-link constraints"
            )
        op.drop_constraint(
            str(old_planning_fks[0]["name"]), "planning_items",
            type_="foreignkey",
        )
        op.drop_constraint(
            str(old_planning_uniques[0]["name"]), "planning_items",
            type_="unique",
        )
        op.drop_constraint(
            old_event_pk, "calendar_events", type_="primary",
        )
        op.alter_column(
            "calendars", "owner_id", existing_type=sa.String(length=36),
            nullable=False,
        )
        op.create_foreign_key(
            "fk_calendars_owner_account", "calendars", "accounts",
            ["owner_id"], ["id"], ondelete="CASCADE",
        )
        op.create_unique_constraint(
            "uq_calendars_id_owner", "calendars", ["id", "owner_id"],
        )
        op.create_check_constraint(
            "ck_calendars_config_version", "calendars", "config_version >= 1",
        )

        op.drop_constraint(
            old_calendar_fk, "calendar_events", type_="foreignkey",
        )
        op.alter_column(
            "calendar_events", "owner_id",
            existing_type=sa.String(length=36), nullable=False,
        )
        op.create_foreign_key(
            "fk_calendar_events_owner_account", "calendar_events", "accounts",
            ["owner_id"], ["id"], ondelete="CASCADE",
        )
        op.create_foreign_key(
            "fk_calendar_events_calendar_owner", "calendar_events", "calendars",
            ["calendar_id", "owner_id"], ["id", "owner_id"],
            ondelete="CASCADE",
        )
        op.create_unique_constraint(
            "uq_calendar_events_uid_owner", "calendar_events",
            ["uid", "owner_id"],
        )
        op.create_unique_constraint(
            "uq_calendar_events_uid_calendar", "calendar_events",
            ["uid", "calendar_id"],
        )
        op.create_primary_key(
            "pk_calendar_events", "calendar_events", ["uid", "owner_id"],
        )
        op.create_check_constraint(
            "ck_calendar_events_version", "calendar_events", "version >= 1",
        )
        op.create_foreign_key(
            "fk_planning_items_calendar_event", "planning_items",
            "calendar_events", ["calendar_event_uid", "calendar_id"],
            ["uid", "calendar_id"], ondelete="SET NULL",
        )
        op.create_unique_constraint(
            "uq_planning_items_calendar_event", "planning_items",
            ["calendar_event_uid", "calendar_id"],
        )
    else:
        raise RuntimeError(
            "Calendar authority migration supports only SQLite and PostgreSQL"
        )


def _create_authority_tables() -> None:
    op.create_index(
        "uq_action_proposals_id_owner", "action_proposals",
        ["id", "owner_id"], unique=True,
    )
    op.create_index("ix_calendars_owner_id", "calendars", ["owner_id"])
    op.create_index(
        "ix_calendar_events_owner_id", "calendar_events", ["owner_id"],
    )

    op.create_table(
        "calendar_action_undos",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("event_uid", sa.String(), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("before_state", sa.JSON(), nullable=False),
        sa.Column("result_event_version", sa.Integer(), nullable=True),
        sa.Column("life_entity_id", sa.String(length=36), nullable=True),
        sa.Column("result_graph_version", sa.Integer(), nullable=True),
        sa.Column("created_link_ids", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="ready"),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "operation IN ('create', 'update', 'reschedule')",
            name="ck_calendar_action_undos_operation",
        ),
        sa.CheckConstraint(
            "state IN ('ready', 'used')", name="ck_calendar_action_undos_state",
        ),
        sa.CheckConstraint(
            "result_event_version IS NULL OR result_event_version >= 1",
            name="ck_calendar_action_undos_event_version",
        ),
        sa.CheckConstraint(
            "result_graph_version IS NULL OR result_graph_version >= 1",
            name="ck_calendar_action_undos_graph_version",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_calendar_action_undos_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id", "owner_id"],
            ["action_proposals.id", "action_proposals.owner_id"],
            name="fk_calendar_action_undos_proposal_owner",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "owner_id", name="uq_calendar_action_undos_id_owner",
        ),
        sa.UniqueConstraint(
            "owner_id", "proposal_id",
            name="uq_calendar_action_undos_owner_proposal",
        ),
    )
    op.create_index(
        "ix_calendar_action_undos_owner_id", "calendar_action_undos", ["owner_id"],
    )
    op.create_index(
        "ix_calendar_action_undos_proposal_id", "calendar_action_undos", ["proposal_id"],
    )
    op.create_index(
        "ix_calendar_action_undos_event_uid", "calendar_action_undos", ["event_uid"],
    )
    op.create_index(
        "ix_calendar_action_undos_owner_state_created", "calendar_action_undos",
        ["owner_id", "state", "created_at"],
    )
    op.create_index(
        "ix_calendar_action_undos_owner_event_created", "calendar_action_undos",
        ["owner_id", "event_uid", "created_at"],
    )

    op.create_table(
        "calendar_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("calendar_id", sa.String(), nullable=False),
        sa.Column("event_uid", sa.String(), nullable=False),
        sa.Column("proposal_id", sa.String(length=36), nullable=True),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=96), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("expected_event_version", sa.Integer(), nullable=False),
        sa.Column("expected_config_version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "operation IN ('create', 'update', 'delete')",
            name="ck_calendar_deliveries_operation",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'processing', 'retry', 'conflict', "
            "'completed', 'cancelled')",
            name="ck_calendar_deliveries_state",
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_calendar_deliveries_attempts",
        ),
        sa.CheckConstraint(
            "expected_event_version >= 1",
            name="ck_calendar_deliveries_event_version",
        ),
        sa.CheckConstraint(
            "expected_config_version >= 1",
            name="ck_calendar_deliveries_config_version",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_calendar_deliveries_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["calendar_id", "owner_id"],
            ["calendars.id", "calendars.owner_id"],
            name="fk_calendar_deliveries_calendar_owner",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id", "owner_id"],
            ["action_proposals.id", "action_proposals.owner_id"],
            name="fk_calendar_deliveries_proposal_owner",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key",
            name="uq_calendar_deliveries_owner_idempotency",
        ),
    )
    op.create_index(
        "ix_calendar_deliveries_owner_id", "calendar_deliveries", ["owner_id"],
    )
    op.create_index(
        "ix_calendar_deliveries_calendar_id", "calendar_deliveries", ["calendar_id"],
    )
    op.create_index(
        "ix_calendar_deliveries_event_uid", "calendar_deliveries", ["event_uid"],
    )
    op.create_index(
        "ix_calendar_deliveries_proposal_id", "calendar_deliveries", ["proposal_id"],
    )
    op.create_index(
        "ix_calendar_deliveries_next_attempt_at", "calendar_deliveries", ["next_attempt_at"],
    )
    op.create_index(
        "ix_calendar_deliveries_lease_expires_at", "calendar_deliveries", ["lease_expires_at"],
    )
    op.create_index(
        "ix_calendar_deliveries_owner_state_due", "calendar_deliveries",
        ["owner_id", "state", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_calendar_deliveries_event_order", "calendar_deliveries",
        ["owner_id", "event_uid", "created_at", "id"],
    )


def upgrade() -> None:
    _require_online_execution()
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("LOCK TABLE accounts, calendars, calendar_events IN ACCESS EXCLUSIVE MODE")
    _preflight_owner_mapping()

    op.add_column(
        "calendars", sa.Column("owner_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "calendars",
        sa.Column(
            "config_version", sa.Integer(), nullable=False, server_default="1",
        ),
    )
    op.add_column(
        "calendar_events",
        sa.Column("owner_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "calendar_events",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    _backfill_owner_ids()
    _upgrade_existing_tables()
    _create_authority_tables()


def _preflight_downgrade() -> None:
    from migrations.versions.contact_authority_20260719_0004 import (
        _preflight_previous_downgrade,
    )

    _preflight_previous_downgrade()
    bind = op.get_bind()
    retained_contacts = sum(
        int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0)
        for table_name in (
            "contact_sources", "contact_records", "contact_deliveries",
            "contact_import_runs",
        )
    )
    if retained_contacts:
        # A command that targets 0003 or earlier would otherwise first commit
        # this SQLite downgrade, then let 0004 discover the retained contacts
        # and refuse. Refusing one revision early keeps both revision and data
        # authority intact for every downgrade destination.
        raise RuntimeError(
            "Contact authority contains data; export or remove it before downgrade"
        )
    retained = sum(
        int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0)
        for table_name in ("calendar_action_undos", "calendar_deliveries")
    )
    if retained:
        raise RuntimeError(
            "Calendar action authority contains data; export or resolve it before downgrade"
        )

    duplicate_uid = bind.execute(sa.text("""
        SELECT uid
        FROM calendar_events
        GROUP BY uid
        HAVING COUNT(*) > 1
        LIMIT 1
    """)).first()
    if duplicate_uid is not None:
        raise RuntimeError(
            "Cross-owner calendar UID data cannot be represented by revision 0004"
        )

    incompatible = bind.execute(sa.text("""
        SELECT 1
        FROM calendars AS c
        LEFT JOIN accounts AS a
          ON (
              trim(COALESCE(c.owner, '')) <> ''
              AND lower(trim(a.username)) = lower(trim(c.owner))
          ) OR (
              trim(COALESCE(c.owner, '')) = ''
              AND a.status = 'active'
          )
        GROUP BY c.id, c.owner, c.owner_id, c.config_version
        HAVING COUNT(a.id) <> 1
            OR MIN(a.id) <> c.owner_id
            OR c.config_version <> 1
        UNION ALL
        SELECT 1
        FROM calendar_events AS e
        LEFT JOIN calendars AS c ON c.id = e.calendar_id
        WHERE c.id IS NULL OR e.owner_id <> c.owner_id OR e.version <> 1
        LIMIT 1
    """)).first()
    if incompatible is not None:
        raise RuntimeError(
            "Calendar authority cannot be represented safely by revision 0004"
        )


def _drop_authority_tables() -> None:
    for index_name in (
        "ix_calendar_deliveries_event_order",
        "ix_calendar_deliveries_owner_state_due",
        "ix_calendar_deliveries_lease_expires_at",
        "ix_calendar_deliveries_next_attempt_at",
        "ix_calendar_deliveries_proposal_id",
        "ix_calendar_deliveries_event_uid",
        "ix_calendar_deliveries_calendar_id",
        "ix_calendar_deliveries_owner_id",
    ):
        op.drop_index(index_name, table_name="calendar_deliveries")
    op.drop_table("calendar_deliveries")

    for index_name in (
        "ix_calendar_action_undos_owner_event_created",
        "ix_calendar_action_undos_owner_state_created",
        "ix_calendar_action_undos_event_uid",
        "ix_calendar_action_undos_proposal_id",
        "ix_calendar_action_undos_owner_id",
    ):
        op.drop_index(index_name, table_name="calendar_action_undos")
    op.drop_table("calendar_action_undos")

    op.drop_index("ix_calendar_events_owner_id", table_name="calendar_events")
    op.drop_index("ix_calendars_owner_id", table_name="calendars")
    op.drop_index("uq_action_proposals_id_owner", table_name="action_proposals")


def _downgrade_existing_tables() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "sqlite":
        _enable_sqlite_foreign_keys(False)
        try:
            with op.batch_alter_table(
                "planning_items", recreate="always",
                naming_convention=_SQLITE_NAMING_CONVENTION,
            ) as batch:
                batch.drop_constraint(
                    "fk_planning_items_calendar_event", type_="foreignkey",
                )
                batch.drop_constraint(
                    "uq_planning_items_calendar_event", type_="unique",
                )
                batch.create_foreign_key(
                    "fk_planning_items_calendar_event_uid_calendar_events",
                    "calendar_events", ["calendar_event_uid"], ["uid"],
                    ondelete="SET NULL",
                )
                batch.create_unique_constraint(
                    "uq_planning_items_calendar_event_uid",
                    ["calendar_event_uid"],
                )

            with op.batch_alter_table(
                "calendar_events", recreate="always",
                naming_convention=_SQLITE_NAMING_CONVENTION,
            ) as batch:
                batch.drop_constraint(
                    "pk_calendar_events", type_="primary",
                )
                batch.drop_constraint(
                    "fk_calendar_events_calendar_owner", type_="foreignkey",
                )
                batch.drop_constraint(
                    "fk_calendar_events_owner_account", type_="foreignkey",
                )
                batch.drop_constraint(
                    "uq_calendar_events_uid_owner", type_="unique",
                )
                batch.drop_constraint(
                    "uq_calendar_events_uid_calendar", type_="unique",
                )
                batch.drop_constraint(
                    "ck_calendar_events_version", type_="check",
                )
                batch.create_foreign_key(
                    "fk_calendar_events_calendar_id_calendars", "calendars",
                    ["calendar_id"], ["id"],
                )
                batch.create_primary_key(
                    "pk_calendar_events", ["uid"],
                )
                batch.drop_column("version")
                batch.drop_column("owner_id")

            with op.batch_alter_table(
                "calendars", recreate="always",
                naming_convention=_SQLITE_NAMING_CONVENTION,
            ) as batch:
                batch.drop_constraint(
                    "fk_calendars_owner_account", type_="foreignkey",
                )
                batch.drop_constraint(
                    "uq_calendars_id_owner", type_="unique",
                )
                batch.drop_constraint(
                    "ck_calendars_config_version", type_="check",
                )
                batch.drop_column("config_version")
                batch.drop_column("owner_id")
        finally:
            _enable_sqlite_foreign_keys(True)
        violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("Calendar authority downgrade violated a foreign key")
    elif dialect == "postgresql":
        op.drop_constraint(
            "fk_planning_items_calendar_event", "planning_items",
            type_="foreignkey",
        )
        op.drop_constraint(
            "uq_planning_items_calendar_event", "planning_items",
            type_="unique",
        )
        op.drop_constraint(
            "pk_calendar_events", "calendar_events", type_="primary",
        )
        op.drop_constraint(
            "fk_calendar_events_calendar_owner", "calendar_events",
            type_="foreignkey",
        )
        op.drop_constraint(
            "fk_calendar_events_owner_account", "calendar_events",
            type_="foreignkey",
        )
        op.drop_constraint(
            "uq_calendar_events_uid_owner", "calendar_events", type_="unique",
        )
        op.drop_constraint(
            "uq_calendar_events_uid_calendar", "calendar_events", type_="unique",
        )
        op.drop_constraint(
            "ck_calendar_events_version", "calendar_events", type_="check",
        )
        op.create_primary_key(
            "pk_calendar_events", "calendar_events", ["uid"],
        )
        op.create_foreign_key(
            "fk_calendar_events_calendar_id_calendars",
            "calendar_events", "calendars", ["calendar_id"], ["id"],
        )
        op.create_foreign_key(
            "fk_planning_items_calendar_event_uid_calendar_events",
            "planning_items", "calendar_events", ["calendar_event_uid"],
            ["uid"], ondelete="SET NULL",
        )
        op.create_unique_constraint(
            "uq_planning_items_calendar_event_uid", "planning_items",
            ["calendar_event_uid"],
        )
        op.drop_column("calendar_events", "version")
        op.drop_column("calendar_events", "owner_id")

        op.drop_constraint(
            "fk_calendars_owner_account", "calendars", type_="foreignkey",
        )
        op.drop_constraint(
            "uq_calendars_id_owner", "calendars", type_="unique",
        )
        op.drop_constraint(
            "ck_calendars_config_version", "calendars", type_="check",
        )
        op.drop_column("calendars", "config_version")
        op.drop_column("calendars", "owner_id")
    else:
        raise RuntimeError(
            "Calendar authority migration supports only SQLite and PostgreSQL"
        )


def downgrade() -> None:
    _require_online_execution()
    _preflight_downgrade()
    _drop_authority_tables()
    _downgrade_existing_tables()
