"""Owner-scoped Study Mode goal and focus-timer API."""

import logging
from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from core.database import Session as DbSession, SessionLocal
from src.auth_helpers import effective_owner, require_user
from src.study_mode import (
    StudyGoalConflictError,
    StudyGoalRequiredError,
    StudyWorkspaceNotFoundError,
    finish_study_timer,
    get_study_state,
    initialize_study_workspace,
    pause_study_timer,
    record_study_review,
    save_study_goal,
    start_study_timer,
    study_state_with_tracker,
)


logger = logging.getLogger(__name__)


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


class StudyInitializeRequest(BaseModel):
    # Empty means "activate this workspace now". The first substantive prompt
    # may arrive here or through either chat endpoint; both share one atomic
    # initializer and therefore cannot overwrite a goal twice.
    prompt: str = Field(default="", max_length=10_000)


def setup_study_routes() -> APIRouter:
    router = APIRouter(prefix="/api/study", tags=["study"])

    def internal_error(operation: str, session_id: str, exc: Exception) -> HTTPException:
        """Log diagnostics server-side without returning internals to clients."""

        logger.exception(
            "Study %s failed for session %s: %s", operation, session_id, exc
        )
        return HTTPException(500, f"Could not {operation}.")

    def workspace_for(request: Request, session_id: str) -> tuple[Optional[str], dict]:
        # Keep the route fail-closed if auth middleware is ever bypassed while
        # preserving Restia's explicit auth-disabled / localhost modes.
        authenticated_owner = str(require_user(request) or "").strip() or None
        resolved_owner = str(effective_owner(request) or "").strip() or None
        db = SessionLocal()
        try:
            row = db.query(
                DbSession.id,
                DbSession.owner,
                DbSession.mode,
                DbSession.name,
            ).filter(
                DbSession.id == session_id
            ).first()
        finally:
            db.close()
        if row is None:
            raise HTTPException(404, f"Study session {session_id} not found")
        # Cookie-authenticated requests require an exact private owner. In an
        # explicit single-user/local mode, legacy unowned sessions remain
        # accessible, but an owned row must still match the resolved operator.
        if authenticated_owner:
            owner_matches = row.owner == authenticated_owner
        else:
            owner_matches = row.owner is None or (
                resolved_owner is not None and row.owner == resolved_owner
            )
        if not owner_matches:
            # Keep the same non-enumerating behavior as the session routes.
            raise HTTPException(404, f"Study session {session_id} not found")
        if str(row.mode or "").lower() != "study":
            raise HTTPException(409, "Selected chat is not a Study workspace")
        return row.owner, {
            "session_id": row.id,
            "title": row.name or "Study workspace",
            "mode": "study",
        }

    def tracked(state: dict, workspace: dict) -> dict:
        return study_state_with_tracker(state, workspace["title"])

    @router.get("/state")
    async def state(
        request: Request,
        session_id: str = Query(..., min_length=1, max_length=200),
    ):
        owner, workspace = workspace_for(request, session_id)
        try:
            return tracked(get_study_state(owner, session_id), workspace)
        except Exception as exc:
            raise internal_error("load Study workspace state", session_id, exc) from exc

    @router.post("/initialize")
    async def initialize(
        payload: StudyInitializeRequest,
        request: Request,
        session_id: str = Query(..., min_length=1, max_length=200),
    ):
        owner, _workspace = workspace_for(request, session_id)
        try:
            return initialize_study_workspace(owner, session_id, payload.prompt)
        except StudyWorkspaceNotFoundError as exc:
            # The session may have been deleted between authorization and the
            # initialization transaction. Preserve the non-enumerating 404.
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, f"Could not initialize Study workspace: {exc}") from exc
        except Exception as exc:
            raise internal_error("initialize Study workspace", session_id, exc) from exc

    @router.put("/goal")
    async def update_goal(
        payload: StudyGoalRequest,
        request: Request,
        session_id: str = Query(..., min_length=1, max_length=200),
    ):
        owner, workspace = workspace_for(request, session_id)
        try:
            return tracked(
                save_study_goal(
                    owner,
                    session_id,
                    payload.goal_text,
                    payload.target_minutes,
                    payload.target_date,
                    reset_progress=payload.reset_progress,
                ),
                workspace,
            )
        except StudyGoalConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except Exception as exc:
            raise internal_error("save Study goal", session_id, exc) from exc

    @router.post("/timer/start")
    async def start_timer(
        request: Request,
        session_id: str = Query(..., min_length=1, max_length=200),
    ):
        owner, workspace = workspace_for(request, session_id)
        try:
            return tracked(start_study_timer(owner, session_id), workspace)
        except StudyGoalRequiredError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise internal_error("start Study timer", session_id, exc) from exc

    @router.post("/timer/pause")
    async def pause_timer(
        request: Request,
        session_id: str = Query(..., min_length=1, max_length=200),
    ):
        owner, workspace = workspace_for(request, session_id)
        try:
            return tracked(pause_study_timer(owner, session_id), workspace)
        except Exception as exc:
            raise internal_error("pause Study timer", session_id, exc) from exc

    @router.post("/timer/finish")
    async def finish_timer(
        request: Request,
        session_id: str = Query(..., min_length=1, max_length=200),
    ):
        owner, workspace = workspace_for(request, session_id)
        try:
            return tracked(finish_study_timer(owner, session_id), workspace)
        except Exception as exc:
            raise internal_error("finish Study timer", session_id, exc) from exc

    @router.post("/review")
    async def record_review(
        payload: StudyReviewRequest,
        request: Request,
        session_id: str = Query(..., min_length=1, max_length=200),
    ):
        owner, workspace = workspace_for(request, session_id)
        try:
            return tracked(record_study_review(owner, session_id, payload.outcome), workspace)
        except ValueError as exc:
            # Keep direct callers and future payload adapters fail-loud even
            # though the public request model already validates exact values.
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise internal_error("record Study review", session_id, exc) from exc

    return router
