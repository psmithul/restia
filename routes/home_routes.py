"""Owner-scoped HTTP API for V3 Home and Personal Administration records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.database import SessionLocal
from src.home_service import (
    HOME_ALERT_MAX_HORIZON_DAYS,
    create_home_record,
    delete_home_record,
    get_home_record,
    home_alert_report,
    home_record_history,
    list_home_records,
    search_home_records,
    serialize_home_record,
    update_home_record,
)
from src.identity import request_account_transaction
from src.life_graph import LifeGraphConflict, LifeGraphError, LifeGraphNotFound


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HomeSourceInput(_StrictModel):
    kind: str = Field(max_length=64)
    label: str = Field(max_length=240)
    source_id: str | None = Field(default=None, max_length=255)
    reference: str | None = Field(default=None, max_length=1_000)
    observed_at: datetime | None = None


class HomeReferencesInput(_StrictModel):
    file_entity_ids: list[str] = Field(default_factory=list, max_length=20)
    document_ids: list[str] = Field(default_factory=list, max_length=20)
    entity_ids: list[str] = Field(default_factory=list, max_length=50)


class HomeRecordCreate(_StrictModel):
    record_type: str = Field(max_length=64)
    title: str = Field(max_length=240)
    effective_at: datetime
    expires_at: datetime | None = None
    due_at: datetime | None = None
    details: dict[str, Any]
    references: HomeReferencesInput = Field(default_factory=HomeReferencesInput)
    source: HomeSourceInput
    note: str = Field(default="", max_length=20_000)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    idempotency_key: str | None = Field(default=None, max_length=256)


class HomeRecordUpdate(_StrictModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    effective_at: datetime | None = None
    expires_at: datetime | None = None
    due_at: datetime | None = None
    details: dict[str, Any] | None = None
    references: HomeReferencesInput | None = None
    source: HomeSourceInput | None = None
    note: str | None = Field(default=None, max_length=20_000)
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)
    status: str | None = Field(default=None, max_length=32)


class VersionedDelete(_StrictModel):
    version: int = Field(ge=1)
    reason: str = Field(default="Home record deleted", max_length=500)


def _model_dict(value: BaseModel | None) -> dict[str, Any] | None:
    if value is None:
        return None
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        return dumper()
    return value.dict()


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


def setup_home_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(prefix="/api/life/home", tags=["life-home"])

    @router.get("/alerts")
    def alerts(
        request: Request,
        as_of: datetime = Query(),
        horizon_days: int = Query(default=90, ge=0, le=HOME_ALERT_MAX_HORIZON_DAYS),
        include_overdue: bool = Query(default=True),
        record_type: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                return home_alert_report(
                    db,
                    owner_id=account.id if account is not None else "missing",
                    as_of=as_of,
                    horizon_days=horizon_days,
                    include_overdue=include_overdue,
                    record_type=record_type,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/search")
    def search(
        request: Request,
        q: str = Query(min_length=1, max_length=500),
        record_type: str | None = Query(default=None, max_length=64),
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
                return search_home_records(
                    db,
                    owner_id=account.id,
                    query_text=q,
                    record_type=record_type,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/records", status_code=201)
    def create_record(
        request: Request, body: HomeRecordCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_home_record(
                    db,
                    account=account,
                    record_type=body.record_type,
                    title=body.title,
                    effective_at=body.effective_at,
                    expires_at=body.expires_at,
                    due_at=body.due_at,
                    details=body.details,
                    references=_model_dict(body.references),
                    source=_model_dict(body.source),
                    note=body.note,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    idempotency_key=body.idempotency_key,
                )
                return {
                    "record": serialize_home_record(entity),
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
        record_type: str | None = Query(default=None, max_length=64),
        status: str | None = Query(default=None, max_length=32),
        include_archived: bool = Query(default=False),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_home_records(
                    db,
                    owner_id=account.id,
                    record_type=record_type,
                    status=status,
                    include_archived=include_archived,
                    limit=limit,
                )
                return {
                    "items": items,
                    "count": len(items),
                    "truncated": truncated,
                }
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
                    raise LifeGraphNotFound("Home record not found")
                items, truncated = home_record_history(
                    db,
                    owner_id=account.id,
                    entity_id=record_id,
                    limit=limit,
                )
                return {
                    "items": items,
                    "count": len(items),
                    "truncated": truncated,
                }
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
                    raise LifeGraphNotFound("Home record not found")
                return {
                    "record": get_home_record(
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
        request: Request, record_id: str, body: HomeRecordUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body) - {"version"}
        changes: dict[str, Any] = {}
        for field in fields:
            value = getattr(body, field)
            if field in {"source", "references"}:
                value = _model_dict(value)
            changes[field] = value
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_home_record(
                    db,
                    account=account,
                    entity_id=record_id,
                    expected_version=body.version,
                    changes=changes,
                )
                return {"record": serialize_home_record(entity)}
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
        request: Request, record_id: str, body: VersionedDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = delete_home_record(
                    db,
                    owner_id=account.id,
                    entity_id=record_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {"record": serialize_home_record(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router


__all__ = ["setup_home_routes"]
