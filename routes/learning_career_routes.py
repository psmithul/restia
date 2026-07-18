"""Owner-scoped HTTP API for typed V3 Learning & Career records."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.database import SessionLocal
from src.identity import request_account_transaction
from src.learning_career_service import (
    career_learning_plan,
    create_learning_career_record,
    delete_learning_career_record,
    get_learning_career_record,
    learning_career_history,
    list_learning_career_records,
    search_learning_career_records,
    serialize_learning_career_record,
    update_learning_career_record,
)
from src.life_graph import LifeGraphConflict, LifeGraphError, LifeGraphNotFound


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LearningCareerSourceLink(_StrictModel):
    source_id: str = Field(max_length=36)
    relation: str = Field(default="supports", max_length=64)
    label: str = Field(default="", max_length=240)
    locator: str = Field(default="", max_length=1_000)


class LearningCareerEntityLink(_StrictModel):
    entity_id: str = Field(max_length=36)
    relation: str = Field(max_length=64)
    label: str = Field(default="", max_length=240)


class LearningCareerWeeklyAction(_StrictModel):
    week_start: date
    definition_of_done: str = Field(max_length=2_000)
    estimated_minutes: int = Field(ge=1, le=10_080)
    priority: str = Field(default="normal", max_length=16)
    status: str = Field(default="planned", max_length=24)


class LearningCareerRecordCreate(_StrictModel):
    domain: str = Field(max_length=16)
    record_kind: str = Field(max_length=48)
    title: str = Field(max_length=240)
    summary: str = Field(default="", max_length=20_000)
    details: dict[str, Any] = Field(default_factory=dict)
    source_links: list[LearningCareerSourceLink] = Field(min_length=1, max_length=20)
    entity_links: list[LearningCareerEntityLink] = Field(
        default_factory=list, max_length=50
    )
    weekly_action: LearningCareerWeeklyAction | None = None
    status: str = Field(default="active", max_length=32)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    occurred_at: datetime | None = None
    due_at: datetime | None = None
    review_at: datetime | None = None
    idempotency_key: str | None = Field(default=None, max_length=256)


class LearningCareerRecordUpdate(_StrictModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    summary: str | None = Field(default=None, max_length=20_000)
    details: dict[str, Any] | None = None
    source_links: list[LearningCareerSourceLink] | None = Field(
        default=None, min_length=1, max_length=20
    )
    entity_links: list[LearningCareerEntityLink] | None = Field(
        default=None, max_length=50
    )
    weekly_action: LearningCareerWeeklyAction | None = None
    status: str | None = Field(default=None, max_length=32)
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)
    occurred_at: datetime | None = None
    due_at: datetime | None = None
    review_at: datetime | None = None


class LearningCareerRecordDelete(_StrictModel):
    version: int = Field(ge=1)
    reason: str = Field(default="Learning/Career record deleted", max_length=500)


def _model_dict(value: BaseModel | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return value.model_dump(exclude_unset=False)


def _model_list(values: list[BaseModel] | None) -> list[dict[str, Any]] | None:
    if values is None:
        return None
    return [_model_dict(value) or {} for value in values]


def _fields_set(value: BaseModel) -> set[str]:
    return set(value.model_fields_set)


def _raise_domain_error(exc: LifeGraphError) -> None:
    if isinstance(exc, LifeGraphNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, LifeGraphConflict):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


def setup_learning_career_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(
        prefix="/api/life/learning-career", tags=["life-learning-career"]
    )

    @router.post("/records", status_code=201)
    def create_record(
        request: Request, body: LearningCareerRecordCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_learning_career_record(
                    db,
                    account=account,
                    domain=body.domain,
                    record_kind=body.record_kind,
                    title=body.title,
                    summary=body.summary,
                    details=body.details,
                    source_links=_model_list(body.source_links),
                    entity_links=_model_list(body.entity_links),
                    weekly_action=_model_dict(body.weekly_action),
                    status=body.status,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    occurred_at=body.occurred_at,
                    due_at=body.due_at,
                    review_at=body.review_at,
                    idempotency_key=body.idempotency_key,
                )
                return {
                    "record": serialize_learning_career_record(entity),
                    "created": created,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/records")
    def list_records(
        request: Request,
        domain: str | None = Query(default=None, max_length=16),
        record_kind: str | None = Query(default=None, max_length=48),
        status: str | None = Query(default=None, max_length=32),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_learning_career_records(
                    db,
                    owner_id=account.id,
                    domain=domain,
                    record_kind=record_kind,
                    status=status,
                    limit=limit,
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/records/search")
    def search_records(
        request: Request,
        q: str = Query(min_length=1, max_length=500),
        domain: str | None = Query(default=None, max_length=16),
        record_kind: str | None = Query(default=None, max_length=48),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "items": [], "count": 0, "scanned": 0,
                        "truncated": False,
                    }
                return search_learning_career_records(
                    db,
                    owner_id=account.id,
                    query_text=q,
                    domain=domain,
                    record_kind=record_kind,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/career-plan/{career_target_id}")
    def career_plan(
        request: Request,
        career_target_id: str,
        week_start: date = Query(),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Learning/Career record not found")
                return career_learning_plan(
                    db,
                    owner_id=account.id,
                    career_target_id=career_target_id,
                    week_start=week_start,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/records/{record_id}/history")
    def history(
        request: Request,
        record_id: str,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Learning/Career record not found")
                items, truncated = learning_career_history(
                    db, owner_id=account.id, entity_id=record_id, limit=limit
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/records/{record_id}")
    def get_record(request: Request, record_id: str) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Learning/Career record not found")
                return {
                    "record": get_learning_career_record(
                        db, owner_id=account.id, entity_id=record_id
                    )
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.patch("/records/{record_id}")
    def update_record(
        request: Request, record_id: str, body: LearningCareerRecordUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body) - {"version"}
        changes: dict[str, Any] = {}
        for field in fields:
            value = getattr(body, field)
            if field == "source_links":
                value = _model_list(value)
            elif field == "entity_links":
                value = _model_list(value)
            elif field == "weekly_action":
                value = _model_dict(value)
            changes[field] = value
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_learning_career_record(
                    db,
                    account=account,
                    entity_id=record_id,
                    expected_version=body.version,
                    changes=changes,
                )
                return {"record": serialize_learning_career_record(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/records/{record_id}")
    def delete_record(
        request: Request, record_id: str, body: LearningCareerRecordDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = delete_learning_career_record(
                    db,
                    owner_id=account.id,
                    entity_id=record_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {"record": serialize_learning_career_record(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router


__all__ = ["setup_learning_career_routes"]
