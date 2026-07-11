"""Home Link invite codes + instance-wide guest reach (routes/link_routes.py).

Pins the security guarantees of the invite-onboarding feature:
  - Only an admin can mint or list invite codes; the plaintext code is shown
    exactly once and only its hash is stored.
  - Redeeming a valid code creates an APPROVED guest with no owner-approval
    wait; the guest can send/read immediately.
  - Invalid / expired / revoked / spent codes all fail the same generic way,
    and single-use codes can't be double-spent.
  - A redeemed guest may message ANY discoverable local user, not just the
    owner ("everyone on the instance" reach) — but a per-user opt-out and a
    per-guest block both hold, and every reachability failure reads as 404 so
    the userbase / block state can't be probed.
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
        for t in ("direct_messages", "link_guests", "home_link",
                  "link_invites", "remote_contact_prefs", "remote_blocks"):
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


def _mint(username="mika", label=None, max_uses=None, expires_in_days=None):
    ep = HUB[("POST", "/api/link/admin/invites")]
    body = lr.InviteCreateRequest(label=label, max_uses=max_uses,
                                  expires_in_days=expires_in_days)
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
    assert sent["message"]["recipient"] == "mika" and sent["message"]["mine"] is True


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

def test_guest_can_message_any_discoverable_local_user():
    token = _redeem(_mint()["code"], "friend")["token"]
    # Directory shows every local account (all discoverable by default).
    names = {u["username"] for u in _directory(token)}
    assert {"mika", "alice", "bob"} <= names
    # Guest DMs a NON-owner; it lands in that user's normal inbox.
    out = _send(token, "hey alice", to="alice")
    assert out["message"]["recipient"] == "alice"
    convos = _run(MSG[("GET", "/api/messages/conversations")](_req("alice")))["conversations"]
    assert convos[0]["username"] == "friend@remote" and convos[0]["unread"] == 1


def test_user_can_opt_out_of_discovery():
    token = _redeem(_mint()["code"], "friend")["token"]
    _run(HUB[("POST", "/api/link/me/remote-prefs")](
        lr.RemotePrefRequest(discoverable=False), _req("alice")))
    names = {u["username"] for u in _directory(token)}
    assert "alice" not in names and "mika" in names   # owner stays reachable
    with pytest.raises(HTTPException) as e:
        _send(token, "hi", to="alice")
    assert e.value.status_code == 404


def test_owner_remains_reachable_even_if_opted_out():
    # The owner opting out must not break the classic Home Link contract.
    _run(HUB[("POST", "/api/link/me/remote-prefs")](
        lr.RemotePrefRequest(discoverable=False), _req("mika")))
    token = _redeem(_mint()["code"], "friend")["token"]
    assert _send(token, "hi owner")["message"]["recipient"] == "mika"
    assert "mika" in {u["username"] for u in _directory(token)}


def test_block_hides_user_and_stops_delivery_then_unblock_restores():
    token = _redeem(_mint()["code"], "friend")["token"]
    _send(token, "first hello", to="bob")     # thread exists
    _run(HUB[("POST", "/api/link/me/block")](
        lr.BlockRequest(handle="friend@remote", action="block"), _req("bob")))
    assert "bob" not in {u["username"] for u in _directory(token)}
    with pytest.raises(HTTPException) as e:
        _send(token, "again", to="bob")
    assert e.value.status_code == 404
    # Unblock restores reachability.
    _run(HUB[("POST", "/api/link/me/block")](
        lr.BlockRequest(handle="friend", action="unblock"), _req("bob")))
    assert "bob" in {u["username"] for u in _directory(token)}
    assert _send(token, "back", to="bob")["message"]["recipient"] == "bob"


def test_unknown_target_is_a_generic_404():
    token = _redeem(_mint()["code"], "friend")["token"]
    with pytest.raises(HTTPException) as e:
        _send(token, "hi", to="ghost")
    assert e.value.status_code == 404


def test_guest_conversations_lists_threads_across_users():
    token = _redeem(_mint()["code"], "friend")["token"]
    _send(token, "to mika")
    _send(token, "to alice", to="alice")
    convos = _run(HUB[("GET", "/api/link/conversations")](_req(bearer=token)))["conversations"]
    assert {c["username"] for c in convos} == {"mika", "alice"}


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
