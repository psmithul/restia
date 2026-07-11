# routes/profile_routes.py
"""User display profiles — set the name shown in chat.

Accounts log in with a username, but people want to be shown by a friendly
name. A profile is just a display name (and optional avatar color) resolved
wherever a username would otherwise be shown. Resolution is exposed as a small
helper so the messaging routes can batch-resolve names without importing the
route handlers.
"""

import logging
from typing import Dict, Iterable, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.database import SessionLocal, UserProfile, utcnow_naive
from src.auth_helpers import require_user

logger = logging.getLogger(__name__)

MAX_NAME_LEN = 48


class ProfileRequest(BaseModel):
    display_name: Optional[str] = None
    avatar_color: Optional[str] = None


def _norm(name: Optional[str]) -> str:
    return str(name or "").strip().lower()


def display_names_for(usernames: Iterable[str]) -> Dict[str, str]:
    """Map {username: display_name} for the given users that have one set.
    Callers fall back to the username for anyone missing."""
    keys = {_norm(u) for u in usernames if _norm(u)}
    if not keys:
        return {}
    db = SessionLocal()
    try:
        rows = db.query(UserProfile).filter(UserProfile.username.in_(keys)).all()
        return {r.username: r.display_name for r in rows if r.display_name}
    finally:
        db.close()


def setup_profile_routes():
    router = APIRouter(prefix="/api/profile", tags=["profile"])

    def _me(request: Request) -> str:
        me = _norm(require_user(request))
        if not me:
            raise HTTPException(403, "A signed-in account is required")
        return me

    @router.get("")
    async def get_profile(request: Request):
        me = _me(request)
        db = SessionLocal()
        try:
            p = db.query(UserProfile).filter(UserProfile.username == me).first()
            return {
                "username": me,
                "display_name": (p.display_name if p else None),
                "avatar_color": (p.avatar_color if p else None),
            }
        finally:
            db.close()

    @router.post("")
    async def set_profile(body: ProfileRequest, request: Request):
        me = _me(request)
        name = (body.display_name or "").strip()
        if len(name) > MAX_NAME_LEN:
            raise HTTPException(400, f"Name too long (max {MAX_NAME_LEN} characters)")
        color = (body.avatar_color or "").strip()[:32] or None
        db = SessionLocal()
        try:
            p = db.query(UserProfile).filter(UserProfile.username == me).first()
            if p is None:
                p = UserProfile(username=me, display_name=name or None,
                                avatar_color=color, updated_at=utcnow_naive())
                db.add(p)
            else:
                p.display_name = name or None
                p.avatar_color = color
                p.updated_at = utcnow_naive()
            db.commit()
            return {"ok": True, "display_name": name or None, "avatar_color": color}
        finally:
            db.close()

    return router
