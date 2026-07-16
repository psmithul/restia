"""Opaque, signed keyset cursors for the owner-scoped Universal Inbox."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime


_CURSOR_VERSION = 1
_CURSOR_DOMAIN = b"restia-inbox-page-v1\x00"
_MAX_CURSOR_LENGTH = 1024
_SIGNATURE_BYTES = hashlib.sha256().digest_size


class InboxCursorError(ValueError):
    """Raised for every malformed, tampered, or context-mismatched cursor."""


@dataclass(frozen=True)
class InboxCursor:
    updated_at: datetime
    item_id: str


def _encode_base64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode_base64(value: str) -> bytes:
    if not value or len(value) > _MAX_CURSOR_LENGTH:
        raise InboxCursorError("Invalid inbox cursor")
    try:
        raw = value.encode("ascii")
        padding = b"=" * (-len(raw) % 4)
        return base64.b64decode(raw + padding, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise InboxCursorError("Invalid inbox cursor") from exc


def _signature_context(*, owner_id: str, status: str, kind: str | None) -> bytes:
    # Query identity stays out of the readable cursor payload while still
    # making a token valid for exactly one owner and filtered list.
    return json.dumps(
        {"k": str(kind or ""), "o": str(owner_id), "s": str(status)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def encode_inbox_cursor(
    *,
    signing_key: bytes,
    owner_id: str,
    status: str,
    kind: str | None,
    updated_at: datetime,
    item_id: str,
) -> str:
    """Encode the final ``(updated_at, id)`` pair and bind it to its query."""

    payload = json.dumps(
        {
            "i": str(item_id),
            "t": updated_at.isoformat(timespec="microseconds"),
            "v": _CURSOR_VERSION,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    signature = hmac.new(
        signing_key,
        _CURSOR_DOMAIN
        + _signature_context(owner_id=owner_id, status=status, kind=kind)
        + b"\x00"
        + payload,
        hashlib.sha256,
    ).digest()
    return _encode_base64(payload + signature)


def decode_inbox_cursor(
    value: str,
    *,
    signing_key: bytes,
    owner_id: str,
    status: str,
    kind: str | None,
) -> InboxCursor:
    """Verify and decode a cursor for exactly one owner-scoped filtered list."""

    try:
        encoded = _decode_base64(value)
        if len(encoded) <= _SIGNATURE_BYTES:
            raise InboxCursorError("Invalid inbox cursor")
        payload, signature = encoded[:-_SIGNATURE_BYTES], encoded[-_SIGNATURE_BYTES:]
        expected = hmac.new(
            signing_key,
            _CURSOR_DOMAIN
            + _signature_context(owner_id=owner_id, status=status, kind=kind)
            + b"\x00"
            + payload,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise InboxCursorError("Invalid inbox cursor")
        decoded = json.loads(payload.decode("utf-8"))
        if not isinstance(decoded, dict) or set(decoded) != {"i", "t", "v"}:
            raise InboxCursorError("Invalid inbox cursor")
        if type(decoded["v"]) is not int or decoded["v"] != _CURSOR_VERSION:
            raise InboxCursorError("Invalid inbox cursor")
        item_id = decoded["i"]
        timestamp_text = decoded["t"]
        if not isinstance(item_id, str) or not item_id or len(item_id) > 255:
            raise InboxCursorError("Invalid inbox cursor")
        if not isinstance(timestamp_text, str):
            raise InboxCursorError("Invalid inbox cursor")
        updated_at = datetime.fromisoformat(timestamp_text)
        if updated_at.tzinfo is not None:
            raise InboxCursorError("Invalid inbox cursor")
        if updated_at.isoformat(timespec="microseconds") != timestamp_text:
            raise InboxCursorError("Invalid inbox cursor")
        return InboxCursor(updated_at=updated_at, item_id=item_id)
    except InboxCursorError:
        raise
    except (UnicodeDecodeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise InboxCursorError("Invalid inbox cursor") from exc
