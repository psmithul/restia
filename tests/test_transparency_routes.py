from __future__ import annotations

import json
import threading
import uuid
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    ActionAudit,
    ActionPolicy,
    ActionProposal,
    Base,
)
from routes.action_policy_routes import setup_action_policy_routes
from routes.transparency_routes import setup_transparency_routes


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
def transparency_api(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'transparency.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    alice = Account(id=str(uuid.uuid4()), username="alice")
    bob = Account(id=str(uuid.uuid4()), username="bob")
    db.add_all([alice, bob])
    db.flush()
    alice_audit = ActionAudit(
        id=str(uuid.uuid4()),
        owner_id=alice.id,
        action="task.updated",
        entity_type="task",
        entity_id="task-alice",
        before_state={"status": "open", "api_key": "audit-secret"},
        after_state={"status": "done", "content_digest": "safe-digest"},
        details={
            "audit": {
                "reason": "Completed from Today",
                "actor_type": "account",
                "actor_id": alice.id,
                "interface": "web",
                "credential_type": "session",
                "workflow_id": "workflow-1",
                "outcome": "completed",
                "idempotency_ref": "request-1",
                "reversible": True,
                "undo_ref": "task:task-alice:1",
            },
            "client_secret": "detail-secret",
            "explanation": "User marked the task complete",
        },
    )
    bob_audit = ActionAudit(
        id=str(uuid.uuid4()),
        owner_id=bob.id,
        action="task.updated",
        entity_type="task",
        entity_id="task-bob",
        before_state={"status": "open"},
        after_state={"status": "done"},
        details={},
    )
    policy = ActionPolicy(
        id=str(uuid.uuid4()),
        owner_id=alice.id,
        domain="email",
        max_autonomy=5,
        external_requires_confirmation=True,
        enabled=True,
        rules={"allowed": ["draft"], "client_secret": "policy-secret"},
        version=1,
    )
    proposal = ActionProposal(
        id=str(uuid.uuid4()),
        owner_id=alice.id,
        domain="email",
        action="send_email",
        autonomy_level=5,
        state="prepared",
        target_type="message",
        target_id="draft-1",
        payload={"subject": "Hello"},
        reason="User asked to send the reviewed draft",
        sources={"message": "draft-1"},
        external=True,
        requires_confirmation=True,
        confirmation_digest="a" * 64,
        result={},
        idempotency_key="sha256:" + "b" * 64,
        version=1,
    )
    db.add_all([alice_audit, bob_audit, policy, proposal])
    db.commit()
    ids = SimpleNamespace(
        alice=alice.id,
        alice_audit=alice_audit.id,
        bob_audit=bob_audit.id,
        proposal=proposal.id,
    )
    db.close()

    app = FastAPI()
    app.state.auth_manager = _IdentityAuthority("alice", "bob")

    @app.middleware("http")
    async def inject_identity(request, call_next):
        request.state.api_token = False
        request.state.current_user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(setup_transparency_routes(session_factory=factory))
    app.include_router(setup_action_policy_routes(session_factory=factory))
    yield SimpleNamespace(app=app, ids=ids)
    engine.dispose()


def _headers(user: str = "alice") -> dict[str, str]:
    return {"x-user": user}


@pytest.mark.asyncio
async def test_audit_inspection_is_structured_redacted_and_owner_scoped(
    transparency_api,
):
    transport = httpx.ASGITransport(app=transparency_api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listing = await client.get("/api/life/audit", headers=_headers())
        assert listing.status_code == 200, listing.text
        assert listing.json()["count"] == 1
        audit = listing.json()["items"][0]
        assert audit["id"] == transparency_api.ids.alice_audit
        assert audit["inputs"] == {
            "status": "open",
            "api_key": "[REDACTED]",
        }
        assert audit["changes"]["content_digest"] == "safe-digest"
        assert audit["reason"] == "Completed from Today"
        assert audit["actor"]["interface"] == "web"
        assert audit["workflow"]["outcome"] == "completed"
        assert audit["reversal"] == {
            "available": True,
            "undo_ref": "task:task-alice:1",
        }
        assert audit["details"]["client_secret"] == "[REDACTED]"

        hidden = await client.get(
            f"/api/life/audit/{transparency_api.ids.alice_audit}",
            headers=_headers("bob"),
        )
        assert hidden.status_code == 404
        own = await client.get(
            f"/api/life/audit/{transparency_api.ids.alice_audit}",
            headers=_headers(),
        )
        assert own.status_code == 200


@pytest.mark.asyncio
async def test_privacy_export_is_portable_bounded_and_contains_no_auth_material(
    transparency_api,
):
    transport = httpx.ASGITransport(app=transparency_api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/life/privacy-export", headers=_headers())
        assert response.status_code == 200, response.text
        export = response.json()
        assert export["schema"] == "restia.v3.privacy-export"
        assert export["principal"]["id"] == transparency_api.ids.alice
        assert export["complete"] is True
        assert export["collections"]["audits"]["count"] == 1
        assert export["collections"]["policies"]["items"][0]["rules"] == {
            "allowed": ["draft"],
            "client_secret": "[REDACTED]",
        }
        proposal = export["collections"]["action_proposals"]["items"][0]
        assert proposal["confirmation_pending"] is True
        rendered = json.dumps(export, sort_keys=True)
        for forbidden in (
            "audit-secret",
            "detail-secret",
            "policy-secret",
        ):
            assert forbidden not in rendered
        assert '"confirmation_digest":' not in rendered
        assert '"idempotency_key":' not in rendered
        assert transparency_api.ids.bob_audit not in rendered


@pytest.mark.asyncio
async def test_action_inspection_explains_inputs_reason_state_and_reversal(
    transparency_api,
):
    transport = httpx.ASGITransport(app=transparency_api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/api/life/actions/{transparency_api.ids.proposal}",
            headers=_headers(),
        )
        assert response.status_code == 200, response.text
        action = response.json()["action"]
        assert action["transparency"] == {
            "inputs": {"subject": "Hello"},
            "changes": {},
            "reason": "User asked to send the reviewed draft",
            "actor": {
                "prepared_by": "restia_workflow",
                "approved_by_account_id": None,
            },
            "workflow": {
                "domain": "email",
                "action": "send_email",
                "state": "prepared",
                "autonomy_level": 5,
                "external": True,
                "requires_confirmation": True,
            },
            "reversal": {
                "reference_recorded": False,
                "reviewed_server_reversal": False,
                "fresh_confirmation_required": True,
            },
        }
        rendered = json.dumps(action, sort_keys=True)
        assert "confirmation_digest" not in rendered
        assert "idempotency_key" not in rendered


def test_application_registers_transparency_routes():
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "app.py"
    ).read_text(encoding="utf-8")
    assert "from routes.transparency_routes import setup_transparency_routes" in source
    assert "app.include_router(setup_transparency_routes())" in source
