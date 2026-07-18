"""Create canonical Account.id-owned profile configuration authority.

Revision ID: 20260728_0013
Revises: 20260727_0012

Mutable profile preferences, app settings, feature preferences, and user-created
integrations converge on versioned SQL rows. Private payloads are encrypted by
the ORM type; deployment secrets remain environment-only and are never
admitted by the service/import boundary.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260728_0013"
down_revision: Union[str, Sequence[str], None] = "20260727_0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PROFILE_CONFIGURATION_REQUIRED_TABLES = frozenset({
    "profile_configurations",
    "profile_configuration_mutations",
    "profile_configuration_import_runs",
})

PROFILE_CONFIGURATION_REQUIRED_COLUMNS = {
    "profile_configurations": frozenset({
        "id", "owner_id", "namespace", "key", "visibility",
        "public_value", "private_value", "state", "source",
        "updated_interface", "version", "deleted_at", "created_at",
        "updated_at",
    }),
    "profile_configuration_mutations": frozenset({
        "id", "owner_id", "configuration_id", "idempotency_digest",
        "request_digest", "operation", "result_version", "created_at",
    }),
    "profile_configuration_import_runs": frozenset({
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
        "profile_configurations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("namespace", sa.String(length=32), nullable=False),
        sa.Column("key", sa.String(length=160), nullable=False),
        sa.Column(
            "visibility", sa.String(length=16), nullable=False,
            server_default="private",
        ),
        sa.Column("public_value", sa.JSON(), nullable=True),
        sa.Column("private_value", sa.JSON(), nullable=True),
        sa.Column(
            "state", sa.String(length=16), nullable=False,
            server_default="active",
        ),
        sa.Column(
            "source", sa.String(length=32), nullable=False,
            server_default="api",
        ),
        sa.Column(
            "updated_interface", sa.String(length=32), nullable=False,
            server_default="domain_service",
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        *_timestamps(),
        sa.CheckConstraint(
            "namespace IN ('setting', 'preference', 'feature', 'integration')",
            name="ck_profile_configuration_namespace",
        ),
        sa.CheckConstraint(
            "visibility IN ('public', 'private')",
            name="ck_profile_configuration_visibility",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'deleted')",
            name="ck_profile_configuration_state",
        ),
        sa.CheckConstraint(
            "source IN ('api', 'browser', 'cli', 'telegram', 'internal_tool', "
            "'domain_service', 'legacy_import')",
            name="ck_profile_configuration_source",
        ),
        sa.CheckConstraint(
            "((visibility = 'public' AND public_value IS NOT NULL "
            "AND private_value IS NULL) OR "
            "(visibility = 'private' AND private_value IS NOT NULL "
            "AND public_value IS NULL))",
            name="ck_profile_configuration_payload_visibility",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_profile_configuration_version",
        ),
        sa.CheckConstraint(
            "((state = 'active' AND deleted_at IS NULL) OR "
            "(state = 'deleted' AND deleted_at IS NOT NULL))",
            name="ck_profile_configuration_delete_state",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "namespace", "key",
            name="uq_profile_configuration_owner_namespace_key",
        ),
    )
    op.create_index(
        "ix_profile_configurations_owner_id",
        "profile_configurations", ["owner_id"], unique=False,
    )
    op.create_index(
        "ix_profile_configuration_owner_namespace_state",
        "profile_configurations", ["owner_id", "namespace", "state", "key"],
        unique=False,
    )

    op.create_table(
        "profile_configuration_mutations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("configuration_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_digest", sa.String(length=64), nullable=False),
        sa.Column("request_digest", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=16), nullable=False),
        sa.Column("result_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "operation IN ('put', 'delete')",
            name="ck_profile_configuration_mutation_operation",
        ),
        sa.CheckConstraint(
            "length(idempotency_digest) = 64 AND length(request_digest) = 64",
            name="ck_profile_configuration_mutation_digests",
        ),
        sa.CheckConstraint(
            "result_version >= 1",
            name="ck_profile_configuration_mutation_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["configuration_id"], ["profile_configurations.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "idempotency_digest",
            name="uq_profile_configuration_mutation_idempotency",
        ),
    )
    op.create_index(
        "ix_profile_configuration_mutations_owner_id",
        "profile_configuration_mutations", ["owner_id"], unique=False,
    )
    op.create_index(
        "ix_profile_configuration_mutations_configuration_id",
        "profile_configuration_mutations", ["configuration_id"], unique=False,
    )

    op.create_table(
        "profile_configuration_import_runs",
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
            "source_kind IN ('settings_json', 'user_prefs_json', "
            "'features_json', 'integrations_json')",
            name="ck_profile_configuration_import_source_kind",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_profile_configuration_import_state",
        ),
        sa.CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_profile_configuration_import_digest",
        ),
        sa.CheckConstraint(
            "imported_count >= 0 AND skipped_count >= 0",
            name="ck_profile_configuration_import_counts",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_profile_configuration_import_version",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["accounts.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "owner_id", "source_kind", "source_sha256",
            name="uq_profile_configuration_import_source",
        ),
    )
    op.create_index(
        "ix_profile_configuration_import_runs_owner_id",
        "profile_configuration_import_runs", ["owner_id"], unique=False,
    )
    op.create_index(
        "ix_profile_configuration_import_owner_state",
        "profile_configuration_import_runs", ["owner_id", "state", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_profile_configuration_import_owner_state",
        table_name="profile_configuration_import_runs",
    )
    op.drop_index(
        "ix_profile_configuration_import_runs_owner_id",
        table_name="profile_configuration_import_runs",
    )
    op.drop_table("profile_configuration_import_runs")

    op.drop_index(
        "ix_profile_configuration_mutations_configuration_id",
        table_name="profile_configuration_mutations",
    )
    op.drop_index(
        "ix_profile_configuration_mutations_owner_id",
        table_name="profile_configuration_mutations",
    )
    op.drop_table("profile_configuration_mutations")

    op.drop_index(
        "ix_profile_configuration_owner_namespace_state",
        table_name="profile_configurations",
    )
    op.drop_index(
        "ix_profile_configurations_owner_id",
        table_name="profile_configurations",
    )
    op.drop_table("profile_configurations")
