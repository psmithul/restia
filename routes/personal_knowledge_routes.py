"""Owner-scoped HTTP API for V3 Personal Memory and Knowledge."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.database import SessionLocal
from src.identity import request_account_transaction
from src.life_graph import LifeGraphConflict, LifeGraphError, LifeGraphNotFound
from src.personal_knowledge_service import (
    citation_backed_answer_evidence,
    create_knowledge_source,
    create_personal_knowledge_record,
    delete_personal_knowledge_record,
    get_knowledge_source,
    get_personal_knowledge_record,
    list_knowledge_sources,
    list_personal_knowledge_records,
    list_stale_personal_knowledge,
    markdown_index_rebuild_manifest,
    personal_knowledge_history,
    search_knowledge_sources,
    search_personal_knowledge_records,
    update_personal_knowledge_record,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KnowledgeSourceCreate(_StrictModel):
    source_kind: str = Field(max_length=40)
    title: str = Field(max_length=240)
    observed_at: datetime
    descriptor: dict[str, Any] | None = None
    source_ref: str | None = Field(default=None, max_length=2_000)
    safe_excerpt: str = Field(default="", max_length=4_000)
    content_sha256: str | None = Field(default=None, max_length=64)
    details: dict[str, Any] | None = None
    sensitivity: str = Field(default="private", max_length=24)
    idempotency_key: str | None = Field(default=None, max_length=256)


class KnowledgeCitationInput(_StrictModel):
    target_kind: str = Field(max_length=40)
    target_id: str = Field(max_length=255)
    relation: str = Field(default="supports", max_length=40)
    locator: str = Field(default="", max_length=500)
    excerpt: str = Field(default="", max_length=2_000)
    note: str = Field(default="", max_length=1_000)


class KnowledgeInferenceInput(_StrictModel):
    generated_by: str = Field(max_length=20)
    method: str = Field(max_length=500)
    basis: str = Field(max_length=2_000)
    model_label: str = Field(default="", max_length=120)


class PersonalKnowledgeCreate(_StrictModel):
    memory_kind: str = Field(max_length=40)
    title: str = Field(max_length=240)
    statement: str = Field(max_length=20_000)
    epistemic_status: str = Field(max_length=40)
    claim_origin: str = Field(max_length=40)
    citations: list[KnowledgeCitationInput] = Field(min_length=1, max_length=30)
    observed_at: datetime | None = None
    reviewed_at: datetime | None = None
    stale_at: datetime | None = None
    stale_after: datetime | None = None
    tags: list[str] = Field(default_factory=list, max_length=40)
    details: dict[str, Any] | None = None
    inference: KnowledgeInferenceInput | None = None
    status: str = Field(default="active", max_length=32)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    provenance: dict[str, Any] | None = None
    idempotency_key: str | None = Field(default=None, max_length=256)


class PersonalKnowledgeUpdate(_StrictModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    statement: str | None = Field(default=None, max_length=20_000)
    epistemic_status: str | None = Field(default=None, max_length=40)
    citations: list[KnowledgeCitationInput] | None = Field(
        default=None, min_length=1, max_length=30,
    )
    observed_at: datetime | None = None
    reviewed_at: datetime | None = None
    stale_at: datetime | None = None
    stale_after: datetime | None = None
    tags: list[str] | None = Field(default=None, max_length=40)
    details: dict[str, Any] | None = None
    inference: KnowledgeInferenceInput | None = None
    status: str | None = Field(default=None, max_length=32)
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)
    provenance: dict[str, Any] | None = None
    reason: str = Field(default="Personal knowledge record updated", max_length=2_000)


class PersonalKnowledgeDelete(_StrictModel):
    version: int = Field(ge=1)
    reason: str = Field(default="Personal knowledge record deleted", max_length=2_000)


def _raise_domain_error(exc: LifeGraphError) -> None:
    if isinstance(exc, LifeGraphNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, LifeGraphConflict):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


def _model_list(values: list[BaseModel]) -> list[dict[str, Any]]:
    return [value.model_dump(exclude_unset=False) for value in values]


def setup_personal_knowledge_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(
        prefix="/api/life/knowledge", tags=["life-personal-knowledge"],
    )

    @router.post("/sources", status_code=201)
    def create_source(
        request: Request, body: KnowledgeSourceCreate,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True,
            ) as account:
                source, created = create_knowledge_source(
                    db,
                    account=account,
                    source_kind=body.source_kind,
                    title=body.title,
                    observed_at=body.observed_at,
                    descriptor=body.descriptor,
                    source_ref=body.source_ref,
                    safe_excerpt=body.safe_excerpt,
                    content_sha256=body.content_sha256,
                    details=body.details,
                    sensitivity=body.sensitivity,
                    idempotency_key=body.idempotency_key,
                )
                return {
                    "source": get_knowledge_source(
                        db, owner_id=account.id, source_id=source.id,
                    ),
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

    @router.get("/sources")
    def list_sources(
        request: Request,
        source_kind: str | None = Query(default=None, max_length=40),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_knowledge_sources(
                    db, owner_id=account.id, source_kind=source_kind, limit=limit,
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/sources/search")
    def search_sources(
        request: Request,
        q: str = Query(min_length=1, max_length=500),
        source_kind: str | None = Query(default=None, max_length=40),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    return {
                        "items": [], "count": 0, "scanned": 0,
                        "truncated": False,
                    }
                return search_knowledge_sources(
                    db,
                    owner_id=account.id,
                    query_text=q,
                    source_kind=source_kind,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/sources/{source_id}")
    def get_source(request: Request, source_id: str) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Knowledge source not found")
                return {
                    "source": get_knowledge_source(
                        db, owner_id=account.id, source_id=source_id,
                    )
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/records", status_code=201)
    def create_record(
        request: Request, body: PersonalKnowledgeCreate,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True,
            ) as account:
                entity, created = create_personal_knowledge_record(
                    db,
                    account=account,
                    memory_kind=body.memory_kind,
                    title=body.title,
                    statement=body.statement,
                    epistemic_status=body.epistemic_status,
                    claim_origin=body.claim_origin,
                    citations=_model_list(body.citations),
                    observed_at=body.observed_at,
                    reviewed_at=body.reviewed_at,
                    stale_at=body.stale_at,
                    stale_after=body.stale_after,
                    tags=body.tags,
                    details=body.details,
                    inference=(
                        body.inference.model_dump(exclude_unset=False)
                        if body.inference is not None else None
                    ),
                    status=body.status,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    provenance=body.provenance,
                    idempotency_key=body.idempotency_key,
                )
                return {
                    "record": get_personal_knowledge_record(
                        db, owner_id=account.id, record_id=entity.id,
                    ),
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
        memory_kind: str | None = Query(default=None, max_length=40),
        epistemic_status: str | None = Query(default=None, max_length=40),
        status: str | None = Query(default=None, max_length=32),
        as_of: datetime | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_personal_knowledge_records(
                    db,
                    owner_id=account.id,
                    memory_kind=memory_kind,
                    epistemic_status=epistemic_status,
                    status=status,
                    limit=limit,
                    as_of=as_of,
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
        memory_kind: str | None = Query(default=None, max_length=40),
        epistemic_status: str | None = Query(default=None, max_length=40),
        as_of: datetime | None = Query(default=None),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    return {
                        "items": [], "count": 0, "scanned": 0,
                        "truncated": False,
                    }
                return search_personal_knowledge_records(
                    db,
                    owner_id=account.id,
                    query_text=q,
                    memory_kind=memory_kind,
                    epistemic_status=epistemic_status,
                    as_of=as_of,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/records/stale")
    def stale_records(
        request: Request,
        as_of: datetime = Query(),
        memory_kind: str | None = Query(default=None, max_length=40),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    return {
                        "items": [], "count": 0, "scanned": 0,
                        "truncated": False, "as_of": as_of.isoformat(),
                    }
                return list_stale_personal_knowledge(
                    db,
                    owner_id=account.id,
                    as_of=as_of,
                    memory_kind=memory_kind,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/evidence")
    def answer_evidence(
        request: Request,
        q: str = Query(min_length=1, max_length=500),
        as_of: datetime = Query(),
        memory_kind: str | None = Query(default=None, max_length=40),
        limit: int = Query(default=10, ge=1, le=50),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    return {
                        "query": q, "as_of": as_of.isoformat(), "evidence": [],
                        "gaps": [], "unsupported": [], "evidence_count": 0,
                        "truncated": False, "synthesizes_answer": False,
                        "mutates_records": False,
                    }
                return citation_backed_answer_evidence(
                    db,
                    owner_id=account.id,
                    query_text=q,
                    as_of=as_of,
                    memory_kind=memory_kind,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/markdown-index-manifest")
    def markdown_manifest(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    return {
                        "items": [], "count": 0, "scanned": 0,
                        "truncated": False,
                        "authority": "durable_markdown_document_not_derived_index",
                        "writes_user_files": False, "mutates_records": False,
                    }
                return markdown_index_rebuild_manifest(
                    db, owner_id=account.id, limit=limit,
                )
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
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Personal knowledge record not found")
                items, truncated = personal_knowledge_history(
                    db, owner_id=account.id, record_id=record_id, limit=limit,
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/records/{record_id}")
    def get_record(
        request: Request,
        record_id: str,
        as_of: datetime | None = Query(default=None),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Personal knowledge record not found")
                return {
                    "record": get_personal_knowledge_record(
                        db,
                        owner_id=account.id,
                        record_id=record_id,
                        as_of=as_of,
                    )
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.patch("/records/{record_id}")
    def update_record(
        request: Request,
        record_id: str,
        body: PersonalKnowledgeUpdate,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True,
            ) as account:
                changes = body.model_dump(
                    exclude_unset=True, exclude={"version", "reason"},
                )
                entity = update_personal_knowledge_record(
                    db,
                    owner_id=account.id,
                    record_id=record_id,
                    expected_version=body.version,
                    changes=changes,
                    reason=body.reason,
                )
                return {
                    "record": get_personal_knowledge_record(
                        db, owner_id=account.id, record_id=entity.id,
                    )
                }
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
        request: Request,
        record_id: str,
        body: PersonalKnowledgeDelete,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True,
            ) as account:
                entity = delete_personal_knowledge_record(
                    db,
                    owner_id=account.id,
                    record_id=record_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {
                    "record": get_personal_knowledge_record(
                        db,
                        owner_id=account.id,
                        record_id=entity.id,
                        include_deleted=True,
                    )
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router


__all__ = ["setup_personal_knowledge_routes"]
