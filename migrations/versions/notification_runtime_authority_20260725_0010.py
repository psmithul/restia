"""Move reminder claims and browser notifications into the main database.

Revision ID: 20260725_0010
Revises: 20260724_0009

The shared cancellation table is the cancel-before-enqueue barrier for both
delivery claims and browser outbox rows.  Browser payloads and linked claim
tokens are encrypted by the ORM; migration columns deliberately use portable
SQL/JSON types so SQLite and PostgreSQL share one schema contract.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260725_0010"
down_revision: Union[str, Sequence[str], None] = "20260724_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NOTIFICATION_RUNTIME_REQUIRED_TABLES = frozenset({
    "reminder_delivery_claims",
    "reminder_cancellations",
    "browser_notifications",
    "notification_runtime_import_runs",
})

NOTIFICATION_RUNTIME_REQUIRED_COLUMNS = {
    "reminder_delivery_claims": frozenset({
        "id", "owner_id", "note_id", "occurrence", "channel", "status",
        "claim_token_digest", "claimed_at", "retry_after", "delivered_at",
        "last_error_code", "version", "created_at", "updated_at",
    }),
    "reminder_cancellations": frozenset({
        "id", "owner_id", "note_id", "scope", "occurrence",
        "cancelled_at", "created_at", "updated_at",
    }),
    "browser_notifications": frozenset({
        "id", "owner_id", "payload", "dedupe_key_digest",
        "claim_owner_id", "claim_note_id", "claim_occurrence",
        "claim_channel", "claim_token", "acknowledged_at", "created_at",
        "updated_at",
    }),
    "notification_runtime_import_runs": frozenset({
        "id", "source_kind", "source_sha256", "state", "details",
        "completed_at", "created_at", "updated_at",
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
        "reminder_delivery_claims",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("note_id", sa.String(length=255), nullable=False),
        sa.Column(
            "occurrence", sa.String(length=255), nullable=False,
            server_default="",
        ),
        sa.Column(
            "channel", sa.String(length=64), nullable=False,
            server_default="browser",
        ),
        sa.Column(
            "status", sa.String(length=32), nullable=False,
            server_default="claimed",
        ),
        sa.Column("claim_token_digest", sa.String(length=64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("retry_after", sa.DateTime(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('claimed', 'awaiting_browser_ack', 'delivered', "
            "'failed', 'cancelled')",
            name="ck_reminder_delivery_claims_status",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_reminder_delivery_claims_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "note_id", "occurrence", "channel",
            name="uq_reminder_delivery_occurrence_channel",
        ),
    )
    op.create_index(
        "ix_reminder_delivery_claims_owner_id",
        "reminder_delivery_claims", ["owner_id"], unique=False,
    )
    op.create_index(
        "ix_reminder_delivery_claims_retry_after",
        "reminder_delivery_claims", ["retry_after"], unique=False,
    )
    op.create_index(
        "ix_reminder_delivery_claims_ready",
        "reminder_delivery_claims",
        ["owner_id", "status", "retry_after", "claimed_at"],
        unique=False,
    )

    op.create_table(
        "reminder_cancellations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("note_id", sa.String(length=255), nullable=False),
        sa.Column("scope", sa.String(length=24), nullable=False),
        sa.Column(
            "occurrence", sa.String(length=255), nullable=False,
            server_default="",
        ),
        sa.Column(
            "cancelled_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "scope IN ('all', 'occurrence')",
            name="ck_reminder_cancellations_scope",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "note_id", "scope", "occurrence",
            name="uq_reminder_cancellations_scope",
        ),
    )
    op.create_index(
        "ix_reminder_cancellations_owner_id",
        "reminder_cancellations", ["owner_id"], unique=False,
    )
    op.create_index(
        "ix_reminder_cancellations_lookup",
        "reminder_cancellations",
        ["owner_id", "note_id", "scope", "occurrence"],
        unique=False,
    )

    op.create_table(
        "browser_notifications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("dedupe_key_digest", sa.String(length=64), nullable=True),
        sa.Column("claim_owner_id", sa.String(length=36), nullable=True),
        sa.Column(
            "claim_note_id", sa.String(length=255), nullable=False,
            server_default="",
        ),
        sa.Column(
            "claim_occurrence", sa.String(length=255), nullable=False,
            server_default="",
        ),
        sa.Column(
            "claim_channel", sa.String(length=64), nullable=False,
            server_default="",
        ),
        sa.Column("claim_token", sa.Text(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["claim_owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "dedupe_key_digest",
            name="uq_browser_notifications_owner_dedupe",
        ),
    )
    op.create_index(
        "ix_browser_notifications_owner_id",
        "browser_notifications", ["owner_id"], unique=False,
    )
    op.create_index(
        "ix_browser_notifications_acknowledged_at",
        "browser_notifications", ["acknowledged_at"], unique=False,
    )
    op.create_index(
        "ix_browser_notifications_pending",
        "browser_notifications", ["owner_id", "acknowledged_at", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_browser_notifications_claim",
        "browser_notifications", ["owner_id", "claim_note_id", "claim_occurrence"],
        unique=False,
    )

    op.create_table(
        "notification_runtime_import_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "state", sa.String(length=24), nullable=False,
            server_default="pending",
        ),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_notification_runtime_import_runs_state",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_notification_runtime_import_runs_digest",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_kind", "source_sha256",
            name="uq_notification_runtime_import_source",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    retained = bind.execute(sa.text("""
        SELECT
          (SELECT COUNT(*) FROM reminder_delivery_claims)
          + (SELECT COUNT(*) FROM reminder_cancellations)
          + (SELECT COUNT(*) FROM browser_notifications)
          + (SELECT COUNT(*) FROM notification_runtime_import_runs)
    """)).scalar_one()
    if int(retained or 0):
        raise RuntimeError(
            "Notification runtime authority contains delivery state; export "
            "and deliberately remove it before downgrading revision "
            "20260725_0010"
        )
    op.drop_table("notification_runtime_import_runs")
    op.drop_index(
        "ix_browser_notifications_claim", table_name="browser_notifications",
    )
    op.drop_index(
        "ix_browser_notifications_pending", table_name="browser_notifications",
    )
    op.drop_index(
        "ix_browser_notifications_acknowledged_at",
        table_name="browser_notifications",
    )
    op.drop_index(
        "ix_browser_notifications_owner_id", table_name="browser_notifications",
    )
    op.drop_table("browser_notifications")
    op.drop_index(
        "ix_reminder_cancellations_lookup", table_name="reminder_cancellations",
    )
    op.drop_index(
        "ix_reminder_cancellations_owner_id",
        table_name="reminder_cancellations",
    )
    op.drop_table("reminder_cancellations")
    op.drop_index(
        "ix_reminder_delivery_claims_ready",
        table_name="reminder_delivery_claims",
    )
    op.drop_index(
        "ix_reminder_delivery_claims_retry_after",
        table_name="reminder_delivery_claims",
    )
    op.drop_index(
        "ix_reminder_delivery_claims_owner_id",
        table_name="reminder_delivery_claims",
    )
    op.drop_table("reminder_delivery_claims")
