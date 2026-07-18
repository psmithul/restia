from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account, ActionAudit, ActionProposal, Base, InboxItem, LifeSource,
)
from routes.ambient_routes import setup_ambient_routes
from routes.action_policy_routes import setup_action_policy_routes
from src.action_policy import ActionPolicyDenied, approve_action
from src.ambient_capabilities import (
    AmbientCapabilityDenied,
    ambient_continuity,
    call_smart_home_connector,
    capture_ambient_signal,
    finish_smart_home_execution,
    list_ambient_capabilities,
    prepare_smart_home_action,
    set_ambient_capability,
    start_smart_home_execution,
    sync_offline_captures,
)
from src.audit_context import bind_request_audit_context
from src.identity import ensure_account
from src.life_core import LifeCoreError
from starlette.requests import Request


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

    def session_user_verification(self, _session_token):
        # Route fixtures model an already verified browser. API-token requests
        # are rejected before this server-side verification hook is consulted.
        return {"verified": True, "method": "webauthn"}


@pytest.fixture()
def ambient_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    import src.secret_storage as secret_storage

    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setattr(secret_storage, "_digest_key", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ambient.db'}",
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

    app.include_router(setup_ambient_routes(session_factory=factory))
    app.include_router(setup_action_policy_routes(session_factory=factory))
    yield SimpleNamespace(
        app=app,
        Session=factory,
        engine=engine,
        account_ids=account_ids,
    )
    engine.dispose()


def _enable(db, account: Account, capability: str, operations: list[str]):
    return set_ambient_capability(
        db,
        account=account,
        capability=capability,
        value={
            "enabled": True,
            "operations": operations,
            "local_only": True,
            "retention_days": 30,
            "require_device_unlock": True,
            "no_training": True,
        },
        expected_version=None,
        source="browser",
        idempotency_key=f"enable-{capability}",
    )


def test_ambient_defaults_are_private_opt_in_and_configuration_is_encrypted(ambient_env):
    db = ambient_env.Session()
    try:
        alice = db.query(Account).filter_by(username="alice").one()
        items = list_ambient_capabilities(db, owner_id=alice.id)
        assert [item["id"] for item in items] == [
            "mobile_voice", "location", "wearable", "transcription", "camera",
            "smart_home", "offline", "cross_device",
        ]
        assert all(item["no_training"] is True for item in items)
        assert next(item for item in items if item["id"] == "mobile_voice")["enabled"] is False
        assert next(item for item in items if item["id"] == "offline")["enabled"] is True

        updated = _enable(db, alice, "mobile_voice", ["capture", "read"])
        assert updated["enabled"] is True
        assert updated["version"] == 1
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id,
            action="ambient.capability.updated",
            entity_id="mobile_voice",
        ).count() == 1
        db.commit()

        raw = db.execute(text(
            "SELECT private_value FROM profile_configurations "
            "WHERE owner_id = :owner AND key = 'ambient.mobile_voice'"
        ), {"owner": alice.id}).scalar_one()
        assert "mobile_voice" not in str(raw)
        assert "enc:c1:" in str(raw)

        with pytest.raises(AmbientCapabilityDenied, match="training"):
            set_ambient_capability(
                db,
                account=alice,
                capability="location",
                value={"enabled": True, "operations": ["capture"], "no_training": False},
                expected_version=None,
                source="browser",
            )
    finally:
        db.rollback()
        db.close()


def test_ambient_capture_offline_replay_and_continuity_are_owner_scoped(ambient_env):
    db = ambient_env.Session()
    try:
        alice = db.query(Account).filter_by(username="alice").one()
        bob = db.query(Account).filter_by(username="bob").one()
        _enable(db, alice, "mobile_voice", ["capture", "read"])
        source, created = capture_ambient_signal(
            db,
            account=alice,
            capability="mobile_voice",
            payload={
                "transcript": "Remember the vibration test result, not a command.",
                "language": "en",
                "confidence": 97,
                "device_ref": "phone-a",
            },
            observed_at=datetime(2026, 7, 17, 8, tzinfo=timezone.utc),
            idempotency_key="voice-1",
            trusted_device_session=True,
        )
        replay, replay_created = capture_ambient_signal(
            db,
            account=alice,
            capability="mobile_voice",
            payload={
                "transcript": "Remember the vibration test result, not a command.",
                "language": "en",
                "confidence": 97,
                "device_ref": "phone-a",
            },
            observed_at=datetime(2026, 7, 17, 8, tzinfo=timezone.utc),
            idempotency_key="voice-1",
            trusted_device_session=True,
        )
        assert created is True
        assert replay_created is False
        assert replay.id == source.id
        assert source.meta_data["interpreted_as_instruction"] is False
        assert db.query(ActionProposal).filter_by(owner_id=alice.id).count() == 0
        voice_items = db.query(InboxItem).filter_by(
            owner_id=alice.id, source_type="voice",
        ).order_by(InboxItem.created_at.asc(), InboxItem.id.asc()).all()
        assert len(voice_items) == 1
        assert voice_items[0].content == (
            "Remember the vibration test result, not a command."
        )
        assert voice_items[0].source_ref == f"life_source:{source.id}"
        assert voice_items[0].meta_data["ingestion_contract"] == {
            "version": 1,
            "source_type": "voice",
            "owner_scoped": True,
            "destination_required": False,
            "classification_can_execute_external_action": False,
            "model_output_has_write_authority": False,
        }
        assert db.query(InboxItem).filter_by(owner_id=bob.id).count() == 0

        synced = sync_offline_captures(
            db,
            account=alice,
            captures=[{
                "capability": "mobile_voice",
                "payload": {
                    "transcript": "Offline note from the same authenticated device.",
                    "language": "en",
                    "confidence": 100,
                    "device_ref": "phone-a",
                },
                "observed_at": datetime(2026, 7, 17, 9, tzinfo=timezone.utc),
                "idempotency_key": "voice-offline-1",
            }],
            trusted_device_session=True,
        )
        assert synced[0]["created"] is True
        assert db.query(InboxItem).filter_by(
            owner_id=alice.id, source_type="voice",
        ).count() == 2
        first_page = ambient_continuity(
            db, owner_id=alice.id, limit=1, trusted_device_session=True,
        )
        assert first_page["count"] == 1
        assert first_page["has_more"] is True
        second_page = ambient_continuity(
            db,
            owner_id=alice.id,
            cursor=first_page["next_cursor"],
            limit=10,
            trusted_device_session=True,
        )
        assert second_page["count"] == 1
        assert {item["id"] for item in first_page["items"] + second_page["items"]} == {
            source.id, synced[0]["source"]["id"],
        }
        assert ambient_continuity(
            db, owner_id=bob.id, trusted_device_session=True,
        )["items"] == []
        db.commit()

        raw = db.execute(text(
            "SELECT safe_excerpt, metadata FROM life_sources WHERE id = :source"
        ), {"source": source.id}).one()
        rendered = json.dumps([str(raw[0]), str(raw[1])])
        assert "vibration test" not in rendered
        assert "phone-a" not in rendered
    finally:
        db.rollback()
        db.close()


def test_transcription_consent_and_smart_home_actions_fail_closed(ambient_env):
    db = ambient_env.Session()
    try:
        alice = db.query(Account).filter_by(username="alice").one()
        _enable(db, alice, "transcription", ["capture", "read"])
        with pytest.raises(AmbientCapabilityDenied, match="consent"):
            capture_ambient_signal(
                db,
                account=alice,
                capability="transcription",
                payload={
                    "text": "Private call",
                    "meeting_title": "Call",
                    "participants_consented": False,
                },
                observed_at=None,
                idempotency_key="call-without-consent",
                trusted_device_session=True,
            )

        _enable(db, alice, "smart_home", ["capture", "read", "prepare_action"])
        created = prepare_smart_home_action(
            db,
            account=alice,
            operation="turn_on",
            integration_id="homeassistant",
            entity_id="light.desk",
            parameters={"brightness": 40},
            reason="Owner requested the desk light",
            sources={"interface": "mobile_voice"},
            idempotency_key="desk-light-on",
            trusted_device_session=True,
        )
        assert created.proposal.autonomy_level == 5
        assert created.proposal.external is True
        assert created.proposal.requires_confirmation is True
        assert created.proposal.state == "prepared"
        assert created.confirmation_token

        # Door unlocking is Level 6 and the default smart-home cap is Level 5.
        with pytest.raises(ActionPolicyDenied, match="domain cap"):
            prepare_smart_home_action(
                db,
                account=alice,
                operation="unlock",
                integration_id="homeassistant",
                entity_id="lock.front_door",
                parameters={},
                reason="Unlock the front door",
                sources={"interface": "mobile_voice"},
                idempotency_key="front-door-unlock",
                trusted_device_session=True,
            )
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize(
    ("capability", "payload", "source_type"),
    [
        (
            "location",
            {
                "latitude": 13.0108,
                "longitude": 74.7943,
                "accuracy_m": 12,
                "label": "NITK campus",
                "device_ref": "phone-a",
            },
            "location",
        ),
        (
            "wearable",
            {
                "metrics": [
                    {"name": "heart_rate", "value": 72, "unit": "bpm"},
                    {"name": "steps", "value": 4200, "unit": "count"},
                ],
                "activity": "walking",
                "device_ref": "watch-a",
            },
            "wearable",
        ),
        (
            "transcription",
            {
                "text": "The team agreed to run the vibration test Friday.",
                "meeting_title": "Controls review",
                "participants_consented": True,
                "device_ref": "laptop-a",
            },
            "transcription",
        ),
        (
            "camera",
            {
                "extracted_text": "Bearing specification 6204",
                "asset_id": "asset-private-1",
                "caption": "Lab whiteboard",
                "device_ref": "phone-a",
            },
            "camera",
        ),
        (
            "smart_home",
            {
                "entity_id": "sensor.lab_temperature",
                "state": "24.2 C",
                "attributes": {"unit": "C"},
                "integration_id": "homeassistant",
            },
            "smart_home",
        ),
    ],
)
def test_every_ambient_capture_capability_persists_private_evidence(
    ambient_env, capability, payload, source_type,
):
    db = ambient_env.Session()
    try:
        alice = db.query(Account).filter_by(username="alice").one()
        _enable(db, alice, capability, ["capture", "read"])
        source, created = capture_ambient_signal(
            db,
            account=alice,
            capability=capability,
            payload=payload,
            observed_at=datetime(2026, 7, 17, 10, tzinfo=timezone.utc),
            idempotency_key=f"capture-{capability}",
            trusted_device_session=True,
        )
        assert created is True
        assert source.source_type == source_type
        assert source.owner_id == alice.id
        assert source.sensitivity == "private"
        assert source.meta_data["no_training"] is True
        assert source.meta_data["interpreted_as_instruction"] is False
        assert db.query(ActionProposal).filter_by(owner_id=alice.id).count() == 0
        expected_inbox_type = {
            "transcription": "meeting_note",
            "camera": "image",
        }.get(capability)
        inbox_items = db.query(InboxItem).filter_by(owner_id=alice.id).all()
        if expected_inbox_type is None:
            assert inbox_items == []
        else:
            assert len(inbox_items) == 1
            assert inbox_items[0].source_type == expected_inbox_type
            assert inbox_items[0].source_ref == f"life_source:{source.id}"
            assert inbox_items[0].meta_data["ambient_capability"] == capability
    finally:
        db.rollback()
        db.close()


def test_device_unlock_requirement_rejects_non_device_credentials(ambient_env):
    db = ambient_env.Session()
    try:
        alice = db.query(Account).filter_by(username="alice").one()
        _enable(db, alice, "location", ["capture", "read"])
        with pytest.raises(AmbientCapabilityDenied, match="unlocked-device"):
            capture_ambient_signal(
                db,
                account=alice,
                capability="location",
                payload={"latitude": 13.0, "longitude": 74.8},
                observed_at=None,
                idempotency_key="location-from-bearer",
                trusted_device_session=False,
            )
    finally:
        db.rollback()
        db.close()


def test_ambient_inbox_failure_rolls_back_source(ambient_env, monkeypatch):
    import src.ambient_capabilities as ambient

    db = ambient_env.Session()
    try:
        alice = db.query(Account).filter_by(username="alice").one()
        _enable(db, alice, "mobile_voice", ["capture", "read"])

        def fail_inbox(*_args, **_kwargs):
            raise LifeCoreError("simulated Inbox failure")

        monkeypatch.setattr(ambient, "create_inbox_item", fail_inbox)
        with pytest.raises(
            ambient.AmbientCapabilityError,
            match="could not enter the Universal Inbox",
        ):
            capture_ambient_signal(
                db,
                account=alice,
                capability="mobile_voice",
                payload={"transcript": "Atomic capture"},
                observed_at=None,
                idempotency_key="atomic-failure",
                trusted_device_session=True,
            )
        db.rollback()
    finally:
        db.close()

    with ambient_env.Session() as verify:
        assert verify.query(LifeSource).count() == 0
        assert verify.query(InboxItem).count() == 0


@pytest.mark.asyncio
async def test_approved_smart_home_action_uses_granted_connector_executor(
    ambient_env, monkeypatch,
):
    db = ambient_env.Session()
    try:
        alice = db.query(Account).filter_by(username="alice").one()
        _enable(db, alice, "smart_home", ["capture", "read", "prepare_action"])
        created = prepare_smart_home_action(
            db,
            account=alice,
            operation="set_level",
            integration_id="homeassistant",
            entity_id="light.desk",
            parameters={"level": 42, "transition": 1},
            reason="Owner requested a lower desk-light level",
            sources={"interface": "mobile_voice"},
            idempotency_key="desk-level-42",
            trusted_device_session=True,
        )
        request = Request({
            "type": "http",
            "method": "POST",
            "path": "/api/life/actions/approve",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("127.0.0.1", 1234),
        })
        request.state.api_token = False
        request.state.current_user = "alice"
        bind_request_audit_context(db, request, alice)
        approved = approve_action(
            db,
            owner_id=alice.id,
            proposal_id=created.proposal.id,
            expected_version=created.proposal.version,
            confirmation_token=created.confirmation_token,
        )
        execution = start_smart_home_execution(
            db,
            owner_id=alice.id,
            proposal_id=approved.id,
            expected_version=approved.version,
        )
        assert execution.method == "POST"
        assert execution.path == "/api/services/light/turn_on"
        assert execution.body == {
            "brightness_pct": 42.0,
            "transition": 1,
            "entity_id": "light.desk",
        }

        calls = []

        async def fake_execute(integration_id, method, path, **kwargs):
            calls.append((integration_id, method, path, kwargs))
            return {"exit_code": 0, "response": "service called"}

        import src.integrations as integrations

        monkeypatch.setattr(integrations, "execute_api_call", fake_execute)
        result = await call_smart_home_connector(
            execution, owner_username="alice",
        )
        finished = finish_smart_home_execution(
            db,
            owner_id=alice.id,
            execution=execution,
            connector_result=result,
        )
        assert finished.state == "completed"
        assert finished.result["ok"] is True
        assert calls == [(
            "homeassistant",
            "POST",
            "/api/services/light/turn_on",
            {
                "body": {
                    "brightness_pct": 42.0,
                    "transition": 1,
                    "entity_id": "light.desk",
                },
                "owner": "alice",
                "approved_external_action": True,
            },
        )]
    finally:
        db.rollback()
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("connector_result", "expected_status", "expected_state"),
    [
        ({"exit_code": 0, "response": "ok"}, 200, "completed"),
        ({"exit_code": 1, "error": "connector grant denied"}, 502, "failed"),
    ],
)
async def test_generic_action_route_executes_reviewed_smart_home_connector(
    ambient_env, monkeypatch, connector_result, expected_status, expected_state,
):
    db = ambient_env.Session()
    try:
        alice = db.query(Account).filter_by(username="alice").one()
        _enable(db, alice, "smart_home", ["capture", "read", "prepare_action"])
        created = prepare_smart_home_action(
            db,
            account=alice,
            operation="turn_on",
            integration_id="homeassistant",
            entity_id="light.desk",
            parameters={},
            reason="Owner requested the desk light",
            sources={"interface": "browser"},
            idempotency_key="route-desk-light-on",
            trusted_device_session=True,
        )
        request = Request({
            "type": "http", "method": "POST", "path": "/approve",
            "headers": [], "query_string": b"", "scheme": "http",
            "server": ("test", 80), "client": ("127.0.0.1", 1234),
        })
        request.state.api_token = False
        request.state.current_user = "alice"
        bind_request_audit_context(db, request, alice)
        approved = approve_action(
            db,
            owner_id=alice.id,
            proposal_id=created.proposal.id,
            expected_version=created.proposal.version,
            confirmation_token=created.confirmation_token,
        )
        proposal_id = approved.id
        approved_version = int(approved.version)
        db.commit()
    finally:
        db.close()

    observed_states = []

    async def fake_connector(execution, *, owner_username):
        check = ambient_env.Session()
        try:
            observed_states.append(
                check.query(ActionProposal).filter_by(id=execution.proposal_id).one().state
            )
        finally:
            check.close()
        return connector_result

    import routes.action_policy_routes as action_policy_routes

    monkeypatch.setattr(action_policy_routes, "call_smart_home_connector", fake_connector)
    transport = httpx.ASGITransport(app=ambient_env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/life/actions/{proposal_id}/execute",
            headers={"x-user": "alice"},
            json={"version": approved_version},
        )
    assert response.status_code == expected_status, response.text
    payload = response.json() if expected_status == 200 else response.json()["detail"]
    if expected_status == 200:
        assert payload["delivery"]["network_performed"] is True
    assert payload["action"]["state"] == expected_state
    assert observed_states == ["executing"]
    verify = ambient_env.Session()
    try:
        assert verify.query(ActionProposal).filter_by(id=proposal_id).one().state == expected_state
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_ambient_routes_cover_capture_offline_sync_continuity_and_policy(ambient_env):
    transport = httpx.ASGITransport(app=ambient_env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"x-user": "alice"}
        listing = await client.get("/api/life/ambient/capabilities", headers=headers)
        assert listing.status_code == 200, listing.text
        assert listing.json()["count"] == 8
        assert listing.json()["privacy"]["no_training"] is True

        enabled = await client.put(
            "/api/life/ambient/capabilities/mobile_voice",
            headers=headers,
            json={
                "enabled": True,
                "operations": ["capture", "read"],
                "local_only": True,
                "retention_days": 14,
                "require_device_unlock": True,
                "no_training": True,
                "idempotency_key": "route-enable-voice",
            },
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["capability"]["version"] == 1

        rejected_training = await client.put(
            "/api/life/ambient/capabilities/location",
            headers=headers,
            json={
                "enabled": True,
                "operations": ["capture"],
                "no_training": False,
            },
        )
        assert rejected_training.status_code == 403

        captured = await client.post(
            "/api/life/ambient/captures/mobile_voice",
            headers=headers,
            json={
                "payload": {"transcript": "Route voice evidence", "confidence": 90},
                "observed_at": "2026-07-17T08:00:00Z",
                "idempotency_key": "route-voice-1",
            },
        )
        assert captured.status_code == 201, captured.text
        assert captured.json()["created"] is True
        assert captured.json()["inbox_item_id"]
        assert captured.json()["interpreted_as_instruction"] is False

        synced = await client.post(
            "/api/life/ambient/offline/sync",
            headers=headers,
            json={"captures": [{
                "capability": "mobile_voice",
                "payload": {"transcript": "Queued while offline", "confidence": 100},
                "observed_at": "2026-07-17T09:00:00Z",
                "idempotency_key": "route-offline-1",
            }]},
        )
        assert synced.status_code == 200, synced.text
        assert synced.json()["replay_safe"] is True
        assert synced.json()["items"][0]["inbox_item_id"]

        continuity = await client.get(
            "/api/life/ambient/continuity", headers=headers,
        )
        assert continuity.status_code == 200, continuity.text
        assert continuity.json()["count"] == 2
        assert all(item["capability"] == "mobile_voice" for item in continuity.json()["items"])

        bob = await client.get(
            "/api/life/ambient/continuity", headers={"x-user": "bob"},
        )
        assert bob.status_code == 200
        assert bob.json()["items"] == []

        token_read = await client.get(
            "/api/life/ambient/continuity",
            headers={"x-api-owner": "alice", "x-api-scopes": "life:read"},
        )
        assert token_read.status_code == 403
        token_write = await client.post(
            "/api/life/ambient/captures/mobile_voice",
            headers={"x-api-owner": "alice", "x-api-scopes": "life:read"},
            json={
                "payload": {"transcript": "Denied token write"},
                "idempotency_key": "denied-token-write",
            },
        )
        assert token_write.status_code == 403

        unlocked_scope_still_not_device_proof = await client.post(
            "/api/life/ambient/captures/mobile_voice",
            headers={"x-api-owner": "alice", "x-api-scopes": "life:write"},
            json={
                "payload": {"transcript": "A bearer token is not device unlock proof"},
                "idempotency_key": "api-token-not-device-proof",
            },
        )
        assert unlocked_scope_still_not_device_proof.status_code == 403


def test_application_registers_ambient_router_once():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert source.count("from routes.ambient_routes import setup_ambient_routes") == 1
    assert source.count("app.include_router(setup_ambient_routes())") == 1
