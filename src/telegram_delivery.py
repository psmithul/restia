"""Canonical database authority for Telegram polling and reply delivery.

The Bot API allows one long poller per bot.  A database lease with a monotonic
fencing token coordinates that poller across Restia replicas; the same row
owns the durable cursor and poison-update attempt counter.  Inbound processing
and reply delivery use independent, expiring claim tokens so webhook and
polling workers cannot concurrently project or send the same update.

Telegram ``sendMessage`` has no idempotency key.  Reply delivery is therefore
honestly at-least-once: a process crash after Telegram accepts a reply but
before the completion commit can replay it after the claim expires.  Fencing
prevents concurrent sends and stale database writes, not that provider-boundary
crash window.

The retired JSON/SQLite sidecars are read only by the bounded adoption method.
Runtime reads never fall back to them after the import marker is committed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from core.database import (
    Account,
    SessionLocal,
    TelegramDeadLetter,
    TelegramInboundUpdate,
    TelegramPollingState,
    TelegramRuntimeImportRun,
    utcnow_naive,
)
from src.secret_storage import decrypt
from src.telegram_inbound_ledger import (
    TelegramInboundConflict,
    TelegramInboundInFlight,
    TelegramInboundLedgerError,
    TelegramReplyPending,
)


POLLING_LEASE = timedelta(seconds=90)
INBOUND_PROCESSING_LEASE = timedelta(minutes=5)
REPLY_DELIVERY_LEASE = timedelta(minutes=2)
LEGACY_SOURCE_KIND = "telegram-runtime-sidecars-v1"
MAX_LEGACY_JSON_BYTES = 2 * 1024 * 1024
MAX_LEGACY_LEDGER_BYTES = 64 * 1024 * 1024
MAX_LEGACY_INBOUND_ROWS = 10_000
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_SAFE_ERROR_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


class TelegramRuntimeAuthorityError(RuntimeError):
    """A stable Telegram coordination invariant failed."""


class TelegramPollingLeaseLost(TelegramRuntimeAuthorityError):
    """A stale poller attempted a fenced state transition."""


@dataclass(frozen=True, slots=True)
class TelegramPollingLease:
    bot_fingerprint: str
    worker_id: str
    fencing_token: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TelegramFailureResolution:
    attempts: int
    resolved: bool
    next_offset: int | None


@dataclass(frozen=True, slots=True)
class TelegramLegacyImportResult:
    state: str
    offset_imported: bool
    dead_letters_imported: int
    inbound_imported: int
    inbound_discarded: int


def _fingerprint(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if _HEX64_RE.fullmatch(normalized) is None:
        raise ValueError("Telegram bot fingerprint must be a 64-character digest")
    return normalized


def _worker_id(value: object) -> str:
    normalized = str(value or "").strip()
    if (
        not normalized
        or len(normalized) > 128
        or any(ord(character) < 33 or ord(character) == 127 for character in normalized)
    ):
        raise ValueError("Telegram worker ID is invalid")
    return normalized


def _update_id(value: object) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Telegram update ID must be an integer") from exc
    if normalized < 0:
        raise ValueError("Telegram update ID must be non-negative")
    return normalized


def _private_text(value: object, *, field: str, required: bool = False) -> str:
    normalized = str(value or "")
    if (
        (required and not normalized)
        or len(normalized) > 100_000
        or "\x00" in normalized
    ):
        raise ValueError(f"Telegram {field} is invalid")
    return normalized


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _database_now(db, supplied: datetime | None = None) -> datetime:
    if supplied is not None:
        return _naive_utc(supplied)
    value = db.query(func.current_timestamp()).scalar()
    return _naive_utc(value) if isinstance(value, datetime) else utcnow_naive()


def _claim_token() -> str:
    return "tgc_" + os.urandom(32).hex()


def _claim_digest(row_id: str, purpose: str, token: str) -> str:
    return hashlib.sha256(
        f"{row_id}\0{purpose}\0{token}".encode("utf-8")
    ).hexdigest()


def _safe_error_type(value: object) -> str:
    normalized = str(value or "")
    return normalized if _SAFE_ERROR_RE.fullmatch(normalized) else "RuntimeError"


class TelegramRuntimeAuthority:
    """Small SQLAlchemy port shared by pollers, webhooks, and tests."""

    def __init__(self, session_factory: Callable = SessionLocal) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _ensure_state(db, bot_fingerprint: str) -> TelegramPollingState:
        row = db.query(TelegramPollingState).filter(
            TelegramPollingState.bot_fingerprint == bot_fingerprint
        ).one_or_none()
        if row is not None:
            return row
        try:
            with db.begin_nested():
                db.add(TelegramPollingState(
                    bot_fingerprint=bot_fingerprint,
                    failure_attempts=0,
                    lease_token=0,
                    version=1,
                ))
                db.flush()
        except IntegrityError:
            db.expire_all()
        row = db.query(TelegramPollingState).filter(
            TelegramPollingState.bot_fingerprint == bot_fingerprint
        ).one_or_none()
        if row is None:
            raise TelegramRuntimeAuthorityError(
                "Telegram polling state could not be initialized"
            )
        return row

    def acquire_polling_lease(
        self,
        *,
        bot_fingerprint: object,
        worker_id: object,
        lease: timedelta = POLLING_LEASE,
        now: datetime | None = None,
    ) -> TelegramPollingLease | None:
        scope = _fingerprint(bot_fingerprint)
        worker = _worker_id(worker_id)
        duration = max(timedelta(seconds=15), lease)
        db = self._session_factory()
        try:
            clock = _database_now(db, now)
            row = self._ensure_state(db, scope)
            previous_version = int(row.version or 1)
            previous_token = int(row.lease_token or 0)
            available = or_(
                TelegramPollingState.lease_owner.is_(None),
                TelegramPollingState.lease_expires_at.is_(None),
                TelegramPollingState.lease_expires_at <= clock,
                TelegramPollingState.lease_owner == worker,
            )
            changed = db.query(TelegramPollingState).filter(
                TelegramPollingState.bot_fingerprint == scope,
                TelegramPollingState.version == previous_version,
                available,
            ).update({
                TelegramPollingState.lease_owner: worker,
                TelegramPollingState.lease_token: previous_token + 1,
                TelegramPollingState.lease_expires_at: clock + duration,
                TelegramPollingState.version: previous_version + 1,
                TelegramPollingState.updated_at: clock,
            }, synchronize_session=False)
            if changed != 1:
                db.rollback()
                return None
            db.commit()
            return TelegramPollingLease(
                bot_fingerprint=scope,
                worker_id=worker,
                fencing_token=previous_token + 1,
                expires_at=clock + duration,
            )
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def renew_polling_lease(
        self,
        claim: TelegramPollingLease,
        *,
        lease: timedelta = POLLING_LEASE,
        now: datetime | None = None,
    ) -> TelegramPollingLease | None:
        duration = max(timedelta(seconds=15), lease)
        db = self._session_factory()
        try:
            clock = _database_now(db, now)
            changed = db.query(TelegramPollingState).filter(
                TelegramPollingState.bot_fingerprint == claim.bot_fingerprint,
                TelegramPollingState.lease_owner == claim.worker_id,
                TelegramPollingState.lease_token == claim.fencing_token,
            ).update({
                TelegramPollingState.lease_expires_at: clock + duration,
                TelegramPollingState.version: TelegramPollingState.version + 1,
                TelegramPollingState.updated_at: clock,
            }, synchronize_session=False)
            if changed != 1:
                db.rollback()
                return None
            db.commit()
            return TelegramPollingLease(
                bot_fingerprint=claim.bot_fingerprint,
                worker_id=claim.worker_id,
                fencing_token=claim.fencing_token,
                expires_at=clock + duration,
            )
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def release_polling_lease(self, claim: TelegramPollingLease) -> bool:
        db = self._session_factory()
        try:
            clock = _database_now(db)
            changed = db.query(TelegramPollingState).filter(
                TelegramPollingState.bot_fingerprint == claim.bot_fingerprint,
                TelegramPollingState.lease_owner == claim.worker_id,
                TelegramPollingState.lease_token == claim.fencing_token,
            ).update({
                TelegramPollingState.lease_owner: None,
                TelegramPollingState.lease_expires_at: None,
                TelegramPollingState.version: TelegramPollingState.version + 1,
                TelegramPollingState.updated_at: clock,
            }, synchronize_session=False)
            db.commit()
            return changed == 1
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _assert_live_polling_claim(
        row: TelegramPollingState,
        claim: TelegramPollingLease,
        clock: datetime,
    ) -> None:
        if (
            row.lease_owner != claim.worker_id
            or int(row.lease_token or 0) != claim.fencing_token
            or row.lease_expires_at is None
            or _naive_utc(row.lease_expires_at) <= clock
        ):
            raise TelegramPollingLeaseLost("Telegram polling lease is missing or expired")

    def polling_cursor(self, bot_fingerprint: object) -> int | None:
        scope = _fingerprint(bot_fingerprint)
        db = self._session_factory()
        try:
            row = self._ensure_state(db, scope)
            value = row.next_offset
            db.commit()
            return int(value) if value is not None else None
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def advance_polling_cursor(
        self,
        claim: TelegramPollingLease,
        next_offset: object,
        *,
        now: datetime | None = None,
    ) -> int:
        offset = _update_id(next_offset)
        db = self._session_factory()
        try:
            clock = _database_now(db, now)
            row = db.query(TelegramPollingState).filter(
                TelegramPollingState.bot_fingerprint == claim.bot_fingerprint
            ).with_for_update().one()
            self._assert_live_polling_claim(row, claim, clock)
            row.next_offset = max(int(row.next_offset or 0), offset)
            row.failure_update_id = None
            row.failure_attempts = 0
            row.version = int(row.version or 1) + 1
            row.updated_at = clock
            db.commit()
            return int(row.next_offset)
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def record_handler_failure(
        self,
        claim: TelegramPollingLease,
        *,
        update_id: object,
        error_type: object,
        max_attempts: int,
        now: datetime | None = None,
    ) -> TelegramFailureResolution:
        update = _update_id(update_id)
        threshold = max(1, min(int(max_attempts), 100))
        safe_error = _safe_error_type(error_type)
        db = self._session_factory()
        try:
            clock = _database_now(db, now)
            row = db.query(TelegramPollingState).filter(
                TelegramPollingState.bot_fingerprint == claim.bot_fingerprint
            ).with_for_update().one()
            self._assert_live_polling_claim(row, claim, clock)
            attempts = (
                int(row.failure_attempts or 0) + 1
                if row.failure_update_id == update
                else 1
            )
            row.failure_update_id = update
            row.failure_attempts = attempts
            resolved = attempts >= threshold
            if resolved:
                dead = db.query(TelegramDeadLetter).filter(
                    TelegramDeadLetter.bot_fingerprint == claim.bot_fingerprint,
                    TelegramDeadLetter.update_id == update,
                ).one_or_none()
                if dead is None:
                    db.add(TelegramDeadLetter(
                        id=str(uuid.uuid4()),
                        bot_fingerprint=claim.bot_fingerprint,
                        update_id=update,
                        error_type=safe_error,
                        attempts=attempts,
                        failed_at=clock,
                    ))
                row.next_offset = max(int(row.next_offset or 0), update + 1)
                row.failure_update_id = None
                row.failure_attempts = 0
            row.version = int(row.version or 1) + 1
            row.updated_at = clock
            db.commit()
            return TelegramFailureResolution(
                attempts=attempts,
                resolved=resolved,
                next_offset=int(row.next_offset) if row.next_offset is not None else None,
            )
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def dead_letter_count(self, bot_fingerprint: object) -> int:
        scope = _fingerprint(bot_fingerprint)
        db = self._session_factory()
        try:
            return int(db.query(TelegramDeadLetter).filter(
                TelegramDeadLetter.bot_fingerprint == scope
            ).count())
        finally:
            db.close()

    @staticmethod
    def _account_exists(db, owner_account_id: str) -> bool:
        if not owner_account_id:
            return True
        return db.query(Account.id).filter(
            Account.id == owner_account_id,
            Account.status == "active",
        ).first() is not None

    @staticmethod
    def _inbound_record(row: TelegramInboundUpdate) -> dict[str, Any]:
        chat = str(row.chat_id or "")
        if not chat:
            raise TelegramInboundLedgerError(
                "Telegram inbound chat binding could not be decrypted"
            )
        return {
            "chat_id": chat,
            "reply_text": str(row.reply_text or ""),
            "owner_account_id": str(row.owner_account_id or ""),
            "status": str(row.status or ""),
        }

    @staticmethod
    def _same_inbound_identity(
        row: TelegramInboundUpdate,
        *,
        chat_id: str,
        owner_account_id: str,
    ) -> bool:
        return (
            str(row.chat_id or "") == chat_id
            and str(row.owner_account_id or "") == owner_account_id
        )

    def load_inbound_record(
        self, bot_fingerprint: object, update_id: object
    ) -> dict[str, Any] | None:
        scope = _fingerprint(bot_fingerprint)
        update = _update_id(update_id)
        db = self._session_factory()
        try:
            row = db.query(TelegramInboundUpdate).filter(
                TelegramInboundUpdate.bot_fingerprint == scope,
                TelegramInboundUpdate.update_id == update,
            ).one_or_none()
            return self._inbound_record(row) if row is not None else None
        finally:
            db.close()

    def claim_inbound_processing(
        self,
        *,
        bot_fingerprint: object,
        update_id: object,
        chat_id: object,
        owner_account_id: object = "",
        worker_id: object,
        lease: timedelta = INBOUND_PROCESSING_LEASE,
        now: datetime | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        scope = _fingerprint(bot_fingerprint)
        update = _update_id(update_id)
        chat = _private_text(chat_id, field="chat binding", required=True)
        owner = str(owner_account_id or "").strip()
        worker = _worker_id(worker_id)
        duration = max(timedelta(seconds=5), lease)
        db = self._session_factory()
        try:
            clock = _database_now(db, now)
            self._ensure_state(db, scope)
            if not self._account_exists(db, owner):
                raise TelegramInboundLedgerError(
                    "Telegram inbound owner account is unavailable"
                )
            token = _claim_token()
            row_id = str(uuid.uuid4())
            inserted = False
            try:
                with db.begin_nested():
                    db.add(TelegramInboundUpdate(
                        id=row_id,
                        bot_fingerprint=scope,
                        update_id=update,
                        chat_id=chat,
                        owner_account_id=owner or None,
                        status="processing",
                        processing_claim_digest=_claim_digest(
                            row_id, "processing", token
                        ),
                        processing_lease_expires_at=clock + duration,
                        version=1,
                    ))
                    db.flush()
                    inserted = True
            except IntegrityError:
                db.expire_all()
            if inserted:
                db.commit()
                return True, {
                    "chat_id": chat,
                    "reply_text": "",
                    "owner_account_id": owner,
                    "status": "processing",
                    "processing_claim_token": token,
                }

            row = db.query(TelegramInboundUpdate).filter(
                TelegramInboundUpdate.bot_fingerprint == scope,
                TelegramInboundUpdate.update_id == update,
            ).one()
            record = self._inbound_record(row)
            if row.status in {"delivered", "discarded"}:
                db.commit()
                return False, record
            if not self._same_inbound_identity(
                row, chat_id=chat, owner_account_id=owner
            ):
                row.status = "discarded"
                row.reply_text = None
                row.processing_claim_digest = None
                row.processing_lease_expires_at = None
                row.reply_claim_digest = None
                row.reply_lease_expires_at = None
                row.version = int(row.version or 1) + 1
                row.updated_at = clock
                db.commit()
                return False, {**record, "status": "discarded", "reply_text": ""}
            if row.status == "reply_pending":
                db.commit()
                return False, record
            if (
                row.processing_lease_expires_at is not None
                and _naive_utc(row.processing_lease_expires_at) > clock
            ):
                db.commit()
                return False, record

            previous_version = int(row.version or 1)
            token = _claim_token()
            changed = db.query(TelegramInboundUpdate).filter(
                TelegramInboundUpdate.id == row.id,
                TelegramInboundUpdate.status == "processing",
                TelegramInboundUpdate.version == previous_version,
                or_(
                    TelegramInboundUpdate.processing_lease_expires_at.is_(None),
                    TelegramInboundUpdate.processing_lease_expires_at <= clock,
                ),
            ).update({
                TelegramInboundUpdate.processing_claim_digest: _claim_digest(
                    row.id, "processing", token
                ),
                TelegramInboundUpdate.processing_lease_expires_at: clock + duration,
                TelegramInboundUpdate.version: previous_version + 1,
                TelegramInboundUpdate.updated_at: clock,
            }, synchronize_session=False)
            if changed != 1:
                db.rollback()
                return False, record
            db.commit()
            return True, {**record, "processing_claim_token": token}
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def store_inbound_reply(
        self,
        *,
        bot_fingerprint: object,
        update_id: object,
        chat_id: object,
        reply_text: object,
        owner_account_id: object = "",
        processing_claim_token: object,
        reply_lease: timedelta = REPLY_DELIVERY_LEASE,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        scope = _fingerprint(bot_fingerprint)
        update = _update_id(update_id)
        chat = _private_text(chat_id, field="chat binding", required=True)
        reply = _private_text(reply_text, field="reply", required=True)
        owner = str(owner_account_id or "").strip()
        processing_token = str(processing_claim_token or "")
        duration = max(timedelta(seconds=5), reply_lease)
        db = self._session_factory()
        try:
            clock = _database_now(db, now)
            row = db.query(TelegramInboundUpdate).filter(
                TelegramInboundUpdate.bot_fingerprint == scope,
                TelegramInboundUpdate.update_id == update,
            ).with_for_update().one_or_none()
            if row is None or not self._same_inbound_identity(
                row, chat_id=chat, owner_account_id=owner
            ):
                raise TelegramInboundConflict(
                    "Telegram update reply does not match its original principal"
                )
            if (
                row.status != "processing"
                or not processing_token
                or row.processing_claim_digest != _claim_digest(
                    row.id, "processing", processing_token
                )
                or row.processing_lease_expires_at is None
                or _naive_utc(row.processing_lease_expires_at) <= clock
            ):
                raise TelegramInboundInFlight()
            reply_token = _claim_token()
            row.reply_text = reply
            row.status = "reply_pending"
            row.processing_claim_digest = None
            row.processing_lease_expires_at = None
            row.reply_claim_digest = _claim_digest(
                row.id, "reply", reply_token
            )
            row.reply_lease_expires_at = clock + duration
            row.version = int(row.version or 1) + 1
            row.updated_at = clock
            db.commit()
            return {
                **self._inbound_record(row),
                "reply_claim_token": reply_token,
            }
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def claim_reply_delivery(
        self,
        *,
        bot_fingerprint: object,
        update_id: object,
        chat_id: object,
        owner_account_id: object = "",
        worker_id: object,
        lease: timedelta = REPLY_DELIVERY_LEASE,
        now: datetime | None = None,
    ) -> tuple[bool, dict[str, Any] | None]:
        # worker_id is deliberately validated and represented in the caller's
        # audit/debug context, while the database stores only the random claim
        # digest.  It never becomes an authorization identity.
        _worker_id(worker_id)
        scope = _fingerprint(bot_fingerprint)
        update = _update_id(update_id)
        chat = _private_text(chat_id, field="chat binding", required=True)
        owner = str(owner_account_id or "").strip()
        duration = max(timedelta(seconds=5), lease)
        db = self._session_factory()
        try:
            clock = _database_now(db, now)
            row = db.query(TelegramInboundUpdate).filter(
                TelegramInboundUpdate.bot_fingerprint == scope,
                TelegramInboundUpdate.update_id == update,
            ).one_or_none()
            if row is None:
                return False, None
            record = self._inbound_record(row)
            if row.status in {"delivered", "discarded"}:
                return False, record
            if not self._same_inbound_identity(
                row, chat_id=chat, owner_account_id=owner
            ):
                row.status = "discarded"
                row.reply_text = None
                row.processing_claim_digest = None
                row.processing_lease_expires_at = None
                row.reply_claim_digest = None
                row.reply_lease_expires_at = None
                row.version = int(row.version or 1) + 1
                row.updated_at = clock
                db.commit()
                return False, {**record, "status": "discarded", "reply_text": ""}
            if row.status != "reply_pending" or not str(row.reply_text or ""):
                return False, record
            if (
                row.reply_lease_expires_at is not None
                and _naive_utc(row.reply_lease_expires_at) > clock
            ):
                return False, record
            previous_version = int(row.version or 1)
            token = _claim_token()
            changed = db.query(TelegramInboundUpdate).filter(
                TelegramInboundUpdate.id == row.id,
                TelegramInboundUpdate.status == "reply_pending",
                TelegramInboundUpdate.version == previous_version,
                or_(
                    TelegramInboundUpdate.reply_lease_expires_at.is_(None),
                    TelegramInboundUpdate.reply_lease_expires_at <= clock,
                ),
            ).update({
                TelegramInboundUpdate.reply_claim_digest: _claim_digest(
                    row.id, "reply", token
                ),
                TelegramInboundUpdate.reply_lease_expires_at: clock + duration,
                TelegramInboundUpdate.version: previous_version + 1,
                TelegramInboundUpdate.updated_at: clock,
            }, synchronize_session=False)
            if changed != 1:
                db.rollback()
                return False, record
            db.commit()
            return True, {**record, "reply_claim_token": token}
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def mark_inbound_delivered(
        self,
        *,
        bot_fingerprint: object,
        update_id: object,
        reply_claim_token: object,
        now: datetime | None = None,
    ) -> bool:
        scope = _fingerprint(bot_fingerprint)
        update = _update_id(update_id)
        token = str(reply_claim_token or "")
        db = self._session_factory()
        try:
            clock = _database_now(db, now)
            row = db.query(TelegramInboundUpdate).filter(
                TelegramInboundUpdate.bot_fingerprint == scope,
                TelegramInboundUpdate.update_id == update,
            ).one_or_none()
            if row is None or not token:
                return False
            changed = db.query(TelegramInboundUpdate).filter(
                TelegramInboundUpdate.id == row.id,
                TelegramInboundUpdate.status == "reply_pending",
                TelegramInboundUpdate.reply_claim_digest == _claim_digest(
                    row.id, "reply", token
                ),
                TelegramInboundUpdate.reply_lease_expires_at > clock,
            ).update({
                TelegramInboundUpdate.status: "delivered",
                TelegramInboundUpdate.reply_claim_digest: None,
                TelegramInboundUpdate.reply_lease_expires_at: None,
                TelegramInboundUpdate.version: TelegramInboundUpdate.version + 1,
                TelegramInboundUpdate.updated_at: clock,
            }, synchronize_session=False)
            db.commit()
            return changed == 1
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def release_reply_delivery(
        self,
        *,
        bot_fingerprint: object,
        update_id: object,
        reply_claim_token: object,
        now: datetime | None = None,
    ) -> bool:
        """Release a known-failed send while preserving the durable reply."""

        scope = _fingerprint(bot_fingerprint)
        update = _update_id(update_id)
        token = str(reply_claim_token or "")
        db = self._session_factory()
        try:
            clock = _database_now(db, now)
            row = db.query(TelegramInboundUpdate).filter(
                TelegramInboundUpdate.bot_fingerprint == scope,
                TelegramInboundUpdate.update_id == update,
            ).one_or_none()
            if row is None or not token:
                return False
            changed = db.query(TelegramInboundUpdate).filter(
                TelegramInboundUpdate.id == row.id,
                TelegramInboundUpdate.status == "reply_pending",
                TelegramInboundUpdate.reply_claim_digest == _claim_digest(
                    row.id, "reply", token
                ),
            ).update({
                TelegramInboundUpdate.reply_claim_digest: None,
                TelegramInboundUpdate.reply_lease_expires_at: None,
                TelegramInboundUpdate.version: TelegramInboundUpdate.version + 1,
                TelegramInboundUpdate.updated_at: clock,
            }, synchronize_session=False)
            db.commit()
            return changed == 1
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _legacy_source_digest(paths: tuple[Path, ...]) -> str:
        digest = hashlib.sha256()
        for path in paths:
            digest.update(path.name.encode("utf-8"))
            try:
                info = path.lstat()
            except OSError:
                digest.update(b"\0missing\0")
                continue
            if not stat.S_ISREG(info.st_mode):
                digest.update(b"\0unsafe-file-type\0")
                continue
            digest.update(f"\0{info.st_size}\0".encode("ascii"))
            limit = (
                MAX_LEGACY_LEDGER_BYTES
                if path.suffix in {".db", ".sqlite", ".sqlite3"}
                else MAX_LEGACY_JSON_BYTES
            )
            if info.st_size > limit:
                digest.update(b"oversized")
                continue
            try:
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
            except OSError:
                digest.update(b"unreadable")
        return digest.hexdigest()

    @staticmethod
    def _read_legacy_json(path: Path) -> object | None:
        try:
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > MAX_LEGACY_JSON_BYTES
            ):
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return None

    @staticmethod
    def _legacy_inbound_rows(
        path: Path,
        *,
        legacy_fingerprint: str,
    ) -> list[dict[str, Any]]:
        try:
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_size > MAX_LEGACY_LEDGER_BYTES
            ):
                return []
            uri = f"file:{path.resolve().as_posix()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
        except (OSError, sqlite3.Error):
            return []
        try:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(telegram_inbound_ledger)")
            }
            required = {
                "bot_fingerprint", "update_id", "chat_id", "reply_text",
                "status", "updated_at",
            }
            if not required.issubset(columns):
                return []
            owner_expression = (
                "owner_account_id" if "owner_account_id" in columns else "''"
            )
            storage_expression = (
                "storage_version" if "storage_version" in columns else "1"
            )
            rows = conn.execute(
                f"""
                SELECT update_id, chat_id, reply_text, status, updated_at,
                       {owner_expression} AS owner_account_id,
                       {storage_expression} AS storage_version
                FROM telegram_inbound_ledger
                WHERE bot_fingerprint = ?
                ORDER BY update_id ASC
                LIMIT ?
                """,
                (legacy_fingerprint, MAX_LEGACY_INBOUND_ROWS + 1),
            ).fetchall()
            if len(rows) > MAX_LEGACY_INBOUND_ROWS:
                return []
            result: list[dict[str, Any]] = []
            for row in rows:
                encrypted = int(row["storage_version"] or 1) >= 2
                def decoded(value: object) -> str:
                    text = str(value or "")
                    return decrypt(text) if encrypted else text
                try:
                    update = _update_id(row["update_id"])
                    chat = _private_text(
                        decoded(row["chat_id"]),
                        field="legacy chat binding", required=True,
                    )
                    reply = _private_text(
                        decoded(row["reply_text"]), field="legacy reply"
                    )
                    owner = str(decoded(row["owner_account_id"])).strip()
                    status = str(row["status"] or "")
                    if status not in {
                        "processing", "reply_pending", "delivered", "discarded"
                    }:
                        continue
                except (TypeError, ValueError):
                    continue
                result.append({
                    "update_id": update,
                    "chat_id": chat,
                    "reply_text": reply,
                    "owner_account_id": owner,
                    "status": status,
                })
            return result
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def adopt_legacy_sidecars(
        self,
        *,
        bot_fingerprint: object,
        bot_token: object,
        data_dir: str | Path,
    ) -> TelegramLegacyImportResult:
        """Transactionally adopt exact-bot legacy state, otherwise ignore it."""

        scope = _fingerprint(bot_fingerprint)
        token = str(bot_token or "")
        directory = Path(data_dir)
        offset_path = directory / "telegram_polling_state.json"
        dead_path = directory / "telegram_dead_letters.json"
        inbound_path = directory / "telegram_inbound_ledger.sqlite3"
        paths = (offset_path, dead_path, inbound_path)
        source_sha256 = self._legacy_source_digest(paths)
        legacy_full = hashlib.sha256(token.encode("utf-8")).hexdigest()
        legacy_short = legacy_full[:24]

        db = self._session_factory()
        try:
            clock = _database_now(db)
            state = self._ensure_state(db, scope)
            existing = db.query(TelegramRuntimeImportRun).filter(
                TelegramRuntimeImportRun.bot_fingerprint == scope,
                TelegramRuntimeImportRun.source_kind == LEGACY_SOURCE_KIND,
            ).one_or_none()
            if existing is not None and existing.state == "completed":
                details = existing.details if isinstance(existing.details, dict) else {}
                db.commit()
                return TelegramLegacyImportResult(
                    state="already_imported",
                    offset_imported=bool(details.get("offset_imported")),
                    dead_letters_imported=int(details.get("dead_letters_imported") or 0),
                    inbound_imported=int(details.get("inbound_imported") or 0),
                    inbound_discarded=int(details.get("inbound_discarded") or 0),
                )

            offset_imported = False
            legacy_offset = self._read_legacy_json(offset_path)
            offset_scope_matches = bool(
                isinstance(legacy_offset, dict)
                and legacy_offset.get("bot_fingerprint") == legacy_full
            )
            if isinstance(legacy_offset, dict):
                value = legacy_offset.get("offset")
                if (
                    offset_scope_matches
                    and isinstance(value, int)
                    and value >= 0
                ):
                    state.next_offset = max(int(state.next_offset or 0), value)
                    offset_imported = True

            dead_letters_imported = 0
            legacy_dead = self._read_legacy_json(dead_path)
            if (
                offset_scope_matches
                and isinstance(legacy_dead, list)
                and len(legacy_dead) <= 100
            ):
                for item in legacy_dead:
                    if not isinstance(item, dict):
                        continue
                    try:
                        update = _update_id(item.get("update_id"))
                        attempts = max(1, min(int(item.get("attempts") or 1), 100))
                    except (TypeError, ValueError):
                        continue
                    if db.query(TelegramDeadLetter.id).filter(
                        TelegramDeadLetter.bot_fingerprint == scope,
                        TelegramDeadLetter.update_id == update,
                    ).first() is not None:
                        continue
                    failed_at = clock
                    try:
                        stamp = float(item.get("failed_at"))
                        failed_at = datetime.fromtimestamp(stamp, tz=timezone.utc).replace(
                            tzinfo=None
                        )
                    except (TypeError, ValueError, OSError, OverflowError):
                        pass
                    db.add(TelegramDeadLetter(
                        id=str(uuid.uuid4()),
                        bot_fingerprint=scope,
                        update_id=update,
                        error_type=_safe_error_type(item.get("error_type")),
                        attempts=attempts,
                        failed_at=failed_at,
                    ))
                    dead_letters_imported += 1

            inbound_imported = 0
            inbound_discarded = 0
            for item in self._legacy_inbound_rows(
                inbound_path, legacy_fingerprint=legacy_short
            ):
                if db.query(TelegramInboundUpdate.id).filter(
                    TelegramInboundUpdate.bot_fingerprint == scope,
                    TelegramInboundUpdate.update_id == item["update_id"],
                ).first() is not None:
                    continue
                owner = str(item["owner_account_id"] or "")
                owner_valid = self._account_exists(db, owner)
                status = str(item["status"])
                if owner and not owner_valid:
                    status = "discarded"
                    inbound_discarded += 1
                reply = str(item["reply_text"] or "")
                if status == "reply_pending" and not reply:
                    status = "discarded"
                    inbound_discarded += 1
                db.add(TelegramInboundUpdate(
                    id=str(uuid.uuid4()),
                    bot_fingerprint=scope,
                    update_id=int(item["update_id"]),
                    chat_id=str(item["chat_id"]),
                    owner_account_id=owner if owner and owner_valid else None,
                    reply_text=reply if status == "reply_pending" else None,
                    status=status,
                    processing_claim_digest=None,
                    processing_lease_expires_at=None,
                    reply_claim_digest=None,
                    reply_lease_expires_at=None,
                    version=1,
                ))
                inbound_imported += 1

            details = {
                "offset_imported": offset_imported,
                "dead_letters_imported": dead_letters_imported,
                "inbound_imported": inbound_imported,
                "inbound_discarded": inbound_discarded,
                "source_preserved": True,
            }
            if existing is None:
                db.add(TelegramRuntimeImportRun(
                    id=str(uuid.uuid4()),
                    bot_fingerprint=scope,
                    source_kind=LEGACY_SOURCE_KIND,
                    state="completed",
                    source_sha256=source_sha256,
                    details=details,
                    completed_at=clock,
                ))
            else:
                existing.state = "completed"
                existing.source_sha256 = source_sha256
                existing.details = details
                existing.completed_at = clock
                existing.updated_at = clock
            state.version = int(state.version or 1) + 1
            state.updated_at = clock
            db.commit()
            return TelegramLegacyImportResult(
                state="completed",
                offset_imported=offset_imported,
                dead_letters_imported=dead_letters_imported,
                inbound_imported=inbound_imported,
                inbound_discarded=inbound_discarded,
            )
        except IntegrityError:
            # A second replica may win the import marker.  Read its committed
            # result rather than applying a second projection.
            db.rollback()
            retry = db.query(TelegramRuntimeImportRun).filter(
                TelegramRuntimeImportRun.bot_fingerprint == scope,
                TelegramRuntimeImportRun.source_kind == LEGACY_SOURCE_KIND,
                TelegramRuntimeImportRun.state == "completed",
            ).one_or_none()
            if retry is None:
                raise
            details = retry.details if isinstance(retry.details, dict) else {}
            return TelegramLegacyImportResult(
                state="already_imported",
                offset_imported=bool(details.get("offset_imported")),
                dead_letters_imported=int(details.get("dead_letters_imported") or 0),
                inbound_imported=int(details.get("inbound_imported") or 0),
                inbound_discarded=int(details.get("inbound_discarded") or 0),
            )
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


telegram_runtime_authority = TelegramRuntimeAuthority()
