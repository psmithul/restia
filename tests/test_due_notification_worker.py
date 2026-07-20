from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import (
    Account,
    Base,
    PlanningItem,
    Project,
    ProjectMember,
    ProjectWorkItem,
    ScheduledTask,
    TaskRun,
)
from src import due_notification_worker as worker


def _factory(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr(worker, "SessionLocal", factory)
    monkeypatch.setattr(
        worker,
        "load_notification_preferences",
        lambda _owner: {"timezone": "UTC"},
    )
    return engine, factory


def test_due_scanner_routes_planning_and_accessible_project_items(monkeypatch):
    engine, factory = _factory(monkeypatch)
    with factory() as db:
        db.add_all([
            Account(id="account-alice", username="alice", status="active"),
            Account(id="account-bob", username="bob", status="active"),
            PlanningItem(
                id="plan-due", owner="alice", title="Submit application",
                details="Attach the controls portfolio", status="open",
                priority="high", due_date="2026-07-20", source="user", version=1,
            ),
            PlanningItem(
                id="plan-future", owner="alice", title="Future work",
                status="open", priority="normal", due_date="2026-07-22",
                source="user", version=1,
            ),
            Project(
                id="project-1", owner="alice", key="CTRL", name="Controls MS",
                description="", template="general", color="#123456",
                archived=False, next_item_number=2, version=1,
            ),
            ProjectMember(project_id="project-1", username="bob", role="viewer"),
            ProjectWorkItem(
                id="work-1", project_id="project-1", item_number=1,
                item_type="task", title="Finish SOP", description="",
                priority="critical", labels=[], reporter="alice",
                due_date="2026-07-19", estimate_minutes=0, logged_minutes=0,
                position=0, archived=False, version=1,
            ),
        ])
        db.commit()

    calls = []

    async def dispatch(**kwargs):
        calls.append(kwargs)
        return {"delivered": True}

    result = asyncio.run(worker.scan_due_notifications(
        now=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
        dispatcher=dispatch,
    ))

    assert result == {
        "scanned": 3,
        "delivered": 3,
        "queued": 0,
        "deferred": 0,
        "suppressed": 0,
        "failed": 0,
    }
    assert {(call["owner"], call["note_id"], call["topic"]) for call in calls} == {
        ("alice", "planning:plan-due", "todos"),
        ("alice", "project-item:work-1", "projects"),
        ("bob", "project-item:work-1", "projects"),
    }
    assert all(call["occurrence"] in {"2026-07-19", "2026-07-20"} for call in calls)
    engine.dispose()


def test_due_scanner_counts_browser_ack_wait_as_queued(monkeypatch):
    engine, factory = _factory(monkeypatch)
    with factory() as db:
        db.add(Account(id="account-alice", username="alice", status="active"))
        db.add(PlanningItem(
            id="plan-due", owner="alice", title="Due now", status="open",
            priority="normal", due_date="2026-07-20", source="user", version=1,
        ))
        db.commit()

    async def dispatch(**_kwargs):
        return {"browser_sent": True, "deferred": True}

    result = asyncio.run(worker.scan_due_notifications(
        now=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
        dispatcher=dispatch,
    ))
    assert result["queued"] == 1
    assert result["failed"] == 0
    engine.dispose()


def test_due_scanner_retries_recent_task_run_notifications(monkeypatch):
    engine, factory = _factory(monkeypatch)
    with factory() as db:
        db.add(Account(id="account-alice", username="alice", status="active"))
        db.add(ScheduledTask(
            id="task-1", owner="alice", name="Research controls labs",
            task_type="research", status="active", notifications_enabled=True,
        ))
        db.add(TaskRun(
            id="run-1", task_id="task-1",
            started_at=datetime(2026, 7, 20, 10),
            finished_at=datetime(2026, 7, 20, 11),
            status="success", result="Three labs shortlisted",
        ))
        db.commit()

    calls = []

    async def dispatch(**kwargs):
        calls.append(kwargs)
        return {"delivered": True}

    result = asyncio.run(worker.scan_due_notifications(
        now=datetime(2026, 7, 20, 12, tzinfo=timezone.utc),
        dispatcher=dispatch,
    ))
    assert result["delivered"] == 1
    assert calls[0]["note_id"] == "scheduled-task:task-1"
    assert calls[0]["occurrence"] == "run-1"
    assert calls[0]["topic"] == "tasks"
    engine.dispose()
