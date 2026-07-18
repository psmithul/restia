"""Canonical SQL models for profile-owned configuration authority.

The models live in an isolated module while the shared-schema migration is
being sequenced.  Importing this module registers the tables on the existing
``core.database.Base`` metadata without creating them implicitly.
"""

from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)

from core.database import Base, EncryptedJSON, TimestampMixin


class _NullableEncryptedJSON(EncryptedJSON):
    """Encrypted JSON whose Python ``None`` remains SQL NULL.

    SQLAlchemy's generic JSON type normally persists ``None`` as a JSON
    ``null`` value.  The visibility constraint needs the unused payload column
    to be a real SQL NULL on both SQLite and PostgreSQL.
    """

    impl = JSON(none_as_null=True)
    cache_ok = True


class ProfileConfiguration(TimestampMixin, Base):
    """One versioned configuration value owned by an immutable account."""

    __tablename__ = "profile_configurations"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    namespace = Column(String(32), nullable=False)
    key = Column(String(160), nullable=False)
    visibility = Column(String(16), nullable=False, default="private")
    public_value = Column(JSON(none_as_null=True), nullable=True)
    private_value = Column(_NullableEncryptedJSON, nullable=True)
    state = Column(String(16), nullable=False, default="active")
    source = Column(String(32), nullable=False, default="api")
    updated_interface = Column(String(32), nullable=False, default="domain_service")
    version = Column(Integer, nullable=False, default=1)
    deleted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "namespace", "key",
            name="uq_profile_configuration_owner_namespace_key",
        ),
        CheckConstraint(
            "namespace IN ('setting', 'preference', 'feature', 'integration')",
            name="ck_profile_configuration_namespace",
        ),
        CheckConstraint(
            "visibility IN ('public', 'private')",
            name="ck_profile_configuration_visibility",
        ),
        CheckConstraint(
            "state IN ('active', 'deleted')",
            name="ck_profile_configuration_state",
        ),
        CheckConstraint(
            "source IN ('api', 'browser', 'cli', 'telegram', 'internal_tool', "
            "'domain_service', 'legacy_import')",
            name="ck_profile_configuration_source",
        ),
        CheckConstraint(
            "((visibility = 'public' AND public_value IS NOT NULL "
            "AND private_value IS NULL) OR "
            "(visibility = 'private' AND private_value IS NOT NULL "
            "AND public_value IS NULL))",
            name="ck_profile_configuration_payload_visibility",
        ),
        CheckConstraint("version >= 1", name="ck_profile_configuration_version"),
        CheckConstraint(
            "((state = 'active' AND deleted_at IS NULL) OR "
            "(state = 'deleted' AND deleted_at IS NOT NULL))",
            name="ck_profile_configuration_delete_state",
        ),
        Index(
            "ix_profile_configuration_owner_namespace_state",
            "owner_id", "namespace", "state", "key",
        ),
    )


class ProfileConfigurationMutation(Base):
    """Replay ledger for idempotent cross-interface configuration writes."""

    __tablename__ = "profile_configuration_mutations"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    configuration_id = Column(
        String(36), ForeignKey("profile_configurations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    idempotency_digest = Column(String(64), nullable=False)
    request_digest = Column(String(64), nullable=False)
    operation = Column(String(16), nullable=False)
    result_version = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "idempotency_digest",
            name="uq_profile_configuration_mutation_idempotency",
        ),
        CheckConstraint(
            "operation IN ('put', 'delete')",
            name="ck_profile_configuration_mutation_operation",
        ),
        CheckConstraint(
            "length(idempotency_digest) = 64 AND length(request_digest) = 64",
            name="ck_profile_configuration_mutation_digests",
        ),
        CheckConstraint(
            "result_version >= 1",
            name="ck_profile_configuration_mutation_version",
        ),
    )


class ProfileConfigurationImportRun(TimestampMixin, Base):
    """Bounded, non-destructive legacy JSON import checkpoint."""

    __tablename__ = "profile_configuration_import_runs"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source_kind = Column(String(48), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    state = Column(String(16), nullable=False, default="pending")
    imported_count = Column(Integer, nullable=False, default=0)
    skipped_count = Column(Integer, nullable=False, default=0)
    details = Column(EncryptedJSON, nullable=False, default=dict)
    completed_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "source_kind", "source_sha256",
            name="uq_profile_configuration_import_source",
        ),
        CheckConstraint(
            "source_kind IN ('settings_json', 'user_prefs_json', "
            "'features_json', 'integrations_json')",
            name="ck_profile_configuration_import_source_kind",
        ),
        CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_profile_configuration_import_state",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_profile_configuration_import_digest",
        ),
        CheckConstraint(
            "imported_count >= 0 AND skipped_count >= 0",
            name="ck_profile_configuration_import_counts",
        ),
        CheckConstraint(
            "version >= 1", name="ck_profile_configuration_import_version",
        ),
        Index(
            "ix_profile_configuration_import_owner_state",
            "owner_id", "state", "created_at",
        ),
    )


__all__ = [
    "ProfileConfiguration",
    "ProfileConfigurationImportRun",
    "ProfileConfigurationMutation",
]
