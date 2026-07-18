"""Email poller lifecycle and shared-replica leadership contracts."""

from __future__ import annotations

import asyncio

import pytest

import core.database as cdb
from src.distributed_leadership import run_database_leased_worker
from tests.helpers.sqlite_db import make_temp_sqlite


@pytest.mark.asyncio
async def test_email_poller_uses_named_database_lease_with_ten_second_poll(
    monkeypatch,
):
    import routes.email_pollers as pollers
    import src.distributed_leadership as leadership

    captured = {}

    async def fake_run(lease_name, worker_factory, **kwargs):
        captured.update(
            lease_name=lease_name,
            worker_factory=worker_factory,
            kwargs=kwargs,
        )

    monkeypatch.setattr(leadership, "run_database_leased_worker", fake_run)

    await pollers._email_poller_leadership_loop()

    assert captured == {
        "lease_name": "email-poller",
        "worker_factory": pollers._scheduled_email_poller,
        "kwargs": {"lease_seconds": 45, "poll_seconds": 10},
    }


@pytest.mark.asyncio
async def test_shared_email_poller_has_one_leader_and_standby_takes_over(
    monkeypatch,
):
    Session, engine, _temp_db = make_temp_sqlite(cdb.Base.metadata)
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "shared")
    starts: list[int] = []
    running = asyncio.Event()

    def worker_factory(replica: int):
        async def worker():
            starts.append(replica)
            running.set()
            await asyncio.Event().wait()

        return worker

    first = asyncio.create_task(run_database_leased_worker(
        "email-poller", worker_factory(1), session_factory=Session,
        lease_seconds=15, poll_seconds=0.05,
    ))
    second = asyncio.create_task(run_database_leased_worker(
        "email-poller", worker_factory(2), session_factory=Session,
        lease_seconds=15, poll_seconds=0.05,
    ))
    try:
        await asyncio.wait_for(running.wait(), timeout=2)
        await asyncio.sleep(0.15)
        assert len(starts) == 1

        leader, standby = (first, second) if starts[0] == 1 else (second, first)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        for _ in range(40):
            if len(starts) == 2:
                break
            await asyncio.sleep(0.05)
        assert len(starts) == 2

        standby.cancel()
        with pytest.raises(asyncio.CancelledError):
            await standby
    finally:
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)
        engine.dispose()


@pytest.mark.asyncio
async def test_email_poller_stop_cancels_and_awaits_leadership_loop(monkeypatch):
    import routes.email_pollers as pollers

    started = asyncio.Event()
    stopped = asyncio.Event()

    async def fake_leadership_loop():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(pollers, "_email_poller_leadership_loop", fake_leadership_loop)
    monkeypatch.setattr(pollers, "_inprocess_pollers_enabled", lambda: True)
    monkeypatch.setattr(pollers, "_poller_task", None)
    monkeypatch.setattr(pollers, "_summarize_task", None)

    pollers._start_poller()
    await asyncio.wait_for(started.wait(), timeout=1)
    task = pollers._poller_task
    assert task is not None
    assert task.get_name() == "restia-email-poller-leadership"

    await pollers._stop_poller()

    assert stopped.is_set()
    assert task.cancelled()
    assert pollers._poller_task is None
    assert pollers._summarize_task is None
