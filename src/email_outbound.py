"""Approval-backed agent email drafts and durable delivery queue authority.

Model-facing email tools may call :func:`prepare_agent_email_action` only.
That function writes one exact, encrypted draft and a Level-5 ActionProposal;
it never opens SMTP or IMAP sockets and never returns confirmation material.

The reviewed web executor calls :func:`queue_approved_email_action` after a
fresh human approval.  It only moves the immutable snapshot into the database
outbox.  Network delivery belongs to a separately fenced worker which must
claim a row with :func:`claim_email_delivery` and report a sanitized outcome.
No request path in this module performs network I/O.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from core.database import (
    Account,
    ActionProposal,
    EmailAccount,
    EmailOutboundDelivery,
    EmailOutboundDraft,
    utcnow_naive,
)
from src.action_policy import (
    ActionPolicyConflict,
    ActionPolicyError,
    complete_action,
    create_action_proposal,
    fail_action,
    get_action_proposal,
    start_action,
)
from src.identity import find_account
from src.life_core import append_action_audit


MAX_BODY_CHARS = 80_000
MAX_BODY_HTML_CHARS = 160_000
MAX_SUBJECT_CHARS = 998
MAX_ADDRESS_HEADER_CHARS = 4_000
MAX_ATTACHMENTS = 20
MAX_ATTACHMENT_FIELD_CHARS = 2_000
MAX_LEGACY_DATABASE_BYTES = 256 * 1024 * 1024
MAX_DELIVERY_ATTEMPTS = 8
DELIVERY_LEASE = timedelta(minutes=2)

_EMAIL_ACTIONS = frozenset({"send_email", "reply_email"})
_EMAIL_KINDS = frozenset({"new", "reply"})
_DELIVERY_ERROR_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class EmailOutboundError(ActionPolicyError):
    """A stable fail-closed error at the agent-email authority boundary."""


@dataclass(frozen=True, slots=True)
class PreparedEmailAction:
    proposal: ActionProposal
    draft: EmailOutboundDraft
    created: bool


@dataclass(frozen=True, slots=True)
class QueuedEmailAction:
    proposal: ActionProposal
    draft: EmailOutboundDraft
    delivery: EmailOutboundDelivery


@dataclass(frozen=True, slots=True)
class EmailDeliveryClaim:
    delivery_id: str
    owner_id: str
    email_account_id: str
    claim_token: str
    content: dict[str, Any]
    content_sha256: str
    attempt: int


@dataclass(frozen=True, slots=True)
class LegacyEmailImportResult:
    imported: int
    reused: int
    source_preserved: bool


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _content_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _bounded_text(
    value: object,
    *,
    field: str,
    limit: int,
    required: bool = False,
    strip: bool = False,
) -> str:
    text = str(value or "")
    if strip:
        text = text.strip()
    if required and not text:
        raise EmailOutboundError(f"{field} is required")
    if len(text) > limit:
        raise EmailOutboundError(f"{field} is too long")
    return text


def _normalize_address_header(value: object, *, field: str, required: bool) -> list[str]:
    if value is None:
        raw_values: list[str] = []
    elif isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, (list, tuple)):
        raw_values = [str(item or "") for item in value]
    else:
        raise EmailOutboundError(f"{field} must be an address string or list")
    combined = ", ".join(raw_values)
    if "\r" in combined or "\n" in combined:
        raise EmailOutboundError(f"{field} contains an invalid header newline")
    if len(combined) > MAX_ADDRESS_HEADER_CHARS:
        raise EmailOutboundError(f"{field} is too long")
    parsed = email.utils.getaddresses(raw_values)
    normalized: list[str] = []
    seen: set[str] = set()
    for display_name, address in parsed:
        clean_address = str(address or "").strip()
        if (
            not clean_address
            or "@" not in clean_address
            or clean_address.startswith("@")
            or clean_address.endswith("@")
            or any(character.isspace() for character in clean_address)
        ):
            raise EmailOutboundError(f"{field} contains an invalid email address")
        key = clean_address.casefold()
        if key in seen:
            continue
        seen.add(key)
        clean_name = " ".join(str(display_name or "").split())
        normalized.append(
            email.utils.formataddr((clean_name, clean_address))
            if clean_name else clean_address
        )
    if required and not normalized:
        raise EmailOutboundError(f"{field} requires at least one address")
    return normalized


def _normalize_references(value: object) -> list[str]:
    if value is None:
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        raise EmailOutboundError("references must be a string or list")
    result: list[str] = []
    for item in values:
        text = _bounded_text(
            item, field="references", limit=998, required=True, strip=True
        )
        if "\r" in text or "\n" in text:
            raise EmailOutboundError("references contains an invalid header newline")
        if text not in result:
            result.append(text)
    return result


def _normalize_attachments(value: object) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise EmailOutboundError("attachments must be a bounded list")
    result: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            row = {"ref": item, "name": Path(item).name}
        elif isinstance(item, dict):
            allowed = {"id", "ref", "name", "content_type", "sha256"}
            if set(item) - allowed:
                raise EmailOutboundError("attachment contains unsupported fields")
            row = {
                str(key): str(field_value or "")
                for key, field_value in item.items()
                if field_value not in (None, "")
            }
        else:
            raise EmailOutboundError("attachment must be a reference object")
        if not row.get("id") and not row.get("ref"):
            raise EmailOutboundError("attachment requires id or ref")
        for key, field_value in row.items():
            if len(field_value) > MAX_ATTACHMENT_FIELD_CHARS:
                raise EmailOutboundError(f"attachment {key} is too long")
        result.append(row)
    return result


def _account_visible_to_owner(email_account: EmailAccount, account: Account) -> bool:
    row_owner = str(email_account.owner or "").strip()
    if row_owner:
        return row_owner == account.username
    owner = account.username.casefold()
    return owner in {
        str(email_account.imap_user or "").strip().casefold(),
        str(email_account.from_address or "").strip().casefold(),
    }


def _owned_email_account(db, *, owner: Account, email_account_id: object) -> EmailAccount:
    account_id = _bounded_text(
        email_account_id,
        field="email_account_id",
        limit=255,
        strip=True,
    )
    unowned = or_(EmailAccount.owner.is_(None), EmailAccount.owner == "")
    legacy_mailbox_match = or_(
        EmailAccount.imap_user == owner.username,
        EmailAccount.from_address == owner.username,
    )
    query = db.query(EmailAccount).filter(
        EmailAccount.enabled.is_(True),
        or_(
            EmailAccount.owner == owner.username,
            and_(unowned, legacy_mailbox_match),
        ),
    )
    if account_id:
        candidates = query.filter(EmailAccount.id == account_id).all()
    else:
        # Resolve the exact account before creating the immutable review
        # snapshot.  This remains owner-scoped and database-only; credentials
        # are neither decrypted nor copied into the action payload.
        candidates = query.order_by(
            EmailAccount.is_default.desc(), EmailAccount.created_at.asc()
        ).all()
    row = candidates[0] if candidates else None
    if row is None:
        raise EmailOutboundError("Email account is not available to this owner")
    return row


def _message_id(*, draft_id: str, from_address: str) -> str:
    _local, _separator, domain = str(from_address or "").rpartition("@")
    safe_domain = re.sub(r"[^A-Za-z0-9.-]", "", domain) or "restia.invalid"
    return f"<restia-{draft_id}@{safe_domain}>"


def _normalize_source(value: object, *, tool: str) -> dict[str, Any]:
    if value is None:
        source: dict[str, Any] = {}
    elif isinstance(value, dict):
        source = dict(value)
    else:
        raise EmailOutboundError("source must be an object")
    # The server owns these fields. Callers may add non-secret provenance but
    # cannot relabel a model-originated outbound action as a human gesture.
    source["interface"] = "agent"
    source["tool"] = tool
    encoded = _canonical_json(source)
    if len(encoded) > 16 * 1024:
        raise EmailOutboundError("source is too large")
    return source


def prepare_agent_email_action(
    db,
    *,
    owner_username: object,
    email_account_id: object,
    to: object,
    subject: object,
    body: object,
    body_html: object = None,
    cc: object = None,
    bcc: object = None,
    attachments: object = None,
    kind: str = "new",
    in_reply_to: object = None,
    references: object = None,
    source_uid: object = None,
    source_folder: object = None,
    source: object = None,
    idempotency_key: object = None,
) -> PreparedEmailAction:
    """Persist one exact Level-5 proposal without returning its raw token."""

    owner_name = _bounded_text(
        owner_username,
        field="owner_username",
        limit=160,
        required=True,
        strip=True,
    )
    owner = find_account(db, owner_name)
    if owner is None:
        raise EmailOutboundError("Authenticated email owner does not exist")
    normalized_kind = str(kind or "new").strip().lower()
    if normalized_kind not in _EMAIL_KINDS:
        raise EmailOutboundError("Unknown outbound email kind")
    email_account = _owned_email_account(
        db, owner=owner, email_account_id=email_account_id
    )
    clean_subject = _bounded_text(
        subject,
        field="subject",
        limit=MAX_SUBJECT_CHARS,
        required=True,
        strip=True,
    )
    if "\r" in clean_subject or "\n" in clean_subject:
        raise EmailOutboundError("subject contains an invalid header newline")
    clean_body = _bounded_text(
        body, field="body", limit=MAX_BODY_CHARS, required=True
    )
    clean_body_html = _bounded_text(
        body_html, field="body_html", limit=MAX_BODY_HTML_CHARS
    )
    clean_to = _normalize_address_header(to, field="to", required=True)
    clean_cc = _normalize_address_header(cc, field="cc", required=False)
    clean_bcc = _normalize_address_header(bcc, field="bcc", required=False)
    clean_attachments = _normalize_attachments(attachments)
    clean_in_reply_to = _bounded_text(
        in_reply_to,
        field="in_reply_to",
        limit=998,
        required=normalized_kind == "reply",
        strip=True,
    )
    if "\r" in clean_in_reply_to or "\n" in clean_in_reply_to:
        raise EmailOutboundError("in_reply_to contains an invalid header newline")
    clean_references = _normalize_references(references)
    clean_source_uid = _bounded_text(
        source_uid,
        field="source_uid",
        limit=255,
        required=normalized_kind == "reply",
        strip=True,
    )
    clean_source_folder = _bounded_text(
        source_folder,
        field="source_folder",
        limit=255,
        required=normalized_kind == "reply",
        strip=True,
    )
    tool = "reply_to_email" if normalized_kind == "reply" else "send_email"
    clean_source = _normalize_source(source, tool=tool)

    account_snapshot = {
        "id": email_account.id,
        "name": str(email_account.name or ""),
        "from_address": str(
            email_account.from_address or email_account.smtp_user
            or email_account.imap_user or ""
        ).strip(),
    }
    key_material = {
        "owner_id": owner.id,
        "email_account_id": email_account.id,
        "kind": normalized_kind,
        "to": clean_to,
        "cc": clean_cc,
        "bcc": clean_bcc,
        "subject": clean_subject,
        "body": clean_body,
        "body_html": clean_body_html or None,
        "attachments": clean_attachments,
        "in_reply_to": clean_in_reply_to,
        "references": clean_references,
        "source_uid": clean_source_uid,
        "source_folder": clean_source_folder,
        "source": clean_source,
    }
    raw_key = str(idempotency_key or "").strip()
    if not raw_key:
        raw_key = "agent-email-v1:" + _content_digest(key_material)
    if len(raw_key) > 1024:
        raise EmailOutboundError("idempotency_key is too long")
    draft_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"restia:email-draft:{owner.id}:{raw_key}"
    ))
    content = {
        "draft_id": draft_id,
        "kind": normalized_kind,
        "account": account_snapshot,
        "to": clean_to,
        "cc": clean_cc,
        "bcc": clean_bcc,
        "subject": clean_subject,
        "body": clean_body,
        "body_html": clean_body_html or None,
        "attachments": clean_attachments,
        "message_id": _message_id(
            draft_id=draft_id, from_address=account_snapshot["from_address"]
        ),
        "threading": {
            "in_reply_to": clean_in_reply_to or None,
            "references": clean_references,
            "source_uid": clean_source_uid or None,
            "source_folder": clean_source_folder or None,
        },
        "source": clean_source,
        "delivery": {"mode": "durable_outbox", "network_on_request": False},
    }
    digest = _content_digest(content)
    created = create_action_proposal(
        db,
        owner_id=owner.id,
        domain="email",
        action="reply_email" if normalized_kind == "reply" else "send_email",
        autonomy_level=5,
        target_type="email_draft",
        target_id=draft_id,
        payload=content,
        reason="Agent prepared exact outbound email content for human review",
        sources=clean_source,
        external=True,
        idempotency_key=raw_key,
    )
    # Intentionally discard ``created.confirmation_token``. Only the web route
    # may issue a fresh challenge to an authenticated human session.
    draft = (
        db.query(EmailOutboundDraft)
        .filter(
            EmailOutboundDraft.id == draft_id,
            EmailOutboundDraft.owner_id == owner.id,
        )
        .first()
    )
    if draft is None:
        draft = EmailOutboundDraft(
            id=draft_id,
            owner_id=owner.id,
            proposal_id=created.proposal.id,
            email_account_id=email_account.id,
            kind=normalized_kind,
            content=content,
            content_sha256=digest,
            source=clean_source,
            state="pending_review",
            version=1,
        )
        try:
            with db.begin_nested():
                db.add(draft)
                db.flush()
        except IntegrityError:
            draft = (
                db.query(EmailOutboundDraft)
                .filter(
                    EmailOutboundDraft.id == draft_id,
                    EmailOutboundDraft.owner_id == owner.id,
                )
                .first()
            )
            if draft is None:
                raise
    if not all((
        draft.proposal_id == created.proposal.id,
        draft.email_account_id == email_account.id,
        draft.kind == normalized_kind,
        dict(draft.content or {}) == content,
        draft.content_sha256 == digest,
    )):
        raise ActionPolicyConflict(
            "Email draft idempotency key was reused for different content"
        )
    return PreparedEmailAction(
        proposal=created.proposal,
        draft=draft,
        created=bool(created.created),
    )


def _linked_email_draft(db, proposal: ActionProposal) -> EmailOutboundDraft | None:
    if not all((
        proposal.domain == "email",
        proposal.action in _EMAIL_ACTIONS,
        proposal.target_type == "email_draft",
        bool(proposal.target_id),
        int(proposal.autonomy_level) == 5,
        bool(proposal.external),
        bool(proposal.requires_confirmation),
    )):
        return None
    draft = (
        db.query(EmailOutboundDraft)
        .filter(
            EmailOutboundDraft.id == proposal.target_id,
            EmailOutboundDraft.owner_id == proposal.owner_id,
            EmailOutboundDraft.proposal_id == proposal.id,
        )
        .first()
    )
    if draft is None:
        return None
    content = dict(draft.content or {})
    if not all((
        content == dict(proposal.payload or {}),
        content.get("draft_id") == draft.id,
        content.get("kind") == draft.kind,
        draft.content_sha256 == _content_digest(content),
        draft.email_account_id == str((content.get("account") or {}).get("id") or ""),
    )):
        return None
    return draft


def is_server_email_action(db, proposal: ActionProposal) -> bool:
    """Return true only for an exact proposal linked to an immutable draft."""

    return _linked_email_draft(db, proposal) is not None


def queue_approved_email_action(
    db,
    *,
    account: Account,
    proposal_id: object,
    expected_version: int,
    now: datetime | None = None,
) -> QueuedEmailAction:
    """Queue exact approved content; never contact SMTP/IMAP on this path."""

    proposal = get_action_proposal(
        db, owner_id=account.id, proposal_id=proposal_id
    )
    if int(proposal.version or 1) != int(expected_version):
        raise ActionPolicyConflict(
            f"Action proposal changed in another client (current version {int(proposal.version or 1)})"
        )
    draft = _linked_email_draft(db, proposal)
    if draft is None:
        raise EmailOutboundError("Action has no reviewed email executor")
    existing = (
        db.query(EmailOutboundDelivery)
        .filter(
            EmailOutboundDelivery.owner_id == account.id,
            EmailOutboundDelivery.proposal_id == proposal.id,
        )
        .first()
    )
    if proposal.state == "executing" and existing is not None:
        return QueuedEmailAction(proposal=proposal, draft=draft, delivery=existing)
    if draft.state != "pending_review":
        raise ActionPolicyConflict(
            f"Email draft is {draft.state}; expected pending_review"
        )
    clock = now or utcnow_naive()
    executing = start_action(
        db,
        owner_id=account.id,
        proposal_id=proposal.id,
        expected_version=expected_version,
        now=clock,
    )
    delivery_id = str(uuid.uuid5(
        uuid.NAMESPACE_URL, f"restia:email-delivery:{account.id}:{proposal.id}"
    ))
    delivery_key = "sha256:" + hashlib.sha256(
        f"email-delivery:{account.id}:{proposal.id}".encode("utf-8")
    ).hexdigest()
    delivery = EmailOutboundDelivery(
        id=delivery_id,
        owner_id=account.id,
        draft_id=draft.id,
        proposal_id=proposal.id,
        email_account_id=draft.email_account_id,
        idempotency_key=delivery_key,
        payload=dict(draft.content or {}),
        content_sha256=draft.content_sha256,
        state="queued",
        attempts=0,
        next_attempt_at=clock,
        version=1,
    )
    try:
        with db.begin_nested():
            db.add(delivery)
            db.flush()
    except IntegrityError:
        delivery = (
            db.query(EmailOutboundDelivery)
            .filter(
                EmailOutboundDelivery.owner_id == account.id,
                EmailOutboundDelivery.proposal_id == proposal.id,
            )
            .first()
        )
        if delivery is None:
            raise
        if not all((
            delivery.draft_id == draft.id,
            delivery.email_account_id == draft.email_account_id,
            dict(delivery.payload or {}) == dict(draft.content or {}),
            delivery.content_sha256 == draft.content_sha256,
        )):
            raise ActionPolicyConflict("Email delivery idempotency conflict")
    draft.state = "queued"
    draft.version = int(draft.version or 1) + 1
    draft.updated_at = clock
    append_action_audit(
        db,
        owner_id=account.id,
        action="email_outbound.queued",
        entity_type="email_outbound_delivery",
        entity_id=delivery.id,
        reason="Freshly approved email was queued for worker delivery",
        after_state={
            "state": delivery.state,
            "attempts": int(delivery.attempts or 0),
            "proposal_state": executing.state,
        },
        details={
            "network_performed": False,
            "content_digest_present": True,
        },
        idempotency_ref=delivery.idempotency_key,
    )
    db.flush()
    return QueuedEmailAction(
        proposal=executing, draft=draft, delivery=delivery
    )


def mark_email_action_rejected(db, proposal: ActionProposal) -> None:
    draft = _linked_email_draft(db, proposal)
    if draft is None or draft.state != "pending_review":
        return
    draft.state = "rejected"
    draft.version = int(draft.version or 1) + 1
    draft.updated_at = utcnow_naive()
    db.flush()


def _claim_digest(*, delivery_id: str, token: str) -> str:
    return hashlib.sha256(f"{delivery_id}\0{token}".encode("utf-8")).hexdigest()


def claim_email_delivery(
    db,
    *,
    now: datetime | None = None,
    lease: timedelta = DELIVERY_LEASE,
) -> EmailDeliveryClaim | None:
    """Lease one ready outbox row using a version-fenced database claim."""

    clock = now or utcnow_naive()
    row = (
        db.query(EmailOutboundDelivery)
        .filter(
            or_(
                EmailOutboundDelivery.state.in_(("queued", "retry")),
                (
                    (EmailOutboundDelivery.state == "claimed")
                    & (EmailOutboundDelivery.lease_expires_at <= clock)
                ),
            ),
            or_(
                EmailOutboundDelivery.next_attempt_at.is_(None),
                EmailOutboundDelivery.next_attempt_at <= clock,
            ),
        )
        .order_by(
            EmailOutboundDelivery.next_attempt_at.asc(),
            EmailOutboundDelivery.created_at.asc(),
        )
        .first()
    )
    if row is None:
        return None
    content = dict(row.payload) if isinstance(row.payload, dict) else {}
    previous_state = row.state
    previous_version = int(row.version or 1)
    token = "edc_" + os.urandom(32).hex()
    attempts = int(row.attempts or 0) + 1
    updated = (
        db.query(EmailOutboundDelivery)
        .filter(
            EmailOutboundDelivery.id == row.id,
            EmailOutboundDelivery.owner_id == row.owner_id,
            EmailOutboundDelivery.state == previous_state,
            EmailOutboundDelivery.version == previous_version,
        )
        .update({
            EmailOutboundDelivery.state: "claimed",
            EmailOutboundDelivery.attempts: attempts,
            EmailOutboundDelivery.claim_token_digest: _claim_digest(
                delivery_id=row.id, token=token
            ),
            EmailOutboundDelivery.claimed_at: clock,
            EmailOutboundDelivery.lease_expires_at: clock + lease,
            EmailOutboundDelivery.version: previous_version + 1,
            EmailOutboundDelivery.updated_at: clock,
        }, synchronize_session="fetch")
    )
    if updated != 1:
        db.expire_all()
        return None
    db.flush()
    return EmailDeliveryClaim(
        delivery_id=row.id,
        owner_id=row.owner_id,
        email_account_id=row.email_account_id,
        claim_token=token,
        content=content,
        content_sha256=row.content_sha256,
        attempt=attempts,
    )


def _claimed_delivery(db, *, delivery_id: object, claim_token: object, now: datetime) -> EmailOutboundDelivery:
    row = (
        db.query(EmailOutboundDelivery)
        .filter(EmailOutboundDelivery.id == str(delivery_id or ""))
        .first()
    )
    token = str(claim_token or "")
    if (
        row is None
        or row.state != "claimed"
        or not token
        or row.claim_token_digest != _claim_digest(delivery_id=row.id, token=token)
        or row.lease_expires_at is None
        or row.lease_expires_at <= now
    ):
        raise EmailOutboundError("Email delivery claim is missing or expired")
    return row


def complete_email_delivery(
    db,
    *,
    delivery_id: object,
    claim_token: object,
    provider_message_id: object = None,
    now: datetime | None = None,
) -> EmailOutboundDelivery:
    """Record a worker-confirmed delivery without persisting credentials/errors."""

    clock = now or utcnow_naive()
    row = _claimed_delivery(
        db, delivery_id=delivery_id, claim_token=claim_token, now=clock
    )
    if (
        not isinstance(row.payload, dict)
        or _content_digest(row.payload) != row.content_sha256
    ):
        raise EmailOutboundError("Email delivery content digest mismatch")
    proposal = get_action_proposal(
        db, owner_id=row.owner_id, proposal_id=row.proposal_id
    )
    if proposal.state != "executing":
        raise ActionPolicyConflict("Email action is not executing")
    provider_ref = _bounded_text(
        provider_message_id,
        field="provider_message_id",
        limit=998,
        strip=True,
    )
    row.state = "delivered"
    row.completed_at = clock
    row.provider_message_id = provider_ref or None
    row.claim_token_digest = None
    row.claimed_at = None
    row.lease_expires_at = None
    row.last_error_code = None
    row.version = int(row.version or 1) + 1
    row.updated_at = clock
    draft = (
        db.query(EmailOutboundDraft)
        .filter(
            EmailOutboundDraft.id == row.draft_id,
            EmailOutboundDraft.owner_id == row.owner_id,
        )
        .one()
    )
    draft.state = "delivered"
    draft.version = int(draft.version or 1) + 1
    draft.updated_at = clock
    complete_action(
        db,
        owner_id=row.owner_id,
        proposal_id=proposal.id,
        expected_version=int(proposal.version or 1),
        result={
            "delivery_state": "delivered",
            "delivery_id": row.id,
            "attempts": int(row.attempts or 0),
            **({"provider_message_id": provider_ref} if provider_ref else {}),
        },
    )
    db.flush()
    return row


def retry_email_delivery(
    db,
    *,
    delivery_id: object,
    claim_token: object,
    error_code: object,
    terminal: bool = False,
    now: datetime | None = None,
) -> EmailOutboundDelivery:
    """Release a failed claim using a stable code, never a raw exception."""

    clock = now or utcnow_naive()
    row = _claimed_delivery(
        db, delivery_id=delivery_id, claim_token=claim_token, now=clock
    )
    code = str(error_code or "").strip().lower()
    if not _DELIVERY_ERROR_RE.fullmatch(code):
        raise EmailOutboundError("Email delivery error code is invalid")
    exhausted = int(row.attempts or 0) >= MAX_DELIVERY_ATTEMPTS
    terminal = bool(terminal or exhausted)
    row.state = "failed" if terminal else "retry"
    row.next_attempt_at = None if terminal else clock + timedelta(
        seconds=min(3600, 2 ** min(int(row.attempts or 1), 10))
    )
    row.claim_token_digest = None
    row.claimed_at = None
    row.lease_expires_at = None
    row.last_error_code = code
    row.version = int(row.version or 1) + 1
    row.updated_at = clock
    if terminal:
        draft = (
            db.query(EmailOutboundDraft)
            .filter(
                EmailOutboundDraft.id == row.draft_id,
                EmailOutboundDraft.owner_id == row.owner_id,
            )
            .one()
        )
        draft.state = "failed"
        draft.version = int(draft.version or 1) + 1
        draft.updated_at = clock
        proposal = get_action_proposal(
            db, owner_id=row.owner_id, proposal_id=row.proposal_id
        )
        fail_action(
            db,
            owner_id=row.owner_id,
            proposal_id=proposal.id,
            expected_version=int(proposal.version or 1),
            result={
                "delivery_state": "failed",
                "delivery_id": row.id,
                "attempts": int(row.attempts or 0),
                "error_code": code,
            },
        )
    db.flush()
    return row


def _legacy_rows(path: Path) -> list[dict[str, Any]]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return []
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_size > MAX_LEGACY_DATABASE_BYTES
    ):
        raise EmailOutboundError("Legacy scheduled-email source is unsafe")
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_emails'"
        ).fetchone()
        if table is None:
            return []
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(scheduled_emails)"
            ).fetchall()
        }
        required = {"id", "to_addr", "subject", "body", "status"}
        if not required.issubset(columns):
            raise EmailOutboundError("Legacy scheduled-email schema is invalid")
        optional = (
            "cc", "bcc", "in_reply_to", "references_hdr", "attachments",
            "owner", "account_id", "odysseus_kind", "created_at",
        )
        select = [*sorted(required), *[name for name in optional if name in columns]]
        order_by = "created_at, id" if "created_at" in columns else "id"
        rows = connection.execute(
            f"SELECT {', '.join(select)} FROM scheduled_emails "
            f"WHERE status = 'agent_draft' ORDER BY {order_by}"
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.DatabaseError as exc:
        raise EmailOutboundError("Legacy scheduled-email database is invalid") from exc
    finally:
        connection.close()


def import_legacy_agent_email_drafts(
    db,
    *,
    source_path: str | Path,
) -> LegacyEmailImportResult:
    """Adopt legacy ``agent_draft`` rows without modifying their source DB."""

    path = Path(source_path)
    rows = _legacy_rows(path)
    if not rows:
        return LegacyEmailImportResult(imported=0, reused=0, source_preserved=True)
    active_accounts = db.query(Account).filter(Account.status == "active").all()
    imported = 0
    reused = 0
    for row in rows:
        owner_name = str(row.get("owner") or "").strip()
        if not owner_name:
            if len(active_accounts) != 1:
                raise EmailOutboundError(
                    "Legacy email draft owner is missing or ambiguous"
                )
            owner_name = active_accounts[0].username
        owner = find_account(db, owner_name)
        if owner is None:
            raise EmailOutboundError("Legacy email draft owner does not exist")
        email_account_id = str(row.get("account_id") or "").strip()
        if not email_account_id:
            visible = [
                candidate
                for candidate in db.query(EmailAccount)
                .filter(EmailAccount.enabled.is_(True)).all()
                if _account_visible_to_owner(candidate, owner)
            ]
            if len(visible) != 1:
                raise EmailOutboundError(
                    "Legacy email draft account is missing or ambiguous"
                )
            email_account_id = visible[0].id
        raw_attachments = row.get("attachments") or "[]"
        try:
            attachments = json.loads(raw_attachments) if isinstance(raw_attachments, str) else raw_attachments
        except json.JSONDecodeError as exc:
            raise EmailOutboundError(
                "Legacy email draft attachments are invalid"
            ) from exc
        kind = "reply" if row.get("in_reply_to") else "new"
        source = {
            "legacy_kind": "scheduled_email_agent_draft",
            "legacy_id": str(row["id"]),
        }
        prepared = prepare_agent_email_action(
            db,
            owner_username=owner_name,
            email_account_id=email_account_id,
            to=row.get("to_addr"),
            cc=row.get("cc"),
            bcc=row.get("bcc"),
            subject=row.get("subject") or "(no subject)",
            body=row.get("body"),
            attachments=attachments,
            kind=kind,
            in_reply_to=row.get("in_reply_to"),
            references=row.get("references_hdr"),
            source_uid=(f"legacy:{row['id']}" if kind == "reply" else None),
            source_folder=("legacy" if kind == "reply" else None),
            source=source,
            idempotency_key=f"legacy-agent-email-v1:{owner.id}:{row['id']}",
        )
        if prepared.created:
            imported += 1
        else:
            reused += 1
        # ORM roundtrip is the cutover verification. The source remains
        # untouched regardless, so a failed transaction is always recoverable.
        if prepared.draft.content_sha256 != _content_digest(
            dict(prepared.draft.content or {})
        ):
            raise EmailOutboundError("Imported email draft verification failed")
    return LegacyEmailImportResult(
        imported=imported, reused=reused, source_preserved=True
    )


def adopt_legacy_agent_email_drafts(
    *,
    session_factory,
    source_path: str | Path,
) -> LegacyEmailImportResult:
    """Run the read-only legacy adoption as one fail-closed transaction."""

    db = session_factory()
    try:
        # pysqlite otherwise defers the outer BEGIN until the first write. The
        # proposal service uses SAVEPOINTs for race-safe idempotency; if that
        # savepoint becomes SQLite's first transaction boundary, releasing it
        # can make an earlier imported row survive a later rollback. Force one
        # real outer transaction so the entire source file remains atomic.
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        result = import_legacy_agent_email_drafts(db, source_path=source_path)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


__all__ = [
    "EmailDeliveryClaim",
    "EmailOutboundError",
    "LegacyEmailImportResult",
    "PreparedEmailAction",
    "QueuedEmailAction",
    "adopt_legacy_agent_email_drafts",
    "claim_email_delivery",
    "complete_email_delivery",
    "import_legacy_agent_email_drafts",
    "is_server_email_action",
    "mark_email_action_rejected",
    "prepare_agent_email_action",
    "queue_approved_email_action",
    "retry_email_delivery",
]
