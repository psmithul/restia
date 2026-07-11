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
from datetime import timedelta
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import and_, or_

from core.auth import RESERVED_USERNAMES
from core.database import (
    DirectMessage, HomeLink, LinkGuest, LinkInvite, RemoteBlock,
    RemoteContactPref, SessionLocal, utcnow_naive,
)
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

# Invite codes: bounded blast radius for a leaked code.
INVITE_DEFAULT_EXPIRY_DAYS = 7
INVITE_MAX_EXPIRY_DAYS = 365
INVITE_MAX_USES_CAP = 100      # a single code can't onboard an unbounded crowd
PUBKEY_MAX_LEN = 128           # base64 X25519 is 44 chars; cap defensively

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
    to: Optional[str] = None     # target local user; omitted → the hub owner


class ConnectRequest(BaseModel):
    handle: str


class RedeemHomeRequest(BaseModel):
    handle: str
    code: str


class GuestActionRequest(BaseModel):
    action: str          # approve | block | delete


class RedeemRequest(BaseModel):
    code: str
    handle: str
    pubkey: Optional[str] = None     # base64 X25519, published for E2EE


class InviteCreateRequest(BaseModel):
    label: Optional[str] = None
    expires_in_days: Optional[int] = None
    max_uses: Optional[int] = None


class BlockRequest(BaseModel):
    handle: str          # guest handle (with or without @remote)
    action: str          # block | unblock


class RemotePrefRequest(BaseModel):
    discoverable: bool


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


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _clean_pubkey(pubkey: Optional[str]) -> Optional[str]:
    """Accept a base64 public key or nothing. A remote caller controls this,
    so cap the length and allow only base64-ish characters — it can't smuggle
    markup or an unbounded blob into the DB / directory responses."""
    if not pubkey or not isinstance(pubkey, str):
        return None
    pk = pubkey.strip()
    if not pk:
        return None
    if len(pk) > PUBKEY_MAX_LEN or not re.match(r"^[A-Za-z0-9+/=_-]+$", pk):
        raise HTTPException(400, "Invalid public key")
    return pk


def _valid_invite(db, code: str) -> Optional[LinkInvite]:
    """A redeemable invite for `code`, or None. Redeemable = exists, not
    revoked, not expired, uses < max_uses. Looked up by hash so the stored
    row never reveals the plaintext code."""
    if not code:
        return None
    inv = db.query(LinkInvite).filter(LinkInvite.code_hash == _hash_code(code)).first()
    if not inv or inv.revoked:
        return None
    if inv.expires_at is not None and inv.expires_at <= utcnow_naive():
        return None
    if inv.uses >= inv.max_uses:
        return None
    return inv


def _is_blocked(db, local_user: str, handle: str) -> bool:
    return db.query(RemoteBlock).filter(
        RemoteBlock.local_user == _norm(local_user),
        RemoteBlock.handle == _norm(handle),
    ).first() is not None


def _is_discoverable(db, request: Request, username: str) -> bool:
    """A local account is reachable by remote guests unless it opted out. The
    hub owner is always reachable, preserving the classic Home Link contract."""
    key = _norm(username)
    try:
        if key == _owner_username(request):
            return True
    except HTTPException:
        pass
    pref = db.query(RemoteContactPref).filter(RemoteContactPref.local_user == key).first()
    return pref is None or bool(pref.discoverable)


def _local_user_pubkey(username: str) -> Optional[str]:
    """A local account's published E2EE public JWK, so a remote guest can
    encrypt to them. None until that user has set up E2EE."""
    db = SessionLocal()
    try:
        from core.database import UserKey
        k = db.query(UserKey).filter(UserKey.username == _norm(username)).first()
        return k.public_jwk if k else None
    finally:
        db.close()


def _directory(request: Request, db, guest: LinkGuest) -> list:
    """Local accounts this guest may message: discoverable, and not blocking
    this guest. Each entry carries the user's E2EE public key (None for now) so
    the guest can encrypt once Feature 2 lands."""
    mgr = getattr(request.app.state, "auth_manager", None)
    out = []
    for uname in _known_users(request):
        key = _norm(uname)
        if not key or not _is_discoverable(db, request, key):
            continue
        if _is_blocked(db, key, guest.handle):
            continue
        try:
            is_admin = bool(mgr and mgr.is_admin(key))
        except Exception:
            is_admin = False
        out.append({"username": key, "is_admin": is_admin, "pubkey": _local_user_pubkey(key)})
    out.sort(key=lambda u: u["username"])
    return out


def _resolve_target(request: Request, db, guest: LinkGuest, to: Optional[str]) -> str:
    """Which local account a guest's message is for. Omitted → the owner (the
    classic 1:1 Home Link contract, kept for old clients). Otherwise the named
    account — but only if it is real, discoverable, and hasn't blocked this
    guest. Every failure reads as the same 404 so a guest can't enumerate the
    userbase or probe block/discoverability state."""
    if not to:
        return _owner_username(request)
    key = _norm(to)
    local = {_norm(u) for u in _known_users(request)}
    if key not in local:
        raise HTTPException(404, "No such recipient")
    if not _is_discoverable(db, request, key) or _is_blocked(db, key, guest.handle):
        raise HTTPException(404, "No such recipient")
    return key


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
    _redeem_limiter = RateLimiter(max_requests=10, window_seconds=300)
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

    @router.post("/redeem")
    async def redeem(body: RedeemRequest, request: Request):
        """Redeem an invite code: create an APPROVED guest immediately — the
        code IS the approval, so there's no pending→approve wait. Same handle
        rules as /register. The use is consumed with a conditional UPDATE so a
        single-use code can't be double-spent by two racing redemptions."""
        _require_hub()
        if not _redeem_limiter.check(_client_ip(request)):
            raise HTTPException(429, "Too many attempts — try again later")
        handle = _norm(body.handle)
        if not HANDLE_RE.match(handle):
            raise HTTPException(
                400, "Handle must be 1-32 chars: lowercase letters, digits, . _ -")
        pubkey = _clean_pubkey(body.pubkey)
        code = (body.code or "").strip()
        gname = guest_username(handle)
        local = {_norm(u) for u in _known_users(request)}
        if handle in local or gname in local or handle in RESERVED_USERNAMES:
            raise HTTPException(409, "Handle unavailable")
        token = secrets.token_urlsafe(32)
        db = SessionLocal()
        try:
            if not _valid_invite(db, code):
                # Generic on purpose: never reveal whether the code was wrong,
                # expired, revoked, or already spent.
                raise HTTPException(403, "Invalid or expired invite code")
            if db.query(LinkGuest).filter(LinkGuest.handle == handle).first():
                raise HTTPException(409, "Handle unavailable")
            if db.query(LinkGuest).count() >= _max_guests():
                raise HTTPException(429, "The hub is not accepting new guests right now")
            # Atomically consume one use: the guard clauses in the WHERE mean two
            # racing redemptions can't both spend the last use of a code.
            consumed = db.query(LinkInvite).filter(
                LinkInvite.code_hash == _hash_code(code),
                LinkInvite.revoked == False,  # noqa: E712
                LinkInvite.uses < LinkInvite.max_uses,
                or_(LinkInvite.expires_at.is_(None), LinkInvite.expires_at > utcnow_naive()),
            ).update({LinkInvite.uses: LinkInvite.uses + 1}, synchronize_session=False)
            if not consumed:
                raise HTTPException(403, "Invalid or expired invite code")
            inv = db.query(LinkInvite).filter(LinkInvite.code_hash == _hash_code(code)).first()
            db.add(LinkGuest(handle=handle, token_hash=_hash_token(token),
                             status=GUEST_APPROVED, created_at=utcnow_naive(),
                             invite_id=inv.id if inv else None, pubkey=pubkey))
            db.commit()
        finally:
            db.close()
        logger.info("Home Link invite redeemed: %s (approved)", gname)
        return {"ok": True, "handle": handle, "guest": gname,
                "status": GUEST_APPROVED, "token": token}

    @router.get("/directory")
    async def directory(request: Request):
        """Local accounts this approved guest may start a chat with — the
        guest's 'new chat' picker on their own instance."""
        _require_hub()
        if not _fetch_limiter.check(_client_ip(request)):
            raise HTTPException(429, "Too many requests — slow down")
        db = SessionLocal()
        try:
            g = _guest_from_bearer(request, db)
            _require_approved(g)
            return {"users": _directory(request, db, g), "me": guest_username(g.handle)}
        finally:
            db.close()

    @router.get("/conversations")
    async def guest_conversations(request: Request):
        """Every local account this guest has a thread with: last message +
        unread, most-recent first. Powers the guest's conversation list."""
        _require_hub()
        if not _fetch_limiter.check(_client_ip(request)):
            raise HTTPException(429, "Too many requests — slow down")
        db = SessionLocal()
        try:
            g = _guest_from_bearer(request, db)
            _require_approved(g)
            gname = guest_username(g.handle)
            rows = (db.query(DirectMessage)
                    .filter(or_(DirectMessage.sender == gname,
                                DirectMessage.recipient == gname))
                    .order_by(DirectMessage.created_at.asc()).all())
            convos: dict = {}
            for m in rows:
                other = m.recipient if m.sender == gname else m.sender
                c = convos.get(other)
                if c is None:
                    c = {"username": other, "last_body": None, "last_at": None,
                         "last_mine": False, "unread": 0}
                    convos[other] = c
                c["last_body"] = m.body
                c["last_at"] = (m.created_at.isoformat() + "Z") if m.created_at else None
                c["last_mine"] = m.sender == gname
                if m.recipient == gname and m.read_at is None:
                    c["unread"] += 1
            ordered = sorted(convos.values(), key=lambda c: c["last_at"] or "", reverse=True)
            return {"conversations": ordered, "me": gname}
        finally:
            db.close()

    @router.get("/messages")
    async def fetch_messages(request: Request, after_id: int = 0, to: Optional[str] = None):
        """The guest's conversation with one local user (`to`; omitted → the
        owner, back-compat). Fetching marks that user's messages to the guest
        as read, mirroring local DM semantics."""
        _require_hub()
        if not _fetch_limiter.check(_client_ip(request)):
            raise HTTPException(429, "Too many requests — slow down")
        db = SessionLocal()
        try:
            g = _guest_from_bearer(request, db)
            _require_approved(g)
            gname = guest_username(g.handle)
            target = _resolve_target(request, db, g, to)
            q = db.query(DirectMessage).filter(_pair_filter(gname, target))
            if after_id:
                q = q.filter(DirectMessage.id > after_id)
            msgs = (q.order_by(DirectMessage.created_at.desc())
                    .limit(MESSAGES_PAGE_LIMIT).all())
            msgs.reverse()
            marked = db.query(DirectMessage).filter(
                DirectMessage.sender == target,
                DirectMessage.recipient == gname,
                DirectMessage.read_at.is_(None),
            ).update({DirectMessage.read_at: utcnow_naive()}, synchronize_session=False)
            g.last_seen = utcnow_naive()
            db.commit()
            if marked:
                # Live read receipt to the local user's open SSE stream.
                try:
                    from routes.messaging_routes import bus
                    bus.publish(target, "read", {"from": gname})
                except Exception:
                    pass
            return {"messages": [_ser(m, gname) for m in msgs],
                    "owner": target, "me": gname}
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
            target = _resolve_target(request, db, g, body.to)
            msg = DirectMessage(sender=gname, recipient=target, body=text,
                                created_at=utcnow_naive(), read_at=None)
            g.last_seen = utcnow_naive()
            db.add(msg)
            db.commit()
            db.refresh(msg)
            # Fan out to the target's open SSE stream (routes/messaging_routes)
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

    # ── Admin: invite codes ─────────────────────────────────────────────────

    @router.post("/admin/invites")
    async def admin_create_invite(body: InviteCreateRequest, request: Request):
        """Mint a one-off (or use-capped) invite code. The plaintext code is
        returned exactly once here — only its hash is stored, so it can't be
        recovered later; revoke and re-issue if it's lost."""
        _require_hub()
        require_admin(request)
        me = _norm(require_user(request)) or "admin"
        days = INVITE_DEFAULT_EXPIRY_DAYS if body.expires_in_days is None else body.expires_in_days
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise HTTPException(400, "expires_in_days must be a number")
        if days <= 0 or days > INVITE_MAX_EXPIRY_DAYS:
            raise HTTPException(400, f"expires_in_days must be 1..{INVITE_MAX_EXPIRY_DAYS}")
        max_uses = 1 if body.max_uses is None else body.max_uses
        try:
            max_uses = int(max_uses)
        except (TypeError, ValueError):
            raise HTTPException(400, "max_uses must be a number")
        if max_uses < 1 or max_uses > INVITE_MAX_USES_CAP:
            raise HTTPException(400, f"max_uses must be 1..{INVITE_MAX_USES_CAP}")
        label = (body.label or "").strip()[:100] or None
        code = secrets.token_urlsafe(24)
        db = SessionLocal()
        try:
            inv = LinkInvite(code_hash=_hash_code(code), created_by=me, label=label,
                             created_at=utcnow_naive(),
                             expires_at=utcnow_naive() + timedelta(days=days),
                             max_uses=max_uses, uses=0, revoked=False)
            db.add(inv)
            db.commit()
            db.refresh(inv)
            logger.info("Home Link invite created by %s (max_uses=%d, %dd)", me, max_uses, days)
            return {"ok": True, "id": inv.id, "code": code, "label": label,
                    "max_uses": max_uses,
                    "expires_at": inv.expires_at.isoformat() + "Z"}
        finally:
            db.close()

    @router.get("/admin/invites")
    async def admin_list_invites(request: Request):
        _require_hub()
        require_admin(request)
        db = SessionLocal()
        try:
            rows = db.query(LinkInvite).order_by(LinkInvite.created_at.desc()).all()
            now = utcnow_naive()
            out = []
            for inv in rows:
                expired = inv.expires_at is not None and inv.expires_at <= now
                spent = inv.uses >= inv.max_uses
                out.append({
                    "id": inv.id, "label": inv.label, "created_by": inv.created_by,
                    "created_at": (inv.created_at.isoformat() + "Z") if inv.created_at else None,
                    "expires_at": (inv.expires_at.isoformat() + "Z") if inv.expires_at else None,
                    "max_uses": inv.max_uses, "uses": inv.uses, "revoked": inv.revoked,
                    "active": (not inv.revoked and not expired and not spent),
                })
            return {"invites": out}
        finally:
            db.close()

    @router.post("/admin/invites/{invite_id}/revoke")
    async def admin_revoke_invite(invite_id: int, request: Request):
        """Kill a code immediately. Guests already created from it keep their
        access (revoke the guest separately if needed)."""
        _require_hub()
        require_admin(request)
        db = SessionLocal()
        try:
            inv = db.query(LinkInvite).filter(LinkInvite.id == invite_id).first()
            if not inv:
                raise HTTPException(404, "No such invite")
            inv.revoked = True
            db.commit()
            return {"ok": True, "id": invite_id, "revoked": True}
        finally:
            db.close()

    # ── Local user: discoverability + per-guest blocks ──────────────────────
    # Session-authed (NOT in app.py's auth-exempt list): each local account
    # controls its own reachability by remote guests.

    def _me_or_403(request: Request) -> str:
        me = _norm(require_user(request))
        if not me:
            raise HTTPException(403, "Sign in required")
        return me

    @router.get("/me/remote-prefs")
    async def get_remote_prefs(request: Request):
        _require_hub()
        me = _me_or_403(request)
        db = SessionLocal()
        try:
            pref = db.query(RemoteContactPref).filter(RemoteContactPref.local_user == me).first()
            blocked = [r.handle for r in
                       db.query(RemoteBlock).filter(RemoteBlock.local_user == me).all()]
            return {"discoverable": pref is None or bool(pref.discoverable), "blocked": blocked}
        finally:
            db.close()

    @router.post("/me/remote-prefs")
    async def set_remote_prefs(body: RemotePrefRequest, request: Request):
        _require_hub()
        me = _me_or_403(request)
        db = SessionLocal()
        try:
            pref = db.query(RemoteContactPref).filter(RemoteContactPref.local_user == me).first()
            if pref is None:
                db.add(RemoteContactPref(local_user=me, discoverable=bool(body.discoverable),
                                         updated_at=utcnow_naive()))
            else:
                pref.discoverable = bool(body.discoverable)
                pref.updated_at = utcnow_naive()
            db.commit()
            return {"ok": True, "discoverable": bool(body.discoverable)}
        finally:
            db.close()

    @router.post("/me/block")
    async def block_guest(body: BlockRequest, request: Request):
        """Block or unblock a specific remote guest for my account only."""
        _require_hub()
        me = _me_or_403(request)
        action = _norm(body.action)
        if action not in ("block", "unblock"):
            raise HTTPException(400, "action must be block or unblock")
        handle = _norm(body.handle)
        if handle.endswith(GUEST_SUFFIX):
            handle = handle[: -len(GUEST_SUFFIX)]
        if not handle:
            raise HTTPException(400, "handle required")
        db = SessionLocal()
        try:
            existing = db.query(RemoteBlock).filter(
                RemoteBlock.local_user == me, RemoteBlock.handle == handle).first()
            if action == "block" and not existing:
                db.add(RemoteBlock(local_user=me, handle=handle, created_at=utcnow_naive()))
                db.commit()
            elif action == "unblock" and existing:
                db.delete(existing)
                db.commit()
            return {"ok": True, "handle": handle, "blocked": action == "block"}
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
        if resp.status_code == 403:
            if detail == PENDING:
                raise HTTPException(403, PENDING)
            # e.g. an invalid/expired invite code — surface the reason, don't 502.
            raise HTTPException(403, detail if isinstance(detail, str) and len(detail) <= 300
                                else "Home server refused the request")
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

    @router.post("/redeem")
    async def redeem_home(body: RedeemHomeRequest, request: Request):
        """Join a hub with an invite code. Unlike /connect (register then wait
        for the owner to approve), a redeemed code is approved on the spot, so
        the conversation is usable immediately."""
        me = _norm(require_user(request))
        if not home_enabled():
            raise HTTPException(404, "Home Link is disabled on this instance")
        handle = _norm(body.handle)
        if not HANDLE_RE.match(handle):
            raise HTTPException(
                400, "Handle must be 1-32 chars: lowercase letters, digits, . _ -")
        code = (body.code or "").strip()
        if not code:
            raise HTTPException(400, "Invite code is required")
        data = await _hub_call("POST", "/api/link/redeem",
                               json_body={"handle": handle, "code": code})
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
                            handle=data.get("handle") or handle, owner=None,
                            token=token, created_at=utcnow_naive()))
            db.commit()
        finally:
            db.close()
        _reset_summary_cache(me)
        return {"ok": True, "handle": data.get("handle") or handle,
                "status": data.get("status") or GUEST_APPROVED,
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
