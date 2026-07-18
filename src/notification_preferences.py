"""Validated, owner-scoped reminder and Telegram digest preferences."""

from __future__ import annotations

import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from routes.prefs_routes import _load_for_user, _save_for_user
from src.settings import load_settings

PREFS_KEY = "notification_preferences"
TIMEZONE_CONFIGURED_KEY = "notification_timezone_configured"
REMINDER_CHANNELS = frozenset({"browser", "email", "ntfy", "webhook", "telegram"})
NOTIFICATION_TOPICS = frozenset({"reminders", "calendar", "todos", "email", "projects", "tasks"})
DIGEST_CADENCES = frozenset({"off", "hourly", "every_3_hours", "every_6_hours", "daily"})
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_prefs_lock = threading.RLock()


class NotificationPreferenceError(ValueError):
    pass


def _owner_key(owner: str | None) -> str:
    return str(owner or "").strip().lower() or "local"


def _bool(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    raise NotificationPreferenceError(f"{field} must be true or false")


def _short_text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise NotificationPreferenceError(f"{field} is too long")
    return text


def _timezone(value: Any) -> str:
    name = _short_text(value, "timezone", 128) or "UTC"
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise NotificationPreferenceError("timezone must be a valid IANA timezone") from None
    return name


def _time(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _TIME_RE.fullmatch(text):
        raise NotificationPreferenceError(f"{field} must use 24-hour HH:MM format")
    return text


def _defaults(global_settings: dict[str, Any]) -> dict[str, Any]:
    timezone = str(global_settings.get("telegram_timezone") or "").strip() or "UTC"
    try:
        ZoneInfo(timezone)
    except Exception:
        timezone = "UTC"
    channel = str(global_settings.get("reminder_channel") or "browser").strip().lower()
    if channel not in REMINDER_CHANNELS:
        channel = "browser"
    return {
        "reminder_channel": channel,
        "reminder_telegram_mirror": bool(global_settings.get("reminder_telegram_mirror", False)),
        "reminder_email_to": str(global_settings.get("reminder_email_to") or ""),
        "reminder_email_account_id": str(global_settings.get("reminder_email_account_id") or ""),
        "reminder_ntfy_topic": str(global_settings.get("reminder_ntfy_topic") or "Reminders"),
        "reminder_webhook_integration_id": str(global_settings.get("reminder_webhook_integration_id") or ""),
        "reminder_webhook_payload_template": str(global_settings.get("reminder_webhook_payload_template") or ""),
        "reminder_llm_synthesis": bool(global_settings.get("reminder_llm_synthesis", False)),
        "reminder_llm_persona": str(global_settings.get("reminder_llm_persona") or ""),
        "notification_topics": sorted(NOTIFICATION_TOPICS),
        "digest_cadence": "off",
        "digest_time": "08:00",
        "timezone": timezone,
        "quiet_hours_enabled": False,
        "quiet_hours_start": "22:00",
        "quiet_hours_end": "07:00",
    }


def _validate_patch(patch: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise NotificationPreferenceError("preferences must be an object")
    clean: dict[str, Any] = {}
    for key, value in patch.items():
        if key == "reminder_channel":
            channel = str(value or "").strip().lower()
            if channel not in REMINDER_CHANNELS:
                raise NotificationPreferenceError("unsupported reminder channel")
            clean[key] = channel
        elif key in {"reminder_telegram_mirror", "reminder_llm_synthesis", "quiet_hours_enabled"}:
            clean[key] = _bool(value, key)
        elif key == "notification_topics":
            if not isinstance(value, list):
                raise NotificationPreferenceError("notification_topics must be a list")
            topics = {str(item or "").strip().lower() for item in value}
            unknown = topics - NOTIFICATION_TOPICS
            if unknown:
                raise NotificationPreferenceError(f"unsupported notification topic: {sorted(unknown)[0]}")
            clean[key] = sorted(topics)
        elif key == "digest_cadence":
            cadence = str(value or "").strip().lower()
            if cadence not in DIGEST_CADENCES:
                raise NotificationPreferenceError("unsupported digest cadence")
            clean[key] = cadence
        elif key == "timezone":
            clean[key] = _timezone(value)
        elif key in {"digest_time", "quiet_hours_start", "quiet_hours_end"}:
            clean[key] = _time(value, key)
        elif key == "reminder_email_to":
            clean[key] = _short_text(value, key, 320)
        elif key in {"reminder_email_account_id", "reminder_webhook_integration_id", "reminder_llm_persona"}:
            clean[key] = _short_text(value, key, 256)
        elif key == "reminder_ntfy_topic":
            clean[key] = _short_text(value, key, 128) or "Reminders"
        elif key == "reminder_webhook_payload_template":
            clean[key] = _short_text(value, key, 8192)
        else:
            raise NotificationPreferenceError(f"unsupported notification preference: {key}")
    return clean


def load_notification_preferences(
    owner: str | None,
    *,
    global_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = (
        global_settings
        if global_settings is not None
        else load_settings(owner=owner)
    )
    result = _defaults(settings)
    try:
        user_prefs = _load_for_user(_owner_key(owner)) or {}
        saved = user_prefs.get(PREFS_KEY)
        if isinstance(saved, dict):
            # Invalid historical values fail closed to defaults field-by-field.
            for key, value in saved.items():
                try:
                    result.update(_validate_patch({key: value}))
                except NotificationPreferenceError:
                    continue
    except Exception:
        pass
    return result


def save_notification_preferences(owner: str | None, patch: dict[str, Any]) -> dict[str, Any]:
    clean = _validate_patch(patch)
    key = _owner_key(owner)
    with _prefs_lock:
        all_user_prefs = _load_for_user(key) or {}
        current = load_notification_preferences(key)
        current.update(clean)
        all_user_prefs[PREFS_KEY] = current
        if "timezone" in clean:
            all_user_prefs[TIMEZONE_CONFIGURED_KEY] = True
        _save_for_user(key, all_user_prefs)
    return current


def notification_timezone_configured(owner: str | None) -> bool:
    """Whether this profile explicitly chose/advertised an IANA timezone."""

    try:
        all_user_prefs = _load_for_user(_owner_key(owner)) or {}
        if all_user_prefs.get(TIMEZONE_CONFIGURED_KEY) is True:
            return True
        # Upgrade heuristic: a historical non-default saved zone was an
        # intentional choice. Merely materialized defaults do not count.
        saved = all_user_prefs.get(PREFS_KEY)
        if not isinstance(saved, dict) or not str(saved.get("timezone") or "").strip():
            return False
        default_zone = _defaults(load_settings(owner=owner)).get("timezone") or "UTC"
        return str(saved.get("timezone")) != str(default_zone)
    except Exception:
        return False


def ensure_notification_timezone(owner: str | None, timezone_name: Any) -> str:
    """Adopt a browser IANA zone once, without overriding a user's choice."""

    candidate = _timezone(timezone_name)
    with _prefs_lock:
        if not notification_timezone_configured(owner):
            save_notification_preferences(owner, {"timezone": candidate})
    return str(load_notification_preferences(owner).get("timezone") or candidate)


def settings_with_notification_preferences(
    owner: str | None,
    base_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings = dict(
        base_settings
        if base_settings is not None
        else load_settings(owner=owner)
    )
    settings.update(load_notification_preferences(owner, global_settings=settings))
    return settings


def normalize_notification_due_date(owner: str | None, value: Any) -> Any:
    """Canonicalize a timed, offset-less UI reminder in the profile timezone.

    ``datetime-local`` intentionally omits an offset. Persisting that raw value
    makes a background worker interpret it in the server/container timezone,
    which can differ from the browser by hours. Date-only todos remain dates;
    already-aware values remain authoritative.
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text or "T" not in text:
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is not None:
        return text
    preferences = load_notification_preferences(owner)
    try:
        zone = ZoneInfo(str(preferences.get("timezone") or "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc
    aware = parsed.replace(tzinfo=zone).astimezone(timezone.utc)
    return aware.isoformat(timespec="seconds").replace("+00:00", "Z")


def notification_topic_enabled(preferences: dict[str, Any], topic: str) -> bool:
    topics = preferences.get("notification_topics")
    return isinstance(topics, list) and str(topic or "reminders").strip().lower() in topics


def quiet_hours_active(
    preferences: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    if not preferences.get("quiet_hours_enabled"):
        return False
    try:
        zone = ZoneInfo(str(preferences.get("timezone") or "UTC"))
        local_now = now.astimezone(zone) if now and now.tzinfo else (now.replace(tzinfo=zone) if now else datetime.now(zone))
        current = local_now.strftime("%H:%M")
        start = _time(preferences.get("quiet_hours_start"), "quiet_hours_start")
        end = _time(preferences.get("quiet_hours_end"), "quiet_hours_end")
        if start == end:
            return False
        if start < end:
            return start <= current < end
        return current >= start or current < end
    except Exception:
        return False


def seconds_until_quiet_hours_end(
    preferences: dict[str, Any],
    *,
    now: datetime | None = None,
) -> int:
    """Seconds until quiet hours end, or zero when they are not active."""
    if not quiet_hours_active(preferences, now=now):
        return 0
    zone = ZoneInfo(str(preferences.get("timezone") or "UTC"))
    local_now = now.astimezone(zone) if now and now.tzinfo else (
        now.replace(tzinfo=zone) if now else datetime.now(zone)
    )
    end_hour, end_minute = (
        int(part) for part in _time(preferences.get("quiet_hours_end"), "quiet_hours_end").split(":")
    )
    end_at = local_now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if end_at <= local_now:
        end_at += timedelta(days=1)
    return max(1, int((end_at - local_now).total_seconds()))
