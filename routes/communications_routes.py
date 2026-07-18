"""HTTP interface for the owner-scoped, read-only Communications Hub."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.database import SessionLocal
from src.communications_hub import (
    CommunicationItemNotFound,
    CommunicationsHubError,
    communications_view,
    convert_communication_item,
    empty_communications_view,
)
from src.identity import request_account_transaction
from src.life_core import (
    LifeCoreConflict,
    LifeCoreError,
    LifeCoreNotFound,
    LifeCoreUnsupported,
)


class CommunicationConversion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(max_length=32)
    process: bool = True
    project_id: str | None = Field(default=None, max_length=36)
    title: str | None = Field(default=None, max_length=240)


def _raise_error(exc: Exception) -> None:
    if isinstance(exc, (CommunicationItemNotFound, LifeCoreNotFound)):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, LifeCoreConflict):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, LifeCoreUnsupported):
        raise HTTPException(409, {
            "status": "unsupported",
            "kind": exc.kind,
            "message": str(exc),
        }) from exc
    raise HTTPException(400, str(exc)) from exc


def setup_communications_routes(
    *,
    session_factory=SessionLocal,
    email_cache_paths: Sequence[Path] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/communications", tags=["communications"])

    @router.get("")
    def get_communications(
        request: Request,
        q: str = Query(default="", max_length=500),
        connectors: str | None = Query(default=None, max_length=240),
        unread_only: bool = Query(default=False),
        important_only: bool = Query(default=False),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return empty_communications_view()
                selected = None
                if connectors is not None:
                    selected = [
                        value.strip()
                        for value in connectors.split(",")
                        if value.strip()
                    ]
                return communications_view(
                    db,
                    account=account,
                    connectors=selected,
                    query=q,
                    unread_only=unread_only,
                    important_only=important_only,
                    limit=limit,
                    email_cache_paths=email_cache_paths,
                )
        except (CommunicationsHubError, LifeCoreError) as exc:
            db.rollback()
            _raise_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/items/{item_id}/convert")
    def convert_item(
        request: Request,
        item_id: str,
        body: CommunicationConversion,
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                return convert_communication_item(
                    db,
                    account=account,
                    item_id=item_id,
                    kind=body.kind,
                    process=body.process,
                    project_id=body.project_id,
                    title=body.title,
                    email_cache_paths=email_cache_paths,
                )
        except (CommunicationsHubError, LifeCoreError) as exc:
            db.rollback()
            _raise_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router
