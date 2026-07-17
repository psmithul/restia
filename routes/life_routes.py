"""Principal-scoped API for Restia's cross-domain life graph."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.database import SessionLocal
from src.identity import request_account_transaction
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    create_entity_link,
    create_life_entity,
    create_life_source,
    delete_entity_link,
    delete_life_entity,
    get_life_entity,
    list_decisions_for_review,
    list_entity_links,
    list_life_entities,
    list_life_entity_versions,
    list_life_sources,
    search_life_entities,
    serialize_entity_link,
    serialize_life_entity,
    serialize_life_entity_version,
    serialize_life_source,
    task_quality_report,
    traverse_life_graph,
    update_life_entity,
)


class LifeSourceCreate(BaseModel):
    source_type: str = Field(max_length=48)
    title: str = Field(default="", max_length=240)
    source_ref: str | None = Field(default=None, max_length=2_000)
    safe_excerpt: str = Field(default="", max_length=4_000)
    content_sha256: str | None = Field(default=None, max_length=64)
    observed_at: datetime | None = None
    sensitivity: str = Field(default="private", max_length=24)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=256)


class LifeEntityCreate(BaseModel):
    entity_type: str = Field(max_length=48)
    title: str = Field(max_length=240)
    summary: str = Field(default="", max_length=20_000)
    status: str = Field(default="active", max_length=32)
    properties: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    domain_ref_type: str | None = Field(default=None, max_length=48)
    domain_ref_id: str | None = Field(default=None, max_length=255)
    occurred_at: datetime | None = None
    due_at: datetime | None = None
    review_at: datetime | None = None
    idempotency_key: str | None = Field(default=None, max_length=256)
    reason: str = Field(default="Life entity created", max_length=500)


class LifeEntityUpdate(BaseModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    summary: str | None = Field(default=None, max_length=20_000)
    status: str | None = Field(default=None, max_length=32)
    properties: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)
    occurred_at: datetime | None = None
    due_at: datetime | None = None
    review_at: datetime | None = None
    reason: str = Field(default="Life entity updated", max_length=500)


class VersionedDelete(BaseModel):
    version: int = Field(ge=1)
    reason: str = Field(default="Life entity deleted", max_length=500)


class EntityLinkCreate(BaseModel):
    source_id: str = Field(max_length=36)
    relation: str = Field(max_length=64)
    target_id: str = Field(max_length=36)
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    reason: str = Field(default="Life entities linked", max_length=500)


class EntityLinkDelete(BaseModel):
    version: int = Field(ge=1)
    reason: str = Field(default="Life entity link deleted", max_length=500)


def _fields_set(body: BaseModel) -> set[str]:
    fields = getattr(body, "model_fields_set", None)
    if fields is None:
        fields = getattr(body, "__fields_set__", set())
    return set(fields)


def _raise_domain_error(exc: LifeGraphError) -> None:
    if isinstance(exc, LifeGraphNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, LifeGraphConflict):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


def setup_life_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(prefix="/api/life", tags=["life"])

    @router.post("/sources", status_code=201)
    def create_source(request: Request, body: LifeSourceCreate) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                source, created = create_life_source(
                    db,
                    account=account,
                    source_type=body.source_type,
                    title=body.title,
                    source_ref=body.source_ref,
                    safe_excerpt=body.safe_excerpt,
                    content_sha256=body.content_sha256,
                    observed_at=body.observed_at,
                    sensitivity=body.sensitivity,
                    metadata=body.metadata,
                    idempotency_key=body.idempotency_key,
                )
                return {"source": serialize_life_source(source), "created": created}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/sources")
    def list_sources(
        request: Request,
        source_type: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_life_sources(
                    db,
                    owner_id=account.id,
                    source_type=source_type,
                    limit=limit,
                )
                return {
                    "items": [serialize_life_source(item) for item in items],
                    "count": len(items),
                    "truncated": truncated,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/entities", status_code=201)
    def create_entity(request: Request, body: LifeEntityCreate) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_life_entity(
                    db,
                    account=account,
                    entity_type=body.entity_type,
                    title=body.title,
                    summary=body.summary,
                    status=body.status,
                    properties=body.properties,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    domain_ref_type=body.domain_ref_type,
                    domain_ref_id=body.domain_ref_id,
                    occurred_at=body.occurred_at,
                    due_at=body.due_at,
                    review_at=body.review_at,
                    idempotency_key=body.idempotency_key,
                    reason=body.reason,
                )
                return {"entity": serialize_life_entity(entity), "created": created}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/entities")
    def list_entities(
        request: Request,
        entity_type: str | None = Query(default=None),
        status: str | None = Query(default=None),
        include_deleted: bool = Query(default=False),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_life_entities(
                    db,
                    owner_id=account.id,
                    entity_type=entity_type,
                    status=status,
                    include_deleted=include_deleted,
                    limit=limit,
                )
                return {
                    "items": [serialize_life_entity(item) for item in items],
                    "count": len(items),
                    "truncated": truncated,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # Fixed paths precede the dynamic entity routes so they cannot be parsed as
    # entity identifiers by older Starlette/FastAPI route matchers.
    @router.get("/search")
    def search_entities(
        request: Request,
        q: str = Query(min_length=1, max_length=500),
        entity_type: str | None = Query(default=None),
        status: str | None = Query(default=None),
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
                return search_life_entities(
                    db,
                    owner_id=account.id,
                    query_text=q,
                    entity_type=entity_type,
                    status=status,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/decisions/review")
    def decisions_for_review(
        request: Request,
        due_before: datetime | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_decisions_for_review(
                    db,
                    owner_id=account.id,
                    due_before=due_before,
                    limit=limit,
                )
                return {
                    "items": [serialize_life_entity(item) for item in items],
                    "count": len(items),
                    "truncated": truncated,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/tasks/quality")
    def task_quality(
        request: Request,
        at: datetime | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "items": [], "count": 0, "scanned": 0,
                        "flag_counts": {}, "truncated": False,
                    }
                return task_quality_report(
                    db, owner_id=account.id, now=at, limit=limit
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/links", status_code=201)
    def create_link(request: Request, body: EntityLinkCreate) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                link, created = create_entity_link(
                    db,
                    account=account,
                    source_id=body.source_id,
                    relation=body.relation,
                    target_id=body.target_id,
                    metadata=body.metadata,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    reason=body.reason,
                )
                return {"link": serialize_entity_link(link), "created": created}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/links")
    def list_links(
        request: Request,
        entity_id: str = Query(max_length=36),
        direction: str = Query(default="both"),
        relation: str | None = Query(default=None),
        include_deleted: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Life entity not found")
                items, truncated = list_entity_links(
                    db,
                    owner_id=account.id,
                    entity_id=entity_id,
                    direction=direction,
                    relation=relation,
                    include_deleted=include_deleted,
                    limit=limit,
                )
                return {
                    "items": [serialize_entity_link(item) for item in items],
                    "count": len(items),
                    "truncated": truncated,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/links/{link_id}")
    def remove_link(
        request: Request, link_id: str, body: EntityLinkDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                link = delete_entity_link(
                    db,
                    owner_id=account.id,
                    link_id=link_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {"link": serialize_entity_link(link)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/entities/{entity_id}/graph")
    def entity_graph(
        request: Request,
        entity_id: str,
        depth: int = Query(default=2, ge=1, le=4),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Life entity not found")
                return traverse_life_graph(
                    db,
                    owner_id=account.id,
                    entity_id=entity_id,
                    depth=depth,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/entities/{entity_id}/versions")
    def entity_versions(
        request: Request,
        entity_id: str,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Life entity not found")
                items, truncated = list_life_entity_versions(
                    db, owner_id=account.id, entity_id=entity_id, limit=limit
                )
                return {
                    "items": [
                        serialize_life_entity_version(item) for item in items
                    ],
                    "count": len(items),
                    "truncated": truncated,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/entities/{entity_id}")
    def get_entity(
        request: Request,
        entity_id: str,
        include_deleted: bool = Query(default=False),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Life entity not found")
                entity = get_life_entity(
                    db,
                    owner_id=account.id,
                    entity_id=entity_id,
                    include_deleted=include_deleted,
                )
                return {"entity": serialize_life_entity(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.patch("/entities/{entity_id}")
    def update_entity(
        request: Request, entity_id: str, body: LifeEntityUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body)
        changes = {
            field: getattr(body, field)
            for field in (
                "title", "summary", "status", "properties", "provenance",
                "confidence", "sensitivity", "occurred_at", "due_at", "review_at",
            )
            if field in fields
        }
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_life_entity(
                    db,
                    owner_id=account.id,
                    entity_id=entity_id,
                    expected_version=body.version,
                    changes=changes,
                    reason=body.reason,
                )
                return {"entity": serialize_life_entity(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/entities/{entity_id}")
    def remove_entity(
        request: Request, entity_id: str, body: VersionedDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = delete_life_entity(
                    db,
                    owner_id=account.id,
                    entity_id=entity_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {"entity": serialize_life_entity(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router
