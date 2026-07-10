# routes/messaging_routes.py
"""Account-to-account direct messages (WhatsApp-style DMs).

A conversation is the unordered pair {me, other}; there is no separate
conversations table. Every query is strictly scoped so a caller can only ever
see rows where they are the sender or the recipient — there is no endpoint that
returns another pair's messages. Recipients are validated against the real user
list, so you can't stash messages under a fabricated username.
"""

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_, and_

from core.database import SessionLocal, DirectMessage
from core.database import utcnow_naive
from routes import link_routes
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

MAX_BODY_LEN = 8000            # generous, but bounded — no unbounded blobs
MESSAGES_PAGE_LIMIT = 200      # max messages returned per conversation fetch


class SendMessageRequest(BaseModel):
    body: str


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


def _serialize(msg: DirectMessage, me: str) -> dict:
    return {
        "id": msg.id,
        "sender": msg.sender,
        "recipient": msg.recipient,
        "body": msg.body,
        "mine": msg.sender == me,
        "created_at": (msg.created_at.isoformat() + "Z") if msg.created_at else None,
        "read": msg.read_at is not None,
    }


def setup_messaging_routes():
    router = APIRouter(prefix="/api/messages", tags=["messages"])

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
            return await link_routes.home_get_conversation(me, after_id)
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

            return {
                "messages": [_serialize(m, me) for m in msgs],
                "other": {"username": other_key, "is_admin": _is_admin(request, other_key)},
                "me": me,
            }
        finally:
            db.close()

    @router.post("/conversations/{other}")
    async def send_message(other: str, req: SendMessageRequest, request: Request):
        """Send a message from me to `other`."""
        me = _require_me(request)
        body = (req.body or "").strip()
        if not body:
            raise HTTPException(400, "Message body is required")
        if len(body) > MAX_BODY_LEN:
            raise HTTPException(400, f"Message too long (max {MAX_BODY_LEN} characters)")
        if link_routes.is_home_contact(other):
            return await link_routes.home_send_message(me, body)
        other_key = _resolve_other(request, other)
        if other_key == me:
            raise HTTPException(400, "Cannot send a message to yourself")

        db = SessionLocal()
        try:
            msg = DirectMessage(
                sender=me,
                recipient=other_key,
                body=body,
                created_at=utcnow_naive(),
                read_at=None,
            )
            db.add(msg)
            db.commit()
            db.refresh(msg)
            return {"message": _serialize(msg, me)}
        finally:
            db.close()

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
            return {"ok": True, "marked": marked}
        finally:
            db.close()

    return router
