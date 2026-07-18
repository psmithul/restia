"""Regression tests for owner-scoped model resolution in scheduled actions."""

import json
import sqlite3
from datetime import datetime
from types import SimpleNamespace

import pytest


class _Column:
    def __eq__(self, _other):
        return True

    def __ne__(self, _other):
        return True

    def __ge__(self, _other):
        return True

    def __le__(self, _other):
        return True


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def limit(self, _limit):
        return self

    def all(self):
        return list(self._rows)


class _Db:
    def __init__(self, rows_by_model):
        self._rows_by_model = rows_by_model
        self.commits = 0
        self.closed = False

    def query(self, model):
        return _Query(self._rows_by_model.get(model, []))

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _resolver_spy(monkeypatch, candidates=None):
    from src import task_endpoint

    calls = []

    def fake_candidates(*args, **kwargs):
        calls.append(kwargs.get("owner"))
        if candidates is None:
            return [("http://llm", "model", {})]
        return list(candidates)

    monkeypatch.setattr(task_endpoint, "resolve_task_candidates", fake_candidates)
    return calls


@pytest.mark.asyncio
async def test_classify_events_resolves_llm_for_task_owner(monkeypatch):
    from core import database
    from src.builtin_actions import action_classify_events

    class FakeCalendarEvent:
        dtstart = _Column()
        status = _Column()

    event = SimpleNamespace(
        summary="Demo presentation",
        event_type="work",
        importance="high",
        color=None,
        dtstart=datetime(2026, 1, 1, 9, 0, 0),
        location="",
    )
    db = _Db({FakeCalendarEvent: [event]})
    calls = _resolver_spy(monkeypatch)

    monkeypatch.setattr(database, "CalendarEvent", FakeCalendarEvent)
    monkeypatch.setattr(database, "SessionLocal", lambda: db)

    message, ok = await action_classify_events("alice")

    assert ok is True
    assert "Scanned 1 upcoming event" in message
    assert calls == ["alice"]
    assert db.closed is True


@pytest.mark.asyncio
async def test_learn_sender_signatures_resolves_llm_for_task_owner(monkeypatch):
    from routes import email_helpers
    from src.builtin_actions import action_learn_sender_signatures

    class FakeImap:
        def __init__(self, owner=""):
            self.owner = owner

        def select(self, *_args, **_kwargs):
            return "OK", []

        def search(self, *_args, **_kwargs):
            return "OK", [b"1 2 3"]

        def fetch(self, _uid, _query):
            return "OK", [(None, b"From: Writer <writer@example.com>\r\n\r\n")]

        def logout(self):
            return None

    calls = _resolver_spy(monkeypatch, candidates=[])
    imap_owners = []

    def fake_imap_connect(_account_id=None, owner=""):
        imap_owners.append(owner)
        return FakeImap(owner)

    monkeypatch.setattr(email_helpers, "_imap_connect", fake_imap_connect)

    message, ok = await action_learn_sender_signatures("alice")

    assert ok is False
    assert message == "No LLM endpoint available"
    assert calls == ["alice"]
    assert imap_owners == ["alice"]


@pytest.mark.asyncio
async def test_learn_sender_signatures_writes_owner_scoped_cache(monkeypatch, tmp_path):
    from routes import email_helpers
    from src import llm_core, task_endpoint
    from src.builtin_actions import action_learn_sender_signatures

    db_path = tmp_path / "scheduled_emails.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", db_path)
    email_helpers._init_scheduled_db()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO sender_signatures
            (from_address, owner, signature_text, sample_count, last_built_at, model_used, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "writer@example.com",
                "bob",
                "bob cached signature",
                3,
                "2999-01-01T00:00:00",
                "old-model",
                "llm",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    class FakeImap:
        def select(self, *_args, **_kwargs):
            return "OK", []

        def search(self, *_args, **_kwargs):
            return "OK", [b"1 2 3"]

        def fetch(self, uid, query):
            if "HEADER.FIELDS" in query:
                return "OK", [(None, b"From: Writer <writer@example.com>\r\n\r\n")]
            return "OK", [
                (
                    None,
                    (
                        b"Thanks for the update.\r\n\r\n"
                        b"Regards,\r\n"
                        b"Writer Example\r\n"
                        b"Example Co.\r\n"
                        + str(uid).encode()
                    ),
                )
            ]

        def logout(self):
            return None

    imap_owners = []

    def fake_imap_connect(_account_id=None, owner=""):
        imap_owners.append(owner)
        return FakeImap()

    monkeypatch.setattr(email_helpers, "_imap_connect", fake_imap_connect)
    monkeypatch.setattr(
        task_endpoint,
        "resolve_task_candidates",
        lambda *args, **kwargs: [("http://llm", "alice-model", {})],
    )

    async def fake_llm_call_async(_candidates, **_kwargs):
        return "Writer Example\nExample Co.\nwriter@example.com"

    monkeypatch.setattr(llm_core, "llm_call_async_with_fallback", fake_llm_call_async)

    message, ok = await action_learn_sender_signatures("alice")

    assert ok is True
    assert message.startswith("Learned sigs: 1 found")
    assert imap_owners == ["alice", "alice"]

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT owner, signature_text, model_used
            FROM sender_signatures
            WHERE from_address = ?
            ORDER BY owner
            """,
            ("writer@example.com",),
        ).fetchall()
    finally:
        conn.close()

    assert rows == [
        ("alice", "Writer Example\nExample Co.\nwriter@example.com", "alice-model"),
        ("bob", "bob cached signature", "old-model"),
    ]


@pytest.mark.asyncio
async def test_check_email_urgency_resolves_llm_candidates_for_task_owner(monkeypatch, tmp_path):
    from core import database
    from src import settings
    from src.builtin_actions import TaskNoop, action_check_email_urgency

    class FakeEmailAccount:
        enabled = _Column()
        owner = _Column()
        imap_user = _Column()
        from_address = _Column()

    db = _Db({FakeEmailAccount: []})
    calls = _resolver_spy(monkeypatch)
    settings_owners = []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(database, "EmailAccount", FakeEmailAccount)
    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(
        settings,
        "load_settings",
        lambda owner=None: settings_owners.append(owner) or {},
    )

    with pytest.raises(TaskNoop, match="no email accounts configured"):
        await action_check_email_urgency("alice")

    assert calls == ["alice"]
    assert settings_owners == ["alice"]
    assert db.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("llm_output", "subject", "body", "message_id", "expected_tags", "expected_tier"),
    [
        (
            "not-json",
            "Tomorrow: ask our cofounder Alex anything",
            "Action required: confirm attendance today. Limited time offer.",
            "<newsletter-fallback@example.com>",
            '["newsletter", "marketing"]',
            "reply-soon 0",
        ),
        (
            '{"score":2,"tags":[],"spam":false,"reason":"confirm attendance"}',
            "Tomorrow: ask our cofounder Alex anything",
            "Action required: confirm attendance today. Limited time offer.",
            "<newsletter-model@example.com>",
            '["newsletter", "marketing"]',
            "reply-soon 0",
        ),
        (
            '{"score":3,"tags":["travel","action-needed"],"spam":false,"reason":"tickets require action"}',
            "Tickets on sale: confirm your seat today",
            "Action required: buy now for this limited time concert offer.",
            "<ticket-promo@example.com>",
            '["newsletter", "marketing"]',
            "reply-soon 0",
        ),
        (
            "not-json",
            "Billing benchmarks for founders",
            "Action required: confirm your place. Limited time offer.",
            "<billing-newsletter@example.com>",
            '["newsletter", "marketing"]',
            "reply-soon 0",
        ),
        (
            '{"score":2,"tags":["bills"],"spam":false,"reason":"payment due tomorrow"}',
            "Final notice: invoice payment due",
            "Amount due tomorrow. Pay within 24 hours to avoid a late charge.",
            "<bill@example.com>",
            '["reply-soon", "bills"]',
            "reply-soon 1",
        ),
    ],
)
async def test_email_tagging_bulk_mail_requires_user_specific_consequence(
    monkeypatch,
    tmp_path,
    llm_output,
    subject,
    body,
    message_id,
    expected_tags,
    expected_tier,
):
    """Bulk CTAs stay informational while a real bill remains actionable."""
    from core import database
    from routes import email_helpers
    from src import (
        builtin_actions,
        email_runtime_authority,
        llm_core,
        settings,
        task_endpoint,
    )

    class FakeEmailAccount:
        enabled = _Column()
        owner = _Column()
        imap_user = _Column()
        from_address = _Column()
        id = _Column()

    account = SimpleNamespace(
        id="account-1",
        owner="alice",
        imap_user="alice@example.com",
        from_address="alice@example.com",
        enabled=True,
    )
    db = _Db({FakeEmailAccount: [account]})
    imap_calls = []

    class FakeImap:
        def select(self, *_args, **_kwargs):
            return "OK", []

        def uid(self, command, *_args):
            if command == "SEARCH":
                return "OK", [b"10"]
            if command == "FETCH":
                raw = (
                    b"From: Littlebird <news@example.com>\r\n"
                    + f"Subject: {subject}\r\n".encode()
                    + f"Message-ID: {message_id}\r\n".encode()
                    + b"List-Unsubscribe: <https://example.com/unsubscribe>\r\n"
                    + b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
                    + body.encode()
                )
                return "OK", [(b"10 (UID 10 FLAGS ())", raw)]
            raise AssertionError(command)

        def logout(self):
            return None

    def fake_imap_connect(account_id=None, owner="", **_kwargs):
        imap_calls.append((account_id, owner))
        return FakeImap()

    async def fake_llm(*_args, **_kwargs):
        return llm_output

    async def no_wait(*_args, **_kwargs):
        return None

    scheduled_db = tmp_path / "scheduled-emails.db"
    cache_dir = tmp_path / "email-urgency-cache"
    monkeypatch.setattr(database, "EmailAccount", FakeEmailAccount)
    monkeypatch.setattr(database, "SessionLocal", lambda: db)
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", scheduled_db)
    monkeypatch.setattr(email_helpers, "_imap_connect", fake_imap_connect)
    monkeypatch.setattr(builtin_actions, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(builtin_actions, "EMAIL_URGENCY_CACHE_DIR", str(cache_dir))
    monkeypatch.setattr(settings, "load_settings", lambda owner=None: {})
    monkeypatch.setattr(task_endpoint, "resolve_task_candidates", lambda **_kwargs: [("http://llm", "model", {})])
    monkeypatch.setattr(llm_core, "llm_call_async_with_fallback", fake_llm)
    monkeypatch.setattr(builtin_actions, "wait_for_interactive_quiet", no_wait)
    canonical_tags = {}

    def fake_list_tags(**kwargs):
        row = canonical_tags.get((kwargs.get("account_id"), message_id))
        return [dict(row)] if row else []

    def fake_upsert_tags(**kwargs):
        canonical_tags[(kwargs.get("account_id"), kwargs.get("message_id"))] = {
            **kwargs,
            "account_id": kwargs.get("account_id"),
            "spam_verdict": bool(kwargs.get("spam_verdict")),
        }

    monkeypatch.setattr(email_runtime_authority, "list_email_tag_states", fake_list_tags)
    monkeypatch.setattr(email_runtime_authority, "upsert_email_tag_state", fake_upsert_tags)

    result, ok = await builtin_actions.action_check_email_urgency("alice")

    assert ok is True, result
    assert imap_calls == [("account-1", "alice")]
    row = canonical_tags[("account-1", message_id)]
    assert json.dumps(row["tags"]) == expected_tags
    assert row["spam_verdict"] is False
    assert expected_tier in result
