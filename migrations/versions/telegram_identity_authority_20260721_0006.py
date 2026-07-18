"""Telegram identity and conversation authority.

Revision ID: 20260721_0006
Revises: 20260720_0005
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260721_0006"
down_revision: Union[str, Sequence[str], None] = "20260720_0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TELEGRAM_REQUIRED_TABLES = frozenset({
    "telegram_principals",
    "telegram_conversation_bindings",
    "telegram_link_codes",
    "telegram_identity_import_runs",
})

TELEGRAM_REQUIRED_COLUMNS = {
    "telegram_principals": frozenset({
        "id", "account_id", "bot_fingerprint", "chat_id",
        "chat_id_digest", "state", "linked_at", "revoked_at", "version",
        "created_at", "updated_at",
    }),
    "telegram_conversation_bindings": frozenset({
        "id", "principal_id", "account_id", "session_id", "version",
        "created_at", "updated_at",
    }),
    "telegram_link_codes": frozenset({
        "id", "account_id", "bot_fingerprint", "code_digest",
        "digest_scheme", "expires_at", "consumed_at", "invalidated_at",
        "created_at", "updated_at",
    }),
    "telegram_identity_import_runs": frozenset({
        "id", "source_kind", "state", "source_sha256", "source_path",
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
        "telegram_principals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("bot_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.Text(), nullable=False),
        sa.Column("chat_id_digest", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="linked"),
        sa.Column(
            "linked_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN ('linked', 'unlinked')",
            name="ck_telegram_principals_state",
        ),
        sa.CheckConstraint(
            "length(bot_fingerprint) = 64 AND length(chat_id_digest) = 64",
            name="ck_telegram_principals_digests",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_telegram_principals_version",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "id", "account_id", name="uq_telegram_principals_id_account",
        ),
        sa.UniqueConstraint(
            "bot_fingerprint", "chat_id_digest",
            name="uq_telegram_principals_bot_chat",
        ),
    )
    op.create_index(
        "ix_telegram_principals_account_id", "telegram_principals",
        ["account_id"], unique=False,
    )
    op.create_index(
        "ix_telegram_principals_account_bot_state", "telegram_principals",
        ["account_id", "bot_fingerprint", "state"], unique=False,
    )

    op.create_table(
        "telegram_conversation_bindings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("principal_id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "version >= 1", name="ck_telegram_conversation_bindings_version",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id", "account_id"],
            ["telegram_principals.id", "telegram_principals.account_id"],
            ondelete="CASCADE",
            name="fk_telegram_binding_principal_account",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "principal_id", name="uq_telegram_conversation_principal",
        ),
    )
    op.create_index(
        "ix_telegram_conversation_bindings_principal_id",
        "telegram_conversation_bindings", ["principal_id"], unique=False,
    )
    op.create_index(
        "ix_telegram_conversation_bindings_account_id",
        "telegram_conversation_bindings", ["account_id"], unique=False,
    )

    op.create_table(
        "telegram_link_codes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("bot_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("code_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "digest_scheme", sa.String(length=32), nullable=False,
            server_default="hmac_sha256_v1",
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "digest_scheme IN ('hmac_sha256_v1', 'legacy_sha256_v1')",
            name="ck_telegram_link_codes_digest_scheme",
        ),
        sa.CheckConstraint(
            "length(bot_fingerprint) = 64 AND length(code_digest) = 64",
            name="ck_telegram_link_codes_digests",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "bot_fingerprint", "code_digest",
            name="uq_telegram_link_codes_bot_digest",
        ),
    )
    op.create_index(
        "ix_telegram_link_codes_account_id", "telegram_link_codes",
        ["account_id"], unique=False,
    )
    op.create_index(
        "ix_telegram_link_codes_expires_at", "telegram_link_codes",
        ["expires_at"], unique=False,
    )
    op.create_index(
        "ix_telegram_link_codes_consumed_at", "telegram_link_codes",
        ["consumed_at"], unique=False,
    )
    op.create_index(
        "ix_telegram_link_codes_invalidated_at", "telegram_link_codes",
        ["invalidated_at"], unique=False,
    )
    op.create_index(
        "ix_telegram_link_codes_account_active", "telegram_link_codes",
        ["account_id", "bot_fingerprint", "consumed_at", "invalidated_at"],
        unique=False,
    )
    op.create_index(
        "uq_telegram_link_codes_account_live", "telegram_link_codes",
        ["account_id", "bot_fingerprint"], unique=True,
        sqlite_where=sa.text(
            "consumed_at IS NULL AND invalidated_at IS NULL"
        ),
        postgresql_where=sa.text(
            "consumed_at IS NULL AND invalidated_at IS NULL"
        ),
    )

    op.create_table(
        "telegram_identity_import_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_kind", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="pending"),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_telegram_identity_import_runs_state",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_telegram_identity_import_runs_digest",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_kind", name="uq_telegram_identity_import_source_kind",
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    retained = bind.execute(sa.text("""
        SELECT
          (SELECT COUNT(*) FROM telegram_principals)
          + (SELECT COUNT(*) FROM telegram_conversation_bindings)
          + (SELECT COUNT(*) FROM telegram_link_codes)
          + (SELECT COUNT(*) FROM telegram_identity_import_runs)
    """)).scalar_one()
    if int(retained or 0):
        raise RuntimeError(
            "Telegram identity authority contains data; export or unlink it "
            "before downgrading revision 20260721_0006"
        )
    op.drop_table("telegram_identity_import_runs")
    op.drop_index(
        "uq_telegram_link_codes_account_live",
        table_name="telegram_link_codes",
    )
    op.drop_index(
        "ix_telegram_link_codes_account_active",
        table_name="telegram_link_codes",
    )
    op.drop_index(
        "ix_telegram_link_codes_invalidated_at",
        table_name="telegram_link_codes",
    )
    op.drop_index(
        "ix_telegram_link_codes_consumed_at", table_name="telegram_link_codes",
    )
    op.drop_index(
        "ix_telegram_link_codes_expires_at", table_name="telegram_link_codes",
    )
    op.drop_index(
        "ix_telegram_link_codes_account_id", table_name="telegram_link_codes",
    )
    op.drop_table("telegram_link_codes")
    op.drop_index(
        "ix_telegram_conversation_bindings_account_id",
        table_name="telegram_conversation_bindings",
    )
    op.drop_index(
        "ix_telegram_conversation_bindings_principal_id",
        table_name="telegram_conversation_bindings",
    )
    op.drop_table("telegram_conversation_bindings")
    op.drop_index(
        "ix_telegram_principals_account_bot_state",
        table_name="telegram_principals",
    )
    op.drop_index(
        "ix_telegram_principals_account_id", table_name="telegram_principals",
    )
    op.drop_table("telegram_principals")
