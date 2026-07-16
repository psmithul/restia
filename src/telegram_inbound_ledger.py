"""Durable Telegram inbound processing/reply ledger."""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


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
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (bot_fingerprint, update_id)
        )
        """
    )
    try:
        os.chmod(db_path, 0o600)
    except OSError:
        pass
    return conn


def load_inbound_record(path: str | Path, fingerprint: str, update_id: int) -> dict | None:
    conn = _connect(path)
    try:
        row = conn.execute(
            """
            SELECT chat_id, reply_text, status FROM telegram_inbound_ledger
            WHERE bot_fingerprint = ? AND update_id = ?
            """,
            (str(fingerprint), int(update_id)),
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn.close()


def claim_inbound_processing(
    path: str | Path,
    *,
    fingerprint: str,
    update_id: int,
    chat_id: str,
    lease_seconds: int = 5 * 60,
) -> tuple[bool, dict | None]:
    """Claim processing once; a crashed claim becomes retryable after its lease."""

    now = datetime.now(timezone.utc)
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT chat_id, reply_text, status, updated_at
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
                    bot_fingerprint, update_id, chat_id, reply_text, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, '', 'processing', ?, ?)
                """,
                (str(fingerprint), int(update_id), str(chat_id), stamp, stamp),
            )
            acquired = True
            record = None
        else:
            record = dict(row)
            try:
                updated = datetime.fromisoformat(str(row["updated_at"] or ""))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
            except ValueError:
                updated = now - timedelta(days=1)
            if row["status"] == "processing" and updated <= now - timedelta(seconds=lease_seconds):
                conn.execute(
                    """
                    UPDATE telegram_inbound_ledger SET updated_at = ?, chat_id = ?
                    WHERE bot_fingerprint = ? AND update_id = ? AND status = 'processing'
                    """,
                    (now.isoformat(), str(chat_id), str(fingerprint), int(update_id)),
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
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO telegram_inbound_ledger(
                bot_fingerprint, update_id, chat_id, reply_text, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'reply_pending', ?, ?)
            ON CONFLICT(bot_fingerprint, update_id) DO UPDATE SET
                reply_text = CASE
                    WHEN telegram_inbound_ledger.reply_text = '' THEN excluded.reply_text
                    ELSE telegram_inbound_ledger.reply_text
                END,
                status = CASE
                    WHEN telegram_inbound_ledger.status = 'delivered' THEN 'delivered'
                    ELSE 'reply_pending'
                END,
                updated_at = excluded.updated_at
            """,
            (
                str(fingerprint), int(update_id), str(chat_id), str(reply_text),
                now, now,
            ),
        )
        # Keep delivered metadata bounded while never evicting a pending reply.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        conn.execute(
            "DELETE FROM telegram_inbound_ledger WHERE status = 'delivered' AND updated_at < ?",
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
