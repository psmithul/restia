"""SQL models for canonical assistant-chat upload metadata."""

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


class ChatUploadMetadata(TimestampMixin, Base):
    """One Account.id-owned blob descriptor with encrypted private metadata."""

    __tablename__ = "chat_upload_metadata"

    id = Column(String(255), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    content_digest = Column(String(64), nullable=False)
    blob_key = Column(String(512), nullable=False)
    payload = Column(EncryptedJSON, nullable=False, default=dict)
    state = Column(String(16), nullable=False, default="active")
    last_accessed_at = Column(DateTime, nullable=False)
    retention_until = Column(DateTime, nullable=False, index=True)
    deleted_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "content_digest",
            name="uq_chat_upload_metadata_owner_content",
        ),
        CheckConstraint(
            "length(content_digest) = 64",
            name="ck_chat_upload_metadata_content_digest",
        ),
        CheckConstraint(
            "state IN ('active', 'tombstoned')",
            name="ck_chat_upload_metadata_state",
        ),
        CheckConstraint(
            "((state = 'active' AND deleted_at IS NULL) OR "
            "(state = 'tombstoned' AND deleted_at IS NOT NULL))",
            name="ck_chat_upload_metadata_delete_state",
        ),
        CheckConstraint("version >= 1", name="ck_chat_upload_metadata_version"),
        Index(
            "ix_chat_upload_metadata_owner_state_retention",
            "owner_id", "state", "retention_until",
        ),
    )


class ChatUploadMetadataImportRun(TimestampMixin, Base):
    """Encrypted checkpoint for a bounded, source-preserving JSON import."""

    __tablename__ = "chat_upload_metadata_import_runs"

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
            name="uq_chat_upload_metadata_import_source",
        ),
        CheckConstraint(
            "source_kind = 'uploads_json'",
            name="ck_chat_upload_metadata_import_source_kind",
        ),
        CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_chat_upload_metadata_import_state",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_chat_upload_metadata_import_digest",
        ),
        CheckConstraint(
            "imported_count >= 0 AND skipped_count >= 0",
            name="ck_chat_upload_metadata_import_counts",
        ),
        CheckConstraint(
            "version >= 1", name="ck_chat_upload_metadata_import_version",
        ),
        Index(
            "ix_chat_upload_metadata_import_owner_state",
            "owner_id", "state", "created_at",
        ),
    )


__all__ = ["ChatUploadMetadata", "ChatUploadMetadataImportRun"]
