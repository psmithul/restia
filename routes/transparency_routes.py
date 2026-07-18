"""Owner-scoped audit inspection and portable V3 control-plane export."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from core.database import (
    ActionAudit,
    ActionPolicy,
    ActionProposal,
    EntityLink,
    InboxItem,
    LifeEntity,
    LifeEntityVersion,
    LifeSource,
    SessionLocal,
)


_SENSITIVE_EXPORT_KEYS = frozenset({
    "api_key", "authorization", "client_secret", "confirmation_digest",
    "cookie", "encryption_key", "password", "password_hash", "refresh_token",
    "secret", "session_token", "token",
})


def _redact_sensitive(value: Any) -> Any:
    """Remove authentication material accidentally embedded in JSON fields.

    Canonical credential tables are not selected by the export at all.  This
    second fence covers free-form metadata, policy rules, and historical audit
    payloads without hiding ordinary content digests or provenance hashes.
    """

    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            sensitive = (
                normalized in _SENSITIVE_EXPORT_KEYS
                or normalized.endswith("_password")
                or normalized.endswith("_secret")
                or normalized.endswith("_api_key")
                or normalized.endswith("_access_token")
                or normalized.endswith("_refresh_token")
            )
            redacted[str(key)] = "[REDACTED]" if sensitive else _redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive(item) for item in value]
    return value
from src.action_policy import serialize_action_policy, serialize_action_proposal
from src.identity import request_account_transaction
from src.life_core import serialize_inbox_item
from src.life_graph import (
    serialize_entity_link,
    serialize_life_entity,
    serialize_life_entity_version,
    serialize_life_source,
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _serialize_audit(row: ActionAudit) -> dict[str, Any]:
    details = dict(row.details or {})
    audit = dict(details.get("audit") or {})
    return {
        "id": row.id,
        "action": row.action,
        "entity_type": row.entity_type,
        "entity_id": row.entity_id,
        "inputs": _redact_sensitive(dict(row.before_state or {})),
        "changes": _redact_sensitive(dict(row.after_state or {})),
        "reason": str(audit.get("reason") or ""),
        "actor": {
            "type": audit.get("actor_type"),
            "id": audit.get("actor_id"),
            "interface": audit.get("interface"),
            "credential_type": audit.get("credential_type"),
        },
        "workflow": {
            "id": audit.get("workflow_id"),
            "outcome": audit.get("outcome"),
            "idempotency_ref": audit.get("idempotency_ref"),
        },
        "reversal": {
            "available": bool(audit.get("reversible")),
            "undo_ref": audit.get("undo_ref"),
        },
        "details": _redact_sensitive({
            key: value for key, value in details.items() if key != "audit"
        }),
        "created_at": _iso(row.created_at),
    }


def _bounded_rows(query, *, limit: int) -> tuple[list[Any], bool]:
    rows = query.limit(limit + 1).all()
    return rows[:limit], len(rows) > limit


def setup_transparency_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(prefix="/api/life", tags=["life-transparency"])

    @router.get("/audit")
    def list_audit(
        request: Request,
        action: str | None = Query(default=None, max_length=80),
        entity_type: str | None = Query(default=None, max_length=48),
        entity_id: str | None = Query(default=None, max_length=255),
        before: datetime | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                query = db.query(ActionAudit).filter(
                    ActionAudit.owner_id == account.id,
                )
                if action:
                    query = query.filter(ActionAudit.action == action)
                if entity_type:
                    query = query.filter(ActionAudit.entity_type == entity_type)
                if entity_id:
                    query = query.filter(ActionAudit.entity_id == entity_id)
                if before:
                    cursor = before
                    if cursor.tzinfo is not None:
                        cursor = cursor.astimezone(timezone.utc).replace(tzinfo=None)
                    query = query.filter(ActionAudit.created_at < cursor)
                rows, truncated = _bounded_rows(
                    query.order_by(
                        ActionAudit.created_at.desc(), ActionAudit.id.desc(),
                    ),
                    limit=limit,
                )
                return {
                    "items": [_serialize_audit(row) for row in rows],
                    "count": len(rows),
                    "truncated": truncated,
                }
        finally:
            db.close()

    @router.get("/audit/{audit_id}")
    def inspect_audit(request: Request, audit_id: str) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    raise HTTPException(404, "Audit record not found")
                row = db.query(ActionAudit).filter(
                    ActionAudit.id == audit_id,
                    ActionAudit.owner_id == account.id,
                ).first()
                if row is None:
                    raise HTTPException(404, "Audit record not found")
                return {"audit": _serialize_audit(row)}
        finally:
            db.close()

    @router.get("/privacy-export")
    def privacy_export(
        request: Request,
        limit_per_collection: int = Query(default=5_000, ge=1, le=20_000),
    ) -> dict[str, Any]:
        """Export canonical V3 control-plane records for the authenticated owner.

        Connector credentials, confirmation digests, sessions, password hashes,
        encryption keys, and other authentication material are deliberately not
        selected.  Each collection reports truncation instead of silently
        claiming that a bounded export is complete.
        """

        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False,
            ) as account:
                if account is None:
                    raise HTTPException(404, "Account not found")

                def collect(query, serializer):
                    rows, truncated = _bounded_rows(query, limit=limit_per_collection)
                    return {
                        "items": [
                            _redact_sensitive(serializer(row)) for row in rows
                        ],
                        "count": len(rows), "truncated": truncated,
                    }

                sources = collect(
                    db.query(LifeSource).filter(LifeSource.owner_id == account.id).order_by(
                        LifeSource.created_at.asc(), LifeSource.id.asc(),
                    ),
                    serialize_life_source,
                )
                entities = collect(
                    db.query(LifeEntity).filter(LifeEntity.owner_id == account.id).order_by(
                        LifeEntity.created_at.asc(), LifeEntity.id.asc(),
                    ),
                    serialize_life_entity,
                )
                links = collect(
                    db.query(EntityLink).filter(EntityLink.owner_id == account.id).order_by(
                        EntityLink.created_at.asc(), EntityLink.id.asc(),
                    ),
                    serialize_entity_link,
                )
                versions = collect(
                    db.query(LifeEntityVersion).filter(
                        LifeEntityVersion.owner_id == account.id,
                    ).order_by(
                        LifeEntityVersion.created_at.asc(), LifeEntityVersion.id.asc(),
                    ),
                    serialize_life_entity_version,
                )
                inbox = collect(
                    db.query(InboxItem).filter(InboxItem.owner_id == account.id).order_by(
                        InboxItem.created_at.asc(), InboxItem.id.asc(),
                    ),
                    serialize_inbox_item,
                )
                audits = collect(
                    db.query(ActionAudit).filter(ActionAudit.owner_id == account.id).order_by(
                        ActionAudit.created_at.asc(), ActionAudit.id.asc(),
                    ),
                    _serialize_audit,
                )
                policies = collect(
                    db.query(ActionPolicy).filter(ActionPolicy.owner_id == account.id).order_by(
                        ActionPolicy.domain.asc(), ActionPolicy.id.asc(),
                    ),
                    serialize_action_policy,
                )
                proposals = collect(
                    db.query(ActionProposal).filter(
                        ActionProposal.owner_id == account.id,
                    ).order_by(
                        ActionProposal.created_at.asc(), ActionProposal.id.asc(),
                    ),
                    serialize_action_proposal,
                )
                collections = {
                    "sources": sources, "entities": entities, "links": links,
                    "entity_versions": versions, "inbox": inbox,
                    "audits": audits, "policies": policies,
                    "action_proposals": proposals,
                }
                return {
                    "schema": "restia.v3.privacy-export",
                    "schema_version": 1,
                    "exported_at": _iso(datetime.now(timezone.utc)),
                    "principal": {
                        "id": account.id,
                        "username": account.username,
                        "status": account.status,
                        "auth_epoch": int(account.auth_epoch or 1),
                    },
                    "excludes": [
                        "password_hashes", "sessions", "confirmation_digests",
                        "connector_credentials", "encryption_keys",
                    ],
                    "collections": collections,
                    "complete": not any(
                        value["truncated"] for value in collections.values()
                    ),
                }
        finally:
            db.close()

    return router
