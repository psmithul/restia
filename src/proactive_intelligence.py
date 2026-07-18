"""Deterministic, owner-scoped proactive intelligence for Restia V3.

This module is a read model over canonical Life entities.  It deliberately has
no route, scheduler, model call, action proposal, mutation, or delivery surface.
Every finding is traceable to stored facts and an explicit, offset-aware
``as_of``.  Findings that depend on the *absence* of evidence are withheld when
their underlying bounded scan is truncated.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import case

from core.database import EntityLink, LifeEntity, LifeEntityVersion, LifeSource
from src.decision_service import (
    DECISION_SCHEMA_VERSION,
    validate_decision_properties,
)
from src.finance_service import (
    FINANCE_SCHEMA_VERSION,
    validate_finance_properties,
)
from src.habit_service import (
    HABIT_LOG_ENTITY_TYPE,
    HABIT_SCHEMA_VERSION,
    expected_routine_dates,
    validate_habit_log_properties,
    validate_habit_properties,
)
from src.health_service import (
    HEALTH_SCHEMA_VERSION,
    health_safety_notice,
    validate_health_properties,
)
from src.home_service import HOME_SCHEMA_VERSION, validate_home_properties
from src.life_graph import LifeGraphError
from src.relationship_service import (
    RELATIONSHIP_SCHEMA_VERSION,
    serialize_relationship_record,
    validate_profile_properties,
)
from src.task_record_service import TASK_SCHEMA_VERSION, validate_task_properties


PROACTIVE_SCHEMA_VERSION = 1
DEFAULT_SCAN_LIMIT = 500
MAX_SCAN_LIMIT = 500
MAX_LINK_SCAN_LIMIT = 2_000
MAX_VERSION_SCAN_LIMIT = 2_000
MAX_OUTPUT_LIMIT = 200
MAX_RAW_SIGNALS = 2_000
MAX_HORIZON_DAYS = 90
MAX_LOOKBACK_DAYS = 366

_TERMINAL_WORK_STATUSES = frozenset({
    "completed", "done", "cancelled", "canceled", "archived", "deleted",
    "superseded", "obsolete",
})
_TERMINAL_DECISION_STATUSES = frozenset({
    "superseded", "cancelled", "canceled", "archived", "deleted",
})
_GOAL_RELATIONS = frozenset({
    "supports", "belongs_to", "part_of", "advances", "serves", "goal",
})
_CONFLICT_RELATIONS = frozenset({
    "conflicts_with", "overlaps_with", "competes_with",
})
_SCHEDULE_RELATIONS = frozenset({
    "scheduled_as", "scheduled_for", "on_calendar", "calendar_event",
})
_RELATIONSHIP_TYPES = frozenset({"commitment", "reminder"})
_FINANCE_DUE_TYPES = frozenset({
    "subscription", "loan", "tax_item", "bill", "receivable",
})
_FINANCE_STATUS_FIELD = {
    "subscription": "subscription_status",
    "loan": "loan_status",
    "tax_item": "tax_status",
    "bill": "bill_status",
    "receivable": "receivable_status",
}
_FINANCE_TERMINAL = {
    "subscription": frozenset({"cancelled", "expired"}),
    "loan": frozenset({"paid", "closed"}),
    "tax_item": frozenset({"paid", "closed"}),
    "bill": frozenset({"paid", "voided"}),
    "receivable": frozenset({"received", "voided"}),
}
# Mirrors Home's record-specific terminal policy without importing a private
# helper.  "expired" stays reportable because an expiry itself is useful input.
_HOME_TERMINAL = {
    "identity_document": frozenset({"replaced", "revoked"}),
    "insurance": frozenset({"cancelled"}),
    "warranty": frozenset({"claimed", "cancelled"}),
    "renewal": frozenset({"completed", "cancelled"}),
    "inventory_item": frozenset({"disposed", "lost", "donated"}),
    "repair": frozenset({"completed", "cancelled"}),
    "purchase": frozenset({"received", "returned", "cancelled"}),
    "delivery": frozenset({"delivered", "cancelled"}),
    "vehicle": frozenset({"sold", "inactive"}),
    "travel_document": frozenset({"replaced", "revoked"}),
    "form": frozenset({"submitted", "completed", "cancelled"}),
    "provider": frozenset({"inactive"}),
    "household_routine": frozenset({"paused", "completed", "cancelled"}),
    "emergency_information": frozenset({"superseded", "archived"}),
}
_HABIT_RESULT_PRIORITY = {
    "recovered": 5,
    "completed": 4,
    "partial": 3,
    "skipped": 2,
    "missed": 1,
}


class ProactiveIntelligenceError(ValueError):
    """Base class for controlled proactive read-model failures."""


class ProactiveInputError(ProactiveIntelligenceError):
    """The caller supplied an invalid explicit report boundary."""


class ProactiveStateError(ProactiveIntelligenceError):
    """Canonical owner state is malformed, ambiguous, or cross-principal."""


def _bounded_int(
    value: object,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool):
        raise ProactiveInputError(
            f"{field} must be an integer from {minimum} to {maximum}"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ProactiveInputError(
            f"{field} must be an integer from {minimum} to {maximum}"
        ) from exc
    if parsed < minimum or parsed > maximum:
        raise ProactiveInputError(
            f"{field} must be an integer from {minimum} to {maximum}"
        )
    return parsed


def _aware_datetime(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ProactiveInputError(
                f"{field} must be an offset-aware ISO-8601 datetime"
            ) from exc
    else:
        raise ProactiveInputError(
            f"{field} must be an offset-aware ISO-8601 datetime"
        )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProactiveInputError(
            f"{field} must include an explicit UTC offset or timezone"
        )
    return parsed


def _stored_datetime(value: object | None, *, field: str) -> datetime | None:
    """Parse canonical stored time to naive UTC, failing closed if malformed."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ProactiveStateError(f"Malformed {field}") from exc
    else:
        raise ProactiveStateError(f"Malformed {field}")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProactiveStateError(f"Malformed {field}: expected an object")
    return value


def _domain_error(domain: str, entity: LifeEntity, exc: Exception) -> ProactiveStateError:
    return ProactiveStateError(
        f"Malformed {domain} state for entity {entity.id}: {exc}"
    )


def _scan_entities(
    db,
    *,
    owner_id: str,
    entity_types: Sequence[str],
    limit: int,
    order: str = "due",
) -> tuple[list[LifeEntity], bool]:
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type.in_(tuple(entity_types)),
        LifeEntity.deleted_at.is_(None),
    )
    if order == "occurred":
        query = query.order_by(
            LifeEntity.occurred_at.desc(),
            LifeEntity.updated_at.desc(),
            LifeEntity.id.asc(),
        )
    elif order == "updated":
        query = query.order_by(LifeEntity.updated_at.desc(), LifeEntity.id.asc())
    else:
        due_rank = case((LifeEntity.due_at.is_(None), 1), else_=0)
        query = query.order_by(
            due_rank.asc(),
            LifeEntity.due_at.asc(),
            LifeEntity.updated_at.desc(),
            LifeEntity.id.asc(),
        )
    rows = query.limit(limit + 1).all()
    return rows[:limit], len(rows) > limit


def _load_links(
    db,
    *,
    owner_id: str,
    limit: int,
    entity_map: dict[str, LifeEntity],
) -> tuple[list[EntityLink], bool]:
    rows = db.query(EntityLink).filter(
        EntityLink.owner_id == owner_id,
        EntityLink.source_type == "life_entity",
        EntityLink.target_type == "life_entity",
        EntityLink.deleted_at.is_(None),
    ).order_by(EntityLink.created_at.asc(), EntityLink.id.asc()).limit(
        limit + 1
    ).all()
    truncated = len(rows) > limit
    rows = rows[:limit]
    endpoint_ids = {
        endpoint
        for row in rows
        for endpoint in (str(row.source_id), str(row.target_id))
    }
    endpoints = {
        row.id: row
        for row in db.query(LifeEntity).filter(LifeEntity.id.in_(endpoint_ids)).all()
    } if endpoint_ids else {}
    for endpoint_id in sorted(endpoint_ids):
        endpoint = endpoints.get(endpoint_id)
        if endpoint is None:
            raise ProactiveStateError(
                f"Owner link references missing Life entity {endpoint_id}"
            )
        if endpoint.owner_id != owner_id:
            raise ProactiveStateError(
                "Owner link crosses the immutable Account.id boundary"
            )
    active: list[EntityLink] = []
    for row in rows:
        source = endpoints[str(row.source_id)]
        target = endpoints[str(row.target_id)]
        if source.deleted_at is not None or target.deleted_at is not None:
            continue
        active.append(row)
        entity_map[source.id] = source
        entity_map[target.id] = target
    return active, truncated


def _load_owned_entity_refs(
    db,
    *,
    owner_id: str,
    entity_ids: Iterable[str],
    entity_map: dict[str, LifeEntity],
    field: str,
) -> None:
    ids = {str(value).strip() for value in entity_ids if str(value).strip()}
    missing = ids - set(entity_map)
    if not missing:
        return
    rows = {
        row.id: row
        for row in db.query(LifeEntity).filter(LifeEntity.id.in_(missing)).all()
    }
    for entity_id in sorted(missing):
        row = rows.get(entity_id)
        if row is None or row.deleted_at is not None:
            raise ProactiveStateError(f"{field} references a missing Life entity")
        if row.owner_id != owner_id:
            raise ProactiveStateError(
                f"{field} crosses the immutable Account.id boundary"
            )
        entity_map[row.id] = row


def _source_ids(value: object) -> set[str]:
    """Collect canonical LifeSource ids from normalized typed payloads."""

    result: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key) == "source_id":
                source_id = str(child or "").strip()
                if source_id:
                    result.add(source_id)
            else:
                result.update(_source_ids(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            result.update(_source_ids(child))
    return result


def _load_sources(
    db,
    *,
    owner_id: str,
    source_ids: set[str],
) -> dict[str, LifeSource]:
    if not source_ids:
        return {}
    rows = {
        row.id: row
        for row in db.query(LifeSource).filter(LifeSource.id.in_(source_ids)).all()
    }
    for source_id in sorted(source_ids):
        row = rows.get(source_id)
        if row is None:
            raise ProactiveStateError(
                f"Typed owner state references missing Life source {source_id}"
            )
        if row.owner_id != owner_id:
            raise ProactiveStateError(
                "Typed owner state crosses the immutable Account.id source boundary"
            )
    return rows


def _source_evidence(source: LifeSource) -> dict[str, Any]:
    return {
        "id": source.id,
        "source_type": source.source_type,
        "title": source.title or "",
        "reference_available": bool(source.source_ref),
        "observed_at": _iso(source.observed_at),
        "captured_at": _iso(source.captured_at),
        "sensitivity": source.sensitivity,
        "version": int(source.version or 1),
    }


def _link_evidence(link: EntityLink) -> dict[str, Any]:
    """Serialize only routing-safe edge identity, never encrypted metadata."""

    return {
        "id": link.id,
        "source_type": link.source_type,
        "source_id": link.source_id,
        "relation": link.relation,
        "target_type": link.target_type,
        "target_id": link.target_id,
        "confidence": int(link.confidence or 0),
        "sensitivity": link.sensitivity,
        "version": int(link.version or 1),
    }


def _signal_id(
    kind: str,
    *,
    entity_ids: Sequence[str],
    discriminator: object,
) -> str:
    material = json.dumps(
        {
            "kind": kind,
            "entity_ids": sorted(set(entity_ids)),
            "discriminator": discriminator,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"{kind}:{digest}"


def _signal(
    *,
    kind: str,
    domain: str,
    title: str,
    summary: str,
    entity_ids: Sequence[str],
    discriminator: object = "",
    due_at: datetime | None = None,
    urgency: int,
    importance: int,
    risk: int,
    urgent: bool = False,
    important: bool = False,
    time_sensitive: bool = False,
    high_risk: bool = False,
    calculation: Mapping[str, Any] | None = None,
    link_ids: Sequence[str] = (),
) -> dict[str, Any]:
    normalized_ids = sorted({str(value) for value in entity_ids if str(value)})
    total = round(urgency * 0.40 + importance * 0.35 + risk * 0.25)
    if total >= 85:
        severity = "critical"
    elif total >= 70:
        severity = "high"
    elif total >= 40:
        severity = "medium"
    else:
        severity = "low"
    return {
        "id": _signal_id(
            kind, entity_ids=normalized_ids, discriminator=discriminator
        ),
        "kind": kind,
        "domain": domain,
        "title": str(title),
        "summary": str(summary),
        "due_at": _iso(due_at),
        "severity": severity,
        "score": {
            "total": total,
            "urgency": int(urgency),
            "importance": int(importance),
            "risk": int(risk),
            "formula": "40% urgency + 35% importance + 25% risk",
        },
        "thresholds": {
            "urgent": bool(urgent),
            "important": bool(important),
            "time_sensitive": bool(time_sensitive),
            "high_risk": bool(high_risk),
        },
        "calculation": dict(calculation or {}),
        "_entity_ids": normalized_ids,
        "_link_ids": sorted({str(value) for value in link_ids if str(value)}),
    }


def _requested_tokens(value: object | None) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, (list, tuple, set)):
        raise ProactiveInputError("explicit_interrupts must be a list")
    if len(value) > 50:
        raise ProactiveInputError(
            "explicit_interrupts must not contain more than 50 items"
        )
    result: set[str] = set()
    for raw in value:
        token = str(raw or "").strip()
        if not token or len(token) > 120:
            raise ProactiveInputError(
                "explicit_interrupts contains an invalid signal kind or id"
            )
        result.add(token)
    return result


def _apply_routing(signal: dict[str, Any], requested: set[str]) -> None:
    thresholds = signal["thresholds"]
    explicitly_requested = (
        signal["id"] in requested or signal["kind"] in requested
    )
    threshold_match = (
        thresholds["urgent"]
        and thresholds["important"]
        and thresholds["time_sensitive"]
    )
    interrupt = bool(
        explicitly_requested or thresholds["high_risk"] or threshold_match
    )
    reasons: list[str] = []
    if explicitly_requested:
        reasons.append("explicitly_requested")
    if thresholds["high_risk"]:
        reasons.append("high_risk")
    if threshold_match:
        reasons.append("urgent_and_important_and_time_sensitive")
    signal["thresholds"]["explicitly_requested"] = explicitly_requested
    signal["routing"] = {
        "channel": "interrupt" if interrupt else "digest",
        "reasons": reasons or ["below_interruption_threshold"],
        "rule": (
            "interrupt only when explicitly requested, high risk, or all of "
            "urgent + important + time-sensitive; otherwise digest"
        ),
    }


def _task_deadline(entity: LifeEntity, properties: Mapping[str, Any]) -> datetime | None:
    if entity.due_at is not None:
        return _stored_datetime(entity.due_at, field=f"task {entity.id} due_at")
    raw = properties.get("deadline")
    return _stored_datetime(raw, field=f"task {entity.id} deadline") if raw else None


def _string_values(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _reaches_goal(
    *,
    starts: set[str],
    adjacency: Mapping[str, set[str]],
    entity_map: Mapping[str, LifeEntity],
) -> bool:
    frontier = set(starts)
    visited = set(frontier)
    for _ in range(6):
        next_frontier: set[str] = set()
        for entity_id in sorted(frontier):
            row = entity_map.get(entity_id)
            if row is not None and row.entity_type == "goal":
                return True
            for target_id in sorted(adjacency.get(entity_id, set())):
                if target_id in visited:
                    continue
                target = entity_map.get(target_id)
                if target is None:
                    continue
                if target.entity_type == "goal":
                    return True
                visited.add(target_id)
                next_frontier.add(target_id)
        if not next_frontier:
            break
        frontier = next_frontier
    return False


def _finance_terminal(properties: Mapping[str, Any]) -> bool:
    kind = str(properties["record_type"])
    if kind not in _FINANCE_DUE_TYPES:
        return False
    field = _FINANCE_STATUS_FIELD[kind]
    return properties["details"].get(field) in _FINANCE_TERMINAL[kind]


def _explicit_subscription_usage(
    entity: LifeEntity,
    *,
    as_of: datetime,
    lookback_days: int,
) -> dict[str, Any] | None:
    provenance = _mapping(
        entity.provenance or {}, field=f"finance provenance {entity.id}"
    )
    nested = provenance.get("usage_evidence") or provenance.get("usage")
    usage = nested if isinstance(nested, Mapping) else provenance
    last_raw = usage.get("last_used_at")
    count_raw = usage.get("usage_count_30d")
    if last_raw in (None, "") and count_raw in (None, ""):
        return None
    last_used = (
        _stored_datetime(last_raw, field=f"subscription {entity.id} last_used_at")
        if last_raw not in (None, "") else None
    )
    count: int | None = None
    if count_raw not in (None, ""):
        if isinstance(count_raw, bool):
            raise ProactiveStateError(
                f"Malformed subscription usage evidence for {entity.id}"
            )
        try:
            count = int(count_raw)
        except (TypeError, ValueError) as exc:
            raise ProactiveStateError(
                f"Malformed subscription usage evidence for {entity.id}"
            ) from exc
        if count < 0:
            raise ProactiveStateError(
                f"Malformed subscription usage evidence for {entity.id}"
            )
    stale_before = as_of - timedelta(days=lookback_days)
    unused = count == 0 or (last_used is not None and last_used <= stale_before)
    return {
        "unused_by_explicit_evidence": unused,
        "last_used_at": _iso(last_used),
        "usage_count_30d": count,
        "stale_before": _iso(stale_before),
        "rule": (
            "only an explicit zero usage count or explicit old last-used time "
            "can produce an unused-subscription input; missing usage evidence cannot"
        ),
    }


def _version_postponements(
    rows: Sequence[LifeEntityVersion],
    *,
    as_of: datetime,
) -> dict[str, dict[str, Any]]:
    by_entity: dict[str, list[LifeEntityVersion]] = defaultdict(list)
    for row in rows:
        created = _stored_datetime(
            row.created_at, field=f"Life entity version {row.id} created_at"
        )
        if created is None or created <= as_of:
            by_entity[row.entity_id].append(row)
    result: dict[str, dict[str, Any]] = {}
    for entity_id, versions in by_entity.items():
        versions.sort(key=lambda row: (int(row.version), row.id))
        prior_due: datetime | None = None
        changes: list[dict[str, Any]] = []
        for row in versions:
            snapshot = _mapping(
                row.snapshot or {}, field=f"Life entity version {row.id} snapshot"
            )
            due = _stored_datetime(
                snapshot.get("due_at"), field=f"Life entity version {row.id} due_at"
            )
            if prior_due is not None and due is not None and due > prior_due:
                changes.append({
                    "version": int(row.version),
                    "from": _iso(prior_due),
                    "to": _iso(due),
                })
            prior_due = due
        if changes:
            result[entity_id] = {
                "postponement_count": len(changes),
                "changes": changes,
                "rule": "a later version moved due_at strictly later",
            }
    return result


def proactive_intelligence_report(
    db,
    *,
    owner_id: str,
    as_of: object,
    horizon_days: int = 30,
    lookback_days: int = 30,
    stale_project_days: int = 30,
    stale_decision_days: int = 30,
    daily_capacity_minutes: int = 480,
    limit: int = 100,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
    explicit_interrupts: object | None = None,
) -> dict[str, Any]:
    """Build a deterministic, factual proactive read model for one Account.id.

    The function only performs bounded reads.  It never flushes, commits,
    creates an audit row, invokes a model, proposes an action, or sends a
    notification.  ``as_of`` must include an explicit UTC offset.
    """

    principal = str(owner_id or "").strip()
    if not principal:
        raise ProactiveInputError("owner_id is required")
    aware_as_of = _aware_datetime(as_of, field="as_of")
    now = _naive_utc(aware_as_of)
    horizon = _bounded_int(
        horizon_days, field="horizon_days", minimum=1, maximum=MAX_HORIZON_DAYS
    )
    lookback = _bounded_int(
        lookback_days,
        field="lookback_days",
        minimum=1,
        maximum=MAX_LOOKBACK_DAYS,
    )
    project_stale = _bounded_int(
        stale_project_days,
        field="stale_project_days",
        minimum=1,
        maximum=3_650,
    )
    decision_stale = _bounded_int(
        stale_decision_days,
        field="stale_decision_days",
        minimum=1,
        maximum=3_650,
    )
    capacity = _bounded_int(
        daily_capacity_minutes,
        field="daily_capacity_minutes",
        minimum=30,
        maximum=1_440,
    )
    bounded_limit = _bounded_int(
        limit, field="limit", minimum=1, maximum=MAX_OUTPUT_LIMIT
    )
    bounded_scan = _bounded_int(
        scan_limit, field="scan_limit", minimum=1, maximum=MAX_SCAN_LIMIT
    )
    requested = _requested_tokens(explicit_interrupts)
    horizon_end = now + timedelta(days=horizon)
    lookback_start = now - timedelta(days=lookback)

    scan_specs = {
        "tasks": (("task",), "due"),
        "projects": (("project",), "updated"),
        "relationships": (("commitment", "reminder"), "due"),
        "relationship_profiles": (("person",), "updated"),
        "finance": (("finance_record",), "occurred"),
        "health": (("health_record",), "occurred"),
        "decisions": (("decision",), "due"),
        "habits": (("habit",), "updated"),
        "habit_logs": ((HABIT_LOG_ENTITY_TYPE,), "occurred"),
        "home": (("home_record",), "due"),
    }
    scans: dict[str, dict[str, Any]] = {}
    domain_rows: dict[str, list[LifeEntity]] = {}
    entity_map: dict[str, LifeEntity] = {}
    for domain, (types, order) in scan_specs.items():
        rows, truncated = _scan_entities(
            db,
            owner_id=principal,
            entity_types=types,
            limit=bounded_scan,
            order=order,
        )
        domain_rows[domain] = rows
        entity_map.update({row.id: row for row in rows})
        scans[domain] = {"scanned": len(rows), "truncated": truncated}

    link_limit = min(MAX_LINK_SCAN_LIMIT, max(20, bounded_scan * 4))
    links, links_truncated = _load_links(
        db,
        owner_id=principal,
        limit=link_limit,
        entity_map=entity_map,
    )
    scans["entity_links"] = {
        "scanned": len(links),
        "truncated": links_truncated,
    }
    links_by_id = {row.id: row for row in links}
    links_by_entity: dict[str, list[EntityLink]] = defaultdict(list)
    goal_adjacency: dict[str, set[str]] = defaultdict(set)
    for row in links:
        links_by_entity[str(row.source_id)].append(row)
        links_by_entity[str(row.target_id)].append(row)
        if row.relation in _GOAL_RELATIONS:
            goal_adjacency[str(row.source_id)].add(str(row.target_id))

    source_ids_by_entity: dict[str, set[str]] = defaultdict(set)

    # Validate all typed state before producing any result.  Legacy generic
    # task/project/person nodes remain readable but do not masquerade as typed
    # domain records.
    task_props: dict[str, Mapping[str, Any]] = {}
    typed_task_ids: set[str] = set()
    task_reference_ids: set[str] = set()
    for entity in domain_rows["tasks"]:
        raw = _mapping(entity.properties or {}, field=f"task {entity.id} properties")
        typed_claim = (
            raw.get("task_schema_version") is not None
            or raw.get("definition_of_done") is not None
            or raw.get("effort_minutes") is not None
        )
        if typed_claim:
            if raw.get("task_schema_version") != TASK_SCHEMA_VERSION:
                raise ProactiveStateError(
                    f"Malformed task state for entity {entity.id}: unsupported schema version"
                )
            try:
                normalized = validate_task_properties(raw, status=entity.status)
            except LifeGraphError as exc:
                raise _domain_error("task", entity, exc) from exc
            typed_task_ids.add(entity.id)
            task_props[entity.id] = normalized
            source_ids_by_entity[entity.id].update(
                _source_ids(normalized) | _source_ids(entity.provenance or {})
            )
            task_reference_ids.update(
                [normalized.get("project_id")] if normalized.get("project_id") else []
            )
            for field in ("people_ids", "dependency_ids", "document_ids"):
                task_reference_ids.update(normalized.get(field) or [])
        else:
            task_props[entity.id] = raw
            task_reference_ids.update(_string_values(raw.get("goal_id")))
            task_reference_ids.update(_string_values(raw.get("goal_ids")))
            task_reference_ids.update(_string_values(raw.get("project_id")))
            task_reference_ids.update(_string_values(raw.get("project_ids")))
    _load_owned_entity_refs(
        db,
        owner_id=principal,
        entity_ids=task_reference_ids,
        entity_map=entity_map,
        field="Task reference",
    )

    relationship_records: dict[str, dict[str, Any]] = {}
    relationship_profile_ref_ids: set[str] = set()
    for entity in domain_rows["relationships"]:
        raw = _mapping(
            entity.properties or {}, field=f"relationship {entity.id} properties"
        )
        if raw.get("relationship_schema_version") is None:
            continue
        if raw.get("relationship_schema_version") != RELATIONSHIP_SCHEMA_VERSION:
            raise ProactiveStateError(
                f"Malformed relationship state for entity {entity.id}: unsupported schema version"
            )
        try:
            record = serialize_relationship_record(
                db, owner_id=principal, entity=entity
            )
        except LifeGraphError as exc:
            raise _domain_error("relationship", entity, exc) from exc
        relationship_records[entity.id] = record
        if record.get("profile_id"):
            relationship_profile_ref_ids.add(str(record["profile_id"]))
        source_ids_by_entity[entity.id].update(
            _source_ids(record) | _source_ids(entity.provenance or {})
        )
    _load_owned_entity_refs(
        db,
        owner_id=principal,
        entity_ids=relationship_profile_ref_ids,
        entity_map=entity_map,
        field="Relationship record",
    )

    relationship_profiles: dict[str, Mapping[str, Any]] = {}
    for entity in domain_rows["relationship_profiles"]:
        raw = _mapping(
            entity.properties or {},
            field=f"relationship profile {entity.id} properties",
        )
        if raw.get("relationship_schema_version") is None:
            continue
        if raw.get("relationship_schema_version") != RELATIONSHIP_SCHEMA_VERSION:
            raise ProactiveStateError(
                f"Malformed relationship profile state for entity {entity.id}: unsupported schema version"
            )
        try:
            normalized = validate_profile_properties(
                db, owner_id=principal, value=raw
            )
        except LifeGraphError as exc:
            raise _domain_error("relationship profile", entity, exc) from exc
        relationship_profiles[entity.id] = normalized
        source_ids_by_entity[entity.id].update(
            _source_ids(normalized) | _source_ids(entity.provenance or {})
        )

    finance_props: dict[str, Mapping[str, Any]] = {}
    for entity in domain_rows["finance"]:
        raw = _mapping(
            entity.properties or {}, field=f"finance {entity.id} properties"
        )
        if raw.get("finance_schema_version") != FINANCE_SCHEMA_VERSION:
            raise ProactiveStateError(
                f"Malformed finance state for entity {entity.id}: unsupported schema version"
            )
        try:
            normalized = validate_finance_properties(raw)
        except LifeGraphError as exc:
            raise _domain_error("finance", entity, exc) from exc
        finance_props[entity.id] = normalized
        source_ids_by_entity[entity.id].update(
            _source_ids(normalized) | _source_ids(entity.provenance or {})
        )

    health_props: dict[str, Mapping[str, Any]] = {}
    for entity in domain_rows["health"]:
        raw = _mapping(
            entity.properties or {}, field=f"health {entity.id} properties"
        )
        if raw.get("health_schema_version") != HEALTH_SCHEMA_VERSION:
            raise ProactiveStateError(
                f"Malformed health state for entity {entity.id}: unsupported schema version"
            )
        try:
            normalized = validate_health_properties(raw)
        except LifeGraphError as exc:
            raise _domain_error("health", entity, exc) from exc
        health_props[entity.id] = normalized
        source_ids_by_entity[entity.id].update(
            _source_ids(normalized) | _source_ids(entity.provenance or {})
        )

    decision_props: dict[str, Mapping[str, Any]] = {}
    decision_reference_ids: set[str] = set()
    for entity in domain_rows["decisions"]:
        raw = _mapping(
            entity.properties or {}, field=f"decision {entity.id} properties"
        )
        if raw.get("decision_schema_version") is None:
            continue
        if raw.get("decision_schema_version") != DECISION_SCHEMA_VERSION:
            raise ProactiveStateError(
                f"Malformed decision state for entity {entity.id}: unsupported schema version"
            )
        try:
            normalized = validate_decision_properties(raw)
        except LifeGraphError as exc:
            raise _domain_error("decision", entity, exc) from exc
        decision_props[entity.id] = normalized
        source_ids_by_entity[entity.id].update(
            _source_ids(normalized) | _source_ids(entity.provenance or {})
        )
        decision_reference_ids.update(
            str(row.get("entity_id"))
            for row in normalized.get("evidence", [])
            if row.get("entity_id")
        )
    _load_owned_entity_refs(
        db,
        owner_id=principal,
        entity_ids=decision_reference_ids,
        entity_map=entity_map,
        field="Decision evidence",
    )

    habit_props: dict[str, Mapping[str, Any]] = {}
    for entity in domain_rows["habits"]:
        raw = _mapping(entity.properties or {}, field=f"habit {entity.id} properties")
        if raw.get("habit_schema_version") != HABIT_SCHEMA_VERSION:
            raise ProactiveStateError(
                f"Malformed habit state for entity {entity.id}: unsupported schema version"
            )
        try:
            normalized = validate_habit_properties(raw)
        except LifeGraphError as exc:
            raise _domain_error("habit", entity, exc) from exc
        habit_props[entity.id] = normalized
        source_ids_by_entity[entity.id].update(_source_ids(entity.provenance or {}))

    habit_log_props: dict[str, Mapping[str, Any]] = {}
    habit_log_habit: dict[str, str] = {}
    for entity in domain_rows["habit_logs"]:
        raw = _mapping(
            entity.properties or {}, field=f"habit log {entity.id} properties"
        )
        if raw.get("habit_log_schema_version") is None:
            continue
        habit_id = str(raw.get("habit_id") or "").strip()
        if not habit_id:
            raise ProactiveStateError(
                f"Malformed habit log state for entity {entity.id}: missing habit_id"
            )
        _load_owned_entity_refs(
            db,
            owner_id=principal,
            entity_ids=[habit_id],
            entity_map=entity_map,
            field="Habit log",
        )
        habit = entity_map[habit_id]
        if habit.entity_type != "habit":
            raise ProactiveStateError(
                f"Malformed habit log state for entity {entity.id}: invalid habit reference"
            )
        if habit.id not in habit_props:
            try:
                habit_props[habit.id] = validate_habit_properties(
                    _mapping(
                        habit.properties or {},
                        field=f"habit {habit.id} properties",
                    )
                )
            except LifeGraphError as exc:
                raise _domain_error("habit", habit, exc) from exc
        try:
            normalized = validate_habit_log_properties(
                raw, habit_properties=habit_props[habit.id]
            )
        except LifeGraphError as exc:
            raise _domain_error("habit log", entity, exc) from exc
        habit_log_props[entity.id] = normalized
        habit_log_habit[entity.id] = habit_id
        source_ids_by_entity[entity.id].update(
            _source_ids(normalized) | _source_ids(entity.provenance or {})
        )

    home_props: dict[str, Mapping[str, Any]] = {}
    home_reference_ids: set[str] = set()
    for entity in domain_rows["home"]:
        raw = _mapping(entity.properties or {}, field=f"home {entity.id} properties")
        if raw.get("home_schema_version") != HOME_SCHEMA_VERSION:
            raise ProactiveStateError(
                f"Malformed home state for entity {entity.id}: unsupported schema version"
            )
        try:
            normalized = validate_home_properties(raw)
        except LifeGraphError as exc:
            raise _domain_error("home", entity, exc) from exc
        home_props[entity.id] = normalized
        source_ids_by_entity[entity.id].update(
            _source_ids(normalized) | _source_ids(entity.provenance or {})
        )
        references = normalized.get("references", {})
        home_reference_ids.update(references.get("file_entity_ids") or [])
        home_reference_ids.update(references.get("entity_ids") or [])
    _load_owned_entity_refs(
        db,
        owner_id=principal,
        entity_ids=home_reference_ids,
        entity_map=entity_map,
        field="Home record",
    )

    all_source_ids = set().union(*source_ids_by_entity.values()) if source_ids_by_entity else set()
    sources = _load_sources(
        db, owner_id=principal, source_ids=all_source_ids
    )

    raw_signals: list[dict[str, Any]] = []
    raw_truncated = False

    def add(signal: dict[str, Any]) -> None:
        nonlocal raw_truncated
        if len(raw_signals) >= MAX_RAW_SIGNALS:
            raw_truncated = True
            return
        raw_signals.append(signal)

    # Tasks: overdue, goal disconnect, unscheduled deadlines, daily workload,
    # and explicit task/project conflicts.
    workload_by_day: dict[date, list[tuple[LifeEntity, int, str]]] = defaultdict(list)
    for entity in domain_rows["tasks"]:
        if entity.status in _TERMINAL_WORK_STATUSES:
            continue
        properties = task_props[entity.id]
        deadline = _task_deadline(entity, properties)
        priority = str(properties.get("priority") or "normal").lower()
        important = priority in {"high", "critical"}
        if deadline is not None and deadline < now:
            overdue_days = max(0, (now - deadline).days)
            add(_signal(
                kind="overdue_task",
                domain="tasks",
                title=f"Overdue task: {entity.title}",
                summary=(
                    "The stored task deadline is before the explicit report time. "
                    "No action has been taken."
                ),
                entity_ids=[entity.id],
                discriminator=_iso(deadline),
                due_at=deadline,
                urgency=95 if important else 72,
                importance=92 if important else 58,
                risk=65 if priority == "critical" else 35,
                urgent=True,
                important=important,
                time_sensitive=True,
                calculation={
                    "deadline": _iso(deadline),
                    "as_of": _iso(now),
                    "overdue_days": overdue_days,
                    "priority": priority,
                    "rule": "deadline is strictly before as_of",
                },
            ))

        property_goals = set(_string_values(properties.get("goal_id"))) | set(
            _string_values(properties.get("goal_ids"))
        )
        property_projects = set(_string_values(properties.get("project_id"))) | set(
            _string_values(properties.get("project_ids"))
        )
        if not links_truncated and not property_goals and not _reaches_goal(
            starts={entity.id, *property_projects},
            adjacency=goal_adjacency,
            entity_map=entity_map,
        ):
            add(_signal(
                kind="goal_disconnected_work",
                domain="work",
                title=f"Goal-disconnected work: {entity.title}",
                summary=(
                    "No stored goal reference or bounded Project-to-Goal path was found. "
                    "This is a graph-quality finding, not an inference about usefulness."
                ),
                entity_ids=[entity.id, *sorted(property_projects)],
                urgency=25,
                importance=60,
                risk=20,
                calculation={
                    "goal_link_scan_truncated": False,
                    "rule": "no explicit goal id and no declared upward goal relation path",
                },
            ))

        if deadline is not None and deadline <= horizon_end and not links_truncated:
            scheduled_fields = any(
                properties.get(field)
                for field in (
                    "scheduled_start", "scheduled_at", "calendar_event_uid",
                    "calendar_event_id",
                )
            )
            scheduled_link = any(
                row.relation in _SCHEDULE_RELATIONS
                for row in links_by_entity.get(entity.id, [])
            )
            if not scheduled_fields and not scheduled_link:
                add(_signal(
                    kind="unscheduled_deadline",
                    domain="work",
                    title=f"Unscheduled deadline: {entity.title}",
                    summary=(
                        "A stored deadline falls inside the bounded horizon, but no "
                        "explicit calendar/scheduling reference was found."
                    ),
                    entity_ids=[entity.id],
                    discriminator=_iso(deadline),
                    due_at=deadline,
                    urgency=75 if deadline <= now + timedelta(days=2) else 45,
                    importance=75 if important else 55,
                    risk=30,
                    urgent=deadline <= now + timedelta(days=2),
                    important=important,
                    time_sensitive=True,
                    calculation={
                        "deadline": _iso(deadline),
                        "horizon_end": _iso(horizon_end),
                        "link_scan_truncated": False,
                        "rule": "deadline in horizon and no explicit scheduling field or relation",
                    },
                ))
        if entity.id in typed_task_ids and deadline is not None and now <= deadline <= horizon_end:
            effort = int(properties.get("effort_minutes") or 0)
            local_deadline = deadline.replace(tzinfo=timezone.utc).astimezone(
                aware_as_of.tzinfo
            )
            workload_by_day[local_deadline.date()].append(
                (entity, effort, priority)
            )

    for local_day, values in sorted(workload_by_day.items()):
        total_effort = sum(value[1] for value in values)
        if total_effort <= capacity:
            continue
        critical = any(value[2] == "critical" for value in values)
        same_or_next_day = local_day <= aware_as_of.date() + timedelta(days=1)
        add(_signal(
            kind="workload_overcommitment",
            domain="work",
            title=f"Recorded effort exceeds capacity for {local_day.isoformat()}",
            summary=(
                "The sum of stored task effort estimates due on this local date exceeds "
                "the configured daily capacity. Restia has not rescheduled anything."
            ),
            entity_ids=[value[0].id for value in values],
            discriminator=local_day.isoformat(),
            urgency=88 if same_or_next_day else 58,
            importance=88 if critical else 72,
            risk=55,
            urgent=same_or_next_day,
            important=True,
            time_sensitive=True,
            calculation={
                "local_date": local_day.isoformat(),
                "timezone": str(aware_as_of.tzinfo),
                "task_count": len(values),
                "total_effort_minutes": total_effort,
                "daily_capacity_minutes": capacity,
                "excess_minutes": total_effort - capacity,
                "rule": "sum of explicit effort estimates exceeds configured capacity",
            },
        ))

    seen_conflicts: set[tuple[str, str, str]] = set()
    for link in links:
        if link.relation not in _CONFLICT_RELATIONS:
            continue
        source = entity_map[str(link.source_id)]
        target = entity_map[str(link.target_id)]
        if source.entity_type not in {"task", "project", "milestone"}:
            continue
        if target.entity_type not in {"task", "project", "milestone"}:
            continue
        if source.status in _TERMINAL_WORK_STATUSES or target.status in _TERMINAL_WORK_STATUSES:
            continue
        pair = tuple(sorted((source.id, target.id)))
        marker = (pair[0], pair[1], link.relation)
        if marker in seen_conflicts:
            continue
        seen_conflicts.add(marker)
        add(_signal(
            kind="conflicting_work",
            domain="work",
            title=f"Explicit work conflict: {source.title} / {target.title}",
            summary=(
                f"A stored '{link.relation}' relation connects two active work records."
            ),
            entity_ids=[source.id, target.id],
            discriminator=link.id,
            urgency=50,
            importance=75,
            risk=45,
            important=True,
            calculation={"relation": link.relation, "link_id": link.id},
            link_ids=[link.id],
        ))

    for entity in domain_rows["projects"]:
        if entity.status in _TERMINAL_WORK_STATUSES:
            continue
        updated = _stored_datetime(
            entity.updated_at, field=f"project {entity.id} updated_at"
        )
        explicit_stall = entity.status in {"blocked", "stalled", "paused"}
        stale = bool(updated and updated <= now - timedelta(days=project_stale))
        if explicit_stall or stale:
            add(_signal(
                kind="stalled_project",
                domain="projects",
                title=f"Project needs review: {entity.title}",
                summary=(
                    "The project has an explicit stalled/blocked status."
                    if explicit_stall else
                    "The project record has not been updated inside the configured freshness window."
                ),
                entity_ids=[entity.id],
                discriminator={"status": entity.status, "updated": _iso(updated)},
                urgency=55 if explicit_stall else 35,
                importance=75,
                risk=40,
                important=True,
                calculation={
                    "status": entity.status,
                    "updated_at": _iso(updated),
                    "stale_before": _iso(now - timedelta(days=project_stale)),
                    "explicit_stall": explicit_stall,
                    "rule": "explicit stalled state or updated_at outside freshness window",
                },
            ))

    work_ids = [
        row.id for row in (*domain_rows["tasks"], *domain_rows["projects"])
    ]
    version_limit = min(MAX_VERSION_SCAN_LIMIT, max(20, bounded_scan * 4))
    version_rows = db.query(LifeEntityVersion).filter(
        LifeEntityVersion.owner_id == principal,
        LifeEntityVersion.entity_id.in_(work_ids),
    ).order_by(
        LifeEntityVersion.entity_id.asc(),
        LifeEntityVersion.version.asc(),
        LifeEntityVersion.id.asc(),
    ).limit(version_limit + 1).all() if work_ids else []
    versions_truncated = len(version_rows) > version_limit
    version_rows = version_rows[:version_limit]
    scans["work_versions"] = {
        "scanned": len(version_rows),
        "truncated": versions_truncated,
    }
    postponements = _version_postponements(version_rows, as_of=now)
    for entity_id, calculation in sorted(postponements.items()):
        entity = entity_map.get(entity_id)
        if entity is None or entity.status in _TERMINAL_WORK_STATUSES:
            continue
        add(_signal(
            kind="deadline_postponement",
            domain="work",
            title=f"Deadline moved later: {entity.title}",
            summary=(
                "Version history contains one or more explicit due-date moves to a later time."
            ),
            entity_ids=[entity.id],
            discriminator=calculation["changes"],
            urgency=35,
            importance=65,
            risk=30,
            calculation=calculation,
        ))

    # Relationships: explicit source-backed commitments, follow-ups, unanswered
    # messages, and relationship care dates only.
    for entity_id, record in sorted(relationship_records.items()):
        entity = entity_map[entity_id]
        if record.get("status") != "open":
            continue
        due = _stored_datetime(record.get("due_at"), field=f"relationship {entity.id} due_at")
        if due is None or due > horizon_end:
            continue
        record_kind = str(record.get("record_kind"))
        reminder_kind = str(record.get("reminder_kind") or record_kind)
        priority = str(record.get("priority") or "normal")
        overdue = due < now
        important = priority in {"high", "urgent"} or record_kind == "commitment"
        kind = (
            "unanswered_message_due"
            if reminder_kind == "unanswered_message"
            else "overdue_commitment"
            if record_kind == "commitment" and overdue
            else "relationship_follow_up"
        )
        add(_signal(
            kind=kind,
            domain="relationships",
            title=f"{record.get('title') or entity.title}",
            summary=(
                "This reminder comes from an explicit, source-backed relationship record. "
                "Restia cannot and did not send a personal message."
            ),
            entity_ids=[entity.id, str(record.get("profile_id"))],
            discriminator=_iso(due),
            due_at=due,
            urgency=92 if overdue and important else 68 if overdue else 45,
            importance=82 if important else 58,
            risk=45,
            urgent=overdue,
            important=important,
            time_sensitive=True,
            calculation={
                "record_kind": record_kind,
                "reminder_kind": reminder_kind,
                "priority": priority,
                "overdue": overdue,
                "source_backed": bool(record.get("source_backed")),
                "rule": "explicit due_at on an open relationship record",
            },
        ))

    for entity_id, properties in sorted(relationship_profiles.items()):
        care = properties.get("care_plan")
        if not isinstance(care, Mapping):
            continue
        due = _stored_datetime(
            care.get("next_due_at"),
            field=f"relationship profile {entity_id} care_plan.next_due_at",
        )
        if due is None or due > horizon_end:
            continue
        entity = entity_map[entity_id]
        add(_signal(
            kind="relationship_care_due",
            domain="relationships",
            title=f"Relationship care review: {entity.title}",
            summary=(
                "An explicit source-backed care plan date falls inside the report horizon."
            ),
            entity_ids=[entity.id],
            discriminator=_iso(due),
            due_at=due,
            urgency=65 if due < now else 38,
            importance=62,
            risk=25,
            time_sensitive=True,
            calculation={
                "next_due_at": _iso(due),
                "interval_days": care.get("interval_days"),
                "rule": "explicit care_plan.next_due_at",
            },
        ))

    # Finance: due facts, explicit usage evidence, and explainable anomaly
    # inputs.  No finding recommends a financial action.
    duplicate_groups: dict[tuple[str, ...], list[LifeEntity]] = defaultdict(list)
    expenses_by_currency: dict[str, list[tuple[Decimal, LifeEntity]]] = defaultdict(list)
    for entity in domain_rows["finance"]:
        properties = finance_props[entity.id]
        record_type = str(properties["record_type"])
        due = _stored_datetime(
            properties.get("due_at"), field=f"finance {entity.id} due_at"
        )
        if (
            record_type in _FINANCE_DUE_TYPES
            and due is not None
            and due <= horizon_end
            and not _finance_terminal(properties)
        ):
            overdue = due < now
            add(_signal(
                kind="finance_due",
                domain="finance",
                title=f"{record_type.replace('_', ' ').title()} due: {entity.title}",
                summary=(
                    "A stored finance record has an explicit due date. This is factual "
                    "record review input, not financial, tax, legal, or investment advice."
                ),
                entity_ids=[entity.id],
                discriminator=_iso(due),
                due_at=due,
                urgency=78 if overdue else 48,
                importance=70 if record_type in {"loan", "tax_item"} else 55,
                risk=55,
                urgent=overdue,
                important=record_type in {"loan", "tax_item"},
                time_sensitive=True,
                calculation={
                    "record_type": record_type,
                    "amount": properties.get("amount"),
                    "currency": properties.get("currency"),
                    "overdue": overdue,
                    "rule": "active finance due_at is inside bounded horizon",
                    "not_advice": True,
                },
            ))
        if record_type == "subscription" and properties["details"].get(
            "subscription_status"
        ) == "active":
            usage = _explicit_subscription_usage(
                entity, as_of=now, lookback_days=lookback
            )
            if usage and usage["unused_by_explicit_evidence"]:
                add(_signal(
                    kind="unused_subscription_input",
                    domain="finance",
                    title=f"Subscription usage review: {entity.title}",
                    summary=(
                        "Stored usage evidence meets the deterministic unused-input rule. "
                        "Restia did not infer usage from absence and does not recommend cancellation."
                    ),
                    entity_ids=[entity.id],
                    discriminator=usage,
                    urgency=20,
                    importance=55,
                    risk=25,
                    calculation={**usage, "not_financial_advice": True},
                ))

        if record_type not in {"income", "expense"}:
            continue
        details = properties["details"]
        effective = _stored_datetime(
            properties.get("effective_at"), field=f"finance {entity.id} effective_at"
        )
        party = details.get("merchant") or details.get("counterparty") or ""
        duplicate_key = (
            record_type,
            str(properties["scope"]),
            str(properties["currency"]),
            str(properties["amount"]),
            _iso(effective) or "",
            str(party).casefold(),
        )
        duplicate_groups[duplicate_key].append(entity)
        if details.get("transaction_status") == "pending" and effective is not None and (
            effective < now - timedelta(days=7)
        ):
            add(_signal(
                kind="finance_anomaly_input",
                domain="finance",
                title=f"Stale pending record: {entity.title}",
                summary=(
                    "The transaction is still recorded as pending more than seven days "
                    "after its effective time. This is a record-quality input, not a fraud finding."
                ),
                entity_ids=[entity.id],
                discriminator="stale_pending",
                urgency=35,
                importance=50,
                risk=35,
                calculation={
                    "anomaly_kind": "stale_pending",
                    "effective_at": _iso(effective),
                    "as_of": _iso(now),
                    "rule": "pending more than 7 days",
                    "not_fraud_determination": True,
                },
            ))
        if record_type == "expense":
            expenses_by_currency[str(properties["currency"])].append(
                (Decimal(str(properties["amount"])), entity)
            )

    for duplicate_key, entities in sorted(duplicate_groups.items()):
        if len(entities) < 2:
            continue
        add(_signal(
            kind="finance_anomaly_input",
            domain="finance",
            title="Possible duplicate finance records",
            summary=(
                "Records share type, scope, currency, amount, effective time, and party label. "
                "This is a comparison input, not a fraud finding."
            ),
            entity_ids=[row.id for row in entities],
            discriminator={"kind": "possible_duplicate", "key": duplicate_key},
            urgency=25,
            importance=55,
            risk=30,
            calculation={
                "anomaly_kind": "possible_duplicate",
                "comparison_fields": [
                    "record_type", "scope", "currency", "amount",
                    "effective_at", "party_label",
                ],
                "not_fraud_determination": True,
            },
        ))
    for currency, values in sorted(expenses_by_currency.items()):
        if len(values) < 5:
            continue
        median = statistics.median([value[0] for value in values])
        if median <= 0:
            continue
        threshold = median * Decimal(3)
        for amount, entity in sorted(values, key=lambda row: (row[0], row[1].id)):
            if amount < threshold:
                continue
            add(_signal(
                kind="finance_anomaly_input",
                domain="finance",
                title=f"Expense comparison input: {entity.title}",
                summary=(
                    "The recorded expense is at least three times the median of the "
                    "bounded same-currency comparison set. This is not a fraud finding."
                ),
                entity_ids=[entity.id],
                discriminator={"kind": "large_vs_median", "currency": currency},
                urgency=25,
                importance=58,
                risk=38,
                calculation={
                    "anomaly_kind": "large_vs_median",
                    "amount": format(amount, "f"),
                    "currency": currency,
                    "median": format(median, "f"),
                    "comparison_count": len(values),
                    "rule": "at least 5 expenses and amount at least 3x median",
                    "not_fraud_determination": True,
                },
            ))

    # Health: explicit reported red flags can interrupt; numeric comparison
    # inputs remain factual digest items and carry no diagnosis or advice.
    metric_series: dict[
        tuple[str, str, str], list[tuple[datetime, float, LifeEntity]]
    ] = defaultdict(list)
    for entity in domain_rows["health"]:
        properties = health_props[entity.id]
        occurred = _stored_datetime(
            entity.occurred_at, field=f"health {entity.id} occurred_at"
        )
        if occurred is None or occurred > now or occurred < lookback_start:
            continue
        safety = health_safety_notice(properties)
        if safety and safety.get("urgent"):
            reported = [str(value) for value in safety.get("signals", [])]
            add(_signal(
                kind="health_reported_red_flag",
                domain="health",
                title=f"Explicit health red-flag record: {entity.title}",
                summary=(
                    "The user's stored symptom text contains explicit red-flag terms. "
                    "Restia is not diagnosing, prescribing, or recommending a medication change."
                ),
                entity_ids=[entity.id],
                discriminator=reported,
                urgency=100,
                importance=100,
                risk=100,
                urgent=True,
                important=True,
                time_sensitive=True,
                high_risk=True,
                calculation={
                    "reported_signals": reported,
                    "recorded_at": _iso(occurred),
                    "factual_input_only": True,
                    "no_diagnosis": True,
                },
            ))
        record_type = str(properties["record_type"])
        for metric in properties.get("metrics", []):
            value = float(metric["value"])
            if not math.isfinite(value):
                raise ProactiveStateError(
                    f"Malformed health metric for entity {entity.id}"
                )
            key = (record_type, str(metric["name"]), str(metric["unit"]))
            metric_series[key].append((occurred, value, entity))
    for (record_type, metric, unit), values in sorted(metric_series.items()):
        values.sort(key=lambda row: (row[0], row[2].id))
        if len(values) < 3:
            continue
        prior = [row[1] for row in values[:-1]]
        baseline = statistics.mean(prior)
        if baseline == 0:
            continue
        latest_at, latest, latest_entity = values[-1]
        percent = round((latest - baseline) * 100.0 / abs(baseline), 2)
        if abs(percent) < 20:
            continue
        add(_signal(
            kind="health_trend_caution_input",
            domain="health",
            title=f"Recorded {metric} changed versus prior average",
            summary=(
                "This is a numerical comparison of stored observations only. It is not "
                "a diagnosis, treatment recommendation, or medical interpretation."
            ),
            entity_ids=[row[2].id for row in values],
            discriminator={
                "record_type": record_type, "metric": metric, "unit": unit,
                "latest_at": _iso(latest_at),
            },
            urgency=25,
            importance=60,
            risk=40,
            calculation={
                "record_type": record_type,
                "metric": metric,
                "unit": unit,
                "comparison_count": len(values),
                "prior_average": baseline,
                "latest": latest,
                "change_percent": percent,
                "rule": "at least 3 observations and latest differs by at least 20% from prior average",
                "factual_input_only": True,
                "no_diagnosis": True,
            },
        ))

    # Decisions: explicit due reviews and stale active assumptions.
    for entity in domain_rows["decisions"]:
        properties = decision_props.get(entity.id)
        if properties is None or entity.status in _TERMINAL_DECISION_STATUSES:
            continue
        stale_before = now - timedelta(days=decision_stale)
        due_ids: list[str] = []
        stale_ids: list[str] = []
        earliest: datetime | None = None
        review_due = entity.review_at is not None and _stored_datetime(
            entity.review_at, field=f"decision {entity.id} review_at"
        ) <= now
        if review_due:
            earliest = _stored_datetime(
                entity.review_at, field=f"decision {entity.id} review_at"
            )
        for assumption in properties.get("assumptions", []):
            if assumption["status"] not in {"unverified", "valid"}:
                continue
            assumption_due = _stored_datetime(
                assumption.get("review_at"),
                field=f"decision {entity.id} assumption review_at",
            )
            if assumption_due is not None and assumption_due <= now:
                due_ids.append(str(assumption["id"]))
                earliest = min(earliest, assumption_due) if earliest else assumption_due
            freshness = (
                _stored_datetime(
                    assumption.get("last_reviewed_at"),
                    field=f"decision {entity.id} assumption last_reviewed_at",
                )
                or _stored_datetime(
                    assumption.get("recorded_at"),
                    field=f"decision {entity.id} assumption recorded_at",
                )
                or _stored_datetime(
                    entity.occurred_at, field=f"decision {entity.id} occurred_at"
                )
                or _stored_datetime(
                    entity.created_at, field=f"decision {entity.id} created_at"
                )
            )
            if freshness is not None and freshness <= stale_before:
                stale_ids.append(str(assumption["id"]))
                earliest = min(earliest, freshness) if earliest else freshness
        if not review_due and not due_ids and not stale_ids:
            continue
        add(_signal(
            kind="stale_decision_assumption",
            domain="decisions",
            title=f"Decision review input: {entity.title}",
            summary=(
                "One or more explicit decision review dates or stored assumptions meet "
                "the deterministic due/stale rule. No decision has been changed."
            ),
            entity_ids=[entity.id],
            discriminator={
                "review_due": review_due,
                "due_ids": sorted(due_ids),
                "stale_ids": sorted(stale_ids),
            },
            due_at=earliest,
            urgency=72 if review_due or due_ids else 38,
            importance=74,
            risk=48,
            urgent=bool(review_due or due_ids),
            important=True,
            time_sensitive=bool(review_due or due_ids),
            calculation={
                "decision_review_due": review_due,
                "due_assumption_ids": sorted(due_ids),
                "stale_assumption_ids": sorted(stale_ids),
                "stale_before": _iso(stale_before),
                "rule": "explicit review_at due or active assumption outside freshness window",
            },
        ))

    # Habits: schedule-aware missed dates using the report's timezone-aware
    # instant converted independently to each habit's IANA timezone.
    habit_absence_scan_truncated = bool(scans["habit_logs"]["truncated"])
    scans["habit_absence_inference"] = {
        "scanned": 0 if habit_absence_scan_truncated else len(habit_log_props),
        "truncated": habit_absence_scan_truncated,
        "skipped": habit_absence_scan_truncated,
    }
    logs_by_habit_date: dict[tuple[str, date], list[tuple[LifeEntity, Mapping[str, Any]]]] = defaultdict(list)
    for entity_id, properties in habit_log_props.items():
        logged_at = _stored_datetime(
            properties.get("logged_at"), field=f"habit log {entity_id} logged_at"
        )
        if logged_at is None or logged_at > now:
            continue
        scheduled = date.fromisoformat(str(properties["scheduled_for"]))
        logs_by_habit_date[(habit_log_habit[entity_id], scheduled)].append(
            (entity_map[entity_id], properties)
        )
    for entity in domain_rows["habits"]:
        if habit_absence_scan_truncated:
            break
        if entity.status != "active":
            continue
        properties = habit_props[entity.id]
        schedule = properties["schedule"]
        schedule_zone = ZoneInfo(schedule["timezone"])
        local_moment = aware_as_of.astimezone(schedule_zone)
        start = local_moment.date() - timedelta(days=lookback - 1)
        expected = expected_routine_dates(
            entity, from_date=start, to_date=local_moment.date()
        )
        if expected and expected[-1] == local_moment.date():
            at = schedule.get("time_of_day")
            if at:
                hour, minute = (int(part) for part in str(at).split(":"))
                due_local = datetime.combine(
                    local_moment.date(),
                    time(hour, minute),
                    tzinfo=schedule_zone,
                ) + timedelta(minutes=int(schedule["grace_minutes"]))
                if local_moment < due_local:
                    expected.pop()
            else:
                expected.pop()
        for scheduled in expected:
            candidates = logs_by_habit_date.get((entity.id, scheduled), [])
            strongest = max(
                candidates,
                key=lambda row: _HABIT_RESULT_PRIORITY[row[1]["result"]],
                default=None,
            )
            if strongest and strongest[1]["result"] in {
                "completed", "recovered", "partial",
            }:
                continue
            result = strongest[1]["result"] if strongest else "missing_log"
            evidence_ids = [entity.id]
            if strongest:
                evidence_ids.append(strongest[0].id)
            add(_signal(
                kind="missed_routine",
                domain="habits",
                title=f"Missed routine: {entity.title}",
                summary=(
                    "The schedule expected this routine and no completed, recovered, or "
                    "partial log was stored for the date. Restia did not execute recovery."
                ),
                entity_ids=evidence_ids,
                discriminator=scheduled.isoformat(),
                urgency=25,
                importance=55,
                risk=20,
                calculation={
                    "scheduled_for": scheduled.isoformat(),
                    "state": result,
                    "schedule_timezone": schedule["timezone"],
                    "as_of_local": local_moment.isoformat(),
                    "rule": "expected schedule date lacks success or partial evidence",
                },
            ))

    # Home/admin: explicit due and expiry dates only.
    for entity in domain_rows["home"]:
        properties = home_props[entity.id]
        record_type = str(properties["record_type"])
        record_status = str(properties["details"].get("record_status") or "")
        if record_status in _HOME_TERMINAL[record_type]:
            continue
        for deadline_kind, field in (("due", "due_at"), ("expiry", "expires_at")):
            deadline = _stored_datetime(
                properties.get(field), field=f"home {entity.id} {field}"
            )
            if deadline is None or deadline > horizon_end:
                continue
            overdue = deadline < now
            add(_signal(
                kind="home_expiry" if deadline_kind == "expiry" else "home_due",
                domain="home",
                title=f"{deadline_kind.title()}: {entity.title}",
                summary=(
                    "A stored Home/Admin record has an explicit due or expiry date "
                    "inside the bounded report horizon. No renewal or external action ran."
                ),
                entity_ids=[entity.id],
                discriminator={"kind": deadline_kind, "at": _iso(deadline)},
                due_at=deadline,
                urgency=82 if overdue else 50,
                importance=72,
                risk=50,
                urgent=overdue,
                important=True,
                time_sensitive=True,
                calculation={
                    "record_type": record_type,
                    "record_status": record_status,
                    "deadline_kind": deadline_kind,
                    "deadline_at": _iso(deadline),
                    "overdue": overdue,
                    "rule": "explicit deadline is inside bounded horizon",
                },
            ))

    for signal in raw_signals:
        _apply_routing(signal, requested)

    def sort_key(signal: Mapping[str, Any]) -> tuple[Any, ...]:
        return (
            0 if signal["routing"]["channel"] == "interrupt" else 1,
            -int(signal["score"]["total"]),
            str(signal.get("due_at") or "9999-12-31T23:59:59Z"),
            str(signal["kind"]),
            str(signal["id"]),
        )

    raw_signals.sort(key=sort_key)
    selected = raw_signals[:bounded_limit]
    output_truncated = len(raw_signals) > bounded_limit

    for signal in selected:
        entity_ids = list(signal.pop("_entity_ids"))
        explicit_link_ids = set(signal.pop("_link_ids"))
        evidence_entities: list[dict[str, Any]] = []
        evidence_source_ids: set[str] = set()
        for entity_id in entity_ids:
            entity = entity_map.get(entity_id)
            if entity is None:
                raise ProactiveStateError(
                    f"Signal evidence references missing owner entity {entity_id}"
                )
            entity_sources = source_ids_by_entity.get(entity_id, set())
            evidence_source_ids.update(entity_sources)
            evidence_entities.append({
                "id": entity.id,
                "entity_type": entity.entity_type,
                "title": entity.title or "",
                "status": entity.status,
                "version": int(entity.version or 1),
                "confidence": int(entity.confidence or 0),
                "sensitivity": entity.sensitivity,
                "domain_ref_type": entity.domain_ref_type,
                "domain_ref_id": entity.domain_ref_id,
                "source_ids": sorted(entity_sources),
            })
        touching = {
            row.id
            for entity_id in entity_ids
            for row in links_by_entity.get(entity_id, [])
        }
        evidence_link_ids = sorted(explicit_link_ids | touching)[:20]
        signal["evidence"] = {
            "entities": evidence_entities,
            "sources": [
                _source_evidence(sources[source_id])
                for source_id in sorted(evidence_source_ids)
            ],
            "entity_links": [
                _link_evidence(links_by_id[link_id])
                for link_id in evidence_link_ids
                if link_id in links_by_id
            ],
        }

    scan_truncated = any(value["truncated"] for value in scans.values())
    truncated = bool(
        scan_truncated or raw_truncated or output_truncated
    )
    domain_counts: dict[str, int] = defaultdict(int)
    for signal in selected:
        domain_counts[str(signal["domain"])] += 1
    interruptions = [
        signal for signal in selected
        if signal["routing"]["channel"] == "interrupt"
    ]
    digest = [
        signal for signal in selected
        if signal["routing"]["channel"] == "digest"
    ]
    return {
        "schema_version": PROACTIVE_SCHEMA_VERSION,
        "owner_id": principal,
        "as_of": _iso(now),
        "as_of_offset": aware_as_of.isoformat(),
        "horizon_days": horizon,
        "horizon_end": _iso(horizon_end),
        "lookback_days": lookback,
        "items": selected,
        "interruptions": interruptions,
        "digest": digest,
        "count": len(selected),
        "total_signals_before_limit": len(raw_signals),
        "domain_counts": dict(sorted(domain_counts.items())),
        "scans": scans,
        "truncated": truncated,
        "truncation": {
            "scan_truncated": scan_truncated,
            "signal_generation_truncated": raw_truncated,
            "output_limit_truncated": output_truncated,
        },
        "routing_policy": {
            "interrupt_when": [
                "explicitly_requested",
                "high_risk",
                "urgent_and_important_and_time_sensitive",
            ],
            "otherwise": "digest",
        },
        "safety_policy": {
            "deterministic": True,
            "model_inference": False,
            "record_only": True,
            "can_mutate": False,
            "can_create_action_proposal": False,
            "can_execute_external_action": False,
            "can_send_or_notify": False,
            "health_is_factual_input_only": True,
            "financial_advice": False,
        },
    }


__all__ = [
    "DEFAULT_SCAN_LIMIT",
    "MAX_HORIZON_DAYS",
    "MAX_LOOKBACK_DAYS",
    "MAX_OUTPUT_LIMIT",
    "MAX_SCAN_LIMIT",
    "PROACTIVE_SCHEMA_VERSION",
    "ProactiveInputError",
    "ProactiveIntelligenceError",
    "ProactiveStateError",
    "proactive_intelligence_report",
]
