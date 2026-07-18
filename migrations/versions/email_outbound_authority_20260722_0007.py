"""Add approval-backed agent email drafts and durable delivery outbox.

Revision ID: 20260722_0007
Revises: 20260721_0006

The migration creates canonical Account.id-owned state only. Legacy
``scheduled_emails.db`` agent drafts are adopted separately by the bounded,
read-only importer so Alembic never depends on a host-local sidecar path.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260722_0007"
down_revision: Union[str, Sequence[str], None] = "20260721_0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EMAIL_OUTBOUND_REQUIRED_TABLES = frozenset({
    "email_outbound_drafts",
    "email_outbound_deliveries",
})

EMAIL_OUTBOUND_REQUIRED_COLUMNS = {
    "email_outbound_drafts": frozenset({
        "id", "owner_id", "proposal_id", "email_account_id", "kind",
        "content", "content_sha256", "source", "state", "version",
        "created_at", "updated_at",
    }),
    "email_outbound_deliveries": frozenset({
        "id", "owner_id", "draft_id", "proposal_id", "email_account_id",
        "idempotency_key", "payload", "content_sha256", "state",
        "attempts", "next_attempt_at", "claim_token_digest", "claimed_at",
        "lease_expires_at", "completed_at", "last_error_code",
        "provider_message_id", "version", "created_at", "updated_at",
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
        "email_outbound_drafts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("email_account_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column(
            "state", sa.String(length=24), nullable=False,
            server_default="pending_review",
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "kind IN ('new', 'reply')",
            name="ck_email_outbound_drafts_kind",
        ),
        sa.CheckConstraint(
            "state IN ('pending_review', 'queued', 'delivered', 'failed', "
            "'rejected')",
            name="ck_email_outbound_drafts_state",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_email_outbound_drafts_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"],
            name="fk_email_outbound_drafts_owner_id_accounts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["email_account_id"], ["email_accounts.id"],
            name="fk_email_outbound_drafts_email_account_id_email_accounts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id", "owner_id"],
            ["action_proposals.id", "action_proposals.owner_id"],
            name="fk_email_outbound_drafts_proposal_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_outbound_drafts"),
        sa.UniqueConstraint(
            "id", "owner_id", name="uq_email_outbound_drafts_id_owner",
        ),
        sa.UniqueConstraint(
            "owner_id", "proposal_id",
            name="uq_email_outbound_drafts_owner_proposal",
        ),
    )
    op.create_index(
        "ix_email_outbound_drafts_owner_id",
        "email_outbound_drafts", ["owner_id"], unique=False,
    )
    op.create_index(
        "ix_email_outbound_drafts_proposal_id",
        "email_outbound_drafts", ["proposal_id"], unique=False,
    )
    op.create_index(
        "ix_email_outbound_drafts_email_account_id",
        "email_outbound_drafts", ["email_account_id"], unique=False,
    )
    op.create_index(
        "ix_email_outbound_drafts_content_sha256",
        "email_outbound_drafts", ["content_sha256"], unique=False,
    )
    op.create_index(
        "ix_email_outbound_drafts_state",
        "email_outbound_drafts", ["state"], unique=False,
    )
    op.create_index(
        "ix_email_outbound_drafts_owner_state_created",
        "email_outbound_drafts", ["owner_id", "state", "created_at"],
        unique=False,
    )

    op.create_table(
        "email_outbound_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("draft_id", sa.String(length=36), nullable=False),
        sa.Column("proposal_id", sa.String(length=36), nullable=False),
        sa.Column("email_account_id", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "state", sa.String(length=24), nullable=False,
            server_default="queued",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("claim_token_digest", sa.String(length=64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN ('queued', 'claimed', 'retry', 'delivered', 'failed', "
            "'cancelled')",
            name="ck_email_outbound_deliveries_state",
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_email_outbound_deliveries_attempts",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_email_outbound_deliveries_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"],
            name="fk_email_outbound_deliveries_owner_id_accounts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["email_account_id"], ["email_accounts.id"],
            name="fk_email_outbound_deliveries_email_account_id_email_accounts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["draft_id", "owner_id"],
            ["email_outbound_drafts.id", "email_outbound_drafts.owner_id"],
            name="fk_email_outbound_deliveries_draft_owner",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["proposal_id", "owner_id"],
            ["action_proposals.id", "action_proposals.owner_id"],
            name="fk_email_outbound_deliveries_proposal_owner",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_email_outbound_deliveries"),
        sa.UniqueConstraint(
            "id", "owner_id", name="uq_email_outbound_deliveries_id_owner",
        ),
        sa.UniqueConstraint(
            "owner_id", "proposal_id",
            name="uq_email_outbound_deliveries_owner_proposal",
        ),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key",
            name="uq_email_outbound_deliveries_owner_idempotency",
        ),
        sa.UniqueConstraint(
            "claim_token_digest",
            name="uq_email_outbound_deliveries_claim_token_digest",
        ),
    )
    op.create_index(
        "ix_email_outbound_deliveries_owner_id",
        "email_outbound_deliveries", ["owner_id"], unique=False,
    )
    op.create_index(
        "ix_email_outbound_deliveries_draft_id",
        "email_outbound_deliveries", ["draft_id"], unique=False,
    )
    op.create_index(
        "ix_email_outbound_deliveries_proposal_id",
        "email_outbound_deliveries", ["proposal_id"], unique=False,
    )
    op.create_index(
        "ix_email_outbound_deliveries_email_account_id",
        "email_outbound_deliveries", ["email_account_id"], unique=False,
    )
    op.create_index(
        "ix_email_outbound_deliveries_content_sha256",
        "email_outbound_deliveries", ["content_sha256"], unique=False,
    )
    op.create_index(
        "ix_email_outbound_deliveries_state",
        "email_outbound_deliveries", ["state"], unique=False,
    )
    op.create_index(
        "ix_email_outbound_deliveries_next_attempt_at",
        "email_outbound_deliveries", ["next_attempt_at"], unique=False,
    )
    op.create_index(
        "ix_email_outbound_deliveries_lease_expires_at",
        "email_outbound_deliveries", ["lease_expires_at"], unique=False,
    )
    op.create_index(
        "ix_email_outbound_deliveries_ready",
        "email_outbound_deliveries",
        ["state", "next_attempt_at", "lease_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("email_outbound_deliveries")
    op.drop_table("email_outbound_drafts")
