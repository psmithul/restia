"""Typed V3 Health & Fitness records on the canonical Life graph.

Health records deliberately remain owner-scoped ``LifeEntity`` rows.  This
module provides a bounded schema for observations and externally sourced facts;
it does not diagnose, prescribe, recommend treatment, or execute medication
changes.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping

from core.database import Account, LifeEntity, LifeSource
from src.life_graph import (
    LifeGraphError,
    LifeGraphNotFound,
    delete_life_entity,
    get_life_entity,
    list_life_entity_versions,
    serialize_life_entity,
    update_life_entity,
    create_life_entity,
)


HEALTH_SCHEMA_VERSION = 1
HEALTH_ENTITY_TYPE = "health_record"
HEALTH_SCAN_LIMIT = 500

HEALTH_RECORD_TYPES = frozenset({
    "weight",
    "measurement",
    "sleep",
    "exercise",
    "nutrition",
    "steps",
    "recovery",
    "water",
    "medication_reminder",
    "appointment",
    "report",
    "symptom",
    "mood_stress",
    "wearable_observation",
})

_SOURCE_KINDS = frozenset({
    "manual",
    "user_observation",
    "import",
    "wearable",
    "clinical_report",
    "prescription",
    "provider",
})
_IMPORT_SOURCE_KINDS = frozenset({
    "import", "wearable", "clinical_report", "prescription", "provider",
})
_SENSITIVITIES = frozenset({"private", "restricted"})
_STATUSES = frozenset({"active", "archived"})
_GROUPINGS = frozenset({"day", "week", "month"})
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

_UNITS = frozenset({
    "kg", "lb", "g", "mg", "mcg",
    "mm", "cm", "m", "in", "km", "mi",
    "ml", "l", "fl_oz",
    "min", "h", "ms",
    "kcal", "kj",
    "steps", "repetitions",
    "percent", "score_0_10", "score_0_100",
    "bpm", "breaths_per_min",
    "celsius", "fahrenheit",
    "mmol_l", "mg_dl", "m_s",
})

_METRIC_RULES: dict[str, dict[str, frozenset[str]]] = {
    "weight": {"weight": frozenset({"kg", "lb"})},
    "sleep": {
        "duration": frozenset({"min", "h"}),
        "quality": frozenset({"score_0_10", "score_0_100", "percent"}),
    },
    "exercise": {
        "duration": frozenset({"min", "h"}),
        "distance": frozenset({"m", "km", "mi"}),
        "energy": frozenset({"kcal", "kj"}),
        "repetitions": frozenset({"repetitions"}),
        "heart_rate": frozenset({"bpm"}),
    },
    "nutrition": {
        "energy": frozenset({"kcal", "kj"}),
        "protein": frozenset({"g", "mg"}),
        "carbohydrate": frozenset({"g", "mg"}),
        "fat": frozenset({"g", "mg"}),
        "fiber": frozenset({"g", "mg"}),
        "sodium": frozenset({"g", "mg"}),
    },
    "steps": {"steps": frozenset({"steps"})},
    "recovery": {
        "score": frozenset({"score_0_100", "percent"}),
        "hrv": frozenset({"ms"}),
        "resting_heart_rate": frozenset({"bpm"}),
    },
    "water": {"volume": frozenset({"ml", "l", "fl_oz"})},
    "symptom": {
        "severity": frozenset({"score_0_10"}),
        "pain": frozenset({"score_0_10"}),
    },
    "mood_stress": {
        "mood": frozenset({"score_0_10"}),
        "stress": frozenset({"score_0_10"}),
    },
}

_REQUIRES_METRICS = frozenset({
    "weight", "measurement", "sleep", "exercise", "nutrition", "steps",
    "recovery", "water", "mood_stress", "wearable_observation",
})
_REQUIRED_METRICS: dict[str, frozenset[str]] = {
    "weight": frozenset({"weight"}),
    "sleep": frozenset({"duration"}),
    "steps": frozenset({"steps"}),
    "water": frozenset({"volume"}),
}

_MEDICAL_MUTATION_KEYS = frozenset({
    "action",
    "execute",
    "executor",
    "tool_call",
    "diagnosis",
    "diagnose",
    "prescribe",
    "prescription",
    "treatment",
    "treatment_recommendation",
    "medication_change",
    "dose_change",
    "new_dose",
    "start_medication",
    "stop_medication",
})
_SECRET_KEY_PARTS = (
    "password", "secret", "token", "credential", "cookie", "authorization",
)

_URGENT_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("chest_pain", ("chest pain", "chest pressure")),
    ("breathing_difficulty", (
        "difficulty breathing", "shortness of breath", "can't breathe", "cannot breathe",
    )),
    ("severe_bleeding", ("severe bleeding", "heavy bleeding", "uncontrolled bleeding")),
    ("stroke_signs", (
        "stroke signs", "face drooping", "slurred speech", "one-sided weakness",
    )),
    ("loss_of_consciousness", ("unconscious", "loss of consciousness", "passed out")),
    ("seizure", ("seizure",)),
    ("severe_allergic_reaction", ("anaphylaxis", "throat swelling")),
    ("overdose_or_self_harm", (
        "overdose", "suicidal intent", "self harm", "self-harm",
    )),
)

URGENT_GUIDANCE = (
    "This may need urgent professional attention. Contact local emergency "
    "services or seek urgent in-person medical help now. Restia cannot "
    "diagnose, prescribe, or recommend medication changes."
)
NON_DIAGNOSTIC_NOTICE = (
    "Restia records health information but does not diagnose, prescribe, or "
    "recommend medication changes."
)


def _text(
    value: object,
    *,
    field: str,
    limit: int,
    required: bool = False,
    preserve_lines: bool = False,
) -> str:
    raw = str(value or "").strip()
    normalized = raw if preserve_lines else " ".join(raw.split())
    if required and not normalized:
        raise LifeGraphError(f"{field} is required")
    if len(normalized) > limit:
        raise LifeGraphError(f"{field} must not exceed {limit} characters")
    return normalized


def _identifier(value: object, *, field: str) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not _IDENTIFIER_RE.fullmatch(normalized):
        raise LifeGraphError(
            f"{field} must be a lowercase identifier using letters, numbers, _, -, or ."
        )
    return normalized


def _datetime(value: object | None, *, field: str, required: bool = False) -> datetime | None:
    if value is None or value == "":
        if required:
            raise LifeGraphError(f"{field} is required")
        return None
    parsed: datetime
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise LifeGraphError(f"{field} must be an ISO-8601 date or datetime") from exc
    else:
        raise LifeGraphError(f"{field} must be an ISO-8601 date or datetime")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _iso(value: object | None, *, field: str) -> str | None:
    parsed = _datetime(value, field=field)
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _json_object(value: object | None, *, field: str, max_bytes: int = 32_000) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LifeGraphError(f"{field} must be an object")
    result = dict(value)
    try:
        encoded = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise LifeGraphError(f"{field} must be JSON-serializable") from exc
    if len(encoded) > max_bytes:
        raise LifeGraphError(f"{field} must not exceed {max_bytes} bytes")
    return result


def _walk_keys(value: object) -> list[str]:
    keys: list[str] = []
    if isinstance(value, Mapping):
        for key, nested in value.items():
            keys.append(str(key).strip().lower())
            keys.extend(_walk_keys(nested))
    elif isinstance(value, list):
        for nested in value:
            keys.extend(_walk_keys(nested))
    return keys


def assert_no_autonomous_medical_mutation(value: object) -> None:
    """Reject executor-shaped or medical-advice fields at the typed boundary."""
    prohibited = sorted(set(_walk_keys(value)) & _MEDICAL_MUTATION_KEYS)
    if prohibited:
        raise LifeGraphError(
            "Health records cannot diagnose, prescribe, recommend treatment, or "
            "execute medication changes"
        )


def _private_metadata(value: object | None) -> dict[str, Any]:
    metadata = _json_object(value, field="private_metadata", max_bytes=16_000)
    for key in _walk_keys(metadata):
        if any(part in key for part in _SECRET_KEY_PARTS):
            raise LifeGraphError("private_metadata must not contain credentials or secrets")
    assert_no_autonomous_medical_mutation(metadata)
    return metadata


def _source(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LifeGraphError("source must be an object")
    allowed = {"kind", "label", "reference", "external_id", "source_id", "provider"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported source fields: {', '.join(unknown)}")
    kind = _identifier(value.get("kind"), field="source.kind")
    if kind not in _SOURCE_KINDS:
        raise LifeGraphError(
            "source.kind must be manual, user_observation, import, wearable, "
            "clinical_report, prescription, or provider"
        )
    source_id = _text(value.get("source_id"), field="source.source_id", limit=36) or None
    return {
        "kind": kind,
        "label": _text(value.get("label"), field="source.label", limit=240, required=True),
        "reference": _text(value.get("reference"), field="source.reference", limit=2_000) or None,
        "external_id": _text(value.get("external_id"), field="source.external_id", limit=500) or None,
        "source_id": source_id,
        "provider": _text(value.get("provider"), field="source.provider", limit=240) or None,
    }


def _metrics(value: object | None, *, record_type: str) -> list[dict[str, Any]]:
    if value is None:
        rows: list[object] = []
    elif isinstance(value, list):
        rows = value
    else:
        raise LifeGraphError("metrics must be a list")
    if len(rows) > 30:
        raise LifeGraphError("metrics must not contain more than 30 items")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    rules = _METRIC_RULES.get(record_type)
    for row in rows:
        if not isinstance(row, Mapping):
            raise LifeGraphError("each metric must be an object")
        unknown = sorted(set(row) - {"name", "value", "unit"})
        if unknown:
            raise LifeGraphError(f"Unsupported metric fields: {', '.join(unknown)}")
        name = _identifier(row.get("name"), field="metric.name")
        if name in seen:
            raise LifeGraphError("metric names must be unique within a health record")
        seen.add(name)
        if isinstance(row.get("value"), bool):
            raise LifeGraphError("metric.value must be a finite number")
        try:
            number = float(row.get("value"))
        except (TypeError, ValueError) as exc:
            raise LifeGraphError("metric.value must be a finite number") from exc
        if not math.isfinite(number):
            raise LifeGraphError("metric.value must be a finite number")
        unit = _identifier(row.get("unit"), field="metric.unit")
        if unit not in _UNITS:
            raise LifeGraphError(f"Unsupported metric unit: {unit}")
        if rules is not None:
            allowed_units = rules.get(name)
            if allowed_units is None:
                raise LifeGraphError(f"Unsupported {record_type} metric: {name}")
            if unit not in allowed_units:
                raise LifeGraphError(
                    f"{record_type}.{name} must use one of: {', '.join(sorted(allowed_units))}"
                )
        if unit == "percent" and not 0 <= number <= 100:
            raise LifeGraphError("percent metrics must be between 0 and 100")
        if unit == "score_0_10" and not 0 <= number <= 10:
            raise LifeGraphError("score_0_10 metrics must be between 0 and 10")
        if unit == "score_0_100" and not 0 <= number <= 100:
            raise LifeGraphError("score_0_100 metrics must be between 0 and 100")
        if unit in {"steps", "repetitions"} and (
            number < 0 or not number.is_integer()
        ):
            raise LifeGraphError(f"{unit} metrics must be non-negative whole numbers")
        if record_type in {
            "weight", "sleep", "exercise", "nutrition", "steps", "recovery", "water",
        } and number < 0:
            raise LifeGraphError(f"{record_type} metrics must not be negative")
        if record_type == "weight" and number == 0:
            raise LifeGraphError("weight metrics must be greater than zero")
        result.append({"name": name, "value": number, "unit": unit})
    if record_type in _REQUIRES_METRICS and not result:
        raise LifeGraphError(f"{record_type} records require at least one metric")
    required = _REQUIRED_METRICS.get(record_type, frozenset())
    missing = sorted(required - seen)
    if missing:
        raise LifeGraphError(
            f"{record_type} records require metric(s): {', '.join(missing)}"
        )
    return result


def _red_flags(value: object | None) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise LifeGraphError("details.red_flags must be a list")
    if len(value) > 20:
        raise LifeGraphError("details.red_flags must not contain more than 20 items")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        label = _text(item, field="details.red_flags", limit=240, required=True)
        marker = label.casefold()
        if marker not in seen:
            seen.add(marker)
            result.append(label)
    return result


def _details(value: object | None, *, record_type: str) -> dict[str, Any]:
    details = _json_object(value, field="details", max_bytes=32_000)
    assert_no_autonomous_medical_mutation(details)
    if record_type == "exercise":
        details["activity"] = _text(
            details.get("activity"), field="details.activity", limit=240, required=True
        )
    elif record_type == "symptom":
        details["description"] = _text(
            details.get("description"), field="details.description", limit=4_000,
            required=True, preserve_lines=True,
        )
        details["red_flags"] = _red_flags(details.get("red_flags"))
    elif record_type == "medication_reminder":
        if details.get("externally_prescribed") is not True:
            raise LifeGraphError(
                "Medication reminders may only record externally prescribed facts"
            )
        for field, limit in (
            ("medication_name", 240), ("schedule", 1_000), ("prescribed_by", 240),
        ):
            details[field] = _text(
                details.get(field), field=f"details.{field}", limit=limit, required=True
            )
        details["dosage"] = _text(
            details.get("dosage"), field="details.dosage", limit=500
        )
        if details.get("prescribed_at") is not None:
            details["prescribed_at"] = _iso(
                details.get("prescribed_at"), field="details.prescribed_at"
            )
    elif record_type == "appointment":
        details["provider"] = _text(
            details.get("provider"), field="details.provider", limit=240, required=True
        )
    elif record_type == "report":
        details["report_type"] = _text(
            details.get("report_type"), field="details.report_type", limit=240,
            required=True,
        )
    elif record_type == "wearable_observation":
        details["device"] = _text(
            details.get("device"), field="details.device", limit=240, required=True
        )
    return details


def _provenance(value: object | None, *, source: Mapping[str, Any]) -> dict[str, Any]:
    provenance = _json_object(value, field="provenance", max_bytes=16_000)
    assert_no_autonomous_medical_mutation(provenance)
    supplied_source_id = str(provenance.get("source_id") or "").strip() or None
    if supplied_source_id and supplied_source_id != source.get("source_id"):
        raise LifeGraphError("provenance.source_id must match source.source_id")
    provenance["source_kind"] = source["kind"]
    provenance["source_label"] = source["label"]
    if source.get("source_id"):
        provenance["source_id"] = source["source_id"]
    else:
        provenance.pop("source_id", None)
    return provenance


def health_safety_notice(properties: Mapping[str, Any]) -> dict[str, Any] | None:
    if properties.get("record_type") != "symptom":
        return None
    details = properties.get("details") if isinstance(properties.get("details"), Mapping) else {}
    reported_flags = [
        str(item) for item in details.get("red_flags", []) if item is not None
    ]
    text = " ".join([
        str(details.get("description") or ""),
        *reported_flags,
    ]).casefold()
    signals = [
        label for label, phrases in _URGENT_SIGNALS
        if any(phrase in text for phrase in phrases)
    ]
    if reported_flags:
        signals.append("reported_red_flag")
    signals = list(dict.fromkeys(signals))
    return {
        "urgent": bool(signals),
        "signals": signals,
        "message": URGENT_GUIDANCE if signals else NON_DIAGNOSTIC_NOTICE,
    }


def validate_health_properties(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LifeGraphError("health properties must be an object")
    record_type = _identifier(value.get("record_type"), field="record_type")
    if record_type not in HEALTH_RECORD_TYPES:
        raise LifeGraphError(
            "record_type must be one of: " + ", ".join(sorted(HEALTH_RECORD_TYPES))
        )
    source = _source(value.get("source"))
    details = _details(value.get("details"), record_type=record_type)
    if record_type == "medication_reminder":
        if source["kind"] not in {"prescription", "provider"}:
            raise LifeGraphError(
                "Medication reminders require a prescription or provider source"
            )
        if not (source.get("reference") or source.get("source_id")):
            raise LifeGraphError(
                "Medication reminders require an external prescription source reference"
            )
    recorded_at = _datetime(value.get("recorded_at"), field="recorded_at", required=True)
    ended_at = _datetime(value.get("ended_at"), field="ended_at")
    if ended_at is not None and recorded_at is not None and ended_at < recorded_at:
        raise LifeGraphError("ended_at must not be before recorded_at")
    if record_type == "sleep" and ended_at is None:
        raise LifeGraphError("sleep records require ended_at")
    return {
        "health_schema_version": HEALTH_SCHEMA_VERSION,
        "record_type": record_type,
        "recorded_at": _iso(recorded_at, field="recorded_at"),
        "ended_at": _iso(ended_at, field="ended_at"),
        "metrics": _metrics(value.get("metrics"), record_type=record_type),
        "details": details,
        "source": source,
        "private_metadata": _private_metadata(value.get("private_metadata")),
    }


def is_typed_health_payload(entity_type: object, properties: object | None = None) -> bool:
    normalized_type = str(entity_type or "").strip().lower()
    if normalized_type == HEALTH_ENTITY_TYPE:
        return True
    return isinstance(properties, Mapping) and (
        properties.get("health_schema_version") is not None
        or properties.get("record_type") in HEALTH_RECORD_TYPES
    )


def _owned_health_record(db, owner_id: str, entity_id: object) -> LifeEntity:
    entity = get_life_entity(db, owner_id=owner_id, entity_id=entity_id)
    if entity.entity_type != HEALTH_ENTITY_TYPE:
        raise LifeGraphNotFound("Health record not found")
    validate_health_properties(entity.properties or {})
    return entity


def _validate_source_authority(db, *, owner_id: str, source: Mapping[str, Any]) -> None:
    source_id = source.get("source_id")
    if source_id and db.query(LifeSource.id).filter(
        LifeSource.id == source_id, LifeSource.owner_id == owner_id,
    ).scalar() is None:
        raise LifeGraphNotFound("Health source not found")


def create_health_record(
    db,
    *,
    account: Account,
    record_type: object,
    title: object,
    recorded_at: object,
    metrics: object | None,
    details: object | None,
    source: object,
    note: object = "",
    ended_at: object | None = None,
    due_at: object | None = None,
    private_metadata: object | None = None,
    provenance: object | None = None,
    confidence: object = 100,
    sensitivity: object = "private",
    idempotency_key: object | None = None,
    import_mode: bool = False,
) -> tuple[LifeEntity, bool]:
    normalized_title = _text(title, field="title", limit=240, required=True)
    normalized_note = _text(note, field="note", limit=20_000, preserve_lines=True)
    normalized_sensitivity = _identifier(sensitivity, field="sensitivity")
    if normalized_sensitivity not in _SENSITIVITIES:
        raise LifeGraphError("Health record sensitivity must be private or restricted")
    properties = validate_health_properties({
        "record_type": record_type,
        "recorded_at": recorded_at,
        "ended_at": ended_at,
        "metrics": metrics,
        "details": details,
        "source": source,
        "private_metadata": private_metadata,
    })
    if import_mode:
        if properties["source"]["kind"] not in _IMPORT_SOURCE_KINDS:
            raise LifeGraphError("Imported health records require an external source kind")
        if not str(idempotency_key or "").strip():
            raise LifeGraphError("Imported health records require idempotency_key")
        if not (
            properties["source"].get("external_id")
            or properties["source"].get("reference")
            or properties["source"].get("source_id")
        ):
            raise LifeGraphError("Imported health records require an external source reference")
    _validate_source_authority(db, owner_id=account.id, source=properties["source"])
    normalized_provenance = _provenance(provenance, source=properties["source"])
    normalized_due_at = _datetime(due_at, field="due_at")
    if properties["record_type"] == "medication_reminder" and normalized_due_at is None:
        raise LifeGraphError("Medication reminders require due_at")
    entity, created = create_life_entity(
        db,
        account=account,
        entity_type=HEALTH_ENTITY_TYPE,
        title=normalized_title,
        summary=normalized_note,
        status="active",
        properties=properties,
        provenance=normalized_provenance,
        confidence=confidence,
        sensitivity=normalized_sensitivity,
        occurred_at=_datetime(recorded_at, field="recorded_at", required=True),
        due_at=normalized_due_at,
        idempotency_key=idempotency_key,
        reason="Health record captured",
    )
    return entity, created


def update_health_record(
    db,
    *,
    account: Account,
    entity_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
) -> LifeEntity:
    entity = _owned_health_record(db, account.id, entity_id)
    allowed = {
        "title", "recorded_at", "ended_at", "metrics", "details", "source",
        "note", "due_at", "private_metadata", "provenance", "confidence",
        "sensitivity", "status",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(f"Unsupported health fields: {', '.join(unknown)}")
    current = validate_health_properties(entity.properties or {})
    if "sensitivity" in changes:
        sensitivity = _identifier(changes["sensitivity"], field="sensitivity")
        if sensitivity not in _SENSITIVITIES:
            raise LifeGraphError("Health record sensitivity must be private or restricted")
    else:
        sensitivity = entity.sensitivity
    merged = dict(current)
    for field in (
        "recorded_at", "ended_at", "metrics", "details", "source", "private_metadata",
    ):
        if field in changes:
            merged[field] = changes[field]
    properties = validate_health_properties(merged)
    if properties["record_type"] == "medication_reminder" and "details" in changes:
        medication_fact_fields = {
            "medication_name", "dosage", "prescribed_by", "prescribed_at",
        }
        current_facts = {
            key: current["details"].get(key) for key in medication_fact_fields
        }
        updated_facts = {
            key: properties["details"].get(key) for key in medication_fact_fields
        }
        if current_facts != updated_facts and "source" not in changes:
            raise LifeGraphError(
                "Medication fact changes require an explicit external prescription source"
            )
    _validate_source_authority(db, owner_id=account.id, source=properties["source"])
    source_changed = properties["source"] != current["source"]
    provenance_value = changes.get("provenance", entity.provenance or {})
    if source_changed and "provenance" not in changes:
        provenance_value = dict(entity.provenance or {})
        provenance_value.pop("source_id", None)
    normalized_provenance = _provenance(
        provenance_value, source=properties["source"]
    )
    entity_changes: dict[str, Any] = {
        "properties": properties,
        "provenance": normalized_provenance,
    }
    if "title" in changes:
        entity_changes["title"] = _text(
            changes["title"], field="title", limit=240, required=True
        )
    if "note" in changes:
        entity_changes["summary"] = _text(
            changes["note"], field="note", limit=20_000, preserve_lines=True
        )
    if "recorded_at" in changes:
        entity_changes["occurred_at"] = _datetime(
            changes["recorded_at"], field="recorded_at", required=True
        )
    if "due_at" in changes:
        entity_changes["due_at"] = _datetime(changes["due_at"], field="due_at")
    effective_due = entity_changes.get("due_at", entity.due_at)
    if properties["record_type"] == "medication_reminder" and effective_due is None:
        raise LifeGraphError("Medication reminders require due_at")
    if "confidence" in changes:
        entity_changes["confidence"] = changes["confidence"]
    if "sensitivity" in changes:
        entity_changes["sensitivity"] = sensitivity
    if "status" in changes:
        status = _identifier(changes["status"], field="status")
        if status not in _STATUSES:
            raise LifeGraphError("Health record status must be active or archived")
        entity_changes["status"] = status
    return update_life_entity(
        db,
        owner_id=account.id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=entity_changes,
        reason="Health record updated",
    )


def delete_health_record(
    db,
    *,
    owner_id: str,
    entity_id: object,
    expected_version: int,
    reason: object = "Health record deleted",
) -> LifeEntity:
    entity = _owned_health_record(db, owner_id, entity_id)
    return delete_life_entity(
        db,
        owner_id=owner_id,
        entity_id=entity.id,
        expected_version=expected_version,
        reason=reason,
    )


def serialize_health_record(entity: LifeEntity) -> dict[str, Any]:
    if entity.entity_type != HEALTH_ENTITY_TYPE:
        raise LifeGraphError("Entity is not a health record")
    properties = validate_health_properties(entity.properties or {})
    payload = serialize_life_entity(entity)
    payload.update({
        "record_type": properties["record_type"],
        "recorded_at": payload["occurred_at"],
        "ended_at": properties["ended_at"],
        "metrics": properties["metrics"],
        "details": properties["details"],
        "source": properties["source"],
        "private_metadata": properties["private_metadata"],
        "note": payload["summary"],
        "safety": health_safety_notice(properties),
    })
    return payload


def get_health_record(db, *, owner_id: str, entity_id: object) -> dict[str, Any]:
    return serialize_health_record(_owned_health_record(db, owner_id, entity_id))


def _record_candidates(
    db,
    *,
    owner_id: str,
    record_type: object | None = None,
    source_kind: object | None = None,
    from_at: object | None = None,
    to_at: object | None = None,
) -> tuple[list[LifeEntity], bool]:
    normalized_type = None
    if record_type:
        normalized_type = _identifier(record_type, field="record_type")
        if normalized_type not in HEALTH_RECORD_TYPES:
            raise LifeGraphError("Unsupported health record_type")
    normalized_source = None
    if source_kind:
        normalized_source = _identifier(source_kind, field="source_kind")
        if normalized_source not in _SOURCE_KINDS:
            raise LifeGraphError("Unsupported health source_kind")
    start = _datetime(from_at, field="from_at")
    end = _datetime(to_at, field="to_at")
    if start and end and start > end:
        raise LifeGraphError("from_at must not be after to_at")
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == HEALTH_ENTITY_TYPE,
        LifeEntity.deleted_at.is_(None),
    )
    if start is not None:
        query = query.filter(LifeEntity.occurred_at >= start)
    if end is not None:
        query = query.filter(LifeEntity.occurred_at <= end)
    rows = query.order_by(
        LifeEntity.occurred_at.desc(), LifeEntity.updated_at.desc(), LifeEntity.id.desc()
    ).limit(HEALTH_SCAN_LIMIT + 1).all()
    truncated = len(rows) > HEALTH_SCAN_LIMIT
    result: list[LifeEntity] = []
    for row in rows[:HEALTH_SCAN_LIMIT]:
        try:
            properties = validate_health_properties(row.properties or {})
        except LifeGraphError:
            continue
        if normalized_type and properties["record_type"] != normalized_type:
            continue
        if normalized_source and properties["source"]["kind"] != normalized_source:
            continue
        result.append(row)
    return result, truncated


def list_health_records(
    db,
    *,
    owner_id: str,
    record_type: object | None = None,
    source_kind: object | None = None,
    from_at: object | None = None,
    to_at: object | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    bounded = max(1, min(100, int(limit)))
    rows, scan_truncated = _record_candidates(
        db,
        owner_id=owner_id,
        record_type=record_type,
        source_kind=source_kind,
        from_at=from_at,
        to_at=to_at,
    )
    return [serialize_health_record(row) for row in rows[:bounded]], (
        scan_truncated or len(rows) > bounded
    )


def search_health_records(
    db,
    *,
    owner_id: str,
    query_text: object,
    record_type: object | None = None,
    source_kind: object | None = None,
    from_at: object | None = None,
    to_at: object | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    needle = str(query_text or "").strip().casefold()
    if not needle:
        raise LifeGraphError("q is required")
    bounded = max(1, min(100, int(limit)))
    rows, scan_truncated = _record_candidates(
        db,
        owner_id=owner_id,
        record_type=record_type,
        source_kind=source_kind,
        from_at=from_at,
        to_at=to_at,
    )
    matches: list[tuple[tuple[int, str, str], dict[str, Any]]] = []
    for entity in rows:
        record = serialize_health_record(entity)
        title = str(record["title"]).casefold()
        note = str(record["note"]).casefold()
        body = json.dumps(
            {
                "record_type": record["record_type"],
                "details": record["details"],
                "source": record["source"],
                "metrics": record["metrics"],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).casefold()
        if title == needle:
            rank, field = 0, "title"
        elif title.startswith(needle):
            rank, field = 1, "title"
        elif needle in title:
            rank, field = 2, "title"
        elif needle in note:
            rank, field = 3, "note"
        elif needle in body:
            rank, field = 4, "properties"
        else:
            continue
        matches.append(((rank, title, entity.id), {"record": record, "match": field, "rank": rank}))
    matches.sort(key=lambda row: row[0])
    items = [item for _, item in matches[:bounded]]
    return {
        "items": items,
        "count": len(items),
        "scanned": len(rows),
        "truncated": scan_truncated or len(matches) > bounded,
    }


def _bucket_start(value: datetime, grouping: str) -> date:
    if grouping == "day":
        return value.date()
    if grouping == "week":
        return (value - timedelta(days=value.weekday())).date()
    return value.date().replace(day=1)


def health_trends(
    db,
    *,
    owner_id: str,
    record_type: object,
    metric: object,
    group_by: object = "day",
    unit: object | None = None,
    from_at: object | None = None,
    to_at: object | None = None,
) -> dict[str, Any]:
    normalized_type = _identifier(record_type, field="record_type")
    if normalized_type not in HEALTH_RECORD_TYPES:
        raise LifeGraphError("Unsupported health record_type")
    normalized_metric = _identifier(metric, field="metric")
    grouping = _identifier(group_by, field="group_by")
    if grouping not in _GROUPINGS:
        raise LifeGraphError("group_by must be day, week, or month")
    normalized_unit = _identifier(unit, field="unit") if unit else None
    if normalized_unit and normalized_unit not in _UNITS:
        raise LifeGraphError("Unsupported health trend unit")
    rows, scan_truncated = _record_candidates(
        db,
        owner_id=owner_id,
        record_type=normalized_type,
        from_at=from_at,
        to_at=to_at,
    )
    grouped: dict[tuple[date, str], list[tuple[datetime, float]]] = {}
    for entity in rows:
        when = entity.occurred_at
        if when is None:
            continue
        properties = validate_health_properties(entity.properties or {})
        for row in properties["metrics"]:
            if row["name"] != normalized_metric:
                continue
            if normalized_unit and row["unit"] != normalized_unit:
                continue
            grouped.setdefault((_bucket_start(when, grouping), row["unit"]), []).append(
                (when, float(row["value"]))
            )
    buckets: list[dict[str, Any]] = []
    for (period, bucket_unit), values in sorted(grouped.items()):
        values.sort(key=lambda row: row[0])
        numbers = [row[1] for row in values]
        buckets.append({
            "period_start": period.isoformat(),
            "unit": bucket_unit,
            "count": len(numbers),
            "minimum": min(numbers),
            "maximum": max(numbers),
            "average": sum(numbers) / len(numbers),
            "sum": sum(numbers),
            "latest": numbers[-1],
        })
    return {
        "record_type": normalized_type,
        "metric": normalized_metric,
        "group_by": grouping,
        "unit": normalized_unit,
        "from_at": _iso(from_at, field="from_at"),
        "to_at": _iso(to_at, field="to_at"),
        "buckets": buckets,
        "count": len(buckets),
        "scanned": len(rows),
        "truncated": scan_truncated,
    }


def health_record_history(
    db, *, owner_id: str, entity_id: object, limit: int = 50
) -> tuple[list[dict[str, Any]], bool]:
    entity = _owned_health_record(db, owner_id, entity_id)
    rows, truncated = list_life_entity_versions(
        db, owner_id=owner_id, entity_id=entity.id, limit=limit
    )
    chronological = list(reversed(rows))
    result: list[dict[str, Any]] = []
    previous: Mapping[str, Any] | None = None
    for row in chronological:
        snapshot = dict(row.snapshot or {})
        properties = validate_health_properties(snapshot.get("properties") or {})
        changed: list[str] = []
        if previous is not None:
            previous_properties = validate_health_properties(
                previous.get("properties") or {}
            )
            for field in (
                "title", "summary", "status", "occurred_at", "due_at",
                "confidence", "sensitivity", "provenance",
            ):
                if previous.get(field) != snapshot.get(field):
                    changed.append(field)
            for field in (
                "metrics", "details", "source", "private_metadata", "ended_at",
            ):
                if previous_properties.get(field) != properties.get(field):
                    changed.append(field)
        result.append({
            "id": row.id,
            "version": int(row.version),
            "created_at": row.created_at.replace(tzinfo=timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "reason": row.reason or "",
            "kind": "created" if previous is None else "health_record_changed",
            "changed_fields": changed,
            "record": {
                "title": snapshot.get("title") or "",
                "status": snapshot.get("status") or "active",
                "record_type": properties["record_type"],
                "recorded_at": snapshot.get("occurred_at"),
                "due_at": snapshot.get("due_at"),
                "metrics": properties["metrics"],
                "source": properties["source"],
            },
        })
        previous = snapshot
    return list(reversed(result)), truncated
