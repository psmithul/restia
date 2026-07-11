"""WebRTC call signaling (routes/call_routes.py).

Pins the relay's guarantees:
  - Calling is only advertised as enabled once TURN is configured; the ICE
    server list reflects env.
  - Signaling forwards a message to exactly the target's stream (and nobody
    else's), validates the kind, bounds the payload, and refuses unknown or
    self targets — with the same uniform 404 as messaging for a non-user.
"""
import asyncio
import itertools
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.call_routes as cr

USERS = {"mika": {}, "alice": {}, "bob": {}}
_n = itertools.count(1)


class _FakeAuth:
    @property
    def users(self):
        return USERS


def _req(username=None):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=username, api_token=False),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=_FakeAuth())),
        client=SimpleNamespace(host=f"10.3.0.{next(_n) % 250}"),
        headers={}, cookies={})


ROUTES = {}
for r in cr.setup_call_routes().routes:
    for m in getattr(r, "methods", set()):
        ROUTES[(m, r.path)] = r.endpoint


def _run(c):
    return asyncio.run(c)


def _signal(me, to, kind, call_id="call-1", data=None):
    body = cr.SignalRequest(to=to, call_id=call_id, kind=kind, data=data)
    return _run(ROUTES[("POST", "/api/calls/signal")](body, _req(me)))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("TURN_URL", raising=False)
    monkeypatch.delenv("TURN_USERNAME", raising=False)
    monkeypatch.delenv("TURN_CREDENTIAL", raising=False)
    monkeypatch.delenv("STUN_URL", raising=False)
    # Reset the module-level bus between tests.
    cr.call_bus = cr._MessageBus()
    yield


def test_config_enabled_by_default_with_stun_only():
    # Calling is available by default (STUN), and reports TURN as not configured.
    cfg = _run(ROUTES[("GET", "/api/calls/config")](_req("mika")))
    assert cfg["enabled"] is True and cfg["turn"] is False
    assert any("stun:" in s["urls"] for s in cfg["ice_servers"])


def test_config_can_be_disabled(monkeypatch):
    monkeypatch.setenv("CALLS_ENABLED", "false")
    cfg = _run(ROUTES[("GET", "/api/calls/config")](_req("mika")))
    assert cfg["enabled"] is False


def test_config_enabled_with_turn(monkeypatch):
    monkeypatch.setenv("TURN_URL", "turn:relay.example.com:3478")
    monkeypatch.setenv("TURN_USERNAME", "u")
    monkeypatch.setenv("TURN_CREDENTIAL", "p")
    cfg = _run(ROUTES[("GET", "/api/calls/config")](_req("mika")))
    assert cfg["enabled"] is True
    turn = [s for s in cfg["ice_servers"] if "turn:" in s["urls"]][0]
    assert turn["username"] == "u" and turn["credential"] == "p"


def test_signal_reaches_only_the_target():
    # Subscribe alice + bob directly to the bus and confirm routing.
    qa = cr.call_bus.subscribe("alice")
    qb = cr.call_bus.subscribe("bob")
    _signal("mika", "bob", "offer", data={"sdp": "v=0..."})
    assert qb.qsize() == 1 and qa.qsize() == 0
    ev, payload = qb.get_nowait()
    assert ev == "call"
    assert payload["from"] == "mika" and payload["kind"] == "offer"
    assert payload["data"]["sdp"] == "v=0..."


def test_signal_rejects_unknown_kind_self_and_nonuser():
    with pytest.raises(HTTPException) as e:
        _signal("mika", "bob", "hack")
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        _signal("mika", "mika", "offer")       # self
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        _signal("mika", "ghost", "offer")      # not a user
    assert e.value.status_code == 404


def test_signal_bounds_payload_size():
    with pytest.raises(HTTPException) as e:
        _signal("mika", "bob", "offer", data={"sdp": "x" * 20000})
    assert e.value.status_code == 400


def test_all_control_kinds_relay():
    q = cr.call_bus.subscribe("bob")
    for k in ("offer", "answer", "ice", "decline", "hangup", "busy", "cancel"):
        _signal("mika", "bob", k)
    assert q.qsize() == 7
