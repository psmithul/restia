"""Authenticated Ambient Life OS capability, capture, and sync routes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.database import SessionLocal
from src.action_policy import (
    ActionPolicyDenied,
    ActionPolicyError,
    serialize_action_proposal,
)
from src.ambient_capabilities import (
    AmbientCapabilityDenied,
    AmbientCapabilityError,
    ambient_continuity,
    capture_ambient_signal,
    get_ambient_capability,
    list_ambient_capabilities,
    prepare_smart_home_action,
    set_ambient_capability,
    sync_offline_captures,
)
from src.identity import request_account_transaction
from src.life_graph import LifeGraphError, serialize_life_source
from src.profile_configuration_service import ProfileConfigurationConflict


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AmbientCapabilityUpdate(_StrictModel):
    enabled: bool
    operations: list[str] = Field(max_length=10)
    local_only: bool = True
    retention_days: int = Field(default=30, ge=1, le=3650)
    require_device_unlock: bool = True
    no_training: bool = True
    expected_version: int | None = Field(default=None, ge=0)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)


class AmbientCapture(_StrictModel):
    payload: dict[str, Any]
    observed_at: datetime | None = None
    idempotency_key: str = Field(min_length=1, max_length=256)


class OfflineCapture(_StrictModel):
    capability: str = Field(min_length=1, max_length=48)
    payload: dict[str, Any]
    observed_at: datetime | None = None
    idempotency_key: str = Field(min_length=1, max_length=256)


class OfflineSync(_StrictModel):
    captures: list[OfflineCapture] = Field(min_length=1, max_length=50)


class SmartHomeAction(_StrictModel):
    operation: str = Field(min_length=1, max_length=32)
    integration_id: str = Field(min_length=1, max_length=160)
    entity_id: str = Field(min_length=1, max_length=255)
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=4000)
    sources: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=256)


def _request_source(request: Request) -> str:
    return "api" if bool(getattr(request.state, "api_token", False)) else "browser"


def _trusted_device_session(request: Request) -> bool:
    """Require recent server-verified WebAuthn user verification.

    A bearer token, cookie, current-user marker, or client-side claim is never
    sufficient proof that a biometric or security-key gesture occurred.
    """

    if bool(getattr(request.state, "api_token", False)):
        return False
    manager = getattr(getattr(request.app, "state", None), "auth_manager", None)
    verifier = getattr(manager, "session_user_verification", None)
    if not callable(verifier):
        return False
    from routes.auth_routes import SESSION_COOKIE

    state = verifier(request.cookies.get(SESSION_COOKIE))
    return bool(isinstance(state, dict) and state.get("verified"))


def _raise_ambient_error(exc: Exception) -> None:
    if isinstance(exc, ProfileConfigurationConflict):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, (AmbientCapabilityDenied, ActionPolicyDenied)):
        raise HTTPException(403, str(exc)) from exc
    if isinstance(exc, (AmbientCapabilityError, LifeGraphError, ActionPolicyError)):
        raise HTTPException(400, str(exc)) from exc
    raise exc


def setup_ambient_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(prefix="/api/life/ambient", tags=["ambient-life"])

    @router.get("/capabilities")
    def capabilities(request: Request) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                items = list_ambient_capabilities(
                    db, owner_id=account.id if account is not None else "",
                )
                return {
                    "capabilities": items,
                    "count": len(items),
                    "privacy": {
                        "private_by_default": True,
                        "no_training": True,
                        "captures_are_instructions": False,
                        "external_actions_require_policy_approval": True,
                    },
                }
        except Exception as exc:
            db.rollback()
            _raise_ambient_error(exc)
        finally:
            db.close()

    @router.get("/capabilities/{capability}")
    def inspect_capability(request: Request, capability: str) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    raise AmbientCapabilityDenied("An account is required")
                return {
                    "capability": get_ambient_capability(
                        db, owner_id=account.id, capability=capability,
                    )
                }
        except Exception as exc:
            db.rollback()
            _raise_ambient_error(exc)
        finally:
            db.close()

    @router.put("/capabilities/{capability}")
    def update_capability(
        request: Request,
        capability: str,
        body: AmbientCapabilityUpdate,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True,
            ) as account:
                item = set_ambient_capability(
                    db,
                    account=account,
                    capability=capability,
                    value={
                        "enabled": body.enabled,
                        "operations": body.operations,
                        "local_only": body.local_only,
                        "retention_days": body.retention_days,
                        "require_device_unlock": body.require_device_unlock,
                        "no_training": body.no_training,
                    },
                    expected_version=body.expected_version,
                    source=_request_source(request),
                    idempotency_key=body.idempotency_key,
                )
                return {"capability": item}
        except Exception as exc:
            db.rollback()
            _raise_ambient_error(exc)
        finally:
            db.close()

    @router.post("/captures/{capability}", status_code=201)
    def capture(
        request: Request,
        capability: str,
        body: AmbientCapture,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True,
            ) as account:
                source, created = capture_ambient_signal(
                    db,
                    account=account,
                    capability=capability,
                    payload=body.payload,
                    observed_at=body.observed_at,
                    idempotency_key=body.idempotency_key,
                    trusted_device_session=_trusted_device_session(request),
                )
                return {
                    "source": serialize_life_source(source),
                    "created": created,
                    "interpreted_as_instruction": False,
                }
        except Exception as exc:
            db.rollback()
            _raise_ambient_error(exc)
        finally:
            db.close()

    @router.post("/offline/sync")
    def offline_sync(request: Request, body: OfflineSync) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True,
            ) as account:
                captures = [item.model_dump() for item in body.captures]
                results = sync_offline_captures(
                    db,
                    account=account,
                    captures=captures,
                    trusted_device_session=_trusted_device_session(request),
                )
                return {
                    "items": results,
                    "count": len(results),
                    "replay_safe": True,
                }
        except Exception as exc:
            db.rollback()
            _raise_ambient_error(exc)
        finally:
            db.close()

    @router.get("/continuity")
    def continuity(
        request: Request,
        cursor: str | None = Query(default=None, max_length=512),
        capability: str | None = Query(default=None, max_length=48),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "has_more": False, "next_cursor": None}
                return ambient_continuity(
                    db,
                    owner_id=account.id,
                    cursor=cursor,
                    capability=capability,
                    limit=limit,
                    trusted_device_session=_trusted_device_session(request),
                )
        except Exception as exc:
            db.rollback()
            _raise_ambient_error(exc)
        finally:
            db.close()

    @router.post("/smart-home/actions", status_code=201)
    def smart_home_action(
        request: Request,
        body: SmartHomeAction,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True,
            ) as account:
                created = prepare_smart_home_action(
                    db,
                    account=account,
                    operation=body.operation,
                    integration_id=body.integration_id,
                    entity_id=body.entity_id,
                    parameters=body.parameters,
                    reason=body.reason,
                    sources=body.sources,
                    idempotency_key=body.idempotency_key,
                    trusted_device_session=_trusted_device_session(request),
                )
                return {
                    "action": serialize_action_proposal(created.proposal),
                    "created": created.created,
                    "confirmation_token": created.confirmation_token,
                    # This route never performs the external side effect.  A
                    # reviewed connector executor must consume an approved
                    # proposal; model/client payload cannot become an executor.
                    "network_performed": False,
                }
        except Exception as exc:
            db.rollback()
            _raise_ambient_error(exc)
        finally:
            db.close()

    return router


__all__ = ["setup_ambient_routes"]
