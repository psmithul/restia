"""Read-only model tool for Restia's canonical owner-scoped Life graph.

The assistant needs one cross-domain query surface, but model output must not
write directly to authoritative records.  ``query_life`` therefore exposes
bounded reads only.  Mutations continue through typed APIs/domain tools and the
reviewed ActionPolicy boundary.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, Optional

from src.tools._common import _parse_tool_args


logger = logging.getLogger(__name__)

LIFE_ANSWER_CONTRACT = {
    "format": "compact_decision_support",
    "lead_with": "answer",
    "reasoning": "evidence_backed_rationale_not_hidden_chain_of_thought",
    "sources": "cite_source_entity_or_evidence_ids",
    "assumptions": "state_missing_stale_inferred_or_uncertain_data",
    "available_actions": "offer_only_supported_reviewed_actions",
}


def _error(message: object) -> Dict[str, Any]:
    return {"error": str(message), "exit_code": 1, "read_only": True}


def _required_owner(owner: Optional[str]) -> str:
    value = str(owner or "").strip()
    if not value:
        raise ValueError("An authenticated owner is required")
    return value


def _empty(action: str) -> Dict[str, Any]:
    base: Dict[str, Any] = {
        "response": "No Life OS records found for this account.",
        "action": action,
        "read_only": True,
        "exit_code": 0,
    }
    if action in {
        "list", "search", "decisions_due", "travel_records",
        "learning_career_records", "learning_career_search",
        "work_business_workspaces", "work_business_records",
        "work_business_search", "knowledge_records", "knowledge_search",
        "knowledge_sources", "knowledge_source_search",
        "automation_definitions", "automation_history",
    }:
        base.update({"items": [], "count": 0, "truncated": False})
    elif action == "summary":
        base.update({
            "counts": {},
            "recent": [],
            "task_attention": {
                "items": [], "count": 0, "scanned": 0,
                "flag_counts": {}, "truncated": False,
            },
            "decision_reviews": {
                "items": [], "count": 0, "scanned": 0,
                "truncated": False,
            },
        })
    elif action == "communications":
        from src.communications_hub import empty_communications_view

        base.update(empty_communications_view())
    return base


async def _do_query_life_impl(content: str, owner: Optional[str] = None) -> Dict:
    """Query the canonical Life graph without creating or mutating records."""

    from core.database import SessionLocal
    from src.decision_service import list_due_decision_reviews
    from src.calendar_intelligence import calendar_time_report
    from src.communications_hub import communications_view
    from src.finance_service import (
        finance_affordability,
        finance_anomaly_input,
        finance_cash_flow,
        finance_forecast,
        finance_net_worth,
        finance_summary,
        list_due_finance_records,
        list_subscriptions,
    )
    from src.health_service import health_trends
    from src.home_service import (
        home_alert_report,
        list_home_records,
        search_home_records,
    )
    from src.habit_service import (
        missed_routine_report,
        weekly_adjustment_report,
        weekly_habit_report,
    )
    from src.identity import find_account
    from src.life_automation import (
        EXECUTION_CONTRACT,
        automation_definition_history,
        evaluate_automation,
        get_automation_definition,
        list_automation_definitions,
        serialize_automation_definition,
    )
    from src.journal_service import (
        get_journal_entry,
        journal_period_review,
        list_journal_entries,
        search_journal_entries,
    )
    from src.learning_career_service import (
        career_learning_plan,
        list_learning_career_records,
        search_learning_career_records,
    )
    from src.relationship_service import (
        list_relationship_profiles,
        relationship_reminders,
    )
    from src.personal_knowledge_service import (
        citation_backed_answer_evidence,
        is_typed_personal_knowledge_payload,
        list_knowledge_sources,
        list_personal_knowledge_records,
        list_stale_personal_knowledge,
        markdown_index_rebuild_manifest,
        search_knowledge_sources,
        search_personal_knowledge_records,
    )
    from src.proactive_intelligence import proactive_intelligence_report
    from src.travel_service import list_travel_records, travel_mode
    from src.work_business_service import (
        list_work_business_records,
        list_work_business_workspaces,
        search_work_business_records,
        workspace_summary,
    )
    from src.life_graph import (
        LifeGraphError,
        get_life_entity,
        list_entity_links,
        list_life_entities,
        search_life_entities,
        serialize_entity_link,
        serialize_life_entity,
        task_quality_report,
        traverse_life_graph,
    )

    try:
        args = _parse_tool_args(content)
        owner_name = _required_owner(owner)
    except ValueError as exc:
        return _error(exc)

    action = str(args.get("action") or "summary").strip().lower().replace("-", "_")
    action = {
        "overview": "summary",
        "find": "search",
        "view": "get",
        "graph": "traverse",
        "quality": "task_quality",
        "due_decisions": "decisions_due",
        "decision_reviews": "decisions_due",
        "calendar_plan": "calendar_time",
        "time_plan": "calendar_time",
        "free_time": "calendar_time",
        "trends": "health_trends",
        "money": "finance_summary",
        "spending": "finance_cash_flow",
        "cash_flow": "finance_cash_flow",
        "subscriptions": "finance_subscriptions",
        "bills": "finance_due",
        "financial_anomalies": "finance_anomalies",
        "net_worth": "finance_net_worth",
        "forecast": "finance_forecast",
        "affordability": "finance_affordability",
        "habits": "habit_weekly",
        "habit_report": "habit_weekly",
        "routine_report": "habit_weekly",
        "missed_routines": "habits_missed",
        "habit_misses": "habits_missed",
        "habit_adjustments": "habit_adjustments",
        "routine_adjustments": "habit_adjustments",
        "people": "relationship_profiles",
        "relationships": "relationship_profiles",
        "relationship_care": "relationship_reminders",
        "follow_ups": "relationship_reminders",
        "journal": "journal_entries",
        "journal_list": "journal_entries",
        "journal_search": "journal_search",
        "journal_get": "journal_get",
        "journal_review": "journal_review",
        "reflection_review": "journal_review",
        "home": "home_records",
        "home_list": "home_records",
        "home_search": "home_search",
        "expiry_alerts": "home_alerts",
        "home_alerts": "home_alerts",
        "travel": "travel_records",
        "trips": "travel_records",
        "travel_list": "travel_records",
        "trip_mode": "travel_mode",
        "learning": "learning_career_records",
        "career": "learning_career_records",
        "learning_search": "learning_career_search",
        "career_search": "learning_career_search",
        "career_plan": "career_learning_plan",
        "workspaces": "work_business_workspaces",
        "work_business": "work_business_workspaces",
        "workspace_records": "work_business_records",
        "workspace_search": "work_business_search",
        "workspace_summary": "work_business_summary",
        "priorities": "proactive_report",
        "priority_report": "proactive_report",
        "proactive": "proactive_report",
        "what_next": "proactive_report",
        "knowledge": "knowledge_records",
        "memory": "knowledge_records",
        "knowledge_list": "knowledge_records",
        "memory_search": "knowledge_search",
        "knowledge_search": "knowledge_search",
        "stale_knowledge": "knowledge_stale",
        "knowledge_gaps": "knowledge_evidence",
        "answer_evidence": "knowledge_evidence",
        "sources": "knowledge_sources",
        "source_search": "knowledge_source_search",
        "markdown_manifest": "knowledge_markdown_manifest",
        "automations": "automation_definitions",
        "automation_list": "automation_definitions",
        "automation": "automation_get",
        "unified_inbox": "communications",
        "communication_search": "communications",
        "communications_search": "communications",
        "messages": "communications",
    }.get(action, action)
    supported = {
        "summary", "list", "search", "get", "traverse",
        "task_quality", "decisions_due", "health_trends", "calendar_time",
        "finance_summary", "finance_cash_flow", "finance_subscriptions",
        "finance_due", "finance_anomalies", "finance_net_worth",
        "finance_forecast", "finance_affordability",
        "habit_weekly", "habits_missed", "habit_adjustments",
        "relationship_profiles", "relationship_reminders",
        "journal_entries", "journal_search", "journal_get", "journal_review",
        "home_records", "home_search", "home_alerts",
        "travel_records", "travel_mode",
        "learning_career_records", "learning_career_search",
        "career_learning_plan",
        "work_business_workspaces", "work_business_records",
        "work_business_search", "work_business_summary",
        "proactive_report", "knowledge_records", "knowledge_search",
        "knowledge_stale", "knowledge_evidence", "knowledge_sources",
        "knowledge_source_search", "knowledge_markdown_manifest",
        "automation_definitions", "automation_get", "automation_history",
        "automation_evaluate", "communications",
    }
    if action not in supported:
        return _error(
            "Unsupported action. Use summary, list, search, get, traverse, "
            "task_quality, decisions_due, health_trends, calendar_time, finance_summary, "
            "finance_cash_flow, finance_subscriptions, finance_due, or "
            "finance_anomalies, finance_net_worth, finance_forecast, "
            "finance_affordability, habit_weekly, habits_missed, or "
            "habit_adjustments, relationship_profiles, or "
            "relationship_reminders, journal_entries, journal_search, "
            "journal_get, journal_review, home_records, home_search, or "
            "home_alerts, travel_records, travel_mode, "
            "learning_career_records, learning_career_search, or "
            "career_learning_plan, work_business_workspaces, "
            "work_business_records, work_business_search, or "
            "work_business_summary, proactive_report, knowledge_records, "
            "knowledge_search, knowledge_stale, knowledge_evidence, "
            "knowledge_sources, knowledge_source_search, or "
            "knowledge_markdown_manifest, automation_definitions, "
            "automation_get, automation_history, automation_evaluate, or communications"
        )

    db = SessionLocal()
    try:
        account = find_account(db, owner_name)
        if account is None:
            return _empty(action)

        limit = max(1, min(100, int(args.get("limit") or 25)))
        if action == "communications":
            raw_connectors = args.get("connectors")
            if raw_connectors is None:
                connectors = None
            elif isinstance(raw_connectors, str):
                connectors = [
                    value.strip() for value in raw_connectors.split(",")
                    if value.strip()
                ]
            elif isinstance(raw_connectors, list) and all(
                isinstance(value, str) for value in raw_connectors
            ):
                connectors = raw_connectors
            else:
                return _error("connectors must be a list of connector names")
            unread_only = args.get("unread_only", False)
            important_only = args.get("important_only", False)
            if type(unread_only) is not bool or type(important_only) is not bool:
                return _error("communication filters must be true or false")
            result = communications_view(
                db,
                account=account,
                connectors=connectors,
                query=args.get("query") or args.get("q") or "",
                unread_only=unread_only,
                important_only=important_only,
                limit=limit,
            )
            return {
                "response": (
                    f"Found {result['count']} unified communication thread(s); "
                    "no message was sent or source state changed."
                ),
                **result,
                "exit_code": 0,
            }
        if action == "calendar_time":
            result = calendar_time_report(
                db,
                owner_id=account.id,
                as_of=args.get("as_of"),
                window_start=args.get("from_at"),
                window_end=args.get("to_at"),
                minimum_slot_minutes=args.get("minimum_slot_minutes") or 30,
                day_start_hour=args.get("day_start_hour") if args.get("day_start_hour") is not None else 6,
                day_end_hour=args.get("day_end_hour") if args.get("day_end_hour") is not None else 23,
                daily_capacity_minutes=args.get("daily_capacity_minutes") or 600,
                travel_buffer_minutes=args.get("travel_buffer_minutes") if args.get("travel_buffer_minutes") is not None else 30,
            )
            return {
                "response": (
                    f"Found {len(result['free_slots'])} free slot(s), "
                    f"{len(result['conflicts'])} conflict(s), and "
                    f"{len(result['unfinished_work'])} unfinished work block(s)."
                ),
                **result,
                "exit_code": 0,
            }
        if action == "automation_definitions":
            enabled = args.get("enabled")
            if enabled is not None and type(enabled) is not bool:
                return _error("enabled must be true or false")
            rows, truncated = list_automation_definitions(
                db,
                owner_id=account.id,
                enabled=enabled,
                limit=limit,
            )
            items = [serialize_automation_definition(row) for row in rows]
            return {
                "response": f"Found {len(items)} automation definition(s).",
                "items": items,
                "count": len(items),
                "truncated": truncated,
                "execution_contract": dict(EXECUTION_CONTRACT),
                "read_only": True,
                "exit_code": 0,
            }

        if action in {
            "automation_get", "automation_history", "automation_evaluate",
        }:
            automation_id = str(
                args.get("automation_id")
                or args.get("entity_id")
                or args.get("id")
                or ""
            ).strip()
            if not automation_id:
                return _error("automation_id is required")
            if action == "automation_get":
                entity = get_automation_definition(
                    db, owner_id=account.id, automation_id=automation_id
                )
                return {
                    "response": f"Found automation: {entity.title}",
                    "automation": serialize_automation_definition(entity),
                    "execution_contract": dict(EXECUTION_CONTRACT),
                    "read_only": True,
                    "exit_code": 0,
                }
            if action == "automation_history":
                items, truncated = automation_definition_history(
                    db,
                    owner_id=account.id,
                    automation_id=automation_id,
                    limit=limit,
                )
                return {
                    "response": f"Found {len(items)} automation version(s).",
                    "items": items,
                    "count": len(items),
                    "truncated": truncated,
                    "execution_contract": dict(EXECUTION_CONTRACT),
                    "read_only": True,
                    "exit_code": 0,
                }
            evaluation = evaluate_automation(
                db,
                owner_id=account.id,
                automation_id=automation_id,
                event=args.get("event"),
            )
            return {
                "response": (
                    "Automation matched; returned typed plans without preparing "
                    "or executing them."
                    if evaluation.matched
                    else "Automation did not match; nothing was prepared or executed."
                ),
                "evaluation": {
                    "automation_id": evaluation.automation_id,
                    "automation_version": evaluation.automation_version,
                    "matched": evaluation.matched,
                    "trigger_type": evaluation.trigger_type,
                    "event_fingerprint": evaluation.event_fingerprint,
                    "event_summary": dict(evaluation.event_summary),
                    "source_ids": list(evaluation.source_ids),
                    "plans": [dict(plan) for plan in evaluation.plans],
                },
                "prepared": False,
                "executed": False,
                "sent": False,
                "execution_contract": dict(EXECUTION_CONTRACT),
                "read_only": True,
                "exit_code": 0,
            }

        if action == "list":
            items, truncated = list_life_entities(
                db,
                owner_id=account.id,
                entity_type=args.get("entity_type"),
                status=args.get("status"),
                include_deleted=False,
                limit=limit,
            )
            visible = [
                item for item in items
                if not is_typed_personal_knowledge_payload(
                    item.entity_type, item.properties
                )
            ]
            payload = [serialize_life_entity(item) for item in visible]
            return {
                "response": f"Found {len(payload)} Life OS record(s).",
                "items": payload,
                "count": len(payload),
                "truncated": truncated,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "search":
            query = str(args.get("query") or args.get("q") or "").strip()
            result = search_life_entities(
                db,
                owner_id=account.id,
                query_text=query,
                entity_type=args.get("entity_type"),
                status=args.get("status"),
                limit=limit,
            )
            result["items"] = [
                item for item in result.get("items", [])
                if not is_typed_personal_knowledge_payload(
                    (item.get("entity") or {}).get("entity_type"),
                    (item.get("entity") or {}).get("properties"),
                )
            ]
            result["count"] = len(result["items"])
            result["typed_personal_knowledge_excluded"] = True
            return {
                "response": f"Found {result['count']} matching Life OS record(s).",
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "get":
            entity_id = str(args.get("entity_id") or args.get("id") or "").strip()
            if not entity_id:
                return _error("entity_id is required")
            entity = get_life_entity(db, owner_id=account.id, entity_id=entity_id)
            if is_typed_personal_knowledge_payload(
                entity.entity_type, entity.properties
            ):
                return _error(
                    "Use knowledge_records, knowledge_search, or "
                    "knowledge_evidence for typed personal knowledge reads"
                )
            links, links_truncated = list_entity_links(
                db,
                owner_id=account.id,
                entity_id=entity.id,
                direction="both",
                limit=limit,
            )
            return {
                "response": f"Found {entity.entity_type}: {entity.title}",
                "entity": serialize_life_entity(entity),
                "links": [serialize_entity_link(link) for link in links],
                "links_truncated": links_truncated,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "traverse":
            entity_id = str(args.get("entity_id") or args.get("id") or "").strip()
            if not entity_id:
                return _error("entity_id is required")
            result = traverse_life_graph(
                db,
                owner_id=account.id,
                entity_id=entity_id,
                depth=max(1, min(8, int(args.get("depth") or 2))),
                limit=max(1, min(200, int(args.get("limit") or 100))),
            )
            excluded_ids = {
                str(entity.get("id"))
                for entity in result.get("entities", [])
                if is_typed_personal_knowledge_payload(
                    entity.get("entity_type"), entity.get("properties")
                )
            }
            if str(result.get("root_id")) in excluded_ids:
                return _error(
                    "Use knowledge_records, knowledge_search, or "
                    "knowledge_evidence for typed personal knowledge reads"
                )
            result["entities"] = [
                entity for entity in result.get("entities", [])
                if str(entity.get("id")) not in excluded_ids
            ]
            result["links"] = [
                link for link in result.get("links", [])
                if str(link.get("source_id")) not in excluded_ids
                and str(link.get("target_id")) not in excluded_ids
            ]
            result["typed_personal_knowledge_excluded"] = True
            return {
                "response": (
                    f"Traversed {len(result['entities'])} record(s) and "
                    f"{len(result['links'])} link(s)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "task_quality":
            result = task_quality_report(
                db, owner_id=account.id, limit=limit
            )
            return {
                "response": f"Checked {result['scanned']} active task(s).",
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "decisions_due":
            result = list_due_decision_reviews(
                db,
                owner_id=account.id,
                due_before=args.get("due_before"),
                stale_after_days=max(
                    1, min(3650, int(args.get("stale_after_days") or 30))
                ),
                limit=limit,
            )
            return {
                "response": f"Found {result['count']} decision review(s) due.",
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "health_trends":
            result = health_trends(
                db,
                owner_id=account.id,
                record_type=args.get("record_type"),
                metric=args.get("metric"),
                group_by=args.get("group_by") or "day",
                unit=args.get("unit"),
                from_at=args.get("from_at"),
                to_at=args.get("to_at"),
            )
            return {
                "response": (
                    f"Computed {len(result.get('buckets') or [])} health trend bucket(s)."
                ),
                **result,
                "medical_notice": (
                    "This is a factual record summary, not diagnosis, treatment, "
                    "or medication advice."
                ),
                "read_only": True,
                "exit_code": 0,
            }

        if action == "habit_weekly":
            result = weekly_habit_report(
                db,
                owner_id=account.id,
                week_start=args.get("week_start"),
                habit_id=args.get("habit_id"),
            )
            return {
                "response": (
                    f"Summarized {result['count']} private habit routine(s) "
                    "for the requested week."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "habits_missed":
            result = missed_routine_report(
                db,
                owner_id=account.id,
                as_of=args.get("as_of"),
                lookback_days=args.get("lookback_days") or 14,
                habit_id=args.get("habit_id"),
            )
            return {
                "response": (
                    f"Found {result['count']} source-backed missed routine "
                    "occurrence(s)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "habit_adjustments":
            result = weekly_adjustment_report(
                db,
                owner_id=account.id,
                week_start=args.get("week_start"),
                habit_id=args.get("habit_id"),
            )
            return {
                "response": (
                    f"Computed {result['count']} deterministic habit review "
                    "suggestion(s); none were applied."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "relationship_profiles":
            items, truncated = list_relationship_profiles(
                db,
                owner_id=account.id,
                subject_kind=args.get("subject_kind"),
                status=args.get("status"),
                limit=limit,
            )
            return {
                "response": f"Found {len(items)} private relationship profile(s).",
                "items": items,
                "count": len(items),
                "truncated": truncated,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "relationship_reminders":
            result = relationship_reminders(
                db,
                owner_id=account.id,
                due_before=args.get("due_before"),
                as_of=args.get("as_of"),
                limit=limit,
            )
            return {
                "response": (
                    f"Found {result['count']} explicit, source-backed "
                    "relationship reminder(s)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "journal_entries":
            items, truncated = list_journal_entries(
                db,
                owner_id=account.id,
                from_date=args.get("from_date"),
                to_date=args.get("to_date"),
                status=args.get("status"),
                limit=limit,
            )
            return {
                "response": f"Found {len(items)} private journal entry or entries.",
                "items": items,
                "count": len(items),
                "truncated": truncated,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "journal_search":
            result = search_journal_entries(
                db,
                owner_id=account.id,
                query_text=args.get("query") or args.get("q"),
                from_date=args.get("from_date"),
                to_date=args.get("to_date"),
                limit=limit,
            )
            return {
                "response": f"Found {result['count']} matching private journal entry or entries.",
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "journal_get":
            entity_id = str(args.get("entity_id") or args.get("id") or "").strip()
            if not entity_id:
                return _error("entity_id is required")
            entry = get_journal_entry(
                db, owner_id=account.id, entity_id=entity_id
            )
            return {
                "response": f"Found private journal entry: {entry['title']}",
                "entry": entry,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "journal_review":
            result = journal_period_review(
                db,
                owner_id=account.id,
                period=args.get("period"),
                anchor_date=args.get("anchor_date"),
            )
            return {
                "response": (
                    f"Reviewed {result['entry_count']} private journal entry or "
                    "entries using explicit structured evidence."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "home_records":
            items, truncated = list_home_records(
                db,
                owner_id=account.id,
                record_type=args.get("record_type"),
                status=args.get("status"),
                include_archived=bool(args.get("include_archived", False)),
                limit=limit,
            )
            return {
                "response": f"Found {len(items)} private home/admin record(s).",
                "items": items,
                "count": len(items),
                "truncated": truncated,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "home_search":
            result = search_home_records(
                db,
                owner_id=account.id,
                query_text=args.get("query") or args.get("q"),
                record_type=args.get("record_type"),
                limit=limit,
            )
            return {
                "response": f"Found {result['count']} matching home/admin record(s).",
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "home_alerts":
            result = home_alert_report(
                db,
                owner_id=account.id,
                as_of=args.get("as_of"),
                horizon_days=(
                    90 if args.get("horizon_days") is None
                    else args.get("horizon_days")
                ),
                include_overdue=bool(args.get("include_overdue", True)),
                record_type=args.get("record_type"),
                limit=limit,
            )
            return {
                "response": (
                    f"Found {result['count']} deterministic home due/expiry "
                    "alert(s)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "travel_records":
            items, truncated = list_travel_records(
                db,
                owner_id=account.id,
                record_kind=args.get("record_kind"),
                trip_id=args.get("trip_id"),
                status=args.get("status"),
                limit=limit,
            )
            return {
                "response": f"Found {len(items)} private travel record(s).",
                "items": items,
                "count": len(items),
                "truncated": truncated,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "travel_mode":
            result = travel_mode(
                db,
                owner_id=account.id,
                as_of=args.get("as_of"),
                offline_only=bool(args.get("offline_only", True)),
                trip_limit=args.get("trip_limit") or 3,
                fact_limit=args.get("fact_limit") or min(limit, 50),
            )
            visible_trip_count = (
                len(result["current_trips"]) + len(result["next_trips"])
            )
            return {
                "response": (
                    f"Prepared {visible_trip_count} current or upcoming trip(s) "
                    "from bounded private records."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "learning_career_records":
            items, truncated = list_learning_career_records(
                db,
                owner_id=account.id,
                domain=args.get("domain"),
                record_kind=args.get("record_kind"),
                status=args.get("status"),
                limit=limit,
            )
            return {
                "response": f"Found {len(items)} private learning/career record(s).",
                "items": items,
                "count": len(items),
                "truncated": truncated,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "learning_career_search":
            result = search_learning_career_records(
                db,
                owner_id=account.id,
                query_text=args.get("query") or args.get("q"),
                domain=args.get("domain"),
                record_kind=args.get("record_kind"),
                limit=limit,
            )
            return {
                "response": (
                    f"Found {result['count']} matching private learning/career "
                    "record(s)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "career_learning_plan":
            target_id = str(
                args.get("career_target_id") or args.get("entity_id") or ""
            ).strip()
            if not target_id:
                return _error("career_target_id is required")
            result = career_learning_plan(
                db,
                owner_id=account.id,
                career_target_id=target_id,
                week_start=args.get("week_start"),
            )
            return {
                "response": (
                    f"Found {result['complete_chain_count']} complete "
                    "career-to-weekly-action chain(s)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "work_business_workspaces":
            items, truncated = list_work_business_workspaces(
                db,
                owner_id=account.id,
                workspace_kind=args.get("workspace_kind"),
                status=args.get("status"),
                limit=limit,
            )
            return {
                "response": f"Found {len(items)} private work/business workspace(s).",
                "items": items,
                "count": len(items),
                "truncated": truncated,
                "read_only": True,
                "exit_code": 0,
            }

        if action in {
            "work_business_records", "work_business_search",
            "work_business_summary",
        }:
            workspace_id = str(args.get("workspace_id") or "").strip()
            if not workspace_id:
                return _error("workspace_id is required")
            if action == "work_business_records":
                items, truncated = list_work_business_records(
                    db,
                    owner_id=account.id,
                    workspace_id=workspace_id,
                    record_kind=args.get("record_kind"),
                    status=args.get("status"),
                    limit=limit,
                )
                return {
                    "response": f"Found {len(items)} workspace record(s).",
                    "items": items,
                    "count": len(items),
                    "truncated": truncated,
                    "read_only": True,
                    "exit_code": 0,
                }
            if action == "work_business_search":
                result = search_work_business_records(
                    db,
                    owner_id=account.id,
                    workspace_id=workspace_id,
                    query_text=args.get("query") or args.get("q"),
                    record_kind=args.get("record_kind"),
                    limit=limit,
                )
                return {
                    "response": f"Found {result['count']} matching workspace record(s).",
                    **result,
                    "read_only": True,
                    "exit_code": 0,
                }
            result = workspace_summary(
                db, owner_id=account.id, workspace_id=workspace_id
            )
            return {
                "response": (
                    f"Summarized {result['totals']['records']} private "
                    "workspace record(s)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "proactive_report":
            as_of = args.get("as_of")
            if not as_of:
                return _error(
                    "as_of with an explicit UTC offset is required for proactive_report"
                )
            result = proactive_intelligence_report(
                db,
                owner_id=account.id,
                as_of=as_of,
                horizon_days=args.get("horizon_days") or 30,
                lookback_days=args.get("lookback_days") or 30,
                stale_project_days=args.get("stale_project_days") or 30,
                stale_decision_days=args.get("stale_decision_days") or 30,
                daily_capacity_minutes=args.get("daily_capacity_minutes") or 480,
                limit=limit,
                scan_limit=args.get("scan_limit") or 500,
                explicit_interrupts=args.get("explicit_interrupts"),
            )
            return {
                "response": (
                    f"Found {result['count']} deterministic priority signal(s): "
                    f"{len(result['interruptions'])} interruption(s) and "
                    f"{len(result['digest'])} digest item(s)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "knowledge_records":
            items, truncated = list_personal_knowledge_records(
                db,
                owner_id=account.id,
                memory_kind=args.get("memory_kind"),
                epistemic_status=args.get("epistemic_status"),
                status=args.get("status"),
                limit=limit,
                as_of=args.get("as_of"),
            )
            return {
                "response": f"Found {len(items)} private knowledge record(s).",
                "items": items,
                "count": len(items),
                "truncated": truncated,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "knowledge_search":
            result = search_personal_knowledge_records(
                db,
                owner_id=account.id,
                query_text=args.get("query") or args.get("q"),
                memory_kind=args.get("memory_kind"),
                epistemic_status=args.get("epistemic_status"),
                limit=limit,
                as_of=args.get("as_of"),
            )
            return {
                "response": (
                    f"Found {result['count']} matching private knowledge record(s)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "knowledge_stale":
            if not args.get("as_of"):
                return _error("as_of is required for knowledge_stale")
            result = list_stale_personal_knowledge(
                db,
                owner_id=account.id,
                as_of=args.get("as_of"),
                memory_kind=args.get("memory_kind"),
                limit=limit,
            )
            return {
                "response": f"Found {result['count']} stale knowledge record(s).",
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "knowledge_evidence":
            if not args.get("as_of"):
                return _error("as_of is required for knowledge_evidence")
            result = citation_backed_answer_evidence(
                db,
                owner_id=account.id,
                query_text=args.get("query") or args.get("q"),
                as_of=args.get("as_of"),
                memory_kind=args.get("memory_kind"),
                limit=min(limit, 50),
            )
            return {
                "response": (
                    f"Found {result['evidence_count']} directly cited knowledge "
                    "claim(s); no answer was synthesized."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "knowledge_sources":
            items, truncated = list_knowledge_sources(
                db,
                owner_id=account.id,
                source_kind=args.get("source_kind"),
                limit=limit,
            )
            return {
                "response": f"Found {len(items)} private knowledge source(s).",
                "items": items,
                "count": len(items),
                "truncated": truncated,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "knowledge_source_search":
            result = search_knowledge_sources(
                db,
                owner_id=account.id,
                query_text=args.get("query") or args.get("q"),
                source_kind=args.get("source_kind"),
                limit=limit,
            )
            return {
                "response": (
                    f"Found {result['count']} matching private knowledge source(s)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "knowledge_markdown_manifest":
            result = markdown_index_rebuild_manifest(
                db, owner_id=account.id, limit=limit,
            )
            return {
                "response": (
                    f"Found {result['count']} durable Markdown rebuild input(s)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "finance_summary":
            result = finance_summary(
                db,
                owner_id=account.id,
                scope=args.get("scope"),
                from_at=args.get("from_at"),
                to_at=args.get("to_at"),
            )
            return {
                "response": (
                    f"Summarized {result['count']} private finance record(s)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "finance_cash_flow":
            result = finance_cash_flow(
                db,
                owner_id=account.id,
                scope=args.get("scope"),
                currency=args.get("currency"),
                group_by=args.get("group_by") or "month",
                from_at=args.get("from_at"),
                to_at=args.get("to_at"),
            )
            return {
                "response": (
                    f"Computed {result['count']} private cash-flow bucket(s)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "finance_subscriptions":
            result = list_subscriptions(
                db,
                owner_id=account.id,
                scope=args.get("scope"),
                status=args.get("status"),
                currency=args.get("currency"),
                limit=limit,
            )
            return {
                "response": f"Found {result['count']} subscription record(s).",
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "finance_due":
            result = list_due_finance_records(
                db,
                owner_id=account.id,
                scope=args.get("scope"),
                due_before=args.get("due_before"),
                include_overdue=bool(args.get("include_overdue", True)),
                limit=limit,
            )
            return {
                "response": f"Found {result['count']} finance record(s) due.",
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "finance_anomalies":
            result = finance_anomaly_input(
                db,
                owner_id=account.id,
                scope=args.get("scope"),
                currency=args.get("currency"),
                from_at=args.get("from_at"),
                to_at=args.get("to_at"),
                limit=limit,
            )
            return {
                "response": (
                    f"Found {result['count']} deterministic finance review signal(s)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "finance_net_worth":
            result = finance_net_worth(
                db,
                owner_id=account.id,
                as_of=args.get("as_of"),
                scope=args.get("scope"),
            )
            return {
                "response": (
                    f"Computed record-backed net worth across "
                    f"{len(result['totals'])} currenc(ies)."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "finance_forecast":
            result = finance_forecast(
                db,
                owner_id=account.id,
                as_of=args.get("as_of"),
                scope=args.get("scope"),
                currency=args.get("currency"),
                horizon_days=args.get("horizon_days") or 30,
                lookback_days=args.get("lookback_days") or 90,
            )
            return {
                "response": (
                    f"Computed a bounded {result['horizon_days']}-day finance "
                    "scenario from stored records."
                ),
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        if action == "finance_affordability":
            result = finance_affordability(
                db,
                owner_id=account.id,
                as_of=args.get("as_of"),
                amount=args.get("amount"),
                currency=args.get("currency"),
                scope=args.get("scope"),
                horizon_days=args.get("horizon_days") or 30,
                lookback_days=args.get("lookback_days") or 90,
            )
            return {
                "response": result["reason"],
                **result,
                "read_only": True,
                "exit_code": 0,
            }

        # summary: bounded, current cross-domain context for the main Restia
        # surface.  Every row retains confidence and provenance from the graph.
        recent, recent_truncated = list_life_entities(
            db, owner_id=account.id, include_deleted=False, limit=100
        )
        recent = [
            row for row in recent
            if not is_typed_personal_knowledge_payload(
                row.entity_type, row.properties
            )
        ]
        counts = Counter(row.entity_type for row in recent)
        task_attention = task_quality_report(
            db, owner_id=account.id, limit=min(limit, 25)
        )
        decision_reviews = list_due_decision_reviews(
            db, owner_id=account.id, limit=min(limit, 25)
        )
        recent_payload = [serialize_life_entity(row) for row in recent[:limit]]
        return {
            "response": (
                f"Life OS summary: {len(recent)} recent record(s), "
                f"{task_attention['count']} task(s) need attention, and "
                f"{decision_reviews['count']} decision review(s) are due."
            ),
            "counts": dict(sorted(counts.items())),
            "counts_scanned": len(recent),
            "counts_truncated": recent_truncated,
            "recent": recent_payload,
            "recent_truncated": recent_truncated or len(recent) > limit,
            "task_attention": task_attention,
            "decision_reviews": decision_reviews,
            "read_only": True,
            "exit_code": 0,
        }
    except (LifeGraphError, TypeError, ValueError) as exc:
        return _error(exc)
    except Exception as exc:
        logger.error("query_life failed safely (%s)", type(exc).__name__)
        return _error("Life OS query failed safely")
    finally:
        db.rollback()
        db.close()


async def do_query_life(content: str, owner: Optional[str] = None) -> Dict:
    """Return a Life read plus the presentation contract for the assistant."""

    result = await _do_query_life_impl(content, owner=owner)
    return {**result, "answer_contract": dict(LIFE_ANSWER_CONTRACT)}


__all__ = ["LIFE_ANSWER_CONTRACT", "do_query_life"]
