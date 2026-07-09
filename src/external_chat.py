"""Shared external chat dispatch helpers.

This module is for non-browser chat surfaces that need to send a normal
Restia chat turn without accepting provider credentials from that surface.
"""

from __future__ import annotations

import logging
import json
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from core.database import ModelEndpoint, Session as DBSession, SessionLocal
from core.models import ChatMessage
from src.endpoint_resolver import resolve_endpoint, resolve_endpoint_by_id

logger = logging.getLogger(__name__)


class ExternalChatError(RuntimeError):
    """Expected external-chat failure that can be shown to an integration user."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ExternalChatResult:
    response: str
    session_id: str
    model: str
    created_session: bool = False


def _persist_session_headers(session_id: str, headers: dict[str, str], owner: Optional[str]) -> None:
    if not headers:
        return
    db = SessionLocal()
    try:
        q = db.query(DBSession).filter(DBSession.id == session_id)
        if owner:
            q = q.filter(DBSession.owner == owner)
        row = q.first()
        if row is not None:
            row.headers = headers
            db.commit()
    except Exception:
        db.rollback()
        logger.warning("Failed to persist external chat session headers", exc_info=True)
    finally:
        db.close()


def _first_visible_endpoint(owner: Optional[str]) -> tuple[str, str, dict[str, str]] | None:
    db = SessionLocal()
    try:
        q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
        if owner:
            from src.auth_helpers import owner_filter

            q = owner_filter(q, ModelEndpoint, owner)
            endpoint = q.order_by(ModelEndpoint.owner.desc(), ModelEndpoint.created_at).first()
        else:
            endpoint = q.filter(ModelEndpoint.owner == None).order_by(ModelEndpoint.created_at).first()  # noqa: E711
        if not endpoint:
            return None
        return resolve_endpoint_by_id(endpoint.id, owner=owner)
    finally:
        db.close()


def resolve_external_chat_endpoint(owner: Optional[str]) -> tuple[str, str, dict[str, str]]:
    endpoint_url, model, headers = resolve_endpoint("default", owner=owner)
    if endpoint_url and model:
        return endpoint_url, model, headers or {}

    fallback = _first_visible_endpoint(owner)
    if fallback:
        endpoint_url, model, headers = fallback
        if endpoint_url and model:
            return endpoint_url, model, headers or {}

    raise ExternalChatError(
        "No configured chat model is available. Configure a default model endpoint in Restia Settings first.",
        status_code=400,
    )


def _message_context(sess) -> list[dict[str, Any]]:
    if hasattr(sess, "get_context_messages"):
        return sess.get_context_messages()
    return [{"role": m.role, "content": m.content} for m in getattr(sess, "history", [])]


async def _run_external_agent_turn(
    sess,
    *,
    session_id: str,
    owner: Optional[str],
) -> str:
    """Run the normal Restia agent loop for non-browser chat surfaces."""
    from src.agent_loop import stream_agent_loop
    from src.agent_tools import MAX_AGENT_ROUNDS as _DEFAULT_ROUNDS
    from src.endpoint_resolver import resolve_chat_fallback_candidates
    from src.settings import get_setting

    try:
        max_rounds = int(get_setting("agent_max_rounds", _DEFAULT_ROUNDS) or _DEFAULT_ROUNDS)
    except (TypeError, ValueError):
        max_rounds = _DEFAULT_ROUNDS
    max_rounds = max(1, min(max_rounds, 200))

    try:
        max_tool_calls = int(get_setting("agent_max_tool_calls", 0) or 0)
    except (TypeError, ValueError):
        max_tool_calls = 0

    try:
        fallbacks = resolve_chat_fallback_candidates(owner=owner)
    except Exception:
        fallbacks = []

    visible_chunks: list[str] = []
    async for event in stream_agent_loop(
        sess.endpoint_url,
        sess.model,
        _message_context(sess),
        headers=sess.headers,
        temperature=0.3,
        max_tokens=4096,
        max_rounds=max_rounds,
        max_tool_calls=max_tool_calls,
        session_id=session_id,
        owner=owner,
        fallbacks=fallbacks,
        workload="foreground",
    ):
        if not isinstance(event, str) or not event.startswith("data: "):
            continue
        payload = event[6:].strip()
        if payload == "[DONE]":
            break
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "delta" in data and not data.get("thinking"):
            visible_chunks.append(str(data.get("delta") or ""))

    reply = "".join(visible_chunks).strip()
    if not reply:
        raise ExternalChatError("The model returned an empty response", status_code=502)
    return reply


async def send_external_chat_message(
    session_manager,
    *,
    message: str,
    owner: Optional[str],
    session_id: Optional[str] = None,
    session_name: str = "External Chat",
    source: str = "external",
    webhook_manager=None,
) -> ExternalChatResult:
    text = (message or "").strip()
    if not text:
        raise ExternalChatError("Message is required", status_code=400)
    if not session_manager:
        raise ExternalChatError("Session manager is not available", status_code=500)

    sess = None
    created_session = False
    if session_id:
        try:
            sess = session_manager.get_session(session_id)
        except Exception:
            sess = None
        if sess is not None and owner and getattr(sess, "owner", None) != owner:
            raise ExternalChatError("Session not found", status_code=404)

    if sess is None:
        endpoint_url, model, headers = resolve_external_chat_endpoint(owner)
        session_id = str(uuid.uuid4())
        sess = session_manager.create_session(
            session_id=session_id,
            name=session_name,
            endpoint_url=endpoint_url,
            model=model,
            owner=owner,
        )
        sess.headers = headers or {}
        _persist_session_headers(session_id, sess.headers, owner)
        created_session = True
    else:
        if not getattr(sess, "endpoint_url", "") or not getattr(sess, "model", ""):
            raise ExternalChatError("Selected chat session has no model configured", status_code=400)
        try:
            from routes.chat_helpers import resolve_session_auth

            resolve_session_auth(sess, session_id, owner=owner)
        except Exception:
            logger.debug("Could not refresh external chat session auth", exc_info=True)

    user_meta = {"source": source}
    sess.add_message(ChatMessage("user", text, metadata=user_meta))

    reply = await _run_external_agent_turn(sess, session_id=session_id, owner=owner)
    sess.add_message(ChatMessage("assistant", reply, metadata={"source": source}))
    session_manager.save_sessions()

    if webhook_manager:
        try:
            webhook_manager.fire_and_forget(
                "chat.completed",
                {
                    "session_id": session_id,
                    "model": sess.model,
                    "source": source,
                    "user_message": text[:2000],
                    "response": str(reply)[:2000],
                },
            )
        except Exception:
            logger.debug("External chat webhook dispatch failed", exc_info=True)

    return ExternalChatResult(
        response=reply,
        session_id=session_id,
        model=sess.model,
        created_session=created_session,
    )
