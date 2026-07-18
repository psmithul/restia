"""Durable authority for recurring encrypted-backup attempts."""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
)

from core.database import Base, TimestampMixin


class BackupRun(TimestampMixin, Base):
    """One deployment-level backup attempt with secret-free diagnostics."""

    __tablename__ = "backup_runs"

    id = Column(String(36), primary_key=True)
    run_key = Column(String(64), nullable=False)
    trigger = Column(String(24), nullable=False, default="scheduled")
    database_mode = Column(String(24), nullable=False)
    state = Column(String(16), nullable=False, default="running")
    archive_name = Column(String(255), nullable=True)
    archive_bytes = Column(BigInteger, nullable=True)
    encrypted = Column(Boolean, nullable=False, default=False)
    verified = Column(Boolean, nullable=False, default=False)
    error_code = Column(String(64), nullable=True)
    error_detail = Column(Text, nullable=True)
    started_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("run_key", name="uq_backup_run_key"),
        CheckConstraint(
            "trigger IN ('scheduled', 'manual')",
            name="ck_backup_run_trigger",
        ),
        CheckConstraint(
            "database_mode IN ('local-single', 'shared')",
            name="ck_backup_run_database_mode",
        ),
        CheckConstraint(
            "state IN ('running', 'completed', 'failed')",
            name="ck_backup_run_state",
        ),
        CheckConstraint(
            "archive_bytes IS NULL OR archive_bytes >= 0",
            name="ck_backup_run_archive_bytes",
        ),
        CheckConstraint(
            "((state = 'running' AND completed_at IS NULL) OR "
            "(state IN ('completed', 'failed') AND completed_at IS NOT NULL))",
            name="ck_backup_run_completion_state",
        ),
        CheckConstraint(
            "(state <> 'completed' OR (archive_name IS NOT NULL "
            "AND archive_bytes IS NOT NULL AND encrypted IS TRUE "
            "AND verified IS TRUE AND error_code IS NULL))",
            name="ck_backup_run_success_evidence",
        ),
        CheckConstraint(
            "(state <> 'failed' OR error_code IS NOT NULL)",
            name="ck_backup_run_failure_evidence",
        ),
        Index("ix_backup_runs_started_at", "started_at"),
        Index("ix_backup_runs_state_started", "state", "started_at"),
    )


__all__ = ["BackupRun"]
