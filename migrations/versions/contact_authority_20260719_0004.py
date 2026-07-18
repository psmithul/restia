"""Add owner-scoped encrypted contact authority.

Revision ID: 20260719_0004
Revises: 20260718_0003

Encrypted ORM fields are represented physically as TEXT/JSON.  The legacy
settings/contacts adoption is intentionally application-level because it must
bind an already-authenticated Account and create durable filesystem backups
before the first database write.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260719_0004"
down_revision: Union[str, Sequence[str], None] = "20260718_0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


CONTACT_REQUIRED_TABLES = frozenset({
    "contact_sources", "contact_records", "contact_deliveries",
    "contact_import_runs",
})

CONTACT_REQUIRED_COLUMNS = {
    "contact_sources": frozenset({
        "id", "owner_id", "kind", "label", "base_url", "username",
        "password", "enabled", "last_sync_at", "sync_state", "last_error",
        "config_version", "version", "created_at", "updated_at",
    }),
    "contact_records": frozenset({
        "id", "owner_id", "source_id", "remote_uid", "remote_uid_digest",
        "remote_href", "remote_etag", "payload", "raw_vcard", "deleted_at",
        "version", "created_at", "updated_at",
    }),
    "contact_deliveries": frozenset({
        "id", "owner_id", "source_id", "record_id", "operation",
        "idempotency_key", "payload", "state", "attempts",
        "next_attempt_at", "claim_token", "claimed_at", "completed_at",
        "last_error_code", "version", "created_at", "updated_at",
    }),
    "contact_import_runs": frozenset({
        "id", "owner_id", "source_kind", "state", "settings_sha256",
        "contacts_sha256", "backup_settings_path", "backup_contacts_path",
        "details", "completed_at", "created_at", "updated_at",
    }),
}


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


def upgrade() -> None:
    op.create_table(
        "contact_sources",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False, server_default="local"),
        # Labels can contain a person's, employer's, or address-book name. Keep
        # the physical column unbounded enough for the encrypted envelope.
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("password", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("sync_state", sa.String(length=24), nullable=False, server_default="idle"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("config_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('local', 'carddav')", name="ck_contact_sources_kind",
        ),
        sa.CheckConstraint(
            "sync_state IN ('idle', 'syncing', 'ready', 'error', 'disabled')",
            name="ck_contact_sources_sync_state",
        ),
        sa.CheckConstraint("version >= 1", name="ck_contact_sources_version"),
        sa.CheckConstraint(
            "config_version >= 1", name="ck_contact_sources_config_version",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "owner_id", name="uq_contact_sources_id_owner"),
    )
    op.create_index("ix_contact_sources_owner_id", "contact_sources", ["owner_id"])
    op.create_index(
        "uq_contact_sources_owner_local",
        "contact_sources",
        ["owner_id"],
        unique=True,
        sqlite_where=sa.text("kind = 'local'"),
        postgresql_where=sa.text("kind = 'local'"),
    )
    op.create_index(
        "uq_contact_sources_owner_carddav",
        "contact_sources",
        ["owner_id"],
        unique=True,
        sqlite_where=sa.text("kind = 'carddav'"),
        postgresql_where=sa.text("kind = 'carddav'"),
    )
    op.create_index(
        "ix_contact_sources_owner_kind_enabled",
        "contact_sources",
        ["owner_id", "kind", "enabled"],
    )

    op.create_table(
        "contact_records",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("remote_uid", sa.Text(), nullable=False),
        sa.Column("remote_uid_digest", sa.String(length=64), nullable=False),
        sa.Column("remote_href", sa.Text(), nullable=True),
        # The encrypted content envelope is longer than the remote token and
        # must not be constrained by the plaintext ETag's former length.
        sa.Column("remote_etag", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("raw_vcard", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint("version >= 1", name="ck_contact_records_version"),
        sa.ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_id", "owner_id"],
            ["contact_sources.id", "contact_sources.owner_id"],
            ondelete="CASCADE",
            name="fk_contact_records_source_owner",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "owner_id", name="uq_contact_records_id_owner"),
        sa.UniqueConstraint(
            "source_id", "remote_uid_digest",
            name="uq_contact_records_source_uid_digest",
        ),
    )
    op.create_index("ix_contact_records_owner_id", "contact_records", ["owner_id"])
    op.create_index("ix_contact_records_source_id", "contact_records", ["source_id"])
    op.create_index("ix_contact_records_deleted_at", "contact_records", ["deleted_at"])
    op.create_index(
        "ix_contact_records_owner_source_deleted",
        "contact_records",
        ["owner_id", "source_id", "deleted_at"],
    )

    op.create_table(
        "contact_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("source_id", sa.String(length=36), nullable=False),
        sa.Column("record_id", sa.String(length=36), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=96), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("claim_token", sa.String(length=36), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "operation IN ('create', 'update', 'delete')",
            name="ck_contact_deliveries_operation",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'processing', 'retry', 'conflict', 'completed')",
            name="ck_contact_deliveries_state",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_contact_deliveries_attempts"),
        sa.CheckConstraint("version >= 1", name="ck_contact_deliveries_version"),
        sa.ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_id", "owner_id"],
            ["contact_sources.id", "contact_sources.owner_id"],
            ondelete="CASCADE",
            name="fk_contact_deliveries_source_owner",
        ),
        sa.ForeignKeyConstraint(
            ["record_id", "owner_id"],
            ["contact_records.id", "contact_records.owner_id"],
            ondelete="CASCADE",
            name="fk_contact_deliveries_record_owner",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key",
            name="uq_contact_deliveries_owner_idempotency",
        ),
    )
    op.create_index("ix_contact_deliveries_owner_id", "contact_deliveries", ["owner_id"])
    op.create_index("ix_contact_deliveries_source_id", "contact_deliveries", ["source_id"])
    op.create_index("ix_contact_deliveries_record_id", "contact_deliveries", ["record_id"])
    op.create_index("ix_contact_deliveries_next_attempt_at", "contact_deliveries", ["next_attempt_at"])
    op.create_index(
        "ix_contact_deliveries_owner_state_due",
        "contact_deliveries",
        ["owner_id", "state", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_contact_deliveries_record_order",
        "contact_deliveries",
        ["owner_id", "record_id", "created_at", "id"],
    )

    op.create_table(
        "contact_import_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("settings_sha256", sa.String(length=64), nullable=True),
        sa.Column("contacts_sha256", sa.String(length=64), nullable=True),
        sa.Column("backup_settings_path", sa.Text(), nullable=True),
        sa.Column("backup_contacts_path", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_contact_import_runs_state",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_kind"),
    )
    op.create_index("ix_contact_import_runs_owner_id", "contact_import_runs", ["owner_id"])


def _preflight_previous_downgrade() -> None:
    """Keep a wrong-key 0003 downgrade from first discarding contact tables.

    SQLite Alembic DDL is non-transactional. A downgrade from 0004 to 0002
    otherwise drops this revision and stamps 0003 before the older revision
    discovers that its private planning/edge content cannot be decrypted.
    Verify the older revision's key-dependent inputs while 0004 is intact.
    """

    from src.secret_storage import is_decryptable, is_encrypted

    bind = op.get_bind()
    planning_rows = bind.execute(sa.text(
        "SELECT title, details FROM planning_items"
    )).all()
    for title, details in planning_rows:
        for field, stored in (("title", title), ("details", details)):
            value = str(stored or "")
            if value.startswith("enc:") and (
                not is_encrypted(value) or not is_decryptable(value)
            ):
                raise RuntimeError(
                    f"PlanningItem {field} could not be decrypted for downgrade"
                )

    edge_rows = bind.execute(sa.text(
        "SELECT metadata, provenance FROM entity_links"
    )).all()
    for metadata, provenance in edge_rows:
        for field, stored in (("metadata", metadata), ("provenance", provenance)):
            value = stored
            if isinstance(value, str):
                try:
                    decoded = __import__("json").loads(value)
                    value = decoded if isinstance(decoded, str) else value
                except (TypeError, ValueError):
                    pass
            if isinstance(value, str) and value.startswith("enc:") and (
                not is_encrypted(value) or not is_decryptable(value)
            ):
                raise RuntimeError(
                    f"EntityLink {field} could not be decrypted with the active key"
                )


def downgrade() -> None:
    _preflight_previous_downgrade()
    bind = op.get_bind()
    retained = sum(
        int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table_name}")).scalar() or 0)
        for table_name in (
            "contact_sources", "contact_records", "contact_deliveries",
            "contact_import_runs",
        )
    )
    if retained:
        raise RuntimeError(
            "Contact authority contains data; export or remove it before downgrade"
        )

    op.drop_index("ix_contact_import_runs_owner_id", table_name="contact_import_runs")
    op.drop_table("contact_import_runs")

    op.drop_index("ix_contact_deliveries_record_order", table_name="contact_deliveries")
    op.drop_index("ix_contact_deliveries_owner_state_due", table_name="contact_deliveries")
    op.drop_index("ix_contact_deliveries_next_attempt_at", table_name="contact_deliveries")
    op.drop_index("ix_contact_deliveries_record_id", table_name="contact_deliveries")
    op.drop_index("ix_contact_deliveries_source_id", table_name="contact_deliveries")
    op.drop_index("ix_contact_deliveries_owner_id", table_name="contact_deliveries")
    op.drop_table("contact_deliveries")

    op.drop_index("ix_contact_records_owner_source_deleted", table_name="contact_records")
    op.drop_index("ix_contact_records_deleted_at", table_name="contact_records")
    op.drop_index("ix_contact_records_source_id", table_name="contact_records")
    op.drop_index("ix_contact_records_owner_id", table_name="contact_records")
    op.drop_table("contact_records")

    op.drop_index("ix_contact_sources_owner_kind_enabled", table_name="contact_sources")
    op.drop_index("uq_contact_sources_owner_carddav", table_name="contact_sources")
    op.drop_index("uq_contact_sources_owner_local", table_name="contact_sources")
    op.drop_index("ix_contact_sources_owner_id", table_name="contact_sources")
    op.drop_table("contact_sources")
