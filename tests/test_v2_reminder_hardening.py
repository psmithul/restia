"""Hostile regressions for V2 reminder/Telegram durability."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


def test_cancel_barriers_block_late_claim_and_enqueue_then_rearm(tmp_path):
    from src.browser_notification_outbox import (
        cancel_browser_notifications_for_reminder,
        enqueue_browser_notification,
        pending_browser_notifications,
        rearm_browser_notifications_for_reminder,
    )
    from src.reminder_delivery_claims import (
        cancel_reminder_deliveries,
        claim_reminder_delivery,
        rearm_reminder_deliveries,
    )

    outbox = tmp_path / "outbox.sqlite3"
    claims = tmp_path / "claims.sqlite3"
    occurrence = "2026-07-16T08:00:00Z"
    cancel_browser_notifications_for_reminder(
        outbox, "alice", "note-1", occurrence=occurrence,
    )
    cancel_reminder_deliveries(
        claims, owner="alice", note_id="note-1", occurrence=occurrence,
    )

    blocked_claim = claim_reminder_delivery(
        claims, owner="alice", note_id="note-1", occurrence=occurrence,
        channel="browser",
    )
    blocked_row = enqueue_browser_notification(
        outbox,
        "alice",
        {"task_id": "reminder-note-1", "body": "stale"},
        dedupe_key=f"reminder:note-1:{occurrence}:primary",
        reminder_claim={
            "owner": "alice", "note_id": "note-1", "occurrence": occurrence,
            "channel": "browser", "token": "stale",
        },
    )
    assert blocked_claim.reason == "cancelled"
    assert blocked_row["_outbox_cancelled"] is True
    assert pending_browser_notifications(outbox, "alice") == []

    rearm_browser_notifications_for_reminder(
        outbox, "alice", "note-1", occurrence=occurrence,
    )
    rearm_reminder_deliveries(
        claims, owner="alice", note_id="note-1", occurrence=occurrence,
    )
    fresh_claim = claim_reminder_delivery(
        claims, owner="alice", note_id="note-1", occurrence=occurrence,
        channel="browser",
    )
    fresh_row = enqueue_browser_notification(
        outbox,
        "alice",
        {"task_id": "reminder-note-1", "body": "restored"},
        dedupe_key=f"reminder:note-1:{occurrence}:primary",
        reminder_claim={
            "owner": "alice", "note_id": "note-1", "occurrence": occurrence,
            "channel": "browser", "token": fresh_claim.token,
        },
    )
    assert fresh_claim.acquired is True
    assert fresh_row["_outbox_created"] is True
    assert [row["body"] for row in pending_browser_notifications(outbox, "alice")] == ["restored"]


def test_recurrence_uses_profile_wall_clock_across_midnight_and_dst():
    from src.builtin_actions import _advance_recurring_due

    kolkata = _advance_recurring_due(
        "2026-07-12T19:30:00Z", "weekly:1", tz_name="Asia/Kolkata",
    )
    new_york = _advance_recurring_due(
        "2027-03-13T14:00:00Z", "daily", tz_name="America/New_York",
    )
    assert datetime.fromisoformat(kolkata.replace("Z", "+00:00")).astimezone(
        ZoneInfo("Asia/Kolkata")
    ).strftime("%a %Y-%m-%d %H:%M") == "Mon 2026-07-20 01:00"
    assert datetime.fromisoformat(new_york.replace("Z", "+00:00")).astimezone(
        ZoneInfo("America/New_York")
    ).strftime("%Y-%m-%d %H:%M") == "2027-03-14 09:00"


def test_cycle_date_uses_owner_timezone_and_safe_fallback():
    from src.note_progression import CYCLE_DUE_KEY, _cycle_is_due

    item = {CYCLE_DUE_KEY: "2026-07-12T19:30:00Z"}  # Mon 01:00 IST
    sunday_utc = datetime.fromisoformat("2026-07-12T00:00:00+00:00")
    assert _cycle_is_due(item, sunday_utc, tz_name="Asia/Kolkata") is False
    assert _cycle_is_due(item, sunday_utc) is False


def test_unrelated_preference_write_does_not_claim_timezone(monkeypatch):
    import src.notification_preferences as prefs

    state = {}
    monkeypatch.setattr(prefs, "_load_for_user", lambda owner: json.loads(json.dumps(state)))

    def save(owner, payload):
        state.clear()
        state.update(json.loads(json.dumps(payload)))

    monkeypatch.setattr(prefs, "_save_for_user", save)
    monkeypatch.setattr(prefs, "load_settings", lambda: {})

    prefs.save_notification_preferences("alice", {"digest_cadence": "daily"})
    assert prefs.notification_timezone_configured("alice") is False
    prefs.ensure_notification_timezone("alice", "Asia/Kolkata")
    assert prefs.notification_timezone_configured("alice") is True
    assert prefs.load_notification_preferences("alice")["timezone"] == "Asia/Kolkata"


def test_legacy_telegram_allowlist_migrates_to_one_owner_only(monkeypatch):
    from src.telegram_bot import TelegramConfig, telegram_chat_ids_for_owner

    monkeypatch.setattr("src.auth_helpers.configured_single_user_owner", lambda request=None: "alice")
    config = TelegramConfig(
        enabled=True,
        bot_token="1:test",
        webhook_secret="secret",
        allowed_chat_ids=frozenset({"111"}),
        allow_all_chats=False,
        owner=None,
        session_map={"111": "legacy"},
        chat_owners={},
    )
    assert telegram_chat_ids_for_owner(config, "alice") == ["111"]
    assert telegram_chat_ids_for_owner(config, "bob") == []


@pytest.mark.asyncio
async def test_inbound_reply_is_built_once_and_retried_from_ledger(monkeypatch, tmp_path):
    import routes.telegram_routes as routes
    import src.constants as constants
    from core.database import Account, SessionLocal
    from src.telegram_bot import TelegramConfig
    from src.telegram_inbound_ledger import TelegramReplyPending

    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    config = TelegramConfig(
        enabled=True, bot_token="1:test", webhook_secret="secret",
        allowed_chat_ids=frozenset({"111"}), allow_all_chats=False,
        owner="alice", session_map={}, chat_owners={},
    )
    db = SessionLocal()
    try:
        if db.query(Account).filter(Account.id == "account-alice").first() is None:
            db.add(Account(
                id="account-alice", username="telegram-test-alice", status="active"
            ))
            db.commit()
    finally:
        db.close()
    builds = []
    replies = []
    ingestions = []

    async def build(*args):
        builds.append(1)
        return "durable answer"

    async def reply(config, incoming, text):
        replies.append(text)
        if len(replies) == 1:
            raise RuntimeError("Telegram temporarily unavailable")

    monkeypatch.setattr(routes, "_build_message_reply", build)
    monkeypatch.setattr(routes, "_reply", reply)
    monkeypatch.setattr(
        routes, "_telegram_owner_account_id", lambda *args: "account-alice",
    )
    monkeypatch.setattr(
        routes,
        "_ingest_telegram_incoming",
        lambda *args, **kwargs: ingestions.append(1),
    )
    update = {
        "update_id": 55,
        "message": {"message_id": 7, "chat": {"id": 111}, "text": "hello"},
    }
    with pytest.raises(TelegramReplyPending):
        await routes._process_update_durably(object(), None, config, update)
    await routes._process_update_durably(object(), None, config, update)
    await routes._process_update_durably(object(), None, config, update)

    assert builds == [1]
    assert ingestions == [1]
    assert replies == ["durable answer", "durable answer"]


@pytest.mark.asyncio
async def test_reply_pending_does_not_consume_poller_poison_attempts(tmp_path):
    import hashlib
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.database import Base, TelegramPollingState
    from src.telegram_delivery import TelegramRuntimeAuthority
    from src.telegram_inbound_ledger import TelegramReplyPending
    from src.telegram_runtime import TelegramPollingService

    engine = create_engine(f"sqlite:///{tmp_path / 'poller.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    authority = TelegramRuntimeAuthority(factory)
    fingerprint = hashlib.sha256(b"pending-bot").hexdigest()
    service = TelegramPollingService(
        runtime_authority=authority,
        worker_id="pending-worker",
        process_lock_path=tmp_path / "poller.lock",
    )
    service._token_fingerprint = fingerprint
    service._database_lease = authority.acquire_polling_lease(
        bot_fingerprint=fingerprint, worker_id="pending-worker"
    )

    async def pending(update):
        raise TelegramReplyPending()

    service.configure(pending)
    update = {"update_id": 99, "message": {}}
    for _ in range(5):
        with pytest.raises(TelegramReplyPending):
            await service._process_updates([update])
    assert service._offset is None
    assert authority.dead_letter_count(fingerprint) == 0
    db = factory()
    try:
        state = db.query(TelegramPollingState).one()
        assert state.failure_attempts == 0
        assert state.failure_update_id is None
    finally:
        db.close()


def test_stale_inbound_processing_lease_recovers(tmp_path):
    import sqlite3
    from src.telegram_inbound_ledger import claim_inbound_processing

    path = tmp_path / "inbound.sqlite3"
    first, _ = claim_inbound_processing(
        path, fingerprint="bot", update_id=7, chat_id="111",
    )
    assert first is True
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE telegram_inbound_ledger SET updated_at = '2000-01-01T00:00:00+00:00'"
    )
    conn.commit()
    conn.close()
    recovered, _ = claim_inbound_processing(
        path, fingerprint="bot", update_id=7, chat_id="111",
    )
    assert recovered is True


@pytest.mark.asyncio
async def test_missing_update_id_is_processed_once_without_recursion(monkeypatch):
    import routes.telegram_routes as routes
    from src.telegram_bot import TelegramConfig

    config = TelegramConfig(
        enabled=True, bot_token="1:test", webhook_secret="secret",
        allowed_chat_ids=frozenset({"111"}), allow_all_chats=False,
        owner="alice", session_map={}, chat_owners={},
    )
    calls = []

    async def process(*args):
        calls.append(args[-1].chat_id)

    monkeypatch.setattr(routes, "_process_message", process)
    monkeypatch.setattr(routes, "_ingest_telegram_incoming", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        routes, "_telegram_owner_account_id", lambda *args: "account-alice",
    )
    await routes._process_update_durably(
        object(), None, config,
        {"message": {"chat": {"id": 111}, "text": "hello"}},
    )
    assert calls == ["111"]


@pytest.mark.asyncio
async def test_email_success_waits_for_failed_telegram_mirror_without_resending_email(
    monkeypatch, tmp_path
):
    import sqlite3
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes
    import routes.note_routes as notes
    import src.notification_preferences as preferences
    import src.settings as settings_module
    import src.telegram_bot as telegram
    from src.telegram_bot import TelegramConfig

    monkeypatch.setattr(notes, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(notes, "_scheduler_ref", None)
    monkeypatch.setattr(settings_module, "load_settings", lambda: {})
    monkeypatch.setattr(preferences, "settings_with_notification_preferences", lambda owner, base: {
        **base,
        "reminder_channel": "email",
        "reminder_telegram_mirror": True,
        "notification_topics": ["reminders"],
        "quiet_hours_enabled": False,
        "reminder_llm_synthesis": False,
    })
    monkeypatch.setattr(preferences, "quiet_hours_active", lambda settings: False)
    monkeypatch.setattr(email_routes, "_get_email_config", lambda **kwargs: {
        "smtp_host": "smtp.example.test",
        "smtp_user": "alice@example.test",
        "smtp_password": "secret",
        "from_address": "alice@example.test",
    })
    smtp_calls = []
    monkeypatch.setattr(email_helpers, "_send_smtp_message", lambda *args: smtp_calls.append(1))
    config = TelegramConfig(
        enabled=True, bot_token="1:test", webhook_secret="secret",
        allowed_chat_ids=frozenset({"111"}), allow_all_chats=False,
        owner=None, session_map={}, chat_owners={"111": "alice"},
    )
    monkeypatch.setattr(telegram, "load_telegram_config", lambda: config)
    telegram_calls = []

    async def send(token, chat_id, text, **kwargs):
        telegram_calls.append(str(chat_id))
        if len(telegram_calls) == 1:
            raise RuntimeError("temporary Telegram outage")

    monkeypatch.setattr(telegram, "send_telegram_message", send)
    first = await notes.dispatch_reminder(
        "Due", "Body", "note-1", owner="alice",
        occurrence="2026-07-16T08:00:00Z",
    )
    assert first["email_sent"] is True
    assert first["telegram_sent"] is False
    assert first["delivered"] is False

    claim_path = tmp_path / "reminder_delivery_claims.sqlite3"
    conn = sqlite3.connect(claim_path)
    conn.execute(
        "UPDATE reminder_delivery_claims SET retry_after = '2000-01-01T00:00:00+00:00' "
        "WHERE status = 'failed'"
    )
    conn.commit()
    conn.close()
    second = await notes.dispatch_reminder(
        "Due", "Body", "note-1", owner="alice",
        occurrence="2026-07-16T08:00:00Z",
    )
    assert second["telegram_sent"] is True
    assert second["delivered"] is True
    assert smtp_calls == [1]
    assert telegram_calls == ["111", "111"]


@pytest.mark.asyncio
async def test_cancel_after_primary_telegram_recipient_claim_blocks_send(
    monkeypatch, tmp_path
):
    import routes.note_routes as notes
    import src.note_reminder_state as reminder_state
    import src.notification_preferences as preferences
    import src.reminder_delivery_claims as claims
    import src.settings as settings_module
    import src.telegram_bot as telegram
    from src.note_reminder_state import cancel_note_reminder
    from src.telegram_bot import TelegramConfig

    occurrence = "2026-07-16T10:00:00Z"
    monkeypatch.setattr(notes, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(notes, "_scheduler_ref", None)
    monkeypatch.setattr(reminder_state, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings_module, "load_settings", lambda: {})
    monkeypatch.setattr(preferences, "settings_with_notification_preferences", lambda owner, base: {
        **base,
        "reminder_channel": "telegram",
        "reminder_telegram_mirror": False,
        "notification_topics": ["reminders"],
        "quiet_hours_enabled": False,
        "reminder_llm_synthesis": False,
    })
    monkeypatch.setattr(preferences, "quiet_hours_active", lambda settings: False)
    config = TelegramConfig(
        enabled=True, bot_token="1:test", webhook_secret="secret",
        allowed_chat_ids=frozenset({"111"}), allow_all_chats=False,
        owner=None, session_map={}, chat_owners={"111": "alice"},
    )
    monkeypatch.setattr(telegram, "load_telegram_config", lambda: config)
    telegram_calls = []

    async def send(token, chat_id, text, **kwargs):
        telegram_calls.append(str(chat_id))

    monkeypatch.setattr(telegram, "send_telegram_message", send)
    original_claim = claims.claim_reminder_delivery

    def claim_then_cancel(path, **kwargs):
        claim = original_claim(path, **kwargs)
        if claim.acquired and str(kwargs.get("channel") or "").startswith(
            "telegram-recipient:"
        ):
            cancel_note_reminder(
                "alice", "note-primary-race", occurrence=occurrence,
            )
        return claim

    monkeypatch.setattr(claims, "claim_reminder_delivery", claim_then_cancel)
    result = await notes.dispatch_reminder(
        "Due", "Body", "note-primary-race", owner="alice",
        occurrence=occurrence,
    )

    assert telegram_calls == []
    assert result["telegram_sent"] is False
    assert result["delivered"] is False
    assert result["acknowledged"] is False


@pytest.mark.asyncio
async def test_cancel_after_telegram_mirror_recipient_claim_blocks_send(
    monkeypatch, tmp_path
):
    import routes.email_helpers as email_helpers
    import routes.email_routes as email_routes
    import routes.note_routes as notes
    import src.note_reminder_state as reminder_state
    import src.notification_preferences as preferences
    import src.reminder_delivery_claims as claims
    import src.settings as settings_module
    import src.telegram_bot as telegram
    from src.note_reminder_state import cancel_note_reminder
    from src.telegram_bot import TelegramConfig

    occurrence = "2026-07-16T11:00:00Z"
    monkeypatch.setattr(notes, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(notes, "_scheduler_ref", None)
    monkeypatch.setattr(reminder_state, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(settings_module, "load_settings", lambda: {})
    monkeypatch.setattr(preferences, "settings_with_notification_preferences", lambda owner, base: {
        **base,
        "reminder_channel": "email",
        "reminder_telegram_mirror": True,
        "notification_topics": ["reminders"],
        "quiet_hours_enabled": False,
        "reminder_llm_synthesis": False,
    })
    monkeypatch.setattr(preferences, "quiet_hours_active", lambda settings: False)
    monkeypatch.setattr(email_routes, "_get_email_config", lambda **kwargs: {
        "smtp_host": "smtp.example.test",
        "smtp_user": "alice@example.test",
        "smtp_password": "secret",
        "from_address": "alice@example.test",
    })
    smtp_calls = []
    monkeypatch.setattr(email_helpers, "_send_smtp_message", lambda *args: smtp_calls.append(1))
    config = TelegramConfig(
        enabled=True, bot_token="1:test", webhook_secret="secret",
        allowed_chat_ids=frozenset({"111"}), allow_all_chats=False,
        owner=None, session_map={}, chat_owners={"111": "alice"},
    )
    monkeypatch.setattr(telegram, "load_telegram_config", lambda: config)
    telegram_calls = []

    async def send(token, chat_id, text, **kwargs):
        telegram_calls.append(str(chat_id))

    monkeypatch.setattr(telegram, "send_telegram_message", send)
    original_claim = claims.claim_reminder_delivery

    def claim_then_cancel(path, **kwargs):
        claim = original_claim(path, **kwargs)
        if claim.acquired and str(kwargs.get("channel") or "").startswith(
            "telegram-mirror-recipient:"
        ):
            cancel_note_reminder(
                "alice", "note-mirror-race", occurrence=occurrence,
            )
        return claim

    monkeypatch.setattr(claims, "claim_reminder_delivery", claim_then_cancel)
    result = await notes.dispatch_reminder(
        "Due", "Body", "note-mirror-race", owner="alice",
        occurrence=occurrence,
    )

    assert smtp_calls == [1]
    assert telegram_calls == []
    assert result["email_sent"] is True
    assert result["telegram_sent"] is False
    assert result["delivered"] is False
    assert result["acknowledged"] is False


@pytest.mark.asyncio
async def test_multi_chat_telegram_retry_sends_only_failed_recipient(monkeypatch, tmp_path):
    import sqlite3
    import routes.note_routes as notes
    import src.notification_preferences as preferences
    import src.settings as settings_module
    import src.telegram_bot as telegram
    from src.telegram_bot import TelegramConfig

    monkeypatch.setattr(notes, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(notes, "_scheduler_ref", None)
    monkeypatch.setattr(settings_module, "load_settings", lambda: {})
    monkeypatch.setattr(preferences, "settings_with_notification_preferences", lambda owner, base: {
        **base,
        "reminder_channel": "telegram",
        "reminder_telegram_mirror": False,
        "notification_topics": ["reminders"],
        "quiet_hours_enabled": False,
        "reminder_llm_synthesis": False,
    })
    monkeypatch.setattr(preferences, "quiet_hours_active", lambda settings: False)
    config = TelegramConfig(
        enabled=True, bot_token="1:test", webhook_secret="secret",
        allowed_chat_ids=frozenset({"1", "2"}), allow_all_chats=False,
        owner=None, session_map={}, chat_owners={"1": "alice", "2": "alice"},
    )
    monkeypatch.setattr(telegram, "load_telegram_config", lambda: config)
    calls = []

    async def send(token, chat_id, text, **kwargs):
        calls.append(str(chat_id))
        if str(chat_id) == "2" and calls.count("2") == 1:
            raise RuntimeError("temporary")

    monkeypatch.setattr(telegram, "send_telegram_message", send)
    kwargs = dict(
        title="Due", note_body="Body", note_id="note-2", owner="alice",
        occurrence="2026-07-16T09:00:00Z",
    )
    first = await notes.dispatch_reminder(**kwargs)
    assert first["telegram_sent"] is False

    conn = sqlite3.connect(tmp_path / "reminder_delivery_claims.sqlite3")
    conn.execute(
        "UPDATE reminder_delivery_claims SET retry_after = '2000-01-01T00:00:00+00:00' "
        "WHERE status = 'failed'"
    )
    conn.commit()
    conn.close()
    second = await notes.dispatch_reminder(**kwargs)
    assert second["telegram_sent"] is True
    assert calls == ["1", "2", "2"]


def test_notification_center_excludes_completed_todos_and_uses_profile_timezone(monkeypatch):
    from datetime import timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as database
    import routes.notification_center_routes as center
    import src.notification_preferences as preferences

    engine = create_engine("sqlite:///:memory:")
    database.Note.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    due = datetime.now(timezone.utc) - timedelta(minutes=30)
    db = factory()
    db.add_all([
        database.Note(
            id="done", owner="alice", title="Done", note_type="todo",
            due_date=due.isoformat(), items=json.dumps([{"text": "x", "done": True}]),
        ),
        database.Note(
            id="open", owner="alice", title="Open", note_type="todo",
            due_date=due.isoformat(), items=json.dumps([{"text": "x", "done": False}]),
        ),
    ])
    db.commit()
    db.close()
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(preferences, "load_notification_preferences", lambda owner: {
        "timezone": "Asia/Kolkata",
    })
    try:
        rows = center._todos_due("alice")
        assert [row["id"] for row in rows] == ["open"]
        expected = due.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%a %H:%M")
        assert rows[0]["due_label"] == expected
    finally:
        engine.dispose()


def test_notification_center_expands_old_recurring_calendar_event(monkeypatch):
    from datetime import timedelta
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as database
    import routes.notification_center_routes as center
    import src.notification_preferences as preferences

    engine = create_engine("sqlite:///:memory:")
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    next_hour = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(hours=1)
    original = next_hour.replace(tzinfo=None) - timedelta(days=7)
    db = factory()
    from src.identity import ensure_account

    account = ensure_account(db, "alice")
    db.add(database.CalendarCal(
        id="cal", owner_id=account.id, owner="alice", name="Calendar"
    ))
    db.add(database.CalendarEvent(
        uid="daily", owner_id=account.id, calendar_id="cal",
        summary="Daily stand-up",
        dtstart=original, dtend=original + timedelta(minutes=30),
        is_utc=True, rrule="FREQ=DAILY", status="confirmed",
    ))
    db.commit()
    db.close()
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(preferences, "load_notification_preferences", lambda owner: {
        "timezone": "UTC",
    })
    try:
        rows = center._events_upcoming("alice")
        assert any(row["summary"] == "Daily stand-up" for row in rows)
    finally:
        engine.dispose()
