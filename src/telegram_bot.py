"""Telegram bridge helpers for Restia."""

from __future__ import annotations

import logging
import os
import re
import secrets
from html import escape as _html_escape
from dataclasses import dataclass
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
    return TelegramConfig(
        enabled=enabled,
        bot_token=bot_token,
        webhook_secret=webhook_secret,
        allowed_chat_ids=allowed_chat_ids,
        allow_all_chats=allow_all,
        owner=owner,
        session_map={str(k): str(v) for k, v in session_map.items() if str(k).strip() and str(v).strip()},
    )


def verify_telegram_secret(config: TelegramConfig, provided_secret: Optional[str]) -> bool:
    if not config.webhook_secret:
        return False
    provided = str(provided_secret or "")
    return bool(provided) and secrets.compare_digest(provided, config.webhook_secret)


def is_chat_allowed(config: TelegramConfig, chat_id: str) -> bool:
    return config.allow_all_chats or str(chat_id) in config.allowed_chat_ids


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
            response.raise_for_status()
            first = False
