"""WebRTC call signaling (routes/call_routes.py).

Pins the relay's guarantees:
  - Calling can use STUN-only local/friendly networks while reporting whether a
    TURN relay is configured.
  - Signaling forwards a message to exactly the target's stream (and nobody
    else's), validates the kind, bounds the payload, and refuses unknown or
    self targets — with the same uniform 404 as messaging for a non-profile.
"""
import asyncio
import itertools
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import routes.call_routes as cr

USERS = {"mika": {}, "alice": {}, "bob": {}}
_n = itertools.count(1)
CALL_ID = "8e288d0b-f6a8-4ec3-a12f-a30f33d76993"
OFFER = {"sdp": "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n", "video": True}
ANSWER = {"sdp": "v=0\r\no=- 2 2 IN IP4 127.0.0.1\r\n"}
ICE = {
    "candidate": {
        "candidate": "candidate:1 1 UDP 2122260223 192.0.2.1 5000 typ host",
        "sdpMid": "0",
        "sdpMLineIndex": 0,
        "usernameFragment": "abcd",
    }
}


def _call_id(n: int) -> str:
    """Deterministic canonical UUIDv4 shape for signaling tests."""
    return f"00000000-0000-4000-8000-{n:012x}"


class _FakeAuth:
    @property
    def users(self):
        return USERS


class _FakeCallAlerts:
    def __init__(self):
        self.pending = {}
        self.begun = []
        self.stopped = []

    def begin(self, **kwargs):
        self.begun.append(kwargs)
        key = (kwargs["owner"], kwargs["transport"], kwargs["call_id"])
        self.pending[key] = {
            "from": kwargs["peer"], "call_id": kwargs["call_id"],
            "kind": "offer", "data": dict(kwargs["offer_data"]),
        }
        return True

    def stop(self, **kwargs):
        self.stopped.append(kwargs)
        return self.pending.pop(
            (kwargs["owner"], kwargs["transport"], kwargs["call_id"]), None
        ) is not None

    def pending_snapshot(self, *, owner, transport):
        return [dict(event) for (profile, route, _), event in self.pending.items()
                if profile == owner and route == transport]


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


def _signal(me, to, kind, call_id=CALL_ID, data=None):
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
    cr.remote_call_bus = cr._MessageBus()
    cr.federated_calls.reset()
    cr.local_stream_quota.reset()
    cr.remote_stream_quota.reset()
    cr.home_stream_quota.reset()
    cr.signal_limiter = cr.RateLimiter(max_requests=120, window_seconds=10)
    cr.offer_limiter = cr.RateLimiter(max_requests=8, window_seconds=60)
    monkeypatch.setattr(cr, "incoming_call_notifications", _FakeCallAlerts())
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
    _signal("mika", "bob", "offer", data=OFFER)
    assert qb.qsize() == 1 and qa.qsize() == 0
    ev, payload = qb.get_nowait()
    assert ev == "call"
    assert payload["from"] == "mika" and payload["kind"] == "offer"
    assert payload["data"] == OFFER


def test_offline_offer_is_replayed_and_answer_stops_its_alert():
    # No subscriber exists when the offer is sent. The short-lived sanitized
    # snapshot lets a browser opened from Telegram receive it once connected.
    _signal("mika", "bob", "offer", data=OFFER)
    assert cr.incoming_call_notifications.begun[0]["owner"] == "bob"

    async def replay():
        response = await ROUTES[("GET", "/api/calls/stream")](_req("bob"))
        iterator = response.body_iterator
        assert "connected" in await anext(iterator)
        frame = await anext(iterator)
        assert '"kind": "offer"' in frame
        await iterator.aclose()

    _run(replay())
    _signal("bob", "mika", "answer", data=ANSWER)
    assert cr.incoming_call_notifications.stopped[-1]["owner"] == "bob"
    assert cr.incoming_call_notifications.pending_snapshot(
        owner="bob", transport="local"
    ) == []


def test_signal_rejects_unknown_kind_self_and_nonuser():
    for kind in ("hack", "Offer", " offer "):
        with pytest.raises(HTTPException) as e:
            _signal("mika", "bob", kind, data=OFFER)
        assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        _signal("mika", "mika", "offer", data=OFFER)       # self
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        _signal("mika", "ghost", "offer", data=OFFER)      # not a user
    assert e.value.status_code == 404


def test_signal_bounds_payload_size():
    with pytest.raises(HTTPException) as e:
        _signal("mika", "bob", "offer",
                data={"sdp": "v=0" + "x" * cr.MAX_SDP_BYTES, "video": True})
    assert e.value.status_code == 400


def test_all_signal_kinds_relay_with_canonical_payloads():
    q = cr.call_bus.subscribe("bob")
    payloads = {
        "offer": OFFER,
        "answer": ANSWER,
        "ice": ICE,
        "decline": {},
        "hangup": {},
        "busy": {},
        "cancel": {},
    }
    for i, (kind, data) in enumerate(payloads.items(), start=1):
        _signal("mika", "bob", kind, call_id=_call_id(i), data=data)
    assert q.qsize() == 7


def test_signal_relays_standard_end_of_candidates_marker():
    """Firefox emits an empty candidate string at the end of a generation."""
    q = cr.call_bus.subscribe("bob")
    marker = {
        "candidate": {
            "candidate": "",
            "sdpMid": "0",
            "sdpMLineIndex": 0,
            "usernameFragment": "abcd",
        }
    }

    assert _signal("mika", "bob", "ice", data=marker) == {"ok": True}
    _, payload = q.get_nowait()
    assert payload["kind"] == "ice"
    assert payload["data"] == marker


@pytest.mark.parametrize("call_id", [
    "",
    "call-1",
    "8E288D0B-F6A8-4EC3-A12F-A30F33D76993",
    "6ba7b810-9dad-11d1-80b4-00c04fd430c8",  # canonical, but UUIDv1
    f" {CALL_ID}",
    CALL_ID + "-truncated",
])
def test_signal_rejects_noncanonical_call_ids(call_id):
    with pytest.raises(HTTPException) as e:
        _signal("mika", "bob", "offer", call_id=call_id, data=OFFER)
    assert e.value.status_code == 400
    assert e.value.detail == "Invalid call_id"


@pytest.mark.parametrize(("kind", "data", "detail"), [
    ("offer", {"sdp": OFFER["sdp"]}, "Invalid SDP payload"),
    ("offer", {"sdp": OFFER["sdp"], "video": "yes"}, "Invalid SDP payload"),
    ("offer", {**OFFER, "extra": True}, "Invalid SDP payload"),
    ("answer", {**ANSWER, "video": False}, "Invalid SDP payload"),
    ("answer", {"sdp": "not-sdp"}, "Invalid SDP payload"),
    ("ice", {"candidate": "candidate:not-an-object"}, "Invalid ICE payload"),
    ("ice", {"candidate": {"candidate": "not-a-candidate"}}, "Invalid ICE payload"),
    ("ice", {"candidate": {"candidate": ICE["candidate"]["candidate"],
                            "sdpMLineIndex": True}}, "Invalid ICE payload"),
    ("ice", {"candidate": {**ICE["candidate"], "private": "leak"}}, "Invalid ICE payload"),
    ("hangup", {"reason": "anything"}, "Control signals cannot contain data"),
])
def test_signal_rejects_kind_mismatched_or_unbounded_shapes(kind, data, detail):
    with pytest.raises(HTTPException) as e:
        _signal("mika", "bob", kind, data=data)
    assert e.value.status_code == 400
    assert e.value.detail == detail


def test_signal_request_forbids_unknown_envelope_fields():
    with pytest.raises(ValidationError):
        cr.SignalRequest(to="bob", call_id=CALL_ID, kind="hangup", data={},
                         **{"from": "mallory"})


def test_offer_attempts_have_a_separate_tight_rate_limit():
    q = cr.call_bus.subscribe("bob")
    for i in range(cr.offer_limiter.max_requests):
        _signal("mika", "bob", "offer", call_id=_call_id(i + 1), data=OFFER)
    assert q.qsize() == cr.offer_limiter.max_requests

    with pytest.raises(HTTPException) as e:
        _signal("mika", "bob", "offer", call_id=_call_id(100), data=OFFER)
    assert e.value.status_code == 429
    assert e.value.detail == "Too many call attempts"

    # The tighter offer budget must not block answer/ICE/control traffic for an
    # already-active call; the general burst limiter remains independent.
    assert _signal("mika", "bob", "answer", call_id=_call_id(101), data=ANSWER) == {"ok": True}


def test_answered_federated_call_uses_active_ttl(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(cr.time, "monotonic", lambda: now[0])
    guest, owner, call_id = "visitor@remote", "mika", _call_id(900)
    guest_identity = 41
    cr.federated_calls.open(call_id, guest, owner, guest_identity, "guest")

    now[0] += cr.FEDERATED_CALL_TTL_S - 1
    cr.federated_calls.authorize(
        call_id, guest, owner, guest_identity, "owner", "answer"
    )
    # Longer than the pending-offer TTL, but well inside the answered-call TTL.
    now[0] += cr.FEDERATED_CALL_TTL_S + 1
    cr.federated_calls.authorize(
        call_id, guest, owner, guest_identity, "guest", "ice"
    )

    now[0] += cr.FEDERATED_ACTIVE_CALL_TTL_S + 1
    with pytest.raises(HTTPException) as exc:
        cr.federated_calls.authorize(
            call_id, guest, owner, guest_identity, "owner", "hangup"
        )
    assert exc.value.status_code == 404


def test_federated_registry_enforces_direction_state_and_one_call_per_guest():
    guest, owner, credential = "visitor@remote", "mika", "credential-a"
    call_id = _call_id(910)
    cr.federated_calls.open(call_id, guest, owner, credential, "guest")

    # The offerer cannot answer itself or open a second call to consume the
    # global registry. Both failures leave the original offer pending.
    with pytest.raises(HTTPException) as exc:
        cr.federated_calls.authorize(
            call_id, guest, owner, credential, "guest", "answer"
        )
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        cr.federated_calls.open(
            _call_id(911), guest, owner, credential, "guest"
        )
    assert exc.value.status_code == 409

    cr.federated_calls.authorize(
        call_id, guest, owner, credential, "owner", "answer"
    )
    for side, kind in (("owner", "answer"), ("owner", "decline"),
                       ("owner", "busy"), ("guest", "cancel")):
        with pytest.raises(HTTPException) as exc:
            cr.federated_calls.authorize(
                call_id, guest, owner, credential, side, kind
            )
        assert exc.value.status_code == 409

    # ICE remains valid during an answered call; hangup is terminal.
    cr.federated_calls.authorize(
        call_id, guest, owner, credential, "guest", "ice"
    )
    cr.federated_calls.authorize(
        call_id, guest, owner, credential, "owner", "hangup"
    )
    cr.federated_calls.close(call_id, guest, owner, credential)
    cr.federated_calls.open(
        _call_id(912), guest, owner, credential, "owner"
    )


def test_revoke_by_credential_publishes_terminal_to_both_sides():
    guest, owner, credential = "visitor@remote", "mika", "credential-b"
    call_id = _call_id(920)
    owner_q = cr.call_bus.subscribe(owner)
    guest_q = cr.remote_call_bus.subscribe(guest)
    cr.federated_calls.open(call_id, guest, owner, credential, "owner")
    cr.federated_calls.authorize(
        call_id, guest, owner, credential, "guest", "answer"
    )

    assert cr.revoke_federated_credential(credential) == 1
    _, local = owner_q.get_nowait()
    _, remote = guest_q.get_nowait()
    assert local == {
        "from": guest, "call_id": call_id, "kind": "hangup", "data": {},
    }
    assert remote["kind"] == "hangup" and remote["_server_terminal"] is True
    with pytest.raises(HTTPException) as exc:
        cr.federated_calls.authorize(
            call_id, guest, owner, credential, "guest", "ice"
        )
    assert exc.value.status_code == 404


def test_local_call_stream_has_per_profile_quota():
    async def scenario():
        endpoint = ROUTES[("GET", "/api/calls/stream")]
        iterators = []
        for _ in range(cr.local_stream_quota.per_key):
            response = await endpoint(_req("mika"))
            iterator = response.body_iterator
            assert "connected" in await anext(iterator)
            iterators.append(iterator)
        rejected_response = await endpoint(_req("mika"))
        rejected_iterator = rejected_response.body_iterator
        with pytest.raises(HTTPException) as exc:
            await anext(rejected_iterator)
        assert exc.value.status_code == 429
        for iterator in iterators:
            await iterator.aclose()

    _run(scenario())
    assert cr.local_stream_quota._active == 0


def test_unstarted_local_call_stream_does_not_consume_quota():
    async def scenario():
        endpoint = ROUTES[("GET", "/api/calls/stream")]
        responses = [await endpoint(_req("mika")) for _ in range(20)]
        assert cr.local_stream_quota._active == 0
        for response in responses:
            await response.body_iterator.aclose()

    _run(scenario())
    assert cr.local_stream_quota._active == 0
