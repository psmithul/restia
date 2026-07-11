"""Real-time DM features: SSE stream, event bus, edit/delete/react/reply.

Pins the upgraded contract of routes/messaging_routes.py:
  - The event bus fans message / update / typing / read events out to both
    participants, serialized per receiver (`mine` differs), and the SSE
    stream endpoint drops its queue on disconnect.
  - Editing and deleting are sender-only; a foreign id reads as 404 so
    message ids can't be probed for existence.
  - Deletion is soft and idempotent: tombstone with blank body, no reactions.
  - Reactions: one per user per message, set/replace/remove; participants only.
  - Replies must quote a non-deleted message from the same {me, other} pair.
  - Federated conversations (home contact, '@remote' guests) reject
    edit/delete/react and answer typing with ok:false — the Home Link
    protocol doesn't sync any of them.
  - The direct_messages feature-column migration is guarded + idempotent.
"""
import asyncio
import json
import sqlite3
import tempfile
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from fastapi import HTTPException

import core.database as cdb
from core.database import DirectMessage, LinkGuest
import routes.link_routes as lr
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
    # resolve_guest / is_home_contact live in link_routes and hit its session.
    monkeypatch.setattr(lr, "SessionLocal", _TS)
    monkeypatch.delenv("RESTIA_HOME_SERVER", raising=False)
    # Fresh tables + a fresh bus each test so events/counts are deterministic.
    with _ENGINE.begin() as conn:
        conn.exec_driver_sql("DELETE FROM direct_messages")
        conn.exec_driver_sql("DELETE FROM link_guests")
    mr.bus._subscribers.clear()
    yield


def _routes():
    router = mr.setup_messaging_routes()
    by = {}
    for r in router.routes:
        for m in getattr(r, "methods", set()):
            by[(m, r.path)] = r.endpoint
    return by


R = _routes()

SEND = R[("POST", "/api/messages/conversations/{other}")]
GETC = R[("GET", "/api/messages/conversations/{other}")]
READ = R[("POST", "/api/messages/conversations/{other}/read")]
TYPING = R[("POST", "/api/messages/conversations/{other}/typing")]
EDIT = R[("PUT", "/api/messages/msg/{msg_id}")]
DELETE = R[("DELETE", "/api/messages/msg/{msg_id}")]
REACT = R[("POST", "/api/messages/msg/{msg_id}/react")]
STREAM = R[("GET", "/api/messages/stream")]


def _run(coro):
    # asyncio.run() spins up (and tears down) a fresh event loop per call, so
    # these sync tests stay isolated from any loop another test module closed.
    return asyncio.run(coro)


def _send(me, other, body="hi", reply_to_id=None):
    out = _run(SEND(other, mr.SendMessageRequest(body=body, reply_to_id=reply_to_id), _req(me)))
    return out["message"]


def _insert_raw(sender, recipient, body="x"):
    """Insert a row directly — used to fabricate federated-pair messages that
    the normal send path would never create locally."""
    db = _TS()
    try:
        m = DirectMessage(sender=sender, recipient=recipient, body=body,
                          created_at=cdb.utcnow_naive(), read_at=None)
        db.add(m)
        db.commit()
        db.refresh(m)
        return m.id
    finally:
        db.close()


def _add_guest(handle="guest1", status="approved"):
    db = _TS()
    try:
        db.add(LinkGuest(handle=handle, token_hash="x" * 64, status=status,
                         created_at=cdb.utcnow_naive()))
        db.commit()
    finally:
        db.close()


# ── Editing ─────────────────────────────────────────────────────────────────

def test_edit_own_message_ok():
    mid = _send("tester", "alice", "typo")["id"]
    out = _run(EDIT(mid, mr.EditMessageRequest(body="fixed"), _req("tester")))
    assert out["message"]["body"] == "fixed"
    assert out["message"]["edited"] is True
    # The other side reads the edited body, flagged as edited.
    conv = _run(GETC("tester", _req("alice"), after_id=0))
    assert [(m["body"], m["edited"]) for m in conv["messages"]] == [("fixed", True)]


def test_edit_someone_elses_message_reads_as_404():
    mid = _send("tester", "alice", "mine")["id"]
    for user in ("alice", "bob"):   # recipient and third party look identical
        with pytest.raises(HTTPException) as ei:
            _run(EDIT(mid, mr.EditMessageRequest(body="hijack"), _req(user)))
        assert ei.value.status_code == 404


def test_edit_deleted_message_rejected():
    mid = _send("tester", "alice")["id"]
    _run(DELETE(mid, _req("tester")))
    with pytest.raises(HTTPException) as ei:
        _run(EDIT(mid, mr.EditMessageRequest(body="zombie"), _req("tester")))
    assert ei.value.status_code == 400


def test_edit_empty_or_oversize_rejected():
    mid = _send("tester", "alice")["id"]
    for body in ("   ", "x" * (mr.MAX_BODY_LEN + 1)):
        with pytest.raises(HTTPException) as ei:
            _run(EDIT(mid, mr.EditMessageRequest(body=body), _req("tester")))
        assert ei.value.status_code == 400


# ── Soft delete ─────────────────────────────────────────────────────────────

def test_delete_is_soft_and_idempotent():
    mid = _send("tester", "alice", "regret")["id"]
    _run(REACT(mid, mr.ReactRequest(emoji="👍"), _req("alice")))

    out = _run(DELETE(mid, _req("tester")))
    assert out["message"]["deleted"] is True
    assert out["message"]["body"] == ""
    assert out["message"]["reactions"] == {}

    # Double delete returns the same tombstone instead of erroring.
    again = _run(DELETE(mid, _req("tester")))
    assert again["message"]["deleted"] is True
    assert again["message"]["id"] == mid

    # Reactions are cleared in the row itself, not just hidden.
    db = _TS()
    try:
        row = db.query(DirectMessage).filter(DirectMessage.id == mid).one()
        assert row.reactions is None
        assert row.body == ""
        assert row.deleted_at is not None
    finally:
        db.close()


def test_delete_someone_elses_message_reads_as_404():
    mid = _send("tester", "alice")["id"]
    with pytest.raises(HTTPException) as ei:
        _run(DELETE(mid, _req("alice")))
    assert ei.value.status_code == 404


def test_deleted_message_serializes_blank_in_conversation():
    mid = _send("tester", "alice", "secret")["id"]
    _run(DELETE(mid, _req("tester")))
    conv = _run(GETC("tester", _req("alice"), after_id=0))
    (m,) = conv["messages"]
    assert m["deleted"] is True
    assert m["body"] == ""
    assert m["reactions"] == {}


# ── Reactions ───────────────────────────────────────────────────────────────

def test_react_set_replace_remove():
    mid = _send("tester", "alice", "react to me")["id"]

    out = _run(REACT(mid, mr.ReactRequest(emoji="👍"), _req("alice")))
    assert out["message"]["reactions"] == {"alice": "👍"}

    # One reaction per user: a second emoji replaces, not appends.
    out = _run(REACT(mid, mr.ReactRequest(emoji="🎉"), _req("alice")))
    assert out["message"]["reactions"] == {"alice": "🎉"}

    # The sender can react to their own message alongside.
    out = _run(REACT(mid, mr.ReactRequest(emoji="❤️"), _req("tester")))
    assert out["message"]["reactions"] == {"alice": "🎉", "tester": "❤️"}

    # Empty string removes only my reaction.
    out = _run(REACT(mid, mr.ReactRequest(emoji=""), _req("alice")))
    assert out["message"]["reactions"] == {"tester": "❤️"}


def test_react_non_participant_rejected():
    mid = _send("tester", "alice")["id"]
    with pytest.raises(HTTPException) as ei:
        _run(REACT(mid, mr.ReactRequest(emoji="👀"), _req("bob")))
    assert ei.value.status_code == 404


def test_react_to_deleted_message_rejected():
    mid = _send("tester", "alice")["id"]
    _run(DELETE(mid, _req("tester")))
    with pytest.raises(HTTPException) as ei:
        _run(REACT(mid, mr.ReactRequest(emoji="👍"), _req("alice")))
    assert ei.value.status_code == 400


# ── Replies ─────────────────────────────────────────────────────────────────

def test_reply_happy_path_with_truncated_preview():
    orig = _send("tester", "alice", "quote me " + "x" * 300)
    reply = _send("alice", "tester", "re: that", reply_to_id=orig["id"])
    assert reply["reply_to"]["id"] == orig["id"]
    assert reply["reply_to"]["sender"] == "tester"
    assert reply["reply_to"]["body"] == ("quote me " + "x" * 300)[:mr.REPLY_PREVIEW_LEN]

    # The quote survives a plain conversation fetch (batch lookup path).
    conv = _run(GETC("alice", _req("tester"), after_id=0))
    assert conv["messages"][-1]["reply_to"]["id"] == orig["id"]


def test_reply_cross_pair_rejected():
    foreign = _send("tester", "alice", "not bob's thread")["id"]
    with pytest.raises(HTTPException) as ei:
        _send("bob", "tester", "sneaky quote", reply_to_id=foreign)
    assert ei.value.status_code == 400


def test_reply_to_deleted_target_rejected():
    orig = _send("tester", "alice", "going away")["id"]
    _run(DELETE(orig, _req("tester")))
    with pytest.raises(HTTPException) as ei:
        _send("alice", "tester", "too late", reply_to_id=orig)
    assert ei.value.status_code == 400


def test_reply_preview_blanks_when_target_deleted_later():
    orig = _send("tester", "alice", "now you see me")["id"]
    _send("alice", "tester", "re", reply_to_id=orig)
    _run(DELETE(orig, _req("tester")))
    conv = _run(GETC("alice", _req("tester"), after_id=0))
    reply = conv["messages"][-1]
    assert reply["reply_to"] == {"id": orig, "sender": "tester", "body": ""}


# ── Event bus ───────────────────────────────────────────────────────────────

def test_bus_delivers_message_update_and_read_events():
    async def scenario():
        q_alice = mr.bus.subscribe("alice")
        q_tester = mr.bus.subscribe("tester")
        try:
            out = await SEND("alice", mr.SendMessageRequest(body="hello"), _req("tester"))
            mid = out["message"]["id"]

            # New message reaches BOTH ends, serialized per receiver.
            ev, data = q_alice.get_nowait()
            assert ev == "message"
            assert data["with"] == "tester"
            assert data["message"]["mine"] is False
            ev, data = q_tester.get_nowait()
            assert ev == "message"
            assert data["with"] == "alice"
            assert data["message"]["mine"] is True

            # Edit fans out as `update`.
            await EDIT(mid, mr.EditMessageRequest(body="hello!"), _req("tester"))
            ev, data = q_alice.get_nowait()
            assert ev == "update" and data["message"]["edited"] is True
            q_tester.get_nowait()  # sender's copy of the update

            # Reaction fans out as `update` too.
            await REACT(mid, mr.ReactRequest(emoji="👍"), _req("alice"))
            ev, data = q_tester.get_nowait()
            assert ev == "update" and data["message"]["reactions"] == {"alice": "👍"}
            q_alice.get_nowait()

            # Opening the conversation marks read → sender gets `read`.
            await GETC("tester", _req("alice"), after_id=0)
            ev, data = q_tester.get_nowait()
            assert ev == "read" and data["from"] == "alice"
            assert q_tester.empty()   # no spurious second event
        finally:
            mr.bus.unsubscribe("alice", q_alice)
            mr.bus.unsubscribe("tester", q_tester)
    _run(scenario())


def test_explicit_read_endpoint_publishes_only_when_rows_marked():
    async def scenario():
        q_tester = mr.bus.subscribe("tester")
        try:
            await SEND("alice", mr.SendMessageRequest(body="unread"), _req("tester"))
            q_tester.get_nowait()   # drain the send echo

            out = await READ("tester", _req("alice"))
            assert out["marked"] == 1
            ev, data = q_tester.get_nowait()
            assert ev == "read" and data["from"] == "alice"

            # Nothing left unread → no event.
            out = await READ("tester", _req("alice"))
            assert out["marked"] == 0
            assert q_tester.empty()
        finally:
            mr.bus.unsubscribe("tester", q_tester)
    _run(scenario())


def test_typing_pings_partner_only():
    async def scenario():
        q_alice = mr.bus.subscribe("alice")
        q_tester = mr.bus.subscribe("tester")
        try:
            out = await TYPING("alice", _req("tester"))
            assert out == {"ok": True}
            ev, data = q_alice.get_nowait()
            assert ev == "typing" and data == {"from": "tester"}
            assert q_tester.empty()   # typing is not echoed to the typist
        finally:
            mr.bus.unsubscribe("alice", q_alice)
            mr.bus.unsubscribe("tester", q_tester)
    _run(scenario())


# ── SSE stream endpoint ─────────────────────────────────────────────────────

def test_stream_delivers_named_events_and_unsubscribes_on_close():
    async def scenario():
        resp = await STREAM(_req("alice"))
        assert resp.media_type == "text/event-stream"
        agen = resp.body_iterator

        first = await asyncio.wait_for(agen.__anext__(), timeout=2)
        assert first.startswith(":")   # connect preamble (comment line)
        assert "alice" in mr.bus._subscribers

        await SEND("alice", mr.SendMessageRequest(body="over the wire"), _req("tester"))
        frame = await asyncio.wait_for(agen.__anext__(), timeout=2)
        head, _, data = frame.partition("\ndata: ")
        assert head == "event: message"
        payload = json.loads(data)
        assert payload["with"] == "tester"
        assert payload["message"]["body"] == "over the wire"
        assert payload["message"]["mine"] is False

        # Closing the stream must drop the queue from the bus (finally path).
        await agen.aclose()
        assert "alice" not in mr.bus._subscribers
    _run(scenario())


def test_stream_requires_signed_in_account():
    with pytest.raises(HTTPException) as ei:
        _run(STREAM(_req("")))
    assert ei.value.status_code == 403


# ── Federated conversations (home contact / '@remote' guests) ───────────────

def test_federated_pairs_reject_edit_delete_react(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://home.example.com")
    home_mid = _insert_raw("tester", "home.example.com", "to home")
    remote_mid = _insert_raw("tester", "guest1@remote", "to guest")

    for mid in (home_mid, remote_mid):
        with pytest.raises(HTTPException) as ei:
            _run(EDIT(mid, mr.EditMessageRequest(body="nope"), _req("tester")))
        assert ei.value.status_code == 400
        with pytest.raises(HTTPException) as ei:
            _run(DELETE(mid, _req("tester")))
        assert ei.value.status_code == 400
        with pytest.raises(HTTPException) as ei:
            _run(REACT(mid, mr.ReactRequest(emoji="👍"), _req("tester")))
        assert ei.value.status_code == 400
        assert ei.value.detail == "Not available in this conversation"


def test_home_contact_send_rejects_reply(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://home.example.com")
    with pytest.raises(HTTPException) as ei:
        _run(SEND("home.example.com",
                  mr.SendMessageRequest(body="hi", reply_to_id=1), _req("tester")))
    assert ei.value.status_code == 400


def test_typing_to_federated_contacts_is_ok_false(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://home.example.com")
    _add_guest("guest1")
    assert _run(TYPING("home.example.com", _req("tester"))) == {"ok": False}
    assert _run(TYPING("guest1@remote", _req("tester"))) == {"ok": False}


def test_conversation_other_carries_home_and_remote_flags():
    _add_guest("guest1")
    conv = _run(GETC("alice", _req("tester"), after_id=0))
    assert conv["other"]["home"] is False
    assert conv["other"]["remote"] is False

    conv = _run(GETC("guest1@remote", _req("tester"), after_id=0))
    assert conv["other"]["home"] is False
    assert conv["other"]["remote"] is True


# ── Migration ───────────────────────────────────────────────────────────────

def test_migration_adds_dm_feature_columns(tmp_path, monkeypatch):
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        """CREATE TABLE direct_messages (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               sender TEXT NOT NULL, recipient TEXT NOT NULL,
               body TEXT NOT NULL, created_at DATETIME NOT NULL,
               read_at DATETIME)"""
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{db_file}")
    cdb._migrate_add_dm_feature_columns()
    cdb._migrate_add_dm_feature_columns()   # idempotent

    conn = sqlite3.connect(db_file)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(direct_messages)")}
    conn.close()
    assert {"edited_at", "deleted_at", "reply_to_id", "reactions"} <= cols
