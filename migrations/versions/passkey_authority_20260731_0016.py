"""Create server-verified passkey and device-unlock authority.

Revision ID: 20260731_0016
Revises: 20260730_0015

WebAuthn challenges are encrypted, single-use, and bound to one database
session.  Successful assertions write only a short-lived verification grant to
that session; ordinary cookies and API tokens remain insufficient proof of a
biometric or security-key gesture.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "20260731_0016"
down_revision: Union[str, Sequence[str], None] = "20260730_0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PASSKEY_REQUIRED_TABLES = frozenset({
    "webauthn_credentials",
    "webauthn_challenges",
})

PASSKEY_REQUIRED_COLUMNS = {
    "auth_sessions": frozenset({
        "user_verified_at",
        "user_verification_expires_at",
        "user_verification_method",
        "user_verification_credential_id",
    }),
    "webauthn_credentials": frozenset({
        "id", "account_id", "credential_id", "public_key", "sign_count",
        "transports", "device_type", "backed_up", "label", "state",
        "last_used_at", "revoked_at", "created_at", "updated_at",
    }),
    "webauthn_challenges": frozenset({
        "id", "account_id", "auth_session_id", "purpose", "challenge",
        "rp_id", "expected_origin", "expires_at", "consumed_at", "created_at",
    }),
}


def upgrade() -> None:
    op.add_column("auth_sessions", sa.Column("user_verified_at", sa.DateTime(), nullable=True))
    op.add_column(
        "auth_sessions",
        sa.Column("user_verification_expires_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "auth_sessions",
        sa.Column("user_verification_method", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "auth_sessions",
        sa.Column("user_verification_credential_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_auth_sessions_user_verification_expires_at",
        "auth_sessions", ["user_verification_expires_at"], unique=False,
    )
    with op.batch_alter_table("auth_sessions") as batch:
        batch.create_check_constraint(
            "ck_auth_sessions_user_verification_state",
            "((user_verified_at IS NULL AND user_verification_expires_at IS NULL "
            "AND user_verification_method IS NULL "
            "AND user_verification_credential_id IS NULL) OR "
            "(user_verified_at IS NOT NULL AND user_verification_expires_at IS NOT NULL "
            "AND user_verification_method = 'webauthn' "
            "AND user_verification_credential_id IS NOT NULL))",
        )

    op.create_table(
        "webauthn_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("credential_id", sa.String(length=2048), nullable=False),
        sa.Column("public_key", sa.LargeBinary(), nullable=False),
        sa.Column("sign_count", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("transports", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("device_type", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("backed_up", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("label", sa.String(length=160), nullable=False, server_default="Passkey"),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("sign_count >= 0", name="ck_webauthn_credential_sign_count"),
        sa.CheckConstraint(
            "state IN ('active', 'revoked')",
            name="ck_webauthn_credential_state",
        ),
        sa.CheckConstraint(
            "((state = 'active' AND revoked_at IS NULL) OR "
            "(state = 'revoked' AND revoked_at IS NOT NULL))",
            name="ck_webauthn_credential_revoke_state",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("credential_id", name="uq_webauthn_credential_id"),
    )
    op.create_index(
        "ix_webauthn_credentials_account_id",
        "webauthn_credentials", ["account_id"], unique=False,
    )
    op.create_index(
        "ix_webauthn_credential_account_state",
        "webauthn_credentials", ["account_id", "state", "created_at"], unique=False,
    )

    op.create_table(
        "webauthn_challenges",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("auth_session_id", sa.String(length=36), nullable=False),
        sa.Column("purpose", sa.String(length=24), nullable=False),
        sa.Column("challenge", sa.Text(), nullable=False),
        sa.Column("rp_id", sa.String(length=255), nullable=False),
        sa.Column("expected_origin", sa.String(length=2048), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "purpose IN ('registration', 'unlock')",
            name="ck_webauthn_challenge_purpose",
        ),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["auth_session_id"], ["auth_sessions.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_webauthn_challenges_account_id",
        "webauthn_challenges", ["account_id"], unique=False,
    )
    op.create_index(
        "ix_webauthn_challenges_auth_session_id",
        "webauthn_challenges", ["auth_session_id"], unique=False,
    )
    op.create_index(
        "ix_webauthn_challenges_expires_at",
        "webauthn_challenges", ["expires_at"], unique=False,
    )
    op.create_index(
        "ix_webauthn_challenge_session_purpose",
        "webauthn_challenges", ["auth_session_id", "purpose", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_webauthn_challenge_session_purpose", table_name="webauthn_challenges",
    )
    op.drop_index("ix_webauthn_challenges_expires_at", table_name="webauthn_challenges")
    op.drop_index("ix_webauthn_challenges_auth_session_id", table_name="webauthn_challenges")
    op.drop_index("ix_webauthn_challenges_account_id", table_name="webauthn_challenges")
    op.drop_table("webauthn_challenges")
    op.drop_index("ix_webauthn_credential_account_state", table_name="webauthn_credentials")
    op.drop_index("ix_webauthn_credentials_account_id", table_name="webauthn_credentials")
    op.drop_table("webauthn_credentials")
    op.drop_index(
        "ix_auth_sessions_user_verification_expires_at", table_name="auth_sessions",
    )
    with op.batch_alter_table("auth_sessions") as batch:
        batch.drop_constraint(
            "ck_auth_sessions_user_verification_state", type_="check",
        )
        batch.drop_column("user_verification_credential_id")
        batch.drop_column("user_verification_method")
        batch.drop_column("user_verification_expires_at")
        batch.drop_column("user_verified_at")
