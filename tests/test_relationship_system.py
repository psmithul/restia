"""Focused contracts for the typed V3 Relationship Manager."""

from __future__ import annotations

import inspect
import threading
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    ActionAudit,
    Base,
    ContactRecord,
    ContactSource,
    EntityLink,
    LifeEntity,
    LifeEntityVersion,
)
from routes.life_routes import setup_life_routes
from routes.relationship_routes import setup_relationship_routes
from src.identity import ensure_account
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    create_life_entity,
    create_life_source,
)
from src.relationship_service import (
    PERSONAL_MESSAGE_MIN_AUTONOMY,
    create_commitment,
    create_follow_up,
    create_interaction,
    create_relationship_profile,
    get_relationship_profile,
    is_typed_relationship_payload,
    link_relationship_context,
    list_relationship_profiles,
    list_relationship_records,
    relationship_history,
    relationship_reminders,
    serialize_relationship_record,
    update_relationship_profile,
    update_relationship_record,
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
def relationship_env(tmp_path):
    db_path = tmp_path / "relationships.db"
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
    app.include_router(setup_relationship_routes(session_factory=factory))
    yield SimpleNamespace(app=app, Session=factory, engine=engine)
    engine.dispose()


def _account(db, username: str):
    account = ensure_account(db, username)
    db.flush()
    return account


def _source(db, account, title="Explicit relationship note"):
    source, _ = create_life_source(
        db,
        account=account,
        source_type="manual_note",
        title=title,
        idempotency_key=f"source:{account.id}:{title}",
    )
    return source


def _contact(db, account, *, name="Alex Rivera", uid="alex-1"):
    source = ContactSource(
        id=f"contact-source-{account.username}",
        owner_id=account.id,
        kind="local",
        label="Local contacts",
        enabled=True,
        sync_state="ready",
    )
    db.add(source)
    db.flush()
    contact = ContactRecord(
        id=f"contact-{account.username}-{uid}",
        owner_id=account.id,
        source_id=source.id,
        remote_uid=uid,
        payload={
            "name": name,
            "emails": [f"{account.username}@example.test"],
            "phones": ["+91 00000 00000"],
        },
        version=1,
    )
    db.add(contact)
    db.flush()
    return contact


def _profile_payload(source, **overrides):
    payload = {
        "title": "Alex Rivera",
        "subject_kind": "person",
        "relationship_type": "friend",
        "contact_origin": {
            "kind": "contact",
            "label": "Address book",
            "source_id": source.id,
            "observed_at": datetime(2026, 7, 1, 8),
        },
        "important_dates": [{
            "label": "Birthday",
            "date": "1999-08-12",
            "recurring_annually": True,
            "source_id": source.id,
        }],
        "preferences": [{
            "key": "coffee",
            "value": "Prefers filter coffee",
            "source_id": source.id,
        }],
        "care_plan": {
            "interval_days": 30,
            "next_due_at": datetime(2026, 7, 10, 9),
            "source_id": source.id,
        },
        "private_notes": "Private context that remains encrypted.",
        "provenance": {"source_id": source.id, "capture": "manual"},
        "idempotency_key": "relationship:alex",
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


def _assert_no_delivery_fields(value):
    if isinstance(value, dict):
        forbidden = {"send", "send_message", "send_email", "recipient", "executor"}
        assert forbidden.isdisjoint({str(key).lower() for key in value})
        for child in value.values():
            _assert_no_delivery_fields(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_delivery_fields(child)


def test_profile_reuses_contact_authority_and_canonical_owner_graph(relationship_env):
    db = relationship_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        source = _source(db, alice)
        contact = _contact(db, alice)
        project, _ = create_life_entity(
            db, account=alice, entity_type="project", title="Launch project"
        )
        file_entity, _ = create_life_entity(
            db, account=alice, entity_type="file", title="Context note"
        )
        organization, _ = create_relationship_profile(
            db,
            account=alice,
            **_profile_payload(
                source,
                title="Acme Labs",
                subject_kind="organization",
                relationship_type="employer",
                contact_origin={
                    "kind": "manual", "label": "User record", "source_id": source.id,
                },
                important_dates=[], preferences=[], care_plan=None,
                idempotency_key="relationship:acme",
            ),
        )
        profile, created = create_relationship_profile(
            db,
            account=alice,
            contact_record_id=contact.id,
            organization_entity_id=organization.id,
            linked_entity_ids=[project.id, file_entity.id],
            **_profile_payload(source),
        )
        db.commit()

        assert created is True
        assert profile.owner_id == alice.id
        assert profile.entity_type == "person"
        assert profile.properties["relationship_schema_version"] == 1
        assert profile.properties["contact_record_id"] == contact.id
        assert "emails" not in profile.properties
        assert "phones" not in profile.properties
        assert db.query(ContactRecord).filter_by(owner_id=alice.id).count() == 1
        assert {
            row.relation
            for row in db.query(EntityLink).filter_by(
                owner_id=alice.id, source_id=profile.id
            )
        } == {"member_of", "related_project", "related_file"}

        record = get_relationship_profile(
            db, owner_id=alice.id, entity_id=profile.id
        )
        assert record["contact_authority"] == {
            "id": contact.id,
            "source_id": contact.source_id,
            "version": 1,
            "name": "Alex Rivera",
        }
        assert record["execution_policy"] == {
            "record_only": True,
            "can_send_personal_message": False,
            "future_personal_message_min_autonomy": PERSONAL_MESSAGE_MIN_AUTONOMY,
            "future_personal_message_requires_confirmation": True,
        }
        _assert_no_delivery_fields(record)
        with pytest.raises(LifeGraphNotFound, match="not found"):
            get_relationship_profile(db, owner_id=bob.id, entity_id=profile.id)
        rows, _ = list_relationship_profiles(db, owner_id=bob.id)
        assert rows == []
    finally:
        db.close()


def test_profile_validation_blocks_cross_owner_sources_contacts_links_and_actions(
    relationship_env,
):
    db = relationship_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        alice_source = _source(db, alice, "Alice source")
        bob_source = _source(db, bob, "Bob source")
        bob_contact = _contact(db, bob)
        bob_project, _ = create_life_entity(
            db, account=bob, entity_type="project", title="Bob private project"
        )

        with pytest.raises(LifeGraphNotFound, match="Contact record"):
            create_relationship_profile(
                db,
                account=alice,
                contact_record_id=bob_contact.id,
                **_profile_payload(alice_source),
            )
        with pytest.raises(LifeGraphNotFound, match="source not found"):
            create_relationship_profile(
                db,
                account=alice,
                **_profile_payload(
                    alice_source,
                    contact_origin={
                        "kind": "manual", "label": "Cross owner",
                        "source_id": bob_source.id,
                    },
                ),
            )
        with pytest.raises(LifeGraphNotFound, match="Life entity"):
            create_relationship_profile(
                db,
                account=alice,
                linked_entity_ids=[bob_project.id],
                **_profile_payload(alice_source),
            )
        with pytest.raises(LifeGraphError, match="messaging or another external action"):
            create_relationship_profile(
                db,
                account=alice,
                **_profile_payload(
                    alice_source,
                    provenance={
                        "source_id": alice_source.id,
                        "send_message": {"recipient": "never"},
                    },
                ),
            )
        with pytest.raises(LifeGraphError, match="credentials or secrets"):
            create_relationship_profile(
                db,
                account=alice,
                **_profile_payload(
                    alice_source,
                    preferences=[{
                        "key": "password_hint", "value": "never",
                        "source_id": alice_source.id,
                    }],
                ),
            )
    finally:
        db.close()


def test_interactions_promises_followups_cas_history_idempotency_and_audit(
    relationship_env,
):
    db = relationship_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        profile, _ = create_relationship_profile(
            db, account=alice, **_profile_payload(source)
        )
        interaction, created = create_interaction(
            db,
            account=alice,
            profile_id=profile.id,
            title="Coffee catch-up",
            occurred_at=datetime(2026, 7, 5, 10),
            channel="in_person",
            direction="mutual",
            note="Discussed project plans.",
            provenance={"source_id": source.id},
            idempotency_key="interaction:coffee",
        )
        repeated, repeated_created = create_interaction(
            db,
            account=alice,
            profile_id=profile.id,
            title="Coffee catch-up",
            occurred_at=datetime(2026, 7, 5, 10),
            channel="in_person",
            direction="mutual",
            note="Discussed project plans.",
            provenance={"source_id": source.id},
            idempotency_key="interaction:coffee",
        )
        commitment, _ = create_commitment(
            db,
            account=alice,
            profile_id=profile.id,
            title="Share design memo",
            due_at=datetime(2026, 7, 8, 18),
            direction="made_by_me",
            provenance={"source_id": source.id},
            idempotency_key="promise:design-memo",
        )
        follow_up, _ = create_follow_up(
            db,
            account=alice,
            profile_id=profile.id,
            title="Ask about conference",
            due_at=datetime(2026, 7, 9, 9),
            priority="high",
            provenance={"source_id": source.id},
            idempotency_key="followup:conference",
        )

        assert repeated.id == interaction.id
        assert repeated_created is False
        assert db.query(LifeEntity).filter_by(
            owner_id=alice.id, entity_type="interaction"
        ).count() == 1
        assert get_relationship_profile(
            db, owner_id=alice.id, entity_id=profile.id
        )["last_interaction_at"] == "2026-07-05T10:00:00Z"

        updated = update_relationship_record(
            db,
            account=alice,
            entity_id=commitment.id,
            expected_version=1,
            changes={"status": "fulfilled", "note": "Shared in person."},
        )
        assert updated.version == 2
        with pytest.raises(LifeGraphConflict, match="another client"):
            update_relationship_record(
                db,
                account=alice,
                entity_id=commitment.id,
                expected_version=1,
                changes={"status": "cancelled"},
            )
        with pytest.raises(LifeGraphError, match="Interactions do not have due_at"):
            update_relationship_record(
                db,
                account=alice,
                entity_id=interaction.id,
                expected_version=1,
                changes={"due_at": datetime(2026, 7, 20)},
            )
        with pytest.raises(LifeGraphError, match="source_id is required"):
            create_follow_up(
                db,
                account=alice,
                profile_id=profile.id,
                title="Unsourced guess",
                due_at=datetime(2026, 7, 10),
                provenance={},
            )

        rows, truncated = list_relationship_records(
            db, owner_id=alice.id, profile_id=profile.id, limit=10
        )
        assert truncated is False
        assert {row["record_kind"] for row in rows} == {
            "interaction", "commitment", "follow_up"
        }
        assert all(row["source_backed"] is True for row in rows)
        assert serialize_relationship_record(
            db, owner_id=alice.id, entity=follow_up
        )["execution_policy"]["can_send_personal_message"] is False
        history, _ = relationship_history(
            db, owner_id=alice.id, entity_id=commitment.id
        )
        assert [row["version"] for row in history] == [2, 1]
        assert db.query(LifeEntityVersion).filter_by(
            owner_id=alice.id, entity_id=commitment.id
        ).count() == 2
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id, entity_id=commitment.id
        ).count() >= 2
        assert db.query(EntityLink).filter_by(
            owner_id=alice.id, source_id=profile.id,
            relation="has_follow_up", target_id=follow_up.id,
        ).count() == 1
    finally:
        db.close()


def test_reminders_are_bounded_deterministic_explicit_and_never_speculative(
    relationship_env,
):
    db = relationship_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        profile, _ = create_relationship_profile(
            db, account=alice, **_profile_payload(source)
        )
        create_commitment(
            db,
            account=alice,
            profile_id=profile.id,
            title="Return borrowed book",
            due_at=datetime(2026, 7, 8, 8),
            direction="made_by_me",
            provenance={"source_id": source.id},
        )
        create_follow_up(
            db,
            account=alice,
            profile_id=profile.id,
            title="Confirm travel dates",
            due_at=datetime(2026, 7, 9, 8),
            provenance={"source_id": source.id},
        )
        result = relationship_reminders(
            db,
            owner_id=alice.id,
            as_of=datetime(2026, 7, 11, 9),
            due_before=datetime(2026, 7, 12, 9),
            limit=2,
        )

        assert result["count"] == 2
        assert result["truncated"] is True
        assert [row["due_at"] for row in result["items"]] == sorted(
            row["due_at"] for row in result["items"]
        )
        assert all(row["source_backed"] is True for row in result["items"])
        assert all(row["source_id"] == source.id for row in result["items"])
        assert result["inference_policy"] == {
            "explicit_source_backed_records_only": True,
            "speculative_messaging_inference": False,
        }
        assert result["execution_policy"]["can_send_personal_message"] is False
        _assert_no_delivery_fields(result)
    finally:
        db.close()


def test_unanswered_message_and_project_relevance_reminders_are_explicitly_typed(
    relationship_env,
):
    db = relationship_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        profile, _ = create_relationship_profile(
            db, account=alice, **_profile_payload(source, care_plan=None)
        )
        project, _ = create_life_entity(
            db, account=alice, entity_type="project", title="Launch project"
        )
        link_relationship_context(
            db,
            account=alice,
            profile_id=profile.id,
            target_id=project.id,
            provenance={"source_id": source.id},
        )
        unanswered, _ = create_follow_up(
            db,
            account=alice,
            profile_id=profile.id,
            title="Review unanswered message",
            due_at=datetime(2026, 7, 9, 8),
            reminder_kind="unanswered_message",
            provenance={"source_id": source.id},
        )
        relevant, _ = create_follow_up(
            db,
            account=alice,
            profile_id=profile.id,
            title="Reconnect about launch project",
            due_at=datetime(2026, 7, 10, 8),
            reminder_kind="project_relevance",
            provenance={"source_id": source.id},
        )
        unlinked, _ = create_relationship_profile(
            db,
            account=alice,
            **_profile_payload(
                source,
                title="Unlinked person",
                idempotency_key="relationship:unlinked",
                care_plan=None,
            ),
        )
        with pytest.raises(LifeGraphError, match="related project"):
            create_follow_up(
                db,
                account=alice,
                profile_id=unlinked.id,
                title="Unsupported project reminder",
                due_at=datetime(2026, 7, 10, 8),
                reminder_kind="project_relevance",
                provenance={"source_id": source.id},
            )
        result = relationship_reminders(
            db,
            owner_id=alice.id,
            as_of=datetime(2026, 7, 8, 8),
            due_before=datetime(2026, 7, 12, 8),
            limit=10,
        )
        by_id = {row["record_id"]: row for row in result["items"]}
        assert by_id[unanswered.id]["reminder_kind"] == "unanswered_message"
        assert by_id[relevant.id]["reminder_kind"] == "project_relevance"
        assert all(row["source_backed"] is True for row in by_id.values())
    finally:
        db.rollback()
        db.close()


def test_profile_cas_and_context_link_owner_validation(relationship_env):
    db = relationship_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        source = _source(db, alice)
        bob_source = _source(db, bob)
        profile, _ = create_relationship_profile(
            db, account=alice, **_profile_payload(source)
        )
        project, _ = create_life_entity(
            db, account=alice, entity_type="project", title="Shared context"
        )
        bob_file, _ = create_life_entity(
            db, account=bob, entity_type="file", title="Bob private file"
        )
        link, created = link_relationship_context(
            db,
            account=alice,
            profile_id=profile.id,
            target_id=project.id,
            provenance={"source_id": source.id},
        )
        assert created is True
        assert link.relation == "related_project"
        with pytest.raises(LifeGraphNotFound, match="Life entity"):
            link_relationship_context(
                db,
                account=alice,
                profile_id=profile.id,
                target_id=bob_file.id,
                provenance={"source_id": source.id},
            )
        with pytest.raises(LifeGraphNotFound, match="source not found"):
            link_relationship_context(
                db,
                account=alice,
                profile_id=profile.id,
                target_id=project.id,
                provenance={"source_id": bob_source.id},
            )
        updated = update_relationship_profile(
            db,
            account=alice,
            entity_id=profile.id,
            expected_version=1,
            changes={"private_notes": "Updated private context."},
        )
        assert updated.version == 2
        with pytest.raises(LifeGraphConflict, match="another client"):
            update_relationship_profile(
                db,
                account=alice,
                entity_id=profile.id,
                expected_version=1,
                changes={"relationship_type": "colleague"},
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_relationship_api_owner_scope_and_generic_bypass_guards(relationship_env):
    db = relationship_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        db.commit()
        source_id = source.id
    finally:
        db.close()

    payload = {
        "title": "Jordan Lee",
        "subject_kind": "person",
        "relationship_type": "colleague",
        "contact_origin": {
            "kind": "manual", "label": "User entry", "source_id": source_id,
        },
        "important_dates": [],
        "preferences": [],
        "provenance": {"source_id": source_id},
        "idempotency_key": "api:relationship:jordan",
    }
    created = await _call(
        relationship_env, "POST", "/api/life/relationships/profiles", json=payload
    )
    assert created.status_code == 201, created.text
    profile_id = created.json()["profile"]["id"]
    assert created.json()["profile"]["execution_policy"][
        "can_send_personal_message"
    ] is False

    bob_list = await _call(
        relationship_env, "GET", "/api/life/relationships/profiles", user="bob"
    )
    assert bob_list.status_code == 200
    assert bob_list.json()["items"] == []
    bob_get = await _call(
        relationship_env,
        "GET",
        f"/api/life/relationships/profiles/{profile_id}",
        user="bob",
    )
    assert bob_get.status_code == 404

    interaction = await _call(
        relationship_env,
        "POST",
        f"/api/life/relationships/profiles/{profile_id}/interactions",
        json={
            "title": "Planning chat",
            "occurred_at": "2026-07-05T10:00:00Z",
            "channel": "in_person",
            "direction": "mutual",
            "provenance": {"source_id": source_id},
        },
    )
    assert interaction.status_code == 201, interaction.text
    record_id = interaction.json()["record"]["id"]
    changed = await _call(
        relationship_env,
        "PATCH",
        f"/api/life/relationships/records/{record_id}",
        json={"version": 1, "note": "Verified note"},
    )
    assert changed.status_code == 200, changed.text
    stale = await _call(
        relationship_env,
        "PATCH",
        f"/api/life/relationships/records/{record_id}",
        json={"version": 1, "note": "Stale overwrite"},
    )
    assert stale.status_code == 409

    # Generic person entities remain valid when they do not claim the typed schema.
    generic = await _call(
        relationship_env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "person",
            "title": "Generic graph person",
            "properties": {"role": "reviewer"},
        },
    )
    assert generic.status_code == 201, generic.text
    assert is_typed_relationship_payload(
        "person", {"role": "reviewer"}
    ) is False

    typed_bypass = await _call(
        relationship_env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "person",
            "title": "Malformed typed profile",
            "properties": {
                "relationship_schema_version": 1,
                "relationship_record_kind": "profile",
            },
        },
    )
    assert typed_bypass.status_code == 400
    assert "relationships" in typed_bypass.text.lower()

    typed_generic_update = await _call(
        relationship_env,
        "PATCH",
        f"/api/life/entities/{profile_id}",
        json={"version": 1, "summary": "Bypass typed validation"},
    )
    assert typed_generic_update.status_code == 400
    assert "relationships" in typed_generic_update.text.lower()
    typed_generic_delete = await _call(
        relationship_env,
        "DELETE",
        f"/api/life/entities/{profile_id}",
        json={"version": 1, "reason": "Bypass typed validation"},
    )
    assert typed_generic_delete.status_code == 400
    assert "generic life api" in typed_generic_delete.text.lower()


def test_relationship_module_exposes_no_delivery_callable():
    import routes.relationship_routes as relationship_routes
    import src.relationship_service as relationship_service

    names = {
        name.lower()
        for module in (relationship_service, relationship_routes)
        for name, value in vars(module).items()
        if inspect.isfunction(value)
    }
    assert not any(
        name.startswith(("send_", "deliver_", "dispatch_")) for name in names
    )
