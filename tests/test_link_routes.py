"""Home Link routes: hub registration/auth, guest DMs landing in the owner's
inbox, owner replies resolving '@remote' names, and the client-side proxy.

Pins the security guarantees of routes/link_routes.py:
  - Hub endpoints 404 unless LINK_HUB_ENABLED=true.
  - Handles are validated, first-come-first-served, and may not shadow a
    local account (either as 'handle' or 'handle@remote').
  - Guest endpoints require the bearer token issued at registration.
  - Local signups can never take an '@remote' name (impersonation guard).
  - The client proxy surfaces 'link_not_connected' instead of 401 so the
    front-end fetch wrapper never redirects to /login.
"""
import asyncio
import itertools
import tempfile
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.link_routes as lr
import routes.messaging_routes as mr
from routes.auth_routes import username_reserved

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)

USERS = {
    "mika": {"is_admin": True},
    "alice": {"is_admin": False},
}

_host_counter = itertools.count(1)


class _FakeAuth:
    def __init__(self, users):
        self._users = users

    @property
    def users(self):
        return self._users

    def is_admin(self, u):
        return self._users.get(u, {}).get("is_admin", False)


def _req(username=None, bearer=None, host=None):
    """Request stand-in. Unique client host per call so the hub's per-IP rate
    limiters never trip across tests."""
    state = SimpleNamespace(current_user=username, api_token=False)
    app = SimpleNamespace(state=SimpleNamespace(auth_manager=_FakeAuth(USERS)))
    client = SimpleNamespace(host=host or f"10.9.{next(_host_counter) // 250}.{next(_host_counter) % 250}")
    headers = {"authorization": f"Bearer {bearer}"} if bearer else {}
    return SimpleNamespace(state=state, app=app, client=client, headers=headers, cookies={})


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(lr, "SessionLocal", _TS)
    monkeypatch.setattr(mr, "SessionLocal", _TS)
    monkeypatch.setenv("LINK_HUB_ENABLED", "true")
    monkeypatch.delenv("LINK_OWNER", raising=False)
    monkeypatch.delenv("RESTIA_HOME_SERVER", raising=False)
    lr._reset_summary_cache()
    with _ENGINE.begin() as conn:
        conn.exec_driver_sql("DELETE FROM direct_messages")
        conn.exec_driver_sql("DELETE FROM link_guests")
        conn.exec_driver_sql("DELETE FROM home_link")
    yield


def _routes(router):
    by = {}
    for r in router.routes:
        for m in getattr(r, "methods", set()):
            by[(m, r.path)] = r.endpoint
    return by


HUB = _routes(lr.setup_link_hub_routes())
HL = _routes(lr.setup_home_link_routes())
MSG = _routes(mr.setup_messaging_routes())


def _run(coro):
    return asyncio.run(coro)


def _register(handle="guest1"):
    reg = HUB[("POST", "/api/link/register")]
    return _run(reg(lr.RegisterRequest(handle=handle), _req()))


# ── Hub: registration ───────────────────────────────────────────────────────

def test_register_returns_token_and_owner():
    out = _register("visitor")
    assert out["ok"] is True
    assert out["guest"] == "visitor@remote"
    assert out["owner"] == "mika"          # first admin
    assert len(out["token"]) > 30


def test_register_rejects_bad_handles():
    for bad in ("", "UPPER!", "has space", "-leading", "x" * 40):
        with pytest.raises(HTTPException) as e:
            _register(bad)
        assert e.value.status_code == 400


def test_register_handle_is_first_come_first_served():
    _register("dibs")
    with pytest.raises(HTTPException) as e:
        _register("dibs")
    assert e.value.status_code == 409


def test_register_cannot_shadow_local_account():
    with pytest.raises(HTTPException) as e:
        _register("alice")
    assert e.value.status_code == 409


def test_hub_disabled_is_a_404(monkeypatch):
    monkeypatch.setenv("LINK_HUB_ENABLED", "false")
    with pytest.raises(HTTPException) as e:
        _register("anyone")
    assert e.value.status_code == 404


def test_link_owner_env_override(monkeypatch):
    monkeypatch.setenv("LINK_OWNER", "alice")
    assert _register("visitor")["owner"] == "alice"


# ── Hub: messaging round-trip ───────────────────────────────────────────────

def test_guest_and_owner_roundtrip():
    token = _register("visitor")["token"]
    hub_send = HUB[("POST", "/api/link/messages")]
    hub_fetch = HUB[("GET", "/api/link/messages")]
    hub_summary = HUB[("GET", "/api/link/summary")]

    out = _run(hub_send(lr.LinkSendRequest(body="hello mika"), _req(bearer=token)))
    assert out["message"]["mine"] is True
    assert out["message"]["recipient"] == "mika"

    # Owner sees the conversation + unread through the normal DM routes.
    convos = _run(MSG[("GET", "/api/messages/conversations")](_req("mika")))["conversations"]
    assert convos[0]["username"] == "visitor@remote"
    assert convos[0]["unread"] == 1

    # Owner replies to the '@remote' name.
    reply = _run(MSG[("POST", "/api/messages/conversations/{other}")](
        "visitor@remote", mr.SendMessageRequest(body="hey!"), _req("mika")))
    assert reply["message"]["recipient"] == "visitor@remote"

    s = _run(hub_summary(_req(bearer=token)))
    assert s["unread"] == 1 and s["last_body"] == "hey!" and s["last_mine"] is False

    # Guest fetch sees both sides and marks the owner's reply read.
    conv = _run(hub_fetch(_req(bearer=token), after_id=0))
    assert [m["body"] for m in conv["messages"]] == ["hello mika", "hey!"]
    assert [m["mine"] for m in conv["messages"]] == [True, False]
    assert _run(hub_summary(_req(bearer=token)))["unread"] == 0


def test_owner_cannot_message_unregistered_remote_name():
    with pytest.raises(HTTPException) as e:
        _run(MSG[("POST", "/api/messages/conversations/{other}")](
            "ghost@remote", mr.SendMessageRequest(body="hi"), _req("mika")))
    assert e.value.status_code == 404


def test_guest_endpoints_require_valid_token():
    _register("visitor")
    for path, args in (
        (("GET", "/api/link/messages"), {"after_id": 0}),
        (("GET", "/api/link/summary"), {}),
    ):
        with pytest.raises(HTTPException) as e:
            _run(HUB[path](_req(bearer="wrong-token"), **args))
        assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        _run(HUB[("POST", "/api/link/messages")](
            lr.LinkSendRequest(body="x"), _req(bearer="wrong-token")))
    assert e.value.status_code == 401


def test_guest_message_body_is_validated():
    token = _register("visitor")["token"]
    send = HUB[("POST", "/api/link/messages")]
    with pytest.raises(HTTPException) as e:
        _run(send(lr.LinkSendRequest(body="  "), _req(bearer=token)))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        _run(send(lr.LinkSendRequest(body="x" * (lr.MAX_BODY_LEN + 1)), _req(bearer=token)))
    assert e.value.status_code == 400


# ── Impersonation guard ─────────────────────────────────────────────────────

def test_local_accounts_cannot_take_remote_names():
    assert username_reserved("visitor@remote") is True
    assert username_reserved("Visitor@REMOTE  ") is True
    assert username_reserved("visitor") is False


# ── Client side (proxy) ─────────────────────────────────────────────────────

def _fake_hub(monkeypatch, responses):
    """Replace the HTTP seam with canned responses keyed by (method, path)."""
    calls = []

    async def fake(method, path, *, token=None, json_body=None, params=None):
        calls.append({"method": method, "path": path, "token": token,
                      "json": json_body, "params": params})
        return responses[(method, path)]

    monkeypatch.setattr(lr, "_hub_call", fake)
    return calls


def test_home_contact_requires_connect_first(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    with pytest.raises(HTTPException) as e:
        _run(MSG[("GET", "/api/messages/conversations/{other}")](
            "hub.example", _req("alice"), after_id=0))
    assert e.value.status_code == 409
    assert e.value.detail == lr.NOT_CONNECTED


def test_connect_stores_link_and_proxy_flows(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    calls = _fake_hub(monkeypatch, {
        ("POST", "/api/link/register"): {
            "ok": True, "handle": "alice", "owner": "mika", "token": "tok123"},
        ("GET", "/api/link/messages"): {
            "messages": [{"id": 1, "body": "hi", "mine": True,
                          "created_at": None, "read": False}],
            "owner": "mika", "me": "alice@remote"},
        ("POST", "/api/link/messages"): {
            "message": {"id": 2, "body": "yo", "mine": True,
                        "created_at": None, "read": False}},
        ("GET", "/api/link/summary"): {
            "owner": "mika", "unread": 3, "last_body": "yo",
            "last_at": "2026-07-10T00:00:00Z", "last_mine": True},
    })

    out = _run(HL[("POST", "/api/homelink/connect")](
        lr.ConnectRequest(handle="alice"), _req("alice")))
    assert out["ok"] is True and out["owner"] == "mika"

    st = _run(HL[("GET", "/api/homelink/status")](_req("alice")))
    assert st["connected"] is True and st["handle"] == "alice"
    assert st["contact"] == "hub.example"

    conv = _run(MSG[("GET", "/api/messages/conversations/{other}")](
        "hub.example", _req("alice"), after_id=0))
    assert conv["other"]["home"] is True
    assert conv["messages"][0]["body"] == "hi"

    sent = _run(MSG[("POST", "/api/messages/conversations/{other}")](
        "hub.example", mr.SendMessageRequest(body="yo"), _req("alice")))
    assert sent["message"]["body"] == "yo"

    # The stored token authenticates every proxied call after connect.
    assert all(c["token"] == "tok123" for c in calls[1:])

    # Home conversation is merged into the list + unread endpoints.
    convos = _run(MSG[("GET", "/api/messages/conversations")](_req("alice")))["conversations"]
    home = [c for c in convos if c.get("home")]
    assert home and home[0]["username"] == "hub.example" and home[0]["unread"] == 3

    unread = _run(MSG[("GET", "/api/messages/unread")](_req("alice")))
    assert unread["by_user"].get("hub.example") == 3


def test_users_picker_includes_home_contact_and_guests(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    _register("visitor")
    users = _run(MSG[("GET", "/api/messages/users")](_req("mika")))["users"]
    by_name = {u["username"]: u for u in users}
    assert by_name["hub.example"]["home"] is True
    assert by_name["visitor@remote"]["remote"] is True
    assert "alice" in by_name


def test_disconnect_forgets_link(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    _fake_hub(monkeypatch, {
        ("POST", "/api/link/register"): {
            "ok": True, "handle": "alice", "owner": "mika", "token": "tok123"},
    })
    _run(HL[("POST", "/api/homelink/connect")](
        lr.ConnectRequest(handle="alice"), _req("alice")))
    _run(HL[("POST", "/api/homelink/disconnect")](_req("alice")))
    st = _run(HL[("GET", "/api/homelink/status")](_req("alice")))
    assert st["connected"] is False
