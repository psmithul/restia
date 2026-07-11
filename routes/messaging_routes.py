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
import json
import logging
from typing import Dict, Optional, Set

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_, and_

from core.database import SessionLocal, DirectMessage
from core.database import utcnow_naive
from routes import link_routes
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

MAX_BODY_LEN = 8000            # generous, but bounded — no unbounded blobs
MESSAGES_PAGE_LIMIT = 200      # max messages returned per conversation fetch
REPLY_PREVIEW_LEN = 140        # quoted-message excerpt shown above a reply
MAX_REACTION_LEN = 16          # one emoji (ZWJ sequences included), not an essay
SSE_KEEPALIVE_S = 15           # comment-ping cadence so proxies don't idle-kill


class SendMessageRequest(BaseModel):
    body: str
    reply_to_id: Optional[int] = None


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
               reply_to: Optional[DirectMessage] = None) -> dict:
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
        "reply_to": None,
    }
    if msg.reply_to_id and reply_to is not None:
        out["reply_to"] = {
            "id": reply_to.id,
            "sender": reply_to.sender,
            "body": "" if reply_to.deleted_at is not None
                    else (reply_to.body or "")[:REPLY_PREVIEW_LEN],
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
                          reply_to: Optional[DirectMessage] = None) -> None:
    """Fan one new/changed message out to both participants' SSE streams,
    serialized per receiver (the `mine` flag differs). Every code path that
    inserts or mutates a direct_messages row must come through here —
    send/edit/delete/react below, and the hub-side guest ingestion in
    routes/link_routes.py — so messages arrive live regardless of entry
    point. Call while the row's attributes are loaded (before Session.close)."""
    for user in (msg.sender, msg.recipient):
        other = msg.recipient if user == msg.sender else msg.sender
        bus.publish(user, event, {"with": other, "message": _serialize(msg, user, reply_to)})


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

    @router.get("/users")
    async def list_dm_users(request: Request):
        """Every account except me — the 'start a new chat' picker. Also
        includes Home Link guests (hub side) and the home contact (client
        side) so both ends can start those conversations."""
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
            out.append({
                "username": link_routes.home_contact_name(),
                "is_admin": False,
                "home": True,
                "connected": link_routes.home_connected(me),
            })
        return {"users": out, "me": me}

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
                c["last_body"] = m.body
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
            out = {"conversations": ordered, "me": me}
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
            return {
                "messages": [_serialize(m, me, replies.get(m.reply_to_id)) for m in msgs],
                "other": {
                    "username": other_key,
                    "is_admin": _is_admin(request, other_key),
                    "home": False,
                    "remote": other_key.endswith(link_routes.GUEST_SUFFIX),
                },
                "me": me,
            }
        finally:
            db.close()

    @router.post("/conversations/{other}")
    async def send_message(other: str, req: SendMessageRequest, request: Request):
        """Send a message from me to `other`, optionally quoting one earlier
        message from this same conversation (reply_to_id)."""
        me = _require_me(request)
        body = (req.body or "").strip()
        if not body:
            raise HTTPException(400, "Message body is required")
        if len(body) > MAX_BODY_LEN:
            raise HTTPException(400, f"Message too long (max {MAX_BODY_LEN} characters)")
        if link_routes.is_home_contact(other):
            # home_send_message proxies a bare body; the link protocol has no
            # reply field, so a quote would silently vanish hub-side.
            if req.reply_to_id:
                raise HTTPException(400, "Replies are not available in this conversation")
            return await link_routes.home_send_message(me, body)
        other_key = _resolve_other(request, other)
        if other_key == me:
            raise HTTPException(400, "Cannot send a message to yourself")

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
            db.commit()
            db.refresh(msg)
            publish_message_event(msg, "message", reply_to)
            return {"message": _serialize(msg, me, reply_to)}
        finally:
            db.close()

    @router.put("/msg/{msg_id}")
    async def edit_message(msg_id: int, req: EditMessageRequest, request: Request):
        """Edit my own message in place (sets edited_at)."""
        me = _require_me(request)
        body = (req.body or "").strip()
        if not body:
            raise HTTPException(400, "Message body is required")
        if len(body) > MAX_BODY_LEN:
            raise HTTPException(400, f"Message too long (max {MAX_BODY_LEN} characters)")
        db = SessionLocal()
        try:
            msg = _own_message(db, me, msg_id)
            _reject_federated_pair(msg, me)
            if msg.deleted_at is not None:
                raise HTTPException(400, "Message was deleted")
            msg.body = body
            msg.edited_at = utcnow_naive()
            db.commit()
            db.refresh(msg)
            reply_to = _reply_target(db, msg)
            publish_message_event(msg, "update", reply_to)
            return {"message": _serialize(msg, me, reply_to)}
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
            publish_message_event(msg, "update", reply_to)
            return {"message": _serialize(msg, me, reply_to)}
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
