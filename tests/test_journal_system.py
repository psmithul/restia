"""Focused contracts for V3 typed Journal & Reflection records."""

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

from core.database import ActionAudit, Base, LifeEntity, LifeEntityVersion
from routes.journal_routes import setup_journal_routes
from routes.life_routes import setup_life_routes
from src.identity import ensure_account
from src.journal_service import (
    create_journal_entry,
    delete_journal_entry,
    get_journal_entry,
    journal_entry_history,
    journal_period_review,
    list_journal_entries,
    search_journal_entries,
    serialize_journal_entry,
    update_journal_entry,
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
def journal_env(tmp_path):
    db_path = tmp_path / "journal.db"
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
    app.include_router(setup_journal_routes(session_factory=factory))
    yield SimpleNamespace(
        app=app, Session=factory, engine=engine, db_path=db_path
    )
    engine.dispose()


def _account(db, username: str):
    account = ensure_account(db, username)
    db.flush()
    return account


def _entry_payload(entry_date: date = date(2026, 7, 13), **overrides):
    payload = {
        "title": "Daily reflection",
        "entry_date": entry_date,
        "body": "Private reflection body",
        "mood": {"label": "steady", "score": 6, "energy": 7},
        "moments": ["Coffee with a friend"],
        "wins": ["Finished the control-system model"],
        "difficulties": ["Context switching"],
        "lessons": ["Block uninterrupted work first"],
        "gratitude": ["Helpful feedback"],
        "ideas": ["Build a smaller calibration jig"],
        "decisions": ["Keep Friday for applications"],
        "principles": ["Protect the highest-leverage work first"],
        "time_notes": ["Deep work took two focused hours"],
        "relationship_notes": ["Ask the recommender for feedback"],
        "goal_progress": ["Application essay reached first draft"],
        "next_changes": ["Move phone outside the room"],
        "promises": [{
            "id": "send_draft",
            "text": "Send the draft",
            "status": "open",
            "due_date": date(2026, 7, 17),
        }],
        "changes": ["Moved deep work before meetings"],
        "improvements": ["Fewer context switches"],
        "pattern_tags": ["context_switching"],
        "evidence": [],
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


def test_real_app_registers_journal_router_once():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert source.count("from routes.journal_routes import setup_journal_routes") == 1
    assert source.count("app.include_router(setup_journal_routes())") == 1


def test_journal_uses_encrypted_account_owned_canonical_life_entity(journal_env):
    private_phrase = "unique-private-journal-phrase-9d938"
    db = journal_env.Session()
    try:
        alice = _account(db, "alice")
        entity, created = create_journal_entry(
            db,
            account=alice,
            **_entry_payload(body=private_phrase, sensitivity="restricted"),
        )
        db.commit()

        assert created is True
        assert entity.entity_type == "journal_entry"
        assert entity.owner_id == alice.id
        assert entity.sensitivity == "restricted"
        assert entity.properties["journal_schema_version"] == 1
        assert entity.properties["entry_date"] == "2026-07-13"
        assert entity.provenance == {"capture": "manual", "domain": "journal"}
        assert serialize_journal_entry(entity)["execution_policy"] == {
            "record_only": True,
            "can_execute_external_actions": False,
            "uses_model_inference": False,
        }
    finally:
        db.close()

    assert private_phrase.encode() not in journal_env.db_path.read_bytes()


def test_owner_isolation_applies_to_get_list_search_report_and_evidence(journal_env):
    db = journal_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        source, _ = create_life_source(
            db, account=alice, source_type="note", title="Private source"
        )
        bob_source, _ = create_life_source(
            db, account=bob, source_type="note", title="Bob source"
        )
        supporting, _ = create_journal_entry(
            db, account=alice, **_entry_payload(title="Supporting entry")
        )
        entry, _ = create_journal_entry(
            db,
            account=alice,
            **_entry_payload(
                title="Alice only",
                evidence=[{
                    "id": "proof",
                    "label": "Explicit evidence",
                    "source_id": source.id,
                    "entity_id": supporting.id,
                }],
                provenance={"capture": "manual", "source_id": source.id},
            ),
        )
        db.commit()

        with pytest.raises(LifeGraphNotFound):
            get_journal_entry(db, owner_id=bob.id, entity_id=entry.id)
        assert list_journal_entries(db, owner_id=bob.id)[0] == []
        assert search_journal_entries(
            db, owner_id=bob.id, query_text="Alice only"
        )["items"] == []
        assert journal_period_review(
            db, owner_id=bob.id, period="weekly", anchor_date=date(2026, 7, 13)
        )["entry_count"] == 0

        with pytest.raises(LifeGraphNotFound, match="evidence source"):
            create_journal_entry(
                db,
                account=alice,
                **_entry_payload(evidence=[{
                    "label": "Wrong owner", "source_id": bob_source.id
                }]),
            )
    finally:
        db.close()


def test_update_is_cas_guarded_audited_and_has_encrypted_version_history(journal_env):
    db = journal_env.Session()
    try:
        alice = _account(db, "alice")
        entity, _ = create_journal_entry(
            db, account=alice, **_entry_payload()
        )
        updated = update_journal_entry(
            db,
            account=alice,
            entity_id=entity.id,
            expected_version=1,
            changes={
                "wins": ["Finished the control-system model", "Submitted the draft"],
                "promises": [{
                    "id": "send_draft",
                    "text": "Send the draft",
                    "status": "kept",
                    "due_date": date(2026, 7, 17),
                    "completed_on": date(2026, 7, 16),
                }],
            },
        )
        db.commit()

        assert updated.version == 2
        with pytest.raises(LifeGraphConflict, match="current version 2"):
            update_journal_entry(
                db,
                account=alice,
                entity_id=entity.id,
                expected_version=1,
                changes={"body": "stale update"},
            )
        history, truncated = journal_entry_history(
            db, owner_id=alice.id, entity_id=entity.id
        )
        assert truncated is False
        assert [row["version"] for row in history] == [2, 1]
        assert set(history[0]["changed_fields"]) == {"wins", "promises"}
        assert db.query(LifeEntityVersion).filter_by(entity_id=entity.id).count() == 2
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id, entity_id=entity.id
        ).count() == 2
    finally:
        db.close()


def test_delete_retains_tombstone_history_and_hides_current_record(journal_env):
    db = journal_env.Session()
    try:
        alice = _account(db, "alice")
        entity, _ = create_journal_entry(
            db, account=alice, **_entry_payload()
        )
        deleted = delete_journal_entry(
            db,
            owner_id=alice.id,
            entity_id=entity.id,
            expected_version=1,
            reason="User removed private reflection",
        )
        db.commit()

        assert deleted.status == "deleted"
        assert deleted.deleted_at is not None
        assert deleted.version == 2
        assert db.query(LifeEntity).filter_by(id=entity.id).one().deleted_at is not None
        assert list_journal_entries(db, owner_id=alice.id)[0] == []
        with pytest.raises(LifeGraphNotFound):
            get_journal_entry(db, owner_id=alice.id, entity_id=entity.id)
        history, _ = journal_entry_history(
            db, owner_id=alice.id, entity_id=entity.id
        )
        assert [row["version"] for row in history] == [2, 1]
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id, entity_id=entity.id, action="life.entity.deleted"
        ).count() == 1
    finally:
        db.close()


def test_weekly_review_is_deterministic_bounded_and_evidence_backed(journal_env):
    db = journal_env.Session()
    try:
        alice = _account(db, "alice")
        source, _ = create_life_source(
            db, account=alice, source_type="file", title="Weekly evidence"
        )
        create_journal_entry(
            db,
            account=alice,
            **_entry_payload(
                entry_date=date(2026, 7, 12),
                title="Carry in",
                promises=[{
                    "id": "old_open",
                    "text": "Finish the portfolio evidence",
                    "status": "open",
                    "due_date": date(2026, 7, 14),
                }],
            ),
        )
        first, _ = create_journal_entry(
            db,
            account=alice,
            **_entry_payload(
                entry_date=date(2026, 7, 13),
                title="Monday",
                mood={"label": "focused", "score": 4, "energy": 5},
                changes=["Moved deep work before meetings"],
                improvements=["Fewer context switches"],
                pattern_tags=["context_switching"],
                evidence=[{
                    "id": "review_note",
                    "label": "Weekly note",
                    "source_id": source.id,
                }],
            ),
        )
        second, _ = create_journal_entry(
            db,
            account=alice,
            **_entry_payload(
                entry_date=date(2026, 7, 19),
                title="Sunday",
                mood={"label": "focused", "score": 8, "energy": 7},
                difficulties=["Context switching"],
                changes=["Moved deep work before meetings"],
                improvements=["Fewer context switches"],
                pattern_tags=["context_switching"],
                promises=[],
            ),
        )
        create_journal_entry(
            db,
            account=alice,
            **_entry_payload(entry_date=date(2026, 7, 20), title="Next Monday"),
        )
        db.commit()

        review = journal_period_review(
            db,
            owner_id=alice.id,
            period="weekly",
            anchor_date=date(2026, 7, 17),
        )

        assert review["period_start"] == "2026-07-13"
        assert review["period_end"] == "2026-07-19"
        assert review["entry_ids"] == [first.id, second.id]
        assert review["entry_count"] == 2
        assert review["method"] == "deterministic_structured_fields_v1"
        assert review["uses_model_inference"] is False
        assert review["mood"]["average_score"] == 6.0
        assert review["mood"]["score_change"] == 4
        assert review["changes"][0]["occurrences"] == 2
        assert review["improvements"][0]["occurrences"] == 2
        assert review["principles"][0]["occurrences"] == 2
        assert review["time"][0]["occurrences"] == 2
        assert review["relationships"][0]["occurrences"] == 2
        assert review["goal_progress"][0]["occurrences"] == 2
        assert review["next_changes"][0]["occurrences"] == 2
        assert any(
            item["pattern"] == "context_switching"
            and item["occurrences"] == 2
            for item in review["repeated_patterns"]
        )
        carry_in = next(
            item for item in review["unfinished_commitments"]
            if item["id"] == "old_open"
        )
        assert carry_in["carried_in"] is True
        assert carry_in["overdue_at_period_end"] is True
        assert review["evidence"] == [{
            "id": "review_note",
            "label": "Weekly note",
            "source_id": source.id,
            "entity_id": None,
            "reference": "",
            "entry_id": first.id,
            "entry_date": "2026-07-13",
        }]
        assert review["execution_policy"]["can_execute_external_actions"] is False
    finally:
        db.close()


@pytest.mark.parametrize(
    ("period", "anchor", "expected_start", "expected_end", "inside", "outside"),
    [
        ("monthly", date(2026, 2, 18), "2026-02-01", "2026-02-28", date(2026, 2, 28), date(2026, 3, 1)),
        ("annual", date(2024, 8, 1), "2024-01-01", "2024-12-31", date(2024, 12, 31), date(2025, 1, 1)),
    ],
)
def test_monthly_and_annual_review_boundaries(
    journal_env, period, anchor, expected_start, expected_end, inside, outside
):
    db = journal_env.Session()
    try:
        alice = _account(db, "alice")
        included, _ = create_journal_entry(
            db, account=alice, **_entry_payload(entry_date=inside, title="Included")
        )
        create_journal_entry(
            db, account=alice, **_entry_payload(entry_date=outside, title="Excluded")
        )
        db.commit()
        report = journal_period_review(
            db, owner_id=alice.id, period=period, anchor_date=anchor
        )
        assert report["period_start"] == expected_start
        assert report["period_end"] == expected_end
        assert report["entry_ids"] == [included.id]
    finally:
        db.close()


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"provenance": {"api_token": "do-not-store"}}, "credentials"),
        ({"provenance": {"execute": "send email"}}, "action payloads"),
        ({"body": "Authorization: Bearer top-secret"}, "credentials"),
        ({"sensitivity": "public"}, "private or restricted"),
        ({"promises": [{"text": "Done", "status": "kept"}]}, "completed_on"),
    ],
)
def test_credentials_actions_public_entries_and_invalid_promises_fail_closed(
    journal_env, overrides, message
):
    db = journal_env.Session()
    try:
        alice = _account(db, "alice")
        with pytest.raises(LifeGraphError, match=message):
            create_journal_entry(
                db, account=alice, **_entry_payload(**overrides)
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_http_routes_are_owner_scoped_strict_and_block_generic_bypass(journal_env):
    create = await _call(
        journal_env,
        "POST",
        "/api/life/journal/entries",
        json={
            "title": "HTTP reflection",
            "entry_date": "2026-07-17",
            "body": "Route body",
            "wins": ["Shipped focused tests"],
            "provenance": {"capture": "manual"},
        },
    )
    assert create.status_code == 201, create.text
    entry = create.json()["entry"]

    bob_get = await _call(
        journal_env,
        "GET",
        f"/api/life/journal/entries/{entry['id']}",
        user="bob",
    )
    assert bob_get.status_code == 404
    bob_list = await _call(
        journal_env, "GET", "/api/life/journal/entries", user="bob"
    )
    assert bob_list.json()["items"] == []

    strict = await _call(
        journal_env,
        "POST",
        "/api/life/journal/entries",
        json={
            "title": "Unsafe",
            "entry_date": "2026-07-17",
            "execute": "send email",
        },
    )
    assert strict.status_code == 422

    generic_create = await _call(
        journal_env,
        "POST",
        "/api/life/entities",
        json={
            "entity_type": "journal_entry",
            "title": "Bypass",
            "properties": {"journal_schema_version": 1, "entry_date": "2026-07-17"},
        },
    )
    assert generic_create.status_code == 400
    assert "journal/entries" in generic_create.json()["detail"]

    generic_update = await _call(
        journal_env,
        "PATCH",
        f"/api/life/entities/{entry['id']}",
        json={"version": 1, "summary": "Bypass update"},
    )
    assert generic_update.status_code == 400
    generic_delete = await _call(
        journal_env,
        "DELETE",
        f"/api/life/entities/{entry['id']}",
        json={"version": 1},
    )
    assert generic_delete.status_code == 400

    update = await _call(
        journal_env,
        "PATCH",
        f"/api/life/journal/entries/{entry['id']}",
        json={"version": 1, "wins": ["Shipped focused tests", "Reviewed history"]},
    )
    assert update.status_code == 200, update.text
    assert update.json()["entry"]["version"] == 2
    stale = await _call(
        journal_env,
        "PATCH",
        f"/api/life/journal/entries/{entry['id']}",
        json={"version": 1, "body": "Stale"},
    )
    assert stale.status_code == 409

    history = await _call(
        journal_env,
        "GET",
        f"/api/life/journal/entries/{entry['id']}/history",
    )
    assert [item["version"] for item in history.json()["items"]] == [2, 1]

    deleted = await _call(
        journal_env,
        "DELETE",
        f"/api/life/journal/entries/{entry['id']}",
        json={"version": 2, "reason": "User deleted journal entry"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["entry"]["status"] == "deleted"
    after_delete = await _call(
        journal_env,
        "GET",
        f"/api/life/journal/entries/{entry['id']}",
    )
    assert after_delete.status_code == 404
    tombstone_history = await _call(
        journal_env,
        "GET",
        f"/api/life/journal/entries/{entry['id']}/history",
    )
    assert [item["version"] for item in tombstone_history.json()["items"]] == [3, 2, 1]
