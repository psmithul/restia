"""Incoming-call Telegram alerts and pending-offer replay contracts."""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.call_notifications import (
    CALL_ALERT_OFFSETS_S,
    CALL_ALERT_TTL_S,
    IncomingCallNotifications,
    build_restia_call_link,
)
from src.telegram_bot import TelegramConfig, telegram_chat_ids_for_owner


CALL_1 = "00000000-0000-4000-8000-000000000001"
CALL_2 = "00000000-0000-4000-8000-000000000002"
CALL_3 = "00000000-0000-4000-8000-000000000003"
OFFER = {"sdp": "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\n", "video": True}


def _config(**overrides) -> TelegramConfig:
    values = {
        "enabled": True,
        "bot_token": "test-only-bot-token",
        "webhook_secret": "webhook-secret",
        "allowed_chat_ids": frozenset({"legacy"}),
        "allow_all_chats": False,
        "owner": "legacy-owner",
        "session_map": {},
        "chat_owners": {"alice-phone": "alice", "bob-phone": "bob"},
    }
    values.update(overrides)
    return TelegramConfig(**values)


class _ControlledTime:
    """Run alert offsets immediately while holding the final expiry sleep."""

    def __init__(self) -> None:
        self.now = 0.0
        self.expiry_gate = asyncio.Event()
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        if delay >= CALL_ALERT_TTL_S:
            await self.expiry_gate.wait()


async def _settle(rounds: int = 40) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


def test_call_link_uses_only_a_canonical_configured_https_origin():
    link = build_restia_call_link(
        "friend@remote",
        {"app_public_url": "https://APP.Restia.Dev:443/"},
    )

    assert link == "https://app.restia.dev/#messages=friend%40remote"
    # The fragment is sufficient to select a chat; sensitive call state must
    # never be embedded in a URL.
    assert "call_id" not in link and CALL_1 not in link


@pytest.mark.parametrize("value", [
    "",
    "http://app.restia.dev",
    "javascript:alert(1)",
    "https://user:pass@app.restia.dev",
    "https://app.restia.dev/subpath",
    "https://app.restia.dev/?next=evil",
    "https://app.restia.dev/#evil",
    "https://app.restia.dev\\@evil.example",
])
def test_call_link_refuses_unsafe_or_non_https_public_urls(value):
    assert build_restia_call_link("alice", {"app_public_url": value}) == ""


@pytest.mark.asyncio
async def test_alerts_reach_only_chats_linked_to_the_callee_and_are_bounded():
    controlled = _ControlledTime()
    sent: list[tuple[str, str, str, bool]] = []

    async def sender(token, chat_id, text, *, rich_text=True):
        sent.append((token, chat_id, text, rich_text))

    manager = IncomingCallNotifications(
        telegram_config_loader=lambda: _config(),
        telegram_chat_lookup=telegram_chat_ids_for_owner,
        telegram_sender=sender,
        settings_loader=lambda: {"app_public_url": "https://app.restia.dev"},
        sleep=controlled.sleep,
        clock=controlled.clock,
    )
    try:
        assert manager.begin(
            owner="Alice",
            peer="friend@remote",
            call_id=CALL_1,
            transport="local",
            offer_data=OFFER,
        ) is True
        # An exact duplicate cannot create a second alert task.
        assert manager.begin(
            owner="alice",
            peer="friend@remote",
            call_id=CALL_1,
            transport="local",
            offer_data=OFFER,
        ) is False
        await _settle()

        assert len(CALL_ALERT_OFFSETS_S) == 4
        assert all(0 <= offset < CALL_ALERT_TTL_S for offset in CALL_ALERT_OFFSETS_S)
        assert len(sent) == len(CALL_ALERT_OFFSETS_S)
        assert {chat_id for _, chat_id, _, _ in sent} == {"alice-phone"}
        assert all(rich_text is False for *_, rich_text in sent)
        assert [f"Alert {n} of 4" in item[2] for n, item in enumerate(sent, 1)] == [True] * 4
        assert all("https://app.restia.dev/#messages=friend%40remote" in item[2] for item in sent)
        assert all(CALL_1 not in item[2] and OFFER["sdp"] not in item[2] for item in sent)
        assert all(item[0] not in item[2] for item in sent)

        snapshot = manager.pending_snapshot(owner="alice", transport="local")
        assert snapshot == [{
            "from": "friend@remote",
            "call_id": CALL_1,
            "kind": "offer",
            "data": OFFER,
        }]
        assert manager.stop(
            owner="alice", transport="local", call_id=CALL_1, peer="friend@remote"
        ) is True
        assert manager.pending_snapshot(owner="alice", transport="local") == []
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_pending_snapshot_is_owner_transport_scoped_and_isolated():
    controlled = _ControlledTime()
    disabled = _config(enabled=False, bot_token="")

    async def no_network(*args, **kwargs):
        raise AssertionError("disabled Telegram must not attempt delivery")

    manager = IncomingCallNotifications(
        telegram_config_loader=lambda: disabled,
        telegram_sender=no_network,
        settings_loader=lambda: {"app_public_url": "https://app.restia.dev"},
        sleep=controlled.sleep,
        clock=controlled.clock,
    )
    try:
        assert manager.begin(
            owner="alice", peer="bob", call_id=CALL_1,
            transport="local", offer_data=OFFER,
        )
        assert manager.begin(
            owner="alice", peer="home.example", call_id=CALL_2,
            transport="home", offer_data={**OFFER, "video": False},
        )
        assert manager.begin(
            owner="bob", peer="alice", call_id=CALL_3,
            transport="local", offer_data=OFFER,
        )
        await _settle()

        local = manager.pending_snapshot(owner="alice", transport="local")
        home = manager.pending_snapshot(owner="alice", transport="home")
        assert [row["call_id"] for row in local] == [CALL_1]
        assert [row["call_id"] for row in home] == [CALL_2]
        assert [row["call_id"] for row in manager.pending_snapshot(
            owner="bob", transport="local"
        )] == [CALL_3]

        # SSE callers receive copies, not references to the retained SDP.
        local[0]["data"]["sdp"] = "tampered"
        assert manager.pending_snapshot(owner="alice", transport="local")[0]["data"] == OFFER
        assert manager.stop(
            owner="alice", transport="local", call_id=CALL_1, peer="mallory"
        ) is False
        assert manager.stop_owner_transport(owner="alice", transport="home") == 1
        assert manager.pending_snapshot(owner="alice", transport="home") == []
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_pending_memory_has_global_and_per_profile_hard_caps():
    controlled = _ControlledTime()
    manager = IncomingCallNotifications(
        telegram_config_loader=lambda: _config(enabled=False),
        sleep=controlled.sleep,
        clock=controlled.clock,
        max_pending=2,
        max_pending_per_owner=1,
    )
    try:
        assert manager.begin(
            owner="alice", peer="one", call_id=CALL_1,
            transport="local", offer_data=OFFER,
        )
        # Same owner is capped even with a different transport/call id.
        assert not manager.begin(
            owner="alice", peer="two", call_id=CALL_2,
            transport="home", offer_data=OFFER,
        )
        assert manager.begin(
            owner="bob", peer="one", call_id=CALL_2,
            transport="local", offer_data=OFFER,
        )
        # Global cap is now full.
        assert not manager.begin(
            owner="charlie", peer="one", call_id=CALL_3,
            transport="local", offer_data=OFFER,
        )
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_expired_offer_is_pruned_and_telegram_failures_leak_no_secrets(caplog):
    controlled = _ControlledTime()
    token = "test-only-super-secret-token"

    async def failing_sender(bot_token, chat_id, text, *, rich_text=True):
        raise RuntimeError(f"failed with {bot_token} for {chat_id}")

    manager = IncomingCallNotifications(
        telegram_config_loader=lambda: _config(bot_token=token),
        telegram_chat_lookup=telegram_chat_ids_for_owner,
        telegram_sender=failing_sender,
        settings_loader=lambda: {"app_public_url": "https://app.restia.dev"},
        sleep=controlled.sleep,
        clock=controlled.clock,
    )
    caplog.set_level(logging.WARNING, logger="src.call_notifications")
    try:
        assert manager.begin(
            owner="alice", peer="friend@remote", call_id=CALL_1,
            transport="local", offer_data=OFFER,
        )
        await _settle()
        assert token not in caplog.text
        assert "alice-phone" not in caplog.text

        controlled.now = CALL_ALERT_TTL_S + 1
        assert manager.pending_snapshot(owner="alice", transport="local") == []
    finally:
        controlled.expiry_gate.set()
        await manager.shutdown()


@pytest.mark.parametrize("kwargs", [
    {"owner": "", "peer": "bob", "call_id": CALL_1, "transport": "local", "offer_data": OFFER},
    {"owner": "alice", "peer": "", "call_id": CALL_1, "transport": "local", "offer_data": OFFER},
    {"owner": "alice", "peer": "bob", "call_id": "guessable", "transport": "local", "offer_data": OFFER},
    {"owner": "alice", "peer": "bob", "call_id": CALL_1, "transport": "remote", "offer_data": OFFER},
    {"owner": "alice", "peer": "bob", "call_id": CALL_1, "transport": "local", "offer_data": {"sdp": "bad", "video": True}},
    {"owner": "alice", "peer": "bob", "call_id": CALL_1, "transport": "local", "offer_data": {**OFFER, "extra": True}},
])
@pytest.mark.asyncio
async def test_manager_rejects_unsanitized_or_ambiguous_inputs(kwargs):
    manager = IncomingCallNotifications(telegram_config_loader=lambda: _config(enabled=False))
    try:
        with pytest.raises(ValueError):
            manager.begin(**kwargs)
    finally:
        await manager.shutdown()
