"""Telegram bot webhook routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request

from core.middleware import require_admin
from src.auth_helpers import require_user
from src.external_chat import ExternalChatError, send_external_chat_message
from src.telegram_bot import (
    TELEGRAM_SECRET_HEADER,
    clear_telegram_session_id,
    consume_telegram_link_code,
    create_telegram_link_code,
    extract_telegram_message,
    get_telegram_session_id,
    is_chat_allowed,
    load_telegram_config,
    send_telegram_message,
    set_telegram_session_id,
    telegram_chat_ids_for_owner,
    telegram_owner_for_chat,
    telegram_status_payload,
    unlink_telegram_owner,
    verify_telegram_secret,
)

logger = logging.getLogger(__name__)


def _telegram_request_owner(request: Request) -> str:
    """Resolve a real account, or the stable single-user owner when auth is off."""
    return require_user(request) or load_telegram_config().owner or "local"


def _command(text: str) -> str:
    head = (text or "").strip().split(maxsplit=1)[0].lower()
    if "@" in head:
        head = head.split("@", 1)[0]
    return head


def _link_code(text: str) -> str:
    parts = (text or "").strip().split(maxsplit=1)
    if len(parts) != 2:
        return ""
    return parts[1].strip() if _command(parts[0]) in {"/link", "/start"} else ""


async def _reply(config, incoming, text: str) -> None:
    await send_telegram_message(
        config.bot_token,
        incoming.chat_id,
        text,
        reply_to_message_id=incoming.message_id,
    )


async def _safe_reply(config, incoming, text: str) -> None:
    try:
        await _reply(config, incoming, text)
    except Exception:
        logger.warning("Telegram reply delivery failed", exc_info=True)


async def _process_message(session_manager, webhook_manager, config, incoming) -> None:
    cmd = _command(incoming.text)
    code = _link_code(incoming.text)
    if code:
        owner = consume_telegram_link_code(code, incoming.chat_id)
        if owner:
            await _safe_reply(config, incoming, f"Telegram is now linked to your Restia account: {owner}.")
        else:
            await _safe_reply(config, incoming, "That Restia link code is invalid or expired. Generate a new code in Settings → Reminders.")
        return
    if cmd in {"/start", "/help"}:
        owner = telegram_owner_for_chat(config, incoming.chat_id)
        await _safe_reply(
            config,
            incoming,
            (f"Restia is connected as {owner}. Send a message to continue this chat, or use /new to start a fresh Restia chat."
             if owner else "This Telegram chat is not linked yet. In Restia, open Settings → Reminders → Telegram, generate a code, then send /link CODE here."),
        )
        return
    if cmd == "/new":
        clear_telegram_session_id(incoming.chat_id)
        await _safe_reply(config, incoming, "Started a fresh Restia chat for this Telegram thread.")
        return

    # Telegram has no browser headers to carry the user's clock, so anchor
    # this task's time context to the configured zone — otherwise relative
    # dates ("today", "at 5pm") resolve against the server's clock (UTC in
    # Docker) instead of the user's.
    try:
        from src.settings import get_setting
        from src.user_time import clear_user_time_context, set_user_tz_name

        clear_user_time_context()
        tz_name = str(get_setting("telegram_timezone", "") or "").strip()
        if tz_name:
            set_user_tz_name(tz_name)
    except Exception:
        logger.debug("Telegram timezone context setup failed", exc_info=True)

    try:
        owner = telegram_owner_for_chat(config, incoming.chat_id)
        if not owner:
            await _safe_reply(config, incoming, "Link this chat first: open Restia Settings → Reminders → Telegram and send /link CODE.")
            return
        result = await send_external_chat_message(
            session_manager,
            message=incoming.text,
            owner=owner,
            session_id=get_telegram_session_id(config, incoming.chat_id),
            session_name="Telegram Chat",
            source="telegram",
            webhook_manager=webhook_manager,
        )
        if result.created_session or get_telegram_session_id(config, incoming.chat_id) != result.session_id:
            set_telegram_session_id(incoming.chat_id, result.session_id)
        await _safe_reply(config, incoming, result.response)
    except ExternalChatError as exc:
        await _safe_reply(config, incoming, f"Restia is not ready: {exc}")
    except Exception:
        logger.exception("Telegram message processing failed")
        await _safe_reply(config, incoming, "Restia hit an internal error while processing that message.")


def setup_telegram_routes(session_manager, webhook_manager=None) -> APIRouter:
    router = APIRouter(prefix="/api/telegram", tags=["telegram"])

    @router.get("/status")
    def telegram_status(request: Request):
        require_admin(request)
        return telegram_status_payload(load_telegram_config())

    @router.get("/me")
    def telegram_me(owner: str = Depends(_telegram_request_owner)):
        config = load_telegram_config()
        linked = telegram_chat_ids_for_owner(config, owner)
        return {
            "enabled": config.enabled,
            "bot_token_configured": bool(config.bot_token),
            "linked": bool(linked),
            "linked_chat_count": len(linked),
        }

    @router.post("/link-code")
    def telegram_link_code(owner: str = Depends(_telegram_request_owner)):
        config = load_telegram_config()
        if not config.enabled or not config.bot_token:
            raise HTTPException(503, "Telegram bridge is not configured by the Restia administrator")
        code, expires_at = create_telegram_link_code(owner)
        return {
            "code": code,
            "command": f"/link {code}",
            "expires_at": expires_at,
            "expires_in": 600,
        }

    @router.delete("/link")
    def telegram_unlink(owner: str = Depends(_telegram_request_owner)):
        return {"ok": True, "removed": unlink_telegram_owner(owner)}

    @router.post("/webhook")
    async def telegram_webhook(request: Request, background_tasks: BackgroundTasks):
        config = load_telegram_config()
        if not config.enabled:
            raise HTTPException(404, "Telegram bridge is disabled")
        if not config.bot_token:
            raise HTTPException(503, "Telegram bot token is not configured")
        if not verify_telegram_secret(config, request.headers.get(TELEGRAM_SECRET_HEADER)):
            raise HTTPException(403, "Invalid Telegram webhook secret")

        try:
            update = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid Telegram update payload")

        incoming = extract_telegram_message(update)
        if incoming is None:
            return {"ok": True, "ignored": "unsupported_update"}
        # Unlinked chats may submit only a one-time /link (or /start CODE)
        # command. Every other message still obeys the allowlist.
        if not is_chat_allowed(config, incoming.chat_id) and not _link_code(incoming.text):
            logger.warning("Ignoring Telegram update from unauthorized chat_id=%s", incoming.chat_id)
            return {"ok": True, "ignored": "unauthorized_chat"}

        background_tasks.add_task(_process_message, session_manager, webhook_manager, config, incoming)
        return {"ok": True}

    return router
