"""Owner-scoped HTTP API for typed V3 Travel records and Travel Mode."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.database import SessionLocal
from src.identity import request_account_transaction
from src.life_graph import LifeGraphConflict, LifeGraphError, LifeGraphNotFound
from src.travel_service import (
    create_travel_record,
    delete_travel_record,
    get_travel_record,
    list_travel_records,
    serialize_travel_record,
    travel_mode,
    travel_record_history,
    update_travel_record,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TravelRecordCreate(_StrictModel):
    record_kind: str = Field(max_length=64)
    title: str = Field(max_length=240)
    details: dict[str, Any]
    trip_id: str | None = Field(default=None, max_length=36)
    summary: str = Field(default="", max_length=20_000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    offline_available: bool | None = None
    related_entity_ids: list[str] = Field(default_factory=list, max_length=50)
    source_ids: list[str] = Field(default_factory=list, max_length=50)
    calendar_event_ids: list[str] = Field(default_factory=list, max_length=50)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    status: str = Field(default="planned", max_length=32)
    domain_ref_type: str | None = Field(default=None, max_length=48)
    domain_ref_id: str | None = Field(default=None, max_length=255)
    idempotency_key: str | None = Field(default=None, max_length=256)


class TravelRecordUpdate(_StrictModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    summary: str | None = Field(default=None, max_length=20_000)
    status: str | None = Field(default=None, max_length=32)
    trip_id: str | None = Field(default=None, max_length=36)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    offline_available: bool | None = None
    details: dict[str, Any] | None = None
    related_entity_ids: list[str] | None = Field(default=None, max_length=50)
    source_ids: list[str] | None = Field(default=None, max_length=50)
    calendar_event_ids: list[str] | None = Field(default=None, max_length=50)
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)


class TravelRecordDelete(_StrictModel):
    version: int = Field(ge=1)
    reason: str = Field(default="Travel record deleted", max_length=500)


def _fields_set(value: BaseModel) -> set[str]:
    fields = getattr(value, "model_fields_set", None)
    if fields is None:
        fields = getattr(value, "__fields_set__", set())
    return set(fields)


def _raise_domain_error(exc: LifeGraphError) -> None:
    if isinstance(exc, LifeGraphNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, LifeGraphConflict):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


def setup_travel_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(prefix="/api/life/travel", tags=["life-travel"])

    @router.get("/mode")
    def get_travel_mode(
        request: Request,
        as_of: datetime = Query(),
        offline_only: bool = Query(default=True),
        trip_limit: int = Query(default=3, ge=1, le=3),
        fact_limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                return travel_mode(
                    db,
                    owner_id=account.id if account is not None else "missing",
                    as_of=as_of,
                    offline_only=offline_only,
                    trip_limit=trip_limit,
                    fact_limit=fact_limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/records", status_code=201)
    def create_record(request: Request, body: TravelRecordCreate) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_travel_record(
                    db,
                    account=account,
                    record_kind=body.record_kind,
                    title=body.title,
                    details=body.details,
                    trip_id=body.trip_id,
                    summary=body.summary,
                    starts_at=body.starts_at,
                    ends_at=body.ends_at,
                    offline_available=body.offline_available,
                    related_entity_ids=body.related_entity_ids,
                    source_ids=body.source_ids,
                    calendar_event_ids=body.calendar_event_ids,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    status=body.status,
                    domain_ref_type=body.domain_ref_type,
                    domain_ref_id=body.domain_ref_id,
                    idempotency_key=body.idempotency_key,
                )
                return {"record": serialize_travel_record(entity), "created": created}
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
        record_kind: str | None = Query(default=None, max_length=64),
        trip_id: str | None = Query(default=None, max_length=36),
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
                items, truncated = list_travel_records(
                    db,
                    owner_id=account.id,
                    record_kind=record_kind,
                    trip_id=trip_id,
                    status=status,
                    limit=limit,
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/records/{record_id}/history")
    def record_history(
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
                    raise LifeGraphNotFound("Travel record not found")
                items, truncated = travel_record_history(
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
                    raise LifeGraphNotFound("Travel record not found")
                return {
                    "record": get_travel_record(
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
        request: Request, record_id: str, body: TravelRecordUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body) - {"version"}
        changes = {field: getattr(body, field) for field in fields}
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_travel_record(
                    db,
                    account=account,
                    entity_id=record_id,
                    expected_version=body.version,
                    changes=changes,
                )
                return {"record": serialize_travel_record(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/records/{record_id}")
    def remove_record(
        request: Request, record_id: str, body: TravelRecordDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = delete_travel_record(
                    db,
                    owner_id=account.id,
                    entity_id=record_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {"record": serialize_travel_record(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router
