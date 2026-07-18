"""Focused contracts for the typed V3 Decisions system."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    ActionAudit,
    Base,
    EntityLink,
    LifeEntity,
    LifeEntityVersion,
)
from routes.life_routes import setup_life_routes
from src.decision_service import (
    create_decision,
    decision_history,
    get_decision,
    list_due_decision_reviews,
    review_decision,
    search_decisions,
    update_decision,
)
from src.identity import ensure_account
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    create_life_entity,
    create_life_source,
)


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
def decision_env(tmp_path):
    db_path = tmp_path / "decisions.db"
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
    yield SimpleNamespace(app=app, Session=factory, engine=engine)
    engine.dispose()


def _account(db, username: str) -> Account:
    account = ensure_account(db, username)
    db.flush()
    return account


def _decision_payload(**overrides):
    payload = {
        "title": "Choose launch architecture",
        "decision_date": datetime(2026, 7, 1, 9),
        "context": "We need one authority before the launch window.",
        "options": [
            {"id": "single", "label": "Single authority", "details": "Keep one graph."},
            {"id": "split", "label": "Split stores", "details": "Duplicate the state."},
        ],
        "chosen_option": "single",
        "reasons": ["Avoid drift", "Preserve owner-scoped recall"],
        "risks": ["Migration takes time"],
        "assumptions": [{
            "id": "usage_growth",
            "text": "Usage will grow after launch",
            "status": "unverified",
            "review_at": datetime(2026, 7, 10, 9),
        }],
        "people": ["Mits", "Alex"],
        "evidence": [{"label": "Architecture memo"}],
        "review_at": datetime(2026, 7, 15, 9),
        "provenance": {"capture": "manual"},
    }
    payload.update(overrides)
    return payload


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.request(method, path, headers=headers, **kwargs)


def test_decision_uses_life_authority_owner_scope_provenance_and_graph_links(
    decision_env,
):
    db = decision_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        source, _ = create_life_source(
            db, account=alice, source_type="document", title="Architecture memo"
        )
        linked = {}
        for entity_type in ("person", "project", "file", "goal"):
            linked[entity_type], _ = create_life_entity(
                db,
                account=alice,
                entity_type=entity_type,
                title=f"Linked {entity_type}",
            )
        payload = _decision_payload(
            evidence=[{
                "label": "Architecture memo",
                "source_id": source.id,
                "entity_id": linked["file"].id,
            }],
            linked_entity_ids=[
                linked["person"].id,
                linked["project"].id,
                linked["goal"].id,
            ],
            provenance={"source_id": source.id, "capture": "manual"},
        )
        decision, created = create_decision(db, account=alice, **payload)
        db.commit()

        assert created is True
        assert decision.entity_type == "decision"
        assert db.query(LifeEntity).filter_by(id=decision.id).one().id == decision.id
        assert decision.properties["decision_schema_version"] == 1
        assert decision.provenance["source_id"] == source.id
        assert {row.relation for row in db.query(EntityLink).filter_by(
            owner_id=alice.id, source_id=decision.id
        )} == {"involves", "relates_to", "supported_by", "supports"}
        record = get_decision(db, owner_id=alice.id, entity_id=decision.id)
        assert {row["target"]["entity_type"] for row in record["links"]} == {
            "person", "project", "file", "goal"
        }
        with pytest.raises(LifeGraphNotFound, match="not found"):
            get_decision(db, owner_id=bob.id, entity_id=decision.id)
        assert search_decisions(
            db, owner_id=bob.id, query_text="architecture memo"
        )["items"] == []
    finally:
        db.close()


def test_decision_validation_rejects_invalid_and_cross_owner_content(decision_env):
    db = decision_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        bob_source, _ = create_life_source(
            db, account=bob, source_type="email", title="Bob private evidence"
        )
        task, _ = create_life_entity(
            db, account=alice, entity_type="task", title="Not an allowed context link"
        )
        with pytest.raises(LifeGraphError, match="between 2 and 20"):
            create_decision(
                db,
                account=alice,
                **_decision_payload(
                    options=[{"id": "only", "label": "Only option"}],
                    chosen_option="only",
                ),
            )
        with pytest.raises(LifeGraphError, match="chosen_option"):
            create_decision(
                db, account=alice, **_decision_payload(chosen_option="missing")
            )
        with pytest.raises(LifeGraphNotFound, match="Evidence source"):
            create_decision(
                db,
                account=alice,
                **_decision_payload(evidence=[{
                    "label": "Must stay private", "source_id": bob_source.id,
                }]),
            )
        with pytest.raises(LifeGraphError, match="people, projects, files, and goals"):
            create_decision(
                db,
                account=alice,
                **_decision_payload(linked_entity_ids=[task.id]),
            )
        with pytest.raises(LifeGraphError, match="context must not exceed"):
            create_decision(
                db, account=alice, **_decision_payload(context="x" * 10_001)
            )
    finally:
        db.close()


def test_decision_optimistic_version_audit_and_review_history(decision_env):
    db = decision_env.Session()
    try:
        alice = _account(db, "alice")
        decision, _ = create_decision(db, account=alice, **_decision_payload())
        assert decision.version == 1
        updated = update_decision(
            db,
            account=alice,
            entity_id=decision.id,
            expected_version=1,
            changes={
                "context": "New evidence changed the operating context.",
                "reasons": ["Avoid drift", "The source was verified"],
            },
        )
        assert updated.version == 2
        with pytest.raises(LifeGraphConflict, match="another client"):
            update_decision(
                db,
                account=alice,
                entity_id=decision.id,
                expected_version=1,
                changes={"context": "Stale overwrite"},
            )
        with pytest.raises(LifeGraphError, match="review operation"):
            update_decision(
                db,
                account=alice,
                entity_id=decision.id,
                expected_version=2,
                changes={"assumptions": [{
                    "id": "usage_growth",
                    "text": "Usage will grow after launch",
                    "status": "valid",
                }]},
            )
        reviewed = review_decision(
            db,
            account=alice,
            entity_id=decision.id,
            expected_version=2,
            summary="The assumption held and the single authority reduced drift.",
            reviewed_at=datetime(2026, 7, 17, 12),
            assumption_updates=[{
                "id": "usage_growth", "status": "valid", "note": "Observed in usage data",
                "review_at": datetime(2026, 8, 17, 12),
            }],
            outcome={"status": "successful", "summary": "No cross-store drift."},
            next_review_at=datetime(2026, 8, 17, 12),
        )
        db.commit()

        assert reviewed.version == 3
        assert reviewed.properties["outcome"]["status"] == "successful"
        assert reviewed.properties["assumptions"][0]["status"] == "valid"
        history, truncated = decision_history(
            db, owner_id=alice.id, entity_id=decision.id
        )
        assert truncated is False
        assert [row["version"] for row in history] == [3, 2, 1]
        assert set(history[0]["kinds"]) == {
            "review_recorded", "outcome_changed", "assumptions_changed"
        }
        assert {"last_review", "outcome", "assumptions"}.issubset(
            history[0]["changed_fields"]
        )
        assert db.query(LifeEntityVersion).filter_by(
            owner_id=alice.id, entity_id=decision.id
        ).count() == 3
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id, entity_id=decision.id
        ).count() == 3
    finally:
        db.close()


def test_due_review_includes_due_and_stale_assumptions_but_not_other_owners(
    decision_env,
):
    db = decision_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        cutoff = datetime(2026, 7, 17, 12)
        due, _ = create_decision(
            db,
            account=alice,
            **_decision_payload(
                title="Explicitly due",
                review_at=cutoff - timedelta(days=1),
                assumptions=[{
                    "id": "due_assumption", "text": "A due assumption",
                    "review_at": cutoff - timedelta(hours=1),
                }],
            ),
        )
        stale, _ = create_decision(
            db,
            account=alice,
            **_decision_payload(
                title="Stale assumption",
                decision_date=cutoff - timedelta(days=90),
                review_at=cutoff + timedelta(days=20),
                assumptions=[{"id": "old_assumption", "text": "Never rechecked"}],
            ),
        )
        create_decision(
            db,
            account=alice,
            **_decision_payload(
                title="Fresh decision",
                decision_date=cutoff,
                review_at=cutoff + timedelta(days=20),
                assumptions=[{"id": "fresh", "text": "Recently recorded"}],
            ),
        )
        create_decision(
            db,
            account=bob,
            **_decision_payload(title="Bob due decision", review_at=cutoff),
        )
        db.commit()

        result = list_due_decision_reviews(
            db,
            owner_id=alice.id,
            due_before=cutoff,
            stale_after_days=30,
        )
        by_id = {row["id"]: row for row in result["items"]}
        assert set(by_id) == {due.id, stale.id}
        assert by_id[due.id]["due_reasons"] == [
            "decision_review_due", "assumption_review_due"
        ]
        assert by_id[due.id]["due_assumption_ids"] == ["due_assumption"]
        assert by_id[stale.id]["due_reasons"] == ["assumption_stale"]
        assert by_id[stale.id]["stale_assumption_ids"] == ["old_assumption"]
    finally:
        db.close()


def test_decision_recall_searches_reasons_risks_assumptions_people_and_evidence(
    decision_env,
):
    db = decision_env.Session()
    try:
        alice = _account(db, "alice")
        decision, _ = create_decision(
            db,
            account=alice,
            **_decision_payload(
                reasons=["Telemetry marker 493 proved the constraint"],
                evidence=[{"label": "Control-system notebook"}],
            ),
        )
        db.commit()
        reason = search_decisions(
            db, owner_id=alice.id, query_text="marker 493"
        )
        evidence = search_decisions(
            db, owner_id=alice.id, query_text="control-system notebook"
        )
        assert [row["decision"]["id"] for row in reason["items"]] == [decision.id]
        assert [row["decision"]["id"] for row in evidence["items"]] == [decision.id]
        assert reason["items"][0]["match"] == "properties"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_decision_routes_enforce_owner_validation_and_version_conflicts(
    decision_env,
):
    db = decision_env.Session()
    try:
        _account(db, "alice")
        _account(db, "bob")
        db.commit()
    finally:
        db.close()

    payload = _decision_payload()
    payload["decision_date"] = payload["decision_date"].isoformat() + "Z"
    payload["review_at"] = payload["review_at"].isoformat() + "Z"
    payload["assumptions"][0]["review_at"] = (
        payload["assumptions"][0]["review_at"].isoformat() + "Z"
    )
    created = await _call(
        decision_env, "POST", "/api/life/decisions", json=payload
    )
    assert created.status_code == 201, created.text
    decision = created.json()["decision"]
    assert decision["version"] == 1

    hidden = await _call(
        decision_env, "GET", f"/api/life/decisions/{decision['id']}", user="bob"
    )
    assert hidden.status_code == 404
    invalid = await _call(
        decision_env,
        "POST",
        "/api/life/decisions",
        json={**payload, "chosen_option": "not_an_option"},
    )
    assert invalid.status_code == 400
    generic_typed_create = await _call(
        decision_env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "decision",
            "title": "Bypass",
            "properties": decision["properties"],
        },
    )
    assert generic_typed_create.status_code == 400
    assert "/api/life/decisions" in generic_typed_create.json()["detail"]
    generic_typed_update = await _call(
        decision_env,
        "PATCH",
        f"/api/life/entities/{decision['id']}",
        json={"version": 1, "summary": "Bypass the typed service"},
    )
    assert generic_typed_update.status_code == 400
    assert "/api/life/decisions/" in generic_typed_update.json()["detail"]
    changed = await _call(
        decision_env,
        "PATCH",
        f"/api/life/decisions/{decision['id']}",
        json={"version": 1, "context": "Updated through the typed API."},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["decision"]["version"] == 2
    stale = await _call(
        decision_env,
        "PATCH",
        f"/api/life/decisions/{decision['id']}",
        json={"version": 1, "context": "A stale client overwrite."},
    )
    assert stale.status_code == 409
    history = await _call(
        decision_env,
        "GET",
        f"/api/life/decisions/{decision['id']}/history",
    )
    assert history.status_code == 200, history.text
    assert [row["version"] for row in history.json()["items"]] == [2, 1]
    due = await _call(
        decision_env,
        "GET",
        "/api/life/decisions/due",
        params={"due_before": "2026-07-17T12:00:00Z"},
    )
    assert due.status_code == 200, due.text
    assert [row["id"] for row in due.json()["items"]] == [decision["id"]]
