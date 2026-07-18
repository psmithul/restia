"""Focused contracts for V3 typed Learning & Career records."""

from __future__ import annotations

import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import ActionAudit, Base, LifeEntityVersion
from routes.learning_career_routes import setup_learning_career_routes
from routes.life_routes import setup_life_routes
from src.identity import ensure_account
from src.learning_career_service import (
    CAREER_RECORD_KINDS,
    LEARNING_RECORD_KINDS,
    career_learning_plan,
    create_learning_career_record,
    delete_learning_career_record,
    get_learning_career_record,
    learning_career_history,
    list_learning_career_records,
    search_learning_career_records,
    update_learning_career_record,
)
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    create_life_source,
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
def learning_env(tmp_path):
    db_path = tmp_path / "learning-career.db"
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
    app.include_router(setup_learning_career_routes(session_factory=factory))
    yield SimpleNamespace(
        app=app, Session=factory, engine=engine, db_path=db_path
    )
    engine.dispose()


def _account(db, username: str):
    account = ensure_account(db, username)
    db.flush()
    return account


def _source(db, account, title="Source"):
    source, _ = create_life_source(
        db, account=account, source_type="manual", title=title
    )
    return source


def _payload(source, domain="learning", record_kind="skill", **overrides):
    payload = {
        "domain": domain,
        "record_kind": record_kind,
        "title": f"{record_kind.replace('_', ' ').title()} record",
        "summary": "Private source-backed record",
        "details": {},
        "source_links": [{
            "source_id": source.id,
            "relation": "supports",
            "label": "Explicit source",
        }],
        "entity_links": [],
        "provenance": {"capture": "manual"},
    }
    payload.update(overrides)
    return payload


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, **kwargs)


def test_real_app_registers_learning_career_router_once():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert source.count(
        "from routes.learning_career_routes import setup_learning_career_routes"
    ) == 1
    assert source.count("app.include_router(setup_learning_career_routes())") == 1


@pytest.mark.parametrize(
    ("domain", "record_kind"),
    [
        *(("learning", kind) for kind in sorted(LEARNING_RECORD_KINDS)),
        *(("career", kind) for kind in sorted(CAREER_RECORD_KINDS)),
    ],
)
def test_every_required_record_kind_uses_account_owned_encrypted_life_entity(
    learning_env, domain, record_kind
):
    db = learning_env.Session()
    private_phrase = f"private-{domain}-{record_kind}-c93ea"
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        entity, created = create_learning_career_record(
            db,
            account=alice,
            **_payload(
                source, domain=domain, record_kind=record_kind,
                summary=private_phrase,
            ),
        )
        db.commit()

        assert created is True
        assert entity.owner_id == alice.id
        assert entity.entity_type == (
            "learning_record" if domain == "learning" else "career_item"
        )
        assert entity.properties["domain"] == domain
        assert entity.properties["record_kind"] == record_kind
        assert entity.properties["source_links"][0]["source_id"] == source.id
        assert entity.provenance["source_ids"] == [source.id]
        assert entity.provenance["domain"] == domain
        assert get_learning_career_record(
            db, owner_id=alice.id, entity_id=entity.id
        )["execution_policy"]["can_apply_or_submit"] is False
    finally:
        db.close()

    assert private_phrase.encode() not in learning_env.db_path.read_bytes()


def test_source_and_entity_references_are_owner_isolated(learning_env):
    db = learning_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        alice_source = _source(db, alice, "Alice source")
        bob_source = _source(db, bob, "Bob source")
        bob_skill, _ = create_learning_career_record(
            db, account=bob, **_payload(bob_source)
        )

        with pytest.raises(LifeGraphNotFound, match="source"):
            create_learning_career_record(
                db, account=alice, **_payload(bob_source)
            )
        with pytest.raises(LifeGraphNotFound, match="Linked Life entity"):
            create_learning_career_record(
                db,
                account=alice,
                **_payload(
                    alice_source,
                    domain="career",
                    record_kind="role",
                    entity_links=[{
                        "entity_id": bob_skill.id,
                        "relation": "requires_capability",
                    }],
                ),
            )

        alice_skill, _ = create_learning_career_record(
            db, account=alice, **_payload(alice_source)
        )
        db.commit()
        with pytest.raises(LifeGraphNotFound):
            get_learning_career_record(
                db, owner_id=bob.id, entity_id=alice_skill.id
            )
        assert list_learning_career_records(db, owner_id=bob.id)[0][0]["id"] == bob_skill.id
        assert search_learning_career_records(
            db, owner_id=bob.id, query_text="Alice"
        )["items"] == []
    finally:
        db.close()


def test_update_delete_are_cas_guarded_audited_and_keep_encrypted_history(
    learning_env,
):
    db = learning_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        entity, _ = create_learning_career_record(
            db,
            account=alice,
            **_payload(
                source,
                details={"proficiency_level": "beginner", "target_level": "advanced"},
            ),
        )
        updated = update_learning_career_record(
            db,
            account=alice,
            entity_id=entity.id,
            expected_version=1,
            changes={
                "details": {
                    "proficiency_level": "intermediate",
                    "target_level": "advanced",
                }
            },
        )
        assert updated.version == 2
        with pytest.raises(LifeGraphConflict, match="current version 2"):
            update_learning_career_record(
                db,
                account=alice,
                entity_id=entity.id,
                expected_version=1,
                changes={"summary": "stale"},
            )
        deleted = delete_learning_career_record(
            db,
            owner_id=alice.id,
            entity_id=entity.id,
            expected_version=2,
        )
        db.commit()

        assert deleted.version == 3
        assert deleted.deleted_at is not None
        with pytest.raises(LifeGraphNotFound):
            get_learning_career_record(db, owner_id=alice.id, entity_id=entity.id)
        history, truncated = learning_career_history(
            db, owner_id=alice.id, entity_id=entity.id
        )
        assert truncated is False
        assert [row["version"] for row in history] == [3, 2, 1]
        assert db.query(LifeEntityVersion).filter_by(entity_id=entity.id).count() == 3
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id, entity_id=entity.id
        ).count() == 3
    finally:
        db.close()


def test_deterministic_career_capability_gap_plan_portfolio_weekly_action_chain(
    learning_env,
):
    db = learning_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        source = _source(db, alice, "Career evidence")

        practice, _ = create_learning_career_record(
            db,
            account=alice,
            **_payload(
                source,
                record_kind="practice",
                title="Tune one controller",
                weekly_action={
                    "week_start": date(2026, 7, 20),
                    "definition_of_done": "One measured response and reflection",
                    "estimated_minutes": 120,
                    "priority": "high",
                },
            ),
        )
        portfolio, _ = create_learning_career_record(
            db,
            account=alice,
            **_payload(
                source,
                domain="career",
                record_kind="portfolio",
                title="Controls portfolio",
                entity_links=[{
                    "entity_id": practice.id, "relation": "weekly_action"
                }],
            ),
        )
        objective, _ = create_learning_career_record(
            db,
            account=alice,
            **_payload(
                source,
                record_kind="learning_objective",
                title="Master feedback control",
                entity_links=[{
                    "entity_id": portfolio.id, "relation": "evidenced_by"
                }],
            ),
        )
        gap, _ = create_learning_career_record(
            db,
            account=alice,
            **_payload(
                source,
                domain="career",
                record_kind="gap",
                title="Experimental controls gap",
                entity_links=[{
                    "entity_id": objective.id, "relation": "addressed_by"
                }],
            ),
        )
        skill, _ = create_learning_career_record(
            db,
            account=alice,
            **_payload(
                source,
                record_kind="skill",
                title="Feedback control",
                entity_links=[{"entity_id": gap.id, "relation": "has_gap"}],
            ),
        )
        role, _ = create_learning_career_record(
            db,
            account=alice,
            **_payload(
                source,
                domain="career",
                record_kind="role",
                title="Controls research role",
                entity_links=[{
                    "entity_id": skill.id, "relation": "requires_capability"
                }],
            ),
        )
        db.commit()

        read_model = career_learning_plan(
            db,
            owner_id=alice.id,
            career_target_id=role.id,
            week_start=date(2026, 7, 20),
        )
        assert read_model["method"] == "explicit_owner_scoped_links_v1"
        assert read_model["uses_model_inference"] is False
        assert read_model["complete_chain_count"] == 1
        chain = read_model["chains"][0]
        assert [
            chain["capability"]["id"],
            chain["gap"]["id"],
            chain["learning_plan"]["id"],
            chain["portfolio"]["id"],
            chain["weekly_action"]["id"],
        ] == [skill.id, gap.id, objective.id, portfolio.id, practice.id]
        assert read_model["coverage"] == {
            "has_capability": True,
            "has_complete_weekly_chain": True,
            "all_records_source_backed": True,
        }
        assert read_model["execution_policy"]["can_apply_or_submit"] is False
        with pytest.raises(LifeGraphNotFound):
            career_learning_plan(
                db,
                owner_id=bob.id,
                career_target_id=role.id,
                week_start=date(2026, 7, 20),
            )
    finally:
        db.close()


@pytest.mark.parametrize(
    "unsafe",
    [
        {"password": "do-not-store"},
        {"tool_call": {"name": "submit_application"}},
        {"send_email": {"recipient": "hiring@example.test"}},
        {"executor": "browser"},
        {"api_key": "secret"},
        {"toolCall": {"name": "submit_application"}},
        {"apiKey": "secret"},
    ],
)
def test_credentials_and_executor_payloads_are_rejected(learning_env, unsafe):
    db = learning_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        with pytest.raises(LifeGraphError, match="credentials|external action|applications"):
            create_learning_career_record(
                db,
                account=alice,
                **_payload(source, details=unsafe),
            )
    finally:
        db.close()


def test_course_credential_display_name_is_not_treated_as_a_secret(learning_env):
    db = learning_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        course, _ = create_learning_career_record(
            db,
            account=alice,
            **_payload(
                source,
                record_kind="course",
                details={"credential_name": "Certificate of Completion"},
            ),
        )
        assert course.properties["details"]["credential_name"] == (
            "Certificate of Completion"
        )
    finally:
        db.close()


def test_source_links_are_required_and_chain_shapes_are_strict(learning_env):
    db = learning_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        gap, _ = create_learning_career_record(
            db,
            account=alice,
            **_payload(source, domain="career", record_kind="gap"),
        )
        with pytest.raises(LifeGraphError, match="at least one"):
            create_learning_career_record(
                db, account=alice, **_payload(source, source_links=[])
            )
        with pytest.raises(LifeGraphError, match="not valid"):
            create_learning_career_record(
                db,
                account=alice,
                **_payload(
                    source,
                    domain="career",
                    record_kind="role",
                    entity_links=[{"entity_id": gap.id, "relation": "has_gap"}],
                ),
            )
    finally:
        db.close()


def test_deleted_link_targets_remain_safe_tombstone_references(learning_env):
    db = learning_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        skill, _ = create_learning_career_record(
            db, account=alice, **_payload(source, title="Control skill")
        )
        role, _ = create_learning_career_record(
            db,
            account=alice,
            **_payload(
                source,
                domain="career",
                record_kind="role",
                title="Control role",
                entity_links=[{
                    "entity_id": skill.id, "relation": "requires_capability"
                }],
            ),
        )
        delete_learning_career_record(
            db, owner_id=alice.id, entity_id=skill.id, expected_version=1
        )
        db.commit()

        surviving = get_learning_career_record(
            db, owner_id=alice.id, entity_id=role.id
        )
        assert surviving["entity_links"][0]["entity_id"] == skill.id
        assert [
            item["id"] for item in list_learning_career_records(
                db, owner_id=alice.id, domain="career"
            )[0]
        ] == [role.id]
        assert career_learning_plan(
            db,
            owner_id=alice.id,
            career_target_id=role.id,
            week_start=date(2026, 7, 20),
        )["coverage"]["has_capability"] is False

        updated = update_learning_career_record(
            db,
            account=alice,
            entity_id=role.id,
            expected_version=1,
            changes={"title": "Updated control role"},
        )
        assert updated.version == 2
        with pytest.raises(LifeGraphNotFound, match="Linked Life entity"):
            update_learning_career_record(
                db,
                account=alice,
                entity_id=role.id,
                expected_version=2,
                changes={"entity_links": [{
                    "entity_id": skill.id, "relation": "requires_capability"
                }]},
            )
    finally:
        db.close()


@pytest.mark.parametrize(
    "provenance",
    [
        {
            "workflow": {
                "operation": "submit_application",
                "payload": "Authorization: Basic dXNlcjpwYXNz",
            }
        },
        {"note": "Authorization: Basic dXNlcjpwYXNz"},
        {"note": "submit_application"},
        {"note": "Imported from https://user:pass@example.test/private"},
    ],
)
def test_provenance_cannot_hide_executor_or_credential_payloads(
    learning_env, provenance
):
    db = learning_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        with pytest.raises(
            LifeGraphError, match="external action|credentials|applications"
        ):
            create_learning_career_record(
                db,
                account=alice,
                **_payload(source, provenance=provenance),
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_scoped_routes_and_generic_typed_bypass_guard(learning_env):
    db = learning_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        db.commit()
    finally:
        db.close()

    payload = _payload(source)
    response = await _call(
        learning_env,
        "POST",
        "/api/life/learning-career/records",
        json=payload,
    )
    assert response.status_code == 201, response.text
    record = response.json()["record"]

    hidden = await _call(
        learning_env,
        "GET",
        f"/api/life/learning-career/records/{record['id']}",
        user="bob",
    )
    assert hidden.status_code == 404

    bypass = await _call(
        learning_env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "learning_record",
            "title": "Bypass",
            "properties": {
                "learning_career_schema_version": 1,
                "domain": "learning",
                "record_kind": "skill",
            },
        },
    )
    assert bypass.status_code == 400
    assert "learning-career" in bypass.json()["detail"].lower()

    update_bypass = await _call(
        learning_env,
        "PATCH",
        f"/api/life/entities/{record['id']}",
        json={"version": record["version"], "title": "Bypass update"},
    )
    assert update_bypass.status_code == 400

    delete_bypass = await _call(
        learning_env,
        "DELETE",
        f"/api/life/entities/{record['id']}",
        json={"version": record["version"]},
    )
    assert delete_bypass.status_code == 400

    updated = await _call(
        learning_env,
        "PATCH",
        f"/api/life/learning-career/records/{record['id']}",
        json={
            "version": record["version"],
            "summary": "Updated through the typed authority",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["record"]["version"] == 2

    history = await _call(
        learning_env,
        "GET",
        f"/api/life/learning-career/records/{record['id']}/history",
    )
    assert history.status_code == 200
    assert [row["version"] for row in history.json()["items"]] == [2, 1]

    deleted = await _call(
        learning_env,
        "DELETE",
        f"/api/life/learning-career/records/{record['id']}",
        json={"version": 2},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["record"]["version"] == 3
    assert deleted.json()["record"]["deleted_at"] is not None
