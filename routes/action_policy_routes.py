"""Owner-scoped V3 autonomy-policy and action-proposal API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

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
    serialize_action_policy,
    serialize_action_proposal,
    set_action_policy,
)
from src.calendar_action_executor import (
    execute_calendar_action,
    is_server_calendar_action,
    reverse_calendar_action,
)
from src.email_outbound import (
    is_server_email_action,
    mark_email_action_rejected,
    queue_approved_email_action,
)
from src.identity import request_account_transaction
from src.ambient_capabilities import (
    call_smart_home_connector,
    finish_smart_home_execution,
    is_server_smart_home_action,
    start_smart_home_execution,
)


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
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class ExecuteBody(VersionBody):
    """A client supplies only the proposal version.

    Reversal references are created by the reviewed server dispatcher in the
    same transaction as the domain mutation; accepting one from a client would
    let an untrusted caller invent an undo path.
    """


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


class ReverseBody(VersionBody):
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


def _serialize_action(db, proposal) -> dict[str, Any]:
    """Add only reviewed server capabilities to the public proposal shape.

    The browser must never infer that an arbitrary proposal can execute or
    reverse merely because it has an action name or an undo reference.  These
    booleans come from the same exact-tuple dispatcher used by the write
    routes; no confirmation material or internal reversal reference is added.
    """

    payload = serialize_action_proposal(proposal)
    reviewed_calendar_action = is_server_calendar_action(proposal)
    reviewed_email_action = is_server_email_action(db, proposal)
    reviewed_smart_home_action = is_server_smart_home_action(proposal)
    payload["reviewed_server_executor"] = bool(
        reviewed_calendar_action
        or reviewed_email_action
        or reviewed_smart_home_action
    )
    payload["reviewed_server_reversal"] = bool(
        reviewed_calendar_action and proposal.undo_ref
    )
    payload["transparency"] = {
        "inputs": dict(payload.get("payload") or {}),
        "changes": dict(payload.get("result") or {}),
        "reason": str(payload.get("reason") or ""),
        "actor": {
            "prepared_by": "restia_workflow",
            "approved_by_account_id": payload.get("approved_by_account_id"),
        },
        "workflow": {
            "domain": payload.get("domain"),
            "action": payload.get("action"),
            "state": payload.get("state"),
            "autonomy_level": payload.get("autonomy_level"),
            "external": bool(payload.get("external")),
            "requires_confirmation": bool(payload.get("requires_confirmation")),
        },
        "reversal": {
            "reference_recorded": bool(payload.get("undo_ref")),
            "reviewed_server_reversal": payload["reviewed_server_reversal"],
            "fresh_confirmation_required": bool(
                payload.get("requires_confirmation")
            ),
        },
    }
    return payload


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
                    "actions": [_serialize_action(db, row) for row in rows],
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
                    "action": _serialize_action(db, created.proposal),
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
                return {"action": _serialize_action(db, proposal)}
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
                    "action": _serialize_action(db, challenge.proposal),
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
                return {"action": _serialize_action(db, proposal)}
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
                existing = get_action_proposal(
                    db, owner_id=account.id, proposal_id=proposal_id
                )
                reviewed_email_action = is_server_email_action(db, existing)
                proposal = reject_action(
                    db,
                    owner_id=account.id,
                    proposal_id=proposal_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                if reviewed_email_action:
                    mark_email_action_rejected(db, proposal)
                return {"action": _serialize_action(db, proposal)}
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    @router.post("/actions/{proposal_id}/execute")
    async def execute(
        request: Request, proposal_id: str, body: ExecuteBody
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            smart_home_execution = None
            owner_username = None
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                existing = get_action_proposal(
                    db,
                    owner_id=account.id,
                    proposal_id=proposal_id,
                )
                if is_server_calendar_action(existing):
                    executed = execute_calendar_action(
                        db,
                        account=account,
                        proposal_id=proposal_id,
                        expected_version=body.version,
                    )
                    return {
                        "action": _serialize_action(db, executed.proposal)
                    }
                if is_server_email_action(db, existing):
                    queued = queue_approved_email_action(
                        db,
                        account=account,
                        proposal_id=proposal_id,
                        expected_version=body.version,
                    )
                    return {
                        "action": _serialize_action(db, queued.proposal),
                        "delivery": {
                            "id": queued.delivery.id,
                            "state": queued.delivery.state,
                            "network_performed": False,
                        },
                    }
                if is_server_smart_home_action(existing):
                    smart_home_execution = start_smart_home_execution(
                        db,
                        owner_id=account.id,
                        proposal_id=proposal_id,
                        expected_version=body.version,
                    )
                    owner_username = account.username
                else:
                    raise ActionPolicyError(
                        "Action has no reviewed server executor"
                    )
            connector_result = await call_smart_home_connector(
                smart_home_execution, owner_username=owner_username,
            )
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                proposal = finish_smart_home_execution(
                    db,
                    owner_id=account.id,
                    execution=smart_home_execution,
                    connector_result=connector_result,
                )
                serialized = _serialize_action(db, proposal)
            exit_code = connector_result.get("exit_code", 1)
            if exit_code != 0 and exit_code != "0":
                raise HTTPException(
                    502,
                    {
                        "message": str(
                            connector_result.get("error")
                            or "Smart-home connector failed"
                        )[:1_000],
                        "action": serialized,
                    },
                )
            return {
                "action": serialized,
                "delivery": {
                    "connector_id": smart_home_execution.integration_id,
                    "method": smart_home_execution.method,
                    "path": smart_home_execution.path,
                    "network_performed": True,
                },
            }
        except HTTPException:
            raise
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
                existing = get_action_proposal(
                    db, owner_id=account.id, proposal_id=proposal_id
                )
                if (
                    is_server_calendar_action(existing)
                    or is_server_email_action(db, existing)
                    or is_server_smart_home_action(existing)
                ):
                    raise ActionPolicyConflict(
                        "Reviewed actions are completed only by their server executor"
                    )
                proposal = complete_action(
                    db,
                    owner_id=account.id,
                    proposal_id=proposal_id,
                    expected_version=body.version,
                    result=body.result,
                    undo_ref=body.undo_ref,
                )
                return {"action": _serialize_action(db, proposal)}
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
                existing = get_action_proposal(
                    db, owner_id=account.id, proposal_id=proposal_id
                )
                if (
                    is_server_calendar_action(existing)
                    or is_server_email_action(db, existing)
                    or is_server_smart_home_action(existing)
                ):
                    raise ActionPolicyConflict(
                        "Reviewed action failures are recorded only by their server executor"
                    )
                proposal = fail_action(
                    db,
                    owner_id=account.id,
                    proposal_id=proposal_id,
                    expected_version=body.version,
                    result=body.result,
                )
                return {"action": _serialize_action(db, proposal)}
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
                existing = get_action_proposal(
                    db,
                    owner_id=account.id,
                    proposal_id=proposal_id,
                )
                if not is_server_calendar_action(existing):
                    raise ActionPolicyError(
                        "Action has no reviewed server reversal"
                    )
                reversed_action = reverse_calendar_action(
                    db,
                    account=account,
                    proposal_id=proposal_id,
                    expected_version=body.version,
                    confirmation_token=body.confirmation_token,
                )
                return {
                    "action": _serialize_action(db, reversed_action.proposal)
                }
        except ActionPolicyError as exc:
            db.rollback()
            _raise_domain_error(exc)
        finally:
            db.close()

    return router
