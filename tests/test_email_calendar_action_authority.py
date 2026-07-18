from __future__ import annotations

import asyncio
import json
from email.message import EmailMessage

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def test_email_calendar_source_keys_are_stable_and_version_is_exact():
    from routes.email_pollers import (
        _email_calendar_expected_version,
        _email_calendar_idempotency_key,
    )

    values = {
        "owner": "alice",
        "message_id": "<message@example.com>",
        "event_ref": "event-1",
        "action": "cancel_event",
    }
    first = _email_calendar_idempotency_key(**values)
    assert first == _email_calendar_idempotency_key(**values)
    assert first.startswith("email-calendar:v1:")
    assert "alice" not in first
    assert "message@example.com" not in first
    for field, changed in (
        ("owner", "bob"),
        ("message_id", "<other@example.com>"),
        ("event_ref", "event-2"),
        ("action", "update_event"),
    ):
        assert _email_calendar_idempotency_key(
            **{**values, field: changed}
        ) != first

    assert _email_calendar_expected_version({"version": 7}) == 7
    for invalid in (None, 0, -1, "7", True):
        with pytest.raises(ValueError, match="exact positive version"):
            _email_calendar_expected_version({"version": invalid})


@pytest.mark.asyncio
async def test_background_email_cancellation_is_only_proposed_and_counted(
    tmp_path, monkeypatch, caplog
):
    import core.database as cdb
    import routes.email_helpers as email_helpers
    import routes.email_pollers as pollers
    import src.email_runtime_authority as email_runtime_authority
    import src.secret_storage as secret_storage
    import src.tool_implementations as tool_implementations
    from core.database import Account, Base

    scheduled_db = tmp_path / "scheduled-email-calendar.db"
    monkeypatch.setattr(email_helpers, "SCHEDULED_DB", scheduled_db)
    monkeypatch.setattr(pollers, "SCHEDULED_DB", scheduled_db)
    email_helpers._init_scheduled_db()
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'email-calendar-authority.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    db.add(Account(id="owner-alice", username="alice"))
    db.commit()
    db.close()
    monkeypatch.setattr(email_runtime_authority, "SessionLocal", factory)

    message = EmailMessage()
    message["Message-ID"] = "<cancel-message@example.com>"
    message["From"] = "organizer@example.com"
    message["To"] = "alice@example.com"
    message["Subject"] = "Customer review cancelled"
    message["Date"] = "Fri, 24 Jul 2026 09:00:00 +0000"
    message.set_content("The customer review scheduled for Friday is cancelled.")
    raw_message = message.as_bytes()

    class FakeImap:
        def select(self, _folder, readonly=True):
            return "OK", [b"1"]

        def uid(self, command, *_args):
            if command == "SEARCH":
                return "OK", [b"7"]
            if command == "FETCH":
                return "OK", [(b"7 (RFC822)", raw_message)]
            raise AssertionError(command)

        def logout(self):
            return "BYE", []

    llm_calls = []
    tool_calls = []

    async def fake_llm(*, messages, **_kwargs):
        llm_calls.append(messages)
        return json.dumps([
            {
                "action": "cancel",
                "uid": "event-to-cancel",
                "version": 4,
            },
            {
                "action": "update",
                "uid": "event-to-update",
                "version": 6,
                "title": "Updated review",
                "date": "2026-07-24T13:00:00",
                "end_date": "2026-07-24T14:00:00",
                "description": "Organizer moved the review",
            },
            {
                "action": "create",
                "title": "New briefing",
                "date": "2026-07-25T09:00:00",
                "end_date": "2026-07-25T10:00:00",
                "description": "Briefing confirmed",
            },
        ])

    async def fake_manage_calendar(content, owner=None):
        payload = json.loads(content)
        tool_calls.append((payload, owner))
        if payload["action"] == "create_event":
            return {
                "exit_code": 0,
                "uid": "created-from-email",
                "proposal_id": "proposal-create",
            }
        return {
            "exit_code": 0,
            "proposal_id": f"proposal-{payload['action']}",
            "proposal_state": "prepared",
            "requires_approval": True,
        }

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(pollers, "_load_settings", lambda: {
        "email_auto_summarize": False,
        "email_auto_reply": False,
        "email_auto_tag": False,
        "email_auto_spam": False,
        "email_auto_calendar": True,
    })
    monkeypatch.setattr(pollers, "_owner_for_email_account", lambda _id: "alice")
    monkeypatch.setattr(pollers, "_imap_connect", lambda *_args, **_kwargs: FakeImap())
    monkeypatch.setattr(
        pollers,
        "_get_email_config",
        lambda *_args, **_kwargs: {"from_address": "alice@example.com"},
    )
    monkeypatch.setattr(
        pollers,
        "resolve_task_candidates",
        lambda **_kwargs: [("http://model.invalid", "model", {})],
    )
    monkeypatch.setattr(pollers, "task_llm_call_async", fake_llm)
    monkeypatch.setattr(tool_implementations, "do_manage_calendar", fake_manage_calendar)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    monkeypatch.setattr(cdb, "get_upcoming_events", lambda *_args, **_kwargs: [
        {
            "uid": "event-to-cancel",
            "title": "Customer review",
            "start": "2026-07-24T11:00:00",
            "version": 4,
        },
        {
            "uid": "event-to-update",
            "title": "Another review",
            "start": "2026-07-24T12:00:00",
            "version": 6,
        },
    ])

    caplog.set_level("INFO")
    result = await pollers._auto_summarize_pass_single(
        account_id="account-alice",
        max_process=1,
    )

    assert len(llm_calls) == 1
    system_prompt = llm_calls[0][0]["content"]
    user_prompt = llm_calls[0][1]["content"]
    assert '"version": 4' in user_prompt
    assert '"version": 6' in user_prompt
    assert "exact uid AND version" in system_prompt
    assert len(tool_calls) == 3
    cancel_call, update_call, create_call = tool_calls
    assert cancel_call == ({
        "action": "cancel_event",
        "uid": "event-to-cancel",
        "version": 4,
        "idempotency_key": pollers._email_calendar_idempotency_key(
            owner="alice",
            message_id="<cancel-message@example.com>",
            event_ref="event-to-cancel",
            action="cancel_event",
        ),
    }, "alice")
    assert update_call[1] == "alice"
    assert update_call[0]["action"] == "update_event"
    assert update_call[0]["uid"] == "event-to-update"
    assert update_call[0]["version"] == 6
    assert update_call[0]["idempotency_key"] == (
        pollers._email_calendar_idempotency_key(
            owner="alice",
            message_id="<cancel-message@example.com>",
            event_ref="event-to-update",
            action="update_event",
        )
    )
    assert create_call[1] == "alice"
    assert create_call[0]["action"] == "create_event"
    assert create_call[0]["idempotency_key"] == (
        pollers._email_calendar_idempotency_key(
            owner="alice",
            message_id="<cancel-message@example.com>",
            event_ref=(
                "New briefing|2026-07-25T09:00:00|"
                "2026-07-25T10:00:00"
            ),
            action="create_event",
        )
    )
    assert "proposed 1 calendar cancellation(s) for human approval" in result
    assert "created 1 calendar event(s)" in result
    assert "cancelled" not in result.lower()
    assert "Proposed cancellation" in caplog.text
    assert "Cancelled event" not in caplog.text

    cached = email_runtime_authority.completed_email_automation_results(
        owner="alice",
        account_id="account-alice",
        operation="calendar",
        message_ids=["<cancel-message@example.com>"],
    )["<cancel-message@example.com>"]
    try:
        assert cached["event_uids"] == [
            "event-to-cancel",
            "event-to-update",
            "created-from-email",
        ]
        assert cached["events_created"] == 3
    finally:
        engine.dispose()
