"""Authenticated security posture for the Restia V3 account boundary."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from core.database import SessionLocal
from src.identity import request_account_transaction
from src.security_posture import build_security_posture


def setup_security_routes(
    *,
    session_factory=SessionLocal,
    backup_dir: str | Path | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api/life", tags=["life-security"])

    @router.get("/security-posture")
    def security_posture(request: Request):
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    raise HTTPException(404, "Account not found")
                return build_security_posture(
                    db, account=account, backup_dir=backup_dir,
                )
        finally:
            db.close()

    return router


__all__ = ["setup_security_routes"]
