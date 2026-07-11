# routes/call_routes.py
"""WebRTC audio/video calling — signaling relay only.

The media itself is peer-to-peer (encrypted by WebRTC/DTLS-SRTP end to end);
this server only relays the small signaling messages the two browsers use to
find each other — SDP offer/answer and ICE candidates — plus call control
(decline / hang up / busy). It never sees or proxies audio or video.

Signaling rides a dedicated always-on SSE stream (/api/calls/stream) so a call
can ring even when the Messages window is closed, keyed per user through the
same in-process bus pattern as direct messages. NAT traversal needs a TURN
server: calling is only advertised as enabled once one is configured
(TURN_URL), matching the deployment's chosen requirement.

Scope: local accounts on the same instance (they share this signaling bus).
Cross-instance calling would relay through the Home Link federation and is a
later step.
"""

import asyncio
import json
import logging
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from routes.messaging_routes import _MessageBus
from src.auth_helpers import require_user
from src.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# Dedicated signaling bus (separate from DM delivery).
call_bus = _MessageBus()

SSE_KEEPALIVE_S = 15
MAX_SIGNAL_BYTES = 16384       # an SDP blob is a few KB; ICE candidates tiny
SIGNAL_KINDS = {"offer", "answer", "ice", "decline", "hangup", "busy", "cancel"}


class SignalRequest(BaseModel):
    to: str
    call_id: str
    kind: str
    data: Optional[dict] = None


def _norm(name: Optional[str]) -> str:
    return str(name or "").strip().lower()


def _require_me(request: Request) -> str:
    me = _norm(require_user(request))
    if not me:
        raise HTTPException(403, "Calling requires a signed-in account")
    return me


def _known_users(request: Request) -> dict:
    mgr = getattr(request.app.state, "auth_manager", None)
    users = getattr(mgr, "users", None)
    return users if isinstance(users, dict) else {}


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
    """The operator chose to require TURN, so calling is only advertised once a
    TURN server is configured — otherwise calls would silently fail behind most
    home routers."""
    return bool(os.getenv("TURN_URL", "").strip())


def setup_call_routes():
    router = APIRouter(prefix="/api/calls", tags=["calls"])

    _signal_limiter = RateLimiter(max_requests=120, window_seconds=10)  # ICE bursts

    @router.get("/config")
    async def config(request: Request):
        """Whether calling is available here, and the ICE servers the browser
        should use for the peer connection."""
        _require_me(request)
        return {"enabled": calls_enabled(), "ice_servers": _ice_servers()}

    @router.get("/stream")
    async def stream(request: Request):
        """Always-on signaling stream for the signed-in user. Emits `call`
        events: {"from", "call_id", "kind", "data"}."""
        me = _require_me(request)

        async def gen():
            q = call_bus.subscribe(me)
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event, data = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
                        continue
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
            finally:
                call_bus.unsubscribe(me, q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.post("/signal")
    async def signal(body: SignalRequest, request: Request):
        """Relay one signaling message to `to`. The server doesn't interpret the
        payload — it just forwards it to the target's stream — but it does
        enforce that the target is a real local account and bound the size."""
        me = _require_me(request)
        if not _signal_limiter.check(me):
            raise HTTPException(429, "Too many signaling messages")
        kind = _norm(body.kind)
        if kind not in SIGNAL_KINDS:
            raise HTTPException(400, "Unknown signal kind")
        target = _norm(body.to)
        if not target or target == me:
            raise HTTPException(400, "Invalid target")
        if target not in {_norm(u) for u in _known_users(request)}:
            # Same uniform 404 as messaging: don't confirm who exists.
            raise HTTPException(404, "No such recipient")
        call_id = str(body.call_id or "")[:64]
        if not call_id:
            raise HTTPException(400, "call_id required")
        data = body.data or {}
        if len(json.dumps(data)) > MAX_SIGNAL_BYTES:
            raise HTTPException(400, "Signal payload too large")
        call_bus.publish(target, "call",
                         {"from": me, "call_id": call_id, "kind": kind, "data": data})
        return {"ok": True}

    return router
