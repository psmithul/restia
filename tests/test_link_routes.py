"""Home Link routes: hub registration/approval/auth, guest DMs landing in the
owner's inbox, owner replies resolving '@remote' names, and the client proxy.

Pins the security guarantees of routes/link_routes.py:
  - Hub endpoints 404 unless LINK_HUB_ENABLED=true.
  - Registration is approval-gated: a pending (or blocked) guest can't send
    or read anything — both states answer with the same 'link_pending' 403.
  - The register response never reveals the owner's username.
  - Handles are validated, first-come-first-served, capped, and may not
    shadow a local account or a reserved name.
  - Guest endpoints require the bearer token issued at registration.
  - Admin guest management requires an admin session.
  - Local signups can never take an '@remote' name (impersonation guard).
  - The client proxy is installation-scoped, binds credentials to the stored
    origin, and sanitizes everything a (possibly hostile) hub returns.
"""
import asyncio
import itertools
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import routes.call_routes as cr
import routes.link_routes as lr
import routes.messaging_routes as mr
from routes.auth_routes import username_reserved
from pydantic import ValidationError

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

PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCS"
    "AQANHQEDgslx/wAAAABJRU5ErkJggg=="
)

_host_counter = itertools.count(1)


class _FakeAuth:
    is_configured = True

    def __init__(self, users):
        self._users = users

    @property
    def users(self):
        return self._users

    def is_admin(self, u):
        return self._users.get(u, {}).get("is_admin", False)


class _FakeCallAlerts:
    def __init__(self):
        self.pending = {}
        self.begun = []
        self.stopped = []

    def begin(self, **kwargs):
        self.begun.append(kwargs)
        self.pending[(kwargs["owner"], kwargs["transport"], kwargs["call_id"])] = {
            "from": kwargs["peer"], "call_id": kwargs["call_id"],
            "kind": "offer", "data": dict(kwargs["offer_data"]),
        }
        return True

    def stop(self, **kwargs):
        self.stopped.append(kwargs)
        return self.pending.pop(
            (kwargs["owner"], kwargs["transport"], kwargs["call_id"]), None
        ) is not None

    def stop_owner_transport(self, *, owner, transport):
        keys = [key for key in self.pending if key[:2] == (owner, transport)]
        for key in keys:
            self.pending.pop(key, None)
        return len(keys)

    def pending_snapshot(self, *, owner, transport):
        return [dict(event) for (profile, route, _), event in self.pending.items()
                if profile == owner and route == transport]


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
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("LINK_OWNER", raising=False)
    monkeypatch.delenv("RESTIA_HOME_SERVER", raising=False)
    monkeypatch.delenv("LINK_MAX_PENDING", raising=False)
    monkeypatch.delenv("LINK_MAX_GUESTS", raising=False)
    lr._reset_summary_cache()
    cr.call_bus = cr._MessageBus()
    cr.remote_call_bus = cr._MessageBus()
    cr.federated_calls.reset()
    cr.local_stream_quota.reset()
    cr.remote_stream_quota.reset()
    cr.home_stream_quota.reset()
    cr.signal_limiter = cr.RateLimiter(max_requests=120, window_seconds=10)
    cr.offer_limiter = cr.RateLimiter(max_requests=8, window_seconds=60)
    monkeypatch.setattr(cr, "incoming_call_notifications", _FakeCallAlerts())
    with _ENGINE.begin() as conn:
        conn.exec_driver_sql("DELETE FROM direct_message_attachments")
        conn.exec_driver_sql("DELETE FROM direct_messages")
        conn.exec_driver_sql("DELETE FROM link_guests")
        conn.exec_driver_sql("DELETE FROM home_link")
        conn.exec_driver_sql("DELETE FROM link_invites")
        conn.exec_driver_sql("DELETE FROM remote_contact_prefs")
        conn.exec_driver_sql("DELETE FROM remote_blocks")
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
CALL = _routes(cr.setup_call_routes())


def _run(coro):
    return asyncio.run(coro)


def _register(handle="guest1"):
    reg = HUB[("POST", "/api/link/register")]
    return _run(reg(lr.RegisterRequest(handle=handle), _req()))


def _admin_action(handle, action, username="mika"):
    act = HUB[("POST", "/api/link/admin/guests/{handle}")]
    return _run(act(handle, lr.GuestActionRequest(action=action), _req(username)))


def _register_approved(handle="guest1"):
    out = _register(handle)
    _admin_action(handle, "approve")
    return out


# ── Hub: registration + approval gate ───────────────────────────────────────

def test_register_is_pending_and_reveals_no_owner():
    out = _register("visitor")
    assert out["ok"] is True
    assert out["guest"] == "visitor@remote"
    assert out["status"] == "pending"
    assert len(out["token"]) > 30
    assert "owner" not in out


def test_pending_guest_cannot_do_anything():
    token = _register("visitor")["token"]
    with pytest.raises(HTTPException) as e:
        _run(HUB[("POST", "/api/link/messages")](
            lr.LinkSendRequest(body="let me in"), _req(bearer=token)))
    assert (e.value.status_code, e.value.detail) == (403, "link_pending")
    with pytest.raises(HTTPException) as e:
        _run(HUB[("GET", "/api/link/messages")](_req(bearer=token), after_id=0))
    assert (e.value.status_code, e.value.detail) == (403, "link_pending")
    with pytest.raises(HTTPException) as e:
        _run(HUB[("GET", "/api/link/summary")](_req(bearer=token)))
    assert (e.value.status_code, e.value.detail) == (403, "link_pending")


def test_blocked_guest_reads_identically_to_pending():
    token = _register_approved("visitor")["token"]
    _admin_action("visitor", "block")
    with pytest.raises(HTTPException) as e:
        _run(HUB[("POST", "/api/link/messages")](
            lr.LinkSendRequest(body="hi"), _req(bearer=token)))
    assert (e.value.status_code, e.value.detail) == (403, "link_pending")
    # Blocked keeps the handle reserved.
    with pytest.raises(HTTPException) as e:
        _register("visitor")
    assert e.value.status_code == 409


def test_delete_frees_the_handle():
    old_token = _register_approved("visitor")["token"]
    _run(HUB[("POST", "/api/link/messages")](
        lr.LinkSendRequest(
            body="old identity secret",
            attachments=[lr.LinkPhotoRequest(name="old.png", data=PNG_B64)],
        ),
        _req(bearer=old_token),
    ))
    _admin_action("visitor", "delete")
    replacement = _register("visitor")
    assert replacement["status"] == "pending"
    _admin_action("visitor", "approve")
    history = _run(HUB[("GET", "/api/link/messages")](
        _req(bearer=replacement["token"]), after_id=0
    ))
    assert history["messages"] == []


def test_register_rejects_bad_and_reserved_handles():
    for bad in ("", "UPPER!", "has space", "-leading", "x" * 40):
        with pytest.raises(HTTPException) as e:
            _register(bad)
        assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        _register("api")           # RESERVED_USERNAMES
    assert e.value.status_code == 409


def test_register_handle_is_first_come_first_served():
    _register("dibs")
    with pytest.raises(HTTPException) as e:
        _register("dibs")
    assert e.value.status_code == 409


def test_register_cannot_shadow_local_account():
    with pytest.raises(HTTPException) as e:
        _register("alice")
    assert e.value.status_code == 409


def test_pending_cap_blocks_registration_floods(monkeypatch):
    monkeypatch.setenv("LINK_MAX_PENDING", "2")
    _register("bot1")
    _register("bot2")
    with pytest.raises(HTTPException) as e:
        _register("bot3")
    assert e.value.status_code == 429


def test_hub_disabled_is_a_404(monkeypatch):
    monkeypatch.setenv("LINK_HUB_ENABLED", "false")
    with pytest.raises(HTTPException) as e:
        _register("anyone")
    assert e.value.status_code == 404


# ── Hub: admin gating ───────────────────────────────────────────────────────

def test_guest_admin_requires_admin():
    _register("visitor")
    with pytest.raises(HTTPException) as e:
        _admin_action("visitor", "approve", username="alice")
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        _run(HUB[("GET", "/api/link/admin/guests")](_req("alice")))
    assert e.value.status_code == 403
    guests = _run(HUB[("GET", "/api/link/admin/guests")](_req("mika")))["guests"]
    assert guests[0]["handle"] == "visitor" and guests[0]["status"] == "pending"


def test_admin_action_validates_input():
    _register("visitor")
    with pytest.raises(HTTPException) as e:
        _admin_action("visitor", "promote")
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        _admin_action("nobody", "approve")
    assert e.value.status_code == 404


def test_pending_requests_surface_to_admin_conversation_list():
    _register("visitor")
    out = _run(MSG[("GET", "/api/messages/conversations")](_req("mika")))
    assert [r["guest"] for r in out["link_requests"]] == ["visitor@remote"]
    # Non-admins don't get the queue.
    out = _run(MSG[("GET", "/api/messages/conversations")](_req("alice")))
    assert "link_requests" not in out


# ── Hub: messaging round-trip (approved) ────────────────────────────────────

def test_guest_and_owner_roundtrip():
    token = _register_approved("visitor")["token"]
    hub_send = HUB[("POST", "/api/link/messages")]
    hub_fetch = HUB[("GET", "/api/link/messages")]
    hub_summary = HUB[("GET", "/api/link/summary")]

    out = _run(hub_send(lr.LinkSendRequest(body="hello mika"), _req(bearer=token)))
    assert out["message"]["mine"] is True
    assert out["message"]["recipient"] == lr.INSTANCE_REMOTE_ALIAS
    assert out["message"]["sender"] == "visitor@remote"
    assert set(out["message"]) >= {"sender", "recipient", "mine"}

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


def test_bearer_sees_one_instance_identity_and_cannot_target_profiles():
    token = _register_approved("visitor")["token"]
    directory = _run(HUB[("GET", "/api/link/directory")](
        _req(bearer=token)
    ))
    assert directory["users"] == [{"username": lr.INSTANCE_REMOTE_ALIAS}]
    serialized = str(directory)
    assert "mika" not in serialized and "alice" not in serialized
    assert "is_admin" not in serialized and "pubkey" not in serialized

    for injected in ("mika", "alice", "ghost", "__instance__"):
        with pytest.raises(HTTPException) as exc:
            _run(HUB[("POST", "/api/link/messages")](
                lr.LinkSendRequest(body="profile injection", to=injected),
                _req(bearer=token),
            ))
        assert exc.value.status_code == 404

    sent = _run(HUB[("POST", "/api/link/messages")](
        lr.LinkSendRequest(
            body="opaque instance target",
            to=lr.INSTANCE_REMOTE_ALIAS,
        ),
        _req(bearer=token),
    ))
    assert sent["message"]["recipient"] == lr.INSTANCE_REMOTE_ALIAS


def test_guest_photo_only_roundtrip_and_pair_scoped_media_fetch():
    token = _register_approved("visitor")['token']
    send = HUB[("POST", "/api/link/messages")]
    fetch = HUB[("GET", "/api/link/messages")]
    media = MSG[("GET", "/api/messages/media/{attachment_id}")]

    out = _run(send(
        lr.LinkSendRequest(
            body="",
            attachments=[lr.LinkPhotoRequest(
                name="remote.svg",
                data=f"data:image/svg+xml;base64,{PNG_B64}",
            )],
        ),
        _req(bearer=token),
    ))["message"]
    assert out["body"] == ""
    assert out["attachments"][0]["mime"] == "image/png"
    attachment_id = out["attachments"][0]["id"]

    history = _run(fetch(_req(bearer=token), after_id=0))
    assert history["messages"][0]["attachments"][0]["id"] == attachment_id
    raw = _run(fetch(
        _req(bearer=token),
        after_id=0,
        media_id=attachment_id,
    ))
    assert raw["data"] and raw["sha256"]

    # The local recipient can render it through the normal pair-scoped route.
    response = _run(media(attachment_id, _req("mika"), peer=""))
    assert response.body.startswith(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(HTTPException) as exc:
        _run(media(attachment_id, _req("alice"), peer=""))
    assert exc.value.status_code == 404


def test_owner_cannot_message_pending_or_unknown_remote_names():
    _register("visitor")   # pending — no conversation may exist yet
    for name in ("visitor@remote", "ghost@remote"):
        with pytest.raises(HTTPException) as e:
            _run(MSG[("POST", "/api/messages/conversations/{other}")](
                name, mr.SendMessageRequest(body="hi"), _req("mika")))
        assert e.value.status_code == 404


def test_owner_can_still_read_blocked_guest_history():
    token = _register_approved("visitor")["token"]
    _run(HUB[("POST", "/api/link/messages")](
        lr.LinkSendRequest(body="before the block"), _req(bearer=token)))
    _admin_action("visitor", "block")
    conv = _run(MSG[("GET", "/api/messages/conversations/{other}")](
        "visitor@remote", _req("mika"), after_id=0))
    assert [m["body"] for m in conv["messages"]] == ["before the block"]


def test_guest_endpoints_require_valid_token():
    _register_approved("visitor")
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
    token = _register_approved("visitor")["token"]
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
    assert username_reserved("remote:grant-id") is True
    assert username_reserved(" Remote-Instance:5 ") is True
    assert username_reserved("visitor") is False


# ── Client side (proxy) ─────────────────────────────────────────────────────

def _fake_hub(monkeypatch, responses):
    """Replace the HTTP seam with canned responses keyed by (method, path).
    A value that is an Exception is raised instead."""
    calls = []

    async def fake(method, path, *, token=None, json_body=None, params=None,
                   base_url=None, max_response_bytes=lr.MAX_HUB_RESPONSE_BYTES):
        calls.append({"method": method, "path": path, "token": token,
                      "json": json_body, "params": params,
                      "base_url": base_url, "max_response_bytes": max_response_bytes})
        r = responses[(method, path)]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(lr, "_hub_call", fake)
    return calls


def test_home_contact_requires_connect_first(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    with pytest.raises(HTTPException) as e:
        _run(MSG[("GET", "/api/messages/conversations/{other}")](
            "hub.example", _req("alice"), after_id=0))
    assert e.value.status_code == 409
    assert e.value.detail == lr.NOT_CONNECTED


def test_home_link_is_shared_by_all_local_profiles(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    _fake_hub(monkeypatch, {
        ("POST", "/api/link/register"): {
            "ok": True, "handle": "alice", "status": "pending", "token": "tok-alice-123456789012"},
    })
    _run(HL[("POST", "/api/homelink/connect")](
        lr.ConnectRequest(handle="alice"), _req("mika")))
    # The pairing belongs to the installation; internal profiles share it.
    assert _run(HL[("GET", "/api/homelink/status")](_req("alice")))["connected"] is True
    status = _run(HL[("GET", "/api/homelink/status")](_req("mika")))
    assert status["connected"] is True
    assert status["owner"] == "mika"
    db = _TS()
    try:
        row = db.query(cdb.HomeLink).one()
        assert row.local_user == lr.INSTANCE_LINK_USER
        assert row.owner == "mika"
    finally:
        db.close()


def test_legacy_profile_scoped_home_link_remains_readable(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    db = _TS()
    try:
        db.add(cdb.HomeLink(
            local_user="alice",
            home_url="https://hub.example",
            handle="legacy",
            owner=None,
            token="tok-legacy-123456789012",
            created_at=cdb.utcnow_naive(),
        ))
        db.commit()
    finally:
        db.close()
    assert lr.home_connected("alice") is True
    assert lr.home_connected("mika") is True
    db = _TS()
    try:
        row = db.query(cdb.HomeLink).one()
        assert row.local_user == lr.INSTANCE_LINK_USER
        assert row.owner == "alice"
    finally:
        db.close()


def test_pending_sentinel_reaches_the_local_ui(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    _fake_hub(monkeypatch, {
        ("POST", "/api/link/register"): {
            "ok": True, "handle": "alice", "status": "pending", "token": "tok-alice-123456789012"},
        ("GET", "/api/link/messages"): HTTPException(403, lr.PENDING),
    })
    out = _run(HL[("POST", "/api/homelink/connect")](
        lr.ConnectRequest(handle="alice"), _req("mika")))
    assert out["status"] == "pending"
    with pytest.raises(HTTPException) as e:
        _run(MSG[("GET", "/api/messages/conversations/{other}")](
            "hub.example", _req("alice"), after_id=0))
    assert (e.value.status_code, e.value.detail) == (403, lr.PENDING)


def test_connect_rejects_malformed_hub_token(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    _fake_hub(monkeypatch, {
        ("POST", "/api/link/register"): {"ok": True, "handle": "alice", "token": {"evil": 1}},
    })
    with pytest.raises(HTTPException) as e:
        _run(HL[("POST", "/api/homelink/connect")](
            lr.ConnectRequest(handle="alice"), _req("mika")))
    assert e.value.status_code == 502


def test_proxy_flows_and_hostile_hub_sanitization(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    calls = _fake_hub(monkeypatch, {
        ("POST", "/api/link/register"): {
            "ok": True, "handle": "alice", "status": "pending", "token": "tok123456789012345678"},
        ("POST", "/api/link/revoke"): {"ok": True},
        ("GET", "/api/link/messages"): {
            "messages": [
                {"id": 1, "body": "hi", "mine": True, "created_at": None, "read": False},
                {"id": "NaN", "body": "x" * 20000, "mine": "yes", "created_at": 12345, "read": 1},
                {"body": {"nested": "junk"}},          # dropped: non-string body
                "not-a-dict",                           # dropped
            ],
            "owner": "mika", "me": "alice@remote"},
        ("POST", "/api/link/messages"): {
            "message": {"id": 2, "body": "yo", "mine": True, "created_at": None, "read": False}},
        ("GET", "/api/link/summary"): {
            "owner": "mika", "unread": "7", "last_body": "yo",
            "last_at": "2026-07-10T00:00:00Z", "last_mine": True},
    })

    _run(HL[("POST", "/api/homelink/connect")](
        lr.ConnectRequest(handle="alice"), _req("mika")))

    conv = _run(MSG[("GET", "/api/messages/conversations/{other}")](
        "hub.example", _req("alice"), after_id=0))
    assert conv["other"]["home"] is True
    bodies = [m["body"] for m in conv["messages"]]
    assert bodies[0] == "hi"
    assert len(bodies) == 2                       # junk entries dropped
    assert len(bodies[1]) == lr.MAX_BODY_LEN      # oversize body truncated
    assert conv["messages"][1]["created_at"] is None   # non-string timestamp dropped

    sent = _run(MSG[("POST", "/api/messages/conversations/{other}")](
        "hub.example", mr.SendMessageRequest(body="yo"), _req("alice")))
    assert sent["message"]["body"] == "yo"

    # The stored token authenticates every proxied call after connect.
    assert all(c["token"] == "tok123456789012345678" for c in calls[1:])
    assert all(c["base_url"] == "https://hub.example" for c in calls)

    # Home conversation is merged into the list + unread endpoints, with the
    # hub's stringly-typed unread coerced to an int.
    convos = _run(MSG[("GET", "/api/messages/conversations")](_req("alice")))["conversations"]
    home = [c for c in convos if c.get("home")]
    assert home and home[0]["username"] == "hub.example" and home[0]["unread"] == 7

    unread = _run(MSG[("GET", "/api/messages/unread")](_req("alice")))
    assert unread["by_user"].get("hub.example") == 7


def test_users_picker_includes_home_contact_and_approved_guests_only(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    _register_approved("friend")
    _register("stranger")   # pending — must not appear
    users = _run(MSG[("GET", "/api/messages/users")](_req("mika")))["users"]
    by_name = {u["username"]: u for u in users}
    assert by_name["hub.example"]["home"] is True
    assert by_name["friend@remote"]["remote"] is True
    assert "stranger@remote" not in by_name
    assert "alice" in by_name


def test_disconnect_forgets_link(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    _fake_hub(monkeypatch, {
        ("POST", "/api/link/register"): {
            "ok": True, "handle": "alice", "status": "pending", "token": "tok123456789012345678"},
        ("POST", "/api/link/revoke"): {"ok": True},
    })
    _run(HL[("POST", "/api/homelink/connect")](
        lr.ConnectRequest(handle="alice"), _req("mika")))
    with pytest.raises(HTTPException) as exc:
        _run(HL[("POST", "/api/homelink/disconnect")](_req("alice")))
    assert exc.value.status_code == 403
    _run(HL[("POST", "/api/homelink/disconnect")](_req("mika")))
    st = _run(HL[("GET", "/api/homelink/status")](_req("alice")))
    assert st["connected"] is False


def test_stored_home_origin_binds_every_bearer_call(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://paired.example")
    calls = []

    async def fake(method, path, *, token=None, json_body=None, params=None,
                   base_url=None, max_response_bytes=lr.MAX_HUB_RESPONSE_BYTES):
        calls.append({"method": method, "path": path, "token": token,
                      "params": params, "base_url": base_url,
                      "max_response_bytes": max_response_bytes})
        if token is None:
            return {"handle": "alice", "status": "approved",
                    "token": "tok-paired-123456789012"}
        if path.endswith("/summary"):
            return {"unread": 0, "last_body": None, "last_at": None}
        if method == "POST":
            return {"message": {"id": 1, "body": "hi", "mine": True,
                                "created_at": None, "read": False}}
        if params and params.get("media_id"):
            return {"id": "a" * 32, "name": "p.png", "mime": "image/png",
                    "size": 1, "width": 1, "height": 1, "data": "eA==",
                    "sha256": "0" * 64}
        return {"messages": [], "owner": "mika", "me": "alice@remote"}

    monkeypatch.setattr(lr, "_hub_call", fake)
    _run(HL[("POST", "/api/homelink/connect")](
        lr.ConnectRequest(handle="alice"), _req("mika")))
    # A later config change must never redirect the stored bearer credential.
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://attacker.example")
    assert lr.home_contact_name() == "paired.example"
    assert _run(HL[("GET", "/api/homelink/status")](_req("mika")))["contact"] == "paired.example"
    _run(lr._home_summary("mika"))
    _run(lr.home_get_conversation("mika"))
    _run(lr.home_send_message("mika", "hi"))
    media = _run(lr.home_get_media("mika", "a" * 32))
    assert media["id"] == "a" * 32

    authenticated = [c for c in calls if c["token"]]
    assert len(authenticated) == 4
    assert all(c["base_url"] == "https://paired.example" for c in authenticated)
    media_call = authenticated[-1]
    assert media_call["max_response_bytes"] == lr.MAX_HUB_MEDIA_RESPONSE_BYTES


# ── Cross-instance call bridge ──────────────────────────────────────────────

FED_CALL_ID = "2ee3c166-e3f4-4e78-804e-128bbb1ca79c"
FED_OFFER = {"sdp": "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n", "video": True}
FED_ANSWER = {"sdp": "v=0\r\no=- 2 2 IN IP4 127.0.0.1\r\n"}
FED_END_OF_CANDIDATES = {
    "candidate": {
        "candidate": "",
        "sdpMid": "0",
        "sdpMLineIndex": 0,
        "usernameFragment": "abcd",
    }
}


def _approved_guest_with_pair(handle="caller"):
    token = _register_approved(handle)["token"]
    _run(HUB[("POST", "/api/link/messages")](
        lr.LinkSendRequest(body="establish call relationship"),
        _req(bearer=token),
    ))
    return token, f"{handle}@remote"


def test_federated_signal_envelope_has_no_identity_fields():
    with pytest.raises(ValidationError):
        lr.LinkCallSignalRequest(
            call_id=FED_CALL_ID,
            kind="offer",
            data=FED_OFFER,
            **{"from": "mika"},
        )
    with pytest.raises(ValidationError):
        lr.LinkCallSignalRequest(
            call_id=FED_CALL_ID,
            kind="offer",
            data=FED_OFFER,
            **{"to": "alice"},
        )


def test_two_instance_call_signals_are_owner_bound_and_profile_free():
    token, guest = _approved_guest_with_pair()
    owner_q = cr.call_bus.subscribe("mika")
    guest_q = cr.remote_call_bus.subscribe(guest)

    out = _run(HUB[("POST", "/api/link/calls/signal")](
        lr.LinkCallSignalRequest(call_id=FED_CALL_ID, kind="offer", data=FED_OFFER),
        _req(bearer=token),
    ))
    assert out == {"ok": True}
    _, owner_event = owner_q.get_nowait()
    assert owner_event["from"] == guest
    assert owner_event["data"] == FED_OFFER
    assert cr.incoming_call_notifications.begun[-1] == {
        "owner": "mika", "peer": guest, "call_id": FED_CALL_ID,
        "transport": "local", "offer_data": FED_OFFER,
    }

    out = _run(CALL[("POST", "/api/calls/signal")](
        cr.SignalRequest(to=guest, call_id=FED_CALL_ID, kind="answer", data=FED_ANSWER),
        _req("mika"),
    ))
    assert out == {"ok": True}
    assert cr.incoming_call_notifications.stopped[-1]["owner"] == "mika"
    _, remote_event = guest_q.get_nowait()
    assert remote_event == {
        "call_id": FED_CALL_ID,
        "kind": "answer",
        "data": FED_ANSWER,
    }
    assert "from" not in remote_event and "to" not in remote_event

    # Firefox sends this standards-defined marker as a non-null candidate
    # object. It must survive both validation layers in the Home Link bridge.
    out = _run(HUB[("POST", "/api/link/calls/signal")](
        lr.LinkCallSignalRequest(
            call_id=FED_CALL_ID,
            kind="ice",
            data=FED_END_OF_CANDIDATES,
        ),
        _req(bearer=token),
    ))
    assert out == {"ok": True}
    _, owner_event = owner_q.get_nowait()
    assert owner_event["kind"] == "ice"
    assert owner_event["data"] == FED_END_OF_CANDIDATES

    # A non-owner local profile cannot inject itself into the pair.
    with pytest.raises(HTTPException) as exc:
        _run(CALL[("POST", "/api/calls/signal")](
            cr.SignalRequest(to=guest, call_id=FED_CALL_ID, kind="ice", data={
                "candidate": {"candidate": "candidate:1 1 UDP 1 192.0.2.1 9 typ host"}
            }),
            _req("alice"),
        ))
    assert exc.value.status_code == 404


def test_federated_calls_fail_closed_without_pair_or_active_binding():
    token = _register_approved("caller")["token"]
    offer = lr.LinkCallSignalRequest(
        call_id=FED_CALL_ID, kind="offer", data=FED_OFFER
    )
    with pytest.raises(HTTPException) as exc:
        _run(HUB[("POST", "/api/link/calls/signal")](offer, _req(bearer=token)))
    assert exc.value.status_code == 403

    token, _ = _approved_guest_with_pair("paired")
    unknown = lr.LinkCallSignalRequest(
        call_id=FED_CALL_ID, kind="answer", data=FED_ANSWER
    )
    with pytest.raises(HTTPException) as exc:
        _run(HUB[("POST", "/api/link/calls/signal")](unknown, _req(bearer=token)))
    assert exc.value.status_code == 404


def test_federated_duplicate_offer_and_disabled_calls_are_rejected(monkeypatch):
    token, _ = _approved_guest_with_pair()
    body = lr.LinkCallSignalRequest(call_id=FED_CALL_ID, kind="offer", data=FED_OFFER)
    _run(HUB[("POST", "/api/link/calls/signal")](body, _req(bearer=token)))
    with pytest.raises(HTTPException) as exc:
        _run(HUB[("POST", "/api/link/calls/signal")](body, _req(bearer=token)))
    assert exc.value.status_code == 409

    monkeypatch.setenv("CALLS_ENABLED", "false")
    with pytest.raises(HTTPException) as exc:
        _run(HUB[("POST", "/api/link/calls/signal")](body, _req(bearer=token)))
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        _run(CALL[("GET", "/api/calls/stream")](_req("mika")))
    assert exc.value.status_code == 404


def test_bearer_revoke_purges_identity_and_delivers_terminal_hangup():
    token, guest = _approved_guest_with_pair("revoked")

    async def scenario():
        response = await HUB[("GET", "/api/link/calls/stream")](
            _req(bearer=token)
        )
        iterator = response.body_iterator
        assert "connected" in await anext(iterator)
        owner_q = cr.call_bus.subscribe("mika")
        await HUB[("POST", "/api/link/calls/signal")](
            lr.LinkCallSignalRequest(
                call_id=FED_CALL_ID, kind="offer", data=FED_OFFER
            ),
            _req(bearer=token),
        )
        _, offer = owner_q.get_nowait()
        assert offer["kind"] == "offer"

        assert await HUB[("POST", "/api/link/revoke")](
            _req(bearer=token)
        ) == {"ok": True}
        terminal_frame = await anext(iterator)
        assert '"kind":"hangup"' in terminal_frame
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)
        _, terminal = owner_q.get_nowait()
        assert terminal["kind"] == "hangup"

    _run(scenario())
    with pytest.raises(HTTPException) as exc:
        _run(HUB[("GET", "/api/link/messages")](
            _req(bearer=token), after_id=0
        ))
    assert exc.value.status_code == 401

    replacement = _register_approved("revoked")
    history = _run(HUB[("GET", "/api/link/messages")](
        _req(bearer=replacement["token"]), after_id=0
    ))
    assert history["messages"] == []
    _run(HUB[("POST", "/api/link/messages")](
        lr.LinkSendRequest(body="replacement identity"),
        _req(bearer=replacement["token"]),
    ))
    with pytest.raises(HTTPException) as exc:
        _run(HUB[("POST", "/api/link/calls/signal")](
            lr.LinkCallSignalRequest(
                call_id=FED_CALL_ID, kind="answer", data=FED_ANSWER
            ),
            _req(bearer=replacement["token"]),
        ))
    assert exc.value.status_code == 404


def test_block_terminates_call_and_approve_cannot_resume_old_id():
    token, _ = _approved_guest_with_pair("blockedcall")

    async def scenario():
        response = await HUB[("GET", "/api/link/calls/stream")](
            _req(bearer=token)
        )
        iterator = response.body_iterator
        assert "connected" in await anext(iterator)
        await HUB[("POST", "/api/link/calls/signal")](
            lr.LinkCallSignalRequest(
                call_id=FED_CALL_ID, kind="offer", data=FED_OFFER
            ),
            _req(bearer=token),
        )
        await HUB[("POST", "/api/link/admin/guests/{handle}")](
            "blockedcall", lr.GuestActionRequest(action="block"), _req("mika")
        )
        assert '"kind":"hangup"' in await anext(iterator)
        await HUB[("POST", "/api/link/admin/guests/{handle}")](
            "blockedcall", lr.GuestActionRequest(action="approve"), _req("mika")
        )
        with pytest.raises(HTTPException) as exc:
            await HUB[("POST", "/api/link/calls/signal")](
                lr.LinkCallSignalRequest(
                    call_id=FED_CALL_ID, kind="answer", data=FED_ANSWER
                ),
                _req(bearer=token),
            )
        assert exc.value.status_code == 404

    _run(scenario())


def test_blocking_guest_revokes_an_already_open_call_stream():
    token, guest = _approved_guest_with_pair()

    async def scenario():
        response = await HUB[("GET", "/api/link/calls/stream")](
            _req(bearer=token)
        )
        assert response.headers["cache-control"] == "no-store"
        iterator = response.body_iterator
        assert "connected" in await anext(iterator)
        db = _TS()
        try:
            row = db.query(cdb.LinkGuest).filter(cdb.LinkGuest.handle == "caller").one()
            row.status = lr.GUEST_BLOCKED
            db.commit()
        finally:
            db.close()
        cr.remote_call_bus.publish(
            guest,
            "call",
            {"call_id": FED_CALL_ID, "kind": "hangup", "data": {}},
        )
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)

    _run(scenario())
    assert cr.remote_stream_quota._active == 0


def test_unstarted_federated_stream_responses_do_not_consume_quotas(monkeypatch):
    token, _ = _approved_guest_with_pair("quota")
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    db = _TS()
    try:
        db.add(cdb.HomeLink(
            local_user=lr.INSTANCE_LINK_USER,
            home_url="https://hub.example",
            handle="quota-home",
            owner="mika",
            token="quota-home-token-1234567890",
            created_at=cdb.utcnow_naive(),
        ))
        db.commit()
    finally:
        db.close()

    async def scenario():
        remote_responses = [
            await HUB[("GET", "/api/link/calls/stream")](
                _req(bearer=token)
            )
            for _ in range(10)
        ]
        home_responses = [
            await HL[("GET", "/api/homelink/calls/stream")](
                _req("mika")
            )
            for _ in range(10)
        ]
        assert cr.remote_stream_quota._active == 0
        assert cr.home_stream_quota._active == 0
        for response in remote_responses + home_responses:
            await response.body_iterator.aclose()

    _run(scenario())
    assert cr.remote_stream_quota._active == 0
    assert cr.home_stream_quota._active == 0


def test_reused_handle_cannot_inherit_old_call_stream_identity():
    old_token, guest = _approved_guest_with_pair("reused")
    db = _TS()
    try:
        old_id = db.query(cdb.LinkGuest.id).filter(cdb.LinkGuest.handle == "reused").scalar()
    finally:
        db.close()

    async def scenario():
        response = await HUB[("GET", "/api/link/calls/stream")](
            _req(bearer=old_token)
        )
        iterator = response.body_iterator
        assert "connected" in await anext(iterator)

        await HUB[("POST", "/api/link/admin/guests/{handle}")](
            "reused", lr.GuestActionRequest(action="delete"), _req("mika")
        )
        replacement = await HUB[("POST", "/api/link/register")](
            lr.RegisterRequest(handle="reused"), _req()
        )
        await HUB[("POST", "/api/link/admin/guests/{handle}")](
            "reused", lr.GuestActionRequest(action="approve"), _req("mika")
        )
        replacement_token = replacement["token"]
        await HUB[("POST", "/api/link/messages")](
            lr.LinkSendRequest(body="new identity"),
            _req(bearer=replacement_token),
        )
        cr.remote_call_bus.publish(
            guest,
            "call",
            {"call_id": FED_CALL_ID, "kind": "hangup", "data": {}},
        )
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)

    _run(scenario())
    db = _TS()
    try:
        replacement_id = db.query(cdb.LinkGuest.id).filter(
            cdb.LinkGuest.handle == "reused"
        ).scalar()
    finally:
        db.close()
    assert replacement_id == old_id, "regression must exercise SQLite PK reuse"


def test_home_call_proxy_is_owner_only_and_uses_stored_origin(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    calls = _fake_hub(monkeypatch, {
        ("POST", "/api/link/register"): {
            "handle": "caller", "status": "approved",
            "token": "tok-home-call-123456789012",
        },
        ("POST", "/api/link/calls/signal"): {"ok": True},
    })
    _run(HL[("POST", "/api/homelink/connect")](
        lr.ConnectRequest(handle="caller"), _req("mika")
    ))
    body = lr.LinkCallSignalRequest(
        call_id=FED_CALL_ID, kind="offer", data=FED_OFFER
    )
    assert _run(HL[("POST", "/api/homelink/calls/signal")](
        body, _req("mika")
    )) == {"ok": True}
    proxied = calls[-1]
    assert proxied["base_url"] == "https://hub.example"
    assert set(proxied["json"]) == {"call_id", "kind", "data"}
    with pytest.raises(HTTPException) as exc:
        _run(HL[("POST", "/api/homelink/calls/signal")](body, _req("alice")))
    assert exc.value.status_code == 403


def test_background_home_stream_retains_offer_for_telegram_link_replay(monkeypatch):
    db = _TS()
    try:
        db.add(cdb.HomeLink(
            local_user=lr.INSTANCE_LINK_USER,
            home_url="https://hub.example",
            handle="caller",
            owner="mika",
            token="watcher-token-1234567890123456",
            created_at=cdb.utcnow_naive(),
        ))
        db.commit()
    finally:
        db.close()

    raw = (
        b": connected\n\n"
        b"event: call\n"
        + b'data: {"call_id":"' + FED_CALL_ID.encode()
        + b'","kind":"offer","data":{"sdp":"v=0\\r\\no=remote\\r\\n","video":true}}\n\n'
    )

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def aiter_bytes(self): yield raw

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        def stream(self, *args, **kwargs): return FakeResponse()

    monkeypatch.setattr(lr.httpx, "AsyncClient", FakeClient)
    snapshot = lr._home_call_watch_snapshot()
    assert snapshot is not None

    async def consume_background():
        return [frame async for frame in lr._proxy_home_call_stream(**snapshot)]

    frames = _run(consume_background())
    assert frames[0] == lr._HOME_CALL_UPSTREAM_READY
    assert frames[0] == (
        'event: call-transport\n'
        'data: {"status":"ready"}\n\n'
    )
    assert any('"kind":"offer"' in frame for frame in frames)
    assert cr.incoming_call_notifications.begun[-1]["transport"] == "home"

    async def ready_only(*args, **kwargs):
        yield lr._HOME_CALL_UPSTREAM_READY

    monkeypatch.setattr(lr, "_proxy_home_call_stream", ready_only)

    async def replay():
        response = await HL[("GET", "/api/homelink/calls/stream")](
            _req("mika")
        )
        iterator = response.body_iterator
        assert "connected" in await anext(iterator)
        assert '"kind":"offer"' in await anext(iterator)
        assert await anext(iterator) == lr._HOME_CALL_UPSTREAM_READY
        await iterator.aclose()

    _run(replay())


def test_repair_invalidates_open_home_proxy_even_when_sqlite_reuses_id(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    db = _TS()
    try:
        old = cdb.HomeLink(
            local_user=lr.INSTANCE_LINK_USER,
            home_url="https://hub.example",
            handle="old",
            owner="mika",
            token="old-token-1234567890123456",
            created_at=cdb.utcnow_naive(),
        )
        db.add(old)
        db.commit()
        db.refresh(old)
        old_id = old.id
    finally:
        db.close()

    raw = (
        b"event: call\n"
        + b'data: {"call_id":"' + FED_CALL_ID.encode()
        + b'","kind":"hangup","data":{}}\n\n'
    )

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/event-stream"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_bytes(self):
            yield raw
            # A keepalive can already be buffered ahead of the revoke terminal
            # on the independent SSE connection. It must not make the proxy
            # close before forwarding the safe hangup.
            yield b": ping\n\n" + raw

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(lr.httpx, "AsyncClient", FakeClient)

    async def scenario():
        response = await HL[("GET", "/api/homelink/calls/stream")](
            _req("mika")
        )
        iterator = response.body_iterator
        assert "connected" in await anext(iterator)
        assert "hangup" in await anext(iterator)

        db = _TS()
        try:
            db.query(cdb.HomeLink).delete(synchronize_session=False)
            db.commit()
            replacement = cdb.HomeLink(
                local_user=lr.INSTANCE_LINK_USER,
                home_url="https://hub.example",
                handle="new",
                owner="mika",
                token="new-token-1234567890123456",
                created_at=cdb.utcnow_naive(),
            )
            db.add(replacement)
            db.commit()
            db.refresh(replacement)
            assert replacement.id == old_id
        finally:
            db.close()

        # The old authenticated stream may receive the hub's revoke terminal
        # just after the local row was deleted/replaced. That one safe hangup
        # must still cross; all other stale signaling remains suppressed.
        assert "hangup" in await anext(iterator)
        with pytest.raises(StopAsyncIteration):
            await anext(iterator)

    _run(scenario())
    assert cr.home_stream_quota._active == 0


def test_reconnect_replaces_all_legacy_rows_and_disconnect_clears_all(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    db = _TS()
    try:
        db.add_all([
            cdb.HomeLink(local_user="alice", home_url="https://old-a.example",
                         handle="a", owner=None, token="old-a-token-1234567890",
                         created_at=cdb.utcnow_naive()),
            cdb.HomeLink(local_user="mika", home_url="https://old-b.example",
                         handle="b", owner=None, token="old-b-token-1234567890",
                         created_at=cdb.utcnow_naive()),
        ])
        db.commit()
    finally:
        db.close()
    calls = _fake_hub(monkeypatch, {
        ("POST", "/api/link/register"): {
            "handle": "fresh", "status": "approved",
            "token": "fresh-token-12345678901234",
        },
        ("POST", "/api/link/revoke"): {"ok": True},
    })
    _run(HL[("POST", "/api/homelink/connect")](
        lr.ConnectRequest(handle="fresh"), _req("mika")
    ))
    assert [call["path"] for call in calls[:3]] == [
        "/api/link/revoke",
        "/api/link/revoke",
        "/api/link/register",
    ]
    assert {(call["base_url"], call["token"]) for call in calls[:2]} == {
        ("https://old-a.example", "old-a-token-1234567890"),
        ("https://old-b.example", "old-b-token-1234567890"),
    }
    db = _TS()
    try:
        rows = db.query(cdb.HomeLink).all()
        assert len(rows) == 1
        assert rows[0].local_user == lr.INSTANCE_LINK_USER
        assert rows[0].token == "fresh-token-12345678901234"
    finally:
        db.close()
    _run(HL[("POST", "/api/homelink/disconnect")](_req("mika")))
    db = _TS()
    try:
        assert db.query(cdb.HomeLink).count() == 0
    finally:
        db.close()


def test_multiple_distinct_legacy_credentials_fail_closed_until_lifecycle_action(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    db = _TS()
    try:
        db.add_all([
            cdb.HomeLink(
                local_user="alice",
                home_url="https://old-a.example",
                handle="a",
                owner=None,
                token="old-a-token-legacy-123456",
                created_at=cdb.utcnow_naive(),
            ),
            cdb.HomeLink(
                local_user="mika",
                home_url="https://old-b.example",
                handle="b",
                owner=None,
                token="old-b-token-legacy-123456",
                created_at=cdb.utcnow_naive(),
            ),
        ])
        db.commit()
    finally:
        db.close()

    with pytest.raises(HTTPException) as exc:
        _run(HL[("GET", "/api/homelink/status")](_req("mika")))
    assert (exc.value.status_code, exc.value.detail) == (409, lr.NOT_CONNECTED)
    assert lr.home_connected("mika") is False
    assert lr.home_contact_name() == "hub.example"
    db = _TS()
    try:
        rows = db.query(cdb.HomeLink).all()
        assert len(rows) == 2
        assert all(row.local_user != lr.INSTANCE_LINK_USER for row in rows)
    finally:
        db.close()


def test_duplicate_legacy_credential_consolidates_without_orphaning_bearer(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    shared_token = "same-legacy-token-1234567890"
    db = _TS()
    try:
        db.add_all([
            cdb.HomeLink(
                local_user="alice",
                home_url="https://old.example",
                handle="same",
                owner="mika",
                token=shared_token,
                created_at=cdb.utcnow_naive(),
            ),
            cdb.HomeLink(
                local_user="mika",
                home_url="https://old.example/",
                handle="same",
                owner=None,
                token=shared_token,
                created_at=cdb.utcnow_naive(),
            ),
        ])
        db.commit()
    finally:
        db.close()

    status = _run(HL[("GET", "/api/homelink/status")](_req("mika")))
    assert status["connected"] is True
    db = _TS()
    try:
        row = db.query(cdb.HomeLink).one()
        assert row.local_user == lr.INSTANCE_LINK_USER
        assert row.token == shared_token
        assert row.owner == "mika"
    finally:
        db.close()


def test_replace_and_disconnect_fail_closed_without_explicit_force(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://new.example")
    db = _TS()
    try:
        db.add(cdb.HomeLink(
            local_user=lr.INSTANCE_LINK_USER,
            home_url="https://old.example",
            handle="old",
            owner="mika",
            token="old-token-force-test-123456",
            created_at=cdb.utcnow_naive(),
        ))
        db.commit()
    finally:
        db.close()
    calls = _fake_hub(monkeypatch, {
        ("POST", "/api/link/revoke"): HTTPException(502, "old hub down"),
        ("POST", "/api/link/register"): {
            "handle": "fresh", "status": "approved",
            "token": "fresh-token-force-test-1234",
        },
    })

    with pytest.raises(HTTPException) as exc:
        _run(HL[("POST", "/api/homelink/connect")](
            lr.ConnectRequest(handle="fresh"), _req("mika")
        ))
    assert (exc.value.status_code, exc.value.detail) == (409, lr.REVOKE_REQUIRED)
    assert [call["path"] for call in calls] == ["/api/link/revoke"]
    db = _TS()
    try:
        assert db.query(cdb.HomeLink).one().handle == "old"
    finally:
        db.close()

    _run(HL[("POST", "/api/homelink/connect")](
        lr.ConnectRequest(handle="fresh", force_replace=True), _req("mika")
    ))
    db = _TS()
    try:
        assert db.query(cdb.HomeLink).one().handle == "fresh"
    finally:
        db.close()

    with pytest.raises(HTTPException) as exc:
        _run(HL[("POST", "/api/homelink/disconnect")](
            _req("mika"), lr.DisconnectHomeRequest()
        ))
    assert exc.value.detail == lr.REVOKE_REQUIRED
    _run(HL[("POST", "/api/homelink/disconnect")](
        _req("mika"), lr.DisconnectHomeRequest(force_local=True)
    ))
    db = _TS()
    try:
        assert db.query(cdb.HomeLink).count() == 0
    finally:
        db.close()


def test_new_registration_failure_after_revoke_leaves_no_stale_local_token(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://new.example")
    db = _TS()
    try:
        db.add(cdb.HomeLink(
            local_user=lr.INSTANCE_LINK_USER,
            home_url="https://old.example",
            handle="old",
            owner="mika",
            token="old-token-revoked-123456789",
            created_at=cdb.utcnow_naive(),
        ))
        db.commit()
    finally:
        db.close()
    _fake_hub(monkeypatch, {
        ("POST", "/api/link/revoke"): {"ok": True},
        ("POST", "/api/link/register"): HTTPException(502, "new hub down"),
    })
    with pytest.raises(HTTPException) as exc:
        _run(HL[("POST", "/api/homelink/connect")](
            lr.ConnectRequest(handle="fresh"), _req("mika")
        ))
    assert exc.value.status_code == 502
    db = _TS()
    try:
        assert db.query(cdb.HomeLink).count() == 0
    finally:
        db.close()


def test_newly_issued_credential_is_revoked_if_local_persistence_loses_race(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    calls = []
    issued = "new-race-token-123456789012345"

    async def fake_hub(method, path, *, token=None, json_body=None, params=None,
                       base_url=None, max_response_bytes=lr.MAX_HUB_RESPONSE_BYTES):
        calls.append((method, path, token, base_url))
        if path == "/api/link/register":
            db = _TS()
            try:
                db.add(cdb.HomeLink(
                    local_user=lr.INSTANCE_LINK_USER,
                    home_url="https://winner.example",
                    handle="winner",
                    owner="mika",
                    token="winner-token-123456789012345",
                    created_at=cdb.utcnow_naive(),
                ))
                db.commit()
            finally:
                db.close()
            return {"handle": "loser", "status": "approved", "token": issued}
        assert path == "/api/link/revoke"
        assert token == issued
        return {"ok": True}

    monkeypatch.setattr(lr, "_hub_call", fake_hub)
    with pytest.raises(HTTPException) as exc:
        _run(HL[("POST", "/api/homelink/connect")](
            lr.ConnectRequest(handle="loser"), _req("mika")
        ))
    assert exc.value.status_code == 409
    assert [path for _, path, _, _ in calls] == [
        "/api/link/register",
        "/api/link/revoke",
    ]
    db = _TS()
    try:
        assert db.query(cdb.HomeLink).one().handle == "winner"
    finally:
        db.close()


def test_home_link_lifecycle_operations_are_serialized(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://hub.example")
    active = 0
    max_active = 0
    revoked = []

    async def fake_hub(method, path, *, token=None, json_body=None, params=None,
                       base_url=None, max_response_bytes=lr.MAX_HUB_RESPONSE_BYTES):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            if path == "/api/link/revoke":
                revoked.append(token)
                return {"ok": True}
            handle = json_body["handle"]
            return {
                "handle": handle,
                "status": "approved",
                "token": f"issued-{handle}-token-123456789",
            }
        finally:
            active -= 1

    monkeypatch.setattr(lr, "_hub_call", fake_hub)

    async def scenario():
        await asyncio.gather(
            HL[("POST", "/api/homelink/connect")](
                lr.ConnectRequest(handle="first"), _req("mika")
            ),
            HL[("POST", "/api/homelink/connect")](
                lr.ConnectRequest(handle="second"), _req("mika")
            ),
        )

    _run(scenario())
    assert max_active == 1
    assert len(revoked) == 1
    db = _TS()
    try:
        assert db.query(cdb.HomeLink).count() == 1
    finally:
        db.close()


def test_connected_contact_label_comes_from_persisted_origin(monkeypatch):
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://env.example")
    db = _TS()
    try:
        db.add(cdb.HomeLink(
            local_user=lr.INSTANCE_LINK_USER,
            home_url="https://paired.example:8443",
            handle="paired",
            owner="mika",
            token="paired-token-label-123456789",
            created_at=cdb.utcnow_naive(),
        ))
        db.commit()
    finally:
        db.close()
    assert lr.home_contact_name() == "paired.example:8443"
    status = _run(HL[("GET", "/api/homelink/status")](_req("mika")))
    assert status["contact"] == "paired.example:8443"
    monkeypatch.setenv("RESTIA_HOME_SERVER", "https://attacker.example")
    assert lr.home_contact_name() == "paired.example:8443"


def test_connect_ui_requires_confirmation_before_force_replace():
    source = Path(lr.__file__).resolve().parents[1] / "static/js/messaging.js"
    text = source.read_text(encoding="utf-8")
    safe = text.index("await connect(false)")
    sentinel = text.index("home_revoke_required", safe)
    confirmation = text.index("styledConfirm", sentinel)
    forced = text.index("await connect(true)", confirmation)
    assert safe < sentinel < confirmation < forced


def test_home_sse_parser_rejects_profile_injection_and_normalizes_origin():
    safe = (
        b"event: call\n"
        + b'data: {"call_id":"' + FED_CALL_ID.encode() +
        b'","kind":"hangup","data":{}}\n'
    )
    assert lr._clean_upstream_call_frame(safe)["kind"] == "hangup"
    injected = safe.replace(b'"call_id"', b'"from":"mika","call_id"')
    assert lr._clean_upstream_call_frame(injected) is None
    assert lr._validated_home_base("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    with pytest.raises(HTTPException):
        lr._validated_home_base("http://hub.example")
    with pytest.raises(HTTPException):
        lr._validated_home_base("https://hub.example/redirect")
