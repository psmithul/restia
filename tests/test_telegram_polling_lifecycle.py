"""Telegram polling owns a lifecycle independent from scheduled Tasks."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_telegram_polling_gate_is_independent_from_task_scheduler(monkeypatch):
    from src.telegram_runtime import inprocess_telegram_polling_enabled

    monkeypatch.setenv("RESTIA_INPROCESS_TASKS", "0")
    monkeypatch.setenv("ODYSSEUS_INPROCESS_TASKS", "0")
    monkeypatch.delenv("RESTIA_INPROCESS_TELEGRAM", raising=False)
    monkeypatch.delenv("ODYSSEUS_INPROCESS_TELEGRAM", raising=False)

    assert inprocess_telegram_polling_enabled() is True

    monkeypatch.setenv("RESTIA_INPROCESS_TELEGRAM", "0")
    assert inprocess_telegram_polling_enabled() is False


def test_telegram_routes_do_not_accept_a_task_scheduler_dependency():
    from routes.telegram_routes import setup_telegram_routes

    assert list(inspect.signature(setup_telegram_routes).parameters) == [
        "session_manager",
        "webhook_manager",
    ]


def test_all_compose_variants_expose_the_dedicated_telegram_gate():
    root = Path(__file__).resolve().parents[1]
    for name in (
        "docker-compose.yml",
        "docker-compose.gpu-nvidia.yml",
        "docker-compose.gpu-amd.yml",
    ):
        source = (root / name).read_text(encoding="utf-8")
        assert "RESTIA_INPROCESS_TELEGRAM=${RESTIA_INPROCESS_TELEGRAM:-}" in source
        assert "ODYSSEUS_INPROCESS_TELEGRAM=${ODYSSEUS_INPROCESS_TELEGRAM:-1}" in source


@pytest.mark.asyncio
async def test_polling_service_start_is_idempotent_and_stop_is_clean(monkeypatch):
    import src.telegram_runtime as runtime

    service = runtime.TelegramPollingService()
    entered = asyncio.Event()

    monkeypatch.setattr(runtime, "load_settings", lambda: {})
    monkeypatch.setattr(
        runtime,
        "load_telegram_config",
        lambda: SimpleNamespace(enabled=False, bot_token=""),
    )

    async def wait_forever(_seconds):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_wait", wait_forever)

    first = service.start()
    second = service.start()
    assert second is first
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert service.status()["poller_running"] is True

    await service.stop()
    assert first.done()
    assert service.status()["poller_running"] is False
    assert service._runner_task is None

    # Shutdown hooks can safely run after partial startup or repeated lifespan
    # teardown without reviving or double-awaiting the worker.
    await service.stop()


@pytest.mark.asyncio
async def test_direct_duplicate_polling_runner_is_rejected(monkeypatch):
    import src.telegram_runtime as runtime

    service = runtime.TelegramPollingService()
    entered = asyncio.Event()
    waits = 0

    monkeypatch.setattr(runtime, "load_settings", lambda: {})
    monkeypatch.setattr(
        runtime,
        "load_telegram_config",
        lambda: SimpleNamespace(enabled=False, bot_token=""),
    )

    async def wait_forever(_seconds):
        nonlocal waits
        waits += 1
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_wait", wait_forever)

    owner = asyncio.create_task(service.run())
    await asyncio.wait_for(entered.wait(), timeout=1)
    duplicate = asyncio.create_task(service.run())
    await asyncio.wait_for(duplicate, timeout=1)

    assert waits == 1
    assert service._runner_task is owner

    await service.stop()
    assert owner.done()
