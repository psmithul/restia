"""Move mutable email runtime state into canonical SQL authority.

Revision ID: 20260727_0012
Revises: 20260726_0011

Only rebuildable connector/LLM caches remain in the local email sidecar.
Tags, automation rules and idempotency, and manual scheduled delivery are
Account.id-owned shared state.  All private values live in ORM-encrypted JSON
or encrypted text columns; routing columns contain only opaque digests and
fenced lease state.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260727_0012"
down_revision: Union[str, Sequence[str], None] = "20260726_0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


EMAIL_RUNTIME_REQUIRED_TABLES = frozenset({
    "email_tag_states",
    "email_automation_rules",
    "email_scheduled_deliveries",
    "email_automation_runs",
    "email_runtime_import_runs",
})

EMAIL_RUNTIME_REQUIRED_COLUMNS = {
    "email_tag_states": frozenset({
        "id", "owner_id", "account_key", "message_digest",
        "location_digest", "payload", "version", "created_at", "updated_at",
    }),
    "email_automation_rules": frozenset({
        "id", "owner_id", "account_key", "rules", "version", "created_at",
        "updated_at",
    }),
    "email_scheduled_deliveries": frozenset({
        "id", "owner_id", "email_account_id", "idempotency_key", "payload",
        "payload_sha256", "scheduled_for", "state", "attempts",
        "next_attempt_at", "claim_token_digest", "claimed_at",
        "lease_expires_at", "completed_at", "last_error_code",
        "provider_message_id", "version", "created_at", "updated_at",
    }),
    "email_automation_runs": frozenset({
        "id", "owner_id", "account_key", "operation", "message_digest",
        "payload", "state", "attempts", "next_attempt_at",
        "claim_token_digest", "claimed_at", "lease_expires_at",
        "completed_at", "last_error_code", "version", "created_at",
        "updated_at",
    }),
    "email_runtime_import_runs": frozenset({
        "id", "owner_id", "source_kind", "source_sha256", "state", "details",
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
        "email_tag_states",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("account_key", sa.String(length=255), nullable=False),
        sa.Column("message_digest", sa.String(length=64), nullable=False),
        sa.Column("location_digest", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "length(message_digest) = 64 AND length(location_digest) = 64",
            name="ck_email_tag_states_digests",
        ),
        sa.CheckConstraint("version >= 1", name="ck_email_tag_states_version"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "account_key", "message_digest",
            name="uq_email_tag_states_message",
        ),
        sa.UniqueConstraint(
            "owner_id", "account_key", "location_digest",
            name="uq_email_tag_states_location",
        ),
    )
    op.create_index(
        "ix_email_tag_states_owner_id", "email_tag_states", ["owner_id"],
        unique=False,
    )
    op.create_index(
        "ix_email_tag_states_owner_account", "email_tag_states",
        ["owner_id", "account_key", "updated_at"], unique=False,
    )

    op.create_table(
        "email_automation_rules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column(
            "account_key", sa.String(length=255), nullable=False,
            server_default="*",
        ),
        sa.Column("rules", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "length(account_key) >= 1", name="ck_email_automation_rules_scope",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_email_automation_rules_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "account_key", name="uq_email_automation_rules_scope",
        ),
    )
    op.create_index(
        "ix_email_automation_rules_owner_id", "email_automation_rules",
        ["owner_id"], unique=False,
    )

    op.create_table(
        "email_scheduled_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("email_account_id", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
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
            name="ck_email_scheduled_deliveries_state",
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_email_scheduled_deliveries_attempts",
        ),
        sa.CheckConstraint(
            "length(payload_sha256) = 64",
            name="ck_email_scheduled_deliveries_payload_digest",
        ),
        sa.CheckConstraint(
            "claim_token_digest IS NULL OR length(claim_token_digest) = 64",
            name="ck_email_scheduled_deliveries_claim_digest",
        ),
        sa.CheckConstraint(
            "(state = 'claimed' AND claim_token_digest IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state <> 'claimed' AND claim_token_digest IS NULL "
            "AND claimed_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_email_scheduled_deliveries_lease_lifecycle",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_email_scheduled_deliveries_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["email_account_id"], ["email_accounts.id"], ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "idempotency_key",
            name="uq_email_scheduled_deliveries_idempotency",
        ),
        sa.UniqueConstraint(
            "claim_token_digest",
            name="uq_email_scheduled_deliveries_claim_token_digest",
        ),
    )
    for name, columns in (
        ("ix_email_scheduled_deliveries_owner_id", ["owner_id"]),
        ("ix_email_scheduled_deliveries_email_account_id", ["email_account_id"]),
        ("ix_email_scheduled_deliveries_scheduled_for", ["scheduled_for"]),
        ("ix_email_scheduled_deliveries_state", ["state"]),
        ("ix_email_scheduled_deliveries_next_attempt_at", ["next_attempt_at"]),
        ("ix_email_scheduled_deliveries_lease_expires_at", ["lease_expires_at"]),
        (
            "ix_email_scheduled_deliveries_ready",
            ["state", "scheduled_for", "next_attempt_at", "lease_expires_at"],
        ),
    ):
        op.create_index(name, "email_scheduled_deliveries", columns, unique=False)

    op.create_table(
        "email_automation_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("account_key", sa.String(length=255), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("message_digest", sa.String(length=64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "state", sa.String(length=24), nullable=False,
            server_default="pending",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("claim_token_digest", sa.String(length=64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "operation IN ('summary', 'reply', 'classify', 'calendar', "
            "'email_received')",
            name="ck_email_automation_runs_operation",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'claimed', 'retry', 'completed', 'failed')",
            name="ck_email_automation_runs_state",
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_email_automation_runs_attempts",
        ),
        sa.CheckConstraint(
            "length(message_digest) = 64",
            name="ck_email_automation_runs_message_digest",
        ),
        sa.CheckConstraint(
            "claim_token_digest IS NULL OR length(claim_token_digest) = 64",
            name="ck_email_automation_runs_claim_digest",
        ),
        sa.CheckConstraint(
            "(state = 'claimed' AND claim_token_digest IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state <> 'claimed' AND claim_token_digest IS NULL "
            "AND claimed_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_email_automation_runs_lease_lifecycle",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_email_automation_runs_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "account_key", "operation", "message_digest",
            name="uq_email_automation_runs_identity",
        ),
        sa.UniqueConstraint(
            "claim_token_digest",
            name="uq_email_automation_runs_claim_token_digest",
        ),
    )
    for name, columns in (
        ("ix_email_automation_runs_owner_id", ["owner_id"]),
        ("ix_email_automation_runs_state", ["state"]),
        ("ix_email_automation_runs_next_attempt_at", ["next_attempt_at"]),
        ("ix_email_automation_runs_lease_expires_at", ["lease_expires_at"]),
        (
            "ix_email_automation_runs_ready",
            ["state", "next_attempt_at", "lease_expires_at"],
        ),
    ):
        op.create_index(name, "email_automation_runs", columns, unique=False)

    op.create_table(
        "email_runtime_import_runs",
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
            name="ck_email_runtime_import_runs_state",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_email_runtime_import_runs_digest",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "source_kind", "source_sha256",
            name="uq_email_runtime_import_runs_source",
        ),
    )
    op.create_index(
        "ix_email_runtime_import_runs_owner_id", "email_runtime_import_runs",
        ["owner_id"], unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    retained = bind.execute(sa.text("""
        SELECT
          (SELECT COUNT(*) FROM email_tag_states)
          + (SELECT COUNT(*) FROM email_automation_rules)
          + (SELECT COUNT(*) FROM email_scheduled_deliveries)
          + (SELECT COUNT(*) FROM email_automation_runs)
          + (SELECT COUNT(*) FROM email_runtime_import_runs)
    """)).scalar_one()
    if int(retained or 0):
        raise RuntimeError(
            "Email runtime authority contains tag, rule, schedule, automation, "
            "or import state; export and deliberately remove it before "
            "downgrading revision 20260727_0012"
        )
    op.drop_index(
        "ix_email_runtime_import_runs_owner_id",
        table_name="email_runtime_import_runs",
    )
    op.drop_table("email_runtime_import_runs")

    for name in (
        "ix_email_automation_runs_ready",
        "ix_email_automation_runs_lease_expires_at",
        "ix_email_automation_runs_next_attempt_at",
        "ix_email_automation_runs_state",
        "ix_email_automation_runs_owner_id",
    ):
        op.drop_index(name, table_name="email_automation_runs")
    op.drop_table("email_automation_runs")

    for name in (
        "ix_email_scheduled_deliveries_ready",
        "ix_email_scheduled_deliveries_lease_expires_at",
        "ix_email_scheduled_deliveries_next_attempt_at",
        "ix_email_scheduled_deliveries_state",
        "ix_email_scheduled_deliveries_scheduled_for",
        "ix_email_scheduled_deliveries_email_account_id",
        "ix_email_scheduled_deliveries_owner_id",
    ):
        op.drop_index(name, table_name="email_scheduled_deliveries")
    op.drop_table("email_scheduled_deliveries")

    op.drop_index(
        "ix_email_automation_rules_owner_id",
        table_name="email_automation_rules",
    )
    op.drop_table("email_automation_rules")

    op.drop_index(
        "ix_email_tag_states_owner_account", table_name="email_tag_states",
    )
    op.drop_index("ix_email_tag_states_owner_id", table_name="email_tag_states")
    op.drop_table("email_tag_states")
