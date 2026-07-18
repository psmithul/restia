"""Focused contracts for the deterministic V3 proactive read model."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

import core.database as cdb
from core.database import ActionAudit, EntityLink, LifeEntity
from src.decision_service import create_decision
from src.finance_service import create_finance_record
from src.habit_service import create_habit, create_habit_log
from src.health_service import create_health_record
from src.home_service import create_home_record
from src.identity import ensure_account
from src.life_graph import (
    create_entity_link,
    create_life_entity,
    create_life_source,
)
from src.proactive_intelligence import (
    ProactiveInputError,
    ProactiveStateError,
    proactive_intelligence_report,
)
from src.relationship_service import (
    create_commitment,
    create_follow_up,
    create_relationship_profile,
)
from src.task_record_service import create_task_record, update_task_record
from tests.helpers.sqlite_db import make_temp_sqlite


_Session, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)


def _owner(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _account(db, prefix: str):
    account = ensure_account(db, _owner(prefix))
    db.flush()
    return account


def _task(
    db,
    account,
    *,
    title: str,
    deadline: object,
    priority: str = "normal",
    effort_minutes: int = 60,
    project_id: str | None = None,
):
    task, _ = create_task_record(
        db,
        account=account,
        title=title,
        definition_of_done=f"{title} has concrete completion evidence.",
        effort_minutes=effort_minutes,
        priority=priority,
        deadline=deadline,
        energy="any",
        contexts=["desk"],
        project_id=project_id,
        people_ids=[],
        dependency_ids=[],
        document_ids=[],
        source={"kind": "manual", "label": "Test planning review"},
        status="active",
        next_action=f"Start {title}",
        provenance={"interface": "focused-test"},
    )
    return task


def _signals(report: dict, kind: str) -> list[dict]:
    return [item for item in report["items"] if item["kind"] == kind]


def _signal_for(report: dict, kind: str, entity_id: str) -> dict:
    return next(
        item
        for item in report["items"]
        if item["kind"] == kind
        and entity_id in {row["id"] for row in item["evidence"]["entities"]}
    )


def test_explicit_offset_aware_as_of_controls_time_without_wall_clock():
    db = _Session()
    try:
        account = _account(db, "proactive-time")
        task = _task(
            db,
            account,
            title="Submit application",
            deadline="2026-07-20T12:00:00Z",
            priority="high",
        )
        db.commit()

        with pytest.raises(ProactiveInputError, match="explicit UTC offset"):
            proactive_intelligence_report(
                db, owner_id=account.id, as_of=datetime(2026, 7, 19, 12)
            )

        before = proactive_intelligence_report(
            db,
            owner_id=account.id,
            as_of="2026-07-19T12:00:00+00:00",
        )
        after = proactive_intelligence_report(
            db,
            owner_id=account.id,
            as_of="2026-07-21T12:00:00+00:00",
        )
        assert not any(
            item["kind"] == "overdue_task"
            and task.id in {row["id"] for row in item["evidence"]["entities"]}
            for item in before["items"]
        )
        assert _signal_for(after, "overdue_task", task.id)["calculation"][
            "as_of"
        ] == "2026-07-21T12:00:00Z"
        assert after["as_of"] == "2026-07-21T12:00:00Z"
        assert after["as_of_offset"] == "2026-07-21T12:00:00+00:00"
    finally:
        db.close()


def test_owner_isolation_deterministic_order_routing_and_no_mutations():
    db = _Session()
    try:
        alice = _account(db, "proactive-alice")
        bob = _account(db, "proactive-bob")
        high = _task(
            db,
            alice,
            title="High overdue",
            deadline="2026-07-15T12:00:00Z",
            priority="high",
        )
        normal = _task(
            db,
            alice,
            title="Normal overdue",
            deadline="2026-07-16T12:00:00Z",
            priority="normal",
        )
        bob_task = _task(
            db,
            bob,
            title="Bob critical private task",
            deadline="2026-07-14T12:00:00Z",
            priority="critical",
        )
        db.commit()
        audit_count = db.query(ActionAudit).count()

        first = proactive_intelligence_report(
            db,
            owner_id=alice.id,
            as_of="2026-07-17T12:00:00Z",
            limit=100,
        )
        second = proactive_intelligence_report(
            db,
            owner_id=alice.id,
            as_of="2026-07-17T12:00:00Z",
            limit=100,
        )
        assert first == second
        all_evidence_ids = {
            entity["id"]
            for item in first["items"]
            for entity in item["evidence"]["entities"]
        }
        assert high.id in all_evidence_ids
        assert normal.id in all_evidence_ids
        assert bob_task.id not in all_evidence_ids
        assert _signal_for(first, "overdue_task", high.id)["routing"][
            "channel"
        ] == "interrupt"
        normal_signal = _signal_for(first, "overdue_task", normal.id)
        assert normal_signal["routing"]["channel"] == "digest"
        assert normal_signal["routing"]["reasons"] == [
            "below_interruption_threshold"
        ]

        explicitly_requested = proactive_intelligence_report(
            db,
            owner_id=alice.id,
            as_of="2026-07-17T12:00:00Z",
            explicit_interrupts=["overdue_task"],
        )
        requested_normal = _signal_for(
            explicitly_requested, "overdue_task", normal.id
        )
        assert requested_normal["routing"]["channel"] == "interrupt"
        assert requested_normal["thresholds"]["explicitly_requested"] is True
        assert db.query(ActionAudit).count() == audit_count
        assert not db.new and not db.dirty and not db.deleted
        assert first["safety_policy"]["can_mutate"] is False
        assert first["safety_policy"]["can_send_or_notify"] is False
    finally:
        db.close()


def test_bounded_scans_surface_truncation_instead_of_claiming_completeness():
    db = _Session()
    try:
        account = _account(db, "proactive-bounded")
        for index in range(3):
            _task(
                db,
                account,
                title=f"Bounded task {index}",
                deadline=f"2026-07-{14 + index:02d}T12:00:00Z",
            )
        db.commit()
        audit_count = db.query(ActionAudit).count()

        report = proactive_intelligence_report(
            db,
            owner_id=account.id,
            as_of="2026-07-20T12:00:00Z",
            scan_limit=2,
        )
        assert report["scans"]["tasks"] == {"scanned": 2, "truncated": True}
        assert report["truncated"] is True
        assert report["truncation"]["scan_truncated"] is True
        assert db.query(ActionAudit).count() == audit_count
    finally:
        db.close()


def test_malformed_typed_state_and_cross_owner_links_fail_closed():
    db = _Session()
    try:
        alice = _account(db, "proactive-malformed")
        malformed, _ = create_life_entity(
            db,
            account=alice,
            entity_type="finance_record",
            title="Malformed finance",
            properties={"finance_schema_version": 1, "record_type": "bill"},
        )
        db.commit()
        with pytest.raises(ProactiveStateError, match=malformed.id):
            proactive_intelligence_report(
                db, owner_id=alice.id, as_of="2026-07-20T12:00:00Z"
            )
    finally:
        db.close()

    db = _Session()
    try:
        alice = _account(db, "proactive-link-alice")
        bob = _account(db, "proactive-link-bob")
        alice_task = _task(
            db,
            alice,
            title="Alice linked task",
            deadline="2026-07-19T12:00:00Z",
        )
        bob_task = _task(
            db,
            bob,
            title="Bob linked task",
            deadline="2026-07-19T12:00:00Z",
        )
        db.add(EntityLink(
            id=str(uuid.uuid4()),
            owner_id=alice.id,
            source_type="life_entity",
            source_id=alice_task.id,
            relation="conflicts_with",
            target_type="life_entity",
            target_id=bob_task.id,
            meta_data={},
            provenance={},
            confidence=100,
            sensitivity="private",
            version=1,
        ))
        db.commit()
        with pytest.raises(ProactiveStateError, match="Account.id boundary"):
            proactive_intelligence_report(
                db, owner_id=alice.id, as_of="2026-07-20T12:00:00Z"
            )
    finally:
        db.close()


def test_finance_health_habit_and_home_inputs_stay_factual_and_record_only():
    db = _Session()
    try:
        account = _account(db, "proactive-domains")
        bill, _ = create_finance_record(
            db,
            account=account,
            record_type="bill",
            title="Electricity bill",
            scope="personal",
            effective_at="2026-07-01T00:00:00Z",
            due_at="2026-07-16T00:00:00Z",
            amount="1500",
            currency="INR",
            source={"kind": "manual", "label": "User entry"},
            details={"payee": "Power provider", "bill_status": "due"},
        )
        subscription, _ = create_finance_record(
            db,
            account=account,
            record_type="subscription",
            title="Unused design subscription",
            scope="personal",
            effective_at="2026-01-01T00:00:00Z",
            due_at="2026-08-01T00:00:00Z",
            amount="999",
            currency="INR",
            source={"kind": "manual", "label": "User entry"},
            details={
                "provider": "Design tool",
                "cadence": "monthly",
                "subscription_status": "active",
            },
            provenance={"usage_evidence": {"usage_count_30d": 0}},
        )
        for index, weight in enumerate((70, 70, 100), start=1):
            create_health_record(
                db,
                account=account,
                record_type="weight",
                title=f"Weight observation {index}",
                recorded_at=f"2026-07-{index:02d}T08:00:00Z",
                metrics=[{"name": "weight", "value": weight, "unit": "kg"}],
                details={},
                source={"kind": "manual", "label": "User entry"},
            )
        symptom, _ = create_health_record(
            db,
            account=account,
            record_type="symptom",
            title="User symptom note",
            recorded_at="2026-07-17T03:00:00Z",
            metrics=[],
            details={"description": "Sudden chest pain", "red_flags": []},
            source={"kind": "manual", "label": "User entry"},
        )
        habit, _ = create_habit(
            db,
            account=account,
            title="Morning setup",
            routine_type="morning",
            schedule={
                "cadence": "daily",
                "start_date": "2026-07-14",
                "time_of_day": "08:00",
                "timezone": "Asia/Kolkata",
                "grace_minutes": 0,
            },
            duration_minutes=15,
        )
        home, _ = create_home_record(
            db,
            account=account,
            record_type="renewal",
            title="Membership renewal",
            effective_at="2026-01-01T00:00:00Z",
            due_at="2026-07-16T00:00:00Z",
            source={"kind": "manual", "label": "User entry"},
            details={
                "renewal_kind": "membership",
                "provider_name": "Professional association",
                "cadence": "annual",
            },
        )
        db.commit()

        report = proactive_intelligence_report(
            db,
            owner_id=account.id,
            as_of="2026-07-17T10:00:00+05:30",
            horizon_days=30,
            lookback_days=30,
            limit=200,
        )
        kinds = {item["kind"] for item in report["items"]}
        assert {
            "finance_due",
            "unused_subscription_input",
            "health_trend_caution_input",
            "health_reported_red_flag",
            "missed_routine",
            "home_due",
        } <= kinds
        assert _signal_for(report, "finance_due", bill.id)["calculation"][
            "not_advice"
        ] is True
        unused = _signal_for(report, "unused_subscription_input", subscription.id)
        assert unused["routing"]["channel"] == "digest"
        assert unused["calculation"]["usage_count_30d"] == 0
        urgent_health = _signal_for(
            report, "health_reported_red_flag", symptom.id
        )
        assert urgent_health["routing"]["channel"] == "interrupt"
        assert urgent_health["calculation"]["no_diagnosis"] is True
        assert _signal_for(report, "missed_routine", habit.id)["calculation"][
            "schedule_timezone"
        ] == "Asia/Kolkata"
        assert _signal_for(report, "home_due", home.id)["calculation"][
            "overdue"
        ] is True
        assert report["safety_policy"]["financial_advice"] is False
        assert report["safety_policy"]["health_is_factual_input_only"] is True
    finally:
        db.close()


def test_relationship_and_decision_inputs_include_source_provenance_evidence():
    db = _Session()
    try:
        account = _account(db, "proactive-relationship")
        source, _ = create_life_source(
            db,
            account=account,
            source_type="meeting",
            title="Project meeting notes",
            observed_at=datetime(2026, 7, 10, 8),
        )
        profile, _ = create_relationship_profile(
            db,
            account=account,
            title="Alex",
            subject_kind="person",
            relationship_type="collaborator",
            contact_origin={
                "kind": "meeting",
                "label": "Project meeting",
                "source_id": source.id,
                "observed_at": "2026-07-10T08:00:00Z",
            },
            provenance={"source_id": source.id},
        )
        commitment, _ = create_commitment(
            db,
            account=account,
            profile_id=profile.id,
            title="Send reviewed draft",
            due_at="2026-07-16T08:00:00Z",
            direction="made_by_me",
            provenance={"source_id": source.id},
        )
        unanswered, _ = create_follow_up(
            db,
            account=account,
            profile_id=profile.id,
            title="Review unanswered message",
            due_at="2026-07-16T09:00:00Z",
            priority="high",
            reminder_kind="unanswered_message",
            provenance={"source_id": source.id},
        )
        decision, _ = create_decision(
            db,
            account=account,
            title="Choose application strategy",
            decision_date="2026-06-01T00:00:00Z",
            context="Choose a bounded application strategy.",
            options=["Focused", "Broad"],
            chosen_option="option_1",
            reasons=["Protect deep-work time"],
            assumptions=[{
                "id": "funding",
                "text": "Funding remains available",
                "status": "unverified",
                "review_at": "2026-07-01T00:00:00Z",
            }],
            review_at="2026-07-01T00:00:00Z",
            provenance={"source_id": source.id},
            evidence=[{"label": "Meeting notes", "source_id": source.id}],
        )
        db.commit()

        report = proactive_intelligence_report(
            db,
            owner_id=account.id,
            as_of="2026-07-17T12:00:00Z",
            limit=200,
        )
        commitment_signal = _signal_for(
            report, "overdue_commitment", commitment.id
        )
        unanswered_signal = _signal_for(
            report, "unanswered_message_due", unanswered.id
        )
        decision_signal = _signal_for(
            report, "stale_decision_assumption", decision.id
        )
        assert commitment_signal["routing"]["channel"] == "interrupt"
        assert unanswered_signal["routing"]["channel"] == "interrupt"
        assert unanswered_signal["calculation"]["reminder_kind"] == (
            "unanswered_message"
        )
        assert decision_signal["calculation"]["due_assumption_ids"] == [
            "funding"
        ]
        assert source.id in {
            row["id"]
            for signal in (commitment_signal, unanswered_signal, decision_signal)
            for row in signal["evidence"]["sources"]
        }
        assert any(
            source.id in entity["source_ids"]
            for entity in commitment_signal["evidence"]["entities"]
        )
        assert all(
            "provenance" not in entity
            for entity in commitment_signal["evidence"]["entities"]
        )
        assert all(
            "source_ref" not in row
            for row in commitment_signal["evidence"]["sources"]
        )
    finally:
        db.close()


def test_work_conflict_capacity_stall_and_postponement_are_deterministic():
    db = _Session()
    try:
        as_of = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(
            minutes=5,
        )
        due_day = (as_of + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        first_due = due_day + timedelta(hours=9)
        second_due = due_day + timedelta(hours=10)
        postponed_due = due_day + timedelta(hours=18)
        account = _account(db, "proactive-work")
        goal, _ = create_life_entity(
            db,
            account=account,
            entity_type="goal",
            title="Ship V3",
            provenance={"interface": "focused-test"},
        )
        project, _ = create_life_entity(
            db,
            account=account,
            entity_type="project",
            title="V3 release",
            status="stalled",
            provenance={"interface": "focused-test"},
        )
        create_entity_link(
            db,
            account=account,
            source_id=project.id,
            relation="supports",
            target_id=goal.id,
            provenance={"interface": "focused-test"},
        )
        first = _task(
            db,
            account,
            title="Finish migration",
            deadline=first_due.isoformat(),
            priority="critical",
            effort_minutes=300,
            project_id=project.id,
        )
        second = _task(
            db,
            account,
            title="Run release verification",
            deadline=second_due.isoformat(),
            priority="high",
            effort_minutes=300,
            project_id=project.id,
        )
        create_entity_link(
            db,
            account=account,
            source_id=first.id,
            relation="conflicts_with",
            target_id=second.id,
            provenance={"interface": "focused-test"},
        )
        update_task_record(
            db,
            account=account,
            entity_id=first.id,
            expected_version=1,
            changes={"deadline": postponed_due.isoformat()},
        )
        db.commit()

        report = proactive_intelligence_report(
            db,
            owner_id=account.id,
            as_of=as_of.isoformat(),
            horizon_days=2,
            daily_capacity_minutes=480,
            limit=200,
        )
        kinds = {item["kind"] for item in report["items"]}
        assert {
            "conflicting_work",
            "workload_overcommitment",
            "stalled_project",
            "deadline_postponement",
        } <= kinds
        assert not any(
            item["kind"] == "goal_disconnected_work"
            and first.id in {row["id"] for row in item["evidence"]["entities"]}
            for item in report["items"]
        )
        workload = _signals(report, "workload_overcommitment")[0]
        assert workload["calculation"]["total_effort_minutes"] == 600
        assert workload["calculation"]["excess_minutes"] == 120
        conflict = _signals(report, "conflicting_work")[0]
        assert any(
            row["relation"] == "conflicts_with"
            for row in conflict["evidence"]["entity_links"]
        )
        assert all(
            "metadata" not in row and "provenance" not in row
            for row in conflict["evidence"]["entity_links"]
        )
        postponed = _signal_for(report, "deadline_postponement", first.id)
        assert postponed["calculation"]["postponement_count"] == 1
        assert _signal_for(report, "stalled_project", project.id)["calculation"][
            "explicit_stall"
        ] is True
    finally:
        db.close()


def test_future_or_truncated_habit_logs_never_prove_a_routine_was_completed():
    db = _Session()
    try:
        account = _account(db, "proactive-habit-time")
        habit, _ = create_habit(
            db,
            account=account,
            title="Daily review",
            routine_type="evening",
            schedule={
                "cadence": "daily",
                "start_date": "2026-07-15",
                "time_of_day": "08:00",
                "timezone": "UTC",
                "grace_minutes": 0,
            },
            duration_minutes=10,
        )
        create_habit_log(
            db,
            account=account,
            habit_id=habit.id,
            result="completed",
            logged_at="2026-07-20T09:00:00Z",
            scheduled_for="2026-07-15",
            quality=90,
            duration_minutes=10,
            source={"kind": "manual", "label": "Future correction"},
        )
        create_habit_log(
            db,
            account=account,
            habit_id=habit.id,
            result="completed",
            logged_at="2026-07-16T09:00:00Z",
            scheduled_for="2026-07-16",
            quality=90,
            duration_minutes=10,
            source={"kind": "manual", "label": "Observed completion"},
        )
        db.commit()

        complete_scan = proactive_intelligence_report(
            db,
            owner_id=account.id,
            as_of="2026-07-17T12:00:00+00:00",
            lookback_days=3,
            scan_limit=50,
            limit=100,
        )
        missed_dates = {
            row["calculation"]["scheduled_for"]
            for row in _signals(complete_scan, "missed_routine")
        }
        assert "2026-07-15" in missed_dates
        assert "2026-07-16" not in missed_dates

        truncated_scan = proactive_intelligence_report(
            db,
            owner_id=account.id,
            as_of="2026-07-17T12:00:00+00:00",
            lookback_days=3,
            scan_limit=1,
            limit=100,
        )
        assert _signals(truncated_scan, "missed_routine") == []
        assert truncated_scan["scans"]["habit_absence_inference"] == {
            "scanned": 0,
            "truncated": True,
            "skipped": True,
        }
    finally:
        db.close()
