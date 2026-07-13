# routes/messaging_routes.py
"""Account-to-account direct messages (WhatsApp-style DMs).

A conversation is the unordered pair {me, other}; there is no separate
conversations table. Every query is strictly scoped so a caller can only ever
see rows where they are the sender or the recipient — there is no endpoint that
returns another pair's messages. Recipients are validated against the real user
list, so you can't stash messages under a fabricated username.

Real-time delivery rides an in-process pub/sub (`bus`): every mutation is
fanned out to both participants' open SSE streams (GET /stream) as named
events — message / update / typing / read. Editing, soft deletion, reactions
and replies apply to local pairs only; conversations that cross the Home Link
federation boundary (routes/link_routes.py) reject those verbs because the
link protocol doesn't sync them.
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import io
import json
import logging
import os
import re
import threading
import uuid
import warnings
from typing import Dict, Optional, Set
from urllib.parse import quote

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import or_, and_
from sqlalchemy.orm import load_only
from starlette.concurrency import run_in_threadpool

from core.database import SessionLocal, DirectMessage, DirectMessageAttachment
from core.database import utcnow_naive
from routes import link_routes
from src.auth_helpers import require_user
from src.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

MAX_BODY_LEN = 8000            # generous, but bounded — no unbounded blobs
MESSAGES_PAGE_LIMIT = 200      # max messages returned per conversation fetch
REPLY_PREVIEW_LEN = 140        # quoted-message excerpt shown above a reply
MAX_REACTION_LEN = 16          # one emoji (ZWJ sequences included), not an essay
SSE_KEEPALIVE_S = 15           # comment-ping cadence so proxies don't idle-kill
MAX_PHOTOS_PER_MESSAGE = 1
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_PHOTO_PIXELS = 12_000_000
MAX_PHOTO_DATA_CHARS = ((MAX_PHOTO_BYTES + 2) // 3) * 4 + 128
_PHOTO_FORMATS = {
    "PNG": ("image/png", ".png"),
    "JPEG": ("image/jpeg", ".jpg"),
    "WEBP": ("image/webp", ".webp"),
}
_PHOTO_ID_RE = re.compile(r"^[0-9a-f]{32}$")

# Pillow expands compressed images to full pixel buffers before re-encoding.
# Keep only one normalization in flight so several authenticated profiles
# cannot multiply that peak memory and OOM the single-process deployment.
# Per-profile rate limiting also bounds sustained CPU-heavy decode attempts.
_photo_decode_slot = threading.BoundedSemaphore(1)
photo_send_limiter = RateLimiter(max_requests=12, window_seconds=60)


class PhotoAttachmentRequest(BaseModel):
    name: str = Field(default="photo", max_length=240)
    # Browser FileReader data URL or raw base64. MIME is derived from decoded
    # bytes, never trusted from this field/name.
    data: str = Field(..., min_length=1, max_length=MAX_PHOTO_DATA_CHARS)


class SendMessageRequest(BaseModel):
    body: str = ""
    reply_to_id: Optional[int] = None
    attachments: list[PhotoAttachmentRequest] = Field(
        default_factory=list,
        max_length=MAX_PHOTOS_PER_MESSAGE,
    )


class EditMessageRequest(BaseModel):
    body: str


class ReactRequest(BaseModel):
    emoji: str


class _MessageBus:
    """In-process fan-out of DM events to SSE subscribers.

    Keyed by lowercase username; each open SSE connection holds its own
    bounded queue (multiple tabs are independent subscribers). Per-process
    only — fine for the single-worker uvicorn deployment; the frontend keeps
    a polling fallback, so a lost event degrades to polling latency rather
    than lost data.
    """

    QUEUE_SIZE = 256

    def __init__(self) -> None:
        self._subscribers: Dict[str, Set[asyncio.Queue]] = {}

    def subscribe(self, username: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self.QUEUE_SIZE)
        self._subscribers.setdefault(_normalize_username(username), set()).add(q)
        return q

    def unsubscribe(self, username: str, q: asyncio.Queue) -> None:
        key = _normalize_username(username)
        subs = self._subscribers.get(key)
        if subs is not None:
            subs.discard(q)
            if not subs:
                self._subscribers.pop(key, None)

    def publish(self, username: str, event: str, data: dict) -> None:
        """Fire-and-forget. On overflow drop that queue's oldest event: a
        stalled consumer only stales its own tab (its polling fallback
        resyncs it), and can never block the sender."""
        for q in tuple(self._subscribers.get(_normalize_username(username), ())):
            try:
                q.put_nowait((event, data))
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait((event, data))
                except asyncio.QueueFull:
                    pass


bus = _MessageBus()


def _auth_manager(request: Request):
    return getattr(request.app.state, "auth_manager", None)


def _known_users(request: Request) -> dict:
    mgr = _auth_manager(request)
    users = getattr(mgr, "users", None)
    return users if isinstance(users, dict) else {}


def _normalize_username(name: Optional[str]) -> str:
    return str(name or "").strip().lower()


def _safe_photo_filename(value: object, suffix: str) -> str:
    """Return a display-only filename with the verified format's suffix."""
    name = str(value or "photo").replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]+", "", name).strip().lstrip(".")
    stem = os.path.splitext(name)[0].strip() or "photo"
    stem = re.sub(r"\s+", " ", stem)[:160].strip() or "photo"
    return stem + suffix


def _decode_photo_data(value: str) -> bytes:
    raw = (value or "").strip()
    if raw.startswith("data:"):
        head, sep, payload = raw.partition(",")
        if not sep or ";base64" not in head.lower():
            raise HTTPException(400, "Photo must be base64 encoded")
    else:
        payload = raw
    # FileReader emits compact base64. Reject whitespace/alternate alphabets so
    # encoded-size checks cannot be bypassed with ignored characters.
    if not payload or any(ch.isspace() for ch in payload):
        raise HTTPException(400, "Photo data is malformed")
    try:
        data = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "Photo data is malformed")
    if not data:
        raise HTTPException(400, "Photo is empty")
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, f"Photo exceeds {MAX_PHOTO_BYTES // (1024 * 1024)} MB limit")
    return data


def _normalize_photo(item: PhotoAttachmentRequest | dict) -> dict:
    """Verify and normalize one untrusted raster image.

    Re-encoding strips EXIF/text/profile payloads and prevents a file extension
    or claimed MIME from controlling the served Content-Type. Animated images
    are rejected: this feature intentionally carries still photos only.
    """
    if isinstance(item, BaseModel):
        item = item.model_dump() if hasattr(item, "model_dump") else item.dict()
    if not isinstance(item, dict):
        raise HTTPException(400, "Invalid photo attachment")
    source = _decode_photo_data(str(item.get("data") or ""))
    try:
        from PIL import Image, ImageOps
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(source)) as probe:
                fmt = str(probe.format or "").upper()
                if fmt not in _PHOTO_FORMATS:
                    raise HTTPException(400, "Only PNG, JPEG, and WebP photos are allowed")
                width, height = int(probe.width or 0), int(probe.height or 0)
                if width < 1 or height < 1 or width * height > MAX_PHOTO_PIXELS:
                    raise HTTPException(413, "Photo dimensions are too large")
                if int(getattr(probe, "n_frames", 1) or 1) != 1:
                    raise HTTPException(400, "Animated images are not allowed")
                probe.verify()

            with Image.open(io.BytesIO(source)) as image:
                if int(getattr(image, "n_frames", 1) or 1) != 1:
                    raise HTTPException(400, "Animated images are not allowed")
                image = ImageOps.exif_transpose(image)
                width, height = image.size
                out = io.BytesIO()
                if fmt == "JPEG":
                    image.convert("RGB").save(out, format="JPEG", quality=90)
                elif fmt == "PNG":
                    if image.mode not in ("RGB", "RGBA", "L", "LA"):
                        image = image.convert("RGBA")
                    image.save(out, format="PNG")
                else:  # WEBP
                    image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                    image.save(out, format="WEBP", quality=90, method=4)
    except HTTPException:
        raise
    except Exception as exc:
        # Pillow's DecompressionBombError/Warning and all parser failures are
        # intentionally indistinguishable to callers.
        logger.info("Rejected invalid DM photo: %s", exc.__class__.__name__)
        raise HTTPException(400, "Invalid or unsafe photo")

    normalized = out.getvalue()
    if not normalized or len(normalized) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Normalized photo exceeds size limit")
    mime, suffix = _PHOTO_FORMATS[fmt]
    return {
        "id": uuid.uuid4().hex,
        "filename": _safe_photo_filename(item.get("name"), suffix),
        "mime": mime,
        "size": len(normalized),
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(normalized).hexdigest(),
        "data_b64": base64.b64encode(normalized).decode("ascii"),
    }


def prepare_photo_attachments(items) -> list[dict]:
    values = list(items or [])
    if len(values) > MAX_PHOTOS_PER_MESSAGE:
        raise HTTPException(400, f"Maximum {MAX_PHOTOS_PER_MESSAGE} photos per message")
    if not values:
        return []
    if not _photo_decode_slot.acquire(blocking=False):
        raise HTTPException(429, "Photo processing is busy — try again shortly")
    try:
        return [_normalize_photo(item) for item in values]
    finally:
        _photo_decode_slot.release()


def attach_prepared_photos(db, message: DirectMessage, prepared: list[dict]) -> list[DirectMessageAttachment]:
    rows = []
    for item in prepared:
        row = DirectMessageAttachment(
            id=item["id"],
            message_id=message.id,
            filename=item["filename"],
            mime=item["mime"],
            size=item["size"],
            width=item["width"],
            height=item["height"],
            sha256=item["sha256"],
            data_b64=item["data_b64"],
            created_at=utcnow_naive(),
        )
        db.add(row)
        rows.append(row)
    return rows


def _attachment_meta(row: DirectMessageAttachment) -> dict:
    return {
        "id": row.id,
        "name": row.filename,
        "mime": row.mime,
        "size": int(row.size or 0),
        "width": int(row.width or 0),
        "height": int(row.height or 0),
    }


def attachment_rows_by_message(db, message_ids) -> dict[int, list[DirectMessageAttachment]]:
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
    out: dict[int, list[DirectMessageAttachment]] = {}
    for row in rows:
        out.setdefault(row.message_id, []).append(row)
    return out


def serialized_attachments(rows) -> list[dict]:
    return [_attachment_meta(row) for row in (rows or [])]


def _photo_preview(rows) -> str:
    count = len(rows or [])
    return "Photo" if count == 1 else (f"{count} photos" if count else "")


def _photo_response(filename: str, mime: str, data: bytes) -> Response:
    allowed = {value[0]: value[1] for value in _PHOTO_FORMATS.values()}
    suffix = allowed.get(str(mime or "").lower())
    if not suffix:
        raise HTTPException(404, "Photo not found")
    safe_name = _safe_photo_filename(filename, suffix)
    ascii_name = safe_name.encode("ascii", "ignore").decode("ascii") or f"photo{suffix}"
    ascii_name = ascii_name.replace('"', "")
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": (
                f'inline; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(safe_name, safe='')}"
            ),
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _decode_stored_photo(encoded, expected_sha256: str = "") -> bytes:
    value = str(encoded or "")
    if not value or len(value) > MAX_PHOTO_DATA_CHARS:
        raise HTTPException(500, "Stored photo is corrupt")
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(500, "Stored photo is corrupt")
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(500, "Stored photo is corrupt")
    if expected_sha256 and not hmac.compare_digest(
        hashlib.sha256(data).hexdigest(),
        str(expected_sha256),
    ):
        raise HTTPException(500, "Stored photo failed integrity verification")
    return data


def _resolve_other(request: Request, other: str) -> str:
    """Normalize + validate a recipient username against the real user list,
    then against registered Home Link guests ('<handle>@remote') so the hub
    owner can reply to remote instances (routes/link_routes.py).

    Returns the canonical username or raises 404 so a probe can't distinguish
    'no such user' from 'not allowed'."""
    key = _normalize_username(other)
    users = _known_users(request)
    # Usernames are stored/compared lowercase (see core/auth.py).
    canon = {_normalize_username(u): u for u in users.keys()}
    if key in canon:
        return key
    if link_routes.resolve_guest(key):
        return key
    raise HTTPException(404, "User not found")


def _require_me(request: Request) -> str:
    """The signed-in account. DMs need a concrete identity on both ends;
    anonymous / single-user modes (require_user returns '') can't participate."""
    me = require_user(request)
    if not me:
        raise HTTPException(403, "Direct messages require a signed-in account")
    return _normalize_username(me)


def _is_admin(request: Request, username: str) -> bool:
    mgr = _auth_manager(request)
    try:
        return bool(mgr and mgr.is_admin(username))
    except Exception:
        return False


def _my_display(me: str) -> Optional[str]:
    """The signed-in user's own display name, or None to fall back to username."""
    try:
        from routes.profile_routes import display_names_for
        return display_names_for([me]).get(me)
    except Exception:
        return None


def _pair_filter(me: str, other: str):
    """Rows belonging to the {me, other} conversation — and only that pair."""
    return or_(
        and_(DirectMessage.sender == me, DirectMessage.recipient == other),
        and_(DirectMessage.sender == other, DirectMessage.recipient == me),
    )


def _is_federated(name: str) -> bool:
    """The home contact or an '@remote' Home Link guest — a conversation that
    crosses the federation boundary. The link protocol only syncs plain sends,
    so edit/delete/react/typing are rejected (or no-op'd) for these."""
    key = _normalize_username(name)
    return link_routes.is_home_contact(key) or key.endswith(link_routes.GUEST_SUFFIX)


def _parse_reactions(raw) -> dict:
    """The reactions column holds JSON {username: emoji}. Parse defensively —
    a corrupt or legacy value must read as 'no reactions', never a 500."""
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(k): str(v)[:MAX_REACTION_LEN]
        for k, v in data.items()
        if isinstance(v, str) and v
    }


def _serialize(msg: DirectMessage, me: str,
               reply_to: Optional[DirectMessage] = None,
               attachments=None,
               reply_attachments=None) -> dict:
    """One message as the frontend renders it, from `me`'s perspective.
    Deleted messages are tombstones: blank body, no reactions. The reply
    quote is denormalized (id/sender/excerpt) so the client never has to
    chase ids — pass the already-fetched target as `reply_to`."""
    deleted = msg.deleted_at is not None
    out = {
        "id": msg.id,
        "sender": msg.sender,
        "recipient": msg.recipient,
        "body": "" if deleted else msg.body,
        "mine": msg.sender == me,
        "created_at": (msg.created_at.isoformat() + "Z") if msg.created_at else None,
        "read": msg.read_at is not None,
        "edited": msg.edited_at is not None,
        "deleted": deleted,
        "reactions": {} if deleted else _parse_reactions(msg.reactions),
        "attachments": [] if deleted else serialized_attachments(attachments),
        "reply_to": None,
    }
    if msg.reply_to_id and reply_to is not None:
        reply_deleted = reply_to.deleted_at is not None
        reply_body = "" if reply_deleted else (reply_to.body or "")[:REPLY_PREVIEW_LEN]
        if not reply_body and not reply_deleted:
            reply_body = _photo_preview(reply_attachments)
        out["reply_to"] = {
            "id": reply_to.id,
            "sender": reply_to.sender,
            "body": reply_body,
        }
    return out


def _reply_target(db, msg: DirectMessage) -> Optional[DirectMessage]:
    if not msg.reply_to_id:
        return None
    return db.query(DirectMessage).filter(DirectMessage.id == msg.reply_to_id).first()


def _reply_targets(db, msgs) -> dict:
    """Batch-fetch every quoted message for a page of messages (one query,
    not one per reply). Targets were pair-validated at send time."""
    ids = {m.reply_to_id for m in msgs if m.reply_to_id}
    if not ids:
        return {}
    rows = db.query(DirectMessage).filter(DirectMessage.id.in_(ids)).all()
    return {r.id: r for r in rows}


def publish_message_event(msg: DirectMessage, event: str = "message",
                          reply_to: Optional[DirectMessage] = None,
                          attachments=None,
                          reply_attachments=None) -> None:
    """Fan one new/changed message out to both participants' SSE streams,
    serialized per receiver (the `mine` flag differs). Every code path that
    inserts or mutates a direct_messages row must come through here —
    send/edit/delete/react below, and the hub-side guest ingestion in
    routes/link_routes.py — so messages arrive live regardless of entry
    point. Call while the row's attributes are loaded (before Session.close)."""
    for user in (msg.sender, msg.recipient):
        other = msg.recipient if user == msg.sender else msg.sender
        bus.publish(user, event, {
            "with": other,
            "message": _serialize(
                msg,
                user,
                reply_to,
                attachments=attachments,
                reply_attachments=reply_attachments,
            ),
        })


def setup_messaging_routes():
    router = APIRouter(prefix="/api/messages", tags=["messages"])

    def _own_message(db, me: str, msg_id: int) -> DirectMessage:
        """A message I sent, or 404. Not-mine reads identically to
        nonexistent so ids can't be probed for existence."""
        msg = (
            db.query(DirectMessage)
            .filter(DirectMessage.id == msg_id, DirectMessage.sender == me)
            .first()
        )
        if msg is None:
            raise HTTPException(404, "Message not found")
        return msg

    def _reject_federated_pair(msg: DirectMessage, me: str) -> None:
        partner = msg.recipient if msg.sender == me else msg.sender
        if _is_federated(partner):
            raise HTTPException(400, "Not available in this conversation")

    @router.get("/stream")
    async def stream_events(request: Request):
        """Live DM events for the signed-in user (SSE). Named events, each
        `event: <name>` + `data: <json>`:

          message → {"with": <partner>, "message": <serialized>} — new message
          update  → same shape — edit / delete / reaction change
          typing  → {"from": <username>} — partner is typing (ephemeral)
          read    → {"from": <username>} — that user just read my messages

        Replaces the frontend's polling loop; polling stays as the fallback."""
        me = _require_me(request)

        async def gen():
            # No SessionLocal in here: events arrive pre-serialized from the
            # bus, so a slow client can never pin a DB session open.
            q = bus.subscribe(me)
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event, data = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        # Comment line — ignored by EventSource, but resets
                        # proxy/browser idle timers.
                        yield ": ping\n\n"
                        continue
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
            finally:
                # Starlette cancels the generator on client disconnect; the
                # queue must be dropped or the bus would fill it forever.
                bus.unsubscribe(me, q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/profiles")
    @router.get("/users", deprecated=True)
    async def list_dm_users(request: Request):
        """Every local profile except me — the 'start a new chat' picker. Also
        includes Home Link guests (hub side) and the home contact (client
        side) so both ends can start those conversations. ``/users`` and the
        ``users`` response key remain for older clients."""
        me = _require_me(request)
        users = _known_users(request)
        out = []
        for uname in users.keys():
            key = _normalize_username(uname)
            if not key or key == me:
                continue
            out.append({"username": key, "is_admin": _is_admin(request, key)})
        if link_routes.hub_enabled():
            local = {u["username"] for u in out}
            for gname in link_routes.list_guests():
                if gname not in local and gname != me:
                    out.append({"username": gname, "is_admin": False, "remote": True})
        out.sort(key=lambda u: u["username"])
        if link_routes.home_enabled():
            connected = link_routes.home_connected(me)
            out.append({
                "username": link_routes.home_contact_name(),
                "is_admin": False,
                "home": True,
                "connected": connected,
            })
        return {"profiles": out, "users": out, "me": me}

    @router.get("/conversations")
    async def list_conversations(request: Request):
        """One row per person I've exchanged messages with: last message +
        unread count, most-recently-active first."""
        me = _require_me(request)
        db = SessionLocal()
        try:
            rows = (
                db.query(DirectMessage)
                .filter(or_(DirectMessage.sender == me, DirectMessage.recipient == me))
                .order_by(DirectMessage.created_at.asc())
                .all()
            )
            attachments = attachment_rows_by_message(db, [m.id for m in rows])
            convos: dict = {}
            for m in rows:
                other = m.recipient if m.sender == me else m.sender
                c = convos.get(other)
                if c is None:
                    c = {
                        "username": other,
                        "is_admin": _is_admin(request, other),
                        "last_body": None,
                        "last_sender": None,
                        "last_at": None,
                        "last_mine": False,
                        "unread": 0,
                    }
                    convos[other] = c
                # rows are ascending, so the final assignment is the latest.
                c["last_body"] = (
                    "" if m.deleted_at is not None
                    else ((m.body or "") or _photo_preview(attachments.get(m.id)))
                )
                c["last_sender"] = m.sender
                c["last_mine"] = m.sender == me
                c["last_at"] = (m.created_at.isoformat() + "Z") if m.created_at else None
                if m.recipient == me and m.read_at is None:
                    c["unread"] += 1
            home = await link_routes.home_conversation_entry(me)
            if home:
                convos[home["username"]] = home
            ordered = sorted(
                convos.values(),
                key=lambda c: c["last_at"] or "",
                reverse=True,
            )
            # Show friendly display names where set (fall back to username).
            try:
                from routes.profile_routes import display_names_for
                names = display_names_for([c["username"] for c in ordered])
                for c in ordered:
                    if c["username"] in names:
                        c["display"] = names[c["username"]]
            except Exception:
                pass
            out = {"conversations": ordered, "me": me, "me_display": _my_display(me)}
            # Hub admins also get the queue of pending Home Link requests so
            # they can approve/block right from the Messages UI.
            if link_routes.hub_enabled() and _is_admin(request, me):
                out["link_requests"] = link_routes.pending_requests()
            return out
        finally:
            db.close()

    @router.get("/unread")
    async def unread_counts(request: Request):
        """Total unread + per-sender breakdown, for the sidebar badge/polling."""
        me = _require_me(request)
        db = SessionLocal()
        try:
            rows = (
                db.query(DirectMessage.sender)
                .filter(DirectMessage.recipient == me, DirectMessage.read_at.is_(None))
                .all()
            )
            by_user: dict = {}
            for (sender,) in rows:
                by_user[sender] = by_user.get(sender, 0) + 1
            home_unread = await link_routes.home_unread(me)
            if home_unread:
                by_user[link_routes.home_contact_name()] = home_unread
            return {"total": sum(by_user.values()), "by_user": by_user}
        finally:
            db.close()

    @router.get("/media/{attachment_id}")
    async def get_message_photo(
        attachment_id: str,
        request: Request,
        peer: str = "",
    ):
        """Return one pair-scoped message photo.

        The opaque id is not authorization: the signed-in profile must be one
        of the parent message's two participants.  There is intentionally no
        administrator override and no generic upload/gallery URL.
        """
        me = _require_me(request)
        if not _PHOTO_ID_RE.fullmatch(attachment_id or ""):
            raise HTTPException(404, "Photo not found")

        if peer and link_routes.is_home_contact(peer):
            remote = await link_routes.home_get_media(me, attachment_id)
            data = _decode_stored_photo(remote.get("data"), remote.get("sha256", ""))
            # The configured hub is still an external trust boundary. Verify
            # and re-encode its bytes locally before a browser ever sees them;
            # its claimed MIME/name can never select executable content.
            try:
                safe = await run_in_threadpool(
                    _normalize_photo,
                    {
                        "name": remote.get("name", "photo"),
                        "data": base64.b64encode(data).decode("ascii"),
                    },
                )
                safe_data = _decode_stored_photo(safe["data_b64"], safe["sha256"])
            except HTTPException:
                raise HTTPException(502, "Home server returned an unsafe photo")
            return _photo_response(safe["filename"], safe["mime"], safe_data)

        db = SessionLocal()
        try:
            row = db.query(DirectMessageAttachment).filter(
                DirectMessageAttachment.id == attachment_id
            ).first()
            if row is None:
                raise HTTPException(404, "Photo not found")
            msg = db.query(DirectMessage).filter(
                DirectMessage.id == row.message_id
            ).first()
            if (
                msg is None
                or msg.deleted_at is not None
                or me not in (msg.sender, msg.recipient)
            ):
                raise HTTPException(404, "Photo not found")
            try:
                data = _decode_stored_photo(row.data_b64, row.sha256)
            except HTTPException:
                logger.exception("Corrupt stored DM photo %s", attachment_id)
                raise
            return _photo_response(row.filename, row.mime, data)
        finally:
            db.close()

    @router.get("/conversations/{other}")
    async def get_conversation(other: str, request: Request, after_id: int = 0):
        """Messages in the {me, other} conversation, chronological. Opening a
        conversation marks the other side's messages to me as read. Pass
        ?after_id=<id> to fetch only newer messages (polling)."""
        me = _require_me(request)
        if link_routes.is_home_contact(other):
            out = await link_routes.home_get_conversation(me, after_id)
            # The frontend gates edit/delete/react/typing on these flags.
            out["other"]["home"] = True
            out["other"]["remote"] = False
            return out
        other_key = _resolve_other(request, other)
        if other_key == me:
            raise HTTPException(400, "Cannot open a conversation with yourself")
        db = SessionLocal()
        try:
            q = db.query(DirectMessage).filter(_pair_filter(me, other_key))
            if after_id:
                q = q.filter(DirectMessage.id > after_id)
            # Cap the window; take the most recent MESSAGES_PAGE_LIMIT then re-sort ascending.
            msgs = (
                q.order_by(DirectMessage.created_at.desc())
                .limit(MESSAGES_PAGE_LIMIT)
                .all()
            )
            msgs.reverse()

            # Mark the other party's messages to me as read.
            now = utcnow_naive()
            marked = (
                db.query(DirectMessage)
                .filter(
                    DirectMessage.sender == other_key,
                    DirectMessage.recipient == me,
                    DirectMessage.read_at.is_(None),
                )
                .update({DirectMessage.read_at: now}, synchronize_session=False)
            )
            if marked:
                db.commit()
                # Live read receipt for the sender's open stream.
                bus.publish(other_key, "read", {"from": me})

            replies = _reply_targets(db, msgs)
            attachment_ids = [m.id for m in msgs]
            attachment_ids.extend(r.id for r in replies.values())
            attachments = attachment_rows_by_message(db, attachment_ids)
            other_names = {}
            try:
                from routes.profile_routes import display_names_for
                other_names = display_names_for([other_key])
            except Exception:
                pass
            return {
                "messages": [
                    _serialize(
                        m,
                        me,
                        replies.get(m.reply_to_id),
                        attachments=attachments.get(m.id),
                        reply_attachments=attachments.get(m.reply_to_id),
                    )
                    for m in msgs
                ],
                "other": {
                    "username": other_key,
                    "display": other_names.get(other_key),
                    "is_admin": _is_admin(request, other_key),
                    "home": False,
                    "remote": other_key.endswith(link_routes.GUEST_SUFFIX),
                },
                "me": me,
                "me_display": _my_display(me),
            }
        finally:
            db.close()

    @router.post("/conversations/{other}")
    async def send_message(other: str, req: SendMessageRequest, request: Request):
        """Send a message from me to `other`, optionally quoting one earlier
        message from this same conversation (reply_to_id)."""
        me = _require_me(request)
        body = (req.body or "").strip()
        if len(body) > MAX_BODY_LEN:
            raise HTTPException(400, f"Message too long (max {MAX_BODY_LEN} characters)")
        if not body and not req.attachments:
            raise HTTPException(400, "A message or photo is required")
        if req.attachments and not photo_send_limiter.check(me):
            raise HTTPException(429, "Too many photos — try again later")
        if link_routes.is_home_contact(other):
            if req.reply_to_id:
                raise HTTPException(400, "Replies are not available in this conversation")
            prepared = await run_in_threadpool(prepare_photo_attachments, req.attachments)
            return await link_routes.home_send_message(me, body, prepared)
        other_key = _resolve_other(request, other)
        if other_key == me:
            raise HTTPException(400, "Cannot send a message to yourself")
        prepared = await run_in_threadpool(prepare_photo_attachments, req.attachments)
        if _is_federated(other_key) and any(
            item["size"] > link_routes.MAX_FEDERATED_PHOTO_BYTES
            for item in prepared
        ):
            raise HTTPException(
                413,
                "Photos sent between instances must be 2 MB or smaller",
            )

        db = SessionLocal()
        try:
            reply_to = None
            if req.reply_to_id:
                # The quoted message must live in this exact pair — quoting
                # across conversations would leak another thread's content.
                reply_to = (
                    db.query(DirectMessage)
                    .filter(DirectMessage.id == req.reply_to_id, _pair_filter(me, other_key))
                    .first()
                )
                if reply_to is None or reply_to.deleted_at is not None:
                    raise HTTPException(400, "Cannot reply to that message")
            msg = DirectMessage(
                sender=me,
                recipient=other_key,
                body=body,
                created_at=utcnow_naive(),
                read_at=None,
                reply_to_id=reply_to.id if reply_to else None,
            )
            db.add(msg)
            db.flush()
            photo_rows = attach_prepared_photos(db, msg, prepared)
            db.commit()
            db.refresh(msg)
            reply_photos = (
                attachment_rows_by_message(db, [reply_to.id]).get(reply_to.id)
                if reply_to else None
            )
            publish_message_event(
                msg,
                "message",
                reply_to,
                attachments=photo_rows,
                reply_attachments=reply_photos,
            )
            return {
                "message": _serialize(
                    msg,
                    me,
                    reply_to,
                    attachments=photo_rows,
                    reply_attachments=reply_photos,
                )
            }
        finally:
            db.close()

    @router.put("/msg/{msg_id}")
    async def edit_message(msg_id: int, req: EditMessageRequest, request: Request):
        """Edit my own message in place (sets edited_at)."""
        me = _require_me(request)
        body = (req.body or "").strip()
        if len(body) > MAX_BODY_LEN:
            raise HTTPException(400, f"Message too long (max {MAX_BODY_LEN} characters)")
        db = SessionLocal()
        try:
            msg = _own_message(db, me, msg_id)
            _reject_federated_pair(msg, me)
            if msg.deleted_at is not None:
                raise HTTPException(400, "Message was deleted")
            photos = attachment_rows_by_message(db, [msg.id]).get(msg.id)
            if not body and not photos:
                raise HTTPException(400, "Message body is required")
            msg.body = body
            msg.edited_at = utcnow_naive()
            db.commit()
            db.refresh(msg)
            reply_to = _reply_target(db, msg)
            reply_photos = (
                attachment_rows_by_message(db, [reply_to.id]).get(reply_to.id)
                if reply_to else None
            )
            publish_message_event(
                msg,
                "update",
                reply_to,
                attachments=photos,
                reply_attachments=reply_photos,
            )
            return {"message": _serialize(
                msg,
                me,
                reply_to,
                attachments=photos,
                reply_attachments=reply_photos,
            )}
        finally:
            db.close()

    @router.delete("/msg/{msg_id}")
    async def delete_message(msg_id: int, request: Request):
        """Soft-delete my own message: keep the row as a tombstone, blank the
        body, drop all reactions. Deleting twice returns the tombstone again
        rather than erroring — two tabs racing the same delete both succeed."""
        me = _require_me(request)
        db = SessionLocal()
        try:
            msg = _own_message(db, me, msg_id)
            _reject_federated_pair(msg, me)
            if msg.deleted_at is None:
                msg.deleted_at = utcnow_naive()
                msg.body = ""
                msg.reactions = None
                db.query(DirectMessageAttachment).filter(
                    DirectMessageAttachment.message_id == msg.id
                ).delete(synchronize_session=False)
                db.commit()
                db.refresh(msg)
                publish_message_event(msg, "update")
            return {"message": _serialize(msg, me)}
        finally:
            db.close()

    @router.post("/msg/{msg_id}/react")
    async def react_to_message(msg_id: int, req: ReactRequest, request: Request):
        """Set/replace my single reaction on a message (non-empty emoji) or
        clear it (empty string). Either participant may react; anyone else
        gets the same 404 a nonexistent id would."""
        me = _require_me(request)
        emoji = (req.emoji or "").strip()[:MAX_REACTION_LEN]
        db = SessionLocal()
        try:
            msg = (
                db.query(DirectMessage)
                .filter(
                    DirectMessage.id == msg_id,
                    or_(DirectMessage.sender == me, DirectMessage.recipient == me),
                )
                .first()
            )
            if msg is None:
                raise HTTPException(404, "Message not found")
            _reject_federated_pair(msg, me)
            if msg.deleted_at is not None:
                raise HTTPException(400, "Message was deleted")
            reactions = _parse_reactions(msg.reactions)
            if emoji:
                reactions[me] = emoji
            else:
                reactions.pop(me, None)
            msg.reactions = json.dumps(reactions) if reactions else None
            db.commit()
            db.refresh(msg)
            reply_to = _reply_target(db, msg)
            photos = attachment_rows_by_message(db, [msg.id]).get(msg.id)
            reply_photos = (
                attachment_rows_by_message(db, [reply_to.id]).get(reply_to.id)
                if reply_to else None
            )
            publish_message_event(
                msg,
                "update",
                reply_to,
                attachments=photos,
                reply_attachments=reply_photos,
            )
            return {"message": _serialize(
                msg,
                me,
                reply_to,
                attachments=photos,
                reply_attachments=reply_photos,
            )}
        finally:
            db.close()

    @router.post("/conversations/{other}/typing")
    async def typing(other: str, request: Request):
        """Ephemeral 'I am typing' ping to `other` — no DB write, just an SSE
        event. Federated conversations can't receive it, so they answer
        ok:false instead of erroring (the frontend simply stops sending)."""
        me = _require_me(request)
        if link_routes.is_home_contact(other):
            return {"ok": False}
        other_key = _resolve_other(request, other)
        if other_key == me:
            raise HTTPException(400, "Cannot open a conversation with yourself")
        if _is_federated(other_key):
            return {"ok": False}
        bus.publish(other_key, "typing", {"from": me})
        return {"ok": True}

    @router.post("/conversations/{other}/read")
    async def mark_read(other: str, request: Request):
        """Mark every message from `other` to me as read."""
        me = _require_me(request)
        if link_routes.is_home_contact(other):
            # Hub-side read marking happens when the conversation is fetched.
            return {"ok": True, "marked": 0}
        other_key = _resolve_other(request, other)
        db = SessionLocal()
        try:
            now = utcnow_naive()
            marked = (
                db.query(DirectMessage)
                .filter(
                    DirectMessage.sender == other_key,
                    DirectMessage.recipient == me,
                    DirectMessage.read_at.is_(None),
                )
                .update({DirectMessage.read_at: now}, synchronize_session=False)
            )
            db.commit()
            if marked:
                # Live read receipt for the sender's open stream.
                bus.publish(other_key, "read", {"from": me})
            return {"ok": True, "marked": marked}
        finally:
            db.close()

    return router
