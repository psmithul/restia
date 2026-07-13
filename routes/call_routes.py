# routes/call_routes.py
"""WebRTC audio/video calling — signaling relay only.

The media itself is peer-to-peer (encrypted by WebRTC/DTLS-SRTP end to end);
this server only relays the small signaling messages the two browsers use to
find each other — SDP offer/answer and ICE candidates — plus call control
(decline / hang up / busy). It never sees or proxies audio or video.

Signaling rides a dedicated always-on SSE stream (/api/calls/stream) so a call
can ring even when the Messages window is closed, keyed per profile through the
same in-process bus pattern as direct messages. NAT traversal needs a TURN
server for strict networks. STUN-only calling remains available for local and
friendly-NAT paths, while `/config` reports whether TURN is configured so the
client can state the reliability limitation explicitly.

Home Link adds a second signaling transport for calls between installations.
The hub binds each federated call id to exactly one approved guest and the
configured hub owner; no local profile name crosses the federation boundary.
"""

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from routes.messaging_routes import _MessageBus
from src.auth_helpers import require_user
from src.call_notifications import incoming_call_notifications
from src.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# Dedicated signaling bus (separate from DM delivery).
call_bus = _MessageBus()
# A separate bus is deliberately used for hub -> guest events. Mixing it with
# ``call_bus`` would make a guest handle another subscriber key on the local
# profile bus and would make accidental profile-name disclosure much easier.
remote_call_bus = _MessageBus()

SSE_KEEPALIVE_S = 15
MAX_SIGNAL_BYTES = 16384       # encoded JSON bytes, not Python characters
MAX_SDP_BYTES = 15360
MAX_ICE_CANDIDATE_BYTES = 2048
MAX_ICE_MID_LEN = 64
MAX_ICE_UFRAG_LEN = 256
MAX_CALL_TARGET_LEN = 96
MAX_CALL_KIND_LEN = 16
FEDERATED_CALL_TTL_S = 180
FEDERATED_ACTIVE_CALL_TTL_S = 12 * 60 * 60
MAX_FEDERATED_CALLS = 512
SIGNAL_KINDS = {"offer", "answer", "ice", "decline", "hangup", "busy", "cancel"}
CONTROL_SIGNAL_KINDS = {"decline", "hangup", "busy", "cancel"}
ICE_CANDIDATE_KEYS = {"candidate", "sdpMid", "sdpMLineIndex", "usernameFragment"}

# General signaling permits ICE bursts. Offers get a much tighter independent
# budget so one authenticated profile cannot make another profile ring without
# bound. Module-level instances let focused tests reset them deterministically.
signal_limiter = RateLimiter(max_requests=120, window_seconds=10)
offer_limiter = RateLimiter(max_requests=8, window_seconds=60)


class _FederatedCallRegistry:
    """Short-lived binding of one call id to one guest/owner pair.

    A bearer token proves the guest at the hub boundary, but follow-up ICE and
    control messages must also belong to a call that the same guest and exact
    hub owner opened. This registry prevents call-id guessing, duplicate-offer
    takeover, and profile injection. It is intentionally in-memory: calls are
    ephemeral and cannot survive a process restart anyway.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, dict[str, Any]] = {}

    def _prune_locked(self, now: float) -> None:
        expired = [
            call_id for call_id, row in self._calls.items()
            if now - float(row.get("touched", 0)) > (
                FEDERATED_ACTIVE_CALL_TTL_S if row.get("answered")
                else FEDERATED_CALL_TTL_S
            )
        ]
        for call_id in expired:
            self._calls.pop(call_id, None)

    def open(self, call_id: str, guest: str, owner: str,
             guest_identity: Any, initiator: str) -> None:
        now = time.monotonic()
        if initiator not in ("guest", "owner"):
            raise HTTPException(400, "Invalid call initiator")
        with self._lock:
            self._prune_locked(now)
            if call_id in self._calls:
                raise HTTPException(409, "Call already exists")
            credential = str(guest_identity)
            if any(row.get("guest_identity") == credential
                   for row in self._calls.values()):
                raise HTTPException(409, "Guest already has an active call")
            if len(self._calls) >= MAX_FEDERATED_CALLS:
                raise HTTPException(503, "Too many active calls")
            self._calls[call_id] = {
                "guest": _norm(guest),
                "guest_identity": credential,
                "owner": _norm(owner),
                "initiator": initiator,
                "touched": now,
                "answered": False,
            }

    def authorize(self, call_id: str, guest: str, owner: str,
                  guest_identity: Any, side: str, kind: str) -> None:
        """Authorize one state transition from the authenticated side."""
        if side not in ("guest", "owner"):
            raise HTTPException(400, "Invalid call side")
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            row = self._calls.get(call_id)
            if (
                row is None
                or row.get("guest") != _norm(guest)
                or row.get("guest_identity") != str(guest_identity)
                or row.get("owner") != _norm(owner)
            ):
                # Uniform 404 avoids revealing another pair's active call id.
                raise HTTPException(404, "Call not found")
            initiator = row.get("initiator")
            answered = bool(row.get("answered"))
            opposite = side != initiator
            valid = False
            if kind == "answer":
                valid = opposite and not answered
                if valid:
                    row["answered"] = True
            elif kind == "ice":
                valid = True
            elif kind in ("decline", "busy"):
                valid = opposite and not answered
            elif kind == "cancel":
                valid = side == initiator and not answered
            elif kind == "hangup":
                # Hangup is always a safe terminal operation, including a
                # connection failure during the offer/answer transition.
                valid = True
            if not valid:
                raise HTTPException(409, "Invalid call state")
            row["touched"] = now

    def close(self, call_id: str, guest: str, owner: str,
              guest_identity: Any) -> None:
        with self._lock:
            row = self._calls.get(call_id)
            if (
                row is not None
                and row.get("guest") == _norm(guest)
                and row.get("guest_identity") == str(guest_identity)
                and row.get("owner") == _norm(owner)
            ):
                self._calls.pop(call_id, None)

    def revoke_by_credential(self, guest_identity: Any) -> list[dict]:
        """Remove and return all sessions for one immutable guest credential."""
        credential = str(guest_identity)
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            revoked = []
            for call_id, row in list(self._calls.items()):
                if row.get("guest_identity") != credential:
                    continue
                revoked.append({"call_id": call_id, **row})
                self._calls.pop(call_id, None)
            return revoked

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()


class _ConcurrentStreamQuota:
    """Small per-identity cap for long-lived SSE connections."""

    def __init__(self, per_key: int = 3, total: int = 128) -> None:
        self.per_key = per_key
        self.total = total
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._active = 0

    def acquire(self, key: str) -> None:
        key = _norm(key)
        with self._lock:
            if self._active >= self.total or self._counts.get(key, 0) >= self.per_key:
                raise HTTPException(429, "Too many call streams")
            self._active += 1
            self._counts[key] = self._counts.get(key, 0) + 1

    def release(self, key: str) -> None:
        key = _norm(key)
        with self._lock:
            count = self._counts.get(key, 0)
            if count <= 1:
                self._counts.pop(key, None)
            else:
                self._counts[key] = count - 1
            if count:
                self._active = max(0, self._active - 1)

    def reset(self) -> None:
        with self._lock:
            self._counts.clear()
            self._active = 0


federated_calls = _FederatedCallRegistry()
local_stream_quota = _ConcurrentStreamQuota(per_key=4, total=256)
remote_stream_quota = _ConcurrentStreamQuota(per_key=3, total=128)
home_stream_quota = _ConcurrentStreamQuota(per_key=2, total=64)


def _begin_incoming_alert(owner: str, peer: str, call_id: str,
                          transport: str, data: dict) -> None:
    """Notification failures must never break authenticated signaling."""
    try:
        incoming_call_notifications.begin(
            owner=owner,
            peer=peer,
            call_id=call_id,
            transport=transport,
            offer_data=data,
        )
    except Exception:
        logger.warning("Incoming call notification could not be started")


def _stop_incoming_alert(owner: str, peer: str, call_id: str,
                         transport: str = "local") -> None:
    try:
        incoming_call_notifications.stop(
            owner=owner,
            peer=peer,
            call_id=call_id,
            transport=transport,
        )
    except Exception:
        logger.warning("Incoming call notification could not be stopped")


def revoke_federated_credential(guest_identity: Any) -> int:
    """Terminate every call for a revoked/blocked/deleted guest credential."""
    revoked = federated_calls.revoke_by_credential(guest_identity)
    for row in revoked:
        call_id = row["call_id"]
        guest = row["guest"]
        owner = row["owner"]
        _stop_incoming_alert(owner, guest, call_id)
        call_bus.publish(
            owner,
            "call",
            {"from": guest, "call_id": call_id, "kind": "hangup", "data": {}},
        )
        remote_call_bus.publish(
            guest,
            "call",
            {
                "call_id": call_id,
                "kind": "hangup",
                "data": {},
                "_server_terminal": True,
            },
        )
    return len(revoked)


class SignalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: StrictStr = Field(min_length=1, max_length=MAX_CALL_TARGET_LEN)
    call_id: StrictStr
    kind: StrictStr = Field(min_length=1, max_length=MAX_CALL_KIND_LEN)
    data: Optional[Dict[str, Any]] = None


def _norm(name: Optional[str]) -> str:
    return str(name or "").strip().lower()


def _require_me(request: Request) -> str:
    me = _norm(require_user(request))
    if not me:
        raise HTTPException(403, "Calling requires a signed-in profile")
    return me


def _known_users(request: Request) -> dict:
    mgr = getattr(request.app.state, "auth_manager", None)
    users = getattr(mgr, "users", None)
    return users if isinstance(users, dict) else {}


def _bad_signal(detail: str) -> HTTPException:
    return HTTPException(400, detail)


def _validate_call_id(raw: str) -> str:
    """Return one canonical UUID call id; reject rather than truncate.

    The browser generates ids with ``crypto.randomUUID()``. Canonical UUIDs are
    both high-entropy and unambiguous across clients, unlike the old truncated
    ``Math.random`` token.
    """
    value = raw or ""
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError):
        raise _bad_signal("Invalid call_id")
    canonical = str(parsed)
    if value != canonical or parsed.version != 4:
        raise _bad_signal("Invalid call_id")
    return canonical


def _validate_sdp(data: dict, *, offer: bool) -> dict:
    expected = {"sdp", "video"} if offer else {"sdp"}
    if set(data) != expected:
        raise _bad_signal("Invalid SDP payload")
    sdp = data.get("sdp")
    if not isinstance(sdp, str) or not sdp.startswith("v=0"):
        raise _bad_signal("Invalid SDP payload")
    if len(sdp.encode("utf-8")) > MAX_SDP_BYTES:
        raise _bad_signal("Signal payload too large")
    if offer and not isinstance(data.get("video"), bool):
        raise _bad_signal("Invalid SDP payload")
    return {"sdp": sdp, **({"video": data["video"]} if offer else {})}


def _validate_ice(data: dict) -> dict:
    if set(data) != {"candidate"} or not isinstance(data.get("candidate"), dict):
        raise _bad_signal("Invalid ICE payload")
    candidate = data["candidate"]
    if "candidate" not in candidate:
        raise _bad_signal("Invalid ICE payload")

    text = candidate.get("candidate")
    if (
        not isinstance(text, str)
        # Browsers may emit an RTCIceCandidate with an empty candidate string
        # to mark the end of one ICE generation.  It is a real, relayable
        # RTCIceCandidateInit value (distinct from the later null event), so
        # accept it while keeping every non-empty value on the candidate:
        # grammar boundary.
        or (text != "" and not text.startswith("candidate:"))
        or len(text.encode("utf-8")) > MAX_ICE_CANDIDATE_BYTES
    ):
        raise _bad_signal("Invalid ICE payload")

    mid = candidate.get("sdpMid")
    if mid is not None and (not isinstance(mid, str) or len(mid) > MAX_ICE_MID_LEN):
        raise _bad_signal("Invalid ICE payload")

    line_index = candidate.get("sdpMLineIndex")
    if line_index is not None and (
        isinstance(line_index, bool)
        or not isinstance(line_index, int)
        or not 0 <= line_index <= 65535
    ):
        raise _bad_signal("Invalid ICE payload")

    ufrag = candidate.get("usernameFragment")
    if ufrag is not None and (
        not isinstance(ufrag, str) or len(ufrag) > MAX_ICE_UFRAG_LEN
    ):
        raise _bad_signal("Invalid ICE payload")

    # Emit only the WebRTC toJSON fields in a stable shape. Missing optional
    # values remain absent rather than being invented by the relay.
    clean = {key: candidate[key] for key in ICE_CANDIDATE_KEYS if key in candidate}
    return {"candidate": clean}


def _validate_signal_data(kind: str, raw: Optional[dict]) -> dict:
    data = raw if raw is not None else {}
    if not isinstance(data, dict):
        raise _bad_signal("Signal data must be an object")
    if kind == "offer":
        clean = _validate_sdp(data, offer=True)
    elif kind == "answer":
        clean = _validate_sdp(data, offer=False)
    elif kind == "ice":
        clean = _validate_ice(data)
    elif kind in CONTROL_SIGNAL_KINDS:
        if data:
            raise _bad_signal("Control signals cannot contain data")
        clean = {}
    else:  # kept defensive for direct helper callers
        raise _bad_signal("Unknown signal kind")
    if len(json.dumps(clean, separators=(",", ":")).encode("utf-8")) > MAX_SIGNAL_BYTES:
        raise _bad_signal("Signal payload too large")
    return clean


def validate_federated_signal(call_id: str, kind: str,
                              data: Optional[dict]) -> tuple[str, str, dict]:
    """Validate the profile-free signal envelope used across Home Link."""
    if kind not in SIGNAL_KINDS:
        raise HTTPException(400, "Unknown signal kind")
    return _validate_call_id(call_id), kind, _validate_signal_data(kind, data)


def clean_federated_event(value: Any) -> Optional[dict]:
    """Sanitize one untrusted hub SSE event for the local browser proxy.

    The remote envelope has no ``from`` or ``to`` field by design. The local
    proxy reconstructs the configured Home Link contact label after this
    validation, so a hostile hub cannot inject an internal-looking profile.
    """
    if not isinstance(value, dict) or set(value) != {"call_id", "kind", "data"}:
        return None
    try:
        call_id, kind, data = validate_federated_signal(
            value.get("call_id"), value.get("kind"), value.get("data")
        )
    except HTTPException:
        return None
    return {"call_id": call_id, "kind": kind, "data": data}


def _require_calls_enabled() -> None:
    if not calls_enabled():
        raise HTTPException(404, "Calling is disabled")


def _ice_servers() -> list:
    """RTCPeerConnection ICE servers from env. STUN is free/always on; TURN is
    the relay that makes calls work across strict NATs."""
    servers = []
    stun = os.getenv("STUN_URL", "stun:stun.l.google.com:19302").strip()
    if stun:
        servers.append({"urls": stun})
    turn = os.getenv("TURN_URL", "").strip()
    if turn:
        s = {"urls": turn}
        user = os.getenv("TURN_USERNAME", "").strip()
        cred = os.getenv("TURN_CREDENTIAL", "").strip()
        if user:
            s["username"] = user
        if cred:
            s["credential"] = cred
        servers.append(s)
    return servers


def calls_enabled() -> bool:
    """Calling is available by default: public STUN alone connects on the same
    LAN and across many home networks. A TURN server (TURN_URL) is strongly
    recommended for reliable connections behind strict NATs, but requiring it
    would hide the feature entirely — so we advertise calling as available and
    let the client surface a hint when only STUN is present. Set
    CALLS_ENABLED=false to turn the feature off completely."""
    return os.getenv("CALLS_ENABLED", "true").strip().lower() != "false"


def turn_configured() -> bool:
    return bool(os.getenv("TURN_URL", "").strip())


def setup_call_routes():
    router = APIRouter(prefix="/api/calls", tags=["calls"])

    @router.get("/config")
    async def config(request: Request):
        """Whether calling is available here, and the ICE servers the browser
        should use for the peer connection. `turn` tells the client whether a
        relay is configured (reliable everywhere) or it's STUN-only (best on
        LAN / friendly NATs)."""
        me = _require_me(request)
        can_home_call = False
        can_remote_call = False
        try:
            from routes import link_routes
            can_home_call = link_routes.home_call_available(me)
            can_remote_call = link_routes.is_hub_call_owner(request, me)
        except Exception:
            # Capability discovery must never grant on an uncertain state.
            pass
        enabled = calls_enabled()
        return {"enabled": enabled, "turn": turn_configured(),
                "ice_servers": _ice_servers(),
                "can_home_call": bool(enabled and can_home_call),
                "can_remote_call": bool(enabled and can_remote_call)}

    @router.get("/stream")
    async def stream(request: Request):
        """Always-on signaling stream for the signed-in user. Emits `call`
        events: {"from", "call_id", "kind", "data"}."""
        me = _require_me(request)
        _require_calls_enabled()

        async def gen():
            acquired = False
            q = None
            try:
                local_stream_quota.acquire(me)
                acquired = True
                q = call_bus.subscribe(me)
                # Subscribe before taking the snapshot and do both without an
                # await. An offer is therefore seen exactly once: either it was
                # already pending, or it arrives on the live queue afterwards.
                pending = incoming_call_notifications.pending_snapshot(
                    owner=me, transport="local"
                )
                yield ": connected\n\n"
                for data in pending:
                    yield f"event: call\ndata: {json.dumps(data)}\n\n"
                while True:
                    try:
                        event, data = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
            finally:
                if q is not None:
                    call_bus.unsubscribe(me, q)
                if acquired:
                    local_stream_quota.release(me)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @router.post("/signal")
    async def signal(body: SignalRequest, request: Request):
        """Relay one signaling message to `to`. The server doesn't interpret the
        SDP itself, but it strictly validates each signal envelope before
        forwarding it to one real local profile."""
        me = _require_me(request)
        _require_calls_enabled()
        if not signal_limiter.check(me):
            raise HTTPException(429, "Too many signaling messages")
        kind = body.kind
        if kind not in SIGNAL_KINDS:
            raise HTTPException(400, "Unknown signal kind")
        if kind == "offer" and not offer_limiter.check(me):
            raise HTTPException(429, "Too many call attempts")
        target = _norm(body.to)
        if not target or target == me:
            raise HTTPException(400, "Invalid target")
        call_id = _validate_call_id(body.call_id)
        data = _validate_signal_data(kind, body.data)

        # Hub owner -> approved Home Link guest. Only the exact configured
        # owner and an existing, unblocked DM pair are eligible. The event put
        # on the dedicated remote bus contains no local profile name.
        if target.endswith("@remote"):
            from routes import link_routes
            guest, owner, guest_identity = link_routes.authorize_local_remote_call(
                request, me, target
            )
            if kind == "offer":
                federated_calls.open(
                    call_id, guest, owner, guest_identity, "owner"
                )
            else:
                federated_calls.authorize(
                    call_id, guest, owner, guest_identity, "owner", kind
                )
            if kind in ("answer", "decline", "busy", "hangup"):
                _stop_incoming_alert(owner, guest, call_id)
            remote_call_bus.publish(
                guest,
                "call",
                {"call_id": call_id, "kind": kind, "data": data},
            )
            if kind in CONTROL_SIGNAL_KINDS:
                federated_calls.close(call_id, guest, owner, guest_identity)
            return {"ok": True}

        if target not in {_norm(u) for u in _known_users(request)}:
            # Same uniform 404 as messaging: don't confirm who exists.
            raise HTTPException(404, "No such recipient")

        if kind == "offer":
            _begin_incoming_alert(target, me, call_id, "local", data)
        elif kind in ("answer", "decline", "busy"):
            _stop_incoming_alert(me, target, call_id)
        elif kind == "cancel":
            _stop_incoming_alert(target, me, call_id)
        elif kind == "hangup":
            _stop_incoming_alert(me, target, call_id)
            _stop_incoming_alert(target, me, call_id)
        call_bus.publish(target, "call",
                         {"from": me, "call_id": call_id, "kind": kind, "data": data})
        return {"ok": True}

    return router
