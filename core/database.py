import os
import json
import hashlib
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import (
    event,
    create_engine,
    Column,
    String,
    Text,
    Boolean,
    DateTime,
    Integer,
    BigInteger,
    ForeignKey,
    ForeignKeyConstraint,
    JSON,
    Index,
    UniqueConstraint,
    CheckConstraint,
    DDL,
    func,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.types import TypeDecorator
from sqlalchemy.ext.declarative import declarative_base, declared_attr
from sqlalchemy.orm import relationship, sessionmaker, backref

from core.platform_compat import safe_chmod
from src.runtime_paths import get_app_root

logger = logging.getLogger(__name__)

# Create base class for declarative models
Base = declarative_base()


def utcnow_naive() -> datetime:
    """Return naive UTC for existing DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def project_owner_quota_lock_key(owner: str) -> str:
    """Return the stable, non-identifying mutex key for an owner quota."""

    normalized = str(owner or "").strip().lower()
    return f"project-owner:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


class TimestampMixin:
    """Mixin that adds timestamp fields to models"""
    @declared_attr
    def created_at(cls):
        return Column(DateTime, default=utcnow_naive, nullable=False)
    
    @declared_attr
    def updated_at(cls):
        return Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)

# Ensure the writable data directory exists before SQLite connects.
from src.constants import DATA_DIR, MEMORY_FILE, USER_PREFS_FILE, SETTINGS_FILE
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
safe_chmod(DATA_DIR, 0o700)


def _default_database_url() -> str:
    return f"sqlite:///{Path(DATA_DIR) / 'app.db'}"


def _normalize_sqlite_url(url: str) -> str:
    if not url.startswith("sqlite:///"):
        return url
    db_path = url.replace("sqlite:///", "", 1)
    if db_path == ":memory:" or os.path.isabs(db_path):
        return url
    return f"sqlite:///{(Path(get_app_root()) / db_path).resolve().as_posix()}"


# Get database URL from environment, default to SQLite in DATA_DIR
DATABASE_URL = _normalize_sqlite_url(os.getenv("DATABASE_URL", _default_database_url()))


def harden_database_permissions() -> None:
    """Keep the local data store private even under a permissive host umask."""
    safe_chmod(DATA_DIR, 0o700)
    if not DATABASE_URL.startswith("sqlite:///"):
        return
    db_path = DATABASE_URL.replace("sqlite:///", "", 1)
    if db_path == ":memory:":
        return
    for candidate in (db_path, db_path + "-wal", db_path + "-shm"):
        if os.path.exists(candidate):
            safe_chmod(candidate, 0o600)

# Create engine
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


# Listening on the Engine class ensures this listener fires for all Engine
# instances created within the process, not just the primary application engine.
# The isinstance(sqlite3.Connection) check ensures that this PRAGMA foreign_keys=ON
# configuration remains a no-op when using non-SQLite database backends.
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        harden_database_permissions()


class EncryptedText(TypeDecorator):
    """Text column transparently encrypted at rest via src.secret_storage.

    Writes are Fernet-encrypted (`enc:` prefix); reads decrypt back to
    plaintext, so all consumers use the column normally. Legacy plaintext
    rows pass through unchanged until their next write (a startup migration
    encrypts them). Protects the SQLite file at rest (stolen backup / leaked
    image), not a live process that can read the key.
    """
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        from src.secret_storage import encrypt
        return encrypt(value)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        from src.secret_storage import decrypt
        return decrypt(value)


class EncryptedContentText(EncryptedText):
    """Encrypted text whose input is always application/user plaintext.

    Credential columns use :class:`EncryptedText` so importing an existing
    envelope is idempotent across key rotation. Content columns cannot reserve
    any plaintext prefix—including a complete Fernet-looking string—so they
    deliberately wrap every non-empty value.
    """
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        from src.secret_storage import encrypt_plaintext
        return encrypt_plaintext(value)


class EncryptedJSON(TypeDecorator):
    """JSON-compatible values encrypted through the app's secret envelope.

    The envelope is stored as a JSON string, so existing ``JSON`` columns do
    not need a table rebuild. Legacy plaintext JSON remains readable and is
    rewritten by the startup migration below. Consumers continue to receive
    ordinary Python values.
    """

    # Keep JSON as the database-level type so existing PostgreSQL/MySQL JSON
    # columns remain writable.  The encrypted envelope is stored as a JSON
    # string; legacy rows arrive here as a dict and remain readable.
    impl = JSON
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("encrypted JSON value must be an object")
        from src.secret_storage import encrypt_plaintext
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return encrypt_plaintext(serialized)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            logger.error("Stored session headers are not a JSON object")
            return {}
        from src.secret_storage import decrypt
        plaintext = decrypt(value)
        if not plaintext:
            return {}
        try:
            decoded = json.loads(plaintext)
        except (TypeError, json.JSONDecodeError):
            logger.error("Failed to decode encrypted session headers")
            return {}
        return decoded if isinstance(decoded, dict) else {}


class Account(TimestampMixin, Base):
    """Stable internal identity shared by every Restia interface.

    Existing authentication continues to use usernames and the local auth
    store.  ``Account.id`` is the durable ownership key for new V3 domains so
    browser cookies and owner-attributed API tokens resolve to the same data,
    while a later username change does not need to rename every V3 row.
    """

    __tablename__ = "accounts"

    id = Column(String(36), primary_key=True)
    username = Column(String(160), nullable=False, unique=True, index=True)
    display_name = Column(String(160), nullable=True)
    status = Column(String(24), nullable=False, default="active")
    auth_epoch = Column(Integer, nullable=False, default=1)
    last_login_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'renaming', 'disabled', "
            "'deletion_pending', 'deleted')",
            name="ck_accounts_status",
        ),
        CheckConstraint("auth_epoch >= 1", name="ck_accounts_auth_epoch"),
        Index("ix_accounts_status", "status"),
    )


class AuthIdentity(TimestampMixin, Base):
    """One authentication-provider subject mapped to an internal account."""

    __tablename__ = "auth_identities"

    id = Column(String(36), primary_key=True)
    account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    provider = Column(String(32), nullable=False, default="local")
    # Provider namespaces are not globally unique (for example, two Supabase
    # projects can issue the same opaque ``sub``). Every writer must bind the
    # exact trusted issuer explicitly; silently defaulting a non-local identity
    # to the local issuer would create an account-linking ambiguity.
    issuer = Column(String(500), nullable=False)
    subject = Column(String(255), nullable=False)
    state = Column(String(24), nullable=False, default="active")
    linked_at = Column(DateTime, nullable=False, default=utcnow_naive)
    last_verified_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "provider", "issuer", "subject",
            name="uq_auth_identity_provider_issuer_subject",
        ),
        CheckConstraint(
            "state IN ('active', 'disabled', 'unlinked')",
            name="ck_auth_identities_state",
        ),
        Index("ix_auth_identity_account_provider", "account_id", "provider"),
        Index("ix_auth_identity_issuer_subject", "issuer", "subject"),
    )


class AuthPolicy(TimestampMixin, Base):
    """Singleton policy row used to serialize global auth decisions."""

    __tablename__ = "auth_policy"

    id = Column(String(32), primary_key=True, default="global")
    signup_enabled = Column(Boolean, nullable=False, default=False)
    bootstrap_completed = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_auth_policy_version"),
    )


class LocalCredential(TimestampMixin, Base):
    """Password credential attached to an immutable account UUID."""

    __tablename__ = "local_credentials"

    id = Column(String(36), primary_key=True)
    account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    password_hash = Column(String(255), nullable=False)
    algorithm = Column(String(32), nullable=False, default="bcrypt")
    version = Column(Integer, nullable=False, default=1)
    password_changed_at = Column(DateTime, nullable=False, default=utcnow_naive)

    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_local_credentials_version"),
    )


class MfaFactor(TimestampMixin, Base):
    """One persisted multi-factor credential for an account."""

    __tablename__ = "mfa_factors"

    id = Column(String(36), primary_key=True)
    account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    kind = Column(String(32), nullable=False, default="totp")
    state = Column(String(24), nullable=False, default="pending")
    secret = Column(EncryptedText, nullable=True)
    pending_secret = Column(EncryptedText, nullable=True)
    confirmed_at = Column(DateTime, nullable=True)
    last_used_step = Column(Integer, nullable=True)

    __table_args__ = (
        UniqueConstraint("account_id", "kind", name="uq_mfa_factor_account_kind"),
        CheckConstraint(
            "state IN ('pending', 'active', 'disabled')",
            name="ck_mfa_factors_state",
        ),
    )


class MfaRecoveryCode(TimestampMixin, Base):
    """One one-time recovery credential; plaintext is never persisted."""

    __tablename__ = "mfa_recovery_codes"

    id = Column(String(36), primary_key=True)
    factor_id = Column(
        String(36), ForeignKey("mfa_factors.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    code_hash = Column(String(255), nullable=False)
    digest_scheme = Column(String(32), nullable=False, default="hmac_sha256_v1")
    used_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("factor_id", "code_hash", name="uq_mfa_recovery_code"),
        Index("ix_mfa_recovery_factor_used", "factor_id", "used_at"),
    )


class AccountRole(Base):
    """Global Restia role assignment, separate from mutable usernames."""

    __tablename__ = "account_roles"

    id = Column(String(36), primary_key=True)
    account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role = Column(String(64), nullable=False)
    granted_by_account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True,
    )
    granted_at = Column(DateTime, nullable=False, default=utcnow_naive)

    __table_args__ = (
        UniqueConstraint("account_id", "role", name="uq_account_role"),
        CheckConstraint("length(role) > 0", name="ck_account_roles_role"),
        Index("ix_account_roles_role", "role"),
    )


class AccountCapability(TimestampMixin, Base):
    """Stored non-admin capability overrides for one account."""

    __tablename__ = "account_capabilities"

    account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    capabilities = Column(JSON, nullable=False, default=dict)


class AuthSession(TimestampMixin, Base):
    """Database-backed browser/native session with a protected token digest."""

    __tablename__ = "auth_sessions"

    id = Column(String(36), primary_key=True)
    account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    token_digest = Column(String(128), nullable=False, unique=True, index=True)
    digest_scheme = Column(String(32), nullable=False, default="hmac_sha256_v1")
    auth_epoch = Column(Integer, nullable=False, default=1)
    expires_at = Column(DateTime, nullable=False, index=True)
    revoked_at = Column(DateTime, nullable=True, index=True)
    last_seen_at = Column(DateTime, nullable=True)
    interface = Column(String(32), nullable=False, default="web")
    auth_method = Column(String(32), nullable=False, default="local")
    source_identity_id = Column(
        String(36), ForeignKey("auth_identities.id", ondelete="SET NULL"),
        nullable=True,
    )
    external_session_id = Column(String(255), nullable=True)

    __table_args__ = (
        CheckConstraint("auth_epoch >= 1", name="ck_auth_sessions_auth_epoch"),
        Index(
            "ix_auth_sessions_account_revoked_expires",
            "account_id", "revoked_at", "expires_at",
        ),
    )


class RetiredAuthSubject(Base):
    """Permanent reservation for a former authentication subject."""

    __tablename__ = "retired_auth_subjects"

    id = Column(String(36), primary_key=True)
    provider = Column(String(32), nullable=False)
    issuer = Column(String(500), nullable=False)
    subject = Column(String(255), nullable=False)
    account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True,
    )
    reason = Column(String(64), nullable=False, default="retired")
    retired_at = Column(DateTime, nullable=False, default=utcnow_naive)

    __table_args__ = (
        UniqueConstraint(
            "provider", "issuer", "subject",
            name="uq_retired_auth_subject",
        ),
        Index("ix_retired_auth_subject_account", "account_id"),
    )


class AuthImportRun(TimestampMixin, Base):
    """Redacted status for one idempotent legacy-auth import source."""

    __tablename__ = "auth_import_runs"

    id = Column(String(36), primary_key=True)
    source_kind = Column(String(64), nullable=False, unique=True)
    state = Column(String(24), nullable=False, default="pending")
    auth_sha256 = Column(String(64), nullable=True)
    sessions_sha256 = Column(String(64), nullable=True)
    backup_auth_path = Column(Text, nullable=True)
    backup_sessions_path = Column(Text, nullable=True)
    details = Column(JSON, nullable=False, default=dict)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_auth_import_runs_state",
        ),
    )


class RuntimeWorkerLease(TimestampMixin, Base):
    """Database-time lease and fencing token for one singleton runtime role."""

    __tablename__ = "runtime_worker_leases"

    lease_name = Column(String(128), primary_key=True)
    holder_id = Column(String(128), nullable=True, index=True)
    fencing_token = Column(BigInteger, nullable=False, default=0)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    heartbeat_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "fencing_token >= 0 AND version >= 1",
            name="ck_runtime_worker_leases_fencing",
        ),
        CheckConstraint(
            "(holder_id IS NULL AND lease_expires_at IS NULL) OR "
            "(holder_id IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_runtime_worker_leases_holder",
        ),
    )


class TelegramPrincipal(TimestampMixin, Base):
    """One Telegram chat principal linked to an immutable Restia account.

    Telegram chat identifiers are sensitive cross-interface correlation data.
    The exact identifier is encrypted while the keyed digest supports exact,
    bot-scoped lookup without placing the plaintext identifier in an index.
    """

    __tablename__ = "telegram_principals"

    id = Column(String(36), primary_key=True)
    account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    bot_fingerprint = Column(String(64), nullable=False)
    chat_id = Column(EncryptedContentText, nullable=False)
    chat_id_digest = Column(String(64), nullable=False)
    state = Column(String(24), nullable=False, default="linked")
    linked_at = Column(DateTime, nullable=False, default=utcnow_naive)
    revoked_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "id", "account_id", name="uq_telegram_principals_id_account",
        ),
        UniqueConstraint(
            "bot_fingerprint", "chat_id_digest",
            name="uq_telegram_principals_bot_chat",
        ),
        CheckConstraint(
            "state IN ('linked', 'unlinked')",
            name="ck_telegram_principals_state",
        ),
        CheckConstraint(
            "length(bot_fingerprint) = 64 AND length(chat_id_digest) = 64",
            name="ck_telegram_principals_digests",
        ),
        CheckConstraint("version >= 1", name="ck_telegram_principals_version"),
        Index(
            "ix_telegram_principals_account_bot_state",
            "account_id", "bot_fingerprint", "state",
        ),
    )


class TelegramConversationBinding(TimestampMixin, Base):
    """The active Restia conversation bound to one linked Telegram chat."""

    __tablename__ = "telegram_conversation_bindings"

    id = Column(String(36), primary_key=True)
    principal_id = Column(String(36), nullable=False, unique=True, index=True)
    account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    session_id = Column(EncryptedContentText, nullable=False)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        ForeignKeyConstraint(
            ["principal_id", "account_id"],
            ["telegram_principals.id", "telegram_principals.account_id"],
            ondelete="CASCADE",
            name="fk_telegram_binding_principal_account",
        ),
        CheckConstraint(
            "version >= 1", name="ck_telegram_conversation_bindings_version",
        ),
    )


class TelegramLinkCode(TimestampMixin, Base):
    """Short-lived, one-time Telegram link credential.

    Only keyed digests are written for newly issued codes.  The legacy digest
    scheme exists solely so an already-issued pre-V3 code can survive the
    verified settings import and be consumed once before expiry.
    """

    __tablename__ = "telegram_link_codes"

    id = Column(String(36), primary_key=True)
    account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    bot_fingerprint = Column(String(64), nullable=False)
    code_digest = Column(String(64), nullable=False)
    digest_scheme = Column(String(32), nullable=False, default="hmac_sha256_v1")
    expires_at = Column(DateTime, nullable=False, index=True)
    consumed_at = Column(DateTime, nullable=True, index=True)
    invalidated_at = Column(DateTime, nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint(
            "bot_fingerprint", "code_digest",
            name="uq_telegram_link_codes_bot_digest",
        ),
        CheckConstraint(
            "digest_scheme IN ('hmac_sha256_v1', 'legacy_sha256_v1')",
            name="ck_telegram_link_codes_digest_scheme",
        ),
        CheckConstraint(
            "length(bot_fingerprint) = 64 AND length(code_digest) = 64",
            name="ck_telegram_link_codes_digests",
        ),
        Index(
            "ix_telegram_link_codes_account_active",
            "account_id", "bot_fingerprint", "consumed_at", "invalidated_at",
        ),
        Index(
            "uq_telegram_link_codes_account_live",
            "account_id", "bot_fingerprint",
            unique=True,
            sqlite_where=text(
                "consumed_at IS NULL AND invalidated_at IS NULL"
            ),
            postgresql_where=text(
                "consumed_at IS NULL AND invalidated_at IS NULL"
            ),
        ),
    )


class TelegramIdentityImportRun(TimestampMixin, Base):
    """Redacted status for the one legacy Telegram settings cutover."""

    __tablename__ = "telegram_identity_import_runs"

    id = Column(String(36), primary_key=True)
    source_kind = Column(String(64), nullable=False, unique=True)
    state = Column(String(24), nullable=False, default="pending")
    source_sha256 = Column(String(64), nullable=False)
    source_path = Column(EncryptedContentText, nullable=True)
    details = Column(EncryptedJSON, nullable=False, default=dict)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_telegram_identity_import_runs_state",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_telegram_identity_import_runs_digest",
        ),
    )


class TelegramPollingState(TimestampMixin, Base):
    """Database-fenced singleton poller state for one Telegram bot."""

    __tablename__ = "telegram_polling_states"

    bot_fingerprint = Column(String(64), primary_key=True)
    next_offset = Column(BigInteger, nullable=True)
    failure_update_id = Column(BigInteger, nullable=True)
    failure_attempts = Column(Integer, nullable=False, default=0)
    lease_owner = Column(String(128), nullable=True)
    lease_token = Column(Integer, nullable=False, default=0)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        CheckConstraint(
            "length(bot_fingerprint) = 64",
            name="ck_telegram_polling_states_fingerprint",
        ),
        CheckConstraint(
            "next_offset IS NULL OR next_offset >= 0",
            name="ck_telegram_polling_states_offset",
        ),
        CheckConstraint(
            "failure_attempts >= 0",
            name="ck_telegram_polling_states_attempts",
        ),
        CheckConstraint(
            "lease_token >= 0 AND version >= 1",
            name="ck_telegram_polling_states_fencing",
        ),
    )


class TelegramDeadLetter(TimestampMixin, Base):
    """Safe metadata for one poison update resolved by the poller."""

    __tablename__ = "telegram_dead_letters"

    id = Column(String(36), primary_key=True)
    bot_fingerprint = Column(
        String(64),
        ForeignKey("telegram_polling_states.bot_fingerprint", ondelete="CASCADE"),
        nullable=False,
    )
    update_id = Column(BigInteger, nullable=False)
    error_type = Column(String(64), nullable=False)
    attempts = Column(Integer, nullable=False)
    failed_at = Column(DateTime, nullable=False, default=utcnow_naive)

    __table_args__ = (
        UniqueConstraint(
            "bot_fingerprint", "update_id",
            name="uq_telegram_dead_letters_bot_update",
        ),
        CheckConstraint(
            "update_id >= 0 AND attempts >= 1",
            name="ck_telegram_dead_letters_resolution",
        ),
        Index(
            "ix_telegram_dead_letters_bot_failed_at",
            "bot_fingerprint", "failed_at",
        ),
    )


class TelegramInboundUpdate(TimestampMixin, Base):
    """One owner-bound inbound update and its at-least-once reply outbox."""

    __tablename__ = "telegram_inbound_updates"

    id = Column(String(36), primary_key=True)
    bot_fingerprint = Column(
        String(64),
        ForeignKey("telegram_polling_states.bot_fingerprint", ondelete="CASCADE"),
        nullable=False,
    )
    update_id = Column(BigInteger, nullable=False)
    chat_id = Column(EncryptedContentText, nullable=False)
    owner_account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    reply_text = Column(EncryptedContentText, nullable=True)
    status = Column(String(24), nullable=False, default="processing")
    processing_claim_digest = Column(String(64), nullable=True)
    processing_lease_expires_at = Column(DateTime, nullable=True)
    reply_claim_digest = Column(String(64), nullable=True)
    reply_lease_expires_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "bot_fingerprint", "update_id",
            name="uq_telegram_inbound_updates_bot_update",
        ),
        CheckConstraint(
            "status IN ('processing', 'reply_pending', 'delivered', 'discarded')",
            name="ck_telegram_inbound_updates_status",
        ),
        CheckConstraint(
            "update_id >= 0 AND version >= 1",
            name="ck_telegram_inbound_updates_version",
        ),
        Index(
            "ix_telegram_inbound_updates_status_lease",
            "bot_fingerprint", "status", "processing_lease_expires_at",
            "reply_lease_expires_at",
        ),
    )


class TelegramRuntimeImportRun(TimestampMixin, Base):
    """Idempotent adoption marker for the retired Telegram sidecars."""

    __tablename__ = "telegram_runtime_import_runs"

    id = Column(String(36), primary_key=True)
    bot_fingerprint = Column(
        String(64),
        ForeignKey("telegram_polling_states.bot_fingerprint", ondelete="CASCADE"),
        nullable=False,
    )
    source_kind = Column(String(64), nullable=False)
    state = Column(String(24), nullable=False, default="pending")
    source_sha256 = Column(String(64), nullable=False)
    details = Column(EncryptedJSON, nullable=False, default=dict)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "bot_fingerprint", "source_kind",
            name="uq_telegram_runtime_import_bot_source",
        ),
        CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_telegram_runtime_import_runs_state",
        ),
        CheckConstraint(
            "length(bot_fingerprint) = 64 AND length(source_sha256) = 64",
            name="ck_telegram_runtime_import_runs_digests",
        ),
    )


class ReminderDeliveryClaim(TimestampMixin, Base):
    """Database-fenced delivery state for one reminder occurrence/channel."""

    __tablename__ = "reminder_delivery_claims"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    note_id = Column(String(255), nullable=False)
    occurrence = Column(String(255), nullable=False, default="")
    channel = Column(String(64), nullable=False, default="browser")
    status = Column(String(32), nullable=False, default="claimed")
    claim_token_digest = Column(String(64), nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    retry_after = Column(DateTime, nullable=True, index=True)
    delivered_at = Column(DateTime, nullable=True)
    last_error_code = Column(String(64), nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "note_id", "occurrence", "channel",
            name="uq_reminder_delivery_occurrence_channel",
        ),
        CheckConstraint(
            "status IN ('claimed', 'awaiting_browser_ack', 'delivered', "
            "'failed', 'cancelled')",
            name="ck_reminder_delivery_claims_status",
        ),
        CheckConstraint(
            "version >= 1", name="ck_reminder_delivery_claims_version",
        ),
        Index(
            "ix_reminder_delivery_claims_ready",
            "owner_id", "status", "retry_after", "claimed_at",
        ),
    )


class ReminderCancellation(TimestampMixin, Base):
    """Shared cancel-before-enqueue barrier for reminder side effects."""

    __tablename__ = "reminder_cancellations"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    note_id = Column(String(255), nullable=False)
    scope = Column(String(24), nullable=False)
    occurrence = Column(String(255), nullable=False, default="")
    cancelled_at = Column(DateTime, nullable=False, default=utcnow_naive)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "note_id", "scope", "occurrence",
            name="uq_reminder_cancellations_scope",
        ),
        CheckConstraint(
            "scope IN ('all', 'occurrence')",
            name="ck_reminder_cancellations_scope",
        ),
        Index(
            "ix_reminder_cancellations_lookup",
            "owner_id", "note_id", "scope", "occurrence",
        ),
    )


class BrowserNotification(TimestampMixin, Base):
    """Owner-scoped browser outbox retained until explicit acknowledgement."""

    __tablename__ = "browser_notifications"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    payload = Column(EncryptedJSON, nullable=False)
    dedupe_key_digest = Column(String(64), nullable=True)
    claim_owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True,
    )
    claim_note_id = Column(String(255), nullable=False, default="")
    claim_occurrence = Column(String(255), nullable=False, default="")
    claim_channel = Column(String(64), nullable=False, default="")
    claim_token = Column(EncryptedContentText, nullable=True)
    acknowledged_at = Column(DateTime, nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "dedupe_key_digest",
            name="uq_browser_notifications_owner_dedupe",
        ),
        Index(
            "ix_browser_notifications_pending",
            "owner_id", "acknowledged_at", "created_at",
        ),
        Index(
            "ix_browser_notifications_claim",
            "owner_id", "claim_note_id", "claim_occurrence",
        ),
    )


class NotificationRuntimeImportRun(TimestampMixin, Base):
    """Idempotent adoption marker for retired reminder/browser sidecars."""

    __tablename__ = "notification_runtime_import_runs"

    id = Column(String(36), primary_key=True)
    source_kind = Column(String(64), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    state = Column(String(24), nullable=False, default="pending")
    details = Column(EncryptedJSON, nullable=False, default=dict)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "source_kind", "source_sha256",
            name="uq_notification_runtime_import_source",
        ),
        CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_notification_runtime_import_runs_state",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_notification_runtime_import_runs_digest",
        ),
    )


class EmailLifeProjection(TimestampMixin, Base):
    """Encrypted, Account.id-owned email-to-Life projection outbox.

    Email index/cache state remains rebuildable connector data.  This row is
    the canonical durable handoff into the Life graph and therefore lives in
    the configured SQL authority.  ``version`` plus the opaque claim digest
    fence stale workers after a lease expires.
    """

    __tablename__ = "email_life_projection_ledger"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    account_key = Column(String(255), nullable=False)
    folder = Column(String(255), nullable=False)
    message_uid = Column(String(255), nullable=False)
    header_sha256 = Column(String(64), nullable=False)
    payload = Column(EncryptedJSON, nullable=True)
    state = Column(String(24), nullable=False, default="pending")
    claim_token_digest = Column(String(64), nullable=True, unique=True)
    claimed_at = Column(DateTime, nullable=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    next_attempt_at = Column(DateTime, nullable=True, index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    last_error_code = Column(String(64), nullable=True)
    completed_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "account_key", "folder", "message_uid",
            "header_sha256", name="uq_email_life_projection_identity",
        ),
        CheckConstraint(
            "state IN ('pending', 'processing', 'failed', 'completed')",
            name="ck_email_life_projection_state",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_email_life_projection_attempts",
        ),
        CheckConstraint(
            "length(header_sha256) = 64",
            name="ck_email_life_projection_header_digest",
        ),
        CheckConstraint(
            "length(account_key) >= 1 AND length(folder) >= 1 "
            "AND length(message_uid) >= 1",
            name="ck_email_life_projection_routing_identity",
        ),
        CheckConstraint(
            "claim_token_digest IS NULL OR length(claim_token_digest) = 64",
            name="ck_email_life_projection_claim_digest",
        ),
        CheckConstraint(
            "(state = 'completed' AND payload IS NULL "
            "AND completed_at IS NOT NULL) OR "
            "(state <> 'completed' AND payload IS NOT NULL)",
            name="ck_email_life_projection_payload_lifecycle",
        ),
        CheckConstraint(
            "(state = 'processing' AND claim_token_digest IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state <> 'processing' AND claim_token_digest IS NULL "
            "AND claimed_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_email_life_projection_lease_lifecycle",
        ),
        CheckConstraint(
            "version >= 1", name="ck_email_life_projection_version",
        ),
        Index(
            "ix_email_life_projection_ready", "owner_id", "account_key",
            "folder", "state", "next_attempt_at", "lease_expires_at",
        ),
    )


class EmailLifeProjectionImportRun(TimestampMixin, Base):
    """Encrypted progress marker for bounded, non-destructive sidecar import."""

    __tablename__ = "email_life_projection_import_runs"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source_kind = Column(String(64), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    state = Column(String(24), nullable=False, default="pending")
    details = Column(EncryptedJSON, nullable=False, default=dict)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "source_kind", "source_sha256",
            name="uq_email_life_projection_import_source",
        ),
        CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_email_life_projection_import_state",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_email_life_projection_import_digest",
        ),
    )


class EmailTagState(TimestampMixin, Base):
    """Canonical encrypted tag/spam state for one mailbox message."""

    __tablename__ = "email_tag_states"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    account_key = Column(String(255), nullable=False)
    message_digest = Column(String(64), nullable=False)
    location_digest = Column(String(64), nullable=False)
    payload = Column(EncryptedJSON, nullable=False, default=dict)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "account_key", "message_digest",
            name="uq_email_tag_states_message",
        ),
        UniqueConstraint(
            "owner_id", "account_key", "location_digest",
            name="uq_email_tag_states_location",
        ),
        CheckConstraint(
            "length(message_digest) = 64 AND length(location_digest) = 64",
            name="ck_email_tag_states_digests",
        ),
        CheckConstraint("version >= 1", name="ck_email_tag_states_version"),
        Index(
            "ix_email_tag_states_owner_account",
            "owner_id", "account_key", "updated_at",
        ),
    )


class EmailAutomationRule(TimestampMixin, Base):
    """Owner-scoped email automation flags shared by every interface."""

    __tablename__ = "email_automation_rules"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    account_key = Column(String(255), nullable=False, default="*")
    rules = Column(EncryptedJSON, nullable=False, default=dict)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "account_key", name="uq_email_automation_rules_scope",
        ),
        CheckConstraint(
            "length(account_key) >= 1", name="ck_email_automation_rules_scope",
        ),
        CheckConstraint(
            "version >= 1", name="ck_email_automation_rules_version",
        ),
    )


class EmailScheduledDelivery(TimestampMixin, Base):
    """Durable manual email schedule with a fenced, database-time lease."""

    __tablename__ = "email_scheduled_deliveries"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    email_account_id = Column(
        String, ForeignKey("email_accounts.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    idempotency_key = Column(String(128), nullable=False)
    payload = Column(EncryptedJSON, nullable=False, default=dict)
    payload_sha256 = Column(String(64), nullable=False)
    scheduled_for = Column(DateTime, nullable=False, index=True)
    state = Column(String(24), nullable=False, default="queued", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=True, index=True)
    claim_token_digest = Column(String(64), nullable=True, unique=True)
    claimed_at = Column(DateTime, nullable=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    completed_at = Column(DateTime, nullable=True)
    last_error_code = Column(String(64), nullable=True)
    provider_message_id = Column(EncryptedContentText, nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "idempotency_key",
            name="uq_email_scheduled_deliveries_idempotency",
        ),
        CheckConstraint(
            "state IN ('queued', 'claimed', 'retry', 'delivered', 'failed', "
            "'cancelled')",
            name="ck_email_scheduled_deliveries_state",
        ),
        CheckConstraint(
            "attempts >= 0", name="ck_email_scheduled_deliveries_attempts",
        ),
        CheckConstraint(
            "length(payload_sha256) = 64",
            name="ck_email_scheduled_deliveries_payload_digest",
        ),
        CheckConstraint(
            "claim_token_digest IS NULL OR length(claim_token_digest) = 64",
            name="ck_email_scheduled_deliveries_claim_digest",
        ),
        CheckConstraint(
            "(state = 'claimed' AND claim_token_digest IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state <> 'claimed' AND claim_token_digest IS NULL "
            "AND claimed_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_email_scheduled_deliveries_lease_lifecycle",
        ),
        CheckConstraint(
            "version >= 1", name="ck_email_scheduled_deliveries_version",
        ),
        Index(
            "ix_email_scheduled_deliveries_ready", "state", "scheduled_for",
            "next_attempt_at", "lease_expires_at",
        ),
    )


class EmailAutomationRun(TimestampMixin, Base):
    """Idempotency record and fenced claim for one email automation action."""

    __tablename__ = "email_automation_runs"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    account_key = Column(String(255), nullable=False)
    operation = Column(String(32), nullable=False)
    message_digest = Column(String(64), nullable=False)
    payload = Column(EncryptedJSON, nullable=False, default=dict)
    state = Column(String(24), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=True, index=True)
    claim_token_digest = Column(String(64), nullable=True, unique=True)
    claimed_at = Column(DateTime, nullable=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    completed_at = Column(DateTime, nullable=True)
    last_error_code = Column(String(64), nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "account_key", "operation", "message_digest",
            name="uq_email_automation_runs_identity",
        ),
        CheckConstraint(
            "operation IN ('summary', 'reply', 'classify', 'calendar', "
            "'email_received')",
            name="ck_email_automation_runs_operation",
        ),
        CheckConstraint(
            "state IN ('pending', 'claimed', 'retry', 'completed', 'failed')",
            name="ck_email_automation_runs_state",
        ),
        CheckConstraint(
            "attempts >= 0", name="ck_email_automation_runs_attempts",
        ),
        CheckConstraint(
            "length(message_digest) = 64",
            name="ck_email_automation_runs_message_digest",
        ),
        CheckConstraint(
            "claim_token_digest IS NULL OR length(claim_token_digest) = 64",
            name="ck_email_automation_runs_claim_digest",
        ),
        CheckConstraint(
            "(state = 'claimed' AND claim_token_digest IS NOT NULL "
            "AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(state <> 'claimed' AND claim_token_digest IS NULL "
            "AND claimed_at IS NULL AND lease_expires_at IS NULL)",
            name="ck_email_automation_runs_lease_lifecycle",
        ),
        CheckConstraint(
            "version >= 1", name="ck_email_automation_runs_version",
        ),
        Index(
            "ix_email_automation_runs_ready", "state", "next_attempt_at",
            "lease_expires_at",
        ),
    )


class EmailRuntimeImportRun(TimestampMixin, Base):
    """Encrypted non-destructive import marker for legacy email sidecars."""

    __tablename__ = "email_runtime_import_runs"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source_kind = Column(String(64), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    state = Column(String(24), nullable=False, default="pending")
    details = Column(EncryptedJSON, nullable=False, default=dict)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "source_kind", "source_sha256",
            name="uq_email_runtime_import_runs_source",
        ),
        CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_email_runtime_import_runs_state",
        ),
        CheckConstraint(
            "length(source_sha256) = 64",
            name="ck_email_runtime_import_runs_digest",
        ),
    )


class ContactSource(TimestampMixin, Base):
    """One owner-scoped local or CardDAV contact authority.

    CardDAV configuration is private user data as well as a credential, so the
    URL and username use the content envelope while the password uses the
    credential envelope.  ``(id, owner_id)`` is deliberately unique so child
    rows can enforce matching ownership with a composite foreign key.
    """

    __tablename__ = "contact_sources"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    kind = Column(String(24), nullable=False, default="local")
    label = Column(EncryptedContentText, nullable=False, default="Contacts")
    base_url = Column(EncryptedContentText, nullable=True)
    username = Column(EncryptedContentText, nullable=True)
    password = Column(EncryptedText, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    last_sync_at = Column(DateTime, nullable=True)
    sync_state = Column(String(24), nullable=False, default="idle")
    last_error = Column(EncryptedContentText, nullable=True)
    config_version = Column(Integer, nullable=False, default=1)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_contact_sources_id_owner"),
        CheckConstraint(
            "kind IN ('local', 'carddav')", name="ck_contact_sources_kind",
        ),
        CheckConstraint(
            "sync_state IN ('idle', 'syncing', 'ready', 'error', 'disabled')",
            name="ck_contact_sources_sync_state",
        ),
        CheckConstraint("version >= 1", name="ck_contact_sources_version"),
        CheckConstraint(
            "config_version >= 1", name="ck_contact_sources_config_version",
        ),
        Index(
            "uq_contact_sources_owner_local",
            "owner_id",
            unique=True,
            sqlite_where=text("kind = 'local'"),
            postgresql_where=text("kind = 'local'"),
        ),
        Index(
            "uq_contact_sources_owner_carddav",
            "owner_id",
            unique=True,
            sqlite_where=text("kind = 'carddav'"),
            postgresql_where=text("kind = 'carddav'"),
        ),
        Index(
            "ix_contact_sources_owner_kind_enabled",
            "owner_id", "kind", "enabled",
        ),
    )


class ContactRecord(TimestampMixin, Base):
    """Materialized owner-scoped contact data for one source."""

    __tablename__ = "contact_records"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source_id = Column(String(36), nullable=False, index=True)
    remote_uid = Column(EncryptedContentText, nullable=False)
    remote_uid_digest = Column(String(64), nullable=False)
    remote_href = Column(EncryptedContentText, nullable=True)
    remote_etag = Column(EncryptedContentText, nullable=True)
    payload = Column(EncryptedJSON, nullable=False, default=dict)
    raw_vcard = Column(EncryptedContentText, nullable=True)
    deleted_at = Column(DateTime, nullable=True, index=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_contact_records_id_owner"),
        ForeignKeyConstraint(
            ["source_id", "owner_id"],
            ["contact_sources.id", "contact_sources.owner_id"],
            ondelete="CASCADE",
            name="fk_contact_records_source_owner",
        ),
        UniqueConstraint(
            "source_id", "remote_uid_digest",
            name="uq_contact_records_source_uid_digest",
        ),
        CheckConstraint("version >= 1", name="ck_contact_records_version"),
        Index(
            "ix_contact_records_owner_source_deleted",
            "owner_id", "source_id", "deleted_at",
        ),
    )


@event.listens_for(ContactRecord, "before_insert")
@event.listens_for(ContactRecord, "before_update")
def _derive_contact_remote_uid_digest(_mapper, _connection, target):
    from src.secret_storage import private_digest

    uid = str(target.remote_uid or "").strip()
    if not uid:
        raise ValueError("Contact remote UID is required")
    target.remote_uid_digest = private_digest("contact-remote-uid-v1", uid)


class ContactDelivery(TimestampMixin, Base):
    """Durable, owner-scoped CardDAV mutation outbox.

    The encrypted payload contains the exact vCard and remote precondition
    needed to replay a connector write after a crash.  Business mutations and
    this row commit together; network delivery is never the authority commit.
    """

    __tablename__ = "contact_deliveries"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source_id = Column(String(36), nullable=False, index=True)
    record_id = Column(String(36), nullable=False, index=True)
    operation = Column(String(16), nullable=False)
    idempotency_key = Column(String(96), nullable=False)
    payload = Column(EncryptedJSON, nullable=False, default=dict)
    state = Column(String(24), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=True, index=True)
    claim_token = Column(String(36), nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    last_error_code = Column(String(64), nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "owner_id"],
            ["contact_sources.id", "contact_sources.owner_id"],
            ondelete="CASCADE",
            name="fk_contact_deliveries_source_owner",
        ),
        ForeignKeyConstraint(
            ["record_id", "owner_id"],
            ["contact_records.id", "contact_records.owner_id"],
            ondelete="CASCADE",
            name="fk_contact_deliveries_record_owner",
        ),
        UniqueConstraint(
            "owner_id", "idempotency_key",
            name="uq_contact_deliveries_owner_idempotency",
        ),
        CheckConstraint(
            "operation IN ('create', 'update', 'delete')",
            name="ck_contact_deliveries_operation",
        ),
        CheckConstraint(
            "state IN ('pending', 'processing', 'retry', 'conflict', 'completed')",
            name="ck_contact_deliveries_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_contact_deliveries_attempts"),
        CheckConstraint("version >= 1", name="ck_contact_deliveries_version"),
        Index(
            "ix_contact_deliveries_owner_state_due",
            "owner_id", "state", "next_attempt_at", "created_at",
        ),
        Index(
            "ix_contact_deliveries_record_order",
            "owner_id", "record_id", "created_at", "id",
        ),
    )


class ContactImportRun(TimestampMixin, Base):
    """Redacted status for the one legacy contacts JSON cutover."""

    __tablename__ = "contact_import_runs"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source_kind = Column(String(64), nullable=False, unique=True)
    state = Column(String(24), nullable=False, default="pending")
    settings_sha256 = Column(String(64), nullable=True)
    contacts_sha256 = Column(String(64), nullable=True)
    backup_settings_path = Column(EncryptedContentText, nullable=True)
    backup_contacts_path = Column(EncryptedContentText, nullable=True)
    details = Column(EncryptedJSON, nullable=False, default=dict)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'completed', 'failed')",
            name="ck_contact_import_runs_state",
        ),
    )


class InboxItem(TimestampMixin, Base):
    """Owner-scoped universal capture awaiting or recording triage."""

    __tablename__ = "inbox_items"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Universal Inbox can contain health, finance, and private message data.
    # Keep only routing/classification fields queryable; user content is
    # encrypted with the same local envelope used by other sensitive domains.
    title = Column(EncryptedContentText, nullable=False, default="")
    content = Column(EncryptedContentText, nullable=False, default="")
    kind = Column(String(32), nullable=False, default="note", index=True)
    status = Column(String(24), nullable=False, default="inbox", index=True)
    source_type = Column(String(48), nullable=False, default="user")
    source_ref = Column(EncryptedContentText, nullable=True)
    meta_data = Column("metadata", EncryptedJSON, nullable=False, default=dict)
    classification_confidence = Column(Integer, nullable=False, default=0)
    classification_reason = Column(String(500), nullable=False, default="")
    processed_target_type = Column(String(48), nullable=True)
    processed_target_id = Column(String(255), nullable=True)
    processed_at = Column(DateTime, nullable=True)
    archived_at = Column(DateTime, nullable=True)
    idempotency_key = Column(String(128), nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_inbox_owner_idempotency"
        ),
        Index("ix_inbox_owner_status_updated", "owner_id", "status", "updated_at"),
        Index("ix_inbox_owner_kind_status", "owner_id", "kind", "status"),
    )


class EntityLink(Base):
    """Typed, owner-scoped edge between an Inbox item and an existing entity."""

    __tablename__ = "entity_links"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source_type = Column(String(48), nullable=False)
    source_id = Column(String(255), nullable=False)
    relation = Column(String(64), nullable=False)
    target_type = Column(String(48), nullable=False)
    target_id = Column(String(255), nullable=False)
    # Relationship metadata can reveal private people, health, finance, or
    # project context.  Legacy plaintext JSON remains readable through the
    # encrypted type and is rewritten on the next mutation.
    meta_data = Column("metadata", EncryptedJSON, nullable=False, default=dict)
    provenance = Column(EncryptedJSON, nullable=False, default=dict)
    confidence = Column(Integer, nullable=False, default=100)
    sensitivity = Column(String(24), nullable=False, default="private")
    version = Column(Integer, nullable=False, default=1)
    deleted_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=utcnow_naive)
    updated_at = Column(
        DateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive,
    )

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "source_type", "source_id", "relation",
            "target_type", "target_id", name="uq_entity_link_edge",
        ),
        Index("ix_entity_links_source", "owner_id", "source_type", "source_id"),
        Index("ix_entity_links_target", "owner_id", "target_type", "target_id"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name="ck_entity_links_confidence",
        ),
        CheckConstraint("version >= 1", name="ck_entity_links_version"),
    )


class ActionAudit(Base):
    """Append-only record of every V3 life-core state transition."""

    __tablename__ = "action_audit"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    action = Column(String(80), nullable=False, index=True)
    entity_type = Column(String(48), nullable=False)
    entity_id = Column(String(255), nullable=False, index=True)
    before_state = Column(JSON, nullable=False, default=dict)
    after_state = Column(JSON, nullable=False, default=dict)
    details = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=utcnow_naive, index=True)

    __table_args__ = (
        Index("ix_action_audit_owner_created", "owner_id", "created_at"),
        Index("ix_action_audit_entity", "owner_id", "entity_type", "entity_id"),
    )


@event.listens_for(ActionAudit, "before_update")
@event.listens_for(ActionAudit, "before_delete")
def _protect_append_only_action_audit(*_args, **_kwargs):
    raise RuntimeError("ActionAudit rows are append-only")


# Mapper hooks protect normal ORM usage; SQLite triggers also reject bulk ORM
# statements and raw SQL.  init_db installs the same idempotent triggers for a
# database where the table was created before these guards were introduced.
event.listen(
    ActionAudit.__table__,
    "after_create",
    DDL("""
        CREATE TRIGGER IF NOT EXISTS action_audit_no_update
        BEFORE UPDATE ON action_audit
        BEGIN
            SELECT RAISE(ABORT, 'ActionAudit rows are append-only');
        END
    """).execute_if(dialect="sqlite"),
)
event.listen(
    ActionAudit.__table__,
    "after_create",
    DDL("""
        CREATE TRIGGER IF NOT EXISTS action_audit_no_delete
        BEFORE DELETE ON action_audit
        BEGIN
            SELECT RAISE(ABORT, 'ActionAudit rows are append-only');
        END
    """).execute_if(dialect="sqlite"),
)


LIFE_ENTITY_TYPES = (
    "person", "area", "goal", "project", "milestone", "task", "action",
    "event", "communication_thread", "message", "note", "file",
    "decision", "habit", "metric", "transaction", "health_record",
    "place", "asset", "reminder", "automation", "source",
    "journal_entry", "workspace", "trip", "interaction", "commitment",
    "period_review", "learning_record", "career_item", "home_record",
    "finance_record",
)


class LifeSource(TimestampMixin, Base):
    """Immutable-principal provenance for captured or imported information."""

    __tablename__ = "life_sources"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source_type = Column(String(48), nullable=False, index=True)
    title = Column(EncryptedContentText, nullable=False, default="")
    source_ref = Column(EncryptedContentText, nullable=True)
    safe_excerpt = Column(EncryptedContentText, nullable=False, default="")
    content_sha256 = Column(String(64), nullable=True, index=True)
    observed_at = Column(DateTime, nullable=True, index=True)
    captured_at = Column(DateTime, nullable=False, default=utcnow_naive, index=True)
    sensitivity = Column(String(24), nullable=False, default="private")
    meta_data = Column("metadata", EncryptedJSON, nullable=False, default=dict)
    idempotency_key = Column(String(128), nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_life_sources_owner_idempotency",
        ),
        Index("ix_life_sources_owner_type_captured", "owner_id", "source_type", "captured_at"),
        CheckConstraint("version >= 1", name="ck_life_sources_version"),
    )


class LifeEntity(TimestampMixin, Base):
    """Principal-scoped node in Restia's cross-domain life graph.

    Mature domain tables remain authoritative.  ``domain_ref_*`` points to
    those rows; domains that do not yet have a dedicated table can use the
    validated encrypted ``properties`` object as their authority without
    creating a second disconnected dashboard store.
    """

    __tablename__ = "life_entities"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    entity_type = Column(String(48), nullable=False, index=True)
    title = Column(EncryptedContentText, nullable=False, default="")
    summary = Column(EncryptedContentText, nullable=False, default="")
    status = Column(String(32), nullable=False, default="active", index=True)
    properties = Column(EncryptedJSON, nullable=False, default=dict)
    provenance = Column(EncryptedJSON, nullable=False, default=dict)
    confidence = Column(Integer, nullable=False, default=100)
    sensitivity = Column(String(24), nullable=False, default="private")
    domain_ref_type = Column(String(48), nullable=True)
    domain_ref_id = Column(String(255), nullable=True)
    occurred_at = Column(DateTime, nullable=True, index=True)
    due_at = Column(DateTime, nullable=True, index=True)
    review_at = Column(DateTime, nullable=True, index=True)
    idempotency_key = Column(String(128), nullable=True)
    version = Column(Integer, nullable=False, default=1)
    deleted_at = Column(DateTime, nullable=True, index=True)

    __table_args__ = (
        UniqueConstraint(
            "id", "owner_id", name="uq_life_entities_id_owner",
        ),
        UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_life_entities_owner_idempotency",
        ),
        UniqueConstraint(
            "owner_id", "entity_type", "domain_ref_type", "domain_ref_id",
            name="uq_life_entities_domain_ref",
        ),
        Index("ix_life_entities_owner_type_status", "owner_id", "entity_type", "status"),
        Index("ix_life_entities_owner_updated", "owner_id", "updated_at"),
        CheckConstraint(
            "entity_type IN (" + ", ".join(repr(value) for value in LIFE_ENTITY_TYPES) + ")",
            name="ck_life_entities_type",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 100",
            name="ck_life_entities_confidence",
        ),
        CheckConstraint("version >= 1", name="ck_life_entities_version"),
    )


class LifeEntityVersion(Base):
    """Append-only snapshot supporting inspection and safe reversal."""

    __tablename__ = "life_entity_versions"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    entity_id = Column(String(36), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    snapshot = Column(EncryptedJSON, nullable=False, default=dict)
    reason = Column(EncryptedContentText, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=utcnow_naive, index=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ("entity_id", "owner_id"),
            ("life_entities.id", "life_entities.owner_id"),
            ondelete="CASCADE",
            name="fk_life_entity_versions_entity_owner",
        ),
        UniqueConstraint(
            "entity_id", "version", name="uq_life_entity_versions_entity_version",
        ),
        Index("ix_life_entity_versions_owner_created", "owner_id", "created_at"),
        CheckConstraint("version >= 1", name="ck_life_entity_versions_version"),
    )


@event.listens_for(LifeEntityVersion, "before_update")
@event.listens_for(LifeEntityVersion, "before_delete")
def _protect_append_only_life_entity_version(*_args, **_kwargs):
    raise RuntimeError("LifeEntityVersion rows are append-only")


event.listen(
    LifeEntityVersion.__table__,
    "after_create",
    DDL("""
        CREATE TRIGGER IF NOT EXISTS life_entity_versions_no_update
        BEFORE UPDATE ON life_entity_versions
        BEGIN
            SELECT RAISE(ABORT, 'LifeEntityVersion rows are append-only');
        END
    """).execute_if(dialect="sqlite"),
)
event.listen(
    LifeEntityVersion.__table__,
    "after_create",
    DDL("""
        CREATE TRIGGER IF NOT EXISTS life_entity_versions_no_delete
        BEFORE DELETE ON life_entity_versions
        BEGIN
            SELECT RAISE(ABORT, 'LifeEntityVersion rows are append-only');
        END
    """).execute_if(dialect="sqlite"),
)


class ActionPolicy(TimestampMixin, Base):
    """Per-principal autonomy ceiling for one life domain."""

    __tablename__ = "action_policies"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    domain = Column(String(48), nullable=False)
    max_autonomy = Column(Integer, nullable=False, default=3)
    external_requires_confirmation = Column(Boolean, nullable=False, default=True)
    enabled = Column(Boolean, nullable=False, default=True)
    rules = Column(EncryptedJSON, nullable=False, default=dict)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("owner_id", "domain", name="uq_action_policies_owner_domain"),
        CheckConstraint(
            "max_autonomy >= 1 AND max_autonomy <= 6",
            name="ck_action_policies_autonomy",
        ),
        CheckConstraint("version >= 1", name="ck_action_policies_version"),
    )


class ActionProposal(TimestampMixin, Base):
    """Durable proposed/executed action with explicit approval semantics."""

    __tablename__ = "action_proposals"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    domain = Column(String(48), nullable=False, index=True)
    action = Column(String(80), nullable=False, index=True)
    autonomy_level = Column(Integer, nullable=False)
    state = Column(String(24), nullable=False, default="prepared", index=True)
    target_type = Column(String(48), nullable=False)
    target_id = Column(String(255), nullable=True)
    payload = Column(EncryptedJSON, nullable=False, default=dict)
    reason = Column(EncryptedContentText, nullable=False, default="")
    sources = Column(EncryptedJSON, nullable=False, default=dict)
    external = Column(Boolean, nullable=False, default=False)
    requires_confirmation = Column(Boolean, nullable=False, default=False)
    confirmation_digest = Column(String(64), nullable=True, unique=True)
    expires_at = Column(DateTime, nullable=True, index=True)
    approved_at = Column(DateTime, nullable=True)
    approved_by_account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True,
    )
    executed_at = Column(DateTime, nullable=True)
    result = Column(EncryptedJSON, nullable=False, default=dict)
    undo_ref = Column(EncryptedContentText, nullable=True)
    idempotency_key = Column(String(128), nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        Index(
            "uq_action_proposals_id_owner", "id", "owner_id", unique=True,
        ),
        UniqueConstraint(
            "owner_id", "idempotency_key", name="uq_action_proposals_owner_idempotency",
        ),
        Index("ix_action_proposals_owner_state_created", "owner_id", "state", "created_at"),
        CheckConstraint(
            "autonomy_level >= 1 AND autonomy_level <= 6",
            name="ck_action_proposals_autonomy",
        ),
        CheckConstraint(
            "state IN ('prepared', 'approved', 'executing', 'completed', "
            "'rejected', 'failed', 'reversed', 'expired')",
            name="ck_action_proposals_state",
        ),
        CheckConstraint(
            "approved_by_account_id IS NULL OR approved_by_account_id = owner_id",
            name="ck_action_proposals_approver_owner",
        ),
        CheckConstraint("version >= 1", name="ck_action_proposals_version"),
    )


class EmailOutboundDraft(TimestampMixin, Base):
    """Exact encrypted agent-authored email awaiting human review."""

    __tablename__ = "email_outbound_drafts"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    proposal_id = Column(String(36), nullable=False, index=True)
    email_account_id = Column(
        String, ForeignKey("email_accounts.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    kind = Column(String(16), nullable=False)
    content = Column(EncryptedJSON, nullable=False, default=dict)
    content_sha256 = Column(String(64), nullable=False, index=True)
    source = Column(EncryptedJSON, nullable=False, default=dict)
    state = Column(String(24), nullable=False, default="pending_review", index=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "id", "owner_id", name="uq_email_outbound_drafts_id_owner",
        ),
        UniqueConstraint(
            "owner_id", "proposal_id",
            name="uq_email_outbound_drafts_owner_proposal",
        ),
        ForeignKeyConstraint(
            ("proposal_id", "owner_id"),
            ("action_proposals.id", "action_proposals.owner_id"),
            ondelete="CASCADE",
            name="fk_email_outbound_drafts_proposal_owner",
        ),
        Index(
            "ix_email_outbound_drafts_owner_state_created",
            "owner_id", "state", "created_at",
        ),
        CheckConstraint(
            "kind IN ('new', 'reply')",
            name="ck_email_outbound_drafts_kind",
        ),
        CheckConstraint(
            "state IN ('pending_review', 'queued', 'delivered', 'failed', "
            "'rejected')",
            name="ck_email_outbound_drafts_state",
        ),
        CheckConstraint("version >= 1", name="ck_email_outbound_drafts_version"),
    )


class EmailOutboundDelivery(TimestampMixin, Base):
    """Durable worker-owned email delivery outbox; never a request-path send."""

    __tablename__ = "email_outbound_deliveries"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    draft_id = Column(String(36), nullable=False, index=True)
    proposal_id = Column(String(36), nullable=False, index=True)
    email_account_id = Column(
        String, ForeignKey("email_accounts.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    idempotency_key = Column(String(128), nullable=False)
    payload = Column(EncryptedJSON, nullable=False, default=dict)
    content_sha256 = Column(String(64), nullable=False, index=True)
    state = Column(String(24), nullable=False, default="queued", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=True, index=True)
    claim_token_digest = Column(String(64), nullable=True, unique=True)
    claimed_at = Column(DateTime, nullable=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    completed_at = Column(DateTime, nullable=True)
    last_error_code = Column(String(64), nullable=True)
    provider_message_id = Column(EncryptedContentText, nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint(
            "id", "owner_id", name="uq_email_outbound_deliveries_id_owner",
        ),
        UniqueConstraint(
            "owner_id", "proposal_id",
            name="uq_email_outbound_deliveries_owner_proposal",
        ),
        UniqueConstraint(
            "owner_id", "idempotency_key",
            name="uq_email_outbound_deliveries_owner_idempotency",
        ),
        ForeignKeyConstraint(
            ("draft_id", "owner_id"),
            ("email_outbound_drafts.id", "email_outbound_drafts.owner_id"),
            ondelete="CASCADE",
            name="fk_email_outbound_deliveries_draft_owner",
        ),
        ForeignKeyConstraint(
            ("proposal_id", "owner_id"),
            ("action_proposals.id", "action_proposals.owner_id"),
            ondelete="CASCADE",
            name="fk_email_outbound_deliveries_proposal_owner",
        ),
        Index(
            "ix_email_outbound_deliveries_ready",
            "state", "next_attempt_at", "lease_expires_at",
        ),
        CheckConstraint(
            "state IN ('queued', 'claimed', 'retry', 'delivered', 'failed', "
            "'cancelled')",
            name="ck_email_outbound_deliveries_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_email_outbound_deliveries_attempts"),
        CheckConstraint("version >= 1", name="ck_email_outbound_deliveries_version"),
    )


class FocusSession(TimestampMixin, Base):
    """Recoverable focus lease over one principal-owned life entity."""

    __tablename__ = "focus_sessions"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    entity_id = Column(String(36), nullable=False, index=True)
    state = Column(String(24), nullable=False, default="active", index=True)
    definition_of_done = Column(EncryptedContentText, nullable=False, default="")
    started_at = Column(DateTime, nullable=False, default=utcnow_naive)
    active_since = Column(DateTime, nullable=True)
    paused_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    elapsed_seconds = Column(Integer, nullable=False, default=0)
    interruptions = Column(EncryptedJSON, nullable=False, default=dict)
    progress = Column(EncryptedJSON, nullable=False, default=dict)
    evidence = Column(EncryptedJSON, nullable=False, default=dict)
    follow_up_entity_ids = Column(EncryptedJSON, nullable=False, default=dict)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        ForeignKeyConstraint(
            ("entity_id", "owner_id"),
            ("life_entities.id", "life_entities.owner_id"),
            ondelete="CASCADE",
            name="fk_focus_sessions_entity_owner",
        ),
        Index("ix_focus_sessions_owner_state", "owner_id", "state"),
        Index(
            "uq_focus_sessions_owner_live",
            "owner_id",
            unique=True,
            sqlite_where=text("state IN ('active', 'paused')"),
            postgresql_where=text("state IN ('active', 'paused')"),
        ),
        CheckConstraint(
            "state IN ('active', 'paused', 'completed', 'abandoned')",
            name="ck_focus_sessions_state",
        ),
        CheckConstraint("elapsed_seconds >= 0", name="ck_focus_sessions_elapsed"),
        CheckConstraint("version >= 1", name="ck_focus_sessions_version"),
    )


class Session(TimestampMixin, Base):
    """
    SQLAlchemy model for Session table.
    Represents a chat session with its configuration and metadata.
    """
    __tablename__ = "sessions"
    
    # Primary key
    id = Column(String, primary_key=True, index=True)
    
    # Session metadata
    name = Column(String, nullable=False)
    endpoint_url = Column(String, nullable=False)
    model = Column(String, nullable=False)
    owner = Column(String, nullable=True, index=True)  # username; null = legacy/shared
    
    # Configuration flags
    rag = Column(Boolean, default=False)
    archived = Column(Boolean, default=False)

    # Organization
    folder = Column(String, nullable=True, default=None)
    
    # Endpoint authorization headers are API credentials.  Keep the public ORM
    # value as a dict while encrypting the serialized object in the database.
    headers = Column(EncryptedJSON, default=dict)
    
    # Timestamps are provided by TimestampMixin
    last_accessed = Column(DateTime, default=func.now(), onupdate=func.now())
    # Timestamp of the last actual MESSAGE in this session. Set explicitly
    # only when a message is persisted (NOT onupdate) — so it's a clean
    # "last conversation" signal, immune to renames / model swaps / merely
    # opening the chat (all of which bump updated_at and last_accessed).
    # The "Last active" sort uses this.
    last_message_at = Column(DateTime, nullable=True, default=None)
    
    
    # Indexes - optimized composites
    __table_args__ = (
        Index('ix_sessions_active', 'archived', 'last_accessed'),
        Index('ix_sessions_search', 'name', 'archived'),
    )
    
    # Properties
    is_important = Column(Boolean, default=False)
    message_count = Column(Integer, default=0)
    total_input_tokens = Column(Integer, default=0)
    total_output_tokens = Column(Integer, default=0)
    mode = Column(String, nullable=True)  # 'agent', 'chat', 'research', or 'study'
    crew_member_id = Column(String, nullable=True)  # links to crew_members.id

    # Relationship to chat messages
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")
    
    @property
    def is_active(self):
        """Check if session is active (not archived)"""
        return not self.archived
    
    def to_dict(self):
        """Convert session to dictionary for JSON serialization"""
        return {
            'id': self.id,
            'name': self.name,
            'model': self.model,
            'endpoint_url': self.endpoint_url,
            'rag': self.rag,
            'archived': self.archived,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'last_accessed': self.last_accessed.isoformat() if self.last_accessed else None,
            'last_message_at': self.last_message_at.isoformat() if self.last_message_at else None,
            'message_count': self.message_count,
            'is_important': self.is_important,
            'folder': self.folder,
            'total_input_tokens': self.total_input_tokens or 0,
            'total_output_tokens': self.total_output_tokens or 0,
            'crew_member_id': self.crew_member_id,
        }

class ChatMessage(Base):
    """
    SQLAlchemy model for ChatMessage table.
    Represents individual chat messages within a session.
    """
    __tablename__ = "chat_messages"
    
    # Primary key - using String to support UUIDs
    id = Column(String, primary_key=True, index=True)
    
    # Foreign key to Session
    session_id = Column(String, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # Message content
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    meta_data = Column("metadata", Text, nullable=True)  # JSON string for metrics etc.

    # Timestamp
    timestamp = Column(DateTime, default=utcnow_naive)
    
    # Relationship to Session
    session = relationship("Session", back_populates="messages")
    
    # Indexes - optimized composite
    __table_args__ = (
        Index('ix_messages_session_time', 'session_id', 'timestamp'),  # Composite for efficient message retrieval
    )


class StudyState(TimestampMixin, Base):
    """Session-scoped Study workspace goal, timer, and accumulated progress.

    A Study chat session UUID is the workspace id, while ``owner`` remains the
    authorization boundary. Legacy installs may still contain the former
    owner-keyed ``local:default``/``user:<name>`` row; ``src.study_mode`` claims
    that row once for the owner's first session-scoped workspace so existing
    goals and logged effort are preserved without being duplicated.

    Keeping the timer in SQLite means refresh/restart cannot lose a focus block.
    """

    __tablename__ = "study_states"

    id = Column(String, primary_key=True)
    owner = Column(String, nullable=True, index=True)
    goal_text = Column(Text, nullable=False, default="")
    target_minutes = Column(Integer, nullable=False, default=0)
    target_date = Column(String, nullable=True)
    total_seconds = Column(Integer, nullable=False, default=0)
    current_session_seconds = Column(Integer, nullable=False, default=0)
    timer_started_at = Column(DateTime, nullable=True)
    timer_running = Column(Boolean, nullable=False, default=False)
    # The focus timer is leased by real Study prompts, never by opening the
    # workspace.  Persisting the most recent prompt lets the server cap and
    # pause stale timers correctly after a closed tab or process restart.
    last_prompt_at = Column(DateTime, nullable=True)
    # Durable one-time setup marker. Goal text cannot serve as this sentinel:
    # a learner may intentionally save wording identical to the starter goal.
    # False means the generated starter is still eligible for replacement by
    # the first substantive prompt; every manual or derived goal sets it true.
    setup_initialized = Column(Boolean, nullable=False, default=False)
    # Deterministic spaced-review state.  These fields deliberately track only
    # demonstrated review evidence; the focus timer remains an effort measure.
    review_level = Column(Integer, nullable=False, default=0)
    review_count = Column(Integer, nullable=False, default=0)
    last_review_result = Column(String, nullable=True)
    last_reviewed_at = Column(DateTime, nullable=True)
    next_review_at = Column(DateTime, nullable=True)


class Project(TimestampMixin, Base):
    """A durable, owner-scoped workflow workspace.

    ``owner`` is always concrete, including auth-disabled installations.  That
    avoids SQLite's special handling of NULL in unique constraints and keeps a
    project key (for example REST) unambiguous within one account.
    """

    __tablename__ = "projects"

    id = Column(String(36), primary_key=True)
    owner = Column(String, nullable=False, index=True)
    key = Column(String(12), nullable=False)
    name = Column(String(160), nullable=False)
    description = Column(Text, nullable=False, default="")
    template = Column(String(32), nullable=False, default="general")
    color = Column(String(16), nullable=False, default="#5b8abf")
    icon = Column(String(32), nullable=True)
    archived = Column(Boolean, nullable=False, default=False, index=True)
    # Completion is a first-class lifecycle state, distinct from archival.
    # Completed projects remain visible as durable outcomes and can be
    # reopened; archival is reserved for removing a project from active use.
    completed_at = Column(DateTime, nullable=True, index=True)
    next_item_number = Column(Integer, nullable=False, default=1)
    version = Column(Integer, nullable=False, default=1)

    members = relationship(
        "ProjectMember", back_populates="project", cascade="all, delete-orphan",
        passive_deletes=True,
    )
    remote_grants = relationship(
        "ProjectRemoteGrant", back_populates="project", cascade="all, delete-orphan",
        passive_deletes=True,
    )
    stages = relationship(
        "ProjectStage", back_populates="project", cascade="all, delete-orphan",
        passive_deletes=True,
    )
    work_items = relationship(
        "ProjectWorkItem", back_populates="project", cascade="all, delete-orphan",
        passive_deletes=True,
    )
    activities = relationship(
        "ProjectActivity", back_populates="project", cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("owner", "key", name="uq_projects_owner_key"),
        Index("ix_projects_owner_archived_updated", "owner", "archived", "updated_at"),
    )


class ProjectQuotaLock(Base):
    """Stable row-level mutexes for cross-project quota admission.

    SQLite serializes project writes with ``BEGIN IMMEDIATE``. Databases with
    row-level locking need a durable row even when an owner does not yet own a
    project; otherwise two first-project creates can both pass a count check.
    Project-scoped lock rows cascade with their project so activity retention
    does not create an unbounded lock registry under create/delete churn.
    """

    __tablename__ = "project_quota_locks"

    key = Column(String(80), primary_key=True)
    project_id = Column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=True,
        index=True,
    )
    created_at = Column(DateTime, nullable=False, default=utcnow_naive)


class ProjectMember(Base):
    """One user's role inside a project."""

    __tablename__ = "project_members"

    project_id = Column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True,
    )
    username = Column(String, primary_key=True)
    role = Column(String(16), nullable=False, default="viewer")
    added_by = Column(String, nullable=True)
    joined_at = Column(DateTime, nullable=False, default=utcnow_naive)

    project = relationship("Project", back_populates="members")

    __table_args__ = (
        Index("ix_project_members_username", "username", "project_id"),
    )


class ProjectRemoteGrant(Base):
    """A project-scoped role granted to one approved Home Link instance.

    Local profiles continue to use :class:`ProjectMember`.  A remote grant has
    its own immutable UUID so project attribution never relies on a reusable
    Home Link handle.  Revoked/declined rows are retained for audit display and
    may be safely re-invited for the same still-existing ``LinkGuest`` identity.
    """

    __tablename__ = "project_remote_grants"

    id = Column(String(36), primary_key=True)
    project_id = Column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
    )
    guest_id = Column(
        Integer, ForeignKey("link_guests.id", ondelete="SET NULL"), nullable=True,
    )
    handle_snapshot = Column(String(32), nullable=False)
    role = Column(String(16), nullable=False, default="viewer")
    status = Column(String(16), nullable=False, default="pending")
    invited_by = Column(String, nullable=False)
    invited_at = Column(DateTime, nullable=False, default=utcnow_naive)
    responded_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1)

    project = relationship("Project", back_populates="remote_grants")
    guest = relationship("LinkGuest")

    __table_args__ = (
        UniqueConstraint(
            "project_id", "guest_id", name="uq_project_remote_grants_project_guest"
        ),
        Index(
            "ix_project_remote_grants_project_status",
            "project_id", "status", "invited_at",
        ),
        Index(
            "ix_project_remote_grants_guest_status",
            "guest_id", "status", "project_id",
        ),
    )


class ProjectStage(TimestampMixin, Base):
    """A user-orderable column in a project's board workflow."""

    __tablename__ = "project_stages"

    id = Column(String(36), primary_key=True)
    project_id = Column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    name = Column(String(80), nullable=False)
    category = Column(String(24), nullable=False, default="todo")
    color = Column(String(16), nullable=False, default="#64748b")
    position = Column(Integer, nullable=False, default=0)
    wip_limit = Column(Integer, nullable=True)

    project = relationship("Project", back_populates="stages")
    work_items = relationship(
        "ProjectWorkItem", back_populates="stage", passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_project_stages_order", "project_id", "position"),
    )


class ProjectWorkItem(TimestampMixin, Base):
    """A Jira-style issue/card belonging to exactly one project."""

    __tablename__ = "project_work_items"

    id = Column(String(36), primary_key=True)
    project_id = Column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    stage_id = Column(
        String(36), ForeignKey("project_stages.id", ondelete="SET NULL"), nullable=True,
        index=True,
    )
    item_number = Column(Integer, nullable=False)
    item_type = Column(String(16), nullable=False, default="task")
    title = Column(String(240), nullable=False)
    description = Column(Text, nullable=False, default="")
    priority = Column(String(16), nullable=False, default="medium")
    labels = Column(JSON, nullable=False, default=list)
    reporter = Column(String, nullable=False)
    assignee = Column(String, nullable=True, index=True)
    start_date = Column(String(10), nullable=True)
    due_date = Column(String(10), nullable=True, index=True)
    estimate_minutes = Column(Integer, nullable=False, default=0)
    logged_minutes = Column(Integer, nullable=False, default=0)
    parent_id = Column(
        String(36), ForeignKey("project_work_items.id", ondelete="SET NULL"), nullable=True,
        index=True,
    )
    blocked_by_id = Column(
        String(36), ForeignKey("project_work_items.id", ondelete="SET NULL"), nullable=True,
        index=True,
    )
    position = Column(Integer, nullable=False, default=0)
    archived = Column(Boolean, nullable=False, default=False, index=True)
    completed_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1)

    project = relationship("Project", back_populates="work_items")
    stage = relationship("ProjectStage", back_populates="work_items")
    parent = relationship(
        "ProjectWorkItem", remote_side=[id], foreign_keys=[parent_id],
        backref=backref("subtasks"),
    )
    blocked_by = relationship(
        "ProjectWorkItem", remote_side=[id], foreign_keys=[blocked_by_id],
        backref=backref("blocking"),
    )
    checklist = relationship(
        "ProjectChecklistItem", back_populates="work_item",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    comments = relationship(
        "ProjectComment", back_populates="work_item",
        cascade="all, delete-orphan", passive_deletes=True,
    )
    attachments = relationship(
        "ProjectAttachment", back_populates="work_item",
        cascade="all, delete-orphan", passive_deletes=True,
    )

    __table_args__ = (
        UniqueConstraint("project_id", "item_number", name="uq_project_work_item_number"),
        Index(
            "ix_project_work_items_board",
            "project_id", "archived", "stage_id", "position",
        ),
    )


class ProjectChecklistItem(TimestampMixin, Base):
    __tablename__ = "project_checklist_items"

    id = Column(String(36), primary_key=True)
    work_item_id = Column(
        String(36), ForeignKey("project_work_items.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    text = Column(String(500), nullable=False)
    is_done = Column(Boolean, nullable=False, default=False)
    position = Column(Integer, nullable=False, default=0)
    created_by = Column(String, nullable=False)
    completed_at = Column(DateTime, nullable=True)

    work_item = relationship("ProjectWorkItem", back_populates="checklist")

    __table_args__ = (
        Index("ix_project_checklist_order", "work_item_id", "position"),
    )


class ProjectComment(TimestampMixin, Base):
    __tablename__ = "project_comments"

    id = Column(String(36), primary_key=True)
    work_item_id = Column(
        String(36), ForeignKey("project_work_items.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    author = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    edited_at = Column(DateTime, nullable=True)

    work_item = relationship("ProjectWorkItem", back_populates="comments")

    __table_args__ = (
        Index("ix_project_comments_item_created", "work_item_id", "created_at"),
    )


class ProjectAttachment(TimestampMixin, Base):
    __tablename__ = "project_attachments"

    id = Column(String(36), primary_key=True)
    work_item_id = Column(
        String(36), ForeignKey("project_work_items.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    uploader = Column(String, nullable=False)
    kind = Column(String(16), nullable=False, default="reference")
    description = Column(String(500), nullable=False, default="")
    original_name = Column(String(240), nullable=False)
    storage_key = Column(String(160), nullable=False, unique=True)
    mime = Column(String(160), nullable=False)
    size = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False, index=True)
    status = Column(String(16), nullable=False, default="ready", index=True)
    supersedes_id = Column(
        String(36), ForeignKey("project_attachments.id", ondelete="SET NULL"),
        nullable=True,
    )

    work_item = relationship("ProjectWorkItem", back_populates="attachments")

    __table_args__ = (
        Index("ix_project_attachments_item_created", "work_item_id", "created_at"),
    )


class ProjectActivity(Base):
    """Append-only project audit/event stream."""

    __tablename__ = "project_activity"

    id = Column(String(36), primary_key=True)
    project_id = Column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    work_item_id = Column(
        String(36), ForeignKey("project_work_items.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    actor = Column(String, nullable=False)
    event_type = Column(String(40), nullable=False, index=True)
    summary = Column(String(500), nullable=False, default="")
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=utcnow_naive, index=True)

    project = relationship("Project", back_populates="activities")

    __table_args__ = (
        Index("ix_project_activity_project_created", "project_id", "created_at"),
        Index("ix_project_activity_item_created", "work_item_id", "created_at"),
    )


class ProgressionEvent(Base):
    """Immutable, owner-scoped evidence used by Restia's progression system.

    Progress is awarded only from a verified state transition in an existing
    feature (for example a todo moving from open to done). ``event_key`` is a
    stable idempotency key, so reopening and re-completing the same work cannot
    mint XP repeatedly.
    """

    __tablename__ = "progression_events"

    id = Column(String(36), primary_key=True)
    owner = Column(String, nullable=False, index=True)
    event_key = Column(String(180), nullable=False)
    source_type = Column(String(48), nullable=False, index=True)
    source_id = Column(String(180), nullable=False, default="")
    title = Column(String(240), nullable=False, default="Completed work")
    xp = Column(Integer, nullable=False)
    details = Column(JSON, nullable=False, default=dict)
    occurred_at = Column(DateTime, nullable=False, default=utcnow_naive, index=True)

    __table_args__ = (
        UniqueConstraint("owner", "event_key", name="uq_progression_owner_event"),
        Index("ix_progression_owner_occurred", "owner", "occurred_at"),
        Index("ix_progression_owner_source", "owner", "source_type"),
    )


class PlanningItem(TimestampMixin, Base):
    """Owner-scoped human work that can be completed or placed on Calendar.

    ``ScheduledTask`` represents an automation Restia runs.  Planning items are
    deliberately separate: they are commitments the user intends to do.  The
    optimistic ``version`` field prevents two open clients from silently
    overwriting each other, while the optional calendar link keeps scheduling
    explicit instead of turning every to-do into an event.
    """

    __tablename__ = "planning_items"

    id = Column(String(36), primary_key=True)
    owner = Column(String, nullable=False, index=True)
    title = Column(EncryptedContentText, nullable=False)
    details = Column(EncryptedContentText, nullable=False, default="")
    status = Column(String(16), nullable=False, default="open", index=True)
    priority = Column(String(16), nullable=False, default="normal")
    due_date = Column(String(10), nullable=True, index=True)
    scheduled_start = Column(DateTime, nullable=True, index=True)
    scheduled_end = Column(DateTime, nullable=True)
    calendar_id = Column(
        String, ForeignKey("calendars.id", ondelete="SET NULL"), nullable=True,
    )
    calendar_event_uid = Column(String, nullable=True)
    completed_at = Column(DateTime, nullable=True, index=True)
    source = Column(String(24), nullable=False, default="user")
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        ForeignKeyConstraint(
            ("calendar_event_uid", "calendar_id"),
            ("calendar_events.uid", "calendar_events.calendar_id"),
            ondelete="SET NULL",
            name="fk_planning_items_calendar_event",
        ),
        UniqueConstraint(
            "calendar_event_uid", "calendar_id",
            name="uq_planning_items_calendar_event",
        ),
        Index("ix_planning_owner_status_due", "owner", "status", "due_date"),
        Index("ix_planning_owner_updated", "owner", "updated_at"),
    )


class Document(TimestampMixin, Base):
    """Living document that the AI can create and edit in-place."""
    __tablename__ = "documents"

    id              = Column(String, primary_key=True, index=True)
    session_id      = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    title           = Column(String, nullable=False, default="Untitled")
    language        = Column(String, nullable=True)          # "python", "markdown", "text", etc.
    current_content = Column(Text, nullable=False, default="")
    version_count   = Column(Integer, default=1)
    is_active       = Column(Boolean, default=True)
    # Soft-archive: hidden from the Library's Documents list/search/Tidy until
    # restored. Distinct from is_active (which tracks "open in a session").
    archived        = Column(Boolean, default=False)
    # Owner of this document. Documents used to derive ownership from their
    # linked chat session, but a session can be deleted (session_id → NULL via
    # SET NULL), orphaning the doc and making it vanish from the owner's
    # Library + search. Owning the row directly is robust against that.
    owner           = Column(String, nullable=True, index=True)
    tidy_verdict    = Column(String, nullable=True)        # "keep", "junk", or None (not yet reviewed)
    # Provenance: if this document was created by opening an email attachment,
    # these point back to the source email so the "Sign and reply" flow can
    # thread a response on the original conversation.
    source_email_uid         = Column(String, nullable=True)
    source_email_folder      = Column(String, nullable=True)
    source_email_account_id  = Column(String, nullable=True)
    source_email_message_id  = Column(String, nullable=True, index=True)

    session  = relationship("Session", backref=backref("documents", cascade="save-update, merge"))
    versions = relationship("DocumentVersion", back_populates="document",
                           cascade="all, delete-orphan", order_by="DocumentVersion.version_number")


class DocumentVersion(Base):
    """Immutable snapshot of a document at a point in time."""
    __tablename__ = "document_versions"

    id             = Column(String, primary_key=True, index=True)
    document_id    = Column(String, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    version_number = Column(Integer, nullable=False)
    content        = Column(Text, nullable=False)
    summary        = Column(String, nullable=True)     # Edit description
    source         = Column(String, default="ai")      # "ai" or "user"
    created_at     = Column(DateTime, default=utcnow_naive)

    document = relationship("Document", back_populates="versions")


class GalleryAlbum(TimestampMixin, Base):
    """A photo album/folder."""
    __tablename__ = "gallery_albums"

    id          = Column(String, primary_key=True, index=True)
    name        = Column(String, nullable=False)
    description = Column(Text, default="")
    cover_id    = Column(String, nullable=True)  # GalleryImage.id for cover photo
    owner       = Column(String, nullable=True, index=True)

    images = relationship("GalleryImage", back_populates="album")


class GalleryImage(TimestampMixin, Base):
    """Stores metadata for photos and AI-generated images."""
    __tablename__ = "gallery_images"

    id         = Column(String, primary_key=True, index=True)
    filename   = Column(String, nullable=False, unique=True)
    prompt     = Column(Text, nullable=False, default="")
    caption    = Column(Text, nullable=True, default="")
    model      = Column(String, nullable=True)
    size       = Column(String, nullable=True)
    quality    = Column(String, nullable=True)
    tags       = Column(String, nullable=True, default="")
    ai_tags    = Column(Text, nullable=True, default="")       # AI-generated tags (comma-separated)
    session_id = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    album_id   = Column(String, ForeignKey("gallery_albums.id", ondelete="SET NULL"), nullable=True, index=True)
    owner      = Column(String, nullable=True, index=True)
    is_active  = Column(Boolean, default=True)
    favorite   = Column(Boolean, default=False)

    # File integrity
    file_hash  = Column(String(64), nullable=True, index=True)  # SHA-256

    # EXIF / photo metadata
    taken_at       = Column(DateTime, nullable=True, index=True)  # EXIF DateTimeOriginal
    camera_make    = Column(String, nullable=True)
    camera_model   = Column(String, nullable=True)
    gps_lat        = Column(String, nullable=True)  # stored as string for precision
    gps_lng        = Column(String, nullable=True)
    width          = Column(Integer, nullable=True)
    height         = Column(Integer, nullable=True)
    file_size      = Column(Integer, nullable=True)  # bytes

    session = relationship("Session", backref=backref("gallery_images"))
    album   = relationship("GalleryAlbum", back_populates="images")

    __table_args__ = (
        Index('ix_gallery_images_tags', 'tags'),
        Index('ix_gallery_images_model', 'model'),
        Index('ix_gallery_images_active', 'is_active', 'created_at'),
    )


class EmailAccount(TimestampMixin, Base):
    """A configured IMAP/SMTP account. Supports multiple accounts per user —
    exactly one row per owner has is_default=True.

    Security note: imap_password / smtp_password are stored Fernet-encrypted
    via src/secret_storage.py. The key lives at data/.app_key (mode 0o600,
    gitignored). Anyone with read access to that file can decrypt every
    row, so the threat model is "stolen SQLite backup" rather than
    "process compromise". On first start any legacy plaintext rows are
    migrated automatically (see _migrate_encrypt_email_passwords).
    """
    __tablename__ = "email_accounts"

    id             = Column(String, primary_key=True, index=True)
    owner          = Column(String, nullable=True, index=True)
    name           = Column(String, nullable=False)  # Display name: "Work", "Personal", etc.
    is_default     = Column(Boolean, default=False, nullable=False)
    enabled        = Column(Boolean, default=True, nullable=False)

    # IMAP (receiving)
    imap_host      = Column(String, default="")
    imap_port      = Column(Integer, default=993)
    imap_user      = Column(String, default="")
    imap_password  = Column(String, default="")
    imap_starttls  = Column(Boolean, default=True)

    # SMTP (sending)
    smtp_host      = Column(String, default="")
    smtp_port      = Column(Integer, default=465)
    smtp_security  = Column(String, default="ssl")  # ssl | starttls | none
    smtp_user      = Column(String, default="")
    smtp_password  = Column(String, default="")

    from_address   = Column(String, default="")
    display_name   = Column(String, nullable=True)   # "Hriday Ranka" — used in From: header

    # OAuth2 (Google / Google Workspace). Tokens stored encrypted via secret_storage.
    oauth_provider      = Column(String, nullable=True)   # "google" or None
    oauth_access_token  = Column(String, nullable=True)   # encrypted
    oauth_refresh_token = Column(String, nullable=True)   # encrypted
    oauth_token_expiry  = Column(String, nullable=True)   # unix timestamp string

    __table_args__ = (
        Index('ix_email_accounts_owner_default', 'owner', 'is_default'),
    )


class ModelEndpoint(TimestampMixin, Base):
    """Admin-configured model endpoints. Models are auto-discovered via /v1/models."""
    __tablename__ = "model_endpoints"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)          # Display label, e.g. "Local vLLM", "OpenRouter"
    base_url = Column(String, nullable=False)      # Base URL, e.g. "http://localhost:8002/v1"
    api_key = Column(EncryptedText, nullable=True)  # Optional provider API key, encrypted at rest
    is_enabled = Column(Boolean, default=True)
    hidden_models = Column(Text, nullable=True)    # JSON list of model IDs that failed probing
    cached_models = Column(Text, nullable=True)    # JSON list of last-known model IDs (avoids probe on list)
    pinned_models = Column(Text, nullable=True)    # JSON list of admin-pinned model IDs (manual, may not appear in /v1/models)
    model_type = Column(String, nullable=True, default="llm")  # "llm" or "image"
    # auto = classify by URL; local = self-hosted server; api/proxy = external
    # OpenAI-compatible API even when reachable through a private/tailnet IP.
    endpoint_kind = Column(String, nullable=True, default="auto")
    # auto = background refresh with TTL/backoff; manual/disabled = cached-first
    # only unless an explicit endpoint probe is requested.
    model_refresh_mode = Column(String, nullable=True, default="auto")
    model_refresh_interval = Column(Integer, nullable=True, default=None)
    model_refresh_timeout = Column(Integer, nullable=True, default=None)
    # Whether models on this endpoint accept OpenAI-style function
    # schemas + emit `tool_calls`. Auto-detected at Cookbook auto-
    # register time from `--enable-auto-tool-choice` in the serve cmd;
    # can be toggled per-endpoint in the UI. NULL = unknown, falls
    # back to the model-name keyword heuristic in agent_loop.py.
    supports_tools = Column(Boolean, nullable=True, default=None)
    # Per-user ownership. NULL = legacy/shared (visible to every user) — this
    # is the historical default. When non-null, the model picker only shows
    # the endpoint to that user (admins always see everything).
    owner = Column(String, nullable=True, index=True)
    # Optional OAuth/session-backed credential row. Used by subscription-backed
    # providers that need refresh tokens instead of a static API key.
    provider_auth_id = Column(String, nullable=True, index=True)


class ProviderAuthSession(TimestampMixin, Base):
    """Encrypted OAuth/session credentials for refresh-aware model providers."""
    __tablename__ = "provider_auth_sessions"

    id = Column(String, primary_key=True, index=True)
    provider = Column(String, nullable=False, index=True)
    owner = Column(String, nullable=True, index=True)
    label = Column(String, nullable=True)
    base_url = Column(String, nullable=False)
    access_token = Column(EncryptedText, nullable=True)
    refresh_token = Column(EncryptedText, nullable=True)
    last_refresh = Column(DateTime, nullable=True)
    auth_mode = Column(String, nullable=True)

class McpServer(TimestampMixin, Base):
    """Admin-configured MCP (Model Context Protocol) tool servers."""
    __tablename__ = "mcp_servers"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    transport = Column(String, nullable=False, default="stdio")  # "stdio" or "sse"
    command = Column(String, nullable=True)      # For stdio: executable path
    args = Column(Text, nullable=True)           # JSON array of command args
    env = Column(Text, nullable=True)            # JSON object of env vars
    url = Column(String, nullable=True)          # For SSE: server URL
    is_enabled = Column(Boolean, default=True)
    oauth_config = Column(Text, nullable=True)   # JSON: provider, keys_file, token_file, scopes
    disabled_tools = Column(Text, nullable=True)  # JSON array of tool names to hide from LLM
    oauth_tokens = Column(EncryptedText, nullable=True)  # JSON {tokens, client_info} for generic MCP OAuth, encrypted at rest


class Comparison(TimestampMixin, Base):
    """Stores A/B model comparison results."""
    __tablename__ = "comparisons"

    id = Column(String, primary_key=True, index=True)
    session_id = Column(String, nullable=True)     # Parent session context (optional)
    owner = Column(String, nullable=True, index=True)  # username
    prompt = Column(Text, nullable=False)
    model_a = Column(String, nullable=False)
    model_b = Column(String, nullable=False)
    endpoint_a = Column(String, nullable=False)
    endpoint_b = Column(String, nullable=False)
    response_a = Column(Text, nullable=True)
    response_b = Column(Text, nullable=True)
    metrics_a = Column(Text, nullable=True)         # JSON string
    metrics_b = Column(Text, nullable=True)         # JSON string
    winner = Column(String, nullable=True)           # "a", "b", "tie", or null
    is_blind = Column(Boolean, default=True)
    blind_mapping = Column(Text, nullable=True)      # JSON: {"left": "a"/"b", "right": "a"/"b"}
    voted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index('ix_comparisons_voted_at', 'voted_at'),
    )


class Signature(TimestampMixin, Base):
    """User-saved visual signatures (image stamps).

    Reusable across PDF form filling, email composition, and document editing.
    `data_png` is a base64-encoded PNG (no `data:` prefix). The SVG vector
    column is reserved for future smooth vector storage. Both are stored
    Fernet-encrypted at rest (see EncryptedText / src.secret_storage); a
    handwritten signature is sensitive, so it must never sit plaintext in the
    DB file. Existing rows are migrated automatically on startup.
    """
    __tablename__ = "signatures"

    id = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    name = Column(String, nullable=False, default="Signature")
    data_png = Column(EncryptedContentText, nullable=False)   # base64 PNG, encrypted at rest
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    svg = Column(EncryptedContentText, nullable=True)         # vector signature, encrypted at rest


class ApiToken(TimestampMixin, Base):
    """API tokens for external integrations (n8n, Make, etc.)."""
    __tablename__ = "api_tokens"

    id = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    account_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    name = Column(String, nullable=False)
    token_hash = Column(String, nullable=False)
    token_prefix = Column(String, nullable=False)  # first 8 chars for display
    digest_scheme = Column(String(32), nullable=False, default="bcrypt_legacy")
    scopes = Column(String, nullable=False, default="chat")
    is_active = Column(Boolean, default=True)
    last_used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)


class Webhook(TimestampMixin, Base):
    """Outgoing webhooks fired on events."""
    __tablename__ = "webhooks"

    id = Column(String, primary_key=True, index=True)
    name = Column(String, nullable=False)
    url = Column(String, nullable=False)
    secret = Column(String, nullable=True)  # HMAC-SHA256 signing secret
    events = Column(String, nullable=False)  # comma-separated event types
    is_active = Column(Boolean, default=True)
    last_triggered_at = Column(DateTime, nullable=True)
    last_status_code = Column(Integer, nullable=True)
    last_error = Column(String, nullable=True)


class UserTool(TimestampMixin, Base):
    """User-created sandboxed mini-apps/tools."""
    __tablename__ = "user_tools"

    id            = Column(String, primary_key=True, index=True)
    name          = Column(String, nullable=False)
    description   = Column(Text, nullable=True)
    icon          = Column(String, nullable=True, default="")
    html_content  = Column(Text, nullable=False)
    scope         = Column(String, nullable=False, default="global")  # "global" or session_id
    session_id    = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    owner         = Column(String, nullable=True, index=True)      # username
    is_pinned     = Column(Boolean, default=False)
    is_active     = Column(Boolean, default=True)
    version       = Column(Integer, default=1)
    author        = Column(String, nullable=True, default="ai")

    session = relationship("Session", backref=backref("user_tools", cascade="all, delete-orphan"))

    __table_args__ = (
        Index('ix_user_tools_scope', 'scope'),
        Index('ix_user_tools_active', 'is_active'),
    )


class UserToolData(Base):
    """Key-value storage for user tool persistent data."""
    __tablename__ = "user_tool_data"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    tool_id    = Column(String, ForeignKey("user_tools.id", ondelete="CASCADE"), nullable=False)
    key        = Column(String, nullable=False)
    value      = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive)
    updated_at = Column(DateTime, default=utcnow_naive, onupdate=utcnow_naive)

    tool = relationship("UserTool", backref=backref("data_entries", cascade="all, delete-orphan"))

    __table_args__ = (
        Index('ix_user_tool_data_tool_key', 'tool_id', 'key', unique=True),
    )


class DirectMessage(Base):
    """A one-to-one message between two user accounts (WhatsApp-style DMs).

    A "conversation" is derived from the unordered {sender, recipient} pair —
    there is no separate conversations table. Bodies are encrypted at rest
    (EncryptedText) since private user-to-user messages are sensitive, matching
    how email passwords / signatures / endpoint keys are stored. Ownership is
    strict: a row is only ever visible to its sender or recipient, enforced in
    routes/messaging_routes.py. Deletion is soft (deleted_at + blanked body)
    so the other side's client renders a tombstone instead of a silent gap."""
    __tablename__ = "direct_messages"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    sender      = Column(String, nullable=False, index=True)   # username who sent it
    recipient   = Column(String, nullable=False, index=True)   # username it's addressed to
    body        = Column(EncryptedContentText, nullable=False)
    created_at  = Column(DateTime, default=utcnow_naive, nullable=False, index=True)
    read_at     = Column(DateTime, nullable=True)              # NULL = unread by recipient
    edited_at   = Column(DateTime, nullable=True)              # NULL = never edited
    deleted_at  = Column(DateTime, nullable=True)              # soft delete: body blanked, tombstone kept
    reply_to_id = Column(Integer, nullable=True)               # quoted message id, same {sender,recipient} pair
    reactions   = Column(Text, nullable=True)                  # JSON {username: emoji}; parsed defensively in routes

    __table_args__ = (
        # Pull one conversation's messages in order.
        Index('ix_dm_pair', 'sender', 'recipient', 'created_at'),
        # Fast unread-count / inbox scans for a recipient.
        Index('ix_dm_unread', 'recipient', 'read_at'),
    )


class DirectMessageAttachment(Base):
    """A durable raster attachment belonging to one direct message.

    Attachment bytes are normalized before insert and stored as base64 through
    ``EncryptedText``.  Keeping media in its own table avoids reusing the
    assistant-chat upload store, whose ownership and retention rules are wrong
    for a two-party conversation.  Read authorization is always derived from
    the parent ``DirectMessage`` pair; there is deliberately no owner/admin
    shortcut on this model.
    """
    __tablename__ = "direct_message_attachments"

    id         = Column(String(36), primary_key=True)
    message_id = Column(
        Integer,
        ForeignKey("direct_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename   = Column(EncryptedContentText, nullable=False)
    mime       = Column(String(32), nullable=False)
    size       = Column(Integer, nullable=False)
    width      = Column(Integer, nullable=False)
    height     = Column(Integer, nullable=False)
    sha256     = Column(String(64), nullable=False)
    data_b64   = Column(EncryptedContentText, nullable=False)
    created_at = Column(DateTime, default=utcnow_naive, nullable=False)

    __table_args__ = (
        Index("ix_dm_attachment_message_created", "message_id", "created_at"),
    )


class LinkGuest(Base):
    """A Home Link guest — someone running their own instance who registered
    with this hub to DM its owner (routes/link_routes.py). Only the SHA-256 of
    the guest's bearer token is stored; the plaintext token lives on the
    guest's instance. Guests appear in direct_messages as '<handle>@remote'.

    A guest is never a user account: no login and no local profile privileges.
    Once approved, the token unlocks the guest↔owner conversation and may
    accept explicit, project-scoped ``ProjectRemoteGrant`` capabilities. New
    registrations start 'pending' and can't send, read, or accept project work;
    'blocked' keeps the handle reserved so a spammer can't re-register it."""
    __tablename__ = "link_guests"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    handle     = Column(String, nullable=False, unique=True, index=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    status     = Column(String, nullable=False, default="pending", index=True)
    created_at = Column(DateTime, default=utcnow_naive, nullable=False)
    last_seen  = Column(DateTime, nullable=True)
    # An approved invite code (LinkInvite) can create a guest pre-approved, so
    # no manual owner approval is needed. NULL = classic register-and-wait guest.
    invite_id  = Column(Integer, nullable=True, index=True)
    # Guest's E2EE public key (base64 X25519), published at redeem time so local
    # users can encrypt to it. NULL until the guest's instance supports E2EE.
    pubkey     = Column(Text, nullable=True)
    # Capability negotiated when the credential was admitted. General
    # Messages invitations are chat-only; legacy and project-pairing guests
    # retain the historical full capability unless explicitly scoped.
    scope      = Column(String(16), nullable=False, default="full", server_default="full")


class LinkInvite(Base):
    """A hub-issued invite code. Redeeming one (routes/link_routes.py) creates
    an *approved* LinkGuest immediately — the code IS the approval, replacing
    the manual pending→approve gate for people the operator deliberately hands
    a code to. Only the SHA-256 of the code is stored; the plaintext is shown
    to the admin once at creation and never persisted. Codes expire, are
    use-capped, and are revocable, so a leaked code has a bounded blast radius."""
    __tablename__ = "link_invites"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    code_hash  = Column(String, nullable=False, unique=True, index=True)
    created_by = Column(String, nullable=False)                # admin who issued it
    label      = Column(String, nullable=True)                 # optional note ("for alice")
    created_at = Column(DateTime, default=utcnow_naive, nullable=False)
    expires_at = Column(DateTime, nullable=True)               # NULL = never (discouraged)
    max_uses   = Column(Integer, nullable=False, default=1)
    uses       = Column(Integer, nullable=False, default=0)
    revoked    = Column(Boolean, nullable=False, default=False)
    # Project pairing codes remain ordinary Home Link invitations, but retain
    # enough context for the owner UI to identify the exact installation that
    # redeemed the code. Project access is still a separate, explicit grant.
    project_id   = Column(String(36), nullable=True, index=True)
    project_role = Column(String(16), nullable=True)
    hub_url      = Column(String(2048), nullable=True)


class RemoteContactPref(Base):
    """Per-local-user control over remote (Home Link guest) contact.
    A redeemed invite grants instance-wide reach, but each local account can
    opt out of being reachable by remote guests. One row per local user; the
    absence of a row means the default (discoverable) applies."""
    __tablename__ = "remote_contact_prefs"

    local_user   = Column(String, primary_key=True)            # normalized username
    discoverable = Column(Boolean, nullable=False, default=True)
    updated_at   = Column(DateTime, default=utcnow_naive, nullable=False)


class RemoteBlock(Base):
    """A specific local user blocking a specific remote guest handle. Blocked
    pairs can't exchange messages and the local user is hidden from that guest's
    directory, without affecting the guest's other conversations."""
    __tablename__ = "remote_blocks"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    local_user = Column(String, nullable=False, index=True)     # normalized username
    handle     = Column(String, nullable=False, index=True)     # guest handle (no @remote)
    created_at = Column(DateTime, default=utcnow_naive, nullable=False)

    __table_args__ = (
        Index('ix_remote_block_pair', 'local_user', 'handle', unique=True),
    )


class HomeLink(Base):
    """This instance's registration with its home server — the credential
    behind a connected Restia contact (routes/link_routes.py).
    New pairings use one installation sentinel in ``local_user``; older
    profile-scoped rows remain readable for migration compatibility. The
    profile that established the shared pairing is kept in ``owner`` as a
    local-only call-routing hint. The bearer token is encrypted at rest."""
    __tablename__ = "home_link"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    local_user = Column(String, nullable=False, default="", unique=True, index=True)
    home_url   = Column(String, nullable=False)
    handle     = Column(String, nullable=False)
    owner      = Column(String, nullable=True)                  # hub owner's username
    token      = Column(EncryptedText, nullable=False)
    created_at = Column(DateTime, default=utcnow_naive, nullable=False)


class OutboundChatLink(Base):
    """One additive outbound chat connection to another Restia installation.

    Full-capability ``HomeLink`` remains deliberately singular because calls
    and linked Projects use it as their primary credential. General Messages
    invitations live here instead, keyed by an opaque UUID so multiple remote
    installations can coexist without hostname collisions or credential
    ambiguity.
    """
    __tablename__ = "outbound_chat_links"

    id         = Column(String(36), primary_key=True)
    home_url   = Column(String(2048), nullable=False, unique=True, index=True)
    handle     = Column(String(32), nullable=False)
    owner      = Column(String, nullable=False, index=True)
    token      = Column(EncryptedText, nullable=False)
    created_at = Column(DateTime, default=utcnow_naive, nullable=False)


class UserKey(Base):
    """A local account's end-to-end-encryption identity keys.

    The server stores the PUBLIC key (so others can encrypt to this account)
    and the private key only in WRAPPED form: AES-GCM ciphertext produced in
    the browser from a key the client derives (PBKDF2) from the user's separate
    encryption passphrase, which is never transmitted. The server therefore
    cannot unwrap the private key or read any message — a stolen database
    yields only ciphertext. The flip side is unrecoverability: if the user
    forgets the passphrase, the wrapped key can't be opened and those messages
    are lost, because the server has nothing that could help. This is stronger
    than the Fernet at-rest encryption on other columns, where a server-held
    key can decrypt everything."""
    __tablename__ = "user_keys"

    username        = Column(String, primary_key=True)          # normalized username
    public_jwk      = Column(Text, nullable=False)              # ECDH P-256 public JWK (JSON)
    wrapped_private = Column(Text, nullable=False)              # {iv, ct}: AES-GCM of the private JWK
    kdf_salt        = Column(String, nullable=False)           # base64 PBKDF2 salt
    kdf_iterations  = Column(Integer, nullable=False, default=210000)
    created_at      = Column(DateTime, default=utcnow_naive, nullable=False)
    updated_at      = Column(DateTime, default=utcnow_naive, nullable=False)


class UserProfile(Base):
    """A local account's display profile — the name (and avatar color) shown in
    chat instead of the raw login username. One row per user; the absence of a
    row just means 'fall back to the username'."""
    __tablename__ = "user_profiles"

    username     = Column(String, primary_key=True)          # normalized login username
    display_name = Column(String, nullable=True)
    avatar_color = Column(String, nullable=True)             # optional hex/hsl override
    updated_at   = Column(DateTime, default=utcnow_naive, nullable=False)


class StatusPost(Base):
    """A BeReal-style 'what I'm doing' photo shared with your chat contacts.

    Ephemeral by design: every post carries an expiry (default 24h) and is only
    visible to accounts you've exchanged direct messages with, plus yourself.
    The image (a base64 data URL) and caption are Fernet-encrypted at rest like
    DM bodies, so a stolen database doesn't leak the pictures."""
    __tablename__ = "status_posts"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    author     = Column(String, nullable=False, index=True)
    image      = Column(EncryptedContentText, nullable=False)     # base64 data URL, encrypted at rest
    caption    = Column(EncryptedContentText, nullable=True)
    created_at = Column(DateTime, default=utcnow_naive, nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)


class StatusView(Base):
    """Records that `viewer` has seen a status post — powers the 'unseen ring'
    around a contact's avatar and the seen-by list for the author."""
    __tablename__ = "status_views"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    status_id  = Column(Integer, nullable=False, index=True)
    viewer     = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=utcnow_naive, nullable=False)

    __table_args__ = (
        Index('ix_status_view_pair', 'status_id', 'viewer', unique=True),
    )


class CrewMember(TimestampMixin, Base):
    """A custom AI persona ('crew member') with its own personality, model, tools, and memory scope."""
    __tablename__ = "crew_members"

    id            = Column(String, primary_key=True, index=True)
    owner         = Column(String, nullable=True, index=True)
    name          = Column(String, nullable=False)
    avatar        = Column(String, nullable=True)
    user_name     = Column(String, nullable=True)          # what they call the user
    personality   = Column(Text, nullable=True)             # system prompt
    model         = Column(String, nullable=True)
    endpoint_url  = Column(String, nullable=True)
    greeting      = Column(Text, nullable=True)
    enabled_tools = Column(Text, nullable=True)             # JSON array or "all"
    session_id    = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    is_active     = Column(Boolean, default=True)
    sort_order    = Column(Integer, default=0)
    is_default_assistant = Column(Boolean, default=False)   # singleton per-owner "personal assistant"
    timezone      = Column(String, nullable=True)           # IANA tz name (e.g. "America/New_York") for scheduled check-ins

    session = relationship("Session", foreign_keys=[session_id],
                           backref=backref("crew_member", uselist=False))


class ScheduledTask(TimestampMixin, Base):
    """A recurring or one-off task — LLM-powered or direct action, time or event triggered."""
    __tablename__ = "scheduled_tasks"

    id             = Column(String, primary_key=True, index=True)
    owner          = Column(String, nullable=True, index=True)
    name           = Column(String, nullable=False, default="Untitled Task")
    prompt         = Column(Text, nullable=True)              # LLM prompt (for task_type="llm")
    task_type      = Column(String, default="llm")            # "llm" | "action"
    action         = Column(String, nullable=True)            # builtin action name (for task_type="action")
    schedule       = Column(String, nullable=True)            # "once", "daily", "weekly", "monthly"
    scheduled_time = Column(String, nullable=True)            # "HH:MM" (24h, stored UTC)
    scheduled_day  = Column(Integer, nullable=True)           # day-of-week 0=Mon for weekly, day-of-month for monthly
    scheduled_date = Column(DateTime, nullable=True)          # exact datetime for "once"
    trigger_type   = Column(String, default="schedule")       # "schedule" | "event"
    trigger_event  = Column(String, nullable=True)            # e.g. "session_created", "message_sent"
    trigger_count  = Column(Integer, nullable=True)           # fire every N events
    trigger_counter = Column(Integer, default=0)              # current count toward trigger_count
    next_run       = Column(DateTime, nullable=True, index=True)
    last_run       = Column(DateTime, nullable=True)
    status         = Column(String, default="active")         # "active", "paused", "completed"
    output_target  = Column(String, default="session")        # "session" (extensible later)
    session_id     = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True)
    model          = Column(String, nullable=True)
    endpoint_url   = Column(String, nullable=True)
    run_count      = Column(Integer, default=0)

    cron_expression = Column(String, nullable=True)           # cron string e.g. "*/5 * * * *"
    then_task_id   = Column(String, ForeignKey("scheduled_tasks.id", ondelete="SET NULL"), nullable=True)
    webhook_token  = Column(String, nullable=True, unique=True)
    crew_member_id = Column(String, nullable=True)     # optional link to crew_members.id
    # character_id historically referenced an agent_characters table that was
    # never actually created. Keep the column for schema compatibility but
    # drop the ForeignKey so SQLAlchemy table sort doesn't fail on flush.
    character_id   = Column(String, nullable=True)
    max_steps      = Column(Integer, nullable=True)       # max agent loop iterations (null=unlimited)
    email_results  = Column(Boolean, default=True)        # email results to character.email_to
    notifications_enabled = Column(Boolean, default=True) # per-task on/off for completion notifications

    session = relationship("Session", backref=backref("scheduled_tasks", cascade="save-update, merge"))
    then_task = relationship("ScheduledTask", remote_side=[id], foreign_keys=[then_task_id])

    __table_args__ = (
        Index('ix_scheduled_tasks_due', 'status', 'next_run'),
        Index('ix_scheduled_tasks_event', 'trigger_type', 'trigger_event', 'status'),
    )


class EditorDraft(TimestampMixin, Base):
    """Persisted in-progress gallery-editor session — layered project state
    that the user can close and reopen later. Stores the full layer payload
    as JSON (with base64-encoded PNG dataURLs per layer) plus a small
    thumbnail for the landing-screen list.
    """
    __tablename__ = "editor_drafts"

    id              = Column(String, primary_key=True, index=True)
    owner           = Column(String, nullable=True, index=True)
    name            = Column(String, nullable=False, default="Untitled")
    # If the draft was opened FROM a gallery photo, point back at it so we
    # can show "Resuming edit of <photo>" and so reopening that photo picks
    # up the same draft rather than starting fresh.
    source_image_id = Column(String, nullable=True, index=True)
    width           = Column(Integer, nullable=True)
    height          = Column(Integer, nullable=True)
    # Full draft body — layer pixels (base64 PNG dataURLs), offsets,
    # opacities, visibility, active id, next id, etc. Kept as TEXT/JSON so
    # we don't have to re-shape the model every time the editor adds a
    # new piece of state.
    payload         = Column(Text, nullable=False, default="")
    # Tiny preview (data URL, ~128px wide) for the landing list. Stored
    # inline so the list endpoint can return everything in one shot.
    thumbnail       = Column(Text, nullable=True)
    is_active       = Column(Boolean, default=True)

    __table_args__ = (
        Index('ix_editor_drafts_owner_updated', 'owner', 'is_active', 'updated_at'),
    )


class TaskRun(Base):
    """Record of a single execution of a ScheduledTask."""
    __tablename__ = "task_runs"

    id          = Column(String, primary_key=True, index=True)
    task_id     = Column(String, ForeignKey("scheduled_tasks.id", ondelete="CASCADE"), nullable=False)
    started_at  = Column(DateTime, nullable=False, default=utcnow_naive)
    finished_at = Column(DateTime, nullable=True)
    status      = Column(String, default="running")  # "running", "success", "error"
    result      = Column(Text, nullable=True)
    error       = Column(Text, nullable=True)
    tokens_used = Column(Integer, nullable=True)
    steps       = Column(Text, nullable=True)             # JSON log of agent tool calls
    model       = Column(String, nullable=True)           # model that actually ran (resolved at execution)

    task = relationship("ScheduledTask", backref=backref("runs", cascade="all, delete-orphan",
                        order_by="TaskRun.started_at.desc()"))

    __table_args__ = (
        Index('ix_task_runs_task', 'task_id', 'started_at'),
    )


class Memory(Base):
    """
    SQLAlchemy model for Memory table.
    Represents persistent memory entries with metadata.
    """
    __tablename__ = "memories"
    
    # Primary key
    id = Column(String, primary_key=True, index=True)
    
    # Memory content
    text = Column(Text, nullable=False)
    
    # Categorization
    category = Column(String, default='fact')
    source = Column(String, default='user')

    # Owner (username)
    owner = Column(String, nullable=True, index=True)

    # Reference to session (nullable)
    session_id = Column(String, ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True, index=True)

    # Timestamp as Unix timestamp
    timestamp = Column(Integer, default=lambda: int(utcnow_naive().timestamp()))

    # Relationship to Session
    session = relationship("Session", backref="memories")

    # Indexes - optimized composites
    __table_args__ = (
        Index('ix_memories_lookup', 'category', 'timestamp'),  # Composite for category-based queries
        Index('ix_memories_session', 'session_id', 'timestamp'),  # Composite for session-based queries
    )

def _migrate_add_link_columns():
    """Add the Home Link approval/scoping columns for databases created by
    the short-lived first cut of the feature (link_guests without `status`,
    home_link without `local_user`). Idempotent; new installs get the full
    schema from create_all. Pre-existing guests are left 'pending' so nobody
    silently gains access when the operator upgrades into the approval gate."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(link_guests)").fetchall()]
        if cols and "status" not in cols:
            conn.execute("ALTER TABLE link_guests ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_link_guests_status ON link_guests(status)")
        cols = [row[1] for row in conn.execute("PRAGMA table_info(home_link)").fetchall()]
        if cols and "local_user" not in cols:
            conn.execute("ALTER TABLE home_link ADD COLUMN local_user TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ix_home_link_local_user ON home_link(local_user)")
        conn.commit()
    except Exception as e:
        logging.getLogger(__name__).warning("Home Link column migration failed: %s", e)
    finally:
        if conn:
            conn.close()


def _migrate_add_dm_feature_columns():
    """Add the DM feature columns (editing, soft delete, replies, reactions)
    to direct_messages for databases created before real-time messaging.
    Guarded + idempotent; new installs get the full schema from create_all."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(direct_messages)").fetchall()]
        if cols:
            if "edited_at" not in cols:
                conn.execute("ALTER TABLE direct_messages ADD COLUMN edited_at DATETIME")
            if "deleted_at" not in cols:
                conn.execute("ALTER TABLE direct_messages ADD COLUMN deleted_at DATETIME")
            if "reply_to_id" not in cols:
                conn.execute("ALTER TABLE direct_messages ADD COLUMN reply_to_id INTEGER")
            if "reactions" not in cols:
                conn.execute("ALTER TABLE direct_messages ADD COLUMN reactions TEXT")
            conn.commit()
    except Exception as e:
        logging.getLogger(__name__).warning(f"direct_messages feature-columns migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_add_link_invite_columns():
    """Add Home Link invite columns introduced after invite-code onboarding.

    The tables themselves are created by ``create_all``. Existing installs
    still need guarded in-place column additions because ``create_all`` never
    alters a table. This migration is intentionally idempotent.
    """
    if not DATABASE_URL.startswith("sqlite:///"):
        from sqlalchemy import inspect as sqlalchemy_inspect

        try:
            with engine.begin() as connection:
                inspector = sqlalchemy_inspect(connection)
                table_names = set(inspector.get_table_names())
                additions = {
                    "link_guests": {
                        "invite_id": "INTEGER",
                        "pubkey": "TEXT",
                        "scope": "VARCHAR(16) DEFAULT 'full'",
                    },
                    "link_invites": {
                        "project_id": "VARCHAR(36)",
                        "project_role": "VARCHAR(16)",
                        "hub_url": "VARCHAR(2048)",
                    },
                }
                for table_name, columns in additions.items():
                    if table_name not in table_names:
                        continue
                    existing = {
                        str(column["name"]) for column in inspector.get_columns(table_name)
                    }
                    for column_name, column_type in columns.items():
                        if column_name not in existing:
                            connection.exec_driver_sql(
                                f"ALTER TABLE {table_name} ADD COLUMN "
                                f"{column_name} {column_type}"
                            )
                if "link_guests" in table_names:
                    connection.exec_driver_sql(
                        "UPDATE link_guests SET scope = 'full' "
                        "WHERE scope IS NULL OR scope = ''"
                    )
                    guest_indexes = {
                        str(index["name"]) for index in inspector.get_indexes("link_guests")
                    }
                    if "ix_link_guests_invite_id" not in guest_indexes:
                        connection.exec_driver_sql(
                            "CREATE INDEX ix_link_guests_invite_id "
                            "ON link_guests(invite_id)"
                        )
                if "link_invites" in table_names:
                    invite_indexes = {
                        str(index["name"]) for index in inspector.get_indexes("link_invites")
                    }
                    if "ix_link_invites_project_id" not in invite_indexes:
                        connection.exec_driver_sql(
                            "CREATE INDEX ix_link_invites_project_id "
                            "ON link_invites(project_id)"
                        )
            return
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "Home Link invite-columns migration failed on %s",
                engine.dialect.name,
            )
            raise RuntimeError(
                "Could not migrate Home Link project-pairing columns"
            ) from exc

    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(link_guests)").fetchall()]
        if cols:
            if "invite_id" not in cols:
                conn.execute("ALTER TABLE link_guests ADD COLUMN invite_id INTEGER")
            if "pubkey" not in cols:
                conn.execute("ALTER TABLE link_guests ADD COLUMN pubkey TEXT")
            if "scope" not in cols:
                conn.execute(
                    "ALTER TABLE link_guests ADD COLUMN scope VARCHAR(16) DEFAULT 'full'"
                )
            conn.execute(
                "UPDATE link_guests SET scope = 'full' WHERE scope IS NULL OR scope = ''"
            )
        invite_cols = [
            row[1]
            for row in conn.execute("PRAGMA table_info(link_invites)").fetchall()
        ]
        if invite_cols:
            if "project_id" not in invite_cols:
                conn.execute("ALTER TABLE link_invites ADD COLUMN project_id VARCHAR(36)")
            if "project_role" not in invite_cols:
                conn.execute("ALTER TABLE link_invites ADD COLUMN project_role VARCHAR(16)")
            if "hub_url" not in invite_cols:
                conn.execute("ALTER TABLE link_invites ADD COLUMN hub_url VARCHAR(2048)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_link_invites_project_id "
                "ON link_invites(project_id)"
            )
        conn.commit()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Home Link invite-columns migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_add_last_message_at_column():
    """Add last_message_at to sessions + backfill from the latest message
    timestamp per session (fallback to last_accessed / created_at when a
    session has no messages). Idempotent: column-add is guarded, and the
    backfill only touches rows where last_message_at is still NULL so it
    won't clobber live values on later restarts."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = [row[1] for row in cursor.fetchall()]
        if "last_message_at" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN last_message_at DATETIME")
        # Backfill any NULL rows: newest message timestamp, else last_accessed,
        # else created_at. Only fills NULLs so it's safe on every startup.
        conn.execute(
            """
            UPDATE sessions
               SET last_message_at = COALESCE(
                   (SELECT MAX(timestamp) FROM chat_messages
                     WHERE chat_messages.session_id = sessions.id),
                   last_accessed,
                   created_at
               )
             WHERE last_message_at IS NULL
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_sessions_last_message_at "
            "ON sessions(archived, last_message_at)"
        )
        conn.commit()
        logging.getLogger(__name__).info("Migrated: added + backfilled 'last_message_at' on sessions")
    except Exception as e:
        logging.getLogger(__name__).warning(f"last_message_at migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _migrate_add_document_archived_column():
    """Add `archived` to documents (soft-archive flag). Guarded + idempotent."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(documents)")
        columns = [row[1] for row in cursor.fetchall()]
        if "archived" not in columns:
            conn.execute("ALTER TABLE documents ADD COLUMN archived BOOLEAN DEFAULT 0")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'archived' to documents")
    except Exception as e:
        logging.getLogger(__name__).warning(f"documents.archived migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_add_owner_column():
    """Add owner column to sessions table if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = [row[1] for row in cursor.fetchall()]
        if "owner" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN owner TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_sessions_owner ON sessions(owner)")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'owner' column to sessions")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Migration check failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _migrate_model_endpoints():
    """Recreate model_endpoints table if schema changed (url->base_url)."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "base_url" not in columns:
            conn.execute("DROP TABLE IF EXISTS model_endpoints")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: dropped old model_endpoints table (schema change)")
    except Exception as e:
        logging.getLogger(__name__).warning(f"model_endpoints migration check failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _migrate_add_hidden_models_column():
    """Add hidden_models column to model_endpoints if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "hidden_models" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN hidden_models TEXT")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'hidden_models' column to model_endpoints")
    except Exception as e:
        logging.getLogger(__name__).warning(f"hidden_models migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _migrate_add_model_endpoint_owner_column():
    """Add owner column to model_endpoints if it doesn't exist.

    Without this column, the per-user model picker query
    `(owner == user) | (owner IS NULL)` fails with `OperationalError:
    no such column: model_endpoints.owner`, leaving non-admin users
    with an empty picker even when `allowed_models` is unrestricted.
    Backfills NULL for existing rows (treated as shared by the filter).
    """
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "owner" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN owner VARCHAR")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_model_endpoints_owner ON model_endpoints(owner)")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'owner' column + index to model_endpoints")
    except Exception as e:
        logging.getLogger(__name__).warning(f"model_endpoints.owner migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_add_provider_auth_id_column():
    """Add provider_auth_id column to model_endpoints if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "provider_auth_id" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN provider_auth_id VARCHAR")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_model_endpoints_provider_auth_id ON model_endpoints(provider_auth_id)")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'provider_auth_id' column + index to model_endpoints")
    except Exception as e:
        logging.getLogger(__name__).warning(f"model_endpoints.provider_auth_id migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_add_model_type_column():
    """Add model_type column to model_endpoints if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "model_type" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN model_type TEXT DEFAULT 'llm'")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'model_type' column to model_endpoints")
    except Exception as e:
        logging.getLogger(__name__).warning(f"model_type migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _migrate_add_model_endpoint_refresh_columns():
    """Add endpoint classification / refresh policy columns if missing."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "endpoint_kind" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN endpoint_kind TEXT DEFAULT 'auto'")
        if columns and "model_refresh_mode" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN model_refresh_mode TEXT DEFAULT 'auto'")
        if columns and "model_refresh_interval" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN model_refresh_interval INTEGER")
        if columns and "model_refresh_timeout" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN model_refresh_timeout INTEGER")
        conn.commit()
    except Exception as e:
        logging.getLogger(__name__).warning(f"model_endpoints refresh-policy migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _migrate_add_task_run_model_column():
    """Add model column to task_runs if it doesn't exist (records which model ran)."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(task_runs)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "model" not in columns:
            conn.execute("ALTER TABLE task_runs ADD COLUMN model TEXT")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'model' column to task_runs")
    except Exception as e:
        logging.getLogger(__name__).warning(f"task_runs model migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _migrate_add_supports_tools_column():
    """Add supports_tools column to model_endpoints if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "supports_tools" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN supports_tools BOOLEAN")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'supports_tools' column to model_endpoints")
    except Exception as e:
        logging.getLogger(__name__).warning(f"supports_tools migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_add_cached_models_column():
    """Add cached_models column to model_endpoints if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "cached_models" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN cached_models TEXT")
            conn.commit()
    except Exception as e:
        logging.getLogger(__name__).warning(f"cached_models migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _migrate_add_pinned_models_column():
    """Add pinned_models column to model_endpoints if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(model_endpoints)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "pinned_models" not in columns:
            conn.execute("ALTER TABLE model_endpoints ADD COLUMN pinned_models TEXT")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'pinned_models' column to model_endpoints")
    except Exception as e:
        logging.getLogger(__name__).warning(f"pinned_models migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _migrate_add_notes_sort_order():
    """Add sort_order, image_url, repeat columns to notes if they don't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(notes)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "sort_order" not in columns:
            conn.execute("ALTER TABLE notes ADD COLUMN sort_order INTEGER DEFAULT 0")
        if columns and "image_url" not in columns:
            conn.execute("ALTER TABLE notes ADD COLUMN image_url TEXT")
        if columns and "repeat" not in columns:
            conn.execute("ALTER TABLE notes ADD COLUMN repeat TEXT DEFAULT 'none'")
        if columns and "ai_classification" not in columns:
            conn.execute("ALTER TABLE notes ADD COLUMN ai_classification TEXT")
        if columns and "ai_content_hash" not in columns:
            conn.execute("ALTER TABLE notes ADD COLUMN ai_content_hash TEXT")
        if columns and "agent_session_id" not in columns:
            conn.execute("ALTER TABLE notes ADD COLUMN agent_session_id TEXT")
        conn.commit()
    except Exception as e:
        logging.getLogger(__name__).warning(f"notes migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_add_study_review_columns():
    """Add durable spaced-review evidence to existing Study Mode installs.

    ``create_all`` covers fresh databases but never alters an existing SQLite
    table.  Add each column independently so the migration is safe to retry
    after an interrupted startup or on an already-upgraded database.
    """

    if not DATABASE_URL.startswith("sqlite:///"):
        return
    db_path = DATABASE_URL.replace("sqlite:///", "", 1)
    if db_path == ":memory:" or not os.path.exists(db_path):
        return

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(study_states)").fetchall()
        }
        if not columns:
            return
        additions = {
            "review_level": "INTEGER NOT NULL DEFAULT 0",
            "review_count": "INTEGER NOT NULL DEFAULT 0",
            "last_review_result": "TEXT",
            "last_reviewed_at": "DATETIME",
            "next_review_at": "DATETIME",
        }
        changed = False
        for column, declaration in additions.items():
            if column in columns:
                continue
            conn.execute(
                f"ALTER TABLE study_states ADD COLUMN {column} {declaration}"
            )
            changed = True
        if changed:
            conn.commit()
            logger.info("Migrated: added spaced-review columns to study_states")
    except Exception as exc:
        logger.warning("study_states review migration failed: %s", exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _migrate_add_study_setup_initialized_column():
    """Add the durable first-prompt sentinel to existing Study databases."""

    if not DATABASE_URL.startswith("sqlite:///"):
        return
    db_path = DATABASE_URL.replace("sqlite:///", "", 1)
    if db_path == ":memory:" or not os.path.exists(db_path):
        return

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(study_states)").fetchall()
        }
        if columns and "setup_initialized" not in columns:
            conn.execute(
                "ALTER TABLE study_states ADD COLUMN "
                "setup_initialized BOOLEAN NOT NULL DEFAULT 0"
            )
            conn.commit()
            logger.info("Migrated: added setup_initialized to study_states")
    except Exception as exc:
        logger.warning("study_states setup migration failed: %s", exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _migrate_add_study_last_prompt_at_column():
    """Add the persisted Study prompt-activity clock to existing databases."""

    if not DATABASE_URL.startswith("sqlite:///"):
        return
    db_path = DATABASE_URL.replace("sqlite:///", "", 1)
    if db_path == ":memory:" or not os.path.exists(db_path):
        return

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(study_states)").fetchall()
        }
        if columns and "last_prompt_at" not in columns:
            conn.execute(
                "ALTER TABLE study_states ADD COLUMN last_prompt_at DATETIME"
            )
            conn.commit()
            logger.info("Migrated: added last_prompt_at to study_states")
    except Exception as exc:
        logger.warning("study_states prompt activity migration failed: %s", exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _migrate_add_mode_column():
    """Add mode column to sessions table if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = [row[1] for row in cursor.fetchall()]
        if "mode" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN mode TEXT")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'mode' column to sessions")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Migration check for mode failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _migrate_add_folder_column():
    """Add folder column to sessions table if it doesn't exist."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = [row[1] for row in cursor.fetchall()]
        if "folder" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN folder TEXT")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'folder' column to sessions")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Migration check for folder failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _migrate_add_token_columns():
    """Add cumulative token tracking columns to sessions table."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(sessions)")
        columns = [row[1] for row in cursor.fetchall()]
        if "total_input_tokens" not in columns:
            conn.execute("ALTER TABLE sessions ADD COLUMN total_input_tokens INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE sessions ADD COLUMN total_output_tokens INTEGER DEFAULT 0")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added token tracking columns to sessions")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Migration check for token columns failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _migrate_add_owner_to_table(table_name: str, index_name: str):
    """Generic helper: add owner TEXT column + index to a table if missing."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute(f"PRAGMA table_info({table_name})")
        columns = [row[1] for row in cursor.fetchall()]
        if "owner" not in columns:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN owner TEXT")
            conn.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name}(owner)")
            conn.commit()
            logging.getLogger(__name__).info(f"Migrated: added 'owner' column to {table_name}")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Migration owner column for {table_name} failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _migrate_add_multiuser_owner_columns():
    """Add owner column to memories, gallery_images, user_tools, comparisons."""
    _migrate_add_owner_to_table("memories", "ix_memories_owner")
    _migrate_add_owner_to_table("gallery_images", "ix_gallery_images_owner")
    _migrate_add_owner_to_table("user_tools", "ix_user_tools_owner")
    _migrate_add_owner_to_table("comparisons", "ix_comparisons_owner")
    _migrate_add_owner_to_table("api_tokens", "ix_api_tokens_owner")
    # documents derived ownership from their session join until this column
    # existed; the legacy-owner sweep (below) backfills it on the next boot.
    _migrate_add_owner_to_table("documents", "ix_documents_owner")


def _migrate_add_gallery_caption_column():
    """Add OCR/vision caption storage for gallery images."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(gallery_images)").fetchall()]
        if columns and "caption" not in columns:
            conn.execute("ALTER TABLE gallery_images ADD COLUMN caption TEXT DEFAULT ''")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added caption column to gallery_images")
    except Exception as e:
        logging.getLogger(__name__).warning(f"Migration gallery caption column failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_add_api_token_scopes_column():
    """Add API token scopes for existing installs.

    Existing tokens get the current only-supported scope (`chat`) so they keep
    working after the schema migration, but route checks no longer treat tokens
    as an unscoped bearer credential.
    """
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(api_tokens)").fetchall()]
        if columns and "scopes" not in columns:
            conn.execute("ALTER TABLE api_tokens ADD COLUMN scopes TEXT NOT NULL DEFAULT 'chat'")
            conn.execute("UPDATE api_tokens SET scopes = 'chat' WHERE scopes IS NULL OR scopes = ''")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added scopes column to api_tokens")
    except Exception as e:
        logging.getLogger(__name__).warning(f"api_tokens.scopes migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_auth_identities_for_unified_auth(bind):
    """Upgrade the SQLite identity namespace without losing FK references.

    SQLite implements a table-level ``UNIQUE`` constraint as an auto-index,
    which cannot be dropped.  Legacy databases therefore need a table rebuild
    to remove ``UNIQUE(provider, subject)``; merely adding the new three-column
    index would leave the old, over-broad constraint active.

    The rebuild is one explicit SQLite transaction. Foreign-key enforcement is
    disabled *before* that transaction so dropping the old parent table cannot
    fire ``ON DELETE`` actions in referencing tables. The original pragma is
    restored afterwards, and any new violation aborts the transaction. IDs,
    account links, timestamps, issuer values, user-created indexes, and
    triggers are copied or recreated before commit.
    """

    raw_connection = bind.raw_connection()
    cursor = None
    original_foreign_keys = None
    rebuilt_identity_table = False
    migration_table = "auth_identities__restia_migration"
    canonical_columns = (
        "id",
        "account_id",
        "provider",
        "issuer",
        "subject",
        "state",
        "linked_at",
        "last_verified_at",
        "created_at",
        "updated_at",
    )
    required_legacy_columns = {
        "id",
        "account_id",
        "provider",
        "subject",
        "created_at",
        "updated_at",
    }
    canonical_indexes = {
        "ix_auth_identities_account_id": (False, ("account_id",)),
        "ix_auth_identity_account_provider": (
            False,
            ("account_id", "provider"),
        ),
        "ix_auth_identity_issuer_subject": (False, ("issuer", "subject")),
    }
    desired_unique_columns = ("provider", "issuer", "subject")

    def _quoted_identifier(value):
        return '"' + str(value).replace('"', '""') + '"'

    def _index_details():
        details = []
        for row in cursor.execute(
            'PRAGMA index_list("auth_identities")'
        ).fetchall():
            name = str(row[1])
            columns = tuple(
                str(info[2]) if info[2] is not None else ""
                for info in cursor.execute(
                    f"PRAGMA index_info({_quoted_identifier(name)})"
                ).fetchall()
            )
            sql_row = cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (name,),
            ).fetchone()
            details.append({
                "name": name,
                "unique": bool(row[2]),
                "partial": bool(row[4]) if len(row) > 4 else False,
                "columns": columns,
                "sql": sql_row[0] if sql_row else None,
            })
        return details

    try:
        # A PRAGMA foreign_keys change is ignored while a transaction is open.
        raw_connection.rollback()
        cursor = raw_connection.cursor()
        original_foreign_keys = int(
            cursor.execute("PRAGMA foreign_keys").fetchone()[0]
        )
        tables = {
            str(row[0])
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "auth_identities" not in tables:
            return
        if "accounts" not in tables:
            raise RuntimeError(
                "Cannot migrate auth_identities without the accounts table"
            )
        if migration_table in tables:
            raise RuntimeError(
                f"Refusing to overwrite stale migration table {migration_table}"
            )

        baseline_foreign_key_violations = {
            tuple(row) for row in cursor.execute("PRAGMA foreign_key_check")
        }
        cursor.execute("PRAGMA foreign_keys=OFF")
        if int(cursor.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
            raise RuntimeError(
                "Could not temporarily disable SQLite foreign-key actions"
            )
        cursor.execute("BEGIN IMMEDIATE")

        columns = {
            str(row[1])
            for row in cursor.execute(
                'PRAGMA table_info("auth_identities")'
            ).fetchall()
        }
        missing_required = required_legacy_columns - columns
        if missing_required:
            raise RuntimeError(
                "Cannot migrate auth_identities; missing columns: "
                + ", ".join(sorted(missing_required))
            )
        unexpected_columns = columns - set(canonical_columns)
        if unexpected_columns:
            raise RuntimeError(
                "Cannot safely rebuild auth_identities with unknown columns: "
                + ", ".join(sorted(unexpected_columns))
            )

        legacy_identity_shape = "issuer" not in columns
        if legacy_identity_shape:
            cursor.execute(
                "ALTER TABLE auth_identities ADD COLUMN issuer VARCHAR(500) "
                "NOT NULL DEFAULT 'restia-local'"
            )
        if "state" not in columns:
            cursor.execute(
                "ALTER TABLE auth_identities ADD COLUMN state VARCHAR(24) "
                "NOT NULL DEFAULT 'active'"
            )
        if "linked_at" not in columns:
            cursor.execute(
                "ALTER TABLE auth_identities ADD COLUMN linked_at DATETIME"
            )
        if "last_verified_at" not in columns:
            cursor.execute(
                "ALTER TABLE auth_identities ADD COLUMN last_verified_at DATETIME"
            )
        cursor.execute(
            "UPDATE auth_identities SET linked_at = "
            "COALESCE(linked_at, created_at, CURRENT_TIMESTAMP)"
        )
        if legacy_identity_shape:
            # Old non-local rows have no trustworthy external issuer. Keep the
            # provider namespace explicit without linking them to a new IdP.
            cursor.execute(
                "UPDATE auth_identities SET issuer = "
                "'legacy:' || lower(provider) "
                "WHERE lower(provider) <> 'local'"
            )

        indexes = _index_details()
        legacy_unique_indexes = [
            item for item in indexes
            if item["unique"]
            and len(item["columns"]) == 2
            and set(item["columns"]) == {"provider", "subject"}
        ]
        desired_unique_exists = any(
            item["unique"]
            and not item["partial"]
            and len(item["columns"]) == 3
            and set(item["columns"]) == set(desired_unique_columns)
            for item in indexes
        )

        for item in indexes:
            expected = canonical_indexes.get(item["name"])
            if expected and (
                item["unique"] != expected[0]
                or item["columns"] != expected[1]
            ):
                raise RuntimeError(
                    f"Index {item['name']} has an unexpected definition"
                )

        if legacy_unique_indexes:
            preserved_index_sql = []
            for item in indexes:
                is_legacy_unique = item in legacy_unique_indexes
                is_desired_unique = (
                    item["unique"]
                    and len(item["columns"]) == 3
                    and set(item["columns"]) == set(desired_unique_columns)
                )
                if (
                    is_legacy_unique
                    or is_desired_unique
                    or item["name"] in canonical_indexes
                ):
                    continue
                if item["sql"]:
                    preserved_index_sql.append(str(item["sql"]))

            preserved_trigger_sql = [
                str(row[0])
                for row in cursor.execute(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='trigger' AND tbl_name='auth_identities' "
                    "AND sql IS NOT NULL ORDER BY name"
                ).fetchall()
            ]
            old_row_count = int(
                cursor.execute(
                    "SELECT COUNT(*) FROM auth_identities"
                ).fetchone()[0]
            )

            cursor.execute(f"""
                CREATE TABLE {_quoted_identifier(migration_table)} (
                    id VARCHAR(36) NOT NULL,
                    account_id VARCHAR(36) NOT NULL,
                    provider VARCHAR(32) NOT NULL,
                    issuer VARCHAR(500) NOT NULL,
                    subject VARCHAR(255) NOT NULL,
                    state VARCHAR(24) NOT NULL,
                    linked_at DATETIME NOT NULL,
                    last_verified_at DATETIME,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    CONSTRAINT uq_auth_identity_provider_issuer_subject
                        UNIQUE (provider, issuer, subject),
                    CONSTRAINT ck_auth_identities_state
                        CHECK (state IN ('active', 'disabled', 'unlinked')),
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                        ON DELETE CASCADE
                )
            """)
            column_list = ", ".join(
                _quoted_identifier(column) for column in canonical_columns
            )
            cursor.execute(
                f"INSERT INTO {_quoted_identifier(migration_table)} "
                f"({column_list}) SELECT {column_list} FROM auth_identities"
            )
            new_row_count = int(
                cursor.execute(
                    f"SELECT COUNT(*) FROM {_quoted_identifier(migration_table)}"
                ).fetchone()[0]
            )
            if new_row_count != old_row_count:
                raise RuntimeError(
                    "auth_identities rebuild did not preserve every row"
                )

            cursor.execute("DROP TABLE auth_identities")
            cursor.execute(
                f"ALTER TABLE {_quoted_identifier(migration_table)} "
                "RENAME TO auth_identities"
            )
            for name, (_, index_columns) in canonical_indexes.items():
                index_column_list = ", ".join(
                    _quoted_identifier(column) for column in index_columns
                )
                cursor.execute(
                    f"CREATE INDEX {_quoted_identifier(name)} "
                    f"ON auth_identities ({index_column_list})"
                )
            for statement in preserved_index_sql:
                cursor.execute(statement)
            for statement in preserved_trigger_sql:
                cursor.execute(statement)
            rebuilt_identity_table = True
        else:
            if not desired_unique_exists:
                cursor.execute(
                    "CREATE UNIQUE INDEX "
                    "uq_auth_identity_provider_issuer_subject "
                    "ON auth_identities(provider, issuer, subject)"
                )
            for name, (_, index_columns) in canonical_indexes.items():
                index_column_list = ", ".join(
                    _quoted_identifier(column) for column in index_columns
                )
                cursor.execute(
                    f"CREATE INDEX IF NOT EXISTS {_quoted_identifier(name)} "
                    f"ON auth_identities ({index_column_list})"
                )

        final_foreign_key_violations = {
            tuple(row) for row in cursor.execute("PRAGMA foreign_key_check")
        }
        new_foreign_key_violations = (
            final_foreign_key_violations - baseline_foreign_key_violations
        )
        if new_foreign_key_violations:
            raise RuntimeError(
                "auth_identities migration introduced foreign-key violations: "
                f"{sorted(new_foreign_key_violations, key=repr)!r}"
            )
        raw_connection.commit()
        if rebuilt_identity_table:
            logger.info(
                "Migrated auth_identities to issuer-qualified uniqueness"
            )
    except Exception:
        raw_connection.rollback()
        raise
    finally:
        try:
            # Also closes any implicit transaction left by an early return.
            raw_connection.rollback()
            if cursor is not None and original_foreign_keys is not None:
                desired_setting = "ON" if original_foreign_keys else "OFF"
                cursor.execute(f"PRAGMA foreign_keys={desired_setting}")
                restored_setting = int(
                    cursor.execute("PRAGMA foreign_keys").fetchone()[0]
                )
                if restored_setting != original_foreign_keys:
                    raise RuntimeError(
                        "Failed to restore SQLite foreign-key enforcement"
                    )
        finally:
            if cursor is not None:
                cursor.close()
            raw_connection.close()


def _migrate_unified_auth_security_constraints(bind):
    """Rebuild legacy auth tables whose SQLite constraints cannot be altered.

    The pushed 0001 foundation already had ``accounts`` and ``api_tokens``.
    Adding columns in place is therefore not enough: a legacy database would
    otherwise miss the account state/epoch checks and the API-token ownership
    cascade that fresh Alembic 0002 databases enforce. This repair preserves
    rows plus non-canonical user indexes/triggers and fails closed on unknown
    columns or newly introduced foreign-key violations.
    """

    if bind.dialect.name != "sqlite":
        return

    raw_connection = bind.raw_connection()
    cursor = None
    original_foreign_keys = None
    account_table = "accounts__restia_security_migration"
    token_table = "api_tokens__restia_security_migration"
    account_columns = (
        "id", "username", "display_name", "status", "auth_epoch",
        "last_login_at", "created_at", "updated_at",
    )
    token_columns = (
        "id", "owner", "account_id", "name", "token_hash", "token_prefix",
        "digest_scheme", "scopes", "is_active", "last_used_at", "revoked_at",
        "expires_at", "created_at", "updated_at",
    )

    def quoted(value):
        return '"' + str(value).replace('"', '""') + '"'

    def table_columns(table_name):
        return tuple(
            str(row[1])
            for row in cursor.execute(
                f"PRAGMA table_info({quoted(table_name)})"
            ).fetchall()
        )

    def preserved_objects(table_name, canonical_indexes):
        statements = []
        for object_type, name, sql in cursor.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE tbl_name=? AND type IN ('index', 'trigger') "
            "AND sql IS NOT NULL ORDER BY type, name",
            (table_name,),
        ).fetchall():
            if object_type == "index" and str(name) in canonical_indexes:
                continue
            statements.append(str(sql))
        return statements

    def copy_rows(source, destination, columns):
        column_sql = ", ".join(quoted(column) for column in columns)
        old_count = int(
            cursor.execute(f"SELECT COUNT(*) FROM {quoted(source)}").fetchone()[0]
        )
        cursor.execute(
            f"INSERT INTO {quoted(destination)} ({column_sql}) "
            f"SELECT {column_sql} FROM {quoted(source)}"
        )
        new_count = int(
            cursor.execute(
                f"SELECT COUNT(*) FROM {quoted(destination)}"
            ).fetchone()[0]
        )
        if old_count != new_count:
            raise RuntimeError(f"{source} security rebuild lost rows")

    try:
        raw_connection.rollback()
        cursor = raw_connection.cursor()
        original_foreign_keys = int(
            cursor.execute("PRAGMA foreign_keys").fetchone()[0]
        )
        tables = {
            str(row[0])
            for row in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if not {"accounts", "api_tokens"}.issubset(tables):
            raise RuntimeError(
                "Cannot repair unified-auth constraints without accounts/api_tokens"
            )
        stale_tables = {account_table, token_table} & tables
        if stale_tables:
            raise RuntimeError(
                "Refusing to overwrite stale auth security migration tables: "
                + ", ".join(sorted(stale_tables))
            )

        account_sql_row = cursor.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'"
        ).fetchone()
        normalized_account_sql = " ".join(
            str(account_sql_row[0] if account_sql_row else "").lower().split()
        )
        rebuild_accounts = not (
            "ck_accounts_status" in normalized_account_sql
            and "ck_accounts_auth_epoch" in normalized_account_sql
        )
        token_foreign_keys = cursor.execute(
            'PRAGMA foreign_key_list("api_tokens")'
        ).fetchall()
        rebuild_tokens = not any(
            str(row[2]) == "accounts"
            and str(row[3]) == "account_id"
            and str(row[4]) == "id"
            and str(row[6]).upper() == "CASCADE"
            for row in token_foreign_keys
        )
        if not rebuild_accounts and not rebuild_tokens:
            return

        if set(table_columns("accounts")) != set(account_columns):
            raise RuntimeError(
                "Cannot safely rebuild accounts with unknown/missing columns"
            )
        if set(table_columns("api_tokens")) != set(token_columns):
            raise RuntimeError(
                "Cannot safely rebuild api_tokens with unknown/missing columns"
            )

        baseline_foreign_key_violations = {
            tuple(row) for row in cursor.execute("PRAGMA foreign_key_check")
        }
        cursor.execute("PRAGMA foreign_keys=OFF")
        if int(cursor.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
            raise RuntimeError(
                "Could not temporarily disable SQLite foreign-key actions"
            )
        cursor.execute("BEGIN IMMEDIATE")

        if rebuild_accounts:
            preserved = preserved_objects(
                "accounts", {"ix_accounts_username", "ix_accounts_status"}
            )
            cursor.execute(f"""
                CREATE TABLE {quoted(account_table)} (
                    id VARCHAR(36) NOT NULL,
                    username VARCHAR(160) NOT NULL,
                    display_name VARCHAR(160),
                    status VARCHAR(24) NOT NULL,
                    auth_epoch INTEGER NOT NULL,
                    last_login_at DATETIME,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    CONSTRAINT ck_accounts_status CHECK (
                        status IN ('active', 'renaming', 'disabled',
                                   'deletion_pending', 'deleted')
                    ),
                    CONSTRAINT ck_accounts_auth_epoch CHECK (auth_epoch >= 1)
                )
            """)
            copy_rows("accounts", account_table, account_columns)
            cursor.execute('DROP TABLE "accounts"')
            cursor.execute(
                f"ALTER TABLE {quoted(account_table)} RENAME TO accounts"
            )
            cursor.execute(
                "CREATE UNIQUE INDEX ix_accounts_username ON accounts(username)"
            )
            cursor.execute(
                "CREATE INDEX ix_accounts_status ON accounts(status)"
            )
            for statement in preserved:
                cursor.execute(statement)

        if rebuild_tokens:
            preserved = preserved_objects(
                "api_tokens",
                {"ix_api_tokens_id", "ix_api_tokens_owner", "ix_api_tokens_account_id"},
            )
            cursor.execute(f"""
                CREATE TABLE {quoted(token_table)} (
                    id VARCHAR NOT NULL,
                    owner VARCHAR,
                    account_id VARCHAR(36),
                    name VARCHAR NOT NULL,
                    token_hash VARCHAR NOT NULL,
                    token_prefix VARCHAR NOT NULL,
                    digest_scheme VARCHAR(32) NOT NULL,
                    scopes VARCHAR NOT NULL,
                    is_active BOOLEAN,
                    last_used_at DATETIME,
                    revoked_at DATETIME,
                    expires_at DATETIME,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    PRIMARY KEY (id),
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                        ON DELETE CASCADE
                )
            """)
            copy_rows("api_tokens", token_table, token_columns)
            cursor.execute('DROP TABLE "api_tokens"')
            cursor.execute(
                f"ALTER TABLE {quoted(token_table)} RENAME TO api_tokens"
            )
            cursor.execute("CREATE INDEX ix_api_tokens_id ON api_tokens(id)")
            cursor.execute("CREATE INDEX ix_api_tokens_owner ON api_tokens(owner)")
            cursor.execute(
                "CREATE INDEX ix_api_tokens_account_id ON api_tokens(account_id)"
            )
            for statement in preserved:
                cursor.execute(statement)

        final_foreign_key_violations = {
            tuple(row) for row in cursor.execute("PRAGMA foreign_key_check")
        }
        new_foreign_key_violations = (
            final_foreign_key_violations - baseline_foreign_key_violations
        )
        if new_foreign_key_violations:
            raise RuntimeError(
                "Unified-auth security repair introduced foreign-key violations: "
                f"{sorted(new_foreign_key_violations, key=repr)!r}"
            )
        raw_connection.commit()
    except Exception:
        raw_connection.rollback()
        raise
    finally:
        try:
            raw_connection.rollback()
            if cursor is not None and original_foreign_keys is not None:
                desired_setting = "ON" if original_foreign_keys else "OFF"
                cursor.execute(f"PRAGMA foreign_keys={desired_setting}")
                if int(cursor.execute("PRAGMA foreign_keys").fetchone()[0]) != original_foreign_keys:
                    raise RuntimeError(
                        "Failed to restore SQLite foreign-key enforcement"
                    )
        finally:
            if cursor is not None:
                cursor.close()
            raw_connection.close()


def _migrate_add_unified_auth_columns(target_engine=None):
    """Upgrade legacy SQLite rows for database-backed unified authentication.

    ``Base.metadata.create_all`` creates the complete schema for a fresh local
    install but does not alter existing tables. Until Alembic becomes the
    authoritative schema path, this bridge adds safe columns and transactionally
    rebuilds the legacy identity table when its old two-column uniqueness would
    collapse subjects from different issuers.

    Unlike older best-effort helpers, failures propagate. Starting with only a
    subset of the security schema would be less safe than refusing startup.
    """

    bind = target_engine or engine
    if bind.dialect.name != "sqlite":
        return

    with bind.begin() as conn:
        tables = {
            str(row[0])
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        def column_names(table_name: str) -> set[str]:
            return {
                str(row[1])
                for row in conn.exec_driver_sql(
                    f'PRAGMA table_info("{table_name}")'
                ).fetchall()
            }

        if "accounts" in tables:
            columns = column_names("accounts")
            if "status" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE accounts ADD COLUMN status VARCHAR(24) "
                    "NOT NULL DEFAULT 'active'"
                )
            if "auth_epoch" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE accounts ADD COLUMN auth_epoch INTEGER "
                    "NOT NULL DEFAULT 1"
                )
            if "last_login_at" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE accounts ADD COLUMN last_login_at DATETIME"
                )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_accounts_status ON accounts(status)"
            )

        if "api_tokens" in tables:
            columns = column_names("api_tokens")
            if "account_id" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE api_tokens ADD COLUMN account_id VARCHAR(36)"
                )
            if "digest_scheme" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE api_tokens ADD COLUMN digest_scheme VARCHAR(32) "
                    "NOT NULL DEFAULT 'bcrypt_legacy'"
                )
            if "revoked_at" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE api_tokens ADD COLUMN revoked_at DATETIME"
                )
            if "expires_at" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE api_tokens ADD COLUMN expires_at DATETIME"
                )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_api_tokens_account_id "
                "ON api_tokens(account_id)"
            )

        if "auth_policy" in tables:
            columns = column_names("auth_policy")
            if "bootstrap_completed" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE auth_policy ADD COLUMN bootstrap_completed "
                    "BOOLEAN NOT NULL DEFAULT 0"
                )

    _migrate_auth_identities_for_unified_auth(bind)
    _migrate_unified_auth_security_constraints(bind)


def _migrate_add_project_completion_column():
    """Add the reversible project completion marker to existing installs."""
    import sqlite3

    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()]
        if columns and "completed_at" not in columns:
            conn.execute("ALTER TABLE projects ADD COLUMN completed_at DATETIME")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_projects_completed_at ON projects (completed_at)"
            )
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added completed_at column to projects")
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        logging.getLogger(__name__).exception(
            "Required projects.completed_at migration failed"
        )
        raise RuntimeError(
            "Required projects.completed_at migration failed; database schema is unusable"
        ) from e
    finally:
        try:
            conn.close()
        except Exception:
            pass

def _migrate_assign_legacy_owner(admin_user: str | None = None):
    """Assign all null-owner data to the first (admin) user.

    ``admin_user`` must come from the unified database auth authority. The
    caller runs this after legacy credential import and periodically thereafter
    so data created while auth is disabled/local-bypassed does not remain
    world-visible. No credential file is consulted here.
    """
    import sqlite3
    admin_user = str(admin_user or "").strip().lower()
    if not admin_user:
        return

    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return

    logger = logging.getLogger(__name__)
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        # Every table with an `owner` column. New tables added later will be
        # picked up automatically because we only UPDATE when the column
        # exists; the explicit list documents intent.
        tables = [
            "sessions", "memories", "gallery_images", "user_tools",
            "comparisons", "documents", "signatures", "notes",
            "calendars", "calendar_events", "integrations",
            "scheduled_tasks", "task_runs", "crew_members",
            "gallery_albums", "gallery_people", "user_tool_data",
            "api_tokens", "webhooks",
        ]
        for table in tables:
            try:
                cursor = conn.execute(f"PRAGMA table_info({table})")
                columns = [row[1] for row in cursor.fetchall()]
                if "owner" in columns:
                    res = conn.execute(f"UPDATE {table} SET owner = ? WHERE owner IS NULL", (admin_user,))
                    if res.rowcount > 0:
                        logger.info(f"Assigned {res.rowcount} legacy rows in {table} to '{admin_user}'")
            except Exception as e:
                logger.warning(f"Legacy owner assignment for {table} failed: {e}")

        # Study Mode has one state row per owner but legacy local installs use
        # the ownerless ``local:default`` row. Never blanket-assign it when an
        # authenticated admin row already exists: ``owner`` is not a unique DB
        # column, and creating two admin rows would make goal/timer lookup
        # ambiguous. If no admin row exists, claim exactly one deterministic
        # ownerless row and leave any malformed extras quarantined.
        try:
            columns = [
                row[1]
                for row in conn.execute("PRAGMA table_info(study_states)").fetchall()
            ]
            if "owner" in columns:
                existing = conn.execute(
                    "SELECT id FROM study_states WHERE owner = ? ORDER BY id LIMIT 1",
                    (admin_user,),
                ).fetchone()
                if existing is None:
                    candidate = conn.execute(
                        """
                        SELECT id FROM study_states
                        WHERE owner IS NULL
                        ORDER BY CASE WHEN id = 'local:default' THEN 0 ELSE 1 END, id
                        LIMIT 1
                        """
                    ).fetchone()
                    if candidate is not None:
                        conn.execute(
                            "UPDATE study_states SET owner = ? WHERE id = ? AND owner IS NULL",
                            (admin_user, candidate[0]),
                        )
                        logger.info(
                            "Assigned legacy Study Mode state '%s' to '%s'",
                            candidate[0],
                            admin_user,
                        )
        except Exception as e:
            logger.warning(f"Legacy owner assignment for study_states failed: {e}")
        conn.commit()
    except Exception as e:
        logger.warning(f"Legacy owner migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Also migrate memory.json
    mem_path = MEMORY_FILE
    try:
        if os.path.exists(mem_path):
            with open(mem_path, "r", encoding="utf-8") as f:
                memories = _json.load(f)
            changed = False
            for m in memories:
                if not m.get("owner"):
                    m["owner"] = admin_user
                    changed = True
            if changed:
                with open(mem_path, "w", encoding="utf-8") as f:
                    _json.dump(memories, f, ensure_ascii=False, indent=2)
                logger.info(f"Assigned {sum(1 for _ in memories)} legacy memories in memory.json to '{admin_user}'")
    except Exception as e:
        logger.warning(f"memory.json legacy migration failed: {e}")

    # Also migrate user_prefs.json to per-user format
    prefs_path = USER_PREFS_FILE
    try:
        if os.path.exists(prefs_path):
            with open(prefs_path, "r", encoding="utf-8") as f:
                prefs = _json.load(f)
            if "_users" not in prefs and prefs:
                # Flat format → nest under admin user
                new_prefs = {"_users": {admin_user: prefs}}
                with open(prefs_path, "w", encoding="utf-8") as f:
                    _json.dump(new_prefs, f, indent=2)
                logger.info(f"Migrated user_prefs.json to per-user format under '{admin_user}'")
    except Exception as e:
        logger.warning(f"user_prefs.json migration failed: {e}")


def _migrate_backfill_document_owner_from_session():
    """Backfill documents.owner from the owner of the linked chat session.

    Must run AFTER the owner column is added and BEFORE the blanket
    legacy-owner sweep, so session-linked docs get their *true* owner
    while only genuinely orphaned (sessionless) docs fall through to the
    admin assignment. Idempotent — only touches NULL-owner rows."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(documents)"))]
            if "owner" not in cols:
                return
            res = conn.execute(text(
                "UPDATE documents SET owner = ("
                "  SELECT s.owner FROM sessions s WHERE s.id = documents.session_id"
                ") WHERE owner IS NULL AND session_id IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM sessions s WHERE s.id = documents.session_id "
                "            AND s.owner IS NOT NULL)"
            ))
            conn.commit()
            if res.rowcount:
                logging.getLogger(__name__).info(
                    f"Backfilled owner on {res.rowcount} session-linked documents")
    except Exception as e:
        logging.getLogger(__name__).warning(f"document owner backfill: {e}")


def _migrate_add_tidy_verdict():
    """Add tidy_verdict column to documents table if missing."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(documents)"))]
            if "tidy_verdict" not in cols:
                conn.execute(text("ALTER TABLE documents ADD COLUMN tidy_verdict VARCHAR"))
                conn.commit()
                logging.getLogger(__name__).info("Added tidy_verdict column to documents")
    except Exception as e:
        logging.getLogger(__name__).warning(f"tidy_verdict migration: {e}")


def _migrate_add_doc_source_email_cols():
    """Add source-email provenance columns to documents (for the Sign-and-Reply flow)."""
    cols_to_add = {
        "source_email_uid":        "VARCHAR",
        "source_email_folder":     "VARCHAR",
        "source_email_account_id": "VARCHAR",
        "source_email_message_id": "VARCHAR",
    }
    try:
        with engine.connect() as conn:
            existing = {r[1] for r in conn.execute(text("PRAGMA table_info(documents)"))}
            for col, spec in cols_to_add.items():
                if col not in existing:
                    conn.execute(text(f"ALTER TABLE documents ADD COLUMN {col} {spec}"))
                    logging.getLogger(__name__).info(f"Added {col} column to documents")
            # Index for lookup-by-message-id (the "find existing draft" path)
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_documents_source_email_message_id "
                "ON documents (source_email_message_id)"
            ))
            conn.commit()
    except Exception as e:
        logging.getLogger(__name__).warning(f"doc source-email migration: {e}")

def _migrate_add_task_automation_columns():
    """Add automation columns to scheduled_tasks table if missing."""
    new_cols = {
        "task_type": "VARCHAR DEFAULT 'llm'",
        "action": "VARCHAR",
        "trigger_type": "VARCHAR DEFAULT 'schedule'",
        "trigger_event": "VARCHAR",
        "trigger_count": "INTEGER",
        "trigger_counter": "INTEGER DEFAULT 0",
    }
    try:
        with engine.connect() as conn:
            cols_info = list(conn.execute(text("PRAGMA table_info(scheduled_tasks)")))
            col_names = [r[1] for r in cols_info]
            for col_name, col_def in new_cols.items():
                if col_name not in col_names:
                    conn.execute(text(f"ALTER TABLE scheduled_tasks ADD COLUMN {col_name} {col_def}"))

            # Check if prompt/schedule/scheduled_time are still NOT NULL — need table rebuild
            notnull_map = {r[1]: r[3] for r in cols_info}
            needs_rebuild = (
                notnull_map.get("prompt", 0) == 1 or
                notnull_map.get("schedule", 0) == 1 or
                notnull_map.get("scheduled_time", 0) == 1
            )
            if needs_rebuild:
                logging.getLogger(__name__).info("Rebuilding scheduled_tasks to make prompt/schedule/scheduled_time nullable")
                conn.execute(text("ALTER TABLE scheduled_tasks RENAME TO _old_scheduled_tasks"))
                conn.execute(text("""
                    CREATE TABLE scheduled_tasks (
                        id VARCHAR PRIMARY KEY,
                        owner VARCHAR,
                        name VARCHAR NOT NULL,
                        prompt TEXT,
                        schedule VARCHAR,
                        scheduled_time VARCHAR,
                        scheduled_day INTEGER,
                        scheduled_date DATETIME,
                        next_run DATETIME,
                        last_run DATETIME,
                        status VARCHAR,
                        output_target VARCHAR,
                        session_id VARCHAR,
                        model VARCHAR,
                        endpoint_url VARCHAR,
                        run_count INTEGER,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        task_type VARCHAR DEFAULT 'llm',
                        action VARCHAR,
                        trigger_type VARCHAR DEFAULT 'schedule',
                        trigger_event VARCHAR,
                        trigger_count INTEGER,
                        trigger_counter INTEGER DEFAULT 0
                    )
                """))
                conn.execute(text("""
                    INSERT INTO scheduled_tasks
                    SELECT id, owner, name, prompt, schedule, scheduled_time,
                           scheduled_day, scheduled_date, next_run, last_run,
                           status, output_target, session_id, model, endpoint_url,
                           run_count, created_at, updated_at,
                           task_type, action, trigger_type, trigger_event,
                           trigger_count, trigger_counter
                    FROM _old_scheduled_tasks
                """))
                conn.execute(text("DROP TABLE _old_scheduled_tasks"))

            conn.commit()
            logging.getLogger(__name__).info("Task automation columns migration complete")
    except Exception as e:
        logging.getLogger(__name__).warning(f"task automation migration: {e}")

def _migrate_add_email_oauth_columns():
    """Add Google OAuth and display_name columns to email_accounts if missing."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(email_accounts)"))]
            for col, typedef in [
                ("oauth_provider",      "TEXT"),
                ("oauth_access_token",  "TEXT"),
                ("oauth_refresh_token", "TEXT"),
                ("oauth_token_expiry",  "TEXT"),
                ("display_name",        "TEXT"),
            ]:
                if col not in cols:
                    conn.execute(text(f"ALTER TABLE email_accounts ADD COLUMN {col} {typedef}"))
            conn.commit()
    except Exception as e:
        logging.getLogger(__name__).warning(f"email oauth columns migration: {e}")


def _migrate_add_oauth_config():
    """Add oauth_config column to mcp_servers table if missing."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(mcp_servers)"))]
            if "oauth_config" not in cols:
                conn.execute(text("ALTER TABLE mcp_servers ADD COLUMN oauth_config TEXT"))
                conn.commit()
                logging.getLogger(__name__).info("Added oauth_config column to mcp_servers")
    except Exception as e:
        logging.getLogger(__name__).warning(f"oauth_config migration: {e}")

def _migrate_add_disabled_tools():
    """Add disabled_tools column to mcp_servers table if missing."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(mcp_servers)"))]
            if "disabled_tools" not in cols:
                conn.execute(text("ALTER TABLE mcp_servers ADD COLUMN disabled_tools TEXT"))
                conn.commit()
                logging.getLogger(__name__).info("Added disabled_tools column to mcp_servers")
    except Exception as e:
        logging.getLogger(__name__).warning(f"disabled_tools migration: {e}")

def _migrate_add_mcp_oauth_tokens_column():
    """Add oauth_tokens column to mcp_servers table if missing.

    The model declares this column as EncryptedText, but the SQL type is plain
    TEXT on purpose: EncryptedText is a SQLAlchemy TypeDecorator that encrypts at
    the Python layer and stores the ciphertext as TEXT, so the DB column type is
    TEXT. This matches the existing encrypted columns (see _migrate_encrypt_*)."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(mcp_servers)"))]
            if "oauth_tokens" not in cols:
                conn.execute(text("ALTER TABLE mcp_servers ADD COLUMN oauth_tokens TEXT"))
                conn.commit()
                logging.getLogger(__name__).info("Added oauth_tokens column to mcp_servers")
    except Exception as e:
        logging.getLogger(__name__).warning(f"oauth_tokens migration: {e}")

def _migrate_add_task_v2_columns():
    """Add cron_expression, then_task_id, webhook_token to scheduled_tasks."""
    new_cols = {
        "cron_expression": "VARCHAR",
        "then_task_id": "VARCHAR",
        "webhook_token": "VARCHAR",
    }
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(scheduled_tasks)"))]
            for col_name, col_def in new_cols.items():
                if col_name not in cols:
                    conn.execute(text(f"ALTER TABLE scheduled_tasks ADD COLUMN {col_name} {col_def}"))
            if "webhook_token" not in cols:
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_scheduled_tasks_webhook ON scheduled_tasks(webhook_token)"))
            conn.commit()
            logging.getLogger(__name__).info("Task v2 columns migration complete")
    except Exception as e:
        logging.getLogger(__name__).warning(f"task v2 migration: {e}")

def _migrate_drop_ping_notes_tasks():
    """One-time cleanup: ping_notes and ping_events used to be seeded as
    user-facing tasks. They're now pure background scanners inside the
    scheduler (no LLM, don't belong in the Tasks UI). Remove existing rows
    + their runs for both. (tidy_sessions/documents/research stay as tasks.)"""
    targets = ("ping_notes", "ping_events")
    try:
        with engine.connect() as conn:
            for action in targets:
                conn.execute(text(
                    "DELETE FROM task_runs WHERE task_id IN "
                    "(SELECT id FROM scheduled_tasks WHERE action=:a)"
                ), {"a": action})
                r = conn.execute(text("DELETE FROM scheduled_tasks WHERE action=:a"), {"a": action})
                if r.rowcount:
                    logging.getLogger(__name__).info(f"Dropped {r.rowcount} {action} task row(s)")
            conn.commit()
    except Exception as e:
        logging.getLogger(__name__).debug(f"drop_ping_notes_tasks: {e}")


def _migrate_add_notifications_enabled():
    """Per-task notification on/off toggle (default ON)."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(scheduled_tasks)"))]
            if "notifications_enabled" not in cols:
                conn.execute(text("ALTER TABLE scheduled_tasks ADD COLUMN notifications_enabled BOOLEAN DEFAULT 1"))
                conn.commit()
                logging.getLogger(__name__).info("Added notifications_enabled column to scheduled_tasks")
    except Exception as e:
        logging.getLogger(__name__).warning(f"notifications_enabled migration: {e}")


def _migrate_add_crew_member_id():
    """Add crew_member_id column to sessions and scheduled_tasks tables if missing."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(sessions)"))]
            if "crew_member_id" not in cols:
                conn.execute(text("ALTER TABLE sessions ADD COLUMN crew_member_id TEXT"))
                conn.commit()
                logging.getLogger(__name__).info("Added crew_member_id column to sessions")
            cols2 = [r[1] for r in conn.execute(text("PRAGMA table_info(scheduled_tasks)"))]
            if "crew_member_id" not in cols2:
                conn.execute(text("ALTER TABLE scheduled_tasks ADD COLUMN crew_member_id TEXT"))
                conn.commit()
                logging.getLogger(__name__).info("Added crew_member_id column to scheduled_tasks")
    except Exception as e:
        logging.getLogger(__name__).warning(f"crew_member_id migration: {e}")

def _migrate_add_assistant_columns():
    """Add is_default_assistant + timezone columns to crew_members for the personal-assistant feature."""
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(crew_members)"))]
            if "is_default_assistant" not in cols:
                conn.execute(text("ALTER TABLE crew_members ADD COLUMN is_default_assistant BOOLEAN DEFAULT 0"))
                conn.commit()
                logging.getLogger(__name__).info("Added is_default_assistant column to crew_members")
            if "timezone" not in cols:
                conn.execute(text("ALTER TABLE crew_members ADD COLUMN timezone TEXT"))
                conn.commit()
                logging.getLogger(__name__).info("Added timezone column to crew_members")
    except Exception as e:
        logging.getLogger(__name__).warning(f"assistant columns migration: {e}")





class Note(TimestampMixin, Base):
    """A Google Keep-style note or checklist."""
    __tablename__ = "notes"

    id         = Column(String, primary_key=True, index=True)
    owner      = Column(String, nullable=True, index=True)
    title      = Column(String, default="")
    content    = Column(Text, nullable=True)
    items      = Column(Text, nullable=True)       # JSON string of [{text, done}]
    note_type  = Column(String, default="note")     # "note" or "checklist"
    color      = Column(String, nullable=True)
    label      = Column(String, nullable=True)
    pinned     = Column(Boolean, default=False)
    archived   = Column(Boolean, default=False)
    due_date   = Column(String, nullable=True)
    source     = Column(String, default="user")     # "user" or "agent"
    session_id = Column(String, nullable=True)
    sort_order = Column(Integer, default=0)
    image_url  = Column(String, nullable=True)      # uploaded image URL (relative path)
    repeat     = Column(String, default="none")     # none, daily, weekly, monthly, yearly
    # Auto-AI fields — populated by /api/notes/{id}/classify. The classification
    # JSON shape is { kind, solvable, confidence, task_prompt, tools, items?: [...] }.
    # Content hash gates re-classification (avoid LLM spend on every save).
    ai_classification = Column(Text, nullable=True)
    ai_content_hash   = Column(String, nullable=True)
    # Chat session spawned by the note's "Agent" button (solve-this-todo).
    # The note shows a clickable tag that opens this session for review.
    agent_session_id  = Column(String, nullable=True)


class CalendarCal(TimestampMixin, Base):
    """A calendar (e.g. 'Personal', 'TimeTree')."""
    __tablename__ = "calendars"

    id    = Column(String, primary_key=True, index=True)
    # Stable cross-interface authority. ``owner`` remains only as the legacy
    # mutable username alias during the additive V3 cutover.
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    owner = Column(String, nullable=True, index=True)
    name  = Column(String, nullable=False)
    color = Column(String, default="#5b8abf")
    source = Column(String, default="local")  # "local" or "caldav"
    # UUID of the CalDAV account in user prefs that owns this calendar.
    # NULL for local calendars and for CalDAV calendars created before
    # multi-account support was added (treated as "use any configured account").
    account_id = Column(String, nullable=True, index=True)
    caldav_base_url = Column(String, nullable=True)
    config_version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        UniqueConstraint("id", "owner_id", name="uq_calendars_id_owner"),
        CheckConstraint(
            "config_version >= 1", name="ck_calendars_config_version",
        ),
    )

    events = relationship("CalendarEvent", back_populates="calendar", cascade="all, delete-orphan")


class CalendarEvent(TimestampMixin, Base):
    """A calendar event."""
    __tablename__ = "calendar_events"

    uid         = Column(String, primary_key=True, index=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        primary_key=True, nullable=False, index=True,
    )
    calendar_id = Column(String, nullable=False, index=True)
    summary     = Column(String, nullable=False, default="")
    description = Column(Text, default="")
    location    = Column(String, default="")
    dtstart     = Column(DateTime, nullable=False, index=True)
    dtend       = Column(DateTime, nullable=False)
    all_day     = Column(Boolean, default=False)
    # True when dtstart/dtend are stored as UTC instants (set on import paths
    # that preserve the source TZID). False = legacy naive-local. Drives the
    # `Z`-suffix on serialization so the frontend interprets correctly.
    is_utc      = Column(Boolean, default=False, nullable=False)
    rrule       = Column(String, default="")
    recurrence_exdates = Column(Text, default="")  # JSON list of skipped occurrence starts
    color       = Column(String, nullable=True)  # per-event color override
    status      = Column(String, default="confirmed")  # confirmed, cancelled
    importance  = Column(String, default="normal")    # low | normal | high | critical
    event_type  = Column(String, nullable=True)        # work | personal | health | travel | meal | social | admin | other
    last_pinged = Column(DateTime, nullable=True)      # last time the assistant pinged about this event
    # "caldav" = pulled from a CalDAV server (so the sync may prune it when it
    # vanishes upstream). NULL/local = created locally (agent, email triage, or
    # a UI event whose write-back failed) and must NOT be pruned by the sync.
    origin      = Column(String, nullable=True, index=True)
    remote_href = Column(String, nullable=True)        # CalDAV object URL for updates/deletes
    remote_etag = Column(String, nullable=True)        # Last seen CalDAV ETag, when available
    caldav_sync_pending = Column(String, nullable=True) # create | update | delete retry marker
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        ForeignKeyConstraint(
            ("calendar_id", "owner_id"),
            ("calendars.id", "calendars.owner_id"),
            ondelete="CASCADE",
            name="fk_calendar_events_calendar_owner",
        ),
        UniqueConstraint(
            "uid", "owner_id", name="uq_calendar_events_uid_owner",
        ),
        UniqueConstraint(
            "uid", "calendar_id", name="uq_calendar_events_uid_calendar",
        ),
        CheckConstraint("version >= 1", name="ck_calendar_events_version"),
    )

    calendar = relationship("CalendarCal", back_populates="events")


class CalendarActionUndo(TimestampMixin, Base):
    """Server-owned, version-fenced reversal state for one calendar proposal.

    Private event snapshots and graph-link identifiers use the content
    encryption envelope.  The indexed identifiers remain structural routing
    keys so the executor can claim a row without decrypting unrelated content.
    """

    __tablename__ = "calendar_action_undos"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    proposal_id = Column(String(36), nullable=False, index=True)
    event_uid = Column(String, nullable=False, index=True)
    operation = Column(String(16), nullable=False)
    before_state = Column(EncryptedJSON, nullable=False, default=dict)
    # These are filled after the local mutation and before the surrounding
    # authority transaction commits. They remain nullable while the undo row
    # is flushed first to establish the Level-4 reversal path.
    result_event_version = Column(Integer, nullable=True)
    life_entity_id = Column(String(36), nullable=True)
    result_graph_version = Column(Integer, nullable=True)
    created_link_ids = Column(EncryptedJSON, nullable=False, default=dict)
    state = Column(String(16), nullable=False, default="ready")
    used_at = Column(DateTime, nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        ForeignKeyConstraint(
            ("proposal_id", "owner_id"),
            ("action_proposals.id", "action_proposals.owner_id"),
            name="fk_calendar_action_undos_proposal_owner",
        ),
        UniqueConstraint(
            "id", "owner_id", name="uq_calendar_action_undos_id_owner",
        ),
        UniqueConstraint(
            "owner_id", "proposal_id",
            name="uq_calendar_action_undos_owner_proposal",
        ),
        Index(
            "ix_calendar_action_undos_owner_state_created",
            "owner_id", "state", "created_at",
        ),
        Index(
            "ix_calendar_action_undos_owner_event_created",
            "owner_id", "event_uid", "created_at",
        ),
        CheckConstraint(
            "operation IN ('create', 'update', 'reschedule')",
            name="ck_calendar_action_undos_operation",
        ),
        CheckConstraint(
            "state IN ('ready', 'used')",
            name="ck_calendar_action_undos_state",
        ),
        CheckConstraint(
            "result_event_version IS NULL OR result_event_version >= 1",
            name="ck_calendar_action_undos_event_version",
        ),
        CheckConstraint(
            "result_graph_version IS NULL OR result_graph_version >= 1",
            name="ck_calendar_action_undos_graph_version",
        ),
        CheckConstraint("version >= 1", name="ck_calendar_action_undos_version"),
    )


class CalendarDelivery(TimestampMixin, Base):
    """Encrypted, replay-safe CalDAV operation committed with local state."""

    __tablename__ = "calendar_deliveries"

    id = Column(String(36), primary_key=True)
    owner_id = Column(
        String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    calendar_id = Column(String, nullable=False, index=True)
    # Event/proposal identifiers deliberately remain durable references rather
    # than cascading child rows: delete compensation and delivery audit must
    # survive a later event/proposal lifecycle change.
    event_uid = Column(String, nullable=False, index=True)
    proposal_id = Column(String(36), nullable=True, index=True)
    operation = Column(String(16), nullable=False)
    idempotency_key = Column(String(96), nullable=False)
    payload = Column(EncryptedJSON, nullable=False, default=dict)
    expected_event_version = Column(Integer, nullable=False)
    expected_config_version = Column(Integer, nullable=False)
    state = Column(String(24), nullable=False, default="pending")
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=True, index=True)
    claim_token = Column(String(36), nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    completed_at = Column(DateTime, nullable=True)
    last_error_code = Column(String(64), nullable=True)
    version = Column(Integer, nullable=False, default=1)

    __table_args__ = (
        ForeignKeyConstraint(
            ("calendar_id", "owner_id"),
            ("calendars.id", "calendars.owner_id"),
            name="fk_calendar_deliveries_calendar_owner",
        ),
        ForeignKeyConstraint(
            ("proposal_id", "owner_id"),
            ("action_proposals.id", "action_proposals.owner_id"),
            name="fk_calendar_deliveries_proposal_owner",
        ),
        UniqueConstraint(
            "owner_id", "idempotency_key",
            name="uq_calendar_deliveries_owner_idempotency",
        ),
        Index(
            "ix_calendar_deliveries_owner_state_due",
            "owner_id", "state", "next_attempt_at", "created_at",
        ),
        Index(
            "ix_calendar_deliveries_event_order",
            "owner_id", "event_uid", "created_at", "id",
        ),
        CheckConstraint(
            "operation IN ('create', 'update', 'delete')",
            name="ck_calendar_deliveries_operation",
        ),
        CheckConstraint(
            "state IN ('pending', 'processing', 'retry', 'conflict', "
            "'completed', 'cancelled')",
            name="ck_calendar_deliveries_state",
        ),
        CheckConstraint("attempts >= 0", name="ck_calendar_deliveries_attempts"),
        CheckConstraint(
            "expected_event_version >= 1",
            name="ck_calendar_deliveries_event_version",
        ),
        CheckConstraint(
            "expected_config_version >= 1",
            name="ck_calendar_deliveries_config_version",
        ),
        CheckConstraint("version >= 1", name="ck_calendar_deliveries_version"),
    )


class CalendarDeletedEvent(TimestampMixin, Base):
    """Hidden CalDAV delete tombstone retained until remote delete succeeds."""
    __tablename__ = "caldav_deleted_events"

    uid = Column(String, primary_key=True, index=True)
    owner = Column(String, nullable=True, index=True)
    calendar_id = Column(String, nullable=True, index=True)
    remote_href = Column(String, nullable=True)
    remote_etag = Column(String, nullable=True)
    caldav_base_url = Column(String, nullable=True)
    summary = Column(String, nullable=True)
    last_error = Column(Text, nullable=True)


class Integration(TimestampMixin, Base):
    """An external service connection (email, RSS, webhook, etc.)."""
    __tablename__ = "integrations"

    id     = Column(String, primary_key=True, index=True)
    owner  = Column(String, nullable=True, index=True)
    name   = Column(String, nullable=False)
    type   = Column(String, nullable=False)  # "email", "rss", "webhook"
    config = Column(JSON, nullable=True)     # type-specific config
    enabled = Column(Boolean, default=True)





def _migrate_seed_email_account():
    """If email_accounts is empty and settings.json has legacy flat imap_host/smtp_host
    keys, create a single default account from them so nothing breaks for users who
    upgraded. Safe to run repeatedly — it short-circuits once any row exists."""
    try:
        with engine.connect() as conn:
            tables = [r[0] for r in conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='email_accounts'"
            ))]
            if "email_accounts" not in tables:
                return
            existing = conn.execute(text("SELECT COUNT(*) FROM email_accounts")).scalar() or 0
            if existing > 0:
                return

        import json as _json
        import uuid as _uuid
        from pathlib import Path
        settings_file = Path(SETTINGS_FILE)
        if not settings_file.exists():
            return
        try:
            s = _json.loads(settings_file.read_text(encoding="utf-8"))
        except Exception:
            return

        imap_host = (s.get("imap_host") or "").strip()
        smtp_host = (s.get("smtp_host") or "").strip()
        if not imap_host and not smtp_host:
            return  # nothing to migrate

        now = utcnow_naive()
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO email_accounts
                  (id, owner, name, is_default, enabled,
                   imap_host, imap_port, imap_user, imap_password, imap_starttls,
                   smtp_host, smtp_port, smtp_user, smtp_password,
                   from_address, created_at, updated_at)
                VALUES
                  (:id, :owner, :name, :is_default, :enabled,
                   :imap_host, :imap_port, :imap_user, :imap_password, :imap_starttls,
                   :smtp_host, :smtp_port, :smtp_user, :smtp_password,
                   :from_address, :created_at, :updated_at)
            """), {
                "id": _uuid.uuid4().hex,
                "owner": None,
                "name": "Default",
                "is_default": True,
                "enabled": True,
                "imap_host": imap_host,
                "imap_port": int(s.get("imap_port") or 993),
                "imap_user": s.get("imap_user") or "",
                "imap_password": s.get("imap_password") or "",
                "imap_starttls": bool(s.get("imap_starttls", True)),
                "smtp_host": smtp_host,
                "smtp_port": int(s.get("smtp_port") or 465),
                "smtp_user": s.get("smtp_user") or "",
                "smtp_password": s.get("smtp_password") or "",
                "from_address": s.get("email_from") or "",
                "created_at": now,
                "updated_at": now,
            })
            logging.getLogger(__name__).info("Seeded email_accounts 'Default' from settings.json")
    except Exception as e:
        logging.getLogger(__name__).warning(f"seed email account migration: {e}")


# WARNING: Foreign-key enforcement is enabled globally for all SQLite connections.
# Any future migrations or schema changes that temporarily violate foreign-key
# constraints will fail. To perform such operations, foreign_keys must be
# temporarily disabled around the migration workflow.
def init_db(*, legacy_principal_initializer=None):
    """
    Initialize the database by creating all tables.
    Should be called when starting the application.
    """
    harden_database_permissions()
    _migrate_model_endpoints()
    Base.metadata.create_all(bind=engine)
    legacy_calendar_authority_pending = (
        _prepare_legacy_calendar_authority_for_fk_validation()
    )
    _migrate_add_unified_auth_columns()
    if (
        legacy_calendar_authority_pending
        and legacy_principal_initializer is not None
    ):
        legacy_principal_initializer()
    _migrate_life_planning_spine()
    _migrate_calendar_authority()
    _migrate_action_audit_guards()
    harden_database_permissions()
    _migrate_add_study_review_columns()
    _migrate_add_study_setup_initialized_column()
    _migrate_add_study_last_prompt_at_column()
    _migrate_add_hidden_models_column()
    _migrate_add_cached_models_column()
    _migrate_add_pinned_models_column()
    _migrate_add_notes_sort_order()
    _migrate_add_model_type_column()
    _migrate_add_model_endpoint_refresh_columns()
    _migrate_add_model_endpoint_owner_column()
    _migrate_add_provider_auth_id_column()
    _migrate_add_supports_tools_column()
    _migrate_add_task_run_model_column()
    _migrate_add_owner_column()
    _migrate_add_document_archived_column()
    _migrate_add_last_message_at_column()
    _migrate_add_link_columns()
    _migrate_add_dm_feature_columns()
    _migrate_add_link_invite_columns()
    _migrate_add_folder_column()
    _migrate_add_token_columns()
    _migrate_add_mode_column()
    _migrate_add_multiuser_owner_columns()
    _migrate_add_gallery_caption_column()
    _migrate_add_api_token_scopes_column()
    _migrate_add_project_completion_column()
    _migrate_backfill_document_owner_from_session()
    _migrate_add_tidy_verdict()
    _migrate_add_doc_source_email_cols()
    _migrate_add_oauth_config()
    _migrate_add_email_oauth_columns()
    _migrate_add_task_automation_columns()
    _migrate_add_disabled_tools()
    _migrate_add_mcp_oauth_tokens_column()
    _migrate_add_task_v2_columns()
    _migrate_add_notifications_enabled()
    _migrate_drop_ping_notes_tasks()
    _migrate_add_crew_member_id()
    _migrate_add_assistant_columns()
    _migrate_add_email_smtp_security()
    _migrate_seed_email_account()
    _migrate_add_calendar_metadata()
    _migrate_add_calendar_is_utc()
    _migrate_add_calendar_origin()
    _migrate_add_calendar_account_id()
    _migrate_add_caldav_sync_columns()
    _migrate_add_calendar_recurrence_exdates()
    _migrate_chat_messages_fts()
    _migrate_encrypt_session_headers()
    _migrate_encrypt_email_passwords()
    _migrate_encrypt_signatures()
    _migrate_encrypt_endpoint_keys()
    _migrate_backfill_task_folders()


def _migrate_action_audit_guards():
    """Install append-only SQLite guards for an already-created audit table."""

    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS action_audit_no_update
            BEFORE UPDATE ON action_audit
            BEGIN
                SELECT RAISE(ABORT, 'ActionAudit rows are append-only');
            END
        """))


def _migrate_life_planning_spine():
    """Complete the additive 0003 shape during pre-Alembic adoption.

    ``create_all`` creates the new Life OS tables but cannot add columns to an
    existing ``entity_links`` table.  This compatibility step exists only for
    the guarded, backed-up legacy SQLite adoption path; databases already at
    0002 execute the reviewed Alembic revision instead.
    """

    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as conn:
        columns = {
            str(row[1])
            for row in conn.execute(text("PRAGMA table_info(entity_links)"))
        }
        additions = (
            ("provenance", "JSON NOT NULL DEFAULT '{}'"),
            ("confidence", "INTEGER NOT NULL DEFAULT 100"),
            ("sensitivity", "VARCHAR(24) NOT NULL DEFAULT 'private'"),
            ("version", "INTEGER NOT NULL DEFAULT 1"),
            ("deleted_at", "DATETIME"),
            (
                "updated_at",
                "DATETIME NOT NULL DEFAULT '1970-01-01 00:00:00'",
            ),
        )
        for name, definition in additions:
            if name not in columns:
                conn.execute(text(
                    f'ALTER TABLE entity_links ADD COLUMN "{name}" {definition}'
                ))
        conn.execute(text(
            "UPDATE entity_links SET updated_at = CURRENT_TIMESTAMP "
            "WHERE updated_at = '1970-01-01 00:00:00'"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_entity_links_deleted_at "
            "ON entity_links (deleted_at)"
        ))
        # SQLite cannot add CHECK constraints without rebuilding the legacy
        # table. Equivalent triggers keep the additive adoption path aligned
        # with EntityLink's model invariants.
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS entity_links_validate_insert
            BEFORE INSERT ON entity_links
            WHEN NEW.confidence < 0 OR NEW.confidence > 100 OR NEW.version < 1
            BEGIN
                SELECT RAISE(ABORT, 'EntityLink confidence/version is invalid');
            END
        """))
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS entity_links_validate_update
            BEFORE UPDATE ON entity_links
            WHEN NEW.confidence < 0 OR NEW.confidence > 100 OR NEW.version < 1
            BEGIN
                SELECT RAISE(ABORT, 'EntityLink confidence/version is invalid');
            END
        """))
        # ADD COLUMN needs a constant default on populated SQLite tables. This
        # trigger gives future non-ORM inserts the runtime timestamp expected
        # by the model instead of permanently retaining the migration sentinel.
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS entity_links_fill_updated_at
            AFTER INSERT ON entity_links
            WHEN NEW.updated_at = '1970-01-01 00:00:00'
            BEGIN
                UPDATE entity_links
                SET updated_at = CURRENT_TIMESTAMP
                WHERE id = NEW.id;
            END
        """))
        # EntityLink.metadata and provenance use EncryptedJSON in V3. The
        # guarded pre-Alembic adoption path stamps directly at head, so it must
        # perform the same online data rewrite as migration 0003.
        from src.secret_storage import (
            encrypt_plaintext,
            is_content_encrypted,
            is_decryptable,
            is_encrypted,
        )

        private_json_rows = conn.execute(text(
            'SELECT id, metadata, provenance FROM entity_links'
        )).all()
        for link_id, raw_metadata, raw_provenance in private_json_rows:
            updates: dict[str, str] = {}
            for field, raw_value in (
                ("metadata", raw_metadata),
                ("provenance", raw_provenance),
            ):
                if isinstance(raw_value, dict):
                    decoded_value = raw_value
                elif isinstance(raw_value, str):
                    try:
                        decoded_value = json.loads(raw_value)
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise RuntimeError(
                            f"EntityLink {field} must be valid JSON before encryption"
                        ) from exc
                    if isinstance(decoded_value, str):
                        if is_encrypted(decoded_value):
                            if not is_decryptable(decoded_value):
                                raise RuntimeError(
                                    f"EntityLink {field} could not be decrypted with the active key"
                                )
                            continue
                        raise RuntimeError(
                            f"EntityLink {field} must be a JSON object before encryption"
                        )
                else:
                    raise RuntimeError(
                        f"EntityLink {field} must be a JSON object before encryption"
                    )
                if not isinstance(decoded_value, dict):
                    raise RuntimeError(
                        f"EntityLink {field} must be a JSON object before encryption"
                    )
                serialized = json.dumps(
                    decoded_value, ensure_ascii=False, separators=(",", ":")
                )
                updates[field] = json.dumps(encrypt_plaintext(serialized))
            if updates:
                assignments = ", ".join(
                    f'"{field}" = :{field}' for field in updates
                )
                conn.execute(
                    text(f'UPDATE entity_links SET {assignments} WHERE id = :id'),
                    {**updates, "id": link_id},
                )

        # PlanningItem.title/details changed from V2 plaintext to V3 content
        # envelopes. The distinct `enc:c1:` marker makes this exact-text
        # rewrite retry-safe without mistaking a user's literal legacy
        # `enc:<fernet>` string for ciphertext.
        planning_rows = conn.execute(text(
            "SELECT id, title, details FROM planning_items"
        )).all()
        for planning_id, raw_title, raw_details in planning_rows:
            title_value = "" if raw_title is None else str(raw_title)
            details_value = "" if raw_details is None else str(raw_details)
            for field, stored in (
                ("title", title_value), ("details", details_value)
            ):
                if is_content_encrypted(stored) and not is_decryptable(stored):
                    raise RuntimeError(
                        f"PlanningItem {field} could not be decrypted with the active key"
                    )
            encrypted_title = (
                title_value
                if is_content_encrypted(title_value)
                else encrypt_plaintext(title_value)
            )
            encrypted_details = (
                details_value
                if is_content_encrypted(details_value)
                else encrypt_plaintext(details_value)
            )
            if (encrypted_title, encrypted_details) != (
                title_value, details_value
            ):
                conn.execute(text(
                    "UPDATE planning_items SET title = :title, details = :details "
                    "WHERE id = :id"
                ), {
                    "title": encrypted_title,
                    "details": encrypted_details,
                    "id": planning_id,
                })
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS life_entity_versions_no_update
            BEFORE UPDATE ON life_entity_versions
            BEGIN
                SELECT RAISE(ABORT, 'LifeEntityVersion rows are append-only');
            END
        """))
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS life_entity_versions_no_delete
            BEFORE DELETE ON life_entity_versions
            BEGIN
                SELECT RAISE(ABORT, 'LifeEntityVersion rows are append-only');
            END
        """))
        conn.execute(text("""
            CREATE TRIGGER IF NOT EXISTS action_audit_no_delete
            BEFORE DELETE ON action_audit
            BEGIN
                SELECT RAISE(ABORT, 'ActionAudit rows are append-only');
            END
        """))


_CALENDAR_AUTHORITY_CHILD_TABLES = (
    "email_outbound_deliveries",
    "email_outbound_drafts",
    "calendar_deliveries",
    "calendar_action_undos",
)


def _legacy_calendar_authority_state():
    """Return whether legacy calendar tables need the reviewed 0005 repair."""

    from sqlalchemy import inspect as sa_inspect

    schema = sa_inspect(engine)
    if not schema.has_table("calendars") or not schema.has_table("calendar_events"):
        return None
    calendar_columns = {
        str(column["name"]) for column in schema.get_columns("calendars")
    }
    event_columns = {
        str(column["name"])
        for column in schema.get_columns("calendar_events")
    }
    calendar_new = {"owner_id", "config_version"} & calendar_columns
    event_new = {"owner_id", "version"} & event_columns
    complete = (
        calendar_new == {"owner_id", "config_version"}
        and event_new == {"owner_id", "version"}
    )
    if bool(calendar_new or event_new) and not complete:
        raise RuntimeError("Refusing a partial legacy calendar-authority schema")
    return not complete


def _drop_empty_calendar_authority_children():
    """Remove only empty V3 children that cannot reference legacy parents yet."""

    with engine.begin() as connection:
        for table_name in _CALENDAR_AUTHORITY_CHILD_TABLES:
            if not connection.dialect.has_table(connection, table_name):
                continue
            retained = int(connection.execute(text(
                f'SELECT COUNT(*) FROM "{table_name}"'
            )).scalar() or 0)
            if retained:
                raise RuntimeError(
                    "Refusing legacy calendar/action adoption with existing "
                    "authority data"
                )
            connection.execute(text(f'DROP TABLE "{table_name}"'))


def _prepare_legacy_calendar_authority_for_fk_validation():
    """Make a create_all retry structurally valid before strict FK checks.

    ``create_all`` can add V3 child tables to a V2.1 database while leaving the
    legacy calendar parents unchanged. SQLite then raises a schema-level foreign
    key mismatch before the unified-auth bridge can establish the principals
    required by revision 0005. Drop only those provably-empty new children; the
    reviewed calendar migration recreates them after parent repair.
    """

    if engine.dialect.name != "sqlite":
        return False
    needs_repair = _legacy_calendar_authority_state()
    if not needs_repair:
        return False
    _drop_empty_calendar_authority_children()
    return True


def _migrate_calendar_authority():
    """Complete the additive 0005 shape during pre-Alembic adoption.

    Fresh and already-versioned databases execute Alembic 0005. The guarded,
    backed-up pre-Alembic SQLite path first runs ``create_all``; that creates
    the new undo/outbox tables but cannot alter existing calendar tables. Reuse
    the reviewed migration's exact owner preflight, backfill, and batch-table
    constraints before the runtime is permitted to stamp the current head.
    """

    if engine.dialect.name != "sqlite":
        return

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from migrations.versions import calendar_authority_20260720_0005 as revision

    needs_repair = _legacy_calendar_authority_state()
    if needs_repair is None:
        return

    if needs_repair:
        # ``create_all`` has already created new empty calendar/email action
        # child tables. Their composite foreign keys cannot become valid until
        # calendars and action_proposals have reviewed owner uniqueness, and
        # SQLite rejects even the owner backfill while a mismatched child
        # exists. Preflight first, then remove only provably-empty new tables
        # and recreate them below.
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                revision._preflight_owner_mapping()
        _drop_empty_calendar_authority_children()

        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            try:
                context = MigrationContext.configure(connection)
                with Operations.context(context):
                    revision.op.add_column(
                        "calendars",
                        Column("owner_id", String(length=36), nullable=True),
                    )
                    revision.op.add_column(
                        "calendars",
                        Column(
                            "config_version", Integer(), nullable=False,
                            server_default="1",
                        ),
                    )
                    revision.op.add_column(
                        "calendar_events",
                        Column("owner_id", String(length=36), nullable=True),
                    )
                    revision.op.add_column(
                        "calendar_events",
                        Column(
                            "version", Integer(), nullable=False,
                            server_default="1",
                        ),
                    )
                    revision._backfill_owner_ids()
                    revision._upgrade_existing_tables(
                        manage_sqlite_foreign_keys=False
                    )
                if connection.in_transaction():
                    connection.commit()
            except Exception:
                if connection.in_transaction():
                    connection.rollback()
                raise
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")
                enabled = int(connection.exec_driver_sql(
                    "PRAGMA foreign_keys"
                ).scalar() or 0)
                if enabled != 1:
                    raise RuntimeError(
                        "Could not restore SQLite foreign-key enforcement"
                    )

    # ``create_all`` does not add indexes to pre-existing tables. These exact
    # indexes are the only remaining 0005 objects not installed by the batch
    # rebuild above; IF NOT EXISTS keeps retry and already-current adoption
    # idempotent without weakening head validation.
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_action_proposals_id_owner "
            "ON action_proposals (id, owner_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_calendars_owner_id "
            "ON calendars (owner_id)"
        ))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_calendar_events_owner_id "
            "ON calendar_events (owner_id)"
        ))

    for table_name in (
        "calendar_action_undos",
        "calendar_deliveries",
        "email_outbound_drafts",
        "email_outbound_deliveries",
    ):
        Base.metadata.tables[table_name].create(bind=engine, checkfirst=True)


def _migrate_backfill_task_folders():
    """Backfill folder='Tasks' on pre-existing task/research sessions.

    Sessions created by the task scheduler (LLM tasks, action tasks, research
    runs) now set folder='Tasks' at creation time.  This migration tags any
    older sessions that predate that assignment.  Idempotent — only touches
    rows where folder is NULL or empty and the title matches known prefixes.
    """
    try:
        with engine.connect() as conn:
            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(sessions)"))]
            if "folder" not in cols:
                return
            res = conn.execute(text(
                "UPDATE sessions SET folder = 'Tasks' "
                "WHERE (folder IS NULL OR folder = '') "
                "AND (name LIKE '[Task] %' OR name LIKE '[Research] %')"
            ))
            conn.commit()
            if res.rowcount:
                logging.getLogger(__name__).info(
                    f"Backfilled folder='Tasks' on {res.rowcount} task/research sessions")
    except Exception as e:
        logging.getLogger(__name__).warning(f"task folder backfill: {e}")


def _migrate_chat_messages_fts():
    """Create and backfill the session transcript FTS index for SQLite."""
    if not DATABASE_URL.startswith("sqlite"):
        return

    db_path = DATABASE_URL.replace("sqlite:///", "")
    if db_path == ":memory:":
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp._odysseus_fts5_probe USING fts5(content)")
            conn.execute("DROP TABLE IF EXISTS temp._odysseus_fts5_probe")
        except Exception as e:
            logging.getLogger(__name__).warning(f"chat_messages FTS migration skipped; FTS5 unavailable: {e}")
            return

        conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chat_messages_fts USING fts5(
                content,
                message_id UNINDEXED,
                session_id UNINDEXED,
                role UNINDEXED
            );

            CREATE TRIGGER IF NOT EXISTS chat_messages_fts_ai
            AFTER INSERT ON chat_messages BEGIN
                INSERT INTO chat_messages_fts(content, message_id, session_id, role)
                VALUES (COALESCE(new.content, ''), new.id, new.session_id, new.role);
            END;

            CREATE TRIGGER IF NOT EXISTS chat_messages_fts_ad
            AFTER DELETE ON chat_messages BEGIN
                DELETE FROM chat_messages_fts WHERE message_id = old.id;
            END;

            CREATE TRIGGER IF NOT EXISTS chat_messages_fts_au
            AFTER UPDATE ON chat_messages BEGIN
                DELETE FROM chat_messages_fts WHERE message_id = old.id;
                INSERT INTO chat_messages_fts(content, message_id, session_id, role)
                VALUES (COALESCE(new.content, ''), new.id, new.session_id, new.role);
            END;
            """
        )
        conn.execute(
            """
            INSERT INTO chat_messages_fts(content, message_id, session_id, role)
            SELECT COALESCE(cm.content, ''), cm.id, cm.session_id, cm.role
            FROM chat_messages cm
            WHERE NOT EXISTS (
                SELECT 1 FROM chat_messages_fts fts
                WHERE fts.message_id = cm.id
            )
            """
        )
        conn.commit()
    except Exception as e:
        logging.getLogger(__name__).warning(f"chat_messages FTS migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_add_email_smtp_security():
    """Add explicit SMTP security mode for Proton Bridge/custom local SMTP."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(email_accounts)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "smtp_security" not in columns:
            conn.execute("ALTER TABLE email_accounts ADD COLUMN smtp_security TEXT DEFAULT 'ssl'")
            conn.execute(
                "UPDATE email_accounts SET smtp_security = CASE "
                "WHEN COALESCE(smtp_port, 465) = 587 THEN 'starttls' "
                "WHEN COALESCE(smtp_port, 465) = 465 THEN 'ssl' "
                "ELSE 'ssl' END "
                "WHERE smtp_security IS NULL OR smtp_security = ''"
            )
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added smtp_security column to email_accounts")
    except Exception as e:
        logging.getLogger(__name__).warning(f"smtp_security migration skipped: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_encrypt_endpoint_keys():
    """Encrypt any plaintext provider API keys in model_endpoints. Idempotent;
    raw SQL so the EncryptedText decorator isn't applied twice."""
    try:
        from src.secret_storage import encrypt, is_encrypted
    except Exception as e:
        logger.warning(f"secret_storage import failed; skipping endpoint-key migration: {e}")
        return
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, api_key FROM model_endpoints")).fetchall()
            migrated = 0
            for rid, key in rows:
                if key and not is_encrypted(key):
                    conn.execute(text("UPDATE model_endpoints SET api_key = :k WHERE id = :id"),
                                 {"k": encrypt(key), "id": rid})
                    migrated += 1
            if migrated:
                conn.commit()
                logger.info(f"Encrypted plaintext API key on {migrated} endpoint row(s)")
    except Exception as e:
        logger.warning(f"Endpoint-key encryption migration skipped: {e}")


def _migrate_encrypt_session_headers():
    """Encrypt legacy plaintext JSON stored in ``sessions.headers``.

    The mapped type stores new values as an encrypted JSON string and handles
    both legacy dicts and encrypted strings on read.  Inspecting the raw driver
    value here keeps the migration idempotent without decrypting/re-encrypting
    every session on each startup.
    """
    try:
        from src.secret_storage import is_encrypted
    except Exception:
        logger.warning("secret_storage import failed; skipping session-header migration")
        return

    try:
        with engine.connect() as conn:
            rows = conn.exec_driver_sql("SELECT id, headers FROM sessions").fetchall()
            migrated = 0
            for session_id, raw_headers in rows:
                if raw_headers in (None, ""):
                    continue

                decoded = raw_headers
                if isinstance(raw_headers, str):
                    if is_encrypted(raw_headers):
                        continue
                    try:
                        decoded = json.loads(raw_headers)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Session-header encryption migration skipped malformed row %s",
                            session_id,
                        )
                        continue
                    if isinstance(decoded, str) and is_encrypted(decoded):
                        continue

                if not isinstance(decoded, dict):
                    logger.warning(
                        "Session-header encryption migration skipped non-object row %s",
                        session_id,
                    )
                    continue

                conn.execute(
                    Session.__table__.update()
                    .where(Session.__table__.c.id == session_id)
                    .values(headers=decoded)
                )
                migrated += 1

            if migrated:
                conn.commit()
                logger.info("Encrypted endpoint headers on %d session row(s)", migrated)
    except Exception:
        # SQLAlchemy exception strings can include bound values. Never attach
        # an endpoint Authorization header to a migration log record.
        logger.warning("Session-header encryption migration skipped")


def _migrate_encrypt_signatures():
    """Encrypt any plaintext signature images still in the signatures table.
    Idempotent — rows already prefixed with `enc:` are skipped. Uses raw SQL
    so the EncryptedText type decorator isn't applied twice."""
    try:
        from src.secret_storage import encrypt, is_encrypted
    except Exception as e:
        logger.warning(f"secret_storage import failed; skipping signature migration: {e}")
        return
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, data_png, svg FROM signatures"
            )).fetchall()
            migrated = 0
            for rid, data_png, svg in rows:
                updates = {}
                if data_png and not is_encrypted(data_png):
                    updates["data_png"] = encrypt(data_png)
                if svg and not is_encrypted(svg):
                    updates["svg"] = encrypt(svg)
                if updates:
                    sets = ", ".join(f"{k} = :{k}" for k in updates)
                    conn.execute(text(f"UPDATE signatures SET {sets} WHERE id = :id"), {**updates, "id": rid})
                    migrated += 1
            if migrated:
                conn.commit()
                logger.info(f"Encrypted plaintext signature(s) on {migrated} row(s)")
    except Exception as e:
        logger.warning(f"Signature encryption migration skipped: {e}")


def _migrate_encrypt_email_passwords():
    """Encrypt any plaintext IMAP/SMTP passwords still in the email_accounts
    table. Idempotent — rows already prefixed with `enc:` are skipped.
    Safe to run on every startup."""
    try:
        from src.secret_storage import encrypt, is_encrypted
    except Exception as e:
        logger.warning(f"secret_storage import failed; skipping password migration: {e}")
        return
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT id, imap_password, smtp_password FROM email_accounts"
            )).fetchall()
            migrated = 0
            for row in rows:
                rid, imap_pw, smtp_pw = row
                updates = {}
                if imap_pw and not is_encrypted(imap_pw):
                    updates["imap_password"] = encrypt(imap_pw)
                if smtp_pw and not is_encrypted(smtp_pw):
                    updates["smtp_password"] = encrypt(smtp_pw)
                if updates:
                    sets = ", ".join(f"{k} = :{k}" for k in updates)
                    params = {**updates, "id": rid}
                    conn.execute(text(f"UPDATE email_accounts SET {sets} WHERE id = :id"), params)
                    migrated += 1
            if migrated:
                conn.commit()
                logger.info(f"Encrypted plaintext passwords on {migrated} email account row(s)")
    except Exception as e:
        logger.warning(f"Password migration failed (will retry next start): {e}")


def _migrate_add_calendar_is_utc():
    """Add is_utc column to calendar_events so imported events can preserve
    their original UTC timestamps (Z-suffix on the wire) without touching
    legacy naive-local rows."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(calendar_events)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "is_utc" not in columns:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN is_utc BOOLEAN DEFAULT 0 NOT NULL")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'is_utc' column to calendar_events")
    except Exception as e:
        logging.getLogger(__name__).warning(f"is_utc migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_add_calendar_origin():
    """Add `origin` to calendar_events so the CalDAV sync can tell server-pulled
    rows (prunable when they vanish upstream) from locally-created ones (agent /
    email triage / failed write-back), which must never be pruned. Idempotent."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(calendar_events)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "origin" not in columns:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN origin TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_calendar_events_origin ON calendar_events(origin)")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'origin' column to calendar_events")
    except Exception as e:
        logging.getLogger(__name__).warning(f"calendar_events.origin migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_add_calendar_account_id():
    """Add `account_id` to calendars so each CalDAV-backed calendar knows which
    credential set (from caldav_accounts in user prefs) owns it. Idempotent."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(calendars)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "account_id" not in columns:
            conn.execute("ALTER TABLE calendars ADD COLUMN account_id TEXT")
            conn.execute("CREATE INDEX IF NOT EXISTS ix_calendars_account_id ON calendars(account_id)")
            conn.commit()
            logging.getLogger(__name__).info("Migrated: added 'account_id' column to calendars")
    except Exception as e:
        logging.getLogger(__name__).warning(f"calendars.account_id migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_add_caldav_sync_columns():
    """Add remote CalDAV metadata used for bidirectional sync."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    try:
        conn = sqlite3.connect(db_path)
        ev_columns = [row[1] for row in conn.execute("PRAGMA table_info(calendar_events)").fetchall()]
        if ev_columns and "remote_href" not in ev_columns:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN remote_href TEXT")
        if ev_columns and "remote_etag" not in ev_columns:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN remote_etag TEXT")
        if ev_columns and "caldav_sync_pending" not in ev_columns:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN caldav_sync_pending TEXT")

        cal_columns = [row[1] for row in conn.execute("PRAGMA table_info(calendars)").fetchall()]
        if cal_columns and "caldav_base_url" not in cal_columns:
            conn.execute("ALTER TABLE calendars ADD COLUMN caldav_base_url TEXT")
        conn.commit()
        conn.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"CalDAV sync metadata migration failed: {e}")


def _migrate_add_calendar_metadata():
    """Add importance/event_type/last_pinged columns to calendar_events table."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(calendar_events)")
        columns = [row[1] for row in cursor.fetchall()]
        if columns and "importance" not in columns:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN importance TEXT DEFAULT 'normal'")
        if columns and "event_type" not in columns:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN event_type TEXT")
        if columns and "last_pinged" not in columns:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN last_pinged DATETIME")
        conn.commit()
    except Exception as e:
        logging.getLogger(__name__).warning(f"calendar_events migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _migrate_add_calendar_recurrence_exdates():
    """Add skipped recurrence occurrences for deleting one instance of a series."""
    import sqlite3
    db_path = DATABASE_URL.replace("sqlite:///", "")
    if not os.path.exists(db_path):
        return
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(calendar_events)").fetchall()]
        if columns and "recurrence_exdates" not in columns:
            conn.execute("ALTER TABLE calendar_events ADD COLUMN recurrence_exdates TEXT DEFAULT ''")
        conn.commit()
    except Exception as e:
        logging.getLogger(__name__).warning(f"calendar_events recurrence_exdates migration failed: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

def get_db():
    """
    Dependency to get a database session.
    Used in FastAPI routes to inject database sessions.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

from contextlib import contextmanager
from typing import Generator

@contextmanager
def get_db_session() -> Generator:
    """Context manager for database sessions"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

def bulk_insert_messages(session_id: str, messages: list):
    """Efficiently insert multiple messages"""
    with get_db_session() as db:
        db.bulk_insert_mappings(
            ChatMessage,
            [
                {
                    'session_id': session_id,
                    'role': msg['role'],
                    'content': msg['content'],
                    'timestamp': utcnow_naive()
                }
                for msg in messages
            ]
        )

def cleanup_old_sessions(days: int = 30):
    """Remove sessions older than specified days"""
    from datetime import timedelta
    
    with get_db_session() as db:
        cutoff_date = utcnow_naive() - timedelta(days=days)
        
        deleted_count = db.query(Session).filter(
            Session.archived == True,
            Session.last_accessed < cutoff_date,
            Session.is_important == False
        ).delete()
        
        return deleted_count

def get_session_stats():
    """Get database statistics"""
    with get_db_session() as db:
        stats = {
            'total_sessions': db.query(Session).count(),
            'active_sessions': db.query(Session).filter(Session.archived == False).count(),
            'archived_sessions': db.query(Session).filter(Session.archived == True).count(),
            'total_messages': db.query(ChatMessage).count(),
            'total_memories': db.query(Memory).count()
        }
        return stats

def get_detailed_stats():
    """Get comprehensive database statistics including file size"""
    stats = get_session_stats()  # Use existing function
    
    # Add database file size
    db_size_mb = 0.0
    if "sqlite" in DATABASE_URL:
        db_path = DATABASE_URL.replace("sqlite:///", "")
        if not os.path.isabs(db_path):
            db_path = os.path.abspath(db_path)
        
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path)
            db_size_mb = round(db_size / (1024 * 1024), 2)
    
    stats['database_size_mb'] = db_size_mb
    return stats

def update_session_last_accessed(session_id: str):
    """Update the last_accessed timestamp for a session"""
    with get_db_session() as db:
        db_session = db.query(Session).filter(Session.id == session_id).first()
        if db_session:
            db_session.last_accessed = utcnow_naive()
            db.commit()
            return True
    return False

def get_session_mode(session_id: str):
    """Return a session's persisted `mode`, or None if unset/unknown.

    Best-effort: never raises (returns None on any DB error) so callers on hot
    request paths needn't guard it. Routed through get_db_session() so the
    connection is always returned to the pool."""
    try:
        with get_db_session() as db:
            return db.query(Session.mode).filter(Session.id == session_id).scalar()
    except Exception:
        logger.warning("Failed to read mode for session %s", session_id)
        return None

def set_session_mode(session_id: str, mode: str) -> bool:
    """Persist a session's `mode`. Best-effort: never raises, returns success.

    Routed through get_db_session() so a failure mid-write (e.g. a SQLite
    'database is locked' under concurrent streams) still returns the connection
    to the pool instead of leaking it — repeated leaks would exhaust it."""
    try:
        with get_db_session() as db:
            db.query(Session).filter(Session.id == session_id).update({"mode": mode})
        return True
    except Exception:
        logger.warning("Failed to persist mode %r for session %s", mode, session_id)
        return False

def get_session_by_id(session_id: str):
    """Get a session by ID"""
    with get_db_session() as db:
        return db.query(Session).filter(Session.id == session_id).first()

def get_upcoming_events(owner, horizon_days: int = 60, limit: int = 40):
    """Upcoming events for one concrete login alias, soonest first.

    Calendar ownership is the immutable ``Account.id``. Missing or unknown
    aliases return no rows so autonomous email processing cannot cross account
    boundaries.
    """
    from datetime import timedelta
    from src.identity import find_account

    owner_alias = str(owner or "").strip()
    if not owner_alias:
        return []
    now = utcnow_naive()
    with get_db_session() as db:
        account = find_account(db, owner_alias)
        if account is None:
            return []
        q = db.query(CalendarEvent).join(CalendarCal).filter(
            CalendarCal.owner_id == account.id,
            CalendarEvent.owner_id == account.id,
            CalendarEvent.dtstart >= now,
            CalendarEvent.dtstart <= now + timedelta(days=horizon_days),
            CalendarEvent.status != "cancelled",
        )
        return [
            {
                "uid": e.uid,
                "title": e.summary or "",
                "start": e.dtstart.isoformat() if e.dtstart else "",
                "version": e.version,
            }
            for e in q.order_by(CalendarEvent.dtstart).limit(limit).all()
        ]

def archive_session(session_id: str):
    """Archive a session"""
    with get_db_session() as db:
        session = db.query(Session).filter(Session.id == session_id).first()
        if session:
            session.archived = True
            db.commit()
            return True
    return False


# Register isolated post-baseline metadata models on the canonical Base.  The
# module imports only Base/type definitions from this file, so loading it after
# all core models avoids a circular initialization while ensuring legacy
# ``Base.metadata.create_all`` compatibility tests include the reviewed tables.
from src.upload_metadata_models import (  # noqa: E402,F401
    ChatUploadMetadata,
    ChatUploadMetadataImportRun,
)
from src.profile_configuration_models import (  # noqa: E402,F401
    ProfileConfiguration,
    ProfileConfigurationImportRun,
    ProfileConfigurationMutation,
)

# Schema initialization is intentionally explicit. Production entrypoints call
# ``src.database_runtime.initialize_database`` before using a session; importing
# model definitions must never create or migrate a database.
