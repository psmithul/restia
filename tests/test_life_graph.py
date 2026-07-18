from __future__ import annotations

import sqlite3
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
    AuthIdentity,
    Base,
    CalendarCal,
    CalendarEvent,
    Document,
    EntityLink,
    LifeEntity,
    LifeEntityVersion,
    LifeSource,
    Note,
)
from routes.life_routes import setup_life_routes
from src.identity import ensure_account
from src.calendar_service import create_calendar_event
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    create_entity_link,
    create_life_entity,
    create_life_source,
    delete_entity_link,
    delete_life_entity,
    get_life_entity,
    list_decisions_for_review,
    list_entity_links,
    list_life_entity_versions,
    list_life_sources,
    search_life_entities,
    task_quality_report,
    traverse_life_graph,
    update_life_entity,
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
def life_graph_env(tmp_path):
    db_path = tmp_path / "life-graph.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
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

    app.include_router(setup_life_routes(session_factory=factory))
    yield SimpleNamespace(
        app=app, Session=factory, engine=engine, db_path=db_path
    )
    engine.dispose()


def _account(db, username: str) -> Account:
    account = ensure_account(db, username)
    db.flush()
    return account


def _seed_domain_reference_records(db):
    alice = db.query(Account).filter(Account.username == "alice").one()
    bob = db.query(Account).filter(Account.username == "bob").one()
    records = {
        "owned": [
            ("note", "note", "note-alice"),
            ("file", "document", "document-alice"),
            ("event", "calendar_event", "event-alice"),
        ],
        "cross_owner": [
            ("note", "note", "note-bob"),
            ("file", "document", "document-bob"),
            ("event", "calendar_event", "event-bob"),
        ],
        "null_owner": [
            ("note", "note", "note-null"),
            ("file", "document", "document-null"),
        ],
    }
    db.add_all([
        Note(id="note-alice", owner="alice", title="Alice note"),
        Note(id="note-bob", owner="bob", title="Bob note"),
        Note(id="note-null", owner=None, title="Legacy note"),
        Document(
            id="document-alice", owner="alice", title="Alice document",
            current_content="",
        ),
        Document(
            id="document-bob", owner="bob", title="Bob document",
            current_content="",
        ),
        Document(
            id="document-null", owner=None, title="Legacy document",
            current_content="",
        ),
        CalendarCal(
            id="calendar-alice", owner_id=alice.id, owner="alice", name="Alice"
        ),
        CalendarCal(
            id="calendar-bob", owner_id=bob.id, owner="bob", name="Bob"
        ),
        CalendarEvent(
            uid="event-alice", owner_id=alice.id, calendar_id="calendar-alice",
            summary="Alice series", dtstart=datetime(2026, 7, 18, 9),
            dtend=datetime(2026, 7, 18, 10), rrule="FREQ=DAILY",
        ),
        CalendarEvent(
            uid="event-bob", owner_id=bob.id, calendar_id="calendar-bob",
            summary="Bob event", dtstart=datetime(2026, 7, 18, 11),
            dtend=datetime(2026, 7, 18, 12),
        ),
    ])
    db.flush()
    return records


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        return await client.request(method, path, headers=headers, **kwargs)


def test_service_claims_owned_note_document_and_exact_base_calendar_uid(
    life_graph_env,
):
    db = life_graph_env.Session()
    try:
        alice = _account(db, "alice")
        _account(db, "bob")
        records = _seed_domain_reference_records(db)
        max_uid = "u" * 255
        db.add(CalendarEvent(
            uid=max_uid, owner_id=alice.id, calendar_id="calendar-alice",
            summary="Boundary event",
            dtstart=datetime(2026, 7, 19, 9), dtend=datetime(2026, 7, 19, 10),
        ))
        db.flush()

        accepted = [*records["owned"], ("event", "calendar_event", max_uid)]
        for index, (entity_type, ref_type, ref_id) in enumerate(accepted):
            entity, created = create_life_entity(
                db,
                account=alice,
                entity_type=entity_type,
                title=f"Owned domain record {index}",
                domain_ref_type=ref_type,
                domain_ref_id=ref_id,
            )
            assert created is True
            assert entity.domain_ref_type == ref_type
            assert entity.domain_ref_id == ref_id

        assert db.query(LifeEntity).filter_by(owner_id=alice.id).count() == 4
    finally:
        db.close()


def test_service_rejects_unowned_missing_and_occurrence_domain_refs(
    life_graph_env,
):
    db = life_graph_env.Session()
    try:
        alice = _account(db, "alice")
        _account(db, "bob")
        records = _seed_domain_reference_records(db)

        rejected = [
            *records["cross_owner"],
            *records["null_owner"],
            ("note", "note", "missing-note"),
            ("file", "document", "missing-document"),
            ("event", "calendar_event", "missing-event"),
        ]
        for entity_type, ref_type, ref_id in rejected:
            with pytest.raises(LifeGraphNotFound, match="Domain record not found"):
                create_life_entity(
                    db,
                    account=alice,
                    entity_type=entity_type,
                    title="Rejected domain record",
                    domain_ref_type=ref_type,
                    domain_ref_id=ref_id,
                )

        with pytest.raises(LifeGraphError, match="base event"):
            create_life_entity(
                db,
                account=alice,
                entity_type="event",
                title="Generated occurrence",
                domain_ref_type="calendar_event",
                domain_ref_id="event-alice::2026-07-19T09:00",
            )
        with pytest.raises(LifeGraphError, match="must not exceed 255"):
            create_life_entity(
                db,
                account=alice,
                entity_type="event",
                title="Overlong event ref",
                domain_ref_type="calendar_event",
                domain_ref_id="u" * 256,
            )
        assert db.query(LifeEntity).filter_by(owner_id=alice.id).count() == 0
    finally:
        db.close()


@pytest.mark.asyncio
async def test_api_domain_refs_enforce_owner_existence_and_base_event_contract(
    life_graph_env,
):
    db = life_graph_env.Session()
    try:
        _account(db, "alice")
        _account(db, "bob")
        records = _seed_domain_reference_records(db)
        db.commit()
    finally:
        db.close()

    for index, (entity_type, ref_type, ref_id) in enumerate(records["owned"]):
        response = await _call(
            life_graph_env,
            "POST",
            "/api/life/entities",
            json={
                "entity_type": entity_type,
                "title": f"API-owned domain record {index}",
                "domain_ref_type": ref_type,
                "domain_ref_id": ref_id,
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["entity"]["domain_ref_type"] == ref_type
        assert response.json()["entity"]["domain_ref_id"] == ref_id

    rejected = [
        *records["cross_owner"],
        *records["null_owner"],
        ("note", "note", "missing-note"),
        ("file", "document", "missing-document"),
        ("event", "calendar_event", "missing-event"),
    ]
    for entity_type, ref_type, ref_id in rejected:
        response = await _call(
            life_graph_env,
            "POST",
            "/api/life/entities",
            json={
                "entity_type": entity_type,
                "title": "API-rejected domain record",
                "domain_ref_type": ref_type,
                "domain_ref_id": ref_id,
            },
        )
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "Domain record not found"

    occurrence = await _call(
        life_graph_env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "event",
            "title": "Generated occurrence",
            "domain_ref_type": "calendar_event",
            "domain_ref_id": "event-alice::2026-07-19T09:00",
        },
    )
    assert occurrence.status_code == 400, occurrence.text
    assert "base event" in occurrence.json()["detail"]

    too_long = await _call(
        life_graph_env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "event",
            "title": "Overlong event ref",
            "domain_ref_type": "calendar_event",
            "domain_ref_id": "u" * 256,
        },
    )
    assert too_long.status_code == 422, too_long.text


def test_source_entity_lifecycle_versions_audits_and_encryption(life_graph_env):
    db = life_graph_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")

        source, created = create_life_source(
            db,
            account=alice,
            source_type="email",
            title="Private source title",
            safe_excerpt="Sensitive source excerpt",
            metadata={"mailbox": "private@example.test"},
            idempotency_key="raw-source-key",
        )
        with pytest.raises(LifeGraphConflict, match="different content"):
            create_life_source(
                db,
                account=alice,
                source_type="email",
                title="Mismatched retry body",
                idempotency_key="raw-source-key",
            )
        retried, retry_created = create_life_source(
            db,
            account=alice,
            source_type="email",
            title="Private source title",
            safe_excerpt="Sensitive source excerpt",
            metadata={"mailbox": "private@example.test"},
            idempotency_key="raw-source-key",
        )
        assert created is True
        assert retry_created is False
        assert retried.id == source.id
        assert len(list_life_sources(db, owner_id=alice.id)[0]) == 1
        assert list_life_sources(db, owner_id=bob.id)[0] == []
        assert source.idempotency_key.startswith("sha256:")
        assert "raw-source-key" not in source.idempotency_key

        entity, entity_created = create_life_entity(
            db,
            account=alice,
            entity_type="decision",
            title="Secret launch decision",
            summary="Confidential rationale",
            properties={"budget": "classified-value"},
            provenance={"source_id": source.id, "quote": "private quote"},
            idempotency_key="raw-entity-key",
            reason="Initial capture",
        )
        with pytest.raises(LifeGraphConflict, match="different entity"):
            create_life_entity(
                db,
                account=alice,
                entity_type="decision",
                title="Mismatched entity retry",
                idempotency_key="raw-entity-key",
            )
        retry, entity_retry_created = create_life_entity(
            db,
            account=alice,
            entity_type="decision",
            title="Secret launch decision",
            summary="Confidential rationale",
            properties={"budget": "classified-value"},
            provenance={"source_id": source.id, "quote": "private quote"},
            idempotency_key="raw-entity-key",
        )
        assert entity_created is True
        assert entity_retry_created is False
        assert retry.id == entity.id
        assert entity.idempotency_key.startswith("sha256:")
        assert db.query(LifeEntityVersion).filter_by(entity_id=entity.id).count() == 1
        assert db.query(ActionAudit).filter_by(owner_id=alice.id).count() == 2

        db.commit()
        with sqlite3.connect(life_graph_env.db_path) as raw_db:
            raw_entity = raw_db.execute(
                "SELECT title, summary, properties, provenance, idempotency_key "
                "FROM life_entities WHERE id=?",
                (entity.id,),
            ).fetchone()
            raw_source = raw_db.execute(
                "SELECT title, safe_excerpt, metadata, idempotency_key "
                "FROM life_sources WHERE id=?",
                (source.id,),
            ).fetchone()
            raw_version = raw_db.execute(
                "SELECT snapshot, reason FROM life_entity_versions WHERE entity_id=?",
                (entity.id,),
            ).fetchone()
        combined = " ".join(str(value) for value in (
            *raw_entity, *raw_source, *raw_version
        ))
        for plaintext in (
            "Secret launch decision",
            "Confidential rationale",
            "classified-value",
            "private quote",
            "Private source title",
            "Sensitive source excerpt",
            "raw-source-key",
            "raw-entity-key",
            "Initial capture",
        ):
            assert plaintext not in combined

        entity = update_life_entity(
            db,
            owner_id=alice.id,
            entity_id=entity.id,
            expected_version=1,
            changes={"status": "accepted", "summary": "Updated rationale"},
            reason="Reviewed",
        )
        assert entity.version == 2
        assert entity.status == "accepted"
        with pytest.raises(LifeGraphConflict):
            update_life_entity(
                db,
                owner_id=alice.id,
                entity_id=entity.id,
                expected_version=1,
                changes={"status": "rejected"},
            )
        with pytest.raises(LifeGraphNotFound):
            get_life_entity(db, owner_id=bob.id, entity_id=entity.id)

        versions, truncated = list_life_entity_versions(
            db, owner_id=alice.id, entity_id=entity.id
        )
        assert truncated is False
        assert [row.version for row in versions] == [2, 1]
        assert versions[0].snapshot["summary"] == "Updated rationale"
        assert versions[1].snapshot["summary"] == "Confidential rationale"

        deleted = delete_life_entity(
            db,
            owner_id=alice.id,
            entity_id=entity.id,
            expected_version=2,
            reason="No longer current",
        )
        assert deleted.version == 3
        assert deleted.deleted_at is not None
        with pytest.raises(LifeGraphNotFound):
            get_life_entity(db, owner_id=alice.id, entity_id=entity.id)
        assert get_life_entity(
            db, owner_id=alice.id, entity_id=entity.id, include_deleted=True
        ).id == entity.id
        assert [
            row.version for row in list_life_entity_versions(
                db, owner_id=alice.id, entity_id=entity.id
            )[0]
        ] == [3, 2, 1]
        assert [
            row.action for row in db.query(ActionAudit).filter_by(
                owner_id=alice.id
            ).order_by(ActionAudit.created_at, ActionAudit.id)
        ] == [
            "life.source.created",
            "life.entity.created",
            "life.entity.updated",
            "life.entity.deleted",
        ]
    finally:
        db.close()


def test_provenance_and_links_are_owner_scoped_idempotent_and_soft_deleted(
    life_graph_env,
):
    db = life_graph_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        alice_source, _ = create_life_source(
            db, account=alice, source_type="note", title="Alice note"
        )
        bob_source, _ = create_life_source(
            db, account=bob, source_type="note", title="Bob note"
        )
        first, _ = create_life_entity(
            db,
            account=alice,
            entity_type="project",
            title="Private project",
            provenance={"source_id": alice_source.id},
        )
        second, _ = create_life_entity(
            db, account=alice, entity_type="goal", title="Private goal"
        )
        bob_entity, _ = create_life_entity(
            db, account=bob, entity_type="goal", title="Bob goal"
        )

        with pytest.raises(LifeGraphNotFound, match="Provenance source"):
            create_life_entity(
                db,
                account=alice,
                entity_type="note",
                title="Cross-owner provenance",
                provenance={"source_id": bob_source.id},
            )
        with pytest.raises(LifeGraphNotFound):
            create_entity_link(
                db,
                account=alice,
                source_id=first.id,
                relation="supports",
                target_id=bob_entity.id,
            )

        link, created = create_entity_link(
            db,
            account=alice,
            source_id=first.id,
            relation="supports",
            target_id=second.id,
            metadata={"private": "edge detail"},
            provenance={"source_ids": [alice_source.id]},
        )
        with pytest.raises(LifeGraphConflict, match="different attributes"):
            create_entity_link(
                db,
                account=alice,
                source_id=first.id,
                relation="supports",
                target_id=second.id,
            )
        retry, retry_created = create_entity_link(
            db,
            account=alice,
            source_id=first.id,
            relation="supports",
            target_id=second.id,
            metadata={"private": "edge detail"},
            provenance={"source_ids": [alice_source.id]},
        )
        assert created is True
        assert retry_created is False
        assert retry.id == link.id
        assert [row.id for row in list_entity_links(
            db, owner_id=alice.id, entity_id=first.id, direction="outgoing"
        )[0]] == [link.id]
        assert [row.id for row in list_entity_links(
            db, owner_id=alice.id, entity_id=second.id, direction="incoming"
        )[0]] == [link.id]
        with pytest.raises(LifeGraphNotFound):
            list_entity_links(db, owner_id=bob.id, entity_id=first.id)

        # A corrupt edge whose own owner matches Alice but whose endpoint does
        # not is never surfaced or mutable through the graph service.
        corrupt = EntityLink(
            id="cross-owner-corrupt-edge",
            owner_id=alice.id,
            source_type="life_entity",
            source_id=first.id,
            relation="references",
            target_type="life_entity",
            target_id=bob_entity.id,
        )
        db.add(corrupt)
        db.flush()
        visible = list_entity_links(
            db, owner_id=alice.id, entity_id=first.id
        )[0]
        assert [row.id for row in visible] == [link.id]
        with pytest.raises(LifeGraphNotFound):
            delete_entity_link(
                db,
                owner_id=alice.id,
                link_id=corrupt.id,
                expected_version=1,
            )

        deleted = delete_entity_link(
            db,
            owner_id=alice.id,
            link_id=link.id,
            expected_version=1,
        )
        assert deleted.version == 2
        assert deleted.deleted_at is not None
        assert list_entity_links(
            db, owner_id=alice.id, entity_id=first.id
        )[0] == []
        assert [row.id for row in list_entity_links(
            db,
            owner_id=alice.id,
            entity_id=first.id,
            include_deleted=True,
        )[0]] == [link.id]
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id, action="life.link.created"
        ).count() == 1
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id, action="life.link.deleted"
        ).count() == 1
    finally:
        db.close()


def test_search_traversal_decision_review_and_all_task_quality_flags(
    life_graph_env,
):
    db = life_graph_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        now = datetime(2026, 7, 17, 12, 0, 0)
        past_decision, _ = create_life_entity(
            db,
            account=alice,
            entity_type="decision",
            title="Review pricing",
            summary="Needle Alpha in rationale",
            review_at=now - timedelta(days=1),
        )
        create_life_entity(
            db,
            account=alice,
            entity_type="decision",
            title="Review later",
            review_at=now + timedelta(days=1),
        )
        create_life_entity(
            db,
            account=bob,
            entity_type="decision",
            title="Needle Alpha belonging to Bob",
            review_at=now - timedelta(days=1),
        )
        goal, _ = create_life_entity(
            db, account=alice, entity_type="goal", title="Launch goal"
        )
        action, _ = create_life_entity(
            db, account=alice, entity_type="action", title="Ship release"
        )
        bad_task, _ = create_life_entity(
            db,
            account=alice,
            entity_type="task",
            title="Prepare launch",
            status="waiting",
            properties={
                "blocked": True,
                "irrelevant": True,
                "search_note": "Needle Alpha",
            },
            due_at=now - timedelta(hours=1),
        )
        connected_task, _ = create_life_entity(
            db,
            account=alice,
            entity_type="task",
            title="Prepare launch",
            properties={"next_action": "Email the team"},
            due_at=now + timedelta(days=1),
        )
        hierarchy_goal, _ = create_life_entity(
            db, account=alice, entity_type="goal", title="Hierarchy goal"
        )
        hierarchy_project, _ = create_life_entity(
            db, account=alice, entity_type="project", title="Hierarchy project"
        )
        hierarchy_milestone, _ = create_life_entity(
            db, account=alice, entity_type="milestone", title="Hierarchy milestone"
        )
        hierarchy_task, _ = create_life_entity(
            db,
            account=alice,
            entity_type="task",
            title="Unique hierarchical task",
            properties={"next_action": "Do the next concrete step"},
        )
        create_entity_link(
            db,
            account=alice,
            source_id=connected_task.id,
            relation="supports",
            target_id=goal.id,
        )
        create_entity_link(
            db,
            account=alice,
            source_id=goal.id,
            relation="implemented_by",
            target_id=action.id,
        )
        create_entity_link(
            db,
            account=alice,
            source_id=action.id,
            relation="advances",
            target_id=connected_task.id,
        )
        create_entity_link(
            db,
            account=alice,
            source_id=hierarchy_task.id,
            relation="part_of",
            target_id=hierarchy_milestone.id,
        )
        create_entity_link(
            db,
            account=alice,
            source_id=hierarchy_milestone.id,
            relation="part_of",
            target_id=hierarchy_project.id,
        )
        create_entity_link(
            db,
            account=alice,
            source_id=hierarchy_project.id,
            relation="part_of",
            target_id=hierarchy_goal.id,
        )

        search = search_life_entities(
            db, owner_id=alice.id, query_text="needle alpha", limit=10
        )
        assert {item["entity"]["id"] for item in search["items"]} == {
            past_decision.id, bad_task.id
        }
        assert search["scanned"] <= 500
        assert all(
            item["entity"]["title"] != "Needle Alpha belonging to Bob"
            for item in search["items"]
        )

        decisions, truncated = list_decisions_for_review(
            db, owner_id=alice.id, due_before=now
        )
        assert truncated is False
        assert [row.id for row in decisions] == [past_decision.id]

        quality = task_quality_report(db, owner_id=alice.id, now=now)
        by_id = {
            item["entity"]["id"]: item["flags"] for item in quality["items"]
        }
        assert by_id[bad_task.id] == [
            "overdue",
            "blocked",
            "waiting",
            "irrelevant",
            "missing_next_action",
            "duplicate",
            "goal_disconnected",
        ]
        assert by_id[connected_task.id] == ["duplicate"]
        assert by_id[hierarchy_task.id] == []
        assert set(quality["flag_counts"]) == {
            "overdue",
            "blocked",
            "waiting",
            "irrelevant",
            "missing_next_action",
            "duplicate",
            "goal_disconnected",
        }

        traversal = traverse_life_graph(
            db, owner_id=alice.id, entity_id=connected_task.id, depth=4
        )
        assert {item["id"] for item in traversal["entities"]} == {
            connected_task.id, goal.id, action.id
        }
        assert len(traversal["links"]) == 3
        assert traversal["depth_requested"] == 4
        bounded = traverse_life_graph(
            db,
            owner_id=alice.id,
            entity_id=connected_task.id,
            depth=4,
            limit=2,
        )
        assert len(bounded["entities"]) == 2
        assert bounded["truncated"] is True
    finally:
        db.close()


def test_full_email_to_goal_chain_is_source_backed_owner_scoped_and_traversable(
    life_graph_env,
):
    db = life_graph_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        source, _ = create_life_source(
            db,
            account=alice,
            source_type="email",
            title="Launch review thread",
            source_ref="email-account:message-42",
            safe_excerpt="Decision and follow-up context",
            idempotency_key="full-chain-email-source",
        )

        def entity(entity_type, title, **kwargs):
            row, _ = create_life_entity(
                db,
                account=alice,
                entity_type=entity_type,
                title=title,
                provenance={"source_id": source.id, "authority": "test-chain"},
                idempotency_key=f"full-chain:{entity_type}:{title}",
                **kwargs,
            )
            return row

        email = entity(
            "message", "Launch review email",
            domain_ref_type="life_source", domain_ref_id=source.id,
            properties={
                "channel": "email", "thread_id": "thread-7",
                "message_id": "message-42",
            },
        )
        person = entity("person", "Alex Reviewer")
        project = entity("project", "Restia V3")
        decision = entity("decision", "Use shared SQL authority")
        task = entity(
            "task", "Finish release verification",
            due_at=datetime(2026, 7, 20, 12),
            properties={"definition_of_done": "All V3 gates pass"},
        )
        deadline = entity(
            "reminder", "Release deadline",
            due_at=datetime(2026, 7, 20, 12),
            properties={"kind": "deadline"},
        )
        event_result = create_calendar_event(
            db,
            account=alice,
            summary="Release verification block",
            event_type="focus",
            dtstart="2026-07-20T10:00:00+05:30",
            dtend="2026-07-20T12:00:00+05:30",
            idempotency_key="full-chain-calendar-event",
        )
        event = event_result.graph_entity
        file = entity(
            "file", "Release evidence.md",
            properties={"content_sha256": "a" * 64},
        )
        goal = entity("goal", "Ship a trustworthy V3")
        chain = (
            (email, "from", person),
            (person, "works_on", project),
            (project, "has_decision", decision),
            (decision, "creates", task),
            (task, "has_deadline", deadline),
            (deadline, "scheduled_as", event),
            (event, "uses", file),
            (file, "supports", goal),
        )
        for left, relation, right in chain:
            create_entity_link(
                db,
                account=alice,
                source_id=left.id,
                relation=relation,
                target_id=right.id,
                provenance={"source_id": source.id, "authority": "test-chain"},
                confidence=100,
            )
        foreign, _ = create_life_entity(
            db, account=bob, entity_type="goal", title="Bob private goal",
        )
        with pytest.raises(LifeGraphNotFound):
            create_entity_link(
                db, account=alice, source_id=goal.id,
                relation="must_not_cross", target_id=foreign.id,
            )
        db.commit()

        traversal = traverse_life_graph(
            db, owner_id=alice.id, entity_id=email.id, depth=8, limit=50,
        )
        assert traversal["depth_requested"] == 8
        assert traversal["depth_reached"] == 8
        assert traversal["truncated"] is False
        assert [
            (left.entity_type, relation, right.entity_type)
            for left, relation, right in chain
        ] == [
            ("message", "from", "person"),
            ("person", "works_on", "project"),
            ("project", "has_decision", "decision"),
            ("decision", "creates", "task"),
            ("task", "has_deadline", "reminder"),
            ("reminder", "scheduled_as", "event"),
            ("event", "uses", "file"),
            ("file", "supports", "goal"),
        ]
        assert {row["id"] for row in traversal["entities"]} == {
            email.id, person.id, project.id, decision.id, task.id,
            deadline.id, event.id, file.id, goal.id,
        }
        assert all(row.owner_id == alice.id for row in db.query(LifeEntity).filter(
            LifeEntity.id.in_({item["id"] for item in traversal["entities"]})
        ))
        assert all(row["version"] >= 1 for row in traversal["entities"])
        assert all(row["confidence"] == 100 for row in traversal["links"])
        assert all(row["provenance"]["source_id"] == source.id for row in traversal["links"])
        assert foreign.id not in {row["id"] for row in traversal["entities"]}
        assert task.due_at == deadline.due_at
    finally:
        db.close()


@pytest.mark.asyncio
async def test_life_routes_share_cookie_and_api_principal_and_enforce_scopes(
    life_graph_env,
):
    empty = await _call(life_graph_env, "GET", "/api/life/entities")
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"items": [], "count": 0, "truncated": False}

    missing_scope = await _call(
        life_graph_env,
        "POST",
        "/api/life/entities",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:read"},
        json={"entity_type": "task", "title": "Scoped task"},
    )
    assert missing_scope.status_code == 403
    assert "life:write" in missing_scope.text

    created = await _call(
        life_graph_env,
        "POST",
        "/api/life/entities",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:write"},
        json={
            "entity_type": "task",
            "title": "Scoped task",
            "properties": {"next_action": "Do it"},
            "idempotency_key": "route-create-key",
        },
    )
    assert created.status_code == 201, created.text
    entity = created.json()["entity"]

    write_scope_read = await _call(
        life_graph_env,
        "GET",
        "/api/life/entities",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:write"},
    )
    assert write_scope_read.status_code == 200
    assert write_scope_read.json()["items"][0]["id"] == entity["id"]

    api_read = await _call(
        life_graph_env,
        "GET",
        "/api/life/entities",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:read"},
    )
    cookie_read = await _call(
        life_graph_env, "GET", "/api/life/entities", user="alice"
    )
    assert api_read.status_code == cookie_read.status_code == 200
    assert api_read.json()["items"][0]["id"] == entity["id"]
    assert cookie_read.json()["items"][0]["id"] == entity["id"]

    bob_get = await _call(
        life_graph_env,
        "GET",
        f"/api/life/entities/{entity['id']}",
        user="bob",
    )
    assert bob_get.status_code == 404

    updated = await _call(
        life_graph_env,
        "PATCH",
        f"/api/life/entities/{entity['id']}",
        json={"version": 1, "status": "in_progress", "reason": "Started"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["entity"]["version"] == 2
    stale = await _call(
        life_graph_env,
        "PATCH",
        f"/api/life/entities/{entity['id']}",
        json={"version": 1, "status": "done"},
    )
    assert stale.status_code == 409

    versions = await _call(
        life_graph_env,
        "GET",
        f"/api/life/entities/{entity['id']}/versions",
    )
    assert versions.status_code == 200, versions.text
    assert [item["version"] for item in versions.json()["items"]] == [2, 1]

    deleted = await _call(
        life_graph_env,
        "DELETE",
        f"/api/life/entities/{entity['id']}",
        json={"version": 2, "reason": "Complete"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["entity"]["deleted_at"] is not None
    hidden = await _call(
        life_graph_env, "GET", f"/api/life/entities/{entity['id']}"
    )
    assert hidden.status_code == 404
    visible = await _call(
        life_graph_env,
        "GET",
        f"/api/life/entities/{entity['id']}",
        params={"include_deleted": "true"},
    )
    assert visible.status_code == 200


@pytest.mark.asyncio
async def test_life_routes_cover_sources_links_queries_and_graph(life_graph_env):
    source_response = await _call(
        life_graph_env,
        "POST",
        "/api/life/sources",
        json={
            "source_type": "email",
            "title": "Launch thread",
            "idempotency_key": "route-source-key",
        },
    )
    assert source_response.status_code == 201, source_response.text
    source = source_response.json()["source"]
    mismatched_source_retry = await _call(
        life_graph_env,
        "POST",
        "/api/life/sources",
        json={
            "source_type": "email",
            "title": "Mismatched retry",
            "idempotency_key": "route-source-key",
        },
    )
    assert mismatched_source_retry.status_code == 409
    source_retry = await _call(
        life_graph_env,
        "POST",
        "/api/life/sources",
        json={
            "source_type": "email",
            "title": "Launch thread",
            "idempotency_key": "route-source-key",
        },
    )
    assert source_retry.status_code == 201
    assert source_retry.json() == {"source": source, "created": False}
    sources = await _call(life_graph_env, "GET", "/api/life/sources")
    assert sources.status_code == 200
    assert [item["id"] for item in sources.json()["items"]] == [source["id"]]

    goal_response = await _call(
        life_graph_env,
        "POST",
        "/api/life/entities",
        json={"entity_type": "goal", "title": "Public launch"},
    )
    task_response = await _call(
        life_graph_env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "task",
            "title": "Write launch memo",
            "summary": "Route search marker",
            "due_at": "2020-01-01T00:00:00Z",
            "provenance": {"source_id": source["id"]},
        },
    )
    decision_response = await _call(
        life_graph_env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "decision",
            "title": "Use the new launch plan",
            "review_at": "2020-01-01T00:00:00Z",
        },
    )
    assert goal_response.status_code == 201, goal_response.text
    assert task_response.status_code == 201, task_response.text
    assert decision_response.status_code == 201, decision_response.text
    goal = goal_response.json()["entity"]
    task = task_response.json()["entity"]
    decision = decision_response.json()["entity"]

    link_response = await _call(
        life_graph_env,
        "POST",
        "/api/life/links",
        json={
            "source_id": task["id"],
            "relation": "supports",
            "target_id": goal["id"],
            "provenance": {"source_id": source["id"]},
        },
    )
    assert link_response.status_code == 201, link_response.text
    link = link_response.json()["link"]
    links = await _call(
        life_graph_env,
        "GET",
        "/api/life/links",
        params={"entity_id": task["id"], "direction": "outgoing"},
    )
    assert links.status_code == 200, links.text
    assert [item["id"] for item in links.json()["items"]] == [link["id"]]

    graph = await _call(
        life_graph_env,
        "GET",
        f"/api/life/entities/{task['id']}/graph",
        params={"depth": 1},
    )
    assert graph.status_code == 200, graph.text
    assert {item["id"] for item in graph.json()["entities"]} == {
        task["id"], goal["id"]
    }
    search = await _call(
        life_graph_env,
        "GET",
        "/api/life/search",
        params={"q": "route search marker"},
    )
    assert search.status_code == 200, search.text
    assert [
        item["entity"]["id"] for item in search.json()["items"]
    ] == [task["id"]]
    decisions = await _call(
        life_graph_env,
        "GET",
        "/api/life/decisions/review",
        params={"due_before": "2021-01-01T00:00:00Z"},
    )
    assert decisions.status_code == 200, decisions.text
    assert [item["id"] for item in decisions.json()["items"]] == [decision["id"]]
    quality = await _call(
        life_graph_env,
        "GET",
        "/api/life/tasks/quality",
        params={"at": "2021-01-01T00:00:00Z"},
    )
    assert quality.status_code == 200, quality.text
    assert quality.json()["items"][0]["entity"]["id"] == task["id"]
    assert quality.json()["items"][0]["flags"] == [
        "overdue", "missing_next_action"
    ]

    deleted = await _call(
        life_graph_env,
        "DELETE",
        f"/api/life/links/{link['id']}",
        json={"version": 1},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["link"]["deleted_at"] is not None
    hidden_links = await _call(
        life_graph_env,
        "GET",
        "/api/life/links",
        params={"entity_id": task["id"]},
    )
    assert hidden_links.status_code == 200
    assert hidden_links.json()["items"] == []


def test_life_entity_versions_are_append_only(life_graph_env):
    db = life_graph_env.Session()
    try:
        alice = _account(db, "alice")
        entity, _ = create_life_entity(
            db, account=alice, entity_type="note", title="Immutable history"
        )
        db.commit()
        version = db.query(LifeEntityVersion).filter_by(
            entity_id=entity.id
        ).one()
        version.reason = "Tampered"
        with pytest.raises(RuntimeError, match="append-only"):
            db.flush()
        db.rollback()
        assert db.query(LifeEntity).filter_by(id=entity.id).count() == 1
        assert db.query(LifeSource).count() == 0
        assert db.query(AuthIdentity).filter_by(account_id=alice.id).count() == 1
    finally:
        db.close()


def test_client_reasons_stay_encrypted_and_out_of_plaintext_audits(
    life_graph_env,
):
    db = life_graph_env.Session()
    try:
        alice = _account(db, "alice")
        create_reason = "private client reason for creation"
        update_reason = "private client reason for update"
        delete_reason = "private client reason for deletion"
        link_create_reason = "private client reason for linking"
        link_delete_reason = "private client reason for unlinking"

        entity, _ = create_life_entity(
            db,
            account=alice,
            entity_type="note",
            title="Reason privacy subject",
            reason=create_reason,
        )
        target, _ = create_life_entity(
            db,
            account=alice,
            entity_type="goal",
            title="Reason privacy target",
        )
        entity = update_life_entity(
            db,
            owner_id=alice.id,
            entity_id=entity.id,
            expected_version=1,
            changes={"status": "in_progress"},
            reason=update_reason,
        )
        link, _ = create_entity_link(
            db,
            account=alice,
            source_id=entity.id,
            relation="supports",
            target_id=target.id,
            reason=link_create_reason,
        )
        delete_entity_link(
            db,
            owner_id=alice.id,
            link_id=link.id,
            expected_version=1,
            reason=link_delete_reason,
        )
        delete_life_entity(
            db,
            owner_id=alice.id,
            entity_id=entity.id,
            expected_version=2,
            reason=delete_reason,
        )
        db.commit()

        version_reasons = [
            row.reason
            for row in db.query(LifeEntityVersion)
            .filter_by(entity_id=entity.id)
            .order_by(LifeEntityVersion.version)
        ]
        assert version_reasons == [
            create_reason,
            update_reason,
            delete_reason,
        ]
        audits = db.query(ActionAudit).filter(
            ActionAudit.owner_id == alice.id,
            ActionAudit.entity_id.in_((entity.id, link.id)),
        ).all()
        assert {row.details["audit"]["reason"] for row in audits} == {
            "Life entity created",
            "Life entity updated",
            "Life entity deleted",
            "Life entities linked",
            "Life entity link deleted",
        }

        with sqlite3.connect(life_graph_env.db_path) as raw_db:
            raw_audits = " ".join(
                row[0]
                for row in raw_db.execute(
                    "SELECT details FROM action_audit WHERE owner_id=?",
                    (alice.id,),
                ).fetchall()
            )
            raw_version_reasons = " ".join(
                row[0]
                for row in raw_db.execute(
                    "SELECT reason FROM life_entity_versions WHERE entity_id=?",
                    (entity.id,),
                ).fetchall()
            )
        for private_reason in (
            create_reason,
            update_reason,
            delete_reason,
            link_create_reason,
            link_delete_reason,
        ):
            assert private_reason not in raw_audits
            assert private_reason not in raw_version_reasons
    finally:
        db.close()
