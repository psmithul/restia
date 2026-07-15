"""The Projects upload cap runs before multipart dependency parsing."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI, File, UploadFile

from core.project_upload_limit import (
    ProjectAttachmentBodyLimitMiddleware,
    _positive_timeout_env,
    is_project_attachment_upload,
)


def _scope(*, path, content_length=None, method="POST"):
    headers = [(b"content-type", b"multipart/form-data; boundary=test")]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "root_path": "",
    }


def _run(app, scope, chunks):
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]
    if not messages:
        messages.append({"type": "http.request", "body": b"", "more_body": False})
    sent = []
    receive_calls = 0

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    return sent, receive_calls


def _response(sent):
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    if not body:
        payload = None
    else:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = body
    return start["status"], payload


@pytest.mark.parametrize("raw_value", ["nan", "inf", "-inf"])
def test_timeout_environment_rejects_non_finite_values(monkeypatch, raw_value):
    name = "RESTIA_TEST_PROJECT_UPLOAD_TIMEOUT_SECONDS"
    monkeypatch.setenv(name, raw_value)

    with pytest.raises(ValueError, match="finite number greater than 0"):
        _positive_timeout_env(name, 1.0)


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("body_idle_timeout_seconds", float("nan")),
        ("body_idle_timeout_seconds", float("inf")),
        ("body_idle_timeout_seconds", float("-inf")),
        ("body_total_timeout_seconds", float("nan")),
        ("body_total_timeout_seconds", float("inf")),
        ("body_total_timeout_seconds", float("-inf")),
    ],
)
def test_middleware_rejects_non_finite_timeout_arguments(argument, value):
    with pytest.raises(ValueError, match="finite number greater than 0"):
        ProjectAttachmentBodyLimitMiddleware(None, **{argument: value})


def test_attachment_upload_matcher_is_exact_and_shared_with_timeout_policy():
    assert is_project_attachment_upload(
        "POST", "/api/projects/project-1/items/item-1/attachments"
    )
    assert not is_project_attachment_upload(
        "GET", "/api/projects/project-1/items/item-1/attachments"
    )
    assert not is_project_attachment_upload(
        "POST", "/api/projects/project-1/items/item-1/attachments/extra"
    )
    app_source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    assert "or is_project_attachment_upload(request.method, path)" in app_source


def test_oversized_content_length_is_rejected_before_parser_or_receive():
    state = {"parser_entered": False, "handler_reached": False}

    class ForbiddenSemaphore:
        entered = False

        async def __aenter__(self):
            self.entered = True
            raise AssertionError("oversized declared body consumed an upload slot")

        async def __aexit__(self, *_args):
            return None

    semaphore = ForbiddenSemaphore()

    async def multipart_endpoint(_scope, receive, send):
        state["parser_entered"] = True
        await receive()
        state["handler_reached"] = True
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = ProjectAttachmentBodyLimitMiddleware(
        multipart_endpoint,
        max_body_bytes=8,
        semaphore=semaphore,
    )
    sent, receive_calls = _run(
        middleware,
        _scope(
            path="/api/projects/project-1/items/item-1/attachments",
            content_length=9,
        ),
        [b"not read"],
    )

    status, payload = _response(sent)
    assert status == 413
    assert "too large" in payload["detail"].lower()
    assert receive_calls == 0
    assert state == {"parser_entered": False, "handler_reached": False}
    assert semaphore.entered is False


def test_chunked_body_is_counted_and_never_reaches_handler():
    state = {"parser_entered": False, "handler_reached": False}

    async def multipart_endpoint(_scope, receive, send):
        state["parser_entered"] = True
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        state["handler_reached"] = True
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"created"})

    middleware = ProjectAttachmentBodyLimitMiddleware(multipart_endpoint, max_body_bytes=8)
    sent, receive_calls = _run(
        middleware,
        _scope(path="/api/projects/project-1/items/item-1/attachments"),
        [b"1234", b"5678", b"9"],
    )

    assert _response(sent)[0] == 413
    assert receive_calls == 3
    assert state == {"parser_entered": True, "handler_reached": False}


def test_declared_length_does_not_disable_chunk_counting():
    state = {"handler_reached": False}

    async def multipart_endpoint(_scope, receive, send):
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        state["handler_reached"] = True
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"created"})

    middleware = ProjectAttachmentBodyLimitMiddleware(multipart_endpoint, max_body_bytes=8)
    sent, _ = _run(
        middleware,
        _scope(
            path="/api/projects/project-1/items/item-1/attachments",
            content_length=1,
        ),
        [b"12345", b"6789"],
    )

    assert _response(sent)[0] == 413
    assert state["handler_reached"] is False


def test_fastapi_multipart_parse_overflow_returns_413_not_generic_400():
    app = FastAPI()
    state = {"handler_reached": False}

    @app.post("/api/projects/project-1/items/item-1/attachments")
    async def upload(file: UploadFile = File(...)):
        state["handler_reached"] = True
        return {"filename": file.filename}

    app.add_middleware(ProjectAttachmentBodyLimitMiddleware, max_body_bytes=32)
    multipart = (
        b"--test\r\n"
        b'Content-Disposition: form-data; name="file"; filename="report.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n"
        b"%PDF-1.7 payload\r\n"
        b"--test--\r\n"
    )
    sent, _ = _run(
        app,
        _scope(path="/api/projects/project-1/items/item-1/attachments"),
        [multipart[:20], multipart[20:]],
    )

    assert _response(sent)[0] == 413
    assert state["handler_reached"] is False


def test_fastapi_normal_multipart_upload_still_reaches_handler():
    app = FastAPI()
    state = {"filename": None, "content": None}

    @app.post("/api/projects/project-1/items/item-1/attachments")
    async def upload(file: UploadFile = File(...)):
        state["filename"] = file.filename
        state["content"] = await file.read()
        return {"ok": True}

    multipart = (
        b"--test\r\n"
        b'Content-Disposition: form-data; name="file"; filename="report.pdf"\r\n'
        b"Content-Type: application/pdf\r\n\r\n"
        b"%PDF-1.7\r\n"
        b"--test--\r\n"
    )
    app.add_middleware(
        ProjectAttachmentBodyLimitMiddleware,
        max_body_bytes=len(multipart),
    )
    sent, _ = _run(
        app,
        _scope(
            path="/api/projects/project-1/items/item-1/attachments",
            content_length=len(multipart),
        ),
        [multipart[:30], multipart[30:]],
    )

    assert _response(sent) == (200, {"ok": True})
    assert state == {"filename": "report.pdf", "content": b"%PDF-1.7"}


def test_normal_upload_and_unrelated_routes_are_unchanged():
    seen_bodies = []

    async def endpoint(_scope, receive, send):
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        seen_bodies.append(body)
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"created"})

    middleware = ProjectAttachmentBodyLimitMiddleware(endpoint, max_body_bytes=8)
    normal_sent, _ = _run(
        middleware,
        _scope(
            path="/api/projects/project-1/items/item-1/attachments",
            content_length=8,
        ),
        [b"1234", b"5678"],
    )
    unrelated_sent, _ = _run(
        middleware,
        _scope(path="/api/other-upload", content_length=100),
        [b"unrelated"],
    )

    assert _response(normal_sent)[0] == 201
    assert _response(unrelated_sent)[0] == 201
    assert seen_bodies == [b"12345678", b"unrelated"]


def test_only_configured_concurrency_enters_multipart_parser():
    async def scenario():
        active = 0
        maximum_active = 0
        parser_entries = []
        first_wave_entered = asyncio.Event()
        release_first_wave = asyncio.Event()

        async def multipart_endpoint(scope, receive, send):
            nonlocal active, maximum_active
            request_number = int(scope["path"].split("/")[3].removeprefix("project-"))
            active += 1
            maximum_active = max(maximum_active, active)
            parser_entries.append(request_number)
            if len(parser_entries) == 2:
                first_wave_entered.set()
            try:
                await release_first_wave.wait()
                await receive()
                await send({"type": "http.response.start", "status": 201, "headers": []})
                await send({"type": "http.response.body", "body": b"created"})
            finally:
                active -= 1

        middleware = ProjectAttachmentBodyLimitMiddleware(
            multipart_endpoint,
            max_body_bytes=8,
            semaphore=asyncio.Semaphore(2),
        )

        async def invoke(request_number):
            path = (
                f"/api/projects/project-{request_number}/items/item-1/attachments"
            )
            body_pending = True
            sent = []

            async def receive():
                nonlocal body_pending
                if body_pending:
                    body_pending = False
                    return {"type": "http.request", "body": b"data", "more_body": False}
                return {"type": "http.disconnect"}

            async def send(message):
                sent.append(message)

            await middleware(_scope(path=path, content_length=4), receive, send)
            return sent

        tasks = [asyncio.create_task(invoke(index)) for index in range(1, 4)]
        await asyncio.wait_for(first_wave_entered.wait(), timeout=1)
        await asyncio.sleep(0)
        assert len(parser_entries) == 2
        assert maximum_active == 2

        release_first_wave.set()
        responses = await asyncio.gather(*tasks)
        return parser_entries, maximum_active, responses

    parser_entries, maximum_active, responses = asyncio.run(scenario())

    assert len(parser_entries) == 3
    assert set(parser_entries) == {1, 2, 3}
    assert maximum_active == 2
    assert [_response(sent)[0] for sent in responses] == [201, 201, 201]


def test_stalled_uploads_time_out_and_release_parser_slots():
    async def scenario():
        parser_entries = []

        async def multipart_endpoint(scope, receive, send):
            request_number = int(scope["path"].split("/")[3].removeprefix("project-"))
            parser_entries.append(request_number)
            await receive()
            await send({"type": "http.response.start", "status": 201, "headers": []})
            await send({"type": "http.response.body", "body": b"created"})

        middleware = ProjectAttachmentBodyLimitMiddleware(
            multipart_endpoint,
            max_body_bytes=8,
            semaphore=asyncio.Semaphore(2),
            body_idle_timeout_seconds=0.02,
            body_total_timeout_seconds=1,
        )

        async def invoke(request_number):
            sent = []

            async def receive():
                if request_number < 3:
                    await asyncio.Event().wait()
                return {"type": "http.request", "body": b"data", "more_body": False}

            async def send(message):
                sent.append(message)

            await middleware(
                _scope(
                    path=f"/api/projects/project-{request_number}/items/item-1/attachments",
                    content_length=4,
                ),
                receive,
                send,
            )
            return sent

        responses = await asyncio.wait_for(
            asyncio.gather(*(invoke(index) for index in range(1, 4))),
            timeout=1,
        )
        return parser_entries, responses

    parser_entries, responses = asyncio.run(scenario())
    assert parser_entries[:2] == [1, 2]
    assert parser_entries[2] == 3
    assert [_response(sent)[0] for sent in responses] == [408, 408, 201]
    for sent in responses[:2]:
        start = next(message for message in sent if message["type"] == "http.response.start")
        assert (b"connection", b"close") in start["headers"]


def test_total_body_timeout_rejects_slow_trickle_upload():
    async def scenario():
        sent = []
        call = 0

        async def multipart_endpoint(_scope, receive, send):
            while True:
                message = await receive()
                if not message.get("more_body", False):
                    break
            await send({"type": "http.response.start", "status": 201, "headers": []})
            await send({"type": "http.response.body", "body": b"created"})

        async def receive():
            nonlocal call
            call += 1
            if call == 1:
                return {"type": "http.request", "body": b"12", "more_body": True}
            await asyncio.sleep(0.2)
            return {"type": "http.request", "body": b"34", "more_body": False}

        async def send(message):
            sent.append(message)

        middleware = ProjectAttachmentBodyLimitMiddleware(
            multipart_endpoint,
            max_body_bytes=8,
            body_idle_timeout_seconds=1,
            body_total_timeout_seconds=0.03,
        )
        await middleware(
            _scope(
                path="/api/projects/project-1/items/item-1/attachments",
                content_length=4,
            ),
            receive,
            send,
        )
        return sent

    sent = asyncio.run(scenario())
    status, payload = _response(sent)
    assert status == 408
    assert "timed out" in payload["detail"].lower()
