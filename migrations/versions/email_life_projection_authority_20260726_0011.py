"""Move the email-to-Life projection ledger into canonical SQL authority.

Revision ID: 20260726_0011
Revises: 20260725_0010

The rebuildable email index may remain a local cache, but its projection
handoff is Account.id-owned shared state.  Private headers, subjects and RFC
thread identifiers are stored only in the ORM-encrypted JSON payload.  The
remaining columns are safe routing metadata, digests and fenced lease state.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260726_0011"
down_revision: Union[str, Sequence[str], None] = "20260725_0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EMAIL_LIFE_PROJECTION_REQUIRED_TABLES = frozenset({
    "email_life_projection_ledger",
    "email_life_projection_import_runs",
})

EMAIL_LIFE_PROJECTION_REQUIRED_COLUMNS = {
    "email_life_projection_ledger": frozenset({
        "id", "owner_id", "account_key", "folder", "message_uid",
        "header_sha256", "payload", "state", "claim_token_digest",
        "claimed_at", "lease_expires_at", "next_attempt_at",
        "attempt_count", "last_error_code", "completed_at", "version",
        "created_at", "updated_at",
    }),
    "email_life_projection_import_runs": frozenset({
        "id", "owner_id", "source_kind", "source_sha256", "state",
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
        "email_life_projection_ledger",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("account_key", sa.String(length=255), nullable=False),
        sa.Column("folder", sa.String(length=255), nullable=False),
        sa.Column("message_uid", sa.String(length=255), nullable=False),
        sa.Column("header_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column(
            "state", sa.String(length=24), nullable=False,
            server_default="pending",
        ),
        sa.Column("claim_token_digest", sa.String(length=64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default="0",
        ),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN ('pending', 'processing', 'failed', 'completed')",
            name="ck_email_life_projection_state",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_email_life_projection_attempts",
        ),
        sa.CheckConstraint(
            "length(header_sha256) = 64",
            name="ck_email_life_projection_header_digest",
        ),
        sa.CheckConstraint(
            "length(account_key) >= 1 AND length(folder) >= 1 "
            "AND length(message_uid) >= 1",
            name="ck_email_life_projection_routing_identity",
        ),
        sa.CheckConstraint(
            "claim_token_digest IS NULL OR length(claim_token_digest) = 64",
            name="ck_email_life_projection_claim_digest",
        ),
        sa.CheckConstraint(
            "(state = 'completed' AND payload IS NULL "
            "AND completed_at IS NOT NULL) OR "
            "(state <> 'completed' AND payload IS NOT NULL)",
            name="ck_email_life_projection_payload_lifecycle",
        ),
        sa.CheckConstraint(
            "(state = 'processing' AND claim_token_digest IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state <> 'processing' AND claim_token_digest IS NULL "
            "AND claimed_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_email_life_projection_lease_lifecycle",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_email_life_projection_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "account_key", "folder", "message_uid",
            "header_sha256", name="uq_email_life_projection_identity",
        ),
        sa.UniqueConstraint(
            "claim_token_digest",
            name="uq_email_life_projection_claim_token_digest",
        ),
    )
    op.create_index(
        "ix_email_life_projection_ledger_owner_id",
        "email_life_projection_ledger", ["owner_id"], unique=False,
    )
    op.create_index(
        "ix_email_life_projection_ledger_lease_expires_at",
        "email_life_projection_ledger", ["lease_expires_at"], unique=False,
    )
    op.create_index(
        "ix_email_life_projection_ledger_next_attempt_at",
        "email_life_projection_ledger", ["next_attempt_at"], unique=False,
    )
    op.create_index(
        "ix_email_life_projection_ready",
        "email_life_projection_ledger",
        ["owner_id", "account_key", "folder", "state", "next_attempt_at",
         "lease_expires_at"],
        unique=False,
    )

    op.create_table(
        "email_life_projection_import_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
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
            name="ck_email_life_projection_import_state",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_email_life_projection_import_digest",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "source_kind", "source_sha256",
            name="uq_email_life_projection_import_source",
        ),
    )
    op.create_index(
        "ix_email_life_projection_import_runs_owner_id",
        "email_life_projection_import_runs", ["owner_id"], unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    retained = bind.execute(sa.text("""
        SELECT
          (SELECT COUNT(*) FROM email_life_projection_ledger)
          + (SELECT COUNT(*) FROM email_life_projection_import_runs)
    """)).scalar_one()
    if int(retained or 0):
        raise RuntimeError(
            "Email Life projection authority contains durable handoff or "
            "import state; export and deliberately remove it before "
            "downgrading revision 20260726_0011"
        )
    op.drop_index(
        "ix_email_life_projection_import_runs_owner_id",
        table_name="email_life_projection_import_runs",
    )
    op.drop_table("email_life_projection_import_runs")
    op.drop_index(
        "ix_email_life_projection_ready",
        table_name="email_life_projection_ledger",
    )
    op.drop_index(
        "ix_email_life_projection_ledger_next_attempt_at",
        table_name="email_life_projection_ledger",
    )
    op.drop_index(
        "ix_email_life_projection_ledger_lease_expires_at",
        table_name="email_life_projection_ledger",
    )
    op.drop_index(
        "ix_email_life_projection_ledger_owner_id",
        table_name="email_life_projection_ledger",
    )
    op.drop_table("email_life_projection_ledger")
