"""Owner-scoped HTTP API for typed V3 relationship records."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.database import SessionLocal
from src.identity import request_account_transaction
from src.life_graph import LifeGraphConflict, LifeGraphError, LifeGraphNotFound
from src.relationship_service import (
    create_commitment,
    create_follow_up,
    create_interaction,
    create_relationship_profile,
    get_relationship_profile,
    get_relationship_record,
    link_relationship_context,
    list_relationship_profiles,
    list_relationship_records,
    relationship_history,
    relationship_reminders,
    serialize_relationship_profile,
    serialize_relationship_record,
    update_relationship_profile,
    update_relationship_record,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContactOriginInput(_StrictModel):
    kind: str = Field(max_length=64)
    label: str = Field(max_length=240)
    source_id: str | None = Field(default=None, max_length=36)
    observed_at: datetime | None = None


class ImportantDateInput(_StrictModel):
    label: str = Field(max_length=160)
    date: datetime | str
    recurring_annually: bool = False
    source_id: str = Field(max_length=36)


class PreferenceInput(_StrictModel):
    key: str = Field(max_length=64)
    value: str = Field(max_length=1_000)
    source_id: str = Field(max_length=36)


class CarePlanInput(_StrictModel):
    interval_days: int = Field(ge=1, le=3_650)
    next_due_at: datetime
    source_id: str = Field(max_length=36)


class RelationshipProfileCreate(_StrictModel):
    title: str = Field(max_length=240)
    subject_kind: str = Field(max_length=32)
    relationship_type: str = Field(max_length=64)
    contact_origin: ContactOriginInput
    contact_record_id: str | None = Field(default=None, max_length=36)
    important_dates: list[ImportantDateInput] = Field(default_factory=list, max_length=40)
    preferences: list[PreferenceInput] = Field(default_factory=list, max_length=60)
    care_plan: CarePlanInput | None = None
    organization_entity_id: str | None = Field(default=None, max_length=36)
    linked_entity_ids: list[str] = Field(default_factory=list, max_length=50)
    private_notes: str = Field(default="", max_length=20_000)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    idempotency_key: str | None = Field(default=None, max_length=256)


class RelationshipProfileUpdate(_StrictModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    relationship_type: str | None = Field(default=None, max_length=64)
    contact_origin: ContactOriginInput | None = None
    important_dates: list[ImportantDateInput] | None = Field(default=None, max_length=40)
    preferences: list[PreferenceInput] | None = Field(default=None, max_length=60)
    care_plan: CarePlanInput | None = None
    private_notes: str | None = Field(default=None, max_length=20_000)
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)
    status: str | None = Field(default=None, max_length=32)


class RelationshipLinkCreate(_StrictModel):
    target_id: str = Field(max_length=36)
    provenance: dict[str, Any]
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)


class InteractionCreate(_StrictModel):
    title: str = Field(max_length=240)
    occurred_at: datetime
    channel: str = Field(max_length=64)
    direction: str = Field(default="unknown", max_length=32)
    note: str = Field(default="", max_length=20_000)
    provenance: dict[str, Any]
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    idempotency_key: str | None = Field(default=None, max_length=256)


class CommitmentCreate(_StrictModel):
    title: str = Field(max_length=240)
    due_at: datetime
    direction: str = Field(max_length=32)
    occurred_at: datetime | None = None
    note: str = Field(default="", max_length=20_000)
    provenance: dict[str, Any]
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    idempotency_key: str | None = Field(default=None, max_length=256)


class FollowUpCreate(_StrictModel):
    title: str = Field(max_length=240)
    due_at: datetime
    priority: str = Field(default="normal", max_length=32)
    reminder_kind: str = Field(default="follow_up", max_length=32)
    occurred_at: datetime | None = None
    note: str = Field(default="", max_length=20_000)
    provenance: dict[str, Any]
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    idempotency_key: str | None = Field(default=None, max_length=256)


class RelationshipRecordUpdate(_StrictModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    note: str | None = Field(default=None, max_length=20_000)
    status: str | None = Field(default=None, max_length=32)
    occurred_at: datetime | None = None
    due_at: datetime | None = None
    channel: str | None = Field(default=None, max_length=64)
    direction: str | None = Field(default=None, max_length=32)
    priority: str | None = Field(default=None, max_length=32)
    reminder_kind: str | None = Field(default=None, max_length=32)
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)


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


def setup_relationship_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(
        prefix="/api/life/relationships", tags=["life-relationships"]
    )

    @router.get("/reminders")
    def reminders(
        request: Request,
        due_before: datetime = Query(),
        as_of: datetime | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return relationship_reminders(
                        db,
                        owner_id="missing",
                        due_before=due_before,
                        as_of=as_of,
                        limit=limit,
                    )
                return relationship_reminders(
                    db,
                    owner_id=account.id,
                    due_before=due_before,
                    as_of=as_of,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/profiles", status_code=201)
    def create_profile(
        request: Request, body: RelationshipProfileCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_relationship_profile(
                    db,
                    account=account,
                    title=body.title,
                    subject_kind=body.subject_kind,
                    relationship_type=body.relationship_type,
                    contact_origin=_model_dict(body.contact_origin),
                    contact_record_id=body.contact_record_id,
                    important_dates=_model_list(body.important_dates),
                    preferences=_model_list(body.preferences),
                    care_plan=_model_dict(body.care_plan),
                    organization_entity_id=body.organization_entity_id,
                    linked_entity_ids=body.linked_entity_ids,
                    private_notes=body.private_notes,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    idempotency_key=body.idempotency_key,
                )
                return {
                    "profile": serialize_relationship_profile(
                        db, owner_id=account.id, entity=entity
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

    @router.get("/profiles")
    def list_profiles(
        request: Request,
        subject_kind: str | None = Query(default=None, max_length=32),
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
                items, truncated = list_relationship_profiles(
                    db,
                    owner_id=account.id,
                    subject_kind=subject_kind,
                    status=status,
                    limit=limit,
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/profiles/{profile_id}")
    def get_profile(request: Request, profile_id: str) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Relationship profile not found")
                return {
                    "profile": get_relationship_profile(
                        db, owner_id=account.id, entity_id=profile_id
                    )
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.patch("/profiles/{profile_id}")
    def update_profile(
        request: Request, profile_id: str, body: RelationshipProfileUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body) - {"version"}
        changes: dict[str, Any] = {}
        for field in fields:
            value = getattr(body, field)
            if field in {"contact_origin", "care_plan"}:
                value = _model_dict(value)
            elif field in {"important_dates", "preferences"}:
                value = _model_list(value)
            changes[field] = value
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_relationship_profile(
                    db,
                    account=account,
                    entity_id=profile_id,
                    expected_version=body.version,
                    changes=changes,
                )
                return {
                    "profile": serialize_relationship_profile(
                        db, owner_id=account.id, entity=entity
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

    @router.post("/profiles/{profile_id}/links", status_code=201)
    def create_link(
        request: Request, profile_id: str, body: RelationshipLinkCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                link, created = link_relationship_context(
                    db,
                    account=account,
                    profile_id=profile_id,
                    target_id=body.target_id,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                )
                return {"link": {
                    "id": link.id,
                    "relation": link.relation,
                    "target_id": link.target_id,
                    "version": int(link.version or 1),
                }, "created": created}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/profiles/{profile_id}/interactions", status_code=201)
    def create_profile_interaction(
        request: Request, profile_id: str, body: InteractionCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_interaction(
                    db, account=account, profile_id=profile_id,
                    title=body.title, occurred_at=body.occurred_at,
                    channel=body.channel, direction=body.direction,
                    note=body.note, provenance=body.provenance,
                    confidence=body.confidence, sensitivity=body.sensitivity,
                    idempotency_key=body.idempotency_key,
                )
                return {
                    "record": serialize_relationship_record(
                        db, owner_id=account.id, entity=entity
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

    @router.post("/profiles/{profile_id}/commitments", status_code=201)
    def create_profile_commitment(
        request: Request, profile_id: str, body: CommitmentCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_commitment(
                    db, account=account, profile_id=profile_id,
                    title=body.title, due_at=body.due_at,
                    direction=body.direction, occurred_at=body.occurred_at,
                    note=body.note, provenance=body.provenance,
                    confidence=body.confidence, sensitivity=body.sensitivity,
                    idempotency_key=body.idempotency_key,
                )
                return {
                    "record": serialize_relationship_record(
                        db, owner_id=account.id, entity=entity
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

    @router.post("/profiles/{profile_id}/follow-ups", status_code=201)
    def create_profile_follow_up(
        request: Request, profile_id: str, body: FollowUpCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_follow_up(
                    db, account=account, profile_id=profile_id,
                    title=body.title, due_at=body.due_at,
                    priority=body.priority, reminder_kind=body.reminder_kind,
                    occurred_at=body.occurred_at,
                    note=body.note, provenance=body.provenance,
                    confidence=body.confidence, sensitivity=body.sensitivity,
                    idempotency_key=body.idempotency_key,
                )
                return {
                    "record": serialize_relationship_record(
                        db, owner_id=account.id, entity=entity
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

    @router.get("/profiles/{profile_id}/records")
    def list_profile_records(
        request: Request,
        profile_id: str,
        record_kind: str | None = Query(default=None, max_length=32),
        status: str | None = Query(default=None, max_length=32),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Relationship profile not found")
                items, truncated = list_relationship_records(
                    db, owner_id=account.id, profile_id=profile_id,
                    record_kind=record_kind, status=status, limit=limit,
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
                    raise LifeGraphNotFound("Relationship record not found")
                return {"record": get_relationship_record(
                    db, owner_id=account.id, entity_id=record_id
                )}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.patch("/records/{record_id}")
    def update_record(
        request: Request, record_id: str, body: RelationshipRecordUpdate
    ) -> dict[str, Any]:
        changes = {
            field: getattr(body, field)
            for field in _fields_set(body) - {"version"}
        }
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_relationship_record(
                    db, account=account, entity_id=record_id,
                    expected_version=body.version, changes=changes,
                )
                return {"record": serialize_relationship_record(
                    db, owner_id=account.id, entity=entity
                )}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/history/{entity_id}")
    def history(
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
                    raise LifeGraphNotFound("Relationship record not found")
                items, truncated = relationship_history(
                    db, owner_id=account.id, entity_id=entity_id, limit=limit
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    return router


__all__ = ["setup_relationship_routes"]
