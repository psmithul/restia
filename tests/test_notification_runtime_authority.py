from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    Base,
    BrowserNotification,
    NotificationRuntimeImportRun,
    ReminderDeliveryClaim,
)
from migrations.versions.notification_runtime_authority_20260725_0010 import (
    NOTIFICATION_RUNTIME_REQUIRED_COLUMNS,
    NOTIFICATION_RUNTIME_REQUIRED_TABLES,
)
from src import notification_delivery_authority as authority
from src import notification_legacy_import as legacy_import
from src.browser_notification_outbox import (
    acknowledge_browser_notifications,
    browser_notification_ack_candidates,
    cancel_browser_notifications_for_reminder,
    enqueue_browser_notification,
    pending_browser_notifications,
    rearm_browser_notifications_for_reminder,
)
from src.database_migrations import (
    SCHEMA_HEAD_REVISION,
    SchemaRevisionError,
    schema_revision_status,
    upgrade_schema,
    validate_head_schema,
)
from src.reminder_delivery_claims import (
    acknowledge_reminder_delivery,
    await_browser_ack,
    cancel_reminder_deliveries,
    claim_reminder_delivery,
    fail_reminder_delivery,
    rearm_reminder_deliveries,
    reminder_claim_is_active,
)
from src.notification_legacy_import import (
    NotificationRuntimeImportError,
    import_legacy_notification_runtime,
)


def _authority_database(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'notification-authority.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(authority, "SessionLocal", factory)
    monkeypatch.setattr(legacy_import, "SessionLocal", factory)
    return engine, factory


def _account_id(factory, username: str) -> str:
    with factory() as db:
        return db.query(Account.id).filter(Account.username == username).scalar()


def test_sql_reminder_claim_is_account_owned_fenced_and_permanently_deduped(
    tmp_path, monkeypatch,
):
    _engine, factory = _authority_database(tmp_path, monkeypatch)
    occurrence = "2026-07-17T09:30:00+00:00"
    now = datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc)

    first = claim_reminder_delivery(
        None,
        owner="Alice",
        note_id="note-1",
        occurrence=occurrence,
        channel="browser",
        now=now,
    )
    assert first.acquired is True
    assert first.token
    account_id = _account_id(factory, "alice")

    competing = claim_reminder_delivery(
        None,
        owner=account_id,
        note_id="note-1",
        occurrence=occurrence,
        channel="browser",
        now=now + timedelta(seconds=1),
    )
    assert (competing.acquired, competing.reason) == (False, "in_flight")
    assert reminder_claim_is_active(
        None,
        owner=account_id,
        note_id="note-1",
        occurrence=occurrence,
        channel="browser",
        token=first.token,
    ) is True
    assert reminder_claim_is_active(
        None,
        owner="bob",
        note_id="note-1",
        occurrence=occurrence,
        channel="browser",
        token=first.token,
    ) is False

    assert acknowledge_reminder_delivery(
        None,
        owner=account_id,
        note_id="note-1",
        occurrence=occurrence,
        channel="browser",
        token=first.token,
        now=now + timedelta(minutes=1),
    ) is True
    duplicate = claim_reminder_delivery(
        None,
        owner="alice",
        note_id="note-1",
        occurrence=occurrence,
        channel="browser",
        now=now + timedelta(days=30),
    )
    assert (duplicate.acquired, duplicate.reason) == (False, "delivered")


def test_sql_browser_outbox_is_owner_scoped_deduped_and_ack_linked(
    tmp_path, monkeypatch,
):
    _engine, factory = _authority_database(tmp_path, monkeypatch)
    occurrence = "2026-07-17T11:00:00Z"
    claim = claim_reminder_delivery(
        None,
        owner="alice",
        note_id="note-linked",
        occurrence=occurrence,
        channel="browser",
    )
    assert claim.acquired
    assert await_browser_ack(
        None,
        owner="alice",
        note_id="note-linked",
        occurrence=occurrence,
        channel="browser",
        token=claim.token,
    )

    created = enqueue_browser_notification(
        None,
        "alice",
        {"id": "notice-1", "task_id": "reminder-note-linked", "body": "Private"},
        dedupe_key=f"reminder:note-linked:{occurrence}",
        reminder_claim={
            "owner": "alice",
            "note_id": "note-linked",
            "occurrence": occurrence,
            "channel": "browser",
            "token": claim.token,
        },
    )
    duplicate = enqueue_browser_notification(
        None,
        "alice",
        {"id": "different-id", "body": "Must not replace immutable payload"},
        dedupe_key=f"reminder:note-linked:{occurrence}",
        reminder_claim={
            "owner": "alice",
            "note_id": "note-linked",
            "occurrence": occurrence,
            "channel": "browser",
            "token": claim.token,
        },
    )
    assert created["_outbox_created"] is True
    assert duplicate["_outbox_created"] is False
    assert duplicate["id"] == "notice-1"
    assert [row["id"] for row in pending_browser_notifications(None, "alice")] == [
        "notice-1"
    ]
    assert pending_browser_notifications(None, "bob") == []
    assert acknowledge_browser_notifications(None, "bob", ["notice-1"]) == 0

    candidates = browser_notification_ack_candidates(None, "alice", ["notice-1"])
    assert len(candidates) == 1
    linked = candidates[0]
    account_id = _account_id(factory, "alice")
    assert linked["claim_owner"] == account_id
    assert linked["claim_token"] == claim.token
    assert acknowledge_reminder_delivery(
        None,
        owner=linked["claim_owner"],
        note_id=linked["claim_note_id"],
        occurrence=linked["claim_occurrence"],
        channel=linked["claim_channel"],
        token=linked["claim_token"],
    )
    assert acknowledge_browser_notifications(None, "alice", ["notice-1"]) == 1
    assert pending_browser_notifications(None, "alice") == []


def test_sql_cancellation_is_shared_cancel_before_enqueue_barrier_and_rearm(
    tmp_path, monkeypatch,
):
    _authority_database(tmp_path, monkeypatch)
    occurrence = "2026-07-18T08:00:00Z"
    assert cancel_browser_notifications_for_reminder(
        None, "alice", "note-cancelled", occurrence=occurrence,
    ) == 0
    blocked_outbox = enqueue_browser_notification(
        None,
        "alice",
        {"id": "blocked", "task_id": "reminder-note-cancelled"},
        dedupe_key="cancelled",
        reminder_claim={
            "owner": "alice",
            "note_id": "note-cancelled",
            "occurrence": occurrence,
            "channel": "browser",
            "token": "opaque",
        },
    )
    assert blocked_outbox["_outbox_cancelled"] is True
    blocked_claim = claim_reminder_delivery(
        None,
        owner="alice",
        note_id="note-cancelled",
        occurrence=occurrence,
        channel="browser",
    )
    assert (blocked_claim.acquired, blocked_claim.reason) == (False, "cancelled")

    assert rearm_browser_notifications_for_reminder(
        None, "alice", "note-cancelled", occurrence=occurrence,
    ) == 1
    assert rearm_reminder_deliveries(
        None,
        owner="alice",
        note_id="note-cancelled",
        occurrence=occurrence,
    ) == 0
    restored = claim_reminder_delivery(
        None,
        owner="alice",
        note_id="note-cancelled",
        occurrence=occurrence,
        channel="browser",
    )
    assert restored.acquired is True
    assert cancel_reminder_deliveries(
        None,
        owner="alice",
        note_id="note-cancelled",
        occurrence=occurrence,
    ) == 1
    assert reminder_claim_is_active(
        None,
        owner="alice",
        note_id="note-cancelled",
        occurrence=occurrence,
        channel="browser",
        token=restored.token,
    ) is False


def test_sql_notification_authority_never_persists_payload_token_or_raw_error(
    tmp_path, monkeypatch,
):
    engine, factory = _authority_database(tmp_path, monkeypatch)
    private_text = "violet-orchid-771"
    raw_error = "password=never-store-this"
    claim = claim_reminder_delivery(
        None,
        owner="alice",
        note_id="note-private",
        occurrence="",
        channel="email",
    )
    enqueue_browser_notification(
        None,
        "alice",
        {"id": "private-notice", "body": private_text},
        dedupe_key=f"dedupe-{private_text}",
        reminder_claim={
            "owner": "alice",
            "note_id": "note-private",
            "occurrence": "",
            "channel": "email",
            "token": claim.token,
        },
    )
    assert fail_reminder_delivery(
        None,
        owner="alice",
        note_id="note-private",
        occurrence="",
        channel="email",
        token=claim.token,
        error=raw_error,
        retry_seconds=1,
    )

    with engine.connect() as connection:
        browser = connection.execute(text(
            "SELECT payload, dedupe_key_digest, claim_token "
            "FROM browser_notifications"
        )).one()
        reminder = connection.execute(text(
            "SELECT claim_token_digest, last_error_code "
            "FROM reminder_delivery_claims"
        )).one()
    raw = " ".join(str(value) for value in (*browser, *reminder))
    assert private_text not in raw
    assert claim.token not in raw
    assert raw_error not in raw
    assert reminder.last_error_code == "delivery_failed"
    assert str(browser.payload).startswith('"enc:c1:')
    assert str(browser.claim_token).startswith("enc:c1:")
    assert len(str(browser.dedupe_key_digest)) == 64
    assert len(str(reminder.claim_token_digest)) == 64

    with factory() as db:
        assert db.query(BrowserNotification).one().payload["body"] == private_text
        assert db.query(ReminderDeliveryClaim).one().last_error_code == "delivery_failed"


def test_revision_0010_manifest_remains_in_current_head_and_is_executable(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh-chain.db'}")
    result = upgrade_schema(engine)

    assert result.current_revisions == (SCHEMA_HEAD_REVISION,)
    assert schema_revision_status(engine).matches_expected is True
    inspector = inspect(engine)
    assert NOTIFICATION_RUNTIME_REQUIRED_TABLES <= set(inspector.get_table_names())
    for table_name, required in NOTIFICATION_RUNTIME_REQUIRED_COLUMNS.items():
        assert required <= {
            str(column["name"]) for column in inspector.get_columns(table_name)
        }


def test_head_validation_rejects_plaintext_browser_payload(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tampered-head.db'}")
    upgrade_schema(engine)
    now = "2026-07-17 09:00:00"
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO accounts(id, username, status, auth_epoch, created_at, updated_at) "
            "VALUES ('00000000-0000-4000-8000-000000000901', 'tamper', "
            "'active', 1, :now, :now)"
        ), {"now": now})
        connection.execute(text(
            "INSERT INTO browser_notifications("
            "id, owner_id, payload, claim_note_id, claim_occurrence, "
            "claim_channel, created_at, updated_at) VALUES ("
            "'00000000-0000-4000-8000-000000000902', "
            "'00000000-0000-4000-8000-000000000901', :payload, '', '', '', "
            ":now, :now)"
        ), {"payload": '{"body":"plaintext"}', "now": now})

    try:
        validate_head_schema(engine)
    except SchemaRevisionError as exc:
        assert "browser_notifications.payload" in str(exc)
    else:  # pragma: no cover - explicit privacy contract
        raise AssertionError("plaintext browser payload passed head validation")


def test_legacy_sidecars_import_once_into_encrypted_account_owned_authority(
    tmp_path, monkeypatch,
):
    engine, factory = _authority_database(tmp_path, monkeypatch)
    reminder_path = tmp_path / "reminder_delivery_claims.sqlite3"
    browser_path = tmp_path / "browser_notification_outbox.sqlite3"
    occurrence = "2026-07-19T07:00:00Z"
    private_body = "legacy-private-body-882"

    legacy_claim = claim_reminder_delivery(
        reminder_path,
        owner="alice",
        note_id="legacy-note",
        occurrence=occurrence,
        channel="browser",
    )
    assert legacy_claim.acquired
    assert await_browser_ack(
        reminder_path,
        owner="alice",
        note_id="legacy-note",
        occurrence=occurrence,
        channel="browser",
        token=legacy_claim.token,
    )
    enqueue_browser_notification(
        browser_path,
        "alice",
        {
            "id": "legacy-notice",
            "task_id": "reminder-legacy-note",
            "body": private_body,
        },
        dedupe_key=f"reminder:legacy-note:{occurrence}",
        reminder_claim={
            "owner": "alice",
            "note_id": "legacy-note",
            "occurrence": occurrence,
            "channel": "browser",
            "token": legacy_claim.token,
        },
    )
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (reminder_path, browser_path)
    }

    first = import_legacy_notification_runtime(
        reminder_path=reminder_path,
        browser_path=browser_path,
    )
    second = import_legacy_notification_runtime(
        reminder_path=reminder_path,
        browser_path=browser_path,
    )

    assert first["reminder"].imported == 1
    assert first["browser"].imported == 1
    assert second["reminder"].already_completed is True
    assert second["browser"].already_completed is True
    assert before == {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (reminder_path, browser_path)
    }
    assert pending_browser_notifications(None, "alice")[0]["body"] == private_body
    candidate = browser_notification_ack_candidates(
        None, "alice", ["legacy-notice"],
    )[0]
    assert candidate["claim_token"] == legacy_claim.token
    assert acknowledge_reminder_delivery(
        None,
        owner=candidate["claim_owner"],
        note_id=candidate["claim_note_id"],
        occurrence=candidate["claim_occurrence"],
        channel=candidate["claim_channel"],
        token=candidate["claim_token"],
    )

    with engine.connect() as connection:
        raw = " ".join(str(value) for value in connection.execute(text(
            "SELECT payload, claim_token FROM browser_notifications"
        )).one())
        assert private_body not in raw
        assert legacy_claim.token not in raw
    with factory() as db:
        markers = db.query(NotificationRuntimeImportRun).all()
        assert len(markers) == 2
        assert {row.state for row in markers} == {"completed"}
        assert all(row.details["source_retained"] is True for row in markers)


def test_corrupt_legacy_sidecar_records_safe_failed_marker_and_is_retained(
    tmp_path, monkeypatch,
):
    _engine, factory = _authority_database(tmp_path, monkeypatch)
    corrupt = tmp_path / "reminder_delivery_claims.sqlite3"
    corrupt.write_bytes(b"not a sqlite database private-secret-991")
    digest_before = hashlib.sha256(corrupt.read_bytes()).hexdigest()

    try:
        import_legacy_notification_runtime(
            reminder_path=corrupt,
            browser_path=tmp_path / "missing-browser.sqlite3",
        )
    except NotificationRuntimeImportError as exc:
        assert "readable SQLite" in str(exc) or "could not be queried" in str(exc)
    else:  # pragma: no cover - explicit failure contract
        raise AssertionError("corrupt notification sidecar was accepted")

    assert hashlib.sha256(corrupt.read_bytes()).hexdigest() == digest_before
    with factory() as db:
        marker = db.query(NotificationRuntimeImportRun).one()
        assert marker.state == "failed"
        assert marker.details["source_retained"] is True
        assert "private-secret" not in str(marker.details)
