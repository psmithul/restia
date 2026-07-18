"""Owner-scoped database authority for contacts and CardDAV snapshots."""

from __future__ import annotations

import csv
import io
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from core.database import (
    ContactDelivery,
    ContactRecord,
    ContactSource,
    utcnow_naive,
)
from src import carddav_contacts as carddav
from src.contact_delivery import (
    enqueue_contact_delivery,
    open_delivery_record_ids,
)
from src.audit_context import (
    SESSION_AUDIT_CONTEXT_KEY,
    bind_service_audit_context,
)
from src.life_core import append_action_audit
from src.secret_storage import encrypt_plaintext, private_digest


CONTACT_REFRESH_SECONDS = 60
CONTACT_REFRESH_LEASE_SECONDS = 45
MAX_CONTACT_IMPORT_CHARS = 16 * 1024 * 1024
MAX_CONTACT_IMPORT_ROWS = 100_000
MAX_CONTACT_NAME_CHARS = 1_000
MAX_CONTACT_EMAIL_CHARS = 512
MAX_CONTACT_PHONE_CHARS = 256
MAX_CONTACT_ADDRESS_CHARS = 8_192
MAX_CONTACT_VALUES = 64
_UNSET = object()


class ContactServiceError(RuntimeError):
    pass


class ContactNotFound(ContactServiceError):
    pass


class ContactConflict(ContactServiceError):
    pass


def _owner_id(value: object) -> str:
    owner_id = str(value or "").strip()
    if not owner_id:
        raise ContactServiceError("A concrete contact owner is required")
    return owner_id


def _source_config(source: ContactSource) -> dict[str, str]:
    return {
        "url": str(source.base_url or ""),
        "username": str(source.username or ""),
        "password": str(source.password or ""),
    }


def _uid_digest(uid: object) -> str:
    value = str(uid or "").strip()
    if not value or len(value) > 512:
        raise ContactServiceError("Contact UID is invalid")
    return private_digest("contact-remote-uid-v1", value)


def _validated_contact(contact: dict[str, Any]) -> dict[str, Any]:
    normalized = carddav.normalize_contact(contact)
    if len(str(normalized.get("name") or "")) > MAX_CONTACT_NAME_CHARS:
        raise ContactServiceError("Contact name is too long")
    emails = list(normalized.get("emails") or [])
    phones = list(normalized.get("phones") or [])
    if len(emails) > MAX_CONTACT_VALUES or any(
        len(str(value or "")) > MAX_CONTACT_EMAIL_CHARS for value in emails
    ):
        raise ContactServiceError("Contact email data is too large")
    if len(phones) > MAX_CONTACT_VALUES or any(
        len(str(value or "")) > MAX_CONTACT_PHONE_CHARS for value in phones
    ):
        raise ContactServiceError("Contact phone data is too large")
    if len(str(normalized.get("address") or "")) > MAX_CONTACT_ADDRESS_CHARS:
        raise ContactServiceError("Contact address is too long")
    return normalized


def _new_credential_plaintext(value: object) -> str | None:
    """Preserve a real password that happens to start with reserved ``enc:``."""

    plaintext = str(value or "")
    if not plaintext:
        return None
    return encrypt_plaintext(plaintext) if plaintext.startswith("enc:") else plaintext


def _source_state(source: ContactSource) -> dict[str, Any]:
    return {
        "kind": source.kind,
        "enabled": bool(source.enabled),
        "configured": bool(str(source.base_url or "").strip()),
        "sync_state": source.sync_state,
        "config_version": int(source.config_version or 1),
        "version": int(source.version or 1),
    }


def _record_state(row: ContactRecord) -> dict[str, Any]:
    payload = dict(row.payload or {})
    return {
        "source_id": row.source_id,
        "version": int(row.version or 1),
        "deleted": row.deleted_at is not None,
        "email_count": len(payload.get("emails") or []),
        "phone_count": len(payload.get("phones") or []),
        "has_address": bool(str(payload.get("address") or "").strip()),
    }


def _reserve_record_version(
    db, *, row: ContactRecord, owner_id: str, expected_version: int,
) -> None:
    expected = int(expected_version)
    if expected < 1:
        raise ContactConflict("Contact version is invalid")
    now = utcnow_naive()
    claimed = db.query(ContactRecord).filter(
        ContactRecord.id == row.id,
        ContactRecord.owner_id == owner_id,
        ContactRecord.version == expected,
    ).update(
        {
            ContactRecord.version: expected + 1,
            ContactRecord.updated_at: now,
        },
        synchronize_session=False,
    )
    if claimed != 1:
        raise ContactConflict("Contact changed; reload it before saving")
    db.flush()
    db.expire(row)


def get_local_source(db, *, owner_id: str, create: bool) -> ContactSource | None:
    owner = _owner_id(owner_id)
    source = db.query(ContactSource).filter(
        ContactSource.owner_id == owner,
        ContactSource.kind == "local",
    ).first()
    if source is not None or not create:
        return source
    source = ContactSource(
        id=str(uuid.uuid4()),
        owner_id=owner,
        kind="local",
        label="Local contacts",
        enabled=True,
        sync_state="ready",
    )
    try:
        with db.begin_nested():
            db.add(source)
            db.flush()
    except IntegrityError:
        source = db.query(ContactSource).filter(
            ContactSource.owner_id == owner,
            ContactSource.kind == "local",
        ).one()
    return source


def get_carddav_source(db, *, owner_id: str) -> ContactSource | None:
    return db.query(ContactSource).filter(
        ContactSource.owner_id == _owner_id(owner_id),
        ContactSource.kind == "carddav",
    ).order_by(ContactSource.created_at.asc(), ContactSource.id.asc()).first()


def get_default_contact_source(
    db, *, owner_id: str, create_local: bool
) -> ContactSource | None:
    remote = get_carddav_source(db, owner_id=owner_id)
    if remote is not None and remote.enabled and str(remote.base_url or "").strip():
        return remote
    return get_local_source(db, owner_id=owner_id, create=create_local)


def serialize_source_config(source: ContactSource | None) -> dict[str, Any]:
    if source is None:
        return {
            "url": "",
            "username": "",
            "password": "",
            "version": None,
            "state_version": None,
            "sync_state": "disabled",
            "sync_error": False,
        }
    return {
        "url": str(source.base_url or ""),
        "username": str(source.username or ""),
        "password": "***" if source.password else "",
        "version": int(source.config_version or 1),
        "state_version": int(source.version or 1),
        "sync_state": str(source.sync_state or "idle"),
        "sync_error": bool(source.sync_state == "error"),
    }


def get_contact_config(db, *, owner_id: str) -> dict[str, Any]:
    return serialize_source_config(get_carddav_source(db, owner_id=owner_id))


def upsert_carddav_config(
    db,
    *,
    owner_id: str,
    url: object = _UNSET,
    username: object = _UNSET,
    password: object = _UNSET,
    label: str = "CardDAV",
    expected_version: int | None = None,
) -> ContactSource:
    owner = _owner_id(owner_id)
    source = get_carddav_source(db, owner_id=owner)
    created = source is None
    if created:
        candidate = ContactSource(
            id=str(uuid.uuid4()),
            owner_id=owner,
            kind="carddav",
            label=str(label or "CardDAV")[:160],
            enabled=False,
            sync_state="disabled",
        )
        try:
            with db.begin_nested():
                db.add(candidate)
                db.flush()
            source = candidate
        except IntegrityError:
            source = get_carddav_source(db, owner_id=owner)
            if source is None:
                raise
            created = False
    before = {} if created else _source_state(source)
    if not created:
        current_config_version = int(source.config_version or 1)
        if (
            expected_version is not None
            and current_config_version != int(expected_version)
        ):
            raise ContactConflict("Contact configuration changed; reload before saving")
        claimed = db.query(ContactSource).filter(
            ContactSource.id == source.id,
            ContactSource.owner_id == owner,
            ContactSource.config_version == current_config_version,
        ).update(
            {
                ContactSource.config_version: current_config_version + 1,
                ContactSource.version: ContactSource.version + 1,
                ContactSource.updated_at: utcnow_naive(),
            },
            synchronize_session=False,
        )
        if claimed != 1:
            raise ContactConflict(
                "Contact configuration changed; reload before saving"
            )
        db.flush()
        db.expire(source)
    previous_url = str(source.base_url or "")
    candidate_url = previous_url

    if url is not _UNSET:
        cleaned = str(url or "").strip()
        candidate_url = carddav.validate_carddav_url(cleaned) if cleaned else ""
    def origin(value: str) -> tuple[str, str, int | None]:
        parsed = urlparse(value) if value else None
        return (
            str(getattr(parsed, "scheme", "") or "").lower(),
            str(getattr(parsed, "hostname", "") or "").lower(),
            getattr(parsed, "port", None),
        )
    url_changed = previous_url.rstrip("/") != candidate_url.rstrip("/")
    origin_changed = url_changed and origin(previous_url) != origin(candidate_url)
    fresh_password = password is not _UNSET and password != "***"
    if origin_changed or (url_changed and not candidate_url):
        # Never carry a stored Basic credential (or identifying username) to a
        # new origin. The user must provide a fresh password in the same edit.
        source.username = None
        source.password = None
    source.base_url = candidate_url or None
    if username is not _UNSET and (not origin_changed or fresh_password):
        source.username = str(username or "") or None
    if fresh_password:
        source.password = _new_credential_plaintext(password)

    configured = bool(str(source.base_url or "").strip())
    source.enabled = configured
    source.sync_state = "idle" if configured else "disabled"
    source.last_error = None
    if created:
        source.config_version = 1
        source.version = 1
    db.flush()
    append_action_audit(
        db,
        owner_id=owner,
        action="contacts.configured",
        entity_type="contact_source",
        entity_id=source.id,
        reason="Contact source configuration changed",
        before_state=before,
        after_state=_source_state(source),
        details={
            "created": created,
            "origin_changed": origin_changed,
            "credentials_rebound": bool(origin_changed and fresh_password),
        },
    )
    if fresh_password:
        db.expire(source, ["password"])
    return source


def _record_query(db, *, owner_id: str, source_id: str):
    return db.query(ContactRecord).filter(
        ContactRecord.owner_id == _owner_id(owner_id),
        ContactRecord.source_id == str(source_id),
    )


def _serialize_record(row: ContactRecord) -> dict[str, Any]:
    payload = dict(row.payload or {})
    contact = carddav.normalize_contact({**payload, "uid": row.remote_uid})
    if row.remote_href:
        contact["href"] = row.remote_href
    contact.update({
        "id": row.id,
        "source_id": row.source_id,
        "version": int(row.version or 1),
        "deleted": row.deleted_at is not None,
    })
    return contact


def _serialize_rows(db, rows: list[ContactRecord]) -> list[dict[str, Any]]:
    serialized = [_serialize_record(row) for row in rows]
    if not rows:
        return serialized
    states: dict[str, set[str]] = {}
    for record_id, state in db.query(
        ContactDelivery.record_id, ContactDelivery.state,
    ).filter(
        ContactDelivery.owner_id == rows[0].owner_id,
        ContactDelivery.record_id.in_([row.id for row in rows]),
        ContactDelivery.state != "completed",
    ).all():
        states.setdefault(str(record_id), set()).add(str(state))
    for contact, row in zip(serialized, rows):
        record_states = states.get(row.id, set())
        contact["sync_state"] = (
            "conflict" if "conflict" in record_states
            else "pending" if record_states
            else "synced"
        )
    return serialized


def _upsert_record(
    db,
    *,
    owner_id: str,
    source: ContactSource,
    contact: dict[str, Any],
    raw_vcard: str | None = None,
    href: str | None = None,
    etag: str | None = None,
) -> ContactRecord:
    normalized = _validated_contact(contact)
    uid = str(normalized["uid"] or "").strip()
    digest = _uid_digest(uid)
    row = _record_query(
        db, owner_id=owner_id, source_id=source.id,
    ).filter(ContactRecord.remote_uid_digest == digest).first()
    next_payload = {
        "name": normalized["name"],
        "emails": normalized["emails"],
        "phones": normalized["phones"],
        "address": normalized["address"],
    }
    next_href = str(href or "") or None
    next_etag = str(etag or "") or None
    next_raw = str(raw_vcard or "") or None
    if row is None:
        row = ContactRecord(
            id=str(uuid.uuid4()),
            owner_id=_owner_id(owner_id),
            source_id=source.id,
            remote_uid=uid,
            remote_uid_digest=digest,
            payload=next_payload,
            remote_href=next_href,
            remote_etag=next_etag,
            raw_vcard=next_raw,
            version=1,
        )
        db.add(row)
    else:
        changed = any((
            dict(row.payload or {}) != next_payload,
            (row.remote_href or None) != next_href,
            (row.remote_etag or None) != next_etag,
            (row.raw_vcard or None) != next_raw,
            row.deleted_at is not None,
        ))
        if changed:
            _reserve_record_version(
                db,
                row=row,
                owner_id=_owner_id(owner_id),
                expected_version=int(row.version or 1),
            )
            row.payload = next_payload
            row.remote_href = next_href
            row.remote_etag = next_etag
            row.raw_vcard = next_raw
            row.deleted_at = None
    db.flush()
    return row


def _rows_for_source(
    db,
    *,
    owner_id: str,
    source_id: str,
    include_deleted_conflicts: bool = False,
) -> list[ContactRecord]:
    query = _record_query(db, owner_id=owner_id, source_id=source_id)
    if include_deleted_conflicts:
        conflict_ids = db.query(ContactDelivery.record_id).filter(
            ContactDelivery.owner_id == owner_id,
            ContactDelivery.source_id == source_id,
            ContactDelivery.state == "conflict",
        )
        query = query.filter(or_(
            ContactRecord.deleted_at.is_(None),
            ContactRecord.id.in_(conflict_ids),
        ))
    else:
        query = query.filter(ContactRecord.deleted_at.is_(None))
    return query.order_by(
        ContactRecord.updated_at.desc(), ContactRecord.id.asc(),
    ).all()


def _rows_for_owner(
    db, *, owner_id: str, include_deleted_conflicts: bool,
) -> list[ContactRecord]:
    owner = _owner_id(owner_id)
    query = db.query(ContactRecord).filter(ContactRecord.owner_id == owner)
    if include_deleted_conflicts:
        conflict_ids = db.query(ContactDelivery.record_id).filter(
            ContactDelivery.owner_id == owner,
            ContactDelivery.state == "conflict",
        )
        query = query.filter(or_(
            ContactRecord.deleted_at.is_(None),
            ContactRecord.id.in_(conflict_ids),
        ))
    else:
        query = query.filter(ContactRecord.deleted_at.is_(None))
    return query.order_by(
        ContactRecord.updated_at.desc(), ContactRecord.id.asc(),
    ).all()


def _bind_detached_contact_audit(
    db, *, owner_id: str, audit_context: dict[str, Any] | None,
) -> None:
    """Restore trusted request attribution on a short-lived DB session."""

    if audit_context:
        context = dict(audit_context)
        # The caller may choose the interface/credential, but this domain
        # operation can only ever act as the owner passed through its DB fence.
        context["actor_id"] = owner_id
        db.info[SESSION_AUDIT_CONTEXT_KEY] = context
        return
    bind_service_audit_context(
        db,
        account_id=owner_id,
        interface="domain_service",
        actor_type="connector",
        credential_type="carddav",
    )


def _read_contact_source_snapshot(
    session_factory,
    *,
    owner_id: str,
    source_id: str,
) -> list[dict[str, Any]]:
    db = session_factory()
    try:
        source = db.query(ContactSource).filter(
            ContactSource.id == str(source_id),
            ContactSource.owner_id == owner_id,
        ).first()
        if source is None:
            raise ContactNotFound("Contact source not found")
        rows = _rows_for_source(
            db,
            owner_id=owner_id,
            source_id=source.id,
            include_deleted_conflicts=True,
        )
        result = _serialize_rows(db, rows)
        db.rollback()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _claim_contact_refresh(
    session_factory,
    *,
    owner_id: str,
    source_id: str,
    audit_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Claim one refresh in a committed, bounded database transaction."""

    db = session_factory()
    try:
        _bind_detached_contact_audit(
            db, owner_id=owner_id, audit_context=audit_context,
        )
        source = db.query(ContactSource).filter(
            ContactSource.id == str(source_id),
            ContactSource.owner_id == owner_id,
        ).first()
        if source is None:
            raise ContactNotFound("Contact source not found")
        if source.kind != "carddav" or not source.enabled or not source.base_url:
            db.rollback()
            return {"claimed": False, "disabled": True}

        now = utcnow_naive()
        claimed_at = source.updated_at
        if claimed_at is not None and getattr(claimed_at, "tzinfo", None):
            claimed_at = claimed_at.replace(tzinfo=None)
        claim_is_fresh = (
            source.sync_state == "syncing"
            and claimed_at is not None
            and now - claimed_at
            < timedelta(seconds=CONTACT_REFRESH_LEASE_SECONDS)
        )
        if claim_is_fresh:
            db.rollback()
            return {"claimed": False, "in_progress": True}

        before = _source_state(source)
        config_version = int(source.config_version or 1)
        source_version = int(source.version or 1)
        updated = db.query(ContactSource).filter(
            ContactSource.id == source.id,
            ContactSource.owner_id == owner_id,
            ContactSource.config_version == config_version,
            ContactSource.version == source_version,
        ).update(
            {
                ContactSource.sync_state: "syncing",
                ContactSource.last_error: None,
                ContactSource.version: source_version + 1,
                ContactSource.updated_at: now,
            },
            synchronize_session=False,
        )
        if updated != 1:
            raise ContactConflict("Another CardDAV refresh already started")
        snapshot = {
            "claimed": True,
            "source_id": source.id,
            "config_version": config_version,
            "claim_version": source_version + 1,
            "config": _source_config(source),
            "before": before,
        }
        # The refresh lease must be visible before DNS resolution or HTTP.
        db.commit()
        return snapshot
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _record_contact_refresh_failure(
    session_factory,
    *,
    owner_id: str,
    snapshot: dict[str, Any],
    audit_context: dict[str, Any] | None,
) -> bool:
    """Record failure only while this exact refresh lease still owns state."""

    db = session_factory()
    try:
        _bind_detached_contact_audit(
            db, owner_id=owner_id, audit_context=audit_context,
        )
        now = utcnow_naive()
        updated = db.query(ContactSource).filter(
            ContactSource.id == snapshot["source_id"],
            ContactSource.owner_id == owner_id,
            ContactSource.config_version == snapshot["config_version"],
            ContactSource.version == snapshot["claim_version"],
            ContactSource.sync_state == "syncing",
        ).update(
            {
                ContactSource.sync_state: "error",
                ContactSource.last_error: "CardDAV refresh failed",
                ContactSource.version: snapshot["claim_version"] + 1,
                ContactSource.updated_at: now,
            },
            synchronize_session=False,
        )
        if updated != 1:
            db.rollback()
            return False
        source = db.query(ContactSource).filter(
            ContactSource.id == snapshot["source_id"],
            ContactSource.owner_id == owner_id,
        ).one()
        append_action_audit(
            db,
            owner_id=owner_id,
            action="contacts.refresh_failed",
            entity_type="contact_source",
            entity_id=source.id,
            reason="CardDAV snapshot refresh failed",
            before_state=dict(snapshot["before"]),
            after_state=_source_state(source),
            details={"error_code": "refresh_failed"},
            outcome="failure",
        )
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _apply_contact_refresh(
    session_factory,
    *,
    owner_id: str,
    snapshot: dict[str, Any],
    prepared: list[tuple[dict[str, Any], str]],
    observed: set[str],
    audit_context: dict[str, Any] | None,
) -> None:
    """Apply a detached response behind config and refresh-lease fences."""

    db = session_factory()
    try:
        _bind_detached_contact_audit(
            db, owner_id=owner_id, audit_context=audit_context,
        )
        now = utcnow_naive()
        # Reserve the exact source generation before touching materialized
        # records. This CAS also holds the source row's write lock until the
        # apply transaction commits, so a delivery/config writer cannot be
        # overwritten by later ORM flushes in this transaction.
        reserved = db.query(ContactSource).filter(
            ContactSource.id == snapshot["source_id"],
            ContactSource.owner_id == owner_id,
            ContactSource.config_version == snapshot["config_version"],
            ContactSource.version == snapshot["claim_version"],
            ContactSource.sync_state == "syncing",
        ).update(
            {
                ContactSource.version: snapshot["claim_version"] + 1,
                ContactSource.updated_at: now,
            },
            synchronize_session=False,
        )
        if reserved != 1:
            raise ContactConflict(
                "Contact source changed while CardDAV was refreshing"
            )
        source = db.query(ContactSource).filter(
            ContactSource.id == snapshot["source_id"],
            ContactSource.owner_id == owner_id,
        ).one()
        protected_ids = open_delivery_record_ids(
            db, owner_id=owner_id, source_id=source.id,
        )
        for contact, digest in prepared:
            existing = _record_query(
                db, owner_id=owner_id, source_id=source.id,
            ).filter(ContactRecord.remote_uid_digest == digest).first()
            if existing is not None and existing.id in protected_ids:
                continue
            _upsert_record(
                db,
                owner_id=owner_id,
                source=source,
                contact=contact,
                raw_vcard=str(contact.get("raw_vcard") or "") or None,
                href=str(contact.get("href") or "") or None,
                etag=str(contact.get("etag") or "") or None,
            )
        for row in _rows_for_source(
            db, owner_id=owner_id, source_id=source.id,
        ):
            if row.id not in protected_ids and row.remote_uid_digest not in observed:
                _reserve_record_version(
                    db,
                    row=row,
                    owner_id=owner_id,
                    expected_version=int(row.version or 1),
                )
                row.deleted_at = now
        source.last_sync_at = now
        source.sync_state = "ready"
        source.last_error = None
        source.updated_at = now
        db.flush()
        append_action_audit(
            db,
            owner_id=owner_id,
            action="contacts.refreshed",
            entity_type="contact_source",
            entity_id=source.id,
            reason="CardDAV snapshot refreshed",
            before_state=dict(snapshot["before"]),
            after_state=_source_state(source),
            details={
                "observed_count": len(observed),
                "protected_pending_count": len(protected_ids),
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def refresh_contact_source_detached(
    session_factory,
    *,
    owner_id: str,
    source_id: str,
    raise_errors: bool = False,
    audit_context: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Refresh CardDAV without retaining a DB session across network I/O."""

    owner = _owner_id(owner_id)
    source_ref = str(source_id)
    snapshot: dict[str, Any] | None = None
    try:
        snapshot = _claim_contact_refresh(
            session_factory,
            owner_id=owner,
            source_id=source_ref,
            audit_context=audit_context,
        )
        if not snapshot.get("claimed"):
            return _read_contact_source_snapshot(
                session_factory, owner_id=owner, source_id=source_ref,
            )

        # Every session used to read/claim the source is closed at this point.
        # Only detached primitives (including a short-lived decrypted config)
        # cross the DNS/HTTP boundary.
        remote_rows = carddav.fetch_contacts(dict(snapshot["config"]))
        observed: set[str] = set()
        prepared: list[tuple[dict[str, Any], str]] = []
        for contact in remote_rows:
            digest = _uid_digest(contact.get("uid"))
            if digest in observed:
                raise ContactServiceError(
                    "CardDAV returned duplicate contact identifiers"
                )
            observed.add(digest)
            prepared.append((contact, digest))
        _apply_contact_refresh(
            session_factory,
            owner_id=owner,
            snapshot=snapshot,
            prepared=prepared,
            observed=observed,
            audit_context=audit_context,
        )
    except Exception as exc:
        if snapshot is not None and snapshot.get("claimed"):
            _record_contact_refresh_failure(
                session_factory,
                owner_id=owner,
                snapshot=snapshot,
                audit_context=audit_context,
            )
        if raise_errors:
            if isinstance(exc, ContactServiceError):
                raise
            raise ContactServiceError("CardDAV refresh failed") from exc
    finally:
        if snapshot is not None:
            config = snapshot.get("config")
            if isinstance(config, dict):
                config.clear()
    return _read_contact_source_snapshot(
        session_factory, owner_id=owner, source_id=source_ref,
    )


def refresh_contact_source(
    db,
    *,
    owner_id: str,
    source_id: str,
    raise_errors: bool = False,
) -> list[dict[str, Any]]:
    """Compatibility wrapper around the detached refresh implementation.

    A caller session may be reused after this returns, but it cannot have
    uncommitted writes: its read transaction is ended before a short-lived
    session claims the refresh and all sessions are closed before CardDAV I/O.
    """

    if db.new or db.dirty or db.deleted:
        raise ContactServiceError(
            "CardDAV refresh requires committed contact state"
        )
    audit_context = dict(db.info.get(SESSION_AUDIT_CONTEXT_KEY) or {})
    bind = db.get_bind()
    db.rollback()
    detached_factory = sessionmaker(bind=bind, expire_on_commit=False)
    return refresh_contact_source_detached(
        detached_factory,
        owner_id=owner_id,
        source_id=source_id,
        raise_errors=raise_errors,
        audit_context=audit_context,
    )


def list_contacts(
    db,
    *,
    owner_id: str,
    refresh: bool = False,
    create_local: bool = False,
) -> list[dict[str, Any]]:
    owner = _owner_id(owner_id)
    # Creating the initial local source is a write-side convenience only.
    # Reads below always union every retained owner source, including disabled
    # or offline CardDAV snapshots, so configuring a connector never hides
    # contacts that were created locally beforehand.
    if create_local:
        get_default_contact_source(
            db, owner_id=owner, create_local=True,
        )
    # Ordinary reads always return the durable snapshot. Network refresh is an
    # explicit operation so compose/search/list requests never inherit a DNS
    # or CardDAV timeout. Callers asking for refresh use the detached path.
    remote = get_carddav_source(db, owner_id=owner)
    if (
        refresh
        and remote is not None
        and remote.enabled
        and str(remote.base_url or "").strip()
    ):
        refresh_contact_source(
            db, owner_id=owner, source_id=remote.id,
        )
    rows = _rows_for_owner(
        db,
        owner_id=owner,
        include_deleted_conflicts=True,
    )
    return _serialize_rows(db, rows)


def search_contacts(
    db, *, owner_id: str, query: str, refresh: bool = False, limit: int = 10
) -> list[dict[str, Any]]:
    needle = str(query or "").strip().lower()
    if not needle:
        return []
    matches: list[dict[str, Any]] = []
    for contact in list_contacts(
        db, owner_id=owner_id, refresh=refresh, create_local=False,
    ):
        if needle in str(contact.get("name") or "").lower() or any(
            needle in str(value or "").lower()
            for value in contact.get("emails") or []
        ):
            matches.append(contact)
        if len(matches) >= max(1, min(int(limit), 100)):
            break
    return matches


def find_duplicate(
    db,
    *,
    owner_id: str,
    email: str = "",
    phones: list[str] | None = None,
) -> dict[str, Any] | None:
    email_value = str(email or "").strip().lower()
    phone_values = {str(value or "").strip() for value in (phones or []) if str(value or "").strip()}
    for contact in list_contacts(db, owner_id=owner_id, create_local=False):
        if email_value and email_value in {
            str(value or "").strip().lower() for value in contact.get("emails") or []
        }:
            return contact
        if phone_values.intersection(contact.get("phones") or []):
            return contact
    return None


def create_contact(
    db,
    *,
    owner_id: str,
    name: str,
    email: str = "",
    phones: list[str] | None = None,
    address: str = "",
) -> dict[str, Any]:
    owner = _owner_id(owner_id)
    source = get_default_contact_source(db, owner_id=owner, create_local=True)
    if source is None:
        raise ContactServiceError("Contact source unavailable")
    contact = _validated_contact({
        "uid": str(uuid.uuid4()),
        "name": name,
        "emails": [email] if str(email or "").strip() else [],
        "phones": phones or [],
        "address": address,
    })
    raw_vcard = carddav.build_vcard(
        contact["name"],
        uid=contact["uid"],
        emails=contact["emails"],
        phones=contact["phones"],
        address=contact["address"],
    )
    row = _upsert_record(
        db,
        owner_id=owner,
        source=source,
        contact=contact,
        raw_vcard=raw_vcard,
    )
    if source.kind == "carddav":
        enqueue_contact_delivery(
            db,
            owner_id=owner,
            source=source,
            record=row,
            operation="create",
            raw_vcard=raw_vcard,
        )
        source.sync_state = "idle"
    append_action_audit(
        db,
        owner_id=owner,
        action="contacts.created",
        entity_type="contact_record",
        entity_id=row.id,
        reason="Contact created",
        after_state=_record_state(row),
        details={"delivery_queued": source.kind == "carddav"},
    )
    return _serialize_record(row)


def _record_by_uid(
    db,
    *,
    owner_id: str,
    uid: str,
    source_id: str | None,
    include_deleted: bool,
) -> tuple[ContactSource, ContactRecord]:
    owner = _owner_id(owner_id)
    selected_source_id = str(source_id or "").strip() or None
    if selected_source_id is not None:
        selected_source = db.query(ContactSource).filter(
            ContactSource.id == selected_source_id,
            ContactSource.owner_id == owner,
        ).first()
        if selected_source is None:
            raise ContactNotFound("Contact source not found")
    else:
        selected_source = None

    query = db.query(ContactRecord).filter(
        ContactRecord.owner_id == owner,
        ContactRecord.remote_uid_digest == _uid_digest(uid),
    )
    if selected_source_id is not None:
        query = query.filter(ContactRecord.source_id == selected_source_id)
    if not include_deleted:
        query = query.filter(ContactRecord.deleted_at.is_(None))
    rows = query.order_by(ContactRecord.id.asc()).limit(2).all()
    if not rows:
        raise ContactNotFound("Contact not found")
    if len(rows) > 1:
        raise ContactConflict(
            "Contact identifier is ambiguous; provide source_id"
        )
    row = rows[0]
    source = selected_source or db.query(ContactSource).filter(
        ContactSource.id == row.source_id,
        ContactSource.owner_id == owner,
    ).one()
    return source, row


def _active_record(
    db, *, owner_id: str, uid: str, source_id: str | None = None,
) -> tuple[ContactSource, ContactRecord]:
    return _record_by_uid(
        db,
        owner_id=owner_id,
        uid=uid,
        source_id=source_id,
        include_deleted=False,
    )


def _open_record_deliveries(
    db, *, owner_id: str, record_id: str,
) -> list[ContactDelivery]:
    return db.query(ContactDelivery).filter(
        ContactDelivery.owner_id == owner_id,
        ContactDelivery.record_id == record_id,
        ContactDelivery.state != "completed",
    ).order_by(
        ContactDelivery.created_at.asc(), ContactDelivery.id.asc(),
    ).all()


def _complete_superseded_delivery(
    delivery: ContactDelivery, *, completed_at,
) -> None:
    delivery.state = "completed"
    delivery.payload = {}
    delivery.claim_token = None
    delivery.claimed_at = None
    delivery.next_attempt_at = None
    delivery.last_error_code = None
    delivery.completed_at = completed_at
    delivery.version = int(delivery.version or 1) + 1


def _snapshot_contact_conflict(
    session_factory,
    *,
    owner_id: str,
    uid: str,
    source_id: str | None,
    expected_version: int,
    audit_context: dict[str, Any] | None,
) -> dict[str, Any]:
    db = session_factory()
    try:
        _bind_detached_contact_audit(
            db, owner_id=owner_id, audit_context=audit_context,
        )
        source, row = _record_by_uid(
            db,
            owner_id=owner_id,
            uid=uid,
            source_id=source_id,
            include_deleted=True,
        )
        if source.kind != "carddav" or not source.enabled or not source.base_url:
            raise ContactConflict("This contact is not linked to CardDAV")
        if int(row.version or 1) != int(expected_version):
            raise ContactConflict("Contact changed; reload it before resolving")
        deliveries = _open_record_deliveries(
            db, owner_id=owner_id, record_id=row.id,
        )
        if not any(item.state == "conflict" for item in deliveries):
            raise ContactConflict("This contact has no unresolved sync conflict")
        if any(item.state == "processing" for item in deliveries):
            raise ContactConflict("Contact delivery is currently in progress")
        config_version = int(source.config_version or 1)
        conflict_generations = {
            int((item.payload or {}).get("source_config_version") or 0)
            for item in deliveries
            if item.state == "conflict"
        }
        # An href observed under an older source generation may point at a
        # different address book path. Derive the target from UID after any
        # config-generation conflict instead of carrying old remote metadata.
        target = (
            carddav.absolute_url(str(source.base_url), str(row.remote_href))
            if row.remote_href and conflict_generations == {config_version}
            else carddav.vcard_url(str(source.base_url), str(row.remote_uid))
        )
        snapshot = {
            "source_id": source.id,
            "record_id": row.id,
            "record_version": int(row.version or 1),
            "remote_uid_digest": row.remote_uid_digest,
            "config_version": config_version,
            "source_version": int(source.version or 1),
            "config": _source_config(source),
            "target": target,
            "before": _record_state(row),
        }
        db.rollback()
        return snapshot
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _record_conflict_resolution_failure(
    session_factory,
    *,
    owner_id: str,
    snapshot: dict[str, Any],
    resolution: str,
    audit_context: dict[str, Any] | None,
) -> None:
    db = session_factory()
    try:
        _bind_detached_contact_audit(
            db, owner_id=owner_id, audit_context=audit_context,
        )
        now = utcnow_naive()
        source_reserved = db.query(ContactSource).filter(
            ContactSource.id == snapshot["source_id"],
            ContactSource.owner_id == owner_id,
            ContactSource.config_version == snapshot["config_version"],
            ContactSource.version == snapshot["source_version"],
        ).update(
            {
                ContactSource.sync_state: "error",
                ContactSource.last_error: "CardDAV conflict resolution failed",
                ContactSource.version: snapshot["source_version"] + 1,
                ContactSource.updated_at: now,
            },
            synchronize_session=False,
        )
        row = db.query(ContactRecord).filter(
            ContactRecord.id == snapshot["record_id"],
            ContactRecord.owner_id == owner_id,
            ContactRecord.version == snapshot["record_version"],
        ).first()
        if source_reserved != 1 or row is None:
            db.rollback()
            return
        source = db.query(ContactSource).filter(
            ContactSource.id == snapshot["source_id"],
            ContactSource.owner_id == owner_id,
        ).one()
        append_action_audit(
            db,
            owner_id=owner_id,
            action="contacts.conflict_resolution_failed",
            entity_type="contact_record",
            entity_id=row.id,
            reason="CardDAV conflict resolution failed",
            before_state=dict(snapshot["before"]),
            after_state=_record_state(row),
            details={
                "resolution": resolution,
                "error_code": "remote_read_failed",
            },
            outcome="failure",
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def resolve_contact_conflict_detached(
    session_factory,
    *,
    owner_id: str,
    uid: str,
    expected_version: int,
    resolution: str,
    source_id: str | None = None,
    audit_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve one conflict by an explicit owner choice.

    ``keep_local`` rebases the current database authority onto the latest
    remote ETag. ``use_remote`` adopts the exact current remote resource (or
    its deletion). Both choices supersede every blocked delivery for the
    record and are fenced by source generation plus record version.
    """

    owner = _owner_id(owner_id)
    choice = str(resolution or "").strip().lower()
    if choice not in {"keep_local", "use_remote"}:
        raise ContactServiceError(
            "Conflict resolution must be keep_local or use_remote"
        )
    if int(expected_version) < 1:
        raise ContactConflict("Contact version is invalid")
    snapshot = _snapshot_contact_conflict(
        session_factory,
        owner_id=owner,
        uid=uid,
        source_id=source_id,
        expected_version=int(expected_version),
        audit_context=audit_context,
    )
    try:
        # The snapshot session is closed before the exact remote resource and
        # its ETag are read. No SQL connection is held across DNS/HTTP.
        raw_vcard, remote_etag, remote_exists = carddav.get_contact_resource(
            dict(snapshot["config"]), target=str(snapshot["target"]),
        )
        remote_contact: dict[str, Any] | None = None
        if choice == "use_remote" and remote_exists:
            parsed = carddav.parse_vcards(raw_vcard)
            if len(parsed) != 1:
                raise ContactServiceError(
                    "CardDAV returned an invalid contact resource"
                )
            remote_contact = parsed[0]
            if _uid_digest(remote_contact.get("uid")) != snapshot[
                "remote_uid_digest"
            ]:
                raise ContactConflict(
                    "CardDAV returned a different contact identifier"
                )
    except Exception as exc:
        _record_conflict_resolution_failure(
            session_factory,
            owner_id=owner,
            snapshot=snapshot,
            resolution=choice,
            audit_context=audit_context,
        )
        config = snapshot.get("config")
        if isinstance(config, dict):
            config.clear()
        if isinstance(exc, ContactServiceError):
            raise
        raise ContactServiceError("CardDAV conflict resolution failed") from exc
    finally:
        config = snapshot.get("config")
        if isinstance(config, dict):
            config.clear()

    db = session_factory()
    try:
        _bind_detached_contact_audit(
            db, owner_id=owner, audit_context=audit_context,
        )
        now = utcnow_naive()
        source_reserved = db.query(ContactSource).filter(
            ContactSource.id == snapshot["source_id"],
            ContactSource.owner_id == owner,
            ContactSource.config_version == snapshot["config_version"],
            ContactSource.version == snapshot["source_version"],
            ContactSource.enabled.is_(True),
        ).update(
            {
                ContactSource.version: snapshot["source_version"] + 1,
                ContactSource.updated_at: now,
            },
            synchronize_session=False,
        )
        if source_reserved != 1:
            raise ContactConflict(
                "Contact source changed during conflict resolution"
            )
        source = db.query(ContactSource).filter(
            ContactSource.id == snapshot["source_id"],
            ContactSource.owner_id == owner,
        ).one()
        row = db.query(ContactRecord).filter(
            ContactRecord.id == snapshot["record_id"],
            ContactRecord.owner_id == owner,
            ContactRecord.version == snapshot["record_version"],
        ).first()
        if row is None:
            raise ContactConflict("Contact changed during conflict resolution")
        deliveries = _open_record_deliveries(
            db, owner_id=owner, record_id=row.id,
        )
        if not any(item.state == "conflict" for item in deliveries):
            raise ContactConflict("This contact conflict was already resolved")
        if any(item.state == "processing" for item in deliveries):
            raise ContactConflict("Contact delivery is currently in progress")

        _reserve_record_version(
            db,
            row=row,
            owner_id=owner,
            expected_version=int(snapshot["record_version"]),
        )
        for delivery in deliveries:
            _complete_superseded_delivery(delivery, completed_at=now)

        queued = False
        if choice == "use_remote":
            if remote_exists and remote_contact is not None:
                normalized = carddav.normalize_contact(remote_contact)
                row.payload = {
                    "name": normalized["name"],
                    "emails": normalized["emails"],
                    "phones": normalized["phones"],
                    "address": normalized["address"],
                }
                row.raw_vcard = raw_vcard
                row.remote_href = str(snapshot["target"])
                row.remote_etag = str(remote_etag or "") or None
                row.deleted_at = None
            else:
                row.remote_href = None
                row.remote_etag = None
                row.deleted_at = now
        else:
            row.remote_href = str(snapshot["target"]) if remote_exists else None
            row.remote_etag = str(remote_etag or "") or None
            payload = dict(row.payload or {})
            desired_vcard = str(row.raw_vcard or "")
            if not desired_vcard and row.deleted_at is None:
                desired_vcard = carddav.build_vcard(
                    str(payload.get("name") or ""),
                    uid=str(row.remote_uid),
                    emails=list(payload.get("emails") or []),
                    phones=list(payload.get("phones") or []),
                    address=str(payload.get("address") or ""),
                )
                row.raw_vcard = desired_vcard
            operation: str | None
            if row.deleted_at is not None:
                operation = "delete" if remote_exists else None
            else:
                operation = "update" if remote_exists else "create"
            if operation is not None:
                enqueue_contact_delivery(
                    db,
                    owner_id=owner,
                    source=source,
                    record=row,
                    operation=operation,
                    raw_vcard=desired_vcard,
                )
                queued = True

        source.sync_state = "idle" if queued else "ready"
        source.last_error = None
        source.updated_at = now
        db.flush()
        append_action_audit(
            db,
            owner_id=owner,
            action="contacts.conflict_resolved",
            entity_type="contact_record",
            entity_id=row.id,
            reason="Owner resolved a CardDAV contact conflict",
            before_state=dict(snapshot["before"]),
            after_state=_record_state(row),
            details={
                "resolution": choice,
                "remote_exists": bool(remote_exists),
                "superseded_delivery_count": len(deliveries),
                "delivery_queued": queued,
            },
        )
        result = _serialize_record(row)
        result["sync_state"] = "pending" if queued else "synced"
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def update_contact(
    db,
    *,
    owner_id: str,
    uid: str,
    name: str,
    emails: list[str],
    phones: list[str],
    address: str = "",
    expected_version: int,
    source_id: str | None = None,
) -> dict[str, Any]:
    source, row = _active_record(
        db, owner_id=owner_id, uid=uid, source_id=source_id,
    )
    if source.kind == "carddav" and (
        not source.enabled or not str(source.base_url or "").strip()
    ):
        raise ContactConflict(
            "CardDAV source is disabled; re-enable it before editing"
        )
    before = _record_state(row)
    _reserve_record_version(
        db,
        row=row,
        owner_id=_owner_id(owner_id),
        expected_version=expected_version,
    )
    existing = _serialize_record(row)
    contact = _validated_contact({
        "uid": row.remote_uid,
        "name": name,
        "emails": emails,
        "phones": phones,
        "address": address if str(address or "").strip() else existing.get("address", ""),
    })
    raw_vcard = carddav.build_vcard(
        contact["name"],
        uid=contact["uid"],
        emails=contact["emails"],
        phones=contact["phones"],
        address=contact["address"],
    )
    row.payload = {
        "name": contact["name"],
        "emails": contact["emails"],
        "phones": contact["phones"],
        "address": contact["address"],
    }
    row.raw_vcard = raw_vcard
    if source.kind == "carddav":
        enqueue_contact_delivery(
            db,
            owner_id=_owner_id(owner_id),
            source=source,
            record=row,
            operation="update",
            raw_vcard=raw_vcard,
        )
        source.sync_state = "idle"
    db.flush()
    append_action_audit(
        db,
        owner_id=_owner_id(owner_id),
        action="contacts.updated",
        entity_type="contact_record",
        entity_id=row.id,
        reason="Contact updated",
        before_state=before,
        after_state=_record_state(row),
        details={"delivery_queued": source.kind == "carddav"},
    )
    return _serialize_record(row)


def delete_contact(
    db,
    *,
    owner_id: str,
    uid: str,
    expected_version: int,
    source_id: str | None = None,
) -> bool:
    source, row = _active_record(
        db, owner_id=owner_id, uid=uid, source_id=source_id,
    )
    if source.kind == "carddav" and (
        not source.enabled or not str(source.base_url or "").strip()
    ):
        raise ContactConflict(
            "CardDAV source is disabled; re-enable it before deleting"
        )
    before = _record_state(row)
    _reserve_record_version(
        db,
        row=row,
        owner_id=_owner_id(owner_id),
        expected_version=expected_version,
    )
    row.deleted_at = utcnow_naive()
    if source.kind == "carddav":
        enqueue_contact_delivery(
            db,
            owner_id=_owner_id(owner_id),
            source=source,
            record=row,
            operation="delete",
        )
        source.sync_state = "idle"
    db.flush()
    append_action_audit(
        db,
        owner_id=_owner_id(owner_id),
        action="contacts.deleted",
        entity_type="contact_record",
        entity_id=row.id,
        reason="Contact deleted",
        before_state=before,
        after_state=_record_state(row),
        details={"delivery_queued": source.kind == "carddav"},
        reversible=source.kind == "local",
        undo_ref=row.id if source.kind == "local" else None,
    )
    return True


def clear_local_contacts(db, *, owner_id: str) -> int:
    source = get_local_source(db, owner_id=owner_id, create=True)
    rows = _record_query(
        db, owner_id=owner_id, source_id=source.id,
    ).filter(ContactRecord.deleted_at.is_(None)).all()
    now = utcnow_naive()
    for row in rows:
        _reserve_record_version(
            db,
            row=row,
            owner_id=_owner_id(owner_id),
            expected_version=int(row.version or 1),
        )
        row.deleted_at = now
    db.flush()
    count = len(rows)
    append_action_audit(
        db,
        owner_id=_owner_id(owner_id),
        action="contacts.local_cleared",
        entity_type="contact_source",
        entity_id=source.id,
        reason="Local contacts were cleared",
        before_state={"active_count": count},
        after_state={"active_count": 0},
        details={"deleted_count": count},
        reversible=True,
        undo_ref=source.id,
    )
    return count


def import_vcards(db, *, owner_id: str, text: str) -> dict[str, int]:
    if len(str(text or "")) > MAX_CONTACT_IMPORT_CHARS:
        raise ContactServiceError("Contact import exceeds the size limit")
    source = get_default_contact_source(db, owner_id=owner_id, create_local=True)
    cards = carddav.prepare_import_cards(text)
    if len(cards) > MAX_CONTACT_IMPORT_ROWS:
        raise ContactServiceError("Contact import contains too many rows")
    digests = [_uid_digest(contact.get("uid")) for contact, _raw in cards]
    if len(digests) != len(set(digests)):
        raise ContactServiceError("Import contains duplicate contact identifiers")
    existing_emails = {
        value.lower()
        for contact in list_contacts(db, owner_id=owner_id, create_local=True)
        for value in contact.get("emails") or []
    }
    imported = 0
    for contact, raw_vcard in cards:
        emails = [str(value).lower() for value in contact.get("emails") or []]
        if emails and any(value in existing_emails for value in emails):
            continue
        row = _upsert_record(
            db,
            owner_id=owner_id,
            source=source,
            contact=contact,
            raw_vcard=raw_vcard,
        )
        if source.kind == "carddav":
            enqueue_contact_delivery(
                db,
                owner_id=_owner_id(owner_id),
                source=source,
                record=row,
                operation="create",
                raw_vcard=raw_vcard,
            )
        existing_emails.update(emails)
        imported += 1
    append_action_audit(
        db,
        owner_id=_owner_id(owner_id),
        action="contacts.imported",
        entity_type="contact_source",
        entity_id=source.id,
        reason="vCard contacts imported",
        after_state={"imported_count": imported},
        details={
            "format": "vcard",
            "delivery_queued": source.kind == "carddav" and imported > 0,
        },
    )
    return {"imported": imported, "failed": 0, "total": len(cards)}


def _csv_rows(text: str) -> list[tuple[str, str, str]]:
    raw = str(text or "").strip()
    if not raw:
        return []
    try:
        dialect = csv.Sniffer().sniff(raw[:2048])
    except Exception:
        dialect = csv.excel
    try:
        has_header = csv.Sniffer().has_header(raw[:2048])
    except Exception:
        has_header = True
    stream = io.StringIO(raw)
    result: list[tuple[str, str, str]] = []
    if has_header:
        for row in csv.DictReader(stream, dialect=dialect):
            lowered = {
                str(key or "").strip().lower(): str(value or "").strip()
                for key, value in row.items()
            }
            result.append((
                lowered.get("name") or lowered.get("full name")
                or lowered.get("full_name") or lowered.get("display name")
                or lowered.get("display_name") or lowered.get("fn") or "",
                lowered.get("email") or lowered.get("email address")
                or lowered.get("email_address") or lowered.get("e-mail")
                or lowered.get("mail") or "",
                lowered.get("phone") or lowered.get("telephone")
                or lowered.get("tel") or "",
            ))
    else:
        for row in csv.reader(stream, dialect=dialect):
            columns = [str(value or "").strip() for value in row]
            if any(columns):
                result.append((
                    columns[0] if columns else "",
                    columns[1] if len(columns) > 1 else "",
                    columns[2] if len(columns) > 2 else "",
                ))
    if len(result) > MAX_CONTACT_IMPORT_ROWS:
        raise ContactServiceError("Contact import contains too many rows")
    return result


def import_csv_contacts(db, *, owner_id: str, text: str) -> dict[str, Any]:
    if len(str(text or "")) > MAX_CONTACT_IMPORT_CHARS:
        raise ContactServiceError("Contact import exceeds the size limit")
    rows = _csv_rows(text)
    if not rows:
        return {"imported": 0, "failed": 0, "total": 0, "error": "No CSV data found"}
    imported = 0
    total = 0
    for name, email, phone in rows:
        email = email.strip()
        if not email:
            continue
        total += 1
        if find_duplicate(
            db, owner_id=owner_id, email=email, phones=[phone] if phone else [],
        ):
            continue
        create_contact(
            db,
            owner_id=owner_id,
            name=name.strip() or email.split("@", 1)[0],
            email=email,
            phones=[phone] if phone else [],
        )
        imported += 1
    return {"imported": imported, "failed": 0, "total": total}


def contacts_to_vcf(contacts: list[dict[str, Any]]) -> str:
    return "".join(
        carddav.build_vcard(
            contact.get("name") or (
                str((contact.get("emails") or [""])[0]).split("@", 1)[0]
                if contact.get("emails") else "Contact"
            ),
            uid=contact.get("uid") or str(uuid.uuid4()),
            emails=list(contact.get("emails") or []),
            phones=list(contact.get("phones") or []),
            address=str(contact.get("address") or ""),
        )
        for contact in contacts
    )


def contacts_to_csv(contacts: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["name", "email", "phone"])
    for contact in contacts:
        emails = list(contact.get("emails") or [""])
        phones = list(contact.get("phones") or [""])
        for index in range(max(len(emails), len(phones), 1)):
            writer.writerow([
                contact.get("name") or "",
                emails[index] if index < len(emails) else "",
                phones[index] if index < len(phones) else "",
            ])
    return output.getvalue()


def import_legacy_snapshot(
    db,
    *,
    owner_id: str,
    contacts: list[dict[str, Any]],
    carddav_config: dict[str, str] | None,
) -> tuple[int, bool]:
    """Import an already-validated legacy snapshot in the caller transaction."""

    configured = bool(carddav_config and str(carddav_config.get("url") or "").strip())
    if configured:
        target = upsert_carddav_config(
            db,
            owner_id=owner_id,
            url=carddav_config.get("url", ""),
            username=carddav_config.get("username", ""),
            password=carddav_config.get("password", ""),
        )
    else:
        target = get_local_source(db, owner_id=owner_id, create=True)
    imported = 0
    for contact in contacts:
        _upsert_record(
            db,
            owner_id=owner_id,
            source=target,
            contact=contact,
            raw_vcard=carddav.build_vcard(
                str(contact.get("name") or ""),
                uid=str(contact.get("uid") or uuid.uuid4()),
                emails=list(contact.get("emails") or []),
                phones=list(contact.get("phones") or []),
                address=str(contact.get("address") or ""),
            ),
        )
        imported += 1
    append_action_audit(
        db,
        owner_id=_owner_id(owner_id),
        action="contacts.legacy_imported",
        entity_type="contact_source",
        entity_id=target.id,
        reason="Legacy contacts adopted into database authority",
        after_state={
            "imported_count": imported,
            "source_kind": target.kind,
        },
        details={"carddav_configured": configured},
    )
    return imported, configured


__all__ = [
    "ContactConflict",
    "ContactNotFound",
    "ContactServiceError",
    "clear_local_contacts",
    "contacts_to_csv",
    "contacts_to_vcf",
    "create_contact",
    "delete_contact",
    "find_duplicate",
    "get_contact_config",
    "get_default_contact_source",
    "get_local_source",
    "import_csv_contacts",
    "import_legacy_snapshot",
    "import_vcards",
    "list_contacts",
    "refresh_contact_source",
    "refresh_contact_source_detached",
    "resolve_contact_conflict_detached",
    "search_contacts",
    "serialize_source_config",
    "update_contact",
    "upsert_carddav_config",
]
