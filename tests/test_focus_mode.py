from __future__ import annotations

import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

import src.focus_mode as focus_mode
from core.database import (
    Account,
    ActionAudit,
    Base,
    EntityLink,
    FocusSession,
    LifeEntity,
    LifeEntityVersion,
    PlanningItem,
)
from routes.focus_routes import setup_focus_routes
from src.focus_mode import FocusConflict, add_focus_progress, pause_focus_session
from src.identity import ensure_account


class _IdentityAuthority:
    def __init__(self, *usernames: str):
        self._config_lock = threading.Lock()
        self._identity_migrations: set[str] = set()
        self.retired_usernames: set[str] = set()
        self.users = {username: {} for username in usernames}

    @property
    def is_configured(self) -> bool:
        return bool(self.users)


@pytest.fixture()
def focus_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'focus.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    alice = ensure_account(db, "alice")
    bob = ensure_account(db, "bob")
    entities = {
        "task": LifeEntity(
            id="00000000-0000-4000-8000-000000000001",
            owner_id=alice.id,
            entity_type="task",
            title="Finish controls report",
            status="open",
            properties={},
            provenance={},
            version=1,
        ),
        "action": LifeEntity(
            id="00000000-0000-4000-8000-000000000002",
            owner_id=alice.id,
            entity_type="action",
            title="Run controller test",
            status="active",
            properties={},
            provenance={},
            version=1,
        ),
        "note": LifeEntity(
            id="00000000-0000-4000-8000-000000000003",
            owner_id=alice.id,
            entity_type="note",
            title="Reference notes",
            status="active",
            properties={},
            provenance={},
            version=1,
        ),
        "inactive": LifeEntity(
            id="00000000-0000-4000-8000-000000000004",
            owner_id=alice.id,
            entity_type="milestone",
            title="Already shipped",
            status="completed",
            properties={},
            provenance={},
            version=1,
        ),
        "bob_task": LifeEntity(
            id="00000000-0000-4000-8000-000000000005",
            owner_id=bob.id,
            entity_type="task",
            title="Bob private task",
            status="active",
            properties={},
            provenance={},
            version=1,
        ),
        "in_progress": LifeEntity(
            id="00000000-0000-4000-8000-000000000006",
            owner_id=alice.id,
            entity_type="milestone",
            title="Tune the controller",
            status="in_progress",
            properties={},
            provenance={},
            version=1,
        ),
        "blocked": LifeEntity(
            id="00000000-0000-4000-8000-000000000007",
            owner_id=alice.id,
            entity_type="task",
            title="Wait for lab access",
            status="blocked",
            properties={},
            provenance={},
            version=1,
        ),
    }
    db.add_all(entities.values())
    db.commit()
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
            request.state.api_token_id = "focus-test-token"
            request.state.current_user = "api"
        else:
            request.state.api_token = False
            request.state.current_user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(setup_focus_routes(session_factory=factory))
    env = SimpleNamespace(
        app=app,
        Session=factory,
        engine=engine,
        alice=alice,
        bob=bob,
        entities=entities,
    )
    try:
        yield env
    finally:
        engine.dispose()


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.request(method, path, headers=headers, **kwargs)


def _start_body(env, key="task"):
    return {
        "entity_id": env.entities[key].id,
        "entity_version": 1,
        "definition_of_done": "A verified result is attached",
    }


def _create_planning_projection(env) -> tuple[str, str]:
    item_id = "00000000-0000-4000-8000-000000000080"
    entity_id = "00000000-0000-4000-8000-000000000081"
    with env.Session() as db:
        db.add(PlanningItem(
            id=item_id,
            owner="alice",
            title="Canonical planning task",
            details="",
            status="open",
            priority="normal",
            source="user",
            version=1,
        ))
        db.add(LifeEntity(
            id=entity_id,
            owner_id=env.alice.id,
            entity_type="task",
            title="Canonical planning task",
            status="open",
            properties={},
            provenance={},
            domain_ref_type="planning_item",
            domain_ref_id=item_id,
            version=1,
        ))
        db.commit()
    return item_id, entity_id


@pytest.mark.asyncio
async def test_start_is_owner_scoped_focusable_and_one_live_lease(focus_env):
    started = await _call(
        focus_env, "POST", "/api/life/focus/start", json=_start_body(focus_env)
    )
    assert started.status_code == 201, started.text
    session = started.json()["session"]
    assert session["state"] == "active"
    assert session["definition_of_done"] == "A verified result is attached"
    assert session["entity"]["id"] == focus_env.entities["task"].id

    current = await _call(focus_env, "GET", "/api/life/focus/current")
    assert current.status_code == 200
    assert current.json()["session"]["id"] == session["id"]
    second = await _call(
        focus_env,
        "POST",
        "/api/life/focus/start",
        json=_start_body(focus_env, "action"),
    )
    assert second.status_code == 409

    assert (
        await _call(focus_env, "GET", "/api/life/focus/current", user="bob")
    ).json()["session"] is None
    cross_owner = await _call(
        focus_env,
        "POST",
        "/api/life/focus/start",
        user="bob",
        json=_start_body(focus_env),
    )
    assert cross_owner.status_code == 404

    abandoned = await _call(
        focus_env,
        "POST",
        f"/api/life/focus/{session['id']}/abandon",
        json={"version": 1},
    )
    assert abandoned.status_code == 200
    assert abandoned.json()["session"]["state"] == "abandoned"

    wrong_type = await _call(
        focus_env,
        "POST",
        "/api/life/focus/start",
        json=_start_body(focus_env, "note"),
    )
    inactive = await _call(
        focus_env,
        "POST",
        "/api/life/focus/start",
        json=_start_body(focus_env, "inactive"),
    )
    assert wrong_type.status_code == inactive.status_code == 409


@pytest.mark.asyncio
async def test_start_accepts_actionable_statuses_and_rejects_blocked(focus_env):
    for key in ("task", "action", "in_progress"):
        started = await _call(
            focus_env,
            "POST",
            "/api/life/focus/start",
            json=_start_body(focus_env, key),
        )
        assert started.status_code == 201, (key, started.text)
        abandoned = await _call(
            focus_env,
            "POST",
            f"/api/life/focus/{started.json()['session']['id']}/abandon",
            json={"version": 1},
        )
        assert abandoned.status_code == 200

    blocked = await _call(
        focus_env,
        "POST",
        "/api/life/focus/start",
        json=_start_body(focus_env, "blocked"),
    )
    assert blocked.status_code == 409


@pytest.mark.asyncio
async def test_start_revalidates_owned_open_canonical_planning_task(focus_env):
    _item_id, entity_id = _create_planning_projection(focus_env)

    started = await _call(
        focus_env,
        "POST",
        "/api/life/focus/start",
        json={
            "entity_id": entity_id,
            "entity_version": 1,
            "definition_of_done": "Canonical work is verified",
        },
    )

    assert started.status_code == 201, started.text
    assert started.json()["session"]["entity_id"] == entity_id


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical_state", ["completed", "missing", "wrong_owner"])
async def test_start_rejects_stale_canonical_planning_projection(
    focus_env,
    canonical_state,
):
    item_id, entity_id = _create_planning_projection(focus_env)
    with focus_env.Session() as db:
        item = db.query(PlanningItem).filter_by(id=item_id).one()
        if canonical_state == "completed":
            item.status = "completed"
            item.completed_at = datetime(2026, 7, 17, 8, 30, 0)
        elif canonical_state == "missing":
            db.delete(item)
        else:
            item.owner = "bob"
        db.commit()

    rejected = await _call(
        focus_env,
        "POST",
        "/api/life/focus/start",
        json={
            "entity_id": entity_id,
            "entity_version": 1,
            "definition_of_done": "Stale work must not start",
        },
    )

    assert rejected.status_code == 409
    assert "Canonical planning task" in rejected.json()["detail"]
    with focus_env.Session() as db:
        assert db.query(FocusSession).count() == 0


@pytest.mark.asyncio
async def test_pause_resume_journals_elapsed_completion_and_audits(
    focus_env, monkeypatch
):
    clock = {"now": datetime(2026, 7, 17, 8, 0, 0)}
    monkeypatch.setattr(focus_mode, "utcnow_naive", lambda: clock["now"])

    started = await _call(
        focus_env, "POST", "/api/life/focus/start", json=_start_body(focus_env)
    )
    session = started.json()["session"]
    session_id = session["id"]
    overall_started_at = session["started_at"]
    assert session["active_since"] == overall_started_at

    clock["now"] += timedelta(seconds=30)
    paused = await _call(
        focus_env,
        "POST",
        f"/api/life/focus/{session_id}/pause",
        json={"version": 1},
    )
    assert paused.status_code == 200, paused.text
    assert paused.json()["session"]["elapsed_seconds"] == 30
    assert paused.json()["session"]["started_at"] == overall_started_at
    assert paused.json()["session"]["active_since"] is None

    clock["now"] += timedelta(seconds=20)
    resumed = await _call(
        focus_env,
        "POST",
        f"/api/life/focus/{session_id}/resume",
        json={"version": 2},
    )
    assert resumed.status_code == 200
    assert resumed.json()["session"]["started_at"] == overall_started_at
    assert resumed.json()["session"]["active_since"] != overall_started_at

    clock["now"] += timedelta(seconds=5)
    interruption = await _call(
        focus_env,
        "POST",
        f"/api/life/focus/{session_id}/interruptions",
        json={"version": 3, "text": "Unexpected phone call"},
    )
    assert interruption.status_code == 200
    assert interruption.json()["session"]["interruptions"][0]["text"] == (
        "Unexpected phone call"
    )
    progress = await _call(
        focus_env,
        "POST",
        f"/api/life/focus/{session_id}/progress",
        json={"version": 4, "text": "Controller test now passes"},
    )
    assert progress.status_code == 200
    evidence = await _call(
        focus_env,
        "POST",
        f"/api/life/focus/{session_id}/evidence",
        json={
            "version": 5,
            "text": "Test report attached",
            "metadata": {"ref": "file:test-report"},
        },
    )
    assert evidence.status_code == 200

    clock["now"] += timedelta(seconds=25)
    completed = await _call(
        focus_env,
        "POST",
        f"/api/life/focus/{session_id}/complete",
        json={"version": 6},
    )
    assert completed.status_code == 200, completed.text
    final = completed.json()["session"]
    assert final["state"] == "completed"
    assert final["elapsed_seconds"] == 60
    assert final["started_at"] == overall_started_at
    assert final["active_since"] is None
    assert len(final["progress"]) == len(final["evidence"]) == 1
    assert (
        await _call(focus_env, "GET", "/api/life/focus/current")
    ).json()["session"] is None
    history = await _call(focus_env, "GET", "/api/life/focus/history")
    assert [row["id"] for row in history.json()["sessions"]] == [session_id]

    db = focus_env.Session()
    try:
        actions = [row.action for row in db.query(ActionAudit).all()]
        assert actions.count("focus.started") == 1
        assert actions.count("focus.paused") == 1
        assert actions.count("focus.resumed") == 1
        assert actions.count("focus.interruption_added") == 1
        assert actions.count("focus.progress_added") == 1
        assert actions.count("focus.evidence_added") == 1
        assert actions.count("focus.completed") == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_completion_creates_owned_followups_once_and_cross_owner_fails(
    focus_env,
):
    started = await _call(
        focus_env, "POST", "/api/life/focus/start", json=_start_body(focus_env)
    )
    session_id = started.json()["session"]["id"]
    payload = {
        "version": 1,
        "follow_ups": [
            {
                "title": "Share the report",
                "summary": "Send it to the supervisor",
                "definition_of_done": "Supervisor has the PDF",
                "due_at": "2026-07-21T15:30:00Z",
            },
            {"title": "Archive raw logs"},
        ],
    }
    completed = await _call(
        focus_env,
        "POST",
        f"/api/life/focus/{session_id}/complete",
        json=payload,
    )
    assert completed.status_code == 200, completed.text
    created_ids = {
        row["id"] for row in completed.json()["follow_ups"]
    }
    assert len(created_ids) == 2
    assert set(completed.json()["session"]["follow_up_entity_ids"]) == created_ids

    # A response-loss retry returns the original result without duplicating
    # tasks or audits, even though it carries the pre-completion version.
    retry = await _call(
        focus_env,
        "POST",
        f"/api/life/focus/{session_id}/complete",
        json=payload,
    )
    assert retry.status_code == 200
    assert {row["id"] for row in retry.json()["follow_ups"]} == created_ids
    denied = await _call(
        focus_env,
        "POST",
        f"/api/life/focus/{session_id}/abandon",
        user="bob",
        json={"version": 1},
    )
    assert denied.status_code == 404

    db = focus_env.Session()
    try:
        tasks = db.query(LifeEntity).filter(
            LifeEntity.id.in_(created_ids),
            LifeEntity.owner_id == focus_env.alice.id,
        ).all()
        assert len(tasks) == 2
        assert all(task.entity_type == "task" and task.status == "open" for task in tasks)
        assert all(
            task.properties["source_focus_session_id"] == session_id
            for task in tasks
        )
        assert all(task.domain_ref_type == "planning_item" for task in tasks)
        planning_ids = {task.domain_ref_id for task in tasks}
        assert None not in planning_ids
        planning_items = db.query(PlanningItem).filter(
            PlanningItem.id.in_(planning_ids)
        ).all()
        assert len(planning_items) == 2
        assert all(
            item.owner == "alice"
            and item.status == "open"
            and item.source == "focus"
            for item in planning_items
        )
        share_item = next(
            item for item in planning_items if item.title == "Share the report"
        )
        assert share_item.due_date == "2026-07-21"
        assert "Supervisor has the PDF" in share_item.details
        assert db.query(PlanningItem).filter_by(owner="bob").count() == 0
        assert db.query(ActionAudit).filter_by(
            action="life.entity.created"
        ).count() == 2
        completion_audit = db.query(ActionAudit).filter_by(
            action="focus.completed"
        ).one()
        assert set(
            completion_audit.details["follow_up_planning_item_ids"]
        ) == planning_ids
    finally:
        db.close()


def test_stale_writer_and_bounded_journal_fail_safe(focus_env, monkeypatch):
    seed = focus_env.Session()
    try:
        session = focus_mode.start_focus_session(
            seed,
            account=seed.query(Account).filter_by(id=focus_env.alice.id).one(),
            entity_id=focus_env.entities["task"].id,
            expected_entity_version=1,
            definition_of_done="Concurrency is verified",
        )
        seed.commit()
        session_id = session.id
    finally:
        seed.close()

    winner = focus_env.Session()
    stale = focus_env.Session()
    try:
        # Materialize stale version 1 before the winning transaction commits.
        stale.query(FocusSession).filter_by(id=session_id).one()
        pause_focus_session(
            winner,
            owner_id=focus_env.alice.id,
            session_id=session_id,
            expected_version=1,
        )
        winner.commit()
        with pytest.raises(FocusConflict, match="another client"):
            pause_focus_session(
                stale,
                owner_id=focus_env.alice.id,
                session_id=session_id,
                expected_version=1,
            )
        stale.rollback()
    finally:
        winner.close()
        stale.close()

    db = focus_env.Session()
    try:
        current = db.query(FocusSession).filter_by(id=session_id).one()
        current.state = "active"
        current.active_since = datetime(2026, 7, 17, 9, 0, 0)
        current.paused_at = None
        db.commit()
        monkeypatch.setattr(focus_mode, "MAX_FOCUS_ENTRIES", 2)
        for index, version in enumerate((2, 3, 4)):
            add_focus_progress(
                db,
                owner_id=focus_env.alice.id,
                session_id=session_id,
                expected_version=version,
                text=f"Progress {index}",
            )
            db.commit()
        db.refresh(current)
        assert [row["text"] for row in current.progress["entries"]] == [
            "Progress 1",
            "Progress 2",
        ]
    finally:
        db.close()


def test_start_claim_rejects_entity_changed_after_preflight(
    focus_env, monkeypatch
):
    db = focus_env.Session()
    account = db.query(Account).filter_by(id=focus_env.alice.id).one()
    original = focus_mode._focusable_entity

    def change_after_preflight(db_arg, **kwargs):
        entity = original(db_arg, **kwargs)
        db_arg.query(LifeEntity).filter(
            LifeEntity.id == entity.id,
            LifeEntity.owner_id == entity.owner_id,
        ).update(
            {LifeEntity.status: "completed", LifeEntity.version: 2},
            synchronize_session=False,
        )
        return entity

    monkeypatch.setattr(
        focus_mode, "_focusable_entity", change_after_preflight
    )
    try:
        with pytest.raises(FocusConflict, match="no longer actionable"):
            focus_mode.start_focus_session(
                db,
                account=account,
                entity_id=focus_env.entities["task"].id,
                expected_entity_version=1,
                definition_of_done="Race is closed",
            )
        assert db.query(FocusSession).count() == 0
    finally:
        db.rollback()
        db.close()


def test_schema_rejects_cross_owner_focus_and_version_rows(focus_env):
    now = datetime(2026, 7, 17, 9, 0, 0)
    db = focus_env.Session()
    try:
        db.add(FocusSession(
            id="00000000-0000-4000-8000-000000000101",
            owner_id=focus_env.bob.id,
            entity_id=focus_env.entities["task"].id,
            state="active",
            definition_of_done="Must be rejected",
            started_at=now,
            active_since=now,
            elapsed_seconds=0,
            interruptions={"entries": []},
            progress={"entries": []},
            evidence={"entries": []},
            follow_up_entity_ids={"ids": []},
            version=1,
        ))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        db.add(LifeEntityVersion(
            id="00000000-0000-4000-8000-000000000102",
            owner_id=focus_env.bob.id,
            entity_id=focus_env.entities["task"].id,
            version=1,
            snapshot={},
            reason="Must be rejected",
        ))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


@pytest.mark.asyncio
async def test_life_api_token_scopes_share_the_same_principal(focus_env):
    read_only_start = await _call(
        focus_env,
        "POST",
        "/api/life/focus/start",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:read"},
        json=_start_body(focus_env),
    )
    assert read_only_start.status_code == 403

    api_start = await _call(
        focus_env,
        "POST",
        "/api/life/focus/start",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:write"},
        json=_start_body(focus_env),
    )
    assert api_start.status_code == 201, api_start.text
    session_id = api_start.json()["session"]["id"]
    browser = await _call(focus_env, "GET", "/api/life/focus/current")
    assert browser.json()["session"]["id"] == session_id

    write_scope_read = await _call(
        focus_env,
        "GET",
        "/api/life/focus/current",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:write"},
    )
    assert write_scope_read.status_code == 200
    assert write_scope_read.json()["session"]["id"] == session_id
    api_read = await _call(
        focus_env,
        "GET",
        "/api/life/focus/current",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:read"},
    )
    assert api_read.status_code == 200
    assert api_read.json()["session"]["id"] == session_id


@pytest.mark.asyncio
async def test_current_focus_includes_only_owned_linked_work_context(focus_env):
    with focus_env.Session() as db:
        note = LifeEntity(
            id="00000000-0000-4000-8000-000000000201",
            owner_id=focus_env.alice.id,
            entity_type="note",
            title="Controller notes",
            summary="Tuning observations",
            status="active",
            properties={},
            provenance={},
            version=1,
        )
        project = LifeEntity(
            id="00000000-0000-4000-8000-000000000202",
            owner_id=focus_env.alice.id,
            entity_type="project",
            title="Controls capstone",
            status="active",
            properties={},
            provenance={},
            version=1,
        )
        db.add_all([note, project])
        db.flush()
        db.add_all([
            EntityLink(
                id="00000000-0000-4000-8000-000000000211",
                owner_id=focus_env.alice.id,
                source_type="life_entity",
                source_id=focus_env.entities["task"].id,
                relation="uses",
                target_type="life_entity",
                target_id=note.id,
                meta_data={},
                provenance={},
                confidence=100,
                sensitivity="private",
                version=1,
            ),
            EntityLink(
                id="00000000-0000-4000-8000-000000000212",
                owner_id=focus_env.alice.id,
                source_type="life_entity",
                source_id=project.id,
                relation="contains",
                target_type="life_entity",
                target_id=focus_env.entities["task"].id,
                meta_data={},
                provenance={},
                confidence=100,
                sensitivity="private",
                version=1,
            ),
            # A corrupt edge carrying Alice's owner id must not expose Bob's
            # endpoint through Focus context.
            EntityLink(
                id="00000000-0000-4000-8000-000000000213",
                owner_id=focus_env.alice.id,
                source_type="life_entity",
                source_id=focus_env.entities["task"].id,
                relation="mentions",
                target_type="life_entity",
                target_id=focus_env.entities["bob_task"].id,
                meta_data={},
                provenance={},
                confidence=100,
                sensitivity="private",
                version=1,
            ),
        ])
        db.commit()

    started = await _call(
        focus_env, "POST", "/api/life/focus/start", json=_start_body(focus_env)
    )
    assert started.status_code == 201, started.text
    session = started.json()["session"]
    assert session["entity"]["properties"] == {}
    assert [(row["entity_type"], row["title"], row["direction"]) for row in session["context"]] == [
        ("note", "Controller notes", "outgoing"),
        ("project", "Controls capstone", "incoming"),
    ]
    assert "Bob private task" not in str(session)

    restored = await _call(focus_env, "GET", "/api/life/focus/current")
    assert restored.status_code == 200
    assert restored.json()["session"]["context"] == session["context"]


@pytest.mark.asyncio
async def test_today_planning_target_is_projected_only_on_explicit_focus_start(
    focus_env,
):
    item_id = "00000000-0000-4000-8000-000000000220"
    with focus_env.Session() as db:
        db.add(PlanningItem(
            id=item_id,
            owner="alice",
            title="Finish Today plan",
            details="Verify the owner-scoped result",
            status="open",
            priority="high",
            source="user",
            version=4,
        ))
        db.commit()
        assert db.query(LifeEntity).filter_by(
            owner_id=focus_env.alice.id,
            domain_ref_type="planning_item",
            domain_ref_id=item_id,
        ).count() == 0

    started = await _call(
        focus_env,
        "POST",
        "/api/life/focus/start",
        json={
            "domain_ref_type": "planning_item",
            "domain_ref_id": item_id,
            "domain_ref_version": 4,
            "definition_of_done": "The plan is finished and verified",
        },
    )
    assert started.status_code == 201, started.text
    entity = started.json()["session"]["entity"]
    assert entity["domain_ref_type"] == "planning_item"
    assert entity["domain_ref_id"] == item_id
    assert entity["title"] == "Finish Today plan"

    with focus_env.Session() as db:
        projections = db.query(LifeEntity).filter_by(
            owner_id=focus_env.alice.id,
            domain_ref_type="planning_item",
            domain_ref_id=item_id,
        ).all()
        assert len(projections) == 1


@pytest.mark.asyncio
async def test_today_planning_focus_rejects_stale_or_mixed_targets(focus_env):
    item_id = "00000000-0000-4000-8000-000000000230"
    with focus_env.Session() as db:
        db.add(PlanningItem(
            id=item_id,
            owner="alice",
            title="Versioned plan",
            details="",
            status="open",
            priority="normal",
            source="user",
            version=3,
        ))
        db.commit()

    stale = await _call(
        focus_env,
        "POST",
        "/api/life/focus/start",
        json={
            "domain_ref_type": "planning_item",
            "domain_ref_id": item_id,
            "domain_ref_version": 2,
            "definition_of_done": "Must not start stale work",
        },
    )
    assert stale.status_code == 409

    mixed = await _call(
        focus_env,
        "POST",
        "/api/life/focus/start",
        json={
            **_start_body(focus_env),
            "domain_ref_type": "planning_item",
            "domain_ref_id": item_id,
            "domain_ref_version": 3,
        },
    )
    assert mixed.status_code == 400
    with focus_env.Session() as db:
        assert db.query(FocusSession).count() == 0
        assert db.query(LifeEntity).filter_by(
            domain_ref_type="planning_item", domain_ref_id=item_id
        ).count() == 0
