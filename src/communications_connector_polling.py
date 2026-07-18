"""Independent, owner-scoped read-only Slack and Twilio polling.

This module deliberately contains no external write operation. Provider data
is fetched with GET-only preset grants and projected through the canonical
Life ingestion boundary. Durable encrypted cursors make retries idempotent and
keep the worker independent from user Tasks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

import httpx

from core.database import Account, SessionLocal, utcnow_naive
from src.communications_polling_models import CommunicationPollState
from src.integration_permissions import (
    IntegrationPermissionError,
    integration_request_allowed,
)
from src.life_ingestion import ingest_readonly_communication_message
from src.profile_configuration_models import ProfileConfiguration
from src.profile_configuration_service import serialize_configuration


log = logging.getLogger(__name__)

PROVIDER_ORIGINS = {
    "slack": "https://slack.com",
    "twilio": "https://api.twilio.com",
}
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TARGETS = 500
MAX_SLACK_CHANNELS = 30
MAX_SLACK_CHANNEL_PAGES = 2
MAX_MESSAGES_PER_CHANNEL = 50
MAX_MESSAGES_PER_POLL = 500
DEFAULT_POLL_SECONDS = 60.0
DEFAULT_HTTP_TIMEOUT_SECONDS = 30.0
_TWILIO_SID_RE = re.compile(r"^AC[a-fA-F0-9]{32}$")


class ReadonlyCommunicationPollingError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = str(code)[:64]
        self.detail = str(detail)[:300]


@dataclass(frozen=True, slots=True)
class PollTarget:
    configuration_id: str
    owner_id: str
    owner_username: str
    provider: str
    integration: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PollResult:
    provider: str
    imported_count: int
    cursor: dict[str, Any]


def _identity_text(value: object, *, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise ReadonlyCommunicationPollingError(
            "provider_response_invalid", f"Provider {field} exceeds its size limit"
        )
    return text


def _content_text(value: object, *, limit: int = 20_000) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    marker = "\n[provider content truncated]"
    return text[: limit - len(marker)] + marker


def _truthy(value: object, *, default: bool = True) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def inprocess_communications_polling_enabled(
    environ: Mapping[str, str] | None = None,
) -> bool:
    env = os.environ if environ is None else environ
    return _truthy(
        env.get(
            "RESTIA_INPROCESS_COMMUNICATION_POLLING",
            env.get("ODYSSEUS_INPROCESS_COMMUNICATION_POLLING"),
        ),
        default=True,
    )


def _bounded_number(
    env: Mapping[str, str], name: str, default: float, minimum: float, maximum: float,
) -> float:
    try:
        value = float(str(env.get(name, default)).strip())
    except (TypeError, ValueError) as exc:
        raise ReadonlyCommunicationPollingError(
            "invalid_polling_configuration", f"{name} must be numeric"
        ) from exc
    if not minimum <= value <= maximum:
        raise ReadonlyCommunicationPollingError(
            "invalid_polling_configuration",
            f"{name} must be between {minimum:g} and {maximum:g}",
        )
    return value


def communications_poll_interval(environ: Mapping[str, str] | None = None) -> float:
    env = os.environ if environ is None else environ
    return _bounded_number(
        env, "RESTIA_COMMUNICATION_POLL_SECONDS", DEFAULT_POLL_SECONDS, 15, 3_600,
    )


def communications_http_timeout(environ: Mapping[str, str] | None = None) -> float:
    env = os.environ if environ is None else environ
    return _bounded_number(
        env,
        "RESTIA_COMMUNICATION_HTTP_TIMEOUT_SECONDS",
        DEFAULT_HTTP_TIMEOUT_SECONDS,
        5,
        120,
    )


def _provider(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in PROVIDER_ORIGINS else None


def _poll_targets(session_factory=SessionLocal) -> list[PollTarget]:
    db = session_factory()
    try:
        rows = db.query(ProfileConfiguration, Account).join(
            Account, Account.id == ProfileConfiguration.owner_id,
        ).filter(
            ProfileConfiguration.namespace == "integration",
            ProfileConfiguration.state == "active",
        ).order_by(
            ProfileConfiguration.owner_id.asc(), ProfileConfiguration.key.asc(),
        ).limit(MAX_TARGETS + 1).all()
        if len(rows) > MAX_TARGETS:
            raise ReadonlyCommunicationPollingError(
                "poll_target_limit_exceeded",
                "Enabled provider integrations exceed the polling limit",
            )
        targets: list[PollTarget] = []
        for row, account in rows:
            try:
                integration = serialize_configuration(row)["value"]
            except Exception:
                log.error("Skipping malformed communications integration configuration")
                continue
            if not isinstance(integration, dict) or integration.get("enabled") is not True:
                continue
            provider = _provider(integration.get("preset"))
            if provider is None:
                continue
            targets.append(PollTarget(
                configuration_id=row.id,
                owner_id=account.id,
                owner_username=account.username,
                provider=provider,
                integration=dict(integration),
            ))
        return targets
    finally:
        db.close()


def _cursor_for_target(target: PollTarget, session_factory=SessionLocal) -> dict[str, Any]:
    db = session_factory()
    try:
        row = db.query(CommunicationPollState).filter(
            CommunicationPollState.configuration_id == target.configuration_id,
            CommunicationPollState.provider == target.provider,
            CommunicationPollState.owner_id == target.owner_id,
        ).first()
        return dict(row.cursor or {}) if row is not None else {}
    finally:
        db.close()


def _update_poll_state(
    target: PollTarget,
    *,
    session_factory,
    cursor: Mapping[str, Any] | None,
    imported_count: int,
    error_code: str | None,
) -> None:
    db = session_factory()
    try:
        row = db.query(CommunicationPollState).filter(
            CommunicationPollState.configuration_id == target.configuration_id,
            CommunicationPollState.provider == target.provider,
            CommunicationPollState.owner_id == target.owner_id,
        ).first()
        if row is None:
            row = CommunicationPollState(
                id=str(uuid.uuid4()),
                owner_id=target.owner_id,
                configuration_id=target.configuration_id,
                provider=target.provider,
                cursor=dict(cursor or {}),
                state="idle",
                last_attempt_at=None,
                last_success_at=None,
                error_code=None,
                consecutive_failures=0,
                imported_count=0,
                version=1,
            )
            db.add(row)
        now = utcnow_naive()
        row.last_attempt_at = now
        if error_code is None:
            row.cursor = dict(cursor or {})
            row.state = "healthy"
            row.last_success_at = now
            row.error_code = None
            row.consecutive_failures = 0
            row.imported_count = int(row.imported_count or 0) + int(imported_count)
        else:
            row.state = "error"
            row.error_code = str(error_code)[:64]
            row.consecutive_failures = int(row.consecutive_failures or 0) + 1
        row.version = int(row.version or 1) + (0 if row.created_at is None else 1)
        db.commit()
    finally:
        db.close()


def _assert_readonly_provider_contract(target: PollTarget, path: str) -> None:
    integration = target.integration
    if _provider(integration.get("preset")) != target.provider:
        raise ReadonlyCommunicationPollingError(
            "provider_contract_invalid", "Provider preset changed during polling"
        )
    origin = str(integration.get("base_url") or "").rstrip("/")
    if origin != PROVIDER_ORIGINS[target.provider]:
        raise ReadonlyCommunicationPollingError(
            "provider_origin_invalid", "Provider polling requires the canonical HTTPS origin"
        )
    try:
        permissions = integration_request_allowed(
            integration, method="GET", path=path, approved_external_action=False,
        )
    except IntegrationPermissionError as exc:
        raise ReadonlyCommunicationPollingError(
            "provider_permission_denied", "Provider does not grant the required read path"
        ) from exc
    if permissions["allowed_methods"] != ["GET"]:
        raise ReadonlyCommunicationPollingError(
            "provider_not_readonly", "Provider polling requires a GET-only permission grant"
        )


def _provider_auth(target: PollTarget) -> tuple[dict[str, str], httpx.Auth | None]:
    credential = str(target.integration.get("api_key") or "")
    if target.provider == "slack":
        if not credential or target.integration.get("auth_type") != "bearer":
            raise ReadonlyCommunicationPollingError(
                "provider_credential_invalid", "Slack bearer credential is not configured"
            )
        return {"Authorization": f"Bearer {credential}"}, None
    if target.integration.get("auth_type") != "basic" or ":" not in credential:
        raise ReadonlyCommunicationPollingError(
            "provider_credential_invalid", "Twilio read credential is not configured"
        )
    sid, token = credential.split(":", 1)
    if not _TWILIO_SID_RE.fullmatch(sid) or not token:
        raise ReadonlyCommunicationPollingError(
            "provider_credential_invalid", "Twilio credential format is invalid"
        )
    return {}, httpx.BasicAuth(sid, token)


async def _request_provider_json(
    client: httpx.AsyncClient,
    target: PollTarget,
    path: str,
    *,
    params: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    _assert_readonly_provider_contract(target, path)
    headers, auth = _provider_auth(target)
    url = PROVIDER_ORIGINS[target.provider] + path
    try:
        content = bytearray()
        async with client.stream(
            "GET", url, params=dict(params or {}), headers=headers, auth=auth,
        ) as response:
            status_code = response.status_code
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ReadonlyCommunicationPollingError(
                        "provider_response_too_large",
                        "Provider response exceeded the size limit",
                    )
    except httpx.TimeoutException as exc:
        raise ReadonlyCommunicationPollingError(
            "provider_timeout", "Provider read timed out"
        ) from exc
    except httpx.RequestError as exc:
        raise ReadonlyCommunicationPollingError(
            "provider_unavailable", "Provider read failed"
        ) from exc
    if status_code < 200 or status_code >= 300:
        raise ReadonlyCommunicationPollingError(
            "provider_http_error", f"Provider returned HTTP {status_code}"
        )
    try:
        payload = httpx.Response(200, content=bytes(content)).json()
    except (UnicodeDecodeError, ValueError) as exc:
        raise ReadonlyCommunicationPollingError(
            "provider_response_invalid", "Provider returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ReadonlyCommunicationPollingError(
            "provider_response_invalid", "Provider response must be an object"
        )
    if target.provider == "slack" and payload.get("ok") is not True:
        raise ReadonlyCommunicationPollingError(
            "slack_api_error", "Slack rejected the read request"
        )
    return payload


def _slack_time(value: object) -> datetime | None:
    try:
        seconds = float(str(value or ""))
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, timezone.utc) if seconds > 0 else None


async def _poll_slack(
    target: PollTarget,
    cursor: Mapping[str, Any],
    *,
    client: httpx.AsyncClient,
    session_factory,
) -> PollResult:
    auth = await _request_provider_json(client, target, "/api/auth.test")
    own_user = str(auth.get("user_id") or "")
    channels: list[dict[str, Any]] = []
    page_cursor = ""
    for _ in range(MAX_SLACK_CHANNEL_PAGES):
        params: dict[str, object] = {
            "exclude_archived": "true",
            "limit": 100,
            "types": "public_channel,private_channel,im,mpim",
        }
        if page_cursor:
            params["cursor"] = page_cursor
        payload = await _request_provider_json(
            client, target, "/api/conversations.list", params=params,
        )
        rows = payload.get("channels")
        if not isinstance(rows, list):
            raise ReadonlyCommunicationPollingError(
                "slack_response_invalid", "Slack channel list is malformed"
            )
        channels.extend(item for item in rows if isinstance(item, dict))
        page_cursor = str(
            (payload.get("response_metadata") or {}).get("next_cursor") or ""
        ).strip()
        if not page_cursor or len(channels) >= MAX_SLACK_CHANNELS:
            break
    channel_cursors = dict(cursor.get("channels") or {})
    imported = 0
    for channel in channels[:MAX_SLACK_CHANNELS]:
        channel_id = _identity_text(
            channel.get("id"), field="channel id", limit=255,
        )
        if not channel_id:
            continue
        params = {"channel": channel_id, "limit": MAX_MESSAGES_PER_CHANNEL}
        oldest = str(channel_cursors.get(channel_id) or "").strip()
        if oldest:
            params["oldest"] = oldest
            params["inclusive"] = "false"
        payload = await _request_provider_json(
            client, target, "/api/conversations.history", params=params,
        )
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise ReadonlyCommunicationPollingError(
                "slack_response_invalid", "Slack message history is malformed"
            )
        highest = oldest
        last_read = str(channel.get("last_read") or "")
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            message_time = _identity_text(
                message.get("ts"), field="message timestamp", limit=64,
            )
            if message_time and (not highest or float(message_time) > float(highest)):
                highest = message_time
            body = _content_text(message.get("text"))
            if not body:
                continue
            author = _identity_text(
                message.get("user")
                or (message.get("bot_profile") or {}).get("name")
                or "Slack",
                field="sender", limit=240,
            )
            outbound = bool(own_user and message.get("user") == own_user)
            unread = False if outbound else (
                not last_read or float(message_time or 0) > float(last_read or 0)
            )
            result = ingest_readonly_communication_message(
                owner=target.owner_username,
                channel="slack",
                connector_id=target.configuration_id,
                conversation_ref=channel_id,
                text=body,
                message_id=_identity_text(
                    message.get("client_msg_id") or message_time,
                    field="message id", limit=500,
                ),
                sender_name=author,
                observed_at=_slack_time(message_time),
                unread=unread,
                important=bool(channel.get("is_im")),
                direction="outbound" if outbound else "inbound",
                expected_owner_id=target.owner_id,
                session_factory=session_factory,
            )
            if result.message_created:
                imported += 1
            if imported >= MAX_MESSAGES_PER_POLL:
                break
        if highest:
            channel_cursors[channel_id] = highest
        if imported >= MAX_MESSAGES_PER_POLL:
            break
    return PollResult(
        provider="slack",
        imported_count=imported,
        cursor={"channels": channel_cursors},
    )


def _twilio_time(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None


def _latest_iso(current: str, candidate: datetime | None) -> str:
    if candidate is None:
        return current
    value = candidate.isoformat().replace("+00:00", "Z")
    return value if not current or value > current else current


async def _poll_twilio(
    target: PollTarget,
    cursor: Mapping[str, Any],
    *,
    client: httpx.AsyncClient,
    session_factory,
) -> PollResult:
    credential = str(target.integration.get("api_key") or "")
    sid = credential.split(":", 1)[0]
    if not _TWILIO_SID_RE.fullmatch(sid):
        raise ReadonlyCommunicationPollingError(
            "provider_credential_invalid", "Twilio credential format is invalid"
        )
    base_path = f"/2010-04-01/Accounts/{sid}"
    message_params: dict[str, object] = {"PageSize": 100}
    call_params: dict[str, object] = {"PageSize": 100}
    message_cursor = str(cursor.get("messages_after") or "")
    call_cursor = str(cursor.get("calls_after") or "")
    if message_cursor:
        message_params["DateSentAfter"] = message_cursor[:10]
    if call_cursor:
        call_params["StartTimeAfter"] = call_cursor[:10]
    message_payload = await _request_provider_json(
        client, target, base_path + "/Messages.json", params=message_params,
    )
    call_payload = await _request_provider_json(
        client, target, base_path + "/Calls.json", params=call_params,
    )
    messages = message_payload.get("messages")
    calls = call_payload.get("calls")
    if not isinstance(messages, list) or not isinstance(calls, list):
        raise ReadonlyCommunicationPollingError(
            "twilio_response_invalid", "Twilio communication lists are malformed"
        )
    imported = 0
    next_message_cursor = message_cursor
    for message in reversed(messages[:100]):
        if not isinstance(message, dict):
            continue
        occurred = _twilio_time(
            message.get("date_sent") or message.get("date_created")
        )
        occurred_iso = occurred.isoformat().replace("+00:00", "Z") if occurred else ""
        if message_cursor and occurred_iso and occurred_iso <= message_cursor:
            continue
        direction = str(message.get("direction") or "").lower()
        outbound = direction.startswith("outbound")
        other_value = message.get("to") if outbound else message.get("from")
        other = _identity_text(other_value, field="phone identity", limit=64)
        body = _content_text(message.get("body"))
        if body:
            result = ingest_readonly_communication_message(
                owner=target.owner_username,
                channel="sms",
                connector_id=target.configuration_id,
                conversation_ref=other or str(message.get("messaging_service_sid") or "sms"),
                text=body,
                message_id=_identity_text(
                    message.get("sid"), field="message id", limit=64,
                ),
                sender_name=other or "Twilio SMS",
                observed_at=occurred,
                unread=not outbound,
                important=False,
                direction="outbound" if outbound else "inbound",
                expected_owner_id=target.owner_id,
                session_factory=session_factory,
            )
            if result.message_created:
                imported += 1
        next_message_cursor = _latest_iso(next_message_cursor, occurred)

    next_call_cursor = call_cursor
    for call in reversed(calls[:100]):
        if not isinstance(call, dict):
            continue
        occurred = _twilio_time(
            call.get("start_time") or call.get("date_created")
        )
        occurred_iso = occurred.isoformat().replace("+00:00", "Z") if occurred else ""
        if call_cursor and occurred_iso and occurred_iso <= call_cursor:
            continue
        direction = str(call.get("direction") or "").lower()
        outbound = direction.startswith("outbound")
        other_value = call.get("to") if outbound else call.get("from")
        other = _identity_text(other_value, field="phone identity", limit=64)
        status = _identity_text(call.get("status") or "unknown", field="call status", limit=32)
        duration = _identity_text(call.get("duration") or "0", field="call duration", limit=32)
        body = (
            f"{'Outgoing' if outbound else 'Incoming'} call "
            f"{'to' if outbound else 'from'} {other or 'unknown contact'}; "
            f"status {status}; duration {duration} seconds."
        )
        result = ingest_readonly_communication_message(
            owner=target.owner_username,
            channel="call",
            connector_id=target.configuration_id,
            conversation_ref=other or "call-log",
            text=body,
            message_id=_identity_text(
                call.get("sid"), field="call id", limit=64,
            ),
            sender_name=other or "Twilio call",
            observed_at=occurred,
            unread=not outbound,
            important=not outbound and status not in {"completed", "canceled"},
            direction="outbound" if outbound else "inbound",
            expected_owner_id=target.owner_id,
            session_factory=session_factory,
        )
        if result.message_created:
            imported += 1
        next_call_cursor = _latest_iso(next_call_cursor, occurred)
    return PollResult(
        provider="twilio",
        imported_count=imported,
        cursor={
            "messages_after": next_message_cursor,
            "calls_after": next_call_cursor,
        },
    )


async def poll_readonly_communications_once(
    *,
    session_factory=SessionLocal,
    client: httpx.AsyncClient | None = None,
) -> dict[str, int]:
    targets = _poll_targets(session_factory)
    result = {"targets": len(targets), "healthy": 0, "failed": 0, "imported": 0}
    owned_client = client is None
    active_client = client or httpx.AsyncClient(timeout=communications_http_timeout())
    try:
        for target in targets:
            cursor = _cursor_for_target(target, session_factory)
            try:
                if target.provider == "slack":
                    polled = await _poll_slack(
                        target, cursor, client=active_client,
                        session_factory=session_factory,
                    )
                else:
                    polled = await _poll_twilio(
                        target, cursor, client=active_client,
                        session_factory=session_factory,
                    )
                _update_poll_state(
                    target,
                    session_factory=session_factory,
                    cursor=polled.cursor,
                    imported_count=polled.imported_count,
                    error_code=None,
                )
                result["healthy"] += 1
                result["imported"] += polled.imported_count
            except asyncio.CancelledError:
                raise
            except ReadonlyCommunicationPollingError as exc:
                _update_poll_state(
                    target,
                    session_factory=session_factory,
                    cursor=None,
                    imported_count=0,
                    error_code=exc.code,
                )
                result["failed"] += 1
                log.error("Read-only %s polling failed: %s", target.provider, exc.code)
            except Exception as exc:
                code = f"poll_runtime_{exc.__class__.__name__.lower()}"[:64]
                _update_poll_state(
                    target,
                    session_factory=session_factory,
                    cursor=None,
                    imported_count=0,
                    error_code=code,
                )
                result["failed"] += 1
                log.error("Read-only %s polling failed: %s", target.provider, code)
        return result
    finally:
        if owned_client:
            await active_client.aclose()


def communications_polling_health(db, *, owner_id: str) -> list[dict[str, Any]]:
    rows = db.query(CommunicationPollState).filter(
        CommunicationPollState.owner_id == owner_id,
    ).order_by(
        CommunicationPollState.provider.asc(), CommunicationPollState.updated_at.desc(),
    ).limit(MAX_TARGETS).all()
    return [
        {
            "provider": row.provider,
            "state": row.state,
            "last_attempt_at": (
                row.last_attempt_at.isoformat() + "Z" if row.last_attempt_at else None
            ),
            "last_success_at": (
                row.last_success_at.isoformat() + "Z" if row.last_success_at else None
            ),
            "error_code": row.error_code,
            "consecutive_failures": int(row.consecutive_failures or 0),
            "imported_count": int(row.imported_count or 0),
        }
        for row in rows
    ]


async def communications_polling_loop(*, session_factory=SessionLocal) -> None:
    interval = communications_poll_interval()
    while True:
        await poll_readonly_communications_once(session_factory=session_factory)
        await asyncio.sleep(interval)


__all__ = [
    "PollResult",
    "PollTarget",
    "ReadonlyCommunicationPollingError",
    "communications_poll_interval",
    "communications_polling_health",
    "communications_polling_loop",
    "inprocess_communications_polling_enabled",
    "poll_readonly_communications_once",
]
