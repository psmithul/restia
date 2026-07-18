from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    ActionProposal,
    Base,
    EmailOutboundDelivery,
    LifeEntity,
    ScheduledTask,
)
from routes.life_automation_routes import setup_life_automation_routes
from src.identity import ensure_account
from src.life_graph import create_life_entity, create_life_source


ROOT = Path(__file__).resolve().parents[1]


class _IdentityAuthority:
    def __init__(self, *usernames: str):
        self._config_lock = threading.Lock()
        self._identity_migrations: set[str] = set()
        self.retired_usernames: set[str] = set()
        self.users = {name: {} for name in usernames}

    @property
    def is_configured(self) -> bool:
        return bool(self.users)


@pytest.fixture()
def automation_api(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'life-automation-routes.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        alice = ensure_account(db, "alice")
        bob = ensure_account(db, "bob")
        db.commit()
        account_ids = {"alice": alice.id, "bob": bob.id}
    finally:
        db.close()

    app = FastAPI()
    app.state.auth_manager = _IdentityAuthority("alice", "bob")

    @app.middleware("http")
    async def inject_identity(request, call_next):
        token_owner = request.headers.get("x-api-owner")
        if token_owner:
            request.state.api_token = True
            request.state.api_token_owner = token_owner
            request.state.api_token_scopes = request.headers.get(
                "x-api-scopes", ""
            ).split(",")
            request.state.current_user = "api"
        else:
            request.state.api_token = False
            request.state.current_user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(setup_life_automation_routes(session_factory=factory))
    yield SimpleNamespace(
        app=app,
        Session=factory,
        engine=engine,
        account_ids=account_ids,
    )
    engine.dispose()


def _headers(
    *, user: str | None = "alice", owner: str | None = None, scopes: str = ""
) -> dict[str, str]:
    if owner is not None:
        return {"x-api-owner": owner, "x-api-scopes": scopes}
    return {"x-user": user} if user is not None else {}


def _definition_payload(*, key: str, action: dict | None = None) -> dict:
    return {
        "name": "Morning review",
        "description": "Prepare a deterministic review",
        "trigger": {"type": "time", "config": {"schedule_key": "morning"}},
        "actions": [
            action
            or {
                "type": "briefing",
                "config": {"title": "Daily review", "sections": ["risks"]},
            }
        ],
        "idempotency_key": key,
    }


def test_application_registers_the_typed_automation_router():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert (
        "from routes.life_automation_routes import setup_life_automation_routes"
        in source
    )
    assert "app.include_router(setup_life_automation_routes())" in source


@pytest.mark.asyncio
async def test_automation_routes_cover_owner_crud_idempotency_cas_and_history(
    automation_api,
):
    transport = httpx.ASGITransport(app=automation_api.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        payload = _definition_payload(key="route-definition-1")
        created = await client.post(
            "/api/life/automations",
            headers=_headers(),
            json=payload,
        )
        assert created.status_code == 201, created.text
        automation = created.json()["automation"]
        assert created.json()["created"] is True
        assert automation["status"] == "active"
        assert automation["version"] == 1

        retry = await client.post(
            "/api/life/automations", headers=_headers(), json=payload
        )
        assert retry.status_code == 201, retry.text
        assert retry.json()["created"] is False
        assert retry.json()["automation"]["id"] == automation["id"]

        mismatch_payload = dict(payload)
        mismatch_payload["name"] = "Mismatched retry"
        mismatch = await client.post(
            "/api/life/automations",
            headers=_headers(),
            json=mismatch_payload,
        )
        assert mismatch.status_code == 409

        listing = await client.get(
            "/api/life/automations", headers=_headers()
        )
        assert listing.status_code == 200, listing.text
        assert listing.json()["count"] == 1
        assert listing.json()["items"][0]["id"] == automation["id"]

        fetched = await client.get(
            f"/api/life/automations/{automation['id']}", headers=_headers()
        )
        assert fetched.status_code == 200
        assert fetched.json()["automation"] == automation

        hidden = await client.get(
            f"/api/life/automations/{automation['id']}",
            headers=_headers(user="bob"),
        )
        assert hidden.status_code == 404
        hidden_history = await client.get(
            f"/api/life/automations/{automation['id']}/history",
            headers=_headers(user="bob"),
        )
        assert hidden_history.status_code == 404

        updated = await client.patch(
            f"/api/life/automations/{automation['id']}",
            headers=_headers(),
            json={"version": 1, "name": "Paused review", "enabled": False},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["automation"]["version"] == 2
        assert updated.json()["automation"]["status"] == "paused"

        stale = await client.patch(
            f"/api/life/automations/{automation['id']}",
            headers=_headers(),
            json={"version": 1, "name": "Stale update"},
        )
        assert stale.status_code == 409
        stale_delete = await client.request(
            "DELETE",
            f"/api/life/automations/{automation['id']}",
            headers=_headers(),
            json={"version": 1, "reason": "Stale delete"},
        )
        assert stale_delete.status_code == 409

        history = await client.get(
            f"/api/life/automations/{automation['id']}/history",
            headers=_headers(),
        )
        assert history.status_code == 200, history.text
        assert [row["version"] for row in history.json()["items"]] == [2, 1]

        deleted = await client.request(
            "DELETE",
            f"/api/life/automations/{automation['id']}",
            headers=_headers(),
            json={"version": 2, "reason": "No longer needed"},
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["automation"]["version"] == 3
        assert deleted.json()["automation"]["status"] == "deleted"
        assert deleted.json()["automation"]["deleted_at"] is not None

        gone = await client.get(
            f"/api/life/automations/{automation['id']}", headers=_headers()
        )
        assert gone.status_code == 404
        deleted_history = await client.get(
            f"/api/life/automations/{automation['id']}/history",
            headers=_headers(),
        )
        assert deleted_history.status_code == 200, deleted_history.text
        assert [row["version"] for row in deleted_history.json()["items"]] == [
            3,
            2,
            1,
        ]

        invalid = dict(_definition_payload(key="invalid-extra"))
        invalid["execute_now"] = True
        rejected = await client.post(
            "/api/life/automations", headers=_headers(), json=invalid
        )
        assert rejected.status_code == 422

    db = automation_api.Session()
    try:
        row = db.query(LifeEntity).filter(LifeEntity.id == automation["id"]).one()
        assert row.owner_id == automation_api.account_ids["alice"]
        assert row.owner_id != automation_api.account_ids["bob"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_evaluation_is_life_read_and_preparation_never_executes_or_sends(
    automation_api,
):
    notification = {
        "type": "notification",
        "config": {
            "channel": "web",
            "title": "Review required",
            "message": "A prepared action is waiting for review.",
        },
    }
    transport = httpx.ASGITransport(app=automation_api.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        read_cannot_create = await client.post(
            "/api/life/automations",
            headers=_headers(user=None, owner="alice", scopes="life:read"),
            json=_definition_payload(key="read-cannot-create", action=notification),
        )
        assert read_cannot_create.status_code == 403

        created = await client.post(
            "/api/life/automations",
            headers=_headers(user=None, owner="alice", scopes="life:write"),
            json=_definition_payload(key="route-notification", action=notification),
        )
        assert created.status_code == 201, created.text
        automation_id = created.json()["automation"]["id"]

        write_implies_read = await client.get(
            f"/api/life/automations/{automation_id}",
            headers=_headers(user=None, owner="alice", scopes="life:write"),
        )
        assert write_implies_read.status_code == 200

        db = automation_api.Session()
        try:
            before = {
                "entities": db.query(LifeEntity).count(),
                "proposals": db.query(ActionProposal).count(),
                "deliveries": db.query(EmailOutboundDelivery).count(),
                "scheduled": db.query(ScheduledTask).count(),
            }
        finally:
            db.close()

        event = {"type": "time", "schedule_key": "morning"}
        evaluated = await client.post(
            f"/api/life/automations/{automation_id}/evaluate",
            headers=_headers(user=None, owner="alice", scopes="life:read"),
            json={"event": event},
        )
        assert evaluated.status_code == 200, evaluated.text
        evaluation = evaluated.json()["evaluation"]
        assert evaluation["matched"] is True
        assert [plan["type"] for plan in evaluation["plans"]] == ["notification"]
        assert evaluation["execution_contract"]["evaluation_side_effects"] is False

        wrong_scope = await client.post(
            f"/api/life/automations/{automation_id}/evaluate",
            headers=_headers(user=None, owner="alice", scopes="todos:read"),
            json={"event": event},
        )
        assert wrong_scope.status_code == 403
        cross_owner = await client.post(
            f"/api/life/automations/{automation_id}/evaluate",
            headers=_headers(user="bob"),
            json={"event": event},
        )
        assert cross_owner.status_code == 404

        read_cannot_prepare = await client.post(
            f"/api/life/automations/{automation_id}/prepare",
            headers=_headers(user=None, owner="alice", scopes="life:read"),
            json={"event": event, "idempotency_key": "route-run-1"},
        )
        assert read_cannot_prepare.status_code == 403

        db = automation_api.Session()
        try:
            assert db.query(LifeEntity).count() == before["entities"]
            assert db.query(ActionProposal).count() == before["proposals"]
            assert db.query(EmailOutboundDelivery).count() == before["deliveries"]
            assert db.query(ScheduledTask).count() == before["scheduled"]
        finally:
            db.close()

        prepared = await client.post(
            f"/api/life/automations/{automation_id}/prepare",
            headers=_headers(user=None, owner="alice", scopes="life:write"),
            json={"event": event, "idempotency_key": "route-run-1"},
        )
        assert prepared.status_code == 200, prepared.text
        preparation = prepared.json()
        assert preparation["created"] is True
        assert preparation["run"]["status"] == "prepared"
        assert len(preparation["proposal_ids"]) == 1
        assert preparation["confirmation_required"] is True
        assert preparation["confirmation_tokens_returned"] is False
        assert "confirmation_tokens" not in preparation
        assert preparation["execution_contract"]["classification_external_actions"] is False

        retry = await client.post(
            f"/api/life/automations/{automation_id}/prepare",
            headers=_headers(user=None, owner="alice", scopes="life:write"),
            json={"event": event, "idempotency_key": "route-run-1"},
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["created"] is False
        assert retry.json()["run"]["id"] == preparation["run"]["id"]
        assert retry.json()["proposal_ids"] == preparation["proposal_ids"]

    db = automation_api.Session()
    try:
        assert db.query(LifeEntity).count() == before["entities"] + 1
        proposals = db.query(ActionProposal).all()
        assert len(proposals) == before["proposals"] + 1
        assert proposals[-1].owner_id == automation_api.account_ids["alice"]
        assert proposals[-1].state == "prepared"
        assert proposals[-1].requires_confirmation is True
        assert proposals[-1].external is True
        assert db.query(EmailOutboundDelivery).count() == before["deliveries"]
        assert db.query(ScheduledTask).count() == before["scheduled"]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_meeting_end_route_only_prepares_owned_source_backed_followups(
    automation_api,
):
    db = automation_api.Session()
    try:
        alice = db.query(Account).filter(Account.username == "alice").one()
        bob = db.query(Account).filter(Account.username == "bob").one()
        source = create_life_source(
            db,
            account=alice,
            source_type="meeting_record",
            title="Design review notes",
            idempotency_key="route-meeting-source",
        )[0]
        bob_source = create_life_source(
            db,
            account=bob,
            source_type="meeting_record",
            title="Bob notes",
            idempotency_key="route-bob-meeting-source",
        )[0]
        meeting = create_life_entity(
            db,
            account=alice,
            entity_type="event",
            title="Design review",
            idempotency_key="route-meeting-event",
        )[0]
        db.commit()
        source_id = source.id
        bob_source_id = bob_source.id
        meeting_id = meeting.id
        deliveries_before = db.query(EmailOutboundDelivery).count()
        scheduled_before = db.query(ScheduledTask).count()
    finally:
        db.close()

    payload = {
        "meeting_entity_id": meeting_id,
        "source_ids": [source_id],
        "follow_ups": [
            {
                "channel": "restia_message",
                "recipient": "person-1",
                "body": "Please review the cited next steps.",
            }
        ],
        "ended_at": "2026-07-17T10:00:00Z",
        "idempotency_key": "route-meeting-end-1",
    }
    transport = httpx.ASGITransport(app=automation_api.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        prepared = await client.post(
            "/api/life/automations/meeting-end/prepare",
            headers=_headers(),
            json=payload,
        )
        assert prepared.status_code == 200, prepared.text
        body = prepared.json()
        assert body["created"] is True
        assert len(body["proposal_ids"]) == 1
        assert body["confirmation_tokens_returned"] is False
        assert "confirmation_tokens" not in body

        retry = await client.post(
            "/api/life/automations/meeting-end/prepare",
            headers=_headers(),
            json=payload,
        )
        assert retry.status_code == 200, retry.text
        assert retry.json()["created"] is False
        assert retry.json()["proposal_ids"] == body["proposal_ids"]

        cross_owner_source = dict(payload)
        cross_owner_source["source_ids"] = [bob_source_id]
        cross_owner_source["idempotency_key"] = "route-meeting-end-cross-owner"
        hidden = await client.post(
            "/api/life/automations/meeting-end/prepare",
            headers=_headers(),
            json=cross_owner_source,
        )
        assert hidden.status_code == 404

    db = automation_api.Session()
    try:
        proposal = db.query(ActionProposal).filter(
            ActionProposal.id == body["proposal_ids"][0]
        ).one()
        assert proposal.owner_id == automation_api.account_ids["alice"]
        assert proposal.state == "prepared"
        assert proposal.requires_confirmation is True
        assert db.query(EmailOutboundDelivery).count() == deliveries_before
        assert db.query(ScheduledTask).count() == scheduled_before
    finally:
        db.close()
