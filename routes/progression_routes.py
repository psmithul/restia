"""Read-only API for Restia V2's completion-backed progression profile."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from fastapi import APIRouter, Query, Request

from core.database import SessionLocal
from src.auth_helpers import require_user, resolved_request_owner
from src.progression import build_progression_summary


def setup_progression_routes(
    *,
    session_factory: Callable[[], Any] = SessionLocal,
    now_factory: Callable[[], datetime] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/progression", tags=["progression"])

    @router.get("")
    def progression_summary(
        request: Request,
        utc_offset_minutes: int = Query(
            default=0,
            ge=-840,
            le=840,
            description="Signed minutes local time is ahead of UTC (India is 330).",
        ),
    ) -> dict[str, Any]:
        admitted_user = require_user(request)
        owner = resolved_request_owner(request, admitted_user=admitted_user)
        kwargs: dict[str, Any] = {
            "owner": owner,
            "session_factory": session_factory,
            "utc_offset_minutes": utc_offset_minutes,
        }
        if now_factory is not None:
            kwargs["now"] = now_factory()
        return build_progression_summary(**kwargs)

    return router
