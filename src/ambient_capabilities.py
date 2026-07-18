"""Owner-scoped capability contracts for Restia's ambient clients.

Ambient inputs are evidence, not instructions.  Mobile, wearable, location,
camera, and transcription clients can capture bounded private observations and
sync them through the same ``Account.id``/LifeSource authority as the rest of
V3.  Captured text is never interpreted as a tool call and smart-home changes
are prepared behind the action-policy boundary rather than executed here.

The capability configuration itself is stored as encrypted, versioned profile
preference data.  This avoids a second device-settings store and makes the same
permissions visible to browser, native, CLI, and future Home Link clients.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping

from sqlalchemy import and_, or_

from core.database import Account, LifeSource
from src.action_policy import ProposalCreation, create_action_proposal
from src.action_policy import (
    complete_action,
    fail_action,
    get_action_proposal,
    start_action,
)
from src.life_core import append_action_audit
from src.life_graph import create_life_source, serialize_life_source
from src.profile_configuration_service import (
    ProfileConfigurationConflict,
    ProfileConfigurationNotFound,
    get_configuration,
    put_configuration,
    serialize_configuration,
)


MAX_CAPTURE_BYTES = 64 * 1024
MAX_OFFLINE_BATCH = 50
AMBIENT_CONFIG_PREFIX = "ambient."


class AmbientCapabilityError(ValueError):
    """A capability, permission, payload, or cursor is invalid."""


class AmbientCapabilityDenied(AmbientCapabilityError):
    """The owner has not granted the requested ambient operation."""


@dataclass(frozen=True, slots=True)
class SmartHomeExecutionRequest:
    proposal_id: str
    proposal_version: int
    integration_id: str
    method: str
    path: str
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AmbientCapabilityDefinition:
    id: str
    label: str
    source_type: str | None
    operations: frozenset[str]
    default_enabled: bool = False
    external_action: bool = False
    description: str = ""


AMBIENT_CAPABILITIES: Mapping[str, AmbientCapabilityDefinition] = MappingProxyType({
    "mobile_voice": AmbientCapabilityDefinition(
        id="mobile_voice",
        label="Mobile voice",
        source_type="voice",
        operations=frozenset({"capture", "read"}),
        description="Private voice transcripts captured by an authenticated client.",
    ),
    "location": AmbientCapabilityDefinition(
        id="location",
        label="Location awareness",
        source_type="location",
        operations=frozenset({"capture", "read"}),
        description="Explicit location observations; background access is opt-in.",
    ),
    "wearable": AmbientCapabilityDefinition(
        id="wearable",
        label="Wearables",
        source_type="wearable",
        operations=frozenset({"capture", "read"}),
        description="Device measurements captured as evidence without medical inference.",
    ),
    "transcription": AmbientCapabilityDefinition(
        id="transcription",
        label="Call and meeting transcription",
        source_type="transcription",
        operations=frozenset({"capture", "read"}),
        description="Call or meeting transcripts with explicit participant-consent evidence.",
    ),
    "camera": AmbientCapabilityDefinition(
        id="camera",
        label="Camera capture",
        source_type="camera",
        operations=frozenset({"capture", "read"}),
        description="Extracted text or an existing private asset reference; raw bytes use uploads.",
    ),
    "smart_home": AmbientCapabilityDefinition(
        id="smart_home",
        label="Smart home",
        source_type="smart_home",
        operations=frozenset({"capture", "read", "prepare_action"}),
        external_action=True,
        description="State observations plus human-approved external action proposals.",
    ),
    "offline": AmbientCapabilityDefinition(
        id="offline",
        label="Offline operation",
        source_type=None,
        operations=frozenset({"sync"}),
        default_enabled=True,
        description="Idempotent replay of an authenticated client's bounded capture outbox.",
    ),
    "cross_device": AmbientCapabilityDefinition(
        id="cross_device",
        label="Cross-device continuity",
        source_type=None,
        operations=frozenset({"read", "sync"}),
        default_enabled=True,
        description="Account-scoped cursor reads over ambient evidence from every interface.",
    ),
})

_SOURCE_TO_CAPABILITY = {
    definition.source_type: definition.id
    for definition in AMBIENT_CAPABILITIES.values()
    if definition.source_type is not None
}

_SMART_HOME_OPERATIONS = frozenset({
    "turn_on", "turn_off", "set_level", "set_temperature", "lock", "unlock",
})


def _definition(capability: object) -> AmbientCapabilityDefinition:
    normalized = str(capability or "").strip().lower().replace("-", "_")
    definition = AMBIENT_CAPABILITIES.get(normalized)
    if definition is None:
        raise AmbientCapabilityError("Unknown ambient capability")
    return definition


def _config_key(capability: str) -> str:
    return AMBIENT_CONFIG_PREFIX + capability


def _default_config(definition: AmbientCapabilityDefinition) -> dict[str, Any]:
    return {
        "enabled": definition.default_enabled,
        "operations": sorted(definition.operations),
        "local_only": True,
        "retention_days": 30,
        "require_device_unlock": True,
        # This is a server invariant, not a preference.  A client cannot turn
        # private ambient evidence into model-training data through this API.
        "no_training": True,
    }


def _normalize_config(
    definition: AmbientCapabilityDefinition,
    value: object,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AmbientCapabilityError("Ambient capability configuration must be an object")
    unknown = set(value) - {
        "enabled", "operations", "local_only", "retention_days",
        "require_device_unlock", "no_training",
    }
    if unknown:
        raise AmbientCapabilityError("Ambient capability configuration contains unsupported fields")
    enabled = value.get("enabled", definition.default_enabled)
    local_only = value.get("local_only", True)
    require_unlock = value.get("require_device_unlock", True)
    if not all(isinstance(item, bool) for item in (enabled, local_only, require_unlock)):
        raise AmbientCapabilityError("Ambient capability flags must be true or false")
    if value.get("no_training", True) is not True:
        raise AmbientCapabilityDenied("Private ambient data cannot be enabled for training")
    raw_operations = value.get("operations", sorted(definition.operations))
    if (
        not isinstance(raw_operations, list)
        or any(not isinstance(item, str) for item in raw_operations)
    ):
        raise AmbientCapabilityError("Ambient operations must be a list of strings")
    operations = {item.strip().lower() for item in raw_operations}
    if "" in operations or not operations <= definition.operations:
        raise AmbientCapabilityDenied("Capability operation was not granted by the server contract")
    raw_retention = value.get("retention_days", 30)
    if isinstance(raw_retention, bool):
        raise AmbientCapabilityError("retention_days must be an integer from 1 to 3650")
    try:
        retention_days = int(raw_retention)
    except (TypeError, ValueError) as exc:
        raise AmbientCapabilityError("retention_days must be an integer from 1 to 3650") from exc
    if retention_days < 1 or retention_days > 3650:
        raise AmbientCapabilityError("retention_days must be an integer from 1 to 3650")
    return {
        "enabled": enabled,
        "operations": sorted(operations),
        "local_only": local_only,
        "retention_days": retention_days,
        "require_device_unlock": require_unlock,
        "no_training": True,
    }


def get_ambient_capability(
    db,
    *,
    owner_id: str,
    capability: object,
) -> dict[str, Any]:
    definition = _definition(capability)
    version = 0
    persisted = False
    updated_at = None
    config = _default_config(definition)
    try:
        record = get_configuration(
            db,
            owner_id=owner_id,
            namespace="preference",
            key=_config_key(definition.id),
        )
    except ProfileConfigurationNotFound:
        record = None
    if record is not None:
        serialized = serialize_configuration(record)
        config = _normalize_config(definition, serialized["value"])
        version = int(serialized["version"])
        persisted = True
        updated_at = serialized["updated_at"]
    return {
        "id": definition.id,
        "label": definition.label,
        "description": definition.description,
        "source_type": definition.source_type,
        "external_action": definition.external_action,
        "supported_operations": sorted(definition.operations),
        "enabled": config["enabled"],
        "operations": config["operations"],
        "local_only": config["local_only"],
        "retention_days": config["retention_days"],
        "require_device_unlock": config["require_device_unlock"],
        "no_training": True,
        "version": version,
        "persisted": persisted,
        "updated_at": updated_at,
    }


def list_ambient_capabilities(db, *, owner_id: str) -> list[dict[str, Any]]:
    return [
        get_ambient_capability(db, owner_id=owner_id, capability=capability)
        for capability in AMBIENT_CAPABILITIES
    ]


def set_ambient_capability(
    db,
    *,
    account: Account,
    capability: object,
    value: object,
    expected_version: int | None,
    source: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    definition = _definition(capability)
    normalized = _normalize_config(definition, value)
    before = get_ambient_capability(
        db, owner_id=account.id, capability=definition.id,
    )
    try:
        result = put_configuration(
            db,
            account=account,
            namespace="preference",
            key=_config_key(definition.id),
            value=normalized,
            expected_version=expected_version,
            source=source,
            idempotency_key=idempotency_key,
        )
    except ProfileConfigurationConflict:
        raise
    after = get_ambient_capability(
        db, owner_id=account.id, capability=definition.id,
    )
    if result.changed:
        append_action_audit(
            db,
            owner_id=account.id,
            action="ambient.capability.updated",
            entity_type="ambient_capability",
            entity_id=definition.id,
            reason="Owner changed an ambient capability grant",
            before_state={
                "enabled": before["enabled"],
                "operations": before["operations"],
                "version": before["version"],
            },
            after_state={
                "enabled": after["enabled"],
                "operations": after["operations"],
                "version": after["version"],
            },
            details={
                "local_only": after["local_only"],
                "require_device_unlock": after["require_device_unlock"],
                "no_training": True,
            },
        )
    after["changed"] = result.changed
    after["idempotent"] = result.idempotent
    return after


def _require_operation(
    db,
    *,
    owner_id: str,
    capability: object,
    operation: str,
    trusted_device_session: bool,
) -> tuple[AmbientCapabilityDefinition, dict[str, Any]]:
    definition = _definition(capability)
    state = get_ambient_capability(
        db, owner_id=owner_id, capability=definition.id,
    )
    if not state["enabled"]:
        raise AmbientCapabilityDenied(f"{definition.label} is disabled")
    if operation not in state["operations"]:
        raise AmbientCapabilityDenied(
            f"{operation} permission is not granted for {definition.label}"
        )
    if state["require_device_unlock"] and not trusted_device_session:
        raise AmbientCapabilityDenied(
            f"{definition.label} requires an authenticated unlocked-device session"
        )
    return definition, state


def _bounded_text(value: object, field: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise AmbientCapabilityError(f"{field} must be text")
    text = value.strip()
    if required and not text:
        raise AmbientCapabilityError(f"{field} is required")
    if len(text) > limit:
        raise AmbientCapabilityError(f"{field} exceeds the length limit")
    return text


def _number(value: object, field: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool):
        raise AmbientCapabilityError(f"{field} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AmbientCapabilityError(f"{field} must be numeric") from exc
    if result < minimum or result > maximum:
        raise AmbientCapabilityError(f"{field} is outside the allowed range")
    return result


def _validate_capture_payload(
    definition: AmbientCapabilityDefinition,
    payload: object,
) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(payload, Mapping):
        raise AmbientCapabilityError("Ambient capture payload must be an object")
    value = dict(payload)
    title = definition.label
    excerpt = ""
    metadata: dict[str, Any]
    if definition.id == "mobile_voice":
        allowed = {"transcript", "language", "device_ref", "confidence"}
        if set(value) - allowed:
            raise AmbientCapabilityError("Mobile voice capture contains unsupported fields")
        excerpt = _bounded_text(value.get("transcript"), "transcript", 50_000, required=True)
        metadata = {
            "transcript": excerpt,
            "language": _bounded_text(str(value.get("language") or ""), "language", 32),
            "device_ref": _bounded_text(str(value.get("device_ref") or ""), "device_ref", 255),
            "confidence": int(_number(value.get("confidence", 100), "confidence", 0, 100)),
        }
    elif definition.id == "location":
        allowed = {"latitude", "longitude", "accuracy_m", "label", "device_ref"}
        if set(value) - allowed:
            raise AmbientCapabilityError("Location capture contains unsupported fields")
        latitude = _number(value.get("latitude"), "latitude", -90, 90)
        longitude = _number(value.get("longitude"), "longitude", -180, 180)
        accuracy = _number(value.get("accuracy_m", 0), "accuracy_m", 0, 100_000)
        label = _bounded_text(str(value.get("label") or ""), "label", 240)
        excerpt = label or "Location observation"
        metadata = {
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_m": accuracy,
            "label": label,
            "device_ref": _bounded_text(str(value.get("device_ref") or ""), "device_ref", 255),
        }
    elif definition.id == "wearable":
        allowed = {"metrics", "device_ref", "activity"}
        if set(value) - allowed:
            raise AmbientCapabilityError("Wearable capture contains unsupported fields")
        raw_metrics = value.get("metrics")
        if not isinstance(raw_metrics, list) or not raw_metrics or len(raw_metrics) > 50:
            raise AmbientCapabilityError("Wearable metrics must contain 1 to 50 items")
        metrics: list[dict[str, Any]] = []
        for raw in raw_metrics:
            if not isinstance(raw, Mapping) or set(raw) - {"name", "value", "unit"}:
                raise AmbientCapabilityError("Wearable metric is invalid")
            metric_value = raw.get("value")
            if isinstance(metric_value, bool) or not isinstance(metric_value, (int, float)):
                raise AmbientCapabilityError("Wearable metric value must be numeric")
            metrics.append({
                "name": _bounded_text(raw.get("name"), "metric name", 64, required=True),
                "value": metric_value,
                "unit": _bounded_text(str(raw.get("unit") or ""), "metric unit", 32),
            })
        activity = _bounded_text(str(value.get("activity") or ""), "activity", 120)
        excerpt = activity or f"{len(metrics)} wearable measurement(s)"
        metadata = {
            "metrics": metrics,
            "activity": activity,
            "device_ref": _bounded_text(str(value.get("device_ref") or ""), "device_ref", 255),
            "medical_inference_performed": False,
        }
    elif definition.id == "transcription":
        allowed = {"text", "meeting_title", "participants_consented", "device_ref"}
        if set(value) - allowed:
            raise AmbientCapabilityError("Transcription capture contains unsupported fields")
        if value.get("participants_consented") is not True:
            raise AmbientCapabilityDenied("Transcription capture requires participant-consent evidence")
        transcript = _bounded_text(value.get("text"), "text", 50_000, required=True)
        meeting_title = _bounded_text(
            str(value.get("meeting_title") or "Meeting transcript"),
            "meeting_title",
            240,
            required=True,
        )
        title = meeting_title
        excerpt = transcript
        metadata = {
            "transcript": transcript,
            "participants_consented": True,
            "device_ref": _bounded_text(str(value.get("device_ref") or ""), "device_ref", 255),
        }
    elif definition.id == "camera":
        allowed = {"extracted_text", "asset_id", "caption", "device_ref"}
        if set(value) - allowed:
            raise AmbientCapabilityError("Camera capture contains unsupported fields")
        extracted = _bounded_text(str(value.get("extracted_text") or ""), "extracted_text", 50_000)
        asset_id = _bounded_text(str(value.get("asset_id") or ""), "asset_id", 255)
        if not extracted and not asset_id:
            raise AmbientCapabilityError("Camera capture requires extracted_text or asset_id")
        caption = _bounded_text(str(value.get("caption") or "Camera capture"), "caption", 240)
        title = caption or "Camera capture"
        excerpt = extracted or caption
        metadata = {
            "extracted_text": extracted,
            "asset_id": asset_id,
            "caption": caption,
            "device_ref": _bounded_text(str(value.get("device_ref") or ""), "device_ref", 255),
            "raw_bytes_accepted": False,
        }
    elif definition.id == "smart_home":
        allowed = {"entity_id", "state", "attributes", "integration_id"}
        if set(value) - allowed:
            raise AmbientCapabilityError("Smart-home observation contains unsupported fields")
        entity_id = _bounded_text(value.get("entity_id"), "entity_id", 255, required=True)
        state = _bounded_text(str(value.get("state") or ""), "state", 240, required=True)
        attributes = value.get("attributes", {})
        if not isinstance(attributes, Mapping):
            raise AmbientCapabilityError("Smart-home attributes must be an object")
        metadata = {
            "entity_id": entity_id,
            "state": state,
            "attributes": dict(attributes),
            "integration_id": _bounded_text(
                str(value.get("integration_id") or ""), "integration_id", 160,
            ),
        }
        title = entity_id
        excerpt = state
    else:
        raise AmbientCapabilityError("This ambient capability does not accept captures")

    canonical = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(canonical.encode("utf-8")) > MAX_CAPTURE_BYTES:
        raise AmbientCapabilityError("Ambient capture exceeds the size limit")
    # LifeSource.safe_excerpt is encrypted too, but keep its public API bound.
    return title, excerpt[:4_000], metadata


def capture_ambient_signal(
    db,
    *,
    account: Account,
    capability: object,
    payload: object,
    observed_at: datetime | None,
    idempotency_key: object,
    trusted_device_session: bool,
) -> tuple[LifeSource, bool]:
    definition, state = _require_operation(
        db,
        owner_id=account.id,
        capability=capability,
        operation="capture",
        trusted_device_session=trusted_device_session,
    )
    if definition.source_type is None:
        raise AmbientCapabilityError("This ambient capability does not accept captures")
    raw_idempotency = _bounded_text(
        idempotency_key, "idempotency_key", 256, required=True,
    )
    title, excerpt, metadata = _validate_capture_payload(definition, payload)
    canonical = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    source_ref = "ambient:" + hashlib.sha256(
        f"{account.id}\0{definition.id}\0{raw_idempotency}".encode("utf-8")
    ).hexdigest()
    stored_metadata = {
        "ambient_capability": definition.id,
        "capture": metadata,
        "retention_days": state["retention_days"],
        "local_only": state["local_only"],
        "no_training": True,
        "interpreted_as_instruction": False,
    }
    return create_life_source(
        db,
        account=account,
        source_type=definition.source_type,
        title=title,
        source_ref=source_ref,
        safe_excerpt=excerpt,
        content_sha256=content_hash,
        observed_at=observed_at,
        sensitivity="private",
        metadata=stored_metadata,
        idempotency_key=f"ambient:{definition.id}:{raw_idempotency}",
    )


def sync_offline_captures(
    db,
    *,
    account: Account,
    captures: object,
    trusted_device_session: bool,
) -> list[dict[str, Any]]:
    _require_operation(
        db,
        owner_id=account.id,
        capability="offline",
        operation="sync",
        trusted_device_session=trusted_device_session,
    )
    if not isinstance(captures, list) or not captures or len(captures) > MAX_OFFLINE_BATCH:
        raise AmbientCapabilityError(
            f"Offline sync must contain 1 to {MAX_OFFLINE_BATCH} captures"
        )
    results: list[dict[str, Any]] = []
    for index, item in enumerate(captures):
        if not isinstance(item, Mapping) or set(item) - {
            "capability", "payload", "observed_at", "idempotency_key",
        }:
            raise AmbientCapabilityError(f"Offline capture {index} is invalid")
        observed = item.get("observed_at")
        if observed is not None and not isinstance(observed, datetime):
            raise AmbientCapabilityError(f"Offline capture {index} observed_at is invalid")
        source, created = capture_ambient_signal(
            db,
            account=account,
            capability=item.get("capability"),
            payload=item.get("payload"),
            observed_at=observed,
            idempotency_key=item.get("idempotency_key"),
            trusted_device_session=trusted_device_session,
        )
        results.append({"source": serialize_life_source(source), "created": created})
    return results


def _cursor_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def _encode_cursor(row: LifeSource) -> str:
    payload = json.dumps(
        {"captured_at": _cursor_datetime(row.captured_at), "id": row.id},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: object) -> tuple[datetime, str] | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) > 512:
        raise AmbientCapabilityError("Continuity cursor is invalid")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
        payload = json.loads(raw.decode("utf-8"))
        captured = datetime.fromisoformat(str(payload["captured_at"]).replace("Z", "+00:00"))
        row_id = str(payload["id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeError) as exc:
        raise AmbientCapabilityError("Continuity cursor is invalid") from exc
    if not row_id or len(row_id) > 64:
        raise AmbientCapabilityError("Continuity cursor is invalid")
    if captured.tzinfo is not None:
        captured = captured.astimezone(timezone.utc).replace(tzinfo=None)
    return captured, row_id


def ambient_continuity(
    db,
    *,
    owner_id: str,
    cursor: object = None,
    capability: object | None = None,
    limit: int = 50,
    trusted_device_session: bool,
) -> dict[str, Any]:
    _require_operation(
        db,
        owner_id=owner_id,
        capability="cross_device",
        operation="read",
        trusted_device_session=trusted_device_session,
    )
    bounded = max(1, min(100, int(limit)))
    source_types = set(_SOURCE_TO_CAPABILITY)
    if capability is not None:
        definition = _definition(capability)
        if definition.source_type is None:
            raise AmbientCapabilityError("Capability has no continuity evidence")
        source_types = {definition.source_type}
    query = db.query(LifeSource).filter(
        LifeSource.owner_id == owner_id,
        LifeSource.source_type.in_(sorted(source_types)),
    )
    decoded = _decode_cursor(cursor)
    if decoded is not None:
        captured_at, row_id = decoded
        query = query.filter(or_(
            LifeSource.captured_at > captured_at,
            and_(LifeSource.captured_at == captured_at, LifeSource.id > row_id),
        ))
    rows = query.order_by(
        LifeSource.captured_at.asc(), LifeSource.id.asc(),
    ).limit(bounded + 1).all()
    visible = rows[:bounded]
    items = []
    for row in visible:
        serialized = serialize_life_source(row)
        serialized["capability"] = _SOURCE_TO_CAPABILITY[row.source_type]
        items.append(serialized)
    return {
        "items": items,
        "count": len(items),
        "has_more": len(rows) > bounded,
        "next_cursor": _encode_cursor(visible[-1]) if visible else str(cursor or "") or None,
    }


def prepare_smart_home_action(
    db,
    *,
    account: Account,
    operation: object,
    integration_id: object,
    entity_id: object,
    parameters: object,
    reason: object,
    sources: object,
    idempotency_key: object,
    trusted_device_session: bool,
) -> ProposalCreation:
    _require_operation(
        db,
        owner_id=account.id,
        capability="smart_home",
        operation="prepare_action",
        trusted_device_session=trusted_device_session,
    )
    normalized_operation = str(operation or "").strip().lower()
    if normalized_operation not in _SMART_HOME_OPERATIONS:
        raise AmbientCapabilityError("Unsupported smart-home operation")
    clean_integration = _bounded_text(
        integration_id, "integration_id", 160, required=True,
    )
    clean_entity = _bounded_text(entity_id, "entity_id", 255, required=True)
    if not isinstance(parameters, Mapping):
        raise AmbientCapabilityError("Smart-home parameters must be an object")
    clean_parameters = dict(parameters)
    encoded = json.dumps(clean_parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 16 * 1024:
        raise AmbientCapabilityError("Smart-home parameters exceed the size limit")
    clean_sources = dict(sources) if isinstance(sources, Mapping) else None
    if clean_sources is None:
        raise AmbientCapabilityError("Smart-home sources must be an object")
    action = f"smart_home_{normalized_operation}"
    requested_level = 6 if normalized_operation == "unlock" else 5
    return create_action_proposal(
        db,
        owner_id=account.id,
        domain="smart_home",
        action=action,
        autonomy_level=requested_level,
        target_type="device",
        target_id=clean_entity,
        payload={
            "integration_id": clean_integration,
            "entity_id": clean_entity,
            "operation": normalized_operation,
            "parameters": clean_parameters,
        },
        reason=_bounded_text(reason, "reason", 4_000, required=True),
        sources=clean_sources,
        external=True,
        idempotency_key=idempotency_key,
    )


def _smart_home_connector_request(proposal) -> tuple[str, str, dict[str, Any]]:
    if (
        proposal.domain != "smart_home"
        or proposal.target_type != "device"
        or proposal.action not in {f"smart_home_{item}" for item in _SMART_HOME_OPERATIONS}
        or not proposal.external
        or not proposal.requires_confirmation
    ):
        raise AmbientCapabilityDenied("Action is not an executable smart-home proposal")
    payload = proposal.payload
    if not isinstance(payload, Mapping) or set(payload) != {
        "integration_id", "entity_id", "operation", "parameters",
    }:
        raise AmbientCapabilityError("Smart-home proposal payload is invalid")
    operation = str(payload.get("operation") or "")
    if proposal.action != f"smart_home_{operation}" or operation not in _SMART_HOME_OPERATIONS:
        raise AmbientCapabilityError("Smart-home proposal operation is invalid")
    integration_id = _bounded_text(
        payload.get("integration_id"), "integration_id", 160, required=True,
    )
    entity_id = _bounded_text(
        payload.get("entity_id"), "entity_id", 255, required=True,
    )
    if entity_id != proposal.target_id or "." not in entity_id:
        raise AmbientCapabilityError("Smart-home entity target is invalid")
    entity_domain = entity_id.split(".", 1)[0]
    if not entity_domain or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_"
        for character in entity_domain
    ):
        raise AmbientCapabilityError("Smart-home entity domain is invalid")
    parameters = payload.get("parameters")
    if not isinstance(parameters, Mapping):
        raise AmbientCapabilityError("Smart-home parameters must be an object")
    body = dict(parameters)
    body["entity_id"] = entity_id
    if operation == "set_level":
        if entity_domain != "light":
            raise AmbientCapabilityError("set_level requires a light entity")
        raw_level = body.pop("level", body.get("brightness_pct"))
        level = _number(raw_level, "level", 0, 100)
        body["brightness_pct"] = round(level, 2)
        service = "turn_on"
    elif operation == "set_temperature":
        if entity_domain != "climate":
            raise AmbientCapabilityError("set_temperature requires a climate entity")
        body["temperature"] = _number(
            body.get("temperature"), "temperature", -50, 80,
        )
        service = "set_temperature"
    elif operation in {"lock", "unlock"}:
        if entity_domain != "lock":
            raise AmbientCapabilityError(f"{operation} requires a lock entity")
        service = operation
    else:
        service = operation
    return integration_id, f"/api/services/{entity_domain}/{service}", body


def is_server_smart_home_action(proposal) -> bool:
    try:
        _smart_home_connector_request(proposal)
    except AmbientCapabilityError:
        return False
    return True


def start_smart_home_execution(
    db,
    *,
    owner_id: str,
    proposal_id: object,
    expected_version: int,
) -> SmartHomeExecutionRequest:
    proposal = get_action_proposal(
        db, owner_id=owner_id, proposal_id=proposal_id,
    )
    integration_id, path, body = _smart_home_connector_request(proposal)
    executing = start_action(
        db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        expected_version=expected_version,
    )
    return SmartHomeExecutionRequest(
        proposal_id=executing.id,
        proposal_version=int(executing.version),
        integration_id=integration_id,
        method="POST",
        path=path,
        body=body,
    )


async def call_smart_home_connector(
    execution: SmartHomeExecutionRequest,
    *,
    owner_username: str,
) -> dict[str, Any]:
    """Use the generic connector only after the policy state is executing."""

    from src.integrations import execute_api_call

    try:
        result = await execute_api_call(
            execution.integration_id,
            execution.method,
            execution.path,
            body=execution.body,
            owner=owner_username,
            approved_external_action=True,
        )
    except Exception as exc:  # the failure is persisted by the caller
        return {"exit_code": 1, "error": f"Smart-home connector failed: {exc}"}
    if not isinstance(result, dict):
        return {"exit_code": 1, "error": "Smart-home connector returned an invalid result"}
    return result


def finish_smart_home_execution(
    db,
    *,
    owner_id: str,
    execution: SmartHomeExecutionRequest,
    connector_result: Mapping[str, Any],
):
    exit_code = connector_result.get("exit_code", 1)
    success = exit_code == 0 or exit_code == "0"
    bounded_result = {
        "connector": execution.integration_id,
        "method": execution.method,
        "path": execution.path,
        "ok": success,
    }
    if success:
        bounded_result["response"] = str(
            connector_result.get("response") or connector_result.get("output") or ""
        )[:12_000]
        return complete_action(
            db,
            owner_id=owner_id,
            proposal_id=execution.proposal_id,
            expected_version=execution.proposal_version,
            result=bounded_result,
        )
    bounded_result["error"] = str(
        connector_result.get("error") or "Smart-home connector failed"
    )[:4_000]
    return fail_action(
        db,
        owner_id=owner_id,
        proposal_id=execution.proposal_id,
        expected_version=execution.proposal_version,
        result=bounded_result,
    )


__all__ = [
    "AMBIENT_CAPABILITIES",
    "AmbientCapabilityDenied",
    "AmbientCapabilityError",
    "SmartHomeExecutionRequest",
    "ambient_continuity",
    "call_smart_home_connector",
    "capture_ambient_signal",
    "get_ambient_capability",
    "is_server_smart_home_action",
    "list_ambient_capabilities",
    "prepare_smart_home_action",
    "finish_smart_home_execution",
    "set_ambient_capability",
    "sync_offline_captures",
    "start_smart_home_execution",
]
