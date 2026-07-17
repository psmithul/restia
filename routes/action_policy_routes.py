"""Owner-scoped V3 autonomy-policy and action-proposal API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from core.database import SessionLocal
from src.action_policy import (
    ActionApprovalRequired,
    ActionConfirmationExpired,
    ActionPolicyConflict,
    ActionPolicyDenied,
    ActionPolicyError,
    ActionPolicyNotFound,
    approve_action,
    complete_action,
    create_action_proposal,
    fail_action,
    get_action_proposal,
    get_effective_action_policy,
    issue_confirmation,
    list_action_policies,
    list_action_proposals,
    reject_action,
    reverse_action,
    serialize_action_policy,
    serialize_action_proposal,
    set_action_policy,
    start_action,
)
from src.identity import request_account_transaction


class PolicyUpdate(BaseModel):
    max_autonomy: int = Field(ge=1, le=6)
    external_requires_confirmation: bool = True
    enabled: bool = True
    rules: dict[str, Any] = Field(default_factory=dict)
    version: int | None = Field(default=None, ge=1)


class ActionCreate(BaseModel):
    domain: str = Field(min_length=1, max_length=48)
    action: str = Field(min_length=1, max_length=80)
    autonomy_level: int = Field(ge=1, le=6)
    target_type: str = Field(min_length=1, max_length=48)
    target_id: str | None = Field(default=None, max_length=255)
    payload: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="", max_length=4000)
    sources: dict[str, Any] = Field(default_factory=dict)
    external: bool = Field(
        default=False,
        description=(
            "Caller risk hint only; Restia's server registry validates the "
            "action, domain, and target and determines effective risk."
        ),
    )
    idempotency_key: str = Field(min_length=1, max_length=1024)


class VersionBody(BaseModel):
    version: int = Field(ge=1)


class ExecuteBody(VersionBody):
    undo_ref: str | None = Field(default=None, max_length=255)


class ConfirmationBody(VersionBody):
    purpose: str = Field(default="approve", pattern="^(approve|reverse)$")


class ApproveBody(VersionBody):
    confirmation_token: str = Field(min_length=1, max_length=256)


class RejectBody(VersionBody):
    reason: str = Field(default="", max_length=1000)


class ResultBody(VersionBody):
    result: dict[str, Any] = Field(default_factory=dict)


class CompleteBody(ResultBody):
    undo_ref: str | None = Field(default=None, max_length=255)


class ReverseBody(ResultBody):
    confirmation_token: str | None = Field(default=None, max_length=256)


def _raise_domain_error(exc: ActionPolicyError) -> None:
    if isinstance(exc, ActionPolicyNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, ActionConfirmationExpired):
        raise HTTPException(410, str(exc)) from exc
    if isinstance(exc, ActionApprovalRequired):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, ActionPolicyConflict):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, ActionPolicyDenied):
        raise HTTPException(403, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


def setup_action_policy_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(prefix="/api/life", tags=["life-actions"])

    @router.get("/policies")
    def list_policies(request: Request) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                policies = list_action_policies(
                    db, owner_id=account.id if account is not None else ""
                )
                return {
                    "policies": [serialize_action_policy(row) for row in policies],
                    "count": len(policies),
                }
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/policies/{domain}")
    def inspect_policy(request: Request, domain: str) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                policy = get_effective_action_policy(
                    db, owner_id=account.id if account is not None else "", domain=domain
                )
                return {"policy": serialize_action_policy(policy)}
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.put("/policies/{domain}")
    def update_policy(
        request: Request, domain: str, body: PolicyUpdate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                policy = set_action_policy(
                    db,
                    owner_id=account.id,
                    domain=domain,
                    max_autonomy=body.max_autonomy,
                    external_requires_confirmation=body.external_requires_confirmation,
                    enabled=body.enabled,
                    rules=body.rules,
                    expected_version=body.version,
                )
                return {"policy": serialize_action_policy(policy)}
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/actions")
    def list_actions(
        request: Request,
        state: str | None = Query(default=None),
        domain: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"actions": [], "count": 0}
                rows = list_action_proposals(
                    db,
                    owner_id=account.id,
                    state=state,
                    domain=domain,
                    limit=limit,
                )
                return {
                    "actions": [serialize_action_proposal(row) for row in rows],
                    "count": len(rows),
                }
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/actions", status_code=201)
    def create_action(request: Request, body: ActionCreate) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                created = create_action_proposal(
                    db,
                    owner_id=account.id,
                    domain=body.domain,
                    action=body.action,
                    autonomy_level=body.autonomy_level,
                    target_type=body.target_type,
                    target_id=body.target_id,
                    payload=body.payload,
                    reason=body.reason,
                    sources=body.sources,
                    external=body.external,
                    idempotency_key=body.idempotency_key,
                )
                return {
                    "action": serialize_action_proposal(created.proposal),
                    "created": created.created,
                    "confirmation_token": created.confirmation_token,
                }
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.get("/actions/{proposal_id}")
    def inspect_action(request: Request, proposal_id: str) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise ActionPolicyNotFound("Action proposal not found")
                proposal = get_action_proposal(
                    db, owner_id=account.id, proposal_id=proposal_id
                )
                return {"action": serialize_action_proposal(proposal)}
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/actions/{proposal_id}/confirmation")
    def refresh_confirmation(
        request: Request, proposal_id: str, body: ConfirmationBody
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                challenge = issue_confirmation(
                    db,
                    owner_id=account.id,
                    proposal_id=proposal_id,
                    expected_version=body.version,
                    purpose=body.purpose,
                )
                return {
                    "action": serialize_action_proposal(challenge.proposal),
                    "confirmation_token": challenge.confirmation_token,
                    "purpose": challenge.purpose,
                }
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/actions/{proposal_id}/approve")
    def approve(
        request: Request, proposal_id: str, body: ApproveBody
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                proposal = approve_action(
                    db,
                    owner_id=account.id,
                    proposal_id=proposal_id,
                    expected_version=body.version,
                    confirmation_token=body.confirmation_token,
                )
                return {"action": serialize_action_proposal(proposal)}
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/actions/{proposal_id}/reject")
    def reject(
        request: Request, proposal_id: str, body: RejectBody
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                proposal = reject_action(
                    db,
                    owner_id=account.id,
                    proposal_id=proposal_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {"action": serialize_action_proposal(proposal)}
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/actions/{proposal_id}/execute")
    def execute(
        request: Request, proposal_id: str, body: ExecuteBody
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                proposal = start_action(
                    db,
                    owner_id=account.id,
                    proposal_id=proposal_id,
                    expected_version=body.version,
                    undo_ref=body.undo_ref,
                )
                return {"action": serialize_action_proposal(proposal)}
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/actions/{proposal_id}/complete")
    def complete(
        request: Request, proposal_id: str, body: CompleteBody
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                proposal = complete_action(
                    db,
                    owner_id=account.id,
                    proposal_id=proposal_id,
                    expected_version=body.version,
                    result=body.result,
                    undo_ref=body.undo_ref,
                )
                return {"action": serialize_action_proposal(proposal)}
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/actions/{proposal_id}/fail")
    def fail(
        request: Request, proposal_id: str, body: ResultBody
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                proposal = fail_action(
                    db,
                    owner_id=account.id,
                    proposal_id=proposal_id,
                    expected_version=body.version,
                    result=body.result,
                )
                return {"action": serialize_action_proposal(proposal)}
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/actions/{proposal_id}/reverse")
    def reverse(
        request: Request, proposal_id: str, body: ReverseBody
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                proposal = reverse_action(
                    db,
                    owner_id=account.id,
                    proposal_id=proposal_id,
                    expected_version=body.version,
                    result=body.result,
                    confirmation_token=body.confirmation_token,
                )
                return {"action": serialize_action_proposal(proposal)}
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    return router
