"""Owner-scoped HTTP API for typed V3 Work and Business workspaces."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.database import SessionLocal
from src.identity import request_account_transaction
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    serialize_entity_link,
)
from src.work_business_service import (
    create_cross_workspace_relation,
    create_work_business_record,
    create_work_business_workspace,
    delete_cross_workspace_relation,
    delete_work_business_record,
    delete_work_business_workspace,
    get_work_business_record,
    get_work_business_workspace,
    list_cross_workspace_relations,
    list_work_business_records,
    list_work_business_workspaces,
    record_history,
    search_work_business_records,
    serialize_work_business_record,
    serialize_work_business_workspace,
    update_work_business_record,
    update_work_business_workspace,
    workspace_history,
    workspace_summary,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkBusinessSourceLink(_StrictModel):
    source_id: str = Field(max_length=36)
    relation: str = Field(default="supports", max_length=64)
    label: str = Field(default="", max_length=240)
    locator: str = Field(default="", max_length=1_000)


class WorkBusinessWorkspaceCreate(_StrictModel):
    workspace_kind: str = Field(max_length=16)
    title: str = Field(max_length=240)
    purpose: str = Field(default="", max_length=5_000)
    details: dict[str, Any] = Field(default_factory=dict)
    source_links: list[WorkBusinessSourceLink] = Field(min_length=1, max_length=20)
    status: str = Field(default="active", max_length=32)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    review_at: datetime | None = None
    idempotency_key: str | None = Field(default=None, max_length=256)


class WorkBusinessWorkspaceUpdate(_StrictModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    purpose: str | None = Field(default=None, max_length=5_000)
    details: dict[str, Any] | None = None
    source_links: list[WorkBusinessSourceLink] | None = Field(
        default=None, min_length=1, max_length=20
    )
    status: str | None = Field(default=None, max_length=32)
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)
    review_at: datetime | None = None


class WorkBusinessRecordCreate(_StrictModel):
    record_kind: str = Field(max_length=48)
    title: str = Field(max_length=240)
    summary: str = Field(default="", max_length=20_000)
    details: dict[str, Any] = Field(default_factory=dict)
    source_links: list[WorkBusinessSourceLink] = Field(min_length=1, max_length=20)
    status: str = Field(default="active", max_length=32)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    occurred_at: datetime | None = None
    due_at: datetime | None = None
    review_at: datetime | None = None
    idempotency_key: str | None = Field(default=None, max_length=256)


class WorkBusinessRecordUpdate(_StrictModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    summary: str | None = Field(default=None, max_length=20_000)
    details: dict[str, Any] | None = None
    source_links: list[WorkBusinessSourceLink] | None = Field(
        default=None, min_length=1, max_length=20
    )
    status: str | None = Field(default=None, max_length=32)
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)
    occurred_at: datetime | None = None
    due_at: datetime | None = None
    review_at: datetime | None = None


class CrossWorkspaceRelationCreate(_StrictModel):
    source_workspace_id: str = Field(max_length=36)
    source_record_id: str = Field(max_length=36)
    relation: str = Field(max_length=64)
    target_workspace_id: str = Field(max_length=36)
    target_record_id: str = Field(max_length=36)
    context: dict[str, Any] = Field(default_factory=dict)
    source_links: list[WorkBusinessSourceLink] = Field(min_length=1, max_length=20)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)


class VersionedDelete(_StrictModel):
    version: int = Field(ge=1)
    reason: str = Field(default="Work/Business record deleted", max_length=500)


def _model_dict(value: BaseModel | None) -> dict[str, Any] | None:
    if value is None:
        return None
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        return dumper()
    return value.dict()


def _model_list(values: list[BaseModel] | None) -> list[dict[str, Any]] | None:
    if values is None:
        return None
    return [_model_dict(value) or {} for value in values]


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


def setup_work_business_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(
        prefix="/api/life/work-business", tags=["life-work-business"]
    )

    @router.post("/workspaces", status_code=201)
    def create_workspace(
        request: Request, body: WorkBusinessWorkspaceCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_work_business_workspace(
                    db,
                    account=account,
                    workspace_kind=body.workspace_kind,
                    title=body.title,
                    purpose=body.purpose,
                    details=body.details,
                    source_links=_model_list(body.source_links),
                    status=body.status,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    review_at=body.review_at,
                    idempotency_key=body.idempotency_key,
                )
                return {
                    "workspace": serialize_work_business_workspace(entity),
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

    @router.get("/workspaces")
    def list_workspaces(
        request: Request,
        workspace_kind: str | None = Query(default=None, max_length=16),
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
                rows, truncated = list_work_business_workspaces(
                    db,
                    owner_id=account.id,
                    workspace_kind=workspace_kind,
                    status=status,
                    limit=limit,
                )
                return {"items": rows, "count": len(rows), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/relations", status_code=201)
    def create_relation(
        request: Request, body: CrossWorkspaceRelationCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                link, created = create_cross_workspace_relation(
                    db,
                    account=account,
                    source_workspace_id=body.source_workspace_id,
                    source_record_id=body.source_record_id,
                    relation=body.relation,
                    target_workspace_id=body.target_workspace_id,
                    target_record_id=body.target_record_id,
                    context=body.context,
                    source_links=_model_list(body.source_links),
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                )
                return {"relation": serialize_entity_link(link), "created": created}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/relations")
    def list_relations(
        request: Request,
        workspace_id: str = Query(max_length=36),
        direction: str = Query(default="both", max_length=16),
        relation: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                rows, truncated = list_cross_workspace_relations(
                    db,
                    owner_id=account.id,
                    workspace_id=workspace_id,
                    direction=direction,
                    relation=relation,
                    limit=limit,
                )
                return {"items": rows, "count": len(rows), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.delete("/relations/{relation_id}")
    def remove_relation(
        request: Request, relation_id: str, body: VersionedDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                link = delete_cross_workspace_relation(
                    db,
                    owner_id=account.id,
                    relation_id=relation_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {"relation": serialize_entity_link(link)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/workspaces/{workspace_id}/records", status_code=201)
    def create_record(
        request: Request, workspace_id: str, body: WorkBusinessRecordCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_work_business_record(
                    db,
                    account=account,
                    workspace_id=workspace_id,
                    record_kind=body.record_kind,
                    title=body.title,
                    summary=body.summary,
                    details=body.details,
                    source_links=_model_list(body.source_links),
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
                    "record": serialize_work_business_record(entity),
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

    @router.get("/workspaces/{workspace_id}/records")
    def list_records(
        request: Request,
        workspace_id: str,
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
                rows, truncated = list_work_business_records(
                    db,
                    owner_id=account.id,
                    workspace_id=workspace_id,
                    record_kind=record_kind,
                    status=status,
                    limit=limit,
                )
                return {"items": rows, "count": len(rows), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/workspaces/{workspace_id}/records/search")
    def search_records(
        request: Request,
        workspace_id: str,
        q: str = Query(min_length=1, max_length=500),
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
                return search_work_business_records(
                    db,
                    owner_id=account.id,
                    workspace_id=workspace_id,
                    query_text=q,
                    record_kind=record_kind,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/workspaces/{workspace_id}/summary")
    def summary(request: Request, workspace_id: str) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Work/Business workspace not found")
                return workspace_summary(
                    db, owner_id=account.id, workspace_id=workspace_id
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/workspaces/{workspace_id}/history")
    def workspace_versions(
        request: Request,
        workspace_id: str,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Work/Business workspace not found")
                rows, truncated = workspace_history(
                    db, owner_id=account.id, workspace_id=workspace_id, limit=limit
                )
                return {"items": rows, "count": len(rows), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/workspaces/{workspace_id}/records/{record_id}/history")
    def record_versions(
        request: Request,
        workspace_id: str,
        record_id: str,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Work/Business record not found")
                rows, truncated = record_history(
                    db,
                    owner_id=account.id,
                    workspace_id=workspace_id,
                    record_id=record_id,
                    limit=limit,
                )
                return {"items": rows, "count": len(rows), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/workspaces/{workspace_id}/records/{record_id}")
    def get_record(
        request: Request, workspace_id: str, record_id: str
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Work/Business record not found")
                return {"record": get_work_business_record(
                    db,
                    owner_id=account.id,
                    workspace_id=workspace_id,
                    record_id=record_id,
                )}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.patch("/workspaces/{workspace_id}/records/{record_id}")
    def update_record(
        request: Request,
        workspace_id: str,
        record_id: str,
        body: WorkBusinessRecordUpdate,
    ) -> dict[str, Any]:
        fields = _fields_set(body)
        changes = {
            field: (
                _model_list(body.source_links)
                if field == "source_links"
                else getattr(body, field)
            )
            for field in (
                "title", "summary", "details", "source_links", "status",
                "provenance", "confidence", "sensitivity", "occurred_at",
                "due_at", "review_at",
            )
            if field in fields
        }
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_work_business_record(
                    db,
                    account=account,
                    workspace_id=workspace_id,
                    record_id=record_id,
                    expected_version=body.version,
                    changes=changes,
                )
                return {"record": serialize_work_business_record(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/workspaces/{workspace_id}/records/{record_id}")
    def remove_record(
        request: Request,
        workspace_id: str,
        record_id: str,
        body: VersionedDelete,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = delete_work_business_record(
                    db,
                    owner_id=account.id,
                    workspace_id=workspace_id,
                    record_id=record_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {"record": serialize_work_business_record(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/workspaces/{workspace_id}")
    def get_workspace(request: Request, workspace_id: str) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Work/Business workspace not found")
                return {"workspace": get_work_business_workspace(
                    db, owner_id=account.id, workspace_id=workspace_id
                )}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.patch("/workspaces/{workspace_id}")
    def update_workspace(
        request: Request, workspace_id: str, body: WorkBusinessWorkspaceUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body)
        changes = {
            field: (
                _model_list(body.source_links)
                if field == "source_links"
                else getattr(body, field)
            )
            for field in (
                "title", "purpose", "details", "source_links", "status",
                "provenance", "confidence", "sensitivity", "review_at",
            )
            if field in fields
        }
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_work_business_workspace(
                    db,
                    account=account,
                    workspace_id=workspace_id,
                    expected_version=body.version,
                    changes=changes,
                )
                return {"workspace": serialize_work_business_workspace(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/workspaces/{workspace_id}")
    def remove_workspace(
        request: Request, workspace_id: str, body: VersionedDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = delete_work_business_workspace(
                    db,
                    owner_id=account.id,
                    workspace_id=workspace_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {"workspace": serialize_work_business_workspace(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router
