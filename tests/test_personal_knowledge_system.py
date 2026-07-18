"""Focused contracts for canonical V3 Personal Memory and Knowledge."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    ActionAudit,
    Base,
    Document,
    LifeEntity,
    LifeEntityVersion,
    LifeSource,
    Note,
    Project,
)
from src.identity import ensure_account
from src.life_graph import (
    LifeGraphConflict,
    LifeGraphError,
    LifeGraphNotFound,
    create_life_entity,
    delete_life_entity,
)
from src.personal_knowledge_service import (
    EPISTEMIC_STATUSES,
    KNOWLEDGE_SOURCE_KINDS,
    MARKDOWN_INDEX_CONTRACT,
    MEMORY_KINDS,
    citation_backed_answer_evidence,
    create_knowledge_source,
    create_personal_knowledge_record,
    delete_personal_knowledge_record,
    get_knowledge_source,
    get_personal_knowledge_record,
    list_knowledge_sources,
    list_personal_knowledge_records,
    list_stale_personal_knowledge,
    markdown_index_rebuild_manifest,
    personal_knowledge_history,
    search_knowledge_sources,
    search_personal_knowledge_records,
    update_personal_knowledge_record,
)


UTC = timezone.utc
OBSERVED = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
REVIEWED = datetime(2026, 7, 2, 9, 0, tzinfo=UTC)
STALE_AT = datetime(2026, 7, 3, 9, 0, tzinfo=UTC)


@pytest.fixture()
def knowledge_env(tmp_path):
    db_path = tmp_path / "personal-knowledge.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield SimpleNamespace(Session=factory, engine=engine, db_path=db_path)
    engine.dispose()


def _account(db, username: str):
    account = ensure_account(db, username)
    db.flush()
    return account


def _document(db, owner, *, title="Durable document", language="markdown", content="# Durable\nEvidence"):
    row = Document(
        id=f"doc-{owner.username}-{abs(hash((title, content))) % 10_000_000}",
        owner=owner.username,
        title=title,
        language=language,
        current_content=content,
        is_active=True,
        archived=False,
    )
    db.add(row)
    db.flush()
    return row


def _note(db, owner, *, title="Source note", content="Durable note evidence"):
    row = Note(
        id=f"note-{owner.username}-{abs(hash((title, content))) % 10_000_000}",
        owner=owner.username,
        title=title,
        content=content,
    )
    db.add(row)
    db.flush()
    return row


def _source(db, account, *, title="Verified lesson", kind="lesson", **kwargs):
    source, _ = create_knowledge_source(
        db,
        account=account,
        source_kind=kind,
        title=title,
        observed_at=OBSERVED,
        safe_excerpt="Bounded supporting evidence",
        source_ref=f"source:{kind}:{title.lower().replace(' ', '-')}",
        **kwargs,
    )
    return source


def _citation(source, **overrides):
    result = {
        "target_kind": "life_source",
        "target_id": source.id,
        "relation": "supports",
        "locator": "source excerpt",
    }
    result.update(overrides)
    return result


def _record(db, account, source, **overrides):
    payload = {
        "memory_kind": "semantic",
        "title": "Bounded fact",
        "statement": "A citation-backed statement",
        "epistemic_status": "confirmed_fact",
        "claim_origin": "source",
        "citations": [_citation(source)],
        "observed_at": OBSERVED,
        "reviewed_at": REVIEWED,
        "tags": ["evidence", "private"],
        "details": {"scope": "focused contract"},
    }
    payload.update(overrides)
    record, _ = create_personal_knowledge_record(db, account=account, **payload)
    return record


def _status_payload(status: str):
    if status == "confirmed_fact":
        return {"claim_origin": "source", "reviewed_at": REVIEWED}
    if status == "user_statement":
        return {"claim_origin": "user"}
    if status == "assumption":
        return {"claim_origin": "user"}
    if status == "inference":
        return {
            "claim_origin": "model",
            "inference": {
                "generated_by": "model",
                "method": "bounded synthesis",
                "basis": "The cited evidence and nothing else",
                "model_label": "local-provider",
            },
        }
    if status == "stale":
        return {
            "claim_origin": "source",
            "reviewed_at": REVIEWED,
            "stale_at": STALE_AT,
        }
    if status == "gap":
        return {"claim_origin": "user"}
    raise AssertionError(status)


@pytest.mark.parametrize("memory_kind", sorted(MEMORY_KINDS))
def test_every_memory_kind_is_encrypted_account_owned_and_citation_backed(
    knowledge_env, memory_kind
):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice, title=f"{memory_kind} source")
        record = _record(
            db,
            alice,
            source,
            memory_kind=memory_kind,
            observed_at=OBSERVED,
        )
        db.commit()

        assert record.owner_id == alice.id
        assert record.entity_type == "source"
        assert record.properties["memory_kind"] == memory_kind
        assert record.provenance["source_ids"] == [source.id]
        assert record.properties["retrieval_policy"]["uses_model_calls"] is False
        assert type(LifeEntity.__table__.c.properties.type).__name__ == "EncryptedJSON"
    finally:
        db.close()


@pytest.mark.parametrize("epistemic_status", sorted(EPISTEMIC_STATUSES))
def test_every_epistemic_status_stays_explicit(knowledge_env, epistemic_status):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice, title=f"{epistemic_status} source")
        record = _record(
            db,
            alice,
            source,
            title=f"{epistemic_status} record",
            statement=f"Explicit {epistemic_status}",
            epistemic_status=epistemic_status,
            **_status_payload(epistemic_status),
        )
        db.commit()
        serialized = get_personal_knowledge_record(
            db, owner_id=alice.id, record_id=record.id
        )
        assert serialized["epistemic_status"] == epistemic_status
        assert serialized["claim_origin"] == _status_payload(epistemic_status)["claim_origin"]
        if epistemic_status == "gap":
            assert serialized["gap"] == {
                "explicit": True,
                "question": f"Explicit {epistemic_status}",
            }
        if epistemic_status == "inference":
            assert serialized["inference"]["explicitly_labelled"] is True
    finally:
        db.close()


@pytest.mark.parametrize("source_kind", sorted(KNOWLEDGE_SOURCE_KINDS))
def test_every_knowledge_source_kind_uses_life_source_authority(
    knowledge_env, source_kind
):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        descriptor = {}
        source_ref = f"source:{source_kind}"
        if source_kind == "note":
            note = _note(db, alice, title=f"{source_kind} evidence")
            descriptor = {"target_kind": "note", "target_id": note.id}
            source_ref = None
        elif source_kind in {"document", "markdown"}:
            document = _document(
                db,
                alice,
                title=f"{source_kind} evidence",
                language="markdown" if source_kind == "markdown" else "text",
            )
            descriptor = {"target_kind": "document", "target_id": document.id}
            source_ref = None
        elif source_kind in {"bookmark", "web_page"}:
            descriptor = {"canonical_uri": f"https://example.test/{source_kind}"}
            source_ref = None
        source, created = create_knowledge_source(
            db,
            account=alice,
            source_kind=source_kind,
            title=f"{source_kind} source",
            observed_at=OBSERVED,
            descriptor=descriptor,
            source_ref=source_ref,
            safe_excerpt="Evidence excerpt",
        )
        db.commit()

        assert created is True
        assert source.owner_id == alice.id
        assert source.source_type == f"knowledge_{source_kind}"
        serialized = get_knowledge_source(
            db, owner_id=alice.id, source_id=source.id
        )
        assert serialized["source_kind"] == source_kind
        assert serialized["immutability_contract"] == {
            "principal_is_immutable": True,
            "content_changes_create_new_source": True,
        }
        assert type(LifeSource.__table__.c["metadata"].type).__name__ == "EncryptedJSON"
    finally:
        db.close()


def test_private_claim_and_source_content_are_not_plaintext_on_disk(knowledge_env):
    claim_phrase = "v3-personal-memory-private-claim-9e1c"
    source_phrase = "v3-personal-memory-private-source-7b2a"
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice, title=source_phrase)
        _record(
            db,
            alice,
            source,
            title="Private encrypted title",
            statement=claim_phrase,
            details={"private_context": claim_phrase},
        )
        db.commit()
    finally:
        db.close()
    raw = knowledge_env.db_path.read_bytes()
    assert claim_phrase.encode() not in raw
    assert source_phrase.encode() not in raw


def test_owner_isolation_applies_to_sources_records_search_and_citations(knowledge_env):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        alice_source = _source(db, alice, title="Alice private source")
        bob_source = _source(db, bob, title="Bob private source")
        alice_record = _record(
            db, alice, alice_source, statement="Alice private memory"
        )
        _record(db, bob, bob_source, statement="Bob secret memory")
        db.commit()

        with pytest.raises(LifeGraphNotFound):
            get_personal_knowledge_record(
                db, owner_id=bob.id, record_id=alice_record.id
            )
        with pytest.raises(LifeGraphNotFound):
            _record(
                db,
                alice,
                bob_source,
                title="Cross-owner citation rejected",
            )
        assert search_personal_knowledge_records(
            db, owner_id=alice.id, query_text="Bob secret"
        )["items"] == []
        assert get_knowledge_source(
            db, owner_id=alice.id, source_id=alice_source.id
        )["title"] == "Alice private source"
        with pytest.raises(LifeGraphNotFound):
            get_knowledge_source(db, owner_id=bob.id, source_id=alice_source.id)
    finally:
        db.close()


def test_existing_record_kinds_are_owner_validated_citation_targets(knowledge_env):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        note = _note(db, alice)
        document = _document(db, alice)
        project = Project(
            id="project-alice",
            owner=alice.username,
            key="PKNOW",
            name="Knowledge project",
            description="",
            template="general",
            color="#5b8abf",
        )
        db.add(project)
        entity_targets = {}
        for entity_type in ("file", "decision", "task"):
            entity, _ = create_life_entity(
                db,
                account=alice,
                entity_type=entity_type,
                title=f"Owned {entity_type}",
                properties={"scope": "citation target"},
            )
            entity_targets[entity_type] = entity
        base_memory = _record(
            db, alice, source, title="Prior memory", statement="Prior evidence"
        )
        citations = [
            _citation(source),
            {"target_kind": "note", "target_id": note.id, "locator": "note body"},
            {"target_kind": "document", "target_id": document.id, "locator": "section 1"},
            {"target_kind": "project", "target_id": project.id, "locator": "project record"},
            {"target_kind": "memory", "target_id": base_memory.id, "locator": "prior claim"},
        ] + [
            {"target_kind": kind, "target_id": entity.id, "locator": f"{kind} record"}
            for kind, entity in entity_targets.items()
        ]
        record = _record(
            db, alice, source, title="Linked claim", citations=citations
        )
        db.commit()

        serialized = get_personal_knowledge_record(
            db, owner_id=alice.id, record_id=record.id
        )
        assert {row["target_kind"] for row in serialized["citations"]} == {
            "life_source", "note", "document", "project", "memory",
            "file", "decision", "task",
        }
        assert all(row["available"] for row in serialized["citations"])
    finally:
        db.close()


def test_tombstoned_memory_and_task_links_degrade_without_breaking_read(knowledge_env):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        cited_memory = _record(
            db, alice, source, title="Cited memory", statement="Old context"
        )
        task, _ = create_life_entity(
            db, account=alice, entity_type="task", title="Cited task"
        )
        record = _record(
            db,
            alice,
            source,
            title="Surviving claim",
            statement="This still has direct source evidence",
            citations=[
                _citation(source),
                {"target_kind": "memory", "target_id": cited_memory.id, "locator": "old claim"},
                {"target_kind": "task", "target_id": task.id, "locator": "task record"},
            ],
        )
        delete_personal_knowledge_record(
            db,
            owner_id=alice.id,
            record_id=cited_memory.id,
            expected_version=1,
        )
        delete_life_entity(
            db, owner_id=alice.id, entity_id=task.id, expected_version=1
        )
        db.commit()

        serialized = get_personal_knowledge_record(
            db, owner_id=alice.id, record_id=record.id
        )
        by_kind = {row["target_kind"]: row for row in serialized["citations"]}
        assert by_kind["life_source"]["available"] is True
        assert by_kind["memory"]["available"] is False
        assert by_kind["memory"]["tombstoned"] is True
        assert by_kind["task"]["available"] is False
        assert by_kind["task"]["tombstoned"] is True
        evidence = citation_backed_answer_evidence(
            db,
            owner_id=alice.id,
            query_text="direct source evidence",
            as_of="2026-07-10T00:00:00Z",
        )
        assert evidence["evidence_count"] == 1
        assert len(evidence["evidence"][0]["unavailable_citations"]) == 2
    finally:
        db.close()


def test_cas_history_and_delete_are_canonical_and_reversible(knowledge_env):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        record = _record(db, alice, source)
        db.commit()

        updated = update_personal_knowledge_record(
            db,
            owner_id=alice.id,
            record_id=record.id,
            expected_version=1,
            changes={
                "statement": "Corrected citation-backed statement",
                "details": {"correction": "user reviewed"},
            },
            reason="Explicit correction",
        )
        assert updated.version == 2
        with pytest.raises(LifeGraphConflict):
            update_personal_knowledge_record(
                db,
                owner_id=alice.id,
                record_id=record.id,
                expected_version=1,
                changes={"statement": "Stale client overwrite"},
            )
        history, truncated = personal_knowledge_history(
            db, owner_id=alice.id, record_id=record.id
        )
        assert truncated is False
        assert [row["version"] for row in history] == [2, 1]
        deleted = delete_personal_knowledge_record(
            db,
            owner_id=alice.id,
            record_id=record.id,
            expected_version=2,
        )
        assert deleted.version == 3
        with pytest.raises(LifeGraphNotFound):
            get_personal_knowledge_record(
                db, owner_id=alice.id, record_id=record.id
            )
        tombstone = get_personal_knowledge_record(
            db,
            owner_id=alice.id,
            record_id=record.id,
            include_deleted=True,
        )
        assert tombstone["status"] == "deleted"
        history, _ = personal_knowledge_history(
            db, owner_id=alice.id, record_id=record.id
        )
        assert [row["version"] for row in history] == [3, 2, 1]
        assert db.query(LifeEntityVersion).filter_by(entity_id=record.id).count() == 3
    finally:
        db.close()


def test_model_inference_is_labelled_and_cannot_be_promoted_in_place(knowledge_env):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        inference = _record(
            db,
            alice,
            source,
            title="Tentative inferred pattern",
            statement="This may be a pattern",
            epistemic_status="inference",
            claim_origin="model",
            reviewed_at=None,
            inference={
                "generated_by": "model",
                "method": "citation comparison",
                "basis": "Two bounded cited observations",
                "model_label": "local-provider",
            },
        )
        db.commit()

        with pytest.raises(LifeGraphError, match="model-origin"):
            update_personal_knowledge_record(
                db,
                owner_id=alice.id,
                record_id=inference.id,
                expected_version=1,
                changes={
                    "epistemic_status": "confirmed_fact",
                    "reviewed_at": REVIEWED,
                },
            )
        db.refresh(inference)
        assert inference.version == 1
        assert inference.properties["epistemic_status"] == "inference"
        evidence = citation_backed_answer_evidence(
            db,
            owner_id=alice.id,
            query_text="pattern",
            as_of="2026-07-10T00:00:00+00:00",
        )
        assert evidence["evidence_count"] == 1
        assert evidence["evidence"][0]["warnings"] == [
            "explicit_inference_not_confirmed_authority"
        ]
        assert evidence["synthesizes_answer"] is False
    finally:
        db.close()


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"memory_kind": "episodic", "observed_at": None}, "observed_at"),
        ({"epistemic_status": "confirmed_fact", "reviewed_at": None}, "reviewed_at"),
        ({"epistemic_status": "stale", "stale_at": None}, "stale_at"),
        ({
            "epistemic_status": "assumption",
            "claim_origin": "user",
            "stale_after": "2026-07-01T00:00:00Z",
            "observed_at": None,
            "reviewed_at": None,
        }, "stale_after"),
        ({"stale_after": "2026-06-01T00:00:00Z"}, "stale_after"),
    ],
)
def test_observation_review_and_stale_dates_are_enforced(
    knowledge_env, overrides, message
):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        with pytest.raises(LifeGraphError, match=message):
            _record(db, alice, source, **overrides)
    finally:
        db.close()


def test_explicit_and_elapsed_staleness_is_deterministic_without_mutation(knowledge_env):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        elapsed = _record(
            db,
            alice,
            source,
            title="Review expired",
            stale_after="2026-07-05T00:00:00Z",
        )
        explicit = _record(
            db,
            alice,
            source,
            title="Explicitly stale",
            epistemic_status="stale",
            stale_at=STALE_AT,
        )
        db.commit()
        before = {
            row.id: (row.version, row.updated_at, dict(row.properties))
            for row in (elapsed, explicit)
        }

        report = list_stale_personal_knowledge(
            db, owner_id=alice.id, as_of="2026-07-10T05:30:00+05:30"
        )
        assert {item["record"]["id"] for item in report["items"]} == {
            elapsed.id, explicit.id,
        }
        assert {item["reason"] for item in report["items"]} == {
            "stale_after_elapsed", "explicitly_stale",
        }
        assert report["as_of"] == "2026-07-10T00:00:00Z"
        assert report["mutates_records"] is False
        for entity in (elapsed, explicit):
            db.refresh(entity)
            assert (entity.version, entity.updated_at, dict(entity.properties)) == before[entity.id]
    finally:
        db.close()


def test_explicit_gap_is_separated_from_answer_evidence(knowledge_env):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        _record(
            db,
            alice,
            source,
            title="Unknown application deadline",
            statement="What is the verified application deadline?",
            epistemic_status="gap",
            claim_origin="user",
            reviewed_at=None,
        )
        db.commit()
        report = citation_backed_answer_evidence(
            db,
            owner_id=alice.id,
            query_text="application deadline",
            as_of="2026-07-10T00:00:00Z",
        )
        assert report["evidence"] == []
        assert report["gaps"][0]["reason"] == "explicit_knowledge_gap"
        assert report["gaps"][0]["epistemic_status"] == "gap"
    finally:
        db.close()


def test_citations_are_required_direct_bounded_and_owner_available(knowledge_env):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        base = _record(db, alice, source, title="Base memory")
        with pytest.raises(LifeGraphError, match="at least one source link"):
            _record(db, alice, source, citations=[])
        with pytest.raises(LifeGraphError, match="directly"):
            _record(
                db,
                alice,
                source,
                citations=[{
                    "target_kind": "memory",
                    "target_id": base.id,
                    "locator": "prior memory",
                }],
            )
        with pytest.raises(LifeGraphError, match="locator"):
            _record(
                db,
                alice,
                source,
                citations=[{
                    "target_kind": "life_source",
                    "target_id": source.id,
                }],
            )
        with pytest.raises(LifeGraphNotFound):
            _record(
                db,
                alice,
                source,
                citations=[{
                    "target_kind": "life_source",
                    "target_id": "missing-source",
                    "locator": "missing",
                }],
            )
        with pytest.raises(LifeGraphError, match="30"):
            _record(
                db,
                alice,
                source,
                citations=[{
                    "target_kind": "life_source",
                    "target_id": source.id,
                    "locator": f"locator {index}",
                } for index in range(31)],
            )
    finally:
        db.close()


def test_bounded_search_and_list_are_deterministic(knowledge_env):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice, title="Search source alpha")
        for index in range(6):
            _record(
                db,
                alice,
                source,
                title=f"Controls alpha {index}",
                statement=f"Controls evidence alpha {index}",
                tags=["alpha", f"tag-{index}"],
            )
        db.commit()

        search = search_personal_knowledge_records(
            db, owner_id=alice.id, query_text="alpha", limit=2
        )
        assert len(search["items"]) == 2
        assert search["count"] == 2
        assert search["truncated"] is True
        assert search["scanned"] == 6
        rows, truncated = list_personal_knowledge_records(
            db, owner_id=alice.id, memory_kind="semantic", limit=3
        )
        assert len(rows) == 3
        assert truncated is True
        source_search = search_knowledge_sources(
            db, owner_id=alice.id, query_text="alpha", limit=1
        )
        assert len(source_search["items"]) == 1
        assert source_search["items"][0]["source"]["id"] == source.id
    finally:
        db.close()


def test_markdown_source_keeps_document_authority_and_rebuildable_index_contract(
    knowledge_env,
):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        markdown = _document(
            db,
            alice,
            title="Durable Markdown",
            language="markdown",
            content="# Principle\nKeep durable Markdown authoritative.",
        )
        source, _ = create_knowledge_source(
            db,
            account=alice,
            source_kind="markdown",
            title="Markdown knowledge source",
            observed_at=OBSERVED,
            descriptor={"target_kind": "document", "target_id": markdown.id},
        )
        db.commit()

        serialized = get_knowledge_source(
            db, owner_id=alice.id, source_id=source.id
        )
        assert serialized["descriptor"]["index_contract"] == MARKDOWN_INDEX_CONTRACT
        assert serialized["source_ref"] == f"document:{markdown.id}"
        manifest = markdown_index_rebuild_manifest(db, owner_id=alice.id)
        item = manifest["items"][0]
        assert item["rebuildable_now"] is True
        assert item["recorded_content_sha256"] == item["current_content_sha256"]
        assert "content" not in item
        assert manifest["authority"] == "durable_markdown_document_not_derived_index"
        assert manifest["writes_user_files"] is False

        markdown.current_content = "# Revised\nDurable authority changed."
        db.flush()
        changed = markdown_index_rebuild_manifest(db, owner_id=alice.id)["items"][0]
        assert changed["content_changed_since_capture"] is True
        assert changed["rebuildable_now"] is True
    finally:
        db.close()


def test_markdown_requires_owned_markdown_document_and_matching_digest(knowledge_env):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        bob = _account(db, "bob")
        text_doc = _document(db, alice, title="Plain text", language="text")
        bob_doc = _document(db, bob, title="Bob markdown", language="markdown")
        with pytest.raises(LifeGraphError, match="Markdown language"):
            create_knowledge_source(
                db,
                account=alice,
                source_kind="markdown",
                title="Invalid text source",
                observed_at=OBSERVED,
                descriptor={"target_kind": "document", "target_id": text_doc.id},
            )
        with pytest.raises(LifeGraphNotFound):
            create_knowledge_source(
                db,
                account=alice,
                source_kind="markdown",
                title="Cross owner",
                observed_at=OBSERVED,
                descriptor={"target_kind": "document", "target_id": bob_doc.id},
            )
        markdown = _document(db, alice, title="Real markdown", language="markdown")
        with pytest.raises(LifeGraphError, match="does not match"):
            create_knowledge_source(
                db,
                account=alice,
                source_kind="markdown",
                title="Wrong digest",
                observed_at=OBSERVED,
                descriptor={"target_kind": "document", "target_id": markdown.id},
                content_sha256="0" * 64,
            )
    finally:
        db.close()


@pytest.mark.parametrize(
    "unsafe_details",
    [
        {"password": "do-not-store"},
        {"nested": {"api_key": "do-not-store"}},
        {"executor": {"tool_call": "send"}},
        {"file_path": "/Users/alice/private.md"},
    ],
)
def test_unsafe_record_payloads_are_rejected(knowledge_env, unsafe_details):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        with pytest.raises(LifeGraphError):
            _record(db, alice, source, details=unsafe_details)
    finally:
        db.close()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"source_ref": "/Users/alice/private.md"}, "filesystem path"),
        ({"source_ref": "../private.md"}, "filesystem path"),
        ({"source_ref": "notes/private.md"}, "filesystem path"),
        ({"source_ref": "password:hunter2"}, "credentials or secrets"),
        ({"source_ref": "https://user:pass@example.test/page"}, "credentials"),
        ({"source_ref": "https://example.test/page?api_key=secret"}, "credential"),
    ],
)
def test_source_descriptors_reject_paths_and_credentials(
    knowledge_env, kwargs, message
):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        with pytest.raises(LifeGraphError, match=message):
            create_knowledge_source(
                db,
                account=alice,
                source_kind="research",
                title="Unsafe source",
                observed_at=OBSERVED,
                **kwargs,
            )
    finally:
        db.close()


@pytest.mark.parametrize(
    "statement",
    [
        "My password is hunter2",
        "See https://user:pass@example.test/private",
        "Use sk-proj-abcdefghijklmnop",
    ],
)
def test_free_text_credentials_are_rejected(knowledge_env, statement):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        with pytest.raises(LifeGraphError, match="credentials or secrets"):
            _record(db, alice, source, statement=statement)
    finally:
        db.close()


def test_contradictions_are_visible_but_never_count_as_support(knowledge_env):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        _record(
            db,
            alice,
            source,
            title="Disputed damping claim",
            statement="The damping claim is disputed.",
            citations=[_citation(source, relation="contradicts")],
        )
        supported = _record(
            db,
            alice,
            source,
            title="Mixed damping claim",
            statement="The damping claim has mixed evidence.",
            citations=[
                _citation(source, relation="supports", locator="support excerpt"),
                _citation(source, relation="contradicts", locator="conflict excerpt"),
            ],
        )
        db.commit()
        report = citation_backed_answer_evidence(
            db,
            owner_id=alice.id,
            query_text="damping claim",
            as_of="2026-07-17T00:00:00Z",
            limit=10,
        )
        assert any(
            row["reason"] == "no_available_supporting_direct_citation"
            for row in report["unsupported"]
        )
        admitted = next(
            row for row in report["evidence"] if row["record_id"] == supported.id
        )
        assert admitted["warnings"] == ["contradicting_evidence_present"]
        assert admitted["contradicting_citations"][0]["relation"] == "contradicts"
    finally:
        db.close()


def test_future_claims_and_sources_are_not_available_as_of(knowledge_env):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice, title="Future evidence")
        source.observed_at = datetime(2026, 7, 20, 9)
        _record(
            db,
            alice,
            source,
            title="Future reviewed claim",
            statement="Future damping evidence",
            observed_at=datetime(2026, 7, 20, 9, tzinfo=UTC),
            reviewed_at=datetime(2026, 7, 20, 10, tzinfo=UTC),
        )
        db.commit()
        report = citation_backed_answer_evidence(
            db,
            owner_id=alice.id,
            query_text="Future damping",
            as_of="2026-07-17T00:00:00Z",
        )
        assert report["evidence_count"] == 0
        assert report["evidence"] == []
    finally:
        db.close()


def test_malformed_canonical_records_and_sources_fail_closed(knowledge_env):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        source = _source(db, alice)
        record = _record(db, alice, source)
        db.commit()

        bad_properties = dict(record.properties)
        bad_properties["citations"] = [{"bogus": "row"}]
        record.properties = bad_properties
        db.commit()
        with pytest.raises(LifeGraphError, match="citation"):
            get_personal_knowledge_record(
                db, owner_id=alice.id, record_id=record.id
            )

        bad_metadata = dict(source.meta_data)
        bad_metadata["descriptor"] = {"description": "password:hunter2"}
        source.meta_data = bad_metadata
        db.commit()
        with pytest.raises(LifeGraphError, match="credentials or secrets"):
            get_knowledge_source(db, owner_id=alice.id, source_id=source.id)
    finally:
        db.close()


def test_queries_do_not_mutate_claims_sources_history_or_audit(knowledge_env):
    db = knowledge_env.Session()
    try:
        alice = _account(db, "alice")
        markdown = _document(db, alice, title="Query markdown", language="markdown")
        markdown_source, _ = create_knowledge_source(
            db,
            account=alice,
            source_kind="markdown",
            title="Query source",
            observed_at=OBSERVED,
            descriptor={"target_kind": "document", "target_id": markdown.id},
        )
        record = _record(
            db,
            alice,
            markdown_source,
            title="Query invariant",
            statement="Query invariant evidence",
            stale_after="2026-07-05T00:00:00Z",
        )
        db.commit()
        db.refresh(record)
        db.refresh(markdown_source)
        before = {
            "record": (record.version, record.updated_at, dict(record.properties)),
            "source": (markdown_source.version, markdown_source.updated_at),
            "history": db.query(LifeEntityVersion).count(),
            "audit": db.query(ActionAudit).count(),
        }

        get_personal_knowledge_record(
            db, owner_id=alice.id, record_id=record.id,
            as_of="2026-07-10T00:00:00Z",
        )
        list_personal_knowledge_records(db, owner_id=alice.id)
        search_personal_knowledge_records(
            db, owner_id=alice.id, query_text="invariant"
        )
        list_stale_personal_knowledge(
            db, owner_id=alice.id, as_of="2026-07-10T00:00:00Z"
        )
        citation_backed_answer_evidence(
            db,
            owner_id=alice.id,
            query_text="invariant",
            as_of="2026-07-10T00:00:00Z",
        )
        get_knowledge_source(db, owner_id=alice.id, source_id=markdown_source.id)
        list_knowledge_sources(db, owner_id=alice.id)
        search_knowledge_sources(db, owner_id=alice.id, query_text="query")
        markdown_index_rebuild_manifest(db, owner_id=alice.id)

        db.refresh(record)
        db.refresh(markdown_source)
        assert (record.version, record.updated_at, dict(record.properties)) == before["record"]
        assert (markdown_source.version, markdown_source.updated_at) == before["source"]
        assert db.query(LifeEntityVersion).count() == before["history"]
        assert db.query(ActionAudit).count() == before["audit"]
    finally:
        db.close()


def test_no_model_network_executor_or_filesystem_capability_exists():
    source = Path(__file__).resolve().parents[1] / "src" / "personal_knowledge_service.py"
    text = source.read_text(encoding="utf-8")
    assert "requests." not in text
    assert "httpx." not in text
    assert "open(" not in text
    assert "subprocess" not in text
    assert "llm_call" not in text
    assert "os.system" not in text
