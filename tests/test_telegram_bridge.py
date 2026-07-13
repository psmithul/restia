import logging
import traceback

import httpx
import pytest
from fastapi import BackgroundTasks

from src.telegram_bot import (
    TELEGRAM_SECRET_HEADER,
    TelegramConfig,
    TelegramDeliveryError,
    consume_telegram_link_code,
    create_telegram_link_code,
    extract_telegram_message,
    format_telegram_html,
    is_chat_allowed,
    send_telegram_message,
    telegram_chat_ids_for_owner,
    telegram_owner_for_chat,
    verify_telegram_secret,
)


def _config(**overrides):
    base = {
        "enabled": True,
        "bot_token": "123:abc",
        "webhook_secret": "secret-token",
        "allowed_chat_ids": frozenset({"111"}),
        "allow_all_chats": False,
        "owner": "alice",
        "session_map": {},
    }
    base.update(overrides)
    return TelegramConfig(**base)


def test_extract_telegram_text_message():
    incoming = extract_telegram_message({
        "message": {
            "message_id": 42,
            "chat": {"id": 111},
            "text": "  hello Restia  ",
        }
    })

    assert incoming is not None
    assert incoming.chat_id == "111"
    assert incoming.message_id == 42
    assert incoming.text == "hello Restia"


def test_telegram_secret_and_chat_allowlist():
    config = _config()

    assert verify_telegram_secret(config, "secret-token") is True
    assert verify_telegram_secret(config, "wrong") is False
    assert is_chat_allowed(config, "111") is True
    assert is_chat_allowed(config, "222") is False
    assert is_chat_allowed(_config(allow_all_chats=True), "222") is True


def test_per_user_chat_ownership_is_scoped():
    config = _config(
        owner="admin",
        allowed_chat_ids=frozenset({"111", "222", "333"}),
        session_map={"111": "legacy", "222": "bob-session", "333": "alice-session"},
        chat_owners={"222": "bob", "333": "alice"},
    )

    assert telegram_owner_for_chat(config, "222") == "bob"
    assert telegram_owner_for_chat(config, "111") == "admin"
    assert telegram_chat_ids_for_owner(config, "bob") == ["222"]
    assert telegram_chat_ids_for_owner(config, "alice") == ["333"]
    assert telegram_chat_ids_for_owner(config, "admin") == ["111"]


def test_one_time_link_code_claims_chat_without_storing_plain_code(monkeypatch):
    state = {
        "telegram_allowed_chat_ids": [],
        "telegram_session_map": {},
        "telegram_chat_owners": {},
        "telegram_link_codes": {},
    }

    monkeypatch.setattr("src.telegram_bot.load_settings", lambda: dict(state))

    def save(updated):
        state.clear()
        state.update(updated)

    monkeypatch.setattr("src.telegram_bot.save_settings", save)

    code, expires_at = create_telegram_link_code("Alice", ttl_seconds=600)
    assert expires_at > 0
    assert code not in str(state["telegram_link_codes"])
    assert consume_telegram_link_code(code, "987") == "alice"
    assert state["telegram_chat_owners"] == {"987": "alice"}
    assert state["telegram_allowed_chat_ids"] == ["987"]
    assert consume_telegram_link_code(code, "987") is None


def test_telegram_html_formatter_escapes_before_adding_tags():
    html = format_telegram_html(
        "# Update\n"
        "**Bold** <script>alert(1)</script>\n"
        "`x < y` and [link](https://example.com?a=1&b=2)\n"
        "> quoted\n"
        "```python\nprint('<ok>')\n```"
    )

    assert "<b>Update</b>" in html
    assert "<b>Bold</b> &lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<code>x &lt; y</code>" in html
    assert '<a href="https://example.com?a=1&amp;b=2">link</a>' in html
    assert "<blockquote>quoted</blockquote>" in html
    assert '<pre><code class="language-python">print(\'&lt;ok&gt;\')</code></pre>' in html


@pytest.mark.asyncio
async def test_send_telegram_message_uses_html_parse_mode(monkeypatch):
    payloads = []

    class _Response:
        def raise_for_status(self):
            return None

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            payloads.append(json)
            return _Response()

    monkeypatch.setattr("src.telegram_bot.httpx.AsyncClient", _Client)

    await send_telegram_message("123:abc", "111", "**hello**", reply_to_message_id=7)

    assert payloads == [{
        "chat_id": "111",
        "text": "<b>hello</b>",
        "parse_mode": "HTML",
        "reply_to_message_id": 7,
        "allow_sending_without_reply": True,
    }]


@pytest.mark.asyncio
async def test_send_telegram_message_redacts_token_from_httpx_info_log(monkeypatch, caplog):
    token = "test-only-token:success"
    real_client = httpx.AsyncClient

    async def handler(request):
        return httpx.Response(200, request=request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "src.telegram_bot.httpx.AsyncClient",
        lambda *args, **kwargs: real_client(*args, transport=transport, **kwargs),
    )
    caplog.set_level(logging.INFO, logger="httpx")

    await send_telegram_message(token, "111", "hello")

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert token not in messages
    assert "bot[REDACTED]/sendMessage" in messages


@pytest.mark.asyncio
async def test_send_telegram_message_redacts_caught_http_status_error(monkeypatch):
    token = "test-only-token:http-error"
    real_client = httpx.AsyncClient

    async def handler(request):
        return httpx.Response(401, request=request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "src.telegram_bot.httpx.AsyncClient",
        lambda *args, **kwargs: real_client(*args, transport=transport, **kwargs),
    )

    with pytest.raises(TelegramDeliveryError) as caught:
        await send_telegram_message(token, "111", "hello")

    assert token not in str(caught.value)
    assert str(caught.value) == "Telegram API request failed with HTTP 401"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    assert caught.value.__context__ is None
    assert token not in "".join(traceback.format_exception(caught.value))


@pytest.mark.asyncio
async def test_send_telegram_message_redacts_caught_transport_error(monkeypatch):
    token = "test-only-token:transport-error"
    real_client = httpx.AsyncClient

    async def handler(request):
        raise httpx.ConnectError(f"could not reach {request.url}", request=request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "src.telegram_bot.httpx.AsyncClient",
        lambda *args, **kwargs: real_client(*args, transport=transport, **kwargs),
    )

    with pytest.raises(TelegramDeliveryError) as caught:
        await send_telegram_message(token, "111", "hello")

    assert token not in str(caught.value)
    assert str(caught.value) == "Telegram API request failed (ConnectError)"
    assert caught.value.__suppress_context__ is True
    assert caught.value.__context__ is None
    assert token not in "".join(traceback.format_exception(caught.value))
    tb = caught.value.__traceback__
    production_frames = []
    while tb is not None:
        if tb.tb_frame.f_code.co_filename.endswith("src/telegram_bot.py"):
            production_frames.append(tb.tb_frame)
        tb = tb.tb_next
    assert production_frames
    assert all(token not in repr(frame.f_locals) for frame in production_frames)


@pytest.mark.asyncio
async def test_send_telegram_message_sanitizes_non_httpx_transport_exception(monkeypatch):
    token = "test-only-token:unexpected-error"

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json):
            raise RuntimeError(f"hook failed for {url}")

    monkeypatch.setattr("src.telegram_bot.httpx.AsyncClient", _Client)

    with pytest.raises(TelegramDeliveryError) as caught:
        await send_telegram_message(token, "111", "hello")

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == "Telegram API request failed"
    assert caught.value.__context__ is None
    assert token not in rendered


class _Request:
    def __init__(self, payload, secret="secret-token"):
        self.headers = {TELEGRAM_SECRET_HEADER: secret}
        self._payload = payload

    async def json(self):
        return self._payload


def _webhook_endpoint(monkeypatch, config):
    import routes.telegram_routes as telegram_routes

    monkeypatch.setattr(telegram_routes, "load_telegram_config", lambda: config)
    router = telegram_routes.setup_telegram_routes(session_manager=object(), webhook_manager=None)
    for route in router.routes:
        if route.path == "/api/telegram/webhook":
            return route.endpoint
    raise AssertionError("telegram webhook route not found")


@pytest.mark.asyncio
async def test_telegram_webhook_rejects_bad_secret(monkeypatch):
    endpoint = _webhook_endpoint(monkeypatch, _config())

    with pytest.raises(Exception) as exc:
        await endpoint(_Request({"message": {"chat": {"id": 111}, "text": "hi"}}, secret="bad"), BackgroundTasks())

    assert getattr(exc.value, "status_code", None) == 403


@pytest.mark.asyncio
async def test_telegram_webhook_ignores_unauthorized_chat_without_background_task(monkeypatch):
    endpoint = _webhook_endpoint(monkeypatch, _config())
    tasks = BackgroundTasks()

    response = await endpoint(
        _Request({"message": {"chat": {"id": 222}, "text": "hi"}}),
        tasks,
    )

    assert response == {"ok": True, "ignored": "unauthorized_chat"}
    assert tasks.tasks == []


@pytest.mark.asyncio
async def test_telegram_webhook_schedules_authorized_message(monkeypatch):
    endpoint = _webhook_endpoint(monkeypatch, _config())
    tasks = BackgroundTasks()

    response = await endpoint(
        _Request({"message": {"message_id": 7, "chat": {"id": 111}, "text": "hi"}}),
        tasks,
    )

    assert response == {"ok": True}
    assert len(tasks.tasks) == 1


@pytest.mark.asyncio
async def test_telegram_webhook_allows_link_command_from_unlisted_chat(monkeypatch):
    endpoint = _webhook_endpoint(monkeypatch, _config())
    tasks = BackgroundTasks()

    response = await endpoint(
        _Request({"message": {"message_id": 8, "chat": {"id": 222}, "text": "/link ABCD1234"}}),
        tasks,
    )

    assert response == {"ok": True}
    assert len(tasks.tasks) == 1


class _ExternalSession:
    def __init__(self):
        self.endpoint_url = "http://model.local/v1/chat/completions"
        self.model = "test-model"
        self.headers = {}
        self.owner = "admin"
        self.messages = []

    def add_message(self, message):
        self.messages.append(message)

    def get_context_messages(self):
        return [{"role": m.role, "content": m.content} for m in self.messages]


class _ExternalSessionManager:
    def __init__(self):
        self.session = _ExternalSession()
        self.created = []
        self.save_calls = 0

    def create_session(self, **kwargs):
        self.created.append(kwargs)
        self.session.owner = kwargs.get("owner")
        self.session.endpoint_url = kwargs.get("endpoint_url")
        self.session.model = kwargs.get("model")
        return self.session

    def save_sessions(self):
        self.save_calls += 1


@pytest.mark.asyncio
async def test_external_chat_uses_agent_loop(monkeypatch):
    import src.external_chat as external_chat

    calls = []

    async def fake_agent_turn(sess, *, session_id, owner):
        calls.append({
            "session_id": session_id,
            "owner": owner,
            "messages": sess.get_context_messages(),
        })
        return "agent response"

    monkeypatch.setattr(external_chat, "resolve_external_chat_endpoint", lambda owner: ("http://model.local/v1/chat/completions", "test-model", {}))
    monkeypatch.setattr(external_chat, "_persist_session_headers", lambda *args, **kwargs: None)
    monkeypatch.setattr(external_chat, "_run_external_agent_turn", fake_agent_turn)

    manager = _ExternalSessionManager()
    result = await external_chat.send_external_chat_message(
        manager,
        message="check my latest email",
        owner="admin",
        session_name="Telegram Chat",
        source="telegram",
    )

    assert result.response == "agent response"
    assert result.created_session is True
    assert manager.created[0]["owner"] == "admin"
    assert manager.save_calls == 1
    assert calls[0]["owner"] == "admin"
    assert calls[0]["messages"][-1]["content"] == "check my latest email"
