import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

import core.database as cdb
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.database import DirectMessage
from routes import notification_center_routes as ncr


def _session_factory():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    engine = create_engine(
        f"sqlite:///{tmp.name}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def test_unread_message_notifications_are_owner_scoped_and_grouped(monkeypatch):
    sessions = _session_factory()
    monkeypatch.setattr(cdb, "SessionLocal", sessions)
    now = datetime.utcnow()
    latest_time = now - timedelta(minutes=1)
    db = sessions()
    try:
        db.add_all([
            DirectMessage(sender="alice", recipient="admin", body="first", created_at=now - timedelta(minutes=2)),
            DirectMessage(sender="alice", recipient="admin", body="latest", created_at=latest_time),
            DirectMessage(sender="bob", recipient="admin", body="already read", created_at=now, read_at=now),
            DirectMessage(sender="alice", recipient="bob", body="not for admin", created_at=now),
        ])
        db.commit()
    finally:
        db.close()

    notifications = ncr._messages_unread("admin")

    assert notifications == [{
        "sender": "alice",
        "preview": "latest",
        "last_at": latest_time.isoformat() + "Z",
        "unread": 2,
    }]


def test_message_notifications_require_concrete_owner():
    assert ncr._messages_unread("") == []


@pytest.mark.asyncio
async def test_notification_center_resolves_local_request_to_concrete_owner(monkeypatch):
    seen = []

    monkeypatch.setattr(
        ncr,
        "resolved_request_owner",
        lambda request, admitted_user: "owner@localhost",
    )
    for name in (
        "_emails_needing_reply",
        "_todos_due",
        "_events_upcoming",
        "_messages_unread",
    ):
        monkeypatch.setattr(
            ncr,
            name,
            lambda owner, section=name: seen.append((section, owner)) or [],
        )

    router = ncr.setup_notification_center_routes()
    endpoint = next(
        route.endpoint for route in router.routes if route.path == "/api/notifications/center"
    )
    result = await endpoint(SimpleNamespace(), admitted_user="")

    assert result["count"] == 0
    assert {owner for _, owner in seen} == {"owner@localhost"}
