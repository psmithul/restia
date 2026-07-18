"""Create database-fenced singleton runtime leadership.

Revision ID: 20260730_0015
Revises: 20260729_0014

Every replica may start every worker role in shared mode, but a database-time
lease and monotonically increasing fencing token select exactly one active
holder.  Rows contain ephemeral coordination state only; no user content or
connector credentials are stored here.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260730_0015"
down_revision: Union[str, Sequence[str], None] = "20260729_0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


RUNTIME_LEADERSHIP_REQUIRED_TABLES = frozenset({
    "runtime_worker_leases",
})

RUNTIME_LEADERSHIP_REQUIRED_COLUMNS = {
    "runtime_worker_leases": frozenset({
        "lease_name", "holder_id", "fencing_token", "lease_expires_at",
        "heartbeat_at", "version", "created_at", "updated_at",
    }),
}


def upgrade() -> None:
    op.create_table(
        "runtime_worker_leases",
        sa.Column("lease_name", sa.String(length=128), nullable=False),
        sa.Column("holder_id", sa.String(length=128), nullable=True),
        sa.Column(
            "fencing_token", sa.BigInteger(), nullable=False, server_default="0",
        ),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "fencing_token >= 0 AND version >= 1",
            name="ck_runtime_worker_leases_fencing",
        ),
        sa.CheckConstraint(
            "(holder_id IS NULL AND lease_expires_at IS NULL) OR "
            "(holder_id IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_runtime_worker_leases_holder",
        ),
        sa.PrimaryKeyConstraint("lease_name"),
    )
    op.create_index(
        "ix_runtime_worker_leases_holder_id",
        "runtime_worker_leases", ["holder_id"], unique=False,
    )
    op.create_index(
        "ix_runtime_worker_leases_lease_expires_at",
        "runtime_worker_leases", ["lease_expires_at"], unique=False,
    )


def downgrade() -> None:
    # Lease rows are rebuildable process-coordination state.  Downgrading to a
    # revision that cannot enter shared mode drops no user or connector data.
    op.drop_index(
        "ix_runtime_worker_leases_lease_expires_at",
        table_name="runtime_worker_leases",
    )
    op.drop_index(
        "ix_runtime_worker_leases_holder_id",
        table_name="runtime_worker_leases",
    )
    op.drop_table("runtime_worker_leases")
