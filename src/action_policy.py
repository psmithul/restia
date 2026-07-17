"""Fail-closed autonomy policy and durable action-proposal lifecycle.

This module is the single backend boundary for V3 action autonomy.  Models and
connectors may prepare proposals, but approval is accepted only from a bound
human account session.  Raw idempotency and confirmation secrets are never
stored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from sqlalchemy.exc import IntegrityError

from core.database import ActionPolicy, ActionProposal, utcnow_naive
from src.audit_context import SESSION_AUDIT_CONTEXT_KEY
from src.life_core import append_action_audit


AUTONOMY_LEVELS = frozenset(range(1, 7))
ACTION_STATES = frozenset({
    "prepared",
    "approved",
    "executing",
    "completed",
    "rejected",
    "failed",
    "reversed",
    "expired",
})

CONFIRMATION_TTL = timedelta(minutes=10)
APPROVED_EXECUTION_TTL = timedelta(minutes=5)

# Unknown domains fail closed at Prepare.  A persisted policy can tighten or
# deliberately raise that cap.  Domains whose useful actions are necessarily
# external/high-risk may reach 5/6, but those levels can never skip approval.
SAFE_DOMAIN_DEFAULTS: dict[str, int] = {
    "general": 3,
    "information": 3,
    "memory": 3,
    "search": 3,
    "tasks": 4,
    "planning": 4,
    "calendar": 4,
    "notes": 4,
    "projects": 4,
    "files": 4,
    "health": 4,
    "home": 4,
    "communications": 5,
    "email": 5,
    "messaging": 5,
    "bookings": 5,
    "travel": 5,
    "smart_home": 5,
    "finance": 6,
    "financial": 6,
    "money": 6,
    "banking": 6,
    "payments": 6,
    "legal": 6,
    "medical": 6,
    "healthcare": 6,
    "destructive": 6,
    "permissions": 6,
    "security": 6,
}

_CRITICAL_DOMAINS = frozenset({
    "finance",
    "financial",
    "money",
    "banking",
    "payments",
    "legal",
    "medical",
    "healthcare",
    "destructive",
    "permissions",
    "security",
})
_DESTRUCTIVE_ACTION_WORDS = frozenset({
    "delete",
    "destroy",
    "drop",
    "erase",
    "format",
    "overwrite",
    "purge",
    "revoke",
    "shred",
    "truncate",
    "unlink",
    "wipe",
})
_OUTBOUND_ACTION_WORDS = frozenset({
    "book",
    "cancel",
    "control",
    "forward",
    "message",
    "notify",
    "post",
    "publish",
    "reply",
    "reserve",
    "send",
    "submit",
    "unlock",
})
_FINANCIAL_ACTION_WORDS = frozenset({
    "bank",
    "banking",
    "buy",
    "deposit",
    "financial",
    "invest",
    "money",
    "pay",
    "payment",
    "purchase",
    "sell",
    "trade",
    "transaction",
    "transfer",
    "wire",
    "withdraw",
})
_LEGAL_ACTION_WORDS = frozenset({
    "agreement",
    "contract",
    "lawsuit",
    "legal",
    "litigation",
})
_MEDICAL_ACTION_WORDS = frozenset({
    "diagnose",
    "diagnosis",
    "dose",
    "dosage",
    "medical",
    "medication",
    "prescribe",
    "prescription",
    "surgery",
    "treatment",
})
_PERMISSION_ACTION_WORDS = frozenset({
    "access",
    "credential",
    "permission",
    "privilege",
    "role",
})
_PERMISSION_MUTATION_WORDS = frozenset({
    "add",
    "assign",
    "change",
    "grant",
    "remove",
    "reset",
    "revoke",
    "set",
    "update",
})
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,79}$")
_MAX_JSON_BYTES = 128 * 1024


class ActionPolicyError(ValueError):
    pass


class ActionPolicyNotFound(ActionPolicyError):
    pass


class ActionPolicyConflict(ActionPolicyError):
    pass


class ActionPolicyDenied(ActionPolicyError):
    pass


class ActionApprovalRequired(ActionPolicyDenied):
    pass


class ActionConfirmationExpired(ActionPolicyError):
    pass


@dataclass(frozen=True)
class EffectiveActionPolicy:
    id: str | None
    owner_id: str
    domain: str
    max_autonomy: int
    external_requires_confirmation: bool
    enabled: bool
    rules: dict[str, Any]
    version: int
    persisted: bool


@dataclass(frozen=True)
class ProposalCreation:
    proposal: ActionProposal
    created: bool
    confirmation_token: str | None = None


@dataclass(frozen=True)
class ConfirmationChallenge:
    proposal: ActionProposal
    confirmation_token: str
    purpose: str


@dataclass(frozen=True)
class RegisteredActionRisk:
    """Server-owned risk contract for one normalized action identifier.

    Callers may request a stricter autonomy level or mark an action external,
    but they may never lower these values.  Reversible Level-4 actions are safe
    only for their registered domain/target combinations.  Observe, suggest,
    and prepare entries are intentionally non-executable.
    """

    minimum_autonomy: int
    external: bool = False
    executable: bool = True
    allowed_domains: frozenset[str] = frozenset()
    allowed_target_types: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.minimum_autonomy not in AUTONOMY_LEVELS:
            raise ValueError("registered autonomy must be from 1 to 6")
        if self.minimum_autonomy < 4 and self.executable:
            raise ValueError("observe, suggest, and prepare actions cannot execute")
        if self.minimum_autonomy == 4 and (
            not self.allowed_domains or not self.allowed_target_types
        ):
            raise ValueError(
                "reversible actions require authoritative domain and target constraints"
            )


# Exact normalized action identifiers are the authority for the actions Restia
# currently knows how to execute.  Word-based detection below remains a
# defence-in-depth escalation for aliases, while unknown executable actions
# fail closed at Level 6 until a reviewed registry entry defines their risk.
REGISTERED_ACTION_RISKS: Mapping[str, RegisteredActionRisk] = MappingProxyType(
    {
        # Read/suggest/prepare actions never enter execution.
        "observe_record": RegisteredActionRisk(1, executable=False),
        "suggest_plan": RegisteredActionRisk(2, executable=False),
        "prepare_summary": RegisteredActionRisk(3, executable=False),
        # Known reversible local mutations. Both fields must match; an action
        # name alone is never sufficient to claim the Level-4 path.
        "create_event": RegisteredActionRisk(
            4,
            allowed_domains=frozenset({"calendar"}),
            allowed_target_types=frozenset({"event"}),
        ),
        "reschedule_event": RegisteredActionRisk(
            4,
            allowed_domains=frozenset({"calendar"}),
            allowed_target_types=frozenset({"event"}),
        ),
        "update_event": RegisteredActionRisk(
            4,
            allowed_domains=frozenset({"calendar"}),
            allowed_target_types=frozenset({"event"}),
        ),
        # Outbound communications and bookings always need approval.
        "book": RegisteredActionRisk(5, external=True),
        "create_booking": RegisteredActionRisk(5, external=True),
        "deliver_email": RegisteredActionRisk(5, external=True),
        "dispatch_email": RegisteredActionRisk(5, external=True),
        "forward": RegisteredActionRisk(5, external=True),
        "forward_email": RegisteredActionRisk(5, external=True),
        "post": RegisteredActionRisk(5, external=True),
        "publish": RegisteredActionRisk(5, external=True),
        "reply": RegisteredActionRisk(5, external=True),
        "reply_email": RegisteredActionRisk(5, external=True),
        "reserve": RegisteredActionRisk(5, external=True),
        "send": RegisteredActionRisk(5, external=True),
        "send_email": RegisteredActionRisk(5, external=True),
        "send_message": RegisteredActionRisk(5, external=True),
        "sync_record": RegisteredActionRisk(5, external=True),
        "submit": RegisteredActionRisk(5, external=True),
        "transmit_email": RegisteredActionRisk(5, external=True),
        # High-risk actions retain Level 6 even if their domain is mislabeled.
        "delete": RegisteredActionRisk(6),
        "destroy": RegisteredActionRisk(6),
        "grant_access": RegisteredActionRisk(6),
        "revoke_access": RegisteredActionRisk(6),
        "transfer": RegisteredActionRisk(6, external=True),
        "transfer_money": RegisteredActionRisk(6, external=True),
    }
)


def _normalize_domain(value: object) -> str:
    domain = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not _NAME_RE.fullmatch(domain):
        raise ActionPolicyError("domain must be a lowercase identifier")
    return domain


def _normalize_action(value: object) -> str:
    action = str(value or "").strip().lower().replace(" ", "_")
    if not _ACTION_RE.fullmatch(action):
        raise ActionPolicyError("action must be a lowercase action identifier")
    return action


def _normalize_target_type(value: object) -> str:
    return _normalize_domain(value)


def _bounded_json(value: object, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ActionPolicyError(f"{field} must be an object")
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ActionPolicyError(f"{field} must be JSON-serializable") from exc
    if len(encoded) > _MAX_JSON_BYTES:
        raise ActionPolicyError(f"{field} must not exceed {_MAX_JSON_BYTES} bytes")
    return dict(value)


def _bounded_text(value: object, field: str, limit: int, *, required: bool = False) -> str:
    text = str(value or "").strip()
    if required and not text:
        raise ActionPolicyError(f"{field} is required")
    if len(text) > limit:
        raise ActionPolicyError(f"{field} is too long")
    return text


def _hash_idempotency_key(value: object) -> str:
    raw = _bounded_text(value, "idempotency_key", 1024, required=True)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _is_destructive_action(action: str) -> bool:
    words = set(re.findall(r"[a-z0-9]+", action.lower()))
    return bool(
        words & _DESTRUCTIVE_ACTION_WORDS
        or ({"permanent", "remove"} <= words)
        or ({"permanently", "remove"} <= words)
    )


def _action_words(action: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", action.lower()))


def _is_critical(domain: str, action: str) -> bool:
    words = _action_words(action)
    semantic_high_risk = bool(
        words & _FINANCIAL_ACTION_WORDS
        or words & _LEGAL_ACTION_WORDS
        or words & _MEDICAL_ACTION_WORDS
        or (
            words & _PERMISSION_ACTION_WORDS
            and words & _PERMISSION_MUTATION_WORDS
        )
    )
    return bool(
        domain in _CRITICAL_DOMAINS
        or _is_destructive_action(action)
        or semantic_high_risk
    )


def _minimum_autonomy(domain: str, action: str) -> int:
    if _is_critical(domain, action):
        return 6
    words = _action_words(action)
    # Side-effect verbs are escalated independent of the caller-provided
    # domain. Otherwise a model could relabel ``send_email`` as ``general``
    # and bypass the external-action approval boundary.
    if words & _OUTBOUND_ACTION_WORDS:
        return 5
    return 1


def _resolve_action_risk(
    *,
    domain: str,
    action: str,
    target_type: str,
    requested_autonomy: int,
    declared_external: bool,
) -> tuple[int, bool, bool, str]:
    """Return the server-authoritative risk floor for a proposal.

    The registry is authoritative for known executable action/domain/target
    combinations. Semantic word detection catches compound aliases such as
    ``send_notification``. Unknown executable actions, mismatched registered
    combinations, and attempts to upgrade a non-executable label are Level 6.
    """

    registered = REGISTERED_ACTION_RISKS.get(action)
    semantic_minimum = _minimum_autonomy(domain, action)
    minimum = max(
        semantic_minimum,
        registered.minimum_autonomy if registered is not None else 1,
    )
    inferred_external = bool(_action_words(action) & _OUTBOUND_ACTION_WORDS)
    effective_external = bool(
        declared_external
        or inferred_external
        or (registered.external if registered is not None else False)
    )
    context_matches = bool(
        registered is not None
        and (
            not registered.allowed_domains or domain in registered.allowed_domains
        )
        and (
            not registered.allowed_target_types
            or target_type in registered.allowed_target_types
        )
    )

    if registered is not None and registered.executable and not context_matches:
        minimum = max(minimum, 6)
        source = "registry_context_mismatch"
    elif registered is not None and not registered.executable and (
        declared_external or requested_autonomy >= 4
    ):
        minimum = max(minimum, 6)
        source = "non_executable_escalation"
    elif registered is None and semantic_minimum == 1 and (
        declared_external or requested_autonomy >= 4
    ):
        minimum = max(minimum, 6)
        source = "unknown_executable"
    elif registered is not None:
        source = "registry"
    elif semantic_minimum > 1:
        source = "semantic_escalation"
    else:
        source = "non_executable"

    if effective_external:
        minimum = max(minimum, 5)
    critical = bool(_is_critical(domain, action) or minimum >= 6)
    return minimum, effective_external, critical, source


def _default_cap(domain: str) -> int:
    return int(SAFE_DOMAIN_DEFAULTS.get(domain, 3))


def _is_human_approval_actor(db, owner_id: str) -> bool:
    context = db.info.get(SESSION_AUDIT_CONTEXT_KEY) or {}
    return bool(
        context.get("actor_type") == "account"
        and context.get("actor_id") == owner_id
        and context.get("credential_type") in {"session", "local"}
        and context.get("interface") == "web"
    )


def get_effective_action_policy(
    db,
    *,
    owner_id: str,
    domain: object,
) -> EffectiveActionPolicy:
    normalized = _normalize_domain(domain)
    row = (
        db.query(ActionPolicy)
        .filter(ActionPolicy.owner_id == owner_id, ActionPolicy.domain == normalized)
        .first()
    )
    if row is None:
        return EffectiveActionPolicy(
            id=None,
            owner_id=owner_id,
            domain=normalized,
            max_autonomy=_default_cap(normalized),
            external_requires_confirmation=True,
            enabled=True,
            rules={},
            version=0,
            persisted=False,
        )
    return EffectiveActionPolicy(
        id=row.id,
        owner_id=row.owner_id,
        domain=row.domain,
        max_autonomy=int(row.max_autonomy),
        external_requires_confirmation=bool(row.external_requires_confirmation),
        enabled=bool(row.enabled),
        rules=dict(row.rules or {}),
        version=int(row.version or 1),
        persisted=True,
    )


def list_action_policies(db, *, owner_id: str) -> list[EffectiveActionPolicy]:
    rows = db.query(ActionPolicy).filter(ActionPolicy.owner_id == owner_id).all()
    persisted = {row.domain: row for row in rows}
    domains = sorted(set(SAFE_DOMAIN_DEFAULTS) | set(persisted))
    return [get_effective_action_policy(db, owner_id=owner_id, domain=domain) for domain in domains]


def serialize_action_policy(policy: EffectiveActionPolicy | ActionPolicy) -> dict[str, Any]:
    if isinstance(policy, ActionPolicy):
        policy = EffectiveActionPolicy(
            id=policy.id,
            owner_id=policy.owner_id,
            domain=policy.domain,
            max_autonomy=int(policy.max_autonomy),
            external_requires_confirmation=bool(policy.external_requires_confirmation),
            enabled=bool(policy.enabled),
            rules=dict(policy.rules or {}),
            version=int(policy.version or 1),
            persisted=True,
        )
    return {
        "id": policy.id,
        "domain": policy.domain,
        "max_autonomy": policy.max_autonomy,
        "external_requires_confirmation": policy.external_requires_confirmation,
        "enabled": policy.enabled,
        "rules": dict(policy.rules),
        "version": policy.version,
        "persisted": policy.persisted,
    }


def _policy_state(policy: ActionPolicy) -> dict[str, Any]:
    return {
        "domain": policy.domain,
        "max_autonomy": int(policy.max_autonomy),
        "external_requires_confirmation": bool(policy.external_requires_confirmation),
        "enabled": bool(policy.enabled),
        "version": int(policy.version or 1),
    }


def set_action_policy(
    db,
    *,
    owner_id: str,
    domain: object,
    max_autonomy: int,
    external_requires_confirmation: bool,
    enabled: bool,
    rules: dict[str, Any] | None = None,
    expected_version: int | None = None,
) -> ActionPolicy:
    normalized = _normalize_domain(domain)
    if type(max_autonomy) is not int or max_autonomy not in AUTONOMY_LEVELS:
        raise ActionPolicyError("max_autonomy must be an integer from 1 to 6")
    if not isinstance(external_requires_confirmation, bool) or not isinstance(enabled, bool):
        raise ActionPolicyError("policy booleans must be true or false")
    clean_rules = _bounded_json(rules or {}, "rules")
    current = get_effective_action_policy(
        db, owner_id=owner_id, domain=normalized
    )
    # A model/API token may only tighten policy.  Raising a cap, turning off an
    # extra confirmation fence, re-enabling a disabled domain, or changing
    # opaque rules requires the owning human session; otherwise policy mutation
    # would be an indirect self-approval path.
    loosens_policy = bool(
        max_autonomy > current.max_autonomy
        or (current.external_requires_confirmation and not external_requires_confirmation)
        or (not current.enabled and enabled)
        or clean_rules != current.rules
    )
    if loosens_policy and not _is_human_approval_actor(db, owner_id):
        raise ActionPolicyDenied(
            "Only the owning human web session may loosen an autonomy policy"
        )
    row = (
        db.query(ActionPolicy)
        .filter(ActionPolicy.owner_id == owner_id, ActionPolicy.domain == normalized)
        .first()
    )
    if row is None:
        if expected_version not in (None, 0):
            raise ActionPolicyConflict("Policy does not exist at the expected version")
        row = ActionPolicy(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            domain=normalized,
            max_autonomy=max_autonomy,
            external_requires_confirmation=external_requires_confirmation,
            enabled=enabled,
            rules=clean_rules,
            version=1,
        )
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError as exc:
            raise ActionPolicyConflict("Policy changed in another client") from exc
        append_action_audit(
            db,
            owner_id=owner_id,
            action="action_policy.created",
            entity_type="action_policy",
            entity_id=row.id,
            reason="Domain autonomy policy created",
            after_state=_policy_state(row),
            details={
                "rules_configured": bool(clean_rules),
                "loosens_policy": loosens_policy,
            },
        )
        return row

    if expected_version is None:
        raise ActionPolicyConflict("Current policy version is required")
    if int(row.version or 1) != int(expected_version):
        raise ActionPolicyConflict(
            f"Policy changed in another client (current version {int(row.version or 1)})"
        )
    before = _policy_state(row)
    next_version = int(expected_version) + 1
    now = utcnow_naive()
    updated = (
        db.query(ActionPolicy)
        .filter(
            ActionPolicy.id == row.id,
            ActionPolicy.owner_id == owner_id,
            ActionPolicy.version == int(expected_version),
        )
        .update(
            {
                ActionPolicy.max_autonomy: max_autonomy,
                ActionPolicy.external_requires_confirmation: external_requires_confirmation,
                ActionPolicy.enabled: enabled,
                ActionPolicy.rules: clean_rules,
                ActionPolicy.version: next_version,
                ActionPolicy.updated_at: now,
            },
            synchronize_session=False,
        )
    )
    if updated != 1:
        raise ActionPolicyConflict("Policy changed in another client")
    db.expire(row)
    db.refresh(row)
    append_action_audit(
        db,
        owner_id=owner_id,
        action="action_policy.updated",
        entity_type="action_policy",
        entity_id=row.id,
        reason="Domain autonomy policy changed",
        before_state=before,
        after_state=_policy_state(row),
        details={
            "rules_configured": bool(clean_rules),
            "loosens_policy": loosens_policy,
        },
    )
    return row


def _confirmation_digest(
    *, owner_id: str, proposal_id: str, version: int, purpose: str, token: str
) -> str:
    bound = "\0".join((owner_id, proposal_id, str(version), purpose, token))
    return hashlib.sha256(bound.encode("utf-8")).hexdigest()


def _new_confirmation(
    *, owner_id: str, proposal_id: str, version: int, purpose: str
) -> tuple[str, str]:
    token = "rac_" + secrets.token_urlsafe(32)
    return token, _confirmation_digest(
        owner_id=owner_id,
        proposal_id=proposal_id,
        version=version,
        purpose=purpose,
        token=token,
    )


def _proposal_state(proposal: ActionProposal) -> dict[str, Any]:
    return {
        "domain": proposal.domain,
        "action": proposal.action,
        "autonomy_level": int(proposal.autonomy_level),
        "state": proposal.state,
        "external": bool(proposal.external),
        "requires_confirmation": bool(proposal.requires_confirmation),
        "version": int(proposal.version or 1),
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def serialize_action_proposal(proposal: ActionProposal) -> dict[str, Any]:
    return {
        "id": proposal.id,
        "domain": proposal.domain,
        "action": proposal.action,
        "autonomy_level": int(proposal.autonomy_level),
        "state": proposal.state,
        "target_type": proposal.target_type,
        "target_id": proposal.target_id,
        "payload": dict(proposal.payload or {}),
        "reason": proposal.reason or "",
        "sources": dict(proposal.sources or {}),
        "external": bool(proposal.external),
        "requires_confirmation": bool(proposal.requires_confirmation),
        "confirmation_pending": bool(proposal.confirmation_digest),
        "expires_at": _iso(proposal.expires_at),
        "approved_at": _iso(proposal.approved_at),
        "approved_by_account_id": proposal.approved_by_account_id,
        "executed_at": _iso(proposal.executed_at),
        "result": dict(proposal.result or {}),
        "undo_ref": proposal.undo_ref,
        "version": int(proposal.version or 1),
        "created_at": _iso(proposal.created_at),
        "updated_at": _iso(proposal.updated_at),
    }


def _proposal_matches(
    proposal: ActionProposal,
    *,
    domain: str,
    action: str,
    autonomy_level: int,
    target_type: str,
    target_id: str | None,
    payload: dict[str, Any],
    reason: str,
    sources: dict[str, Any],
    external: bool,
    requires_confirmation: bool,
) -> bool:
    return all((
        proposal.domain == domain,
        proposal.action == action,
        int(proposal.autonomy_level) == autonomy_level,
        proposal.target_type == target_type,
        proposal.target_id == target_id,
        dict(proposal.payload or {}) == payload,
        (proposal.reason or "") == reason,
        dict(proposal.sources or {}) == sources,
        bool(proposal.external) == external,
        bool(proposal.requires_confirmation) == requires_confirmation,
    ))


def create_action_proposal(
    db,
    *,
    owner_id: str,
    domain: object,
    action: object,
    autonomy_level: int,
    target_type: object,
    target_id: object | None = None,
    payload: dict[str, Any] | None = None,
    reason: object = "",
    sources: dict[str, Any] | None = None,
    external: bool = False,
    idempotency_key: object,
    now: datetime | None = None,
) -> ProposalCreation:
    normalized_domain = _normalize_domain(domain)
    normalized_action = _normalize_action(action)
    normalized_target_type = _normalize_target_type(target_type)
    if type(autonomy_level) is not int or autonomy_level not in AUTONOMY_LEVELS:
        raise ActionPolicyError("autonomy_level must be an integer from 1 to 6")
    if not isinstance(external, bool):
        raise ActionPolicyError("external must be true or false")
    clean_target_id = (
        _bounded_text(target_id, "target_id", 255) if target_id is not None else None
    )
    clean_payload = _bounded_json(payload or {}, "payload")
    clean_reason = _bounded_text(reason, "reason", 4000)
    clean_sources = _bounded_json(sources or {}, "sources")
    hashed_key = _hash_idempotency_key(idempotency_key)

    policy = get_effective_action_policy(
        db, owner_id=owner_id, domain=normalized_domain
    )
    if not policy.enabled:
        raise ActionPolicyDenied("Actions are disabled for this domain")
    (
        risk_minimum,
        effective_external,
        critical,
        risk_source,
    ) = _resolve_action_risk(
        domain=normalized_domain,
        action=normalized_action,
        target_type=normalized_target_type,
        requested_autonomy=autonomy_level,
        declared_external=external,
    )
    effective_level = max(
        autonomy_level,
        risk_minimum,
    )
    if effective_level > policy.max_autonomy:
        raise ActionPolicyDenied(
            f"Action autonomy level {effective_level} exceeds the {normalized_domain} domain cap {policy.max_autonomy}"
        )
    # external_requires_confirmation is an extra fence only.  It can never
    # weaken the unconditional level-5/6 or critical-action approval rules.
    requires_confirmation = bool(
        critical
        or effective_level >= 5
        or (effective_external and policy.external_requires_confirmation)
    )

    existing = (
        db.query(ActionProposal)
        .filter(
            ActionProposal.owner_id == owner_id,
            ActionProposal.idempotency_key == hashed_key,
        )
        .first()
    )
    expected = {
        "domain": normalized_domain,
        "action": normalized_action,
        "autonomy_level": effective_level,
        "target_type": normalized_target_type,
        "target_id": clean_target_id,
        "payload": clean_payload,
        "reason": clean_reason,
        "sources": clean_sources,
        "external": effective_external,
        "requires_confirmation": requires_confirmation,
    }
    if existing is not None:
        if not _proposal_matches(existing, **expected):
            raise ActionPolicyConflict(
                "Idempotency key was already used for a different action proposal"
            )
        return ProposalCreation(existing, False, None)

    clock = now or utcnow_naive()
    proposal_id = str(uuid.uuid4())
    token = None
    digest = None
    expires_at = None
    if requires_confirmation:
        token, digest = _new_confirmation(
            owner_id=owner_id,
            proposal_id=proposal_id,
            version=1,
            purpose="approve",
        )
        expires_at = clock + CONFIRMATION_TTL
    proposal = ActionProposal(
        id=proposal_id,
        owner_id=owner_id,
        domain=normalized_domain,
        action=normalized_action,
        autonomy_level=effective_level,
        state="prepared",
        target_type=normalized_target_type,
        target_id=clean_target_id,
        payload=clean_payload,
        reason=clean_reason,
        sources=clean_sources,
        external=effective_external,
        requires_confirmation=requires_confirmation,
        confirmation_digest=digest,
        expires_at=expires_at,
        result={},
        idempotency_key=hashed_key,
        version=1,
    )
    try:
        with db.begin_nested():
            db.add(proposal)
            db.flush()
    except IntegrityError:
        winner = (
            db.query(ActionProposal)
            .filter(
                ActionProposal.owner_id == owner_id,
                ActionProposal.idempotency_key == hashed_key,
            )
            .first()
        )
        if winner is None:
            raise
        if not _proposal_matches(winner, **expected):
            raise ActionPolicyConflict(
                "Idempotency key was already used for a different action proposal"
            )
        return ProposalCreation(winner, False, None)

    append_action_audit(
        db,
        owner_id=owner_id,
        action="action_proposal.created",
        entity_type="action_proposal",
        entity_id=proposal.id,
        reason="Action proposal prepared behind autonomy policy",
        after_state=_proposal_state(proposal),
        details={
            "external": effective_external,
            "critical": critical,
            "requested_autonomy_level": autonomy_level,
            "requested_external": external,
            "risk_source": risk_source,
            "policy_version": policy.version,
            "target_type": normalized_target_type,
            "target_id_present": bool(clean_target_id),
        },
        idempotency_ref=hashed_key,
    )
    return ProposalCreation(proposal, True, token)


def get_action_proposal(db, *, owner_id: str, proposal_id: object) -> ActionProposal:
    row = (
        db.query(ActionProposal)
        .filter(
            ActionProposal.id == str(proposal_id),
            ActionProposal.owner_id == owner_id,
        )
        .first()
    )
    if row is None:
        raise ActionPolicyNotFound("Action proposal not found")
    return row


def list_action_proposals(
    db,
    *,
    owner_id: str,
    state: str | None = None,
    domain: str | None = None,
    limit: int = 50,
) -> list[ActionProposal]:
    query = db.query(ActionProposal).filter(ActionProposal.owner_id == owner_id)
    if state:
        normalized_state = str(state).strip().lower()
        if normalized_state not in ACTION_STATES:
            raise ActionPolicyError("Unknown action proposal state")
        query = query.filter(ActionProposal.state == normalized_state)
    if domain:
        query = query.filter(ActionProposal.domain == _normalize_domain(domain))
    bounded = max(1, min(100, int(limit)))
    return query.order_by(ActionProposal.created_at.desc(), ActionProposal.id.desc()).limit(bounded).all()


def _require_version(proposal: ActionProposal, expected_version: int) -> None:
    if type(expected_version) is not int or expected_version < 1:
        raise ActionPolicyError("version must be a positive integer")
    if int(proposal.version or 1) != expected_version:
        raise ActionPolicyConflict(
            f"Action proposal changed in another client (current version {int(proposal.version or 1)})"
        )


def _require_state(proposal: ActionProposal, allowed: Iterable[str]) -> None:
    allowed_set = frozenset(allowed)
    if proposal.state not in allowed_set:
        choices = ", ".join(sorted(allowed_set))
        raise ActionPolicyConflict(
            f"Action proposal is {proposal.state}; expected one of: {choices}"
        )


def _transition(
    db,
    proposal: ActionProposal,
    *,
    expected_version: int,
    allowed_states: Iterable[str],
    new_state: str,
    action_name: str,
    reason: str,
    values: dict[Any, Any] | None = None,
    details: dict[str, Any] | None = None,
    extra_filters: Iterable[Any] = (),
) -> ActionProposal:
    _require_version(proposal, expected_version)
    _require_state(proposal, allowed_states)
    before = _proposal_state(proposal)
    next_version = expected_version + 1
    now = utcnow_naive()
    update_values: dict[Any, Any] = {
        ActionProposal.state: new_state,
        ActionProposal.version: next_version,
        ActionProposal.updated_at: now,
    }
    update_values.update(values or {})
    updated = (
        db.query(ActionProposal)
        .filter(
            ActionProposal.id == proposal.id,
            ActionProposal.owner_id == proposal.owner_id,
            ActionProposal.version == expected_version,
            ActionProposal.state == proposal.state,
            *tuple(extra_filters),
        )
        .update(update_values, synchronize_session=False)
    )
    if updated != 1:
        db.expire_all()
        raise ActionPolicyConflict("Action proposal changed in another client")
    db.expire(proposal)
    db.refresh(proposal)
    append_action_audit(
        db,
        owner_id=proposal.owner_id,
        action=action_name,
        entity_type="action_proposal",
        entity_id=proposal.id,
        reason=reason,
        before_state=before,
        after_state=_proposal_state(proposal),
        details=details,
        reversible=bool(proposal.undo_ref),
        undo_ref=proposal.undo_ref,
    )
    return proposal


def _assert_policy_still_allows(db, proposal: ActionProposal) -> None:
    policy = get_effective_action_policy(
        db, owner_id=proposal.owner_id, domain=proposal.domain
    )
    risk_minimum, effective_external, critical, _risk_source = _resolve_action_risk(
        domain=proposal.domain,
        action=proposal.action,
        target_type=proposal.target_type,
        requested_autonomy=int(proposal.autonomy_level),
        declared_external=bool(proposal.external),
    )
    effective_level = max(int(proposal.autonomy_level), risk_minimum)
    requires_confirmation = bool(
        critical
        or effective_level >= 5
        or (effective_external and policy.external_requires_confirmation)
    )
    if (
        int(proposal.autonomy_level) < risk_minimum
        or (effective_external and not proposal.external)
        or (requires_confirmation and not proposal.requires_confirmation)
    ):
        # Covers proposals persisted before a registry hardening as well as any
        # stale connector process attempting to execute weaker stored metadata.
        raise ActionPolicyDenied(
            "Action proposal risk classification is stale; create a new proposal"
        )
    if not policy.enabled or int(proposal.autonomy_level) > policy.max_autonomy:
        raise ActionPolicyDenied("Current domain policy no longer permits this action")


def _require_human_approval_actor(db, owner_id: str) -> None:
    if not _is_human_approval_actor(db, owner_id):
        raise ActionPolicyDenied(
            "Fresh approval must come from the owning human web session"
        )


def issue_confirmation(
    db,
    *,
    owner_id: str,
    proposal_id: object,
    expected_version: int,
    purpose: str = "approve",
    now: datetime | None = None,
) -> ConfirmationChallenge:
    normalized_purpose = str(purpose or "").strip().lower()
    if normalized_purpose not in {"approve", "reverse"}:
        raise ActionPolicyError("confirmation purpose must be approve or reverse")
    proposal = get_action_proposal(db, owner_id=owner_id, proposal_id=proposal_id)
    _require_version(proposal, expected_version)
    if normalized_purpose == "approve":
        _require_state(proposal, {"prepared", "approved", "expired"})
        if not proposal.requires_confirmation:
            raise ActionPolicyConflict("This action does not require confirmation")
        next_state = "prepared"
    else:
        _require_state(proposal, {"completed"})
        if not proposal.undo_ref:
            raise ActionPolicyConflict("Action has no reversal reference")
        if not proposal.requires_confirmation:
            raise ActionPolicyConflict("This reversal does not require confirmation")
        next_state = "completed"

    next_version = expected_version + 1
    token, digest = _new_confirmation(
        owner_id=owner_id,
        proposal_id=proposal.id,
        version=next_version,
        purpose=normalized_purpose,
    )
    clock = now or utcnow_naive()
    values = {
        ActionProposal.confirmation_digest: digest,
        ActionProposal.expires_at: clock + CONFIRMATION_TTL,
    }
    if normalized_purpose == "approve":
        values.update({
            ActionProposal.approved_at: None,
            ActionProposal.approved_by_account_id: None,
        })
    proposal = _transition(
        db,
        proposal,
        expected_version=expected_version,
        allowed_states={proposal.state},
        new_state=next_state,
        action_name="action_proposal.confirmation_issued",
        reason="Fresh single-use confirmation challenge issued",
        values=values,
        details={"purpose": normalized_purpose},
    )
    return ConfirmationChallenge(proposal, token, normalized_purpose)


def _verify_confirmation(
    proposal: ActionProposal,
    *,
    token: object,
    purpose: str,
    now: datetime,
) -> str:
    if not proposal.confirmation_digest:
        raise ActionApprovalRequired("A fresh confirmation challenge is required")
    clean_token = _bounded_text(token, "confirmation_token", 256, required=True)
    if proposal.expires_at is None or proposal.expires_at <= now:
        raise ActionConfirmationExpired("Confirmation token expired")
    expected = _confirmation_digest(
        owner_id=proposal.owner_id,
        proposal_id=proposal.id,
        version=int(proposal.version),
        purpose=purpose,
        token=clean_token,
    )
    if not hmac.compare_digest(expected, proposal.confirmation_digest):
        raise ActionPolicyDenied("Invalid confirmation token")
    return expected


def approve_action(
    db,
    *,
    owner_id: str,
    proposal_id: object,
    expected_version: int,
    confirmation_token: object,
    now: datetime | None = None,
) -> ActionProposal:
    proposal = get_action_proposal(db, owner_id=owner_id, proposal_id=proposal_id)
    _require_version(proposal, expected_version)
    _require_state(proposal, {"prepared"})
    if not proposal.requires_confirmation:
        raise ActionPolicyConflict("This action does not require approval")
    _assert_policy_still_allows(db, proposal)
    _require_human_approval_actor(db, owner_id)
    clock = now or utcnow_naive()
    digest = _verify_confirmation(
        proposal, token=confirmation_token, purpose="approve", now=clock
    )
    return _transition(
        db,
        proposal,
        expected_version=expected_version,
        allowed_states={"prepared"},
        new_state="approved",
        action_name="action_proposal.approved",
        reason="Owning human approved the prepared action",
        values={
            ActionProposal.confirmation_digest: None,
            ActionProposal.expires_at: clock + APPROVED_EXECUTION_TTL,
            ActionProposal.approved_at: clock,
            ActionProposal.approved_by_account_id: owner_id,
        },
        details={"approval": "fresh_human_session"},
        extra_filters={ActionProposal.confirmation_digest == digest},
    )


def reject_action(
    db,
    *,
    owner_id: str,
    proposal_id: object,
    expected_version: int,
    reason: object = "",
) -> ActionProposal:
    proposal = get_action_proposal(db, owner_id=owner_id, proposal_id=proposal_id)
    clean_reason = _bounded_text(reason, "reason", 1000)
    return _transition(
        db,
        proposal,
        expected_version=expected_version,
        allowed_states={"prepared", "approved", "expired"},
        new_state="rejected",
        action_name="action_proposal.rejected",
        reason="Action proposal rejected by its owner",
        values={
            ActionProposal.confirmation_digest: None,
            ActionProposal.expires_at: None,
            ActionProposal.result: (
                {"rejection_reason": clean_reason} if clean_reason else {}
            ),
        },
        details={"reason_provided": bool(clean_reason)},
    )


def start_action(
    db,
    *,
    owner_id: str,
    proposal_id: object,
    expected_version: int,
    undo_ref: object | None = None,
    now: datetime | None = None,
) -> ActionProposal:
    proposal = get_action_proposal(db, owner_id=owner_id, proposal_id=proposal_id)
    if int(proposal.autonomy_level) < 4:
        raise ActionPolicyDenied(
            "Observe, suggest, and prepare proposals cannot enter execution"
        )
    _assert_policy_still_allows(db, proposal)
    clock = now or utcnow_naive()
    if proposal.requires_confirmation:
        if proposal.expires_at is None or proposal.expires_at <= clock:
            raise ActionConfirmationExpired("Fresh action approval expired")
        allowed = {"approved"}
    else:
        allowed = {"prepared"}
    _require_version(proposal, expected_version)
    _require_state(proposal, allowed)
    clean_undo = (
        _bounded_text(undo_ref, "undo_ref", 255)
        if undo_ref is not None else None
    )
    if int(proposal.autonomy_level) == 4 and not (
        clean_undo or proposal.undo_ref
    ):
        # The reversal path must exist before the connector performs the side
        # effect. Requiring it only at completion is too late: an irreversible
        # action may already have happened while the proposal remains stuck in
        # ``executing``.
        raise ActionPolicyError(
            "Level 4 execution requires a reversal reference before execution"
        )
    values: dict[Any, Any] = {
        ActionProposal.confirmation_digest: None,
        ActionProposal.expires_at: None,
        ActionProposal.executed_at: clock,
    }
    if clean_undo:
        values[ActionProposal.undo_ref] = clean_undo
    return _transition(
        db,
        proposal,
        expected_version=expected_version,
        allowed_states=allowed,
        new_state="executing",
        action_name="action_proposal.executing",
        reason="Approved action execution started",
        values=values,
    )


def complete_action(
    db,
    *,
    owner_id: str,
    proposal_id: object,
    expected_version: int,
    result: dict[str, Any] | None = None,
    undo_ref: object | None = None,
) -> ActionProposal:
    proposal = get_action_proposal(db, owner_id=owner_id, proposal_id=proposal_id)
    clean_result = _bounded_json(result or {}, "result")
    clean_undo = (
        _bounded_text(undo_ref, "undo_ref", 255) if undo_ref is not None else None
    )
    if proposal.undo_ref and clean_undo and proposal.undo_ref != clean_undo:
        raise ActionPolicyConflict(
            "The action reversal reference cannot change after execution starts"
        )
    resolved_undo = clean_undo or proposal.undo_ref
    if int(proposal.autonomy_level) == 4 and not resolved_undo:
        raise ActionPolicyError("Level 4 completion requires a reversal reference")
    return _transition(
        db,
        proposal,
        expected_version=expected_version,
        allowed_states={"executing"},
        new_state="completed",
        action_name="action_proposal.completed",
        reason="Action execution completed",
        values={
            ActionProposal.result: clean_result,
            ActionProposal.undo_ref: resolved_undo,
        },
        details={"has_result": bool(clean_result)},
    )


def fail_action(
    db,
    *,
    owner_id: str,
    proposal_id: object,
    expected_version: int,
    result: dict[str, Any] | None = None,
) -> ActionProposal:
    proposal = get_action_proposal(db, owner_id=owner_id, proposal_id=proposal_id)
    clean_result = _bounded_json(result or {}, "result")
    return _transition(
        db,
        proposal,
        expected_version=expected_version,
        allowed_states={"executing"},
        new_state="failed",
        action_name="action_proposal.failed",
        reason="Action execution failed",
        values={ActionProposal.result: clean_result},
        details={"has_result": bool(clean_result)},
    )


def reverse_action(
    db,
    *,
    owner_id: str,
    proposal_id: object,
    expected_version: int,
    result: dict[str, Any] | None = None,
    confirmation_token: object | None = None,
    now: datetime | None = None,
) -> ActionProposal:
    proposal = get_action_proposal(db, owner_id=owner_id, proposal_id=proposal_id)
    _require_version(proposal, expected_version)
    _require_state(proposal, {"completed"})
    if not proposal.undo_ref:
        raise ActionPolicyConflict("Action has no reversal reference")
    clean_result = _bounded_json(result or {}, "result")
    extra_filters: set[Any] = set()
    if proposal.requires_confirmation:
        _require_human_approval_actor(db, owner_id)
        digest = _verify_confirmation(
            proposal,
            token=confirmation_token,
            purpose="reverse",
            now=now or utcnow_naive(),
        )
        extra_filters.add(ActionProposal.confirmation_digest == digest)
    return _transition(
        db,
        proposal,
        expected_version=expected_version,
        allowed_states={"completed"},
        new_state="reversed",
        action_name="action_proposal.reversed",
        reason="Completed action reversed through its recorded reversal path",
        values={
            ActionProposal.result: clean_result,
            ActionProposal.confirmation_digest: None,
            ActionProposal.expires_at: None,
        },
        details={"fresh_confirmation": bool(proposal.requires_confirmation)},
        extra_filters=extra_filters,
    )
