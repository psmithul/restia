"""Focused V2 Telegram setup, preferences, and reminder reliability tests."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from fastapi import FastAPI
from fastapi import HTTPException

from src.telegram_bot import TelegramConfig


@pytest.fixture(autouse=True)
def _restore_real_builtin_actions_after_legacy_stub_tests():
    """Keep this module order-independent from legacy tests that stub imports."""
    module = sys.modules.get("src.builtin_actions")
    if module is not None and not hasattr(module, "action_ping_notes"):
        sys.modules.pop("src.builtin_actions", None)
        importlib.invalidate_caches()
        importlib.import_module("src.builtin_actions")


def _config(**overrides):
    values = {
        "enabled": True,
        "bot_token": "123456:abcdefghijklmnopqrstuvwxyz_123456",
        "webhook_secret": "safe-secret",
        "allowed_chat_ids": frozenset({"42"}),
        "allow_all_chats": False,
        "owner": None,
        "session_map": {},
        "chat_owners": {"42": "alice"},
    }
    values.update(overrides)
    return TelegramConfig(**values)


def _leased_polling_service(tmp_path, *, label="bot"):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.database import Base
    from src.telegram_delivery import TelegramRuntimeAuthority
    from src.telegram_runtime import TelegramPollingService

    engine = create_engine(
        f"sqlite:///{tmp_path / (label + '.db')}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    authority = TelegramRuntimeAuthority(factory)
    fingerprint = hashlib.sha256(label.encode("utf-8")).hexdigest()
    service = TelegramPollingService(
        runtime_authority=authority,
        worker_id=f"test-{label}",
        process_lock_path=tmp_path / (label + ".lock"),
    )
    service._token_fingerprint = fingerprint
    service._database_lease = authority.acquire_polling_lease(
        bot_fingerprint=fingerprint, worker_id=f"test-{label}"
    )
    assert service._database_lease is not None
    return service, authority, factory, fingerprint


@pytest.mark.asyncio
async def test_token_only_polling_setup_needs_no_public_url(monkeypatch):
    import src.telegram_runtime as runtime

    calls = []
    monkeypatch.setattr(
        runtime,
        "inspect_telegram_webhook",
        lambda token: asyncio.sleep(0, result={"url": "", "pending_update_count": 0, "last_error_present": False}),
    )

    async def api_call(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True, "result": True}

    monkeypatch.setattr(runtime, "telegram_api_call", api_call)
    result = await runtime.configure_telegram_transport(
        bot_token=_config().bot_token,
        mode="polling",
        bot_identity={"id": "123", "username": "restia_bot"},
    )

    assert result["mode"] == "polling"
    assert result["webhook_url"] == ""
    assert calls == []


@pytest.mark.asyncio
async def test_polling_refuses_to_steal_foreign_webhook(monkeypatch):
    import src.telegram_runtime as runtime

    monkeypatch.setattr(
        runtime,
        "inspect_telegram_webhook",
        lambda token: asyncio.sleep(0, result={"url": "https://other.example/hook", "pending_update_count": 2, "last_error_present": False}),
    )
    with pytest.raises(runtime.TelegramWebhookConflict, match="another service"):
        await runtime.configure_telegram_transport(
            bot_token=_config().bot_token,
            mode="polling",
            bot_identity={"id": "123"},
        )


@pytest.mark.asyncio
async def test_explicit_takeover_deletes_foreign_webhook_without_dropping_updates(monkeypatch):
    import src.telegram_runtime as runtime

    calls = []
    monkeypatch.setattr(
        runtime,
        "inspect_telegram_webhook",
        lambda token: asyncio.sleep(0, result={"url": "https://other.example/hook", "pending_update_count": 2, "last_error_present": False}),
    )

    async def api_call(token, method, **kwargs):
        calls.append((method, kwargs.get("payload")))
        return {"ok": True, "result": True}

    monkeypatch.setattr(runtime, "telegram_api_call", api_call)
    await runtime.configure_telegram_transport(
        bot_token=_config().bot_token,
        mode="polling",
        bot_identity={"id": "123"},
        replace_foreign_webhook=True,
    )

    assert calls == [("deleteWebhook", {"drop_pending_updates": False})]


def test_setup_payload_never_contains_bot_token(monkeypatch):
    import routes.telegram_routes as routes

    token = "123456:never-return-this-token-value"
    monkeypatch.setattr(routes, "load_settings", lambda: {
        "telegram_bot_token": token,
        "telegram_runtime_mode": "polling",
        "telegram_bot_id": "123",
        "telegram_bot_username": "restia_bot",
        "telegram_bot_first_name": "Restia",
        "telegram_registered_webhook_url": "",
        "app_public_url": "",
    })
    monkeypatch.setattr(routes, "load_telegram_config", lambda: _config(bot_token=token))
    monkeypatch.setattr(routes, "telegram_runtime_mode", lambda settings=None: "polling")
    monkeypatch.setattr(routes.telegram_polling_service, "status", lambda: {"poller_running": True, "last_error": "", "last_update_at": None})

    payload = routes._telegram_setup_payload()
    assert token not in json.dumps(payload)
    assert payload["bot_token_configured"] is True
    assert payload["bot"]["username"] == "restia_bot"


def test_notification_preferences_are_owner_scoped_and_validated(monkeypatch):
    import src.notification_preferences as preferences

    state = {}
    monkeypatch.setattr(preferences, "_load_for_user", lambda owner: dict(state.get(owner, {})))
    monkeypatch.setattr(preferences, "_save_for_user", lambda owner, value: state.__setitem__(owner, dict(value)))
    monkeypatch.setattr(preferences, "load_settings", lambda owner=None: {})

    saved = preferences.save_notification_preferences("Alice", {
        "reminder_channel": "telegram",
        "notification_topics": ["todos", "calendar"],
        "digest_cadence": "every_3_hours",
        "timezone": "Asia/Kolkata",
        "quiet_hours_enabled": True,
        "quiet_hours_start": "22:30",
        "quiet_hours_end": "07:15",
    })

    assert saved["reminder_channel"] == "telegram"
    assert preferences.load_notification_preferences("alice")["notification_topics"] == ["calendar", "todos"]
    assert preferences.load_notification_preferences("bob")["reminder_channel"] == "browser"
    with pytest.raises(preferences.NotificationPreferenceError, match="IANA"):
        preferences.save_notification_preferences("alice", {"timezone": "Mars/Olympus"})
    with pytest.raises(preferences.NotificationPreferenceError, match="unsupported notification topic"):
        preferences.save_notification_preferences("alice", {"notification_topics": ["secrets"]})


@pytest.mark.asyncio
async def test_auth_disabled_telegram_ui_link_and_dispatch_share_profile_owner(monkeypatch):
    import routes.note_routes as note_routes
    import routes.telegram_routes as routes

    monkeypatch.setenv("AUTH_ENABLED", "false")
    owners = []
    stored = {}

    def save_preferences(owner, body):
        owners.append(("preferences", owner))
        stored[owner] = dict(body)
        return dict(body)

    monkeypatch.setattr(routes, "save_notification_preferences", save_preferences)
    monkeypatch.setattr(routes, "load_notification_preferences", lambda owner: dict(stored.get(owner, {})))
    monkeypatch.setattr(routes, "_sync_digest_task", lambda owner, prefs: None)
    monkeypatch.setattr(routes, "load_telegram_config", lambda: _config(chat_owners={"42": "alice"}))
    monkeypatch.setattr(routes, "telegram_chat_ids_for_owner", lambda config, owner: ["42"] if owner == "alice" else [])

    def link_code(owner):
        owners.append(("link", owner))
        return "ABC123", "2026-07-16T14:30:00Z"

    monkeypatch.setattr(routes, "create_telegram_link_code", link_code)

    async def dispatch(**kwargs):
        owners.append(("dispatch", kwargs.get("owner")))
        return {"telegram_sent": True}

    monkeypatch.setattr(note_routes, "dispatch_reminder", dispatch)
    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(
        is_configured=True,
        users={"alice": {"is_admin": True}},
    )
    app.include_router(routes.setup_telegram_routes(object()))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://restia.test") as client:
        saved = await client.put(
            "/api/telegram/preferences",
            json={"reminder_channel": "telegram", "digest_cadence": "daily"},
        )
        assert saved.status_code == 200, saved.text
        linked = await client.post("/api/telegram/link-code")
        assert linked.status_code == 200, linked.text
        tested = await client.post("/api/telegram/test")
        assert tested.status_code == 200, tested.text

    assert owners == [
        ("preferences", "alice"),
        ("link", "alice"),
        ("dispatch", "alice"),
    ]
    assert "local" not in stored


@pytest.mark.asyncio
async def test_failed_telegram_delivery_is_not_acknowledged_or_cached(monkeypatch, tmp_path):
    import routes.note_routes as notes
    import src.notification_preferences as preferences
    import src.telegram_bot as telegram

    monkeypatch.setattr(notes, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(preferences, "settings_with_notification_preferences", lambda owner, base: {
        **base,
        "reminder_channel": "telegram",
        "reminder_telegram_mirror": False,
        "notification_topics": ["reminders"],
        "quiet_hours_enabled": False,
    })
    monkeypatch.setattr("src.settings.load_settings", lambda owner=None: {})
    monkeypatch.setattr(telegram, "load_telegram_config", lambda: _config())

    async def fail_send(*args, **kwargs):
        raise telegram.TelegramDeliveryError("Telegram API sendMessage failed (code 503)")

    monkeypatch.setattr(telegram, "send_telegram_message", fail_send)
    scheduler = SimpleNamespace(
        add_notification=lambda **kwargs: None,
        _reminder_claim_path=tmp_path / "reminder_delivery_claims.sqlite3",
    )
    monkeypatch.setattr(notes, "_scheduler_ref", scheduler)

    result = await notes.dispatch_reminder(
        "Important", "Do the thing", "note-1", owner="alice", occurrence="2026-07-16T09:00:00Z"
    )

    assert result["telegram_sent"] is False
    assert result["browser_sent"] is True
    assert result["delivered"] is False
    assert result["acknowledged"] is False
    assert not (tmp_path / "note_pings_alice.json").exists()


@pytest.mark.asyncio
async def test_browser_and_scanner_concurrency_share_one_durable_claim(monkeypatch, tmp_path):
    import routes.note_routes as notes
    import src.notification_preferences as preferences
    import src.telegram_bot as telegram

    monkeypatch.setattr(notes, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(preferences, "settings_with_notification_preferences", lambda owner, base: {
        **base,
        "reminder_channel": "telegram",
        "reminder_telegram_mirror": False,
        "notification_topics": ["reminders"],
        "quiet_hours_enabled": False,
    })
    monkeypatch.setattr("src.settings.load_settings", lambda owner=None: {})
    monkeypatch.setattr(telegram, "load_telegram_config", lambda: _config())
    monkeypatch.setattr(notes, "_scheduler_ref", None)

    send_started = asyncio.Event()
    release_send = asyncio.Event()
    sends = []

    async def slow_send(token, chat_id, text):
        sends.append((chat_id, text))
        send_started.set()
        await release_send.wait()

    monkeypatch.setattr(telegram, "send_telegram_message", slow_send)
    occurrence = "2026-07-16T09:00:00Z"
    browser = asyncio.create_task(notes.dispatch_reminder(
        "Important", "Do the thing", "shared-note",
        owner="alice", occurrence=occurrence, queue_browser=False,
    ))
    await send_started.wait()
    scanner = asyncio.create_task(notes.dispatch_reminder(
        "Important", "Do the thing", "shared-note",
        owner="alice", occurrence=occurrence, queue_browser=True,
    ))
    scanner_result = await scanner
    release_send.set()
    browser_result = await browser

    assert len(sends) == 1
    assert scanner_result["deferred"] is True
    assert scanner_result["suppression_reason"] == "delivery_in_flight"
    assert browser_result["delivered"] is True

    replay = await notes.dispatch_reminder(
        "Important", "Do the thing", "shared-note",
        owner="alice", occurrence=occurrence,
    )
    assert replay["skipped"] is True
    assert replay["acknowledged"] is True
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_external_browser_mirror_is_shown_but_topic_and_quiet_choices_are_silent(monkeypatch, tmp_path):
    import routes.note_routes as notes
    import src.notification_preferences as preferences
    import src.telegram_bot as telegram
    from src.task_scheduler import TaskScheduler

    state = {"topics": ["reminders"], "quiet": False}
    monkeypatch.setattr(notes, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(preferences, "settings_with_notification_preferences", lambda owner, base: {
        **base,
        "reminder_channel": "telegram",
        "reminder_telegram_mirror": False,
        "notification_topics": list(state["topics"]),
        "quiet_hours_enabled": state["quiet"],
    })
    monkeypatch.setattr(preferences, "quiet_hours_active", lambda settings: state["quiet"])
    monkeypatch.setattr("src.settings.load_settings", lambda owner=None: {})
    monkeypatch.setattr(telegram, "load_telegram_config", lambda: _config())
    monkeypatch.setattr(telegram, "send_telegram_message", lambda *args, **kwargs: asyncio.sleep(0))
    scheduler = TaskScheduler(None)
    scheduler._notification_outbox_path = tmp_path / "browser-outbox.sqlite3"
    scheduler._reminder_claim_path = tmp_path / "reminder_delivery_claims.sqlite3"
    monkeypatch.setattr(notes, "_scheduler_ref", scheduler)

    delivered = await notes.dispatch_reminder(
        "External", "Delivered to Telegram", "external-1", owner="alice",
        occurrence="occurrence-1", queue_browser=True,
    )
    assert delivered["telegram_sent"] is True
    assert delivered["browser_sent"] is True
    assert delivered["show_browser"] is True
    assert len(scheduler.pending_notifications("alice")) == 1

    state["topics"] = []
    suppressed = await notes.dispatch_reminder(
        "Suppressed", "No UI", "external-2", owner="alice",
        occurrence="occurrence-2", queue_browser=True,
    )
    assert suppressed["suppressed"] is True
    assert suppressed["browser_sent"] is False
    assert len(scheduler.pending_notifications("alice")) == 1

    state["topics"] = ["reminders"]
    state["quiet"] = True
    deferred = await notes.dispatch_reminder(
        "Deferred", "No UI", "external-3", owner="alice",
        occurrence="occurrence-3", queue_browser=True,
    )
    assert deferred["deferred"] is True
    assert deferred["browser_sent"] is False
    assert len(scheduler.pending_notifications("alice")) == 1


@pytest.mark.asyncio
async def test_recurring_browser_reminder_advances_only_after_explicit_outbox_ack(monkeypatch, tmp_path):
    import core.database as database
    import routes.note_routes as notes
    import src.builtin_actions as actions
    import src.notification_preferences as preferences
    from src.task_scheduler import TaskScheduler

    note = _due_note(repeat="daily")
    original_due = note.due_date
    db = _NoteDb(note)
    monkeypatch.setattr(actions, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(notes, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr("src.settings.load_settings", lambda owner=None: {})
    monkeypatch.setattr(preferences, "load_notification_preferences", lambda owner: {"reminder_channel": "browser"})
    monkeypatch.setattr(preferences, "settings_with_notification_preferences", lambda owner, base: {
        **base,
        "reminder_channel": "browser",
        "notification_topics": ["reminders"],
        "quiet_hours_enabled": False,
    })
    monkeypatch.setattr(preferences, "quiet_hours_active", lambda settings: False)
    scheduler = TaskScheduler(None)
    scheduler._notification_outbox_path = tmp_path / "browser-outbox.sqlite3"
    scheduler._reminder_claim_path = tmp_path / "reminder_delivery_claims.sqlite3"
    monkeypatch.setattr(notes, "_scheduler_ref", scheduler)

    with pytest.raises(actions.TaskNoop, match="Deferred 1 reminder"):
        await actions.action_ping_notes("alice")
    assert note.due_date == original_due
    pending = scheduler.pending_notifications("alice")
    assert len(pending) == 1

    ack = scheduler.acknowledge_notifications_detailed("alice", [pending[0]["id"]])
    assert ack == {"acknowledged": 1, "reminder_acknowledged": 1}
    (tmp_path / "note_pings_alice.json").unlink(missing_ok=True)

    result, ok = await actions.action_ping_notes("alice")
    assert ok is True
    assert "Pinged 1 note" in result
    assert note.due_date != original_due
    assert db.commits == 1


def test_durable_claim_recovers_after_crashed_attempt_lease(tmp_path):
    from src.reminder_delivery_claims import claim_reminder_delivery

    path = tmp_path / "claims.sqlite3"
    now = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
    first = claim_reminder_delivery(
        path, owner="alice", note_id="note-1", occurrence="due-1",
        channel="telegram", now=now,
    )
    blocked = claim_reminder_delivery(
        path, owner="alice", note_id="note-1", occurrence="due-1",
        channel="telegram", now=now + timedelta(minutes=1),
    )
    recovered = claim_reminder_delivery(
        path, owner="alice", note_id="note-1", occurrence="due-1",
        channel="telegram", now=now + timedelta(minutes=6),
    )

    assert first.acquired is True
    assert blocked == blocked.__class__(False, reason="in_flight")
    assert recovered.acquired is True
    assert recovered.token != first.token


class _NoteQuery:
    def __init__(self, note):
        self.note = note

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return [self.note]

    def update(self, values, synchronize_session=False):
        del synchronize_session
        for key, value in values.items():
            setattr(self.note, getattr(key, "key", str(key)), value)
        return 1


class _NoteDb:
    def __init__(self, note):
        self.note = note
        self.commits = 0

    def query(self, model):
        return _NoteQuery(self.note)

    def commit(self):
        self.commits += 1

    def rollback(self):
        return None

    def close(self):
        return None


def _due_note(*, age_days=0, repeat="none"):
    due = datetime.now(timezone.utc) - timedelta(days=age_days, seconds=1)
    return SimpleNamespace(
        id="late-note",
        owner="alice",
        archived=False,
        due_date=due.isoformat(),
        repeat=repeat,
        title="Late reminder",
        content="Still needs doing",
        items=None,
        label=None,
        note_type="note",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds_until_due", [1, 60])
async def test_note_scanner_never_sends_future_reminders(
    monkeypatch, tmp_path, seconds_until_due
):
    import core.database as database
    import src.builtin_actions as actions

    note = _due_note()
    note.due_date = (
        datetime.now(timezone.utc) + timedelta(seconds=seconds_until_due)
    ).isoformat()
    db = _NoteDb(note)
    monkeypatch.setattr(actions, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(database, "SessionLocal", lambda: db)

    with pytest.raises(actions.TaskNoop, match="none currently due"):
        await actions.action_ping_notes("alice")
    assert db.commits == 0
    assert note.due_date


@pytest.mark.asyncio
async def test_late_one_off_reminder_catches_up_after_long_downtime(monkeypatch, tmp_path):
    import core.database as database
    import routes.note_routes as note_routes
    import src.builtin_actions as actions
    import src.notification_preferences as preferences

    note = _due_note(age_days=10)
    db = _NoteDb(note)
    monkeypatch.setattr(actions, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(preferences, "load_notification_preferences", lambda owner: {"reminder_channel": "telegram"})
    calls = []

    async def delivered(**kwargs):
        calls.append(kwargs)
        return {"channel": "telegram", "telegram_sent": True, "delivered": True, "acknowledged": True}

    monkeypatch.setattr(note_routes, "dispatch_reminder", delivered)
    result, ok = await actions.action_ping_notes("alice")

    assert ok is True
    assert "Pinged 1 note" in result
    assert calls[0]["occurrence"] == note.due_date
    cache = json.loads((tmp_path / "note_pings_alice.json").read_text())
    assert cache[note.id]["at"]
    assert cache[note.id]["occurrence"] == note.due_date


@pytest.mark.asyncio
async def test_failed_recurring_delivery_is_retryable_and_does_not_advance(monkeypatch, tmp_path):
    import core.database as database
    import routes.note_routes as note_routes
    import src.builtin_actions as actions
    import src.notification_preferences as preferences

    note = _due_note(repeat="daily")
    original_due = note.due_date
    db = _NoteDb(note)
    monkeypatch.setattr(actions, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(preferences, "load_notification_preferences", lambda owner: {"reminder_channel": "telegram"})

    async def failed(**kwargs):
        return {
            "channel": "telegram",
            "telegram_sent": False,
            "browser_sent": True,
            "delivered": False,
            "acknowledged": False,
            "telegram_error": "temporary failure",
        }

    monkeypatch.setattr(note_routes, "dispatch_reminder", failed)
    result, ok = await actions.action_ping_notes("alice")

    assert ok is False
    assert "retry scheduled" in result
    assert note.due_date == original_due
    assert db.commits == 0
    cache = json.loads((tmp_path / "note_pings_alice.json").read_text())
    assert "attempt_at" in cache[note.id]
    assert "at" not in cache[note.id]


@pytest.mark.asyncio
async def test_quiet_hours_defer_is_reported_without_advancing(monkeypatch, tmp_path):
    import core.database as database
    import routes.note_routes as note_routes
    import src.builtin_actions as actions
    import src.notification_preferences as preferences

    note = _due_note(repeat="daily")
    original_due = note.due_date
    db = _NoteDb(note)
    monkeypatch.setattr(actions, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(preferences, "load_notification_preferences", lambda owner: {"reminder_channel": "telegram"})

    async def deferred(**kwargs):
        return {"channel": "telegram", "deferred": True, "acknowledged": False, "suppression_reason": "quiet_hours"}

    monkeypatch.setattr(note_routes, "dispatch_reminder", deferred)
    with pytest.raises(actions.TaskNoop, match="Deferred 1 reminder"):
        await actions.action_ping_notes("alice")
    assert note.due_date == original_due
    assert db.commits == 0


@pytest.mark.asyncio
async def test_profile_telegram_test_requires_telegram_sent(monkeypatch):
    import routes.note_routes as note_routes
    import routes.telegram_routes as routes

    monkeypatch.setattr(routes, "load_telegram_config", lambda: _config())
    router = routes.setup_telegram_routes(object())
    endpoint = next(route.endpoint for route in router.routes if route.path == "/api/telegram/test")

    async def failed(**kwargs):
        return {"telegram_sent": False, "telegram_error": "not delivered"}

    monkeypatch.setattr(note_routes, "dispatch_reminder", failed)
    with pytest.raises(HTTPException) as exc:
        await endpoint(owner="alice")
    assert exc.value.status_code == 502

    async def sent(**kwargs):
        return {"telegram_sent": True}

    monkeypatch.setattr(note_routes, "dispatch_reminder", sent)
    assert await endpoint(owner="alice") == {"ok": True, "telegram_sent": True}


def test_v2_ui_exposes_polling_bot_setup_and_checks_telegram_delivery():
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    js = (root / "static" / "js" / "settings.js").read_text(encoding="utf-8")

    assert 'id="set-telegram-bot-token"' in html
    assert 'type="password"' in html
    assert '<option value="polling">Local polling (recommended)</option>' in html
    assert 'id="set-telegram-webhook-replace-row" style="display:flex' in html
    assert 'enable Replace existing webhook to migrate it here' in html
    assert 'id="set-telegram-link-status" role="status" aria-live="polite"' in html
    assert 'data-notification-topic="projects"' in html
    assert "telegramWebhookReplaceRow.style.display = 'flex'" in js
    assert 'replace_existing_webhook: !!telegramWebhookReplace?.checked' in js
    assert 'telegramLinkStatusPoller?.start(telegramLinkExpiresAt)' in js
    assert 'resumeTelegramLinkStatus()' in js
    assert "fetch('/api/telegram/me'" in js
    assert 'stopTelegramLinkStatusPolling();' in js
    assert "if (!res.ok || !data.telegram_sent)" in js
    assert "channelSel.value === 'telegram'" in js and "!data.telegram_sent" in js


def test_telegram_link_status_poller_links_expires_and_cancels_with_no_timer_leaks():
    root = Path(__file__).resolve().parents[1]
    module_path = root / "static" / "js" / "telegramOnboarding.js"
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

(async () => {
  const source = fs.readFileSync(process.argv[1], 'utf8');
  const context = vm.createContext({ console });
  const mod = new vm.SourceTextModule(source, { context, identifier: 'telegramOnboarding.js' });
  await mod.link(() => { throw new Error('unexpected import'); });
  await mod.evaluate();
  const { createTelegramLinkStatusPoller } = mod.namespace;

  let clock = 1_000_000;
  let nextTimer = 1;
  const timers = new Map();
  const setTimer = (fn, delay) => {
    const id = nextTimer++;
    timers.set(id, { fn, delay });
    return id;
  };
  const clearTimer = id => timers.delete(id);
  const runNext = async () => {
    const [id, timer] = timers.entries().next().value;
    assert.ok(timer, 'expected a scheduled status poll');
    timers.delete(id);
    clock += timer.delay;
    await timer.fn();
  };

  let refreshes = 0;
  let linked = 0;
  let expired = 0;
  const poller = createTelegramLinkStatusPoller({
    refresh: async () => ({ linked: ++refreshes >= 2 }),
    onLinked: () => { linked += 1; },
    onExpired: () => { expired += 1; },
    now: () => clock,
    setTimer,
    clearTimer,
  });
  assert.equal(poller.start((clock + 10_000) / 1000), true);
  assert.equal(timers.size, 1);
  await runNext();
  assert.equal(refreshes, 1);
  assert.equal(timers.size, 1);
  await runNext();
  assert.equal(linked, 1);
  assert.equal(expired, 0);
  assert.equal(poller.isRunning(), false);
  assert.equal(timers.size, 0);

  const expiring = createTelegramLinkStatusPoller({
    refresh: async () => { throw new Error('must not poll beyond expiry'); },
    onExpired: () => { expired += 1; },
    now: () => clock,
    setTimer,
    clearTimer,
  });
  expiring.start((clock + 1000) / 1000);
  await runNext();
  assert.equal(expired, 1);
  assert.equal(expiring.isRunning(), false);
  assert.equal(timers.size, 0);

  const cancelled = createTelegramLinkStatusPoller({
    refresh: async () => ({ linked: false }),
    now: () => clock,
    setTimer,
    clearTimer,
  });
  cancelled.start((clock + 10_000) / 1000);
  cancelled.stop();
  assert.equal(cancelled.isRunning(), false);
  assert.equal(timers.size, 0);
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
"""
    result = subprocess.run(
        ["node", "--experimental-vm-modules", "-e", script, str(module_path)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_digest_renderer_includes_only_selected_profile_topics():
    from src.builtin_actions import _render_telegram_digest_sections

    text = _render_telegram_digest_sections(
        datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc),
        {"todos", "projects"},
        {
            "email": ["private email subject"],
            "todos": ["Finish report"],
            "calendar": ["Secret meeting"],
            "projects": ["REST: Restia V2 (3 open)"],
        },
    )

    assert "To do:" in text and "Finish report" in text
    assert "Projects:" in text and "REST: Restia V2" in text
    assert "Email:" not in text and "private email subject" not in text
    assert "Calendar:" not in text and "Secret meeting" not in text


@pytest.mark.asyncio
async def test_digest_cadence_durable_state_prevents_scheduler_retry_duplicate(monkeypatch, tmp_path):
    import src.builtin_actions as actions
    import src.notification_preferences as preferences
    import src.telegram_bot as telegram

    monkeypatch.setattr(actions, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(telegram, "load_telegram_config", lambda: _config())
    monkeypatch.setattr(preferences, "load_notification_preferences", lambda owner: {
        "digest_cadence": "every_6_hours",
        "notification_topics": ["todos"],
        "timezone": "UTC",
        "quiet_hours_enabled": False,
    })
    monkeypatch.setattr(preferences, "quiet_hours_active", lambda prefs, **kwargs: False)
    (tmp_path / "telegram_digest_alice.json").write_text(json.dumps({
        "last_sent_at": datetime.now(timezone.utc).isoformat(),
        "cadence": "every_6_hours",
    }))

    with pytest.raises(actions.TaskNoop, match="6-hour cadence is not due"):
        await actions.action_telegram_hourly_digest("alice")


@pytest.mark.asyncio
async def test_partial_digest_retries_only_failed_chat_with_original_cycle(monkeypatch, tmp_path):
    import src.builtin_actions as actions

    path = tmp_path / "telegram_digest_alice.json"
    started = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
    calls = []

    async def first_send(token, chat_id, text):
        calls.append((str(chat_id), text))
        if str(chat_id) == "2":
            raise RuntimeError("temporary")

    result, ok = await actions._deliver_telegram_digest_cycle(
        state_path=path,
        state={},
        chat_ids=["1", "2"],
        text="original digest",
        cadence="every_6_hours",
        cycle_started_at=started,
        bot_token="token",
        send_message=first_send,
    )
    assert ok is False and "1 chat(s) pending retry" in result
    state = json.loads(path.read_text())
    assert set(state["pending"]["sent"]) == {"1"}

    async def retry_send(token, chat_id, text):
        calls.append((str(chat_id), text))

    result, ok = await actions._deliver_telegram_digest_cycle(
        state_path=path,
        state=json.loads(path.read_text()),
        chat_ids=["1", "2"],
        text="newly rendered text must be ignored",
        cadence="every_6_hours",
        cycle_started_at=started + timedelta(hours=1),
        bot_token="token",
        send_message=retry_send,
    )

    assert ok is True and "retry completed" in result
    assert calls == [
        ("1", "original digest"),
        ("2", "original digest"),
        ("2", "original digest"),
    ]
    state = json.loads(path.read_text())
    assert "pending" not in state
    assert state["last_sent_at"] == started.isoformat()
    assert state["chat_deliveries"]["1"]["cycle_id"] == started.isoformat()
    assert state["chat_deliveries"]["2"]["cycle_id"] == started.isoformat()


def test_urgent_email_dispatch_uses_email_topic():
    import inspect
    import src.builtin_actions as actions

    source = inspect.getsource(actions.action_check_email_urgency)
    call = source[source.index('note_id="urgent-email"'):]
    assert 'topic="email"' in call[:240]


def test_notification_clients_keep_suppressed_and_deferred_responses_silent():
    root = Path(__file__).resolve().parents[1]
    notes = (root / "static" / "js" / "notes.js").read_text(encoding="utf-8")
    calendar = (root / "static" / "js" / "calendar" / "reminders.js").read_text(encoding="utf-8")
    tasks = (root / "static" / "js" / "tasks.js").read_text(encoding="utf-8")

    assert "data && data.suppressed" in notes
    assert "data && data.deferred && !data.show_browser" in notes
    assert "data && data.browser_sent && data.show_browser" in notes
    assert "result.browser_sent && result.show_browser" in calendar
    assert "!result.suppressed && !result.skipped" in calendar
    assert "ack.reminder_acknowledged > 0" in calendar
    assert "/api/tasks/notifications/ack" in notes
    assert "/api/tasks/notifications/ack" in calendar
    assert "/api/tasks/notifications/ack" in tasks
    assert "window.addEventListener('load', startNotificationPolling" in tasks
    assert "_pollTaskNotifications();" in tasks


def test_browser_notification_outbox_survives_restart_overflow_and_requires_ack(tmp_path):
    from src.task_scheduler import TaskScheduler

    path = tmp_path / "browser-outbox.sqlite3"
    before_restart = TaskScheduler(None)
    before_restart._notification_outbox_path = path
    ids = []
    for index in range(75):
        item = before_restart.add_notification(
            task_name=f"Reminder {index}", status="success",
            task_id=f"note-{index}", owner="alice", body="Due now",
        )
        ids.append(item["id"])
    assert len(before_restart._pending_notifications) == 50  # legacy memory cap remains harmless

    after_restart = TaskScheduler(None)
    after_restart._notification_outbox_path = path
    first_read = after_restart.pending_notifications("alice")
    second_read = after_restart.pending_notifications("alice")
    assert len(first_read) == 75
    assert [item["id"] for item in second_read] == [item["id"] for item in first_read]

    assert after_restart.acknowledge_notifications("bob", ids[:5]) == 0
    assert after_restart.acknowledge_notifications("alice", ids[:5]) == 5
    assert len(after_restart.pending_notifications("alice")) == 70

    # A stable logical occurrence key prevents a broken external channel's
    # retries from accumulating duplicate browser mirrors.
    first = after_restart.add_notification(
        "External reminder", "success", "reminder-1", owner="alice",
        body="Still due", dedupe_key="reminder:note-1:occurrence-1",
    )
    duplicate = after_restart.add_notification(
        "External reminder", "success", "reminder-1", owner="alice",
        body="Still due", dedupe_key="reminder:note-1:occurrence-1",
    )
    assert duplicate["id"] == first["id"]
    assert duplicate["_outbox_created"] is False
    assert len(after_restart.pending_notifications("alice")) == 71

    # Auth-disabled notifications normalize to one concrete local owner and
    # remain available after a scheduler restart.
    local_item = after_restart.add_notification(
        "Local reminder", "success", "local-1", owner=None, body="Offline-safe",
    )
    local_restart = TaskScheduler(None)
    local_restart._notification_outbox_path = path
    from src.auth_helpers import DEFAULT_LOCAL_OWNER
    assert local_item["id"] in {
        item["id"] for item in local_restart.pending_notifications(DEFAULT_LOCAL_OWNER)
    }


@pytest.mark.asyncio
async def test_notification_api_uses_stable_owner_when_auth_is_disabled(monkeypatch):
    import routes.task_routes as routes
    from src.auth_helpers import DEFAULT_LOCAL_OWNER

    calls = []

    class Scheduler:
        def pending_notifications(self, owner):
            calls.append(("read", owner))
            return [{"id": "n1"}]

        def acknowledge_notifications(self, owner, ids):
            calls.append(("ack", owner, ids))
            return len(ids)

    class Request:
        state = SimpleNamespace(current_user=None)

        async def json(self):
            return {"ids": ["n1"]}

    monkeypatch.setattr(routes, "require_user", lambda request: "")
    monkeypatch.setattr(routes, "get_current_user", lambda request: None)
    monkeypatch.setattr(
        routes,
        "resolved_request_owner",
        lambda request, admitted_user="": DEFAULT_LOCAL_OWNER,
    )
    router = routes.setup_task_routes(Scheduler())
    get_endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/tasks/notifications" and "GET" in route.methods
    )
    ack_endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/tasks/notifications/ack" and "POST" in route.methods
    )

    assert await get_endpoint(Request()) == {"notifications": [{"id": "n1"}]}
    assert await ack_endpoint(Request()) == {
        "ok": True,
        "acknowledged": 1,
        "reminder_acknowledged": 0,
    }
    assert calls == [
        ("read", DEFAULT_LOCAL_OWNER),
        ("ack", DEFAULT_LOCAL_OWNER, ["n1"]),
    ]


@pytest.mark.asyncio
async def test_notification_api_merges_profile_and_legacy_single_user_outboxes(
    monkeypatch, tmp_path
):
    import routes.task_routes as routes
    from src.auth_helpers import DEFAULT_LOCAL_OWNER
    from src.task_scheduler import TaskScheduler

    scheduler = TaskScheduler(None)
    scheduler._notification_outbox_path = tmp_path / "browser-outbox.sqlite3"
    scheduler._reminder_claim_path = tmp_path / "reminder-claims.sqlite3"
    profile_item = scheduler.add_notification(
        "Profile reminder", "success", "profile-1", owner="alice", body="Due"
    )
    legacy_item = scheduler.add_notification(
        "Legacy automation", "success", "legacy-1", owner=None, body="Finished"
    )

    class Request:
        state = SimpleNamespace(current_user=None)

        async def json(self):
            return {"ids": [profile_item["id"], legacy_item["id"]]}

    monkeypatch.setattr(routes, "require_user", lambda request: "")
    monkeypatch.setattr(
        routes, "resolved_request_owner", lambda request, admitted_user="": "alice"
    )
    monkeypatch.setattr(
        routes,
        "allows_legacy_null_owner",
        lambda request, admitted_user="": True,
    )
    router = routes.setup_task_routes(scheduler)
    get_endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/tasks/notifications" and "GET" in route.methods
    )
    ack_endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/tasks/notifications/ack" and "POST" in route.methods
    )

    pending = await get_endpoint(Request())
    assert {item["id"] for item in pending["notifications"]} == {
        profile_item["id"],
        legacy_item["id"],
    }
    acknowledged = await ack_endpoint(Request())
    assert acknowledged == {
        "ok": True,
        "acknowledged": 2,
        "reminder_acknowledged": 0,
    }
    assert scheduler.pending_notifications("alice") == []
    assert scheduler.pending_notifications(DEFAULT_LOCAL_OWNER) == []


@pytest.mark.asyncio
async def test_poller_retries_before_offset_then_dead_letters_poison_update(tmp_path):
    service, authority, factory, fingerprint = _leased_polling_service(
        tmp_path, label="poison"
    )
    attempts = []

    async def poison(update):
        attempts.append(update["update_id"])
        raise RuntimeError("unsafe message content must not be persisted")

    service.configure(poison)
    update = {"update_id": 100, "message": {"text": "private text"}}
    with pytest.raises(RuntimeError):
        await service._process_updates([update])
    assert service._offset is None
    with pytest.raises(RuntimeError):
        await service._process_updates([update])
    assert service._offset is None

    await service._process_updates([update])
    assert service._offset == 101
    assert attempts == [100, 100, 100]
    assert authority.dead_letter_count(fingerprint) == 1
    from core.database import TelegramDeadLetter

    db = factory()
    try:
        row = db.query(TelegramDeadLetter).one()
    finally:
        db.close()
    assert (row.update_id, row.error_type, row.attempts) == (
        100, "RuntimeError", 3
    )


@pytest.mark.asyncio
async def test_poller_offset_survives_restart_and_is_scoped_to_bot(monkeypatch, tmp_path):
    handled = []
    first, authority, _factory, fingerprint = _leased_polling_service(
        tmp_path, label="offset"
    )
    first.configure(lambda update: asyncio.sleep(0, result=handled.append(update["update_id"])))
    await first._process_updates([{"update_id": 40, "message": {}}])

    assert authority.polling_cursor(fingerprint) == 41
    different = hashlib.sha256(b"different-bot").hexdigest()
    assert authority.polling_cursor(different) is None
    assert handled == [40]


def test_multi_profile_note_scheduler_excludes_legacy_null_owner_rows():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.database import Note
    from src.auth_helpers import owner_filter

    engine = create_engine("sqlite:///:memory:")
    Note.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    try:
        session.add_all([
            Note(id="alice-note", owner="alice", title="A", due_date="2026-07-16T08:00:00Z"),
            Note(id="bob-note", owner="bob", title="B", due_date="2026-07-16T08:00:00Z"),
            Note(id="legacy-note", owner=None, title="Legacy", due_date="2026-07-16T08:00:00Z"),
        ])
        session.commit()
        alice = owner_filter(session.query(Note), Note, "alice", include_shared=False).all()
        bob = owner_filter(session.query(Note), Note, "bob", include_shared=False).all()
        assert [note.id for note in alice] == ["alice-note"]
        assert [note.id for note in bob] == ["bob-note"]
    finally:
        session.close()
        engine.dispose()

    source = Path(__file__).resolve().parents[1] / "src" / "builtin_actions.py"
    assert "owner_filter(q, _N, owner, include_shared=False)" in source.read_text(encoding="utf-8")


def test_digest_project_visibility_includes_members_without_cross_owner_leakage():
    from sqlalchemy import create_engine, func, or_
    from sqlalchemy.orm import sessionmaker

    from core.database import Project, ProjectMember

    engine = create_engine("sqlite:///:memory:")
    Project.__table__.create(engine)
    ProjectMember.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    try:
        session.add_all([
            Project(id="owned", owner="alice", key="OWN", name="Owned"),
            Project(id="shared", owner="carol", key="SHR", name="Shared"),
            Project(id="private", owner="carol", key="PRV", name="Private"),
            ProjectMember(project_id="shared", username="Bob", role="viewer"),
        ])
        session.commit()

        def visible(actor):
            member_ids = session.query(ProjectMember.project_id).filter(
                func.lower(ProjectMember.username) == actor.lower()
            )
            return {
                row.id for row in session.query(Project).filter(
                    or_(func.lower(Project.owner) == actor.lower(), Project.id.in_(member_ids)),
                    Project.archived == False,  # noqa: E712
                    Project.completed_at.is_(None),
                ).all()
            }

        assert visible("alice") == {"owned"}
        assert visible("bob") == {"shared"}
        assert visible("carol") == {"shared", "private"}
    finally:
        session.close()
        engine.dispose()


def test_unlink_falls_stale_telegram_preferences_back_to_browser(monkeypatch):
    import routes.telegram_routes as routes

    state = {"reminder_channel": "telegram", "reminder_telegram_mirror": True, "digest_cadence": "off"}
    monkeypatch.setattr(routes, "unlink_telegram_owner", lambda owner: 2)
    monkeypatch.setattr(routes, "load_notification_preferences", lambda owner: dict(state))

    def save(owner, patch):
        state.update(patch)
        return dict(state)

    monkeypatch.setattr(routes, "save_notification_preferences", save)
    monkeypatch.setattr(routes, "_sync_digest_task", lambda owner, prefs: None)
    router = routes.setup_telegram_routes(object())
    endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/telegram/link" and "DELETE" in route.methods
    )
    result = endpoint(owner="alice")

    assert result["removed"] == 2
    assert result["reminder_channel"] == "browser"
    assert result["reminder_telegram_mirror"] is False
    settings_js = (Path(__file__).resolve().parents[1] / "static/js/settings.js").read_text()
    assert "(!telegramConfigured || !telegramLinked)" in settings_js


@pytest.mark.parametrize(
    ("cadence", "digest_time", "expected"),
    [
        ("hourly", "08:00", "0 * * * *"),
        ("every_3_hours", "08:00", "0 */3 * * *"),
        ("every_6_hours", "08:00", "0 */6 * * *"),
        ("daily", "07:35", "35 7 * * *"),
    ],
)
def test_digest_task_uses_cadence_specific_cron(monkeypatch, cadence, digest_time, expected):
    import core.database as database
    import routes.telegram_routes as routes
    import src.task_scheduler as scheduler

    task = SimpleNamespace(
        prompt="", status="paused", next_run=None,
        cron_expression="0 * * * *",
    )

    class Query:
        def filter(self, *args):
            return self

        def first(self):
            return task

    class Db:
        def query(self, model):
            return Query()

        def commit(self):
            return None

        def close(self):
            return None

    calls = []
    monkeypatch.setattr(database, "SessionLocal", lambda: Db())

    def next_run(*args, **kwargs):
        calls.append(kwargs)
        return datetime(2026, 7, 16, 9, 0)

    monkeypatch.setattr(scheduler, "compute_next_run", next_run)
    routes._sync_digest_task("alice", {
        "digest_cadence": cadence,
        "digest_time": digest_time,
        "timezone": "Asia/Kolkata",
    })

    assert task.cron_expression == expected
    assert calls[0]["cron_expression"] == expected
    assert calls[0]["tz_name"] == "Asia/Kolkata"


@pytest.mark.asyncio
async def test_failed_new_bot_configuration_never_detaches_working_old_webhook(monkeypatch):
    import routes.telegram_routes as routes

    old_token = "111111:abcdefghijklmnopqrstuvwxyz_123456"
    new_token = "222222:abcdefghijklmnopqrstuvwxyz_123456"
    settings = {
        "telegram_bot_token": old_token,
        "telegram_bot_id": "111",
        "telegram_registered_webhook_url": "https://old.example/api/telegram/webhook",
        "telegram_webhook_secret": "old-secret",
    }
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(routes, "require_admin", lambda request: None)
    monkeypatch.setattr(routes, "load_settings", lambda: dict(settings))
    monkeypatch.setattr(routes, "load_telegram_config", lambda: _config(bot_token=old_token))
    monkeypatch.setattr(
        routes, "inspect_telegram_bot",
        lambda token: asyncio.sleep(0, result={"id": "222", "username": "new", "first_name": "New"}),
    )
    monkeypatch.setattr(
        routes, "inspect_telegram_webhook",
        lambda token: asyncio.sleep(0, result={"url": "", "pending_update_count": 0}),
    )

    async def fail_new(**kwargs):
        raise routes.TelegramWebhookConflict("foreign webhook")

    monkeypatch.setattr(routes, "configure_telegram_transport", fail_new)
    api_calls = []

    async def api_call(*args, **kwargs):
        api_calls.append((args, kwargs))
        return {"ok": True}

    monkeypatch.setattr(routes, "telegram_api_call", api_call)
    router = routes.setup_telegram_routes(object())
    endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/telegram/config" and "PUT" in route.methods
    )
    with pytest.raises(HTTPException) as exc:
        await endpoint(
            request=SimpleNamespace(),
            body=routes.TelegramConfigUpdate(bot_token=new_token, mode="polling"),
        )

    assert exc.value.status_code == 409
    assert api_calls == []


@pytest.mark.asyncio
async def test_same_bot_save_failure_restores_owned_webhook_secret(monkeypatch):
    import routes.telegram_routes as routes

    token = "111111:abcdefghijklmnopqrstuvwxyz_123456"
    old_url = "https://old.example/api/telegram/webhook"
    settings = {
        "telegram_bot_token": token,
        "telegram_bot_id": "111",
        "telegram_bot_username": "old",
        "telegram_registered_webhook_url": old_url,
        "telegram_webhook_secret": "old-secret",
    }
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(routes, "require_admin", lambda request: None)
    monkeypatch.setattr(routes, "load_settings", lambda: dict(settings))
    monkeypatch.setattr(routes, "load_telegram_config", lambda: _config(bot_token=token))
    monkeypatch.setattr(
        routes, "inspect_telegram_bot",
        lambda value: asyncio.sleep(0, result={"id": "111", "username": "old", "first_name": "Old"}),
    )
    monkeypatch.setattr(
        routes, "inspect_telegram_webhook",
        lambda value: asyncio.sleep(0, result={"url": old_url, "pending_update_count": 0}),
    )
    monkeypatch.setattr(
        routes, "configure_telegram_transport",
        lambda **kwargs: asyncio.sleep(0, result={"mode": "polling", "webhook_url": ""}),
    )
    monkeypatch.setattr(routes, "save_settings", lambda values: (_ for _ in ()).throw(OSError("disk full")))
    api_calls = []

    async def api_call(bot_token, method, **kwargs):
        api_calls.append((bot_token, method, kwargs.get("payload")))
        return {"ok": True}

    monkeypatch.setattr(routes, "telegram_api_call", api_call)
    router = routes.setup_telegram_routes(object())
    endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/telegram/config" and "PUT" in route.methods
    )
    with pytest.raises(HTTPException) as exc:
        await endpoint(
            request=SimpleNamespace(),
            body=routes.TelegramConfigUpdate(bot_token=token, mode="polling"),
        )

    assert exc.value.status_code == 500
    assert api_calls == [(
        token,
        "setWebhook",
        {
            "url": old_url,
            "drop_pending_updates": False,
            "secret_token": "old-secret",
            "allowed_updates": ["message", "edited_message"],
        },
    )]


@pytest.mark.asyncio
async def test_foreign_webhook_takeover_save_failure_performs_no_telegram_mutation(monkeypatch):
    import routes.telegram_routes as routes

    old_token = "111111:abcdefghijklmnopqrstuvwxyz_123456"
    new_token = "222222:abcdefghijklmnopqrstuvwxyz_123456"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setattr(routes, "require_admin", lambda request: None)
    monkeypatch.setattr(routes, "load_settings", lambda: {
        "telegram_bot_token": old_token,
        "telegram_bot_id": "111",
        "telegram_registered_webhook_url": "https://old.example/api/telegram/webhook",
        "telegram_webhook_secret": "old-secret",
    })
    monkeypatch.setattr(routes, "load_telegram_config", lambda: _config(bot_token=old_token))
    monkeypatch.setattr(
        routes, "inspect_telegram_bot",
        lambda token: asyncio.sleep(0, result={"id": "222", "username": "new", "first_name": "New"}),
    )
    monkeypatch.setattr(
        routes, "inspect_telegram_webhook",
        lambda token: asyncio.sleep(0, result={"url": "https://foreign.example/hook", "pending_update_count": 0}),
    )
    mutations = []

    async def configure(**kwargs):
        mutations.append("configure")
        return {"mode": "polling", "webhook_url": ""}

    monkeypatch.setattr(routes, "configure_telegram_transport", configure)
    monkeypatch.setattr(routes, "telegram_api_call", lambda *args, **kwargs: mutations.append("api"))
    monkeypatch.setattr(routes, "save_settings", lambda values: (_ for _ in ()).throw(OSError("disk full")))
    router = routes.setup_telegram_routes(object())
    endpoint = next(
        route.endpoint for route in router.routes
        if route.path == "/api/telegram/config" and "PUT" in route.methods
    )
    with pytest.raises(HTTPException) as exc:
        await endpoint(
            request=SimpleNamespace(),
            body=routes.TelegramConfigUpdate(
                bot_token=new_token,
                mode="polling",
                replace_existing_webhook=True,
            ),
        )

    assert exc.value.status_code == 500
    assert mutations == []


def test_digest_uses_calendar_occurrence_expansion_and_profile_timezone():
    source = (Path(__file__).resolve().parents[1] / "src/builtin_actions.py").read_text(encoding="utf-8")
    digest = source[source.index("async def _action_telegram_hourly_digest_locked"):source.index("async def action_telegram_hourly_digest")]
    assert "_expand_rrule" in digest
    assert "parsed_start.astimezone(zone)" in digest
    assert "CalendarEvent.dtstart >= now" not in digest
    assert "ScheduledTask.action.is_(None)" in digest
    assert "Project.id.in_(membership_ids)" in digest
    assert "_func.lower(Project.owner) == _actor" in digest
    assert "next_run_utc.astimezone(zone)" in digest


@pytest.mark.asyncio
async def test_daily_digest_inside_quiet_hours_defers_and_delivers_once_after_end(monkeypatch, tmp_path):
    import core.database as database
    import src.builtin_actions as actions
    import src.notification_preferences as preferences
    import src.telegram_bot as telegram

    class EmptyQuery:
        def filter(self, *args):
            return self

        def order_by(self, *args):
            return self

        def limit(self, value):
            return self

        def all(self):
            return []

    class EmptyDb:
        def query(self, model):
            return EmptyQuery()

        def close(self):
            return None

    monkeypatch.setattr(actions, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(database, "SessionLocal", lambda: EmptyDb())
    monkeypatch.setattr(telegram, "load_telegram_config", lambda: _config())
    monkeypatch.setattr(preferences, "load_notification_preferences", lambda owner: {
        "digest_cadence": "daily",
        "digest_time": "23:00",
        "notification_topics": ["todos"],
        "timezone": "Asia/Kolkata",
        "quiet_hours_enabled": True,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "07:00",
    })
    sends = []

    async def send(token, chat_id, text):
        sends.append((str(chat_id), text))

    monkeypatch.setattr(telegram, "send_telegram_message", send)
    at_schedule = datetime(2026, 7, 16, 23, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    with pytest.raises(actions.TaskDeferred) as deferred:
        await actions.action_telegram_hourly_digest("alice", _now=at_schedule)
    assert 7 * 60 * 60 <= deferred.value.delay_seconds <= 8 * 60 * 60
    state_path = tmp_path / "telegram_digest_alice.json"
    assert json.loads(state_path.read_text())["quiet_deferred_cycle_at"]
    assert sends == []

    after_quiet = datetime(2026, 7, 17, 7, 0, tzinfo=at_schedule.tzinfo)
    result, ok = await actions.action_telegram_hourly_digest("alice", _now=after_quiet)
    assert ok is True and "sent to 1 chat" in result
    assert len(sends) == 1
    state = json.loads(state_path.read_text())
    assert "quiet_deferred_cycle_at" not in state
    assert state["last_sent_at"] == at_schedule.astimezone(timezone.utc).isoformat()

    with pytest.raises(actions.TaskNoop, match="today's delivery time has not arrived"):
        await actions.action_telegram_hourly_digest(
            "alice", _now=after_quiet + timedelta(minutes=1),
        )
    assert len(sends) == 1
