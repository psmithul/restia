"""Principal-scoped API for Restia's cross-domain life graph."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from core.database import SessionLocal
from src.decision_service import (
    create_decision,
    decision_history,
    get_decision,
    list_decisions,
    list_due_decision_reviews,
    review_decision,
    search_decisions,
    serialize_decision,
    update_decision,
)
from src.identity import request_account_transaction
from src.health_service import (
    create_health_record,
    delete_health_record,
    get_health_record,
    health_record_history,
    health_trends,
    is_typed_health_payload,
    list_health_records,
    search_health_records,
    serialize_health_record,
    update_health_record,
)
from src.finance_service import (
    create_finance_record,
    delete_finance_record,
    finance_affordability,
    finance_anomaly_input,
    finance_cash_flow,
    finance_forecast,
    finance_net_worth,
    finance_record_history,
    finance_summary,
    get_finance_record,
    import_finance_records,
    is_typed_finance_payload,
    list_due_finance_records,
    list_finance_records,
    list_subscriptions,
    search_finance_records,
    serialize_finance_record,
    update_finance_record,
)
from src.habit_service import (
    create_habit,
    create_habit_log,
    delete_habit,
    delete_habit_log,
    get_habit,
    get_habit_log,
    habit_history,
    habit_log_history,
    is_typed_habit_payload,
    list_habit_logs,
    list_habits,
    missed_routine_report,
    search_habit_logs,
    search_habits,
    serialize_habit,
    serialize_habit_log,
    update_habit,
    update_habit_log,
    weekly_adjustment_report,
    weekly_habit_report,
)
from src.home_service import is_typed_home_payload
from src.relationship_service import is_typed_relationship_payload
from src.journal_service import is_typed_journal_payload
from src.travel_service import is_typed_travel_payload
from src.task_record_service import is_typed_task_payload
from src.learning_career_service import is_typed_learning_career_payload
from src.personal_knowledge_service import (
    is_typed_personal_knowledge_payload,
    is_typed_personal_knowledge_source_payload,
)
from src.work_business_service import (
    generic_link_requires_work_business_route,
    is_typed_work_business_payload,
    is_work_business_relation_id,
)
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
    list_life_entities,
    list_life_entity_versions,
    list_life_sources,
    search_life_entities,
    serialize_entity_link,
    serialize_life_entity,
    serialize_life_entity_version,
    serialize_life_source,
    task_quality_report,
    traverse_life_graph,
    update_life_entity,
)


class LifeSourceCreate(BaseModel):
    source_type: str = Field(max_length=48)
    title: str = Field(default="", max_length=240)
    source_ref: str | None = Field(default=None, max_length=2_000)
    safe_excerpt: str = Field(default="", max_length=4_000)
    content_sha256: str | None = Field(default=None, max_length=64)
    observed_at: datetime | None = None
    sensitivity: str = Field(default="private", max_length=24)
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=256)


class LifeEntityCreate(BaseModel):
    entity_type: str = Field(max_length=48)
    title: str = Field(max_length=240)
    summary: str = Field(default="", max_length=20_000)
    status: str = Field(default="active", max_length=32)
    properties: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    domain_ref_type: str | None = Field(default=None, max_length=48)
    domain_ref_id: str | None = Field(default=None, max_length=255)
    occurred_at: datetime | None = None
    due_at: datetime | None = None
    review_at: datetime | None = None
    idempotency_key: str | None = Field(default=None, max_length=256)
    reason: str = Field(default="Life entity created", max_length=500)


class LifeEntityUpdate(BaseModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    summary: str | None = Field(default=None, max_length=20_000)
    status: str | None = Field(default=None, max_length=32)
    properties: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)
    occurred_at: datetime | None = None
    due_at: datetime | None = None
    review_at: datetime | None = None
    reason: str = Field(default="Life entity updated", max_length=500)


class VersionedDelete(BaseModel):
    version: int = Field(ge=1)
    reason: str = Field(default="Life entity deleted", max_length=500)


class EntityLinkCreate(BaseModel):
    source_id: str = Field(max_length=36)
    relation: str = Field(max_length=64)
    target_id: str = Field(max_length=36)
    metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    reason: str = Field(default="Life entities linked", max_length=500)


class EntityLinkDelete(BaseModel):
    version: int = Field(ge=1)
    reason: str = Field(default="Life entity link deleted", max_length=500)


class DecisionOptionInput(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    label: str = Field(max_length=240)
    details: str = Field(default="", max_length=2_000)


class DecisionAssumptionInput(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    text: str = Field(max_length=1_000)
    status: str = Field(default="unverified", max_length=24)
    review_at: datetime | None = None
    note: str = Field(default="", max_length=2_000)


class DecisionEvidenceInput(BaseModel):
    label: str = Field(max_length=1_000)
    url: str | None = Field(default=None, max_length=2_000)
    source_id: str | None = Field(default=None, max_length=36)
    entity_id: str | None = Field(default=None, max_length=36)


class DecisionOutcomeInput(BaseModel):
    status: str = Field(default="pending", max_length=24)
    summary: str = Field(default="", max_length=4_000)
    recorded_at: datetime | None = None


class DecisionCreate(BaseModel):
    title: str = Field(max_length=240)
    decision_date: datetime | None = None
    context: str = Field(max_length=10_000)
    options: list[DecisionOptionInput] = Field(min_length=2, max_length=20)
    chosen_option: str = Field(max_length=64)
    reasons: list[str] = Field(min_length=1, max_length=30)
    risks: list[str] = Field(default_factory=list, max_length=30)
    assumptions: list[DecisionAssumptionInput] = Field(default_factory=list, max_length=40)
    people: list[str] = Field(default_factory=list, max_length=30)
    evidence: list[DecisionEvidenceInput] = Field(default_factory=list, max_length=40)
    review_at: datetime | None = None
    outcome: DecisionOutcomeInput | None = None
    linked_entity_ids: list[str] = Field(default_factory=list, max_length=50)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    idempotency_key: str | None = Field(default=None, max_length=256)


class DecisionUpdate(BaseModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    decision_date: datetime | None = None
    context: str | None = Field(default=None, max_length=10_000)
    options: list[DecisionOptionInput] | None = Field(default=None, min_length=2, max_length=20)
    chosen_option: str | None = Field(default=None, max_length=64)
    reasons: list[str] | None = Field(default=None, min_length=1, max_length=30)
    risks: list[str] | None = Field(default=None, max_length=30)
    assumptions: list[DecisionAssumptionInput] | None = Field(default=None, max_length=40)
    people: list[str] | None = Field(default=None, max_length=30)
    evidence: list[DecisionEvidenceInput] | None = Field(default=None, max_length=40)
    review_at: datetime | None = None
    outcome: DecisionOutcomeInput | None = None
    linked_entity_ids: list[str] | None = Field(default=None, max_length=50)
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)
    status: str | None = Field(default=None, max_length=32)


class DecisionAssumptionReview(BaseModel):
    id: str = Field(max_length=64)
    status: str | None = Field(default=None, max_length=24)
    review_at: datetime | None = None
    note: str | None = Field(default=None, max_length=2_000)


class DecisionReview(BaseModel):
    version: int = Field(ge=1)
    summary: str = Field(max_length=4_000)
    reviewed_at: datetime | None = None
    assumption_updates: list[DecisionAssumptionReview] = Field(
        default_factory=list, max_length=40
    )
    outcome: DecisionOutcomeInput | None = None
    next_review_at: datetime | None = None


class StrictHealthInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HealthMetricInput(StrictHealthInput):
    name: str = Field(max_length=64)
    value: float
    unit: str = Field(max_length=32)


class HealthSourceInput(StrictHealthInput):
    kind: str = Field(max_length=64)
    label: str = Field(max_length=240)
    reference: str | None = Field(default=None, max_length=2_000)
    external_id: str | None = Field(default=None, max_length=500)
    source_id: str | None = Field(default=None, max_length=36)
    provider: str | None = Field(default=None, max_length=240)


class HealthRecordCreate(StrictHealthInput):
    record_type: str = Field(max_length=64)
    title: str = Field(max_length=240)
    recorded_at: datetime
    ended_at: datetime | None = None
    due_at: datetime | None = None
    metrics: list[HealthMetricInput] = Field(default_factory=list, max_length=30)
    details: dict[str, Any] = Field(default_factory=dict)
    source: HealthSourceInput
    note: str = Field(default="", max_length=20_000)
    private_metadata: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    idempotency_key: str | None = Field(default=None, max_length=256)


class HealthRecordUpdate(StrictHealthInput):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    recorded_at: datetime | None = None
    ended_at: datetime | None = None
    due_at: datetime | None = None
    metrics: list[HealthMetricInput] | None = Field(default=None, max_length=30)
    details: dict[str, Any] | None = None
    source: HealthSourceInput | None = None
    note: str | None = Field(default=None, max_length=20_000)
    private_metadata: dict[str, Any] | None = None
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)
    status: str | None = Field(default=None, max_length=32)


class HealthImport(StrictHealthInput):
    records: list[HealthRecordCreate] = Field(min_length=1, max_length=100)


class StrictFinanceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FinanceSourceInput(StrictFinanceInput):
    kind: str = Field(max_length=64)
    label: str = Field(max_length=240)
    reference: str | None = Field(default=None, max_length=1_000)
    external_id: str | None = Field(default=None, max_length=256)
    source_id: str | None = Field(default=None, max_length=36)
    observed_at: datetime | None = None


class FinanceRecordCreate(StrictFinanceInput):
    record_type: str = Field(max_length=64)
    title: str = Field(max_length=240)
    scope: str = Field(max_length=24)
    effective_at: datetime
    source: FinanceSourceInput
    amount: str | int | float | None = None
    currency: str | None = Field(default=None, max_length=3)
    unit: str | None = Field(default=None, max_length=16)
    due_at: datetime | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(default="", max_length=20_000)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    idempotency_key: str | None = Field(default=None, max_length=256)


class FinanceRecordUpdate(StrictFinanceInput):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    scope: str | None = Field(default=None, max_length=24)
    effective_at: datetime | None = None
    source: FinanceSourceInput | None = None
    amount: str | int | float | None = None
    currency: str | None = Field(default=None, max_length=3)
    unit: str | None = Field(default=None, max_length=16)
    due_at: datetime | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    details: dict[str, Any] | None = None
    note: str | None = Field(default=None, max_length=20_000)
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)
    status: str | None = Field(default=None, max_length=32)


class FinanceImport(StrictFinanceInput):
    records: list[FinanceRecordCreate] = Field(min_length=1, max_length=100)


class StrictHabitInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HabitScheduleInput(StrictHabitInput):
    cadence: str = Field(max_length=32)
    start_date: date
    end_date: date | None = None
    days_of_week: list[int] = Field(default_factory=list, max_length=7)
    interval_days: int | None = Field(default=None, ge=1, le=365)
    time_of_day: str | None = Field(default=None, max_length=5)
    timezone: str = Field(default="UTC", max_length=64)
    grace_minutes: int = Field(default=0, ge=0, le=720)


class HabitTriggerInput(StrictHabitInput):
    kind: str = Field(max_length=32)
    cue: str = Field(max_length=500)
    at: str | None = Field(default=None, max_length=5)


class HabitChecklistInput(StrictHabitInput):
    id: str | None = Field(default=None, max_length=64)
    label: str = Field(max_length=300)
    required: bool = True


class HabitMinimumViableInput(StrictHabitInput):
    duration_minutes: int | None = Field(default=None, ge=1, le=1_440)
    checklist_item_ids: list[str] = Field(default_factory=list, max_length=50)
    description: str = Field(default="", max_length=1_000)


class HabitRecoveryRulesInput(StrictHabitInput):
    strategy: str = Field(default="next_available", max_length=32)
    window_hours: int = Field(default=24, ge=1, le=720)
    minimum_duration_minutes: int | None = Field(default=None, ge=1, le=1_440)
    note: str = Field(default="", max_length=1_000)


class HabitCreate(StrictHabitInput):
    title: str = Field(max_length=240)
    routine_type: str = Field(max_length=64)
    schedule: HabitScheduleInput
    triggers: list[HabitTriggerInput] = Field(default_factory=list, max_length=20)
    checklist: list[HabitChecklistInput] = Field(default_factory=list, max_length=50)
    contexts: list[str] = Field(default_factory=list, max_length=20)
    duration_minutes: int = Field(ge=1, le=1_440)
    minimum_viable: HabitMinimumViableInput | None = None
    recovery_rules: HabitRecoveryRulesInput | None = None
    note: str = Field(default="", max_length=20_000)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    idempotency_key: str | None = Field(default=None, max_length=256)


class HabitUpdate(StrictHabitInput):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=240)
    routine_type: str | None = Field(default=None, max_length=64)
    schedule: HabitScheduleInput | None = None
    triggers: list[HabitTriggerInput] | None = Field(default=None, max_length=20)
    checklist: list[HabitChecklistInput] | None = Field(default=None, max_length=50)
    contexts: list[str] | None = Field(default=None, max_length=20)
    duration_minutes: int | None = Field(default=None, ge=1, le=1_440)
    minimum_viable: HabitMinimumViableInput | None = None
    recovery_rules: HabitRecoveryRulesInput | None = None
    note: str | None = Field(default=None, max_length=20_000)
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)
    status: str | None = Field(default=None, max_length=32)


class HabitLogSourceInput(StrictHabitInput):
    kind: str = Field(default="manual", max_length=32)
    label: str = Field(default="User entry", max_length=240)
    external_id: str | None = Field(default=None, max_length=500)


class HabitChecklistEvidenceInput(StrictHabitInput):
    item_id: str = Field(max_length=64)
    status: str = Field(max_length=32)
    note: str = Field(default="", max_length=500)


class HabitLogCreate(StrictHabitInput):
    habit_id: str = Field(max_length=36)
    result: str = Field(max_length=32)
    logged_at: datetime
    scheduled_for: date
    quality: int | None = Field(default=None, ge=0, le=100)
    friction: int = Field(default=0, ge=0, le=100)
    failure_causes: list[str] = Field(default_factory=list, max_length=20)
    duration_minutes: int = Field(default=0, ge=0, le=1_440)
    checklist_evidence: list[HabitChecklistEvidenceInput] = Field(
        default_factory=list, max_length=50
    )
    source: HabitLogSourceInput | None = None
    recovery_of_log_id: str | None = Field(default=None, max_length=36)
    note: str = Field(default="", max_length=20_000)
    provenance: dict[str, Any] = Field(default_factory=dict)
    confidence: int = Field(default=100, ge=0, le=100)
    sensitivity: str = Field(default="private", max_length=24)
    idempotency_key: str | None = Field(default=None, max_length=256)


class HabitLogUpdate(StrictHabitInput):
    version: int = Field(ge=1)
    result: str | None = Field(default=None, max_length=32)
    logged_at: datetime | None = None
    scheduled_for: date | None = None
    quality: int | None = Field(default=None, ge=0, le=100)
    friction: int | None = Field(default=None, ge=0, le=100)
    failure_causes: list[str] | None = Field(default=None, max_length=20)
    duration_minutes: int | None = Field(default=None, ge=0, le=1_440)
    checklist_evidence: list[HabitChecklistEvidenceInput] | None = Field(
        default=None, max_length=50
    )
    source: HabitLogSourceInput | None = None
    recovery_of_log_id: str | None = Field(default=None, max_length=36)
    note: str | None = Field(default=None, max_length=20_000)
    provenance: dict[str, Any] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    sensitivity: str | None = Field(default=None, max_length=24)


def _fields_set(body: BaseModel) -> set[str]:
    fields = getattr(body, "model_fields_set", None)
    if fields is None:
        fields = getattr(body, "__fields_set__", set())
    return set(fields)


def _model_dict(value: BaseModel | None) -> dict[str, Any] | None:
    if value is None:
        return None
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(exclude_unset=True)
    return value.dict(exclude_unset=True)


def _model_list(values: list[BaseModel] | None) -> list[dict[str, Any]] | None:
    if values is None:
        return None
    return [_model_dict(value) or {} for value in values]


def _raise_domain_error(exc: LifeGraphError) -> None:
    if isinstance(exc, LifeGraphNotFound):
        raise HTTPException(404, str(exc)) from exc
    if isinstance(exc, LifeGraphConflict):
        raise HTTPException(409, str(exc)) from exc
    raise HTTPException(400, str(exc)) from exc


def _typed_knowledge_read_error() -> LifeGraphError:
    return LifeGraphError(
        "Use /api/life/knowledge for typed personal knowledge reads so "
        "citation availability, staleness, and epistemic labels are preserved"
    )


def _generic_read_items(items):
    return [
        item for item in items
        if not is_typed_personal_knowledge_payload(
            item.entity_type, item.properties
        )
    ]


def _generic_search_result(result: dict[str, Any]) -> dict[str, Any]:
    visible = [
        item for item in result.get("items", [])
        if not is_typed_personal_knowledge_payload(
            (item.get("entity") or {}).get("entity_type"),
            (item.get("entity") or {}).get("properties"),
        )
    ]
    return {
        **result,
        "items": visible,
        "count": len(visible),
        "typed_personal_knowledge_excluded": True,
    }


def _generic_graph_result(result: dict[str, Any]) -> dict[str, Any]:
    entities = list(result.get("entities") or [])
    excluded_ids = {
        str(entity.get("id"))
        for entity in entities
        if is_typed_personal_knowledge_payload(
            entity.get("entity_type"), entity.get("properties")
        )
    }
    if str(result.get("root_id")) in excluded_ids:
        raise _typed_knowledge_read_error()
    visible_entities = [
        entity for entity in entities if str(entity.get("id")) not in excluded_ids
    ]
    visible_links = [
        link for link in list(result.get("links") or [])
        if str(link.get("source_id")) not in excluded_ids
        and str(link.get("target_id")) not in excluded_ids
    ]
    return {
        **result,
        "entities": visible_entities,
        "links": visible_links,
        "typed_personal_knowledge_excluded": True,
    }


def setup_life_routes(*, session_factory=SessionLocal) -> APIRouter:
    router = APIRouter(prefix="/api/life", tags=["life"])

    @router.post("/sources", status_code=201)
    def create_source(request: Request, body: LifeSourceCreate) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                if is_typed_personal_knowledge_source_payload(
                    body.source_type, body.metadata
                ):
                    raise LifeGraphError(
                        "Use /api/life/knowledge/sources for typed personal "
                        "knowledge sources"
                    )
                source, created = create_life_source(
                    db,
                    account=account,
                    source_type=body.source_type,
                    title=body.title,
                    source_ref=body.source_ref,
                    safe_excerpt=body.safe_excerpt,
                    content_sha256=body.content_sha256,
                    observed_at=body.observed_at,
                    sensitivity=body.sensitivity,
                    metadata=body.metadata,
                    idempotency_key=body.idempotency_key,
                )
                return {"source": serialize_life_source(source), "created": created}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/sources")
    def list_sources(
        request: Request,
        source_type: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_life_sources(
                    db,
                    owner_id=account.id,
                    source_type=source_type,
                    limit=limit,
                )
                visible = [
                    item for item in items
                    if not is_typed_personal_knowledge_source_payload(
                        item.source_type, item.meta_data
                    )
                ]
                return {
                    "items": [serialize_life_source(item) for item in visible],
                    "count": len(visible),
                    "truncated": truncated,
                    "typed_personal_knowledge_excluded": True,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/entities", status_code=201)
    def create_entity(request: Request, body: LifeEntityCreate) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                if (
                    str(body.entity_type).strip().lower() == "decision"
                    and body.properties.get("decision_schema_version") is not None
                ):
                    raise LifeGraphError(
                        "Use /api/life/decisions for typed Decision records"
                    )
                if is_typed_health_payload(body.entity_type, body.properties):
                    raise LifeGraphError(
                        "Use /api/life/health/records for typed Health records"
                    )
                if is_typed_finance_payload(body.entity_type, body.properties):
                    raise LifeGraphError(
                        "Use /api/life/finance/records for typed Finance records"
                    )
                if is_typed_habit_payload(body.entity_type, body.properties):
                    raise LifeGraphError(
                        "Use /api/life/habits for typed Habit and routine-log records"
                    )
                if is_typed_task_payload(body.entity_type, body.properties):
                    raise LifeGraphError(
                        "Use /api/life/tasks for typed human Task records"
                    )
                if is_typed_home_payload(body.entity_type, body.properties):
                    raise LifeGraphError(
                        "Use /api/life/home/records for typed Home records"
                    )
                if is_typed_relationship_payload(body.entity_type, body.properties):
                    raise LifeGraphError(
                        "Use /api/life/relationships for typed Relationship records"
                    )
                if is_typed_journal_payload(body.entity_type, body.properties):
                    raise LifeGraphError(
                        "Use /api/life/journal/entries for typed Journal records"
                    )
                if is_typed_travel_payload(body.entity_type, body.properties):
                    raise LifeGraphError(
                        "Use /api/life/travel/records for typed Travel records"
                    )
                if is_typed_learning_career_payload(
                    body.entity_type, body.properties
                ):
                    raise LifeGraphError(
                        "Use /api/life/learning-career/records for typed "
                        "Learning/Career records"
                    )
                if is_typed_work_business_payload(
                    body.entity_type, body.properties
                ):
                    raise LifeGraphError(
                        "Use /api/life/work-business for typed Work/Business records"
                    )
                if is_typed_personal_knowledge_payload(
                    body.entity_type, body.properties
                ):
                    raise LifeGraphError(
                        "Use /api/life/knowledge/records for typed personal "
                        "knowledge records"
                    )
                entity, created = create_life_entity(
                    db,
                    account=account,
                    entity_type=body.entity_type,
                    title=body.title,
                    summary=body.summary,
                    status=body.status,
                    properties=body.properties,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    domain_ref_type=body.domain_ref_type,
                    domain_ref_id=body.domain_ref_id,
                    occurred_at=body.occurred_at,
                    due_at=body.due_at,
                    review_at=body.review_at,
                    idempotency_key=body.idempotency_key,
                    reason=body.reason,
                )
                return {"entity": serialize_life_entity(entity), "created": created}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/entities")
    def list_entities(
        request: Request,
        entity_type: str | None = Query(default=None),
        status: str | None = Query(default=None),
        include_deleted: bool = Query(default=False),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_life_entities(
                    db,
                    owner_id=account.id,
                    entity_type=entity_type,
                    status=status,
                    include_deleted=include_deleted,
                    limit=limit,
                )
                visible = _generic_read_items(items)
                return {
                    "items": [serialize_life_entity(item) for item in visible],
                    "count": len(visible),
                    "truncated": truncated,
                    "typed_personal_knowledge_excluded": True,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # Fixed paths precede the dynamic entity routes so they cannot be parsed as
    # entity identifiers by older Starlette/FastAPI route matchers.
    @router.get("/search")
    def search_entities(
        request: Request,
        q: str = Query(min_length=1, max_length=500),
        entity_type: str | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "items": [], "count": 0, "scanned": 0,
                        "truncated": False,
                    }
                return _generic_search_result(search_life_entities(
                    db,
                    owner_id=account.id,
                    query_text=q,
                    entity_type=entity_type,
                    status=status,
                    limit=limit,
                ))
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/decisions/review")
    def decisions_for_review(
        request: Request,
        due_before: datetime | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_decisions_for_review(
                    db,
                    owner_id=account.id,
                    due_before=due_before,
                    limit=limit,
                )
                return {
                    "items": [serialize_life_entity(item) for item in items],
                    "count": len(items),
                    "truncated": truncated,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/decisions", status_code=201)
    def create_typed_decision(
        request: Request, body: DecisionCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_decision(
                    db,
                    account=account,
                    title=body.title,
                    decision_date=body.decision_date,
                    context=body.context,
                    options=_model_list(body.options),
                    chosen_option=body.chosen_option,
                    reasons=body.reasons,
                    risks=body.risks,
                    assumptions=_model_list(body.assumptions),
                    people=body.people,
                    evidence=_model_list(body.evidence),
                    review_at=body.review_at,
                    outcome=_model_dict(body.outcome),
                    linked_entity_ids=body.linked_entity_ids,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    idempotency_key=body.idempotency_key,
                )
                return {
                    "decision": get_decision(
                        db, owner_id=account.id, entity_id=entity.id
                    ),
                    "created": created,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/decisions")
    def list_typed_decisions(
        request: Request,
        status: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_decisions(
                    db, owner_id=account.id, status=status, limit=limit
                )
                return {
                    "items": items,
                    "count": len(items),
                    "truncated": truncated,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/decisions/due")
    def due_typed_decisions(
        request: Request,
        due_before: datetime | None = Query(default=None),
        stale_after_days: int = Query(default=30, ge=1, le=3650),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "items": [], "count": 0, "scanned": 0,
                        "truncated": False,
                    }
                return list_due_decision_reviews(
                    db,
                    owner_id=account.id,
                    due_before=due_before,
                    stale_after_days=stale_after_days,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/decisions/search")
    def search_typed_decisions(
        request: Request,
        q: str = Query(min_length=1, max_length=500),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "items": [], "count": 0, "scanned": 0,
                        "truncated": False,
                    }
                return search_decisions(
                    db, owner_id=account.id, query_text=q, limit=limit
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/decisions/{decision_id}/history")
    def typed_decision_history(
        request: Request,
        decision_id: str,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Decision not found")
                items, truncated = decision_history(
                    db, owner_id=account.id, entity_id=decision_id, limit=limit
                )
                return {
                    "items": items,
                    "count": len(items),
                    "truncated": truncated,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/decisions/{decision_id}/review")
    def review_typed_decision(
        request: Request, decision_id: str, body: DecisionReview
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = review_decision(
                    db,
                    account=account,
                    entity_id=decision_id,
                    expected_version=body.version,
                    summary=body.summary,
                    reviewed_at=body.reviewed_at,
                    assumption_updates=_model_list(body.assumption_updates),
                    outcome=_model_dict(body.outcome),
                    next_review_at=body.next_review_at,
                )
                return {"decision": serialize_decision(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/decisions/{decision_id}")
    def get_typed_decision(
        request: Request, decision_id: str
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Decision not found")
                return {
                    "decision": get_decision(
                        db, owner_id=account.id, entity_id=decision_id
                    )
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.patch("/decisions/{decision_id}")
    def update_typed_decision(
        request: Request, decision_id: str, body: DecisionUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body) - {"version"}
        changes: dict[str, Any] = {}
        for field in fields:
            value = getattr(body, field)
            if field in {"options", "assumptions", "evidence"}:
                value = _model_list(value)
            elif field == "outcome":
                value = _model_dict(value)
            changes[field] = value
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_decision(
                    db,
                    account=account,
                    entity_id=decision_id,
                    expected_version=body.version,
                    changes=changes,
                )
                return {"decision": serialize_decision(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/health/records", status_code=201)
    def create_typed_health_record(
        request: Request, body: HealthRecordCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_health_record(
                    db,
                    account=account,
                    record_type=body.record_type,
                    title=body.title,
                    recorded_at=body.recorded_at,
                    ended_at=body.ended_at,
                    due_at=body.due_at,
                    metrics=_model_list(body.metrics),
                    details=body.details,
                    source=_model_dict(body.source),
                    note=body.note,
                    private_metadata=body.private_metadata,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    idempotency_key=body.idempotency_key,
                )
                return {
                    "record": serialize_health_record(entity),
                    "created": created,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/health/import", status_code=201)
    def import_typed_health_records(
        request: Request, body: HealthImport
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                items: list[dict[str, Any]] = []
                created_count = 0
                for record in body.records:
                    entity, created = create_health_record(
                        db,
                        account=account,
                        record_type=record.record_type,
                        title=record.title,
                        recorded_at=record.recorded_at,
                        ended_at=record.ended_at,
                        due_at=record.due_at,
                        metrics=_model_list(record.metrics),
                        details=record.details,
                        source=_model_dict(record.source),
                        note=record.note,
                        private_metadata=record.private_metadata,
                        provenance=record.provenance,
                        confidence=record.confidence,
                        sensitivity=record.sensitivity,
                        idempotency_key=record.idempotency_key,
                        import_mode=True,
                    )
                    items.append(serialize_health_record(entity))
                    created_count += int(created)
                return {
                    "items": items,
                    "count": len(items),
                    "created_count": created_count,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/health/records")
    def list_typed_health_records(
        request: Request,
        record_type: str | None = Query(default=None),
        source_kind: str | None = Query(default=None),
        from_at: datetime | None = Query(default=None),
        to_at: datetime | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_health_records(
                    db,
                    owner_id=account.id,
                    record_type=record_type,
                    source_kind=source_kind,
                    from_at=from_at,
                    to_at=to_at,
                    limit=limit,
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/health/search")
    def search_typed_health_records(
        request: Request,
        q: str = Query(min_length=1, max_length=500),
        record_type: str | None = Query(default=None),
        source_kind: str | None = Query(default=None),
        from_at: datetime | None = Query(default=None),
        to_at: datetime | None = Query(default=None),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "items": [], "count": 0, "scanned": 0,
                        "truncated": False,
                    }
                return search_health_records(
                    db,
                    owner_id=account.id,
                    query_text=q,
                    record_type=record_type,
                    source_kind=source_kind,
                    from_at=from_at,
                    to_at=to_at,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/health/trends")
    def typed_health_trends(
        request: Request,
        record_type: str = Query(max_length=64),
        metric: str = Query(max_length=64),
        group_by: str = Query(default="day", max_length=16),
        unit: str | None = Query(default=None, max_length=32),
        from_at: datetime | None = Query(default=None),
        to_at: datetime | None = Query(default=None),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "record_type": record_type,
                        "metric": metric,
                        "group_by": group_by,
                        "unit": unit,
                        "buckets": [],
                        "count": 0,
                        "scanned": 0,
                        "truncated": False,
                    }
                return health_trends(
                    db,
                    owner_id=account.id,
                    record_type=record_type,
                    metric=metric,
                    group_by=group_by,
                    unit=unit,
                    from_at=from_at,
                    to_at=to_at,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/health/records/{record_id}/history")
    def typed_health_record_history(
        request: Request,
        record_id: str,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Health record not found")
                items, truncated = health_record_history(
                    db, owner_id=account.id, entity_id=record_id, limit=limit
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/health/records/{record_id}")
    def get_typed_health_record(
        request: Request, record_id: str
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Health record not found")
                return {
                    "record": get_health_record(
                        db, owner_id=account.id, entity_id=record_id
                    )
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.patch("/health/records/{record_id}")
    def update_typed_health_record(
        request: Request, record_id: str, body: HealthRecordUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body) - {"version"}
        changes: dict[str, Any] = {}
        for field in fields:
            value = getattr(body, field)
            if field == "metrics":
                value = _model_list(value)
            elif field == "source":
                value = _model_dict(value)
            changes[field] = value
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_health_record(
                    db,
                    account=account,
                    entity_id=record_id,
                    expected_version=body.version,
                    changes=changes,
                )
                return {"record": serialize_health_record(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/health/records/{record_id}")
    def remove_typed_health_record(
        request: Request, record_id: str, body: VersionedDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = delete_health_record(
                    db,
                    owner_id=account.id,
                    entity_id=record_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {"record": serialize_health_record(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/finance/records", status_code=201)
    def create_typed_finance_record(
        request: Request, body: FinanceRecordCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_finance_record(
                    db,
                    account=account,
                    record_type=body.record_type,
                    title=body.title,
                    scope=body.scope,
                    effective_at=body.effective_at,
                    source=_model_dict(body.source),
                    amount=body.amount,
                    currency=body.currency,
                    unit=body.unit,
                    due_at=body.due_at,
                    period_start=body.period_start,
                    period_end=body.period_end,
                    details=body.details,
                    note=body.note,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    idempotency_key=body.idempotency_key,
                )
                return {"record": serialize_finance_record(entity), "created": created}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/finance/import", status_code=201)
    def import_typed_finance_records(
        request: Request, body: FinanceImport
    ) -> dict[str, Any]:
        records = [_model_dict(record) or {} for record in body.records]
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entities, created_count = import_finance_records(
                    db, account=account, records=records
                )
                return {
                    "items": [serialize_finance_record(entity) for entity in entities],
                    "count": len(entities),
                    "created_count": created_count,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/finance/records")
    def list_typed_finance_records(
        request: Request,
        record_type: str | None = Query(default=None, max_length=64),
        scope: str | None = Query(default=None, max_length=24),
        currency: str | None = Query(default=None, max_length=3),
        from_at: datetime | None = Query(default=None),
        to_at: datetime | None = Query(default=None),
        include_archived: bool = Query(default=False),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_finance_records(
                    db,
                    owner_id=account.id,
                    record_type=record_type,
                    scope=scope,
                    currency=currency,
                    from_at=from_at,
                    to_at=to_at,
                    include_archived=include_archived,
                    limit=limit,
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/finance/search")
    def search_typed_finance_records(
        request: Request,
        q: str = Query(min_length=1, max_length=500),
        record_type: str | None = Query(default=None, max_length=64),
        scope: str | None = Query(default=None, max_length=24),
        currency: str | None = Query(default=None, max_length=3),
        from_at: datetime | None = Query(default=None),
        to_at: datetime | None = Query(default=None),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "scanned": 0, "truncated": False}
                return search_finance_records(
                    db, owner_id=account.id, query_text=q, record_type=record_type,
                    scope=scope, currency=currency, from_at=from_at, to_at=to_at,
                    limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/finance/summary")
    def typed_finance_summary(
        request: Request,
        scope: str | None = Query(default=None, max_length=24),
        from_at: datetime | None = Query(default=None),
        to_at: datetime | None = Query(default=None),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"record_counts": {}, "count": 0, "truncated": False}
                return finance_summary(
                    db, owner_id=account.id, scope=scope, from_at=from_at, to_at=to_at
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/finance/cash-flow")
    def typed_finance_cash_flow(
        request: Request,
        scope: str | None = Query(default=None, max_length=24),
        currency: str | None = Query(default=None, max_length=3),
        group_by: str = Query(default="month", max_length=16),
        from_at: datetime | None = Query(default=None),
        to_at: datetime | None = Query(default=None),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"buckets": [], "count": 0, "scanned": 0, "truncated": False}
                return finance_cash_flow(
                    db, owner_id=account.id, scope=scope, currency=currency,
                    group_by=group_by, from_at=from_at, to_at=to_at,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/finance/net-worth")
    def typed_finance_net_worth(
        request: Request,
        as_of: datetime = Query(),
        scope: str | None = Query(default=None, max_length=24),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "totals": {}, "evidence": [], "evidence_count": 0,
                        "truncated": False, "exchange_rates_used": False,
                    }
                return finance_net_worth(
                    db, owner_id=account.id, as_of=as_of, scope=scope,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/finance/forecast")
    def typed_finance_forecast(
        request: Request,
        as_of: datetime = Query(),
        scope: str | None = Query(default=None, max_length=24),
        currency: str | None = Query(default=None, max_length=3),
        horizon_days: int = Query(default=30, ge=1, le=365),
        lookback_days: int = Query(default=90, ge=7, le=730),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"projections": {}, "truncated": False}
                return finance_forecast(
                    db, owner_id=account.id, as_of=as_of, scope=scope,
                    currency=currency, horizon_days=horizon_days,
                    lookback_days=lookback_days,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/finance/affordability")
    def typed_finance_affordability(
        request: Request,
        as_of: datetime = Query(),
        amount: str = Query(min_length=1, max_length=32),
        currency: str = Query(min_length=3, max_length=3),
        scope: str | None = Query(default=None, max_length=24),
        horizon_days: int = Query(default=30, ge=1, le=365),
        lookback_days: int = Query(default=90, ge=7, le=730),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "status": "insufficient_evidence",
                        "can_execute_purchase_or_transfer": False,
                        "truncated": False,
                    }
                return finance_affordability(
                    db, owner_id=account.id, as_of=as_of, amount=amount,
                    currency=currency, scope=scope, horizon_days=horizon_days,
                    lookback_days=lookback_days,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/finance/subscriptions")
    def typed_finance_subscriptions(
        request: Request,
        scope: str | None = Query(default=None, max_length=24),
        status: str | None = Query(default=None, max_length=24),
        currency: str | None = Query(default=None, max_length=3),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "totals": {}, "truncated": False}
                return list_subscriptions(
                    db, owner_id=account.id, scope=scope, status=status,
                    currency=currency, limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/finance/due")
    def typed_finance_due(
        request: Request,
        scope: str | None = Query(default=None, max_length=24),
        due_before: datetime | None = Query(default=None),
        include_overdue: bool = Query(default=True),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                return list_due_finance_records(
                    db, owner_id=account.id, scope=scope, due_before=due_before,
                    include_overdue=include_overdue, limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/finance/anomaly-input")
    def typed_finance_anomaly_input(
        request: Request,
        scope: str | None = Query(default=None, max_length=24),
        currency: str | None = Query(default=None, max_length=3),
        from_at: datetime | None = Query(default=None),
        to_at: datetime | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"signals": [], "count": 0, "scanned": 0, "truncated": False}
                return finance_anomaly_input(
                    db, owner_id=account.id, scope=scope, currency=currency,
                    from_at=from_at, to_at=to_at, limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/finance/records/{record_id}/history")
    def typed_finance_record_history(
        request: Request, record_id: str,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Finance record not found")
                items, truncated = finance_record_history(
                    db, owner_id=account.id, entity_id=record_id, limit=limit
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/finance/records/{record_id}")
    def get_typed_finance_record(
        request: Request, record_id: str
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Finance record not found")
                return {"record": get_finance_record(
                    db, owner_id=account.id, entity_id=record_id
                )}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.patch("/finance/records/{record_id}")
    def update_typed_finance_record(
        request: Request, record_id: str, body: FinanceRecordUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body) - {"version"}
        changes: dict[str, Any] = {}
        for field in fields:
            value = getattr(body, field)
            if field == "source":
                value = _model_dict(value)
            changes[field] = value
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_finance_record(
                    db, account=account, entity_id=record_id,
                    expected_version=body.version, changes=changes,
                )
                return {"record": serialize_finance_record(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/finance/records/{record_id}")
    def remove_typed_finance_record(
        request: Request, record_id: str, body: VersionedDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = delete_finance_record(
                    db, owner_id=account.id, entity_id=record_id,
                    expected_version=body.version, reason=body.reason,
                )
                return {"record": serialize_finance_record(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/habits", status_code=201)
    def create_typed_habit(
        request: Request, body: HabitCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_habit(
                    db,
                    account=account,
                    title=body.title,
                    routine_type=body.routine_type,
                    schedule=_model_dict(body.schedule),
                    triggers=_model_list(body.triggers),
                    checklist=_model_list(body.checklist),
                    contexts=body.contexts,
                    duration_minutes=body.duration_minutes,
                    minimum_viable=_model_dict(body.minimum_viable),
                    recovery_rules=_model_dict(body.recovery_rules),
                    note=body.note,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    idempotency_key=body.idempotency_key,
                )
                return {"habit": serialize_habit(entity), "created": created}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/habits")
    def list_typed_habits(
        request: Request,
        routine_type: str | None = Query(default=None, max_length=64),
        status: str | None = Query(default=None, max_length=32),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_habits(
                    db, owner_id=account.id, routine_type=routine_type,
                    status=status, limit=limit,
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/habits/search")
    def search_typed_habits(
        request: Request,
        q: str = Query(min_length=1, max_length=500),
        routine_type: str | None = Query(default=None, max_length=64),
        status: str | None = Query(default=None, max_length=32),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "items": [], "count": 0, "scanned": 0,
                        "truncated": False,
                    }
                return search_habits(
                    db, owner_id=account.id, query_text=q,
                    routine_type=routine_type, status=status, limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/habits/reports/weekly")
    def typed_habit_weekly_report(
        request: Request,
        week_start: date = Query(),
        habit_id: str | None = Query(default=None, max_length=36),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "week_start": week_start.isoformat(), "items": [],
                        "count": 0, "overall_consistency_percent": 0.0,
                        "truncated": False,
                    }
                return weekly_habit_report(
                    db, owner_id=account.id, week_start=week_start,
                    habit_id=habit_id,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/habits/reports/missed")
    def typed_missed_routine_report(
        request: Request,
        as_of: datetime = Query(),
        lookback_days: int = Query(default=14, ge=1, le=366),
        habit_id: str | None = Query(default=None, max_length=36),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "as_of": as_of.isoformat(), "lookback_days": lookback_days,
                        "items": [], "count": 0, "truncated": False,
                    }
                return missed_routine_report(
                    db, owner_id=account.id, as_of=as_of,
                    lookback_days=lookback_days, habit_id=habit_id,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/habits/reports/weekly-adjustment")
    def typed_habit_weekly_adjustment(
        request: Request,
        week_start: date = Query(),
        habit_id: str | None = Query(default=None, max_length=36),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "week_start": week_start.isoformat(), "items": [],
                        "count": 0, "truncated": False,
                        "execution_policy": {
                            "record_only": True, "can_apply_automatically": False,
                        },
                    }
                return weekly_adjustment_report(
                    db, owner_id=account.id, week_start=week_start,
                    habit_id=habit_id,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/habits/logs", status_code=201)
    def create_typed_habit_log(
        request: Request, body: HabitLogCreate
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity, created = create_habit_log(
                    db,
                    account=account,
                    habit_id=body.habit_id,
                    result=body.result,
                    logged_at=body.logged_at,
                    scheduled_for=body.scheduled_for,
                    quality=body.quality,
                    friction=body.friction,
                    failure_causes=body.failure_causes,
                    duration_minutes=body.duration_minutes,
                    checklist_evidence=_model_list(body.checklist_evidence),
                    source=_model_dict(body.source),
                    recovery_of_log_id=body.recovery_of_log_id,
                    note=body.note,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    idempotency_key=body.idempotency_key,
                )
                habit = get_life_entity(
                    db, owner_id=account.id,
                    entity_id=entity.properties["habit_id"],
                    include_deleted=True,
                )
                return {
                    "log": serialize_habit_log(entity, habit=habit),
                    "created": created,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/habits/logs")
    def list_typed_habit_logs(
        request: Request,
        habit_id: str | None = Query(default=None, max_length=36),
        result: str | None = Query(default=None, max_length=32),
        from_date: date | None = Query(default=None),
        to_date: date | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {"items": [], "count": 0, "truncated": False}
                items, truncated = list_habit_logs(
                    db, owner_id=account.id, habit_id=habit_id, result=result,
                    from_date=from_date, to_date=to_date, limit=limit,
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/habits/logs/search")
    def search_typed_habit_logs(
        request: Request,
        q: str = Query(min_length=1, max_length=500),
        habit_id: str | None = Query(default=None, max_length=36),
        limit: int = Query(default=25, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "items": [], "count": 0, "scanned": 0,
                        "truncated": False,
                    }
                return search_habit_logs(
                    db, owner_id=account.id, query_text=q,
                    habit_id=habit_id, limit=limit,
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/habits/logs/{log_id}/history")
    def typed_habit_log_history(
        request: Request, log_id: str,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Habit log not found")
                items, truncated = habit_log_history(
                    db, owner_id=account.id, entity_id=log_id, limit=limit
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/habits/logs/{log_id}")
    def get_typed_habit_log(
        request: Request, log_id: str
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Habit log not found")
                return {"log": get_habit_log(db, owner_id=account.id, entity_id=log_id)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.patch("/habits/logs/{log_id}")
    def update_typed_habit_log(
        request: Request, log_id: str, body: HabitLogUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body) - {"version"}
        changes: dict[str, Any] = {}
        for field in fields:
            value = getattr(body, field)
            if field == "checklist_evidence":
                value = _model_list(value)
            elif field == "source":
                value = _model_dict(value)
            changes[field] = value
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_habit_log(
                    db, account=account, entity_id=log_id,
                    expected_version=body.version, changes=changes,
                )
                habit = get_life_entity(
                    db, owner_id=account.id,
                    entity_id=entity.properties["habit_id"],
                    include_deleted=True,
                )
                return {"log": serialize_habit_log(entity, habit=habit)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/habits/logs/{log_id}")
    def remove_typed_habit_log(
        request: Request, log_id: str, body: VersionedDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = delete_habit_log(
                    db, owner_id=account.id, entity_id=log_id,
                    expected_version=body.version, reason=body.reason,
                )
                habit = get_life_entity(
                    db, owner_id=account.id,
                    entity_id=entity.properties["habit_id"],
                    include_deleted=True,
                )
                return {"log": serialize_habit_log(entity, habit=habit)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/habits/{habit_id}/history")
    def typed_habit_history(
        request: Request, habit_id: str,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Habit not found")
                items, truncated = habit_history(
                    db, owner_id=account.id, entity_id=habit_id, limit=limit
                )
                return {"items": items, "count": len(items), "truncated": truncated}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/habits/{habit_id}")
    def get_typed_habit(
        request: Request, habit_id: str
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Habit not found")
                return {"habit": get_habit(db, owner_id=account.id, entity_id=habit_id)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.patch("/habits/{habit_id}")
    def update_typed_habit(
        request: Request, habit_id: str, body: HabitUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body) - {"version"}
        changes: dict[str, Any] = {}
        for field in fields:
            value = getattr(body, field)
            if field in {"triggers", "checklist"}:
                value = _model_list(value)
            elif field in {"schedule", "minimum_viable", "recovery_rules"}:
                value = _model_dict(value)
            changes[field] = value
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = update_habit(
                    db, account=account, entity_id=habit_id,
                    expected_version=body.version, changes=changes,
                )
                return {"habit": serialize_habit(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/habits/{habit_id}")
    def remove_typed_habit(
        request: Request, habit_id: str, body: VersionedDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                entity = delete_habit(
                    db, owner_id=account.id, entity_id=habit_id,
                    expected_version=body.version, reason=body.reason,
                )
                return {"habit": serialize_habit(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/tasks/quality")
    def task_quality(
        request: Request,
        at: datetime | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    return {
                        "items": [], "count": 0, "scanned": 0,
                        "flag_counts": {}, "truncated": False,
                    }
                return task_quality_report(
                    db, owner_id=account.id, now=at, limit=limit
                )
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.post("/links", status_code=201)
    def create_link(request: Request, body: EntityLinkCreate) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                if generic_link_requires_work_business_route(
                    db,
                    owner_id=account.id,
                    source_id=body.source_id,
                    target_id=body.target_id,
                    metadata=body.metadata,
                ):
                    raise LifeGraphError(
                        "Use /api/life/work-business/relations for explicit "
                        "cross-workspace relations"
                    )
                link, created = create_entity_link(
                    db,
                    account=account,
                    source_id=body.source_id,
                    relation=body.relation,
                    target_id=body.target_id,
                    metadata=body.metadata,
                    provenance=body.provenance,
                    confidence=body.confidence,
                    sensitivity=body.sensitivity,
                    reason=body.reason,
                )
                return {"link": serialize_entity_link(link), "created": created}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/links")
    def list_links(
        request: Request,
        entity_id: str = Query(max_length=36),
        direction: str = Query(default="both"),
        relation: str | None = Query(default=None),
        include_deleted: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Life entity not found")
                root = get_life_entity(
                    db, owner_id=account.id, entity_id=entity_id
                )
                if is_typed_personal_knowledge_payload(
                    root.entity_type, root.properties
                ):
                    raise _typed_knowledge_read_error()
                items, truncated = list_entity_links(
                    db,
                    owner_id=account.id,
                    entity_id=entity_id,
                    direction=direction,
                    relation=relation,
                    include_deleted=include_deleted,
                    limit=limit,
                )
                return {
                    "items": [serialize_entity_link(item) for item in items],
                    "count": len(items),
                    "truncated": truncated,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/links/{link_id}")
    def remove_link(
        request: Request, link_id: str, body: EntityLinkDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                if is_work_business_relation_id(
                    db, owner_id=account.id, relation_id=link_id
                ):
                    raise LifeGraphError(
                        "Use /api/life/work-business/relations/{relation_id} "
                        "to delete a cross-workspace relation"
                    )
                link = delete_entity_link(
                    db,
                    owner_id=account.id,
                    link_id=link_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {"link": serialize_entity_link(link)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/entities/{entity_id}/graph")
    def entity_graph(
        request: Request,
        entity_id: str,
        depth: int = Query(default=2, ge=1, le=8),
        limit: int = Query(default=100, ge=1, le=200),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Life entity not found")
                return _generic_graph_result(traverse_life_graph(
                    db,
                    owner_id=account.id,
                    entity_id=entity_id,
                    depth=depth,
                    limit=limit,
                ))
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/entities/{entity_id}/versions")
    def entity_versions(
        request: Request,
        entity_id: str,
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Life entity not found")
                entity = get_life_entity(
                    db, owner_id=account.id, entity_id=entity_id,
                    include_deleted=True,
                )
                if is_typed_personal_knowledge_payload(
                    entity.entity_type, entity.properties
                ):
                    raise _typed_knowledge_read_error()
                items, truncated = list_life_entity_versions(
                    db, owner_id=account.id, entity_id=entity_id, limit=limit
                )
                return {
                    "items": [
                        serialize_life_entity_version(item) for item in items
                    ],
                    "count": len(items),
                    "truncated": truncated,
                }
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.get("/entities/{entity_id}")
    def get_entity(
        request: Request,
        entity_id: str,
        include_deleted: bool = Query(default=False),
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:read",), write=False
            ) as account:
                if account is None:
                    raise LifeGraphNotFound("Life entity not found")
                entity = get_life_entity(
                    db,
                    owner_id=account.id,
                    entity_id=entity_id,
                    include_deleted=include_deleted,
                )
                if is_typed_personal_knowledge_payload(
                    entity.entity_type, entity.properties
                ):
                    raise _typed_knowledge_read_error()
                return {"entity": serialize_life_entity(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.patch("/entities/{entity_id}")
    def update_entity(
        request: Request, entity_id: str, body: LifeEntityUpdate
    ) -> dict[str, Any]:
        fields = _fields_set(body)
        changes = {
            field: getattr(body, field)
            for field in (
                "title", "summary", "status", "properties", "provenance",
                "confidence", "sensitivity", "occurred_at", "due_at", "review_at",
            )
            if field in fields
        }
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                existing = get_life_entity(
                    db, owner_id=account.id, entity_id=entity_id
                )
                if (
                    existing.entity_type == "decision"
                    and (
                        (existing.properties or {}).get("decision_schema_version") == 1
                        or (
                            isinstance(changes.get("properties"), dict)
                            and changes["properties"].get("decision_schema_version")
                            is not None
                        )
                    )
                ):
                    raise LifeGraphError(
                        "Use /api/life/decisions/{decision_id} to update a typed Decision"
                    )
                if (
                    is_typed_health_payload(existing.entity_type, existing.properties)
                    or is_typed_health_payload(
                        existing.entity_type, changes.get("properties")
                    )
                ):
                    raise LifeGraphError(
                        "Use /api/life/health/records/{record_id} to update a typed Health record"
                    )
                if (
                    is_typed_finance_payload(existing.entity_type, existing.properties)
                    or is_typed_finance_payload(
                        existing.entity_type, changes.get("properties")
                    )
                ):
                    raise LifeGraphError(
                        "Use /api/life/finance/records/{record_id} to update a typed Finance record"
                    )
                if (
                    is_typed_habit_payload(existing.entity_type, existing.properties)
                    or is_typed_habit_payload(
                        existing.entity_type, changes.get("properties")
                    )
                ):
                    raise LifeGraphError(
                        "Use /api/life/habits to update typed Habit and routine-log records"
                    )
                if (
                    is_typed_task_payload(existing.entity_type, existing.properties)
                    or is_typed_task_payload(
                        existing.entity_type, changes.get("properties")
                    )
                ):
                    raise LifeGraphError(
                        "Use /api/life/tasks/{task_id} to update a typed human Task"
                    )
                if (
                    is_typed_home_payload(existing.entity_type, existing.properties)
                    or is_typed_home_payload(
                        existing.entity_type, changes.get("properties")
                    )
                ):
                    raise LifeGraphError(
                        "Use /api/life/home/records/{record_id} to update a typed Home record"
                    )
                if (
                    is_typed_relationship_payload(
                        existing.entity_type, existing.properties
                    )
                    or is_typed_relationship_payload(
                        existing.entity_type, changes.get("properties")
                    )
                ):
                    raise LifeGraphError(
                        "Use /api/life/relationships to update typed Relationship records"
                    )
                if (
                    is_typed_journal_payload(existing.entity_type, existing.properties)
                    or is_typed_journal_payload(
                        existing.entity_type, changes.get("properties")
                    )
                ):
                    raise LifeGraphError(
                        "Use /api/life/journal/entries/{entry_id} to update a typed Journal record"
                    )
                if (
                    is_typed_travel_payload(existing.entity_type, existing.properties)
                    or is_typed_travel_payload(
                        existing.entity_type, changes.get("properties")
                    )
                ):
                    raise LifeGraphError(
                        "Use /api/life/travel/records/{record_id} to update a typed Travel record"
                    )
                if (
                    is_typed_learning_career_payload(
                        existing.entity_type, existing.properties
                    )
                    or is_typed_learning_career_payload(
                        existing.entity_type, changes.get("properties")
                    )
                ):
                    raise LifeGraphError(
                        "Use /api/life/learning-career/records/{record_id} to update "
                        "a typed Learning/Career record"
                    )
                if (
                    is_typed_work_business_payload(
                        existing.entity_type, existing.properties
                    )
                    or is_typed_work_business_payload(
                        existing.entity_type, changes.get("properties")
                    )
                ):
                    raise LifeGraphError(
                        "Use /api/life/work-business to update a typed "
                        "Work/Business record"
                    )
                if (
                    is_typed_personal_knowledge_payload(
                        existing.entity_type, existing.properties
                    )
                    or is_typed_personal_knowledge_payload(
                        existing.entity_type, changes.get("properties")
                    )
                ):
                    raise LifeGraphError(
                        "Use /api/life/knowledge/records/{record_id} to update "
                        "a typed personal knowledge record"
                    )
                entity = update_life_entity(
                    db,
                    owner_id=account.id,
                    entity_id=entity_id,
                    expected_version=body.version,
                    changes=changes,
                    reason=body.reason,
                )
                return {"entity": serialize_life_entity(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @router.delete("/entities/{entity_id}")
    def remove_entity(
        request: Request, entity_id: str, body: VersionedDelete
    ) -> dict[str, Any]:
        db = session_factory()
        try:
            with request_account_transaction(
                db, request, required_scopes=("life:write",), write=True
            ) as account:
                existing = get_life_entity(
                    db, owner_id=account.id, entity_id=entity_id
                )
                if is_typed_health_payload(
                    existing.entity_type, existing.properties
                ):
                    raise LifeGraphError(
                        "Use /api/life/health/records/{record_id} to delete a typed Health record"
                    )
                if is_typed_finance_payload(
                    existing.entity_type, existing.properties
                ):
                    raise LifeGraphError(
                        "Use /api/life/finance/records/{record_id} to delete a typed Finance record"
                    )
                if is_typed_habit_payload(
                    existing.entity_type, existing.properties
                ):
                    raise LifeGraphError(
                        "Use /api/life/habits to delete typed Habit and routine-log records"
                    )
                if is_typed_task_payload(
                    existing.entity_type, existing.properties
                ):
                    raise LifeGraphError(
                        "Use /api/life/tasks/{task_id} to delete a typed human Task"
                    )
                if is_typed_home_payload(
                    existing.entity_type, existing.properties
                ):
                    raise LifeGraphError(
                        "Use /api/life/home/records/{record_id} to delete a typed Home record"
                    )
                if is_typed_relationship_payload(
                    existing.entity_type, existing.properties
                ):
                    raise LifeGraphError(
                        "Typed Relationship records cannot be deleted through the generic Life API"
                    )
                if is_typed_journal_payload(
                    existing.entity_type, existing.properties
                ):
                    raise LifeGraphError(
                        "Use /api/life/journal/entries/{entry_id} to delete a typed Journal record"
                    )
                if is_typed_travel_payload(
                    existing.entity_type, existing.properties
                ):
                    raise LifeGraphError(
                        "Use /api/life/travel/records/{record_id} to delete a typed Travel record"
                    )
                if is_typed_learning_career_payload(
                    existing.entity_type, existing.properties
                ):
                    raise LifeGraphError(
                        "Use /api/life/learning-career/records/{record_id} to delete "
                        "a typed Learning/Career record"
                    )
                if is_typed_work_business_payload(
                    existing.entity_type, existing.properties
                ):
                    raise LifeGraphError(
                        "Use /api/life/work-business to delete a typed "
                        "Work/Business record"
                    )
                if is_typed_personal_knowledge_payload(
                    existing.entity_type, existing.properties
                ):
                    raise LifeGraphError(
                        "Use /api/life/knowledge/records/{record_id} to delete "
                        "a typed personal knowledge record"
                    )
                entity = delete_life_entity(
                    db,
                    owner_id=account.id,
                    entity_id=entity_id,
                    expected_version=body.version,
                    reason=body.reason,
                )
                return {"entity": serialize_life_entity(entity)}
        except LifeGraphError as exc:
            db.rollback()
            _raise_domain_error(exc)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return router
