from __future__ import annotations

import sqlite3

from src.telegram_inbound_ledger import (
    claim_inbound_processing,
    load_inbound_record,
    store_inbound_reply,
)


def _create_legacy_row(path, *, status: str) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE telegram_inbound_ledger (
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
        conn.execute(
            """
            INSERT INTO telegram_inbound_ledger(
                bot_fingerprint, update_id, chat_id, reply_text, status,
                created_at, updated_at
            ) VALUES ('bot', 7, '111-private', 'legacy private reply', ?,
                      '2000-01-01T00:00:00+00:00',
                      '2000-01-01T00:00:00+00:00')
            """,
            (status,),
        )
        conn.commit()


def test_new_inbound_claim_encrypts_chat_owner_and_reply(tmp_path):
    path = tmp_path / "inbound.sqlite3"
    acquired, _ = claim_inbound_processing(
        path,
        fingerprint="bot",
        update_id=7,
        chat_id="111-private",
        owner_account_id="account-private",
    )
    assert acquired is True
    store_inbound_reply(
        path,
        fingerprint="bot",
        update_id=7,
        chat_id="111-private",
        reply_text="private durable answer",
        owner_account_id="account-private",
    )

    assert load_inbound_record(path, "bot", 7) == {
        "chat_id": "111-private",
        "reply_text": "private durable answer",
        "owner_account_id": "account-private",
        "status": "reply_pending",
    }
    with sqlite3.connect(path) as conn:
        raw = conn.execute(
            """
            SELECT chat_id, reply_text, owner_account_id, storage_version
            FROM telegram_inbound_ledger
            """
        ).fetchone()
    assert all(str(value).startswith("enc:c1:") for value in raw[:3])
    rendering = " ".join(str(value) for value in raw)
    assert "111-private" not in rendering
    assert "private durable answer" not in rendering
    assert "account-private" not in rendering
    assert raw[3] == 2


def test_legacy_delivered_row_is_encrypted_and_still_deduplicates(tmp_path):
    path = tmp_path / "legacy-delivered.sqlite3"
    _create_legacy_row(path, status="delivered")

    acquired, record = claim_inbound_processing(
        path,
        fingerprint="bot",
        update_id=7,
        chat_id="111-private",
        owner_account_id="current-account",
    )

    assert acquired is False
    assert record["status"] == "delivered"
    assert record["chat_id"] == "111-private"
    assert record["reply_text"] == "legacy private reply"
    assert record["owner_account_id"] == ""
    with sqlite3.connect(path) as conn:
        raw = conn.execute(
            """
            SELECT chat_id, reply_text, owner_account_id, storage_version
            FROM telegram_inbound_ledger
            """
        ).fetchone()
    assert raw[0].startswith("enc:c1:")
    assert raw[1].startswith("enc:c1:")
    assert raw[2] == ""
    assert raw[3] == 2


def test_legacy_inflight_row_is_terminally_discarded_not_rebound(tmp_path):
    path = tmp_path / "legacy-processing.sqlite3"
    _create_legacy_row(path, status="processing")

    acquired, record = claim_inbound_processing(
        path,
        fingerprint="bot",
        update_id=7,
        chat_id="111-private",
        owner_account_id="current-account",
        lease_seconds=1,
    )

    assert acquired is False
    assert record["status"] == "discarded"
    assert record["reply_text"] == ""
    assert load_inbound_record(path, "bot", 7)["status"] == "discarded"
