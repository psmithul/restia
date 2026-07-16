"""Home Link invite codes + instance-wide guest reach (routes/link_routes.py).

Pins the security guarantees of the invite-onboarding feature:
  - Only an admin can mint or list invite codes; the plaintext code is shown
    exactly once and only its hash is stored.
  - Redeeming a valid code creates an APPROVED guest with no owner-approval
    wait; the guest can send/read immediately.
  - Invalid / expired / revoked / spent codes all fail the same generic way,
    and single-use codes can't be double-spent.
  - A bearer represents one remote installation and can reach only the hub
    installation's opaque identity. Internal profile names, roles, and keys are
    neither discoverable nor valid routing targets.
"""
import asyncio
import itertools
import tempfile
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
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

# mika = admin/owner, alice + bob = ordinary local accounts (non-owner targets).
USERS = {
    "mika": {"is_admin": True},
    "alice": {"is_admin": False},
    "bob": {"is_admin": False},
}

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


def _req(username=None, bearer=None):
    state = SimpleNamespace(current_user=username, api_token=False)
    app = SimpleNamespace(state=SimpleNamespace(auth_manager=_FakeAuth(USERS)))
    n = next(_host_counter)
    client = SimpleNamespace(host=f"10.7.{n // 250}.{n % 250}")
    headers = {"authorization": f"Bearer {bearer}"} if bearer else {}
    return SimpleNamespace(state=state, app=app, client=client, headers=headers, cookies={})


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(lr, "SessionLocal", _TS)
    monkeypatch.setattr(mr, "SessionLocal", _TS)
    monkeypatch.setenv("LINK_HUB_ENABLED", "true")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("LINK_OWNER", raising=False)
    monkeypatch.delenv("LINK_MAX_GUESTS", raising=False)
    lr._reset_summary_cache()
    with _ENGINE.begin() as conn:
        for t in ("direct_message_attachments", "direct_messages", "link_guests", "home_link",
                  "outbound_chat_links", "link_invites", "remote_contact_prefs", "remote_blocks"):
            conn.exec_driver_sql(f"DELETE FROM {t}")
    yield


def _routes(router):
    by = {}
    for r in router.routes:
        for m in getattr(r, "methods", set()):
            by[(m, r.path)] = r.endpoint
    return by


HUB = _routes(lr.setup_link_hub_routes())
MSG = _routes(mr.setup_messaging_routes())


def _run(coro):
    return asyncio.run(coro)


def _mint(username="mika", label=None, max_uses=None, expires_in_days=None,
          hub_url=None):
    ep = HUB[("POST", "/api/link/admin/invites")]
    body = lr.InviteCreateRequest(label=label, max_uses=max_uses,
                                  expires_in_days=expires_in_days,
                                  hub_url=hub_url)
    return _run(ep(body, _req(username)))


def _redeem(code, handle="guestx", pubkey=None):
    ep = HUB[("POST", "/api/link/redeem")]
    return _run(ep(lr.RedeemRequest(code=code, handle=handle, pubkey=pubkey), _req()))


def _send(token, body, to=None):
    ep = HUB[("POST", "/api/link/messages")]
    return _run(ep(lr.LinkSendRequest(body=body, to=to), _req(bearer=token)))


def _directory(token):
    ep = HUB[("GET", "/api/link/directory")]
    return _run(ep(_req(bearer=token)))["users"]


# ── Admin: minting codes ────────────────────────────────────────────────────

def test_mint_returns_plaintext_once_and_stores_only_hash():
    out = _mint(label="for a friend")
    assert out["ok"] and out["code"] and len(out["code"]) > 20
    # Only the hash is persisted.
    db = _TS()
    try:
        inv = db.query(cdb.LinkInvite).filter(cdb.LinkInvite.id == out["id"]).first()
        assert inv.code_hash == lr._hash_code(out["code"])
        assert inv.code_hash != out["code"]
        assert inv.label == "for a friend" and inv.created_by == "mika"
    finally:
        db.close()


def test_mint_returns_origin_bound_portable_chat_invitation():
    made = _mint(hub_url="https://PAIR.Example:443/")
    assert made["hub_url"] == "https://pair.example"
    assert made["invitation"].startswith(lr.CONNECTION_INVITE_PREFIX)
    values = parse_qs(made["invitation"].split("?", 1)[1], strict_parsing=True)
    assert values == {
        "scope": ["chat"],
        "hub": ["https://pair.example"],
        "code": [made["code"]],
    }


def test_mint_rejects_unsafe_advertised_origins_before_storing():
    db = _TS()
    try:
        before = db.query(cdb.LinkInvite).count()
    finally:
        db.close()
    for unsafe in (
        "http://peer.example",
        "https://user:pass@peer.example",
        "https://peer.example/path",
        "https://peer.example?code=leak",
        "javascript:alert(1)",
    ):
        with pytest.raises(HTTPException) as exc:
            _mint(hub_url=unsafe)
        assert exc.value.status_code == 400
    db = _TS()
    try:
        assert db.query(cdb.LinkInvite).count() == before
    finally:
        db.close()


def test_mint_requires_admin():
    with pytest.raises(HTTPException) as e:
        _mint(username="alice")
    assert e.value.status_code == 403


def test_mint_validates_bounds():
    for kw in ({"max_uses": 0}, {"max_uses": 9999}, {"expires_in_days": 0},
               {"expires_in_days": 100000}):
        with pytest.raises(HTTPException) as e:
            _mint(**kw)
        assert e.value.status_code == 400


def test_list_invites_never_leaks_the_code():
    made = _mint(label="x")
    rows = _run(HUB[("GET", "/api/link/admin/invites")](_req("mika")))["invites"]
    assert rows and rows[0]["id"] == made["id"]
    assert "code" not in rows[0] and "code_hash" not in rows[0]
    assert rows[0]["active"] is True and rows[0]["uses"] == 0


# ── Redeem: the happy path is instant approval ──────────────────────────────

def test_redeem_creates_an_approved_guest_immediately():
    code = _mint()["code"]
    out = _redeem(code, "friend")
    assert out["status"] == "approved" and out["guest"] == "friend@remote"
    token = out["token"]
    # No approval step: the guest can message the owner right away.
    sent = _send(token, "hi mika")
    assert sent["message"]["recipient"] == lr.INSTANCE_REMOTE_ALIAS
    assert sent["message"]["mine"] is True


def test_general_invitation_is_chat_only_on_both_installations():
    redeemed = _redeem(_mint()["code"], "friend")
    _send(redeemed["token"], "hello owner")
    db = _TS()
    try:
        guest = db.query(cdb.LinkGuest).filter(cdb.LinkGuest.handle == "friend").one()
        assert guest.scope == "chat"
    finally:
        db.close()

    picker = _run(MSG[("GET", "/api/messages/profiles")](_req("mika")))
    contact = next(row for row in picker["profiles"] if row["username"] == "friend@remote")
    assert contact["remote"] is True
    assert contact["chat_only"] is True
    assert contact["can_call"] is False

    thread = _run(MSG[("GET", "/api/messages/conversations/{other}")] (
        "friend@remote", _req("mika"), 0
    ))
    assert thread["other"]["chat_only"] is True
    assert thread["other"]["can_call"] is False

    with pytest.raises(HTTPException) as exc:
        lr.authorize_local_remote_call(_req("mika"), "mika", "friend@remote")
    assert (exc.value.status_code, exc.value.detail) == (404, "Call not available")


def test_redeem_consumes_a_single_use_code():
    code = _mint(max_uses=1)["code"]
    _redeem(code, "first")
    with pytest.raises(HTTPException) as e:
        _redeem(code, "second")
    assert e.value.status_code == 403


def test_redeem_multi_use_code_allows_capped_number():
    code = _mint(max_uses=2)["code"]
    _redeem(code, "a1")
    _redeem(code, "a2")
    with pytest.raises(HTTPException) as e:
        _redeem(code, "a3")
    assert e.value.status_code == 403


def test_redeem_rejects_bad_wrong_revoked_and_expired_codes():
    # wrong code
    with pytest.raises(HTTPException) as e:
        _redeem("not-a-real-code", "x1")
    assert e.value.status_code == 403
    # revoked
    made = _mint()
    _run(HUB[("POST", "/api/link/admin/invites/{invite_id}/revoke")](made["id"], _req("mika")))
    with pytest.raises(HTTPException) as e:
        _redeem(made["code"], "x2")
    assert e.value.status_code == 403
    # expired (force the row's expiry into the past)
    made2 = _mint()
    db = _TS()
    try:
        inv = db.query(cdb.LinkInvite).filter(cdb.LinkInvite.id == made2["id"]).first()
        inv.expires_at = cdb.utcnow_naive().replace(year=2000)
        db.commit()
    finally:
        db.close()
    with pytest.raises(HTTPException) as e:
        _redeem(made2["code"], "x3")
    assert e.value.status_code == 403


def test_redeem_validates_handle_and_blocks_shadowing():
    code = _mint(max_uses=10)["code"]
    with pytest.raises(HTTPException) as e:
        _redeem(code, "Bad Handle!")
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        _redeem(code, "alice")          # shadows a local account
    assert e.value.status_code == 409


def test_redeem_stores_pubkey_and_rejects_junk_pubkey():
    code = _mint(max_uses=10)["code"]
    _redeem(code, "keyed", pubkey="QUJDREVGabcdef0123456789+/=")
    db = _TS()
    try:
        g = db.query(cdb.LinkGuest).filter(cdb.LinkGuest.handle == "keyed").first()
        assert g.pubkey == "QUJDREVGabcdef0123456789+/="
        assert g.invite_id is not None
    finally:
        db.close()
    with pytest.raises(HTTPException) as e:
        _redeem(code, "keyed2", pubkey="<script>not base64</script>")
    assert e.value.status_code == 400


def test_redeem_404s_when_hub_disabled(monkeypatch):
    code = _mint()["code"]
    monkeypatch.setenv("LINK_HUB_ENABLED", "false")
    with pytest.raises(HTTPException) as e:
        _redeem(code, "nope")
    assert e.value.status_code == 404


# ── Instance-wide reach + per-user controls ─────────────────────────────────

def test_guest_sees_and_messages_only_the_opaque_installation_identity():
    token = _redeem(_mint()["code"], "friend")["token"]
    assert _directory(token) == [{"username": lr.INSTANCE_REMOTE_ALIAS}]
    assert set(_directory(token)[0]) == {"username"}
    out = _send(token, "hello installation", to=lr.INSTANCE_REMOTE_ALIAS)
    assert out["message"]["recipient"] == lr.INSTANCE_REMOTE_ALIAS
    assert "mika" not in str(out) and "alice" not in str(out)
    owner_convos = _run(MSG[("GET", "/api/messages/conversations")](
        _req("mika")
    ))["conversations"]
    assert owner_convos[0]["username"] == "friend@remote"
    assert _run(MSG[("GET", "/api/messages/conversations")](
        _req("alice")
    ))["conversations"] == []


def test_inbound_guest_picker_and_replies_are_hub_owner_only():
    token = _redeem(_mint()["code"], "friend")["token"]
    _send(token, "hello owner")
    owner_picker = _run(MSG[("GET", "/api/messages/profiles")](_req("mika")))
    other_picker = _run(MSG[("GET", "/api/messages/profiles")](_req("alice")))
    assert "friend@remote" in {row["username"] for row in owner_picker["profiles"]}
    assert "friend@remote" not in {row["username"] for row in other_picker["profiles"]}
    with pytest.raises(HTTPException) as exc:
        _run(MSG[("POST", "/api/messages/conversations/{other}")] (
            "friend@remote", mr.SendMessageRequest(body="undeliverable"), _req("alice")
        ))
    assert (exc.value.status_code, exc.value.detail) == (404, "User not found")
    reply = _run(MSG[("POST", "/api/messages/conversations/{other}")] (
        "friend@remote", mr.SendMessageRequest(body="hello back"), _req("mika")
    ))
    assert reply["message"]["recipient"] == "friend@remote"


def test_guest_approval_queue_and_actions_are_hub_owner_only(monkeypatch):
    monkeypatch.setitem(USERS["alice"], "is_admin", True)
    monkeypatch.setenv("LINK_OWNER", "mika")
    db = _TS()
    try:
        db.add(cdb.LinkGuest(
            handle="waiting",
            token_hash="f" * 64,
            status=lr.GUEST_PENDING,
            created_at=cdb.utcnow_naive(),
            scope="chat",
        ))
        db.commit()
    finally:
        db.close()

    owner_convos = _run(MSG[("GET", "/api/messages/conversations")](_req("mika")))
    other_admin_convos = _run(MSG[("GET", "/api/messages/conversations")](_req("alice")))
    assert owner_convos["link_requests"][0]["handle"] == "waiting"
    assert "link_requests" not in other_admin_convos

    for method, path, args in (
        ("GET", "/api/link/admin/guests", (_req("alice"),)),
        (
            "POST",
            "/api/link/admin/guests/{handle}",
            ("waiting", lr.GuestActionRequest(action="approve"), _req("alice")),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            _run(HUB[(method, path)](*args))
        assert exc.value.status_code == 403

    approved = _run(HUB[("POST", "/api/link/admin/guests/{handle}")](
        "waiting", lr.GuestActionRequest(action="approve"), _req("mika")
    ))
    assert approved["status"] == lr.GUEST_APPROVED


def test_internal_profile_names_are_never_bearer_targets_or_directory_entries():
    token = _redeem(_mint()["code"], "friend")["token"]
    _run(HUB[("POST", "/api/link/me/remote-prefs")](
        lr.RemotePrefRequest(discoverable=False), _req("alice")))
    assert _directory(token) == [{"username": lr.INSTANCE_REMOTE_ALIAS}]
    for profile in USERS:
        with pytest.raises(HTTPException) as e:
            _send(token, "profile injection", to=profile)
        assert e.value.status_code == 404


def test_owner_remains_reachable_even_if_opted_out():
    # Internal profile discoverability does not alter the opaque installation
    # identity or the owner routing hidden behind it.
    _run(HUB[("POST", "/api/link/me/remote-prefs")](
        lr.RemotePrefRequest(discoverable=False), _req("mika")))
    token = _redeem(_mint()["code"], "friend")["token"]
    assert _send(token, "hi owner")["message"]["recipient"] == lr.INSTANCE_REMOTE_ALIAS
    assert _directory(token) == [{"username": lr.INSTANCE_REMOTE_ALIAS}]


def test_owner_block_hides_instance_and_stops_delivery_then_unblock_restores():
    token = _redeem(_mint()["code"], "friend")["token"]
    _send(token, "first hello")
    _run(HUB[("POST", "/api/link/me/block")](
        lr.BlockRequest(handle="friend@remote", action="block"), _req("mika")))
    assert _directory(token) == []
    with pytest.raises(HTTPException) as e:
        _send(token, "again")
    assert e.value.status_code == 404
    _run(HUB[("POST", "/api/link/me/block")](
        lr.BlockRequest(handle="friend", action="unblock"), _req("mika")))
    assert _directory(token) == [{"username": lr.INSTANCE_REMOTE_ALIAS}]
    assert _send(token, "back")["message"]["recipient"] == lr.INSTANCE_REMOTE_ALIAS


def test_unknown_target_is_a_generic_404():
    token = _redeem(_mint()["code"], "friend")["token"]
    with pytest.raises(HTTPException) as e:
        _send(token, "hi", to="ghost")
    assert e.value.status_code == 404


def test_guest_conversations_hide_internal_profiles_and_legacy_threads():
    token = _redeem(_mint()["code"], "friend")["token"]
    _send(token, "to installation")
    # Simulate a pre-boundary legacy row addressed to another local profile.
    db = _TS()
    try:
        db.add(cdb.DirectMessage(
            sender="friend@remote",
            recipient="alice",
            body="legacy profile thread",
            created_at=cdb.utcnow_naive(),
        ))
        db.commit()
    finally:
        db.close()
    convos = _run(HUB[("GET", "/api/link/conversations")](
        _req(bearer=token)
    ))["conversations"]
    assert len(convos) == 1
    assert convos[0]["username"] == lr.INSTANCE_REMOTE_ALIAS
    assert convos[0]["last_body"] == "to installation"
    assert "mika" not in str(convos) and "alice" not in str(convos)


def test_prefs_and_block_require_signed_in_user():
    # Auth-enabled + no session → require_user rejects with 401 (the 403 "Sign
    # in required" branch only applies in explicit anonymous modes, where
    # require_user returns ""). Either way an unauthenticated caller is refused.
    with pytest.raises(HTTPException) as e:
        _run(HUB[("POST", "/api/link/me/remote-prefs")](
            lr.RemotePrefRequest(discoverable=False), _req(None)))
    assert e.value.status_code in (401, 403)
    # Simulate an anonymous mode (require_user returns "") → our own 403 fires.
    import routes.link_routes as _lr
    orig = _lr.require_user
    _lr.require_user = lambda request: ""
    try:
        with pytest.raises(HTTPException) as e:
            _run(HUB[("POST", "/api/link/me/remote-prefs")](
                lr.RemotePrefRequest(discoverable=False), _req(None)))
        assert e.value.status_code == 403
    finally:
        _lr.require_user = orig
