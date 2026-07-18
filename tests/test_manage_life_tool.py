"""The main Restia agent gets one safe, read-only Life OS query surface."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import core.database as cdb
from core.database import Account, ActionAudit, LifeEntity, LifeEntityVersion
from src.decision_service import create_decision
from src.calendar_service import create_calendar_event
from src.finance_service import create_finance_record
from src.health_service import create_health_record
from src.habit_service import create_habit, create_habit_log
from src.journal_service import create_journal_entry
from src.home_service import create_home_record
from src.learning_career_service import create_learning_career_record
from src.identity import ensure_account
from src.life_graph import create_entity_link, create_life_entity
from src.life_graph import create_life_source
from src.relationship_service import create_relationship_profile
from src.travel_service import create_travel_record
from src.work_business_service import (
    create_work_business_record,
    create_work_business_workspace,
)
from tests.helpers.sqlite_db import make_temp_sqlite


_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)


@pytest.fixture(autouse=True)
def _bind_temp_db(monkeypatch):
    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    yield


def _owner(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


async def _call(owner: str | None, **payload):
    from src.tool_implementations import do_query_life

    return await do_query_life(json.dumps(payload), owner=owner)


def _seed_graph() -> tuple[str, str, str, str]:
    alice_name = _owner("life-alice")
    bob_name = _owner("life-bob")
    db = _TS()
    try:
        alice = ensure_account(db, alice_name)
        bob = ensure_account(db, bob_name)
        goal, _ = create_life_entity(
            db,
            account=alice,
            entity_type="goal",
            title="Ship V3",
            summary="Release the verified Life OS",
            provenance={"interface": "test"},
            confidence=96,
        )
        task, _ = create_life_entity(
            db,
            account=alice,
            entity_type="task",
            title="Alpha release checklist",
            summary="Verify the final gates",
            properties={"definition_of_done": "All release gates pass"},
            provenance={"interface": "test"},
            confidence=91,
            due_at=datetime.utcnow() - timedelta(hours=1),
        )
        create_entity_link(
            db,
            account=alice,
            source_id=task.id,
            relation="supports",
            target_id=goal.id,
            provenance={"interface": "test"},
        )
        create_life_entity(
            db,
            account=bob,
            entity_type="task",
            title="Alpha private Bob task",
            summary="Must never cross account scope",
            provenance={"interface": "test"},
        )
        db.commit()
        return alice_name, bob_name, task.id, goal.id
    finally:
        db.close()


async def test_empty_read_does_not_create_an_account_or_any_audit_row():
    owner = _owner("missing")
    result = await _call(owner, action="summary")
    assert result["exit_code"] == 0
    assert result["read_only"] is True
    assert result["counts"] == {}
    assert result["answer_contract"] == {
        "format": "compact_decision_support",
        "lead_with": "answer",
        "reasoning": "evidence_backed_rationale_not_hidden_chain_of_thought",
        "sources": "cite_source_entity_or_evidence_ids",
        "assumptions": "state_missing_stale_inferred_or_uncertain_data",
        "available_actions": "offer_only_supported_reviewed_actions",
    }

    db = _TS()
    try:
        assert db.query(Account).filter(Account.username == owner).count() == 0
    finally:
        db.close()


def test_main_agent_prompt_requires_compact_evidence_backed_decision_support():
    source = (Path(__file__).resolve().parents[1] / "src" / "agent_loop.py").read_text(
        encoding="utf-8"
    )

    for phrase in (
        "Lead with the direct answer",
        "evidence-backed rationale",
        "Sources",
        "Assumptions or uncertainty",
        "Available actions",
        "never expose or claim hidden chain-of-thought",
    ):
        assert phrase in source


async def test_summary_search_get_and_traverse_are_owner_scoped_and_sourced():
    alice, _bob, task_id, goal_id = _seed_graph()

    summary = await _call(alice, action="summary", limit=20)
    assert summary["exit_code"] == 0
    assert summary["counts"] == {"goal": 1, "task": 1}
    assert summary["task_attention"]["flag_counts"]["overdue"] == 1
    assert all("confidence" in row and "provenance" in row for row in summary["recent"])

    searched = await _call(alice, action="search", query="Alpha", limit=20)
    assert searched["exit_code"] == 0
    assert [row["entity"]["id"] for row in searched["items"]] == [task_id]
    assert "Bob" not in json.dumps(searched)

    fetched = await _call(alice, action="get", entity_id=task_id)
    assert fetched["entity"]["version"] == 1
    assert fetched["entity"]["provenance"] == {"interface": "test"}
    assert len(fetched["links"]) == 1
    assert fetched["links"][0]["target_id"] == goal_id

    traversed = await _call(alice, action="traverse", entity_id=task_id, depth=2)
    assert {row["id"] for row in traversed["entities"]} == {task_id, goal_id}
    assert len(traversed["links"]) == 1


async def test_due_decisions_and_health_trends_use_typed_domain_authority():
    owner = _owner("typed-life")
    db = _TS()
    try:
        account = ensure_account(db, owner)
        decision, _ = create_decision(
            db,
            account=account,
            title="Choose shared database boundary",
            decision_date=datetime.utcnow() - timedelta(days=5),
            context="Cross-interface login needs one immutable principal.",
            options=["Direct client tables", "Restia API plus PostgreSQL"],
            chosen_option="option_2",
            reasons=["Keeps authorization and audit server-side"],
            review_at=datetime.utcnow() - timedelta(minutes=1),
            provenance={"interface": "test"},
        )
        for days, value in ((2, 70.0), (1, 69.5)):
            create_health_record(
                db,
                account=account,
                record_type="weight",
                title="Weight observation",
                recorded_at=datetime.utcnow() - timedelta(days=days),
                metrics=[{"name": "weight", "value": value, "unit": "kg"}],
                details={},
                source={"kind": "manual", "label": "User entry"},
                provenance={"interface": "test"},
            )
        db.commit()
        decision_id = decision.id
    finally:
        db.close()

    due = await _call(owner, action="decisions_due")
    assert due["exit_code"] == 0
    assert [row["id"] for row in due["items"]] == [decision_id]
    assert "decision_review_due" in due["items"][0]["due_reasons"]

    trend = await _call(
        owner,
        action="health_trends",
        record_type="weight",
        metric="weight",
        group_by="day",
        unit="kg",
    )
    assert trend["exit_code"] == 0
    assert trend["count"] == 2
    assert [row["latest"] for row in trend["buckets"]] == [70.0, 69.5]
    assert "not diagnosis" in trend["medical_notice"]


async def test_finance_reads_use_typed_authority_and_never_expose_an_executor():
    owner = _owner("typed-finance")
    db = _TS()
    try:
        account = ensure_account(db, owner)
        common = {
            "account": account,
            "scope": "personal",
            "source": {"kind": "manual", "label": "User entry"},
            "currency": "INR",
            "provenance": {"interface": "test"},
        }
        create_finance_record(
            db,
            **common,
            record_type="income",
            title="July income",
            effective_at=datetime(2026, 7, 1, 9),
            amount="1000",
            details={"category": "salary", "counterparty": "Employer"},
        )
        create_finance_record(
            db,
            **common,
            record_type="expense",
            title="Travel expense",
            effective_at=datetime(2026, 7, 2, 9),
            amount="250",
            details={"category": "travel", "merchant": "Bus operator"},
        )
        create_finance_record(
            db,
            **common,
            record_type="balance_observation",
            title="Daily account",
            effective_at=datetime(2026, 7, 16, 9),
            amount="5000",
            details={"balance_kind": "account"},
        )
        create_finance_record(
            db,
            **common,
            record_type="subscription",
            title="Cloud subscription",
            effective_at=datetime(2026, 7, 1, 9),
            due_at=datetime.utcnow() + timedelta(days=3),
            amount="100",
            details={"provider": "Cloud service", "cadence": "monthly"},
        )
        db.commit()
    finally:
        db.close()

    summary = await _call(owner, action="finance_summary")
    assert summary["exit_code"] == 0
    assert summary["cash_flow"] == {
        "income": {"INR": "1000"},
        "expense": {"INR": "250"},
        "net": {"INR": "750"},
    }

    cash_flow = await _call(
        owner,
        action="finance_cash_flow",
        currency="INR",
        group_by="month",
        from_at="2026-07-01T00:00:00",
        to_at="2026-07-31T23:59:59",
    )
    assert cash_flow["buckets"][0]["net"] == "750"
    assert cash_flow["read_only"] is True

    subscriptions = await _call(owner, action="finance_subscriptions")
    assert subscriptions["count"] == 1
    assert subscriptions["items"][0]["execution_policy"] == {
        "risk_level": 6,
        "record_only": True,
        "can_execute_financial_action": False,
    }

    due = await _call(
        owner,
        action="finance_due",
        due_before=(datetime.utcnow() + timedelta(days=7)).isoformat(),
    )
    assert [row["title"] for row in due["items"]] == ["Cloud subscription"]

    signals = await _call(owner, action="finance_anomalies")
    assert signals["read_only"] is True
    assert "not financial" in signals["analysis_notice"].lower()

    as_of = "2026-07-17T12:00:00+05:30"
    worth = await _call(owner, action="finance_net_worth", as_of=as_of)
    assert worth["totals"]["INR"]["net_worth"] == "5000"
    assert worth["exchange_rates_used"] is False
    assert worth["read_only"] is True

    forecast = await _call(
        owner, action="finance_forecast", as_of=as_of,
        currency="INR", horizon_days=30, lookback_days=30,
    )
    assert forecast["read_only"] is True
    assert forecast["projections"]["INR"]["projected_net_change"] == "650.00"

    affordability = await _call(
        owner, action="finance_affordability", as_of=as_of,
        amount="5200", currency="INR", horizon_days=30, lookback_days=30,
    )
    assert affordability["status"] == "supported_by_recorded_inputs"
    assert affordability["can_execute_purchase_or_transfer"] is False
    assert affordability["read_only"] is True


async def test_calendar_time_query_is_owner_scoped_and_never_mutates():
    owner = _owner("calendar-time")
    other = _owner("calendar-time-other")
    db = _TS()
    try:
        account = ensure_account(db, owner)
        other_account = ensure_account(db, other)
        event = create_calendar_event(
            db, account=account, summary="Protected focus", event_type="focus",
            dtstart="2026-07-17T09:00:00+05:30",
            dtend="2026-07-17T11:00:00+05:30",
        )
        create_calendar_event(
            db, account=other_account, summary="Other owner private meeting",
            event_type="meeting", dtstart="2026-07-17T09:30:00+05:30",
            dtend="2026-07-17T12:00:00+05:30",
        )
        db.commit()
        version = event.event.version
        event_uid = event.event.uid
        owner_id = account.id
    finally:
        db.close()

    result = await _call(
        owner, action="calendar_time",
        as_of="2026-07-17T08:00:00+05:30",
        from_at="2026-07-17T06:00:00+05:30",
        to_at="2026-07-18T00:00:00+05:30",
    )
    assert result["exit_code"] == 0
    assert result["event_count"] == 1
    assert result["events"][0]["uid"] == event_uid
    assert result["read_only"] is True
    assert result["can_reschedule_or_create"] is False
    assert "Other owner" not in str(result)

    db = _TS()
    try:
        stored = db.query(cdb.CalendarEvent).filter_by(
            uid=event_uid, owner_id=owner_id,
        ).one()
        assert stored.version == version
    finally:
        db.close()


async def test_habit_reports_use_timezone_aware_typed_evidence_and_never_apply():
    owner = _owner("typed-habit")
    db = _TS()
    try:
        account = ensure_account(db, owner)
        habit, _ = create_habit(
            db,
            account=account,
            title="Morning reset",
            routine_type="morning",
            schedule={
                "cadence": "daily",
                "start_date": "2026-07-13",
                "timezone": "Asia/Kolkata",
                "time_of_day": "07:00",
                "grace_minutes": 30,
            },
            duration_minutes=10,
            checklist=[{"id": "water", "label": "Drink water", "required": True}],
            provenance={"interface": "test"},
        )
        create_habit_log(
            db,
            account=account,
            habit_id=habit.id,
            result="completed",
            logged_at="2026-07-13T02:00:00Z",
            scheduled_for="2026-07-13",
            quality=80,
            friction=10,
            duration_minutes=10,
            checklist_evidence=[{"item_id": "water", "status": "completed"}],
            provenance={"interface": "test"},
        )
        db.commit()
        habit_id = habit.id
    finally:
        db.close()

    weekly = await _call(
        owner, action="habit_weekly", week_start="2026-07-13", habit_id=habit_id
    )
    assert weekly["exit_code"] == 0
    assert weekly["items"][0]["completed_count"] == 1
    assert weekly["items"][0]["expected_count"] == 7
    assert weekly["read_only"] is True

    missed = await _call(
        owner,
        action="habits_missed",
        habit_id=habit_id,
        as_of="2026-07-15T08:00:00+05:30",
        lookback_days=3,
    )
    assert missed["count"] == 2
    assert missed["time_basis"].startswith("aware as_of")

    adjustments = await _call(
        owner,
        action="habit_adjustments",
        week_start="2026-07-13",
        habit_id=habit_id,
    )
    assert adjustments["execution_policy"] == {
        "record_only": True,
        "can_apply_automatically": False,
    }
    assert adjustments["read_only"] is True

    invalid = await _call(owner, action="habits_missed", as_of="2026-07-15T08:00:00")
    assert invalid["exit_code"] == 1
    assert "UTC offset" in invalid["error"]


async def test_relationship_reads_require_explicit_source_backing_and_never_send():
    owner = _owner("typed-relationship")
    db = _TS()
    try:
        account = ensure_account(db, owner)
        source, _ = create_life_source(
            db,
            account=account,
            source_type="manual_note",
            title="Relationship check-in note",
            idempotency_key="relationship-source",
        )
        profile, _ = create_relationship_profile(
            db,
            account=account,
            title="Alex Rivera",
            subject_kind="person",
            relationship_type="friend",
            contact_origin={
                "kind": "manual",
                "label": "User statement",
                "source_id": source.id,
                "observed_at": "2026-07-01T08:00:00Z",
            },
            care_plan={
                "interval_days": 30,
                "next_due_at": "2026-07-20T09:00:00Z",
                "source_id": source.id,
            },
            provenance={"source_id": source.id, "capture": "manual"},
        )
        db.commit()
        profile_id = profile.id
    finally:
        db.close()

    profiles = await _call(owner, action="relationship_profiles")
    assert [row["id"] for row in profiles["items"]] == [profile_id]
    assert profiles["items"][0]["execution_policy"] == {
        "record_only": True,
        "can_send_personal_message": False,
        "future_personal_message_min_autonomy": 5,
        "future_personal_message_requires_confirmation": True,
    }

    reminders = await _call(
        owner,
        action="relationship_reminders",
        as_of="2026-07-17T00:00:00Z",
        due_before="2026-07-31T23:59:59Z",
    )
    assert reminders["count"] == 1
    assert reminders["items"][0]["source_id"]
    assert reminders["items"][0]["source_backed"] is True
    assert reminders["execution_policy"]["can_send_personal_message"] is False
    assert reminders["read_only"] is True


async def test_journal_reads_and_period_review_are_private_and_deterministic():
    owner = _owner("typed-journal")
    db = _TS()
    try:
        account = ensure_account(db, owner)
        entry, _ = create_journal_entry(
            db,
            account=account,
            title="Weekly reflection",
            entry_date="2026-07-17",
            body="Private evidence, not a model interpretation.",
            mood={"label": "focused", "score": 8, "energy": 7},
            wins=["Finished the controller model"],
            difficulties=["Context switching"],
            lessons=["Protect deep work"],
            gratitude=["Useful feedback"],
            principles=["Evidence before confidence"],
            time_notes=["Two hours of deep work"],
            relationship_notes=["Followed up with recommender"],
            goal_progress=["Application essay reached first draft"],
            next_changes=["Move phone outside the room"],
            provenance={"capture": "manual"},
        )
        db.commit()
        entry_id = entry.id
    finally:
        db.close()

    listed = await _call(owner, action="journal_entries")
    assert [row["id"] for row in listed["items"]] == [entry_id]
    searched = await _call(owner, action="journal_search", query="controller")
    assert searched["items"][0]["entry"]["id"] == entry_id
    fetched = await _call(owner, action="journal_get", entity_id=entry_id)
    assert fetched["entry"]["body"].startswith("Private evidence")
    review = await _call(
        owner,
        action="journal_review",
        period="weekly",
        anchor_date="2026-07-17",
    )
    assert review["uses_model_inference"] is False
    assert review["principles"][0]["text"] == "Evidence before confidence"
    assert review["time"][0]["text"] == "Two hours of deep work"
    assert review["relationships"][0]["occurrences"] == 1
    assert review["goal_progress"][0]["occurrences"] == 1
    assert review["next_changes"][0]["occurrences"] == 1
    assert review["read_only"] is True


async def test_home_records_and_expiry_alerts_are_explicit_and_record_only():
    owner = _owner("typed-home")
    db = _TS()
    try:
        account = ensure_account(db, owner)
        record, _ = create_home_record(
            db,
            account=account,
            record_type="renewal",
            title="Professional membership renewal",
            effective_at="2026-07-01T08:00:00Z",
            due_at="2026-08-01T09:00:00Z",
            source={"kind": "manual", "label": "User entry"},
            details={
                "renewal_kind": "membership",
                "provider_name": "Association",
                "cadence": "annual",
            },
            provenance={"capture": "manual"},
        )
        db.commit()
        record_id = record.id
    finally:
        db.close()

    listed = await _call(owner, action="home_records", record_type="renewal")
    assert [row["id"] for row in listed["items"]] == [record_id]
    searched = await _call(owner, action="home_search", query="membership")
    assert searched["items"][0]["record"]["id"] == record_id
    alerts = await _call(
        owner,
        action="home_alerts",
        as_of="2026-07-17T00:00:00Z",
        horizon_days=30,
    )
    assert alerts["count"] == 1
    assert alerts["items"][0]["record_id"] == record_id
    assert alerts["items"][0]["deadline_kind"] == "due"
    assert "does not renew" in alerts["record_only_notice"]
    assert alerts["read_only"] is True


async def test_travel_reads_and_mode_are_bounded_offline_and_non_executing():
    owner = _owner("typed-travel")
    db = _TS()
    try:
        account = ensure_account(db, owner)
        trip, _ = create_travel_record(
            db,
            account=account,
            record_kind="trip",
            title="Tokyo controls workshop",
            details={
                "destination": "Tokyo",
                "trip_timezone": "Asia/Tokyo",
                "purpose": "Workshop",
            },
            starts_at="2026-07-20T09:00:00+09:00",
            ends_at="2026-07-24T18:00:00+09:00",
            offline_available=True,
            provenance={"capture": "manual"},
        )
        create_travel_record(
            db,
            account=account,
            record_kind="document",
            trip_id=trip.id,
            title="Travel insurance",
            details={
                "document_kind": "insurance",
                "storage_ref": "vault://travel/insurance.pdf",
            },
            offline_available=True,
            provenance={"capture": "manual"},
        )
        db.commit()
        trip_id = trip.id
    finally:
        db.close()

    records = await _call(owner, action="travel_records", record_kind="trip")
    assert [row["id"] for row in records["items"]] == [trip_id]
    assert records["items"][0]["execution_policy"]["can_book"] is False
    assert records["read_only"] is True

    mode = await _call(
        owner,
        action="travel_mode",
        as_of="2026-07-21T12:00:00+09:00",
        offline_only=True,
        fact_limit=10,
    )
    assert [row["id"] for row in mode["current_trips"]] == [trip_id]
    assert mode["document_availability"] == {
        "total": 1,
        "available_offline": 1,
        "unavailable_offline": 0,
    }
    assert mode["execution_policy"]["uses_network"] is False
    assert mode["execution_policy"]["can_purchase"] is False
    assert mode["read_only"] is True


async def test_learning_career_reads_expose_source_backed_weekly_chain_only():
    owner = _owner("typed-learning-career")
    db = _TS()
    try:
        account = ensure_account(db, owner)
        source, _ = create_life_source(
            db, account=account, source_type="manual", title="Career evidence"
        )

        def create(domain, kind, title, *, links=None, weekly=None):
            return create_learning_career_record(
                db,
                account=account,
                domain=domain,
                record_kind=kind,
                title=title,
                details={},
                source_links=[{
                    "source_id": source.id,
                    "relation": "supports",
                    "label": "Explicit evidence",
                }],
                entity_links=links or [],
                weekly_action=weekly,
                provenance={"capture": "manual"},
            )[0]

        practice = create(
            "learning", "practice", "Tune one controller",
            weekly={
                "week_start": "2026-07-20",
                "definition_of_done": "Measured response and reflection",
                "estimated_minutes": 120,
                "priority": "high",
            },
        )
        portfolio = create(
            "career", "portfolio", "Controls portfolio",
            links=[{"entity_id": practice.id, "relation": "weekly_action"}],
        )
        objective = create(
            "learning", "learning_objective", "Master feedback control",
            links=[{"entity_id": portfolio.id, "relation": "evidenced_by"}],
        )
        gap = create(
            "career", "gap", "Experimental controls gap",
            links=[{"entity_id": objective.id, "relation": "addressed_by"}],
        )
        skill = create(
            "learning", "skill", "Feedback control",
            links=[{"entity_id": gap.id, "relation": "has_gap"}],
        )
        role = create(
            "career", "role", "Controls research role",
            links=[{"entity_id": skill.id, "relation": "requires_capability"}],
        )
        db.commit()
        role_id = role.id
    finally:
        db.close()

    records = await _call(
        owner, action="learning_career_records", domain="career", limit=20
    )
    assert records["count"] == 3
    assert all(row["domain"] == "career" for row in records["items"])
    searched = await _call(
        owner, action="learning_career_search", query="feedback", domain="learning"
    )
    assert searched["items"][0]["record"]["record_kind"] == "skill"

    plan = await _call(
        owner,
        action="career_learning_plan",
        career_target_id=role_id,
        week_start="2026-07-20",
    )
    assert plan["complete_chain_count"] == 1
    assert plan["coverage"]["all_records_source_backed"] is True
    assert plan["uses_model_inference"] is False
    assert plan["execution_policy"]["can_apply_or_submit"] is False
    assert plan["read_only"] is True


async def test_work_business_reads_stay_inside_exact_workspace_and_never_execute():
    owner = _owner("typed-work-business")
    db = _TS()
    try:
        account = ensure_account(db, owner)
        source, _ = create_life_source(
            db, account=account, source_type="manual", title="Workspace evidence"
        )
        source_links = [{
            "source_id": source.id,
            "relation": "supports",
            "label": "Explicit evidence",
        }]
        work, _ = create_work_business_workspace(
            db,
            account=account,
            workspace_kind="work",
            title="Research work",
            purpose="Keep research commitments isolated",
            details={"operating_note": "Measured work only"},
            source_links=source_links,
            provenance={"capture": "manual"},
        )
        business, _ = create_work_business_workspace(
            db,
            account=account,
            workspace_kind="business",
            title="Business experiments",
            purpose="Keep commercial records isolated",
            details={},
            source_links=source_links,
            provenance={"capture": "manual"},
        )
        create_work_business_record(
            db,
            account=account,
            workspace_id=work.id,
            record_kind="project",
            title="Controller experiment",
            summary="Private research plan",
            details={"label": "experiment"},
            source_links=source_links,
            provenance={"capture": "manual"},
        )
        create_work_business_record(
            db,
            account=account,
            workspace_id=business.id,
            record_kind="revenue",
            title="Pilot revenue",
            summary="Private commercial observation",
            details={"label": "pilot"},
            source_links=source_links,
            provenance={"capture": "manual"},
        )
        db.commit()
        work_id = work.id
    finally:
        db.close()

    workspaces = await _call(
        owner, action="work_business_workspaces", workspace_kind="work"
    )
    assert [row["id"] for row in workspaces["items"]] == [work_id]

    records = await _call(
        owner, action="work_business_records", workspace_id=work_id
    )
    assert [row["record_kind"] for row in records["items"]] == ["project"]
    assert "revenue" not in json.dumps(records)

    searched = await _call(
        owner,
        action="work_business_search",
        workspace_id=work_id,
        query="controller",
    )
    assert searched["items"][0]["record"]["record_kind"] == "project"

    summary = await _call(
        owner, action="work_business_summary", workspace_id=work_id
    )
    assert summary["totals"]["records"] == 1
    assert summary["by_kind"]["revenue"] == 0
    assert summary["uses_model_inference"] is False
    assert summary["execution_policy"] == {
        "record_only": True,
        "can_send_outreach": False,
        "can_send_messages": False,
        "can_submit_proposals": False,
        "can_make_payments": False,
    }
    assert summary["read_only"] is True


async def test_all_actions_are_read_only_and_invalid_calls_fail_closed():
    owner, _bob, task_id, _goal_id = _seed_graph()
    db = _TS()
    try:
        before = {
            "entities": db.query(LifeEntity).count(),
            "versions": db.query(LifeEntityVersion).count(),
            "audits": db.query(ActionAudit).count(),
        }
    finally:
        db.close()

    for payload in (
        {"action": "list"},
        {"action": "get", "entity_id": task_id},
        {"action": "task_quality"},
    ):
        result = await _call(owner, **payload)
        assert result["exit_code"] == 0
        assert result["read_only"] is True

    unsupported = await _call(owner, action="create", title="Must not exist")
    assert unsupported["exit_code"] == 1
    assert "Unsupported action" in unsupported["error"]
    missing_owner = await _call(None, action="summary")
    assert missing_owner["exit_code"] == 1

    db = _TS()
    try:
        after = {
            "entities": db.query(LifeEntity).count(),
            "versions": db.query(LifeEntityVersion).count(),
            "audits": db.query(ActionAudit).count(),
        }
        assert after == before
    finally:
        db.close()


async def test_automation_queries_are_owner_scoped_versioned_and_never_prepare():
    from src.life_automation import (
        create_automation_definition,
        update_automation_definition,
    )

    owner = _owner("life-automation-alice")
    bob_owner = _owner("life-automation-bob")
    db = _TS()
    try:
        alice = ensure_account(db, owner)
        bob = ensure_account(db, bob_owner)
        definition, _ = create_automation_definition(
            db,
            account=alice,
            name="Morning review",
            trigger={"type": "time", "config": {"schedule_key": "morning"}},
            actions=[{
                "type": "notification",
                "config": {
                    "channel": "web",
                    "title": "Review required",
                    "message": "A prepared action is waiting for review.",
                },
            }],
            idempotency_key="query-life-automation-alice",
        )
        updated = update_automation_definition(
            db,
            owner_id=alice.id,
            automation_id=definition.id,
            expected_version=1,
            changes={"description": "Owner-scoped read-only inspection"},
        )
        create_automation_definition(
            db,
            account=bob,
            name="Bob private automation",
            trigger={"type": "time", "config": {"schedule_key": "morning"}},
            actions=[{
                "type": "briefing",
                "config": {"title": "Bob only", "sections": ["private"]},
            }],
            idempotency_key="query-life-automation-bob",
        )
        db.commit()
        automation_id = updated.id
        before = {
            "entities": db.query(LifeEntity).count(),
            "versions": db.query(LifeEntityVersion).count(),
            "audits": db.query(ActionAudit).count(),
            "proposals": db.query(cdb.ActionProposal).count(),
        }
    finally:
        db.close()

    listed = await _call(owner, action="automations", enabled=True, limit=20)
    assert listed["exit_code"] == 0
    assert listed["read_only"] is True
    assert [item["id"] for item in listed["items"]] == [automation_id]
    assert "Bob" not in json.dumps(listed)

    fetched = await _call(
        owner, action="automation_get", automation_id=automation_id
    )
    assert fetched["automation"]["version"] == 2
    assert fetched["automation"]["description"] == (
        "Owner-scoped read-only inspection"
    )

    history = await _call(
        owner, action="automation_history", automation_id=automation_id
    )
    assert [item["version"] for item in history["items"]] == [2, 1]

    evaluated = await _call(
        owner,
        action="automation_evaluate",
        automation_id=automation_id,
        event={"type": "time", "schedule_key": "morning"},
    )
    assert evaluated["evaluation"]["matched"] is True
    assert [plan["type"] for plan in evaluated["evaluation"]["plans"]] == [
        "notification"
    ]
    assert evaluated["prepared"] is False
    assert evaluated["executed"] is False
    assert evaluated["sent"] is False
    assert evaluated["read_only"] is True

    hidden = await _call(
        bob_owner, action="automation_get", automation_id=automation_id
    )
    assert hidden["exit_code"] == 1
    assert "not found" in hidden["error"].lower()

    db = _TS()
    try:
        after = {
            "entities": db.query(LifeEntity).count(),
            "versions": db.query(LifeEntityVersion).count(),
            "audits": db.query(ActionAudit).count(),
            "proposals": db.query(cdb.ActionProposal).count(),
        }
        assert after == before
    finally:
        db.close()


def test_schema_registry_dispatch_and_plan_mode_all_classify_query_life():
    from src.agent_tools import FUNCTION_TOOL_SCHEMAS, TOOL_TAGS
    from src.tool_index import ASSISTANT_ALWAYS_AVAILABLE, BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_security import PLAN_MODE_READONLY_TOOLS, _PLAN_MODE_KNOWN_MUTATORS

    schemas = {
        row["function"]["name"]: row["function"] for row in FUNCTION_TOOL_SCHEMAS
    }
    assert "query_life" in schemas
    assert schemas["query_life"]["parameters"]["required"] == ["action"]
    query_parameters = schemas["query_life"]["parameters"]
    assert {
        "automation_definitions", "automation_get", "automation_history",
        "automation_evaluate",
    } <= set(query_parameters["properties"]["action"]["enum"])
    assert "today" in query_parameters["properties"]["action"]["enum"]
    assert query_parameters["properties"]["utc_offset_minutes"] == {
        "type": "integer",
        "minimum": -840,
        "maximum": 840,
        "description": (
            "Required for action=today. Signed minutes local time is ahead of "
            "UTC, for example 330 for India."
        ),
    }
    assert "automation_prepare" not in query_parameters["properties"]["action"]["enum"]
    assert query_parameters["properties"]["event"]["type"] == "object"
    assert "query_life" in TOOL_TAGS
    assert "query_life" in BUILTIN_TOOL_DESCRIPTIONS
    assert "query_life" in ASSISTANT_ALWAYS_AVAILABLE
    assert "query_life" in PLAN_MODE_READONLY_TOOLS
    assert "query_life" not in _PLAN_MODE_KNOWN_MUTATORS

    source = open("src/tool_execution.py", encoding="utf-8").read()
    assert 'tool == "query_life"' in source
    assert "do_query_life(content, owner=owner)" in source
    life_source = open("src/tools/life.py", encoding="utf-8").read()
    assert "prepare_automation_run" not in life_source
    assert "create_action_proposal" not in life_source
    assert "build_owner_today_snapshot" in life_source

    agent_prompt = open("src/agent_loop.py", encoding="utf-8").read()
    assert 'use `today` with the user\'s exact `utc_offset_minutes`' in agent_prompt
    assert "same source-backed control-plane answer as the Today screen" in agent_prompt
