"""Database-fenced leadership for singleton background runtime roles.

Every web replica may run this authority, but only the holder of the current
database-time lease and fencing token may drive a singleton scheduler/poller.
The token changes on every acquisition, so a paused former process cannot
renew or release a newer holder's lease after it resumes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from core.database import RuntimeWorkerLease, SessionLocal


logger = logging.getLogger(__name__)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_HOLDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$")
MIN_LEASE_SECONDS = 15
MAX_LEASE_SECONDS = 600
DEFAULT_LEASE_SECONDS = 45


class RuntimeLeadershipError(RuntimeError):
    """The leadership request or stored state is invalid."""


@dataclass(frozen=True)
class RuntimeLeadershipToken:
    lease_name: str
    holder_id: str
    fencing_token: int
    version: int
    expires_at: datetime


def new_runtime_holder_id(prefix: str = "worker") -> str:
    clean = re.sub(r"[^A-Za-z0-9_.:@-]", "-", str(prefix or "worker"))[:40]
    clean = clean.strip("-._:@") or "worker"
    return f"{clean}:{uuid.uuid4().hex}"


def _lease_name(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if not _NAME_RE.fullmatch(normalized):
        raise RuntimeLeadershipError("Runtime lease name is invalid")
    return normalized


def _holder_id(value: object) -> str:
    normalized = str(value or "").strip()
    if not _HOLDER_RE.fullmatch(normalized):
        raise RuntimeLeadershipError("Runtime lease holder is invalid")
    return normalized


def _duration(value: object) -> timedelta:
    if isinstance(value, bool):
        raise RuntimeLeadershipError("Runtime lease duration is invalid")
    try:
        seconds = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeLeadershipError("Runtime lease duration is invalid") from exc
    if seconds < MIN_LEASE_SECONDS or seconds > MAX_LEASE_SECONDS:
        raise RuntimeLeadershipError(
            f"Runtime lease duration must be {MIN_LEASE_SECONDS}–{MAX_LEASE_SECONDS} seconds"
        )
    return timedelta(seconds=seconds)


def _normalize_db_time(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        try:
            result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise RuntimeLeadershipError("Database clock returned an invalid value") from exc
    if result.tzinfo is not None:
        result = result.astimezone(timezone.utc).replace(tzinfo=None)
    return result


def database_now(db) -> datetime:
    """Read the database clock; process wall clocks never decide expiry."""

    return _normalize_db_time(
        db.execute(select(func.current_timestamp())).scalar_one()
    )


def _begin_serialized_write(db) -> None:
    if db.get_bind().dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _token(row: RuntimeWorkerLease) -> RuntimeLeadershipToken:
    if (
        not row.holder_id
        or row.lease_expires_at is None
        or int(row.fencing_token or 0) < 1
        or int(row.version or 0) < 1
    ):
        raise RuntimeLeadershipError("Stored runtime leadership state is malformed")
    return RuntimeLeadershipToken(
        lease_name=str(row.lease_name),
        holder_id=str(row.holder_id),
        fencing_token=int(row.fencing_token),
        version=int(row.version),
        expires_at=_normalize_db_time(row.lease_expires_at),
    )


class RuntimeLeadershipAuthority:
    """Small SQLAlchemy port shared by app lifecycles and worker CLIs."""

    def __init__(self, session_factory: Callable = SessionLocal) -> None:
        self._session_factory = session_factory

    def acquire(
        self,
        *,
        lease_name: object,
        holder_id: object,
        lease_seconds: object = DEFAULT_LEASE_SECONDS,
    ) -> RuntimeLeadershipToken | None:
        name = _lease_name(lease_name)
        holder = _holder_id(holder_id)
        duration = _duration(lease_seconds)
        # A concurrent first insert can lose its uniqueness race. Roll back and
        # retry once so it observes the winner instead of surfacing a 500.
        for attempt in range(2):
            db = self._session_factory()
            try:
                _begin_serialized_write(db)
                clock = database_now(db)
                row = db.query(RuntimeWorkerLease).filter(
                    RuntimeWorkerLease.lease_name == name
                ).with_for_update().one_or_none()
                if row is None:
                    row = RuntimeWorkerLease(
                        lease_name=name,
                        holder_id=holder,
                        fencing_token=1,
                        lease_expires_at=clock + duration,
                        heartbeat_at=clock,
                        version=1,
                    )
                    db.add(row)
                else:
                    expires_at = (
                        _normalize_db_time(row.lease_expires_at)
                        if row.lease_expires_at is not None else None
                    )
                    available = (
                        row.holder_id is None
                        or expires_at is None
                        or expires_at <= clock
                        or str(row.holder_id) == holder
                    )
                    if not available:
                        db.rollback()
                        return None
                    row.holder_id = holder
                    row.fencing_token = int(row.fencing_token or 0) + 1
                    row.lease_expires_at = clock + duration
                    row.heartbeat_at = clock
                    row.version = int(row.version or 0) + 1
                    row.updated_at = clock
                db.commit()
                db.refresh(row)
                return _token(row)
            except IntegrityError:
                db.rollback()
                if attempt:
                    raise RuntimeLeadershipError(
                        "Runtime leadership row could not be initialized"
                    )
            finally:
                db.close()
        return None

    def renew(
        self,
        token: RuntimeLeadershipToken,
        *,
        lease_seconds: object = DEFAULT_LEASE_SECONDS,
    ) -> RuntimeLeadershipToken | None:
        duration = _duration(lease_seconds)
        name = _lease_name(token.lease_name)
        holder = _holder_id(token.holder_id)
        db = self._session_factory()
        try:
            _begin_serialized_write(db)
            clock = database_now(db)
            row = db.query(RuntimeWorkerLease).filter(
                RuntimeWorkerLease.lease_name == name,
                RuntimeWorkerLease.holder_id == holder,
                RuntimeWorkerLease.fencing_token == int(token.fencing_token),
                RuntimeWorkerLease.version == int(token.version),
                RuntimeWorkerLease.lease_expires_at > clock,
            ).with_for_update().one_or_none()
            if row is None:
                db.rollback()
                return None
            row.lease_expires_at = clock + duration
            row.heartbeat_at = clock
            row.version = int(row.version) + 1
            row.updated_at = clock
            db.commit()
            db.refresh(row)
            return _token(row)
        finally:
            db.close()

    def release(self, token: RuntimeLeadershipToken) -> bool:
        name = _lease_name(token.lease_name)
        holder = _holder_id(token.holder_id)
        db = self._session_factory()
        try:
            _begin_serialized_write(db)
            clock = database_now(db)
            changed = db.query(RuntimeWorkerLease).filter(
                RuntimeWorkerLease.lease_name == name,
                RuntimeWorkerLease.holder_id == holder,
                RuntimeWorkerLease.fencing_token == int(token.fencing_token),
                RuntimeWorkerLease.version == int(token.version),
            ).update({
                RuntimeWorkerLease.holder_id: None,
                RuntimeWorkerLease.lease_expires_at: None,
                RuntimeWorkerLease.heartbeat_at: clock,
                RuntimeWorkerLease.version: int(token.version) + 1,
                RuntimeWorkerLease.updated_at: clock,
            }, synchronize_session=False)
            if changed != 1:
                db.rollback()
                return False
            db.commit()
            return True
        finally:
            db.close()

    def status(self, lease_name: object) -> dict[str, object]:
        name = _lease_name(lease_name)
        db = self._session_factory()
        try:
            clock = database_now(db)
            row = db.query(RuntimeWorkerLease).filter(
                RuntimeWorkerLease.lease_name == name
            ).one_or_none()
            if row is None:
                return {
                    "lease_name": name,
                    "held": False,
                    "fencing_token": 0,
                    "version": 0,
                }
            expires = (
                _normalize_db_time(row.lease_expires_at)
                if row.lease_expires_at is not None else None
            )
            return {
                "lease_name": name,
                "held": bool(row.holder_id and expires and expires > clock),
                # Never expose a replica identifier through diagnostics.
                "fencing_token": int(row.fencing_token or 0),
                "version": int(row.version or 0),
                "expires_at": expires.isoformat() if expires else None,
            }
        finally:
            db.close()


def shared_database_mode(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return str(
        env.get("RESTIA_DATABASE_MODE")
        or env.get("ODYSSEUS_DATABASE_MODE")
        or "local-single"
    ).strip().lower() == "shared"


async def run_database_leased_worker(
    lease_name: str,
    worker_factory: Callable[[], object],
    *,
    session_factory: Callable = SessionLocal,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    poll_seconds: float = 10.0,
) -> None:
    """Run one long-lived worker locally or behind shared DB leadership.

    In shared mode every replica may enter this function. Standbys poll the
    database; a holder renews while the worker runs; lease loss cancels that
    worker before the replica returns to standby. The factory must create a
    fresh coroutine for each takeover.
    """

    if not callable(worker_factory):
        raise RuntimeLeadershipError("Runtime worker factory must be callable")
    if not shared_database_mode():
        await worker_factory()
        return

    bounded_poll = max(0.05, min(float(poll_seconds), lease_seconds / 3))
    authority = RuntimeLeadershipAuthority(session_factory)
    holder = new_runtime_holder_id(lease_name)
    token: RuntimeLeadershipToken | None = None
    worker: asyncio.Task | None = None
    try:
        while True:
            if token is None:
                acquire_task = asyncio.create_task(asyncio.to_thread(
                    authority.acquire,
                    lease_name=lease_name,
                    holder_id=holder,
                    lease_seconds=lease_seconds,
                ))
                try:
                    token = await asyncio.shield(acquire_task)
                except asyncio.CancelledError:
                    # Cancelling ``to_thread`` does not stop the database call.
                    # Observe its result and release a just-acquired lease so a
                    # standby does not wait for expiry during clean shutdown.
                    try:
                        acquired = await acquire_task
                        if acquired is not None:
                            await asyncio.shield(asyncio.to_thread(
                                authority.release, acquired,
                            ))
                    except Exception:
                        logger.exception(
                            "Runtime worker %s cancelled acquisition cleanup failed",
                            lease_name,
                        )
                    raise
                except Exception:
                    logger.exception(
                        "Runtime worker %s leadership acquisition failed", lease_name
                    )
                    token = None
                if token is None:
                    await asyncio.sleep(bounded_poll)
                    continue
                worker = asyncio.create_task(
                    worker_factory(), name=f"restia-{lease_name}-leader"
                )
                logger.info("Runtime worker %s acquired leadership", lease_name)

            done, _pending = await asyncio.wait(
                {worker}, timeout=bounded_poll, return_when=asyncio.FIRST_COMPLETED
            )
            if done:
                # A long-lived worker returning is explicit failure. Release so
                # another replica can retry; propagate its exception when any.
                await worker
                released = await asyncio.to_thread(authority.release, token)
                if not released:
                    logger.warning(
                        "Runtime worker %s completion release was fenced", lease_name
                    )
                token = None
                worker = None
                await asyncio.sleep(bounded_poll)
                continue
            renew_task = asyncio.create_task(asyncio.to_thread(
                authority.renew, token, lease_seconds=lease_seconds
            ))
            try:
                renewed = await asyncio.shield(renew_task)
            except asyncio.CancelledError:
                # A renewal may already have committed after the coroutine was
                # cancelled. Capture its newest fencing version so ``finally``
                # can explicitly release that exact lease instead of leaving a
                # healthy standby blocked until timeout.
                try:
                    renewed_after_cancel = await renew_task
                    if renewed_after_cancel is not None:
                        token = renewed_after_cancel
                except Exception:
                    logger.exception(
                        "Runtime worker %s cancelled renewal cleanup failed",
                        lease_name,
                    )
                raise
            except Exception:
                logger.exception(
                    "Runtime worker %s leadership renewal failed", lease_name
                )
                renewed = None
            if renewed is not None:
                token = renewed
                continue

            logger.error(
                "Runtime worker %s lost leadership; cancelling local worker",
                lease_name,
            )
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
            token = None
            worker = None
    finally:
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        if token is not None:
            try:
                released = await asyncio.shield(asyncio.to_thread(
                    authority.release, token,
                ))
                if not released:
                    logger.warning(
                        "Runtime worker %s shutdown release was fenced", lease_name
                    )
            except Exception:
                logger.exception(
                    "Runtime worker %s shutdown release failed", lease_name
                )


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "RuntimeLeadershipAuthority",
    "RuntimeLeadershipError",
    "RuntimeLeadershipToken",
    "database_now",
    "new_runtime_holder_id",
    "run_database_leased_worker",
    "shared_database_mode",
]
