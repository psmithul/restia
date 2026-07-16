# routes/link_routes.py
"""Home Link — connect and message between Restia installations.

Two halves, both defined here:

Hub (any Restia installation, ``LINK_HUB_ENABLED=true``): a small public API
that remote installations register against. A guest picks a handle and receives a
bearer token, but starts **pending**: nothing can be sent or read until the
hub owner approves the request from the Messages UI (or blocks it). Approved
guests' messages land in the owner's ordinary inbox as ``<handle>@remote`` —
the owner replies from the normal DM UI (routes/messaging_routes.py resolves
``@remote`` recipients against the link_guests table).

Client (an installation that accepts an invitation or requests access): the
Messages UI shows a contact named after the connected Restia host. This
instance proxies that conversation to the selected hub, authenticated by the
token stored (encrypted) in the home_link table — one sentinel row per
installation, reused by its internal profiles. ``RESTIA_HOME_SERVER`` remains
as an optional deployment default for backwards compatibility; fresh installs
start with no preselected contact and connect from the Messages UI instead.

Security model, in one place:
  - A guest is never a user account on the hub. Its bearer authenticates one
    linked installation, not any local profile. It can reach the guest↔owner
    conversation and the closed Projects namespace; every Projects operation
    additionally requires an active, project-scoped owner grant.
  - Registration is approval-gated (pending → approved/blocked by the owner),
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

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import tempfile
import time
import uuid
from datetime import timedelta
from functools import wraps
from typing import Any, BinaryIO, Dict, Literal, Optional
from urllib.parse import quote, unquote, urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy import and_, func, or_, text
from sqlalchemy.orm import load_only
from starlette.concurrency import run_in_threadpool

from core.auth import RESERVED_USERNAMES
from core.database import (
    DirectMessage, DirectMessageAttachment, HomeLink, LinkGuest, LinkInvite,
    OutboundChatLink, Project,
    ProjectRemoteGrant, ProjectWorkItem, RemoteBlock, RemoteContactPref,
    SessionLocal, utcnow_naive,
)
from core.middleware import require_admin
from core.project_upload_limit import PROJECT_ATTACHMENT_REQUEST_MAX_BYTES
from src.auth_helpers import require_user
from src.public_origin import canonical_shared_origin, is_loopback_origin
from src.rate_limiter import RateLimiter
from src.project_office_preview import (
    OFFICE_PREVIEW_MAX_RESPONSE_BYTES,
    OfficePreviewError,
    sanitize_office_preview_payload,
)
from src.upload_limits import PROJECT_ATTACHMENT_MAX_BYTES
from src.settings import get_setting

logger = logging.getLogger(__name__)

# Guests are namespaced so they can never shadow a local account; the reverse
# (a local signup grabbing an '@remote' name) is blocked in routes/auth_routes.py.
GUEST_SUFFIX = "@remote"
DEFAULT_HOME_SERVER = ""
CONNECTION_INVITE_PREFIX = "restia-invite:v1?"
HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
MAX_BODY_LEN = 8000            # keep in sync with routes/messaging_routes.py
MESSAGES_PAGE_LIMIT = 200
# Seconds a hub summary (last message + unread, for the list row / badge) is
# reused before another round-trip. 0 meant every conversation-list and badge
# poll blocked on a network call to the remote hub — a big, constant latency
# hit for any instance with a Home Link contact. The open thread still polls
# the hub directly (THREAD_POLL_HOME_MS) for actual messages, so a few seconds
# of list-preview staleness is invisible but removes most of the remote calls.
SUMMARY_CACHE_TTL = 4
MAX_HUB_RESPONSE_BYTES = 2 * 1024 * 1024  # refuse absurd payloads from a hub
MAX_HUB_MEDIA_RESPONSE_BYTES = 3 * 1024 * 1024
# Projects has intentionally large but finite response shapes.  The archived
# board can contain 10,000 cards (each with a 240-character title and thirty
# 40-character labels), while item detail/comment pages can contain 200
# 50,000-character comments plus checklist and attachment metadata.  128 MiB
# covers those legal maxima even under worst-case JSON string escaping while
# still putting a hard boundary around a hostile or broken home server.
MAX_PROJECT_PROXY_JSON_RESPONSE_BYTES = 128 * 1024 * 1024
MAX_HUB_RESPONSE_CEILING_BYTES = max(
    MAX_HUB_MEDIA_RESPONSE_BYTES,
    MAX_PROJECT_PROXY_JSON_RESPONSE_BYTES,
)
MAX_PHOTOS_PER_MESSAGE = 1
MAX_PHOTO_PIXELS = 12_000_000
MAX_FEDERATED_PHOTO_BYTES = 2 * 1024 * 1024
MAX_FEDERATED_PHOTO_DATA_CHARS = ((MAX_FEDERATED_PHOTO_BYTES + 2) // 3) * 4 + 128
PHOTO_ID_RE = re.compile(r"^[0-9a-f]{32}$")
PHOTO_MIMES = {"image/png", "image/jpeg", "image/webp"}
INSTANCE_LINK_USER = "__instance__"
OUTBOUND_CHAT_PREFIX = "restia:"
# The bearer-facing hub API represents the whole installation as one external
# user.  Local login names are profiles and must never become routable or
# discoverable through a Home Link credential.
INSTANCE_REMOTE_ALIAS = "instance"
MAX_CALL_SSE_FRAME_BYTES = 24 * 1024
MAX_CALL_SSE_BUFFER_BYTES = 48 * 1024
HOME_LINK_TERMINAL_GRACE_S = 3.0
HOME_CALL_WATCH_IDLE_S = 5.0
HOME_CALL_WATCH_MAX_BACKOFF_S = 20.0
# This marker is deliberately a browser-visible SSE event rather than a
# comment.  The local Home Link proxy can accept its browser connection before
# it has reached (and authenticated to) the remote hub; EventSource's native
# ``open`` event therefore is not proof that call signaling is usable.  Emit a
# credential-free readiness event only after the upstream bearer-scoped queue
# is subscribed.
_HOME_CALL_UPSTREAM_READY = (
    'event: call-transport\n'
    'data: {"status":"ready"}\n\n'
)
_home_call_watch_config_warned = False
MAX_CALL_TARGET_LEN = 96
MAX_CALL_KIND_LEN = 16
MAX_PROJECT_PROXY_JSON_BYTES = 512 * 1024
PROJECT_PROXY_UPLOAD_SPOOL_BYTES = 1024 * 1024
PROJECT_MUTATION_INDETERMINATE_DETAIL = (
    "Home server did not confirm this project change. It may have succeeded; "
    "reload linked projects before retrying."
)
PROJECT_UPLOAD_UPSTREAM_ERROR_DETAILS = {
    408: "Project attachment upload timed out",
    413: "Project attachment is too large",
    415: "Project attachment upload must be multipart/form-data",
    422: "Invalid project attachment upload request",
    507: "Home server could not store the project attachment",
}
PROJECT_ATTACHMENT_PREVIEW_MIMES = frozenset({
    "application/pdf",
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
})
_PROJECT_PREVIEW_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
_PROJECT_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
_PROJECT_UNSATISFIED_RANGE_RE = re.compile(r"^bytes \*/(\d+)$")

# Home Link's browser-facing Projects proxy is deliberately a closed protocol,
# not a general-purpose forwarder. Each path segment is bounded and the allowed
# method for every route shape is enumerated below.
_PROJECT_PROXY_SEGMENT = (
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_PROJECT_PROXY_RULES = tuple(
    (re.compile(pattern), frozenset(methods))
    for pattern, methods in (
        (r"", ("GET",)),
        (r"overview", ("GET",)),
        (r"invitations", ("GET",)),
        (rf"invitations/{_PROJECT_PROXY_SEGMENT}/respond", ("POST",)),
        (rf"attachments/{_PROJECT_PROXY_SEGMENT}/download", ("GET",)),
        (rf"attachments/{_PROJECT_PROXY_SEGMENT}/view", ("GET",)),
        (rf"attachments/{_PROJECT_PROXY_SEGMENT}/preview", ("GET",)),
        (rf"{_PROJECT_PROXY_SEGMENT}", ("GET", "PATCH")),
        (rf"{_PROJECT_PROXY_SEGMENT}/(?:overview|context|board|stages|items|activity)", ("GET",)),
        (rf"{_PROJECT_PROXY_SEGMENT}/stages", ("POST",)),
        (rf"{_PROJECT_PROXY_SEGMENT}/stages/order", ("PUT",)),
        (rf"{_PROJECT_PROXY_SEGMENT}/stages/{_PROJECT_PROXY_SEGMENT}", ("PATCH", "DELETE")),
        (rf"{_PROJECT_PROXY_SEGMENT}/items", ("POST",)),
        (rf"{_PROJECT_PROXY_SEGMENT}/items/order", ("PUT",)),
        (rf"{_PROJECT_PROXY_SEGMENT}/items/{_PROJECT_PROXY_SEGMENT}", ("GET", "PATCH", "DELETE")),
        (rf"{_PROJECT_PROXY_SEGMENT}/items/{_PROJECT_PROXY_SEGMENT}/(?:move|archive|restore)", ("POST",)),
        (rf"{_PROJECT_PROXY_SEGMENT}/items/{_PROJECT_PROXY_SEGMENT}/checklist", ("POST",)),
        (rf"{_PROJECT_PROXY_SEGMENT}/items/{_PROJECT_PROXY_SEGMENT}/checklist/{_PROJECT_PROXY_SEGMENT}", ("PATCH", "DELETE")),
        (rf"{_PROJECT_PROXY_SEGMENT}/items/{_PROJECT_PROXY_SEGMENT}/comments", ("GET", "POST")),
        (rf"{_PROJECT_PROXY_SEGMENT}/items/{_PROJECT_PROXY_SEGMENT}/comments/{_PROJECT_PROXY_SEGMENT}", ("PATCH", "DELETE")),
        (rf"{_PROJECT_PROXY_SEGMENT}/items/{_PROJECT_PROXY_SEGMENT}/attachments", ("GET", "POST")),
        (rf"{_PROJECT_PROXY_SEGMENT}/attachments/{_PROJECT_PROXY_SEGMENT}", ("DELETE",)),
    )
)
_PROJECT_PROXY_QUERY_KEYS = frozenset({
    "archived",
    "assignee",
    "before",
    "include_archived",
    "limit",
    "move_to_stage_id",
    "offset",
    "q",
    "stage_id",
    "version",
    "work_item_id",
})

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
REVOKE_REQUIRED = "home_revoke_required"

_home_link_lifecycle_lock = asyncio.Lock()
_project_remote_limiter = RateLimiter(max_requests=600, window_seconds=60)
_project_remote_invalid_limiter = RateLimiter(max_requests=60, window_seconds=60)


def _serialized_home_link_lifecycle(fn):
    """Serialize installation-wide pairing changes within the app process."""
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        async with _home_link_lifecycle_lock:
            return await fn(*args, **kwargs)
    return wrapped


def _max_pending() -> int:
    return int(os.getenv("LINK_MAX_PENDING", "25"))


def _max_guests() -> int:
    return int(os.getenv("LINK_MAX_GUESTS", "500"))


def _serialize_guest_admission(db) -> None:
    """Reserve the durable guest-cap admission lane before count-then-insert."""

    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        # SQLite's writer reservation is cross-thread and cross-process.
        db.execute(text("BEGIN IMMEDIATE"))
    elif dialect == "postgresql":
        # Stable transaction-scoped advisory key for LinkGuest admissions.
        db.execute(text("SELECT pg_advisory_xact_lock(57962613968965)"))


class RegisterRequest(BaseModel):
    handle: str
    scope: Literal["full", "chat"] = "full"


class LinkPhotoRequest(BaseModel):
    name: str = Field(default="photo", max_length=240)
    data: str = Field(..., min_length=1, max_length=MAX_FEDERATED_PHOTO_DATA_CHARS)


class LinkSendRequest(BaseModel):
    body: str = ""
    # Compatibility field for older clients. Only the opaque installation
    # alias is accepted; local profile names are never valid bearer targets.
    to: Optional[str] = None
    attachments: list[LinkPhotoRequest] = Field(
        default_factory=list,
        max_length=MAX_PHOTOS_PER_MESSAGE,
    )


class ConnectRequest(BaseModel):
    handle: str
    home_url: Optional[str] = Field(default=None, max_length=2048)
    force_replace: bool = False


class RedeemHomeRequest(BaseModel):
    handle: str
    code: str
    home_url: Optional[str] = Field(default=None, max_length=2048)
    force_replace: bool = False


class DisconnectHomeRequest(BaseModel):
    force_local: bool = False


class ProjectInvitationResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["accept", "decline"]
    version: int = Field(ge=1)


class GuestActionRequest(BaseModel):
    action: str          # approve | block | delete


class RedeemRequest(BaseModel):
    code: str
    handle: str
    pubkey: Optional[str] = None     # base64 X25519, published for E2EE
    scope: Optional[Literal["chat", "project"]] = None


class InviteCreateRequest(BaseModel):
    label: Optional[str] = None
    expires_in_days: Optional[int] = None
    max_uses: Optional[int] = None
    hub_url: Optional[str] = Field(default=None, max_length=2048)


class BlockRequest(BaseModel):
    handle: str          # guest handle (with or without @remote)
    action: str          # block | unblock


class RemotePrefRequest(BaseModel):
    discoverable: bool


class LinkCallSignalRequest(BaseModel):
    """Profile-free signaling envelope accepted from a linked instance.

    ``to`` and ``from`` are intentionally absent and unknown keys are
    rejected. The bearer token supplies the guest identity and the hub's
    configured owner supplies the only possible destination.
    """

    model_config = ConfigDict(extra="forbid")

    call_id: StrictStr = Field(min_length=36, max_length=36)
    kind: StrictStr = Field(min_length=1, max_length=MAX_CALL_KIND_LEN)
    data: Optional[Dict[str, Any]] = None


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


def _raw_client_ip(request: Request) -> str:
    """Return the transport peer without trusting caller-controlled headers.

    Home Link's public registration and messaging endpoints retain their
    Cloudflare-aware limiter key above. Projects needs a different boundary:
    before a bearer has authenticated, forwarding headers are attacker input.
    Once authenticated, requests are keyed by the durable guest identity instead
    of any network address, so legitimate instances behind one tunnel/NAT do not
    throttle each other.
    """

    value = str(getattr(getattr(request, "client", None), "host", "") or "").strip()
    return value[:128] or "unknown"


# ── Hub side ────────────────────────────────────────────────────────────────

def hub_enabled() -> bool:
    # The bearer-facing routes expose no local account surface and every
    # registration is approval- or invite-gated. Enable this safe boundary by
    # default so every Restia install can issue and accept connection invites;
    # operators can still opt out explicitly.
    return os.getenv("LINK_HUB_ENABLED", "true").lower() == "true"


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


def guest_contact_capabilities(name: str) -> dict:
    """Return browser-safe capabilities for one inbound Restia contact.

    The guest's stored scope is the authority. A general Messages invitation
    must not inherit the historical remote-call capability merely because it
    is visible to the hub owner.
    """
    key = _norm(name)
    if not key.endswith(GUEST_SUFFIX):
        return {"remote": False, "chat_only": False, "can_call": False}
    handle = key[: -len(GUEST_SUFFIX)]
    db = SessionLocal()
    try:
        guest = db.query(LinkGuest).filter(LinkGuest.handle == handle).first()
        if guest is None or guest.status not in (GUEST_APPROVED, GUEST_BLOCKED):
            return {"remote": True, "chat_only": True, "can_call": False}
        chat_only = str(getattr(guest, "scope", "full") or "full") == "chat"
        return {
            "remote": True,
            "chat_only": chat_only,
            "can_call": not chat_only and guest.status == GUEST_APPROVED,
        }
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


def invitation_origin(request: Request, supplied: Optional[str] = None) -> str:
    """Return the origin advertised inside a portable Restia invitation.

    Explicit input and the configured Public App URL must be HTTPS, except for
    loopback development. A remote Host header is never trusted as an
    internet-reachable identity; request.base_url is only a convenience on
    loopback.
    """
    raw_supplied = str(supplied or "").strip()
    if raw_supplied:
        return canonical_shared_origin(raw_supplied, allow_loopback_http=True)
    configured = canonical_shared_origin(
        get_setting("app_public_url", ""),
        allow_loopback_http=False,
    )
    if configured:
        return configured
    fallback = canonical_shared_origin(
        str(getattr(request, "base_url", "") or "").rstrip("/"),
        allow_loopback_http=True,
    )
    return fallback if is_loopback_origin(fallback) else ""


def build_connection_invitation(hub_url: str, code: str) -> str:
    """Build the pasteable invitation accepted by the Messages UI."""
    origin = canonical_shared_origin(hub_url, allow_loopback_http=True)
    clean_code = str(code or "").strip()
    if not origin or not clean_code:
        return ""
    return CONNECTION_INVITE_PREFIX + urlencode({
        "scope": "chat",
        "hub": origin,
        "code": clean_code,
    })


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


def _directory(request: Request, db, guest: LinkGuest) -> list:
    """Return one opaque installation identity, never its internal profiles.

    Older Home Link builds exposed usernames, admin flags, and per-profile
    public keys here. A bearer credential identifies another *installation*,
    not a profile-directory capability, so the only externally routable peer
    is the configured instance owner behind this fixed alias.
    """
    owner = _owner_username(request)
    if _is_blocked(db, owner, guest.handle):
        return []
    return [{"username": INSTANCE_REMOTE_ALIAS}]


def _resolve_target(request: Request, db, guest: LinkGuest, to: Optional[str]) -> str:
    """Resolve the one installation-scoped bearer target.

    Omitted targets remain compatible with existing clients. Explicit targets
    must use the opaque installation alias; accepting a local username here
    would let a bearer enumerate or inject messages into internal profiles.
    """
    if to is not None and _norm(to) != INSTANCE_REMOTE_ALIAS:
        raise HTTPException(404, "No such recipient")
    owner = _owner_username(request)
    if _is_blocked(db, owner, guest.handle):
        raise HTTPException(404, "No such recipient")
    return owner


def _owner_username(request: Request) -> str:
    """Who guest messages are addressed to: an admin LINK_OWNER, then an admin.

    Connection management is owner-only and admin-authenticated, so accepting
    a configured non-admin here would make the approval queue impossible for
    every profile. Anonymous/single-profile modes retain the final-account
    fallback.
    """
    env_owner = _norm(os.getenv("LINK_OWNER", ""))
    users = _known_users(request)
    names = sorted(_norm(u) for u in users.keys() if _norm(u))
    mgr = getattr(request.app.state, "auth_manager", None)
    if env_owner:
        if env_owner in names:
            try:
                if not getattr(mgr, "is_configured", False) or mgr.is_admin(env_owner):
                    return env_owner
                logger.warning("LINK_OWNER=%r is not an administrator; using an admin owner", env_owner)
            except Exception:
                logger.warning("Could not validate LINK_OWNER=%r; using an admin owner", env_owner)
        else:
            logger.warning("LINK_OWNER=%r is not an existing account", env_owner)
    for name in names:
        try:
            if mgr and mgr.is_admin(name):
                return name
        except Exception:
            pass
    if names:
        return names[0]
    raise HTTPException(503, "Home Link hub has no owner account yet")


def is_hub_call_owner(request: Request, username: str) -> bool:
    """Whether ``username`` is the one profile allowed to call linked guests."""
    if not hub_enabled():
        return False
    try:
        return _norm(username) == _owner_username(request)
    except HTTPException:
        return False


def is_hub_owner(request: Request, username: Optional[str]) -> bool:
    """Whether this profile is the installation identity behind hub guests."""
    return is_hub_call_owner(request, _norm(username))


def _require_call_pair(db, guest: LinkGuest, owner: str) -> tuple[str, str]:
    """Authorize the single guest↔hub-owner pair used by federated calls."""
    _require_approved(guest)
    if str(getattr(guest, "scope", "full") or "full") == "chat":
        raise HTTPException(404, "Call not available")
    owner = _norm(owner)
    gname = guest_username(guest.handle)
    if not owner or _is_blocked(db, owner, guest.handle):
        raise HTTPException(404, "Call not available")
    # A bearer credential alone cannot ring an account it has never interacted
    # with. Requiring a durable DM pair also makes the call button and the
    # signaling authorization share the same relationship boundary.
    if db.query(DirectMessage.id).filter(_pair_filter(gname, owner)).first() is None:
        raise HTTPException(403, "Call requires an existing conversation")
    return gname, owner


def authorize_local_remote_call(request: Request, local_user: str,
                                remote_name: str) -> tuple[str, str, str]:
    """Authorize a hub-side profile signaling to one linked guest."""
    owner = _owner_username(request)
    if _norm(local_user) != owner:
        raise HTTPException(404, "No such recipient")
    target = _norm(remote_name)
    if not target.endswith(GUEST_SUFFIX):
        raise HTTPException(404, "No such recipient")
    handle = target[: -len(GUEST_SUFFIX)]
    db = SessionLocal()
    try:
        guest = db.query(LinkGuest).filter(LinkGuest.handle == handle).first()
        if guest is None:
            raise HTTPException(404, "No such recipient")
        gname, owner = _require_call_pair(db, guest, owner)
        return gname, owner, str(guest.token_hash)
    finally:
        db.close()


def _guest_call_pair_still_allowed(request: Request, gname: str, owner: str,
                                   guest_identity: str) -> bool:
    """Revalidate a long-lived guest stream so block/revoke takes effect."""
    try:
        if _owner_username(request) != _norm(owner):
            return False
        key = _norm(gname)
        if not key.endswith(GUEST_SUFFIX):
            return False
        handle = key[: -len(GUEST_SUFFIX)]
        db = SessionLocal()
        try:
            guest = db.query(LinkGuest).filter(
                LinkGuest.token_hash == str(guest_identity),
                LinkGuest.handle == handle,
            ).first()
            if guest is None:
                return False
            _require_call_pair(db, guest, owner)
            return True
        finally:
            db.close()
    except HTTPException:
        return False


def _clean_remote_bus_call(data) -> tuple[Optional[dict], bool]:
    """Strip the internal terminal marker before crossing the hub boundary."""
    if not isinstance(data, dict):
        return None, False
    terminal = data.get("_server_terminal") is True
    candidate = {key: data.get(key) for key in ("call_id", "kind", "data")}
    from routes import call_routes
    return call_routes.clean_federated_event(candidate), terminal


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


def _revoke_guest_project_grants(
    db,
    guest: LinkGuest,
    *,
    project_owner: Optional[str] = None,
) -> int:
    """Detach this guest's project capabilities, optionally for one owner.

    Admin block/purge is installation-global. ``/me/block`` is explicitly a
    per-profile action, so it must only revoke grants on projects that profile
    owns and must leave the bearer usable for other project owners.
    """
    guest_id = getattr(guest, "id", None)
    if guest_id is None:
        return 0
    now = utcnow_naive()
    grant_query = db.query(ProjectRemoteGrant.id).filter(
        ProjectRemoteGrant.guest_id == int(guest_id)
    )
    normalized_owner = _norm(project_owner)
    if normalized_owner:
        grant_query = grant_query.join(
            Project, Project.id == ProjectRemoteGrant.project_id
        ).filter(func.lower(Project.owner) == normalized_owner)
    grant_ids = [
        str(grant_id).strip().lower()
        for (grant_id,) in grant_query.all()
    ]
    if not grant_ids:
        return 0
    principals = [f"remote:{grant_id}" for grant_id in grant_ids]
    db.query(ProjectWorkItem).filter(
        ProjectWorkItem.assignee.in_(principals)
    ).update(
        {
            ProjectWorkItem.assignee: None,
            ProjectWorkItem.version: ProjectWorkItem.version + 1,
            ProjectWorkItem.updated_at: now,
        },
        synchronize_session=False,
    )
    return int(
        db.query(ProjectRemoteGrant)
        .filter(ProjectRemoteGrant.id.in_(grant_ids))
        .update(
            {
                ProjectRemoteGrant.guest_id: None,
                ProjectRemoteGrant.status: "revoked",
                ProjectRemoteGrant.revoked_at: now,
                ProjectRemoteGrant.version: ProjectRemoteGrant.version + 1,
            },
            synchronize_session=False,
        )
        or 0
    )


def _purge_guest_identity(db, guest: LinkGuest) -> str:
    """Delete one guest and all handle-keyed history before handle reuse."""
    # Null the immutable guest association before deleting the credential row.
    # The handle snapshot remains audit-only and can never authorize a later
    # guest who happens to register the same spelling.
    _revoke_guest_project_grants(db, guest)
    gname = guest_username(guest.handle)
    message_ids = [
        row_id for (row_id,) in db.query(DirectMessage.id).filter(
            or_(DirectMessage.sender == gname,
                DirectMessage.recipient == gname)
        ).all()
    ]
    if message_ids:
        db.query(DirectMessageAttachment).filter(
            DirectMessageAttachment.message_id.in_(message_ids)
        ).delete(synchronize_session=False)
        db.query(DirectMessage).filter(
            DirectMessage.id.in_(message_ids)
        ).delete(synchronize_session=False)
    credential = str(guest.token_hash)
    db.delete(guest)
    return credential


def _attachment_meta(row: DirectMessageAttachment) -> dict:
    return {
        "id": row.id,
        "name": row.filename,
        "mime": row.mime,
        "size": int(row.size or 0),
        "width": int(row.width or 0),
        "height": int(row.height or 0),
    }


def _attachments_by_message(db, message_ids) -> dict:
    ids = {int(mid) for mid in (message_ids or []) if mid is not None}
    if not ids:
        return {}
    rows = (
        db.query(DirectMessageAttachment)
        .options(load_only(
            DirectMessageAttachment.id,
            DirectMessageAttachment.message_id,
            DirectMessageAttachment.filename,
            DirectMessageAttachment.mime,
            DirectMessageAttachment.size,
            DirectMessageAttachment.width,
            DirectMessageAttachment.height,
            DirectMessageAttachment.created_at,
        ))
        .filter(DirectMessageAttachment.message_id.in_(ids))
        .order_by(DirectMessageAttachment.created_at.asc())
        .all()
    )
    out = {}
    for row in rows:
        out.setdefault(row.message_id, []).append(row)
    return out


def _photo_preview(rows) -> str:
    count = len(rows or [])
    return "Photo" if count == 1 else (f"{count} photos" if count else "")


def _ser(msg: DirectMessage, me: str, attachments=None,
         *, hide_local_profile: bool = False) -> dict:
    mine = msg.sender == me
    sender = msg.sender
    recipient = msg.recipient
    if hide_local_profile:
        sender = me if mine else INSTANCE_REMOTE_ALIAS
        recipient = INSTANCE_REMOTE_ALIAS if mine else me
    return {
        "id": msg.id,
        "sender": sender,
        "recipient": recipient,
        "body": msg.body,
        "mine": mine,
        "created_at": (msg.created_at.isoformat() + "Z") if msg.created_at else None,
        "read": msg.read_at is not None,
        "attachments": [_attachment_meta(row) for row in (attachments or [])],
    }


def _project_invitation_dict(
    grant: ProjectRemoteGrant,
    project: Project,
) -> dict[str, Any]:
    """Return the only project metadata exposed before a grant is accepted."""
    return {
        "id": str(grant.id),
        "role": str(grant.role),
        "status": str(grant.status),
        "version": int(grant.version or 1),
        "invited_at": (
            grant.invited_at.isoformat() + "Z" if grant.invited_at else None
        ),
        "responded_at": (
            grant.responded_at.isoformat() + "Z" if grant.responded_at else None
        ),
        "project": {
            "id": str(project.id),
            "key": str(project.key),
            "name": str(project.name),
            "description": str(project.description or ""),
            "color": str(project.color),
            "icon": str(project.icon) if project.icon else None,
            "archived": bool(project.archived),
        },
    }


async def require_link_project_remote(request: Request) -> str:
    """Authenticate and project-scope one public Home Link Projects request."""
    _require_hub()
    db = SessionLocal()
    try:
        try:
            guest = _guest_from_bearer(request, db)
        except HTTPException as exc:
            if exc.status_code == 401 and not _project_remote_invalid_limiter.check(
                _raw_client_ip(request)
            ):
                raise HTTPException(429, "Too many requests — slow down") from exc
            raise
        if not _project_remote_limiter.check(f"guest:{int(guest.id)}"):
            raise HTTPException(429, "Too many requests — slow down")
        _require_approved(guest)
        request_path = str(getattr(getattr(request, "url", None), "path", "") or "")
        prefix = "/api/link/projects"
        if request_path != prefix and not request_path.startswith(prefix + "/"):
            raise HTTPException(404, "Project route not found")
        remote_path = request_path[len(prefix):].strip("/")
        _project_proxy_kind(request.method, remote_path)
        grant_id = None
        project_id = request.path_params.get("project_id")
        if project_id:
            row = (
                db.query(ProjectRemoteGrant, Project)
                .join(Project, Project.id == ProjectRemoteGrant.project_id)
                .filter(
                    ProjectRemoteGrant.project_id == str(project_id),
                    ProjectRemoteGrant.guest_id == int(guest.id),
                    ProjectRemoteGrant.status == "active",
                )
                .first()
            )
            if row is None:
                # Do not reveal whether the project exists to a different
                # linked installation.
                raise HTTPException(404, "Project not found")
            grant, project = row
            if _is_blocked(db, project.owner, guest.handle):
                raise HTTPException(404, "Project not found")
            grant_id = str(grant.id)
        from routes.project_routes import set_remote_project_context

        principal = set_remote_project_context(
            request,
            guest_id=int(guest.id),
            grant_id=grant_id,
        )
        request.state.project_remote_handle = str(guest.handle)
        guest.last_seen = utcnow_naive()
        db.commit()
        return principal
    finally:
        db.close()


def setup_link_project_invitation_routes() -> APIRouter:
    """Bearer-only invitation inbox for a linked installation."""
    router = APIRouter(
        prefix="/api/link/projects",
        tags=["link-projects"],
        dependencies=[Depends(require_link_project_remote)],
    )

    @router.get("/invitations")
    async def list_project_invitations(request: Request):
        guest_id = getattr(request.state, "project_remote_guest_id", None)
        if guest_id is None:
            raise HTTPException(401, "Invalid project credential")
        db = SessionLocal()
        try:
            handle = str(getattr(request.state, "project_remote_handle", "") or "")
            rows = (
                db.query(ProjectRemoteGrant, Project)
                .join(Project, Project.id == ProjectRemoteGrant.project_id)
                .filter(
                    ProjectRemoteGrant.guest_id == int(guest_id),
                    ProjectRemoteGrant.status == "pending",
                )
                .order_by(ProjectRemoteGrant.invited_at.asc())
                .all()
            )
            rows = [
                (grant, project)
                for grant, project in rows
                if handle and not _is_blocked(db, project.owner, handle)
            ]
            return {
                "invitations": [
                    _project_invitation_dict(grant, project)
                    for grant, project in rows
                ]
            }
        finally:
            db.close()

    @router.post("/invitations/{grant_id}/respond")
    async def respond_to_project_invitation(
        grant_id: str,
        body: ProjectInvitationResponseRequest,
        request: Request,
    ):
        guest_id = getattr(request.state, "project_remote_guest_id", None)
        if guest_id is None:
            raise HTTPException(401, "Invalid project credential")
        next_status = "active" if body.action == "accept" else "declined"
        now = utcnow_naive()
        db = SessionLocal()
        try:
            invitation = (
                db.query(ProjectRemoteGrant, Project)
                .join(Project, Project.id == ProjectRemoteGrant.project_id)
                .filter(
                    ProjectRemoteGrant.id == str(grant_id),
                    ProjectRemoteGrant.guest_id == int(guest_id),
                )
                .first()
            )
            handle = str(getattr(request.state, "project_remote_handle", "") or "")
            if invitation is None or not handle:
                raise HTTPException(404, "Invitation not found")
            _, invitation_project = invitation
            if _is_blocked(db, invitation_project.owner, handle):
                raise HTTPException(404, "Invitation not found")
            changed = (
                db.query(ProjectRemoteGrant)
                .filter(
                    ProjectRemoteGrant.id == str(grant_id),
                    ProjectRemoteGrant.guest_id == int(guest_id),
                    ProjectRemoteGrant.status == "pending",
                    ProjectRemoteGrant.version == int(body.version),
                )
                .update(
                    {
                        ProjectRemoteGrant.status: next_status,
                        ProjectRemoteGrant.responded_at: now,
                        ProjectRemoteGrant.version: ProjectRemoteGrant.version + 1,
                    },
                    synchronize_session=False,
                )
            )
            if changed != 1:
                visible = (
                    db.query(ProjectRemoteGrant.id)
                    .filter(
                        ProjectRemoteGrant.id == str(grant_id),
                        ProjectRemoteGrant.guest_id == int(guest_id),
                    )
                    .first()
                )
                db.rollback()
                if visible is None:
                    raise HTTPException(404, "Invitation not found")
                raise HTTPException(409, "Invitation changed; refresh and try again")
            db.commit()
            row = (
                db.query(ProjectRemoteGrant, Project)
                .join(Project, Project.id == ProjectRemoteGrant.project_id)
                .filter(
                    ProjectRemoteGrant.id == str(grant_id),
                    ProjectRemoteGrant.guest_id == int(guest_id),
                )
                .first()
            )
            if row is None:
                raise HTTPException(409, "Invitation changed; refresh and try again")
            grant, project = row
            return {"invitation": _project_invitation_dict(grant, project)}
        except HTTPException:
            db.rollback()
            raise
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router


def setup_link_hub_routes():
    router = APIRouter(prefix="/api/link", tags=["link"])

    _register_limiter = RateLimiter(max_requests=5, window_seconds=300)
    _redeem_limiter = RateLimiter(max_requests=10, window_seconds=300)
    _send_limiter = RateLimiter(max_requests=30, window_seconds=60)
    _fetch_limiter = RateLimiter(max_requests=240, window_seconds=60)
    _call_signal_limiter = RateLimiter(max_requests=120, window_seconds=10)
    _call_offer_limiter = RateLimiter(max_requests=8, window_seconds=60)

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
            _serialize_guest_admission(db)
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
                             status=GUEST_PENDING, created_at=utcnow_naive(),
                             scope=body.scope))
            db.commit()
        finally:
            db.close()
        logger.info("Home Link request: %s (pending approval)", gname)
        # The token is issued now (it's the guest's only credential) but stays
        # useless until approval. Deliberately no owner username / hub details.
        return {"ok": True, "handle": handle, "guest": gname,
                "status": GUEST_PENDING, "scope": body.scope, "token": token}

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
            _serialize_guest_admission(db)
            inv = _valid_invite(db, code)
            if not inv:
                # Generic on purpose: never reveal whether the code was wrong,
                # expired, revoked, or already spent.
                raise HTTPException(403, "Invalid or expired invite code")
            actual_scope = "project" if inv.project_id else "chat"
            if body.scope is not None and body.scope != actual_scope:
                # Generic on purpose: do not reveal valid codes from another
                # capability namespace or consume them through the wrong UI.
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
            db.add(LinkGuest(
                handle=handle,
                token_hash=_hash_token(token),
                status=GUEST_APPROVED,
                created_at=utcnow_naive(),
                invite_id=inv.id if inv else None,
                pubkey=pubkey,
                scope=actual_scope,
            ))
            db.commit()
        finally:
            db.close()
        logger.info("Home Link invite redeemed: %s (approved)", gname)
        return {"ok": True, "handle": handle, "guest": gname,
                "status": GUEST_APPROVED, "scope": actual_scope, "token": token}

    @router.post("/revoke")
    async def revoke_link(request: Request):
        """Invalidate the exact bearer identity and purge its retained data."""
        _require_hub()
        db = SessionLocal()
        try:
            guest = _guest_from_bearer(request, db)
            credential = _purge_guest_identity(db, guest)
            db.commit()
        finally:
            db.close()
        from routes import call_routes
        call_routes.revoke_federated_credential(credential)
        return {"ok": True}

    @router.get("/directory")
    async def directory(request: Request):
        """The one opaque installation identity this guest may contact."""
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
        """The installation conversation, without local profile disclosure."""
        _require_hub()
        if not _fetch_limiter.check(_client_ip(request)):
            raise HTTPException(429, "Too many requests — slow down")
        db = SessionLocal()
        try:
            g = _guest_from_bearer(request, db)
            _require_approved(g)
            gname = guest_username(g.handle)
            owner = _resolve_target(request, db, g, None)
            rows = (db.query(DirectMessage)
                    .filter(_pair_filter(gname, owner))
                    .order_by(DirectMessage.created_at.asc()).all())
            attachments = _attachments_by_message(db, [m.id for m in rows])
            conversation = None
            for m in rows:
                if conversation is None:
                    conversation = {
                        "username": INSTANCE_REMOTE_ALIAS,
                        "last_body": None,
                        "last_at": None,
                        "last_mine": False,
                        "unread": 0,
                    }
                conversation["last_body"] = (
                    (m.body or "") or _photo_preview(attachments.get(m.id))
                )
                conversation["last_at"] = (
                    m.created_at.isoformat() + "Z" if m.created_at else None
                )
                conversation["last_mine"] = m.sender == gname
                if m.recipient == gname and m.read_at is None:
                    conversation["unread"] += 1
            return {
                "conversations": [conversation] if conversation else [],
                "me": gname,
            }
        finally:
            db.close()

    @router.get("/messages")
    async def fetch_messages(
        request: Request,
        after_id: int = 0,
        to: Optional[str] = None,
        media_id: Optional[str] = None,
    ):
        """The guest's installation conversation.

        ``to`` is retained only for the opaque instance alias; local profile
        names are never bearer-routable. Fetching marks hidden owner-routed
        replies as read, mirroring local DM semantics.
        """
        _require_hub()
        if not _fetch_limiter.check(_client_ip(request)):
            raise HTTPException(429, "Too many requests — slow down")
        db = SessionLocal()
        try:
            g = _guest_from_bearer(request, db)
            _require_approved(g)
            gname = guest_username(g.handle)
            target = _resolve_target(request, db, g, to)
            if media_id is not None:
                if not PHOTO_ID_RE.fullmatch(media_id):
                    raise HTTPException(404, "Photo not found")
                photo = db.query(DirectMessageAttachment).filter(
                    DirectMessageAttachment.id == media_id
                ).first()
                parent = None
                if photo is not None:
                    parent = db.query(DirectMessage).filter(
                        DirectMessage.id == photo.message_id,
                        _pair_filter(gname, target),
                        DirectMessage.deleted_at.is_(None),
                    ).first()
                if photo is None or parent is None:
                    raise HTTPException(404, "Photo not found")
                g.last_seen = utcnow_naive()
                db.commit()
                return {
                    **_attachment_meta(photo),
                    "sha256": photo.sha256,
                    "data": photo.data_b64,
                }
            q = db.query(DirectMessage).filter(_pair_filter(gname, target))
            if after_id:
                q = q.filter(DirectMessage.id > after_id)
            msgs = (q.order_by(DirectMessage.created_at.desc())
                    .limit(MESSAGES_PAGE_LIMIT).all())
            msgs.reverse()
            attachments = _attachments_by_message(db, [m.id for m in msgs])
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
            return {
                "messages": [
                    _ser(
                        m,
                        gname,
                        attachments.get(m.id),
                        hide_local_profile=True,
                    )
                    for m in msgs
                ],
                "owner": INSTANCE_REMOTE_ALIAS,
                "me": gname,
            }
        finally:
            db.close()

    @router.post("/messages")
    async def send_message(body: LinkSendRequest, request: Request):
        _require_hub()
        if not _send_limiter.check(_client_ip(request)):
            raise HTTPException(429, "Too many requests — slow down")
        text = (body.body or "").strip()
        if len(text) > MAX_BODY_LEN:
            raise HTTPException(400, f"Message too long (max {MAX_BODY_LEN} characters)")
        if not text and not body.attachments:
            raise HTTPException(400, "A message or photo is required")
        db = SessionLocal()
        try:
            g = _guest_from_bearer(request, db)
            _require_approved(g)
            gname = guest_username(g.handle)
            target = _resolve_target(request, db, g, body.to)
            from routes.messaging_routes import (
                attach_prepared_photos,
                prepare_photo_attachments,
                publish_message_event,
            )
            prepared = await run_in_threadpool(prepare_photo_attachments, body.attachments)
            if any(item["size"] > MAX_FEDERATED_PHOTO_BYTES for item in prepared):
                raise HTTPException(
                    413,
                    "Photos sent between instances must be 2 MB or smaller",
                )
            msg = DirectMessage(sender=gname, recipient=target, body=text,
                                created_at=utcnow_naive(), read_at=None)
            g.last_seen = utcnow_naive()
            db.add(msg)
            db.flush()
            photo_rows = attach_prepared_photos(db, msg, prepared)
            db.commit()
            db.refresh(msg)
            # Fan out to the target's open SSE stream (routes/messaging_routes)
            # so hub-ingested guest messages arrive live too. Imported lazily —
            # messaging_routes imports this module at load time.
            publish_message_event(msg, attachments=photo_rows)
            return {
                "message": _ser(
                    msg,
                    gname,
                    photo_rows,
                    hide_local_profile=True,
                )
            }
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
            last_photos = (
                _attachments_by_message(db, [last.id]).get(last.id)
                if last else None
            )
            unread = (db.query(DirectMessage)
                      .filter(DirectMessage.sender == owner,
                              DirectMessage.recipient == gname,
                              DirectMessage.read_at.is_(None))
                      .count())
            return {
                "owner": INSTANCE_REMOTE_ALIAS,
                "unread": unread,
                "last_body": ((last.body or "") or _photo_preview(last_photos)) if last else None,
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
        me = _norm(require_user(request))
        if not is_hub_owner(request, me):
            raise HTTPException(403, "Only the Restia owner can manage connection requests")
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
        the handle and purges that identity's old conversation)."""
        _require_hub()
        require_admin(request)
        me = _norm(require_user(request))
        if not is_hub_owner(request, me):
            raise HTTPException(403, "Only the Restia owner can manage connection requests")
        action = _norm(body.action)
        if action not in ("approve", "block", "delete"):
            raise HTTPException(400, "action must be approve, block, or delete")
        db = SessionLocal()
        try:
            g = db.query(LinkGuest).filter(LinkGuest.handle == _norm(handle)).first()
            if not g:
                raise HTTPException(404, "No such guest")
            revoked_credential = None
            if action == "delete":
                revoked_credential = _purge_guest_identity(db, g)
            else:
                g.status = GUEST_APPROVED if action == "approve" else GUEST_BLOCKED
                if action == "block":
                    _revoke_guest_project_grants(db, g)
                    revoked_credential = str(g.token_hash)
            db.commit()
            if revoked_credential:
                from routes import call_routes
                call_routes.revoke_federated_credential(revoked_credential)
            logger.info("Home Link guest %s: %s", action, guest_username(_norm(handle)))
            return {"ok": True, "handle": _norm(handle),
                    "status": None if action == "delete" else g.status}
        finally:
            db.close()

    # ── Admin: invite codes ─────────────────────────────────────────────────

    @router.post("/admin/invites")
    async def admin_create_invite(body: InviteCreateRequest, request: Request):
        """Mint a one-off (or use-capped) invite code. The plaintext code is
        returned exactly once inside a portable Restia invitation — only its
        hash is stored, so it can't be recovered later; revoke and re-issue if
        it's lost."""
        _require_hub()
        require_admin(request)
        me = _norm(require_user(request)) or "admin"
        if not is_hub_owner(request, me):
            raise HTTPException(403, "Only the Restia owner can create chat invitations")
        hub_url = invitation_origin(request, body.hub_url)
        if body.hub_url and not hub_url:
            raise HTTPException(
                400,
                "Restia address must be an HTTPS origin (loopback HTTP is allowed for local testing)",
            )
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
                    "hub_url": hub_url,
                    "invitation": build_connection_invitation(hub_url, code),
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
    # Session-authenticated legacy preferences remain available, while only
    # the configured owner routes behind the external installation identity.

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
            if action == "block":
                guest_row = db.query(LinkGuest).filter(LinkGuest.handle == handle).first()
                if guest_row is not None:
                    _revoke_guest_project_grants(db, guest_row, project_owner=me)
            existing = db.query(RemoteBlock).filter(
                RemoteBlock.local_user == me, RemoteBlock.handle == handle).first()
            if action == "block" and not existing:
                db.add(RemoteBlock(local_user=me, handle=handle, created_at=utcnow_naive()))
            elif action == "unblock" and existing:
                db.delete(existing)
            db.commit()
            return {"ok": True, "handle": handle, "blocked": action == "block"}
        finally:
            db.close()

    @router.post("/calls/signal")
    async def guest_call_signal(body: LinkCallSignalRequest, request: Request):
        """Relay one profile-free signal from an approved guest to the owner."""
        _require_hub()
        from routes import call_routes
        call_routes._require_calls_enabled()
        db = SessionLocal()
        try:
            guest = _guest_from_bearer(request, db)
            owner = _owner_username(request)
            gname, owner = _require_call_pair(db, guest, owner)
            if not _call_signal_limiter.check(gname):
                raise HTTPException(429, "Too many signaling messages")
            call_id, kind, data = call_routes.validate_federated_signal(
                body.call_id, body.kind, body.data
            )
            if kind == "offer" and not _call_offer_limiter.check(gname):
                raise HTTPException(429, "Too many call attempts")
            if kind == "offer":
                call_routes.federated_calls.open(
                    call_id, gname, owner, str(guest.token_hash), "guest"
                )
                call_routes._begin_incoming_alert(
                    owner, gname, call_id, "local", data
                )
            else:
                call_routes.federated_calls.authorize(
                    call_id,
                    gname,
                    owner,
                    str(guest.token_hash),
                    "guest",
                    kind,
                )
                if kind in ("cancel", "hangup"):
                    call_routes._stop_incoming_alert(owner, gname, call_id)
            call_routes.call_bus.publish(
                owner,
                "call",
                {"from": gname, "call_id": call_id, "kind": kind, "data": data},
            )
            if kind in call_routes.CONTROL_SIGNAL_KINDS:
                call_routes.federated_calls.close(
                    call_id, gname, owner, str(guest.token_hash)
                )
            guest.last_seen = utcnow_naive()
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.get("/calls/stream")
    async def guest_call_stream(request: Request):
        """Hub -> guest call stream; events intentionally omit profile names."""
        _require_hub()
        from routes import call_routes
        call_routes._require_calls_enabled()
        db = SessionLocal()
        try:
            guest = _guest_from_bearer(request, db)
            owner = _owner_username(request)
            gname, _ = _require_call_pair(db, guest, owner)
            guest_identity = str(guest.token_hash)
        finally:
            db.close()
        async def gen():
            acquired = False
            q = None

            def queued_terminal():
                while True:
                    try:
                        queued_event, queued_data = q.get_nowait()
                    except asyncio.QueueEmpty:
                        return None
                    clean, terminal = _clean_remote_bus_call(queued_data)
                    if queued_event == "call" and terminal and clean is not None:
                        return clean

            try:
                call_routes.remote_stream_quota.acquire(gname)
                acquired = True
                q = call_routes.remote_call_bus.subscribe(gname)
                yield ": connected\n\n"
                while True:
                    try:
                        event, data = await asyncio.wait_for(
                            q.get(), timeout=call_routes.SSE_KEEPALIVE_S
                        )
                    except asyncio.TimeoutError:
                        if not _guest_call_pair_still_allowed(
                            request, gname, owner, guest_identity
                        ):
                            terminal = queued_terminal()
                            if terminal is not None:
                                yield ("event: call\n"
                                       f"data: {json.dumps(terminal, separators=(',', ':'))}\n\n")
                            return
                        yield ": ping\n\n"
                        continue
                    clean, server_terminal = _clean_remote_bus_call(data)
                    if event == "call" and server_terminal and clean is not None:
                        yield ("event: call\n"
                               f"data: {json.dumps(clean, separators=(',', ':'))}\n\n")
                        return
                    if not _guest_call_pair_still_allowed(
                        request, gname, owner, guest_identity
                    ):
                        terminal = queued_terminal()
                        if terminal is not None:
                            yield ("event: call\n"
                                   f"data: {json.dumps(terminal, separators=(',', ':'))}\n\n")
                        return
                    if event == "call" and clean is not None:
                        yield f"event: call\ndata: {json.dumps(clean, separators=(',', ':'))}\n\n"
            finally:
                if q is not None:
                    call_routes.remote_call_bus.unsubscribe(gname, q)
                if acquired:
                    call_routes.remote_stream_quota.release(gname)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return router


# ── Client side ─────────────────────────────────────────────────────────────

def home_server() -> str:
    return os.getenv("RESTIA_HOME_SERVER", DEFAULT_HOME_SERVER).strip().rstrip("/")


def home_enabled() -> bool:
    if home_server():
        return True
    db = SessionLocal()
    try:
        return (
            db.query(HomeLink.id).first() is not None
            or db.query(OutboundChatLink.id).first() is not None
        )
    finally:
        db.close()


def home_contact_name(base_url: Optional[str] = None) -> str:
    """The connected installation's contact name in the local Messages UI.

    It is labelled from its pinned stored origin, never from a later
    environment change.
    """
    selected = str(base_url or "").strip()
    if not selected:
        db = SessionLocal()
        try:
            # Reuse the fail-closed legacy migration path. Selecting the newest
            # row directly could silently strand another distinct bearer.
            try:
                link = _load_home_link(db, "")
            except HTTPException as exc:
                if exc.status_code != 409 or exc.detail != NOT_CONNECTED:
                    raise
                # Keep the configured contact visible as disconnected so the
                # user can run the serialized connect/replace flow that revokes
                # every legacy bearer (and offers explicit force on failure).
                link = None
            selected = str(link.home_url or "").strip() if link else home_server()
        finally:
            db.close()
    if not selected:
        return ""
    try:
        selected = _validated_home_base(selected)
    except HTTPException:
        return ""
    return _norm(urlparse(selected).netloc)


def is_home_contact(name: Optional[str]) -> bool:
    if is_outbound_chat_contact(name):
        return True
    contact = home_contact_name()
    return bool(contact) and _norm(name) == contact


def outbound_chat_contact(link_or_id: Any) -> str:
    value = getattr(link_or_id, "id", link_or_id)
    try:
        return OUTBOUND_CHAT_PREFIX + str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return ""


def is_outbound_chat_contact(name: Optional[str]) -> bool:
    value = _norm(name)
    if not value.startswith(OUTBOUND_CHAT_PREFIX):
        return False
    try:
        uuid.UUID(value[len(OUTBOUND_CHAT_PREFIX):])
        return True
    except (TypeError, ValueError, AttributeError):
        return False


def _outbound_chat_link(db, contact: str, owner: Optional[str] = None) -> Optional[OutboundChatLink]:
    if not is_outbound_chat_contact(contact):
        return None
    link_id = str(uuid.UUID(_norm(contact)[len(OUTBOUND_CHAT_PREFIX):]))
    query = db.query(OutboundChatLink).filter(OutboundChatLink.id == link_id)
    if owner is not None:
        query = query.filter(OutboundChatLink.owner == _norm(owner))
    return query.first()


def _require_outbound_chat_link(db, contact: str, owner: str) -> OutboundChatLink:
    link = _outbound_chat_link(db, contact, owner)
    if link is None:
        # Keep forged/stale opaque IDs indistinguishable from unknown users.
        raise HTTPException(404, "User not found")
    return link


def outbound_chat_contacts(owner: str) -> list[dict[str, Any]]:
    """All additive chat contacts visible to one local owner profile."""
    key = _norm(owner)
    if not key:
        return []
    db = SessionLocal()
    try:
        rows = (
            db.query(OutboundChatLink)
            .filter(OutboundChatLink.owner == key)
            .order_by(OutboundChatLink.created_at.asc(), OutboundChatLink.id.asc())
            .all()
        )
        return [{
            "username": outbound_chat_contact(row),
            "display": home_contact_name(row.home_url),
            "is_admin": False,
            "home": True,
            "chat_only": True,
            "can_call": False,
            "connected": True,
        } for row in rows]
    finally:
        db.close()


def _load_home_link(db, me: str) -> Optional[HomeLink]:
    rows = (
        db.query(HomeLink)
        .order_by(HomeLink.created_at.desc(), HomeLink.id.desc())
        .all()
    )
    if not rows:
        return None

    # Legacy releases stored one bearer per profile. Multiple rows may be
    # duplicate copies of the same credential, which are safe to consolidate.
    # Distinct credentials cannot be discarded synchronously: each must first
    # be revoked at its own pinned origin by connect/redeem/disconnect. Refuse
    # ordinary use instead of silently orphaning remote identities.
    fingerprints = set()
    try:
        for row in rows:
            fingerprints.add((
                _validated_home_base(str(row.home_url)),
                _hash_token(str(row.token)),
            ))
    except Exception as exc:
        logger.warning("Home Link legacy credentials could not be validated")
        raise HTTPException(409, NOT_CONNECTED) from exc
    if len(fingerprints) != 1:
        raise HTTPException(409, NOT_CONNECTED)

    shared = next(
        (row for row in rows if row.local_user == INSTANCE_LINK_USER),
        None,
    )
    winner = shared or rows[0]
    previous_user = _norm(winner.local_user)
    try:
        if not _norm(winner.owner):
            fallback_owner = next(
                (_norm(row.owner) for row in rows if _norm(row.owner)),
                next(
                    (
                        _norm(row.local_user)
                        for row in rows
                        if _norm(row.local_user)
                        and _norm(row.local_user) != INSTANCE_LINK_USER
                    ),
                    _norm(me) or previous_user,
                ),
            )
            winner.owner = (
                fallback_owner
                if fallback_owner and fallback_owner != INSTANCE_LINK_USER
                else None
            )
        winner.local_user = INSTANCE_LINK_USER
        for row in rows:
            if row.id != winner.id:
                db.delete(row)
        db.commit()
        db.refresh(winner)
        return winner
    except Exception:
        db.rollback()
        retry = db.query(HomeLink).filter(
            HomeLink.local_user == INSTANCE_LINK_USER
        ).first()
        leftovers = db.query(HomeLink.id).filter(
            HomeLink.local_user != INSTANCE_LINK_USER
        ).first()
        if retry is not None and leftovers is None:
            return retry
        logger.exception("Home Link legacy migration failed")
        raise HTTPException(409, NOT_CONNECTED)


def home_connected(me: str) -> bool:
    db = SessionLocal()
    try:
        try:
            return _load_home_link(db, me) is not None
        except HTTPException as exc:
            if exc.status_code == 409 and exc.detail == NOT_CONNECTED:
                return False
            raise
    finally:
        db.close()


def home_call_available(me: str) -> bool:
    """Only the profile that established the installation link may call home."""
    key = _norm(me)
    if not key or not home_enabled():
        return False
    db = SessionLocal()
    try:
        link = _load_home_link(db, key)
        return bool(link and _norm(link.owner) == key)
    except HTTPException:
        return False
    finally:
        db.close()


def _require_home_call_link(db, me: str) -> HomeLink:
    link = _require_link(db, me)
    if not _norm(link.owner) or _norm(link.owner) != _norm(me):
        raise HTTPException(403, "Only the Home Link owner can call this contact")
    return link


def _clean_attachment_meta(value) -> Optional[dict]:
    if not isinstance(value, dict):
        return None
    attachment_id = value.get("id")
    name = value.get("name")
    mime = value.get("mime")
    if not isinstance(attachment_id, str) or not PHOTO_ID_RE.fullmatch(attachment_id):
        return None
    if not isinstance(name, str) or not isinstance(mime, str) or mime not in PHOTO_MIMES:
        return None
    name = re.sub(r"[\x00-\x1f\x7f]+", "", os.path.basename(name))[:180] or "photo"
    try:
        size = int(value.get("size") or 0)
        width = int(value.get("width") or 0)
        height = int(value.get("height") or 0)
    except (TypeError, ValueError):
        return None
    if (
        size < 1 or size > MAX_FEDERATED_PHOTO_BYTES
        or width < 1 or height < 1
        or width * height > MAX_PHOTO_PIXELS
    ):
        return None
    return {
        "id": attachment_id,
        "name": name,
        "mime": mime,
        "size": size,
        "width": width,
        "height": height,
    }


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
    raw_attachments = m.get("attachments")
    attachments = []
    if isinstance(raw_attachments, list):
        for value in raw_attachments[:MAX_PHOTOS_PER_MESSAGE]:
            clean = _clean_attachment_meta(value)
            if clean:
                attachments.append(clean)
    clean_body = body[:MAX_BODY_LEN]
    if not clean_body and not attachments:
        return None
    created = m.get("created_at")
    return {
        "id": mid,
        "body": clean_body,
        "mine": bool(m.get("mine")),
        "created_at": created[:64] if isinstance(created, str) else None,
        "read": bool(m.get("read")),
        "attachments": attachments,
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


def _validated_home_base(base_url: Optional[str]) -> str:
    value = str(base_url or home_server()).strip()
    parsed = urlparse(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise HTTPException(502, "Home server URL is invalid")
    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        raise HTTPException(502, "Home server URL is invalid")
    loopback = host == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not loopback:
        raise HTTPException(502, "Home server must use HTTPS")
    # Return an origin, not caller-controlled path text. Normalizing default
    # ports also makes stored-origin comparisons deterministic.
    display_host = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme == "https" else 80
    port_part = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme}://{display_host}{port_part}"


def _requested_home_base(value: Optional[str]) -> str:
    """Validate a user-selected connection target with a useful 4xx error."""
    candidate = str(value or "").strip() or home_server()
    if not candidate:
        raise HTTPException(400, "Enter the HTTPS address of the other Restia")
    try:
        return _validated_home_base(candidate)
    except HTTPException as exc:
        if exc.status_code == 502:
            raise HTTPException(400, str(exc.detail)) from exc
        raise


def _raise_hub_response_error(
    status_code: int,
    content: bytes,
    *,
    allow_not_found: bool = False,
    safe_status_details: Optional[dict[int, str]] = None,
) -> None:
    """Map a bounded upstream error without exposing its URL or credential."""
    try:
        parsed = json.loads(content)
        detail = parsed.get("detail") if isinstance(parsed, dict) else None
    except Exception:
        detail = None
    # A rejected token means we're effectively not connected. Surface the
    # connect-card sentinel instead of 401: a raw 401 would trip the browser's
    # redirect-to-/login behavior.
    if status_code == 401:
        raise HTTPException(409, NOT_CONNECTED)
    if status_code == 403:
        if detail == PENDING:
            raise HTTPException(403, PENDING)
        raise HTTPException(
            403,
            detail
            if isinstance(detail, str) and len(detail) <= 300
            else "Home server refused the request",
        )
    safe_status_details = safe_status_details or {}
    if status_code in safe_status_details:
        fallback = str(safe_status_details[status_code])[:300]
        raise HTTPException(
            status_code,
            detail
            if isinstance(detail, str) and len(detail) <= 300
            else fallback,
        )
    if status_code in (400, 409, 429) or (allow_not_found and status_code == 404):
        raise HTTPException(
            status_code,
            detail
            if isinstance(detail, str) and len(detail) <= 300
            else "Home server refused the request",
        )
    raise HTTPException(502, f"Home server error ({status_code})")


async def _hub_call(method: str, path: str, *, token: Optional[str] = None,
                    json_body: Optional[dict] = None,
                    params: Optional[dict] = None,
                    base_url: Optional[str] = None,
                    max_response_bytes: int = MAX_HUB_RESPONSE_BYTES,
                    allow_not_found: bool = False,
                    indeterminate_mutation: bool = False) -> dict:
    """One HTTP round-trip to the home server, with errors mapped to local
    HTTP errors. Kept as a single seam so tests can monkeypatch it."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    target = _validated_home_base(base_url) + path
    try:
        response_limit = int(max_response_bytes)
    except (TypeError, ValueError):
        response_limit = MAX_HUB_RESPONSE_BYTES
    response_limit = max(1024, min(response_limit, MAX_HUB_RESPONSE_CEILING_BYTES))
    try:
        async with httpx.AsyncClient(
            timeout=10,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                method,
                target,
                json=json_body,
                params=params,
                headers=headers,
            ) as resp:
                content = bytearray()
                async for chunk in resp.aiter_bytes():
                    if len(content) + len(chunk) > response_limit:
                        if indeterminate_mutation:
                            raise HTTPException(
                                502,
                                PROJECT_MUTATION_INDETERMINATE_DETAIL,
                            )
                        raise HTTPException(502, "Home server response too large")
                    content.extend(chunk)
                status_code = resp.status_code
    except httpx.HTTPError as e:
        if indeterminate_mutation:
            raise HTTPException(
                502,
                PROJECT_MUTATION_INDETERMINATE_DETAIL,
            ) from e
        raise HTTPException(502, f"Home server unreachable ({e.__class__.__name__})")
    if status_code >= 400:
        _raise_hub_response_error(
            status_code,
            bytes(content),
            allow_not_found=allow_not_found,
        )
    try:
        data = json.loads(bytes(content))
    except Exception as exc:
        if indeterminate_mutation:
            raise HTTPException(
                502,
                PROJECT_MUTATION_INDETERMINATE_DETAIL,
            ) from exc
        raise HTTPException(502, "Home server returned malformed data")
    if not isinstance(data, dict):
        if indeterminate_mutation:
            raise HTTPException(
                502,
                PROJECT_MUTATION_INDETERMINATE_DETAIL,
            )
        return {}
    return data


def _require_link(db, me: str) -> HomeLink:
    link = _load_home_link(db, me)
    if not link:
        raise HTTPException(409, NOT_CONNECTED)
    return link


def _chat_link_snapshot(me: str, contact: Optional[str] = None) -> dict[str, Any]:
    """Pin one outbound chat credential without exposing its ORM row."""
    key = _norm(me)
    requested = _norm(contact)
    db = SessionLocal()
    try:
        if is_outbound_chat_contact(requested):
            link = _require_outbound_chat_link(db, requested, key)
            base_url = _validated_home_base(link.home_url)
            return {
                "identity": f"chat:{link.id}",
                "token": str(link.token),
                "base_url": base_url,
                "contact": outbound_chat_contact(link),
                "display": home_contact_name(base_url),
                "chat_only": True,
                "can_call": False,
            }

        link = _require_link(db, key)
        base_url = _validated_home_base(link.home_url)
        legacy_contact = home_contact_name(base_url)
        if requested and requested != legacy_contact:
            raise HTTPException(404, "User not found")
        return {
            "identity": f"home:{int(link.id)}",
            "token": str(link.token),
            "base_url": base_url,
            "contact": legacy_contact,
            "display": legacy_contact,
            "chat_only": False,
            "can_call": bool(_norm(link.owner) == key),
        }
    finally:
        db.close()


def _project_proxy_kind(method: str, remote_path: str) -> str:
    """Validate one relative Projects route and classify its transport."""
    normalized_method = str(method or "").upper()
    normalized_path = str(remote_path or "").strip("/")
    path_matched = False
    for pattern, methods in _PROJECT_PROXY_RULES:
        if pattern.fullmatch(normalized_path):
            path_matched = True
            if normalized_method not in methods:
                # Some route shapes intentionally have separate read and write
                # rules (for example GET and POST on ``/{project}/items``).
                # Keep looking before deciding the path rejects this method.
                continue
            if normalized_path.endswith("/download"):
                return "download"
            if normalized_path.endswith("/view"):
                return "preview"
            if normalized_path.endswith("/preview"):
                return "office_preview"
            if normalized_method == "POST" and normalized_path.endswith("/attachments"):
                return "upload"
            return "json"
    if path_matched:
        raise HTTPException(405, "Method not allowed")
    raise HTTPException(404, "Project route not found")


def _project_proxy_params(request: Request) -> dict[str, str]:
    params: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in _PROJECT_PROXY_QUERY_KEYS:
            raise HTTPException(400, f"Unsupported project query parameter: {key}")
        if key in params:
            raise HTTPException(400, f"Duplicate project query parameter: {key}")
        if len(value) > 300:
            raise HTTPException(400, f"Project query parameter is too long: {key}")
        params[key] = value
    return params


async def _project_proxy_json_body(request: Request) -> Optional[dict]:
    if request.method.upper() in ("GET", "DELETE"):
        return None
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > MAX_PROJECT_PROXY_JSON_BYTES:
                raise HTTPException(413, "Project request body is too large")
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length")
    body = await request.body()
    if len(body) > MAX_PROJECT_PROXY_JSON_BYTES:
        raise HTTPException(413, "Project request body is too large")
    if not body:
        return None
    try:
        value = json.loads(body)
    except Exception:
        raise HTTPException(400, "Project request body must be valid JSON")
    if not isinstance(value, dict):
        raise HTTPException(400, "Project request body must be an object")
    return value


async def _run_project_spool_io(function, *args):
    """Finish one worker-thread file operation before propagating cancellation."""
    operation = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        # Cancelling ``to_thread`` does not stop its worker. Waiting here keeps a
        # route-level finally from closing the spool under an in-flight read or
        # write, which can otherwise corrupt the stream or crash the worker.
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                # Repeated client/task cancellation must not cancel the wrapper
                # Task while its underlying thread is still using the spool.
                continue
            except BaseException:
                break
        if operation.done():
            try:
                operation.result()
            except BaseException:
                pass
        raise


async def _stage_project_proxy_upload(
    request: Request,
) -> tuple[BinaryIO, str, int]:
    """Bound and spool a raw multipart body before taking the lifecycle lock."""
    content_type = request.headers.get("content-type") or ""
    if not content_type.lower().startswith("multipart/form-data;"):
        raise HTTPException(415, "Project attachment upload must be multipart/form-data")
    declared = request.headers.get("content-length")
    if declared:
        try:
            declared_size = int(declared)
        except ValueError:
            raise HTTPException(400, "Invalid Content-Length")
        if declared_size < 0:
            raise HTTPException(400, "Invalid Content-Length")
        if declared_size > PROJECT_ATTACHMENT_REQUEST_MAX_BYTES:
            raise HTTPException(413, "Project attachment request body is too large")

    staged = tempfile.SpooledTemporaryFile(
        max_size=PROJECT_PROXY_UPLOAD_SPOOL_BYTES,
        mode="w+b",
    )
    received = 0
    try:
        async for chunk in request.stream():
            received += len(chunk)
            if received > PROJECT_ATTACHMENT_REQUEST_MAX_BYTES:
                raise HTTPException(413, "Project attachment request body is too large")
            if chunk:
                await _run_project_spool_io(staged.write, chunk)
        await _run_project_spool_io(staged.seek, 0)
        return staged, content_type, received
    except BaseException:
        staged.close()
        raise


async def _iter_staged_project_upload(staged: BinaryIO):
    while True:
        chunk = await _run_project_spool_io(
            staged.read,
            PROJECT_PROXY_UPLOAD_SPOOL_BYTES,
        )
        if not chunk:
            return
        yield chunk


def _require_home_project_link_snapshot(request: Request) -> dict[str, Any]:
    """Authorize a same-origin Projects proxy call and pin its credential."""
    if bool(getattr(request.state, "api_token", False)):
        raise HTTPException(403, "A signed-in browser session is required")
    me = _require_local_profile(request)
    is_admin = False
    try:
        require_admin(request)
        is_admin = True
    except HTTPException:
        pass
    db = SessionLocal()
    try:
        link = _require_link(db, me)
        owner = _norm(link.owner)
        if not is_admin and (not owner or owner != me):
            raise HTTPException(
                403,
                "Only an admin or the Home Link owner can access linked projects",
            )
        token = str(link.token)
        base_url = _validated_home_base(link.home_url)
        return {
            "link_identity": int(link.id),
            "owner": owner,
            "token": token,
            "token_fingerprint": _hash_token(token),
            "base_url": base_url,
        }
    finally:
        db.close()


def _assert_home_project_link_current(
    snapshot: dict[str, Any],
    *,
    mutation: bool = False,
) -> None:
    if not _home_call_link_still_current(
        snapshot["link_identity"],
        snapshot["owner"],
        snapshot["token_fingerprint"],
        snapshot["base_url"],
    ):
        detail = (
            "Home Link changed while the project operation was completing. "
            "It may have succeeded on the previous link; reload linked projects before retrying."
            if mutation
            else "Home Link changed; retry"
        )
        raise HTTPException(409, detail)


async def _read_bounded_upstream_response(
    response: httpx.Response,
    *,
    limit: int = MAX_HUB_RESPONSE_BYTES,
) -> bytes:
    content = bytearray()
    async for chunk in response.aiter_bytes():
        if len(content) + len(chunk) > limit:
            raise HTTPException(502, "Home server response too large")
        content.extend(chunk)
    return bytes(content)


async def _proxy_home_project_upload(
    staged_upload: tuple[BinaryIO, str, int],
    remote_path: str,
    snapshot: dict[str, Any],
) -> JSONResponse:
    """Stream one staged, bounded multipart request to the pinned hub."""
    staged, content_type, content_length = staged_upload
    headers = {
        "Authorization": f"Bearer {snapshot['token']}",
        "Content-Type": content_type,
        "Content-Length": str(content_length),
    }
    target = snapshot["base_url"] + "/api/link/projects/" + remote_path
    try:
        timeout = httpx.Timeout(connect=10, read=60, write=60, pool=10)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "POST",
                target,
                headers=headers,
                content=_iter_staged_project_upload(staged),
            ) as response:
                try:
                    content = await _read_bounded_upstream_response(response)
                except HTTPException as exc:
                    raise HTTPException(
                        502,
                        PROJECT_MUTATION_INDETERMINATE_DETAIL,
                    ) from exc
                status_code = response.status_code
    except httpx.HTTPError as exc:
        raise HTTPException(
            502,
            PROJECT_MUTATION_INDETERMINATE_DETAIL,
        ) from exc
    if status_code >= 400:
        _raise_hub_response_error(
            status_code,
            content,
            allow_not_found=True,
            safe_status_details=PROJECT_UPLOAD_UPSTREAM_ERROR_DETAILS,
        )
    try:
        value = json.loads(content)
    except Exception as exc:
        raise HTTPException(
            502,
            PROJECT_MUTATION_INDETERMINATE_DETAIL,
        ) from exc
    if not isinstance(value, dict):
        raise HTTPException(502, PROJECT_MUTATION_INDETERMINATE_DETAIL)
    _assert_home_project_link_current(snapshot, mutation=True)
    # Preserve the authoritative endpoint's creation status for API semantics
    # and observability. Restia's browser accepts every 2xx response, while
    # external consumers may still distinguish creation (201) from update.
    return JSONResponse(
        status_code=status_code,
        content=value,
        headers={"Cache-Control": "no-store"},
    )


def _validated_project_preview_range(value: object) -> Optional[str]:
    header = str(value or "").strip()
    if not header:
        return None
    if len(header) > 100 or "," in header:
        raise HTTPException(416, "Requested attachment range is not satisfiable")
    match = _PROJECT_PREVIEW_RANGE_RE.fullmatch(header)
    if not match:
        raise HTTPException(416, "Requested attachment range is not satisfiable")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise HTTPException(416, "Requested attachment range is not satisfiable")
    try:
        if start_text and end_text and int(end_text) < int(start_text):
            raise HTTPException(416, "Requested attachment range is not satisfiable")
        if not start_text and int(end_text) <= 0:
            raise HTTPException(416, "Requested attachment range is not satisfiable")
    except ValueError as exc:
        raise HTTPException(
            416, "Requested attachment range is not satisfiable"
        ) from exc
    return header


def _safe_project_download_headers(
    response: httpx.Response,
    *,
    inline: bool = False,
    range_requested: object = False,
) -> dict[str, str]:
    headers = {
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Referrer-Policy": "no-referrer",
    }
    content_type = response.headers.get("content-type")
    if content_type and len(content_type) <= 200:
        headers["Content-Type"] = content_type
    if inline:
        content_type_base = str(content_type or "").split(";", 1)[0].strip().lower()
        if content_type_base not in PROJECT_ATTACHMENT_PREVIEW_MIMES:
            raise HTTPException(502, "Home server returned an unsafe attachment preview type")
    content_length = response.headers.get("content-length")
    if not content_length:
        # The authoritative Restia endpoint is a FileResponse and always knows
        # the stored byte count. Refuse an unbounded/chunked upstream before
        # downstream headers are sent; silently truncating a 200 response at
        # the streaming limit would produce a corrupt deliverable.
        raise HTTPException(502, "Home server omitted attachment size")
    try:
        size = int(content_length)
    except ValueError:
        raise HTTPException(502, "Home server returned invalid attachment metadata")
    if size < 0 or size > PROJECT_ATTACHMENT_MAX_BYTES:
        raise HTTPException(502, "Home server returned an invalid attachment size")
    headers["Content-Length"] = str(size)
    status_code = int(getattr(response, "status_code", 200) or 200)
    if inline:
        requested_range_value = (
            str(range_requested)
            if isinstance(range_requested, str)
            else ""
        )
        has_requested_range = bool(range_requested)
        if status_code not in (200, 206):
            raise HTTPException(502, f"Home server error ({status_code})")
        if has_requested_range != (status_code == 206):
            raise HTTPException(502, "Home server returned inconsistent attachment range metadata")
        headers["Accept-Ranges"] = "bytes"
        if status_code == 206:
            content_range = str(response.headers.get("content-range") or "").strip()
            match = _PROJECT_CONTENT_RANGE_RE.fullmatch(content_range)
            if not match:
                raise HTTPException(502, "Home server returned invalid attachment range metadata")
            start, end, total = (int(value) for value in match.groups())
            if (
                total < 1
                or total > PROJECT_ATTACHMENT_MAX_BYTES
                or start < 0
                or end < start
                or end >= total
                or size != end - start + 1
            ):
                raise HTTPException(502, "Home server returned invalid attachment range metadata")
            if requested_range_value:
                requested = _PROJECT_PREVIEW_RANGE_RE.fullmatch(
                    requested_range_value
                )
                if requested is None:
                    raise HTTPException(502, "Home server returned inconsistent attachment range metadata")
                requested_start, requested_end = requested.groups()
                if requested_start:
                    expected_start = int(requested_start)
                    expected_end = (
                        min(int(requested_end), total - 1)
                        if requested_end
                        else total - 1
                    )
                else:
                    suffix = min(int(requested_end), total)
                    expected_start = total - suffix
                    expected_end = total - 1
                if start != expected_start or end != expected_end:
                    raise HTTPException(502, "Home server returned inconsistent attachment range metadata")
            headers["Content-Range"] = content_range
    disposition = response.headers.get("content-disposition") or ""
    encoded_match = re.search(r"filename\*=utf-8''([^;]+)", disposition, re.IGNORECASE)
    plain_match = re.search(r'filename="?([^";]+)', disposition, re.IGNORECASE)
    raw_name = encoded_match.group(1) if encoded_match else (
        plain_match.group(1) if plain_match else "attachment"
    )
    name = unquote(raw_name)
    name = re.sub(r"[\x00-\x1f\x7f]+", "", os.path.basename(name))[:180]
    name = name.replace('"', "_").replace("\\", "_") or "attachment"
    fallback = re.sub(r"[^A-Za-z0-9._ ()-]+", "_", name) or "attachment"
    headers["Content-Disposition"] = (
        f'{"inline" if inline else "attachment"}; filename="{fallback}"; '
        f"filename*=UTF-8''{quote(name, safe='')}"
    )
    return headers


async def _proxy_home_project_download(
    remote_path: str,
    snapshot: dict[str, Any],
    *,
    inline: bool = False,
    range_header: object = None,
) -> StreamingResponse:
    """Open a bounded, redirect-free attachment stream from the pinned hub."""
    safe_range = (
        _validated_project_preview_range(range_header)
        if inline
        else None
    )
    target = snapshot["base_url"] + "/api/link/projects/" + remote_path
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=60, write=60, pool=10),
        follow_redirects=False,
        trust_env=False,
    )
    response = None
    try:
        upstream_headers = {
            "Authorization": f"Bearer {snapshot['token']}",
            # httpx transparently decodes gzip/br by default.  The proxy
            # forwards the upstream Content-Length, so transformed bytes
            # would otherwise be truncated or rejected by the downstream
            # HTTP server.  Require an identity representation instead.
            "Accept-Encoding": "identity",
        }
        if safe_range is not None:
            upstream_headers["Range"] = safe_range
        upstream_request = client.build_request(
            "GET",
            target,
            headers=upstream_headers,
        )
        response = await client.send(upstream_request, stream=True)
        content_encoding = (response.headers.get("content-encoding") or "").strip().lower()
        if content_encoding and content_encoding != "identity":
            raise HTTPException(
                502,
                "Home server returned an encoded attachment unexpectedly",
            )
        if inline and response.status_code == 416:
            content_range = str(response.headers.get("content-range") or "").strip()
            match = _PROJECT_UNSATISFIED_RANGE_RE.fullmatch(content_range)
            if (
                not match
                or int(match.group(1)) < 1
                or int(match.group(1)) > PROJECT_ATTACHMENT_MAX_BYTES
            ):
                raise HTTPException(502, "Home server returned invalid attachment range metadata")
            await _read_bounded_upstream_response(response)
            await response.aclose()
            await client.aclose()
            raise HTTPException(
                416,
                "Requested attachment range is not satisfiable",
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": content_range,
                },
            )
        if response.status_code >= 400:
            content = await _read_bounded_upstream_response(response)
            await response.aclose()
            await client.aclose()
            _raise_hub_response_error(
                response.status_code,
                content,
                allow_not_found=True,
            )
        allowed_statuses = (200, 206) if inline else (200,)
        if response.status_code not in allowed_statuses:
            await response.aclose()
            await client.aclose()
            raise HTTPException(502, f"Home server error ({response.status_code})")
        headers = _safe_project_download_headers(
            response,
            inline=inline,
            range_requested=safe_range or False,
        )
        _assert_home_project_link_current(snapshot)
    except httpx.HTTPError as exc:
        if response is not None:
            await response.aclose()
        await client.aclose()
        raise HTTPException(
            502,
            f"Home server unreachable ({exc.__class__.__name__})",
        )
    except BaseException:
        if response is not None:
            await response.aclose()
        await client.aclose()
        raise

    async def body():
        received = 0
        try:
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > PROJECT_ATTACHMENT_MAX_BYTES:
                    logger.warning("Home Link project attachment exceeded its declared limit")
                    return
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        status_code=response.status_code,
        headers=headers,
        media_type=headers.get("Content-Type", "application/octet-stream"),
    )


# Conversation-list / badge polls hit each hub at most once per TTL.
_summary_cache: dict = {}


def _reset_summary_cache(me: Optional[str] = None, contact: Optional[str] = None):
    if me is None:
        _summary_cache.clear()
    else:
        owner = _norm(me)
        selected = _norm(contact)
        for key in list(_summary_cache):
            if key[0] == owner and (not selected or key[1] == selected):
                _summary_cache.pop(key, None)


async def _home_summary(me: str, contact: Optional[str] = None) -> Optional[dict]:
    """Cached hub summary for one contact, or None when not connected /
    pending / hub unreachable with nothing cached."""
    try:
        snapshot = _chat_link_snapshot(me, contact)
    except HTTPException:
        return None
    key = (_norm(me), snapshot["contact"])
    now = time.monotonic()
    entry = _summary_cache.get(key)
    if entry and now - entry["ts"] < SUMMARY_CACHE_TTL:
        return entry["data"]
    data = entry["data"] if entry else None
    try:
        data = _clean_summary(await _hub_call(
            "GET",
            "/api/link/summary",
            token=snapshot["token"],
            base_url=snapshot["base_url"],
        ))
    except HTTPException:
        # Pending/unreachable: keep serving the last good summary (or None);
        # retry after the TTL.
        pass
    _summary_cache[key] = {"ts": now, "data": data}
    return data


def _chat_contacts_for_owner(me: str) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    db = SessionLocal()
    try:
        try:
            link = _load_home_link(db, me)
        except HTTPException:
            link = None
        if link is not None:
            contact = home_contact_name(link.home_url)
            if contact:
                contacts.append({
                    "username": contact,
                    "display": contact,
                    "chat_only": False,
                    "can_call": bool(_norm(link.owner) == _norm(me)),
                })
    finally:
        db.close()
    contacts.extend(outbound_chat_contacts(me))
    return contacts


async def home_conversation_entries(me: str) -> list[dict]:
    """Conversation rows for every connected outbound Restia."""
    contacts = _chat_contacts_for_owner(me)
    if not contacts:
        return []
    semaphore = asyncio.Semaphore(4)

    async def build(meta: dict[str, Any]) -> dict:
        async with semaphore:
            summary = await _home_summary(me, meta["username"]) or {}
        contact = meta["username"]
        return {
            "username": contact,
            "display": meta.get("display") or contact,
            "is_admin": False,
            "home": True,
            "chat_only": bool(meta.get("chat_only")),
            "can_call": bool(meta.get("can_call")),
            "last_body": summary.get("last_body"),
            "last_sender": contact if not summary.get("last_mine") else None,
            "last_at": summary.get("last_at"),
            "last_mine": bool(summary.get("last_mine")),
            "unread": int(summary.get("unread") or 0),
        }

    return list(await asyncio.gather(*(build(meta) for meta in contacts)))


async def home_conversation_entry(me: str) -> Optional[dict]:
    """Backwards-compatible first outbound conversation row."""
    entries = await home_conversation_entries(me)
    return entries[0] if entries else None


async def home_unread_counts(me: str) -> dict[str, int]:
    entries = await home_conversation_entries(me)
    return {
        row["username"]: int(row.get("unread") or 0)
        for row in entries
        if int(row.get("unread") or 0) > 0
    }


async def home_unread(me: str) -> int:
    return sum((await home_unread_counts(me)).values())


async def home_get_conversation(me: str, after_id: int = 0,
                                contact: Optional[str] = None) -> dict:
    snapshot = _chat_link_snapshot(me, contact)
    data = await _hub_call(
        "GET",
        "/api/link/messages",
        token=snapshot["token"],
        params={"after_id": int(after_id)},
        base_url=snapshot["base_url"],
    )
    _reset_summary_cache(me, snapshot["contact"])  # fetch marked hub-side messages read
    raw = data.get("messages")
    messages = []
    if isinstance(raw, list):
        for m in raw[:MESSAGES_PAGE_LIMIT]:
            clean = _clean_message(m)
            if clean:
                messages.append(clean)
    return {
        "messages": messages,
        "other": {
            "username": snapshot["contact"],
            "display": snapshot["display"],
            "is_admin": False,
            "home": True,
            "chat_only": snapshot["chat_only"],
            "can_call": snapshot["can_call"],
        },
        "me": me,
    }


async def home_send_message(me: str, body: str, prepared=None,
                            contact: Optional[str] = None) -> dict:
    snapshot = _chat_link_snapshot(me, contact)
    attachments = []
    for item in list(prepared or [])[:MAX_PHOTOS_PER_MESSAGE]:
        if int(item.get("size") or 0) > MAX_FEDERATED_PHOTO_BYTES:
            raise HTTPException(
                413,
                "Photos sent between instances must be 2 MB or smaller",
            )
        encoded = str(item.get("data_b64") or "")
        if not encoded or len(encoded) > MAX_FEDERATED_PHOTO_DATA_CHARS:
            raise HTTPException(413, "Federated photo is too large")
        attachments.append({"name": str(item.get("filename") or "photo")[:240], "data": encoded})
    data = await _hub_call(
        "POST",
        "/api/link/messages",
        token=snapshot["token"],
        json_body={"body": body, "attachments": attachments},
        base_url=snapshot["base_url"],
    )
    _reset_summary_cache(me, snapshot["contact"])
    msg = _clean_message(data.get("message"))
    if not msg:
        raise HTTPException(502, "Home server returned malformed data")
    return {"message": msg}


async def home_get_media(me: str, attachment_id: str,
                         contact: Optional[str] = None) -> dict:
    if not PHOTO_ID_RE.fullmatch(str(attachment_id or "")):
        raise HTTPException(404, "Photo not found")
    snapshot = _chat_link_snapshot(me, contact)
    data = await _hub_call(
        "GET",
        "/api/link/messages",
        token=snapshot["token"],
        params={"media_id": attachment_id},
        base_url=snapshot["base_url"],
        max_response_bytes=MAX_HUB_MEDIA_RESPONSE_BYTES,
    )
    meta = _clean_attachment_meta(data)
    encoded = data.get("data") if isinstance(data, dict) else None
    digest = data.get("sha256") if isinstance(data, dict) else None
    if (
        not meta
        or not isinstance(encoded, str)
        or not encoded
        or len(encoded) > MAX_FEDERATED_PHOTO_DATA_CHARS
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise HTTPException(502, "Home server returned malformed photo data")
    return {**meta, "data": encoded, "sha256": digest}


def _pop_sse_frame(buffer: bytearray) -> Optional[bytes]:
    """Remove one LF or CRLF-delimited SSE frame from ``buffer``."""
    candidates = []
    for marker in (b"\n\n", b"\r\n\r\n"):
        idx = buffer.find(marker)
        if idx >= 0:
            candidates.append((idx, marker))
    if not candidates:
        return None
    idx, marker = min(candidates, key=lambda item: item[0])
    frame = bytes(buffer[:idx])
    del buffer[: idx + len(marker)]
    return frame


def _clean_upstream_call_frame(frame: bytes) -> Optional[dict]:
    if not frame or len(frame) > MAX_CALL_SSE_FRAME_BYTES:
        return None
    try:
        text = frame.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    event_name = "message"
    data_lines = []
    for raw_line in text.splitlines():
        if not raw_line or raw_line.startswith(":"):
            continue
        key, _, value = raw_line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if key == "event":
            event_name = value
        elif key == "data":
            data_lines.append(value)
    if event_name != "call" or not data_lines:
        return None
    encoded = "\n".join(data_lines)
    if len(encoded.encode("utf-8")) > MAX_CALL_SSE_FRAME_BYTES:
        return None
    try:
        value = json.loads(encoded)
    except Exception:
        return None
    from routes import call_routes
    return call_routes.clean_federated_event(value)


def _track_home_incoming_call(owner: str, contact: str, event: dict) -> None:
    """Drive local alert/replay state from one sanitized hub event."""
    from routes import call_routes

    kind = event.get("kind")
    call_id = event.get("call_id")
    if kind == "offer":
        call_routes._begin_incoming_alert(
            owner, contact, call_id, "home", event.get("data") or {}
        )
    elif kind in call_routes.CONTROL_SIGNAL_KINDS or kind == "answer":
        call_routes._stop_incoming_alert(
            owner, contact, call_id, transport="home"
        )


def _home_call_link_still_current(link_identity: int, owner: str,
                                  token_fingerprint: str,
                                  expected_base_url: str) -> bool:
    db = SessionLocal()
    try:
        link = db.query(HomeLink).filter(
            HomeLink.id == int(link_identity),
            HomeLink.local_user == INSTANCE_LINK_USER,
        ).first()
        if link is None or _norm(link.owner) != _norm(owner):
            return False
        try:
            current_base = _validated_home_base(link.home_url)
        except HTTPException:
            return False
        current_fingerprint = _hash_token(str(link.token))
        return (
            current_base == expected_base_url
            and secrets.compare_digest(current_fingerprint, token_fingerprint)
        )
    finally:
        db.close()


async def _proxy_home_call_stream(token: str, base_url: str, contact: str,
                                  link_identity: int, owner: str,
                                  token_fingerprint: str):
    """Bounded server-to-server SSE parser for the stored Home Link origin."""
    normalized_base = _validated_home_base(base_url)
    target = normalized_base + "/api/link/calls/stream"
    # The hub emits a keepalive every 15s. A bounded read timeout ensures a
    # stalled old connection also closes and is re-authorized on reconnect.
    timeout = httpx.Timeout(connect=10, read=35, write=10, pool=10)
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "GET",
                target,
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status_code != 200:
                    logger.warning("Home call stream refused with status %s", resp.status_code)
                    return
                content_type = (resp.headers.get("content-type") or "").lower()
                if "text/event-stream" not in content_type:
                    logger.warning("Home call stream returned a non-SSE response")
                    return
                buffer = bytearray()
                chunks = resp.aiter_bytes().__aiter__()
                stale_deadline = None
                loop = asyncio.get_running_loop()
                while True:
                    if not _home_call_link_still_current(
                        link_identity,
                        owner,
                        token_fingerprint,
                        normalized_base,
                    ) and stale_deadline is None:
                        # Revocation and the bearer-backed SSE use independent
                        # HTTP connections. Keep the old stream alive only for
                        # a short, bounded window so the hub's terminal hangup
                        # cannot be lost behind an already-buffered keepalive.
                        stale_deadline = loop.time() + HOME_LINK_TERMINAL_GRACE_S
                    timeout_s = None
                    if stale_deadline is not None:
                        timeout_s = stale_deadline - loop.time()
                        if timeout_s <= 0:
                            return
                    try:
                        next_chunk = chunks.__anext__()
                        chunk = (
                            await asyncio.wait_for(next_chunk, timeout=timeout_s)
                            if timeout_s is not None
                            else await next_chunk
                        )
                    except (StopAsyncIteration, asyncio.TimeoutError):
                        return
                    buffer.extend(chunk)
                    while True:
                        frame = _pop_sse_frame(buffer)
                        if frame is None:
                            break
                        # The hub subscribes its bearer-scoped queue before it
                        # emits this first comment. Exposing a private comment
                        # lets the browser proxy close the snapshot/live race
                        # without trusting any remote payload.
                        if frame.strip() == b": connected":
                            yield _HOME_CALL_UPSTREAM_READY
                            continue
                        clean = _clean_upstream_call_frame(frame)
                        current = _home_call_link_still_current(
                            link_identity, owner, token_fingerprint,
                            normalized_base,
                        )
                        if not current:
                            if stale_deadline is None:
                                stale_deadline = (
                                    loop.time() + HOME_LINK_TERMINAL_GRACE_S
                                )
                            # Forward exactly one safe terminal control event.
                            # No SDP/ICE/offer from a stale bearer may cross into
                            # the newly paired installation state.
                            if clean is not None and clean.get("kind") == "hangup":
                                _track_home_incoming_call(owner, contact, clean)
                                local = {"from": contact, **clean}
                                yield (
                                    "event: call\n"
                                    f"data: {json.dumps(local, separators=(',', ':'))}\n\n"
                                )
                                return
                            continue
                        if clean is not None:
                            # Reconstruct only the configured contact label;
                            # never trust a remote profile name.
                            _track_home_incoming_call(owner, contact, clean)
                            local = {"from": contact, **clean}
                            yield (
                                "event: call\n"
                                f"data: {json.dumps(local, separators=(',', ':'))}\n\n"
                            )
                    if len(buffer) > MAX_CALL_SSE_BUFFER_BYTES:
                        logger.warning("Home call stream exceeded the SSE frame bound")
                        return
    except httpx.HTTPError as exc:
        logger.warning("Home call stream disconnected: %s", exc.__class__.__name__)


def _home_call_watch_snapshot() -> Optional[dict[str, Any]]:
    """Load one immutable, validated Home Link watcher configuration."""
    global _home_call_watch_config_warned
    db = SessionLocal()
    try:
        link = (
            db.query(HomeLink)
            .filter(HomeLink.local_user == INSTANCE_LINK_USER)
            .order_by(HomeLink.created_at.desc(), HomeLink.id.desc())
            .first()
        )
        if link is None or not _norm(link.owner):
            _home_call_watch_config_warned = False
            return None
        token = str(link.token)
        base_url = _validated_home_base(link.home_url)
        snapshot = {
            "token": token,
            "base_url": base_url,
            "contact": home_contact_name(base_url),
            "link_identity": int(link.id),
            "owner": _norm(link.owner),
            "token_fingerprint": _hash_token(token),
        }
        _home_call_watch_config_warned = False
        return snapshot
    except Exception:
        # Encrypted credential/configuration failures stay fail-closed and the
        # exception text is omitted because it can retain secret material.
        if not _home_call_watch_config_warned:
            logger.warning("Home call alert watcher configuration is unavailable")
            _home_call_watch_config_warned = True
        return None
    finally:
        db.close()


async def home_call_alert_watcher() -> None:
    """Keep one bearer-scoped hub stream alive even when no browser is open.

    This is what lets the remote installation notify its own linked Telegram
    chats and retain the short-lived offer before the user follows the link.
    """
    backoff = 1.0
    previous_identity: Optional[tuple[str, int, str, str]] = None
    while True:
        snapshot = _home_call_watch_snapshot()
        if snapshot is None:
            if previous_identity is not None:
                from routes import call_routes
                call_routes.incoming_call_notifications.stop_owner_transport(
                    owner=previous_identity[0], transport="home"
                )
                previous_identity = None
            await asyncio.sleep(HOME_CALL_WATCH_IDLE_S)
            continue
        identity = (
            snapshot["owner"],
            snapshot["link_identity"],
            snapshot["token_fingerprint"],
            snapshot["base_url"],
        )
        if previous_identity is not None and previous_identity != identity:
            from routes import call_routes
            call_routes.incoming_call_notifications.stop_owner_transport(
                owner=previous_identity[0], transport="home"
            )
        previous_identity = identity
        try:
            async for _frame in _proxy_home_call_stream(**snapshot):
                # Validation, alert lifecycle, and pending-offer retention all
                # happen inside the shared parser before a frame is yielded.
                if _frame == _HOME_CALL_UPSTREAM_READY:
                    backoff = 1.0
            backoff = min(max(1.0, backoff * 2), HOME_CALL_WATCH_MAX_BACKOFF_S)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Home call alert watcher disconnected")
            backoff = min(max(1.0, backoff * 2), HOME_CALL_WATCH_MAX_BACKOFF_S)
        await asyncio.sleep(backoff)


def _require_local_profile(request: Request) -> str:
    me = _norm(require_user(request))
    if not me:
        raise HTTPException(403, "A signed-in profile is required")
    return me


async def _request_remote_revoke(token: str, base_url: str) -> None:
    try:
        result = await _hub_call(
            "POST",
            "/api/link/revoke",
            token=token,
            base_url=base_url,
        )
    except HTTPException as exc:
        # A rejected bearer means the remote identity is already gone.
        if exc.status_code == 409 and exc.detail == NOT_CONNECTED:
            return
        raise
    if result.get("ok") is not True:
        raise HTTPException(502, "Home server did not confirm revocation")


async def _revoke_stored_home_link(me: str, *, force: bool) -> bool:
    """Revoke the pinned remote credential, then remove its local copy.

    Network or protocol failure leaves the credential in place unless the
    caller explicitly chose the destructive local-only escape hatch.
    """
    db = SessionLocal()
    try:
        rows = db.query(HomeLink).order_by(HomeLink.id.asc()).all()
        if not rows:
            return False
        snapshots = []
        for link in rows:
            try:
                token = str(link.token)
                token_hash = _hash_token(token)
            except Exception as exc:
                if not force:
                    raise HTTPException(409, REVOKE_REQUIRED) from exc
                token = None
                token_hash = f"unreadable:{int(link.id)}"
                logger.warning(
                    "Forcing local removal of an unreadable Home Link credential"
                )
            snapshots.append({
                "id": int(link.id),
                "local_user": str(link.local_user),
                "handle": str(link.handle),
                "owner": str(link.owner or ""),
                "token": token,
                "token_hash": token_hash,
                "base_url": str(link.home_url),
            })
    finally:
        db.close()

    # Legacy installations can contain multiple profile-scoped credentials.
    # Revoke every distinct pinned origin+bearer before deleting any local row.
    attempted = set()
    for snapshot in snapshots:
        if snapshot["token"] is None:
            continue
        remote_key = (snapshot["base_url"], snapshot["token_hash"])
        if remote_key in attempted:
            continue
        attempted.add(remote_key)
        try:
            await _request_remote_revoke(
                snapshot["token"], snapshot["base_url"]
            )
        except HTTPException:
            if not force:
                raise HTTPException(409, REVOKE_REQUIRED)
            logger.warning("Forcing local Home Link removal after revoke failure")

    def fingerprints(values):
        return sorted((
            int(value["id"]),
            value["local_user"],
            value["handle"],
            value["owner"],
            value["base_url"],
            value["token_hash"],
        ) for value in values)

    db = SessionLocal()
    try:
        current = db.query(HomeLink).order_by(HomeLink.id.asc()).all()
        current_snapshots = []
        for link in current:
            try:
                token_hash = _hash_token(str(link.token))
            except Exception:
                token_hash = f"unreadable:{int(link.id)}"
            current_snapshots.append({
                "id": int(link.id),
                "local_user": str(link.local_user),
                "handle": str(link.handle),
                "owner": str(link.owner or ""),
                "base_url": str(link.home_url),
                "token_hash": token_hash,
            })
        if fingerprints(current_snapshots) != fingerprints(snapshots):
            raise HTTPException(409, "Home Link changed; retry")
        db.query(HomeLink).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    _reset_summary_cache()
    return True


async def _persist_new_home_link(*, token: str, base_url: str, handle: str,
                                 owner: str) -> None:
    """Persist one issued credential or revoke it if local ownership is lost."""
    error = None
    db = SessionLocal()
    try:
        # A row appearing after the serialized revoke indicates another process
        # changed the installation. Never delete it to make ours win.
        if db.query(HomeLink.id).first() is not None:
            raise HTTPException(409, "Home Link changed; retry")
        db.add(HomeLink(
            local_user=INSTANCE_LINK_USER,
            home_url=base_url,
            handle=handle,
            owner=owner,
            token=token,
            created_at=utcnow_naive(),
        ))
        db.commit()
        return
    except BaseException as exc:
        error = exc
        db.rollback()
    finally:
        db.close()

    cleanup_task = asyncio.create_task(_request_remote_revoke(token, base_url))
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        # The remote identity already exists, so request cancellation is not a
        # reason to abandon cleanup. Wait for the shielded revoke before
        # propagating the original persistence/cancellation failure.
        try:
            await cleanup_task
        except BaseException as cleanup_exc:
            logger.exception("Failed to revoke a newly issued Home Link credential")
            raise HTTPException(
                502,
                "Home Link changed locally and the new remote credential could not be revoked",
            ) from cleanup_exc
    except BaseException as cleanup_exc:
        logger.exception("Failed to revoke a newly issued Home Link credential")
        raise HTTPException(
            502,
            "Home Link changed locally and the new remote credential could not be revoked",
        ) from cleanup_exc
    raise error


def _ensure_outbound_chat_origin_available(base_url: str) -> None:
    db = SessionLocal()
    try:
        peer_exists = db.query(OutboundChatLink.id).filter(
            OutboundChatLink.home_url == base_url
        ).first() is not None
        primary_exists = db.query(HomeLink.id).filter(
            HomeLink.home_url == base_url
        ).first() is not None
        if peer_exists or primary_exists:
            raise HTTPException(409, "This Restia installation is already connected")
    finally:
        db.close()


def _ensure_no_outbound_chat_for_primary(base_url: str) -> None:
    db = SessionLocal()
    try:
        if db.query(OutboundChatLink.id).filter(
            OutboundChatLink.home_url == base_url
        ).first() is not None:
            raise HTTPException(
                409,
                "Disconnect the existing Messages-only link to this Restia before making it the primary Home Link",
            )
    finally:
        db.close()


def _acquire_chat_owner_identity(request: Request, owner: str):
    """Serialize final chat-link persistence with profile identity changes.

    A remote register/redeem call can outlive the profile that started it. The
    auth configuration lock closes that race: deletion either observes the
    committed link and stops, or reserves/retires the identity first and this
    write fails so its newly issued remote credential is revoked.
    """
    manager = getattr(getattr(request, "app", None), "state", None)
    manager = getattr(manager, "auth_manager", None)
    lock = getattr(manager, "_config_lock", None)
    if lock is not None:
        lock.acquire()
    try:
        key = _norm(owner)
        active = set(getattr(manager, "_identity_migrations", set()) or set())
        if key in {_norm(value) for value in active}:
            raise HTTPException(409, "Profile identity changed; retry")
        auth_enabled = os.getenv("AUTH_ENABLED", "true").lower() != "false"
        configured = bool(getattr(manager, "is_configured", False))
        users = getattr(manager, "users", {}) or {}
        known = {_norm(value) for value in users.keys()}
        if auth_enabled and configured and key not in known:
            raise HTTPException(409, "Profile identity changed; retry")
        return lock
    except BaseException:
        if lock is not None:
            lock.release()
        raise


async def _persist_new_outbound_chat_link(*, token: str, base_url: str,
                                          handle: str, owner: str,
                                          request: Request) -> OutboundChatLink:
    """Persist one additive chat credential or revoke the remote orphan."""
    link_id = str(uuid.uuid4())
    error: Optional[BaseException] = None
    identity_lock = None
    db = SessionLocal()
    try:
        identity_lock = _acquire_chat_owner_identity(request, owner)
        if db.query(OutboundChatLink.id).filter(
            OutboundChatLink.home_url == base_url
        ).first() is not None:
            raise HTTPException(409, "This Restia installation is already connected")
        db.add(OutboundChatLink(
            id=link_id,
            home_url=base_url,
            handle=handle,
            owner=_norm(owner),
            token=token,
            created_at=utcnow_naive(),
        ))
        db.commit()
        row = db.query(OutboundChatLink).filter(OutboundChatLink.id == link_id).one()
        db.expunge(row)
        return row
    except BaseException as exc:
        error = exc
        db.rollback()
    finally:
        db.close()
        if identity_lock is not None:
            identity_lock.release()

    cleanup_task = asyncio.create_task(_request_remote_revoke(token, base_url))
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        try:
            await cleanup_task
        except BaseException as cleanup_exc:
            raise HTTPException(
                502,
                "Restia connection changed locally and the new remote credential could not be revoked",
            ) from cleanup_exc
    except BaseException as cleanup_exc:
        raise HTTPException(
            502,
            "Restia connection changed locally and the new remote credential could not be revoked",
        ) from cleanup_exc
    assert error is not None
    raise error


async def _disconnect_outbound_chat(contact: str, owner: str, *, force: bool) -> bool:
    db = SessionLocal()
    try:
        link = _require_outbound_chat_link(db, contact, owner)
        snapshot = {
            "id": str(link.id),
            "home_url": str(link.home_url),
            "token": str(link.token),
            "token_hash": _hash_token(str(link.token)),
        }
    finally:
        db.close()
    try:
        await _request_remote_revoke(snapshot["token"], snapshot["home_url"])
    except HTTPException:
        if not force:
            raise HTTPException(409, REVOKE_REQUIRED)
        logger.warning("Forcing one local chat-link removal after revoke failure")

    db = SessionLocal()
    try:
        current = db.query(OutboundChatLink).filter(
            OutboundChatLink.id == snapshot["id"],
            OutboundChatLink.owner == _norm(owner),
        ).first()
        if current is None:
            return False
        if (
            str(current.home_url) != snapshot["home_url"]
            or not secrets.compare_digest(
                _hash_token(str(current.token)), snapshot["token_hash"]
            )
        ):
            raise HTTPException(409, "Restia connection changed; retry")
        db.delete(current)
        db.commit()
    finally:
        db.close()
    _reset_summary_cache(owner, contact)
    return True


def setup_home_link_routes():
    router = APIRouter(prefix="/api/homelink", tags=["homelink"])

    @router.get("/status")
    async def status(request: Request):
        me = _require_local_profile(request)
        db = SessionLocal()
        try:
            link = _load_home_link(db, me)
            pinned_home = link.home_url if link else home_server()
            return {
                "enabled": home_enabled(),
                "home": pinned_home,
                "contact": home_contact_name(pinned_home),
                "connected": link is not None,
                "handle": link.handle if link else None,
                "owner": link.owner if link else None,
                "invite_origin": invitation_origin(request),
                "chat_contacts": outbound_chat_contacts(me),
            }
        finally:
            db.close()

    @router.post("/chat/connect")
    @_serialized_home_link_lifecycle
    async def connect_chat(body: ConnectRequest, request: Request):
        """Request approval from another Restia without replacing other chats."""
        me = _require_local_profile(request)
        require_admin(request)
        handle = _norm(body.handle)
        if not HANDLE_RE.match(handle):
            raise HTTPException(
                400, "Handle must be 1-32 chars: lowercase letters, digits, . _ -")
        selected_home = _requested_home_base(body.home_url)
        _ensure_outbound_chat_origin_available(selected_home)
        data = await _hub_call(
            "POST",
            "/api/link/register",
            json_body={"handle": handle, "scope": "chat"},
            base_url=selected_home,
        )
        token = data.get("token")
        if not isinstance(token, str) or not (20 <= len(token) <= 128):
            if isinstance(token, str) and token:
                try:
                    await asyncio.shield(_request_remote_revoke(token, selected_home))
                except BaseException:
                    logger.exception("Malformed chat-link credential could not be revoked")
            raise HTTPException(502, "Restia returned malformed connection data")
        linked_handle = _norm(data.get("handle")) if isinstance(data.get("handle"), str) else handle
        if not HANDLE_RE.match(linked_handle):
            linked_handle = handle
        link = await _persist_new_outbound_chat_link(
            token=token,
            base_url=selected_home,
            handle=linked_handle,
            owner=me,
            request=request,
        )
        _reset_summary_cache(me)
        return {
            "ok": True,
            "handle": linked_handle,
            "status": data.get("status") or GUEST_PENDING,
            "contact": outbound_chat_contact(link),
            "display": home_contact_name(selected_home),
            "chat_only": True,
        }

    @router.post("/chat/redeem")
    @_serialized_home_link_lifecycle
    async def redeem_chat(body: RedeemHomeRequest, request: Request):
        """Accept one chat invitation additively, preserving every other peer."""
        me = _require_local_profile(request)
        require_admin(request)
        handle = _norm(body.handle)
        if not HANDLE_RE.match(handle):
            raise HTTPException(
                400, "Handle must be 1-32 chars: lowercase letters, digits, . _ -")
        code = str(body.code or "").strip()
        if not code:
            raise HTTPException(400, "Invite code is required")
        selected_home = _requested_home_base(body.home_url)
        _ensure_outbound_chat_origin_available(selected_home)
        data = await _hub_call(
            "POST",
            "/api/link/redeem",
            json_body={"handle": handle, "code": code, "scope": "chat"},
            base_url=selected_home,
        )
        token = data.get("token")
        if not isinstance(token, str) or not (20 <= len(token) <= 128):
            if isinstance(token, str) and token:
                try:
                    await asyncio.shield(_request_remote_revoke(token, selected_home))
                except BaseException:
                    logger.exception("Malformed chat-link credential could not be revoked")
            raise HTTPException(502, "Restia returned malformed connection data")
        if data.get("scope") != "chat":
            try:
                await asyncio.shield(_request_remote_revoke(token, selected_home))
            except BaseException as cleanup_exc:
                raise HTTPException(
                    502,
                    "Restia returned the wrong invitation scope and the credential could not be revoked",
                ) from cleanup_exc
            raise HTTPException(400, "This invitation is not for Messages")
        linked_handle = _norm(data.get("handle")) if isinstance(data.get("handle"), str) else handle
        if not HANDLE_RE.match(linked_handle):
            linked_handle = handle
        link = await _persist_new_outbound_chat_link(
            token=token,
            base_url=selected_home,
            handle=linked_handle,
            owner=me,
            request=request,
        )
        _reset_summary_cache(me)
        return {
            "ok": True,
            "handle": linked_handle,
            "status": data.get("status") or GUEST_APPROVED,
            "contact": outbound_chat_contact(link),
            "display": home_contact_name(selected_home),
            "chat_only": True,
        }

    @router.post("/chat/{contact_id}/disconnect")
    @_serialized_home_link_lifecycle
    async def disconnect_chat(contact_id: str, request: Request,
                              body: Optional[DisconnectHomeRequest] = None):
        me = _require_local_profile(request)
        require_admin(request)
        contact = outbound_chat_contact(contact_id)
        if not contact:
            raise HTTPException(404, "User not found")
        removed = await _disconnect_outbound_chat(
            contact,
            me,
            force=bool(body and body.force_local),
        )
        return {"ok": True, "removed": removed, "contact": contact}

    @router.post("/connect")
    @_serialized_home_link_lifecycle
    async def connect(body: ConnectRequest, request: Request):
        """Register this installation with another Restia under a handle. The
        registration starts pending until the hub owner approves it."""
        me = _require_local_profile(request)
        require_admin(request)
        handle = _norm(body.handle)
        if not HANDLE_RE.match(handle):
            raise HTTPException(
                400, "Handle must be 1-32 chars: lowercase letters, digits, . _ -")
        selected_home = _requested_home_base(body.home_url)
        _ensure_no_outbound_chat_for_primary(selected_home)
        await _revoke_stored_home_link(me, force=bool(body.force_replace))
        data = await _hub_call(
            "POST",
            "/api/link/register",
            json_body={"handle": handle},
            base_url=selected_home,
        )
        token = data.get("token")
        if not isinstance(token, str) or not (20 <= len(token) <= 128):
            if isinstance(token, str) and token:
                try:
                    await asyncio.shield(
                        _request_remote_revoke(token, selected_home)
                    )
                except BaseException:
                    logger.exception("Malformed Home Link credential could not be revoked")
            raise HTTPException(502, "Home server returned malformed data")
        linked_handle = _norm(data.get("handle")) if isinstance(data.get("handle"), str) else handle
        if not HANDLE_RE.match(linked_handle):
            linked_handle = handle
        await _persist_new_home_link(
            token=token,
            base_url=selected_home,
            handle=linked_handle,
            owner=me,
        )
        _reset_summary_cache()
        return {"ok": True, "handle": linked_handle,
                "status": data.get("status") or GUEST_PENDING,
                "contact": home_contact_name(selected_home)}

    @router.post("/redeem")
    @_serialized_home_link_lifecycle
    async def redeem_home(body: RedeemHomeRequest, request: Request):
        """Accept an invitation from another Restia.

        Unlike /connect (register then wait for the owner to approve), a
        redeemed code is approved on the spot, so the conversation is usable
        immediately.
        """
        me = _require_local_profile(request)
        require_admin(request)
        handle = _norm(body.handle)
        if not HANDLE_RE.match(handle):
            raise HTTPException(
                400, "Handle must be 1-32 chars: lowercase letters, digits, . _ -")
        code = (body.code or "").strip()
        if not code:
            raise HTTPException(400, "Invite code is required")
        selected_home = _requested_home_base(body.home_url)
        _ensure_no_outbound_chat_for_primary(selected_home)
        await _revoke_stored_home_link(me, force=bool(body.force_replace))
        data = await _hub_call(
            "POST",
            "/api/link/redeem",
            json_body={"handle": handle, "code": code, "scope": "project"},
            base_url=selected_home,
        )
        token = data.get("token")
        if not isinstance(token, str) or not (20 <= len(token) <= 128):
            if isinstance(token, str) and token:
                try:
                    await asyncio.shield(
                        _request_remote_revoke(token, selected_home)
                    )
                except BaseException:
                    logger.exception("Malformed Home Link credential could not be revoked")
            raise HTTPException(502, "Home server returned malformed data")
        if data.get("scope") != "project":
            try:
                await asyncio.shield(_request_remote_revoke(token, selected_home))
            except BaseException as cleanup_exc:
                raise HTTPException(
                    502,
                    "Restia returned the wrong invitation scope and the credential could not be revoked",
                ) from cleanup_exc
            raise HTTPException(
                400,
                "This invitation is for Messages; accept it from New message instead",
            )
        linked_handle = _norm(data.get("handle")) if isinstance(data.get("handle"), str) else handle
        if not HANDLE_RE.match(linked_handle):
            linked_handle = handle
        await _persist_new_home_link(
            token=token,
            base_url=selected_home,
            handle=linked_handle,
            owner=me,
        )
        _reset_summary_cache()
        return {"ok": True, "handle": linked_handle,
                "status": data.get("status") or GUEST_APPROVED,
                "contact": home_contact_name(selected_home)}

    @router.post("/disconnect")
    @_serialized_home_link_lifecycle
    async def disconnect(request: Request,
                         body: Optional[DisconnectHomeRequest] = None):
        """Revoke the remote identity and remove its stored local credential."""
        me = _require_local_profile(request)
        db = SessionLocal()
        try:
            # Do not run lazy migration here: a multi-credential legacy state
            # must remain disconnectable so every bearer can be revoked (or
            # removed only through the explicit force_local escape hatch).
            link = (
                db.query(HomeLink)
                .order_by(
                    (HomeLink.local_user == INSTANCE_LINK_USER).desc(),
                    HomeLink.created_at.desc(),
                    HomeLink.id.desc(),
                )
                .first()
            )
            is_admin = False
            try:
                require_admin(request)
                is_admin = True
            except HTTPException:
                pass
            effective_owner = _norm(link.owner) if link else ""
            if link and not effective_owner and link.local_user != INSTANCE_LINK_USER:
                effective_owner = _norm(link.local_user)
            if link and not is_admin and effective_owner != me:
                raise HTTPException(403, "Only an admin or the Home Link owner can disconnect")
        finally:
            db.close()
        await _revoke_stored_home_link(
            me, force=bool(body and body.force_local)
        )
        from routes import call_routes
        call_routes.incoming_call_notifications.stop_owner_transport(
            owner=effective_owner or me, transport="home"
        )
        return {"ok": True}

    @router.post("/calls/signal")
    async def home_call_signal(body: LinkCallSignalRequest, request: Request):
        """Same-origin proxy for one owner profile's signal to the hub."""
        me = _require_local_profile(request)
        from routes import call_routes
        call_routes._require_calls_enabled()
        db = SessionLocal()
        try:
            link = _require_home_call_link(db, me)
            token = link.token
            base_url = link.home_url
        finally:
            db.close()
        # Validate locally before an outbound request, then let the hub repeat
        # validation and bind it to the bearer-derived guest.
        call_id, kind, data = call_routes.validate_federated_signal(
            body.call_id, body.kind, body.data
        )
        if kind in ("answer", "decline", "busy", "hangup"):
            call_routes._stop_incoming_alert(
                me, home_contact_name(base_url), call_id, transport="home"
            )
        return await _hub_call(
            "POST",
            "/api/link/calls/signal",
            token=token,
            json_body={"call_id": call_id, "kind": kind, "data": data},
            base_url=base_url,
        )

    @router.get("/calls/stream")
    async def home_call_stream(request: Request):
        """Same-origin, owner-only proxy for the hub's bounded call SSE."""
        me = _require_local_profile(request)
        from routes import call_routes
        call_routes._require_calls_enabled()
        db = SessionLocal()
        try:
            link = _require_home_call_link(db, me)
            token = link.token
            base_url = link.home_url
            link_identity = int(link.id)
            link_owner = _norm(link.owner)
            token_fingerprint = _hash_token(str(token))
        finally:
            db.close()
        contact = home_contact_name(base_url)

        async def gen():
            acquired = False
            try:
                call_routes.home_stream_quota.acquire(me)
                acquired = True
                replayed: set[str] = set()
                pending = call_routes.incoming_call_notifications.pending_snapshot(
                    owner=me, transport="home"
                )
                yield ": connected\n\n"
                for data in pending:
                    replayed.add(data["call_id"])
                    yield (
                        "event: call\n"
                        f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
                    )
                async for frame in _proxy_home_call_stream(
                    token,
                    base_url,
                    contact,
                    link_identity,
                    link_owner,
                    token_fingerprint,
                ):
                    if frame == _HOME_CALL_UPSTREAM_READY:
                        # The remote queue is now subscribed. Replay anything
                        # captured by the background watcher during setup; new
                        # signals after this point arrive on the live stream.
                        for data in call_routes.incoming_call_notifications.pending_snapshot(
                            owner=me, transport="home"
                        ):
                            if data["call_id"] in replayed:
                                continue
                            replayed.add(data["call_id"])
                            yield (
                                "event: call\n"
                                f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
                            )
                        yield frame
                        continue
                    yield frame
            finally:
                if acquired:
                    call_routes.home_stream_quota.release(me)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def proxy_projects(request: Request, remote_path: str):
        normalized_path = str(remote_path or "").strip("/")
        kind = _project_proxy_kind(request.method, normalized_path)
        params = _project_proxy_params(request)
        if kind != "json" and params:
            raise HTTPException(400, "This project route does not accept query parameters")
        is_mutation = request.method.upper() != "GET"
        has_body_intake = kind == "upload" or (
            kind == "json" and request.method.upper() not in ("GET", "DELETE")
        )
        # Authenticate and pin the intended link before accepting a potentially
        # large or slow body. The same snapshot is revalidated under the lock,
        # so a reconnect during staging cannot reroute the mutation to a new hub.
        staged_snapshot = (
            _require_home_project_link_snapshot(request)
            if has_body_intake
            else None
        )
        # Client body intake must never hold the installation-wide lifecycle
        # lock. A slow or disconnected browser would otherwise prevent even an
        # emergency Home Link disconnect. Uploads retain the same hard ASGI
        # limit and are additionally bounded here before being spooled.
        body = await _project_proxy_json_body(request) if kind == "json" else None
        staged_upload = (
            await _stage_project_proxy_upload(request)
            if kind == "upload"
            else None
        )

        async def execute_against_current_link():
            snapshot = staged_snapshot or _require_home_project_link_snapshot(request)
            if staged_snapshot is not None:
                # Nothing has been sent yet, so this is a definite stale-link
                # failure rather than an indeterminate mutation result.
                _assert_home_project_link_current(snapshot)
            if kind == "upload":
                assert staged_upload is not None
                return await _proxy_home_project_upload(
                    staged_upload,
                    normalized_path,
                    snapshot,
                )
            if kind == "download":
                return await _proxy_home_project_download(normalized_path, snapshot)
            if kind == "preview":
                return await _proxy_home_project_download(
                    normalized_path,
                    snapshot,
                    inline=True,
                    range_header=request.headers.get("range"),
                )
            remote_api_path = "/api/link/projects"
            if normalized_path:
                remote_api_path += "/" + normalized_path
            data = await _hub_call(
                request.method.upper(),
                remote_api_path,
                token=snapshot["token"],
                json_body=body,
                params=params or None,
                base_url=snapshot["base_url"],
                max_response_bytes=(
                    OFFICE_PREVIEW_MAX_RESPONSE_BYTES
                    if kind == "office_preview"
                    else MAX_PROJECT_PROXY_JSON_RESPONSE_BYTES
                ),
                allow_not_found=True,
                # Once sent, transport/read/size/shape failures are completion-
                # ambiguous. The hub may have committed before its response was
                # lost, so these failures must never invite a blind retry.
                indeterminate_mutation=is_mutation,
            )
            if kind == "office_preview":
                try:
                    data = sanitize_office_preview_payload(data)
                except OfficePreviewError as exc:
                    raise HTTPException(
                        502, "Home server returned an invalid Office preview"
                    ) from exc
            # A disconnect/reconnect in another process while the request was
            # in flight must not let an old bearer response repopulate the new
            # link's UI.
            _assert_home_project_link_current(snapshot, mutation=is_mutation)
            if kind == "office_preview":
                return JSONResponse(
                    content=data,
                    headers={
                        "Cache-Control": "private, no-store",
                        "Pragma": "no-cache",
                        "X-Content-Type-Options": "nosniff",
                    },
                )
            return data

        try:
            if is_mutation:
                # Connect/redeem/disconnect use this same lock. In the normal
                # single-process deployment a link cannot be replaced between
                # the upstream commit and our snapshot check.
                async with _home_link_lifecycle_lock:
                    return await execute_against_current_link()
            return await execute_against_current_link()
        finally:
            if staged_upload is not None:
                staged_upload[0].close()

    @router.api_route(
        "/projects",
        methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    )
    async def proxy_projects_root(request: Request):
        return await proxy_projects(request, "")

    @router.api_route(
        "/projects/{remote_path:path}",
        methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    )
    async def proxy_projects_path(remote_path: str, request: Request):
        return await proxy_projects(request, remote_path)

    return router
