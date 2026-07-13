"""Bounded incoming-call alerts and short-lived offer replay.

The WebRTC routes remain responsible for authenticating and validating call
signals.  This module only retains an already-sanitized incoming offer long
enough for a browser opened from a Telegram alert to receive it, and delivers
that alert to Telegram chats explicitly linked to the callee profile.

Nothing here is durable: SDP can contain private network information, so
pending offers live in memory only and expire with the normal 45-second ring
window.  Call identifiers and SDP are never included in Telegram messages or
links.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional
from urllib.parse import quote, urlsplit
from uuid import UUID

from src.settings import load_settings
from src.telegram_bot import (
    TelegramConfig,
    load_telegram_config,
    send_telegram_message,
    telegram_chat_ids_for_owner,
)

logger = logging.getLogger(__name__)

CALL_ALERT_TTL_S = 45.0
# Send notifications more frequently to simulate continuous ringing
CALL_ALERT_OFFSETS_S = (0.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0)
MAX_PENDING_CALLS = 256
MAX_PENDING_CALLS_PER_OWNER = 4
MAX_TELEGRAM_CHATS_PER_PROFILE = 4
MAX_OWNER_LEN = 96
MAX_PEER_LEN = 96
MAX_SDP_BYTES = 15_360
ALLOWED_TRANSPORTS = frozenset({"local", "home"})

_TelegramSender = Callable[..., Awaitable[Any]]
_TelegramConfigLoader = Callable[[], TelegramConfig]
_TelegramChatLookup = Callable[[TelegramConfig, str], list[str]]
_SettingsLoader = Callable[[], Mapping[str, Any]]
_Sleep = Callable[[float], Awaitable[None]]
_Clock = Callable[[], float]


def _normalized_profile(value: Any) -> str:
    profile = str(value or "").strip().lower()
    if not profile or len(profile) > MAX_OWNER_LEN or any(ord(ch) < 32 for ch in profile):
        raise ValueError("Invalid call alert owner")
    return profile


def _normalized_peer(value: Any) -> str:
    peer = str(value or "").strip()
    if not peer or len(peer) > MAX_PEER_LEN or any(ord(ch) < 32 for ch in peer):
        raise ValueError("Invalid call alert peer")
    return peer


def _canonical_call_id(value: Any) -> str:
    raw = str(value or "")
    try:
        parsed = UUID(raw)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Invalid call alert id") from exc
    canonical = str(parsed)
    if raw != canonical or parsed.version != 4:
        raise ValueError("Invalid call alert id")
    return canonical


def _normalized_transport(value: Any) -> str:
    transport = str(value or "").strip().lower()
    if transport not in ALLOWED_TRANSPORTS:
        raise ValueError("Invalid call alert transport")
    return transport


def _clean_offer_data(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"sdp", "video"}:
        raise ValueError("Invalid call alert offer")
    sdp = value.get("sdp")
    video = value.get("video")
    if (
        not isinstance(sdp, str)
        or not sdp.startswith("v=0")
        or len(sdp.encode("utf-8")) > MAX_SDP_BYTES
        or not isinstance(video, bool)
    ):
        raise ValueError("Invalid call alert offer")
    return {"sdp": sdp, "video": video}


def _https_public_origin(settings: Mapping[str, Any]) -> str:
    """Return a canonical configured HTTPS origin, or an empty string.

    Never derive notification links from Host/X-Forwarded-Host: incoming Home
    Link requests are remote-controlled, and a forged host would become a
    phishing link delivered by the trusted Restia Telegram bot.
    """
    raw = str((settings or {}).get("app_public_url") or "").strip()
    if not raw or len(raw) > 2048 or any(ord(ch) < 33 for ch in raw):
        return "https://app.restia.dev"
    try:
        parsed = urlsplit(raw)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            return ""
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return ""
    if not host or any(ch in host for ch in ("/", "\\", "@")):
        return ""
    rendered_host = f"[{host}]" if ":" in host else host
    if port is not None and port != 443:
        rendered_host = f"{rendered_host}:{port}"
    return f"https://{rendered_host}"


def build_restia_call_link(peer: str, settings: Optional[Mapping[str, Any]] = None) -> str:
    """Build a same-installation Messages deep-link from trusted settings.

    The peer label is kept in the URL fragment, which is not sent in HTTP
    requests or reverse-proxy logs.  The call id is deliberately absent.
    """
    clean_peer = _normalized_peer(peer)
    configured = load_settings() if settings is None else settings
    origin = _https_public_origin(configured)
    if not origin:
        return ""
    return f"{origin}/#messages={quote(clean_peer, safe='')}"


@dataclass
class _PendingIncomingCall:
    owner: str
    peer: str
    call_id: str
    transport: str
    offer_data: dict[str, Any]
    created_at: float
    expires_at: float
    task: Optional[asyncio.Task] = None

    @property
    def key(self) -> tuple[str, str, str]:
        return self.owner, self.transport, self.call_id

    def event(self) -> dict[str, Any]:
        return {
            "from": self.peer,
            "call_id": self.call_id,
            "kind": "offer",
            "data": dict(self.offer_data),
        }


class IncomingCallNotifications:
    """Own pending incoming offers and their bounded Telegram alert tasks."""

    def __init__(
        self,
        *,
        telegram_config_loader: _TelegramConfigLoader = load_telegram_config,
        telegram_chat_lookup: _TelegramChatLookup = telegram_chat_ids_for_owner,
        telegram_sender: _TelegramSender = send_telegram_message,
        settings_loader: _SettingsLoader = load_settings,
        sleep: _Sleep = asyncio.sleep,
        clock: _Clock = time.monotonic,
        alert_offsets_s: tuple[float, ...] = CALL_ALERT_OFFSETS_S,
        ttl_s: float = CALL_ALERT_TTL_S,
        max_pending: int = MAX_PENDING_CALLS,
        max_pending_per_owner: int = MAX_PENDING_CALLS_PER_OWNER,
        max_chats_per_profile: int = MAX_TELEGRAM_CHATS_PER_PROFILE,
        delivery_timeout_s: float = 10.0,
    ) -> None:
        offsets = tuple(float(value) for value in alert_offsets_s)
        ttl = float(ttl_s)
        if (
            not offsets
            or len(offsets) > len(CALL_ALERT_OFFSETS_S)
            or offsets[0] != 0.0
            or tuple(sorted(set(offsets))) != offsets
            or ttl <= 0
            or any(value < 0 or value >= ttl for value in offsets)
        ):
            raise ValueError("Invalid call alert schedule")
        self._telegram_config_loader = telegram_config_loader
        self._telegram_chat_lookup = telegram_chat_lookup
        self._telegram_sender = telegram_sender
        self._settings_loader = settings_loader
        self._sleep = sleep
        self._clock = clock
        self._alert_offsets = offsets
        self._ttl = ttl
        self._max_pending = max(1, min(int(max_pending), MAX_PENDING_CALLS))
        self._max_pending_per_owner = max(
            1, min(int(max_pending_per_owner), MAX_PENDING_CALLS_PER_OWNER)
        )
        self._max_chats = max(
            1, min(int(max_chats_per_profile), MAX_TELEGRAM_CHATS_PER_PROFILE)
        )
        self._delivery_timeout = max(0.1, min(float(delivery_timeout_s), 15.0))
        self._lock = threading.RLock()
        self._pending: dict[tuple[str, str, str], _PendingIncomingCall] = {}

    def _remove_locked(self, key: tuple[str, str, str], *, cancel: bool) -> bool:
        pending = self._pending.pop(key, None)
        if pending is None:
            return False
        task = pending.task
        if cancel and task is not None and not task.done():
            task.cancel()
        return True

    def _prune_locked(self, now: float) -> None:
        for key, pending in list(self._pending.items()):
            if pending.expires_at <= now:
                self._remove_locked(key, cancel=True)

    def begin(
        self,
        *,
        owner: str,
        peer: str,
        call_id: str,
        transport: str,
        offer_data: Mapping[str, Any],
    ) -> bool:
        """Register one incoming offer and start its alert lifecycle.

        Returns ``False`` for an exact duplicate or when a strict capacity bound
        refuses a new record.  Signaling callers should continue fail-safe call
        handling even if notifications are unavailable.
        """
        clean_owner = _normalized_profile(owner)
        clean_peer = _normalized_peer(peer)
        clean_id = _canonical_call_id(call_id)
        clean_transport = _normalized_transport(transport)
        clean_offer = _clean_offer_data(offer_data)
        now = self._clock()
        key = (clean_owner, clean_transport, clean_id)

        with self._lock:
            self._prune_locked(now)
            if key in self._pending:
                return False
            if len(self._pending) >= self._max_pending:
                logger.warning("Incoming call notification capacity reached")
                return False
            owner_count = sum(row.owner == clean_owner for row in self._pending.values())
            if owner_count >= self._max_pending_per_owner:
                logger.warning("Incoming call notification profile capacity reached")
                return False
            pending = _PendingIncomingCall(
                owner=clean_owner,
                peer=clean_peer,
                call_id=clean_id,
                transport=clean_transport,
                offer_data=clean_offer,
                created_at=now,
                expires_at=now + self._ttl,
            )
            self._pending[key] = pending
            try:
                task = asyncio.create_task(self._run_alerts(key))
            except Exception:
                self._pending.pop(key, None)
                raise
            pending.task = task
            task.add_done_callback(lambda completed, task_key=key: self._task_done(task_key, completed))
        return True

    def _task_done(self, key: tuple[str, str, str], task: asyncio.Task) -> None:
        failed = False
        if not task.cancelled():
            try:
                failed = task.exception() is not None
            except (asyncio.CancelledError, asyncio.InvalidStateError):
                failed = False
        with self._lock:
            current = self._pending.get(key)
            if current is not None and current.task is task:
                self._pending.pop(key, None)
        if failed:
            # Exception strings can retain third-party request URLs.  The type
            # and message are intentionally omitted from logs.
            logger.warning("Incoming call notification task failed")

    async def _send_one(self, token: str, chat_id: str, text: str) -> None:
        try:
            await asyncio.wait_for(
                self._telegram_sender(token, chat_id, text, rich_text=False),
                timeout=self._delivery_timeout,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Neither bot credentials nor chat identifiers belong in logs.
            logger.warning("Incoming call Telegram delivery failed")

    async def _deliver(self, pending: _PendingIncomingCall, attempt: int) -> None:
        try:
            config = self._telegram_config_loader()
            if not config.enabled or not config.bot_token:
                return
            chat_ids = self._telegram_chat_lookup(config, pending.owner)[: self._max_chats]
            if not chat_ids:
                return
            link = build_restia_call_link(pending.peer, self._settings_loader())
            kind = "video" if pending.offer_data["video"] else "voice"
            lines = [
                f"Incoming Restia {kind} call",
                f"From: {pending.peer}",
                f"Alert {attempt} of {len(self._alert_offsets)}",
            ]
            if link:
                lines.append(f"Open Restia: {link}")
            else:
                lines.append("Open Restia in your browser to answer.")
            text = "\n".join(lines)
            await asyncio.gather(*(
                self._send_one(config.bot_token, chat_id, text)
                for chat_id in chat_ids
            ))
        except asyncio.CancelledError:
            raise
        except Exception:
            # Configuration/parsing failures must never break call signaling or
            # expose a credential-bearing exception string.
            logger.warning("Incoming call Telegram notification unavailable")

    async def _run_alerts(self, key: tuple[str, str, str]) -> None:
        for attempt, offset in enumerate(self._alert_offsets, start=1):
            with self._lock:
                pending = self._pending.get(key)
                if pending is None:
                    return
                wake_at = pending.created_at + offset
            delay = max(0.0, wake_at - self._clock())
            if delay:
                await self._sleep(delay)
            with self._lock:
                pending = self._pending.get(key)
                if pending is None or pending.expires_at <= self._clock():
                    return
            await self._deliver(pending, attempt)

        # Keep the offer replayable until the same deadline as the browser's
        # ringing state, even after the last Telegram repeat has been sent.
        with self._lock:
            pending = self._pending.get(key)
            if pending is None:
                return
            delay = max(0.0, pending.expires_at - self._clock())
        if delay:
            await self._sleep(delay)
        with self._lock:
            current = self._pending.get(key)
            if current is pending:
                self._pending.pop(key, None)

    def stop(
        self,
        *,
        owner: str,
        transport: str,
        call_id: str,
        peer: Optional[str] = None,
    ) -> bool:
        """Cancel one alert lifecycle after a terminal call transition."""
        key = (
            _normalized_profile(owner),
            _normalized_transport(transport),
            _canonical_call_id(call_id),
        )
        clean_peer = _normalized_peer(peer) if peer is not None else None
        with self._lock:
            pending = self._pending.get(key)
            if pending is None or (clean_peer is not None and pending.peer != clean_peer):
                return False
            return self._remove_locked(key, cancel=True)

    def stop_owner_transport(self, *, owner: str, transport: str) -> int:
        """Cancel every pending call for a disconnected/revoked transport."""
        clean_owner = _normalized_profile(owner)
        clean_transport = _normalized_transport(transport)
        removed = 0
        with self._lock:
            for key, pending in list(self._pending.items()):
                if pending.owner == clean_owner and pending.transport == clean_transport:
                    removed += int(self._remove_locked(key, cancel=True))
        return removed

    def pending_snapshot(self, *, owner: str, transport: str) -> list[dict[str, Any]]:
        """Return isolated copies of active offers eligible for SSE replay."""
        clean_owner = _normalized_profile(owner)
        clean_transport = _normalized_transport(transport)
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            rows = [
                pending for pending in self._pending.values()
                if pending.owner == clean_owner and pending.transport == clean_transport
            ]
            rows.sort(key=lambda item: (item.created_at, item.call_id))
            return [row.event() for row in rows]

    async def shutdown(self) -> None:
        """Cancel and await every retained alert task."""
        with self._lock:
            tasks = [row.task for row in self._pending.values() if row.task is not None]
            self._pending.clear()
            for task in tasks:
                if not task.done():
                    task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


# Process-local singleton used by the call and Home Link routes.  It creates no
# task until an authenticated, validated incoming offer is registered.
incoming_call_notifications = IncomingCallNotifications()
