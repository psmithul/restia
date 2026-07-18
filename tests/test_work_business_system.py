"""Focused contracts for V3 Work and Business workspaces."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import ActionAudit, Base, LifeEntity, LifeEntityVersion
from routes.life_routes import setup_life_routes
from routes.work_business_routes import setup_work_business_routes
from src.identity import ensure_account
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    create_life_source,
)
from src.work_business_service import (
    RECORD_ENTITY_TYPES,
    WORK_BUSINESS_RECORD_KINDS,
    create_cross_workspace_relation,
    create_work_business_record,
    create_work_business_workspace,
    delete_cross_workspace_relation,
    delete_work_business_record,
    delete_work_business_workspace,
    get_work_business_record,
    list_cross_workspace_relations,
    list_work_business_records,
    record_history,
    search_work_business_records,
    update_work_business_record,
    update_work_business_workspace,
    workspace_history,
    workspace_summary,
)


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
def workspace_env(tmp_path):
    db_path = tmp_path / "work-business.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.state.auth_manager = _IdentityAuthority("alice", "bob")

    @app.middleware("http")
    async def inject_identity(request, call_next):
        request.state.api_token = False
        request.state.current_user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(setup_life_routes(session_factory=factory))
    app.include_router(setup_work_business_routes(session_factory=factory))
    yield SimpleNamespace(
        app=app, Session=factory, engine=engine, db_path=db_path
    )
    engine.dispose()


def _account(db, username: str):
    account = ensure_account(db, username)
    db.flush()
    return account


def _source(db, account, title="Workspace source"):
    source, _ = create_life_source(
        db, account=account, source_type="manual", title=title
    )
    return source


def _source_links(source):
    return [{
        "source_id": source.id,
        "relation": "supports",
        "label": "Explicit owner-scoped source",
    }]


def _workspace(db, account, source, kind="work", title=None):
    workspace, _ = create_work_business_workspace(
        db,
        account=account,
        workspace_kind=kind,
        title=title or f"{kind.title()} workspace",
        purpose=f"Private {kind} context",
        details={"operating_note": f"Keep {kind} records isolated"},
        source_links=_source_links(source),
        provenance={"capture": "manual"},
    )
    return workspace


def _record(db, account, workspace, source, kind="project", **overrides):
    payload = {
        "record_kind": kind,
        "title": f"{kind.replace('_', ' ').title()} record",
        "summary": "Private workspace record",
        "details": {"label": f"bounded-{kind}"},
        "source_links": _source_links(source),
        "provenance": {"capture": "manual"},
    }
    payload.update(overrides)
    record, _ = create_work_business_record(
        db, account=account, workspace_id=workspace.id, **payload
    )
    return record


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.request(method, path, headers=headers, **kwargs)


def test_real_app_registers_work_business_router_once():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert source.count(
        "from routes.work_business_routes import setup_work_business_routes"
    ) == 1
    assert source.count("app.include_router(setup_work_business_routes())") == 1


@pytest.mark.parametrize("workspace_kind", ["work", "business"])
@pytest.mark.parametrize("record_kind", sorted(WORK_BUSINESS_RECORD_KINDS))
def test_every_required_kind_is_encrypted_account_owned_and_workspace_isolated(
    workspace_env, workspace_kind, record_kind
):
    db = workspace_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        workspace = _workspace(db, alice, source, kind=workspace_kind)
        record = _record(db, alice, workspace, source, kind=record_kind)
        db.commit()

        assert workspace.owner_id == alice.id
        assert workspace.entity_type == "workspace"
        assert record.owner_id == alice.id
        assert record.entity_type == RECORD_ENTITY_TYPES[record_kind]
        assert record.properties["workspace_id"] == workspace.id
        assert record.properties["workspace_kind"] == workspace_kind
        assert record.properties["record_kind"] == record_kind
        assert record.provenance["source_ids"] == [source.id]
        assert record.provenance["workspace_id"] == workspace.id
        read = get_work_business_record(
            db,
            owner_id=alice.id,
            workspace_id=workspace.id,
            record_id=record.id,
        )
        assert read["execution_policy"] == {
            "record_only": True,
            "can_send_outreach": False,
            "can_send_messages": False,
            "can_submit_proposals": False,
            "can_make_payments": False,
        }
        assert type(LifeEntity.__table__.c.properties.type).__name__ == "EncryptedJSON"
    finally:
        db.close()


def test_private_workspace_content_is_not_plaintext_on_disk(workspace_env):
    private_phrase = "v3-workspace-private-phrase-91c4"
    db = workspace_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        workspace = _workspace(db, alice, source)
        _record(
            db,
            alice,
            workspace,
            source,
            summary=private_phrase,
            details={"private_context": private_phrase},
        )
        db.commit()
    finally:
        db.close()
    assert private_phrase.encode() not in workspace_env.db_path.read_bytes()


def test_workspace_and_account_isolation_summary_and_search(workspace_env):
    db = workspace_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        alice_source = _source(db, alice, "Alice source")
        bob_source = _source(db, bob, "Bob source")
        work = _workspace(db, alice, alice_source, "work", "Research work")
        business = _workspace(db, alice, alice_source, "business", "Venture")
        bob_work = _workspace(db, bob, bob_source, "work", "Bob private")
        work_record = _record(
            db, alice, work, alice_source, "objective", title="Controls objective"
        )
        _record(db, alice, business, alice_source, "revenue", title="July revenue")
        _record(db, bob, bob_work, bob_source, "project", title="Bob secret project")
        db.commit()

        with pytest.raises(LifeGraphNotFound):
            get_work_business_record(
                db,
                owner_id=alice.id,
                workspace_id=business.id,
                record_id=work_record.id,
            )
        with pytest.raises(LifeGraphNotFound):
            get_work_business_record(
                db,
                owner_id=bob.id,
                workspace_id=work.id,
                record_id=work_record.id,
            )
        work_rows, _ = list_work_business_records(
            db, owner_id=alice.id, workspace_id=work.id
        )
        business_rows, _ = list_work_business_records(
            db, owner_id=alice.id, workspace_id=business.id
        )
        assert [row["id"] for row in work_rows] == [work_record.id]
        assert [row["record_kind"] for row in business_rows] == ["revenue"]
        assert search_work_business_records(
            db,
            owner_id=alice.id,
            workspace_id=work.id,
            query_text="Bob secret",
        )["items"] == []

        summary = workspace_summary(db, owner_id=alice.id, workspace_id=work.id)
        assert summary["method"] == "deterministic_owner_scoped_workspace_summary_v1"
        assert summary["uses_model_inference"] is False
        assert summary["totals"]["records"] == 1
        assert summary["by_kind"]["objective"] == 1
        assert summary["by_kind"]["revenue"] == 0
    finally:
        db.close()


def test_explicit_cross_workspace_links_never_broaden_access_and_are_cas_deleted(
    workspace_env,
):
    db = workspace_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        source = _source(db, alice)
        bob_source = _source(db, bob)
        work = _workspace(db, alice, source, "work")
        business = _workspace(db, alice, source, "business")
        bob_business = _workspace(db, bob, bob_source, "business")
        objective = _record(db, alice, work, source, "objective")
        proposal = _record(db, alice, business, source, "proposal")
        bob_proposal = _record(db, bob, bob_business, bob_source, "proposal")

        link, created = create_cross_workspace_relation(
            db,
            account=alice,
            source_workspace_id=work.id,
            source_record_id=objective.id,
            relation="supports",
            target_workspace_id=business.id,
            target_record_id=proposal.id,
            context={"reason": "Explicit handoff"},
            source_links=_source_links(source),
            provenance={"capture": "manual"},
        )
        assert created is True
        with pytest.raises(LifeGraphError, match="different workspaces"):
            create_cross_workspace_relation(
                db,
                account=alice,
                source_workspace_id=work.id,
                source_record_id=objective.id,
                relation="supports",
                target_workspace_id=work.id,
                target_record_id=objective.id,
                context={},
                source_links=_source_links(source),
            )
        with pytest.raises(LifeGraphNotFound):
            create_cross_workspace_relation(
                db,
                account=alice,
                source_workspace_id=work.id,
                source_record_id=objective.id,
                relation="supports",
                target_workspace_id=bob_business.id,
                target_record_id=bob_proposal.id,
                context={},
                source_links=_source_links(source),
            )
        db.commit()

        outgoing, _ = list_cross_workspace_relations(
            db, owner_id=alice.id, workspace_id=work.id, direction="outgoing"
        )
        incoming, _ = list_cross_workspace_relations(
            db, owner_id=alice.id, workspace_id=business.id, direction="incoming"
        )
        assert [row["id"] for row in outgoing] == [link.id]
        assert [row["id"] for row in incoming] == [link.id]
        with pytest.raises(LifeGraphNotFound):
            list_cross_workspace_relations(
                db, owner_id=bob.id, workspace_id=work.id
            )
        with pytest.raises(LifeGraphConflict, match="relations"):
            delete_work_business_record(
                db,
                owner_id=alice.id,
                workspace_id=work.id,
                record_id=objective.id,
                expected_version=1,
            )
        with pytest.raises(LifeGraphConflict, match="current version 1"):
            delete_cross_workspace_relation(
                db,
                owner_id=alice.id,
                relation_id=link.id,
                expected_version=2,
            )
        deleted_link = delete_cross_workspace_relation(
            db,
            owner_id=alice.id,
            relation_id=link.id,
            expected_version=1,
        )
        assert deleted_link.version == 2
        deleted_record = delete_work_business_record(
            db,
            owner_id=alice.id,
            workspace_id=work.id,
            record_id=objective.id,
            expected_version=1,
        )
        assert deleted_record.deleted_at is not None
    finally:
        db.close()


def test_workspace_and_record_cas_history_audit_and_delete(workspace_env):
    db = workspace_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        workspace = _workspace(db, alice, source)
        record = _record(db, alice, workspace, source, "project")
        updated = update_work_business_record(
            db,
            account=alice,
            workspace_id=workspace.id,
            record_id=record.id,
            expected_version=1,
            changes={"summary": "Updated source-backed project"},
        )
        assert updated.version == 2
        with pytest.raises(LifeGraphConflict, match="current version 2"):
            update_work_business_record(
                db,
                account=alice,
                workspace_id=workspace.id,
                record_id=record.id,
                expected_version=1,
                changes={"summary": "stale"},
            )
        with pytest.raises(LifeGraphConflict, match="workspace records"):
            delete_work_business_workspace(
                db,
                owner_id=alice.id,
                workspace_id=workspace.id,
                expected_version=1,
            )
        deleted = delete_work_business_record(
            db,
            owner_id=alice.id,
            workspace_id=workspace.id,
            record_id=record.id,
            expected_version=2,
        )
        updated_workspace = update_work_business_workspace(
            db,
            account=alice,
            workspace_id=workspace.id,
            expected_version=1,
            changes={"purpose": "Updated purpose"},
        )
        assert updated_workspace.version == 2
        deleted_workspace = delete_work_business_workspace(
            db,
            owner_id=alice.id,
            workspace_id=workspace.id,
            expected_version=2,
        )
        db.commit()

        assert deleted.version == 3
        assert deleted_workspace.version == 3
        record_versions, _ = record_history(
            db,
            owner_id=alice.id,
            workspace_id=workspace.id,
            record_id=record.id,
        )
        workspace_versions, _ = workspace_history(
            db, owner_id=alice.id, workspace_id=workspace.id
        )
        assert [row["version"] for row in record_versions] == [3, 2, 1]
        assert [row["version"] for row in workspace_versions] == [3, 2, 1]
        assert db.query(LifeEntityVersion).filter_by(entity_id=record.id).count() == 3
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id, entity_id=record.id
        ).count() == 3
    finally:
        db.close()


@pytest.mark.parametrize(
    "unsafe",
    [
        {"password": "never"},
        {"apiKey": "never"},
        {"toolCall": {"name": "send_email"}},
        {"sendEmail": {"to": "prospect@example.test"}},
        {"submitProposal": {"id": "proposal-1"}},
        {"paymentExecutor": {"amount": "100"}},
        {"outreach_executor": "browser"},
        {"webhook": "https://example.test/send"},
        {"payload": {"name": "send_email", "arguments": {}}},
        {"embedded": "https://user:password@example.test/private"},
        {"embedded": "See https://user:password@example.test/private"},
    ],
)
def test_credentials_and_executor_payloads_are_rejected(workspace_env, unsafe):
    db = workspace_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        workspace = _workspace(db, alice, source)
        with pytest.raises(LifeGraphError, match="credentials|external action|outreach"):
            _record(
                db, alice, workspace, source, "proposal", details=unsafe
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_scoped_routes_and_generic_typed_bypass_guard(workspace_env):
    db = workspace_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        db.commit()
    finally:
        db.close()

    workspace_response = await _call(
        workspace_env,
        "POST",
        "/api/life/work-business/workspaces",
        json={
            "workspace_kind": "work",
            "title": "Typed work",
            "purpose": "Owner-scoped work",
            "source_links": _source_links(source),
        },
    )
    assert workspace_response.status_code == 201, workspace_response.text
    workspace = workspace_response.json()["workspace"]
    record_response = await _call(
        workspace_env,
        "POST",
        f"/api/life/work-business/workspaces/{workspace['id']}/records",
        json={
            "record_kind": "task",
            "title": "Typed task",
            "source_links": _source_links(source),
        },
    )
    assert record_response.status_code == 201, record_response.text
    record = record_response.json()["record"]

    hidden = await _call(
        workspace_env,
        "GET",
        f"/api/life/work-business/workspaces/{workspace['id']}/records/{record['id']}",
        user="bob",
    )
    assert hidden.status_code == 404

    bypass = await _call(
        workspace_env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "task",
            "title": "Bypass",
            "properties": {
                "work_business_record_schema_version": 1,
                "workspace_id": workspace["id"],
                "workspace_kind": "work",
                "record_kind": "task",
            },
        },
    )
    assert bypass.status_code == 400
    assert "work-business" in bypass.json()["detail"].lower()

    update_bypass = await _call(
        workspace_env,
        "PATCH",
        f"/api/life/entities/{record['id']}",
        json={"version": record["version"], "title": "Bypass update"},
    )
    assert update_bypass.status_code == 400
    delete_bypass = await _call(
        workspace_env,
        "DELETE",
        f"/api/life/entities/{record['id']}",
        json={"version": record["version"]},
    )
    assert delete_bypass.status_code == 400

    updated = await _call(
        workspace_env,
        "PATCH",
        f"/api/life/work-business/workspaces/{workspace['id']}/records/{record['id']}",
        json={"version": 1, "summary": "Updated through typed authority"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["record"]["version"] == 2

    summary = await _call(
        workspace_env,
        "GET",
        f"/api/life/work-business/workspaces/{workspace['id']}/summary",
    )
    assert summary.status_code == 200, summary.text
    assert summary.json()["by_kind"]["task"] == 1

    business_response = await _call(
        workspace_env,
        "POST",
        "/api/life/work-business/workspaces",
        json={
            "workspace_kind": "business",
            "title": "Typed business",
            "source_links": _source_links(source),
        },
    )
    business = business_response.json()["workspace"]
    proposal_response = await _call(
        workspace_env,
        "POST",
        f"/api/life/work-business/workspaces/{business['id']}/records",
        json={
            "record_kind": "proposal",
            "title": "Typed proposal",
            "source_links": _source_links(source),
        },
    )
    proposal = proposal_response.json()["record"]
    link_bypass = await _call(
        workspace_env,
        "POST",
        "/api/life/links",
        json={
            "source_id": record["id"],
            "relation": "supports",
            "target_id": proposal["id"],
        },
    )
    assert link_bypass.status_code == 400
    assert "work-business/relations" in link_bypass.json()["detail"]
