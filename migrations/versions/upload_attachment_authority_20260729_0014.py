"""Create canonical chat-upload metadata and import authority.

Revision ID: 20260729_0014
Revises: 20260728_0013

Upload bytes remain behind the validated filesystem blob contract.  This
revision moves mutable uploads.json metadata into encrypted, Account.id-owned
SQL rows with owner/hash idempotency, retention tombstones, and CAS versions.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260729_0014"
down_revision: Union[str, Sequence[str], None] = "20260728_0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


UPLOAD_METADATA_REQUIRED_TABLES = frozenset({
    "chat_upload_metadata",
    "chat_upload_metadata_import_runs",
})

UPLOAD_METADATA_REQUIRED_COLUMNS = {
    "chat_upload_metadata": frozenset({
        "id", "owner_id", "content_digest", "blob_key", "payload", "state",
        "last_accessed_at", "retention_until", "deleted_at", "version",
        "created_at", "updated_at",
    }),
    "chat_upload_metadata_import_runs": frozenset({
        "id", "owner_id", "source_kind", "source_sha256", "state",
        "imported_count", "skipped_count", "details", "completed_at",
        "version", "created_at", "updated_at",
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
        "chat_upload_metadata",
        sa.Column("id", sa.String(length=255), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("content_digest", sa.String(length=64), nullable=False),
        sa.Column("blob_key", sa.String(length=512), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column(
            "state", sa.String(length=16), nullable=False,
            server_default="active",
        ),
        sa.Column("last_accessed_at", sa.DateTime(), nullable=False),
        sa.Column("retention_until", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "length(content_digest) = 64",
            name="ck_chat_upload_metadata_content_digest",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'tombstoned')",
            name="ck_chat_upload_metadata_state",
        ),
        sa.CheckConstraint(
            "((state = 'active' AND deleted_at IS NULL) OR "
            "(state = 'tombstoned' AND deleted_at IS NOT NULL))",
            name="ck_chat_upload_metadata_delete_state",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_chat_upload_metadata_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "content_digest",
            name="uq_chat_upload_metadata_owner_content",
        ),
    )
    op.create_index(
        "ix_chat_upload_metadata_owner_id",
        "chat_upload_metadata", ["owner_id"], unique=False,
    )
    op.create_index(
        "ix_chat_upload_metadata_retention_until",
        "chat_upload_metadata", ["retention_until"], unique=False,
    )
    op.create_index(
        "ix_chat_upload_metadata_owner_state_retention",
        "chat_upload_metadata",
        ["owner_id", "state", "retention_until"],
        unique=False,
    )

    op.create_table(
        "chat_upload_metadata_import_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("source_kind", sa.String(length=48), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "state", sa.String(length=16), nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "imported_count", sa.Integer(), nullable=False, server_default="0",
        ),
        sa.Column(
            "skipped_count", sa.Integer(), nullable=False, server_default="0",
        ),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.CheckConstraint(
            "source_kind = 'uploads_json'",
            name="ck_chat_upload_metadata_import_source_kind",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_chat_upload_metadata_import_state",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_chat_upload_metadata_import_digest",
        ),
        sa.CheckConstraint(
            "imported_count >= 0 AND skipped_count >= 0",
            name="ck_chat_upload_metadata_import_counts",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_chat_upload_metadata_import_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "source_kind", "source_sha256",
            name="uq_chat_upload_metadata_import_source",
        ),
    )
    op.create_index(
        "ix_chat_upload_metadata_import_runs_owner_id",
        "chat_upload_metadata_import_runs", ["owner_id"], unique=False,
    )
    op.create_index(
        "ix_chat_upload_metadata_import_owner_state",
        "chat_upload_metadata_import_runs",
        ["owner_id", "state", "created_at"], unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    retained = bind.execute(sa.text("""
        SELECT
          (SELECT COUNT(*) FROM chat_upload_metadata)
          + (SELECT COUNT(*) FROM chat_upload_metadata_import_runs)
    """)).scalar_one()
    if int(retained or 0):
        raise RuntimeError(
            "Upload metadata authority contains retained upload or import "
            "state; export and deliberately remove it before downgrading "
            "revision 20260729_0014"
        )
    op.drop_index(
        "ix_chat_upload_metadata_import_owner_state",
        table_name="chat_upload_metadata_import_runs",
    )
    op.drop_index(
        "ix_chat_upload_metadata_import_runs_owner_id",
        table_name="chat_upload_metadata_import_runs",
    )
    op.drop_table("chat_upload_metadata_import_runs")
    op.drop_index(
        "ix_chat_upload_metadata_owner_state_retention",
        table_name="chat_upload_metadata",
    )
    op.drop_index(
        "ix_chat_upload_metadata_retention_until",
        table_name="chat_upload_metadata",
    )
    op.drop_index(
        "ix_chat_upload_metadata_owner_id",
        table_name="chat_upload_metadata",
    )
    op.drop_table("chat_upload_metadata")
