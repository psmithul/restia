# routes/link_routes.py
"""Home Link — DM the developer from any self-hosted instance.

Two halves, both defined here:

Hub (the developer's instance, ``LINK_HUB_ENABLED=true``): a small public API
that remote instances register against. A guest picks a handle and receives a
bearer token, but starts **pending**: nothing can be sent or read until the
hub owner approves the request from the Messages UI (or blocks it). Approved
guests' messages land in the owner's ordinary inbox as ``<handle>@remote`` —
the owner replies from the normal DM UI (routes/messaging_routes.py resolves
``@remote`` recipients against the link_guests table).

Client (every instance, ``RESTIA_HOME_SERVER``, default the upstream author's
hub): the Messages UI shows one extra contact named after the home server's
host. Opening it for the first time asks the user to pick a handle; after
that this instance proxies that one conversation to the hub, authenticated by
the token stored (encrypted) in the home_link table — one row per local
account, so users of a shared instance can't read each other's thread. Set
``RESTIA_HOME_SERVER=`` (empty) to remove the contact entirely — the hub
instance itself should do this so it doesn't offer a chat with itself.

Security model, in one place:
  - A guest is never a user account on the hub. The token's entire scope is
    the one guest↔owner conversation; every other route still requires the
    hub's own session auth (only /api/link/register|messages|summary are
    auth-exempt in app.py).
  - Registration is approval-gated (pending → approved/blocked by an admin),
    rate-limited per real client IP (CF-Connecting-IP aware — behind the
    Cloudflare tunnel every request reaches uvicorn from loopback), and
    capped (LINK_MAX_PENDING / LINK_MAX_GUESTS) so bots can't fill the DB.
  - Handles can't shadow local accounts or reserved names, and local signups
    ending in '@remote' are rejected (routes/auth_routes.py), so neither side
    can impersonate the other. Blocking keeps the handle reserved.
  - The register response reveals nothing about the hub (not even the
    owner's username) until the owner approves.
  - Everything the client proxies back from the hub is sanitized
    (_clean_message/_clean_summary): a hostile home server can't inject
    unexpected types or unbounded payloads into the local UI.
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

from core.auth import RESERVED_USERNAMES
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
SUMMARY_CACHE_TTL = 0          # seconds between hub round-trips for badge/list polls
MAX_HUB_RESPONSE_BYTES = 2 * 1024 * 1024  # refuse absurd payloads from a hub

GUEST_PENDING = "pending"
GUEST_APPROVED = "approved"
GUEST_BLOCKED = "blocked"

# Sentinel detail strings the front-end matches on.
NOT_CONNECTED = "link_not_connected"   # no registration stored → show connect card
PENDING = "link_pending"               # registered, awaiting owner approval


def _max_pending() -> int:
    return int(os.getenv("LINK_MAX_PENDING", "25"))


def _max_guests() -> int:
    return int(os.getenv("LINK_MAX_GUESTS", "500"))


class RegisterRequest(BaseModel):
    handle: str


class LinkSendRequest(BaseModel):
    body: str


class ConnectRequest(BaseModel):
    handle: str


class GuestActionRequest(BaseModel):
    action: str          # approve | block | delete


# ── Shared helpers ──────────────────────────────────────────────────────────

def _norm(name: Optional[str]) -> str:
    return str(name or "").strip().lower()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _known_users(request: Request) -> dict:
    mgr = getattr(request.app.state, "auth_manager", None)
    users = getattr(mgr, "users", None)
    return users if isinstance(users, dict) else {}


def _client_ip(request: Request) -> str:
    """Best-effort real client IP for rate limiting. Behind the Cloudflare
    tunnel every request reaches uvicorn from loopback, so client.host alone
    would put all visitors in one bucket (one bot exhausts the limit for
    everyone). CF-Connecting-IP is set by Cloudflare and can't be forged
    through the tunnel; a direct-to-origin caller could forge it, which is
    why the hard caps (_max_pending/_max_guests) exist as the backstop."""
    for header in ("cf-connecting-ip", "x-forwarded-for"):
        v = (request.headers.get(header) or "").split(",")[0].strip()
        if v:
            return v
    return getattr(getattr(request, "client", None), "host", "") or "unknown"


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
    """If `name` is a registered, non-pending guest ('<handle>@remote'),
    return it normalized; else None. Used by messaging_routes so the owner
    can reply. Blocked guests stay resolvable so the owner can still read
    the old thread; pending guests don't (no conversation can exist yet,
    and the owner shouldn't DM someone they haven't approved)."""
    key = _norm(name)
    if not key.endswith(GUEST_SUFFIX):
        return None
    handle = key[: -len(GUEST_SUFFIX)]
    db = SessionLocal()
    try:
        g = db.query(LinkGuest).filter(LinkGuest.handle == handle).first()
        if g and g.status in (GUEST_APPROVED, GUEST_BLOCKED):
            return key
        return None
    finally:
        db.close()


def list_guests() -> list:
    """Approved guest usernames — the owner's 'new chat' picker."""
    db = SessionLocal()
    try:
        rows = (db.query(LinkGuest.handle)
                .filter(LinkGuest.status == GUEST_APPROVED)
                .order_by(LinkGuest.handle.asc()).all())
        return [guest_username(h) for (h,) in rows]
    finally:
        db.close()


def pending_requests() -> list:
    """Pending registrations, oldest first — shown to hub admins in the
    Messages UI so they can approve or block."""
    if not hub_enabled():
        return []
    db = SessionLocal()
    try:
        rows = (db.query(LinkGuest)
                .filter(LinkGuest.status == GUEST_PENDING)
                .order_by(LinkGuest.created_at.asc()).all())
        return [{
            "handle": g.handle,
            "guest": guest_username(g.handle),
            "requested_at": (g.created_at.isoformat() + "Z") if g.created_at else None,
        } for g in rows]
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


def _require_approved(g: LinkGuest):
    """Pending and blocked read identically as 'pending' on purpose: a
    blocked spammer learns nothing from the response, and a guest blocked
    mid-conversation just sees 'waiting' rather than an invitation to
    re-register under a new handle."""
    if g.status != GUEST_APPROVED:
        raise HTTPException(403, PENDING)


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
    _fetch_limiter = RateLimiter(max_requests=240, window_seconds=60)

    @router.post("/register")
    async def register(body: RegisterRequest, request: Request):
        """Ask for a handle. First-come, first-served — but the token stays
        useless until the hub owner approves the request."""
        _require_hub()
        if not _register_limiter.check(_client_ip(request)):
            raise HTTPException(429, "Too many requests — try again later")
        handle = _norm(body.handle)
        if not HANDLE_RE.match(handle):
            raise HTTPException(
                400, "Handle must be 1-32 chars: lowercase letters, digits, . _ -")
        gname = guest_username(handle)
        # A guest may not shadow a real account on this hub (either spelling)
        # or a reserved name.
        local = {_norm(u) for u in _known_users(request)}
        if handle in local or gname in local or handle in RESERVED_USERNAMES:
            raise HTTPException(409, "Handle unavailable")
        token = secrets.token_urlsafe(32)
        db = SessionLocal()
        try:
            # Same detail as the local-account collision above: the response
            # must not reveal whether a handle clashes with a guest or with a
            # real account name (that would enumerate the hub's users).
            if db.query(LinkGuest).filter(LinkGuest.handle == handle).first():
                raise HTTPException(409, "Handle unavailable")
            # Hard caps: the backstop against registration floods from
            # forged/rotating IPs. Pending is what a bot can actually fill.
            pending = db.query(LinkGuest).filter(LinkGuest.status == GUEST_PENDING).count()
            if pending >= _max_pending():
                raise HTTPException(429, "The hub is not accepting new requests right now")
            if db.query(LinkGuest).count() >= _max_guests():
                raise HTTPException(429, "The hub is not accepting new requests right now")
            db.add(LinkGuest(handle=handle, token_hash=_hash_token(token),
                             status=GUEST_PENDING, created_at=utcnow_naive()))
            db.commit()
        finally:
            db.close()
        logger.info("Home Link request: %s (pending approval)", gname)
        # The token is issued now (it's the guest's only credential) but stays
        # useless until approval. Deliberately no owner username / hub details.
        return {"ok": True, "handle": handle, "guest": gname,
                "status": GUEST_PENDING, "token": token}

    @router.get("/messages")
    async def fetch_messages(request: Request, after_id: int = 0):
        """The guest's conversation with the owner. Fetching marks the owner's
        messages to the guest as read (the guest's UI only polls while the
        thread is open, mirroring local DM semantics)."""
        _require_hub()
        if not _fetch_limiter.check(_client_ip(request)):
            raise HTTPException(429, "Too many requests — slow down")
        db = SessionLocal()
        try:
            g = _guest_from_bearer(request, db)
            _require_approved(g)
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
        if not _send_limiter.check(_client_ip(request)):
            raise HTTPException(429, "Too many requests — slow down")
        text = (body.body or "").strip()
        if not text:
            raise HTTPException(400, "Message body is required")
        if len(text) > MAX_BODY_LEN:
            raise HTTPException(400, f"Message too long (max {MAX_BODY_LEN} characters)")
        db = SessionLocal()
        try:
            g = _guest_from_bearer(request, db)
            _require_approved(g)
            gname = guest_username(g.handle)
            owner = _owner_username(request)
            msg = DirectMessage(sender=gname, recipient=owner, body=text,
                                created_at=utcnow_naive(), read_at=None)
            g.last_seen = utcnow_naive()
            db.add(msg)
            db.commit()
            db.refresh(msg)
            # Fan out to the owner's open SSE stream (routes/messaging_routes)
            # so hub-ingested guest messages arrive live too. Imported lazily —
            # messaging_routes imports this module at load time.
            from routes.messaging_routes import publish_message_event
            publish_message_event(msg)
            return {"message": _ser(msg, gname)}
        finally:
            db.close()

    @router.get("/summary")
    async def summary(request: Request):
        """Last message + unread count, one cheap call for the guest's
        conversation-list/badge polling."""
        _require_hub()
        if not _fetch_limiter.check(_client_ip(request)):
            raise HTTPException(429, "Too many requests — slow down")
        db = SessionLocal()
        try:
            g = _guest_from_bearer(request, db)
            _require_approved(g)
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

    # ── Admin: approve / block / delete guests ──────────────────────────────
    # These paths are NOT in app.py's auth-exempt list, so the hub's normal
    # session auth runs first; require_admin then gates to admins.

    from core.middleware import require_admin

    @router.get("/admin/guests")
    async def admin_list_guests(request: Request):
        _require_hub()
        require_admin(request)
        db = SessionLocal()
        try:
            rows = db.query(LinkGuest).order_by(LinkGuest.created_at.asc()).all()
            return {"guests": [{
                "handle": g.handle,
                "guest": guest_username(g.handle),
                "status": g.status,
                "requested_at": (g.created_at.isoformat() + "Z") if g.created_at else None,
                "last_seen": (g.last_seen.isoformat() + "Z") if g.last_seen else None,
            } for g in rows]}
        finally:
            db.close()

    @router.post("/admin/guests/{handle}")
    async def admin_guest_action(handle: str, body: GuestActionRequest, request: Request):
        """approve: guest can chat. block: guest sees 'pending' forever and
        the handle stays reserved. delete: forget the guest entirely (frees
        the handle; their old messages remain in direct_messages)."""
        _require_hub()
        require_admin(request)
        action = _norm(body.action)
        if action not in ("approve", "block", "delete"):
            raise HTTPException(400, "action must be approve, block, or delete")
        db = SessionLocal()
        try:
            g = db.query(LinkGuest).filter(LinkGuest.handle == _norm(handle)).first()
            if not g:
                raise HTTPException(404, "No such guest")
            if action == "delete":
                db.delete(g)
            else:
                g.status = GUEST_APPROVED if action == "approve" else GUEST_BLOCKED
            db.commit()
            logger.info("Home Link guest %s: %s", action, guest_username(_norm(handle)))
            return {"ok": True, "handle": _norm(handle),
                    "status": None if action == "delete" else g.status}
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


def _load_home_link(db, me: str) -> Optional[HomeLink]:
    return db.query(HomeLink).filter(HomeLink.local_user == _norm(me)).first()


def home_connected(me: str) -> bool:
    db = SessionLocal()
    try:
        return _load_home_link(db, me) is not None
    finally:
        db.close()


def _clean_message(m) -> Optional[dict]:
    """Coerce one hub-returned message into the exact shape the local UI
    expects — a hostile home server must not be able to smuggle other types
    or unbounded strings into our API responses."""
    if not isinstance(m, dict):
        return None
    try:
        mid = int(m.get("id") or 0)
    except (TypeError, ValueError):
        mid = 0
    body = m.get("body")
    if not isinstance(body, str):
        return None
    created = m.get("created_at")
    return {
        "id": mid,
        "body": body[:MAX_BODY_LEN],
        "mine": bool(m.get("mine")),
        "created_at": created[:64] if isinstance(created, str) else None,
        "read": bool(m.get("read")),
    }


def _clean_summary(s) -> dict:
    if not isinstance(s, dict):
        return {}
    try:
        unread = max(0, int(s.get("unread") or 0))
    except (TypeError, ValueError):
        unread = 0
    last_body = s.get("last_body")
    last_at = s.get("last_at")
    return {
        "unread": unread,
        "last_body": last_body[:MAX_BODY_LEN] if isinstance(last_body, str) else None,
        "last_at": last_at[:64] if isinstance(last_at, str) else None,
        "last_mine": bool(s.get("last_mine")),
    }


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
    if len(resp.content) > MAX_HUB_RESPONSE_BYTES:
        raise HTTPException(502, "Home server response too large")
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
        # Awaiting (or denied) approval — the front-end shows the waiting card.
        if resp.status_code == 403 and detail == PENDING:
            raise HTTPException(403, PENDING)
        if resp.status_code in (400, 409, 429):
            raise HTTPException(resp.status_code,
                                detail if isinstance(detail, str) and len(detail) <= 300
                                else "Home server refused the request")
        raise HTTPException(502, f"Home server error ({resp.status_code})")
    try:
        data = resp.json()
    except Exception:
        raise HTTPException(502, "Home server returned malformed data")
    return data if isinstance(data, dict) else {}


def _require_link(db, me: str) -> HomeLink:
    link = _load_home_link(db, me)
    if not link:
        raise HTTPException(409, NOT_CONNECTED)
    return link


# Conversation-list / badge polls hit the hub at most once per TTL, per user.
_summary_cache: dict = {}


def _reset_summary_cache(me: Optional[str] = None):
    if me is None:
        _summary_cache.clear()
    else:
        _summary_cache.pop(_norm(me), None)


async def _home_summary(me: str) -> Optional[dict]:
    """Cached hub summary for this local user, or None when not connected /
    pending / hub unreachable with nothing cached."""
    key = _norm(me)
    db = SessionLocal()
    try:
        link = _load_home_link(db, key)
        if not link:
            return None
        token = link.token
    finally:
        db.close()
    now = time.monotonic()
    entry = _summary_cache.get(key)
    if entry and now - entry["ts"] < SUMMARY_CACHE_TTL:
        return entry["data"]
    data = entry["data"] if entry else None
    try:
        data = _clean_summary(await _hub_call("GET", "/api/link/summary", token=token))
    except HTTPException:
        # Pending/unreachable: keep serving the last good summary (or None);
        # retry after the TTL.
        pass
    _summary_cache[key] = {"ts": now, "data": data}
    return data


async def home_conversation_entry(me: str) -> Optional[dict]:
    """An entry shaped like messaging_routes' conversation rows, or None."""
    if not home_enabled() or not home_connected(me):
        return None
    s = await _home_summary(me) or {}
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


async def home_unread(me: str) -> int:
    if not home_enabled() or not home_connected(me):
        return 0
    s = await _home_summary(me) or {}
    return int(s.get("unread") or 0)


async def home_get_conversation(me: str, after_id: int = 0) -> dict:
    db = SessionLocal()
    try:
        link = _require_link(db, me)
        token = link.token
    finally:
        db.close()
    data = await _hub_call("GET", "/api/link/messages", token=token,
                           params={"after_id": int(after_id)})
    _reset_summary_cache(me)  # fetch marked hub-side messages read
    raw = data.get("messages")
    messages = []
    if isinstance(raw, list):
        for m in raw[:MESSAGES_PAGE_LIMIT]:
            clean = _clean_message(m)
            if clean:
                messages.append(clean)
    return {
        "messages": messages,
        "other": {"username": home_contact_name(), "is_admin": False, "home": True},
        "me": me,
    }


async def home_send_message(me: str, body: str) -> dict:
    db = SessionLocal()
    try:
        link = _require_link(db, me)
        token = link.token
    finally:
        db.close()
    data = await _hub_call("POST", "/api/link/messages", token=token,
                           json_body={"body": body})
    _reset_summary_cache(me)
    msg = _clean_message(data.get("message"))
    if not msg:
        raise HTTPException(502, "Home server returned malformed data")
    return {"message": msg}


def setup_home_link_routes():
    router = APIRouter(prefix="/api/homelink", tags=["homelink"])

    @router.get("/status")
    async def status(request: Request):
        me = _norm(require_user(request))
        db = SessionLocal()
        try:
            link = _load_home_link(db, me)
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
        """Register this account with the home server under a handle. The
        registration starts pending until the hub owner approves it."""
        me = _norm(require_user(request))
        if not home_enabled():
            raise HTTPException(404, "Home Link is disabled on this instance")
        handle = _norm(body.handle)
        if not HANDLE_RE.match(handle):
            raise HTTPException(
                400, "Handle must be 1-32 chars: lowercase letters, digits, . _ -")
        data = await _hub_call("POST", "/api/link/register",
                               json_body={"handle": handle})
        token = data.get("token")
        if not isinstance(token, str) or not (20 <= len(token) <= 128):
            raise HTTPException(502, "Home server returned malformed data")
        db = SessionLocal()
        try:
            old = _load_home_link(db, me)
            if old:
                db.delete(old)
                db.flush()
            db.add(HomeLink(local_user=me, home_url=home_server(),
                            handle=data.get("handle") or handle,
                            owner=None,
                            token=token,
                            created_at=utcnow_naive()))
            db.commit()
        finally:
            db.close()
        _reset_summary_cache(me)
        return {"ok": True, "handle": data.get("handle") or handle,
                "status": data.get("status") or GUEST_PENDING,
                "contact": home_contact_name()}

    @router.post("/disconnect")
    async def disconnect(request: Request):
        """Forget the stored registration (the hub keeps the handle)."""
        me = _norm(require_user(request))
        db = SessionLocal()
        try:
            link = _load_home_link(db, me)
            if link:
                db.delete(link)
                db.commit()
        finally:
            db.close()
        _reset_summary_cache(me)
        return {"ok": True}

    return router
