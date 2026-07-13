"""Telegram bridge helpers for Restia."""

from __future__ import annotations

import logging
import os
import re
import secrets
import hashlib
import threading
import time
import traceback
from html import escape as _html_escape
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import httpx

from src.settings import load_settings, save_settings

logger = logging.getLogger(__name__)

TELEGRAM_SECRET_HEADER = "x-telegram-bot-api-secret-token"
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_SAFE_CHUNK = 3800
_CODE_BLOCK_RE = re.compile(r"```([A-Za-z0-9_+.-]*)\n?([\s\S]*?)```")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)<>]+|mailto:[^\s)<>]+)\)")
_BOLD_RE = re.compile(r"(\*\*|__)([^\n]+?)\1")
_STRIKE_RE = re.compile(r"~~([^\n]+?)~~")
_HEADING_RE = re.compile(r"(?m)^(#{1,6})\s+(.+)$")
_QUOTE_RE = re.compile(r"(?m)^&gt;\s?(.+)$")


class TelegramDeliveryError(RuntimeError):
    """Telegram delivery failed without exposing request credentials."""


class _TelegramTokenLogFilter(logging.Filter):
    """Redact the path credential from HTTPX request log records."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def clear(self) -> None:
        self._token = ""

    def _redact(self, value: Any) -> Any:
        if not self._token:
            return value
        try:
            rendered = str(value)
        except Exception:
            return value
        if self._token not in rendered:
            return value
        return rendered.replace(self._token, "[REDACTED]")

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: self._redact(value) for key, value in record.args.items()}
        record.stack_info = self._redact(record.stack_info)
        if record.exc_info:
            rendered = "".join(traceback.format_exception(*record.exc_info))
            if self._token in rendered:
                # Formatters render exc_info after filters run, so replacing
                # only msg/args would leave an exception-bearing log unsafe.
                record.exc_info = None
                record.exc_text = self._redact(rendered)
        return True


def _install_telegram_log_filter(token: str) -> tuple[_TelegramTokenLogFilter, list[Any]]:
    """Cover HTTPX plus propagated HTTP Core records for one request window."""
    token_filter = _TelegramTokenLogFilter(token)
    targets: list[Any] = []
    candidates: list[Any] = [
        logging.getLogger("httpx"),
        logging.getLogger("httpcore"),
        logging.getLogger("httpcore.connection"),
        logging.getLogger("httpcore.http11"),
        logging.getLogger("httpcore.http2"),
        logging.getLogger("httpcore.proxy"),
        logging.getLogger("httpcore.socks"),
        *logging.getLogger().handlers,
    ]
    for target in candidates:
        if target in targets:
            continue
        target.addFilter(token_filter)
        targets.append(target)
    return token_filter, targets


def _escape_telegram_href(value: str) -> str:
    return str(value or "").replace('"', "&quot;")


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool
    bot_token: str
    webhook_secret: str
    allowed_chat_ids: frozenset[str]
    allow_all_chats: bool
    owner: Optional[str]
    session_map: dict[str, str]
    chat_owners: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TelegramIncomingMessage:
    chat_id: str
    text: str
    message_id: Optional[int] = None


def _env(name: str) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _chat_ids(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        raw: Iterable[Any] = value.replace(" ", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        raw = value
    else:
        raw = [value]
    return frozenset(str(item).strip() for item in raw if str(item).strip())


def load_telegram_config() -> TelegramConfig:
    settings = load_settings()
    enabled = _as_bool(_env("TELEGRAM_ENABLED"), _as_bool(settings.get("telegram_enabled"), False))
    bot_token = _env("TELEGRAM_BOT_TOKEN") or str(settings.get("telegram_bot_token") or "").strip()
    webhook_secret = _env("TELEGRAM_WEBHOOK_SECRET") or str(settings.get("telegram_webhook_secret") or "").strip()
    allowed_raw = _env("TELEGRAM_ALLOWED_CHAT_IDS")
    allowed_chat_ids = _chat_ids(allowed_raw if allowed_raw is not None else settings.get("telegram_allowed_chat_ids"))
    allow_all = _as_bool(_env("TELEGRAM_ALLOW_ALL_CHATS"), _as_bool(settings.get("telegram_allow_all_chats"), False))
    owner = _env("TELEGRAM_OWNER") or str(settings.get("telegram_owner") or "").strip() or None
    session_map = settings.get("telegram_session_map") or {}
    if not isinstance(session_map, dict):
        session_map = {}
    chat_owners = settings.get("telegram_chat_owners") or {}
    if not isinstance(chat_owners, dict):
        chat_owners = {}
    return TelegramConfig(
        enabled=enabled,
        bot_token=bot_token,
        webhook_secret=webhook_secret,
        allowed_chat_ids=allowed_chat_ids,
        allow_all_chats=allow_all,
        owner=owner,
        session_map={str(k): str(v) for k, v in session_map.items() if str(k).strip() and str(v).strip()},
        chat_owners={str(k): str(v).strip().lower() for k, v in chat_owners.items() if str(k).strip() and str(v).strip()},
    )


def verify_telegram_secret(config: TelegramConfig, provided_secret: Optional[str]) -> bool:
    if not config.webhook_secret:
        return False
    provided = str(provided_secret or "")
    return bool(provided) and secrets.compare_digest(provided, config.webhook_secret)


def is_chat_allowed(config: TelegramConfig, chat_id: str) -> bool:
    chat_key = str(chat_id)
    return config.allow_all_chats or chat_key in config.allowed_chat_ids or chat_key in config.chat_owners


def telegram_owner_for_chat(config: TelegramConfig, chat_id: str) -> Optional[str]:
    """Return the local Restia account linked to a Telegram chat.

    ``telegram_owner`` remains a backwards-compatible fallback for existing
    single-user installations. New multi-user links always win.
    """
    return config.chat_owners.get(str(chat_id)) or config.owner


def telegram_chat_ids_for_owner(config: TelegramConfig, owner: str) -> list[str]:
    """Return only Telegram chats belonging to ``owner``.

    Legacy single-owner installations keep their old allowlist/session-map
    behavior. Once per-chat ownership exists, cross-user delivery is refused.
    """
    owner_key = str(owner or "").strip().lower()
    linked = {chat_id for chat_id, linked_owner in config.chat_owners.items() if linked_owner == owner_key}
    legacy = (set(config.allowed_chat_ids) | set(config.session_map.keys())) - set(config.chat_owners)
    if config.owner and config.owner.strip().lower() == owner_key:
        linked |= legacy
    elif not config.chat_owners and not config.owner:
        linked |= legacy
    return sorted(linked)


_TELEGRAM_LINK_TTL_SECONDS = 10 * 60
_telegram_link_lock = threading.RLock()


def _link_code_digest(code: str) -> str:
    return hashlib.sha256(str(code or "").strip().upper().encode("utf-8")).hexdigest()


def create_telegram_link_code(owner: str, *, ttl_seconds: int = _TELEGRAM_LINK_TTL_SECONDS) -> tuple[str, int]:
    """Create a short-lived, one-time code that links a Telegram chat."""
    owner_key = str(owner or "").strip().lower()
    if not owner_key:
        raise ValueError("A Restia user is required")
    ttl = max(60, min(int(ttl_seconds), 3600))
    now = int(time.time())
    code = secrets.token_hex(4).upper()
    with _telegram_link_lock:
        settings = load_settings()
        raw_codes = settings.get("telegram_link_codes") or {}
        codes = raw_codes if isinstance(raw_codes, dict) else {}
        codes = {
            str(digest): record
            for digest, record in codes.items()
            if isinstance(record, dict) and int(record.get("expires_at") or 0) > now
        }
        # A user needs at most one active code. Reissuing invalidates the old one.
        codes = {digest: record for digest, record in codes.items() if str(record.get("owner") or "").lower() != owner_key}
        codes[_link_code_digest(code)] = {"owner": owner_key, "expires_at": now + ttl}
        settings["telegram_link_codes"] = codes
        save_settings(settings)
    return code, now + ttl


def consume_telegram_link_code(code: str, chat_id: str) -> Optional[str]:
    """Consume a one-time link code and return the linked Restia username."""
    digest = _link_code_digest(code)
    chat_key = str(chat_id or "").strip()
    if not chat_key or not str(code or "").strip():
        return None
    now = int(time.time())
    with _telegram_link_lock:
        settings = load_settings()
        raw_codes = settings.get("telegram_link_codes") or {}
        codes = raw_codes if isinstance(raw_codes, dict) else {}
        record = codes.get(digest)
        if not isinstance(record, dict) or int(record.get("expires_at") or 0) <= now:
            return None
        owner = str(record.get("owner") or "").strip().lower()
        if not owner:
            return None
        codes.pop(digest, None)
        codes = {
            str(key): value
            for key, value in codes.items()
            if isinstance(value, dict) and int(value.get("expires_at") or 0) > now
        }
        chat_owners = settings.get("telegram_chat_owners") or {}
        if not isinstance(chat_owners, dict):
            chat_owners = {}
        previous_owner = str(chat_owners.get(chat_key) or "").strip().lower()
        chat_owners[chat_key] = owner
        allowed = set(_chat_ids(settings.get("telegram_allowed_chat_ids")))
        allowed.add(chat_key)
        if previous_owner and previous_owner != owner:
            session_map = settings.get("telegram_session_map") or {}
            if isinstance(session_map, dict):
                session_map.pop(chat_key, None)
                settings["telegram_session_map"] = session_map
        settings["telegram_link_codes"] = codes
        settings["telegram_chat_owners"] = chat_owners
        settings["telegram_allowed_chat_ids"] = sorted(allowed)
        save_settings(settings)
        return owner


def unlink_telegram_owner(owner: str) -> int:
    """Remove every Telegram chat linked to a local user."""
    owner_key = str(owner or "").strip().lower()
    if not owner_key:
        return 0
    with _telegram_link_lock:
        settings = load_settings()
        raw_owners = settings.get("telegram_chat_owners") or {}
        chat_owners = raw_owners if isinstance(raw_owners, dict) else {}
        removed = {chat_id for chat_id, linked_owner in chat_owners.items() if str(linked_owner).strip().lower() == owner_key}
        if not removed:
            return 0
        settings["telegram_chat_owners"] = {chat_id: linked_owner for chat_id, linked_owner in chat_owners.items() if chat_id not in removed}
        settings["telegram_allowed_chat_ids"] = sorted(set(_chat_ids(settings.get("telegram_allowed_chat_ids"))) - removed)
        session_map = settings.get("telegram_session_map") or {}
        if isinstance(session_map, dict):
            settings["telegram_session_map"] = {chat_id: session_id for chat_id, session_id in session_map.items() if chat_id not in removed}
        save_settings(settings)
        return len(removed)


def extract_telegram_message(update: dict[str, Any]) -> Optional[TelegramIncomingMessage]:
    if not isinstance(update, dict):
        return None
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    chat = message.get("chat") or {}
    if not isinstance(chat, dict) or chat.get("id") is None:
        return None
    text = message.get("text")
    if text is None:
        text = message.get("caption")
    if not isinstance(text, str) or not text.strip():
        return None
    message_id = message.get("message_id")
    return TelegramIncomingMessage(
        chat_id=str(chat.get("id")),
        text=text.strip(),
        message_id=message_id if isinstance(message_id, int) else None,
    )


def get_telegram_session_id(config: TelegramConfig, chat_id: str) -> Optional[str]:
    return config.session_map.get(str(chat_id))


def set_telegram_session_id(chat_id: str, session_id: str) -> None:
    settings = load_settings()
    session_map = settings.get("telegram_session_map") or {}
    if not isinstance(session_map, dict):
        session_map = {}
    session_map[str(chat_id)] = str(session_id)
    settings["telegram_session_map"] = session_map
    save_settings(settings)


def clear_telegram_session_id(chat_id: str) -> None:
    settings = load_settings()
    session_map = settings.get("telegram_session_map") or {}
    if not isinstance(session_map, dict):
        return
    if str(chat_id) in session_map:
        session_map.pop(str(chat_id), None)
        settings["telegram_session_map"] = session_map
        save_settings(settings)


def telegram_status_payload(config: TelegramConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "bot_token_configured": bool(config.bot_token),
        "webhook_secret_configured": bool(config.webhook_secret),
        "allowed_chat_ids_count": len(config.allowed_chat_ids),
        "allow_all_chats": config.allow_all_chats,
        "owner": config.owner or "",
        "active_chats": len(config.session_map),
        "linked_chats": len(config.chat_owners),
        "linked_users": len(set(config.chat_owners.values())),
        "webhook_path": "/api/telegram/webhook",
    }


def _chunks(text: str) -> list[str]:
    text = str(text or "")
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > TELEGRAM_MESSAGE_LIMIT:
        split_at = remaining.rfind("\n", 0, TELEGRAM_SAFE_CHUNK)
        if split_at < 1:
            split_at = TELEGRAM_SAFE_CHUNK
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    chunks.append(remaining)
    return chunks


def format_telegram_html(text: str) -> str:
    """Render a safe subset of Markdown as Telegram HTML parse mode."""
    raw = str(text or "")
    placeholders: list[tuple[str, str]] = []

    def _stash_code(match: re.Match[str]) -> str:
        language = (match.group(1) or "").strip()
        code = _html_escape((match.group(2) or "").strip("\n"), quote=False)
        lang_attr = f' class="language-{_html_escape(language, quote=True)}"' if language else ""
        token = f"@@RESTIA_TG_CODE_{len(placeholders)}@@"
        placeholders.append((token, f"<pre><code{lang_attr}>{code}</code></pre>"))
        return token

    raw = _CODE_BLOCK_RE.sub(_stash_code, raw)
    html = _html_escape(raw, quote=False)

    html = _LINK_RE.sub(lambda m: f'<a href="{_escape_telegram_href(m.group(2))}">{m.group(1)}</a>', html)
    html = _INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", html)
    html = _HEADING_RE.sub(lambda m: f"<b>{m.group(2)}</b>", html)
    html = _BOLD_RE.sub(lambda m: f"<b>{m.group(2)}</b>", html)
    html = _STRIKE_RE.sub(lambda m: f"<s>{m.group(1)}</s>", html)
    html = _QUOTE_RE.sub(lambda m: f"<blockquote>{m.group(1)}</blockquote>", html)

    for token, replacement in placeholders:
        html = html.replace(token, replacement)
    return html


async def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    reply_to_message_id: Optional[int] = None,
    rich_text: bool = True,
) -> None:
    if not bot_token:
        raise ValueError("Telegram bot token is not configured")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    token_filter, filter_targets = _install_telegram_log_filter(bot_token)
    failure: str | None = None
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            first = True
            for chunk in _chunks(text):
                payload: dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": format_telegram_html(chunk) if rich_text else str(chunk or ""),
                }
                if rich_text:
                    payload["parse_mode"] = "HTML"
                if first and reply_to_message_id is not None:
                    payload["reply_to_message_id"] = reply_to_message_id
                    payload["allow_sending_without_reply"] = True
                response = await client.post(url, json=payload)
                status = int(getattr(response, "status_code", 200))
                if status < 200 or status >= 300:
                    failure = f"Telegram API request failed with HTTP {status}"
                    break
                first = False
    except httpx.HTTPError as exc:
        failure = f"Telegram API request failed ({exc.__class__.__name__})"
    except Exception:
        # Third-party transports/hooks can raise non-HTTPX exceptions whose
        # message contains the request URL. Never let that text escape.
        failure = "Telegram API request failed"
    finally:
        try:
            for target in filter_targets:
                target.removeFilter(token_filter)
        finally:
            token_filter.clear()

    # Raise outside the exception handler so the sensitive HTTPX exception is
    # not retained as __context__ or rendered by an upstream traceback logger.
    if failure:
        # Some error reporters capture frame locals. Clear every local that can
        # retain the Bot API credential or the credential-bearing request.
        bot_token = ""
        url = ""
        response = None
        client = None
        filter_targets = []
        raise TelegramDeliveryError(failure) from None
