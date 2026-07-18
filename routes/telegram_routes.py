"""Telegram bot setup, profile linking, polling, and webhook routes."""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel

from core.middleware import require_admin
from src.auth_helpers import require_user, resolved_request_owner
from src.external_chat import ExternalChatError, send_external_chat_message
from src.life_ingestion import ingest_telegram_message
from src.notification_preferences import (
    NotificationPreferenceError,
    load_notification_preferences,
    notification_timezone_configured,
    save_notification_preferences,
)
from src.settings import load_settings, save_settings
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
from src.telegram_runtime import (
    TELEGRAM_TOKEN_RE,
    TelegramAPIError,
    TelegramWebhookConflict,
    configure_telegram_transport,
    inspect_telegram_bot,
    inspect_telegram_webhook,
    telegram_api_call,
    telegram_polling_service,
    telegram_runtime_mode,
)
from src.telegram_identity import revoke_bot_authority

logger = logging.getLogger(__name__)


def _telegram_request_owner(request: Request) -> str:
    """Resolve a real account, or the stable single-user owner when auth is off."""
    admitted = require_user(request)
    return resolved_request_owner(request, admitted_user=admitted)


class TelegramConfigUpdate(BaseModel):
    bot_token: str = ""
    enabled: bool = True
    mode: str = "polling"
    public_url: str = ""
    replace_existing_webhook: bool = False


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


def _chat_log_ref(config, chat_id: str) -> str:
    """Return a bounded keyed reference; never place a chat ID in logs."""

    try:
        from src.telegram_identity import telegram_chat_digest

        if config.bot_fingerprint:
            return telegram_chat_digest(config.bot_fingerprint, chat_id)[:12]
    except Exception:
        pass
    return "unlinked"


async def _reply(config, incoming, text: str) -> None:
    await send_telegram_message(
        config.bot_token,
        incoming.chat_id,
        text,
        reply_to_message_id=incoming.message_id,
    )


async def _build_message_reply(session_manager, webhook_manager, config, incoming) -> str:
    """Apply one inbound message and return the reply without sending it."""

    cmd = _command(incoming.text)
    code = _link_code(incoming.text)
    if code:
        owner = consume_telegram_link_code(code, incoming.chat_id)
        if owner:
            return f"Telegram is now linked to your Restia account: {owner}."
        return "That Restia link code is invalid or expired. Generate a new code in Settings → Reminders."
    if cmd in {"/start", "/help"}:
        owner = telegram_owner_for_chat(config, incoming.chat_id)
        return (
            f"Restia is connected as {owner}. Send a message to continue this chat, or use /new to start a fresh Restia chat."
            if owner
            else "This Telegram chat is not linked yet. In Restia, open Settings → Reminders → Telegram, generate a code, then send /link CODE here."
        )
    if cmd == "/new":
        clear_telegram_session_id(incoming.chat_id)
        return "Started a fresh Restia chat for this Telegram thread."

    try:
        owner = telegram_owner_for_chat(config, incoming.chat_id)
        if not owner:
            return "Link this chat first: open Restia Settings → Reminders → Telegram and send /link CODE."
        # Telegram has no browser headers to carry the user's clock. Anchor
        # relative dates to this linked profile's own notification timezone.
        try:
            from src.user_time import clear_user_time_context, set_user_tz_name

            clear_user_time_context()
            tz_name = str(load_notification_preferences(owner).get("timezone") or "").strip()
            if tz_name:
                set_user_tz_name(tz_name)
        except Exception:
            logger.debug("Telegram timezone context setup failed", exc_info=True)
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
        return str(result.response or "")
    except ExternalChatError as exc:
        return f"Restia is not ready: {exc}"


async def _process_message(session_manager, webhook_manager, config, incoming) -> None:
    """Compatibility entrypoint: process, then propagate reply failures."""

    reply = await _build_message_reply(session_manager, webhook_manager, config, incoming)
    await _reply(config, incoming, reply)


def _ingest_telegram_incoming(
    config,
    incoming,
    *,
    fingerprint: str,
    update_id: int | None,
    expected_owner_id: str | None = None,
):
    """Project one ordinary linked message; commands remain control traffic."""

    if _command(incoming.text).startswith("/"):
        return None
    owner = telegram_owner_for_chat(config, incoming.chat_id)
    if not owner:
        return None
    return ingest_telegram_message(
        owner=owner,
        bot_fingerprint=fingerprint,
        chat_id=incoming.chat_id,
        text=incoming.text,
        message_id=incoming.message_id,
        update_id=update_id,
        expected_owner_id=expected_owner_id,
    )


def _telegram_owner_account_id(config, incoming) -> str:
    """Resolve the immutable first-claim principal for one Telegram update."""

    # A one-time link command intentionally starts without an owner and may
    # create that link while processing. Keep its claim unbound; commands are
    # control traffic and never enter the Life graph.
    if _link_code(incoming.text):
        return ""
    owner = telegram_owner_for_chat(config, incoming.chat_id)
    if not owner:
        return ""
    from core.database import SessionLocal
    from src.identity import find_account

    db = SessionLocal()
    try:
        account = find_account(db, owner)
        if account is None:
            raise RuntimeError("Telegram linked account is unavailable")
        return str(account.id)
    finally:
        db.close()


async def _process_update_durably(session_manager, webhook_manager, config, update: dict) -> None:
    """Process an update once and retry only its durably stored reply."""

    from src.constants import DATA_DIR
    from src.telegram_inbound_ledger import (
        bot_fingerprint as legacy_bot_fingerprint,
    )
    from src.telegram_delivery import (
        TelegramInboundInFlight,
        TelegramReplyPending,
        telegram_runtime_authority,
    )
    from src.telegram_identity import (
        telegram_bot_fingerprint,
    )

    incoming = extract_telegram_message(update)
    if incoming is None:
        return
    raw_update_id = update.get("update_id")
    owner_account_id = _telegram_owner_account_id(config, incoming)
    runtime_fingerprint = (
        str(config.bot_fingerprint or "")
        or telegram_bot_fingerprint(bot_token=config.bot_token)
    )
    ingestion_fingerprint = legacy_bot_fingerprint(config.bot_token)
    try:
        update_id = int(raw_update_id)
    except (TypeError, ValueError):
        _ingest_telegram_incoming(
            config,
            incoming,
            fingerprint=ingestion_fingerprint,
            update_id=None,
            expected_owner_id=owner_account_id or None,
        )
        await _process_message(session_manager, webhook_manager, config, incoming)
        return

    # This is an idempotent marker read after the first successful cutover.
    # The retired sidecars are never a runtime fallback.
    telegram_runtime_authority.adopt_legacy_sidecars(
        bot_fingerprint=runtime_fingerprint,
        bot_token=config.bot_token,
        data_dir=DATA_DIR,
    )
    worker_id = "inbound:" + uuid.uuid4().hex
    acquired, record = telegram_runtime_authority.claim_inbound_processing(
        bot_fingerprint=runtime_fingerprint,
        update_id=update_id,
        chat_id=incoming.chat_id,
        owner_account_id=owner_account_id,
        worker_id=worker_id,
    )
    if not acquired:
        status = str((record or {}).get("status") or "")
        if status in {"delivered", "discarded"}:
            return
        if status == "reply_pending" and (record or {}).get("reply_text"):
            if str(record.get("chat_id") or "") != str(incoming.chat_id):
                raise RuntimeError("Telegram update replay chat does not match its durable record")
            claimed, delivery = telegram_runtime_authority.claim_reply_delivery(
                bot_fingerprint=runtime_fingerprint,
                update_id=update_id,
                chat_id=incoming.chat_id,
                owner_account_id=owner_account_id,
                worker_id=worker_id,
            )
            if not claimed or delivery is None:
                if str((delivery or {}).get("status") or "") in {
                    "delivered", "discarded",
                }:
                    return
                raise TelegramReplyPending()
            try:
                await _reply(config, incoming, str(delivery["reply_text"]))
            except Exception as exc:
                telegram_runtime_authority.release_reply_delivery(
                    bot_fingerprint=runtime_fingerprint,
                    update_id=update_id,
                    reply_claim_token=delivery["reply_claim_token"],
                )
                raise TelegramReplyPending() from exc
            if not telegram_runtime_authority.mark_inbound_delivered(
                bot_fingerprint=runtime_fingerprint,
                update_id=update_id,
                reply_claim_token=delivery["reply_claim_token"],
            ):
                raise TelegramReplyPending()
            return
        raise TelegramInboundInFlight()

    # Ingestion happens only for the durable owner of this update and before
    # reply generation.  Its exception intentionally sits outside the broad
    # reply catch below: the ledger keeps the processing lease, so a later
    # retry can re-run this idempotent projection instead of marking the update
    # delivered without its Life evidence.
    _ingest_telegram_incoming(
        config,
        incoming,
        fingerprint=ingestion_fingerprint,
        update_id=update_id,
        expected_owner_id=owner_account_id or None,
    )
    try:
        reply_text = await _build_message_reply(
            session_manager, webhook_manager, config, incoming,
        )
    except Exception:
        logger.exception("Telegram message processing failed")
        reply_text = "Restia hit an internal error while processing that message."
    delivery = telegram_runtime_authority.store_inbound_reply(
        bot_fingerprint=runtime_fingerprint,
        update_id=update_id,
        chat_id=incoming.chat_id,
        reply_text=reply_text,
        owner_account_id=owner_account_id,
        processing_claim_token=str(
            (record or {}).get("processing_claim_token") or ""
        ),
    )
    try:
        await _reply(config, incoming, reply_text)
    except Exception as exc:
        telegram_runtime_authority.release_reply_delivery(
            bot_fingerprint=runtime_fingerprint,
            update_id=update_id,
            reply_claim_token=delivery["reply_claim_token"],
        )
        raise TelegramReplyPending() from exc
    if not telegram_runtime_authority.mark_inbound_delivered(
        bot_fingerprint=runtime_fingerprint,
        update_id=update_id,
        reply_claim_token=delivery["reply_claim_token"],
    ):
        raise TelegramReplyPending()


def _telegram_setup_payload() -> dict:
    settings = load_settings()
    config = load_telegram_config()
    runtime = telegram_polling_service.status()
    mode = telegram_runtime_mode(settings)
    username = str(settings.get("telegram_bot_username") or "")
    return {
        "enabled": bool(config.enabled),
        "bot_token_configured": bool(config.bot_token),
        "bot_token_managed_by_env": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
        "mode": mode,
        "bot": {
            "id": str(settings.get("telegram_bot_id") or ""),
            "username": username,
            "first_name": str(settings.get("telegram_bot_first_name") or ""),
            "url": f"https://t.me/{username}" if username else "",
        },
        "public_url": str(settings.get("app_public_url") or ""),
        "webhook_registered": bool(settings.get("telegram_registered_webhook_url")),
        "linked_chats": len(config.chat_owners),
        "linked_profiles": len(set(config.chat_owners.values())),
        "runtime": runtime,
    }


def _sync_digest_task(owner: str, preferences: dict) -> None:
    """Keep the existing built-in digest task aligned with profile cadence."""
    from core.database import ScheduledTask, SessionLocal, utcnow_naive
    from src.task_scheduler import compute_next_run

    cadence = str(preferences.get("digest_cadence") or "off")
    active = cadence != "off"
    cron_expression = {
        "hourly": "0 * * * *",
        "every_3_hours": "0 */3 * * *",
        "every_6_hours": "0 */6 * * *",
    }.get(cadence, "0 * * * *")
    if cadence == "daily":
        try:
            hour, minute = (int(part) for part in str(preferences.get("digest_time") or "08:00").split(":"))
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except (TypeError, ValueError):
            hour, minute = 8, 0
        cron_expression = f"{minute} {hour} * * *"
    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(
            ScheduledTask.owner == owner,
            ScheduledTask.task_type == "action",
            ScheduledTask.action == "telegram_hourly_digest",
        ).first()
        if task is None:
            task = ScheduledTask(
                id=str(uuid.uuid4()),
                owner=owner,
                name="Telegram Digest",
                task_type="action",
                action="telegram_hourly_digest",
                trigger_type="schedule",
                schedule="cron",
                cron_expression=cron_expression,
                output_target="none",
                notifications_enabled=False,
            )
            db.add(task)
        task.prompt = json.dumps({
            "managed_by": "notification_preferences",
            "notification_timezone": preferences.get("timezone") or "UTC",
        })
        task.cron_expression = cron_expression
        task.status = "active" if active else "paused"
        task.next_run = (
            compute_next_run(
                "cron",
                None,
                after=utcnow_naive(),
                cron_expression=cron_expression,
                tz_name=str(preferences.get("timezone") or "UTC"),
            )
            if active else None
        )
        db.commit()
    finally:
        db.close()


def setup_telegram_routes(session_manager, webhook_manager=None) -> APIRouter:
    router = APIRouter(prefix="/api/telegram", tags=["telegram"])

    async def _handle_polled_update(update: dict) -> None:
        config = load_telegram_config()
        incoming = extract_telegram_message(update)
        if incoming is None:
            return
        if not is_chat_allowed(config, incoming.chat_id) and not _link_code(incoming.text):
            logger.warning(
                "Ignoring Telegram update from unauthorized chat_ref=%s",
                _chat_log_ref(config, incoming.chat_id),
            )
            return
        await _process_update_durably(
            session_manager,
            webhook_manager,
            config,
            update,
        )

    telegram_polling_service.configure(_handle_polled_update)

    @router.get("/status")
    def telegram_status(request: Request):
        require_admin(request)
        return {**telegram_status_payload(load_telegram_config()), **_telegram_setup_payload()}

    @router.get("/config")
    def telegram_config(request: Request):
        require_admin(request)
        return _telegram_setup_payload()

    @router.put("/config")
    async def update_telegram_config(request: Request, body: TelegramConfigUpdate):
        require_admin(request)
        settings = load_settings()
        env_token = str(os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        supplied = str(body.bot_token or "").strip()
        if supplied and env_token:
            raise HTTPException(409, "Telegram token is managed by TELEGRAM_BOT_TOKEN and cannot be replaced in the UI")
        token = supplied or env_token or str(settings.get("telegram_bot_token") or "").strip()
        if not token:
            raise HTTPException(400, "Enter the bot token from BotFather")
        if not TELEGRAM_TOKEN_RE.fullmatch(token):
            raise HTTPException(400, "That does not look like a Telegram bot token")

        mode = str(body.mode or "polling").strip().lower()
        public_url = str(body.public_url or settings.get("app_public_url") or "").strip().rstrip("/")
        webhook_secret = str(settings.get("telegram_webhook_secret") or "").strip() or secrets.token_urlsafe(32)
        old_config = load_telegram_config()
        old_token = old_config.bot_token
        old_bot_fingerprint = old_config.bot_fingerprint
        old_registered = str(settings.get("telegram_registered_webhook_url") or "")
        replacement_previous_url = ""
        try:
            identity = await inspect_telegram_bot(token)
            replacement_previous_url = str((await inspect_telegram_webhook(token)).get("url") or "")
        except TelegramAPIError as exc:
            raise HTTPException(400, str(exc)) from None

        if mode not in {"polling", "webhook"}:
            raise HTTPException(400, "Telegram transport must be polling or webhook")
        if mode == "webhook" and not public_url.startswith("https://"):
            raise HTTPException(400, "Webhook mode requires this instance's public HTTPS URL")
        previous_bot_id = str(settings.get("telegram_bot_id") or "")
        bot_changed = bool(
            (previous_bot_id and previous_bot_id != identity["id"])
            or (
                not previous_bot_id
                and old_token
                and old_token != token
            )
        )
        expected_webhook_url = f"{public_url}/api/telegram/webhook" if mode == "webhook" else ""
        replacement_owned = bool(
            replacement_previous_url
            and replacement_previous_url == old_registered
            and identity["id"] == previous_bot_id
        )
        foreign_takeover = bool(replacement_previous_url and not replacement_owned)
        if foreign_takeover and not body.replace_existing_webhook:
            raise HTTPException(
                409,
                "This bot already has a webhook owned by another service. "
                "Choose Replace existing webhook only if this bot belongs to this Restia instance.",
            )

        candidate = dict(settings)
        candidate["telegram_enabled"] = bool(body.enabled)
        if not env_token:
            candidate["telegram_bot_token"] = token
        candidate["telegram_webhook_secret"] = webhook_secret
        candidate["telegram_runtime_mode"] = mode
        candidate["telegram_bot_id"] = identity["id"]
        candidate["telegram_bot_username"] = identity["username"]
        candidate["telegram_bot_first_name"] = identity["first_name"]
        candidate["telegram_registered_webhook_url"] = expected_webhook_url
        if public_url:
            candidate["app_public_url"] = public_url
        if bot_changed:
            # Chat IDs are meaningful only for the bot that received the link
            # command. Never carry account links to a replacement bot.
            candidate["telegram_allowed_chat_ids"] = []
            candidate["telegram_chat_owners"] = {}
            candidate["telegram_session_map"] = {}
            candidate["telegram_link_codes"] = {}

        # Telegram cannot reveal a foreign webhook's secret_token, so it cannot
        # be reconstructed after takeover. Persist the complete candidate first
        # in that explicit-takeover case; a local write failure then performs no
        # Telegram mutation at all.
        candidate_pre_saved = False
        if foreign_takeover:
            try:
                save_settings(candidate)
                candidate_pre_saved = True
            except Exception as exc:
                raise HTTPException(
                    500,
                    "Telegram configuration could not be saved; the existing webhook was not changed",
                ) from exc

        try:
            transport = await configure_telegram_transport(
                bot_token=token,
                mode=mode,
                bot_identity=identity,
                public_url=public_url,
                webhook_secret=webhook_secret,
                owned_webhook_url=old_registered,
                owned_bot_id=previous_bot_id,
                replace_foreign_webhook=bool(body.replace_existing_webhook),
            )
        except (TelegramWebhookConflict, TelegramAPIError) as exc:
            if candidate_pre_saved:
                try:
                    save_settings(settings)
                except Exception:
                    logger.exception("Telegram candidate settings rollback failed")
                    raise HTTPException(500, "Telegram setup failed and the previous local configuration could not be restored") from None
            status = 409 if isinstance(exc, TelegramWebhookConflict) else 400
            raise HTTPException(status, str(exc)) from None

        candidate["telegram_runtime_mode"] = transport["mode"]
        candidate["telegram_registered_webhook_url"] = transport["webhook_url"]
        if not candidate_pre_saved:
            try:
                save_settings(candidate)
            except Exception as exc:
            # The old bot is still untouched. Best-effort rollback the new
            # bot's transport so a local persistence failure does not leave an
            # externally active configuration Restia does not own.
                try:
                    if replacement_previous_url:
                        rollback_payload: dict[str, Any] = {
                            "url": replacement_previous_url,
                            "drop_pending_updates": False,
                        }
                        if (
                            token == old_token
                            and replacement_previous_url == old_registered
                            and webhook_secret
                        ):
                            rollback_payload.update({
                                "secret_token": webhook_secret,
                                "allowed_updates": ["message", "edited_message"],
                            })
                        await telegram_api_call(token, "setWebhook", payload=rollback_payload)
                    elif transport.get("webhook_url"):
                        await telegram_api_call(
                            token, "deleteWebhook", payload={"drop_pending_updates": False}
                        )
                except TelegramAPIError:
                    logger.exception("Telegram replacement transport rollback failed")
                raise HTTPException(500, "Telegram configuration could not be saved; the existing bot was kept") from exc

        settings = candidate

        cleanup_warning = ""
        # Only after the replacement is validated, configured, and durably
        # selected do we detach an owned webhook from the old bot.
        if old_token and old_token != token and old_registered:
            try:
                old_identity = await inspect_telegram_bot(old_token)
                old_info = await inspect_telegram_webhook(old_token)
                if (
                    old_identity["id"] == previous_bot_id
                    and old_info["url"] == old_registered
                ):
                    await telegram_api_call(
                        old_token, "deleteWebhook", payload={"drop_pending_updates": False}
                    )
            except TelegramAPIError:
                cleanup_warning = "The new bot is active, but the old bot webhook could not be detached automatically."
                logger.warning("Telegram old-bot webhook cleanup failed after replacement")
        if bot_changed and old_bot_fingerprint:
            # SQL is the identity authority. A replacement bot must not regain
            # the previous bot's chat principals if its old credential is
            # configured again later.
            revoke_bot_authority(old_bot_fingerprint)
        telegram_polling_service.wake()
        # Drop every local reference to the plaintext credential before return.
        token = ""
        supplied = ""
        old_token = ""
        payload = _telegram_setup_payload()
        if cleanup_warning:
            payload["cleanup_warning"] = cleanup_warning
        return payload

    @router.delete("/config")
    async def delete_telegram_config(request: Request):
        require_admin(request)
        if os.getenv("TELEGRAM_BOT_TOKEN"):
            raise HTTPException(409, "Remove TELEGRAM_BOT_TOKEN from the instance environment to disconnect this bot")
        settings = load_settings()
        old_config = load_telegram_config()
        token = str(settings.get("telegram_bot_token") or "").strip()
        registered = str(settings.get("telegram_registered_webhook_url") or "")
        candidate = dict(settings)
        for key, value in {
            "telegram_enabled": False,
            "telegram_bot_token": "",
            "telegram_webhook_secret": "",
            "telegram_bot_id": "",
            "telegram_bot_username": "",
            "telegram_bot_first_name": "",
            "telegram_registered_webhook_url": "",
            "telegram_allowed_chat_ids": [],
            "telegram_chat_owners": {},
            "telegram_session_map": {},
            "telegram_link_codes": {},
        }.items():
            candidate[key] = value
        # Persist the complete local teardown before detaching Telegram. A
        # disk failure therefore cannot leave Restia claiming ownership of a
        # webhook that it already removed externally.
        try:
            save_settings(candidate)
        except Exception as exc:
            raise HTTPException(
                500,
                "Telegram configuration could not be removed; the registered webhook was not changed",
            ) from exc
        if token and registered:
            try:
                identity = await inspect_telegram_bot(token)
                info = await inspect_telegram_webhook(token)
                if (
                    identity["id"] == str(settings.get("telegram_bot_id") or "")
                    and info["url"] == registered
                ):
                    await telegram_api_call(token, "deleteWebhook", payload={"drop_pending_updates": False})
            except TelegramAPIError as exc:
                try:
                    save_settings(settings)
                except Exception:
                    logger.exception("Telegram teardown rollback failed")
                    raise HTTPException(
                        500,
                        "Telegram webhook removal failed and the previous local configuration could not be restored",
                    ) from None
                raise HTTPException(502, f"Could not safely disconnect the registered Telegram webhook: {exc}") from None
        if old_config.bot_fingerprint:
            revoke_bot_authority(old_config.bot_fingerprint)
        token = ""
        telegram_polling_service.wake()
        return {"ok": True}

    @router.get("/me")
    def telegram_me(owner: str = Depends(_telegram_request_owner)):
        settings = load_settings()
        config = load_telegram_config()
        linked = telegram_chat_ids_for_owner(config, owner)
        username = str(settings.get("telegram_bot_username") or "")
        return {
            "enabled": config.enabled,
            "bot_token_configured": bool(config.bot_token),
            "linked": bool(linked),
            "linked_chat_count": len(linked),
            "bot_username": username,
            "bot_url": f"https://t.me/{username}" if username else "",
            "mode": telegram_runtime_mode(settings),
        }

    @router.get("/preferences")
    def telegram_preferences(owner: str = Depends(_telegram_request_owner)):
        return {
            **load_notification_preferences(owner),
            "timezone_configured": notification_timezone_configured(owner),
        }

    @router.put("/preferences")
    async def update_telegram_preferences(request: Request, owner: str = Depends(_telegram_request_owner)):
        try:
            body = await request.json()
            previous = load_notification_preferences(owner)
            preferences = save_notification_preferences(owner, body)
            if (
                previous.get("digest_cadence") != preferences.get("digest_cadence")
                or list(previous.get("notification_topics") or [])
                != list(preferences.get("notification_topics") or [])
            ):
                from src.builtin_actions import clear_telegram_digest_pending

                await clear_telegram_digest_pending(owner)
            _sync_digest_task(owner, preferences)
            return {
                **preferences,
                "timezone_configured": notification_timezone_configured(owner),
            }
        except NotificationPreferenceError as exc:
            raise HTTPException(400, str(exc)) from None
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid notification preferences") from None

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
        removed = unlink_telegram_owner(owner)
        preferences = load_notification_preferences(owner)
        fallback: dict[str, Any] = {}
        if preferences.get("reminder_channel") == "telegram":
            fallback["reminder_channel"] = "browser"
        if preferences.get("reminder_telegram_mirror"):
            fallback["reminder_telegram_mirror"] = False
        if fallback:
            preferences = save_notification_preferences(owner, fallback)
            _sync_digest_task(owner, preferences)
        return {
            "ok": True,
            "removed": removed,
            "reminder_channel": preferences.get("reminder_channel"),
            "reminder_telegram_mirror": bool(preferences.get("reminder_telegram_mirror")),
        }

    @router.post("/test")
    async def telegram_test(owner: str = Depends(_telegram_request_owner)):
        config = load_telegram_config()
        if not (config.enabled and config.bot_token):
            raise HTTPException(503, "Telegram bot is not configured for this Restia instance")
        if not telegram_chat_ids_for_owner(config, owner):
            raise HTTPException(409, "Link this profile to a Telegram chat first")
        from routes.note_routes import dispatch_reminder

        result = await dispatch_reminder(
            title="Restia Telegram test",
            note_body="Telegram is connected to this Restia profile.",
            note_id=f"telegram-test-{owner}-{int(time.time() * 1000)}",
            owner=owner,
            queue_browser=False,
            settings_override={
                "reminder_channel": "telegram",
                "reminder_telegram_mirror": False,
                # A connectivity test verifies the bot/link, independently of
                # the profile's normal topic suppression policy.
                "notification_topics": ["reminders"],
            },
            respect_quiet_hours=False,
        )
        if not result.get("telegram_sent"):
            raise HTTPException(502, result.get("telegram_error") or "Telegram test was not delivered")
        return {"ok": True, "telegram_sent": True}

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
            logger.warning(
                "Ignoring Telegram update from unauthorized chat_ref=%s",
                _chat_log_ref(config, incoming.chat_id),
            )
            return {"ok": True, "ignored": "unauthorized_chat"}

        # Acknowledge Telegram only after Restia has accepted and processed the
        # update. If this process dies first, Telegram retries instead of the
        # message being silently lost in an in-memory background task.
        await _process_update_durably(session_manager, webhook_manager, config, update)
        return {"ok": True}

    return router
