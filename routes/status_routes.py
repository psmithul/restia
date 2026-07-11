# routes/status_routes.py
"""Status photos — a BeReal-style "what I'm doing" share.

Each account can post a photo (optional caption) that its chat contacts see for
a while and then it expires. "Contacts" are exactly the accounts you've
exchanged direct messages with — the same trust boundary as a DM — plus
yourself, so a status is never broadcast to strangers on the instance.

Images are stored base64 + Fernet-encrypted at rest (EncryptedText), fetched
only through an authorization check that re-confirms the viewer is a contact of
the author. Posts are ephemeral: expired rows are filtered out of every read
and opportunistically purged.
"""

import logging
import re
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import or_

from core.database import (
    DirectMessage, SessionLocal, StatusPost, StatusView, utcnow_naive,
)
from src.auth_helpers import require_user
from src.upload_limits import read_byte_limit_env

logger = logging.getLogger(__name__)

STATUS_UPLOAD_MAX_BYTES = read_byte_limit_env("RESTIA_STATUS_UPLOAD_MAX_BYTES", 8 * 1024 * 1024)
MAX_CAPTION_LEN = 280
DEFAULT_TTL_HOURS = 24
MAX_TTL_HOURS = 168            # a week, tops
# A data URL for a still image only — no SVG (script vector) and no other types.
_DATA_URL_RE = re.compile(r"^data:image/(png|jpeg|jpg|webp|gif);base64,[A-Za-z0-9+/=\s]+$")


class StatusPostRequest(BaseModel):
    image: str                 # data:image/...;base64,....
    caption: Optional[str] = None
    ttl_hours: Optional[int] = None


def _norm(name: Optional[str]) -> str:
    return str(name or "").strip().lower()


def _require_me(request: Request) -> str:
    me = _norm(require_user(request))
    if not me:
        raise HTTPException(403, "Status posts require a signed-in account")
    return me


def _contacts(db, me: str) -> set:
    """Accounts `me` has a DM thread with, plus me — the audience for a status
    and the gate for viewing one."""
    rows = (db.query(DirectMessage.sender, DirectMessage.recipient)
            .filter(or_(DirectMessage.sender == me, DirectMessage.recipient == me))
            .all())
    out = {me}
    for s, r in rows:
        out.add(s if s != me else r)
    return out


def _purge_expired(db) -> None:
    now = utcnow_naive()
    expired = [row.id for row in db.query(StatusPost.id).filter(StatusPost.expires_at <= now).all()]
    if expired:
        db.query(StatusView).filter(StatusView.status_id.in_(expired)).delete(synchronize_session=False)
        db.query(StatusPost).filter(StatusPost.id.in_(expired)).delete(synchronize_session=False)
        db.commit()


def setup_status_routes():
    router = APIRouter(prefix="/api/status", tags=["status"])

    @router.post("/post")
    async def create_status(body: StatusPostRequest, request: Request):
        """Post a photo visible to my contacts until it expires."""
        me = _require_me(request)
        image = (body.image or "").strip()
        if not _DATA_URL_RE.match(image):
            raise HTTPException(400, "image must be a base64 data URL (png/jpeg/webp/gif)")
        # base64 inflates ~4/3; bound the decoded size against the upload cap.
        approx_bytes = int(len(image) * 3 / 4)
        if approx_bytes > STATUS_UPLOAD_MAX_BYTES:
            raise HTTPException(400, "Image too large")
        caption = (body.caption or "").strip()[:MAX_CAPTION_LEN] or None
        ttl = DEFAULT_TTL_HOURS if body.ttl_hours is None else int(body.ttl_hours)
        if ttl < 1 or ttl > MAX_TTL_HOURS:
            raise HTTPException(400, f"ttl_hours must be 1..{MAX_TTL_HOURS}")
        now = utcnow_naive()
        db = SessionLocal()
        try:
            post = StatusPost(author=me, image=image, caption=caption,
                              created_at=now, expires_at=now + timedelta(hours=ttl))
            db.add(post)
            db.commit()
            db.refresh(post)
            return {"ok": True, "id": post.id,
                    "expires_at": post.expires_at.isoformat() + "Z"}
        finally:
            db.close()

    @router.get("/feed")
    async def feed(request: Request):
        """Contacts' (and my own) live statuses, grouped by author, most-recent
        author first. Metadata only — images load per-post from /{id}/image."""
        me = _require_me(request)
        db = SessionLocal()
        try:
            _purge_expired(db)
            contacts = _contacts(db, me)
            now = utcnow_naive()
            rows = (db.query(StatusPost)
                    .filter(StatusPost.author.in_(contacts), StatusPost.expires_at > now)
                    .order_by(StatusPost.created_at.asc()).all())
            seen_ids = {sv.status_id for sv in
                        db.query(StatusView).filter(StatusView.viewer == me).all()}
            authors: dict = {}
            for p in rows:
                a = authors.get(p.author)
                if a is None:
                    a = {"author": p.author, "mine": p.author == me,
                         "posts": [], "last_at": None, "has_unseen": False}
                    authors[p.author] = a
                seen = p.id in seen_ids or p.author == me
                a["posts"].append({
                    "id": p.id,
                    "caption": p.caption,
                    "created_at": p.created_at.isoformat() + "Z",
                    "seen": seen,
                })
                a["last_at"] = p.created_at.isoformat() + "Z"
                if not seen:
                    a["has_unseen"] = True
            # Unseen contacts first, then most-recent; my own tile leads.
            ordered = sorted(
                authors.values(),
                key=lambda a: (a["mine"], a["has_unseen"], a["last_at"] or ""),
                reverse=True,
            )
            return {"statuses": ordered, "me": me}
        finally:
            db.close()

    def _post_if_visible(db, me: str, status_id: int) -> StatusPost:
        p = db.query(StatusPost).filter(StatusPost.id == status_id).first()
        if not p or p.expires_at <= utcnow_naive():
            raise HTTPException(404, "Status not found")
        # A viewer must be a contact of the author (same gate as DMs).
        if p.author != me and me not in _contacts(db, p.author):
            raise HTTPException(404, "Status not found")
        return p

    @router.get("/{status_id}/image")
    async def get_image(status_id: int, request: Request):
        """The photo itself, for the viewer — authorization re-checked here so
        the image is never served to a non-contact."""
        me = _require_me(request)
        db = SessionLocal()
        try:
            p = _post_if_visible(db, me, status_id)
            return {"id": p.id, "author": p.author, "image": p.image,
                    "caption": p.caption,
                    "created_at": p.created_at.isoformat() + "Z"}
        finally:
            db.close()

    @router.post("/{status_id}/seen")
    async def mark_seen(status_id: int, request: Request):
        """Record that I viewed this status (idempotent)."""
        me = _require_me(request)
        db = SessionLocal()
        try:
            p = _post_if_visible(db, me, status_id)
            if p.author == me:
                return {"ok": True}      # authors don't "view" their own
            exists = (db.query(StatusView)
                      .filter(StatusView.status_id == status_id, StatusView.viewer == me)
                      .first())
            if not exists:
                db.add(StatusView(status_id=status_id, viewer=me, created_at=utcnow_naive()))
                db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.get("/{status_id}/viewers")
    async def viewers(status_id: int, request: Request):
        """Who's seen my status (author only)."""
        me = _require_me(request)
        db = SessionLocal()
        try:
            p = db.query(StatusPost).filter(StatusPost.id == status_id).first()
            if not p or p.author != me:
                raise HTTPException(404, "Status not found")
            names = [sv.viewer for sv in
                     db.query(StatusView).filter(StatusView.status_id == status_id).all()]
            return {"viewers": sorted(names), "count": len(names)}
        finally:
            db.close()

    @router.delete("/{status_id}")
    async def delete_status(status_id: int, request: Request):
        """Delete my own status."""
        me = _require_me(request)
        db = SessionLocal()
        try:
            p = db.query(StatusPost).filter(StatusPost.id == status_id).first()
            if not p or p.author != me:
                raise HTTPException(404, "Status not found")
            db.query(StatusView).filter(StatusView.status_id == status_id).delete(synchronize_session=False)
            db.delete(p)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    return router
