"""Read-only owner-scoped API for deterministic V3 proactive intelligence."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from core.database import SessionLocal
from src.identity import request_account_transaction
from src.proactive_intelligence import (
    ProactiveInputError,
    ProactiveStateError,
    proactive_intelligence_report,
)


def setup_proactive_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(prefix="/api/life/proactive", tags=["life-proactive"])

    @router.get("/report")
    def report(
        request: Request,
        as_of: datetime = Query(
            description="ISO-8601 report time with an explicit UTC offset",
        ),
        horizon_days: int = Query(default=30, ge=1, le=90),
        lookback_days: int = Query(default=30, ge=1, le=366),
        stale_project_days: int = Query(default=30, ge=1, le=3650),
        stale_decision_days: int = Query(default=30, ge=1, le=3650),
        daily_capacity_minutes: int = Query(default=480, ge=30, le=1440),
        limit: int = Query(default=100, ge=1, le=200),
        scan_limit: int = Query(default=500, ge=1, le=500),
        explicit_interrupt: list[str] | None = Query(default=None),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    return {
                        "schema_version": 1,
                        "items": [],
                        "interruptions": [],
                        "digest": [],
                        "count": 0,
                        "total_signals_before_limit": 0,
                        "domain_counts": {},
                        "truncated": False,
                        "as_of_offset": as_of.isoformat(),
                        "routing_policy": {
                            "interrupt_when": [
                                "explicitly_requested",
                                "high_risk",
                                "urgent_and_important_and_time_sensitive",
                            ],
                            "otherwise": "digest",
                        },
                        "safety_policy": {
                            "deterministic": True,
                            "model_inference": False,
                            "record_only": True,
                            "can_mutate": False,
                            "can_send_or_notify": False,
                        },
                    }
                return proactive_intelligence_report(
                    db,
                    owner_id=account.id,
                    as_of=as_of,
                    horizon_days=horizon_days,
                    lookback_days=lookback_days,
                    stale_project_days=stale_project_days,
                    stale_decision_days=stale_decision_days,
                    daily_capacity_minutes=daily_capacity_minutes,
                    limit=limit,
                    scan_limit=scan_limit,
                    explicit_interrupts=explicit_interrupt,
                )
        except ProactiveInputError as exc:
            db.rollback()
            raise HTTPException(400, str(exc)) from exc
        except ProactiveStateError as exc:
            db.rollback()
            raise HTTPException(409, str(exc)) from exc
        finally:
            db.close()

    return router


__all__ = ["setup_proactive_routes"]
