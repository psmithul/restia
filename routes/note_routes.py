# routes/note_routes.py
"""Google Keep-style notes / checklists API."""

import json
import uuid
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.database import SessionLocal, Note, utcnow_naive
from core.middleware import INTERNAL_TOOL_USER
from src.auth_helpers import (
    DEFAULT_LOCAL_OWNER,
    allows_legacy_null_owner,
    owner_storage_key,
    require_user,
    resolved_request_owner,
)
from src.constants import DATA_DIR
from sqlalchemy.orm.attributes import flag_modified
from src.note_progression import (
    award_note_item_completions,
    completion_items_fully_done,
    normalize_created_items,
    normalize_updated_items,
    public_note_items,
    recurring_advance_values,
    recurring_occurrence_is_due,
)
from src.notification_preferences import (
    ensure_notification_timezone,
    load_notification_preferences,
    normalize_notification_due_date,
)

logger = logging.getLogger(__name__)

_COMPLETION_NOTE_TYPES = {"todo", "checklist", "goal"}


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class NoteCreate(BaseModel):
    title: str = ""
    content: Optional[str] = None
    items: Optional[list] = None
    note_type: str = "note"
    color: Optional[str] = None
    label: Optional[str] = None
    pinned: bool = False
    due_date: Optional[str] = None
    source: str = "user"
    session_id: Optional[str] = None
    image_url: Optional[str] = None
    repeat: Optional[str] = "none"
    sort_order: Optional[int] = None
    client_timezone: Optional[str] = None


class NoteUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    items: Optional[list] = None
    note_type: Optional[str] = None
    color: Optional[str] = None
    label: Optional[str] = None
    pinned: Optional[bool] = None
    archived: Optional[bool] = None
    due_date: Optional[str] = None
    image_url: Optional[str] = None
    repeat: Optional[str] = None
    sort_order: Optional[int] = None
    agent_session_id: Optional[str] = None
    client_timezone: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _note_to_dict(note: Note) -> Dict[str, Any]:
    items = None
    if note.items:
        try:
            items = public_note_items(json.loads(note.items))
        except (json.JSONDecodeError, TypeError):
            items = None
    ai_cls = None
    raw_ai = getattr(note, "ai_classification", None)
    if raw_ai:
        try:
            ai_cls = json.loads(raw_ai)
        except (json.JSONDecodeError, TypeError):
            ai_cls = None
    return {
        "id": note.id,
        "owner": note.owner,
        "title": note.title,
        "content": note.content,
        "items": items,
        "note_type": note.note_type,
        "color": note.color,
        "label": note.label,
        "pinned": note.pinned,
        "archived": note.archived,
        "due_date": note.due_date,
        "source": note.source,
        "session_id": note.session_id,
        "sort_order": note.sort_order or 0,
        "image_url": note.image_url,
        "repeat": note.repeat or "none",
        "ai_classification": ai_cls,
        "ai_content_hash": getattr(note, "ai_content_hash", None),
        "agent_session_id": getattr(note, "agent_session_id", None),
        "created_at": note.created_at.isoformat() if note.created_at else None,
        "updated_at": note.updated_at.isoformat() if note.updated_at else None,
    }


def _reminder_text_from_note(note: Note) -> tuple[str, str]:
    """Return the reminder title/body from a stored note row."""
    title = (note.title or "Note reminder").strip() or "Note reminder"
    if note.items:
        try:
            items = json.loads(note.items)
        except (json.JSONDecodeError, TypeError):
            items = None
        if isinstance(items, list):
            pending: list[str] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if item.get("done") or item.get("checked"):
                    continue
                text = str(item.get("text") or "").strip()
                if text:
                    pending.append(text)
            if pending:
                shown = "\n".join(f"- {text}" for text in pending[:8])
                extra = f"\n...and {len(pending) - 8} more" if len(pending) > 8 else ""
                return title, f"Pending ({len(pending)}):\n{shown}{extra}"
            return title, f"{len(items)} item{'s' if len(items) != 1 else ''}"
    return title, (note.content or "").strip()[:400]



# ---------------------------------------------------------------------------
# Reminder dispatch — module-level so background tasks (built-in actions)
# can call it directly without an HTTP roundtrip + auth cookie. The route
# version below is a thin wrapper that pulls `owner` from the request.
# ---------------------------------------------------------------------------

# Scheduler reference — set by setup_note_routes() so dispatch_reminder can
# push a parallel in-app notification (frontend polls the scheduler's queue
# and fires real browser Notification(...) popups). Optional; works without it.
_scheduler_ref = None


def _cancel_pending_note_reminder(
    owner: str,
    note_id: str,
    *,
    occurrence: str | None = None,
) -> dict[str, int]:
    """Tombstone stale outbox rows and unfinished delivery claims."""

    from src.note_reminder_state import cancel_note_reminder

    return cancel_note_reminder(
        owner,
        note_id,
        occurrence=occurrence,
        scheduler=_scheduler_ref,
    )


def _rearm_pending_note_reminder(
    owner: str,
    note_id: str,
    *,
    occurrence: str | None = None,
) -> int:
    """Allow a deliberately restored/rescheduled occurrence to notify again."""

    from src.note_reminder_state import rearm_note_reminder

    result = rearm_note_reminder(
        owner,
        note_id,
        occurrence=occurrence,
        scheduler=_scheduler_ref,
    )
    return result["outbox"] + result["claims"]


async def dispatch_reminder(
    title: str,
    note_body: str,
    note_id: str,
    owner: str = "",
    queue_browser: bool = True,
    settings_override: dict | None = None,
    topic: str = "reminders",
    respect_quiet_hours: bool = True,
    occurrence: str = "",
) -> dict:
    """Fire a reminder via the configured channel (browser/email/ntfy/webhook).

    Args:
        title: short headline shown to the user
        note_body: longer body text
        note_id: stable id (used as tag/dedupe in browser notifications)
        owner: the user this reminder belongs to — scopes SMTP config to
               their account so we don't cross-leak credentials

    Returns: {synthesis, email_sent, ntfy_sent}. Browser channel is wired via
    the in-memory notification queue picked up by the frontend poller, so
    nothing is "sent" synchronously for it — the channel just routes there.
    """
    from src.settings import load_settings
    from src.auth_helpers import resolved_runtime_owner
    from src.notification_preferences import (
        notification_topic_enabled,
        quiet_hours_active,
        settings_with_notification_preferences,
    )
    # Claims, outbox rows, preferences, and ACKs must share one concrete owner;
    # legacy null-owner notes otherwise create an empty-owner claim that a
    # DEFAULT_LOCAL_OWNER browser can never acknowledge.
    owner = resolved_runtime_owner(owner)
    settings = settings_with_notification_preferences(owner, load_settings())
    settings.update(settings_override or {})
    channel = settings.get("reminder_channel", "browser")
    telegram_fallback = False
    if str(channel).strip().lower() == "telegram":
        try:
            from src.telegram_bot import load_telegram_config, telegram_chat_ids_for_owner
            _telegram_config = load_telegram_config()
            if not (
                _telegram_config.enabled
                and _telegram_config.bot_token
                and telegram_chat_ids_for_owner(_telegram_config, owner)
            ):
                channel = "browser"
                settings["reminder_channel"] = "browser"
                settings["reminder_telegram_mirror"] = False
                telegram_fallback = True
        except Exception:
            # Keep the configured Telegram channel when status inspection is
            # unavailable; the normal send path will return a specific error.
            pass
    _tg_mirror = str(settings.get("reminder_telegram_mirror", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    telegram_mirror_unavailable = False
    if _tg_mirror and str(channel).strip().lower() != "telegram":
        try:
            from src.telegram_bot import load_telegram_config, telegram_chat_ids_for_owner

            mirror_config = load_telegram_config()
            if not (
                mirror_config.enabled
                and mirror_config.bot_token
                and telegram_chat_ids_for_owner(mirror_config, owner)
            ):
                _tg_mirror = False
                telegram_mirror_unavailable = True
        except Exception:
            # An optional mirror must never deadlock the selected primary
            # channel merely because Telegram status is unavailable.
            _tg_mirror = False
            telegram_mirror_unavailable = True
    mirror_retry_only = False
    primary_already_delivered = False
    llm_on = bool(settings.get("reminder_llm_synthesis", False))
    title = (title or "").strip()
    note_body = (note_body or "").strip()
    cache_key = str(note_id) if note_id else ""
    cache = {}
    cache_path = None
    if respect_quiet_hours and quiet_hours_active(settings):
        return {
            "channel": channel,
            "topic": topic,
            "synthesis": None,
            "email_sent": False,
            "ntfy_sent": False,
            "webhook_sent": False,
            "telegram_sent": False,
            "browser_sent": False,
            "delivered": False,
            "acknowledged": False,
            "deferred": True,
            "suppression_reason": "quiet_hours",
        }
    if cache_key:
        try:
            import json as _json
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            from pathlib import Path as _P
            _slug = owner_storage_key(owner)
            cache_path = _P(DATA_DIR) / f"note_pings_{_slug}.json"
            if cache_path.exists():
                cache = _json.loads(cache_path.read_text(encoding="utf-8"))
            last = cache.get(cache_key)
            if last:
                last_channel = None
                last_occurrence = ""
                if isinstance(last, dict):
                    last_channel = last.get("channel")
                    last_occurrence = str(last.get("occurrence") or "")
                    last_mirror_complete = bool(last.get("mirror_complete"))
                    last = last.get("at")
                else:
                    last_mirror_complete = False
                last_dt = _dt.fromisoformat(str(last))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=_tz.utc)
                # Legacy cache values were plain timestamps and could be
                # written by the frontend even when the email/ntfy send failed.
                # Treat those as browser-only dedupe so email reminders can be
                # retried by the backend scanner after a failed frontend path.
                should_skip = last_dt >= _dt.now(_tz.utc) - _td(minutes=25)
                if occurrence and last_occurrence == str(occurrence):
                    should_skip = True
                if should_skip and channel in ("email", "ntfy", "webhook", "telegram"):
                    should_skip = last_channel == channel
                if should_skip and not (
                    _tg_mirror
                    and str(channel).strip().lower() != "telegram"
                    and not last_mirror_complete
                ):
                    return {
                        "synthesis": None,
                        "email_sent": False,
                        "ntfy_sent": False,
                        "webhook_sent": False,
                        "telegram_sent": False,
                        "browser_sent": True,
                        "delivered": True,
                        "acknowledged": True,
                        "skipped": True,
                    }
        except Exception as _e:
            logger.debug(f"dispatch_reminder: cache read failed: {_e}")

    # Durable claim shared by browser-triggered delivery and the background
    # scanner. SQLite serializes this across threads/processes; the dispatcher
    # refuses to perform an external side effect unless it owns the claim.
    claim_token = ""
    claim_path = None
    if cache_key:
        try:
            from pathlib import Path as _P
            from src.reminder_delivery_claims import claim_reminder_delivery

            claim_path = _P(DATA_DIR) / "reminder_delivery_claims.sqlite3"
            claim = claim_reminder_delivery(
                claim_path,
                owner=owner,
                note_id=cache_key,
                occurrence=str(occurrence or ""),
                channel=str(channel or "browser"),
            )
            if not claim.acquired:
                already_delivered = claim.reason == "delivered"
                if already_delivered and _tg_mirror and str(channel).strip().lower() != "telegram":
                    # The selected primary channel already succeeded, but a
                    # per-chat Telegram mirror obligation may still be pending.
                    mirror_retry_only = True
                    primary_already_delivered = True
                else:
                    return {
                    "channel": channel,
                    "topic": topic,
                    "synthesis": None,
                    "email_sent": False,
                    "ntfy_sent": False,
                    "webhook_sent": False,
                    "telegram_sent": False,
                    "browser_sent": False,
                    "delivered": already_delivered,
                    "acknowledged": already_delivered,
                    "skipped": already_delivered,
                    "deferred": not already_delivered,
                    "suppression_reason": f"delivery_{claim.reason}",
                    }
            else:
                claim_token = claim.token
        except Exception as exc:
            # Sending without a claim could duplicate an email/Telegram/etc.
            # Fail closed and let the caller retry once durable state works.
            logger.exception("dispatch_reminder: durable claim failed for %s", cache_key)
            raise RuntimeError("Reminder delivery claim could not be recorded") from exc

    def _claim_still_active() -> bool:
        if claim_path is None or not claim_token:
            return True
        from src.reminder_delivery_claims import reminder_claim_is_active

        return reminder_claim_is_active(
            claim_path,
            owner=owner,
            note_id=cache_key,
            occurrence=str(occurrence or ""),
            channel=str(channel or "browser"),
            token=claim_token,
        )

    def _cancelled_result() -> dict:
        return {
            "channel": channel,
            "topic": topic,
            "synthesis": None,
            "email_sent": False,
            "ntfy_sent": False,
            "webhook_sent": False,
            "telegram_sent": False,
            "browser_sent": False,
            "delivered": False,
            "acknowledged": False,
            "deferred": True,
            "show_browser": False,
            "suppression_reason": "delivery_cancelled",
        }

    if not notification_topic_enabled(settings, topic):
        if claim_path is not None and claim_token:
            from src.reminder_delivery_claims import acknowledge_reminder_delivery
            acknowledge_reminder_delivery(
                claim_path,
                owner=owner,
                note_id=cache_key,
                occurrence=str(occurrence or ""),
                channel=str(channel or "browser"),
                token=claim_token,
            )
        return {
            "channel": channel,
            "topic": topic,
            "synthesis": None,
            "email_sent": False,
            "ntfy_sent": False,
            "webhook_sent": False,
            "telegram_sent": False,
            "browser_sent": False,
            "delivered": False,
            "acknowledged": True,
            "suppressed": True,
            "suppression_reason": "topic_disabled",
        }

    if not _claim_still_active():
        return _cancelled_result()

    synthesis = None
    _SYNTH_FAILED_TAG = "[utility model unavailable — no summary generated]"
    if llm_on:
        try:
            from src.endpoint_resolver import resolve_endpoint
            from src.llm_core import llm_call_async
            from src.reminder_personas import synthesis_system_prompt
            url, model, headers = resolve_endpoint("utility", owner=owner or None)
            if not url:
                url, model, headers = resolve_endpoint("default", owner=owner or None)
            if url and model:
                persona_id = (settings.get("reminder_llm_persona") or "").strip()
                sys_prompt = synthesis_system_prompt(persona_id)
                raw = await llm_call_async(
                    url=url, model=model,
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": f"Title: {title}\n\n{note_body}".strip()},
                    ],
                    temperature=0.7, max_tokens=200, headers=headers, timeout=30,
                )
                from src.text_helpers import strip_think as _strip_think
                # prose=True strips untagged "The user wants me to…" chain-of-thought.
                # prompt_echo=True strips Qwen-style "Thinking Process:" / leaked
                # prompt prefixes. Both are safe here because this is a
                # one-sentence LLM-only output, not user-pasted content.
                synthesis = _strip_think(raw or "", prose=True, prompt_echo=True)
                # Reminder synthesis is supposed to be ONE sentence. Strip-think's
                # paragraph-based heuristic misses cases where the model puts
                # reasoning + answer on consecutive lines inside one paragraph
                # (e.g. "I should write... [\n] You have one task waiting...").
                # Walk lines, drop reasoning/prompt-echo lines, then keep the
                # last surviving line — that's the actual warm sentence.
                if synthesis:
                    import re as _re
                    # Tightened: target ACTUAL self-talk (model narrating what
                    # it'll do) rather than any first-person sentence. The old
                    # pattern killed legit warm sentences like "I'll see you
                    # tomorrow" or "I should be done by then". New rules:
                    #  • "I (need|should|have|'ll|will) (write|draft|reply|…)"
                    #    only matches when followed by a TASK verb taking an
                    #    OBJECT (so first-person + intransitive verb passes).
                    #  • Self-instructional patterns the model emits verbatim:
                    #    "I should write something that reminds them…",
                    #    "I need to draft…", "Let me think…".
                    #  • Explicit instructions echoed back from the prompt:
                    #    "Keep it under 25 words", "No greetings".
                    _reasoning = _re.compile(
                        r"^\s*(?:"
                        # "I should write/draft/compose…" with a task-object follow
                        r"i (?:need|should|have|'ll|will|am going|am)\s+to\s+"
                        r"(?:write|draft|compose|craft|generate|produce|create|"
                        r"summarize|answer|provide|note|address|remind|output)"
                        r"\s+(?:a |an |the |something|this|that|here|them|him|her|"
                        r"you|user|reply|response|sentence|message|line|warm)|"
                        # The model literally narrating about the user
                        r"the user (?:wants|is asking|asks|needs|wrote|said|requested) (?:me )?(?:to|for|that|about|something)|"
                        # "Let me [think/write/draft/…] (about/for/the …)"
                        r"let me (?:think|write|draft|consider|note|see|check)\b\s+(?:about|for|the|this|that|if|whether)|"
                        # "Looking at the/this/that …"
                        r"looking at (?:the|this|that)\b|"
                        # "Based on the/this/what …"
                        r"based on (?:the|this|what|context|that)\b|"
                        # Prompt-echo of length / style instructions
                        r"keep it under \d+ words\b|"
                        r"(?:no greetings|no preamble|no hashtags|just output the)\b"
                        r").*",
                        _re.IGNORECASE,
                    )
                    # Echo of the prompt's "Pending:" / "<N> pending" tail.
                    _echo = _re.compile(
                        r"^\s*(?:pending\s*[:.]|(?:\d+|one|two|three|four|five)\s+pending\b)",
                        _re.IGNORECASE,
                    )
                    lines = [ln for ln in synthesis.splitlines() if ln.strip()]
                    cleaned = [ln for ln in lines if not _reasoning.match(ln) and not _echo.match(ln)]
                    if cleaned:
                        # The model's actual answer is normally the LAST surviving
                        # line — reasoning leads, answer trails.
                        synthesis = cleaned[-1].strip()
            else:
                synthesis = _SYNTH_FAILED_TAG
        except Exception as e:
            logger.warning(f"Reminder LLM synthesis failed: {e}")
            synthesis = _SYNTH_FAILED_TAG
        if synthesis:
            _s = synthesis.strip(); _low = _s.lower()
            if (not _s or _low.startswith("error:") or _low.startswith("[error")
                    or "operation failed" in _low
                    or ("upstream" in _low and "failed" in _low)) and synthesis != _SYNTH_FAILED_TAG:
                logger.warning(f"Reminder synthesis looked like an error, replacing: {_s[:120]!r}")
                synthesis = _SYNTH_FAILED_TAG

    # Cancellation may arrive while optional synthesis is running. Revalidate
    # the token after that await and immediately before channel side effects.
    if not _claim_still_active():
        return _cancelled_result()

    email_sent = False
    email_error = ""
    if channel == "email" and not mirror_retry_only:
        try:
            from routes.email_routes import _get_email_config
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            from datetime import datetime as _dt
            # `reminder_email_account_id` lets the user pick WHICH email
            # account to send reminders from (when they have several
            # configured in Integrations). Falls back to the default
            # account when no explicit choice is saved.
            _acc_id = (settings.get("reminder_email_account_id") or "").strip() or None
            cfg = _get_email_config(account_id=_acc_id, owner=owner or "")
            if not (cfg.get("smtp_host") and cfg.get("smtp_user") and cfg.get("smtp_password")):
                try:
                    from core.database import SessionLocal as _SL, EmailAccount as _EA
                    from sqlalchemy import and_, or_
                    db = _SL()
                    try:
                        q = db.query(_EA).filter(_EA.enabled == True)  # noqa: E712
                        if owner:
                            unowned = or_(_EA.owner == None, _EA.owner == "")  # noqa: E711
                            same_mailbox = or_(_EA.imap_user == owner, _EA.from_address == owner)
                            q = q.filter(or_(_EA.owner == owner, and_(unowned, same_mailbox)))
                        for row in q.order_by(_EA.is_default.desc(), _EA.created_at.asc()).all():
                            trial = _get_email_config(account_id=row.id, owner=owner or "")
                            if trial.get("smtp_host") and trial.get("smtp_user") and trial.get("smtp_password"):
                                cfg = trial
                                break
                    finally:
                        db.close()
                except Exception as _fallback_error:
                    logger.debug(f"Reminder SMTP fallback lookup failed: {_fallback_error}")
            from_addr = (cfg.get("from_address") or cfg.get("smtp_user") or "").strip()
            recipient = (settings.get("reminder_email_to") or "").strip() or from_addr
            # Loud diagnostic so we can see WHY a reminder didn't send (the
            # previous "silently no-op when cfg has no smtp_host" was invisible).
            logger.info(
                "dispatch_reminder[email] note_id=%s owner=%r "
                "has_smtp_host=%s has_smtp_user=%s has_from=%s has_recipient=%s",
                note_id, owner,
                bool(cfg.get("smtp_host")), bool(cfg.get("smtp_user")),
                bool(from_addr), bool(recipient),
            )
            missing = []
            if not cfg.get("smtp_host"):
                missing.append("SMTP host")
            if not cfg.get("smtp_user"):
                missing.append("SMTP user")
            if not cfg.get("smtp_password"):
                missing.append("SMTP password")
            if not from_addr:
                missing.append("from address")
            if not recipient:
                missing.append("recipient")
            if missing:
                email_error = "Missing " + ", ".join(missing)
                logger.warning(
                    "Reminder email not sent for note_id=%s account=%r: %s",
                    note_id, cfg.get("account_name"), email_error,
                )
            else:
                msg = MIMEMultipart("alternative")
                msg["From"] = from_addr
                msg["To"] = recipient
                _t = title or 'Note'
                _t = _t[len('Reminder:'):].strip() if _t.lower().startswith('reminder:') else _t
                msg["Subject"] = f"Reminder (Restia): {_t}"
                msg["Date"] = _dt.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")
                msg["X-Restia-Origin"] = "odysseus-ui"
                msg["X-Restia-Kind"] = "reminder"
                msg["X-Restia-Ref"] = str(note_id)
                # Body shape: synthesis (warm sentence) → blank line → bold
                # title header → note details. The title was previously only
                # in the subject line, so the email read like a faceless
                # to-do list with no anchor to which note triggered it.
                _body_chunks = []
                if synthesis:
                    _body_chunks.append(synthesis)
                if _t:
                    _body_chunks.append(_t)
                if note_body:
                    _body_chunks.append(note_body)
                plain = "\n\n".join(_body_chunks) if _body_chunks else title
                msg.attach(MIMEText(plain, "plain", "utf-8"))

                def _smtp_send():
                    if not _claim_still_active():
                        raise RuntimeError("Reminder delivery was cancelled")
                    from routes.email_helpers import _send_smtp_message
                    _send_smtp_message(cfg, from_addr, [recipient], msg.as_string())

                import asyncio as _aio
                await _aio.to_thread(_smtp_send)
                email_sent = True
        except Exception as e:
            email_error = str(e) or e.__class__.__name__
            logger.warning(f"Reminder email send failed: {e}")

    webhook_sent = False
    webhook_error = ""
    if channel == "webhook" and not mirror_retry_only:
        try:
            import httpx
            import json as _wjson
            from src.integrations import load_integrations
            # Built-in payload defaults for known presets so users don't have
            # to configure a template just to use a standard service.
            _PRESET_TEMPLATE_DEFAULTS = {
                "discord_webhook": '{"embeds": [{"title": "{{title}}", "description": "{{message}}", "color": 5793266}]}',
            }
            intg_id = settings.get("reminder_webhook_integration_id", "").strip()
            template = settings.get("reminder_webhook_payload_template", "").strip()
            if not intg_id:
                webhook_error = "No webhook integration selected"
            else:
                intg = next(
                    (i for i in load_integrations()
                     if i.get("id") == intg_id and i.get("base_url")),
                    None,
                )
                if not intg:
                    webhook_error = f"Integration {intg_id!r} not found or missing base URL"
                else:
                    # Fall back to a built-in default for known presets so
                    # users don't have to configure a template for standard
                    # services like Discord.
                    if not template:
                        template = _PRESET_TEMPLATE_DEFAULTS.get(intg.get("preset", ""), "")
                    if not template:
                        webhook_error = "No payload template configured"
                    else:
                        # Render template: JSON-escape the values so the result
                        # is always valid JSON regardless of special characters.
                        # dumps() returns `"value"` — strip outer quotes.
                        msg = (synthesis or note_body or title or "Reminder")[:4000]
                        _t = _wjson.dumps(title or "Reminder")[1:-1]
                        _m = _wjson.dumps(msg)[1:-1]
                        rendered = template.replace("{{title}}", _t).replace("{{message}}", _m)
                        hdrs = {"Content-Type": "application/json"}
                        api_key = intg.get("api_key", "")
                        auth_type = (intg.get("auth_type") or "none").lower()
                        if api_key:
                            if auth_type == "bearer":
                                hdrs["Authorization"] = f"Bearer {api_key}"
                            elif auth_type == "header":
                                hdrs[intg.get("auth_header") or "Authorization"] = api_key
                        url = intg["base_url"].rstrip("/")
                        # SSRF guard — matches the pattern used by webhook_routes,
                        # CalDAV, search, and embeddings. Blocks link-local / metadata
                        # addresses (169.254.x.x) by default; set
                        # REMINDER_WEBHOOK_BLOCK_PRIVATE_IPS=true to also block
                        # RFC-1918 ranges for locked-down deployments.
                        import os as _os
                        from src.url_safety import check_outbound_url as _chk
                        _block = _os.getenv("REMINDER_WEBHOOK_BLOCK_PRIVATE_IPS", "false").lower() == "true"
                        _ok, _reason = _chk(url, block_private=_block)
                        if not _ok:
                            webhook_error = f"Webhook URL rejected: {_reason}"
                        else:
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                if not _claim_still_active():
                                    webhook_error = "Reminder delivery was cancelled"
                                else:
                                    resp = await client.post(url, content=rendered.encode(), headers=hdrs)
                                    webhook_sent = resp.is_success
                                    if not webhook_sent:
                                        webhook_error = f"Webhook returned HTTP {resp.status_code}"
        except Exception as e:
            webhook_error = str(e) or e.__class__.__name__
            logger.warning(f"Reminder webhook send failed: {e}")

    ntfy_sent = False
    ntfy_error = ""
    if channel == "ntfy" and not mirror_retry_only:
        try:
            from src.integrations import load_integrations
            import httpx
            intg = next(
                (i for i in load_integrations()
                 if i.get("preset") == "ntfy" and i.get("enabled", True) and i.get("base_url")),
                None,
            )
            if intg:
                base = intg["base_url"].rstrip("/")
                topic = settings.get("reminder_ntfy_topic") or "reminders"
                ntfy_body = synthesis or note_body or title
                hdrs = {"Title": title or "Reminder", "Priority": "high", "Tags": "bell"}
                api_key = intg.get("api_key", "")
                if api_key:
                    hdrs["Authorization"] = f"Bearer {api_key}"
                # SSRF guard — same check (and env knob) as the webhook branch
                # above: link-local / metadata addresses are always rejected;
                # REMINDER_WEBHOOK_BLOCK_PRIVATE_IPS=true also blocks RFC-1918
                # so a ntfy base_url can't be pointed at internal services.
                import os as _os
                from src.url_safety import check_outbound_url as _chk
                _block = _os.getenv("REMINDER_WEBHOOK_BLOCK_PRIVATE_IPS", "false").lower() == "true"
                _ok, _reason = _chk(f"{base}/{topic}", block_private=_block)
                if not _ok:
                    ntfy_error = f"ntfy URL rejected: {_reason}"
                else:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        if not _claim_still_active():
                            ntfy_error = "Reminder delivery was cancelled"
                        else:
                            resp = await client.post(f"{base}/{topic}", content=ntfy_body, headers=hdrs)
                            ntfy_sent = resp.is_success
                            if not ntfy_sent:
                                ntfy_error = f"ntfy returned HTTP {resp.status_code}"
            else:
                ntfy_error = "No enabled ntfy integration"
        except Exception as e:
            ntfy_error = str(e) or e.__class__.__name__
            logger.warning(f"Reminder ntfy send failed: {e}")

    # Telegram — fires when it's the primary channel OR when the mirror flag
    # is on (reminder_telegram_mirror), so the Telegram bridge doubles as a
    # notification layer on top of whatever primary channel is configured.
    telegram_sent = False
    telegram_error = ""
    if channel == "telegram" or _tg_mirror:
        try:
            from src.telegram_bot import load_telegram_config, send_telegram_message, telegram_chat_ids_for_owner
            _tg = load_telegram_config()
            if not (_tg.enabled and _tg.bot_token):
                telegram_error = "Telegram bridge disabled or bot token missing"
            else:
                _explicit = str(settings.get("reminder_telegram_chat_id") or "").strip()
                if _explicit:
                    _owner_chats = set(telegram_chat_ids_for_owner(_tg, owner))
                    _tg_chats = [
                        c.strip() for c in _explicit.replace(" ", ",").split(",")
                        if c.strip() and c.strip() in _owner_chats
                    ]
                else:
                    _tg_chats = telegram_chat_ids_for_owner(_tg, owner)
                if not _tg_chats:
                    telegram_error = "No Telegram chat target (set reminder_telegram_chat_id or allow a chat)"
                else:
                    _tg_title = (title or "Reminder").strip()
                    _tg_msg = "\n".join(
                        part for part in (
                            f"🔔 {_tg_title}",
                            (synthesis or "").strip(),
                            (note_body or "").strip(),
                        ) if part
                    )[:4000]
                    _tg_ok = 0
                    _tg_failures = 0
                    for _chat in _tg_chats:
                        if not _claim_still_active():
                            _tg_failures += 1
                            telegram_error = "Reminder delivery was cancelled"
                            continue
                        _recipient_claim = None
                        _recipient_channel = ""
                        # The parent claim protects the occurrence as a whole;
                        # recipient claims remember which linked chats already
                        # succeeded so a partial outage retries only failures.
                        if (
                            claim_path is not None
                            and cache_key
                        ):
                            try:
                                import hashlib as _hashlib
                                from src.reminder_delivery_claims import claim_reminder_delivery

                                _recipient_role = (
                                    "telegram-recipient:"
                                    if str(channel).strip().lower() == "telegram"
                                    else "telegram-mirror-recipient:"
                                )
                                _recipient_channel = (
                                    _recipient_role
                                    + _hashlib.sha256(str(_chat).encode("utf-8")).hexdigest()[:24]
                                )
                                _recipient_claim = claim_reminder_delivery(
                                    claim_path,
                                    owner=owner,
                                    note_id=cache_key,
                                    occurrence=str(occurrence or ""),
                                    channel=_recipient_channel,
                                )
                                if not _recipient_claim.acquired:
                                    if _recipient_claim.reason == "delivered":
                                        _tg_ok += 1
                                    else:
                                        _tg_failures += 1
                                    continue
                            except Exception as _claim_error:
                                _tg_failures += 1
                                telegram_error = "Telegram recipient delivery state is unavailable"
                                logger.warning(
                                    "Reminder telegram recipient claim failed: %s",
                                    _claim_error,
                                )
                                continue
                        try:
                            # Cancellation can land after the per-recipient
                            # claim is created. Revalidate both obligations at
                            # the last possible point before the irreversible
                            # Telegram request. The recipient check is also
                            # required for mirror-only retries, where the
                            # already-delivered parent has no active token.
                            _recipient_claim_active = True
                            if _recipient_claim is not None and _recipient_claim.token:
                                from src.reminder_delivery_claims import reminder_claim_is_active

                                _recipient_claim_active = reminder_claim_is_active(
                                    claim_path,
                                    owner=owner,
                                    note_id=cache_key,
                                    occurrence=str(occurrence or ""),
                                    channel=_recipient_channel,
                                    token=_recipient_claim.token,
                                )
                            if not _claim_still_active() or not _recipient_claim_active:
                                _tg_failures += 1
                                telegram_error = "Reminder delivery was cancelled"
                                continue
                            await send_telegram_message(_tg.bot_token, _chat, _tg_msg)
                            _tg_ok += 1
                            if _recipient_claim is not None and _recipient_claim.token:
                                from src.reminder_delivery_claims import acknowledge_reminder_delivery

                                acknowledge_reminder_delivery(
                                    claim_path,
                                    owner=owner,
                                    note_id=cache_key,
                                    occurrence=str(occurrence or ""),
                                    channel=_recipient_channel,
                                    token=_recipient_claim.token,
                                )
                        except Exception as _te:
                            _tg_failures += 1
                            telegram_error = str(_te) or _te.__class__.__name__
                            logger.warning("Reminder telegram send failed for chat %s: %s", _chat, _te)
                            if _recipient_claim is not None and _recipient_claim.token:
                                try:
                                    from src.reminder_delivery_claims import fail_reminder_delivery

                                    fail_reminder_delivery(
                                        claim_path,
                                        owner=owner,
                                        note_id=cache_key,
                                        occurrence=str(occurrence or ""),
                                        channel=_recipient_channel,
                                        token=_recipient_claim.token,
                                        error="telegram_recipient_delivery_failed",
                                    )
                                except Exception:
                                    logger.exception("Telegram recipient failure state could not be saved")
                    telegram_sent = bool(_tg_chats) and _tg_ok == len(_tg_chats)
                    if not telegram_sent and _tg_failures:
                        telegram_error = (
                            f"Telegram reached {_tg_ok} of {len(_tg_chats)} linked chats; "
                            f"{_tg_failures} will retry"
                        )
        except Exception as e:
            telegram_error = str(e) or e.__class__.__name__
            logger.warning(f"Reminder telegram send failed: {e}")
        if telegram_error and not telegram_sent:
            logger.warning("Reminder telegram not delivered: %s", telegram_error)

    # In-app browser notification ALWAYS fires (regardless of channel). The
    # frontend polls `/api/tasks/notifications` and turns any entry with a
    # `body` into a real `Notification(...)` — same surface as task-success
    # popups. Lets the user see reminders inside the app even when the
    # primary channel is email/ntfy and the tab is open.
    browser_sent = False
    browser_notification_id = ""
    show_browser = False
    # The default browser channel is always persisted first. A foreground
    # caller can display it immediately and ack the returned ID; if that tab
    # disappears, the non-destructive task poller will still recover it.
    should_queue_browser = bool(queue_browser or channel == "browser" or occurrence)
    if (
        should_queue_browser
        and not mirror_retry_only
        and _scheduler_ref is not None
        and _claim_still_active()
    ):
        try:
            import hashlib as _hashlib
            _browser_occurrence = str(occurrence or "").strip()
            if not _browser_occurrence:
                _browser_occurrence = _hashlib.sha256(
                    f"{title}\0{note_body}".encode("utf-8", errors="replace")
                ).hexdigest()
            # A browser mirror for a failed external channel and a later
            # browser-primary delivery are different obligations. Keeping the
            # role in the key prevents an already-ACKed mirror from swallowing
            # the new primary outbox row and deadlocking its durable claim.
            _browser_role = "primary" if str(channel).strip().lower() == "browser" else "mirror"
            _browser_dedupe_key = f"reminder:{cache_key}:{_browser_occurrence}:{_browser_role}"
            _browser_item = _scheduler_ref.add_notification(
                task_name=title or "Reminder",
                status="success",
                task_id=f"reminder-{note_id}",
                owner=owner or None,
                body=(synthesis or note_body or title or "").strip()[:500] or "Reminder",
                dedupe_key=_browser_dedupe_key,
                # Link mirrors too (without a token) so rescheduling can cancel
                # only the stale occurrence. Only browser-primary rows can ACK
                # a durable delivery claim.
                reminder_claim=(
                    {
                        "owner": owner,
                        "note_id": cache_key,
                        "occurrence": str(occurrence or ""),
                        "channel": "browser" if str(channel).strip().lower() == "browser" else "",
                        "token": claim_token if str(channel).strip().lower() == "browser" else "",
                    }
                    if cache_key
                    else None
                ),
            )
            browser_sent = not bool((_browser_item or {}).get("_outbox_cancelled", False))
            browser_notification_id = str((_browser_item or {}).get("id") or "")
            show_browser = bool(
                browser_sent
                and (
                (_browser_item or {}).get("_outbox_created", True)
                or (_browser_item or {}).get("_outbox_pending", False)
                )
            )
        except Exception as _e:
            logger.warning("dispatch_reminder: durable browser notification enqueue failed: %s", _e)

    primary_delivered = primary_already_delivered or reminder_delivery_succeeded({
        "channel": channel,
        "telegram_fallback": telegram_fallback,
        "email_sent": email_sent,
        "ntfy_sent": ntfy_sent,
        "webhook_sent": webhook_sent,
        "telegram_sent": telegram_sent,
        # A durable browser enqueue is pending, not delivered, until the client
        # explicitly acknowledges that outbox row.
        "browser_sent": browser_sent and str(channel).strip().lower() != "browser",
    })
    # Enabling a mirror makes it an explicit delivery obligation. The primary
    # claim may be acknowledged independently, while recurrence waits until
    # every linked mirror chat has also succeeded.
    delivered = bool(primary_delivered and (not _tg_mirror or telegram_sent))

    # Complete the durable claim before updating the compatibility JSON cache.
    # A failed channel is persisted with retry backoff; it is never converted
    # into an acknowledgement merely because the in-app mirror was queued.
    if claim_path is not None and claim_token:
        try:
            if str(channel).strip().lower() == "browser" and browser_sent:
                from src.reminder_delivery_claims import await_browser_ack
                if not await_browser_ack(
                    claim_path,
                    owner=owner,
                    note_id=cache_key,
                    occurrence=str(occurrence or ""),
                    channel="browser",
                    token=claim_token,
                ):
                    logger.error("dispatch_reminder: browser claim could not enter ack-pending state for %s", cache_key)
            elif primary_delivered:
                from src.reminder_delivery_claims import acknowledge_reminder_delivery
                if not acknowledge_reminder_delivery(
                    claim_path,
                    owner=owner,
                    note_id=cache_key,
                    occurrence=str(occurrence or ""),
                    channel=str(channel or "browser"),
                    token=claim_token,
                ):
                    logger.error("dispatch_reminder: lost durable claim acknowledgement for %s", cache_key)
            else:
                # Cancellation races are expected when a user archives or
                # reschedules while an attempt is preparing. The tombstone has
                # already made the claim inactive, so do not report a false
                # ledger failure or convert it into retry backoff.
                if _claim_still_active():
                    from src.reminder_delivery_claims import fail_reminder_delivery
                    _delivery_error = (
                        telegram_error or email_error or ntfy_error or webhook_error
                        or "required_channel_not_delivered"
                    )
                    if not fail_reminder_delivery(
                        claim_path,
                        owner=owner,
                        note_id=cache_key,
                        occurrence=str(occurrence or ""),
                        channel=str(channel or "browser"),
                        token=claim_token,
                        error=str(_delivery_error),
                    ):
                        logger.error("dispatch_reminder: lost durable failure acknowledgement for %s", cache_key)
        except Exception:
            # The external side effect may already have happened. Preserve the
            # result and legacy atomic cache, but make the state failure loud.
            logger.exception("dispatch_reminder: durable claim acknowledgement failed for %s", cache_key)

    # Dedupe across paths: write to the same cache file `action_ping_notes`
    # reads, so the background scanner's REPING_MIN window suppresses a
    # second send for the same note within 25 min. Without this, a note
    # whose due_date fires while the user has the app open got TWO emails
    # (frontend-fired here + background-fired by ping_notes 0–5 min later).
    # Cache only a successful REQUIRED channel. The in-app browser mirror must
    # not acknowledge a failed Telegram/email/ntfy/webhook delivery; otherwise
    # the scanner suppresses the retry and recurring notes advance despite the
    # user never receiving the channel they selected.
    if primary_delivered and note_id:
        try:
            import json as _json
            from datetime import datetime as _dt, timezone as _tz
            from pathlib import Path as _P
            # Per-owner cache so the scanner's prune step on user A's run
            # doesn't drop user B's just-fired entry (review C4).
            _STATE = cache_path
            if _STATE is None:
                _slug = owner_storage_key(owner)
                _STATE = _P(DATA_DIR) / f"note_pings_{_slug}.json"
            _STATE.parent.mkdir(parents=True, exist_ok=True)
            try:
                _cache = cache or (_json.loads(_STATE.read_text(encoding="utf-8")) if _STATE.exists() else {})
            except Exception:
                _cache = {}
            sent_channel = str(channel or "browser")
            _cache[cache_key or str(note_id)] = {
                "at": _dt.now(_tz.utc).isoformat(),
                "channel": sent_channel,
                "occurrence": str(occurrence or ""),
                "mirror_complete": bool(not _tg_mirror or telegram_sent),
            }
            from core.atomic_io import atomic_write_json as _atomic_write_json
            _atomic_write_json(str(_STATE), _cache)
        except Exception as _e:
            logger.debug(f"dispatch_reminder: cache write failed: {_e}")

    return {
        "channel": channel,
        "telegram_fallback": telegram_fallback,
        "synthesis": synthesis,
        "email_sent": email_sent,
        "email_error": email_error,
        "ntfy_sent": ntfy_sent,
        "ntfy_error": ntfy_error,
        "webhook_sent": webhook_sent,
        "webhook_error": webhook_error,
        "telegram_sent": telegram_sent,
        "telegram_error": telegram_error,
        "telegram_mirror_unavailable": telegram_mirror_unavailable,
        "browser_sent": browser_sent,
        "browser_notification_id": browser_notification_id,
        "show_browser": show_browser,
        "delivered": delivered,
        "acknowledged": delivered,
        "deferred": bool(str(channel).strip().lower() == "browser" and browser_sent and not delivered),
        "suppression_reason": (
            "browser_ack_pending"
            if str(channel).strip().lower() == "browser" and browser_sent and not delivered
            else ""
        ),
    }


def reminder_delivery_succeeded(result: dict) -> bool:
    """Whether the user's selected primary channel actually delivered."""
    channel = str((result or {}).get("channel") or "browser").strip().lower()
    field = {
        "email": "email_sent",
        "ntfy": "ntfy_sent",
        "webhook": "webhook_sent",
        "telegram": "telegram_sent",
        "browser": "browser_sent",
    }.get(channel, "browser_sent")
    return bool((result or {}).get(field))


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------

def setup_note_routes(task_scheduler=None):
    # Expose the scheduler to module-level `dispatch_reminder` so reminders
    # can also push to the in-app notification queue (the polling system
    # turns each entry into a real browser Notification + the existing
    # tasks-tab badge / dot system).
    global _scheduler_ref
    _scheduler_ref = task_scheduler

    router = APIRouter(prefix="/api/notes", tags=["notes"])

    def _owner(request: Request) -> str:
        # require_user, not bare get_current_user: a request that reaches
        # these owner-scoped routes with NO identity (auth-middleware
        # regression, SSRF from a sibling service) must fail closed (401)
        # when auth is configured — not be treated as the single-user mode
        # and handed blanket access to every account's notes. The documented
        # anonymous modes (AUTH_ENABLED=false, LOCALHOST_BYPASS on loopback,
        # unconfigured first-run) still resolve to None, the single-user
        # path. fire_reminder below already gated this way; the CRUD routes
        # did not.
        admitted = require_user(request)
        return resolved_request_owner(request, admitted_user=admitted)

    def _allows_legacy(request: Request) -> bool:
        return allows_legacy_null_owner(request)

    def _scope_query(query, request: Request, owner: str):
        if _allows_legacy(request):
            return query.filter(
                (Note.owner == owner)
                | (Note.owner == DEFAULT_LOCAL_OWNER)
                | (Note.owner == None)  # noqa: E711
            )
        return query.filter(Note.owner == owner)

    def _can_access(note: Note, request: Request, owner: str) -> bool:
        if note.owner == owner:
            return True
        return _allows_legacy(request) and note.owner in (None, DEFAULT_LOCAL_OWNER)

    def _is_admin_or_single_user(request: Request, user: str | None) -> bool:
        if user == INTERNAL_TOOL_USER:
            return True
        if not user:
            # require_user() already admitted this request, which only happens
            # for auth-disabled, loopback-bypass, or unconfigured single-user
            # modes. There is no separate non-admin account boundary there.
            return True
        try:
            from src.auth_runtime import get_auth_manager
            auth_mgr = (
                getattr(request.app.state, "auth_manager", None)
                or get_auth_manager()
            )
            if not getattr(auth_mgr, "is_configured", True):
                return True
            return bool(auth_mgr.is_admin(user))
        except Exception:
            return False

    # --- LIST ---
    @router.get("")
    def list_notes(
        request: Request,
        archived: Optional[bool] = None,
        label: Optional[str] = None,
        note_type: Optional[str] = None,
    ):
        user = _owner(request)
        db = SessionLocal()
        try:
            q = _scope_query(db.query(Note), request, user)
            if archived is not None:
                q = q.filter(Note.archived == archived)
            else:
                q = q.filter(Note.archived == False)
            if label:
                q = q.filter(Note.label == label)
            if note_type:
                q = q.filter(Note.note_type == note_type)
            # Archived view: most recently archived first. Active view: pin + manual order.
            if archived is True:
                notes = q.order_by(Note.updated_at.desc()).all()
            else:
                notes = q.order_by(Note.pinned.desc(), Note.sort_order.asc(), Note.updated_at.desc()).all()
            return {"notes": [_note_to_dict(n) for n in notes]}
        finally:
            db.close()

    # --- CREATE ---
    @router.post("")
    def create_note(request: Request, body: NoteCreate):
        user = _owner(request)
        db = SessionLocal()
        try:
            if body.client_timezone:
                ensure_notification_timezone(user, body.client_timezone)
            normalized_due_date = normalize_notification_due_date(user, body.due_date)
            created_items = (
                normalize_created_items(
                    body.items,
                    repeat=body.repeat,
                    due_date=normalized_due_date,
                )
                if body.items is not None
                else None
            )
            note = Note(
                id=str(uuid.uuid4()),
                owner=user,
                title=body.title,
                content=body.content,
                items=json.dumps(created_items) if created_items is not None else None,
                note_type=body.note_type,
                color=body.color,
                label=body.label,
                pinned=body.pinned,
                due_date=normalized_due_date,
                source=body.source,
                session_id=body.session_id,
                image_url=body.image_url,
                repeat=body.repeat or "none",
                sort_order=body.sort_order if body.sort_order is not None else 0,
            )
            db.add(note)
            db.commit()
            db.refresh(note)
            return _note_to_dict(note)
        finally:
            db.close()

    # --- GET ONE ---
    @router.get("/{note_id}")
    def get_note(request: Request, note_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            note = db.query(Note).filter(Note.id == note_id).first()
            if not note:
                raise HTTPException(404, "Note not found")
            # SECURITY: strict ownership — previously `note.owner and note.owner != user`
            # let any user touch a row whose owner field was null/empty.
            if not _can_access(note, request, user):
                raise HTTPException(404, "Note not found")
            return _note_to_dict(note)
        finally:
            db.close()

    # --- UPDATE ---
    @router.put("/{note_id}")
    def update_note(request: Request, note_id: str, body: NoteUpdate):
        user = _owner(request)
        db = SessionLocal()
        try:
            if body.client_timezone:
                ensure_notification_timezone(user, body.client_timezone)
            note = db.query(Note).filter(Note.id == note_id).first()
            if not note:
                raise HTTPException(404, "Note not found")
            # SECURITY: strict ownership — previously `note.owner and note.owner != user`
            # let any user touch a row whose owner field was null/empty.
            if not _can_access(note, request, user):
                raise HTTPException(404, "Note not found")

            old_items: list = []
            if note.items:
                try:
                    parsed_items = json.loads(note.items)
                    if isinstance(parsed_items, list):
                        old_items = parsed_items
                except (json.JSONDecodeError, TypeError):
                    pass
            next_items: list | None = None
            canonical_old_items = old_items
            previous_repeat = note.repeat
            previous_due_date = note.due_date
            previous_fully_done = completion_items_fully_done(note.note_type, old_items)

            if body.title is not None:
                note.title = body.title
            if body.content is not None:
                note.content = body.content
            if body.items is not None:
                canonical_old_items, next_items = normalize_updated_items(
                    old_items,
                    body.items,
                    repeat=previous_repeat,
                    due_date=previous_due_date,
                )
                note.items = json.dumps(next_items)
                flag_modified(note, "items")
            if body.note_type is not None:
                note.note_type = body.note_type
            if body.color is not None:
                note.color = body.color
            if body.label is not None:
                note.label = body.label
            if body.pinned is not None:
                note.pinned = body.pinned
            if body.archived is not None:
                note.archived = body.archived
            fields_set = getattr(body, "model_fields_set", getattr(body, "__fields_set__", set()))
            if "due_date" in fields_set:
                note.due_date = normalize_notification_due_date(user, body.due_date)
            if body.image_url is not None:
                note.image_url = body.image_url
            if body.repeat is not None:
                note.repeat = body.repeat
            if body.sort_order is not None:
                note.sort_order = body.sort_order
            if body.agent_session_id is not None:
                note.agent_session_id = body.agent_session_id

            if next_items is not None and str(note.note_type or "").lower() in _COMPLETION_NOTE_TYPES:
                award_note_item_completions(
                    db,
                    note=note,
                    # Legacy null/default-owned notes keep their storage scope,
                    # but completion evidence belongs to the resolved profile.
                    owner=user,
                    old_items=canonical_old_items,
                    new_items=next_items,
                )
                # The award helper stamps the first completion time into the
                # item payload after the initial assignment above.
                note.items = json.dumps(next_items)
                flag_modified(note, "items")

            current_fully_done = completion_items_fully_done(
                note.note_type,
                next_items if next_items is not None else note.items,
            )
            cancel_pending = bool(
                (body.archived is True)
                or ("due_date" in fields_set and note.due_date != previous_due_date)
                or (body.repeat is not None and note.repeat != previous_repeat)
                or (
                    next_items is not None
                    and current_fully_done
                )
            )
            rearm_pending = bool(
                not note.archived
                and note.due_date
                and (
                    body.archived is False
                    or ("due_date" in fields_set and note.due_date != previous_due_date)
                    or (body.repeat is not None and note.repeat != previous_repeat)
                    or (previous_fully_done and not current_fully_done)
                )
            )
            cancel_occurrence = None if body.archived is True else previous_due_date
            if cancel_pending:
                _cancel_pending_note_reminder(
                    user,
                    note.id,
                    occurrence=cancel_occurrence,
                )
            rearm_occurrence = None if body.archived is False else note.due_date
            if rearm_pending:
                _rearm_pending_note_reminder(
                    user,
                    note.id,
                    occurrence=rearm_occurrence,
                )
            try:
                db.commit()
            except Exception:
                if rearm_pending:
                    _cancel_pending_note_reminder(
                        user,
                        note.id,
                        occurrence=rearm_occurrence,
                    )
                if cancel_pending:
                    _rearm_pending_note_reminder(
                        user,
                        note.id,
                        occurrence=cancel_occurrence,
                    )
                raise
            db.refresh(note)
            return _note_to_dict(note)
        finally:
            db.close()

    # --- DELETE ---
    @router.delete("/{note_id}")
    def delete_note(request: Request, note_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            note = db.query(Note).filter(Note.id == note_id).first()
            if not note:
                raise HTTPException(404, "Note not found")
            # SECURITY: strict ownership — previously `note.owner and note.owner != user`
            # let any user touch a row whose owner field was null/empty.
            if not _can_access(note, request, user):
                raise HTTPException(404, "Note not found")
            _cancel_pending_note_reminder(user, note_id)
            db.delete(note)
            try:
                db.commit()
            except Exception:
                _rearm_pending_note_reminder(user, note_id)
                raise
            return {"ok": True}
        finally:
            db.close()

    # --- TOGGLE PIN ---
    @router.post("/{note_id}/pin")
    def toggle_pin(request: Request, note_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            note = db.query(Note).filter(Note.id == note_id).first()
            if not note:
                raise HTTPException(404, "Note not found")
            # SECURITY: strict ownership — previously `note.owner and note.owner != user`
            # let any user touch a row whose owner field was null/empty.
            if not _can_access(note, request, user):
                raise HTTPException(404, "Note not found")
            note.pinned = not note.pinned
            db.commit()
            return {"ok": True, "pinned": note.pinned}
        finally:
            db.close()

    # --- TOGGLE ARCHIVE ---
    @router.post("/{note_id}/archive")
    def toggle_archive(request: Request, note_id: str):
        user = _owner(request)
        db = SessionLocal()
        try:
            note = db.query(Note).filter(Note.id == note_id).first()
            if not note:
                raise HTTPException(404, "Note not found")
            # SECURITY: strict ownership — previously `note.owner and note.owner != user`
            # let any user touch a row whose owner field was null/empty.
            if not _can_access(note, request, user):
                raise HTTPException(404, "Note not found")
            note.archived = not note.archived
            if note.archived:
                _cancel_pending_note_reminder(
                    user,
                    note.id,
                )
            elif note.due_date:
                _rearm_pending_note_reminder(user, note.id)
            try:
                db.commit()
            except Exception:
                if note.archived:
                    _rearm_pending_note_reminder(user, note.id)
                elif note.due_date:
                    _cancel_pending_note_reminder(user, note.id)
                raise
            return {"ok": True, "archived": note.archived}
        finally:
            db.close()

    # --- TOGGLE CHECKLIST ITEM ---
    @router.post("/{note_id}/items/{index}/toggle")
    def toggle_item(request: Request, note_id: str, index: int):
        user = _owner(request)
        db = SessionLocal()
        try:
            note = db.query(Note).filter(Note.id == note_id).first()
            if not note:
                raise HTTPException(404, "Note not found")
            # SECURITY: strict ownership — previously `note.owner and note.owner != user`
            # let any user touch a row whose owner field was null/empty.
            if not _can_access(note, request, user):
                raise HTTPException(404, "Note not found")
            if not note.items:
                raise HTTPException(400, "Note has no checklist items")
            old_items = json.loads(note.items)
            if index < 0 or index >= len(old_items):
                raise HTTPException(400, f"Item index {index} out of range")
            submitted = public_note_items(old_items)
            submitted[index]["done"] = not submitted[index].get("done", False)
            canonical_old, items = normalize_updated_items(
                old_items,
                submitted,
                repeat=note.repeat,
                due_date=note.due_date,
            )
            if str(note.note_type or "").lower() in _COMPLETION_NOTE_TYPES:
                award_note_item_completions(
                    db,
                    note=note,
                    owner=user,
                    old_items=canonical_old,
                    new_items=items,
                )
            note.items = json.dumps(items)
            flag_modified(note, "items")
            completed_now = completion_items_fully_done(note.note_type, items)
            if completed_now:
                _cancel_pending_note_reminder(
                    user,
                    note.id,
                    occurrence=note.due_date,
                )
            elif note.due_date:
                _rearm_pending_note_reminder(
                    user,
                    note.id,
                    occurrence=note.due_date,
                )
            try:
                db.commit()
            except Exception:
                if completed_now:
                    _rearm_pending_note_reminder(
                        user,
                        note.id,
                        occurrence=note.due_date,
                    )
                elif note.due_date:
                    _cancel_pending_note_reminder(
                        user,
                        note.id,
                        occurrence=note.due_date,
                    )
                raise
            return {"ok": True, "items": public_note_items(items)}
        finally:
            db.close()

    # --- ADVANCE RECURRING CHECKLIST ---
    @router.post("/{note_id}/advance-recurrence")
    def advance_recurrence(request: Request, note_id: str):
        """Advance one completed recurring cycle using only server state.

        The client cannot submit a due date or cycle id.  This prevents a
        mutable reminder date from minting a fresh XP key while still letting
        a legitimately completed habit begin its next server-owned cycle.
        """

        user = _owner(request)
        db = SessionLocal()
        try:
            note = db.query(Note).filter(Note.id == note_id).first()
            if not note or not _can_access(note, request, user):
                raise HTTPException(404, "Note not found")
            if note.archived:
                raise HTTPException(409, "Restore the note before advancing it")
            if str(note.repeat or "none").strip().lower() == "none" or not note.due_date:
                raise HTTPException(409, "Note is not a scheduled recurring checklist")
            try:
                items = json.loads(note.items or "[]")
            except (json.JSONDecodeError, TypeError):
                items = []
            real_items = [item for item in items if isinstance(item, dict)]
            checklist_like = str(note.note_type or "").lower() in _COMPLETION_NOTE_TYPES
            if checklist_like and (
                not real_items
                or any(
                    not bool(item.get("done") or item.get("checked"))
                    for item in real_items
                )
            ):
                raise HTTPException(409, "Complete the current checklist before advancing it")
            if not recurring_occurrence_is_due(
                items_json=note.items,
                repeat=note.repeat,
                due_date=note.due_date,
                tz_name=str(load_notification_preferences(user).get("timezone") or "UTC"),
            ):
                raise HTTPException(409, "The next recurrence is not due yet")
            from src.builtin_actions import _advance_recurring_due

            reminder_timezone = str(load_notification_preferences(user).get("timezone") or "UTC")
            next_due = _advance_recurring_due(
                note.due_date,
                note.repeat,
                tz_name=reminder_timezone,
            )
            if not next_due:
                raise HTTPException(409, "Could not calculate the next recurrence")
            old_due = note.due_date
            old_items = note.items
            old_repeat = note.repeat
            advanced_items, advanced_due = recurring_advance_values(
                items_json=old_items,
                repeat=old_repeat,
                due_date=old_due,
                next_due_date=next_due,
            )
            updated = (
                db.query(Note)
                .filter(
                    Note.id == note.id,
                    Note.due_date == old_due,
                    Note.items == old_items,
                    Note.repeat == old_repeat,
                    Note.archived.is_(False),
                )
                .update(
                    {
                        Note.items: advanced_items,
                        Note.due_date: advanced_due,
                        Note.updated_at: utcnow_naive(),
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                db.rollback()
                raise HTTPException(409, "This recurrence was already advanced")
            _cancel_pending_note_reminder(user, note.id, occurrence=old_due)
            _rearm_pending_note_reminder(
                user,
                note.id,
                occurrence=advanced_due,
            )
            try:
                db.commit()
            except Exception:
                _cancel_pending_note_reminder(
                    user,
                    note.id,
                    occurrence=advanced_due,
                )
                _rearm_pending_note_reminder(user, note.id, occurrence=old_due)
                raise
            db.refresh(note)
            return _note_to_dict(note)
        finally:
            db.close()

    # --- FIRE REMINDER ---
    @router.post("/fire-reminder")
    async def fire_reminder(request: Request):
        """Dispatch a reminder according to user settings.

        Called by the frontend when a reminder fires. Optionally generates an
        LLM synthesis line and/or sends an email through configured SMTP.
        Returns {synthesis, email_sent}.
        """
        # Gate against anonymous callers — LLM synthesis can burn tokens.
        user = require_user(request)
        body = await request.json()
        note_id = str(body.get("note_id") or "").strip()
        if not note_id:
            raise HTTPException(400, "note_id required")

        caller = _owner(request)
        is_test = note_id.startswith("test-")
        is_admin = _is_admin_or_single_user(request, user)
        _override: dict = {}
        if is_test:
            if not is_admin:
                raise HTTPException(403, "Admin only")
            title = (body.get("title") or "Test Reminder").strip() or "Test Reminder"
            note_body = (body.get("body") or "").strip()
            # Optional overrides let the admin settings test button pass the
            # current UI values directly so it never races a pending save.
            if body.get("channel"):
                _override["reminder_channel"] = body["channel"]
            if body.get("webhook_integration_id"):
                _override["reminder_webhook_integration_id"] = body["webhook_integration_id"]
            if body.get("webhook_payload_template"):
                _override["reminder_webhook_payload_template"] = body["webhook_payload_template"]
            # Mirror the in-UI AI Synthesis toggle + persona so the test
            # actually exercises the synthesis path before/without a Save.
            if "llm_synthesis" in body:
                _override["reminder_llm_synthesis"] = bool(body["llm_synthesis"])
            if "llm_persona" in body:
                _override["reminder_llm_persona"] = str(body["llm_persona"] or "")
        else:
            db = SessionLocal()
            try:
                note = db.query(Note).filter(Note.id == note_id).first()
                if not note:
                    raise HTTPException(404, "Note not found")
                if not _can_access(note, request, caller):
                    raise HTTPException(404, "Note not found")
                current_note = _note_to_dict(note)
                if note.archived:
                    _cancel_pending_note_reminder(
                        caller,
                        note.id,
                    )
                    return {
                        "suppressed": True,
                        "acknowledged": True,
                        "show_browser": False,
                        "suppression_reason": "note_archived",
                        "current_note": current_note,
                    }
                if completion_items_fully_done(note.note_type, note.items):
                    _cancel_pending_note_reminder(
                        caller,
                        note.id,
                        occurrence=note.due_date,
                    )
                    return {
                        "suppressed": True,
                        "acknowledged": True,
                        "show_browser": False,
                        "suppression_reason": "note_completed",
                        "current_note": current_note,
                    }
                title, note_body = _reminder_text_from_note(note)
                occurrence = str(getattr(note, "due_date", None) or "")
                canonical_due = normalize_notification_due_date(caller, occurrence)
                try:
                    if "T" not in str(canonical_due or ""):
                        raise ValueError("reminders require a timed due date")
                    parsed_due = datetime.fromisoformat(
                        str(canonical_due or "").replace("Z", "+00:00")
                    )
                    if parsed_due.tzinfo is None:
                        parsed_due = parsed_due.replace(tzinfo=timezone.utc)
                    due_in_future = parsed_due.astimezone(timezone.utc) > datetime.now(timezone.utc)
                except (TypeError, ValueError):
                    due_in_future = True
                if due_in_future:
                    return {
                        "deferred": True,
                        "acknowledged": False,
                        "show_browser": False,
                        "suppression_reason": "not_due_yet",
                        "current_note": current_note,
                    }
                note_label = str(getattr(note, "label", None) or "").strip().lower()
                note_type = str(getattr(note, "note_type", None) or "").strip().lower()
                topic = "calendar" if note_label == "calendar" else (
                    "todos" if note_type in {"todo", "checklist", "goal"} else "reminders"
                )
            finally:
                db.close()

        dispatch_kwargs = dict(
            title=title, note_body=note_body, note_id=note_id,
            owner=caller or "",
            # The dispatcher uses the stored occurrence to persist a browser
            # mirror while preserving this route's foreground-call contract.
            queue_browser=False,
            settings_override=_override or None,
        )
        if not is_test and occurrence:
            dispatch_kwargs["occurrence"] = occurrence
        if not is_test and topic != "reminders":
            dispatch_kwargs["topic"] = topic
        return await dispatch_reminder(**dispatch_kwargs)

    # --- REORDER NOTES ---
    @router.post("/reorder")
    async def reorder_notes(request: Request):
        """Update sort_order for a list of note IDs in the order provided."""
        user = _owner(request)
        body = await request.json()
        ids = body.get("ids", [])
        if not isinstance(ids, list):
            raise HTTPException(400, "ids must be a list")
        db = SessionLocal()
        try:
            for i, nid in enumerate(ids):
                note = _scope_query(
                    db.query(Note).filter(Note.id == nid), request, user
                ).first()
                if note:
                    note.sort_order = i
            db.commit()
            return {"ok": True, "count": len(ids)}
        finally:
            db.close()

    return router
