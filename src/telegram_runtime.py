"""Safe Telegram Bot API setup and the local long-polling transport.

The Bot API embeds the credential in the request path.  Every request in this
module therefore installs the same log redaction filter used by message
delivery and raises only credential-free errors.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import stat
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from src.settings import is_setting_overridden, load_settings
from src.constants import DATA_DIR
from src.telegram_bot import _install_telegram_log_filter, load_telegram_config
from src.telegram_delivery import (
    POLLING_LEASE,
    TelegramPollingLease,
    TelegramPollingLeaseLost,
    TelegramRuntimeAuthority,
    telegram_runtime_authority,
)

logger = logging.getLogger(__name__)

TELEGRAM_TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")
TELEGRAM_TRANSPORTS = frozenset({"polling", "webhook"})
_DISABLED_VALUES = frozenset({"0", "false", "no", "off", ""})


class _TelegramPollingProcessLease:
    """Non-blocking, crash-safe ownership for one local polling process.

    ``TelegramPollingService.start`` prevents duplicate coroutines only inside
    one interpreter.  Uvicorn can also be launched with multiple workers (or a
    second Restia process can briefly overlap during restart), and Telegram's
    ``getUpdates`` API permits only one active poller per bot.  An OS file lock
    scopes ownership to the shared Restia data directory and is released by
    the kernel if the owning process exits.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    @property
    def owned(self) -> bool:
        return self._fd is not None

    @staticmethod
    def _is_busy(exc: OSError) -> bool:
        return exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}

    def try_acquire(self) -> bool:
        if self._fd is not None:
            return True

        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_BINARY", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("Telegram polling lock is not a regular file")
            try:
                os.fchmod(fd, 0o600)
            except (AttributeError, OSError):
                # Windows may not expose meaningful POSIX mode bits. The file
                # contains no credential or message data, so locking remains
                # safe even when chmod is unavailable.
                pass

            if os.name == "nt":
                import msvcrt

                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                    os.fsync(fd)
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    if self._is_busy(exc):
                        os.close(fd)
                        return False
                    raise
            else:
                import fcntl

                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if self._is_busy(exc):
                        os.close(fd)
                        return False
                    raise
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

        self._fd = fd
        return True

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            logger.warning("Telegram polling ownership unlock failed", exc_info=True)
        finally:
            os.close(fd)


def inprocess_telegram_polling_enabled() -> bool:
    """Return whether this process owns Telegram long polling.

    This gate is intentionally independent from the scheduled-task runner.
    ``RESTIA_INPROCESS_TASKS=0`` can disable outbound schedules without
    disabling inbound Telegram chat.  The legacy variable remains supported
    for existing installations.
    """

    value = os.getenv("RESTIA_INPROCESS_TELEGRAM")
    if value in (None, ""):
        value = os.getenv("ODYSSEUS_INPROCESS_TELEGRAM")
    if value is None:
        value = "1"
    return str(value).strip().lower() not in _DISABLED_VALUES


class TelegramAPIError(RuntimeError):
    """Credential-free Telegram API failure."""

    def __init__(self, message: str, *, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class TelegramWebhookConflict(TelegramAPIError):
    """The bot is already attached to a webhook Restia does not own."""


def telegram_runtime_mode(settings: dict[str, Any] | None = None) -> str:
    """Resolve the transport while preserving existing webhook installs.

    Polling is the default for new/private instances.  Before this setting
    existed, the setup script stored a public URL and webhook secret; infer
    webhook mode for those installations until they explicitly choose a mode.
    """
    env_mode = str(os.getenv("TELEGRAM_RUNTIME_MODE") or "").strip().lower()
    if env_mode in TELEGRAM_TRANSPORTS:
        return env_mode
    values = settings or load_settings()
    configured = str(values.get("telegram_runtime_mode") or "").strip().lower()
    if is_setting_overridden("telegram_runtime_mode") and configured in TELEGRAM_TRANSPORTS:
        return configured
    if (
        values.get("telegram_enabled")
        and values.get("telegram_bot_token")
        and values.get("telegram_webhook_secret")
        and str(values.get("app_public_url") or "").strip().startswith("https://")
    ):
        return "webhook"
    return configured if configured in TELEGRAM_TRANSPORTS else "polling"


async def telegram_api_call(
    bot_token: str,
    method: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Call one Bot API method without leaking the path credential."""
    token = str(bot_token or "").strip()
    api_method = str(method or "").strip()
    if not token:
        raise TelegramAPIError("Telegram bot token is not configured")
    if not api_method or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]+", api_method):
        raise TelegramAPIError("Invalid Telegram API method")

    url = f"https://api.telegram.org/bot{token}/{api_method}"
    token_filter, filter_targets = _install_telegram_log_filter(token)
    failure: str | None = None
    error_code: int | None = None
    body: dict[str, Any] = {}
    response = None
    client = None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload or {})
            status = int(getattr(response, "status_code", 0) or 0)
            try:
                parsed = response.json()
                body = parsed if isinstance(parsed, dict) else {}
            except Exception:
                body = {}
            raw_code = body.get("error_code")
            try:
                error_code = int(raw_code) if raw_code is not None else None
            except (TypeError, ValueError):
                error_code = None
            if status < 200 or status >= 300 or body.get("ok") is not True:
                label = error_code or status or "unknown"
                failure = f"Telegram API {api_method} failed (code {label})"
    except httpx.HTTPError as exc:
        failure = f"Telegram API {api_method} failed ({exc.__class__.__name__})"
    except Exception:
        failure = f"Telegram API {api_method} failed"
    finally:
        try:
            for target in filter_targets:
                target.removeFilter(token_filter)
        finally:
            token_filter.clear()

    if failure:
        # Clear credential-bearing locals before raising; error reporters can
        # retain traceback frames even when the exception message is safe.
        token = ""
        bot_token = ""
        url = ""
        response = None
        client = None
        filter_targets = []
        raise TelegramAPIError(failure, error_code=error_code) from None
    return body


async def inspect_telegram_bot(bot_token: str) -> dict[str, Any]:
    """Validate a token and return only display-safe bot identity fields."""
    data = await telegram_api_call(bot_token, "getMe")
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    if result.get("is_bot") is False or result.get("id") is None:
        raise TelegramAPIError("Telegram getMe returned an invalid bot identity")
    return {
        "id": str(result.get("id")),
        "username": str(result.get("username") or "")[:64],
        "first_name": str(result.get("first_name") or "")[:128],
    }


async def inspect_telegram_webhook(bot_token: str) -> dict[str, Any]:
    data = await telegram_api_call(bot_token, "getWebhookInfo")
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    return {
        "url": str(result.get("url") or ""),
        "pending_update_count": max(0, int(result.get("pending_update_count") or 0)),
        "last_error_present": bool(result.get("last_error_date")),
    }


async def configure_telegram_transport(
    *,
    bot_token: str,
    mode: str,
    bot_identity: dict[str, Any],
    public_url: str = "",
    webhook_secret: str = "",
    owned_webhook_url: str = "",
    owned_bot_id: str = "",
    replace_foreign_webhook: bool = False,
) -> dict[str, Any]:
    """Reconcile polling/webhook mode without stealing a foreign webhook."""
    selected_mode = str(mode or "").strip().lower()
    if selected_mode not in TELEGRAM_TRANSPORTS:
        raise TelegramAPIError("Telegram transport must be polling or webhook")

    info = await inspect_telegram_webhook(bot_token)
    current_url = info["url"]
    current_bot_id = str(bot_identity.get("id") or "")
    owned = bool(
        current_url
        and owned_webhook_url
        and current_url == owned_webhook_url
        and current_bot_id
        and current_bot_id == str(owned_bot_id or "")
    )

    expected_url = ""
    if selected_mode == "webhook":
        base = str(public_url or "").strip().rstrip("/")
        if not base.startswith("https://"):
            raise TelegramAPIError("Webhook mode requires this instance's public HTTPS URL")
        if not webhook_secret:
            raise TelegramAPIError("Webhook mode requires a webhook secret")
        expected_url = f"{base}/api/telegram/webhook"

    conflicts = bool(current_url and not owned and current_url != expected_url)
    # Even an exact URL is not considered ours when the persisted bot-id/url
    # ownership record is absent.  An explicit takeover prevents silently
    # replacing another Restia instance that happens to use the same hostname.
    if current_url and not owned and current_url == expected_url:
        conflicts = True
    if conflicts and not replace_foreign_webhook:
        raise TelegramWebhookConflict(
            "This bot already has a webhook owned by another service. "
            "Choose Replace existing webhook only if this bot belongs to this Restia instance."
        )

    if selected_mode == "polling":
        if current_url:
            await telegram_api_call(
                bot_token,
                "deleteWebhook",
                payload={"drop_pending_updates": False},
            )
    else:
        await telegram_api_call(
            bot_token,
            "setWebhook",
            payload={
                "url": expected_url,
                "secret_token": webhook_secret,
                "allowed_updates": ["message", "edited_message"],
                "drop_pending_updates": False,
            },
        )

    return {
        "mode": selected_mode,
        "webhook_url": expected_url,
        "pending_update_count": info["pending_update_count"],
    }


class TelegramPollingService:
    """One long-polling worker for the one bot configured on this instance."""

    MAX_UPDATE_ATTEMPTS = 3
    OWNERSHIP_RETRY_SECONDS = 3.0
    DATABASE_LEASE = POLLING_LEASE
    DATABASE_HEARTBEAT_SECONDS = 25.0

    def __init__(
        self,
        *,
        process_lock_path: Path | None = None,
        runtime_authority: TelegramRuntimeAuthority | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._update_handler: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._wake = asyncio.Event()
        self._runner_task: asyncio.Task | None = None
        self._process_lease = _TelegramPollingProcessLease(
            process_lock_path or (Path(DATA_DIR) / "telegram_polling.lock")
        )
        self._offset: int | None = None
        self._token_fingerprint = ""
        self._runtime_authority = runtime_authority or telegram_runtime_authority
        self._worker_id = worker_id or ("poller:" + uuid.uuid4().hex)
        self._database_lease: TelegramPollingLease | None = None
        self._database_lease_lost = False
        self._database_heartbeat_task: asyncio.Task | None = None
        self._running = False
        self._standby = False
        self._last_error = ""
        self._last_update_at: float | None = None

    def configure(self, update_handler: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        self._update_handler = update_handler

    def wake(self) -> None:
        self._wake.set()

    def status(self) -> dict[str, Any]:
        if self._token_fingerprint:
            try:
                dead_letter_count = self._runtime_authority.dead_letter_count(
                    self._token_fingerprint
                )
            except Exception:
                dead_letter_count = 0
        else:
            dead_letter_count = 0
        return {
            "poller_running": self._running,
            "poller_standby": self._standby,
            "last_error": self._last_error,
            "last_update_at": self._last_update_at,
            "dead_letter_count": dead_letter_count,
            "database_leased": self._database_lease is not None,
        }

    def _load_durable_offset(self, fingerprint: str) -> int | None:
        return self._runtime_authority.polling_cursor(fingerprint)

    def _advance_offset(self, offset: int) -> None:
        """Persist before using an offset to survive restart replay."""
        if self._database_lease_lost or self._database_lease is None:
            raise TelegramPollingLeaseLost("Telegram polling lease was lost")
        self._offset = self._runtime_authority.advance_polling_cursor(
            self._database_lease, int(offset)
        )

    async def _process_updates(self, updates: list[Any]) -> None:
        """Handle updates in order and advance offsets only after resolution."""
        if self._update_handler is None:
            return
        if self._database_lease_lost or self._database_lease is None:
            raise TelegramPollingLeaseLost(
                "Telegram polling updates require a database lease"
            )
        for update in updates:
            if not isinstance(update, dict):
                continue
            update_id = update.get("update_id")
            try:
                await self._update_handler(update)
            except Exception as exc:
                from src.telegram_inbound_ledger import (
                    TelegramInboundInFlight,
                    TelegramReplyPending,
                )

                if isinstance(exc, (TelegramInboundInFlight, TelegramReplyPending)):
                    # A crash/concurrent request left a live durable lease. It
                    # is not poison content and must not consume DLQ attempts.
                    raise
                if not isinstance(update_id, int):
                    raise
                if self._database_lease_lost:
                    raise TelegramPollingLeaseLost(
                        "Telegram polling lease was lost"
                    ) from exc
                resolution = self._runtime_authority.record_handler_failure(
                    self._database_lease,
                    update_id=update_id,
                    error_type=exc.__class__.__name__,
                    max_attempts=self.MAX_UPDATE_ATTEMPTS,
                )
                if not resolution.resolved:
                    raise
                self._offset = resolution.next_offset
                logger.error(
                    "Telegram update %s dead-lettered after %s handler failures",
                    update_id,
                    resolution.attempts,
                )
                continue
            if isinstance(update_id, int):
                self._advance_offset(update_id + 1)
            self._last_update_at = time.time()

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def _database_heartbeat(self, claim: TelegramPollingLease) -> None:
        """Renew one fenced lease while network/LLM processing is in flight."""

        try:
            while self._database_lease is not None:
                await asyncio.sleep(self.DATABASE_HEARTBEAT_SECONDS)
                current = self._database_lease
                if current is None or current.fencing_token != claim.fencing_token:
                    return
                renewed = self._runtime_authority.renew_polling_lease(
                    current, lease=self.DATABASE_LEASE
                )
                if renewed is None:
                    self._database_lease_lost = True
                    self._wake.set()
                    return
                self._database_lease = renewed
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._database_lease_lost = True
            self._last_error = "Telegram polling ownership unavailable"
            logger.error(
                "Telegram polling database lease renewal failed (%s)",
                exc.__class__.__name__,
            )
            self._wake.set()

    async def _drop_database_lease(self, *, release: bool) -> None:
        heartbeat = self._database_heartbeat_task
        self._database_heartbeat_task = None
        if heartbeat is not None and not heartbeat.done():
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
        claim = self._database_lease
        self._database_lease = None
        self._database_lease_lost = False
        if release and claim is not None:
            try:
                self._runtime_authority.release_polling_lease(claim)
            except Exception:
                logger.warning(
                    "Telegram polling database lease release failed",
                    exc_info=True,
                )

    def start(self) -> asyncio.Task:
        """Start the sole in-process poller and return its owned task.

        FastAPI lifespan hooks can be entered more than once in test runners
        and embedded deployments.  Reusing the active task prevents two
        ``getUpdates`` loops from racing for the same bot in one process.
        """

        task = self._runner_task
        if task is not None and not task.done():
            return task
        # asyncio primitives bind lazily to an event loop. Recreate the wake
        # event for a clean restart in another lifespan/event loop.
        self._wake = asyncio.Event()
        task = asyncio.create_task(self.run(), name="restia-telegram-poller")
        self._runner_task = task
        return task

    async def stop(self) -> None:
        """Cancel and await the owned poller task; safe to call repeatedly."""

        task = self._runner_task
        if task is None:
            self._running = False
            self._standby = False
            return
        if task is asyncio.current_task():
            task.cancel()
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if self._runner_task is task:
                self._runner_task = None
            self._running = False
            self._standby = False

    async def run(self) -> None:
        current = asyncio.current_task()
        registered = self._runner_task
        if registered is not None and registered is not current and not registered.done():
            logger.warning("Duplicate Telegram polling runner ignored")
            return
        if registered is None or registered.done():
            # ``run`` remains usable as a compatibility entrypoint, while
            # still taking ownership and preventing another direct runner.
            self._wake = asyncio.Event()
            self._runner_task = current
        backoff = 1.0
        try:
            while True:
                if not self._process_lease.owned:
                    try:
                        owns_process_lease = self._process_lease.try_acquire()
                    except Exception as exc:
                        self._running = False
                        self._standby = False
                        self._last_error = "Telegram polling ownership unavailable"
                        logger.error(
                            "Telegram polling ownership unavailable (%s)",
                            exc.__class__.__name__,
                        )
                        await self._wait(self.OWNERSHIP_RETRY_SECONDS)
                        continue
                    if not owns_process_lease:
                        # Another local worker is healthy enough to hold the
                        # kernel lease. Stay ready to take over after a crash or
                        # rolling restart without racing its getUpdates calls.
                        self._running = False
                        self._standby = True
                        self._last_error = ""
                        await self._wait(self.OWNERSHIP_RETRY_SECONDS)
                        continue
                    self._running = True
                    self._standby = False
                    self._last_error = ""
                    logger.info("Telegram polling ownership acquired")

                settings = load_settings()
                config = load_telegram_config()
                if (
                    not config.enabled
                    or not config.bot_token
                    or telegram_runtime_mode(settings) != "polling"
                    or self._update_handler is None
                ):
                    if self._database_lease is not None:
                        await self._drop_database_lease(release=True)
                    self._token_fingerprint = ""
                    self._offset = None
                    self._last_error = ""
                    await self._wait(3.0)
                    continue

                from src.telegram_identity import telegram_bot_fingerprint

                fingerprint = (
                    str(getattr(config, "bot_fingerprint", "") or "")
                    or telegram_bot_fingerprint(bot_token=config.bot_token)
                )
                if fingerprint != self._token_fingerprint:
                    if self._database_lease is not None:
                        await self._drop_database_lease(release=True)
                    self._token_fingerprint = fingerprint
                    self._offset = None
                if self._database_lease_lost:
                    await self._drop_database_lease(release=False)
                if self._database_lease is None:
                    try:
                        self._runtime_authority.adopt_legacy_sidecars(
                            bot_fingerprint=fingerprint,
                            bot_token=config.bot_token,
                            data_dir=DATA_DIR,
                        )
                        claim = self._runtime_authority.acquire_polling_lease(
                            bot_fingerprint=fingerprint,
                            worker_id=self._worker_id,
                            lease=self.DATABASE_LEASE,
                        )
                    except Exception as exc:
                        self._running = False
                        self._standby = False
                        self._last_error = "Telegram polling ownership unavailable"
                        logger.error(
                            "Telegram polling database ownership unavailable (%s)",
                            exc.__class__.__name__,
                        )
                        await self._wait(self.OWNERSHIP_RETRY_SECONDS)
                        continue
                    if claim is None:
                        self._running = False
                        self._standby = True
                        self._last_error = ""
                        await self._wait(self.OWNERSHIP_RETRY_SECONDS)
                        continue
                    self._database_lease = claim
                    self._database_lease_lost = False
                    self._offset = self._load_durable_offset(fingerprint)
                    self._database_heartbeat_task = asyncio.create_task(
                        self._database_heartbeat(claim),
                        name="restia-telegram-poller-lease",
                    )
                    self._running = True
                    self._standby = False
                    logger.info("Telegram database polling lease acquired")
                payload: dict[str, Any] = {
                    "timeout": 25,
                    "allowed_updates": ["message", "edited_message"],
                }
                if self._offset is not None:
                    payload["offset"] = self._offset
                try:
                    data = await telegram_api_call(
                        config.bot_token,
                        "getUpdates",
                        payload=payload,
                        timeout=35.0,
                    )
                    updates = data.get("result") if isinstance(data.get("result"), list) else []
                    await self._process_updates(updates)
                    self._last_error = ""
                    backoff = 1.0
                except asyncio.CancelledError:
                    raise
                except TelegramPollingLeaseLost:
                    self._last_error = ""
                    await self._drop_database_lease(release=False)
                    self._running = False
                    self._standby = True
                    await self._wait(self.OWNERSHIP_RETRY_SECONDS)
                except TelegramAPIError as exc:
                    self._last_error = str(exc)
                    logger.warning("Telegram polling paused: %s", exc)
                    await self._wait(backoff)
                    backoff = min(backoff * 2, 30.0)
                except Exception as exc:
                    self._last_error = "Telegram polling failed"
                    logger.warning("Telegram polling failed (%s)", exc.__class__.__name__)
                    await self._wait(backoff)
                    backoff = min(backoff * 2, 30.0)
        finally:
            await self._drop_database_lease(release=True)
            self._running = False
            self._standby = False
            self._token_fingerprint = ""
            self._offset = None
            self._process_lease.release()
            if self._runner_task is current:
                self._runner_task = None


telegram_polling_service = TelegramPollingService()
