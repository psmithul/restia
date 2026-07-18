"""Create owner-scoped read-only provider polling cursors.

Revision ID: 20260802_0018
Revises: 20260801_0017
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260802_0018"
down_revision: Union[str, Sequence[str], None] = "20260801_0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


COMMUNICATION_POLLING_REQUIRED_TABLES = frozenset({"communication_poll_states"})
COMMUNICATION_POLLING_REQUIRED_COLUMNS = {
    "communication_poll_states": frozenset({
        "id", "owner_id", "configuration_id", "provider", "cursor",
        "state", "last_attempt_at", "last_success_at", "error_code",
        "consecutive_failures", "imported_count", "version", "created_at",
        "updated_at",
    }),
}


def upgrade() -> None:
    op.create_table(
        "communication_poll_states",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("configuration_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=16), nullable=False),
        sa.Column("cursor", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("imported_count", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "provider IN ('slack', 'twilio')",
            name="ck_communication_poll_provider",
        ),
        sa.CheckConstraint(
            "state IN ('idle', 'healthy', 'error')",
            name="ck_communication_poll_state",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0 AND imported_count >= 0 AND version >= 1",
            name="ck_communication_poll_counters",
        ),
        sa.CheckConstraint(
            "((state = 'error' AND error_code IS NOT NULL) OR "
            "(state IN ('idle', 'healthy') AND error_code IS NULL))",
            name="ck_communication_poll_error_state",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["configuration_id"], ["profile_configurations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "configuration_id", "provider",
            name="uq_communication_poll_configuration_provider",
        ),
    )
    op.create_index(
        "ix_communication_poll_states_owner_id",
        "communication_poll_states", ["owner_id"], unique=False,
    )
    op.create_index(
        "ix_communication_poll_states_configuration_id",
        "communication_poll_states", ["configuration_id"], unique=False,
    )
    op.create_index(
        "ix_communication_poll_owner_provider_state",
        "communication_poll_states", ["owner_id", "provider", "state", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_communication_poll_owner_provider_state",
        table_name="communication_poll_states",
    )
    op.drop_index(
        "ix_communication_poll_states_configuration_id",
        table_name="communication_poll_states",
    )
    op.drop_index(
        "ix_communication_poll_states_owner_id",
        table_name="communication_poll_states",
    )
    op.drop_table("communication_poll_states")
