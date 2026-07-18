from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Account, Base, LifeEntity
from src.communications_connector_polling import (
    communications_polling_health,
    poll_readonly_communications_once,
)
from src.communications_hub import communications_view
from src.communications_polling_models import CommunicationPollState
from src.identity import ensure_account
from src.integrations import INTEGRATION_PRESETS
from src.profile_configuration_service import put_configuration


SLACK_TOKEN = "xoxb-private-slack-token"
TWILIO_SID = "AC" + "1" * 32
TWILIO_TOKEN = "private-twilio-auth-token"


@pytest.fixture()
def polling_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(secret_storage, "_digest_key", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'communications-polling.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        alice = ensure_account(db, "alice")
        put_configuration(
            db,
            account=alice,
            namespace="integration",
            key="alice-slack",
            value={
                "id": "alice-slack",
                "preset": "slack",
                **INTEGRATION_PRESETS["slack"],
                "api_key": SLACK_TOKEN,
                "enabled": True,
            },
            source="domain_service",
        )
        put_configuration(
            db,
            account=alice,
            namespace="integration",
            key="alice-twilio",
            value={
                "id": "alice-twilio",
                "preset": "twilio",
                **INTEGRATION_PRESETS["twilio"],
                "api_key": f"{TWILIO_SID}:{TWILIO_TOKEN}",
                "enabled": True,
            },
            source="domain_service",
        )
        db.commit()
        owner_id = alice.id
    yield factory, engine, owner_id
    engine.dispose()


def _provider_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/auth.test":
        assert request.method == "GET"
        assert request.headers["authorization"] == f"Bearer {SLACK_TOKEN}"
        return httpx.Response(200, json={"ok": True, "user_id": "U-RESTIA"})
    if path == "/api/conversations.list":
        return httpx.Response(200, json={
            "ok": True,
            "channels": [{
                "id": "D-ALICE-BOB",
                "is_im": True,
                "last_read": "1784342399.000000",
            }],
            "response_metadata": {"next_cursor": ""},
        })
    if path == "/api/conversations.history":
        assert request.url.params["channel"] == "D-ALICE-BOB"
        return httpx.Response(200, json={
            "ok": True,
            "messages": [{
                "type": "message",
                "user": "U-BOB",
                "text": "Slack: please confirm the robotics review by Friday?",
                "ts": "1784342400.000001",
                "client_msg_id": "slack-message-1",
            }],
        })
    if path.endswith("/Messages.json"):
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, json={
            "messages": [{
                "sid": "SM" + "2" * 32,
                "direction": "inbound",
                "from": "+15550001111",
                "to": "+15550002222",
                "body": "SMS: the payment receipt is ready.",
                "date_sent": "Sat, 18 Jul 2026 08:00:00 +0000",
            }],
        })
    if path.endswith("/Calls.json"):
        return httpx.Response(200, json={
            "calls": [{
                "sid": "CA" + "3" * 32,
                "direction": "inbound",
                "from": "+15550003333",
                "to": "+15550002222",
                "status": "completed",
                "duration": "42",
                "start_time": "Sat, 18 Jul 2026 09:00:00 +0000",
            }],
        })
    raise AssertionError(f"unexpected provider read: {request.method} {request.url}")


@pytest.mark.asyncio
async def test_slack_and_twilio_poll_into_owner_scoped_readonly_hub_idempotently(
    polling_env,
):
    factory, engine, owner_id = polling_env
    client = httpx.AsyncClient(transport=httpx.MockTransport(_provider_handler))
    try:
        first = await poll_readonly_communications_once(
            session_factory=factory, client=client,
        )
        second = await poll_readonly_communications_once(
            session_factory=factory, client=client,
        )
    finally:
        await client.aclose()

    assert first == {"targets": 2, "healthy": 2, "failed": 0, "imported": 3}
    assert second == {"targets": 2, "healthy": 2, "failed": 0, "imported": 0}

    with factory() as db:
        alice = db.query(Account).filter_by(id=owner_id).one()
        view = communications_view(db, account=alice)
        assert {thread["connector"] for thread in view["threads"]} == {
            "slack", "sms", "call",
        }
        assert all(
            thread["messages"][0]["direction"] == "inbound"
            for thread in view["threads"]
        )
        assert db.query(LifeEntity).filter_by(
            owner_id=owner_id, entity_type="message",
        ).count() == 3
        health = communications_polling_health(db, owner_id=owner_id)
        assert len(health) == 2
        assert {item["state"] for item in health} == {"healthy"}
        assert db.query(CommunicationPollState).count() == 2

    connection = sqlite3.connect(engine.url.database)
    try:
        raw = "\n".join(connection.iterdump())
    finally:
        connection.close()
    assert SLACK_TOKEN not in raw
    assert TWILIO_TOKEN not in raw
    assert "D-ALICE-BOB" not in raw


@pytest.mark.asyncio
async def test_provider_origin_fails_closed_without_leaking_credential(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(secret_storage, "_digest_key", None)
    engine = create_engine(f"sqlite:///{tmp_path / 'bad-origin.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        owner = ensure_account(db, "owner")
        put_configuration(
            db,
            account=owner,
            namespace="integration",
            key="bad-slack",
            value={
                "id": "bad-slack",
                "preset": "slack",
                **INTEGRATION_PRESETS["slack"],
                "base_url": "https://attacker.example.test",
                "api_key": SLACK_TOKEN,
                "enabled": True,
            },
            source="domain_service",
        )
        db.commit()
        owner_id = owner.id

    calls = 0

    def forbidden(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
    try:
        result = await poll_readonly_communications_once(
            session_factory=factory, client=client,
        )
    finally:
        await client.aclose()
    assert result == {"targets": 1, "healthy": 0, "failed": 1, "imported": 0}
    assert calls == 0
    with factory() as db:
        health = communications_polling_health(db, owner_id=owner_id)
        assert health[0]["error_code"] == "provider_origin_invalid"
        assert SLACK_TOKEN not in str(health)
    engine.dispose()


def test_provider_polling_has_no_external_write_surface_and_no_tasks_dependency():
    import src.communications_connector_polling as polling

    assert not [
        name for name, value in inspect.getmembers(polling, callable)
        if "send" in name.casefold()
    ]
    for provider in ("slack", "twilio"):
        permissions = INTEGRATION_PRESETS[provider]["permissions"]
        assert permissions["allowed_methods"] == ["GET"]

    root = Path(__file__).resolve().parents[1]
    source = (root / "app.py").read_text(encoding="utf-8")
    polling_source = (
        root / "src" / "communications_connector_polling.py"
    ).read_text(encoding="utf-8")
    assert "readonly-communications-polling" in source
    assert source.index("readonly-communications-polling") < source.index(
        "await task_scheduler.start()"
    )
    assert "TaskScheduler" not in polling_source
    assert "ScheduledTask" not in polling_source
