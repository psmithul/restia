"""Telegram polling, inbound processing, and reply-delivery authority.

Revision ID: 20260724_0009
Revises: 20260723_0008

The Telegram Bot API permits only one active long poller for a bot.  These
tables move that singleton lease, its monotonic cursor and poison-update
resolution, plus inbound/reply idempotency, into the configured application
database.  Payload text remains encrypted by the ORM; the dead-letter table
contains safe metadata only.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260724_0009"
down_revision: Union[str, Sequence[str], None] = "20260723_0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TELEGRAM_RUNTIME_REQUIRED_TABLES = frozenset({
    "telegram_polling_states",
    "telegram_dead_letters",
    "telegram_inbound_updates",
    "telegram_runtime_import_runs",
})

TELEGRAM_RUNTIME_REQUIRED_COLUMNS = {
    "telegram_polling_states": frozenset({
        "bot_fingerprint", "next_offset", "failure_update_id",
        "failure_attempts", "lease_owner", "lease_token",
        "lease_expires_at", "version", "created_at", "updated_at",
    }),
    "telegram_dead_letters": frozenset({
        "id", "bot_fingerprint", "update_id", "error_type", "attempts",
        "failed_at", "created_at", "updated_at",
    }),
    "telegram_inbound_updates": frozenset({
        "id", "bot_fingerprint", "update_id", "chat_id",
        "owner_account_id", "reply_text", "status",
        "processing_claim_digest", "processing_lease_expires_at",
        "reply_claim_digest", "reply_lease_expires_at", "version",
        "created_at", "updated_at",
    }),
    "telegram_runtime_import_runs": frozenset({
        "id", "bot_fingerprint", "source_kind", "state", "source_sha256",
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
        "telegram_polling_states",
        sa.Column("bot_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("next_offset", sa.BigInteger(), nullable=True),
        sa.Column("failure_update_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "failure_attempts", sa.Integer(), nullable=False,
            server_default="0",
        ),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column(
            "lease_token", sa.Integer(), nullable=False, server_default="0",
        ),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "length(bot_fingerprint) = 64",
            name="ck_telegram_polling_states_fingerprint",
        ),
        sa.CheckConstraint(
            "next_offset IS NULL OR next_offset >= 0",
            name="ck_telegram_polling_states_offset",
        ),
        sa.CheckConstraint(
            "failure_attempts >= 0",
            name="ck_telegram_polling_states_attempts",
        ),
        sa.CheckConstraint(
            "lease_token >= 0 AND version >= 1",
            name="ck_telegram_polling_states_fencing",
        ),
        sa.PrimaryKeyConstraint("bot_fingerprint"),
    )
    op.create_index(
        "ix_telegram_polling_states_lease_expires_at",
        "telegram_polling_states", ["lease_expires_at"], unique=False,
    )

    op.create_table(
        "telegram_dead_letters",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("bot_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("error_type", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column(
            "failed_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        *_timestamps(),
        sa.CheckConstraint(
            "update_id >= 0 AND attempts >= 1",
            name="ck_telegram_dead_letters_resolution",
        ),
        sa.ForeignKeyConstraint(
            ["bot_fingerprint"], ["telegram_polling_states.bot_fingerprint"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bot_fingerprint", "update_id",
            name="uq_telegram_dead_letters_bot_update",
        ),
    )
    op.create_index(
        "ix_telegram_dead_letters_bot_failed_at", "telegram_dead_letters",
        ["bot_fingerprint", "failed_at"], unique=False,
    )

    op.create_table(
        "telegram_inbound_updates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("bot_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("owner_account_id", sa.String(length=36), nullable=True),
        sa.Column("reply_text", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.String(length=24), nullable=False,
            server_default="processing",
        ),
        sa.Column("processing_claim_digest", sa.String(length=64), nullable=True),
        sa.Column("processing_lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("reply_claim_digest", sa.String(length=64), nullable=True),
        sa.Column("reply_lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('processing', 'reply_pending', 'delivered', 'discarded')",
            name="ck_telegram_inbound_updates_status",
        ),
        sa.CheckConstraint(
            "update_id >= 0 AND version >= 1",
            name="ck_telegram_inbound_updates_version",
        ),
        sa.ForeignKeyConstraint(
            ["bot_fingerprint"], ["telegram_polling_states.bot_fingerprint"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owner_account_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bot_fingerprint", "update_id",
            name="uq_telegram_inbound_updates_bot_update",
        ),
    )
    op.create_index(
        "ix_telegram_inbound_updates_status_lease",
        "telegram_inbound_updates",
        ["bot_fingerprint", "status", "processing_lease_expires_at",
         "reply_lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_telegram_inbound_updates_owner",
        "telegram_inbound_updates", ["owner_account_id"], unique=False,
    )

    op.create_table(
        "telegram_runtime_import_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("bot_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column(
            "state", sa.String(length=24), nullable=False,
            server_default="pending",
        ),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_telegram_runtime_import_runs_state",
        ),
        sa.CheckConstraint(
            "length(bot_fingerprint) = 64 AND length(source_sha256) = 64",
            name="ck_telegram_runtime_import_runs_digests",
        ),
        sa.ForeignKeyConstraint(
            ["bot_fingerprint"], ["telegram_polling_states.bot_fingerprint"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bot_fingerprint", "source_kind",
            name="uq_telegram_runtime_import_bot_source",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    retained = bind.execute(sa.text("""
        SELECT
          (SELECT COUNT(*) FROM telegram_dead_letters)
          + (SELECT COUNT(*) FROM telegram_inbound_updates)
          + (SELECT COUNT(*) FROM telegram_runtime_import_runs)
          + (SELECT COUNT(*) FROM telegram_polling_states)
    """)).scalar_one()
    if int(retained or 0):
        raise RuntimeError(
            "Telegram runtime authority contains polling or delivery state; "
            "export and deliberately remove it before downgrading revision "
            "20260724_0009"
        )
    op.drop_table("telegram_runtime_import_runs")
    op.drop_index(
        "ix_telegram_inbound_updates_owner",
        table_name="telegram_inbound_updates",
    )
    op.drop_index(
        "ix_telegram_inbound_updates_status_lease",
        table_name="telegram_inbound_updates",
    )
    op.drop_table("telegram_inbound_updates")
    op.drop_index(
        "ix_telegram_dead_letters_bot_failed_at",
        table_name="telegram_dead_letters",
    )
    op.drop_table("telegram_dead_letters")
    op.drop_index(
        "ix_telegram_polling_states_lease_expires_at",
        table_name="telegram_polling_states",
    )
    op.drop_table("telegram_polling_states")
