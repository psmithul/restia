"""Canonical V3 Personal Memory and Knowledge records.

The service deliberately builds on Restia's existing authorities instead of
creating a second memory database.  Durable evidence is an ``Account.id``
owned ``LifeSource`` (or an owner-validated existing record); each claim is an
encrypted ``LifeEntity`` with append-only version history and optimistic CAS.

This boundary is record and retrieval only.  It does not call a model, fetch a
URL, write a user file, execute a tool, or store credentials.  A derived search
index may be discarded and rebuilt from the durable records described here.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit

from core.database import Account, Document, LifeEntity, LifeSource, Note, Project
from src.life_graph import (
    LifeGraphError,
    LifeGraphNotFound,
    create_life_entity,
    create_life_source,
    delete_life_entity,
    list_life_entity_versions,
    serialize_life_entity,
    serialize_life_entity_version,
    serialize_life_source,
    update_life_entity,
)


PERSONAL_KNOWLEDGE_SCHEMA_VERSION = 1
PERSONAL_KNOWLEDGE_SOURCE_SCHEMA_VERSION = 1
PERSONAL_KNOWLEDGE_AUTHORITY = "personal_knowledge_v1"
PERSONAL_KNOWLEDGE_SOURCE_AUTHORITY = "personal_knowledge_source_v1"
PERSONAL_KNOWLEDGE_ENTITY_TYPE = "source"
PERSONAL_KNOWLEDGE_SCAN_LIMIT = 750
PERSONAL_KNOWLEDGE_SOURCE_SCAN_LIMIT = 500

MEMORY_KINDS = frozenset({
    "semantic",
    "episodic",
    "decision",
    "preference",
    "procedural",
    "relationship",
    "project",
    "task",
    "source",
})

KNOWLEDGE_SOURCE_KINDS = frozenset({
    "note",
    "document",
    "research",
    "bookmark",
    "web_page",
    "meeting",
    "writing",
    "academic_record",
    "business_record",
    "idea",
    "lesson",
    "markdown",
})

EPISTEMIC_STATUSES = frozenset({
    "confirmed_fact",
    "user_statement",
    "assumption",
    "inference",
    "stale",
    "gap",
})
CLAIM_ORIGINS = frozenset({"user", "source", "model", "import"})
RECORD_STATUSES = frozenset({"active", "archived"})
SENSITIVITIES = frozenset({"private", "restricted"})
CITATION_TARGET_KINDS = frozenset({
    "life_source", "memory", "document", "file", "note", "decision",
    "project", "task",
})
CITATION_RELATIONS = frozenset({
    "supports", "reports", "derived_from", "context", "contradicts",
    "documents", "observed_in",
})
SUPPORTING_CITATION_RELATIONS = frozenset({
    "supports", "reports", "derived_from", "documents", "observed_in",
})

RETRIEVAL_POLICY = {
    "record_only": True,
    "uses_model_calls": False,
    "uses_network_calls": False,
    "writes_user_files": False,
    "can_execute_external_actions": False,
    "can_auto_confirm_model_inference": False,
}

MARKDOWN_INDEX_CONTRACT = {
    "durable_format": "markdown",
    "durable_authority": "document.current_content",
    "derived_index": True,
    "rebuildable": True,
    "index_is_authority": False,
    "writes_user_files": False,
}

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_URL_CREDENTIAL_RE = re.compile(
    r"[a-z][a-z0-9+.-]*://[^/@\s]+:[^/@\s]+@", re.IGNORECASE
)
_BEARER_RE = re.compile(r"\bauthorization\s*:\s*bearer\s+\S+", re.IGNORECASE)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
_INLINE_SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(?:password|passwd|passcode|secret|token|credential|cookie|"
    r"authorization|api[ _-]?key|private[ _-]?key|seed[ _-]?phrase|"
    r"recovery[ _-]?phrase|access[ _-]?key|session[ _-]?key)"
    r"\s*(?::|=|\bis\b)\s*[^\s,;]+",
    re.IGNORECASE,
)
_KNOWN_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{8,}|"
    r"gh[pousr]_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
)
_RELATIVE_FILE_PATH_RE = re.compile(
    r"^(?:\.{1,2}/)?(?:[^/\s]+/)+[^/\s]+\.[A-Za-z0-9]{1,16}(?:[?#].*)?$"
)

_SECRET_KEY_PARTS = (
    "password", "passwd", "passcode", "secret", "token", "credential",
    "cookie", "authorization", "api_key", "private_key", "seed_phrase",
    "recovery_phrase", "access_key", "session_key",
)
_EXECUTOR_KEYS = frozenset({
    "action", "execute", "executor", "tool", "tool_call", "function_call",
    "external_action", "webhook", "send", "send_message", "send_email",
    "dispatch", "submit", "http_request", "shell", "command", "payload",
    "request_body", "arguments",
})
_FILESYSTEM_PATH_KEYS = frozenset({
    "path", "file_path", "filepath", "filesystem_path", "directory",
    "directory_path", "root_path", "absolute_path", "relative_path",
})


def _compact_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().lower())


def _is_secret_key(value: object) -> bool:
    compact = _compact_key(value)
    return any(_compact_key(part) in compact for part in _SECRET_KEY_PARTS)


def _is_executor_key(value: object) -> bool:
    key = str(value or "").strip().lower().replace("-", "_")
    compact = _compact_key(key)
    return (
        key in _EXECUTOR_KEYS
        or key.startswith(("send_", "execute_", "dispatch_", "submit_"))
        or compact in {_compact_key(item) for item in _EXECUTOR_KEYS}
        or any(marker in compact for marker in (
            "executor", "toolcall", "functioncall", "externalaction",
            "httprequest", "webhook",
        ))
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
    if (
        _URL_CREDENTIAL_RE.search(normalized)
        or _BEARER_RE.search(normalized)
        or _PRIVATE_KEY_RE.search(normalized)
        or _INLINE_SECRET_ASSIGNMENT_RE.search(normalized)
        or _KNOWN_SECRET_TOKEN_RE.search(normalized)
    ):
        raise LifeGraphError(f"{field} must not contain credentials or secrets")
    return normalized


def _token(value: object, *, field: str, choices: frozenset[str]) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "_")
    if not normalized or len(normalized) > 64 or not _TOKEN_RE.fullmatch(normalized):
        raise LifeGraphError(
            f"{field} must be a lowercase token using letters, numbers, _, -, or ."
        )
    if normalized not in choices:
        raise LifeGraphError(
            f"{field} must be one of: {', '.join(sorted(choices))}"
        )
    return normalized


def _datetime(
    value: object | None, *, field: str, required: bool = False
) -> datetime | None:
    if value is None or value == "":
        if required:
            raise LifeGraphError(f"{field} is required")
        return None
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
            raise LifeGraphError(
                f"{field} must be an ISO-8601 date or datetime"
            ) from exc
    else:
        raise LifeGraphError(f"{field} must be an ISO-8601 date or datetime")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _as_of(value: object) -> datetime:
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise LifeGraphError("as_of must be an offset-aware ISO-8601 datetime") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise LifeGraphError("as_of must be an offset-aware ISO-8601 datetime")
    if parsed.tzinfo is None:
        raise LifeGraphError("as_of must include a UTC offset")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    parsed = value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _json_safe(value: object) -> object:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    return value


def _assert_safe_payload(value: object, *, field: str) -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            if _is_secret_key(key):
                raise LifeGraphError(f"{field} must not contain credentials or secrets")
            if _is_executor_key(key):
                raise LifeGraphError(
                    f"{field} cannot request a model, network, file, or external action"
                )
            if key in _FILESYSTEM_PATH_KEYS:
                raise LifeGraphError(f"{field} must not contain filesystem paths")
            _assert_safe_payload(child, field=field)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_safe_payload(child, field=field)
    elif isinstance(value, str):
        _text(value, field=field, limit=20_000, preserve_lines=True)


def _bounded_object(
    value: object | None, *, field: str, max_bytes: int = 32_000
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise LifeGraphError(f"{field} must be an object")
    result = _json_safe(dict(value))
    if not isinstance(result, dict):
        raise LifeGraphError(f"{field} must be an object")
    _assert_safe_payload(result, field=field)
    try:
        encoded = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise LifeGraphError(f"{field} must be JSON-serializable") from exc
    if len(encoded) > max_bytes:
        raise LifeGraphError(f"{field} must not exceed {max_bytes} bytes")
    return result


def _bounded_list(
    value: object | None,
    *,
    field: str,
    max_items: int,
    item_limit: int,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise LifeGraphError(f"{field} must be a list")
    if len(value) > max_items:
        raise LifeGraphError(f"{field} must not contain more than {max_items} items")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        item = _text(raw, field=field, limit=item_limit, required=True)
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _assert_not_path(value: object, *, field: str) -> str:
    text = _text(value, field=field, limit=2_000, required=True)
    normalized = text.replace("\\", "/")
    parsed = urlsplit(text)
    if (
        normalized.startswith(("/", "~/", "file:"))
        or _WINDOWS_PATH_RE.match(text)
        or "../" in normalized
        or normalized == ".."
        or (not parsed.scheme and _RELATIVE_FILE_PATH_RE.fullmatch(normalized))
    ):
        raise LifeGraphError(f"{field} must not be a filesystem path")
    return text


def _safe_url(value: object, *, field: str) -> str:
    text = _assert_not_path(value, field=field)
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LifeGraphError(f"{field} must be an http or https URL")
    if parsed.username or parsed.password:
        raise LifeGraphError(f"{field} must not contain embedded credentials")
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if _is_secret_key(key):
            raise LifeGraphError(f"{field} must not contain credential query parameters")
    return text


def _content_sha256(value: object | None) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if not _HEX_SHA256_RE.fullmatch(text):
        raise LifeGraphError(
            "content_sha256 must be 64 lowercase hexadecimal characters"
        )
    return text


def _account_for_owner(db, owner_id: str) -> Account:
    account = db.query(Account).filter(Account.id == str(owner_id)).first()
    if account is None:
        raise LifeGraphNotFound("Account not found")
    return account


def _life_entity_reference(
    db,
    *,
    owner_id: str,
    target_id: str,
    entity_type: str,
    include_deleted: bool,
) -> LifeEntity | None:
    query = db.query(LifeEntity).filter(
        LifeEntity.id == target_id,
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == entity_type,
    )
    if not include_deleted:
        query = query.filter(LifeEntity.deleted_at.is_(None))
    return query.first()


def _resolve_reference(
    db,
    *,
    account: Account,
    target_kind: str,
    target_id: str,
    require_available: bool,
) -> dict[str, Any]:
    """Resolve one citation without making another owner's row observable."""

    available = False
    tombstoned = False
    title = ""
    if target_kind == "life_source":
        row = db.query(LifeSource).filter(
            LifeSource.id == target_id, LifeSource.owner_id == account.id
        ).first()
        if row is not None:
            available = True
            title = str(row.title or "")
    elif target_kind == "document":
        row = db.query(Document).filter(
            Document.id == target_id, Document.owner == account.username
        ).first()
        if row is not None:
            available = True
            title = str(row.title or "")
    elif target_kind == "note":
        row = db.query(Note).filter(
            Note.id == target_id, Note.owner == account.username
        ).first()
        if row is not None:
            available = True
            title = str(row.title or "")
    elif target_kind == "project":
        entity = _life_entity_reference(
            db, owner_id=account.id, target_id=target_id,
            entity_type="project", include_deleted=True,
        )
        if entity is not None:
            tombstoned = entity.deleted_at is not None
            available = not tombstoned
            title = str(entity.title or "")
        else:
            row = db.query(Project).filter(
                Project.id == target_id, Project.owner == account.username
            ).first()
            if row is not None:
                available = True
                title = str(row.name or "")
    elif target_kind in {"file", "decision", "task"}:
        entity = _life_entity_reference(
            db, owner_id=account.id, target_id=target_id,
            entity_type=target_kind, include_deleted=True,
        )
        if entity is not None:
            tombstoned = entity.deleted_at is not None
            available = not tombstoned
            title = str(entity.title or "")
    elif target_kind == "memory":
        entity = _life_entity_reference(
            db, owner_id=account.id, target_id=target_id,
            entity_type=PERSONAL_KNOWLEDGE_ENTITY_TYPE, include_deleted=True,
        )
        props = entity.properties if entity is not None else {}
        if entity is not None and isinstance(props, Mapping) and (
            props.get("personal_knowledge_schema_version")
            == PERSONAL_KNOWLEDGE_SCHEMA_VERSION
            and props.get("authority") == PERSONAL_KNOWLEDGE_AUTHORITY
        ):
            tombstoned = entity.deleted_at is not None
            available = not tombstoned
            title = str(entity.title or "")

    if require_available and not available:
        raise LifeGraphNotFound("Citation target not found")
    return {
        "available": available,
        "tombstoned": tombstoned,
        "title": title if available or tombstoned else "",
    }


def _knowledge_source_type(source_kind: str) -> str:
    return f"knowledge_{source_kind}"


def _source_kind_from_row(source: LifeSource) -> str | None:
    metadata = source.meta_data if isinstance(source.meta_data, Mapping) else {}
    kind = metadata.get("knowledge_source_kind")
    expected = (
        _knowledge_source_type(kind)
        if isinstance(kind, str) and kind in KNOWLEDGE_SOURCE_KINDS else None
    )
    if (
        metadata.get("personal_knowledge_source_schema_version")
        != PERSONAL_KNOWLEDGE_SOURCE_SCHEMA_VERSION
        or metadata.get("authority") != PERSONAL_KNOWLEDGE_SOURCE_AUTHORITY
        or expected != source.source_type
    ):
        return None
    return str(kind)


def _normalize_source_descriptor(
    db,
    *,
    account: Account,
    source_kind: str,
    value: object | None,
    source_ref: object | None,
    content_sha256: object | None,
) -> tuple[dict[str, Any], str | None, str | None]:
    descriptor = _bounded_object(value, field="source descriptor", max_bytes=16_000)
    allowed = {
        "target_kind", "target_id", "canonical_uri", "external_id",
        "revision", "media_type", "language", "description",
    }
    unknown = sorted(set(descriptor) - allowed)
    if unknown:
        raise LifeGraphError(
            f"Unsupported source descriptor fields: {', '.join(unknown)}"
        )

    target_kind = str(descriptor.get("target_kind") or "").strip().lower()
    target_id = str(descriptor.get("target_id") or "").strip()
    if bool(target_kind) != bool(target_id):
        raise LifeGraphError("source descriptor target_kind and target_id are paired")
    if target_kind:
        if target_kind not in CITATION_TARGET_KINDS - {"memory"}:
            raise LifeGraphError("Unsupported source descriptor target_kind")
        if len(target_id) > 255:
            raise LifeGraphError("source descriptor target_id is too long")
        _resolve_reference(
            db, account=account, target_kind=target_kind,
            target_id=target_id, require_available=True,
        )

    if source_kind == "note" and target_kind != "note":
        raise LifeGraphError("note sources require an owned Note target")
    if source_kind == "document" and target_kind != "document":
        raise LifeGraphError("document sources require an owned Document target")

    canonical_uri = descriptor.get("canonical_uri")
    if canonical_uri:
        canonical_uri = _safe_url(canonical_uri, field="canonical_uri")
    if source_kind in {"bookmark", "web_page"} and not canonical_uri:
        if source_ref:
            canonical_uri = _safe_url(source_ref, field="source_ref")
        else:
            raise LifeGraphError(f"{source_kind} sources require canonical_uri")

    normalized_ref: str | None
    if canonical_uri:
        normalized_ref = str(canonical_uri)
    elif target_kind:
        normalized_ref = f"{target_kind}:{target_id}"
    elif source_ref is not None:
        candidate_ref = _assert_not_path(source_ref, field="source_ref")
        normalized_ref = (
            _safe_url(candidate_ref, field="source_ref")
            if urlsplit(candidate_ref).scheme.lower() in {"http", "https"}
            else candidate_ref
        )
    else:
        normalized_ref = None

    digest = _content_sha256(content_sha256)
    normalized: dict[str, Any] = {
        "target_kind": target_kind or None,
        "target_id": target_id or None,
        "canonical_uri": canonical_uri or None,
        "external_id": _text(
            descriptor.get("external_id"), field="external_id", limit=255
        ) or None,
        "revision": _text(
            descriptor.get("revision"), field="revision", limit=120
        ) or None,
        "media_type": _text(
            descriptor.get("media_type"), field="media_type", limit=120
        ) or None,
        "language": _text(
            descriptor.get("language"), field="language", limit=64
        ) or None,
        "description": _text(
            descriptor.get("description"), field="description", limit=2_000,
            preserve_lines=True,
        ),
    }

    if source_kind == "markdown":
        if target_kind != "document":
            raise LifeGraphError(
                "markdown sources require an owned Markdown Document target"
            )
        document = db.query(Document).filter(
            Document.id == target_id, Document.owner == account.username
        ).first()
        if document is None:
            raise LifeGraphNotFound("Markdown document not found")
        if str(document.language or "").strip().lower() not in {"md", "markdown"}:
            raise LifeGraphError("markdown source Document must use Markdown language")
        computed = hashlib.sha256(
            str(document.current_content or "").encode("utf-8")
        ).hexdigest()
        if digest is not None and digest != computed:
            raise LifeGraphError("content_sha256 does not match the Markdown Document")
        digest = computed
        normalized["language"] = "markdown"
        normalized["media_type"] = "text/markdown"
        normalized["index_contract"] = dict(MARKDOWN_INDEX_CONTRACT)
    return normalized, normalized_ref, digest


def create_knowledge_source(
    db,
    *,
    account: Account,
    source_kind: object,
    title: object,
    observed_at: object,
    descriptor: object | None = None,
    source_ref: object | None = None,
    safe_excerpt: object = "",
    content_sha256: object | None = None,
    details: object | None = None,
    sensitivity: object = "private",
    idempotency_key: object | None = None,
) -> tuple[LifeSource, bool]:
    kind = _token(source_kind, field="source_kind", choices=KNOWLEDGE_SOURCE_KINDS)
    normalized_title = _text(title, field="title", limit=240, required=True)
    normalized_observed_at = _datetime(
        observed_at, field="observed_at", required=True
    )
    normalized_sensitivity = _token(
        sensitivity, field="sensitivity", choices=SENSITIVITIES
    )
    normalized_descriptor, normalized_ref, digest = _normalize_source_descriptor(
        db,
        account=account,
        source_kind=kind,
        value=descriptor,
        source_ref=source_ref,
        content_sha256=content_sha256,
    )
    metadata = {
        "personal_knowledge_source_schema_version": (
            PERSONAL_KNOWLEDGE_SOURCE_SCHEMA_VERSION
        ),
        "authority": PERSONAL_KNOWLEDGE_SOURCE_AUTHORITY,
        "knowledge_source_kind": kind,
        "descriptor": normalized_descriptor,
        "details": _bounded_object(details, field="source details", max_bytes=16_000),
        "immutability_contract": {
            "principal_is_immutable": True,
            "content_changes_create_new_source": True,
        },
        "retrieval_policy": dict(RETRIEVAL_POLICY),
    }
    return create_life_source(
        db,
        account=account,
        source_type=_knowledge_source_type(kind),
        title=normalized_title,
        source_ref=normalized_ref,
        safe_excerpt=_text(
            safe_excerpt, field="safe_excerpt", limit=4_000,
            preserve_lines=True,
        ),
        content_sha256=digest,
        observed_at=normalized_observed_at,
        sensitivity=normalized_sensitivity,
        metadata=metadata,
        idempotency_key=idempotency_key,
    )


def _serialize_knowledge_source(
    db, *, account: Account, source: LifeSource
) -> dict[str, Any]:
    kind = _source_kind_from_row(source)
    if source.owner_id != account.id or kind is None:
        raise LifeGraphNotFound("Knowledge source not found")
    metadata = source.meta_data if isinstance(source.meta_data, Mapping) else {}
    expected_metadata_keys = {
        "personal_knowledge_source_schema_version", "authority",
        "knowledge_source_kind", "descriptor", "details",
        "immutability_contract", "retrieval_policy",
    }
    if set(metadata) != expected_metadata_keys:
        raise LifeGraphError("Stored personal knowledge source metadata is malformed")
    _text(source.title, field="stored source title", limit=240, required=True)
    _text(
        source.safe_excerpt, field="stored source excerpt", limit=4_000,
        preserve_lines=True,
    )
    _datetime(source.observed_at, field="stored source observed_at", required=True)
    _token(
        source.sensitivity, field="stored source sensitivity",
        choices=SENSITIVITIES,
    )
    descriptor = _bounded_object(
        metadata.get("descriptor"), field="stored source descriptor",
        max_bytes=16_000,
    )
    descriptor_input = dict(descriptor)
    if kind == "markdown":
        descriptor_input.pop("index_contract", None)
    normalized_descriptor, normalized_ref, normalized_digest = (
        _normalize_source_descriptor(
            db,
            account=account,
            source_kind=kind,
            value=descriptor_input,
            source_ref=source.source_ref,
            content_sha256=source.content_sha256,
        )
    )
    if (
        normalized_descriptor != descriptor
        or normalized_ref != source.source_ref
        or normalized_digest != source.content_sha256
    ):
        raise LifeGraphError("Stored personal knowledge source descriptor is malformed")
    details = _bounded_object(
        metadata.get("details"), field="stored source details", max_bytes=16_000
    )
    if dict(metadata.get("immutability_contract") or {}) != {
        "principal_is_immutable": True,
        "content_changes_create_new_source": True,
    }:
        raise LifeGraphError("Stored personal knowledge source contract is malformed")
    if dict(metadata.get("retrieval_policy") or {}) != RETRIEVAL_POLICY:
        raise LifeGraphError("Stored personal knowledge source policy is malformed")
    serialized = serialize_life_source(source)
    target_kind = descriptor.get("target_kind")
    target_id = descriptor.get("target_id")
    availability = {"available": True, "tombstoned": False, "title": ""}
    if target_kind and target_id:
        availability = _resolve_reference(
            db,
            account=account,
            target_kind=str(target_kind),
            target_id=str(target_id),
            require_available=False,
        )
    serialized.update({
        "source_kind": kind,
        "descriptor": descriptor,
        "details": details,
        "availability": availability,
        "immutability_contract": dict(metadata.get("immutability_contract") or {}),
        "retrieval_policy": dict(RETRIEVAL_POLICY),
    })
    return serialized


def get_knowledge_source(
    db, *, owner_id: str, source_id: object
) -> dict[str, Any]:
    account = _account_for_owner(db, owner_id)
    source = db.query(LifeSource).filter(
        LifeSource.id == str(source_id), LifeSource.owner_id == owner_id
    ).first()
    if source is None:
        raise LifeGraphNotFound("Knowledge source not found")
    return _serialize_knowledge_source(db, account=account, source=source)


def list_knowledge_sources(
    db,
    *,
    owner_id: str,
    source_kind: object | None = None,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    account = _account_for_owner(db, owner_id)
    bounded = max(1, min(100, int(limit)))
    normalized_kind = (
        _token(source_kind, field="source_kind", choices=KNOWLEDGE_SOURCE_KINDS)
        if source_kind else None
    )
    query = db.query(LifeSource).filter(LifeSource.owner_id == owner_id)
    if normalized_kind:
        query = query.filter(
            LifeSource.source_type == _knowledge_source_type(normalized_kind)
        )
    else:
        query = query.filter(LifeSource.source_type.in_([
            _knowledge_source_type(kind) for kind in sorted(KNOWLEDGE_SOURCE_KINDS)
        ]))
    candidates = query.order_by(
        LifeSource.captured_at.desc(), LifeSource.id.desc()
    ).limit(PERSONAL_KNOWLEDGE_SOURCE_SCAN_LIMIT + 1).all()
    scan_truncated = len(candidates) > PERSONAL_KNOWLEDGE_SOURCE_SCAN_LIMIT
    admitted: list[LifeSource] = []
    for source in candidates[:PERSONAL_KNOWLEDGE_SOURCE_SCAN_LIMIT]:
        if _source_kind_from_row(source) is None:
            continue
        admitted.append(source)
        if len(admitted) > bounded:
            break
    rows = [
        _serialize_knowledge_source(db, account=account, source=source)
        for source in admitted[:bounded]
    ]
    return rows, scan_truncated or len(admitted) > bounded


def search_knowledge_sources(
    db,
    *,
    owner_id: str,
    query_text: object,
    source_kind: object | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    needle = _text(query_text, field="q", limit=500, required=True).casefold()
    bounded = max(1, min(100, int(limit)))
    account = _account_for_owner(db, owner_id)
    normalized_kind = (
        _token(source_kind, field="source_kind", choices=KNOWLEDGE_SOURCE_KINDS)
        if source_kind else None
    )
    query = db.query(LifeSource).filter(LifeSource.owner_id == owner_id)
    if normalized_kind:
        query = query.filter(
            LifeSource.source_type == _knowledge_source_type(normalized_kind)
        )
    else:
        query = query.filter(LifeSource.source_type.in_([
            _knowledge_source_type(kind) for kind in sorted(KNOWLEDGE_SOURCE_KINDS)
        ]))
    candidates = query.order_by(
        LifeSource.captured_at.desc(), LifeSource.id.desc()
    ).limit(PERSONAL_KNOWLEDGE_SOURCE_SCAN_LIMIT + 1).all()
    scan_truncated = len(candidates) > PERSONAL_KNOWLEDGE_SOURCE_SCAN_LIMIT
    matches: list[tuple[tuple[int, str, str], LifeSource, str]] = []
    scanned = 0
    for source in candidates[:PERSONAL_KNOWLEDGE_SOURCE_SCAN_LIMIT]:
        if _source_kind_from_row(source) is None:
            continue
        scanned += 1
        title = str(source.title or "").casefold()
        excerpt = str(source.safe_excerpt or "").casefold()
        metadata = source.meta_data if isinstance(source.meta_data, Mapping) else {}
        details = json.dumps(
            metadata.get("details") or {}, ensure_ascii=False, sort_keys=True
        ).casefold()
        if title == needle:
            rank, field = 0, "title"
        elif title.startswith(needle):
            rank, field = 1, "title"
        elif needle in title:
            rank, field = 2, "title"
        elif needle in excerpt:
            rank, field = 3, "safe_excerpt"
        elif needle in details:
            rank, field = 4, "details"
        else:
            continue
        matches.append(((rank, title, source.id), source, field))
    matches.sort(key=lambda item: item[0])
    items = [
        {
            "source": _serialize_knowledge_source(
                db, account=account, source=source
            ),
            "match": field,
            "rank": sort_key[0],
        }
        for sort_key, source, field in matches[:bounded]
    ]
    return {
        "items": items,
        "count": min(len(matches), bounded),
        "scanned": scanned,
        "truncated": scan_truncated or len(matches) > bounded,
        "method": "bounded_deterministic_source_search_v1",
        "retrieval_policy": dict(RETRIEVAL_POLICY),
    }


def _normalize_citations(
    db,
    *,
    account: Account,
    value: object,
    record_id: str | None = None,
    require_available: bool = True,
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise LifeGraphError("citations must contain at least one source link")
    if len(value) > 30:
        raise LifeGraphError("citations must not contain more than 30 items")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    has_direct_evidence = False
    for raw_item in value:
        item = _bounded_object(raw_item, field="citation", max_bytes=8_000)
        allowed = {
            "target_kind", "target_id", "relation", "locator", "excerpt",
            "note", "citation_id",
        }
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise LifeGraphError(
                f"Unsupported citation fields: {', '.join(unknown)}"
            )
        target_kind = _token(
            item.get("target_kind"),
            field="citations.target_kind",
            choices=CITATION_TARGET_KINDS,
        )
        target_id = _text(
            item.get("target_id"), field="citations.target_id", limit=255,
            required=True,
        )
        if target_kind == "memory" and record_id and target_id == record_id:
            raise LifeGraphError("A memory record cannot cite itself")
        relation = _token(
            item.get("relation") or "supports",
            field="citations.relation",
            choices=CITATION_RELATIONS,
        )
        locator = _text(
            item.get("locator"), field="citations.locator", limit=500
        )
        if locator:
            _assert_not_path(locator, field="citations.locator")
        excerpt = _text(
            item.get("excerpt"), field="citations.excerpt", limit=2_000,
            preserve_lines=True,
        )
        note = _text(
            item.get("note"), field="citations.note", limit=1_000,
            preserve_lines=True,
        )
        if not locator and not excerpt:
            raise LifeGraphError(
                "Each citation requires a locator or a bounded evidence excerpt"
            )
        _resolve_reference(
            db,
            account=account,
            target_kind=target_kind,
            target_id=target_id,
            require_available=require_available,
        )
        digest_input = "\x1f".join((
            target_kind, target_id, relation, locator, excerpt,
        ))
        citation_id = hashlib.sha256(
            digest_input.encode("utf-8")
        ).hexdigest()[:24]
        if citation_id in seen:
            continue
        seen.add(citation_id)
        has_direct_evidence = has_direct_evidence or target_kind != "memory"
        result.append({
            "citation_id": citation_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "relation": relation,
            "locator": locator,
            "excerpt": excerpt,
            "note": note,
        })
    if not has_direct_evidence:
        raise LifeGraphError(
            "At least one citation must link directly to a durable source or record"
        )
    return result


def _normalize_inference(
    value: object | None,
    *,
    epistemic_status: str,
    claim_origin: str,
) -> dict[str, Any]:
    inference = _bounded_object(value, field="inference", max_bytes=12_000)
    allowed = {"generated_by", "method", "basis", "model_label"}
    unknown = sorted(set(inference) - allowed)
    if unknown:
        raise LifeGraphError(
            f"Unsupported inference fields: {', '.join(unknown)}"
        )
    if epistemic_status == "inference" or claim_origin == "model":
        generated_by = _token(
            inference.get("generated_by"),
            field="inference.generated_by",
            choices=frozenset({"human", "model"}),
        )
        method = _text(
            inference.get("method"), field="inference.method", limit=500,
            required=True,
        )
        basis = _text(
            inference.get("basis"), field="inference.basis", limit=2_000,
            required=True, preserve_lines=True,
        )
        model_label = _text(
            inference.get("model_label"), field="inference.model_label", limit=120
        )
        if claim_origin == "model" and generated_by != "model":
            raise LifeGraphError(
                "model-origin claims must be explicitly labelled generated_by=model"
            )
        if generated_by == "model" and claim_origin != "model":
            raise LifeGraphError(
                "model-generated inference must use claim_origin=model"
            )
        return {
            "generated_by": generated_by,
            "method": method,
            "basis": basis,
            "model_label": model_label or None,
            "explicitly_labelled": True,
        }
    if inference:
        raise LifeGraphError(
            "inference metadata is only valid for inference or model-origin records"
        )
    return {}


def _normalize_record_payload(
    db,
    *,
    account: Account,
    memory_kind: object,
    title: object,
    statement: object,
    epistemic_status: object,
    claim_origin: object,
    citations: object,
    observed_at: object | None,
    reviewed_at: object | None,
    stale_at: object | None,
    stale_after: object | None,
    tags: object | None,
    details: object | None,
    inference: object | None,
    status: object,
    sensitivity: object,
    provenance: object | None,
    record_id: str | None = None,
) -> dict[str, Any]:
    kind = _token(memory_kind, field="memory_kind", choices=MEMORY_KINDS)
    normalized_title = _text(title, field="title", limit=240, required=True)
    normalized_statement = _text(
        statement, field="statement", limit=20_000, required=True,
        preserve_lines=True,
    )
    epistemic = _token(
        epistemic_status, field="epistemic_status", choices=EPISTEMIC_STATUSES
    )
    origin = _token(claim_origin, field="claim_origin", choices=CLAIM_ORIGINS)
    normalized_status = _token(
        status, field="status", choices=RECORD_STATUSES
    )
    normalized_sensitivity = _token(
        sensitivity, field="sensitivity", choices=SENSITIVITIES
    )
    normalized_observed = _datetime(observed_at, field="observed_at")
    normalized_reviewed = _datetime(reviewed_at, field="reviewed_at")
    normalized_stale = _datetime(stale_at, field="stale_at")
    normalized_stale_after = _datetime(stale_after, field="stale_after")

    if kind == "episodic" and normalized_observed is None:
        raise LifeGraphError("episodic memories require observed_at")
    if epistemic == "confirmed_fact" and normalized_reviewed is None:
        raise LifeGraphError("confirmed facts require reviewed_at")
    if epistemic == "user_statement" and origin != "user":
        raise LifeGraphError("user_statement records require claim_origin=user")
    if epistemic == "stale" and normalized_stale is None:
        raise LifeGraphError("stale records require stale_at")
    if epistemic != "stale" and normalized_stale is not None:
        raise LifeGraphError("stale_at is only valid when epistemic_status=stale")
    if origin == "model" and epistemic not in {"inference", "stale"}:
        raise LifeGraphError(
            "model-origin claims must remain explicitly inference or stale; "
            "create a separately reviewed fact instead of promoting them in place"
        )
    if normalized_stale and normalized_observed and normalized_stale < normalized_observed:
        raise LifeGraphError("stale_at cannot be earlier than observed_at")
    anchor = normalized_reviewed or normalized_observed
    if normalized_stale_after and anchor and normalized_stale_after < anchor:
        raise LifeGraphError("stale_after cannot be earlier than its review/observation")
    if normalized_stale_after and anchor is None:
        raise LifeGraphError("stale_after requires reviewed_at or observed_at")

    normalized_citations = _normalize_citations(
        db,
        account=account,
        value=citations,
        record_id=record_id,
        require_available=True,
    )
    normalized_inference = _normalize_inference(
        inference, epistemic_status=epistemic, claim_origin=origin
    )
    normalized_details = _bounded_object(
        details, field="details", max_bytes=32_000
    )
    normalized_tags = _bounded_list(
        tags, field="tags", max_items=40, item_limit=80
    )
    source_ids = sorted({
        citation["target_id"] for citation in normalized_citations
        if citation["target_kind"] == "life_source"
    })
    linked_record_ids = sorted({
        citation["target_id"] for citation in normalized_citations
        if citation["target_kind"] != "life_source"
    })
    user_provenance = _bounded_object(
        provenance, field="provenance", max_bytes=16_000
    )
    server_fields = {
        "authority", "memory_kind", "claim_origin", "source_ids",
        "linked_record_ids", "citation_ids",
    }
    overridden = sorted(set(user_provenance) & server_fields)
    if overridden:
        raise LifeGraphError(
            "Provenance authority fields are server-controlled: "
            + ", ".join(overridden)
        )
    normalized_provenance = {
        **user_provenance,
        "authority": PERSONAL_KNOWLEDGE_AUTHORITY,
        "memory_kind": kind,
        "claim_origin": origin,
        "source_ids": source_ids,
        "linked_record_ids": linked_record_ids,
        "citation_ids": [row["citation_id"] for row in normalized_citations],
    }
    properties = {
        "personal_knowledge_schema_version": PERSONAL_KNOWLEDGE_SCHEMA_VERSION,
        "authority": PERSONAL_KNOWLEDGE_AUTHORITY,
        "memory_kind": kind,
        "epistemic_status": epistemic,
        "claim_origin": origin,
        "citations": normalized_citations,
        "observed_at": _iso(normalized_observed),
        "reviewed_at": _iso(normalized_reviewed),
        "stale_at": _iso(normalized_stale),
        "stale_after": _iso(normalized_stale_after),
        "tags": normalized_tags,
        "details": normalized_details,
        "inference": normalized_inference,
        "gap": (
            {"explicit": True, "question": normalized_statement}
            if epistemic == "gap" else None
        ),
        "retrieval_policy": dict(RETRIEVAL_POLICY),
    }
    return {
        "memory_kind": kind,
        "title": normalized_title,
        "statement": normalized_statement,
        "status": normalized_status,
        "sensitivity": normalized_sensitivity,
        "properties": properties,
        "provenance": normalized_provenance,
        "observed_at": normalized_observed,
        "reviewed_at": normalized_reviewed,
    }


def create_personal_knowledge_record(
    db,
    *,
    account: Account,
    memory_kind: object,
    title: object,
    statement: object,
    epistemic_status: object,
    claim_origin: object,
    citations: object,
    observed_at: object | None = None,
    reviewed_at: object | None = None,
    stale_at: object | None = None,
    stale_after: object | None = None,
    tags: object | None = None,
    details: object | None = None,
    inference: object | None = None,
    status: object = "active",
    confidence: object = 100,
    sensitivity: object = "private",
    provenance: object | None = None,
    idempotency_key: object | None = None,
) -> tuple[LifeEntity, bool]:
    payload = _normalize_record_payload(
        db,
        account=account,
        memory_kind=memory_kind,
        title=title,
        statement=statement,
        epistemic_status=epistemic_status,
        claim_origin=claim_origin,
        citations=citations,
        observed_at=observed_at,
        reviewed_at=reviewed_at,
        stale_at=stale_at,
        stale_after=stale_after,
        tags=tags,
        details=details,
        inference=inference,
        status=status,
        sensitivity=sensitivity,
        provenance=provenance,
    )
    return create_life_entity(
        db,
        account=account,
        entity_type=PERSONAL_KNOWLEDGE_ENTITY_TYPE,
        title=payload["title"],
        summary=payload["statement"],
        status=payload["status"],
        properties=payload["properties"],
        provenance=payload["provenance"],
        confidence=confidence,
        sensitivity=payload["sensitivity"],
        occurred_at=payload["observed_at"],
        review_at=payload["reviewed_at"],
        idempotency_key=idempotency_key,
        reason="Personal knowledge record created",
    )


def _record_properties(entity: LifeEntity) -> dict[str, Any]:
    properties = entity.properties if isinstance(entity.properties, Mapping) else {}
    memory_kind = properties.get("memory_kind")
    epistemic_status = properties.get("epistemic_status")
    claim_origin = properties.get("claim_origin")
    if (
        entity.entity_type != PERSONAL_KNOWLEDGE_ENTITY_TYPE
        or properties.get("personal_knowledge_schema_version")
        != PERSONAL_KNOWLEDGE_SCHEMA_VERSION
        or properties.get("authority") != PERSONAL_KNOWLEDGE_AUTHORITY
        or not isinstance(memory_kind, str)
        or memory_kind not in MEMORY_KINDS
        or not isinstance(epistemic_status, str)
        or epistemic_status not in EPISTEMIC_STATUSES
        or not isinstance(claim_origin, str)
        or claim_origin not in CLAIM_ORIGINS
        or not isinstance(properties.get("citations"), list)
        or not properties.get("citations")
    ):
        raise LifeGraphNotFound("Personal knowledge record not found")
    allowed_property_keys = {
        "personal_knowledge_schema_version", "authority", "memory_kind",
        "epistemic_status", "claim_origin", "citations", "observed_at",
        "reviewed_at", "stale_at", "stale_after", "tags", "details",
        "inference", "gap", "retrieval_policy",
    }
    if set(properties) != allowed_property_keys:
        raise LifeGraphError("Personal knowledge properties are malformed")
    _text(entity.title, field="stored title", limit=240, required=True)
    _text(
        entity.summary, field="stored statement", limit=20_000,
        required=True, preserve_lines=True,
    )
    stored_statuses = (
        RECORD_STATUSES | frozenset({"deleted"})
        if entity.deleted_at is not None else RECORD_STATUSES
    )
    _token(entity.status, field="stored status", choices=stored_statuses)
    _token(entity.sensitivity, field="stored sensitivity", choices=SENSITIVITIES)
    normalized_tags = _bounded_list(
        properties.get("tags"), field="stored tags", max_items=40, item_limit=80
    )
    if normalized_tags != list(properties.get("tags") or []):
        raise LifeGraphError("Personal knowledge tags are malformed")
    normalized_details = _bounded_object(
        properties.get("details"), field="stored details", max_bytes=32_000
    )
    if normalized_details != dict(properties.get("details") or {}):
        raise LifeGraphError("Personal knowledge details are malformed")
    stored_inference = dict(properties.get("inference") or {})
    inference_input = dict(stored_inference)
    inference_input.pop("explicitly_labelled", None)
    normalized_inference = _normalize_inference(
        inference_input,
        epistemic_status=epistemic_status,
        claim_origin=claim_origin,
    )
    if normalized_inference != stored_inference:
        raise LifeGraphError("Personal knowledge inference is malformed")
    if dict(properties.get("retrieval_policy") or {}) != RETRIEVAL_POLICY:
        raise LifeGraphError("Personal knowledge retrieval policy is malformed")
    for field in ("observed_at", "reviewed_at", "stale_at", "stale_after"):
        _datetime(properties.get(field), field=f"stored {field}")

    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    has_direct_evidence = False
    citation_fields = {
        "citation_id", "target_kind", "target_id", "relation", "locator",
        "excerpt", "note",
    }
    for raw_citation in properties["citations"]:
        if not isinstance(raw_citation, Mapping) or set(raw_citation) != citation_fields:
            raise LifeGraphError("Stored personal knowledge citation is malformed")
        citation = dict(raw_citation)
        citation_id = _text(
            citation.get("citation_id"), field="stored citation_id", limit=24,
            required=True,
        )
        if not re.fullmatch(r"[0-9a-f]{24}", citation_id):
            raise LifeGraphError("Stored personal knowledge citation id is malformed")
        target_kind = _token(
            citation.get("target_kind"), field="stored citation target_kind",
            choices=CITATION_TARGET_KINDS,
        )
        target_id = _text(
            citation.get("target_id"), field="stored citation target_id",
            limit=255, required=True,
        )
        if target_kind == "memory" and target_id == entity.id:
            raise LifeGraphError("Stored personal knowledge citation cannot cite itself")
        relation = _token(
            citation.get("relation"), field="stored citation relation",
            choices=CITATION_RELATIONS,
        )
        locator = _text(
            citation.get("locator"), field="stored citation locator", limit=500
        )
        if locator:
            _assert_not_path(locator, field="stored citation locator")
        excerpt = _text(
            citation.get("excerpt"), field="stored citation excerpt",
            limit=2_000, preserve_lines=True,
        )
        note = _text(
            citation.get("note"), field="stored citation note",
            limit=1_000, preserve_lines=True,
        )
        if not locator and not excerpt:
            raise LifeGraphError("Stored personal knowledge citation lacks evidence")
        expected_id = hashlib.sha256("\x1f".join((
            target_kind, target_id, relation, locator, excerpt,
        )).encode("utf-8")).hexdigest()[:24]
        if citation_id != expected_id or citation_id in seen:
            raise LifeGraphError("Stored personal knowledge citation digest is malformed")
        seen.add(citation_id)
        has_direct_evidence = has_direct_evidence or target_kind != "memory"
        citations.append({
            "citation_id": citation_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "relation": relation,
            "locator": locator,
            "excerpt": excerpt,
            "note": note,
        })
    if not has_direct_evidence or citations != list(properties["citations"]):
        raise LifeGraphError("Stored personal knowledge citations are malformed")
    provenance = entity.provenance if isinstance(entity.provenance, Mapping) else {}
    _assert_safe_payload(provenance, field="stored personal knowledge provenance")
    citation_ids = [
        row.get("citation_id") for row in properties["citations"]
        if isinstance(row, Mapping)
    ]
    if (
        provenance.get("authority") != PERSONAL_KNOWLEDGE_AUTHORITY
        or provenance.get("memory_kind") != properties.get("memory_kind")
        or provenance.get("claim_origin") != properties.get("claim_origin")
        or list(provenance.get("citation_ids") or []) != citation_ids
    ):
        raise LifeGraphError("Personal knowledge provenance is malformed")
    return dict(properties)


def is_typed_personal_knowledge_payload(
    entity_type: object,
    properties: object,
) -> bool:
    """Return whether generic Life mutation routes must defer to this domain."""

    if str(entity_type or "").strip().lower() != PERSONAL_KNOWLEDGE_ENTITY_TYPE:
        return False
    if not isinstance(properties, Mapping):
        return False
    memory_kind = properties.get("memory_kind")
    epistemic_status = properties.get("epistemic_status")
    return bool(
        properties.get("personal_knowledge_schema_version") is not None
        or properties.get("authority") == PERSONAL_KNOWLEDGE_AUTHORITY
        or (isinstance(memory_kind, str) and memory_kind in MEMORY_KINDS)
        or (
            isinstance(epistemic_status, str)
            and epistemic_status in EPISTEMIC_STATUSES
        )
    )


def is_typed_personal_knowledge_source_payload(
    source_type: object,
    metadata: object,
) -> bool:
    """Return whether generic Life source creation must defer to this domain."""

    normalized_type = str(source_type or "").strip().lower()
    if normalized_type.startswith("knowledge_"):
        return True
    if not isinstance(metadata, Mapping):
        return False
    kind = metadata.get("knowledge_source_kind")
    return bool(
        metadata.get("personal_knowledge_source_schema_version") is not None
        or metadata.get("authority") == PERSONAL_KNOWLEDGE_SOURCE_AUTHORITY
        or (isinstance(kind, str) and kind in KNOWLEDGE_SOURCE_KINDS)
    )


def _owned_record(
    db,
    owner_id: str,
    record_id: object,
    *,
    include_deleted: bool = False,
) -> LifeEntity:
    query = db.query(LifeEntity).filter(
        LifeEntity.id == str(record_id),
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == PERSONAL_KNOWLEDGE_ENTITY_TYPE,
    )
    if not include_deleted:
        query = query.filter(LifeEntity.deleted_at.is_(None))
    entity = query.first()
    if entity is None:
        raise LifeGraphNotFound("Personal knowledge record not found")
    _record_properties(entity)
    return entity


def _citation_with_availability(
    db,
    *,
    account: Account,
    citation: Mapping[str, Any],
    as_of: datetime | None = None,
) -> dict[str, Any]:
    target_kind = str(citation.get("target_kind") or "")
    target_id = str(citation.get("target_id") or "")
    availability = _resolve_reference(
        db,
        account=account,
        target_kind=target_kind,
        target_id=target_id,
        require_available=False,
    )
    future_evidence = False
    if as_of is not None and availability["available"]:
        if target_kind == "life_source":
            source = db.query(LifeSource).filter(
                LifeSource.id == target_id, LifeSource.owner_id == account.id
            ).first()
            if source is not None:
                evidence_time = source.observed_at or source.captured_at
                future_evidence = bool(
                    evidence_time is not None
                    and _datetime(
                        evidence_time, field="citation evidence time", required=True
                    ) > as_of
                )
        elif target_kind in {"memory", "file", "decision", "task", "project"}:
            entity = db.query(LifeEntity).filter(
                LifeEntity.id == target_id, LifeEntity.owner_id == account.id
            ).first()
            if entity is not None and entity.occurred_at is not None:
                future_evidence = bool(
                    _datetime(
                        entity.occurred_at,
                        field="citation entity occurred_at",
                        required=True,
                    ) > as_of
                )
    return {
        **dict(citation),
        "available": bool(availability["available"] and not future_evidence),
        "tombstoned": bool(availability["tombstoned"]),
        "target_title": availability["title"],
        "not_available_as_of": future_evidence,
    }


def _effective_epistemic_status(
    properties: Mapping[str, Any], *, as_of: datetime | None
) -> tuple[str, str | None]:
    explicit = str(properties.get("epistemic_status") or "")
    if explicit == "stale":
        return "stale", "explicitly_stale"
    if as_of is not None and properties.get("stale_after"):
        threshold = _datetime(
            properties.get("stale_after"), field="stale_after", required=True
        )
        if threshold is not None and threshold <= as_of:
            return "stale", "stale_after_elapsed"
    return explicit, None


def serialize_personal_knowledge_record(
    db,
    *,
    entity: LifeEntity,
    as_of: object | None = None,
) -> dict[str, Any]:
    properties = _record_properties(entity)
    account = _account_for_owner(db, entity.owner_id)
    normalized_as_of = _as_of(as_of) if as_of is not None else None
    effective, stale_reason = _effective_epistemic_status(
        properties, as_of=normalized_as_of
    )
    citations = [
        _citation_with_availability(
            db, account=account, citation=row, as_of=normalized_as_of
        )
        for row in properties["citations"]
        if isinstance(row, Mapping)
    ]
    serialized = serialize_life_entity(entity)
    serialized.update({
        "memory_kind": properties["memory_kind"],
        "statement": entity.summary or "",
        "epistemic_status": properties["epistemic_status"],
        "effective_epistemic_status": effective,
        "stale_reason": stale_reason,
        "claim_origin": properties["claim_origin"],
        "citations": citations,
        "observed_at": properties.get("observed_at"),
        "reviewed_at": properties.get("reviewed_at"),
        "stale_at": properties.get("stale_at"),
        "stale_after": properties.get("stale_after"),
        "tags": list(properties.get("tags") or []),
        "details": dict(properties.get("details") or {}),
        "inference": dict(properties.get("inference") or {}),
        "gap": dict(properties.get("gap") or {}) if properties.get("gap") else None,
        "retrieval_policy": dict(RETRIEVAL_POLICY),
    })
    return serialized


def get_personal_knowledge_record(
    db,
    *,
    owner_id: str,
    record_id: object,
    include_deleted: bool = False,
    as_of: object | None = None,
) -> dict[str, Any]:
    entity = _owned_record(
        db, owner_id, record_id, include_deleted=include_deleted
    )
    return serialize_personal_knowledge_record(db, entity=entity, as_of=as_of)


def update_personal_knowledge_record(
    db,
    *,
    owner_id: str,
    record_id: object,
    expected_version: int,
    changes: Mapping[str, Any],
    reason: object = "Personal knowledge record updated",
) -> LifeEntity:
    if not isinstance(changes, Mapping):
        raise LifeGraphError("changes must be an object")
    allowed = {
        "title", "statement", "epistemic_status", "citations", "observed_at",
        "reviewed_at", "stale_at", "stale_after", "tags", "details",
        "inference", "status", "confidence", "sensitivity", "provenance",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise LifeGraphError(
            f"Unsupported personal knowledge fields: {', '.join(unknown)}"
        )
    entity = _owned_record(db, owner_id, record_id)
    properties = _record_properties(entity)
    account = _account_for_owner(db, owner_id)
    current_provenance = dict(entity.provenance or {})
    user_provenance = {
        key: value for key, value in current_provenance.items()
        if key not in {
            "authority", "memory_kind", "claim_origin", "source_ids",
            "linked_record_ids", "citation_ids",
        }
    }
    payload = {
        "memory_kind": properties["memory_kind"],
        "title": entity.title,
        "statement": entity.summary,
        "epistemic_status": properties["epistemic_status"],
        "claim_origin": properties["claim_origin"],
        "citations": properties["citations"],
        "observed_at": properties.get("observed_at"),
        "reviewed_at": properties.get("reviewed_at"),
        "stale_at": properties.get("stale_at"),
        "stale_after": properties.get("stale_after"),
        "tags": properties.get("tags") or [],
        "details": properties.get("details") or {},
        "inference": properties.get("inference") or {},
        "status": entity.status,
        "sensitivity": entity.sensitivity,
        "provenance": user_provenance,
    }
    for key, value in changes.items():
        if key not in {"confidence"}:
            payload[key] = value
    normalized = _normalize_record_payload(
        db,
        account=account,
        record_id=entity.id,
        **payload,
    )
    normalized_changes: dict[str, Any] = {
        "title": normalized["title"],
        "summary": normalized["statement"],
        "status": normalized["status"],
        "properties": normalized["properties"],
        "provenance": normalized["provenance"],
        "sensitivity": normalized["sensitivity"],
        "occurred_at": normalized["observed_at"],
        "review_at": normalized["reviewed_at"],
    }
    if "confidence" in changes:
        normalized_changes["confidence"] = changes["confidence"]
    return update_life_entity(
        db,
        owner_id=owner_id,
        entity_id=entity.id,
        expected_version=expected_version,
        changes=normalized_changes,
        reason=_text(reason, field="reason", limit=2_000, required=True),
    )


def delete_personal_knowledge_record(
    db,
    *,
    owner_id: str,
    record_id: object,
    expected_version: int,
    reason: object = "Personal knowledge record deleted",
) -> LifeEntity:
    entity = _owned_record(db, owner_id, record_id)
    return delete_life_entity(
        db,
        owner_id=owner_id,
        entity_id=entity.id,
        expected_version=expected_version,
        reason=_text(reason, field="reason", limit=2_000, required=True),
    )


def personal_knowledge_history(
    db,
    *,
    owner_id: str,
    record_id: object,
    limit: int = 50,
) -> tuple[list[dict[str, Any]], bool]:
    entity = _owned_record(db, owner_id, record_id, include_deleted=True)
    rows, truncated = list_life_entity_versions(
        db, owner_id=owner_id, entity_id=entity.id, limit=limit
    )
    return [serialize_life_entity_version(row) for row in rows], truncated


def _record_candidates(
    db, *, owner_id: str, include_deleted: bool = False
) -> tuple[list[LifeEntity], bool]:
    query = db.query(LifeEntity).filter(
        LifeEntity.owner_id == owner_id,
        LifeEntity.entity_type == PERSONAL_KNOWLEDGE_ENTITY_TYPE,
    )
    if not include_deleted:
        query = query.filter(LifeEntity.deleted_at.is_(None))
    candidates = query.order_by(
        LifeEntity.updated_at.desc(), LifeEntity.id.desc()
    ).limit(PERSONAL_KNOWLEDGE_SCAN_LIMIT + 1).all()
    truncated = len(candidates) > PERSONAL_KNOWLEDGE_SCAN_LIMIT
    rows: list[LifeEntity] = []
    for entity in candidates[:PERSONAL_KNOWLEDGE_SCAN_LIMIT]:
        try:
            _record_properties(entity)
        except LifeGraphNotFound:
            continue
        rows.append(entity)
    return rows, truncated


def list_personal_knowledge_records(
    db,
    *,
    owner_id: str,
    memory_kind: object | None = None,
    epistemic_status: object | None = None,
    status: object | None = None,
    include_deleted: bool = False,
    limit: int = 50,
    as_of: object | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    bounded = max(1, min(100, int(limit)))
    normalized_kind = (
        _token(memory_kind, field="memory_kind", choices=MEMORY_KINDS)
        if memory_kind else None
    )
    normalized_epistemic = (
        _token(
            epistemic_status,
            field="epistemic_status",
            choices=EPISTEMIC_STATUSES,
        ) if epistemic_status else None
    )
    normalized_status = (
        _token(status, field="status", choices=RECORD_STATUSES)
        if status else None
    )
    candidates, scan_truncated = _record_candidates(
        db, owner_id=owner_id, include_deleted=include_deleted
    )
    admitted: list[LifeEntity] = []
    for entity in candidates:
        properties = _record_properties(entity)
        if as_of is not None:
            normalized_as_of = _as_of(as_of)
            explicit_times = [
                _datetime(properties.get(field), field=field)
                for field in ("observed_at", "reviewed_at")
            ]
            if any(
                value is not None and value > normalized_as_of
                for value in explicit_times
            ):
                continue
        if normalized_kind and properties["memory_kind"] != normalized_kind:
            continue
        if normalized_epistemic and (
            properties["epistemic_status"] != normalized_epistemic
        ):
            continue
        if normalized_status and entity.status != normalized_status:
            continue
        admitted.append(entity)
        if len(admitted) > bounded:
            break
    rows = [
        serialize_personal_knowledge_record(db, entity=entity, as_of=as_of)
        for entity in admitted[:bounded]
    ]
    return rows, scan_truncated or len(admitted) > bounded


def search_personal_knowledge_records(
    db,
    *,
    owner_id: str,
    query_text: object,
    memory_kind: object | None = None,
    epistemic_status: object | None = None,
    limit: int = 25,
    as_of: object | None = None,
) -> dict[str, Any]:
    needle = _text(query_text, field="q", limit=500, required=True).casefold()
    bounded = max(1, min(100, int(limit)))
    normalized_kind = (
        _token(memory_kind, field="memory_kind", choices=MEMORY_KINDS)
        if memory_kind else None
    )
    normalized_epistemic = (
        _token(
            epistemic_status,
            field="epistemic_status",
            choices=EPISTEMIC_STATUSES,
        ) if epistemic_status else None
    )
    normalized_as_of = _as_of(as_of) if as_of is not None else None
    candidates, scan_truncated = _record_candidates(db, owner_id=owner_id)
    matches: list[tuple[tuple[int, str, str], LifeEntity, str]] = []
    for entity in candidates:
        props = _record_properties(entity)
        if normalized_as_of is not None:
            explicit_times = [
                _datetime(props.get(field), field=field)
                for field in ("observed_at", "reviewed_at")
            ]
            if any(
                value is not None and value > normalized_as_of
                for value in explicit_times
            ):
                continue
        if normalized_kind and props["memory_kind"] != normalized_kind:
            continue
        if normalized_epistemic and props["epistemic_status"] != normalized_epistemic:
            continue
        title = str(entity.title or "").casefold()
        statement = str(entity.summary or "").casefold()
        tags = " ".join(str(item) for item in props.get("tags") or []).casefold()
        details = json.dumps(
            props.get("details") or {}, ensure_ascii=False, sort_keys=True
        ).casefold()
        if title == needle:
            rank, field = 0, "title"
        elif title.startswith(needle):
            rank, field = 1, "title"
        elif needle in title:
            rank, field = 2, "title"
        elif needle in statement:
            rank, field = 3, "statement"
        elif needle in tags:
            rank, field = 4, "tags"
        elif needle in details:
            rank, field = 5, "details"
        else:
            continue
        matches.append(((rank, title, entity.id), entity, field))
    matches.sort(key=lambda item: item[0])
    aware_as_of = (
        normalized_as_of.replace(tzinfo=timezone.utc)
        if normalized_as_of is not None else None
    )
    items = [
        {
            "record": serialize_personal_knowledge_record(
                db, entity=entity, as_of=aware_as_of
            ),
            "match": field,
            "rank": sort_key[0],
        }
        for sort_key, entity, field in matches[:bounded]
    ]
    return {
        "items": items,
        "count": min(len(matches), bounded),
        "scanned": len(candidates),
        "truncated": scan_truncated or len(matches) > bounded,
        "method": "bounded_deterministic_personal_knowledge_search_v1",
        "retrieval_policy": dict(RETRIEVAL_POLICY),
    }


def list_stale_personal_knowledge(
    db,
    *,
    owner_id: str,
    as_of: object,
    memory_kind: object | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    normalized_as_of = _as_of(as_of)
    bounded = max(1, min(100, int(limit)))
    normalized_kind = (
        _token(memory_kind, field="memory_kind", choices=MEMORY_KINDS)
        if memory_kind else None
    )
    candidates, scan_truncated = _record_candidates(db, owner_id=owner_id)
    rows: list[tuple[tuple[str, str], LifeEntity, str | None]] = []
    aware_as_of = normalized_as_of.replace(tzinfo=timezone.utc)
    for entity in candidates:
        properties = _record_properties(entity)
        if normalized_kind and properties["memory_kind"] != normalized_kind:
            continue
        effective, reason = _effective_epistemic_status(
            properties, as_of=normalized_as_of
        )
        if effective != "stale":
            continue
        threshold = str(
            properties.get("stale_at") or properties.get("stale_after") or ""
        )
        rows.append(((threshold, entity.id), entity, reason))
    rows.sort(key=lambda item: item[0])
    items = [
        {
            "record": serialize_personal_knowledge_record(
                db, entity=entity, as_of=aware_as_of
            ),
            "reason": reason,
            "threshold": sort_key[0] or None,
        }
        for sort_key, entity, reason in rows[:bounded]
    ]
    return {
        "items": items,
        "count": min(len(rows), bounded),
        "scanned": len(candidates),
        "truncated": scan_truncated or len(rows) > bounded,
        "as_of": _iso(normalized_as_of),
        "method": "deterministic_explicit_staleness_v1",
        "mutates_records": False,
        "retrieval_policy": dict(RETRIEVAL_POLICY),
    }


def citation_backed_answer_evidence(
    db,
    *,
    owner_id: str,
    query_text: object,
    as_of: object,
    memory_kind: object | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Return bounded evidence for an answer; never synthesize the answer.

    Every admitted claim has at least one currently available, direct citation.
    Gaps are kept separate and stale/model-derived claims retain explicit
    warnings.  The caller may formulate an answer, but this function cannot
    turn an inference into confirmed authority.
    """

    normalized_as_of = _as_of(as_of)
    bounded = max(1, min(50, int(limit)))
    aware_as_of = normalized_as_of.replace(tzinfo=timezone.utc)
    search = search_personal_knowledge_records(
        db,
        owner_id=owner_id,
        query_text=query_text,
        memory_kind=memory_kind,
        limit=min(100, max(bounded * 4, bounded)),
        as_of=aware_as_of,
    )
    evidence: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    unsupported: list[dict[str, Any]] = []
    for match in search["items"]:
        record = match["record"]
        all_direct_available = [
            citation for citation in record["citations"]
            if citation.get("available")
            and citation.get("target_kind") != "memory"
        ]
        direct_available = [
            citation for citation in all_direct_available
            if citation.get("relation") in SUPPORTING_CITATION_RELATIONS
        ]
        contradicting = [
            citation for citation in all_direct_available
            if citation.get("relation") == "contradicts"
        ]
        unavailable = [
            citation for citation in record["citations"]
            if not citation.get("available")
        ]
        base = {
            "record_id": record["id"],
            "memory_kind": record["memory_kind"],
            "title": record["title"],
            "statement": record["statement"],
            "epistemic_status": record["epistemic_status"],
            "effective_epistemic_status": record["effective_epistemic_status"],
            "claim_origin": record["claim_origin"],
            "confidence": record["confidence"],
            "citations": direct_available,
            "contradicting_citations": contradicting,
            "unavailable_citations": unavailable,
            "match": match["match"],
            "rank": match["rank"],
        }
        if record["epistemic_status"] == "gap":
            gaps.append({**base, "reason": "explicit_knowledge_gap"})
            continue
        if not direct_available:
            unsupported.append({
                **base,
                "reason": "no_available_supporting_direct_citation",
            })
            continue
        warnings: list[str] = []
        if record["effective_epistemic_status"] == "stale":
            warnings.append("stale")
        if record["epistemic_status"] == "assumption":
            warnings.append("assumption")
        if record["claim_origin"] == "model" or (
            record["epistemic_status"] == "inference"
        ):
            warnings.append("explicit_inference_not_confirmed_authority")
        if contradicting:
            warnings.append("contradicting_evidence_present")
        evidence.append({**base, "warnings": warnings})

    return {
        "query": _text(query_text, field="q", limit=500, required=True),
        "as_of": _iso(normalized_as_of),
        "evidence": evidence[:bounded],
        "gaps": gaps[:bounded],
        "unsupported": unsupported[:bounded],
        "evidence_count": min(len(evidence), bounded),
        "truncated": bool(
            search["truncated"]
            or len(evidence) > bounded
            or len(gaps) > bounded
            or len(unsupported) > bounded
        ),
        "method": "deterministic_citation_backed_evidence_v1",
        "synthesizes_answer": False,
        "mutates_records": False,
        "retrieval_policy": dict(RETRIEVAL_POLICY),
    }


def markdown_index_rebuild_manifest(
    db,
    *,
    owner_id: str,
    limit: int = 50,
) -> dict[str, Any]:
    """Describe rebuild inputs for derived Markdown search data.

    The manifest reads existing Document content only to calculate the current
    digest.  It never returns the content and never writes a user file or index.
    """

    account = _account_for_owner(db, owner_id)
    bounded = max(1, min(100, int(limit)))
    candidates = db.query(LifeSource).filter(
        LifeSource.owner_id == owner_id,
        LifeSource.source_type == _knowledge_source_type("markdown"),
    ).order_by(
        LifeSource.captured_at.desc(), LifeSource.id.desc()
    ).limit(PERSONAL_KNOWLEDGE_SOURCE_SCAN_LIMIT + 1).all()
    scan_truncated = len(candidates) > PERSONAL_KNOWLEDGE_SOURCE_SCAN_LIMIT
    rows: list[dict[str, Any]] = []
    for source in candidates[:PERSONAL_KNOWLEDGE_SOURCE_SCAN_LIMIT]:
        if _source_kind_from_row(source) != "markdown":
            continue
        metadata = source.meta_data if isinstance(source.meta_data, Mapping) else {}
        descriptor = metadata.get("descriptor")
        if not isinstance(descriptor, Mapping):
            continue
        document_id = str(descriptor.get("target_id") or "")
        document = db.query(Document).filter(
            Document.id == document_id, Document.owner == account.username
        ).first()
        current_digest = (
            hashlib.sha256(
                str(document.current_content or "").encode("utf-8")
            ).hexdigest()
            if document is not None else None
        )
        contract = dict(descriptor.get("index_contract") or {})
        contract_valid = contract == MARKDOWN_INDEX_CONTRACT
        rows.append({
            "source_id": source.id,
            "document_id": document_id,
            "durable_record_available": document is not None,
            "recorded_content_sha256": source.content_sha256,
            "current_content_sha256": current_digest,
            "content_changed_since_capture": bool(
                current_digest is not None
                and source.content_sha256 is not None
                and current_digest != source.content_sha256
            ),
            "index_contract": contract,
            "rebuildable_now": bool(document is not None and contract_valid),
        })
    return {
        "items": rows[:bounded],
        "count": min(len(rows), bounded),
        "scanned": min(
            len(candidates), PERSONAL_KNOWLEDGE_SOURCE_SCAN_LIMIT
        ),
        "truncated": scan_truncated or len(rows) > bounded,
        "authority": "durable_markdown_document_not_derived_index",
        "method": "read_only_markdown_index_manifest_v1",
        "writes_user_files": False,
        "mutates_records": False,
        "retrieval_policy": dict(RETRIEVAL_POLICY),
    }


__all__ = [
    "CITATION_TARGET_KINDS",
    "CLAIM_ORIGINS",
    "EPISTEMIC_STATUSES",
    "KNOWLEDGE_SOURCE_KINDS",
    "MARKDOWN_INDEX_CONTRACT",
    "MEMORY_KINDS",
    "PERSONAL_KNOWLEDGE_AUTHORITY",
    "PERSONAL_KNOWLEDGE_ENTITY_TYPE",
    "RETRIEVAL_POLICY",
    "citation_backed_answer_evidence",
    "create_knowledge_source",
    "create_personal_knowledge_record",
    "delete_personal_knowledge_record",
    "get_knowledge_source",
    "get_personal_knowledge_record",
    "is_typed_personal_knowledge_payload",
    "list_knowledge_sources",
    "list_personal_knowledge_records",
    "list_stale_personal_knowledge",
    "markdown_index_rebuild_manifest",
    "personal_knowledge_history",
    "search_knowledge_sources",
    "search_personal_knowledge_records",
    "serialize_personal_knowledge_record",
    "update_personal_knowledge_record",
]
