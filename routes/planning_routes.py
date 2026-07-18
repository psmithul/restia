"""Owner-scoped CRUD and scheduling API for Restia V2 planning items."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.database import SessionLocal
from src.auth_helpers import require_user, resolved_request_owner
from src.identity import request_account_transaction
from src.planning import (
    PlanningConflict,
    PlanningError,
    PlanningNotFound,
    complete_planning_item,
    create_planning_item,
    list_planning_items,
    reopen_planning_item,
    schedule_planning_item,
    serialize_planning_item,
    update_planning_item,
)


class PlanningCreate(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    details: str = Field(default="", max_length=20_000)
    priority: str = "normal"
    due_date: str | None = None


class PlanningUpdate(BaseModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    details: str | None = Field(default=None, max_length=20_000)
    priority: str | None = None
    due_date: str | None = None


class PlanningVersion(BaseModel):
    version: int = Field(ge=1)


class PlanningSchedule(PlanningVersion):
    start: datetime
    end: datetime | None = None
    due_date: str | None = None
    add_to_calendar: bool = True
    calendar_id: str | None = None


def _owner(request: Request) -> str:
    admitted = require_user(request)
    return resolved_request_owner(request, admitted_user=admitted)


def _raise_domain_error(exc: PlanningError) -> None:
    if isinstance(exc, PlanningNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, PlanningConflict):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


def setup_planning_routes(
    *, session_factory: Callable[[], Any] = SessionLocal,
) -> APIRouter:
    router = APIRouter(prefix="/api/planning", tags=["planning"])

    @router.get("")
    def list_items(
        request: Request,
        status: str = Query(default="all", pattern="^(all|open|completed)$"),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(db, request, write=False) as account:
                owner = account.username if account is not None else _owner(request)
                items, truncated = list_planning_items(
                    db, owner=owner, status=status, limit=limit
                )
                return {
                    "items": [serialize_planning_item(item) for item in items],
                    "count": len(items),
                    "truncated": truncated,
                }
        except PlanningError as exc:
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("", status_code=201)
    def create_item(request: Request, body: PlanningCreate) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(db, request, write=True) as account:
                item = create_planning_item(
                    db,
                    owner=account.username,
                    title=body.title,
                    details=body.details,
                    priority=body.priority,
                    due_date=body.due_date,
                )
                db.flush()
                return serialize_planning_item(item)
        except PlanningError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.patch("/{item_id}")
    def update_item(
        request: Request, item_id: str, body: PlanningUpdate
    ) -> dict[str, Any]:
        fields = getattr(body, "model_fields_set", None)
        if fields is None:
            fields = getattr(body, "__fields_set__", set())
        kwargs: dict[str, Any] = {}
        for field in ("title", "details", "priority", "due_date"):
            if field in fields:
                kwargs[field] = getattr(body, field)
        db = session_factory()
        try:
            with request_account_transaction(db, request, write=True) as account:
                item = update_planning_item(
                    db,
                    owner=account.username,
                    account=account,
                    item_id=item_id,
                    expected_version=body.version,
                    **kwargs,
                )
                db.flush()
                return serialize_planning_item(item)
        except PlanningError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/{item_id}/complete")
    def complete_item(
        request: Request, item_id: str, body: PlanningVersion
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(db, request, write=True) as account:
                item = complete_planning_item(
                    db,
                    owner=account.username,
                    item_id=item_id,
                    expected_version=body.version,
                )
                db.flush()
                return serialize_planning_item(item)
        except PlanningError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/{item_id}/reopen")
    def reopen_item(
        request: Request, item_id: str, body: PlanningVersion
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(db, request, write=True) as account:
                item = reopen_planning_item(
                    db,
                    owner=account.username,
                    item_id=item_id,
                    expected_version=body.version,
                )
                db.flush()
                return serialize_planning_item(item)
        except PlanningError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/{item_id}/schedule")
    def schedule_item(
        request: Request, item_id: str, body: PlanningSchedule
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(db, request, write=True) as account:
                item = schedule_planning_item(
                    db,
                    owner=account.username,
                    account=account,
                    item_id=item_id,
                    expected_version=body.version,
                    start=body.start,
                    end=body.end,
                    due_date=body.due_date,
                    add_to_calendar=body.add_to_calendar,
                    calendar_id=body.calendar_id,
                )
                db.flush()
                return serialize_planning_item(item)
        except PlanningError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router
