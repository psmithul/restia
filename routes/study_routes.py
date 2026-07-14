"""Owner-scoped Study Mode goal and focus-timer API."""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from src.auth_helpers import effective_owner, require_user
from src.study_mode import (
    StudyGoalConflictError,
    StudyGoalRequiredError,
    finish_study_timer,
    get_study_state,
    pause_study_timer,
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


def setup_study_routes() -> APIRouter:
    router = APIRouter(prefix="/api/study", tags=["study"])

    def owner_for(request: Request) -> Optional[str]:
        # Keep the route fail-closed if auth middleware is ever bypassed while
        # preserving Restia's explicit auth-disabled / localhost modes.
        require_user(request)
        return effective_owner(request)

    @router.get("/state")
    async def state(request: Request):
        return get_study_state(owner_for(request))

    @router.put("/goal")
    async def update_goal(payload: StudyGoalRequest, request: Request):
        owner = owner_for(request)
        try:
            return save_study_goal(
                owner,
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
    async def start_timer(request: Request):
        owner = owner_for(request)
        try:
            return start_study_timer(owner)
        except StudyGoalRequiredError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(500, f"Could not start study timer: {exc}") from exc

    @router.post("/timer/pause")
    async def pause_timer(request: Request):
        owner = owner_for(request)
        try:
            return pause_study_timer(owner)
        except Exception as exc:
            raise HTTPException(500, f"Could not pause study timer: {exc}") from exc

    @router.post("/timer/finish")
    async def finish_timer(request: Request):
        owner = owner_for(request)
        try:
            return finish_study_timer(owner)
        except Exception as exc:
            raise HTTPException(500, f"Could not finish study timer: {exc}") from exc

    return router
