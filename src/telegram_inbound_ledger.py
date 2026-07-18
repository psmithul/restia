"""Retired Telegram inbound sidecar codec used only for legacy adoption.

Production polling, webhook processing, and reply delivery use the configured
SQLAlchemy database through :mod:`src.telegram_delivery`.  This module remains
readable so the bounded one-time importer can preserve older installations and
so downgrade/privacy fixtures can validate the historical envelope format; no
runtime route writes it.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.secret_storage import decrypt, encrypt_plaintext


_STORAGE_VERSION = 2


class TelegramInboundLedgerError(RuntimeError):
    """The durable inbound record is corrupt or internally inconsistent."""


class TelegramInboundConflict(TelegramInboundLedgerError):
    """A replay does not match the immutable first-claim principal evidence."""


class TelegramInboundInFlight(RuntimeError):
    """A durable processing lease exists; retry without poison accounting."""

    def __init__(self, retry_after_seconds: int = 30):
        super().__init__("Telegram update is already being processed")
        self.retry_after_seconds = max(1, int(retry_after_seconds))


class TelegramReplyPending(RuntimeError):
    """Processing is durable but the Telegram reply still needs delivery."""

    def __init__(self, retry_after_seconds: int = 15):
        super().__init__("Telegram reply delivery is pending")
        self.retry_after_seconds = max(1, int(retry_after_seconds))


def bot_fingerprint(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:24]


def _connect(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS telegram_inbound_ledger (
            bot_fingerprint TEXT NOT NULL,
            update_id INTEGER NOT NULL,
            chat_id TEXT NOT NULL,
            reply_text TEXT NOT NULL DEFAULT '',
            owner_account_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            storage_version INTEGER NOT NULL DEFAULT 2,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (bot_fingerprint, update_id)
        )
        """
    )
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(telegram_inbound_ledger)")
    }
    if "owner_account_id" not in columns:
        conn.execute(
            "ALTER TABLE telegram_inbound_ledger "
            "ADD COLUMN owner_account_id TEXT NOT NULL DEFAULT ''"
        )
    if "storage_version" not in columns:
        # Existing chat/reply values are legacy plaintext until the bounded
        # migration below rewrites them in the same connection.
        conn.execute(
            "ALTER TABLE telegram_inbound_ledger "
            "ADD COLUMN storage_version INTEGER NOT NULL DEFAULT 1"
        )
    _encrypt_legacy_rows(conn)
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    return conn


def _encrypt_legacy_rows(conn: sqlite3.Connection) -> None:
    """Upgrade legacy plaintext payload columns before exposing any record."""

    rows = conn.execute(
        """
        SELECT rowid, chat_id, reply_text, owner_account_id
        FROM telegram_inbound_ledger
        WHERE storage_version < ?
        """,
        (_STORAGE_VERSION,),
    ).fetchall()
    if not rows:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in rows:
            conn.execute(
                """
                UPDATE telegram_inbound_ledger
                SET chat_id = ?, reply_text = ?, owner_account_id = ?,
                    storage_version = ?
                WHERE rowid = ? AND storage_version < ?
                """,
                (
                    encrypt_plaintext(str(row[1] or "")),
                    encrypt_plaintext(str(row[2] or "")),
                    encrypt_plaintext(str(row[3] or "")),
                    _STORAGE_VERSION,
                    int(row[0]),
                    _STORAGE_VERSION,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _decode_private(value: object, *, field: str, required: bool = False) -> str:
    stored = str(value or "")
    plaintext = decrypt(stored)
    # New encrypted empty values are stored as the empty string, so any
    # non-empty envelope that decrypts to empty is corruption/wrong-key and
    # must not be confused with an unlinked principal or absent reply.
    if (stored and not plaintext) or (required and not plaintext):
        raise TelegramInboundLedgerError(
            f"Telegram inbound {field} could not be decrypted"
        )
    return plaintext


def _decoded_record(row: sqlite3.Row) -> dict:
    return {
        "chat_id": _decode_private(row["chat_id"], field="chat binding", required=True),
        "reply_text": _decode_private(row["reply_text"], field="reply"),
        "owner_account_id": _decode_private(
            row["owner_account_id"], field="owner binding"
        ),
        "status": str(row["status"] or ""),
    }


def _assert_same_claim(
    record: dict,
    *,
    chat_id: str,
    owner_account_id: str,
) -> None:
    if str(record.get("chat_id") or "") != str(chat_id):
        raise TelegramInboundConflict(
            "Telegram update replay does not match its original chat"
        )
    if str(record.get("owner_account_id") or "") != str(owner_account_id or ""):
        raise TelegramInboundConflict(
            "Telegram update replay does not match its original account"
        )


def _same_claim(record: dict, *, chat_id: str, owner_account_id: str) -> bool:
    return (
        str(record.get("chat_id") or "") == str(chat_id)
        and str(record.get("owner_account_id") or "")
        == str(owner_account_id or "")
    )


def load_inbound_record(path: str | Path, fingerprint: str, update_id: int) -> dict | None:
    conn = _connect(path)
    try:
        row = conn.execute(
            """
            SELECT chat_id, reply_text, owner_account_id, status
            FROM telegram_inbound_ledger
            WHERE bot_fingerprint = ? AND update_id = ?
            """,
            (str(fingerprint), int(update_id)),
        ).fetchone()
        return _decoded_record(row) if row is not None else None
    finally:
        conn.close()


def claim_inbound_processing(
    path: str | Path,
    *,
    fingerprint: str,
    update_id: int,
    chat_id: str,
    owner_account_id: str = "",
    lease_seconds: int = 5 * 60,
) -> tuple[bool, dict | None]:
    """Claim processing once; a crashed claim becomes retryable after its lease."""

    now = datetime.now(timezone.utc)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT chat_id, reply_text, owner_account_id, status, updated_at
            FROM telegram_inbound_ledger
            WHERE bot_fingerprint = ? AND update_id = ?
            """,
            (str(fingerprint), int(update_id)),
        ).fetchone()
        acquired = False
        if row is None:
            stamp = now.isoformat()
            conn.execute(
                """
                INSERT INTO telegram_inbound_ledger(
                    bot_fingerprint, update_id, chat_id, reply_text,
                    owner_account_id, status, storage_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, '', ?, 'processing', ?, ?, ?)
                """,
                (
                    str(fingerprint),
                    int(update_id),
                    encrypt_plaintext(str(chat_id)),
                    encrypt_plaintext(str(owner_account_id or "")),
                    _STORAGE_VERSION,
                    stamp,
                    stamp,
                ),
            )
            acquired = True
            record = None
        else:
            record = _decoded_record(row)
            # Terminal rows never run owner-scoped work again. In particular,
            # a pre-encryption delivered row has no owner binding, but must
            # continue to deduplicate its already-resolved Telegram update.
            if record["status"] in {"delivered", "discarded"}:
                conn.commit()
                return False, record
            if not _same_claim(
                record,
                chat_id=str(chat_id),
                owner_account_id=str(owner_account_id or ""),
            ):
                # A relink/removal between crash and retry must neither project
                # into the new account nor keep a webhook in an infinite 500
                # loop. Legacy in-flight rows also have an unknowable owner;
                # discard them instead of guessing. The already-committed Life
                # projection, if any, remains owned by the first principal.
                conn.execute(
                    """
                    UPDATE telegram_inbound_ledger
                    SET status = 'discarded', reply_text = '', updated_at = ?
                    WHERE bot_fingerprint = ? AND update_id = ?
                      AND status NOT IN ('delivered', 'discarded')
                    """,
                    (now.isoformat(), str(fingerprint), int(update_id)),
                )
                conn.commit()
                return False, {
                    **record,
                    "reply_text": "",
                    "status": "discarded",
                }
            try:
                updated = datetime.fromisoformat(str(row["updated_at"] or ""))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
            except ValueError:
                updated = now - timedelta(days=1)
            if row["status"] == "processing" and updated <= now - timedelta(seconds=lease_seconds):
                conn.execute(
                    """
                    UPDATE telegram_inbound_ledger SET updated_at = ?
                    WHERE bot_fingerprint = ? AND update_id = ? AND status = 'processing'
                    """,
                    (now.isoformat(), str(fingerprint), int(update_id)),
                )
                acquired = True
                record = None
        conn.commit()
        return acquired, record
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def store_inbound_reply(
    path: str | Path,
    *,
    fingerprint: str,
    update_id: int,
    chat_id: str,
    reply_text: str,
    owner_account_id: str = "",
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT chat_id, reply_text, owner_account_id, status
            FROM telegram_inbound_ledger
            WHERE bot_fingerprint = ? AND update_id = ?
            """,
            (str(fingerprint), int(update_id)),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO telegram_inbound_ledger(
                    bot_fingerprint, update_id, chat_id, reply_text,
                    owner_account_id, status, storage_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'reply_pending', ?, ?, ?)
                """,
                (
                    str(fingerprint),
                    int(update_id),
                    encrypt_plaintext(str(chat_id)),
                    encrypt_plaintext(str(reply_text)),
                    encrypt_plaintext(str(owner_account_id or "")),
                    _STORAGE_VERSION,
                    now,
                    now,
                ),
            )
        else:
            record = _decoded_record(row)
            _assert_same_claim(
                record,
                chat_id=str(chat_id),
                owner_account_id=str(owner_account_id or ""),
            )
            stored_reply = str(row["reply_text"] or "")
            if not record["reply_text"]:
                stored_reply = encrypt_plaintext(str(reply_text))
            next_status = (
                record["status"]
                if record["status"] in {"delivered", "discarded"}
                else "reply_pending"
            )
            conn.execute(
                """
                UPDATE telegram_inbound_ledger
                SET reply_text = ?, status = ?, updated_at = ?
                WHERE bot_fingerprint = ? AND update_id = ?
                """,
                (
                    stored_reply,
                    next_status,
                    now,
                    str(fingerprint),
                    int(update_id),
                ),
            )
        # Keep delivered metadata bounded while never evicting a pending reply.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        conn.execute(
            "DELETE FROM telegram_inbound_ledger "
            "WHERE status IN ('delivered', 'discarded') AND updated_at < ?",
            (cutoff,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_inbound_delivered(path: str | Path, fingerprint: str, update_id: int) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(path)
    try:
        cursor = conn.execute(
            """
            UPDATE telegram_inbound_ledger
            SET status = 'delivered', updated_at = ?
            WHERE bot_fingerprint = ? AND update_id = ? AND status = 'reply_pending'
            """,
            (now, str(fingerprint), int(update_id)),
        )
        return cursor.rowcount == 1
    finally:
        conn.close()
