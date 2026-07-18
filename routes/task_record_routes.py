"""Strict owner-scoped API for V3 human tasks.

This router is intentionally separate from ``/api/tasks``: that older surface
manages recurring background automations, while these records describe work a
person intends to complete in the canonical Life graph.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.database import SessionLocal
from src.identity import request_account_transaction
from src.life_graph import LifeGraphConflict, LifeGraphError, LifeGraphNotFound
from src.task_record_service import (
    create_task_record,
    delete_task_record,
    get_task_record,
    list_task_records,
    search_task_records,
    serialize_task_record,
    task_record_history,
    update_task_record,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskRecordCreate(_StrictModel):
    title: str = Field(min_length=1, max_length=240)
    definition_of_done: str = Field(min_length=1, max_length=4_000)
    effort_minutes: int = Field(ge=1, le=10_080)
    priority: str = "normal"
    deadline: datetime | None = None
    energy: str = "any"
    contexts: list[str] = Field(default_factory=list, max_length=20)
    project_id: str | None = Field(default=None, max_length=64)
    people_ids: list[str] = Field(default_factory=list, max_length=50)
    dependency_ids: list[str] = Field(default_factory=list, max_length=50)
    document_ids: list[str] = Field(default_factory=list, max_length=50)
    source: dict[str, Any] | None = None
    status: str = "active"
    next_action: str | None = Field(default=None, max_length=1_000)
    waiting_on: str | None = Field(default=None, max_length=1_000)
    completion_evidence: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    completed_at: datetime | None = None
    note: str = Field(default="", max_length=20_000)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = "private"
    idempotency_key: str | None = Field(default=None, max_length=256)


class TaskRecordUpdate(_StrictModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    definition_of_done: str | None = Field(default=None, min_length=1, max_length=4_000)
    effort_minutes: int | None = Field(default=None, ge=1, le=10_080)
    priority: str | None = None
    deadline: datetime | None = None
    energy: str | None = None
    contexts: list[str] | None = Field(default=None, max_length=20)
    project_id: str | None = Field(default=None, max_length=64)
    people_ids: list[str] | None = Field(default=None, max_length=50)
    dependency_ids: list[str] | None = Field(default=None, max_length=50)
    document_ids: list[str] | None = Field(default=None, max_length=50)
    source: dict[str, Any] | None = None
    status: str | None = None
    next_action: str | None = Field(default=None, max_length=1_000)
    waiting_on: str | None = Field(default=None, max_length=1_000)
    completion_evidence: list[dict[str, Any]] | None = Field(default=None, max_length=30)
    completed_at: datetime | None = None
    note: str | None = Field(default=None, max_length=20_000)
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = None


class TaskRecordDelete(_StrictModel):
    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1_000)


def _fields_set(model: BaseModel) -> set[str]:
    fields = getattr(model, "model_fields_set", None)
    if fields is None:
        fields = getattr(model, "__fields_set__", set())
    return set(fields)


def _model_dump(model: BaseModel) -> dict[str, Any]:
    dumper = getattr(model, "model_dump", None)
    return dict(dumper() if callable(dumper) else model.dict())


def _raise_domain_error(exc: LifeGraphError) -> None:
    if isinstance(exc, LifeGraphNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, LifeGraphConflict):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


def setup_task_record_routes(
    *, session_factory: Callable[[], Any] = SessionLocal,
) -> APIRouter:
    router = APIRouter(prefix="/api/life/tasks", tags=["life-tasks"])

    @router.post("", status_code=201)
    def create_record(request: Request, body: TaskRecordCreate) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_task_record(
                    db, account=account, **_model_dump(body)
                )
                return {"task": serialize_task_record(entity), "created": created}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("")
    def list_records(
        request: Request,
        status: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_task_records(
                    db, owner_id=account.id, status=status, limit=limit
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/search")
    def search_records(
        request: Request,
        q: str = Query(min_length=1, max_length=500),
        status: str | None = Query(default=None),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                return search_task_records(
                    db, owner_id=account.id, query_text=q,
                    status=status, limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/{task_id}/history")
    def history(
        request: Request, task_id: str,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Task not found")
                items, truncated = task_record_history(
                    db, owner_id=account.id, entity_id=task_id, limit=limit
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/{task_id}")
    def get_record(request: Request, task_id: str) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Task not found")
                return {"task": get_task_record(
                    db, owner_id=account.id, entity_id=task_id
                )}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.patch("/{task_id}")
    def update_record(
        request: Request, task_id: str, body: TaskRecordUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body) - {"version"}
        changes = {field: getattr(body, field) for field in fields}
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_task_record(
                    db, account=account, entity_id=task_id,
                    expected_version=body.version, changes=changes,
                )
                return {"task": serialize_task_record(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/{task_id}")
    def delete_record(
        request: Request, task_id: str, body: TaskRecordDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = delete_task_record(
                    db, owner_id=account.id, entity_id=task_id,
                    expected_version=body.version, reason=body.reason,
                )
                return {"task": serialize_task_record(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router
