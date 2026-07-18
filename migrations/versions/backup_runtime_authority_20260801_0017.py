"""Create durable recurring encrypted-backup run authority.

Revision ID: 20260801_0017
Revises: 20260731_0016
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260801_0017"
down_revision: Union[str, Sequence[str], None] = "20260731_0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BACKUP_RUNTIME_REQUIRED_TABLES = frozenset({"backup_runs"})
BACKUP_RUNTIME_REQUIRED_COLUMNS = {
    "backup_runs": frozenset({
        "id", "run_key", "trigger", "database_mode", "state",
        "archive_name", "archive_bytes", "encrypted", "verified",
        "error_code", "error_detail", "started_at", "completed_at",
        "created_at", "updated_at",
    }),
}


def upgrade() -> None:
    op.create_table(
        "backup_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_key", sa.String(length=64), nullable=False),
        sa.Column("trigger", sa.String(length=24), nullable=False),
        sa.Column("database_mode", sa.String(length=24), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("archive_name", sa.String(length=255), nullable=True),
        sa.Column("archive_bytes", sa.BigInteger(), nullable=True),
        sa.Column("encrypted", sa.Boolean(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "trigger IN ('scheduled', 'manual')", name="ck_backup_run_trigger",
        ),
        sa.CheckConstraint(
            "database_mode IN ('local-single', 'shared')",
            name="ck_backup_run_database_mode",
        ),
        sa.CheckConstraint(
            "state IN ('running', 'completed', 'failed')",
            name="ck_backup_run_state",
        ),
        sa.CheckConstraint(
            "archive_bytes IS NULL OR archive_bytes >= 0",
            name="ck_backup_run_archive_bytes",
        ),
        sa.CheckConstraint(
            "((state = 'running' AND completed_at IS NULL) OR "
            "(state IN ('completed', 'failed') AND completed_at IS NOT NULL))",
            name="ck_backup_run_completion_state",
        ),
        sa.CheckConstraint(
            "(state <> 'completed' OR (archive_name IS NOT NULL "
            "AND archive_bytes IS NOT NULL AND encrypted IS TRUE "
            "AND verified IS TRUE AND error_code IS NULL))",
            name="ck_backup_run_success_evidence",
        ),
        sa.CheckConstraint(
            "(state <> 'failed' OR error_code IS NOT NULL)",
            name="ck_backup_run_failure_evidence",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_key", name="uq_backup_run_key"),
    )
    op.create_index(
        "ix_backup_runs_started_at", "backup_runs", ["started_at"], unique=False,
    )
    op.create_index(
        "ix_backup_runs_state_started", "backup_runs", ["state", "started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_backup_runs_state_started", table_name="backup_runs")
    op.drop_index("ix_backup_runs_started_at", table_name="backup_runs")
    op.drop_table("backup_runs")
