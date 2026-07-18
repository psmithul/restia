"""Owner-scoped HTTP API for typed V3 Journal & Reflection records."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.database import SessionLocal
from src.identity import request_account_transaction
from src.journal_service import (
    create_journal_entry,
    delete_journal_entry,
    get_journal_entry,
    journal_entry_history,
    journal_period_review,
    list_journal_entries,
    search_journal_entries,
    serialize_journal_entry,
    update_journal_entry,
)
from src.life_graph import LifeGraphConflict, LifeGraphError, LifeGraphNotFound


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JournalMoodInput(_StrictModel):
    label: str = Field(default="", max_length=100)
    score: int | None = Field(default=None, ge=0, le=10)
    energy: int | None = Field(default=None, ge=0, le=10)


class JournalPromiseInput(_StrictModel):
    id: str | None = Field(default=None, max_length=64)
    text: str = Field(max_length=2_000)
    status: str = Field(default="open", max_length=24)
    due_date: date | None = None
    completed_on: date | None = None
    note: str = Field(default="", max_length=2_000)


class JournalEvidenceInput(_StrictModel):
    id: str | None = Field(default=None, max_length=64)
    label: str = Field(max_length=1_000)
    source_id: str | None = Field(default=None, max_length=36)
    entity_id: str | None = Field(default=None, max_length=36)
    reference: str = Field(default="", max_length=2_000)


class JournalEntryCreate(_StrictModel):
    title: str = Field(max_length=240)
    entry_date: date
    body: str = Field(default="", max_length=20_000)
    mood: JournalMoodInput | None = None
    moments: list[str] = Field(default_factory=list, max_length=50)
    wins: list[str] = Field(default_factory=list, max_length=50)
    difficulties: list[str] = Field(default_factory=list, max_length=50)
    lessons: list[str] = Field(default_factory=list, max_length=50)
    gratitude: list[str] = Field(default_factory=list, max_length=50)
    ideas: list[str] = Field(default_factory=list, max_length=50)
    decisions: list[str] = Field(default_factory=list, max_length=50)
    principles: list[str] = Field(default_factory=list, max_length=50)
    time_notes: list[str] = Field(default_factory=list, max_length=50)
    relationship_notes: list[str] = Field(default_factory=list, max_length=50)
    goal_progress: list[str] = Field(default_factory=list, max_length=50)
    next_changes: list[str] = Field(default_factory=list, max_length=50)
    promises: list[JournalPromiseInput] = Field(default_factory=list, max_length=50)
    changes: list[str] = Field(default_factory=list, max_length=50)
    improvements: list[str] = Field(default_factory=list, max_length=50)
    pattern_tags: list[str] = Field(default_factory=list, max_length=50)
    evidence: list[JournalEvidenceInput] = Field(default_factory=list, max_length=50)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    idempotency_key: str | None = Field(default=None, max_length=256)


class JournalEntryUpdate(_StrictModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    entry_date: date | None = None
    body: str | None = Field(default=None, max_length=20_000)
    mood: JournalMoodInput | None = None
    moments: list[str] | None = Field(default=None, max_length=50)
    wins: list[str] | None = Field(default=None, max_length=50)
    difficulties: list[str] | None = Field(default=None, max_length=50)
    lessons: list[str] | None = Field(default=None, max_length=50)
    gratitude: list[str] | None = Field(default=None, max_length=50)
    ideas: list[str] | None = Field(default=None, max_length=50)
    decisions: list[str] | None = Field(default=None, max_length=50)
    principles: list[str] | None = Field(default=None, max_length=50)
    time_notes: list[str] | None = Field(default=None, max_length=50)
    relationship_notes: list[str] | None = Field(default=None, max_length=50)
    goal_progress: list[str] | None = Field(default=None, max_length=50)
    next_changes: list[str] | None = Field(default=None, max_length=50)
    promises: list[JournalPromiseInput] | None = Field(default=None, max_length=50)
    changes: list[str] | None = Field(default=None, max_length=50)
    improvements: list[str] | None = Field(default=None, max_length=50)
    pattern_tags: list[str] | None = Field(default=None, max_length=50)
    evidence: list[JournalEvidenceInput] | None = Field(default=None, max_length=50)
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)
    status: str | None = Field(default=None, max_length=32)


class JournalEntryDelete(_StrictModel):
    version: int = Field(ge=1)
    reason: str = Field(default="Journal entry deleted", max_length=500)


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


def setup_journal_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(prefix="/api/life/journal", tags=["life-journal"])

    @router.post("/entries", status_code=201)
    def create_entry(request: Request, body: JournalEntryCreate) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_journal_entry(
                    db,
                    account=account,
                    title=body.title,
                    entry_date=body.entry_date,
                    body=body.body,
                    mood=_model_dict(body.mood),
                    moments=body.moments,
                    wins=body.wins,
                    difficulties=body.difficulties,
                    lessons=body.lessons,
                    gratitude=body.gratitude,
                    ideas=body.ideas,
                    decisions=body.decisions,
                    principles=body.principles,
                    time_notes=body.time_notes,
                    relationship_notes=body.relationship_notes,
                    goal_progress=body.goal_progress,
                    next_changes=body.next_changes,
                    promises=_model_list(body.promises),
                    changes=body.changes,
                    improvements=body.improvements,
                    pattern_tags=body.pattern_tags,
                    evidence=_model_list(body.evidence),
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    idempotency_key=body.idempotency_key,
                )
                return {"entry": serialize_journal_entry(entity), "created": created}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/entries")
    def list_entries(
        request: Request,
        from_date: date | None = Query(default=None),
        to_date: date | None = Query(default=None),
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
                items, truncated = list_journal_entries(
                    db,
                    owner_id=account.id,
                    from_date=from_date,
                    to_date=to_date,
                    status=status,
                    limit=limit,
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/entries/search")
    def search_entries(
        request: Request,
        q: str = Query(min_length=1, max_length=500),
        from_date: date | None = Query(default=None),
        to_date: date | None = Query(default=None),
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
                return search_journal_entries(
                    db,
                    owner_id=account.id,
                    query_text=q,
                    from_date=from_date,
                    to_date=to_date,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/reviews/{period}")
    def period_review(
        request: Request,
        period: str,
        anchor_date: date = Query(),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    # Preserve the exact deterministic period boundary without
                    # creating a principal during a read.
                    return journal_period_review(
                        db,
                        owner_id="missing",
                        period=period,
                        anchor_date=anchor_date,
                    )
                return journal_period_review(
                    db,
                    owner_id=account.id,
                    period=period,
                    anchor_date=anchor_date,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/entries/{entry_id}/history")
    def entry_history(
        request: Request,
        entry_id: str,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Journal entry not found")
                items, truncated = journal_entry_history(
                    db, owner_id=account.id, entity_id=entry_id, limit=limit
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/entries/{entry_id}")
    def get_entry(request: Request, entry_id: str) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Journal entry not found")
                return {
                    "entry": get_journal_entry(
                        db, owner_id=account.id, entity_id=entry_id
                    )
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.patch("/entries/{entry_id}")
    def update_entry(
        request: Request, entry_id: str, body: JournalEntryUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body) - {"version"}
        changes: dict[str, Any] = {}
        for field in fields:
            value = getattr(body, field)
            if field == "mood":
                value = _model_dict(value)
            elif field in {"promises", "evidence"}:
                value = _model_list(value)
            changes[field] = value
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_journal_entry(
                    db,
                    account=account,
                    entity_id=entry_id,
                    expected_version=body.version,
                    changes=changes,
                )
                return {"entry": serialize_journal_entry(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/entries/{entry_id}")
    def remove_entry(
        request: Request, entry_id: str, body: JournalEntryDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = delete_journal_entry(
                    db,
                    owner_id=account.id,
                    entity_id=entry_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {"entry": serialize_journal_entry(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router
