"""Direct-message routes: pair-scoping (no IDOR), validation, read state.

Pins the security guarantees of routes/messaging_routes.py:
  - A message is only ever visible to its sender or recipient — a third
    account sees an empty conversation and zero unread.
  - Recipients are validated against the real user list (404 on unknown).
  - Self-DMs and empty bodies are rejected; oversize bodies are rejected.
  - Opening a conversation marks the other side's messages to me as read.
  - Anonymous callers (no signed-in user) are refused.
"""
import asyncio
import tempfile
import types
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from fastapi import HTTPException

import core.database as cdb
from core.database import DirectMessage
import routes.messaging_routes as mr

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)

USERS = {
    "tester": {"is_admin": True},
    "alice": {"is_admin": False},
    "bob": {"is_admin": False},
}


class _FakeAuth:
    def __init__(self, users):
        self._users = users

    @property
    def users(self):
        return self._users

    def is_admin(self, u):
        return self._users.get(u, {}).get("is_admin", False)


def _req(username):
    """Build a minimal Request stand-in that middleware would have populated."""
    state = SimpleNamespace(current_user=username, api_token=False)
    app = SimpleNamespace(state=SimpleNamespace(auth_manager=_FakeAuth(USERS)))
    client = SimpleNamespace(host="127.0.0.1")
    return SimpleNamespace(state=state, app=app, client=client, headers={}, cookies={})


@pytest.fixture(autouse=True)
def _patch_session(monkeypatch):
    monkeypatch.setattr(mr, "SessionLocal", _TS)
    # Fresh table each test so counts are deterministic.
    with _ENGINE.begin() as conn:
        conn.exec_driver_sql("DELETE FROM direct_messages")
    yield


def _routes():
    router = mr.setup_messaging_routes()
    by = {}
    for r in router.routes:
        for m in getattr(r, "methods", set()):
            by[(m, r.path)] = r.endpoint
    return by


R = _routes()


def _run(coro):
    # asyncio.run() spins up (and tears down) a fresh event loop per call, so
    # these sync tests stay isolated from any loop another test module closed.
    return asyncio.run(coro)


# ── Happy path ──────────────────────────────────────────────────────────────

def test_send_and_fetch_roundtrip():
    send = R[("POST", "/api/messages/conversations/{other}")]
    getc = R[("GET", "/api/messages/conversations/{other}")]

    out = _run(send("alice", mr.SendMessageRequest(body="hi alice"), _req("tester")))
    assert out["message"]["sender"] == "tester"
    assert out["message"]["recipient"] == "alice"
    assert out["message"]["mine"] is True

    # Alice sees it (not mine, unread until she opens).
    conv = _run(getc("tester", _req("alice"), after_id=0))
    assert [m["body"] for m in conv["messages"]] == ["hi alice"]
    assert conv["messages"][0]["mine"] is False


def test_conversation_list_and_unread():
    send = R[("POST", "/api/messages/conversations/{other}")]
    lst = R[("GET", "/api/messages/conversations")]
    unread = R[("GET", "/api/messages/unread")]

    _run(send("alice", mr.SendMessageRequest(body="one"), _req("tester")))
    _run(send("alice", mr.SendMessageRequest(body="two"), _req("tester")))

    u = _run(unread(_req("alice")))
    assert u["total"] == 2
    assert u["by_user"] == {"tester": 2}

    convos = _run(lst(_req("alice")))["conversations"]
    assert len(convos) == 1
    assert convos[0]["username"] == "tester"
    assert convos[0]["unread"] == 2
    assert convos[0]["last_body"] == "two"


def test_opening_conversation_marks_read():
    send = R[("POST", "/api/messages/conversations/{other}")]
    getc = R[("GET", "/api/messages/conversations/{other}")]
    unread = R[("GET", "/api/messages/unread")]

    _run(send("alice", mr.SendMessageRequest(body="ping"), _req("tester")))
    assert _run(unread(_req("alice")))["total"] == 1

    _run(getc("tester", _req("alice"), after_id=0))  # alice opens
    assert _run(unread(_req("alice")))["total"] == 0


# ── Security: strict pair scoping (no IDOR) ─────────────────────────────────

def test_third_party_cannot_read_conversation():
    send = R[("POST", "/api/messages/conversations/{other}")]
    getc = R[("GET", "/api/messages/conversations/{other}")]
    unread = R[("GET", "/api/messages/unread")]

    _run(send("alice", mr.SendMessageRequest(body="secret between us"), _req("tester")))

    # Bob asks for his conversation with tester — must be empty.
    conv = _run(getc("tester", _req("bob"), after_id=0))
    assert conv["messages"] == []
    # And bob has no unread from this exchange.
    assert _run(unread(_req("bob")))["total"] == 0


def test_users_list_excludes_self():
    users = R[("GET", "/api/messages/users")]
    out = _run(users(_req("tester")))
    names = {u["username"] for u in out["users"]}
    assert "tester" not in names
    # The Home Link contact (routes/link_routes.py) may also be present;
    # local accounts are exactly the non-home entries.
    local = {u["username"] for u in out["users"] if not u.get("home")}
    assert local == {"alice", "bob"}


# ── Validation ──────────────────────────────────────────────────────────────

def test_send_to_unknown_user_404():
    send = R[("POST", "/api/messages/conversations/{other}")]
    with pytest.raises(HTTPException) as ei:
        _run(send("ghost", mr.SendMessageRequest(body="hi"), _req("tester")))
    assert ei.value.status_code == 404


def test_send_to_self_rejected():
    send = R[("POST", "/api/messages/conversations/{other}")]
    with pytest.raises(HTTPException) as ei:
        _run(send("tester", mr.SendMessageRequest(body="hi"), _req("tester")))
    assert ei.value.status_code == 400


def test_empty_body_rejected():
    send = R[("POST", "/api/messages/conversations/{other}")]
    with pytest.raises(HTTPException) as ei:
        _run(send("alice", mr.SendMessageRequest(body="   "), _req("tester")))
    assert ei.value.status_code == 400


def test_oversize_body_rejected():
    send = R[("POST", "/api/messages/conversations/{other}")]
    with pytest.raises(HTTPException) as ei:
        _run(send("alice", mr.SendMessageRequest(body="x" * (mr.MAX_BODY_LEN + 1)), _req("tester")))
    assert ei.value.status_code == 400


def test_anonymous_caller_refused():
    lst = R[("GET", "/api/messages/conversations")]
    with pytest.raises(HTTPException) as ei:
        _run(lst(_req("")))  # no signed-in user
    assert ei.value.status_code == 403


def test_case_insensitive_recipient():
    send = R[("POST", "/api/messages/conversations/{other}")]
    getc = R[("GET", "/api/messages/conversations/{other}")]
    # Sending to "Alice" (mixed case) normalizes to "alice".
    _run(send("Alice", mr.SendMessageRequest(body="hey"), _req("tester")))
    conv = _run(getc("tester", _req("alice"), after_id=0))
    assert [m["body"] for m in conv["messages"]] == ["hey"]
