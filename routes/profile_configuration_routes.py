"""Authenticated cross-interface API for profile configuration authority."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.database import SessionLocal
from src.identity import request_account_transaction
from src.profile_configuration_service import (
    ProfileConfigurationConflict,
    ProfileConfigurationError,
    ProfileConfigurationNotFound,
    delete_configuration,
    get_configuration,
    list_configurations,
    put_configuration,
    serialize_configuration,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigurationPut(_StrictModel):
    value: Any
    expected_version: int | None = Field(default=None, ge=0)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)


class ConfigurationDelete(_StrictModel):
    expected_version: int = Field(ge=1)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)


def _raise_configuration_error(exc: ProfileConfigurationError) -> None:
    if isinstance(exc, ProfileConfigurationNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, ProfileConfigurationConflict):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


def _request_source(request: Request) -> str:
    return "api" if bool(getattr(request.state, "api_token", False)) else "browser"


def setup_profile_configuration_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(
        prefix="/api/profile/configuration",
        tags=["profile-configuration"],
    )

    @router.get("")
    def list_profile_configuration(
        request: Request,
        namespace: str | None = Query(default=None, max_length=32),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("profile:read",), write=False,
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                rows, truncated = list_configurations(
                    db,
                    owner_id=account.id,
                    namespace=namespace,
                    limit=limit,
                )
                return {
                    "items": [serialize_configuration(row) for row in rows],
                    "count": len(rows),
                    "truncated": truncated,
                }
        except ProfileConfigurationError as exc:
            db.rollback()
            _raise_configuration_error(exc)
        finally:
            db.close()

    @router.get("/{namespace}/{key}")
    def get_profile_configuration(
        request: Request, namespace: str, key: str,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("profile:read",), write=False,
            ) as account:
                if account is None:
                    raise ProfileConfigurationNotFound("Profile configuration not found")
                record = get_configuration(
                    db,
                    owner_id=account.id,
                    namespace=namespace,
                    key=key,
                )
                return {"configuration": serialize_configuration(record)}
        except ProfileConfigurationError as exc:
            db.rollback()
            _raise_configuration_error(exc)
        finally:
            db.close()

    @router.put("/{namespace}/{key}")
    def put_profile_configuration(
        request: Request, namespace: str, key: str, body: ConfigurationPut,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("profile:write",), write=True,
            ) as account:
                result = put_configuration(
                    db,
                    account=account,
                    namespace=namespace,
                    key=key,
                    value=body.value,
                    expected_version=body.expected_version,
                    source=_request_source(request),
                    idempotency_key=body.idempotency_key,
                )
                return {
                    "configuration": serialize_configuration(result.record),
                    "created": result.created,
                    "changed": result.changed,
                    "idempotent": result.idempotent,
                }
        except ProfileConfigurationError as exc:
            db.rollback()
            _raise_configuration_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/{namespace}/{key}")
    def delete_profile_configuration(
        request: Request, namespace: str, key: str, body: ConfigurationDelete,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("profile:write",), write=True,
            ) as account:
                result = delete_configuration(
                    db,
                    account=account,
                    namespace=namespace,
                    key=key,
                    expected_version=body.expected_version,
                    source=_request_source(request),
                    idempotency_key=body.idempotency_key,
                )
                return {
                    "configuration": serialize_configuration(
                        result.record, include_deleted=True,
                    ),
                    "changed": result.changed,
                    "idempotent": result.idempotent,
                }
        except ProfileConfigurationError as exc:
            db.rollback()
            _raise_configuration_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router


__all__ = ["setup_profile_configuration_routes"]
