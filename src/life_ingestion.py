"""Deterministic, owner-scoped projections for external communications.

Transport connectors remain authoritative.  This module records their
immutable evidence in the Life graph without sending messages, creating tasks,
or mutating calendars.  Every projection is committed in one main-database
transaction so a failed message or link cannot leave a partial graph behind.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from core.database import SessionLocal
from src.audit_context import bind_service_audit_context
from src.auth_helpers import resolved_runtime_owner
from src.identity import ensure_account
from src.life_core import create_inbox_item
from src.life_graph import (
    create_entity_link,
    create_life_entity,
    create_life_source,
)


SessionFactory = Callable[[], Any]
_MESSAGE_ID_RE = re.compile(r"<[^<>\s]+>")
CAPTURE_CONTRACT_VERSION = 1
GENERIC_READ_ONLY_COMMUNICATION_CHANNELS = frozenset({
    "slack",
    "sms",
    "call",
    "other",
})
READ_ONLY_LIFE_COMMUNICATION_CHANNELS = frozenset({
    "whatsapp",
    *GENERIC_READ_ONLY_COMMUNICATION_CHANNELS,
})

# These values describe where evidence entered Restia, not where it will be
# processed.  Classification remains destination-free and cannot execute an
# action.  Keeping the vocabulary here gives web, voice, upload, and connector
# adapters one contract without introducing another queue or record store.
CAPTURE_SOURCE_CATEGORIES = frozenset({
    "text",
    "voice",
    "image",
    "screenshot",
    "link",
    "email",
    "telegram",
    "whatsapp",
    "restia_message",
    "notification",
    *GENERIC_READ_ONLY_COMMUNICATION_CHANNELS,
    "file",
    "meeting_note",
    "task",
    "idea",
    "receipt",
    "reminder",
    "saved_post",
    "research_paper",
})


class LifeIngestionError(RuntimeError):
    """One external record could not be projected atomically."""


@dataclass(frozen=True)
class LifeIngestionResult:
    owner_id: str
    source_id: str
    thread_id: str
    message_id: str
    link_id: str
    source_created: bool
    thread_created: bool
    message_created: bool
    link_created: bool
    inbox_id: str
    inbox_created: bool


@dataclass(frozen=True)
class InboxCaptureResult:
    owner_id: str
    inbox_id: str
    source_type: str
    created: bool


def normalize_capture_source_category(value: object) -> str:
    """Normalize every supported public capture label to one storage token."""

    normalized = re.sub(
        r"[^a-z0-9]+", "_", str(value or "").strip().lower()
    ).strip("_")
    aliases = {
        "audio": "voice",
        "thought": "text",
        "thoughts": "text",
        "voice_note": "voice",
        "voice_notes": "voice",
        "document": "file",
        "documents": "file",
        "files": "file",
        "photo": "image",
        "upload": "file",
        "url": "link",
        "links": "link",
        "screenshots": "screenshot",
        "emails": "email",
        "forwarded_email": "email",
        "forwarded_emails": "email",
        "forwarded_message": "restia_message",
        "forwarded_messages": "restia_message",
        "quick_capture": "text",
        "whatsapp_message": "whatsapp",
        "whatsapp_messages": "whatsapp",
        "slack_message": "slack",
        "slack_messages": "slack",
        "text_message": "sms",
        "text_messages": "sms",
        "sms_message": "sms",
        "sms_messages": "sms",
        "calls": "call",
        "phone_call": "call",
        "phone_calls": "call",
        "call_transcript": "call",
        "call_transcripts": "call",
        "other_message": "other",
        "other_messages": "other",
        "other_messaging_system": "other",
        "other_messaging_systems": "other",
        "meeting_notes": "meeting_note",
        "tasks": "task",
        "ideas": "idea",
        "receipts": "receipt",
        "reminders": "reminder",
        "saved_posts": "saved_post",
        "research_papers": "research_paper",
        "user": "text",
    }
    category = aliases.get(normalized, normalized)
    if category not in CAPTURE_SOURCE_CATEGORIES:
        raise LifeIngestionError("Unsupported Universal Inbox capture source")
    return category


def capture_ingestion_metadata(
    *, source_type: str, metadata: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Apply the server-owned Universal Inbox safety contract."""

    category = normalize_capture_source_category(source_type)
    payload = dict(metadata or {})
    payload["ingestion_contract"] = {
        "version": CAPTURE_CONTRACT_VERSION,
        "source_type": category,
        "owner_scoped": True,
        "destination_required": False,
        "classification_can_execute_external_action": False,
        "model_output_has_write_authority": False,
    }
    return payload


def ingest_inbox_capture(
    *,
    owner: str,
    source_type: str,
    title: str = "",
    content: str = "",
    source_ref: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    idempotency_key: str | None = None,
    kind: str | None = None,
    expected_owner_id: str | None = None,
    session_factory: SessionFactory | None = None,
    audit_interface: str = "domain_service",
) -> InboxCaptureResult:
    """Enter one capture through the canonical, destination-free Inbox.

    The adapter owns only ingestion.  It does not process the Inbox item,
    create a calendar event/task, invoke a model, or perform an external
    action.  Retries converge through the existing owner/idempotency unique
    constraint.
    """

    concrete_owner = str(owner or "").strip()
    if not concrete_owner:
        raise LifeIngestionError("Universal Inbox ingestion requires an owner")
    concrete_owner = resolved_runtime_owner(concrete_owner)
    category = normalize_capture_source_category(source_type)
    factory = session_factory or SessionLocal
    db = factory()
    try:
        account = ensure_account(db, concrete_owner)
        if expected_owner_id is not None and account.id != str(expected_owner_id):
            raise LifeIngestionError(
                "Universal Inbox owner binding changed before ingestion"
            )
        bind_service_audit_context(
            db,
            account_id=account.id,
            interface=str(audit_interface or "capture")[:64],
            actor_type="connector",
            credential_type="connector",
        )
        item, created = create_inbox_item(
            db,
            account=account,
            title=title,
            content=content,
            kind=kind,
            source_type=category,
            source_ref=source_ref,
            metadata=capture_ingestion_metadata(
                source_type=category, metadata=metadata
            ),
            idempotency_key=idempotency_key,
        )
        db.commit()
        return InboxCaptureResult(
            owner_id=account.id,
            inbox_id=item.id,
            source_type=category,
            created=created,
        )
    except Exception as exc:
        db.rollback()
        if isinstance(exc, LifeIngestionError):
            raise
        raise LifeIngestionError(
            f"Universal Inbox ingestion failed ({exc.__class__.__name__})"
        ) from exc
    finally:
        db.close()


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _message_id_tokens(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        tokens: list[str] = []
        for item in value:
            for token in _message_id_tokens(item):
                if token not in tokens:
                    tokens.append(token)
        return tokens
    text = str(value or "").strip()
    if not text:
        return []
    matched = _MESSAGE_ID_RE.findall(text)
    if matched:
        return list(dict.fromkeys(matched))
    # Some providers emit a bare Message-ID.  Preserve it exactly instead of
    # using subject normalization, which can merge unrelated conversations.
    return [text]


def _observed_at(row: Mapping[str, Any]) -> datetime | None:
    raw = str(row.get("date") or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    try:
        epoch = float(row.get("date_epoch") or 0)
    except (TypeError, ValueError):
        epoch = 0
    return datetime.fromtimestamp(epoch, timezone.utc) if epoch > 0 else None


def _create_projection(
    *,
    owner: str,
    source_type: str,
    source_identity: str,
    source_title: str,
    source_excerpt: str,
    source_content_hash: str,
    source_metadata: dict[str, Any],
    observed_at: datetime | None,
    thread_identity: str,
    thread_title: str,
    thread_properties: dict[str, Any],
    message_title: str,
    message_summary: str,
    message_properties: dict[str, Any],
    session_factory: SessionFactory | None,
    expected_owner_id: str | None = None,
    audit_interface: str | None = None,
) -> LifeIngestionResult:
    factory = session_factory or SessionLocal
    db = factory()
    try:
        # SQLite defers BEGIN until a write.  The Life graph services use
        # nested savepoints for concurrent idempotency, and releasing the
        # first savepoint can otherwise become the database-level commit.
        # Start the outer write transaction explicitly so a later projection
        # failure rolls every graph row back as one unit.
        if db.get_bind().dialect.name == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        account = ensure_account(db, owner)
        if expected_owner_id is not None and account.id != str(expected_owner_id):
            raise LifeIngestionError(
                f"{source_type.title()} owner binding changed before projection"
            )
        bind_service_audit_context(
            db,
            account_id=account.id,
            interface=audit_interface or source_type,
            actor_type="connector",
            credential_type="connector",
        )
        source, source_created = create_life_source(
            db,
            account=account,
            source_type=source_type,
            title=source_title,
            source_ref=source_identity,
            safe_excerpt=source_excerpt,
            content_sha256=source_content_hash,
            observed_at=observed_at,
            metadata=source_metadata,
            idempotency_key=f"{source_type}-source:{source_identity}",
        )
        # Conversation threads are stable across their messages, so their
        # fields contain only deterministic thread evidence.  Message-specific
        # provenance belongs on the message and its edge, not on this node.
        thread, thread_created = create_life_entity(
            db,
            account=account,
            entity_type="communication_thread",
            title=thread_title,
            properties=thread_properties,
            idempotency_key=f"{source_type}-thread:{thread_identity}",
            reason=f"{source_type.title()} conversation projected",
        )
        message, message_created = create_life_entity(
            db,
            account=account,
            entity_type="message",
            title=message_title,
            summary=message_summary,
            properties=message_properties,
            provenance={"source_id": source.id},
            domain_ref_type="life_source",
            domain_ref_id=source.id,
            occurred_at=observed_at,
            idempotency_key=f"{source_type}-message:{source_identity}",
            reason=f"{source_type.title()} message projected",
        )
        link, link_created = create_entity_link(
            db,
            account=account,
            source_id=message.id,
            relation="part_of",
            target_id=thread.id,
            provenance={"source_id": source.id},
            reason=f"{source_type.title()} message linked to conversation",
        )
        inbox_item, inbox_created = create_inbox_item(
            db,
            account=account,
            title=source_title,
            content=source_excerpt or message_summary or source_title,
            source_type=normalize_capture_source_category(source_type),
            source_ref=f"life_source:{source.id}",
            metadata=capture_ingestion_metadata(
                source_type=normalize_capture_source_category(source_type),
                metadata={
                    "life_source_id": source.id,
                    "message_entity_id": message.id,
                    "thread_entity_id": thread.id,
                },
            ),
            idempotency_key=f"{source_type}-inbox:{source_identity}",
        )
        db.commit()
        return LifeIngestionResult(
            owner_id=account.id,
            source_id=source.id,
            thread_id=thread.id,
            message_id=message.id,
            link_id=link.id,
            source_created=source_created,
            thread_created=thread_created,
            message_created=message_created,
            link_created=link_created,
            inbox_id=inbox_item.id,
            inbox_created=inbox_created,
        )
    except Exception as exc:
        db.rollback()
        raise LifeIngestionError(
            f"{source_type.title()} Life ingestion failed "
            f"({exc.__class__.__name__})"
        ) from exc
    finally:
        db.close()


def ingest_email_headers(
    *,
    owner: str,
    account_id: str | None,
    folder: str,
    emails: Iterable[Mapping[str, Any]],
    session_factory: SessionFactory | None = None,
    expected_owner_id: str | None = None,
) -> list[LifeIngestionResult]:
    """Project committed email-index headers in their original order."""

    concrete_owner = resolved_runtime_owner(owner)
    account_value = account_id or "default"
    mailbox_account = str(account_value).strip() or f"default:{owner or ''}"
    mailbox_folder = str(folder or "INBOX").strip() or "INBOX"
    results: list[LifeIngestionResult] = []
    for row in emails:
        if not isinstance(row, Mapping):
            raise LifeIngestionError("Email Life ingestion requires header objects")
        uid = str(row.get("uid") or "").strip()
        if not uid:
            raise LifeIngestionError("Email Life ingestion requires an indexed UID")
        external_message_ids = _message_id_tokens(row.get("message_id"))
        external_message_id = external_message_ids[0] if external_message_ids else ""
        subject = str(row.get("subject") or "(no subject)").strip() or "(no subject)"
        sender_name = str(row.get("from_name") or "").strip()
        sender_address = str(row.get("from_address") or "").strip()
        date_iso = str(row.get("date") or "").strip()
        immutable_header = {
            "subject": subject,
            "from_name": sender_name,
            "from_address": sender_address,
            "date": date_iso,
        }
        header_hash = _stable_hash(immutable_header)
        if external_message_id:
            source_identity = (
                f"email:{mailbox_account}:message-id:{external_message_id}"
            )
            identity_kind = "message_id"
        else:
            # IMAP UIDVALIDITY is not available in the current index.  Binding
            # a missing-Message-ID fallback to immutable headers prevents a
            # recycled UID from silently aliasing different mail.
            source_identity = (
                f"email:{mailbox_account}:folder:{mailbox_folder}:uid:{uid}:"
                f"header:{header_hash}"
            )
            identity_kind = "mailbox_uid_header"

        references = _message_id_tokens(row.get("references"))
        in_reply_to = _message_id_tokens(row.get("in_reply_to"))
        thread_key = (
            (references[0] if references else "")
            or (in_reply_to[0] if in_reply_to else "")
            or external_message_id
            or source_identity
        )
        thread_identity = f"email:{mailbox_account}:thread:{thread_key}"
        sender = sender_name
        if sender_address and sender_address not in sender:
            sender = f"{sender} <{sender_address}>" if sender else sender_address
        excerpt_parts = [
            part
            for part in (
                f"From: {sender}" if sender else "",
                f"Date: {date_iso}" if date_iso else "",
            )
            if part
        ]
        excerpt = "\n".join(excerpt_parts)
        source_metadata: dict[str, Any] = {
            "channel": "email",
            "mailbox_account": mailbox_account,
            "identity_kind": identity_kind,
            "thread_key": thread_key,
        }
        if external_message_id:
            source_metadata["message_id"] = external_message_id
        else:
            source_metadata.update({"folder": mailbox_folder, "uid": uid})
        message_properties = {
            **source_metadata,
            "sender_name": sender_name,
            "sender_address": sender_address,
        }
        results.append(
            _create_projection(
                owner=concrete_owner,
                source_type="email",
                source_identity=source_identity,
                source_title=subject,
                source_excerpt=excerpt,
                source_content_hash=header_hash,
                source_metadata=source_metadata,
                observed_at=_observed_at(row),
                thread_identity=thread_identity,
                thread_title="Email conversation",
                thread_properties={
                    "channel": "email",
                    "mailbox_account": mailbox_account,
                    "thread_key": thread_key,
                },
                message_title=subject,
                message_summary=excerpt,
                message_properties=message_properties,
                session_factory=session_factory,
                expected_owner_id=expected_owner_id,
            )
        )
    return results


def ingest_telegram_message(
    *,
    owner: str,
    bot_fingerprint: str,
    chat_id: str,
    text: str,
    message_id: int | None,
    update_id: int | None,
    expected_owner_id: str | None = None,
    session_factory: SessionFactory | None = None,
) -> LifeIngestionResult:
    """Project one linked, non-command Telegram message."""

    concrete_owner = str(owner or "").strip()
    if not concrete_owner:
        raise LifeIngestionError("Telegram Life ingestion requires a linked owner")
    concrete_owner = resolved_runtime_owner(concrete_owner)
    fingerprint = str(bot_fingerprint or "").strip()
    chat = str(chat_id or "").strip()
    body = str(text or "").strip()
    if not fingerprint or not chat or not body:
        raise LifeIngestionError(
            "Telegram Life ingestion requires bot, chat, and message evidence"
        )
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if update_id is not None:
        source_identity = f"telegram:{fingerprint}:update:{int(update_id)}"
        identity_kind = "update_id"
    else:
        # The Bot API normally supplies update_id.  The compatibility path is
        # still deterministic and treats an edit as new evidence by including
        # the content hash alongside the chat/message pair.
        fallback_message = str(message_id) if message_id is not None else "unknown"
        source_identity = (
            f"telegram:{fingerprint}:chat:{chat}:message:{fallback_message}:"
            f"content:{body_hash}"
        )
        identity_kind = "chat_message_content"
    thread_identity = f"telegram:{fingerprint}:chat:{chat}"
    metadata = {
        "channel": "telegram",
        "bot_fingerprint": fingerprint,
        "chat_id": chat,
        "identity_kind": identity_kind,
        "update_id": update_id,
        "message_id": message_id,
    }
    first_line = next(
        (line.strip() for line in body.splitlines() if line.strip()),
        "Telegram message",
    )
    return _create_projection(
        owner=concrete_owner,
        source_type="telegram",
        source_identity=source_identity,
        source_title=first_line,
        source_excerpt=body,
        source_content_hash=body_hash,
        source_metadata=metadata,
        observed_at=None,
        thread_identity=thread_identity,
        thread_title="Telegram conversation",
        thread_properties={
            "channel": "telegram",
            "bot_fingerprint": fingerprint,
            "chat_id": chat,
        },
        message_title=first_line,
        message_summary=body,
        message_properties=metadata,
        session_factory=session_factory,
        expected_owner_id=expected_owner_id,
    )


def ingest_whatsapp_readonly_message(
    *,
    owner: str,
    connector_id: str,
    conversation_ref: str,
    text: str,
    message_id: str | None = None,
    sender_name: str = "",
    observed_at: datetime | None = None,
    unread: bool = True,
    important: bool = False,
    expected_owner_id: str | None = None,
    session_factory: SessionFactory | None = None,
) -> LifeIngestionResult:
    """Project one inbound WhatsApp record without adding a transport client.

    Restia's WhatsApp boundary is intentionally import/read-only.  This
    function accepts evidence that an approved read connector already
    retrieved and projects it through the same immutable Life graph contract
    as email and Telegram.  It cannot fetch, reply, send, or obtain provider
    credentials, and this module deliberately exposes no WhatsApp send
    callable.
    """

    concrete_owner = str(owner or "").strip()
    if not concrete_owner:
        raise LifeIngestionError("WhatsApp Life ingestion requires a linked owner")
    concrete_owner = resolved_runtime_owner(concrete_owner)
    connector = str(connector_id or "").strip()
    conversation = str(conversation_ref or "").strip()
    body = str(text or "").strip()
    external_message_id = str(message_id or "").strip()
    sender = str(sender_name or "").strip()[:240]
    if not connector or not conversation or not body:
        raise LifeIngestionError(
            "WhatsApp Life ingestion requires connector, conversation, and message evidence"
        )
    if not isinstance(unread, bool) or not isinstance(important, bool):
        raise LifeIngestionError("WhatsApp read state must be boolean evidence")

    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    # WhatsApp addresses and chat identifiers can be phone numbers.  Keep
    # them only inside encrypted metadata and use hashes for cleartext
    # idempotency/source references.
    connector_hash = _stable_hash({"connector": connector})
    conversation_hash = _stable_hash({
        "connector": connector,
        "conversation": conversation,
    })
    identity_material = {
        "connector": connector,
        "conversation": conversation,
        "message_id": external_message_id,
        "body_sha256": body_hash if not external_message_id else "",
    }
    source_identity = f"whatsapp:{_stable_hash(identity_material)}"
    identity_kind = "message_id" if external_message_id else "content_hash"
    thread_identity = f"whatsapp:{conversation_hash}"
    metadata = {
        "channel": "whatsapp",
        "connector_id": connector,
        "connector_hash": connector_hash,
        "conversation_ref": conversation,
        "conversation_hash": conversation_hash,
        "identity_kind": identity_kind,
        "message_id": external_message_id or None,
        "sender_name": sender,
        "direction": "inbound",
        "unread": unread,
        "important": important,
        "read_only": True,
    }
    first_line = next(
        (line.strip() for line in body.splitlines() if line.strip()),
        "WhatsApp message",
    )
    return _create_projection(
        owner=concrete_owner,
        source_type="whatsapp",
        source_identity=source_identity,
        source_title=first_line,
        source_excerpt=body,
        source_content_hash=body_hash,
        source_metadata=metadata,
        observed_at=observed_at,
        thread_identity=thread_identity,
        thread_title="WhatsApp conversation",
        thread_properties={
            "channel": "whatsapp",
            "connector_id": connector,
            "connector_hash": connector_hash,
            "conversation_ref": conversation,
            "conversation_hash": conversation_hash,
            "read_only": True,
        },
        message_title=first_line[:240],
        message_summary=body,
        message_properties=metadata,
        session_factory=session_factory,
        expected_owner_id=expected_owner_id,
        audit_interface="domain_service",
    )


def ingest_readonly_communication_message(
    *,
    owner: str,
    channel: str,
    connector_id: str,
    conversation_ref: str,
    text: str,
    message_id: str | None = None,
    sender_name: str = "",
    observed_at: datetime | None = None,
    unread: bool = True,
    important: bool = False,
    direction: str = "inbound",
    expected_owner_id: str | None = None,
    session_factory: SessionFactory | None = None,
) -> LifeIngestionResult:
    """Project imported Slack, SMS, call, or other messaging evidence.

    This is an ingestion boundary, not a transport client.  The caller must
    already have read access to the source record.  Restia stores a canonical
    owner-scoped projection and deliberately exposes no fetch, reply, or send
    operation for these channels.
    """

    concrete_owner = str(owner or "").strip()
    if not concrete_owner:
        raise LifeIngestionError(
            "Read-only communication ingestion requires a linked owner"
        )
    concrete_owner = resolved_runtime_owner(concrete_owner)
    normalized_channel = normalize_capture_source_category(channel)
    if normalized_channel not in GENERIC_READ_ONLY_COMMUNICATION_CHANNELS:
        raise LifeIngestionError(
            "Read-only communication channel must be slack, sms, call, or other"
        )
    connector = str(connector_id or "").strip()
    conversation = str(conversation_ref or "").strip()
    body = str(text or "").strip()
    external_message_id = str(message_id or "").strip()
    sender = str(sender_name or "").strip()[:240]
    normalized_direction = str(direction or "").strip().lower()
    if not connector or not conversation or not body:
        raise LifeIngestionError(
            "Read-only communication ingestion requires connector, "
            "conversation, and message evidence"
        )
    if normalized_direction not in {"inbound", "outbound"}:
        raise LifeIngestionError(
            "Read-only communication direction must be inbound or outbound"
        )
    if not isinstance(unread, bool) or not isinstance(important, bool):
        raise LifeIngestionError("Read-only communication state must be boolean evidence")

    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    connector_hash = _stable_hash({"connector": connector})
    conversation_hash = _stable_hash({
        "connector": connector,
        "conversation": conversation,
    })
    identity_material = {
        "connector": connector,
        "conversation": conversation,
        "message_id": external_message_id,
        "body_sha256": body_hash if not external_message_id else "",
    }
    source_identity = (
        f"{normalized_channel}:{_stable_hash(identity_material)}"
    )
    thread_identity = f"{normalized_channel}:{conversation_hash}"
    metadata = {
        "channel": normalized_channel,
        "connector_id": connector,
        "connector_hash": connector_hash,
        "conversation_ref": conversation,
        "conversation_hash": conversation_hash,
        "identity_kind": "message_id" if external_message_id else "content_hash",
        "message_id": external_message_id or None,
        "sender_name": sender,
        "direction": normalized_direction,
        "unread": unread if normalized_direction == "inbound" else False,
        "important": important,
        "read_only": True,
    }
    label = normalized_channel.upper() if normalized_channel == "sms" else (
        normalized_channel.title()
    )
    first_line = next(
        (line.strip() for line in body.splitlines() if line.strip()),
        f"{label} communication",
    )
    return _create_projection(
        owner=concrete_owner,
        source_type=normalized_channel,
        source_identity=source_identity,
        source_title=first_line[:240],
        source_excerpt=body,
        source_content_hash=body_hash,
        source_metadata=metadata,
        observed_at=observed_at,
        thread_identity=thread_identity,
        thread_title=f"{label} conversation",
        thread_properties={
            "channel": normalized_channel,
            "connector_id": connector,
            "connector_hash": connector_hash,
            "conversation_ref": conversation,
            "conversation_hash": conversation_hash,
            "read_only": True,
        },
        message_title=first_line[:240],
        message_summary=body,
        message_properties=metadata,
        session_factory=session_factory,
        expected_owner_id=expected_owner_id,
        audit_interface="domain_service",
    )
