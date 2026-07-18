"""Durable delivery worker for freshly approved agent-authored email.

The request path only creates and approves immutable SQL snapshots.  This
module is the sole bridge from a claimed ``email_outbound_deliveries`` row to
SMTP/IMAP.  It commits the lease before opening any network connection, closes
all database sessions before transport, and records only stable error codes.

SMTP does not provide an idempotency key.  A process can therefore crash after
the server accepts a message but before Restia commits completion; an expired
lease will replay the same deterministic Message-ID.  Delivery is deliberately
at-least-once, not exactly-once, and providers are not assumed to deduplicate.
"""

from __future__ import annotations

import email.utils
import hashlib
import html
import json
import logging
import mimetypes
import os
import re
import smtplib
import socket
import stat
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from typing import Any, Callable

from core.database import (
    Account,
    ActionProposal,
    EmailAccount,
    EmailOutboundDelivery,
    EmailOutboundDraft,
    utcnow_naive,
)
from src.email_outbound import (
    EmailDeliveryClaim,
    EmailOutboundError,
    claim_email_delivery,
    complete_email_delivery,
    is_server_email_action,
    retry_email_delivery,
)
from src.upload_limits import EMAIL_COMPOSE_UPLOAD_MAX_BYTES


logger = logging.getLogger(__name__)

WORKER_LEASE = timedelta(minutes=10)
DEFAULT_BATCH_SIZE = 25
MAX_ATTACHMENTS = 20
MAX_TOTAL_ATTACHMENT_BYTES = 50 * 1024 * 1024
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CONTENT_TYPE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]*$"
)
_MESSAGE_ID_RE = re.compile(r"^<[^<>\s\r\n]+@[^<>\s\r\n]+>$")


class EmailDeliveryWorkerError(RuntimeError):
    """A sanitized worker failure safe to persist as a stable code."""

    def __init__(self, code: str, *, terminal: bool = True):
        normalized = str(code or "").strip().lower()
        if not _ERROR_CODE_RE.fullmatch(normalized):
            normalized = "email_worker_failed"
        super().__init__(normalized)
        self.code = normalized
        self.terminal = bool(terminal)


@dataclass(frozen=True, slots=True)
class EmailWorkerResult:
    delivery_id: str
    state: str
    attempt: int
    error_code: str | None = None
    network_performed: bool = False
    sent_appended: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _DeliverySnapshot:
    owner_id: str
    owner_username: str
    email_account_id: str
    content: dict[str, Any]
    smtp_config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _ResolvedAttachment:
    data: bytes
    name: str
    content_type: str


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _content_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _account_visible_to_owner(email_account: EmailAccount, account: Account) -> bool:
    row_owner = str(email_account.owner or "").strip()
    if row_owner:
        return row_owner == account.username
    owner = str(account.username or "").casefold()
    return owner in {
        str(email_account.imap_user or "").strip().casefold(),
        str(email_account.from_address or "").strip().casefold(),
    }


def _current_from_address(email_account: EmailAccount) -> str:
    return str(
        email_account.from_address
        or email_account.smtp_user
        or email_account.imap_user
        or ""
    ).strip()


def _expected_message_id(*, draft_id: str, from_address: str) -> str:
    _local, _separator, domain = str(from_address or "").rpartition("@")
    safe_domain = re.sub(r"[^A-Za-z0-9.-]", "", domain) or "restia.invalid"
    return f"<restia-{draft_id}@{safe_domain}>"


def _validate_text(
    value: object,
    *,
    code: str,
    required: bool = False,
    limit: int,
) -> str:
    if not isinstance(value, str):
        raise EmailDeliveryWorkerError(code)
    if required and not value:
        raise EmailDeliveryWorkerError(code)
    if len(value) > limit:
        raise EmailDeliveryWorkerError(code)
    return value


def _validate_addresses(
    value: object,
    *,
    required: bool,
) -> tuple[list[str], list[str]]:
    if not isinstance(value, list):
        raise EmailDeliveryWorkerError("recipient_snapshot_invalid")
    headers: list[str] = []
    envelope: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item or "\r" in item or "\n" in item:
            raise EmailDeliveryWorkerError("recipient_snapshot_invalid")
        parsed = email.utils.getaddresses([item])
        if len(parsed) != 1:
            raise EmailDeliveryWorkerError("recipient_snapshot_invalid")
        _name, address = parsed[0]
        address = str(address or "").strip()
        if (
            not address
            or "@" not in address
            or address.startswith("@")
            or address.endswith("@")
            or any(character.isspace() for character in address)
        ):
            raise EmailDeliveryWorkerError("recipient_snapshot_invalid")
        key = address.casefold()
        if key in seen:
            raise EmailDeliveryWorkerError("recipient_snapshot_invalid")
        seen.add(key)
        headers.append(item)
        envelope.append(address)
    if required and not headers:
        raise EmailDeliveryWorkerError("recipient_snapshot_invalid")
    return headers, envelope


def _validate_content(content: object) -> dict[str, Any]:
    if not isinstance(content, dict):
        raise EmailDeliveryWorkerError("approval_snapshot_invalid")
    draft_id = _validate_text(
        content.get("draft_id"),
        code="approval_snapshot_invalid",
        required=True,
        limit=64,
    )
    kind = str(content.get("kind") or "")
    if kind not in {"new", "reply"}:
        raise EmailDeliveryWorkerError("threading_snapshot_invalid")
    account = content.get("account")
    if not isinstance(account, dict):
        raise EmailDeliveryWorkerError("account_snapshot_invalid")
    account_id = _validate_text(
        account.get("id"),
        code="account_snapshot_invalid",
        required=True,
        limit=255,
    )
    from_address = _validate_text(
        account.get("from_address"),
        code="account_snapshot_invalid",
        required=True,
        limit=998,
    )
    _validate_addresses([from_address], required=True)
    if not isinstance(account.get("name"), str):
        raise EmailDeliveryWorkerError("account_snapshot_invalid")

    to_headers, to_envelope = _validate_addresses(
        content.get("to"), required=True
    )
    cc_headers, cc_envelope = _validate_addresses(
        content.get("cc"), required=False
    )
    bcc_headers, bcc_envelope = _validate_addresses(
        content.get("bcc"), required=False
    )
    subject = _validate_text(
        content.get("subject"),
        code="approval_snapshot_invalid",
        required=True,
        limit=998,
    )
    if "\r" in subject or "\n" in subject:
        raise EmailDeliveryWorkerError("approval_snapshot_invalid")
    body = _validate_text(
        content.get("body"),
        code="approval_snapshot_invalid",
        required=True,
        limit=80_000,
    )
    body_html_value = content.get("body_html")
    if body_html_value is not None and not isinstance(body_html_value, str):
        raise EmailDeliveryWorkerError("approval_snapshot_invalid")
    if isinstance(body_html_value, str) and len(body_html_value) > 160_000:
        raise EmailDeliveryWorkerError("approval_snapshot_invalid")

    message_id = _validate_text(
        content.get("message_id"),
        code="message_id_invalid",
        required=True,
        limit=998,
    )
    if (
        not _MESSAGE_ID_RE.fullmatch(message_id)
        or message_id != _expected_message_id(
            draft_id=draft_id, from_address=from_address
        )
    ):
        raise EmailDeliveryWorkerError("message_id_invalid")

    threading = content.get("threading")
    if not isinstance(threading, dict):
        raise EmailDeliveryWorkerError("threading_snapshot_invalid")
    in_reply_to = threading.get("in_reply_to")
    source_uid = threading.get("source_uid")
    source_folder = threading.get("source_folder")
    references = threading.get("references")
    if not isinstance(references, list) or any(
        not isinstance(value, str)
        or not value
        or "\r" in value
        or "\n" in value
        or len(value) > 998
        for value in references
    ):
        raise EmailDeliveryWorkerError("threading_snapshot_invalid")
    if kind == "reply":
        for value in (in_reply_to, source_uid, source_folder):
            if not isinstance(value, str) or not value:
                raise EmailDeliveryWorkerError("threading_snapshot_invalid")
        if in_reply_to not in references:
            raise EmailDeliveryWorkerError("threading_snapshot_invalid")
    elif any(value not in (None, "") for value in (
        in_reply_to, source_uid, source_folder,
    )):
        raise EmailDeliveryWorkerError("threading_snapshot_invalid")

    attachments = content.get("attachments")
    if not isinstance(attachments, list) or len(attachments) > MAX_ATTACHMENTS:
        raise EmailDeliveryWorkerError("attachment_snapshot_invalid")
    source = content.get("source")
    if not isinstance(source, dict) or source.get("interface") != "agent":
        raise EmailDeliveryWorkerError("approval_snapshot_invalid")
    if content.get("delivery") != {
        "mode": "durable_outbox",
        "network_on_request": False,
    }:
        raise EmailDeliveryWorkerError("approval_snapshot_invalid")

    return {
        "draft_id": draft_id,
        "kind": kind,
        "account_id": account_id,
        "from_address": from_address,
        "to_headers": to_headers,
        "cc_headers": cc_headers,
        "bcc_headers": bcc_headers,
        "recipients": to_envelope + cc_envelope + bcc_envelope,
        "subject": subject,
        "body": body,
        "body_html": body_html_value,
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "references": references,
        "attachments": attachments,
    }


def _load_delivery_snapshot(
    session_factory,
    claim: EmailDeliveryClaim,
    *,
    get_email_config: Callable[..., dict[str, Any]],
) -> _DeliverySnapshot:
    db = session_factory()
    try:
        delivery = db.query(EmailOutboundDelivery).filter(
            EmailOutboundDelivery.id == claim.delivery_id,
            EmailOutboundDelivery.owner_id == claim.owner_id,
        ).one_or_none()
        if delivery is None or delivery.state != "claimed":
            raise EmailDeliveryWorkerError("delivery_claim_invalid")
        draft = db.query(EmailOutboundDraft).filter(
            EmailOutboundDraft.id == delivery.draft_id,
            EmailOutboundDraft.owner_id == delivery.owner_id,
        ).one_or_none()
        proposal = db.query(ActionProposal).filter(
            ActionProposal.id == delivery.proposal_id,
            ActionProposal.owner_id == delivery.owner_id,
        ).one_or_none()
        account = db.query(Account).filter(
            Account.id == delivery.owner_id,
            Account.status == "active",
        ).one_or_none()
        email_account = db.query(EmailAccount).filter(
            EmailAccount.id == delivery.email_account_id,
            EmailAccount.enabled.is_(True),
        ).one_or_none()
        if account is None:
            raise EmailDeliveryWorkerError("owner_inactive")
        if email_account is None:
            raise EmailDeliveryWorkerError("email_account_missing")
        if not _account_visible_to_owner(email_account, account):
            raise EmailDeliveryWorkerError("email_account_owner_mismatch")
        if draft is None or proposal is None:
            raise EmailDeliveryWorkerError("approval_snapshot_invalid")
        if (
            proposal.state != "executing"
            or proposal.approved_at is None
            or proposal.approved_by_account_id != account.id
            or draft.state != "queued"
            or not is_server_email_action(db, proposal)
        ):
            raise EmailDeliveryWorkerError("approval_snapshot_invalid")
        content = delivery.payload
        if not isinstance(content, dict):
            raise EmailDeliveryWorkerError("content_digest_mismatch")
        if (
            _content_digest(content) != delivery.content_sha256
            or delivery.content_sha256 != draft.content_sha256
            or content != dict(draft.content or {})
            or content != dict(proposal.payload or {})
            or content != claim.content
            or claim.content_sha256 != delivery.content_sha256
        ):
            raise EmailDeliveryWorkerError("content_digest_mismatch")
        checked = _validate_content(content)
        expected_action = (
            "reply_email" if checked["kind"] == "reply" else "send_email"
        )
        if (
            proposal.action != expected_action
            or draft.kind != checked["kind"]
            or checked["draft_id"] != draft.id
            or checked["account_id"] != email_account.id
            or delivery.email_account_id != draft.email_account_id
            or _current_from_address(email_account) != checked["from_address"]
        ):
            raise EmailDeliveryWorkerError("account_snapshot_changed")
        owner_username = str(account.username)
        email_account_id = str(email_account.id)
        detached_content = dict(content)
    finally:
        db.close()

    try:
        smtp_config = dict(get_email_config(
            email_account_id, owner=owner_username
        ) or {})
    except Exception as exc:
        raise EmailDeliveryWorkerError("smtp_configuration_unavailable") from exc
    if str(smtp_config.get("account_id") or "") != email_account_id:
        raise EmailDeliveryWorkerError("smtp_configuration_unavailable")
    if str(smtp_config.get("from_address") or "").strip() != checked["from_address"]:
        raise EmailDeliveryWorkerError("account_snapshot_changed")
    if not smtp_config.get("smtp_host") or not smtp_config.get("smtp_user"):
        raise EmailDeliveryWorkerError("smtp_configuration_unavailable")
    if not (
        smtp_config.get("smtp_password") or smtp_config.get("oauth_provider")
    ):
        raise EmailDeliveryWorkerError("smtp_configuration_unavailable")
    return _DeliverySnapshot(
        owner_id=claim.owner_id,
        owner_username=owner_username,
        email_account_id=email_account_id,
        content=detached_content,
        smtp_config=smtp_config,
    )


def _strict_attachment_name(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise EmailDeliveryWorkerError("attachment_name_invalid")
    if (
        Path(value).name != value
        or value in {".", ".."}
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise EmailDeliveryWorkerError("attachment_name_invalid")
    return value


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise EmailDeliveryWorkerError("attachment_unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise EmailDeliveryWorkerError("attachment_reference_invalid")
        if info.st_size > EMAIL_COMPOSE_UPLOAD_MAX_BYTES:
            raise EmailDeliveryWorkerError("attachment_too_large")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(EMAIL_COMPOSE_UPLOAD_MAX_BYTES + 1)
        if len(data) > EMAIL_COMPOSE_UPLOAD_MAX_BYTES:
            raise EmailDeliveryWorkerError("attachment_too_large")
        return data
    finally:
        os.close(fd)


def _default_attachment_loader(
    item: dict[str, Any],
    owner_username: str,
) -> tuple[bytes, str | None]:
    attachment_id = str(item.get("id") or "").strip()
    attachment_ref = str(item.get("ref") or "").strip()
    metadata_type: str | None = None
    if attachment_id:
        from core.constants import BASE_DIR
        from src.upload_handler import create_production_upload_handler

        handler = create_production_upload_handler(BASE_DIR)
        resolved = handler.resolve_upload(
            attachment_id,
            owner=owner_username,
            auth_manager=None,
            allow_admin=False,
        )
        if not isinstance(resolved, dict) or not resolved.get("path"):
            raise EmailDeliveryWorkerError("attachment_owner_mismatch")
        path = Path(str(resolved["path"]))
        metadata_type = str(resolved.get("mime") or "") or None
    elif attachment_ref:
        from routes.email_helpers import COMPOSE_UPLOADS_DIR

        if (
            Path(attachment_ref).name != attachment_ref
            or attachment_ref in {".", ".."}
            or "/" in attachment_ref
            or "\\" in attachment_ref
        ):
            raise EmailDeliveryWorkerError("attachment_reference_invalid")
        root = Path(COMPOSE_UPLOADS_DIR).resolve()
        path = root / attachment_ref
        try:
            link_info = path.lstat()
            if stat.S_ISLNK(link_info.st_mode):
                raise EmailDeliveryWorkerError("attachment_reference_invalid")
            resolved_path = path.resolve(strict=True)
        except OSError as exc:
            raise EmailDeliveryWorkerError("attachment_unavailable") from exc
        if resolved_path.parent != root:
            raise EmailDeliveryWorkerError("attachment_reference_invalid")
        path = resolved_path
    else:
        raise EmailDeliveryWorkerError("attachment_reference_invalid")
    return _read_regular_file(path), metadata_type


def _resolve_attachments(
    values: list[dict[str, Any]],
    *,
    owner_username: str,
    attachment_loader: Callable[
        [dict[str, Any], str], tuple[bytes, str | None]
    ],
) -> list[_ResolvedAttachment]:
    resolved: list[_ResolvedAttachment] = []
    total_bytes = 0
    for item in values:
        if not isinstance(item, dict):
            raise EmailDeliveryWorkerError("attachment_snapshot_invalid")
        has_id = bool(str(item.get("id") or "").strip())
        has_ref = bool(str(item.get("ref") or "").strip())
        if has_id == has_ref:
            raise EmailDeliveryWorkerError("attachment_reference_invalid")
        name = _strict_attachment_name(item.get("name"))
        approved_digest = str(item.get("sha256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", approved_digest):
            raise EmailDeliveryWorkerError("attachment_digest_missing")
        try:
            data, metadata_type = attachment_loader(item, owner_username)
        except EmailDeliveryWorkerError:
            raise
        except Exception as exc:
            raise EmailDeliveryWorkerError("attachment_unavailable") from exc
        if not isinstance(data, bytes):
            raise EmailDeliveryWorkerError("attachment_unavailable")
        total_bytes += len(data)
        if total_bytes > MAX_TOTAL_ATTACHMENT_BYTES:
            raise EmailDeliveryWorkerError("attachment_too_large")
        if hashlib.sha256(data).hexdigest() != approved_digest:
            raise EmailDeliveryWorkerError("attachment_digest_mismatch")
        content_type = str(item.get("content_type") or metadata_type or "").strip()
        if not content_type:
            content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if not _CONTENT_TYPE_RE.fullmatch(content_type):
            raise EmailDeliveryWorkerError("attachment_content_type_invalid")
        resolved.append(_ResolvedAttachment(
            data=data,
            name=name,
            content_type=content_type.lower(),
        ))
    return resolved


def _safe_html(approved_html: str | None, plain_body: str) -> str:
    if approved_html:
        try:
            import nh3

            cleaned = nh3.clean(
                approved_html,
                tags={
                    "a", "b", "blockquote", "br", "code", "del", "div",
                    "em", "h1", "h2", "h3", "i", "li", "ol", "p", "pre",
                    "s", "span", "strike", "strong", "u", "ul",
                },
                attributes={"a": {"href"}},
                url_schemes={"http", "https", "mailto"},
                strip_comments=True,
            ).strip()
            if cleaned:
                return f"<html><body>{cleaned}</body></html>"
        except Exception:
            # Falling back to escaped exact plain text is safe and does not
            # introduce unreviewed content.
            pass
    escaped = html.escape(plain_body).replace("\n", "<br>\n")
    return f"<html><body>{escaped}</body></html>"


def _build_message(
    content: dict[str, Any],
    attachments: list[_ResolvedAttachment],
    *,
    now: datetime | None = None,
) -> tuple[EmailMessage, list[str]]:
    checked = _validate_content(content)
    message = EmailMessage(policy=SMTP)
    try:
        message["From"] = checked["from_address"]
        message["To"] = ", ".join(checked["to_headers"])
        if checked["cc_headers"]:
            message["Cc"] = ", ".join(checked["cc_headers"])
        message["Subject"] = checked["subject"]
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        message["Date"] = email.utils.format_datetime(clock)
        message["Message-ID"] = checked["message_id"]
        message["X-Restia-Origin"] = "agent-approved-action"
        message["X-Restia-Kind"] = checked["kind"]
        message["X-Restia-Ref"] = checked["draft_id"]
        if checked["in_reply_to"]:
            message["In-Reply-To"] = checked["in_reply_to"]
        if checked["references"]:
            message["References"] = " ".join(checked["references"])
        message.set_content(checked["body"])
        message.add_alternative(
            _safe_html(checked["body_html"], checked["body"]), subtype="html"
        )
        for attachment in attachments:
            maintype, subtype = attachment.content_type.split("/", 1)
            message.add_attachment(
                attachment.data,
                maintype=maintype,
                subtype=subtype,
                filename=attachment.name,
            )
    except EmailDeliveryWorkerError:
        raise
    except Exception as exc:
        raise EmailDeliveryWorkerError("mime_snapshot_invalid") from exc
    return message, list(checked["recipients"])


def _classify_transport_error(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "smtp_auth_failed", True
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "smtp_recipients_rejected", True
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "smtp_sender_rejected", True
    if isinstance(exc, smtplib.SMTPResponseException):
        code = int(getattr(exc, "smtp_code", 0) or 0)
        if 500 <= code <= 599:
            return "smtp_message_rejected", True
        return "smtp_temporary_failure", False
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, OSError)):
        return "smtp_unavailable", False
    if isinstance(exc, smtplib.SMTPException):
        return "smtp_delivery_failed", False
    return "smtp_delivery_failed", False


def _record_failure(
    session_factory,
    claim: EmailDeliveryClaim,
    *,
    code: str,
    terminal: bool,
    now: datetime | None,
) -> str:
    db = session_factory()
    try:
        row = retry_email_delivery(
            db,
            delivery_id=claim.delivery_id,
            claim_token=claim.claim_token,
            error_code=code,
            terminal=terminal,
            now=now,
        )
        db.commit()
        return str(row.state)
    except Exception:
        db.rollback()
        logger.error(
            "email_delivery_failure_record_failed delivery_id=%s code=%s",
            claim.delivery_id,
            code,
        )
        return "claimed"
    finally:
        db.close()


def deliver_one_email_outbound(
    *,
    session_factory=None,
    now: datetime | None = None,
    send_smtp: Callable[..., None] | None = None,
    get_email_config: Callable[..., dict[str, Any]] | None = None,
    imap_factory=None,
    detect_sent_folder: Callable[[Any], str] | None = None,
    quote_folder: Callable[[str], str] | None = None,
    attachment_loader: Callable[
        [dict[str, Any], str], tuple[bytes, str | None]
    ] | None = None,
) -> EmailWorkerResult | None:
    """Claim, commit, deliver, and finalize at most one approved email."""

    if session_factory is None:
        from core.database import SessionLocal

        session_factory = SessionLocal
    if any(value is None for value in (
        send_smtp, get_email_config, imap_factory,
        detect_sent_folder, quote_folder,
    )):
        from routes.email_helpers import (
            _detect_sent_folder,
            _get_email_config,
            _imap,
            _q,
            _send_smtp_message,
        )

        send_smtp = send_smtp or _send_smtp_message
        get_email_config = get_email_config or _get_email_config
        imap_factory = imap_factory or _imap
        detect_sent_folder = detect_sent_folder or _detect_sent_folder
        quote_folder = quote_folder or _q
    attachment_loader = attachment_loader or _default_attachment_loader
    clock = now or utcnow_naive()

    claim_db = session_factory()
    try:
        claim = claim_email_delivery(
            claim_db,
            now=clock,
            lease=WORKER_LEASE,
        )
        if claim is None:
            claim_db.rollback()
            return None
        # The durable lease is visible to every process before DNS/SMTP/IMAP.
        claim_db.commit()
    except Exception:
        claim_db.rollback()
        logger.error("email_delivery_claim_failed")
        return None
    finally:
        claim_db.close()

    try:
        snapshot = _load_delivery_snapshot(
            session_factory,
            claim,
            get_email_config=get_email_config,
        )
        checked = _validate_content(snapshot.content)
        attachments = _resolve_attachments(
            checked["attachments"],
            owner_username=snapshot.owner_username,
            attachment_loader=attachment_loader,
        )
        message, recipients = _build_message(
            snapshot.content, attachments, now=now
        )
    except EmailDeliveryWorkerError as exc:
        state = _record_failure(
            session_factory,
            claim,
            code=exc.code,
            terminal=exc.terminal,
            now=now,
        )
        return EmailWorkerResult(
            delivery_id=claim.delivery_id,
            state=state,
            attempt=claim.attempt,
            error_code=exc.code,
        )
    except Exception:
        code = "approval_snapshot_invalid"
        state = _record_failure(
            session_factory,
            claim,
            code=code,
            terminal=True,
            now=now,
        )
        return EmailWorkerResult(
            delivery_id=claim.delivery_id,
            state=state,
            attempt=claim.attempt,
            error_code=code,
        )

    message_bytes = message.as_bytes(policy=SMTP)
    try:
        send_smtp(
            snapshot.smtp_config,
            checked["from_address"],
            recipients,
            message_bytes,
        )
    except Exception as exc:
        code, terminal = _classify_transport_error(exc)
        state = _record_failure(
            session_factory,
            claim,
            code=code,
            terminal=terminal,
            now=now,
        )
        return EmailWorkerResult(
            delivery_id=claim.delivery_id,
            state=state,
            attempt=claim.attempt,
            error_code=code,
            network_performed=True,
        )

    sent_appended = False
    try:
        with imap_factory(
            snapshot.email_account_id,
            owner=snapshot.owner_username,
        ) as imap:
            sent_folder = detect_sent_folder(imap)
            status, _data = imap.append(
                quote_folder(sent_folder), "\\Seen", None, message_bytes
            )
            sent_appended = status == "OK"
    except Exception:
        # SMTP acceptance is authoritative for completion. Sent append is a
        # post-transport convenience and must never trigger a duplicate retry.
        logger.warning(
            "email_delivery_sent_append_failed delivery_id=%s",
            claim.delivery_id,
        )

    complete_db = session_factory()
    try:
        complete_email_delivery(
            complete_db,
            delivery_id=claim.delivery_id,
            claim_token=claim.claim_token,
            provider_message_id=checked["message_id"],
            now=now,
        )
        complete_db.commit()
    except Exception:
        complete_db.rollback()
        # SMTP may already have accepted the message. Leave the claim intact;
        # its lease expiry will replay the deterministic Message-ID. Reporting
        # this as delivered would be dishonest, while marking retry now could
        # race another process before the lease fence expires.
        logger.error(
            "email_delivery_completion_record_failed delivery_id=%s",
            claim.delivery_id,
        )
        return EmailWorkerResult(
            delivery_id=claim.delivery_id,
            state="claimed",
            attempt=claim.attempt,
            error_code="completion_record_failed",
            network_performed=True,
            sent_appended=sent_appended,
        )
    finally:
        complete_db.close()

    return EmailWorkerResult(
        delivery_id=claim.delivery_id,
        state="delivered",
        attempt=claim.attempt,
        network_performed=True,
        sent_appended=sent_appended,
    )


def drain_email_outbox_once(
    *,
    session_factory=None,
    max_deliveries: int = DEFAULT_BATCH_SIZE,
    **worker_dependencies,
) -> dict[str, Any]:
    """Drain a bounded batch for the in-process poller or external CLI."""

    try:
        limit = max(1, min(int(max_deliveries), 100))
    except (TypeError, ValueError):
        limit = DEFAULT_BATCH_SIZE
    results: list[EmailWorkerResult] = []
    for _ in range(limit):
        result = deliver_one_email_outbound(
            session_factory=session_factory,
            **worker_dependencies,
        )
        if result is None:
            break
        results.append(result)
    return {
        "attempted": len(results),
        "delivered": [
            result.delivery_id for result in results
            if result.state == "delivered"
        ],
        "retried": [
            {"id": result.delivery_id, "error": result.error_code}
            for result in results if result.state == "retry"
        ],
        "failed": [
            {"id": result.delivery_id, "error": result.error_code}
            for result in results if result.state == "failed"
        ],
        "incomplete": [
            {"id": result.delivery_id, "error": result.error_code}
            for result in results if result.state == "claimed"
        ],
        "at_least_once": True,
    }


__all__ = [
    "EmailDeliveryWorkerError",
    "EmailWorkerResult",
    "deliver_one_email_outbound",
    "drain_email_outbox_once",
]
