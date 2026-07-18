from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    AuthIdentity,
    Base,
    EmailLifeProjection,
    EmailLifeProjectionImportRun,
    LifeSource,
)
from src.email_life_projection_ledger import (
    EmailLifeProjectionLedgerError,
    drain_email_life_projections,
    enqueue_email_life_headers,
    ensure_email_life_projection_schema,
)


@pytest.fixture()
def projection_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'main.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(
        autocommit=False, autoflush=False, bind=engine,
    )
    db = factory()
    db.add(Account(id="account-alice", username="alice", status="active"))
    db.commit()
    db.close()
    try:
        yield engine, factory, tmp_path
    finally:
        engine.dispose()


def _header(uid="1", **overrides):
    row = {
        "uid": uid,
        "message_id": f"<{uid}@example.test>",
        "references": [],
        "in_reply_to": "",
        "subject": f"Message {uid}",
        "from_name": "Alex",
        "from_address": "alex@example.test",
        "date": "2026-07-17T09:00:00+00:00",
        "date_epoch": 1784278800,
    }
    row.update(overrides)
    return row


def _enqueue(factory, row, *, owner="alice"):
    return enqueue_email_life_headers(
        owner=owner,
        account_key="mail-a",
        folder="INBOX",
        emails=[row],
        session_factory=factory,
    )


def _create_index(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE email_message_index (
                owner TEXT NOT NULL DEFAULT '',
                account_key TEXT NOT NULL DEFAULT '',
                folder TEXT NOT NULL,
                uid TEXT NOT NULL,
                message_id TEXT,
                subject TEXT,
                from_name TEXT,
                from_address TEXT,
                to_text TEXT,
                cc_text TEXT,
                date_iso TEXT,
                date_display TEXT,
                date_epoch REAL DEFAULT 0,
                size INTEGER DEFAULT 0,
                flags TEXT DEFAULT '',
                has_attachments INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL,
                projection_payload_ciphertext TEXT,
                PRIMARY KEY (owner, account_key, folder, uid)
            )
            """
        )


def _insert_index(
    path,
    row,
    *,
    updated_at="2026-07-17T09:00:00Z",
    encrypted_recovery=False,
):
    from src.email_life_projection_ledger import (
        encode_email_life_projection_recovery_payload,
    )

    recovery = (
        encode_email_life_projection_recovery_payload(row)
        if encrypted_recovery else None
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO email_message_index(
                owner, account_key, folder, uid, message_id, subject,
                from_name, from_address, date_iso, date_epoch, updated_at,
                projection_payload_ciphertext
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner, account_key, folder, uid) DO UPDATE SET
                message_id=excluded.message_id,
                subject=excluded.subject,
                from_name=excluded.from_name,
                from_address=excluded.from_address,
                date_iso=excluded.date_iso,
                date_epoch=excluded.date_epoch,
                updated_at=excluded.updated_at,
                projection_payload_ciphertext=excluded.projection_payload_ciphertext
            """,
            (
                "alice", "mail-a", "INBOX", row["uid"], row["message_id"],
                row["subject"], row["from_name"], row["from_address"],
                row["date"], row["date_epoch"], updated_at, recovery,
            ),
        )


def test_enqueue_uses_account_id_and_keeps_subject_and_thread_ids_encrypted(
    projection_env,
):
    engine, factory, _tmp_path = projection_env
    secret = "Acquisition codename violet-orchid-771"
    inserted = _enqueue(factory, _header(
        subject=secret,
        references=["<private-root@example.test>"],
        in_reply_to="<private-parent@example.test>",
    ))
    assert inserted == 1

    db = factory()
    row = db.query(EmailLifeProjection).one()
    assert row.owner_id == "account-alice"
    assert row.payload["subject"] == secret
    assert row.payload["references"] == ["<private-root@example.test>"]
    db.close()

    raw = engine.raw_connection()
    try:
        stored = raw.cursor().execute(
            "SELECT owner_id, payload, header_sha256 FROM "
            "email_life_projection_ledger"
        ).fetchone()
    finally:
        raw.close()
    rendering = " ".join(str(value) for value in stored)
    assert stored[0] == "account-alice"
    assert secret not in rendering
    assert "private-root" not in rendering
    assert "private-parent" not in rendering
    assert "enc:c1:" in rendering


def test_completed_marker_is_idempotent_and_payload_is_cleared(projection_env):
    engine, factory, _tmp_path = projection_env
    header = _header(uid="12")
    assert _enqueue(factory, header) == 1
    calls = []
    first = drain_email_life_projections(
        ingest=lambda **kwargs: calls.append(kwargs),
        owner="alice",
        account_key="mail-a",
        folder="INBOX",
        session_factory=factory,
    )
    assert first.completed == 1
    assert _enqueue(factory, header) == 0
    second = drain_email_life_projections(
        ingest=lambda **kwargs: calls.append("duplicate"),
        owner="alice",
        account_key="mail-a",
        folder="INBOX",
        session_factory=factory,
    )
    assert second.attempted == 0
    assert len(calls) == 1
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT state, payload, attempt_count, version "
            "FROM email_life_projection_ledger"
        )).one() == ("completed", None, 1, 3)


def test_projection_failure_uses_safe_code_and_exponential_retry(projection_env):
    _engine, factory, _tmp_path = projection_env
    _enqueue(factory, _header())
    secret_error = "password=never-store-this subject=private"
    first = drain_email_life_projections(
        ingest=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError(secret_error)
        ),
        owner="alice",
        account_key="mail-a",
        folder="INBOX",
        retry_delay_seconds=1,
        session_factory=factory,
    )
    assert first.failed == 1
    db = factory()
    row = db.query(EmailLifeProjection).one()
    first_retry = row.next_attempt_at
    assert row.state == "failed"
    assert row.last_error_code == "projection_failed"
    assert secret_error not in repr(row.__dict__)
    row.next_attempt_at = datetime(2000, 1, 1)
    db.commit()
    db.close()

    second = drain_email_life_projections(
        ingest=lambda **_kwargs: (_ for _ in ()).throw(ValueError("other")),
        owner="account-alice",
        account_key="mail-a",
        folder="INBOX",
        retry_delay_seconds=1,
        session_factory=factory,
    )
    assert second.failed == 1
    db = factory()
    row = db.query(EmailLifeProjection).one()
    assert row.attempt_count == 2
    assert row.next_attempt_at > first_retry
    assert (row.next_attempt_at - row.updated_at).total_seconds() >= 2
    db.close()


def test_claim_commits_before_ingestion_and_competing_worker_is_fenced(
    projection_env,
):
    _engine, factory, _tmp_path = projection_env
    _enqueue(factory, _header())
    entered = threading.Event()
    release = threading.Event()
    calls = []
    results = []

    def slow_ingest(**_kwargs):
        # A separate SQL transaction can read and write while the external Life
        # operation is in flight; the projection claimant holds no DB session.
        db = factory()
        assert db.query(EmailLifeProjection).one().state == "processing"
        db.query(Account).filter(Account.id == "account-alice").update({
            Account.display_name: "Alice",
        })
        db.commit()
        db.close()
        calls.append("first")
        entered.set()
        assert release.wait(timeout=5)

    worker = threading.Thread(target=lambda: results.append(
        drain_email_life_projections(
            ingest=slow_ingest,
            owner="alice",
            account_key="mail-a",
            folder="INBOX",
            limit=1,
            session_factory=factory,
        )
    ))
    worker.start()
    assert entered.wait(timeout=5)
    competing = drain_email_life_projections(
        ingest=lambda **_kwargs: calls.append("second"),
        owner="alice",
        account_key="mail-a",
        folder="INBOX",
        limit=1,
        session_factory=factory,
    )
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert competing.attempted == 0
    assert calls == ["first"]
    assert results[0].completed == 1


def test_expired_lease_is_reclaimed_with_new_cas_version(projection_env):
    _engine, factory, _tmp_path = projection_env
    _enqueue(factory, _header())
    db = factory()
    row = db.query(EmailLifeProjection).one()
    row.state = "processing"
    row.claim_token_digest = "a" * 64
    row.claimed_at = datetime.utcnow() - timedelta(hours=1)
    row.lease_expires_at = datetime.utcnow() - timedelta(minutes=30)
    row.version = 7
    db.commit()
    db.close()

    result = drain_email_life_projections(
        ingest=lambda **_kwargs: None,
        owner="alice",
        account_key="mail-a",
        folder="INBOX",
        lease_seconds=1,
        session_factory=factory,
    )
    assert result.completed == 1
    db = factory()
    row = db.query(EmailLifeProjection).one()
    assert row.state == "completed"
    assert row.version == 9
    db.close()


def test_owner_rename_during_claim_fails_closed_without_cross_account_projection(
    projection_env,
):
    _engine, factory, _tmp_path = projection_env
    from src.life_ingestion import ingest_email_headers

    _enqueue(factory, _header())

    def rename_then_ingest(**kwargs):
        db = factory()
        account = db.query(Account).filter(
            Account.id == "account-alice"
        ).one()
        identity = db.query(AuthIdentity).filter(
            AuthIdentity.account_id == account.id,
            AuthIdentity.provider == "local",
        ).one()
        account.username = "alice-renamed"
        identity.subject = "alice-renamed"
        db.commit()
        db.close()
        return ingest_email_headers(session_factory=factory, **kwargs)

    result = drain_email_life_projections(
        ingest=rename_then_ingest,
        owner="alice",
        account_key="mail-a",
        folder="INBOX",
        retry_delay_seconds=0,
        session_factory=factory,
    )
    assert result.failed == 1
    db = factory()
    assert db.query(Account).count() == 1
    assert db.query(Account).one().username == "alice-renamed"
    assert db.query(LifeSource).count() == 0
    assert db.query(EmailLifeProjection).one().state == "failed"
    db.close()


def test_committed_cache_row_backfills_boundedly_and_import_is_non_destructive(
    projection_env,
):
    _engine, factory, tmp_path = projection_env
    sidecar = tmp_path / "scheduled.db"
    _create_index(sidecar)
    _insert_index(sidecar, _header(uid="1"), updated_at="2026-07-17T09:00:01Z")
    _insert_index(sidecar, _header(uid="2"), updated_at="2026-07-17T09:00:02Z")
    calls = []
    first = drain_email_life_projections(
        sidecar,
        ingest=lambda **kwargs: calls.append(kwargs["emails"][0]["uid"]),
        owner="alice",
        account_key="mail-a",
        folder="INBOX",
        limit=1,
        backfill_limit=1,
        session_factory=factory,
    )
    assert first.backfilled == 1
    assert first.completed == 1
    assert calls == ["1"]
    with sqlite3.connect(sidecar) as source:
        assert source.execute(
            "SELECT COUNT(*) FROM email_message_index"
        ).fetchone()[0] == 2

    second = drain_email_life_projections(
        sidecar,
        ingest=lambda **kwargs: calls.append(kwargs["emails"][0]["uid"]),
        owner="account-alice",
        account_key="mail-a",
        folder="INBOX",
        limit=2,
        backfill_limit=1,
        session_factory=factory,
    )
    assert second.backfilled == 1
    assert second.completed == 1
    assert calls == ["1", "2"]
    db = factory()
    marker = db.query(EmailLifeProjectionImportRun).one()
    assert marker.owner_id == "account-alice"
    assert marker.details["cursor_rowid"] == 2
    db.close()


def test_cache_update_after_import_cursor_is_backfilled_by_updated_at(
    projection_env,
):
    _engine, factory, tmp_path = projection_env
    sidecar = tmp_path / "scheduled.db"
    _create_index(sidecar)
    original = _header(uid="1", message_id="")
    _insert_index(sidecar, original, updated_at="2026-07-17T09:00:00Z")
    calls = []
    drain_email_life_projections(
        sidecar,
        ingest=lambda **kwargs: calls.append(kwargs["emails"][0]["subject"]),
        owner="alice",
        account_key="mail-a",
        folder="INBOX",
        backfill_limit=8,
        session_factory=factory,
    )

    replacement = {**original, "subject": "Replacement occupant"}
    _insert_index(sidecar, replacement, updated_at="2026-07-17T10:00:00Z")
    changed = drain_email_life_projections(
        sidecar,
        ingest=lambda **kwargs: calls.append(kwargs["emails"][0]["subject"]),
        owner="alice",
        account_key="mail-a",
        folder="INBOX",
        backfill_limit=8,
        session_factory=factory,
    )
    assert changed.backfilled == 1
    assert changed.completed == 1
    assert calls == ["Message 1", "Replacement occupant"]


def test_encrypted_cache_recovery_preserves_rfc_thread_evidence(projection_env):
    _engine, factory, tmp_path = projection_env
    sidecar = tmp_path / "scheduled.db"
    _create_index(sidecar)
    header = _header(
        uid="44",
        references=["<private-root@example.test>", "<middle@example.test>"],
        in_reply_to="<middle@example.test>",
        subject="Private recovered subject",
    )
    _insert_index(sidecar, header, encrypted_recovery=True)
    calls = []
    result = drain_email_life_projections(
        sidecar,
        ingest=lambda **kwargs: calls.append(kwargs["emails"][0]),
        owner="alice",
        account_key="mail-a",
        folder="INBOX",
        session_factory=factory,
    )
    assert result.backfilled == 1
    assert result.completed == 1
    assert calls[0]["references"] == [
        "<private-root@example.test>",
        "<middle@example.test>",
    ]
    assert calls[0]["in_reply_to"] == "<middle@example.test>"
    with sqlite3.connect(sidecar) as source:
        stored = source.execute(
            "SELECT projection_payload_ciphertext FROM email_message_index"
        ).fetchone()[0]
    assert stored.startswith("enc:c1:")
    assert "private-root" not in stored


def test_legacy_encrypted_ledger_imports_completed_marker_without_reprojection(
    projection_env,
):
    _engine, factory, tmp_path = projection_env
    sidecar = tmp_path / "scheduled.db"
    header = _header(uid="7")
    from src.email_life_projection_ledger import _payload_hash

    with sqlite3.connect(sidecar) as conn:
        ensure_email_life_projection_schema(conn)
        conn.execute(
            """
            INSERT INTO email_life_projection_ledger(
                owner, account_key, folder, uid, header_sha256,
                payload_ciphertext, state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, '', 'completed', ?, ?)
            """,
            (
                "alice", "mail-a", "INBOX", "7", _payload_hash(header),
                "2026-07-17T09:00:00Z", "2026-07-17T09:00:00Z",
            ),
        )
    calls = []
    result = drain_email_life_projections(
        sidecar,
        ingest=lambda **kwargs: calls.append(kwargs),
        owner="alice",
        account_key="mail-a",
        folder="INBOX",
        session_factory=factory,
    )
    assert result.backfilled == 1
    assert result.attempted == 0
    assert calls == []
    db = factory()
    row = db.query(EmailLifeProjection).one()
    assert row.message_uid == "7"
    assert row.state == "completed"
    assert row.payload is None
    db.close()


def test_partial_legacy_schema_fails_closed_and_writes_no_import_marker(
    projection_env,
):
    _engine, factory, tmp_path = projection_env
    sidecar = tmp_path / "scheduled.db"
    with sqlite3.connect(sidecar) as conn:
        conn.execute("CREATE TABLE email_message_index(owner TEXT, uid TEXT)")
    with pytest.raises(
        EmailLifeProjectionLedgerError, match="schema is incomplete"
    ):
        drain_email_life_projections(
            sidecar,
            ingest=lambda **_kwargs: None,
            owner="alice",
            account_key="mail-a",
            folder="INBOX",
            session_factory=factory,
        )
    db = factory()
    assert db.query(EmailLifeProjection).count() == 0
    assert db.query(EmailLifeProjectionImportRun).count() == 0
    db.close()
