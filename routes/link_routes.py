# routes/link_routes.py
"""Home Link — DM the developer from any self-hosted instance.

Two halves, both defined here:

Hub (the developer's instance, ``LINK_HUB_ENABLED=true``): a small public API
that remote instances register against. A guest picks a handle, receives a
bearer token, and their messages land in the hub owner's ordinary Messages
inbox as ``<handle>@remote`` — the owner replies from the normal DM UI
(routes/messaging_routes.py resolves ``@remote`` recipients against the
link_guests table).

Client (every instance, ``RESTIA_HOME_SERVER``, default the upstream author's
hub): the Messages UI shows one extra contact named after the home server's
host. Opening it for the first time asks the user to pick a handle; after
that this instance proxies that one conversation to the hub, authenticated by
the token stored (encrypted) in the single-row home_link table. Set
``RESTIA_HOME_SERVER=`` (empty) to remove the contact entirely — the hub
instance itself should do this so it doesn't offer a chat with itself.
"""

import hashlib
import logging
import os
import re
import secrets
import time
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import and_, or_

from core.database import DirectMessage, HomeLink, LinkGuest, SessionLocal, utcnow_naive
from src.auth_helpers import require_user
from src.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

# Guests are namespaced so they can never shadow a local account; the reverse
# (a local signup grabbing an '@remote' name) is blocked in routes/auth_routes.py.
GUEST_SUFFIX = "@remote"
DEFAULT_HOME_SERVER = "https://app.restia.dev"
HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
MAX_BODY_LEN = 8000            # keep in sync with routes/messaging_routes.py
MESSAGES_PAGE_LIMIT = 200
SUMMARY_CACHE_TTL = 15         # seconds between hub round-trips for badge/list polls

# Sentinel detail string the front-end matches on to show the connect card.
NOT_CONNECTED = "link_not_connected"


class RegisterRequest(BaseModel):
    handle: str


class LinkSendRequest(BaseModel):
    body: str


class ConnectRequest(BaseModel):
    handle: str


# ── Shared helpers ──────────────────────────────────────────────────────────

def _norm(name: Optional[str]) -> str:
    return str(name or "").strip().lower()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _known_users(request: Request) -> dict:
    mgr = getattr(request.app.state, "auth_manager", None)
    users = getattr(mgr, "users", None)
    return users if isinstance(users, dict) else {}


# ── Hub side ────────────────────────────────────────────────────────────────

def hub_enabled() -> bool:
    return os.getenv("LINK_HUB_ENABLED", "false").lower() == "true"


def _require_hub():
    # 404, not 403 — instances that keep the hub off don't advertise it.
    if not hub_enabled():
        raise HTTPException(404, "Not found")


def guest_username(handle: str) -> str:
    return handle + GUEST_SUFFIX


def resolve_guest(name: str) -> Optional[str]:
    """If `name` is a registered guest ('<handle>@remote'), return it
    normalized; else None. Used by messaging_routes so the owner can reply."""
    key = _norm(name)
    if not key.endswith(GUEST_SUFFIX):
        return None
    handle = key[: -len(GUEST_SUFFIX)]
    db = SessionLocal()
    try:
        g = db.query(LinkGuest).filter(LinkGuest.handle == handle).first()
        return key if g else None
    finally:
        db.close()


def list_guests() -> list:
    """All registered guest usernames — the owner's 'new chat' picker."""
    db = SessionLocal()
    try:
        rows = db.query(LinkGuest.handle).order_by(LinkGuest.handle.asc()).all()
        return [guest_username(h) for (h,) in rows]
    finally:
        db.close()


def _owner_username(request: Request) -> str:
    """Who guest messages are addressed to: LINK_OWNER, else the first admin,
    else the first account."""
    env_owner = _norm(os.getenv("LINK_OWNER", ""))
    users = _known_users(request)
    names = sorted(_norm(u) for u in users.keys() if _norm(u))
    if env_owner:
        if env_owner in names:
            return env_owner
        logger.warning("LINK_OWNER=%r is not an existing account", env_owner)
    mgr = getattr(request.app.state, "auth_manager", None)
    for name in names:
        try:
            if mgr and mgr.is_admin(name):
                return name
        except Exception:
            pass
    if names:
        return names[0]
    raise HTTPException(503, "Home Link hub has no owner account yet")


def _guest_from_bearer(request: Request, db) -> LinkGuest:
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = auth[7:].strip()
    g = db.query(LinkGuest).filter(LinkGuest.token_hash == _hash_token(token)).first()
    if not g:
        raise HTTPException(401, "Invalid token")
    return g


def _pair_filter(a: str, b: str):
    return or_(
        and_(DirectMessage.sender == a, DirectMessage.recipient == b),
        and_(DirectMessage.sender == b, DirectMessage.recipient == a),
    )


def _ser(msg: DirectMessage, me: str) -> dict:
    return {
        "id": msg.id,
        "sender": msg.sender,
        "recipient": msg.recipient,
        "body": msg.body,
        "mine": msg.sender == me,
        "created_at": (msg.created_at.isoformat() + "Z") if msg.created_at else None,
        "read": msg.read_at is not None,
    }


def setup_link_hub_routes():
    router = APIRouter(prefix="/api/link", tags=["link"])

    _register_limiter = RateLimiter(max_requests=5, window_seconds=300)
    _send_limiter = RateLimiter(max_requests=30, window_seconds=60)

    @router.post("/register")
    async def register(body: RegisterRequest, request: Request):
        """Claim a handle, get a bearer token. First-come, first-served."""
        _require_hub()
        if not _register_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        handle = _norm(body.handle)
        if not HANDLE_RE.match(handle):
            raise HTTPException(
                400, "Handle must be 1-32 chars: lowercase letters, digits, . _ -")
        gname = guest_username(handle)
        # A guest may not shadow a real account on this hub (either spelling).
        local = {_norm(u) for u in _known_users(request)}
        if handle in local or gname in local:
            raise HTTPException(409, "Handle unavailable")
        owner = _owner_username(request)
        token = secrets.token_urlsafe(32)
        db = SessionLocal()
        try:
            if db.query(LinkGuest).filter(LinkGuest.handle == handle).first():
                raise HTTPException(409, "Handle already taken")
            db.add(LinkGuest(handle=handle, token_hash=_hash_token(token),
                             created_at=utcnow_naive()))
            db.commit()
        finally:
            db.close()
        logger.info("Home Link guest registered: %s", gname)
        return {"ok": True, "handle": handle, "guest": gname,
                "owner": owner, "token": token}

    @router.get("/messages")
    async def fetch_messages(request: Request, after_id: int = 0):
        """The guest's conversation with the owner. Fetching marks the owner's
        messages to the guest as read (the guest's UI only polls while the
        thread is open, mirroring local DM semantics)."""
        _require_hub()
        db = SessionLocal()
        try:
            g = _guest_from_bearer(request, db)
            gname = guest_username(g.handle)
            owner = _owner_username(request)
            q = db.query(DirectMessage).filter(_pair_filter(gname, owner))
            if after_id:
                q = q.filter(DirectMessage.id > after_id)
            msgs = (q.order_by(DirectMessage.created_at.desc())
                    .limit(MESSAGES_PAGE_LIMIT).all())
            msgs.reverse()
            db.query(DirectMessage).filter(
                DirectMessage.sender == owner,
                DirectMessage.recipient == gname,
                DirectMessage.read_at.is_(None),
            ).update({DirectMessage.read_at: utcnow_naive()}, synchronize_session=False)
            g.last_seen = utcnow_naive()
            db.commit()
            return {"messages": [_ser(m, gname) for m in msgs],
                    "owner": owner, "me": gname}
        finally:
            db.close()

    @router.post("/messages")
    async def send_message(body: LinkSendRequest, request: Request):
        _require_hub()
        if not _send_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — slow down")
        text = (body.body or "").strip()
        if not text:
            raise HTTPException(400, "Message body is required")
        if len(text) > MAX_BODY_LEN:
            raise HTTPException(400, f"Message too long (max {MAX_BODY_LEN} characters)")
        db = SessionLocal()
        try:
            g = _guest_from_bearer(request, db)
            gname = guest_username(g.handle)
            owner = _owner_username(request)
            msg = DirectMessage(sender=gname, recipient=owner, body=text,
                                created_at=utcnow_naive(), read_at=None)
            g.last_seen = utcnow_naive()
            db.add(msg)
            db.commit()
            db.refresh(msg)
            return {"message": _ser(msg, gname)}
        finally:
            db.close()

    @router.get("/summary")
    async def summary(request: Request):
        """Last message + unread count, one cheap call for the guest's
        conversation-list/badge polling."""
        _require_hub()
        db = SessionLocal()
        try:
            g = _guest_from_bearer(request, db)
            gname = guest_username(g.handle)
            owner = _owner_username(request)
            last = (db.query(DirectMessage).filter(_pair_filter(gname, owner))
                    .order_by(DirectMessage.created_at.desc()).first())
            unread = (db.query(DirectMessage)
                      .filter(DirectMessage.sender == owner,
                              DirectMessage.recipient == gname,
                              DirectMessage.read_at.is_(None))
                      .count())
            return {
                "owner": owner,
                "unread": unread,
                "last_body": last.body if last else None,
                "last_at": (last.created_at.isoformat() + "Z") if last and last.created_at else None,
                "last_mine": bool(last and last.sender == gname),
            }
        finally:
            db.close()

    return router


# ── Client side ─────────────────────────────────────────────────────────────

def home_server() -> str:
    return os.getenv("RESTIA_HOME_SERVER", DEFAULT_HOME_SERVER).strip().rstrip("/")


def home_enabled() -> bool:
    return bool(home_server())


def home_contact_name() -> str:
    """The special contact's username in the local Messages UI — the home
    server's host, e.g. 'app.restia.dev'."""
    if not home_enabled():
        return ""
    return _norm(urlparse(home_server()).netloc) or _norm(home_server())


def is_home_contact(name: Optional[str]) -> bool:
    return home_enabled() and _norm(name) == home_contact_name()


def _load_home_link(db) -> Optional[HomeLink]:
    return db.query(HomeLink).filter(HomeLink.id == 1).first()


def home_connected() -> bool:
    db = SessionLocal()
    try:
        return _load_home_link(db) is not None
    finally:
        db.close()


async def _hub_call(method: str, path: str, *, token: Optional[str] = None,
                    json_body: Optional[dict] = None,
                    params: Optional[dict] = None) -> dict:
    """One HTTP round-trip to the home server, with errors mapped to local
    HTTP errors. Kept as a single seam so tests can monkeypatch it."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.request(method, home_server() + path,
                                        json=json_body, params=params,
                                        headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Home server unreachable ({e.__class__.__name__})")
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        # A rejected token means we're effectively not connected. Surface the
        # connect-card sentinel instead of 401: a raw 401 would trip the
        # front-end fetch wrapper's redirect-to-/login behavior.
        if resp.status_code == 401:
            raise HTTPException(409, NOT_CONNECTED)
        if resp.status_code in (400, 409, 429):
            raise HTTPException(resp.status_code, detail or "Home server refused the request")
        raise HTTPException(502, detail or f"Home server error ({resp.status_code})")
    return resp.json()


def _require_link(db) -> HomeLink:
    link = _load_home_link(db)
    if not link:
        raise HTTPException(409, NOT_CONNECTED)
    return link


# Conversation-list / badge polls hit the hub at most once per TTL.
_summary_cache = {"ts": 0.0, "data": None}


def _reset_summary_cache():
    _summary_cache["ts"] = 0.0
    _summary_cache["data"] = None


async def _home_summary() -> Optional[dict]:
    """Cached hub summary, or None when not connected / hub unreachable with
    nothing cached."""
    db = SessionLocal()
    try:
        link = _load_home_link(db)
        if not link:
            return None
        token = link.token
    finally:
        db.close()
    now = time.monotonic()
    if now - _summary_cache["ts"] < SUMMARY_CACHE_TTL and _summary_cache["data"] is not None:
        return _summary_cache["data"]
    try:
        data = await _hub_call("GET", "/api/link/summary", token=token)
        _summary_cache["data"] = data
    except HTTPException:
        # Keep serving the last good summary; retry after the TTL.
        pass
    _summary_cache["ts"] = now
    return _summary_cache["data"]


async def home_conversation_entry() -> Optional[dict]:
    """An entry shaped like messaging_routes' conversation rows, or None."""
    if not home_enabled() or not home_connected():
        return None
    s = await _home_summary() or {}
    return {
        "username": home_contact_name(),
        "is_admin": False,
        "home": True,
        "last_body": s.get("last_body"),
        "last_sender": home_contact_name() if not s.get("last_mine") else None,
        "last_at": s.get("last_at"),
        "last_mine": bool(s.get("last_mine")),
        "unread": int(s.get("unread") or 0),
    }


async def home_unread() -> int:
    if not home_enabled() or not home_connected():
        return 0
    s = await _home_summary() or {}
    return int(s.get("unread") or 0)


async def home_get_conversation(me: str, after_id: int = 0) -> dict:
    db = SessionLocal()
    try:
        link = _require_link(db)
        token = link.token
    finally:
        db.close()
    data = await _hub_call("GET", "/api/link/messages", token=token,
                           params={"after_id": after_id})
    _reset_summary_cache()  # fetch marked hub-side messages read
    return {
        "messages": data.get("messages") or [],
        "other": {"username": home_contact_name(), "is_admin": False, "home": True},
        "me": me,
    }


async def home_send_message(body: str) -> dict:
    db = SessionLocal()
    try:
        link = _require_link(db)
        token = link.token
    finally:
        db.close()
    data = await _hub_call("POST", "/api/link/messages", token=token,
                           json_body={"body": body})
    _reset_summary_cache()
    return {"message": data.get("message")}


def setup_home_link_routes():
    router = APIRouter(prefix="/api/homelink", tags=["homelink"])

    @router.get("/status")
    async def status(request: Request):
        require_user(request)
        db = SessionLocal()
        try:
            link = _load_home_link(db)
            return {
                "enabled": home_enabled(),
                "home": home_server(),
                "contact": home_contact_name(),
                "connected": link is not None,
                "handle": link.handle if link else None,
                "owner": link.owner if link else None,
            }
        finally:
            db.close()

    @router.post("/connect")
    async def connect(body: ConnectRequest, request: Request):
        """Register this instance with the home server under a handle."""
        require_user(request)
        if not home_enabled():
            raise HTTPException(404, "Home Link is disabled on this instance")
        handle = _norm(body.handle)
        if not HANDLE_RE.match(handle):
            raise HTTPException(
                400, "Handle must be 1-32 chars: lowercase letters, digits, . _ -")
        data = await _hub_call("POST", "/api/link/register",
                               json_body={"handle": handle})
        db = SessionLocal()
        try:
            old = _load_home_link(db)
            if old:
                db.delete(old)
                db.flush()
            db.add(HomeLink(id=1, home_url=home_server(),
                            handle=data.get("handle") or handle,
                            owner=data.get("owner"),
                            token=data["token"],
                            created_at=utcnow_naive()))
            db.commit()
        finally:
            db.close()
        _reset_summary_cache()
        return {"ok": True, "handle": data.get("handle") or handle,
                "owner": data.get("owner"), "contact": home_contact_name()}

    @router.post("/disconnect")
    async def disconnect(request: Request):
        """Forget the stored registration (the hub keeps the handle)."""
        require_user(request)
        db = SessionLocal()
        try:
            link = _load_home_link(db)
            if link:
                db.delete(link)
                db.commit()
        finally:
            db.close()
        _reset_summary_cache()
        return {"ok": True}

    return router
