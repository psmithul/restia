"""Database authority for passkeys and user-verification ceremonies.

Credential public keys and opaque credential identifiers are not secrets, but
challenges are encrypted and every ceremony is bound to one account and one
database-backed Restia session.  A successful assertion records a short-lived
user-verification grant on that exact session; an ordinary bearer token or
cookie is never treated as biometric proof.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    LargeBinary,
    String,
    UniqueConstraint,
)

from core.database import Base, EncryptedText, TimestampMixin


class WebAuthnCredential(TimestampMixin, Base):
    """One server-verified public-key credential owned by an account."""

    __tablename__ = "webauthn_credentials"

    id = Column(String(36), primary_key=True)
    account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    credential_id = Column(String(2048), nullable=False)
    public_key = Column(LargeBinary, nullable=False)
    sign_count = Column(BigInteger, nullable=False, default=0)
    transports = Column(JSON, nullable=False, default=list)
    device_type = Column(String(32), nullable=False, default="unknown")
    backed_up = Column(Boolean, nullable=False, default=False)
    label = Column(String(160), nullable=False, default="Passkey")
    state = Column(String(16), nullable=False, default="active")
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("credential_id", name="uq_webauthn_credential_id"),
        CheckConstraint("sign_count >= 0", name="ck_webauthn_credential_sign_count"),
        CheckConstraint(
            "state IN ('active', 'revoked')",
            name="ck_webauthn_credential_state",
        ),
        CheckConstraint(
            "((state = 'active' AND revoked_at IS NULL) OR "
            "(state = 'revoked' AND revoked_at IS NOT NULL))",
            name="ck_webauthn_credential_revoke_state",
        ),
        Index(
            "ix_webauthn_credential_account_state",
            "account_id", "state", "created_at",
        ),
    )


class WebAuthnChallenge(Base):
    """Single-use ceremony challenge bound to an authenticated session."""

    __tablename__ = "webauthn_challenges"

    id = Column(String(36), primary_key=True)
    account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    auth_session_id = Column(
        String(36), ForeignKey("auth_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    purpose = Column(String(24), nullable=False)
    challenge = Column(EncryptedText, nullable=False)
    rp_id = Column(String(255), nullable=False)
    expected_origin = Column(String(2048), nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    consumed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('registration', 'unlock')",
            name="ck_webauthn_challenge_purpose",
        ),
        Index(
            "ix_webauthn_challenge_session_purpose",
            "auth_session_id", "purpose", "expires_at",
        ),
    )


__all__ = ["WebAuthnChallenge", "WebAuthnCredential"]
