from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("mcp")

import core.database as database
import mcp_servers.email_server as email_server
import src.email_outbound as email_outbound


class _FakeDb:
    def __init__(self):
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def test_agent_send_ignores_legacy_toggle_and_never_calls_mail_network(monkeypatch):
    fake_db = _FakeDb()
    captured = {}

    monkeypatch.setattr(database, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(
        email_server,
        "_resolve_account",
        lambda selector: {
            "id": "email-account-1",
            "owner": "alice",
            "name": "Alice Mail",
        },
    )
    monkeypatch.setattr(email_server, "_current_owner", lambda: "alice")
    monkeypatch.setattr(
        email_server,
        "_read_agent_email_confirm_setting",
        lambda: False,
    )

    def prepare(_db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            proposal=SimpleNamespace(id="proposal-1", state="prepared"),
            draft=SimpleNamespace(id="draft-1"),
        )

    monkeypatch.setattr(email_outbound, "prepare_agent_email_action", prepare)

    def network_forbidden(*_args, **_kwargs):
        raise AssertionError("agent outbound attempted mail network")

    monkeypatch.setattr(email_server, "_smtp_connect", network_forbidden)
    monkeypatch.setattr(email_server, "_imap_connect", network_forbidden)

    result = email_server._send_email(
        to="ada@example.test",
        subject="Review",
        body="Exact body",
        cc="team@example.test",
        account="Alice Mail",
        idempotency_key="tool-call-1",
    )

    assert result == {
        "success": True,
        "pending": True,
        "pending_id": "proposal-1",
        "draft_id": "draft-1",
        "action_state": "prepared",
        "requires_confirmation": True,
        "message": (
            "Draft staged for explicit review in Restia. Nothing has been "
            "sent or queued for delivery."
        ),
    }
    assert fake_db.committed and fake_db.closed
    assert captured["owner_username"] == "alice"
    assert captured["email_account_id"] == "email-account-1"
    assert captured["to"] == "ada@example.test"
    assert captured["subject"] == "Review"
    assert captured["body"] == "Exact body"
    assert captured["cc"] == "team@example.test"
    assert captured["kind"] == "new"


def test_reply_requires_exact_review_headers_and_performs_no_imap(monkeypatch):
    monkeypatch.setattr(
        email_server,
        "_imap_connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("reply preparation fetched IMAP")
        ),
    )
    missing = email_server._reply_to_email(
        uid="42",
        body="Thanks.",
        account="Alice Mail",
    )
    assert "error" in missing
    assert "nothing was sent" in missing["error"]

    captured = {}

    def stage(**kwargs):
        captured.update(kwargs)
        return {"pending": True, "pending_id": "proposal-reply"}

    monkeypatch.setattr(email_server, "_send_email", stage)
    result = email_server._reply_to_email(
        uid="42",
        folder="INBOX",
        body="Thanks.",
        to="sender@example.test",
        subject="Status",
        in_reply_to="<message@example.test>",
        references=["<older@example.test>"],
        reply_all=True,
        cc="team@example.test",
        account="Alice Mail",
    )

    assert result["pending"] is True
    assert captured["kind"] == "reply"
    assert captured["to"] == "sender@example.test"
    assert captured["subject"] == "Re: Status"
    assert captured["in_reply_to"] == "<message@example.test>"
    assert captured["references"] == [
        "<older@example.test>",
        "<message@example.test>",
    ]
    assert captured["source_uid"] == "42"
    assert captured["source_folder"] == "INBOX"
    assert captured["cc"] == "team@example.test"
