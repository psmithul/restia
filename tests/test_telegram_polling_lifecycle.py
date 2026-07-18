"""Telegram polling owns a lifecycle independent from scheduled Tasks."""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Ownership handoff is retried on a 0.01s cadence in these tests, but a loaded
# full-suite run can starve the pollers for seconds — wait on wall-clock time,
# not a fixed iteration count.
TAKEOVER_TIMEOUT_SECONDS = 15.0


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
    assert service._process_lease.owned is False

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


@pytest.mark.asyncio
async def test_only_one_local_process_service_polls_and_standby_takes_over(
    monkeypatch, tmp_path
):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import src.telegram_runtime as runtime
    from src.telegram_delivery import TelegramPollingState, TelegramRuntimeAuthority

    lock_path = tmp_path / "telegram_polling.lock"
    engine = create_engine(
        f"sqlite:///{tmp_path / 'local-process-runtime.db'}",
        connect_args={"check_same_thread": False},
    )
    # Use the metadata bound to the authority's model. Some migration tests
    # deliberately reload ``core.database`` during the full suite.
    TelegramPollingState.metadata.create_all(engine)
    authority = TelegramRuntimeAuthority(sessionmaker(bind=engine, autoflush=False))
    first = runtime.TelegramPollingService(
        process_lock_path=lock_path, runtime_authority=authority
    )
    second = runtime.TelegramPollingService(
        process_lock_path=lock_path, runtime_authority=authority
    )
    first.configure(lambda _update: asyncio.sleep(0))
    second.configure(lambda _update: asyncio.sleep(0))
    first.OWNERSHIP_RETRY_SECONDS = 0.01
    second.OWNERSHIP_RETRY_SECONDS = 0.01

    monkeypatch.setattr(runtime, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runtime, "load_settings", lambda: {})
    # Mode resolution consults the suite-global core.database session, which
    # other full-suite tests may leave unusable; this test owns its own engine
    # and is only about lease takeover, so pin the transport.
    monkeypatch.setattr(runtime, "telegram_runtime_mode", lambda settings=None: "polling")
    monkeypatch.setattr(
        runtime,
        "load_telegram_config",
        lambda: SimpleNamespace(enabled=True, bot_token="123456:abcdefghijklmnopqrstuvwxyz"),
    )

    entered_calls = 0
    hold_call = asyncio.Event()

    async def blocking_api_call(*_args, **_kwargs):
        nonlocal entered_calls
        entered_calls += 1
        await hold_call.wait()
        return {"ok": True, "result": []}

    monkeypatch.setattr(runtime, "telegram_api_call", blocking_api_call)

    first_task = first.start()
    second_task = second.start()
    try:
        deadline = asyncio.get_running_loop().time() + TAKEOVER_TIMEOUT_SECONDS
        while True:
            statuses = (first.status(), second.status())
            if entered_calls == 1 and sum(s["poller_standby"] for s in statuses) == 1:
                break
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("one poller did not become owner while the other stood by")
            await asyncio.sleep(0.01)

        assert sum(s["poller_running"] for s in statuses) == 1
        assert entered_calls == 1

        owner, standby = (first, second) if first.status()["poller_running"] else (second, first)
        await owner.stop()

        deadline = asyncio.get_running_loop().time() + TAKEOVER_TIMEOUT_SECONDS
        while True:
            if standby.status()["poller_running"] and entered_calls == 2:
                break
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("standby poller did not take ownership after owner stopped")
            await asyncio.sleep(0.01)

        assert standby.status()["poller_standby"] is False
        assert entered_calls == 2
    finally:
        await first.stop()
        await second.stop()
        hold_call.set()
        await asyncio.gather(first_task, second_task, return_exceptions=True)
        engine.dispose()


@pytest.mark.asyncio
async def test_database_lease_coordinates_two_hosts_and_standby_takes_over(
    monkeypatch, tmp_path,
):
    import hashlib

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import src.telegram_runtime as runtime
    from src.telegram_delivery import TelegramPollingState, TelegramRuntimeAuthority

    engine = create_engine(
        f"sqlite:///{tmp_path / 'shared-runtime.db'}",
        connect_args={"check_same_thread": False},
    )
    TelegramPollingState.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    authority = TelegramRuntimeAuthority(factory)
    fingerprint = hashlib.sha256(b"shared-bot").hexdigest()
    first = runtime.TelegramPollingService(
        process_lock_path=tmp_path / "host-a.lock",
        runtime_authority=authority,
        worker_id="host-a",
    )
    second = runtime.TelegramPollingService(
        process_lock_path=tmp_path / "host-b.lock",
        runtime_authority=authority,
        worker_id="host-b",
    )
    first.configure(lambda _update: asyncio.sleep(0))
    second.configure(lambda _update: asyncio.sleep(0))
    first.OWNERSHIP_RETRY_SECONDS = 0.01
    second.OWNERSHIP_RETRY_SECONDS = 0.01

    monkeypatch.setattr(runtime, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runtime, "load_settings", lambda: {})
    # See test_only_one_local_process_service_polls_and_standby_takes_over:
    # keep this lease test independent of the suite-global database.
    monkeypatch.setattr(runtime, "telegram_runtime_mode", lambda settings=None: "polling")
    monkeypatch.setattr(
        runtime,
        "load_telegram_config",
        lambda: SimpleNamespace(
            enabled=True,
            bot_token="123456:abcdefghijklmnopqrstuvwxyz",
            bot_fingerprint=fingerprint,
        ),
    )

    calls: list[str] = []
    hold_call = asyncio.Event()

    async def blocking_api_call(*_args, **_kwargs):
        task = asyncio.current_task()
        calls.append(task.get_name() if task is not None else "unknown")
        await hold_call.wait()
        return {"ok": True, "result": []}

    monkeypatch.setattr(runtime, "telegram_api_call", blocking_api_call)
    first_task = first.start()
    second_task = second.start()
    try:
        deadline = asyncio.get_running_loop().time() + TAKEOVER_TIMEOUT_SECONDS
        while True:
            statuses = (first.status(), second.status())
            if len(calls) == 1 and sum(s["poller_standby"] for s in statuses) == 1:
                break
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("database lease did not select exactly one polling host")
            await asyncio.sleep(0.01)

        assert sum(s["database_leased"] for s in statuses) == 1
        owner, standby = (
            (first, second)
            if first.status()["database_leased"]
            else (second, first)
        )
        await owner.stop()
        deadline = asyncio.get_running_loop().time() + TAKEOVER_TIMEOUT_SECONDS
        while True:
            if standby.status()["database_leased"] and len(calls) == 2:
                break
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("database standby did not take over after lease release")
            await asyncio.sleep(0.01)
        assert standby.status()["poller_standby"] is False
    finally:
        hold_call.set()
        await first.stop()
        await second.stop()
        await asyncio.gather(first_task, second_task, return_exceptions=True)


def test_process_lease_rejects_symlink_lock_path(tmp_path):
    import src.telegram_runtime as runtime

    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("platform does not expose no-follow file opens")
    target = tmp_path / "target"
    target.write_text("not a lock", encoding="utf-8")
    lock_path = tmp_path / "telegram_polling.lock"
    lock_path.symlink_to(target)

    lease = runtime._TelegramPollingProcessLease(lock_path)
    with pytest.raises(OSError):
        lease.try_acquire()
    assert lease.owned is False


def test_process_lease_is_exclusive_across_python_processes(tmp_path):
    import src.telegram_runtime as runtime

    lock_path = tmp_path / "telegram_polling.lock"
    owner = runtime._TelegramPollingProcessLease(lock_path)
    assert owner.try_acquire() is True
    child_code = (
        "from pathlib import Path; "
        "from src.telegram_runtime import _TelegramPollingProcessLease as Lease; "
        f"lease = Lease(Path({str(lock_path)!r})); "
        "print(int(lease.try_acquire())); lease.release()"
    )

    try:
        blocked = subprocess.run(
            [sys.executable, "-c", child_code],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
        )
        assert blocked.stdout.strip() == "0"
    finally:
        owner.release()

    takeover = subprocess.run(
        [sys.executable, "-c", child_code],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    assert takeover.stdout.strip() == "1"
