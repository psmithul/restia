"""Independent delivery loop for date-based work notifications.

Human planning items and project work items are not ``ScheduledTask`` rows, so
the task scheduler cannot discover their due dates.  This worker scans those
tables directly and routes every occurrence through ``dispatch_reminder``.
That gives due work the same durable claims, retry backoff, browser outbox,
quiet-hours policy, topic preferences, and Telegram delivery as note reminders.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_

from core.database import (
    Account,
    PlanningItem,
    Project,
    ProjectMember,
    ProjectWorkItem,
    ScheduledTask,
    SessionLocal,
    TaskRun,
)
from src.notification_preferences import load_notification_preferences

logger = logging.getLogger(__name__)

_runtime: dict[str, Any] = {
    "running": False,
    "last_scan_at": None,
    "last_success_at": None,
    "last_error_at": None,
    "last_error": "",
    "last_result": {},
}


def inprocess_due_notifications_enabled() -> bool:
    value = os.getenv("RESTIA_INPROCESS_NOTIFICATIONS", "1").strip().lower()
    return value not in {"0", "false", "no", "off", ""}


def due_notification_worker_status() -> dict[str, Any]:
    return {
        "enabled": inprocess_due_notifications_enabled(),
        **_runtime,
    }


def _local_today(owner: str, now: datetime) -> str:
    try:
        zone = ZoneInfo(str(load_notification_preferences(owner).get("timezone") or "UTC"))
    except Exception:
        zone = timezone.utc
    aware = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return aware.astimezone(zone).date().isoformat()


def _due_payloads(now: datetime) -> list[dict[str, str]]:
    """Load private text into bounded payloads, then close the DB before I/O."""

    db = SessionLocal()
    try:
        owners = [
            str(row[0]).strip().lower()
            for row in db.query(Account.username).filter(Account.status == "active").all()
            if row[0]
        ]
        payloads: list[dict[str, str]] = []
        for owner in sorted(set(owners)):
            today = _local_today(owner, now)
            planning_rows = (
                db.query(PlanningItem)
                .filter(
                    PlanningItem.owner == owner,
                    PlanningItem.status == "open",
                    PlanningItem.due_date.isnot(None),
                    PlanningItem.due_date <= today,
                )
                .order_by(PlanningItem.due_date.asc(), PlanningItem.id.asc())
                .limit(250)
                .all()
            )
            for item in planning_rows:
                overdue = str(item.due_date) < today
                detail = (item.details or "").strip()
                payloads.append({
                    "owner": owner,
                    "note_id": f"planning:{item.id}",
                    "occurrence": str(item.due_date or ""),
                    "topic": "todos",
                    "title": f"{'Overdue task' if overdue else 'Task due'}: {item.title}",
                    "body": "\n".join(part for part in (
                        f"Due {item.due_date}",
                        f"Priority: {item.priority}" if item.priority else "",
                        detail[:1200],
                    ) if part),
                })

            member_projects = db.query(ProjectMember.project_id).filter(
                func.lower(ProjectMember.username) == owner
            )
            project_rows = (
                db.query(ProjectWorkItem, Project)
                .join(Project, Project.id == ProjectWorkItem.project_id)
                .filter(
                    or_(func.lower(Project.owner) == owner, Project.id.in_(member_projects)),
                    Project.archived.is_(False),
                    Project.completed_at.is_(None),
                    ProjectWorkItem.archived.is_(False),
                    ProjectWorkItem.completed_at.is_(None),
                    ProjectWorkItem.due_date.isnot(None),
                    ProjectWorkItem.due_date <= today,
                )
                .order_by(ProjectWorkItem.due_date.asc(), ProjectWorkItem.id.asc())
                .limit(250)
                .all()
            )
            for item, project in project_rows:
                overdue = str(item.due_date) < today
                payloads.append({
                    "owner": owner,
                    "note_id": f"project-item:{item.id}",
                    "occurrence": str(item.due_date or ""),
                    "topic": "projects",
                    "title": (
                        f"{'Overdue project task' if overdue else 'Project task due'}: "
                        f"{project.key}-{item.item_number}"
                    ),
                    "body": "\n".join(part for part in (
                        item.title,
                        f"Project: {project.name}",
                        f"Due {item.due_date}",
                        f"Priority: {item.priority}" if item.priority else "",
                    ) if part),
                })

            # Task completion is dispatched immediately by TaskScheduler, but
            # provider failures need another caller to exercise the durable
            # retry claim. Rebuild recent payloads from SQL so retries survive
            # a process restart without storing a second private-text outbox.
            cutoff = (now if now.tzinfo is None else now.astimezone(timezone.utc).replace(tzinfo=None)) - timedelta(hours=24)
            run_rows = (
                db.query(TaskRun, ScheduledTask)
                .join(ScheduledTask, ScheduledTask.id == TaskRun.task_id)
                .filter(
                    ScheduledTask.owner == owner,
                    ScheduledTask.task_type.in_(("llm", "research")),
                    or_(
                        ScheduledTask.notifications_enabled.is_(True),
                        ScheduledTask.notifications_enabled.is_(None),
                    ),
                    TaskRun.status.in_(("success", "error")),
                    TaskRun.finished_at.isnot(None),
                    TaskRun.finished_at >= cutoff,
                )
                .order_by(TaskRun.finished_at.desc(), TaskRun.id.asc())
                .limit(250)
                .all()
            )
            for run, task in run_rows:
                succeeded = run.status == "success"
                payloads.append({
                    "owner": owner,
                    "note_id": f"scheduled-task:{task.id}",
                    "occurrence": str(run.id),
                    "topic": "tasks",
                    "title": f"Task {'completed' if succeeded else 'failed'}: {task.name}",
                    "body": str(
                        (run.result if succeeded else run.error)
                        or ("Task completed successfully" if succeeded else "Task execution failed")
                    )[:4000],
                })
        return payloads
    finally:
        db.close()


async def scan_due_notifications(
    *,
    now: datetime | None = None,
    dispatcher: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, int]:
    """Scan once. Durable reminder claims make repeated calls idempotent."""

    if dispatcher is None:
        from routes.note_routes import dispatch_reminder

        dispatcher = dispatch_reminder
    clock = now or datetime.now(timezone.utc)
    payloads = await asyncio.to_thread(_due_payloads, clock)
    result = {"scanned": len(payloads), "delivered": 0, "queued": 0, "deferred": 0, "suppressed": 0, "failed": 0}
    for payload in payloads:
        try:
            delivery = await dispatcher(
                title=payload["title"],
                note_body=payload["body"],
                note_id=payload["note_id"],
                owner=payload["owner"],
                topic=payload["topic"],
                occurrence=payload["occurrence"],
            )
            if delivery.get("suppressed") or delivery.get("skipped"):
                result["suppressed"] += 1
            elif delivery.get("delivered") or delivery.get("acknowledged"):
                result["delivered"] += 1
            elif delivery.get("browser_sent"):
                result["queued"] += 1
            elif delivery.get("deferred"):
                result["deferred"] += 1
            else:
                result["failed"] += 1
                logger.warning(
                    "Due notification delivery failed for %s (%s)",
                    payload["note_id"],
                    delivery.get("telegram_error")
                    or delivery.get("email_error")
                    or delivery.get("ntfy_error")
                    or delivery.get("webhook_error")
                    or delivery.get("suppression_reason")
                    or "no delivery path succeeded",
                )
        except Exception:
            result["failed"] += 1
            logger.exception("Due notification dispatch crashed for %s", payload["note_id"])
    return result


async def due_notification_loop() -> None:
    """Continuously scan due work without depending on the task scheduler."""

    interval = max(15, int(os.getenv("RESTIA_NOTIFICATION_SCAN_SECONDS", "60")))
    _runtime["running"] = True
    try:
        while True:
            stamp = datetime.now(timezone.utc).isoformat()
            _runtime["last_scan_at"] = stamp
            try:
                result = await scan_due_notifications()
                _runtime["last_result"] = result
                _runtime["last_success_at"] = datetime.now(timezone.utc).isoformat()
                _runtime["last_error"] = ""
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _runtime["last_error_at"] = datetime.now(timezone.utc).isoformat()
                _runtime["last_error"] = type(exc).__name__
                logger.exception("Due notification scan failed")
            await asyncio.sleep(interval)
    finally:
        _runtime["running"] = False
