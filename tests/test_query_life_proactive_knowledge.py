"""End-to-end read-only contracts for proactive and personal-knowledge queries."""

from __future__ import annotations

import json
import uuid

import pytest

import core.database as cdb
from core.database import ActionAudit, LifeEntity, LifeEntityVersion
from src.identity import ensure_account
from src.personal_knowledge_service import (
    create_knowledge_source,
    create_personal_knowledge_record,
)
from src.task_record_service import create_task_record
from tests.helpers.sqlite_db import make_temp_sqlite


_Session, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)


@pytest.fixture(autouse=True)
def _bind_query_database(monkeypatch):
    monkeypatch.setattr(cdb, "SessionLocal", _Session)
    yield


async def _query(owner: str, **payload):
    from src.tool_implementations import do_query_life

    return await do_query_life(json.dumps(payload), owner=owner)


def _username(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _seed() -> tuple[str, str, str]:
    alice_name = _username("knowledge-alice")
    bob_name = _username("knowledge-bob")
    db = _Session()
    try:
        alice = ensure_account(db, alice_name)
        ensure_account(db, bob_name)
        source, _ = create_knowledge_source(
            db,
            account=alice,
            source_kind="lesson",
            title="Reviewed controls lesson",
            observed_at="2026-07-17T09:00:00+00:00",
            source_ref="lesson:query-life-controls",
            safe_excerpt="The bounded review explicitly discusses damping.",
        )
        record, _ = create_personal_knowledge_record(
            db,
            account=alice,
            memory_kind="semantic",
            title="Damping review",
            statement="The test plant requires a citation-backed damping review.",
            epistemic_status="confirmed_fact",
            claim_origin="source",
            reviewed_at="2026-07-17T10:00:00+00:00",
            stale_after="2026-08-17T10:00:00+00:00",
            citations=[{
                "target_kind": "life_source",
                "target_id": source.id,
                "relation": "supports",
                "locator": "bounded excerpt",
            }],
        )
        create_task_record(
            db,
            account=alice,
            title="Submit the verified application",
            definition_of_done="The application has a stored submission receipt.",
            effort_minutes=90,
            priority="critical",
            deadline="2026-07-18T12:00:00+00:00",
            energy="high",
            contexts=["desk"],
            people_ids=[],
            dependency_ids=[],
            document_ids=[],
            source={"kind": "manual", "label": "Query Life integration test"},
            status="active",
            next_action="Review and submit the final form",
            provenance={"interface": "test"},
        )
        db.commit()
        return alice_name, bob_name, record.id
    finally:
        db.close()


@pytest.mark.asyncio
async def test_typed_knowledge_queries_preserve_evidence_and_generic_reads_hide_it():
    alice, bob, record_id = _seed()

    listed = await _query(
        alice,
        action="knowledge_records",
        as_of="2026-07-18T00:00:00+00:00",
    )
    assert listed["exit_code"] == 0
    assert [item["id"] for item in listed["items"]] == [record_id]
    assert listed["items"][0]["epistemic_status"] == "confirmed_fact"
    assert listed["items"][0]["citations"][0]["available"] is True

    evidence = await _query(
        alice,
        action="knowledge_evidence",
        query="damping",
        as_of="2026-07-18T00:00:00+00:00",
    )
    assert evidence["exit_code"] == 0
    assert evidence["synthesizes_answer"] is False
    assert evidence["evidence"][0]["record_id"] == record_id

    generic_list = await _query(alice, action="list", entity_type="source")
    generic_search = await _query(alice, action="search", query="damping")
    generic_get = await _query(alice, action="get", entity_id=record_id)
    assert record_id not in {item["id"] for item in generic_list["items"]}
    assert record_id not in {item["id"] for item in generic_search["items"]}
    assert generic_get["exit_code"] != 0
    assert "knowledge" in generic_get["error"].lower()

    bob_result = await _query(
        bob,
        action="knowledge_search",
        query="damping",
        as_of="2026-07-18T00:00:00+00:00",
    )
    assert bob_result["items"] == []


@pytest.mark.asyncio
async def test_proactive_query_is_deterministic_owner_scoped_and_read_only():
    alice, bob, _record_id = _seed()
    db = _Session()
    try:
        before = {
            "entities": db.query(LifeEntity).count(),
            "versions": db.query(LifeEntityVersion).count(),
            "audits": db.query(ActionAudit).count(),
        }
    finally:
        db.close()

    first = await _query(
        alice,
        action="proactive_report",
        as_of="2026-07-18T09:00:00+00:00",
    )
    second = await _query(
        alice,
        action="proactive_report",
        as_of="2026-07-18T09:00:00+00:00",
    )
    assert first == second
    assert first["exit_code"] == 0
    assert first["read_only"] is True
    assert first["interruptions"]
    assert all(item["routing"]["channel"] == "interrupt" for item in first["interruptions"])
    assert all(item["evidence"]["entities"] for item in first["items"])

    bob_result = await _query(
        bob,
        action="proactive_report",
        as_of="2026-07-18T09:00:00+00:00",
    )
    assert bob_result["items"] == []

    db = _Session()
    try:
        after = {
            "entities": db.query(LifeEntity).count(),
            "versions": db.query(LifeEntityVersion).count(),
            "audits": db.query(ActionAudit).count(),
        }
    finally:
        db.close()
    assert after == before


@pytest.mark.asyncio
async def test_proactive_requires_an_explicit_offset_aware_time_boundary():
    alice, _bob, _record_id = _seed()

    missing = await _query(alice, action="proactive_report")
    naive = await _query(
        alice, action="proactive_report", as_of="2026-07-18T09:00:00"
    )
    assert missing["exit_code"] != 0
    assert "explicit UTC offset" in missing["error"]
    assert naive["exit_code"] != 0
    assert "explicit UTC offset" in naive["error"]


def test_schema_and_agent_prompt_expose_the_safe_typed_contracts():
    from src.agent_loop import TOOL_SECTIONS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS

    schema = next(
        item["function"]
        for item in FUNCTION_TOOL_SCHEMAS
        if item["function"]["name"] == "query_life"
    )
    actions = set(schema["parameters"]["properties"]["action"]["enum"])
    assert {
        "proactive_report",
        "knowledge_records",
        "knowledge_search",
        "knowledge_stale",
        "knowledge_evidence",
        "knowledge_sources",
    } <= actions
    prompt = TOOL_SECTIONS["query_life"]
    assert "explicit offset-aware `as_of`" in prompt
    assert "never synthesizes" in prompt
    assert "Generic graph reads deliberately" in prompt
    assert "exclude typed personal-knowledge records" in prompt
