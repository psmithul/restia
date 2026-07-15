"""ASGI body cap for durable project attachment uploads.

FastAPI resolves ``UploadFile`` parameters before entering the route handler,
so a limit enforced inside that handler is too late to stop Starlette's
multipart parser from spooling an unbounded request.  This middleware sits in
front of routing and is deliberately scoped to the one attachment endpoint.
"""

from __future__ import annotations

import asyncio
import math
import os
import re
from typing import Optional

from starlette.responses import JSONResponse

from src.upload_limits import PROJECT_ATTACHMENT_MAX_BYTES, format_byte_limit


# The route has a handful of small text fields in addition to the file.  Leave
# enough room for normal multipart boundaries and headers without making the
# request-body ceiling materially larger than the configured file ceiling.
PROJECT_MULTIPART_OVERHEAD_BYTES = 64 * 1024
PROJECT_ATTACHMENT_REQUEST_MAX_BYTES = (
    PROJECT_ATTACHMENT_MAX_BYTES + PROJECT_MULTIPART_OVERHEAD_BYTES
)
try:
    PROJECT_UPLOAD_CONCURRENCY = int(
        os.getenv("RESTIA_PROJECT_UPLOAD_CONCURRENCY", "2")
    )
except ValueError as exc:
    raise ValueError("RESTIA_PROJECT_UPLOAD_CONCURRENCY must be an integer") from exc
if not 1 <= PROJECT_UPLOAD_CONCURRENCY <= 16:
    raise ValueError("RESTIA_PROJECT_UPLOAD_CONCURRENCY must be between 1 and 16")


def _positive_timeout_env(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number greater than 0")
    return value


PROJECT_UPLOAD_BODY_IDLE_TIMEOUT_SECONDS = _positive_timeout_env(
    "RESTIA_PROJECT_UPLOAD_BODY_IDLE_TIMEOUT_SECONDS", 30.0
)
PROJECT_UPLOAD_BODY_TOTAL_TIMEOUT_SECONDS = _positive_timeout_env(
    "RESTIA_PROJECT_UPLOAD_BODY_TOTAL_TIMEOUT_SECONDS", 900.0
)

# This is intentionally the one process-local upload semaphore. Acquiring it
# before calling FastAPI bounds multipart parsing buffers as well as validation,
# durable storage, and the metadata transaction performed by the route.
PROJECT_UPLOAD_SEMAPHORE = asyncio.Semaphore(PROJECT_UPLOAD_CONCURRENCY)

_ATTACHMENT_UPLOAD_PATH = re.compile(
    # The same durable upload can be reached directly, through the bearer-
    # authenticated hub namespace, or through the signed-in same-origin Home
    # Link proxy. Keep this exact so unrelated link/proxy request bodies do not
    # inherit the large body allowance or long request timeout.
    r"^/api/(?:projects|link/projects|homelink/projects)/"
    r"[^/]+/items/[^/]+/attachments/?$"
)


def is_project_attachment_upload(method: object, path: object) -> bool:
    """Return whether this request is the durable Project upload endpoint."""

    return (
        str(method or "").upper() == "POST"
        and bool(_ATTACHMENT_UPLOAD_PATH.fullmatch(str(path or "")))
    )


class _ProjectAttachmentBodyTooLarge(Exception):
    pass


class _ProjectAttachmentBodyTimeout(Exception):
    pass


def _declared_content_length(scope: dict) -> Optional[int]:
    """Return the largest valid Content-Length value, or ``None``.

    Uvicorn normally rejects malformed or conflicting values before ASGI.  If
    another server passes one through, treating it as unknown keeps the chunk
    counter active instead of trusting an ambiguous declaration.
    """

    values: list[int] = []
    for key, raw_value in scope.get("headers") or ():
        if key.lower() != b"content-length":
            continue
        try:
            text = raw_value.decode("ascii").strip()
            value = int(text)
        except (UnicodeDecodeError, ValueError):
            return None
        if value < 0 or text != str(value):
            return None
        values.append(value)
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        return None
    return values[0]


class ProjectAttachmentBodyLimitMiddleware:
    """Bound multipart bytes before Starlette parses a project attachment."""

    def __init__(
        self,
        app,
        max_body_bytes: int = PROJECT_ATTACHMENT_REQUEST_MAX_BYTES,
        semaphore=None,
        body_idle_timeout_seconds: float = PROJECT_UPLOAD_BODY_IDLE_TIMEOUT_SECONDS,
        body_total_timeout_seconds: float = PROJECT_UPLOAD_BODY_TOTAL_TIMEOUT_SECONDS,
    ):
        if int(max_body_bytes) < 1:
            raise ValueError("max_body_bytes must be greater than 0")
        idle_timeout = float(body_idle_timeout_seconds)
        total_timeout = float(body_total_timeout_seconds)
        if not math.isfinite(idle_timeout) or idle_timeout <= 0:
            raise ValueError(
                "body_idle_timeout_seconds must be a finite number greater than 0"
            )
        if not math.isfinite(total_timeout) or total_timeout <= 0:
            raise ValueError(
                "body_total_timeout_seconds must be a finite number greater than 0"
            )
        self.app = app
        self.max_body_bytes = int(max_body_bytes)
        self.semaphore = semaphore if semaphore is not None else PROJECT_UPLOAD_SEMAPHORE
        self.body_idle_timeout_seconds = idle_timeout
        self.body_total_timeout_seconds = total_timeout
        self.detail = (
            "Project attachment request is too large "
            f"({format_byte_limit(PROJECT_ATTACHMENT_MAX_BYTES)} attachment limit)"
        )
        self.timeout_detail = "Project attachment upload body timed out"

    @staticmethod
    def _matches(scope: dict) -> bool:
        return (
            scope.get("type") == "http"
            and is_project_attachment_upload(scope.get("method"), scope.get("path"))
        )

    async def _reject(self, scope, receive, send) -> None:
        response = JSONResponse({"detail": self.detail}, status_code=413)
        await response(scope, receive, send)

    async def _reject_timeout(self, scope, receive, send) -> None:
        # The request body is incomplete, so make the connection non-reusable
        # and let the client retry with a fresh upload request.
        response = JSONResponse(
            {"detail": self.timeout_detail},
            status_code=408,
            headers={"Connection": "close"},
        )
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send) -> None:
        if not self._matches(scope):
            await self.app(scope, receive, send)
            return

        declared = _declared_content_length(scope)
        if declared is not None and declared > self.max_body_bytes:
            # Reject without touching ``receive``. This keeps the multipart
            # parser (and its SpooledTemporaryFile) completely out of the path.
            await self._reject(scope, receive, send)
            return

        # Queue before reading any body chunks. At most the configured number
        # of requests can therefore enter Starlette's multipart parser.
        async with self.semaphore:
            await self._call_bounded(scope, receive, send)

    async def _call_bounded(self, scope, receive, send) -> None:
        received = 0
        overflowed = False
        timed_out = False
        body_complete = False
        started_at = asyncio.get_running_loop().time()

        async def receive_limited():
            nonlocal received, overflowed, timed_out, body_complete
            if body_complete:
                return await receive()
            elapsed = asyncio.get_running_loop().time() - started_at
            remaining_total = self.body_total_timeout_seconds - elapsed
            if remaining_total <= 0:
                timed_out = True
                raise _ProjectAttachmentBodyTimeout
            try:
                message = await asyncio.wait_for(
                    receive(),
                    timeout=min(self.body_idle_timeout_seconds, remaining_total),
                )
            except asyncio.TimeoutError:
                timed_out = True
                raise _ProjectAttachmentBodyTimeout
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > self.max_body_bytes:
                    overflowed = True
                    # A private exception avoids FastAPI converting it before
                    # the upload dependency has a chance to enter the handler.
                    raise _ProjectAttachmentBodyTooLarge
                if not message.get("more_body", False):
                    body_complete = True
            return message

        async def send_limited(message):
            # FastAPI converts arbitrary dependency/body parsing failures into
            # a generic 400. Once our counter has tripped, suppress that inner
            # response so this middleware remains the source of the 413.
            if not overflowed and not timed_out:
                await send(message)

        try:
            await self.app(scope, receive_limited, send_limited)
        except _ProjectAttachmentBodyTooLarge:
            overflowed = True
        except _ProjectAttachmentBodyTimeout:
            timed_out = True

        if overflowed:
            await self._reject(scope, receive, send)
        elif timed_out:
            await self._reject_timeout(scope, receive, send)
