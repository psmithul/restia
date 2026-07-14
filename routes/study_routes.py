"""Owner-scoped Study Mode goal and focus-timer API."""

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from core.database import Session as DbSession, SessionLocal
from src.auth_helpers import effective_owner, effective_user, require_user
from src.study_mode import (
    StudyGoalConflictError,
    StudyGoalRequiredError,
    finish_study_timer,
    get_study_state,
    pause_study_timer,
    record_study_review,
    save_study_goal,
    start_study_timer,
)


class StudyGoalRequest(BaseModel):
    goal_text: str = Field(..., min_length=1, max_length=500)
    target_minutes: int = Field(..., ge=15, le=525_600)
    target_date: Optional[str] = Field(default=None, max_length=10)
    reset_progress: bool = False

    @field_validator("goal_text")
    @classmethod
    def clean_goal(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("goal cannot be empty")
        return cleaned

    @field_validator("target_date")
    @classmethod
    def valid_target_date(cls, value: Optional[str]) -> Optional[str]:
        if value in (None, ""):
            return None
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("target_date must use YYYY-MM-DD") from exc
        return value


class StudyReviewRequest(BaseModel):
    outcome: Literal["missed", "hinted", "clean", "transfer"]


def setup_study_routes() -> APIRouter:
    router = APIRouter(prefix="/api/study", tags=["study"])

    def owner_for(request: Request, session_id: str) -> Optional[str]:
        # Keep the route fail-closed if auth middleware is ever bypassed while
        # preserving Restia's explicit auth-disabled / localhost modes.
        require_user(request)
        user = effective_user(request)
        db = SessionLocal()
        try:
            row = db.query(DbSession.owner, DbSession.mode).filter(
                DbSession.id == session_id
            ).first()
        finally:
            db.close()
        if row is None or (user and row.owner != user):
            # Keep the same non-enumerating behavior as the session routes.
            raise HTTPException(404, f"Study session {session_id} not found")
        if str(row.mode or "").lower() != "study":
            raise HTTPException(409, "Selected chat is not a Study workspace")
        return effective_owner(request)

    @router.get("/state")
    async def state(
        request: Request,
        session_id: str = Query(..., min_length=1, max_length=200),
    ):
        return get_study_state(owner_for(request, session_id), session_id)

    @router.put("/goal")
    async def update_goal(
        payload: StudyGoalRequest,
        request: Request,
        session_id: str = Query(..., min_length=1, max_length=200),
    ):
        owner = owner_for(request, session_id)
        try:
            return save_study_goal(
                owner,
                session_id,
                payload.goal_text,
                payload.target_minutes,
                payload.target_date,
                reset_progress=payload.reset_progress,
            )
        except StudyGoalConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(500, f"Could not save study goal: {exc}") from exc

    @router.post("/timer/start")
    async def start_timer(
        request: Request,
        session_id: str = Query(..., min_length=1, max_length=200),
    ):
        owner = owner_for(request, session_id)
        try:
            return start_study_timer(owner, session_id)
        except StudyGoalRequiredError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(500, f"Could not start study timer: {exc}") from exc

    @router.post("/timer/pause")
    async def pause_timer(
        request: Request,
        session_id: str = Query(..., min_length=1, max_length=200),
    ):
        owner = owner_for(request, session_id)
        try:
            return pause_study_timer(owner, session_id)
        except Exception as exc:
            raise HTTPException(500, f"Could not pause study timer: {exc}") from exc

    @router.post("/timer/finish")
    async def finish_timer(
        request: Request,
        session_id: str = Query(..., min_length=1, max_length=200),
    ):
        owner = owner_for(request, session_id)
        try:
            return finish_study_timer(owner, session_id)
        except Exception as exc:
            raise HTTPException(500, f"Could not finish study timer: {exc}") from exc

    @router.post("/review")
    async def record_review(
        payload: StudyReviewRequest,
        request: Request,
        session_id: str = Query(..., min_length=1, max_length=200),
    ):
        owner = owner_for(request, session_id)
        try:
            return record_study_review(owner, session_id, payload.outcome)
        except ValueError as exc:
            # Keep direct callers and future payload adapters fail-loud even
            # though the public request model already validates exact values.
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(500, f"Could not record study review: {exc}") from exc

    return router
