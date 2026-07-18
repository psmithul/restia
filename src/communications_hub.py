"""Owner-scoped, read-only view over Restia communication authorities.

The hub does not become a transport or a second message store.  Email cache
rows, canonical connector Life projections, Restia direct messages, and
browser notifications are read in place and normalized only for display and
search.  An explicit conversion command enters the existing Universal Inbox
and uses its canonical processor; classification never sends anything.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from sqlalchemy import or_

from core.database import (
    Account,
    BrowserNotification,
    DirectMessage,
    EmailAccount,
    EntityLink,
    LifeEntity,
    TelegramPrincipal,
)
from src.life_core import (
    INBOX_KINDS,
    create_inbox_item,
    process_inbox_item,
    serialize_inbox_item,
)
from src.life_ingestion import READ_ONLY_LIFE_COMMUNICATION_CHANNELS


COMMUNICATION_CONNECTORS = (
    "email",
    "telegram",
    "restia_message",
    "whatsapp",
    "slack",
    "sms",
    "call",
    "notification",
    "other",
)
WHATSAPP_READ_ONLY_CAPABILITIES = frozenset({
    "read", "summarize", "draft", "extract", "convert_to_life",
})
LIFE_PROJECTION_CONNECTORS = frozenset({
    "email",
    "telegram",
    *READ_ONLY_LIFE_COMMUNICATION_CHANNELS,
})
MAX_SOURCE_SCAN = 500
MAX_THREAD_MESSAGES = 10

_EMAIL_RE = re.compile(
    r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])",
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"(?<![\w@])@[A-Za-z0-9_.-]{2,64}\b")
_PHONE_RE = re.compile(r"(?<!\w)(\+?\d[\d ()-]{7,}\d)(?!\w)")
_COMMITMENT_RE = re.compile(
    r"\b(?:i(?:'ll| will| can| need to| must)|we(?:'ll| will| can| need to| must)|"
    r"promise(?:d)?|commit(?:ted)? to|will (?:send|share|deliver|finish|confirm|reply))\b",
    re.IGNORECASE,
)
_RELATIVE_DEADLINE_RE = re.compile(
    r"\b(?:due|deadline|by|before|no later than)\s+"
    r"(?:today|tomorrow|tonight|end of day|eod|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
    r"(?:\s+(?:at|by)\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?\b",
    re.IGNORECASE,
)
_ISO_DEADLINE_RE = re.compile(
    r"\b(20\d{2}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(?::\d{2})?(Z|[+-]\d{2}:?\d{2})?)?\b"
)
_QUESTION_RE = re.compile(r"\?")


class CommunicationsHubError(ValueError):
    """The unified communications request is invalid."""


class CommunicationItemNotFound(CommunicationsHubError):
    """The requested communication is not visible to this owner."""


@dataclass(frozen=True)
class _CommunicationItem:
    raw_ref: str
    dedupe_key: str
    connector: str
    thread_ref: str
    title: str
    body: str
    sender: str
    direction: str
    occurred_at: datetime | None
    unread: bool | None
    explicit_importance: int = 0
    cached_summary: str = ""
    cached_draft: str = ""
    source_entity_id: str | None = None


def _bounded_text(value: object, *, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    else:
        current = current.astimezone(timezone.utc)
    return current.isoformat().replace("+00:00", "Z")


def _parse_datetime(value: object, *, epoch: object = None) -> datetime | None:
    raw = str(value or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        seconds = float(epoch or 0)
    except (TypeError, ValueError):
        seconds = 0
    return datetime.fromtimestamp(seconds, timezone.utc) if seconds > 0 else None


def _timestamp(value: datetime | None) -> float:
    if value is None:
        return 0.0
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.timestamp()


def _public_id(owner_id: str, prefix: str, raw_ref: str) -> str:
    digest = hashlib.sha256(
        f"communications-v1\0{owner_id}\0{prefix}\0{raw_ref}".encode("utf-8")
    ).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _importance_number(value: object) -> int:
    if isinstance(value, bool):
        return 3 if value else 0
    if isinstance(value, (int, float)):
        return max(0, min(3, int(value)))
    return {
        "critical": 3,
        "urgent": 3,
        "high": 3,
        "important": 3,
        "normal": 1,
        "medium": 1,
        "low": 0,
    }.get(str(value or "").strip().lower(), 0)


def _dedupe_dicts(values: Iterable[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for value in values:
        marker = tuple(str(value.get(key) or "").casefold() for key in keys)
        if marker in seen:
            continue
        seen.add(marker)
        output.append(value)
    return output


def extract_communication_signals(
    text: object,
    *,
    sender: object = "",
) -> dict[str, Any]:
    """Return bounded deterministic evidence; never infer beyond the text."""

    content = _bounded_text(text, limit=20_000)
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|[\r\n]+", content)
        if part.strip()
    ]
    commitments = [
        {
            "text": sentence[:500],
            "actor": _bounded_text(sender, limit=240) or "unspecified",
            "confidence": 90,
        }
        for sentence in sentences
        if _COMMITMENT_RE.search(sentence)
    ][:8]

    deadlines: list[dict[str, Any]] = []
    for match in _ISO_DEADLINE_RE.finditer(content):
        prefix = content[max(0, match.start() - 40):match.start()]
        if not re.search(
            r"(?:\bdue(?:\s+(?:on|by))?|\bdeadline(?:\s+(?:is|on))?|"
            r"\bby|\bbefore|\bno later than)\s*$",
            prefix,
            flags=re.IGNORECASE,
        ):
            continue
        normalized_at = None
        raw = match.group(0)
        try:
            if match.group(2):
                suffix = match.group(3) or "Z"
                offset = suffix
                if re.fullmatch(r"[+-]\d{4}", offset):
                    offset = offset[:3] + ":" + offset[3:]
                normalized_at = _iso(
                    datetime.fromisoformat(
                        f"{match.group(1)}T{match.group(2)}{offset}".replace("Z", "+00:00")
                    )
                )
            else:
                normalized_at = date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            continue
        deadlines.append({
            "text": raw[:120],
            "normalized_at": normalized_at,
            "confidence": 98,
        })
    for match in _RELATIVE_DEADLINE_RE.finditer(content):
        deadlines.append({
            "text": match.group(0)[:120],
            "normalized_at": None,
            "confidence": 82,
        })
    deadlines = _dedupe_dicts(deadlines, "text")[:8]

    contacts: list[dict[str, Any]] = []
    sender_value = _bounded_text(sender, limit=240)
    if sender_value and sender_value.casefold() not in {
        "restia", "telegram", "whatsapp", "slack", "sms", "call",
        "notification", "other", "unknown",
    }:
        contacts.append({"kind": "sender", "value": sender_value})
    contacts.extend(
        {"kind": "email", "value": match.group(1)}
        for match in _EMAIL_RE.finditer(content)
    )
    contacts.extend(
        {"kind": "handle", "value": match.group(0)}
        for match in _HANDLE_RE.finditer(content)
    )
    for match in _PHONE_RE.finditer(content):
        raw_phone = match.group(1).strip()
        # Calendar dates are not contacts even though their digit/hyphen
        # shape otherwise satisfies a permissive international-phone regex.
        if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", raw_phone):
            continue
        digits = re.sub(r"\D", "", raw_phone)
        if 8 <= len(digits) <= 15:
            contacts.append({"kind": "phone", "value": raw_phone})
    contacts = _dedupe_dicts(contacts, "kind", "value")[:12]

    questions = [sentence[:500] for sentence in sentences if "?" in sentence][:8]
    return {
        "commitments": commitments,
        "deadlines": deadlines,
        "contacts": contacts,
        "questions": questions,
    }


def _life_projection_items(
    db,
    *,
    account: Account,
) -> list[_CommunicationItem]:
    rows = db.query(LifeEntity).filter(
        LifeEntity.owner_id == account.id,
        LifeEntity.entity_type == "message",
        LifeEntity.deleted_at.is_(None),
    ).order_by(
        LifeEntity.occurred_at.desc(),
        LifeEntity.created_at.desc(),
        LifeEntity.id.desc(),
    ).limit(MAX_SOURCE_SCAN).all()
    if not rows:
        return []
    ids = [row.id for row in rows]
    links = db.query(EntityLink).filter(
        EntityLink.owner_id == account.id,
        EntityLink.source_type == "life_entity",
        EntityLink.source_id.in_(ids),
        EntityLink.relation == "part_of",
        EntityLink.target_type == "life_entity",
        EntityLink.deleted_at.is_(None),
    ).all()
    thread_by_message = {str(link.source_id): str(link.target_id) for link in links}

    result: list[_CommunicationItem] = []
    for row in rows:
        properties = dict(row.properties or {})
        connector = str(properties.get("channel") or "").strip().lower()
        if connector not in LIFE_PROJECTION_CONNECTORS:
            continue
        unread_value = properties.get("unread")
        unread = unread_value if isinstance(unread_value, bool) else None
        direction = str(properties.get("direction") or "inbound").strip().lower()
        if direction not in {"inbound", "outbound"}:
            direction = "inbound"
        sender = _bounded_text(
            properties.get("sender_name")
            or properties.get("sender_address")
            or properties.get("sender"),
            limit=240,
        )
        mailbox = _bounded_text(properties.get("mailbox_account"), limit=255)
        external_message = _bounded_text(properties.get("message_id"), limit=500)
        if connector == "email" and external_message:
            dedupe_key = f"email:{mailbox}:{external_message}"
        else:
            dedupe_key = f"life:{row.id}"
        fallback_thread = (
            properties.get("thread_key")
            or properties.get("conversation_hash")
            or properties.get("chat_id")
            or row.id
        )
        result.append(_CommunicationItem(
            raw_ref=f"life:{row.id}",
            dedupe_key=dedupe_key,
            connector=connector,
            thread_ref=thread_by_message.get(row.id) or f"{connector}:{fallback_thread}",
            title=_bounded_text(row.title, limit=240) or f"{connector.title()} message",
            body=_bounded_text(row.summary or row.title, limit=20_000),
            sender=sender or connector.title(),
            direction=direction,
            occurred_at=row.occurred_at or row.created_at,
            unread=unread,
            explicit_importance=max(
                _importance_number(properties.get("importance")),
                _importance_number(properties.get("important")),
            ),
            cached_summary=_bounded_text(properties.get("thread_summary"), limit=4_000),
            cached_draft=_bounded_text(properties.get("response_suggestion"), limit=4_000),
            source_entity_id=row.id,
        ))
    return result


def _default_email_cache_paths() -> tuple[Path, ...]:
    from src.constants import EMAIL_CACHE_DB, SCHEDULED_EMAILS_DB

    paths: list[Path] = []
    for value in (EMAIL_CACHE_DB, SCHEDULED_EMAILS_DB):
        path = Path(value)
        if path not in paths:
            paths.append(path)
    return tuple(paths)


def _open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    return connection


def _cache_text_map(
    connection: sqlite3.Connection,
    *,
    table: str,
    column: str,
    owners: Sequence[str],
    message_ids: Sequence[str],
) -> dict[tuple[str, str], str]:
    if not owners or not message_ids:
        return {}
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if table not in tables:
        return {}
    columns = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if not {"message_id", "owner", column}.issubset(columns):
        return {}
    owner_placeholders = ",".join("?" for _ in owners)
    message_placeholders = ",".join("?" for _ in message_ids)
    query = (
        f"SELECT message_id, owner, {column} FROM {table} "
        f"WHERE owner IN ({owner_placeholders}) "
        f"AND message_id IN ({message_placeholders})"
    )
    rows = connection.execute(query, [*owners, *message_ids]).fetchall()
    return {
        (str(row["message_id"] or ""), str(row["owner"] or "")): str(row[column] or "")
        for row in rows
    }


def _email_cache_items(
    db,
    *,
    account: Account,
    life_thread_refs: Mapping[str, str],
    email_cache_paths: Sequence[Path] | None,
) -> tuple[list[_CommunicationItem], bool, list[str]]:
    aliases = tuple(dict.fromkeys((account.username, account.id)))
    configured = db.query(EmailAccount).filter(
        EmailAccount.enabled.is_(True),
        EmailAccount.owner.in_(aliases),
    ).order_by(EmailAccount.created_at.asc()).limit(100).all()
    account_keys = tuple(str(row.id) for row in configured)
    if not account_keys:
        return [], False, []
    paths = tuple(email_cache_paths) if email_cache_paths is not None else _default_email_cache_paths()
    result: list[_CommunicationItem] = []
    errors: list[str] = []
    seen: set[str] = set()
    for path_value in paths:
        path = Path(path_value)
        if not path.is_file():
            continue
        try:
            connection = _open_sqlite_readonly(path)
        except sqlite3.Error:
            errors.append("email_cache_unavailable")
            continue
        try:
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(email_message_index)"
                ).fetchall()
            }
            required = {
                "owner", "account_key", "folder", "uid", "message_id",
                "subject", "from_name", "from_address", "to_text",
                "date_iso", "date_epoch", "flags",
            }
            if not required.issubset(columns):
                errors.append("email_cache_schema_unavailable")
                continue
            owner_placeholders = ",".join("?" for _ in aliases)
            account_placeholders = ",".join("?" for _ in account_keys)
            rows = connection.execute(
                "SELECT owner, account_key, folder, uid, message_id, subject, "
                "from_name, from_address, to_text, date_iso, date_epoch, flags "
                "FROM email_message_index "
                f"WHERE owner IN ({owner_placeholders}) "
                f"AND account_key IN ({account_placeholders}) "
                "ORDER BY date_epoch DESC, updated_at DESC LIMIT ?",
                [*aliases, *account_keys, MAX_SOURCE_SCAN],
            ).fetchall()
            message_ids = tuple(dict.fromkeys(
                str(row["message_id"] or "").strip()
                for row in rows if str(row["message_id"] or "").strip()
            ))
            summaries = _cache_text_map(
                connection,
                table="email_summaries",
                column="summary",
                owners=aliases,
                message_ids=message_ids,
            )
            drafts = _cache_text_map(
                connection,
                table="email_ai_replies",
                column="reply",
                owners=aliases,
                message_ids=message_ids,
            )
            for row in rows:
                owner_alias = str(row["owner"] or "")
                account_key = str(row["account_key"] or "")
                folder = str(row["folder"] or "INBOX")
                uid = str(row["uid"] or "")
                message_id = str(row["message_id"] or "").strip()
                dedupe_key = (
                    f"email:{account_key}:{message_id}"
                    if message_id
                    else f"email-location:{account_key}:{folder}:{uid}"
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                flags = str(row["flags"] or "")
                folder_lower = folder.casefold()
                outbound = any(
                    token in folder_lower for token in ("sent", "draft", "outbox")
                )
                subject = _bounded_text(row["subject"], limit=240) or "(no subject)"
                sender_name = _bounded_text(row["from_name"], limit=240)
                sender_address = _bounded_text(row["from_address"], limit=320)
                sender = sender_name
                if sender_address and sender_address not in sender:
                    sender = f"{sender} <{sender_address}>" if sender else sender_address
                if outbound:
                    sender = _bounded_text(row["to_text"], limit=320) or "Recipient"
                fallback_thread = message_id or f"{folder}:{uid}"
                thread_ref = life_thread_refs.get(dedupe_key) or (
                    f"email:{account_key}:message:{fallback_thread}"
                )
                cached_summary = _bounded_text(
                    summaries.get((message_id, owner_alias)), limit=4_000
                )
                result.append(_CommunicationItem(
                    raw_ref=f"email-cache:{account_key}:{folder}:{uid}",
                    dedupe_key=dedupe_key,
                    connector="email",
                    thread_ref=thread_ref,
                    title=subject,
                    body=cached_summary or subject,
                    sender=sender or "Email",
                    direction="outbound" if outbound else "inbound",
                    occurred_at=_parse_datetime(
                        row["date_iso"], epoch=row["date_epoch"]
                    ),
                    unread=(None if outbound else "\\Seen" not in flags),
                    explicit_importance=(3 if "\\Flagged" in flags else 0),
                    cached_summary=cached_summary,
                    cached_draft=_bounded_text(
                        drafts.get((message_id, owner_alias)), limit=4_000
                    ),
                ))
        except sqlite3.Error:
            errors.append("email_cache_read_failed")
        finally:
            connection.close()
    return result, True, list(dict.fromkeys(errors))


def _direct_message_items(db, *, account: Account) -> list[_CommunicationItem]:
    username = account.username
    rows = db.query(DirectMessage).filter(
        or_(DirectMessage.sender == username, DirectMessage.recipient == username)
    ).order_by(
        DirectMessage.created_at.desc(), DirectMessage.id.desc()
    ).limit(MAX_SOURCE_SCAN).all()
    result: list[_CommunicationItem] = []
    for row in rows:
        inbound = row.recipient == username
        other = row.sender if inbound else row.recipient
        body = "Message deleted" if row.deleted_at is not None else str(row.body or "")
        result.append(_CommunicationItem(
            raw_ref=f"restia-message:{row.id}",
            dedupe_key=f"restia-message:{row.id}",
            connector="restia_message",
            thread_ref=f"restia-message:{other}",
            title=f"Conversation with {other}",
            body=_bounded_text(body, limit=20_000),
            sender=other if inbound else account.username,
            direction="inbound" if inbound else "outbound",
            occurred_at=row.created_at,
            unread=(bool(row.read_at is None) if inbound else False),
        ))
    return result


def _notification_items(db, *, account: Account) -> list[_CommunicationItem]:
    rows = db.query(BrowserNotification).filter(
        BrowserNotification.owner_id == account.id
    ).order_by(
        BrowserNotification.created_at.desc(), BrowserNotification.id.desc()
    ).limit(MAX_SOURCE_SCAN).all()
    result: list[_CommunicationItem] = []
    for row in rows:
        payload = dict(row.payload or {})
        title = _bounded_text(
            payload.get("title") or payload.get("subject") or "Restia notification",
            limit=240,
        )
        body = _bounded_text(
            payload.get("body")
            or payload.get("message")
            or payload.get("text")
            or title,
            limit=20_000,
        )
        thread_hint = _bounded_text(
            payload.get("task_id")
            or payload.get("thread_id")
            or payload.get("type")
            or row.id,
            limit=500,
        )
        result.append(_CommunicationItem(
            raw_ref=f"notification:{row.id}",
            dedupe_key=f"notification:{row.id}",
            connector="notification",
            thread_ref=f"notification:{thread_hint}",
            title=title,
            body=body,
            sender="Restia",
            direction="inbound",
            occurred_at=_parse_datetime(payload.get("timestamp")) or row.created_at,
            unread=row.acknowledged_at is None,
            explicit_importance=max(
                _importance_number(payload.get("importance")),
                _importance_number(payload.get("priority")),
                _importance_number(payload.get("urgency")),
            ),
        ))
    return result


def _collect_items(
    db,
    *,
    account: Account,
    email_cache_paths: Sequence[Path] | None,
) -> tuple[list[_CommunicationItem], dict[str, bool], list[str]]:
    life_items = _life_projection_items(db, account=account)
    life_thread_refs = {
        item.dedupe_key: item.thread_ref
        for item in life_items if item.connector == "email"
    }
    email_items, email_configured, errors = _email_cache_items(
        db,
        account=account,
        life_thread_refs=life_thread_refs,
        email_cache_paths=email_cache_paths,
    )
    cache_keys = {item.dedupe_key for item in email_items}
    items = [
        *email_items,
        *(item for item in life_items if item.dedupe_key not in cache_keys),
        *_direct_message_items(db, account=account),
        *_notification_items(db, account=account),
    ]
    telegram_configured = db.query(TelegramPrincipal.id).filter(
        TelegramPrincipal.account_id == account.id,
        TelegramPrincipal.state == "linked",
    ).first() is not None
    source_state = {
        name: any(item.connector == name for item in items)
        for name in COMMUNICATION_CONNECTORS
    }
    source_state.update({
        "email": email_configured or source_state["email"],
        "telegram": telegram_configured or source_state["telegram"],
        "restia_message": True,
        "notification": True,
    })
    items.sort(key=lambda item: (_timestamp(item.occurred_at), item.raw_ref), reverse=True)
    return items, source_state, errors


def _connector_policy(connector: str, *, enabled: bool) -> dict[str, Any]:
    draft_allowed = connector != "notification"
    transport_send_available = connector in {
        "email", "telegram", "restia_message",
    }
    policy = {
        "id": connector,
        "enabled": bool(enabled),
        "read": {
            "allowed": bool(enabled),
            "side_effect_free": True,
        },
        "draft": {
            "allowed": bool(enabled and draft_allowed),
            "suggestion_only": True,
            "requires_human_review": True,
        },
        "send": {
            "available_in_existing_connector": bool(
                enabled and transport_send_available
            ),
            "available_in_hub": False,
            "requires_human_confirmation": bool(
                enabled and transport_send_available
            ),
            "generic_capability": False,
        },
        "read_only_connector": connector in {
            "notification", *READ_ONLY_LIFE_COMMUNICATION_CHANNELS,
        },
    }
    if connector in READ_ONLY_LIFE_COMMUNICATION_CHANNELS:
        policy["capabilities"] = sorted(WHATSAPP_READ_ONLY_CAPABILITIES)
    return policy


def _serialize_item(item: _CommunicationItem, *, owner_id: str) -> dict[str, Any]:
    item_id = _public_id(owner_id, item.connector, item.raw_ref)
    signals = extract_communication_signals(item.body, sender=item.sender)
    return {
        "id": item_id,
        "connector": item.connector,
        "title": item.title,
        "preview": item.body[:800],
        "sender": item.sender,
        "direction": item.direction,
        "occurred_at": _iso(item.occurred_at),
        "unread": item.unread,
        "importance_evidence": item.explicit_importance,
        "signals": signals,
        "provenance": {
            "life_entity_id": item.source_entity_id,
            "canonical_projection": bool(item.source_entity_id),
        },
        "conversion": {
            "available": True,
            "kinds": sorted(INBOX_KINDS),
            "external_action": False,
        },
    }


def _draft_suggestion(
    latest: _CommunicationItem,
    *,
    signals: Mapping[str, Any],
) -> str:
    if latest.cached_draft:
        return latest.cached_draft
    if signals.get("deadlines"):
        return "Thanks — I’ve noted the deadline. I’ll confirm the next step shortly."
    if signals.get("questions"):
        return "Thanks for the message. I’ll check this and get back to you shortly."
    if signals.get("commitments"):
        return "Thanks — noted. Please keep me posted on the update."
    return "Thanks for the update."


def _serialize_thread(
    items: Sequence[_CommunicationItem],
    *,
    account: Account,
) -> dict[str, Any]:
    ordered = sorted(items, key=lambda item: (_timestamp(item.occurred_at), item.raw_ref))
    latest = ordered[-1]
    public_items = [_serialize_item(item, owner_id=account.id) for item in ordered]
    participants = sorted({item.sender for item in ordered if item.sender})[:20]
    commitments: list[dict[str, Any]] = []
    deadlines: list[dict[str, Any]] = []
    contacts: list[dict[str, Any]] = []
    questions: list[str] = []
    for raw, public in zip(ordered, public_items):
        signal = public["signals"]
        for value in signal["commitments"]:
            commitments.append({**value, "source_item_id": public["id"]})
        for value in signal["deadlines"]:
            deadlines.append({**value, "source_item_id": public["id"]})
        contacts.extend(signal["contacts"])
        questions.extend(signal["questions"])
    commitments = _dedupe_dicts(commitments, "text", "actor")[:12]
    deadlines = _dedupe_dicts(deadlines, "text")[:12]
    contacts = _dedupe_dicts(contacts, "kind", "value")[:20]
    unread_count = sum(item.unread is True for item in ordered)
    unknown_unread_count = sum(item.unread is None for item in ordered)
    importance_score = max((item.explicit_importance for item in ordered), default=0)
    if unread_count:
        importance_score = max(importance_score, 2)
    if deadlines or commitments or questions:
        importance_score = max(importance_score, 1)
    importance_label = "high" if importance_score >= 3 else (
        "normal" if importance_score else "low"
    )
    cached_summary = next(
        (item.cached_summary for item in reversed(ordered) if item.cached_summary),
        "",
    )
    summary = cached_summary or (
        f"{len(ordered)} message{'s' if len(ordered) != 1 else ''} via "
        f"{latest.connector.replace('_', ' ')}"
        + (f" with {', '.join(participants[:3])}." if participants else ".")
        + (f" Latest: {latest.body[:240]}" if latest.body else "")
    )
    follow_ups: list[dict[str, Any]] = []
    latest_public_id = public_items[-1]["id"]
    if latest.direction == "inbound" and latest.unread is True:
        follow_ups.append({
            "reason": "unread inbound communication",
            "suggested_action": "review and decide whether to reply",
            "source_item_id": latest_public_id,
        })
    if questions and latest.direction == "inbound":
        follow_ups.append({
            "reason": "question awaiting an answer",
            "suggested_action": "draft a response for review",
            "source_item_id": latest_public_id,
        })
    if deadlines:
        follow_ups.append({
            "reason": "deadline evidence found",
            "suggested_action": "convert the deadline or commitment to a Life task",
            "source_item_id": deadlines[0]["source_item_id"],
        })
    if commitments:
        follow_ups.append({
            "reason": "commitment evidence found",
            "suggested_action": "track the commitment and its follow-up",
            "source_item_id": commitments[0]["source_item_id"],
        })
    policy = _connector_policy(latest.connector, enabled=True)
    suggestion = None
    if latest.direction == "inbound" and policy["draft"]["allowed"]:
        suggestion = {
            "text": _draft_suggestion(latest, signals=public_items[-1]["signals"]),
            "kind": "draft_suggestion",
            "requires_human_review": True,
            "send_attempted": False,
            "available_in_hub": False,
        }
    thread_id = _public_id(account.id, "thread", f"{latest.connector}:{latest.thread_ref}")
    return {
        "id": thread_id,
        "connector": latest.connector,
        "title": latest.title,
        "participants": participants,
        "last_at": _iso(latest.occurred_at),
        "message_count": len(ordered),
        "unread_count": unread_count,
        "unknown_unread_count": unknown_unread_count,
        "importance": {
            "score": importance_score,
            "label": importance_label,
        },
        "summary": summary[:4_000],
        "response_suggestion": suggestion,
        "commitments": commitments,
        "deadlines": deadlines,
        "contacts": contacts,
        "follow_ups": _dedupe_dicts(follow_ups, "reason", "source_item_id")[:8],
        "messages": public_items[-MAX_THREAD_MESSAGES:],
        "messages_truncated": len(public_items) > MAX_THREAD_MESSAGES,
    }


def _empty_view(*, connector_state: Mapping[str, bool] | None = None) -> dict[str, Any]:
    state = {name: False for name in COMMUNICATION_CONNECTORS}
    if connector_state:
        state.update({name: bool(value) for name, value in connector_state.items()})
    return {
        "read_only": True,
        "threads": [],
        "count": 0,
        "total_threads": 0,
        "truncated": False,
        "unread_threads": 0,
        "unread_items": 0,
        "unknown_unread_items": 0,
        "important_threads": 0,
        "connectors": [
            _connector_policy(name, enabled=state[name])
            for name in COMMUNICATION_CONNECTORS
        ],
        "source_errors": [],
        "conversion_contract": {
            "path": "universal_inbox",
            "explicit_user_action_required": True,
            "classification_can_execute_external_action": False,
        },
        "send_endpoints": [],
    }


def empty_communications_view() -> dict[str, Any]:
    """Stable empty response for a principal without canonical Life rows."""

    return _empty_view()


def communications_view(
    db,
    *,
    account: Account,
    connectors: Iterable[str] | None = None,
    query: object = "",
    unread_only: bool = False,
    important_only: bool = False,
    limit: int = 50,
    email_cache_paths: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """Build one bounded view without acknowledging or mutating any source."""

    bounded_limit = max(1, min(100, int(limit)))
    selected = {
        str(value or "").strip().lower()
        for value in (connectors or COMMUNICATION_CONNECTORS)
        if str(value or "").strip()
    }
    unknown = selected - set(COMMUNICATION_CONNECTORS)
    if unknown:
        raise CommunicationsHubError(
            "Unsupported communication connector: " + ", ".join(sorted(unknown))
        )
    if not isinstance(unread_only, bool) or not isinstance(important_only, bool):
        raise CommunicationsHubError("Communication filters must be boolean")

    raw_items, source_state, source_errors = _collect_items(
        db, account=account, email_cache_paths=email_cache_paths
    )
    raw_items = [item for item in raw_items if item.connector in selected]
    grouped: dict[tuple[str, str], list[_CommunicationItem]] = {}
    for item in raw_items:
        grouped.setdefault((item.connector, item.thread_ref), []).append(item)
    threads = [
        _serialize_thread(values, account=account)
        for values in grouped.values()
    ]
    threads.sort(
        key=lambda thread: (str(thread.get("last_at") or ""), str(thread["id"])),
        reverse=True,
    )
    needle = str(query or "").strip().casefold()
    if needle:
        threads = [
            thread for thread in threads
            if needle in " ".join((
                str(thread.get("title") or ""),
                str(thread.get("summary") or ""),
                " ".join(str(value) for value in thread.get("participants", [])),
                " ".join(
                    str(message.get("preview") or "")
                    for message in thread.get("messages", [])
                ),
            )).casefold()
        ]
    if unread_only:
        threads = [thread for thread in threads if int(thread["unread_count"]) > 0]
    if important_only:
        threads = [
            thread for thread in threads
            if int(thread["importance"]["score"]) >= 3
        ]
    total = len(threads)
    returned = threads[:bounded_limit]
    item_counts = {name: 0 for name in COMMUNICATION_CONNECTORS}
    unread_counts = {name: 0 for name in COMMUNICATION_CONNECTORS}
    for item in raw_items:
        item_counts[item.connector] += 1
        if item.unread is True:
            unread_counts[item.connector] += 1
    connectors_payload = []
    for name in COMMUNICATION_CONNECTORS:
        policy = _connector_policy(name, enabled=source_state.get(name, False))
        policy["item_count"] = item_counts[name]
        policy["unread_count"] = unread_counts[name]
        connectors_payload.append(policy)
    return {
        "read_only": True,
        "threads": returned,
        "count": len(returned),
        "total_threads": total,
        "truncated": total > bounded_limit,
        "unread_threads": sum(int(thread["unread_count"]) > 0 for thread in threads),
        "unread_items": sum(item.unread is True for item in raw_items),
        "unknown_unread_items": sum(item.unread is None for item in raw_items),
        "important_threads": sum(
            int(thread["importance"]["score"]) >= 3 for thread in threads
        ),
        "connectors": connectors_payload,
        "source_errors": source_errors,
        "conversion_contract": {
            "path": "universal_inbox",
            "explicit_user_action_required": True,
            "classification_can_execute_external_action": False,
        },
        "send_endpoints": [],
    }


def convert_communication_item(
    db,
    *,
    account: Account,
    item_id: object,
    kind: object,
    process: bool = True,
    project_id: str | None = None,
    title: object | None = None,
    email_cache_paths: Sequence[Path] | None = None,
) -> dict[str, Any]:
    """Explicitly capture one visible item through Universal Inbox authority."""

    requested_id = str(item_id or "").strip()
    normalized_kind = str(kind or "").strip().lower().replace("-", "_")
    if normalized_kind not in INBOX_KINDS:
        raise CommunicationsHubError("Unsupported Universal Inbox conversion kind")
    if not isinstance(process, bool):
        raise CommunicationsHubError("process must be boolean")
    raw_items, _source_state, _errors = _collect_items(
        db, account=account, email_cache_paths=email_cache_paths
    )
    source = next(
        (
            item for item in raw_items
            if _public_id(account.id, item.connector, item.raw_ref) == requested_id
        ),
        None,
    )
    if source is None:
        raise CommunicationItemNotFound("Communication item not found")
    public_thread_id = _public_id(
        account.id, "thread", f"{source.connector}:{source.thread_ref}"
    )
    signals = extract_communication_signals(source.body, sender=source.sender)
    clean_title = _bounded_text(title, limit=240) if title is not None else source.title
    inbox_item, created = create_inbox_item(
        db,
        account=account,
        title=clean_title or source.title or source.body[:240],
        content=source.body or source.title,
        kind=normalized_kind,
        source_type=source.connector,
        source_ref=f"communications:{requested_id}",
        metadata={
            "communication": {
                "item_id": requested_id,
                "thread_id": public_thread_id,
                "connector": source.connector,
                "direction": source.direction,
                "sender": source.sender[:240],
                "signals": signals,
                "source_life_entity_id": source.source_entity_id,
            }
        },
        idempotency_key=f"communications-convert:{requested_id}:{normalized_kind}",
    )
    if process:
        inbox_item = process_inbox_item(
            db,
            account=account,
            item_id=inbox_item.id,
            expected_version=int(inbox_item.version or 1),
            project_id=project_id,
        )
    return {
        "item": serialize_inbox_item(inbox_item),
        "created": created,
        "processed": inbox_item.status in {"processed", "archived"},
        "conversion_path": "universal_inbox",
        "external_action_executed": False,
        "send_attempted": False,
    }
