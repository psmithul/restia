"""Contracts for database-time singleton worker leadership."""

from __future__ import annotations

import concurrent.futures
import asyncio
import types
from datetime import timedelta

import pytest

import core.database as cdb
from src.distributed_leadership import (
    RuntimeLeadershipAuthority,
    RuntimeLeadershipError,
    database_now,
    run_database_leased_worker,
)
from tests.helpers.sqlite_db import make_temp_sqlite


@pytest.fixture()
def leadership_env():
    Session, engine, temp_db = make_temp_sqlite(cdb.Base.metadata)
    yield Session, engine, temp_db
    engine.dispose()


def test_only_one_holder_acquires_and_database_expiry_allows_takeover(leadership_env):
    Session, _engine, _temp_db = leadership_env
    first = RuntimeLeadershipAuthority(Session)
    second = RuntimeLeadershipAuthority(Session)

    token = first.acquire(
        lease_name="task-scheduler", holder_id="replica-a", lease_seconds=45
    )
    assert token is not None
    assert second.acquire(
        lease_name="task-scheduler", holder_id="replica-b", lease_seconds=45
    ) is None

    db = Session()
    try:
        row = db.query(cdb.RuntimeWorkerLease).one()
        row.lease_expires_at = database_now(db) - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    replacement = second.acquire(
        lease_name="task-scheduler", holder_id="replica-b", lease_seconds=45
    )
    assert replacement is not None
    assert replacement.fencing_token > token.fencing_token
    assert first.renew(token) is None
    assert first.release(token) is False


def test_renewal_and_release_require_the_latest_fencing_version(leadership_env):
    Session, _engine, _temp_db = leadership_env
    authority = RuntimeLeadershipAuthority(Session)
    token = authority.acquire(
        lease_name="nightly-maintenance", holder_id="replica-a", lease_seconds=45
    )
    assert token is not None

    renewed = authority.renew(token, lease_seconds=60)
    assert renewed is not None
    assert renewed.fencing_token == token.fencing_token
    assert renewed.version == token.version + 1
    assert authority.renew(token) is None
    assert authority.release(token) is False
    assert authority.release(renewed) is True

    status = authority.status("nightly-maintenance")
    assert status["held"] is False
    assert "holder_id" not in status


def test_reacquiring_as_same_holder_fences_older_paused_process(leadership_env):
    Session, _engine, _temp_db = leadership_env
    authority = RuntimeLeadershipAuthority(Session)
    old = authority.acquire(
        lease_name="email-poller", holder_id="replica-a", lease_seconds=45
    )
    current = authority.acquire(
        lease_name="email-poller", holder_id="replica-a", lease_seconds=45
    )
    assert old is not None and current is not None
    assert current.fencing_token == old.fencing_token + 1
    assert authority.renew(old) is None
    assert authority.release(old) is False
    assert authority.renew(current) is not None


def test_concurrent_first_acquisition_has_exactly_one_winner(leadership_env):
    Session, _engine, _temp_db = leadership_env

    def acquire(holder: str):
        return RuntimeLeadershipAuthority(Session).acquire(
            lease_name="singleton", holder_id=holder, lease_seconds=45
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(acquire, [f"replica-{index}" for index in range(8)]))
    winners = [token for token in results if token is not None]
    assert len(winners) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lease_name", "../scheduler"),
        ("lease_name", ""),
        ("holder_id", "replica a"),
        ("holder_id", ""),
        ("lease_seconds", 14),
        ("lease_seconds", 601),
        ("lease_seconds", True),
    ],
)
def test_invalid_leadership_inputs_fail_closed(leadership_env, field, value):
    Session, _engine, _temp_db = leadership_env
    kwargs = {
        "lease_name": "task-scheduler",
        "holder_id": "replica-a",
        "lease_seconds": 45,
    }
    kwargs[field] = value
    with pytest.raises(RuntimeLeadershipError):
        RuntimeLeadershipAuthority(Session).acquire(**kwargs)


@pytest.mark.asyncio
async def test_leased_worker_is_transparent_in_local_single_mode(
    leadership_env, monkeypatch,
):
    Session, _engine, _temp_db = leadership_env
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "local-single")
    calls: list[str] = []

    async def worker():
        calls.append("ran")

    await run_database_leased_worker(
        "local-worker", worker, session_factory=Session, poll_seconds=0.05
    )
    assert calls == ["ran"]
    db = Session()
    try:
        assert db.query(cdb.RuntimeWorkerLease).count() == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_shared_leased_worker_has_one_active_replica_and_standby_takeover(
    leadership_env, monkeypatch,
):
    Session, _engine, _temp_db = leadership_env
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "shared")
    starts: list[int] = []
    running = asyncio.Event()

    def factory(replica: int):
        async def worker():
            starts.append(replica)
            running.set()
            await asyncio.Event().wait()

        return worker

    first = asyncio.create_task(run_database_leased_worker(
        "shared-worker", factory(1), session_factory=Session,
        lease_seconds=15, poll_seconds=0.05,
    ))
    second = asyncio.create_task(run_database_leased_worker(
        "shared-worker", factory(2), session_factory=Session,
        lease_seconds=15, poll_seconds=0.05,
    ))
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


@pytest.mark.asyncio
async def test_shared_task_schedulers_join_the_database_leadership_election(
    leadership_env, monkeypatch,
):
    Session, _engine, _temp_db = leadership_env
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "shared")
    monkeypatch.setattr(cdb, "SessionLocal", Session)
    from src.task_scheduler import TaskScheduler

    starts: list[str] = []

    async def fake_start(self):
        starts.append(self.label)
        self._running = True
        self._scheduler_disabled_reason = None
        return True

    async def fake_stop(self, *, reason):
        self._running = False

    first = TaskScheduler(None)
    second = TaskScheduler(None)
    first.label = "first"
    second.label = "second"
    first._start_leader_tasks = types.MethodType(fake_start, first)
    second._start_leader_tasks = types.MethodType(fake_start, second)
    first._stop_leader_tasks = types.MethodType(fake_stop, first)
    second._stop_leader_tasks = types.MethodType(fake_stop, second)

    assert await first.start() is True
    assert await second.start() is True
    assert starts == ["first"]
    assert first._leadership_token is not None
    assert second._leadership_token is None
    assert second._scheduler_disabled_reason == "leadership_standby"

    await second.stop()
    await first.stop()
