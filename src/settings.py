# src/settings.py
"""Canonical Account.id-owned settings and feature preference adapters."""

import time
from typing import Any

# Tiny per-profile TTL cache for hot-path SQL configuration reads.
_CACHE_TTL = 2.0
_settings_cache: dict[str, tuple[float, dict]] = {}
_features_cache: dict[str, tuple[float, dict]] = {}

# Values in this file are configuration, but several are credentials.  The
# explicit set documents current fields while suffix matching also protects
# legacy/optional provider settings that are not materialized in defaults.
SECRET_SETTING_KEYS = frozenset({
    "brave_api_key",
    "google_pse_key",
    "search_url",
    "serper_api_key",
    "tavily_api_key",
    "telegram_bot_token",
    "telegram_webhook_secret",
})
_SECRET_SETTING_SUFFIXES = (
    "_api_key",
    "_credential",
    "_credentials",
    "_key",
    "_password",
    "_secret",
    "_token",
)


def _is_secret_setting(key: Any, value: Any) -> bool:
    normalized = key.lower().replace("-", "_") if isinstance(key, str) else ""
    return (
        isinstance(key, str)
        and isinstance(value, str)
        and (
            normalized in SECRET_SETTING_KEYS
            or normalized.endswith(_SECRET_SETTING_SUFFIXES)
        )
    )


def _invalidate_caches():
    global _settings_cache, _features_cache
    _settings_cache = {}
    _features_cache = {}

# ── Default values ──

DEFAULT_SETTINGS = {
    # Retained only so older settings files/UI clients round-trip cleanly.
    # Agent mail safety is no longer configurable: every model-originated
    # send/reply creates an encrypted Level-5 ActionProposal and never opens
    # SMTP/IMAP or writes to the legacy scheduled-email sidecar.
    "agent_email_confirm": True,
    "image_gen_enabled": False,
    "image_model": "",
    "image_quality": "medium",
    "vision_model": "",
    "vision_enabled": True,
    # Ordered fallback chain for the Vision model (image analysis, OCR, tagging).
    "vision_model_fallbacks": [],
    # Public base URL used to build clickable deep-links in outgoing alerts
    # (e.g., urgency alert email). Example: "https://chat.example.com"
    "app_public_url": "",
    "tts_enabled": True,
    "tts_provider": "disabled",
    "tts_model": "tts-1",
    "tts_voice": "alloy",
    "tts_speed": "1",
    "stt_enabled": False,
    "stt_provider": "disabled",
    "stt_model": "base",
    "stt_language": "",
    "search_provider": "searxng",
    # Default fallback chain — when the primary provider fails or
    # rate-limits, we try DuckDuckGo next. Free, no API key required, so
    # safe to ship on by default for every user.
    "search_fallback_chain": ["duckduckgo"],
    "search_url": "",
    "search_result_count": 5,
    # SafeSearch level applied to every provider that exposes one.
    # "strict"   — block adult / explicit results (default; matches what users
    #              expect from a research tool and avoids unrelated NSFW URLs
    #              bleeding in via provider "related" / spam recommendations)
    # "moderate" — provider-default behavior (filter explicit but allow
    #              suggestive content)
    # "off"      — disable filtering entirely (advanced users only)
    #
    # Providers that honor this setting (translated to each provider's native
    # param in src/search/providers.py:_safesearch_for):
    #     SearXNG       safesearch=0/1/2 (JSON API, HTML scrape, news fallback)
    #     Brave Search  safesearch=off/moderate/strict
    #     DuckDuckGo    safesearch=off/moderate/on (library + HTML kp param)
    #     Google PSE    safe=active (omitted for "off"; PSE has no middle tier)
    #     Serper.dev    safe=active (omitted for "off"; proxies Google's `safe`)
    # Providers NOT touched: Tavily (no SafeSearch knob; filters at index time)
    # and any custom backend reached via search_url — they keep whatever the
    # backend itself decides, so operators stay in control of self-hosted /
    # niche search instances.
    "search_safesearch": "strict",
    "brave_api_key": "",
    "google_pse_key": "",
    "google_pse_cx": "",
    "tavily_api_key": "",
    "serper_api_key": "",
    "research_endpoint_id": "",
    "research_model": "",
    "research_search_provider": "",
    "research_max_tokens": 16384,
    "research_extraction_timeout_seconds": 90,
    # Lightweight planning/query LLM calls happen before any search starts.
    # Keep them separately tunable so slow local backends are not capped by
    # the old 30s/60s per-call defaults.
    "research_planning_timeout_seconds": 90,
    "research_query_timeout_seconds": 90,
    "research_extraction_concurrency": 3,
    # Hard wall-clock cap on a single deep-research run. The previous 600s
    # (10 min) default cut off slow local / edge LLMs mid-synthesis; 1800s
    # (30 min) is comfortable for most local setups while still bounding
    # runaway jobs. Set to 0 to disable the cap entirely (unlimited) — only
    # for very long deep-research runs, since a stalled job then runs an
    # unbounded model/API bill. Other values are bounded to [60, 86400].
    # Tune through the authenticated Settings surface.
    "research_run_timeout_seconds": 1800,
    "agent_max_tool_calls": 0,
    "agent_max_rounds": 20,  # per-message agent step cap (clamped 1..200)
    # Soft input-token budget for the agent loop. The DEFAULT value (6000) is the
    # "auto" sentinel: it means "scale the budget to the model's context window"
    # (#1230) — so long-context models aren't capped at 6000. Set ANY OTHER value
    # to enforce an explicit cap (clamped to the window only — hard_max does not
    # apply to explicit budgets, #1230); set 0 to disable soft-trimming. The
    # default is treated as auto because the settings-save path materializes
    # defaults, so a persisted 6000 can't be told apart from a deliberate 6000 —
    # to pin a budget near the default, use a nearby value (e.g. 5999).
    "agent_input_token_budget": 6000,
    # Ceiling on the *auto-derived* input budget; a configurable setting since #1273
    # (the merged #1230 left it a module constant). No effect on an explicit budget
    # — a deliberate value is honoured (#1230). Default matches
    # `src.context_budget.DEFAULT_HARD_MAX`; lower this for
    # cost-paranoid setups, raise it on premium APIs with very large windows you
    # want to actually use (e.g. 900_000 to fill a 1M-context model). See
    # `compute_input_token_budget`.
    "agent_input_token_hard_max": 200_000,
    "agent_stream_timeout_seconds": 300,
    # Extra directory roots that read_file / write_file may access, in
    # addition to the built-in project data/ and system temp dirs. Each
    # entry is an absolute path. Sensitive subpaths (.ssh, .gnupg, shell
    # rc files, SSH key files) are always blocked regardless of roots.
    "tool_path_extra_roots": [],
    "task_endpoint_id": "",
    "task_model": "",
    "default_endpoint_id": "",
    "default_model": "",
    # Optional prose style used only for normal document writing/editing.
    # Email replies use email_writing_style instead because greetings,
    # signatures, and mailbox identity rules are medium-specific.
    "document_writing_style": "",
    # Ordered fallback chain for the default chat model. Each entry is
    # {"endpoint_id": "...", "model": "..."}. If the primary model fails
    # before producing output (endpoint offline / errors), the chat
    # dispatch retries the next entry in order.
    "default_model_fallbacks": [],
    # When True, non-admin users inherit global default model/endpoint/fallbacks
    # when they have no personal defaults. When False, users only use their
    # personal defaults (no global fallback). Default is False.
    "share_defaults_with_users": False,
    "utility_endpoint_id": "",
    "utility_model": "",
    # Ordered fallback chain for the Utility model (summarization, naming,
    # tidy actions, etc.).
    "utility_model_fallbacks": [],
    "teacher_model": "",
    "teacher_enabled": False,
    "teacher_tier2_enabled": False,
    # Skills: minimum self-reported confidence for an auto-written (LLM-authored)
    # DRAFT skill to be injected into the agent prompt. Published skills always
    # qualify. Keeps low-confidence auto-skills out of context until they're
    # vetted/published. 0 disables the gate.
    "skill_autosave_min_confidence": 0.85,
    # Max relevant skills injected into the prompt for one request. The skills
    # library can grow beyond this; cleanup/retirement is an explicit review flow.
    "skill_max_injected": 3,
    # Reminders
    "reminder_channel": "browser",   # "browser" | "email" | "ntfy" | "webhook" | "telegram"
    "reminder_llm_synthesis": False,
    "reminder_llm_persona": "",
    "reminder_ntfy_topic": "Reminders",
    "reminder_email_to": "",
    # Mirror every reminder to Telegram IN ADDITION to the primary channel —
    # makes the Telegram bridge act as a notification layer.
    "reminder_telegram_mirror": False,
    # Explicit chat id(s) for reminder delivery (comma-separated). Empty =
    # fall back to allowed_chat_ids + active session chats.
    "reminder_telegram_chat_id": "",
    # Generic outbound webhook channel: pick any saved Integration as the
    # target and supply a JSON payload template. Use {{title}} and {{message}}
    # as placeholders — they are JSON-escaped before substitution, so the
    # rendered string is always valid JSON. Works with Discord, Slack, Teams,
    # ntfy (JSON mode), or any service that accepts a POST with a JSON body.
    "reminder_webhook_integration_id": "",
    "reminder_webhook_payload_template": "",
    # Telegram bridge. Secrets can also be supplied via environment variables:
    # TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET,
    # TELEGRAM_ALLOWED_CHAT_IDS, TELEGRAM_ALLOW_ALL_CHATS, TELEGRAM_OWNER.
    "telegram_enabled": False,
    "telegram_bot_token": "",
    "telegram_webhook_secret": "",
    # New/private instances use Bot API long polling, so no public URL or
    # router/NAT setup is required. Existing script-configured webhook installs
    # are inferred as webhook mode until this field is explicitly saved.
    "telegram_runtime_mode": "polling",  # "polling" | "webhook"
    # Display-safe identity + ownership proof for webhook conflict checks.
    # The token itself remains encrypted separately and is never returned by
    # Telegram routes.
    "telegram_bot_id": "",
    "telegram_bot_username": "",
    "telegram_bot_first_name": "",
    "telegram_registered_webhook_url": "",
    # Identity fields below are retained only as an import source for installs
    # upgrading from settings.json. Runtime authorization is SQL-backed and
    # must never fall back to these values.
    "telegram_allowed_chat_ids": [],
    "telegram_allow_all_chats": False,
    "telegram_owner": "",
    "telegram_session_map": {},
    # Legacy multi-user Telegram routing import fields. New chats are linked to
    # immutable Account.id values with SQL-backed, one-time link codes.
    "telegram_chat_owners": {},
    "telegram_link_codes": {},
    # IANA timezone for Telegram conversations (e.g. "Asia/Kolkata"). Telegram
    # has no browser headers to carry the user's clock, so chats resolve
    # relative dates against this zone; empty = server-local time.
    "telegram_timezone": "",
    # Email triage scanner rules. Running/paused state and schedule live in
    # Tasks via the built-in `check_email_urgency` task.
    "urgent_email_prompt": (
        "Flag as urgent: explicit deadlines, time-sensitive requests, "
        "work-blocking issues, messages from people I report to, or anything "
        "where a delayed reply costs money/trust. Someone waiting outside, "
        "at the door, locked out, or unable to get in is urgent now. "
        "Newsletters, marketing, automated digests, and FYI-only updates are "
        "NOT urgent."
    ),
    # Keyboard shortcuts (action: key combination)
    "keybinds": {
        "search": "ctrl+k",
        "toggle_sidebar": "ctrl+b",
        "new_session": "ctrl+alt+n",
        "star_session": "ctrl+alt+s",
        "delete_session": "ctrl+alt+d",
        "admin_panel": "ctrl+shift+u",
        "cancel": "escape",
    },
}

DEFAULT_FEATURES = {
    "web_search": True,
    "web_fetch": True,
    "deep_research": False,
    "memory": True,
    "document_editor": True,
    "rag": True,
    "sensitive_filter": True,
    "gallery": True,
}


# ── Canonical Account.id-owned settings ──

def _runtime_owner(owner: str | None = None) -> str:
    if str(owner or "").strip():
        return str(owner).strip().lower()
    try:
        from src.auth_runtime import primary_admin_username

        primary = str(primary_admin_username() or "").strip().lower()
        if primary:
            return primary
    except Exception:
        pass
    from src.auth_helpers import resolved_runtime_owner

    return resolved_runtime_owner()


def _configuration_account(db, owner: str | None, *, write: bool):
    from src.identity import ensure_account, find_account

    username = _runtime_owner(owner)
    return ensure_account(db, username) if write else find_account(db, username)


def load_settings(owner: str | None = None) -> dict:
    """Load canonical SQL settings merged with immutable application defaults."""

    global _settings_cache

    from core.database import SessionLocal
    from src.profile_configuration_adapters import profile_settings

    cache_key = _runtime_owner(owner)
    now = time.monotonic()
    if not isinstance(_settings_cache, dict):
        # A stale extension/test may still clear the pre-SQL singleton cache by
        # assigning None. Recover without changing the canonical authority.
        _settings_cache = {}
    cached = _settings_cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return dict(cached[1])
    db = SessionLocal()
    try:
        account = _configuration_account(db, cache_key, write=False)
        merged = (
            profile_settings(db, owner_id=account.id)
            if account is not None else dict(DEFAULT_SETTINGS)
        )
        db.rollback()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    _settings_cache[cache_key] = (now, dict(merged))
    return dict(merged)


def save_settings(settings: dict, owner: str | None = None):
    """Persist mutable settings to canonical SQL with optimistic versions."""

    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    from core.database import SessionLocal
    from src.profile_configuration_service import (
        list_configurations,
        put_configuration,
        serialize_configuration,
    )

    db = SessionLocal()
    try:
        account = _configuration_account(db, owner, write=True)
        rows, truncated = list_configurations(
            db, owner_id=account.id, namespace="setting", limit=500,
        )
        if truncated:
            raise RuntimeError("Canonical setting count exceeds the adapter limit")
        existing = {row.key: row for row in rows}
        for key in DEFAULT_SETTINGS:
            if key not in settings:
                continue
            current = existing.get(key)
            if (
                current is not None
                and serialize_configuration(current)["value"] == settings[key]
            ):
                continue
            put_configuration(
                db,
                account=account,
                namespace="setting",
                key=key,
                value=settings[key],
                expected_version=int(current.version) if current is not None else None,
                source="domain_service",
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    _invalidate_caches()


def get_setting(key: str, default: Any = None, owner: str | None = None) -> Any:
    """Read a single setting value."""
    return load_settings(owner=owner).get(key, default)


def is_setting_overridden(key: str, owner: str | None = None) -> bool:
    """Whether a canonical SQL row explicitly overrides this setting."""

    from core.database import SessionLocal
    from src.profile_configuration_service import (
        ProfileConfigurationNotFound,
        get_configuration,
    )

    db = SessionLocal()
    try:
        account = _configuration_account(db, owner, write=False)
        if account is None:
            return False
        try:
            get_configuration(
                db, owner_id=account.id, namespace="setting", key=key,
            )
        except ProfileConfigurationNotFound:
            return False
        return True
    finally:
        db.rollback()
        db.close()


# Per-user settings (user prefs override the global admin default). Used for
# keys that a user is allowed to choose individually — currently the vision
# model + image-generation model. The owner argument is the authed username
# resolved by FastAPI deps; an empty/None owner falls through to the global.
_PER_USER_KEYS = {
    "vision_model", "vision_enabled", "vision_model_fallbacks",
    "image_model", "image_gen_enabled", "image_quality",
    # Default chat endpoint / model — without per-user resolution every new
    # account inherited whatever the most-recent admin picked, which then
    # got injected into the chat composer on first open.
    "default_endpoint_id", "default_model", "default_model_fallbacks",
    "utility_endpoint_id", "utility_model", "utility_model_fallbacks",
    "research_endpoint_id", "research_model",
}


def get_user_setting(key: str, owner: str = "", default: Any = None) -> Any:
    """Resolve `key` from the caller's per-user prefs first, falling back to
    the global setting. Only the small whitelist in `_PER_USER_KEYS` is
    eligible — for any other key this is equivalent to `get_setting(key)`.

    Falls back gracefully if the prefs module can't be imported (cycle/early
    boot) — admin-global settings keep working.
    """
    if owner and key in _PER_USER_KEYS:
        try:
            from routes.prefs_routes import _load_for_user
            prefs = _load_for_user(owner) or {}
            if key in prefs and prefs[key] not in (None, ""):
                return prefs[key]
        except Exception:
            pass
    return get_setting(key, default, owner=owner or None)


# ── Canonical Account.id-owned feature preferences ──

def load_features(owner: str | None = None) -> dict:
    global _features_cache

    from core.database import SessionLocal
    from src.profile_configuration_adapters import profile_features

    cache_key = _runtime_owner(owner)
    now = time.monotonic()
    if not isinstance(_features_cache, dict):
        _features_cache = {}
    cached = _features_cache.get(cache_key)
    if cached and (now - cached[0]) < _CACHE_TTL:
        return dict(cached[1])
    db = SessionLocal()
    try:
        account = _configuration_account(db, cache_key, write=False)
        merged = (
            profile_features(db, owner_id=account.id)
            if account is not None else dict(DEFAULT_FEATURES)
        )
        db.rollback()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    _features_cache[cache_key] = (now, dict(merged))
    return dict(merged)


def save_features(features: dict, owner: str | None = None):
    if not isinstance(features, dict):
        raise ValueError("features must be an object")
    from core.database import SessionLocal
    from src.profile_configuration_service import (
        list_configurations,
        put_configuration,
        serialize_configuration,
    )

    db = SessionLocal()
    try:
        account = _configuration_account(db, owner, write=True)
        rows, truncated = list_configurations(
            db, owner_id=account.id, namespace="feature", limit=100,
        )
        if truncated:
            raise RuntimeError("Canonical feature count exceeds the adapter limit")
        existing = {row.key: row for row in rows}
        for key in DEFAULT_FEATURES:
            if key not in features:
                continue
            current = existing.get(key)
            if (
                current is not None
                and serialize_configuration(current)["value"] == features[key]
            ):
                continue
            put_configuration(
                db,
                account=account,
                namespace="feature",
                key=key,
                value=features[key],
                expected_version=int(current.version) if current is not None else None,
                source="domain_service",
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    _invalidate_caches()
