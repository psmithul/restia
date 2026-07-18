"""Structured, privacy-safe actor context for immutable V3 action audits."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Mapping


SESSION_AUDIT_CONTEXT_KEY = "restia_action_audit_context"
_INTERFACES = frozenset({
    "web",
    "api",
    "cli",
    "email",
    "telegram",
    "voice",
    "automation",
    "home_link",
    "internal_tool",
    "domain_service",
})
_OUTCOMES = frozenset({"success", "failure", "denied", "cancelled"})
_PROTECTED_REFERENCE_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _bounded(value: object, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] if text else None


def _protected_reference(value: object) -> str | None:
    """Return a bounded one-way reference suitable for an immutable audit row."""

    text = _bounded(value, 4096)
    if not text:
        return None
    if _PROTECTED_REFERENCE_RE.fullmatch(text):
        return text
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bind_audit_context(
    db,
    *,
    actor_type: object,
    actor_id: object,
    interface: object,
    credential_type: object,
    credential_id: object | None = None,
    workflow_id: object | None = None,
) -> dict[str, Any]:
    """Persist one server-trusted attribution context on a DB session."""

    normalized_interface = _bounded(interface, 32)
    if normalized_interface not in _INTERFACES:
        raise ValueError("Unknown action audit interface")
    normalized_actor_id = _bounded(actor_id, 255)
    if not normalized_actor_id:
        raise ValueError("Action audit actor_id is required")
    context = {
        "actor_type": _bounded(actor_type, 32) or "service",
        "actor_id": normalized_actor_id,
        "interface": normalized_interface,
        "credential_type": _bounded(credential_type, 32) or "internal",
        "credential_id": _bounded(credential_id, 255),
        "workflow_id": _bounded(workflow_id, 255),
    }
    db.info[SESSION_AUDIT_CONTEXT_KEY] = context
    return dict(context)


def bind_request_audit_context(db, request, account) -> dict[str, Any]:
    """Bind one admitted request's actor metadata to its SQLAlchemy session.

    Only server-populated request state is consulted. Raw headers, bearer
    tokens, cookies, usernames, and email addresses are never copied into the
    audit payload.
    """

    state = getattr(request, "state", None)
    api_token = bool(getattr(state, "api_token", False))
    configured_interface = _bounded(
        getattr(state, "restia_interface", None), 32
    )
    if configured_interface not in _INTERFACES:
        if api_token:
            configured_interface = "api"
        elif os.getenv("AUTH_ENABLED", "true").lower() == "false":
            configured_interface = "web"
        else:
            configured_interface = "web"

    return _bind_audit_context(
        db,
        actor_type="api_token" if api_token else "account",
        actor_id=account.id,
        interface=configured_interface,
        credential_type="api_token" if api_token else (
            "local" if os.getenv("AUTH_ENABLED", "true").lower() == "false"
            else "session"
        ),
        credential_id=(
            _bounded(getattr(state, "api_token_id", None), 255)
            if api_token else None
        ),
        workflow_id=(
            getattr(state, "workflow_id", None)
            or getattr(state, "automation_run_id", None)
        ),
    )


def bind_service_audit_context(
    db,
    *,
    account_id: object,
    interface: str,
    actor_type: str = "account",
    credential_type: str = "internal",
    credential_id: object | None = None,
    workflow_id: object | None = None,
) -> dict[str, Any]:
    """Bind attribution for a trusted non-HTTP adapter or background workflow.

    Telegram, voice, CLI, automation, Home Link, and in-process domain adapters
    call this at their SQL boundary. Callers pass only stable row/run IDs; raw
    tokens, chat payloads, usernames, and connector secrets are never accepted
    implicitly from request headers.
    """

    return _bind_audit_context(
        db,
        actor_type=actor_type,
        actor_id=account_id,
        interface=interface,
        credential_type=credential_type,
        credential_id=credential_id,
        workflow_id=workflow_id,
    )


def build_action_audit_details(
    db,
    *,
    owner_id: str,
    reason: str,
    outcome: str = "success",
    idempotency_ref: object | None = None,
    reversible: bool = False,
    undo_ref: object | None = None,
    domain_details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge domain metadata with the required cross-interface audit shape."""

    raw_context = db.info.get(SESSION_AUDIT_CONTEXT_KEY) or {}
    actor_id = _bounded(raw_context.get("actor_id"), 255) or str(owner_id)
    interface = _bounded(raw_context.get("interface"), 32)
    if interface not in _INTERFACES:
        interface = "domain_service"
    actor_type = _bounded(raw_context.get("actor_type"), 32) or "service"
    credential_type = (
        _bounded(raw_context.get("credential_type"), 32) or "internal"
    )
    normalized_outcome = str(outcome or "").strip().lower()
    if normalized_outcome not in _OUTCOMES:
        raise ValueError("Unknown action audit outcome")

    audit = {
        "actor_type": actor_type,
        "actor_id": actor_id,
        "interface": interface,
        "credential_type": credential_type,
        "credential_id": _bounded(raw_context.get("credential_id"), 255),
        "workflow_id": _bounded(raw_context.get("workflow_id"), 255),
        "reason": _bounded(reason, 500) or "unspecified",
        "outcome": normalized_outcome,
        "idempotency_ref": _protected_reference(idempotency_ref),
        "reversible": bool(reversible),
        # Undo references are server-issued opaque IDs, not credentials. Keep
        # them usable by the future reversal endpoint; raw idempotency keys are
        # protected separately above.
        "undo_ref": _bounded(undo_ref, 255),
    }
    details = dict(domain_details or {})
    details["audit"] = audit
    return details
