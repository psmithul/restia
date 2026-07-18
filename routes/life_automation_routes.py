"""Typed, owner-scoped HTTP boundary for the V3 Life automation engine.

Evaluation is a ``life:read`` operation and runs in a rollback-only request
transaction.  Preparation is a ``life:write`` operation because it persists a
run record and, for external actions, review-only proposals.  Neither endpoint
executes a typed action, invokes a connector, or returns confirmation secrets.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.database import SessionLocal
from src.identity import request_account_transaction
from src.life_automation import (
    EXECUTION_CONTRACT,
    AutomationConflict,
    AutomationError,
    AutomationEvaluation,
    AutomationNotFound,
    AutomationPolicyDenied,
    AutomationPreparation,
    automation_definition_history,
    create_automation_definition,
    delete_automation_definition,
    evaluate_automation,
    get_automation_definition,
    list_automation_definitions,
    prepare_automation_run,
    prepare_meeting_end_workflow,
    serialize_automation_definition,
    update_automation_definition,
)
from src.life_graph import serialize_life_entity


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AutomationTriggerInput(_StrictModel):
    type: str = Field(min_length=1, max_length=64)
    config: dict[str, Any]


class AutomationActionInput(_StrictModel):
    type: str = Field(min_length=1, max_length=64)
    config: dict[str, Any]
    autonomy_level: int | None = Field(default=None, ge=1, le=6)


class AutomationCreate(_StrictModel):
    name: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=20_000)
    trigger: AutomationTriggerInput
    actions: list[AutomationActionInput] = Field(min_length=1, max_length=32)
    enabled: bool = True
    source_ids: list[str] = Field(default_factory=list, max_length=64)
    sensitivity: str = Field(default="private", min_length=1, max_length=24)
    idempotency_key: str = Field(min_length=1, max_length=1_024)


class AutomationUpdate(_StrictModel):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = Field(default=None, max_length=20_000)
    trigger: AutomationTriggerInput | None = None
    actions: list[AutomationActionInput] | None = Field(
        default=None, min_length=1, max_length=32
    )
    enabled: bool | None = None
    source_ids: list[str] | None = Field(default=None, max_length=64)
    sensitivity: str | None = Field(default=None, min_length=1, max_length=24)


class AutomationDelete(_StrictModel):
    version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1_000)


class AutomationEvaluate(_StrictModel):
    event: dict[str, Any]


class AutomationPrepare(_StrictModel):
    event: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=1_024)


class MeetingFollowUpInput(_StrictModel):
    channel: str = Field(min_length=1, max_length=32)
    recipient: str = Field(min_length=1, max_length=1_000)
    body: str = Field(min_length=1, max_length=20_000)
    subject: str | None = Field(default=None, max_length=998)
    thread_id: str | None = Field(default=None, max_length=512)
    email_account_id: str | None = Field(default=None, max_length=128)


class MeetingEndPrepare(_StrictModel):
    meeting_entity_id: str = Field(min_length=1, max_length=64)
    source_ids: list[str] = Field(min_length=1, max_length=64)
    follow_ups: list[MeetingFollowUpInput] = Field(min_length=1, max_length=20)
    ended_at: datetime | None = None
    idempotency_key: str = Field(min_length=1, max_length=1_024)


def _fields_set(model: BaseModel) -> set[str]:
    fields = getattr(model, "model_fields_set", None)
    if fields is None:
        fields = getattr(model, "__fields_set__", set())
    return set(fields)


def _model_dump(model: BaseModel, *, exclude_none: bool = False) -> dict[str, Any]:
    dumper = getattr(model, "model_dump", None)
    if callable(dumper):
        return dict(dumper(exclude_none=exclude_none))
    return dict(model.dict(exclude_none=exclude_none))


def _raise_domain_error(exc: AutomationError) -> None:
    if isinstance(exc, AutomationNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, AutomationConflict):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, AutomationPolicyDenied):
        raise HTTPException(403, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


def _serialize_evaluation(value: AutomationEvaluation) -> dict[str, Any]:
    return {
        "automation_id": value.automation_id,
        "automation_version": value.automation_version,
        "matched": value.matched,
        "trigger_type": value.trigger_type,
        "event_fingerprint": value.event_fingerprint,
        "event_summary": dict(value.event_summary),
        "source_ids": list(value.source_ids),
        "plans": [dict(plan) for plan in value.plans],
        "execution_contract": dict(EXECUTION_CONTRACT),
    }


def _serialize_preparation(value: AutomationPreparation) -> dict[str, Any]:
    # Confirmation challenges are deliberately not returned.  A later review
    # surface must issue a fresh challenge for the immutable proposal.
    return {
        "evaluation": _serialize_evaluation(value.evaluation),
        "run": (
            serialize_life_entity(value.run_entity)
            if value.run_entity is not None
            else None
        ),
        "created": value.created,
        "proposal_ids": list(value.proposal_ids),
        "confirmation_required": bool(value.proposal_ids),
        "confirmation_tokens_returned": False,
        "execution_contract": dict(EXECUTION_CONTRACT),
    }


def setup_life_automation_routes(
    *, session_factory: Callable[[], Any] = SessionLocal,
) -> APIRouter:
    router = APIRouter(
        prefix="/api/life/automations", tags=["life-automations"]
    )

    @router.post("", status_code=201)
    def create_definition(
        request: Request, body: AutomationCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_automation_definition(
                    db,
                    account=account,
                    **_model_dump(body, exclude_none=True),
                )
                return {
                    "automation": serialize_automation_definition(entity),
                    "created": created,
                }
        except AutomationError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("")
    def list_definitions(
        request: Request,
        enabled: bool | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                rows, truncated = list_automation_definitions(
                    db, owner_id=account.id, enabled=enabled, limit=limit
                )
                items = [serialize_automation_definition(row) for row in rows]
                return {
                    "items": items,
                    "count": len(items),
                    "truncated": truncated,
                }
        except AutomationError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    # This static path must remain ahead of /{automation_id} routes.
    @router.post("/meeting-end/prepare")
    def prepare_meeting_end(
        request: Request, body: MeetingEndPrepare
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            values = _model_dump(body, exclude_none=True)
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                result = prepare_meeting_end_workflow(
                    db, account=account, **values
                )
                return _serialize_preparation(result)
        except AutomationError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/{automation_id}/history")
    def history(
        request: Request,
        automation_id: str,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise AutomationNotFound("Automation definition not found")
                items, truncated = automation_definition_history(
                    db,
                    owner_id=account.id,
                    automation_id=automation_id,
                    limit=limit,
                )
                return {
                    "items": items,
                    "count": len(items),
                    "truncated": truncated,
                }
        except AutomationError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/{automation_id}/evaluate")
    def evaluate_definition(
        request: Request, automation_id: str, body: AutomationEvaluate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise AutomationNotFound("Automation definition not found")
                result = evaluate_automation(
                    db,
                    owner_id=account.id,
                    automation_id=automation_id,
                    event=body.event,
                )
                return {"evaluation": _serialize_evaluation(result)}
        except AutomationError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/{automation_id}/prepare")
    def prepare_definition(
        request: Request, automation_id: str, body: AutomationPrepare
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                result = prepare_automation_run(
                    db,
                    account=account,
                    automation_id=automation_id,
                    event=body.event,
                    idempotency_key=body.idempotency_key,
                )
                return _serialize_preparation(result)
        except AutomationError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/{automation_id}")
    def get_definition(request: Request, automation_id: str) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise AutomationNotFound("Automation definition not found")
                entity = get_automation_definition(
                    db, owner_id=account.id, automation_id=automation_id
                )
                return {"automation": serialize_automation_definition(entity)}
        except AutomationError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.patch("/{automation_id}")
    def update_definition(
        request: Request, automation_id: str, body: AutomationUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body) - {"version"}
        changes: dict[str, Any] = {}
        for field in fields:
            value = getattr(body, field)
            if isinstance(value, BaseModel):
                value = _model_dump(value, exclude_none=True)
            elif isinstance(value, list) and value and isinstance(value[0], BaseModel):
                value = [
                    _model_dump(item, exclude_none=True) for item in value
                ]
            changes[field] = value
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_automation_definition(
                    db,
                    owner_id=account.id,
                    automation_id=automation_id,
                    expected_version=body.version,
                    changes=changes,
                )
                return {"automation": serialize_automation_definition(entity)}
        except AutomationError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/{automation_id}")
    def delete_definition(
        request: Request, automation_id: str, body: AutomationDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = delete_automation_definition(
                    db,
                    owner_id=account.id,
                    automation_id=automation_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {"automation": serialize_automation_definition(entity)}
        except AutomationError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router


__all__ = [
    "AutomationActionInput",
    "AutomationCreate",
    "AutomationDelete",
    "AutomationEvaluate",
    "AutomationPrepare",
    "AutomationTriggerInput",
    "AutomationUpdate",
    "MeetingEndPrepare",
    "MeetingFollowUpInput",
    "setup_life_automation_routes",
]
