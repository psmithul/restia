"""Canonical SQL authority for email-to-Life projections.

The IMAP message index is a rebuildable local cache.  Projection delivery is
not: it is an encrypted, ``Account.id``-owned outbox in the configured
SQLAlchemy database.  Cache and authority use separate transactions, so the
request path commits the cache first, enqueues the same immutable headers in
SQL, and every bounded drain rechecks the legacy cache.  That idempotent
backfill closes either crash window without claiming cross-database atomicity.

Claims commit before Life ingestion starts.  A digest-only lease token and
optimistic version fence every completion/failure update; no SQL transaction
is held while the Life graph commits.  If ingestion commits and completion
recording fails, lease expiry deliberately retries the idempotent projection.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy import and_, null, or_
from sqlalchemy.exc import IntegrityError

from core.database import (
    Account,
    EmailLifeProjection,
    EmailLifeProjectionImportRun,
    SessionLocal,
    utcnow_naive,
)
from src.identity import ensure_account, find_account
from src.secret_storage import decrypt, encrypt_plaintext, private_digest


LEDGER_TABLE = "email_life_projection_ledger"
DEFAULT_LEASE_SECONDS = 5 * 60
DEFAULT_RETRY_DELAY_SECONDS = 30
MAX_RETRY_DELAY_SECONDS = 6 * 60 * 60
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_IMPORT_BATCH = 500
_HEX_64_RE = re.compile(r"[0-9a-f]{64}")


class EmailLifeProjectionLedgerError(RuntimeError):
    """The canonical projection handoff could not proceed safely."""


@dataclass(frozen=True)
class EmailLifeProjectionDrainResult:
    backfilled: int = 0
    attempted: int = 0
    completed: int = 0
    failed: int = 0


@dataclass(frozen=True)
class _Claim:
    row_id: str
    owner_id: str
    owner_username: str
    account_key: str
    folder: str
    message_uid: str
    header_sha256: str
    claim_token_digest: str
    version: int
    attempt_count: int
    payload: dict[str, Any]


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _message_id_tokens(value: object) -> list[str]:
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            for token in _message_id_tokens(item):
                if token not in result:
                    result.append(token)
        return result
    text = str(value or "").strip()
    if not text:
        return []
    matched = re.findall(r"<[^<>\s]+>", text)
    return list(dict.fromkeys(matched)) if matched else [text]


def _bounded_identity(value: object, *, field: str, default: str = "") -> str:
    normalized = str(value or default).strip() or default
    if not normalized:
        raise EmailLifeProjectionLedgerError(
            f"Email projection {field} is required"
        )
    if len(normalized) > 255:
        raise EmailLifeProjectionLedgerError(
            f"Email projection {field} exceeds the safety limit"
        )
    return normalized


def _immutable_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    uid = _bounded_identity(row.get("uid"), field="message UID")
    try:
        date_epoch = float(row.get("date_epoch") or 0)
    except (TypeError, ValueError):
        date_epoch = 0.0
    payload = {
        "uid": uid,
        "message_id": str(row.get("message_id") or "").strip(),
        "references": _message_id_tokens(row.get("references")),
        "in_reply_to": (
            _message_id_tokens(row.get("in_reply_to")) or [""]
        )[0],
        "subject": str(row.get("subject") or "(no subject)"),
        "from_name": str(row.get("from_name") or ""),
        "from_address": str(row.get("from_address") or ""),
        "date": str(row.get("date") or ""),
        "date_epoch": date_epoch,
    }
    encoded = _canonical_json(payload).encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise EmailLifeProjectionLedgerError(
            "Email projection header payload exceeds the safety limit"
        )
    return payload


def _payload_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def encode_email_life_projection_recovery_payload(
    row: Mapping[str, Any],
) -> str:
    """Encrypt one immutable cache recovery envelope.

    This envelope contains no delivery state and is never claimed directly;
    it only lets the bounded canonical importer preserve RFC thread evidence
    if the process stops after the cache transaction but before SQL enqueue.
    """

    return encrypt_plaintext(_canonical_json(_immutable_payload(row)))


def _resolve_account(db, owner: object, *, create: bool) -> Account:
    raw = str(owner or "").strip()
    if not raw:
        raise EmailLifeProjectionLedgerError(
            "Email projection requires a concrete owner"
        )
    account = db.query(Account).filter(
        Account.id == raw,
        Account.status == "active",
    ).one_or_none()
    if account is None:
        account = ensure_account(db, raw) if create else find_account(db, raw)
    if account is None or account.status != "active":
        raise EmailLifeProjectionLedgerError(
            "Email projection owner does not resolve to an active account"
        )
    return account


def _begin_sqlite_write(db) -> None:
    if db.get_bind().dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")


def _identity_query(
    db,
    *,
    owner_id: str,
    account_key: str,
    folder: str,
    message_uid: str,
    header_sha256: str,
):
    return db.query(EmailLifeProjection).filter(
        EmailLifeProjection.owner_id == owner_id,
        EmailLifeProjection.account_key == account_key,
        EmailLifeProjection.folder == folder,
        EmailLifeProjection.message_uid == message_uid,
        EmailLifeProjection.header_sha256 == header_sha256,
    )


def _enqueue_one(
    db,
    *,
    owner_id: str,
    account_key: str,
    folder: str,
    payload: Mapping[str, Any],
    header_sha256: str,
) -> bool:
    message_uid = _bounded_identity(
        payload.get("uid"),
        field="message UID",
    )
    existing = _identity_query(
        db,
        owner_id=owner_id,
        account_key=account_key,
        folder=folder,
        message_uid=message_uid,
        header_sha256=header_sha256,
    ).one_or_none()
    if existing is not None:
        return False
    row = EmailLifeProjection(
        id=str(uuid.uuid4()),
        owner_id=owner_id,
        account_key=account_key,
        folder=folder,
        message_uid=message_uid,
        header_sha256=header_sha256,
        payload=dict(payload),
        state="pending",
        completed_at=None,
        attempt_count=0,
        version=1,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        # A concurrent producer won the deterministic identity.  The unique
        # key makes that an idempotent success, not a second projection.
        return False
    return True


def _enqueue_payload(
    db,
    *,
    owner_id: str,
    account_key: str,
    folder: str,
    payload: Mapping[str, Any],
) -> bool:
    normalized = _immutable_payload(payload)
    return _enqueue_one(
        db,
        owner_id=owner_id,
        account_key=account_key,
        folder=folder,
        payload=normalized,
        header_sha256=_payload_hash(normalized),
    )


def enqueue_email_life_headers(
    *,
    owner: str,
    account_key: str,
    folder: str,
    emails: Iterable[Mapping[str, Any]],
    session_factory: Callable[[], Any] | None = None,
) -> int:
    """Commit immutable encrypted headers to the configured SQL authority."""

    normalized_account = _bounded_identity(
        account_key, field="mail account", default="default"
    )
    normalized_folder = _bounded_identity(
        folder, field="folder", default="INBOX"
    )
    materialized = list(islice(iter(emails), MAX_IMPORT_BATCH + 1))
    if len(materialized) > MAX_IMPORT_BATCH:
        raise EmailLifeProjectionLedgerError(
            "Email projection enqueue batch exceeds the safety limit"
        )
    factory = session_factory or SessionLocal
    db = factory()
    try:
        _begin_sqlite_write(db)
        account = _resolve_account(db, owner, create=True)
        inserted = 0
        for row in materialized:
            if not isinstance(row, Mapping):
                raise EmailLifeProjectionLedgerError(
                    "Email projection requires header objects"
                )
            inserted += int(_enqueue_payload(
                db,
                owner_id=account.id,
                account_key=normalized_account,
                folder=normalized_folder,
                payload=row,
            ))
        db.commit()
        return inserted
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def ensure_email_life_projection_schema(conn: sqlite3.Connection) -> None:
    """Create only the retired sidecar shape for import fixtures/tools.

    Production enqueue/drain code never writes this table.  Keeping the helper
    explicit lets migration tests build a faithful legacy source without
    making the sidecar an authority again.
    """

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
            owner TEXT NOT NULL,
            account_key TEXT NOT NULL,
            folder TEXT NOT NULL,
            uid TEXT NOT NULL,
            header_sha256 TEXT NOT NULL,
            payload_ciphertext TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL DEFAULT 'pending',
            claim_token TEXT,
            claimed_at TEXT,
            next_attempt_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error_code TEXT,
            completed_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (owner, account_key, folder, uid, header_sha256)
        )
        """
    )


def _open_legacy_readonly(path: str | Path) -> sqlite3.Connection | None:
    candidate = Path(path)
    if not candidate.is_file():
        return None
    uri = candidate.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _legacy_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if exists is None:
        return set()
    return {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
    }


def _legacy_source_digest(
    path: Path,
    conn: sqlite3.Connection,
    *,
    table: str,
    owner_id: str,
    account_key: str,
    folder: str | None,
) -> str:
    stat = path.stat()
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    evidence = {
        "path_sha256": hashlib.sha256(
            str(path.resolve()).encode("utf-8")
        ).hexdigest(),
        "device": int(stat.st_dev),
        "inode": int(stat.st_ino),
        "schema_sha256": hashlib.sha256(
            str((schema or [""])[0] or "").encode("utf-8")
        ).hexdigest(),
        "table": table,
        "owner_id": owner_id,
        "account_key": account_key,
        "folder": str(folder or "*"),
    }
    return hashlib.sha256(_canonical_json(evidence).encode("utf-8")).hexdigest()


def _import_marker(
    db,
    *,
    owner_id: str,
    source_kind: str,
    source_sha256: str,
) -> EmailLifeProjectionImportRun:
    marker = db.query(EmailLifeProjectionImportRun).filter(
        EmailLifeProjectionImportRun.owner_id == owner_id,
        EmailLifeProjectionImportRun.source_kind == source_kind,
        EmailLifeProjectionImportRun.source_sha256 == source_sha256,
    ).with_for_update().one_or_none()
    if marker is None:
        marker = EmailLifeProjectionImportRun(
            id=str(uuid.uuid4()),
            owner_id=owner_id,
            source_kind=source_kind,
            source_sha256=source_sha256,
            state="pending",
            details={
                "cursor_updated_at": "",
                "cursor_rowid": 0,
                "imported": 0,
            },
        )
        db.add(marker)
        db.flush()
    return marker


def _legacy_owner_clause(aliases: list[str]) -> tuple[str, list[str]]:
    concrete = list(dict.fromkeys(str(value) for value in aliases if str(value)))
    if not concrete:
        raise EmailLifeProjectionLedgerError(
            "Legacy projection import requires an exact owner"
        )
    return "owner IN (" + ",".join("?" for _ in concrete) + ")", concrete


def _select_legacy_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    cursor_updated_at: str,
    cursor_rowid: int,
    aliases: list[str],
    account_key: str,
    folder: str | None,
    limit: int,
    has_recovery_payload: bool = False,
) -> list[sqlite3.Row]:
    owner_clause, owner_params = _legacy_owner_clause(aliases)
    folder_clause = "" if folder is None else " AND folder=?"
    params: list[Any] = [
        cursor_updated_at, cursor_updated_at, cursor_rowid,
        *owner_params, account_key,
    ]
    if folder is not None:
        params.append(folder)
    params.append(limit)
    if table == LEDGER_TABLE:
        projection = (
            "rowid AS legacy_rowid, owner, account_key, folder, uid, "
            "header_sha256, payload_ciphertext, state, updated_at"
        )
    else:
        recovery_column = (
            ", projection_payload_ciphertext" if has_recovery_payload else ""
        )
        projection = (
            "rowid AS legacy_rowid, owner, account_key, folder, uid, "
            "message_id, subject, from_name, from_address, date_iso, date_epoch"
            f", updated_at{recovery_column}"
        )
    return conn.execute(
        f"""
        SELECT {projection}
        FROM {table}
        WHERE (updated_at>? OR (updated_at=? AND rowid>?))
          AND {owner_clause} AND account_key=? {folder_clause}
        ORDER BY updated_at ASC, rowid ASC
        LIMIT ?
        """,
        params,
    ).fetchall()


def _legacy_payload(
    row: sqlite3.Row,
    *,
    table: str,
) -> tuple[dict[str, Any] | None, str, bool]:
    if table == LEDGER_TABLE:
        digest = str(row["header_sha256"] or "").lower()
        if _HEX_64_RE.fullmatch(digest) is None:
            raise EmailLifeProjectionLedgerError(
                "Legacy projection row has an invalid header digest"
            )
        ciphertext = str(row["payload_ciphertext"] or "")
        completed = str(row["state"] or "") == "completed"
        if not ciphertext:
            if not completed:
                raise EmailLifeProjectionLedgerError(
                    "Legacy projection row has no retry payload"
                )
            return None, digest, True
        plaintext = decrypt(ciphertext)
        try:
            decoded = json.loads(plaintext) if plaintext else None
        except (TypeError, ValueError) as exc:
            raise EmailLifeProjectionLedgerError(
                "Legacy projection payload is invalid"
            ) from exc
        if not isinstance(decoded, dict):
            raise EmailLifeProjectionLedgerError(
                "Legacy projection payload is invalid"
            )
        payload = _immutable_payload(decoded)
        if _payload_hash(payload) != digest:
            raise EmailLifeProjectionLedgerError(
                "Legacy projection payload integrity check failed"
            )
        return payload, digest, completed
    recovery_ciphertext = (
        str(row["projection_payload_ciphertext"] or "")
        if "projection_payload_ciphertext" in row.keys() else ""
    )
    if recovery_ciphertext:
        plaintext = decrypt(recovery_ciphertext)
        try:
            decoded = json.loads(plaintext) if plaintext else None
        except (TypeError, ValueError) as exc:
            raise EmailLifeProjectionLedgerError(
                "Email index projection recovery payload is invalid"
            ) from exc
        if not isinstance(decoded, dict):
            raise EmailLifeProjectionLedgerError(
                "Email index projection recovery payload is invalid"
            )
        recovered = _immutable_payload(decoded)
        if str(recovered["uid"]) != str(row["uid"] or ""):
            raise EmailLifeProjectionLedgerError(
                "Email index projection recovery identity is inconsistent"
            )
        return recovered, _payload_hash(recovered), False
    payload = _immutable_payload({
        "uid": str(row["uid"] or ""),
        "message_id": str(row["message_id"] or ""),
        "references": [],
        "in_reply_to": "",
        "subject": str(row["subject"] or "(no subject)"),
        "from_name": str(row["from_name"] or ""),
        "from_address": str(row["from_address"] or ""),
        "date": str(row["date_iso"] or ""),
        "date_epoch": float(row["date_epoch"] or 0),
    })
    return payload, _payload_hash(payload), False


def import_legacy_email_projection_sidecar(
    path: str | Path,
    *,
    owner: str,
    account_key: str,
    folder: str | None = None,
    limit: int = 32,
    session_factory: Callable[[], Any] | None = None,
) -> int:
    """Boundedly import legacy ledger/cache rows without modifying the source."""

    bounded = max(0, min(int(limit), MAX_IMPORT_BATCH))
    if bounded == 0:
        return 0
    legacy = _open_legacy_readonly(path)
    if legacy is None:
        return 0
    factory = session_factory or SessionLocal
    db = factory()
    try:
        _begin_sqlite_write(db)
        account = _resolve_account(db, owner, create=True)
        normalized_account = _bounded_identity(
            account_key, field="mail account", default="default"
        )
        normalized_folder = (
            None if folder is None else
            _bounded_identity(folder, field="folder", default="INBOX")
        )
        aliases = [str(owner or "").strip(), account.id, account.username]
        inserted = 0
        remaining = bounded
        required = {
            LEDGER_TABLE: {
                "owner", "account_key", "folder", "uid", "header_sha256",
                "payload_ciphertext", "state", "updated_at",
            },
            "email_message_index": {
                "owner", "account_key", "folder", "uid", "message_id",
                "subject", "from_name", "from_address", "date_iso",
                "date_epoch", "updated_at",
            },
        }
        for table, expected_columns in required.items():
            if remaining <= 0:
                break
            columns = _legacy_table_columns(legacy, table)
            if not columns:
                continue
            missing = sorted(expected_columns - columns)
            if missing:
                raise EmailLifeProjectionLedgerError(
                    f"Legacy {table} schema is incomplete"
                )
            source_kind = (
                "legacy_projection_ledger"
                if table == LEDGER_TABLE else "legacy_email_message_index"
            )
            source_sha256 = _legacy_source_digest(
                Path(path), legacy, table=table, owner_id=account.id,
                account_key=normalized_account, folder=normalized_folder,
            )
            marker = _import_marker(
                db,
                owner_id=account.id,
                source_kind=source_kind,
                source_sha256=source_sha256,
            )
            details = dict(marker.details or {})
            cursor_updated_at = str(details.get("cursor_updated_at") or "")
            cursor_rowid = max(0, int(details.get("cursor_rowid") or 0))
            rows = _select_legacy_rows(
                legacy,
                table=table,
                cursor_updated_at=cursor_updated_at,
                cursor_rowid=cursor_rowid,
                aliases=aliases,
                account_key=normalized_account,
                folder=normalized_folder,
                limit=remaining,
                has_recovery_payload=(
                    "projection_payload_ciphertext" in columns
                ),
            )
            imported_for_source = 0
            for row in rows:
                payload, digest, completed = _legacy_payload(row, table=table)
                message_uid = str(row["uid"] or "").strip()
                if not message_uid:
                    raise EmailLifeProjectionLedgerError(
                        "Legacy projection row has no message UID"
                    )
                existing = _identity_query(
                    db,
                    owner_id=account.id,
                    account_key=normalized_account,
                    folder=str(row["folder"] or "INBOX"),
                    message_uid=message_uid,
                    header_sha256=digest,
                ).one_or_none()
                if existing is None:
                    new_row = EmailLifeProjection(
                        id=str(uuid.uuid4()),
                        owner_id=account.id,
                        account_key=normalized_account,
                        folder=_bounded_identity(
                            row["folder"], field="folder", default="INBOX"
                        ),
                        message_uid=_bounded_identity(
                            message_uid, field="message UID"
                        ),
                        header_sha256=digest,
                        payload=null() if completed else dict(payload or {}),
                        state="completed" if completed else "pending",
                        completed_at=utcnow_naive() if completed else None,
                        attempt_count=0,
                        version=1,
                    )
                    db.add(new_row)
                    db.flush()
                    inserted += 1
                imported_for_source += 1
                cursor_updated_at = str(row["updated_at"] or "")
                cursor_rowid = int(row["legacy_rowid"])
            total_imported = int(details.get("imported") or 0) + imported_for_source
            marker.details = {
                "cursor_updated_at": cursor_updated_at,
                "cursor_rowid": cursor_rowid,
                "imported": total_imported,
                "table": table,
            }
            exhausted = len(rows) < remaining
            marker.state = "completed" if exhausted else "pending"
            marker.completed_at = utcnow_naive() if exhausted else None
            remaining -= len(rows)
        db.commit()
        return inserted
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
        legacy.close()


def _eligible(now: datetime):
    return or_(
        EmailLifeProjection.state == "pending",
        and_(
            EmailLifeProjection.state == "failed",
            or_(
                EmailLifeProjection.next_attempt_at.is_(None),
                EmailLifeProjection.next_attempt_at <= now,
            ),
        ),
        and_(
            EmailLifeProjection.state == "processing",
            or_(
                EmailLifeProjection.lease_expires_at.is_(None),
                EmailLifeProjection.lease_expires_at <= now,
            ),
        ),
    )


def _claim_next(
    *,
    owner_id: str,
    account_key: str | None,
    folder: str | None,
    lease_seconds: int,
    session_factory: Callable[[], Any],
) -> _Claim | None:
    db = session_factory()
    try:
        now = utcnow_naive()
        query = db.query(EmailLifeProjection).filter(
            EmailLifeProjection.owner_id == owner_id,
            EmailLifeProjection.payload.isnot(None),
            _eligible(now),
        )
        if account_key is not None:
            query = query.filter(EmailLifeProjection.account_key == account_key)
        if folder is not None:
            query = query.filter(EmailLifeProjection.folder == folder)
        query = query.order_by(
            EmailLifeProjection.next_attempt_at.asc(),
            EmailLifeProjection.updated_at.asc(),
            EmailLifeProjection.id.asc(),
        )
        if db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        else:
            query = query.with_for_update()
        row = query.first()
        if row is None:
            db.commit()
            return None
        account = db.query(Account).filter(
            Account.id == row.owner_id,
            Account.status == "active",
        ).one_or_none()
        if account is None:
            raise EmailLifeProjectionLedgerError(
                "Email projection owner no longer exists"
            )
        payload = dict(row.payload or {})
        token = uuid.uuid4().hex
        token_digest = private_digest("email-life-projection-claim", token)
        expected_version = int(row.version or 1)
        next_version = expected_version + 1
        updated = db.query(EmailLifeProjection).filter(
            EmailLifeProjection.id == row.id,
            EmailLifeProjection.owner_id == owner_id,
            EmailLifeProjection.version == expected_version,
            _eligible(now),
        ).update({
            EmailLifeProjection.state: "processing",
            EmailLifeProjection.claim_token_digest: token_digest,
            EmailLifeProjection.claimed_at: now,
            EmailLifeProjection.lease_expires_at: now + timedelta(
                seconds=max(1, int(lease_seconds))
            ),
            EmailLifeProjection.next_attempt_at: None,
            EmailLifeProjection.last_error_code: None,
            EmailLifeProjection.attempt_count:
                EmailLifeProjection.attempt_count + 1,
            EmailLifeProjection.version: next_version,
            EmailLifeProjection.updated_at: now,
        }, synchronize_session=False)
        if int(updated or 0) != 1:
            db.rollback()
            return None
        claim = _Claim(
            row_id=str(row.id),
            owner_id=str(row.owner_id),
            owner_username=str(account.username),
            account_key=str(row.account_key),
            folder=str(row.folder),
            message_uid=str(row.message_uid),
            header_sha256=str(row.header_sha256),
            claim_token_digest=token_digest,
            version=next_version,
            attempt_count=int(row.attempt_count or 0) + 1,
            payload=payload,
        )
        db.commit()
        return claim
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _decode_claim_payload(claim: _Claim) -> dict[str, Any]:
    normalized = _immutable_payload(claim.payload)
    if (
        normalized["uid"] != claim.message_uid
        or _payload_hash(normalized) != claim.header_sha256
    ):
        raise EmailLifeProjectionLedgerError(
            "Email projection payload integrity check failed"
        )
    return normalized


def _mark_completed(
    claim: _Claim,
    *,
    session_factory: Callable[[], Any],
) -> None:
    db = session_factory()
    try:
        now = utcnow_naive()
        updated = db.query(EmailLifeProjection).filter(
            EmailLifeProjection.id == claim.row_id,
            EmailLifeProjection.owner_id == claim.owner_id,
            EmailLifeProjection.version == claim.version,
            EmailLifeProjection.state == "processing",
            EmailLifeProjection.claim_token_digest == claim.claim_token_digest,
        ).update({
            EmailLifeProjection.state: "completed",
            EmailLifeProjection.payload: null(),
            EmailLifeProjection.claim_token_digest: None,
            EmailLifeProjection.claimed_at: None,
            EmailLifeProjection.lease_expires_at: None,
            EmailLifeProjection.next_attempt_at: None,
            EmailLifeProjection.last_error_code: None,
            EmailLifeProjection.completed_at: now,
            EmailLifeProjection.version: claim.version + 1,
            EmailLifeProjection.updated_at: now,
        }, synchronize_session=False)
        if int(updated or 0) != 1:
            db.rollback()
            raise EmailLifeProjectionLedgerError(
                "Email projection lease was lost after durable ingestion"
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _mark_failed(
    claim: _Claim,
    *,
    error_code: str,
    retry_delay_seconds: int,
    session_factory: Callable[[], Any],
) -> bool:
    safe_code = (
        error_code
        if error_code in {"payload_invalid", "projection_failed"}
        else "projection_failed"
    )
    base = max(0, int(retry_delay_seconds))
    exponent = min(max(0, claim.attempt_count - 1), 10)
    delay = min(MAX_RETRY_DELAY_SECONDS, base * (2 ** exponent))
    now = utcnow_naive()
    db = session_factory()
    try:
        updated = db.query(EmailLifeProjection).filter(
            EmailLifeProjection.id == claim.row_id,
            EmailLifeProjection.owner_id == claim.owner_id,
            EmailLifeProjection.version == claim.version,
            EmailLifeProjection.state == "processing",
            EmailLifeProjection.claim_token_digest == claim.claim_token_digest,
        ).update({
            EmailLifeProjection.state: "failed",
            EmailLifeProjection.claim_token_digest: None,
            EmailLifeProjection.claimed_at: None,
            EmailLifeProjection.lease_expires_at: None,
            EmailLifeProjection.next_attempt_at: now + timedelta(seconds=delay),
            EmailLifeProjection.last_error_code: safe_code,
            EmailLifeProjection.version: claim.version + 1,
            EmailLifeProjection.updated_at: now,
        }, synchronize_session=False)
        if int(updated or 0) == 1:
            db.commit()
            return True
        db.rollback()
        return False
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def drain_email_life_projections(
    legacy_path: str | Path | None = None,
    *,
    ingest: Callable[..., Any] | None = None,
    owner: str,
    account_key: str | None = None,
    folder: str | None = None,
    limit: int = 8,
    backfill_limit: int = 32,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS,
    raise_on_failure: bool = False,
    session_factory: Callable[[], Any] | None = None,
) -> EmailLifeProjectionDrainResult:
    """Import a bounded cache batch, then drain canonical SQL claims."""

    factory = session_factory or SessionLocal
    normalized_account = (
        None if account_key is None else
        _bounded_identity(account_key, field="mail account", default="default")
    )
    normalized_folder = (
        None if folder is None else
        _bounded_identity(folder, field="folder", default="INBOX")
    )
    db = factory()
    try:
        account = _resolve_account(db, owner, create=True)
        owner_id = str(account.id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    backfilled = 0
    if legacy_path is not None and normalized_account is not None:
        backfilled = import_legacy_email_projection_sidecar(
            legacy_path,
            owner=owner,
            account_key=normalized_account,
            folder=normalized_folder,
            limit=max(0, min(int(backfill_limit), MAX_IMPORT_BATCH)),
            session_factory=factory,
        )

    if ingest is None:
        from src.life_ingestion import ingest_email_headers

        ingest = ingest_email_headers

    attempted = completed = failed = 0
    for _ in range(max(0, min(int(limit), 100))):
        claim = _claim_next(
            owner_id=owner_id,
            account_key=normalized_account,
            folder=normalized_folder,
            lease_seconds=lease_seconds,
            session_factory=factory,
        )
        if claim is None:
            break
        attempted += 1
        stage = "payload"
        try:
            payload = _decode_claim_payload(claim)
            stage = "projection"
            ingest(
                owner=claim.owner_username,
                account_id=claim.account_key,
                folder=claim.folder,
                emails=[payload],
                expected_owner_id=claim.owner_id,
            )
            # The Life transaction has committed before this CAS. A lost CAS
            # intentionally leaves the claim retryable; Life ingestion is
            # identity-idempotent, so it never forges a completed marker.
            _mark_completed(claim, session_factory=factory)
            completed += 1
        except Exception as exc:
            try:
                _mark_failed(
                    claim,
                    error_code=(
                        "payload_invalid" if stage == "payload"
                        else "projection_failed"
                    ),
                    retry_delay_seconds=retry_delay_seconds,
                    session_factory=factory,
                )
            except Exception:
                # The lease itself is the durable recovery path when marking
                # failure cannot reach the authority database.
                pass
            failed += 1
            if raise_on_failure:
                raise EmailLifeProjectionLedgerError(
                    "Email Life projection failed and remains retryable"
                ) from exc
            if int(retry_delay_seconds) <= 0:
                break

    return EmailLifeProjectionDrainResult(
        backfilled=backfilled,
        attempted=attempted,
        completed=completed,
        failed=failed,
    )
