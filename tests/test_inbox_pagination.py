from __future__ import annotations

import base64
from datetime import datetime

from src.inbox_pagination import decode_inbox_cursor, encode_inbox_cursor


def _raw_cursor(token: str) -> bytes:
    encoded = token.encode("ascii")
    return base64.urlsafe_b64decode(encoded + b"=" * (-len(encoded) % 4))


def test_cursor_round_trip_is_unambiguous_when_binary_signature_contains_dot():
    timestamp = datetime(2026, 7, 16, 12, 30, 45, 123456)
    found_dot_signature = False

    for index in range(256):
        key = f"deterministic-test-key-{index}".encode("ascii")
        token = encode_inbox_cursor(
            signing_key=key,
            owner_id="private-owner-uuid",
            status="inbox",
            kind=None,
            updated_at=timestamp,
            item_id="item-0099",
        )
        raw = _raw_cursor(token)
        found_dot_signature = found_dot_signature or b"." in raw[-32:]
        cursor = decode_inbox_cursor(
            token,
            signing_key=key,
            owner_id="private-owner-uuid",
            status="inbox",
            kind=None,
        )
        assert cursor.updated_at == timestamp
        assert cursor.item_id == "item-0099"

    assert found_dot_signature


def test_cursor_payload_does_not_disclose_owner_or_filter_context():
    token = encode_inbox_cursor(
        signing_key=b"cursor-privacy-test-key",
        owner_id="sensitive-owner-uuid",
        status="processed",
        kind="expense",
        updated_at=datetime(2026, 7, 16, 12, 30, 45),
        item_id="item-1",
    )
    readable_payload = _raw_cursor(token)[:-32]

    assert b"sensitive-owner-uuid" not in readable_payload
    assert b"processed" not in readable_payload
    assert b"expense" not in readable_payload
