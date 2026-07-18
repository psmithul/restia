"""Principal-scoped HTTP API for recoverable focus sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.database import LifeEntity, SessionLocal
from src.focus_mode import (
    MAX_DEFINITION_OF_DONE_LENGTH,
    MAX_ENTRY_TEXT_LENGTH,
    MAX_FOLLOW_UPS,
    FocusConflict,
    FocusError,
    FocusNotFound,
    abandon_focus_session,
    add_focus_evidence,
    add_focus_interruption,
    add_focus_progress,
    complete_focus_session,
    get_current_focus_session,
    get_focus_context,
    get_owned_focus_entities,
    list_focus_history,
    pause_focus_session,
    resume_focus_session,
    resolve_planning_focus_entity,
    serialize_focus_session,
    start_focus_session,
)
from src.identity import request_account_transaction


class FocusStart(BaseModel):
    entity_id: str | None = Field(default=None, min_length=1, max_length=36)
    entity_version: int | None = Field(default=None, ge=1)
    domain_ref_type: str | None = Field(default=None, min_length=1, max_length=48)
    domain_ref_id: str | None = Field(default=None, min_length=1, max_length=255)
    domain_ref_version: int | None = Field(default=None, ge=1)
    definition_of_done: str = Field(
        min_length=1, max_length=MAX_DEFINITION_OF_DONE_LENGTH
    )


class FocusVersion(BaseModel):
    version: int = Field(ge=1)


class FocusEntry(FocusVersion):
    text: str = Field(min_length=1, max_length=MAX_ENTRY_TEXT_LENGTH)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FocusFollowUp(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(default="", max_length=20_000)
    definition_of_done: str = Field(
        default="", max_length=MAX_DEFINITION_OF_DONE_LENGTH
    )
    due_at: datetime | None = None


class FocusFinish(FocusVersion):
    follow_ups: list[FocusFollowUp] = Field(
        default_factory=list, max_length=MAX_FOLLOW_UPS
    )


def _raise_domain_error(exc: FocusError) -> None:
    if isinstance(exc, FocusNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, FocusConflict):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


def _model_dict(value: BaseModel) -> dict[str, Any]:
    dumper = getattr(value, "model_dump", None)
    if callable(dumper):
        return dumper()
    return value.dict()


def _serialize_follow_up(entity: LifeEntity) -> dict[str, Any]:
    return {
        "id": entity.id,
        "entity_type": entity.entity_type,
        "title": entity.title or "",
        "summary": entity.summary or "",
        "status": entity.status,
        "properties": entity.properties or {},
        "due_at": (
            entity.due_at.isoformat() + "Z" if entity.due_at is not None else None
        ),
        "version": int(entity.version or 1),
    }


def _serialize_live_session(db, owner_id: str, session) -> dict[str, Any]:
    entity = get_owned_focus_entities(
        db, owner_id=owner_id, entity_ids=(session.entity_id,)
    ).get(session.entity_id)
    context = get_focus_context(
        db, owner_id=owner_id, entity_id=session.entity_id
    ) if entity is not None else []
    return serialize_focus_session(session, entity=entity, context=context)


def setup_focus_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(prefix="/api/life/focus", tags=["life-focus"])

    @router.get("/current")
    def current_focus(request: Request) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"session": None}
                session = get_current_focus_session(db, owner_id=account.id)
                if session is None:
                    return {"session": None}
                return {"session": _serialize_live_session(db, account.id, session)}
        except FocusError as exc:
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/history")
    def focus_history(
        request: Request,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"sessions": [], "count": 0, "truncated": False}
                sessions, truncated = list_focus_history(
                    db, owner_id=account.id, limit=limit
                )
                entities = get_owned_focus_entities(
                    db,
                    owner_id=account.id,
                    entity_ids=(session.entity_id for session in sessions),
                )
                return {
                    "sessions": [
                        serialize_focus_session(
                            session, entity=entities.get(session.entity_id)
                        )
                        for session in sessions
                    ],
                    "count": len(sessions),
                    "truncated": truncated,
                }
        except FocusError as exc:
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/start", status_code=201)
    def start(request: Request, body: FocusStart) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                direct_target = bool(
                    body.entity_id is not None or body.entity_version is not None
                )
                domain_target = bool(
                    body.domain_ref_type is not None
                    or body.domain_ref_id is not None
                    or body.domain_ref_version is not None
                )
                if direct_target == domain_target:
                    raise FocusError(
                        "Supply exactly one Life entity or canonical domain target"
                    )
                if direct_target:
                    if body.entity_id is None or body.entity_version is None:
                        raise FocusError(
                            "entity_id and entity_version are required"
                        )
                    entity_id = body.entity_id
                    entity_version = body.entity_version
                else:
                    if body.domain_ref_type != "planning_item":
                        raise FocusError(
                            "Only planning_item canonical Focus targets are supported"
                        )
                    if body.domain_ref_id is None or body.domain_ref_version is None:
                        raise FocusError(
                            "domain_ref_id and domain_ref_version are required"
                        )
                    entity = resolve_planning_focus_entity(
                        db,
                        account=account,
                        planning_item_id=body.domain_ref_id,
                        expected_planning_version=body.domain_ref_version,
                    )
                    entity_id = entity.id
                    entity_version = int(entity.version or 1)
                session = start_focus_session(
                    db,
                    account=account,
                    entity_id=entity_id,
                    expected_entity_version=entity_version,
                    definition_of_done=body.definition_of_done,
                )
                return {"session": _serialize_live_session(db, account.id, session)}
        except FocusError as exc:
            _raise_domain_error(exc)
        finally:
            db.close()

    def mutate_version(
        request: Request,
        session_id: str,
        body: FocusVersion,
        operation,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                session = operation(
                    db,
                    owner_id=account.id,
                    session_id=session_id,
                    expected_version=body.version,
                )
                return {
                    "session": _serialize_live_session(db, account.id, session)
                }
        except FocusError as exc:
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/{session_id}/pause")
    def pause(
        request: Request, session_id: str, body: FocusVersion
    ) -> dict[str, Any]:
        return mutate_version(
            request, session_id, body, pause_focus_session
        )

    @router.post("/{session_id}/resume")
    def resume(
        request: Request, session_id: str, body: FocusVersion
    ) -> dict[str, Any]:
        return mutate_version(
            request, session_id, body, resume_focus_session
        )

    def append_entry(
        request: Request,
        session_id: str,
        body: FocusEntry,
        operation,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                session, entry = operation(
                    db,
                    owner_id=account.id,
                    session_id=session_id,
                    expected_version=body.version,
                    text=body.text,
                    metadata=body.metadata,
                )
                return {
                    "session": _serialize_live_session(db, account.id, session),
                    "entry": entry,
                }
        except FocusError as exc:
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/{session_id}/interruptions")
    def add_interruption(
        request: Request, session_id: str, body: FocusEntry
    ) -> dict[str, Any]:
        return append_entry(
            request, session_id, body, add_focus_interruption
        )

    @router.post("/{session_id}/progress")
    def add_progress(
        request: Request, session_id: str, body: FocusEntry
    ) -> dict[str, Any]:
        return append_entry(request, session_id, body, add_focus_progress)

    @router.post("/{session_id}/evidence")
    def add_evidence(
        request: Request, session_id: str, body: FocusEntry
    ) -> dict[str, Any]:
        return append_entry(request, session_id, body, add_focus_evidence)

    def finish(
        request: Request,
        session_id: str,
        body: FocusFinish,
        operation,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                session, tasks = operation(
                    db,
                    owner_id=account.id,
                    session_id=session_id,
                    expected_version=body.version,
                    follow_ups=[_model_dict(item) for item in body.follow_ups],
                )
                return {
                    "session": _serialize_live_session(db, account.id, session),
                    "follow_ups": [_serialize_follow_up(task) for task in tasks],
                }
        except FocusError as exc:
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/{session_id}/complete")
    def complete(
        request: Request, session_id: str, body: FocusFinish
    ) -> dict[str, Any]:
        return finish(request, session_id, body, complete_focus_session)

    @router.post("/{session_id}/abandon")
    def abandon(
        request: Request, session_id: str, body: FocusFinish
    ) -> dict[str, Any]:
        return finish(request, session_id, body, abandon_focus_session)

    return router
