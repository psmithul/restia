"""Owner-scoped durable cursors for read-only communications providers."""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)

from core.database import Base, EncryptedJSON, TimestampMixin


class CommunicationPollState(TimestampMixin, Base):
    """One provider integration's encrypted cursor and bounded run health."""

    __tablename__ = "communication_poll_states"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    configuration_id = Column(
        String(36), ForeignKey("profile_configurations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    provider = Column(String(16), nullable=False)
    cursor = Column(EncryptedJSON, nullable=False, default=dict)
    state = Column(String(16), nullable=False, default="idle")
    last_attempt_at = Column(DateTime, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    error_code = Column(String(64), nullable=True)
    consecutive_failures = Column(Integer, nullable=False, default=0)
    imported_count = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "configuration_id", "provider",
            name="uq_communication_poll_configuration_provider",
        ),
        CheckConstraint(
            "provider IN ('slack', 'twilio')",
            name="ck_communication_poll_provider",
        ),
        CheckConstraint(
            "state IN ('idle', 'healthy', 'error')",
            name="ck_communication_poll_state",
        ),
        CheckConstraint(
            "consecutive_failures >= 0 AND imported_count >= 0 AND version >= 1",
            name="ck_communication_poll_counters",
        ),
        CheckConstraint(
            "((state = 'error' AND error_code IS NOT NULL) OR "
            "(state IN ('idle', 'healthy') AND error_code IS NULL))",
            name="ck_communication_poll_error_state",
        ),
        Index(
            "ix_communication_poll_owner_provider_state",
            "owner_id", "provider", "state", "updated_at",
        ),
    )


__all__ = ["CommunicationPollState"]
