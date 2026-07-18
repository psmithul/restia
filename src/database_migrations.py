"""Immutable Alembic revision handling and guarded legacy schema adoption."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import hmac
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from migrations.versions.restia_schema_baseline_20260717_0002 import (
    BASELINE_REQUIRED_COLUMNS,
    BASELINE_REQUIRED_TABLES,
)
from migrations.versions.life_planning_spine_20260718_0003 import (
    V3_REQUIRED_COLUMNS,
    V3_REQUIRED_TABLES,
)
from migrations.versions.contact_authority_20260719_0004 import (
    CONTACT_REQUIRED_COLUMNS,
    CONTACT_REQUIRED_TABLES,
)
from migrations.versions.calendar_authority_20260720_0005 import (
    CALENDAR_REQUIRED_COLUMNS,
    CALENDAR_REQUIRED_TABLES,
)
from migrations.versions.telegram_identity_authority_20260721_0006 import (
    TELEGRAM_REQUIRED_COLUMNS,
    TELEGRAM_REQUIRED_TABLES,
)
from migrations.versions.email_outbound_authority_20260722_0007 import (
    EMAIL_OUTBOUND_REQUIRED_COLUMNS,
    EMAIL_OUTBOUND_REQUIRED_TABLES,
)
from migrations.versions.telegram_runtime_authority_20260724_0009 import (
    TELEGRAM_RUNTIME_REQUIRED_COLUMNS,
    TELEGRAM_RUNTIME_REQUIRED_TABLES,
)
from migrations.versions.notification_runtime_authority_20260725_0010 import (
    NOTIFICATION_RUNTIME_REQUIRED_COLUMNS,
    NOTIFICATION_RUNTIME_REQUIRED_TABLES,
)
from migrations.versions.email_life_projection_authority_20260726_0011 import (
    EMAIL_LIFE_PROJECTION_REQUIRED_COLUMNS,
    EMAIL_LIFE_PROJECTION_REQUIRED_TABLES,
)
from migrations.versions.email_runtime_authority_20260727_0012 import (
    EMAIL_RUNTIME_REQUIRED_COLUMNS,
    EMAIL_RUNTIME_REQUIRED_TABLES,
)
from migrations.versions.profile_configuration_authority_20260728_0013 import (
    PROFILE_CONFIGURATION_REQUIRED_COLUMNS,
    PROFILE_CONFIGURATION_REQUIRED_TABLES,
)
from migrations.versions.upload_attachment_authority_20260729_0014 import (
    UPLOAD_METADATA_REQUIRED_COLUMNS,
    UPLOAD_METADATA_REQUIRED_TABLES,
)
from migrations.versions.distributed_worker_leadership_20260730_0015 import (
    RUNTIME_LEADERSHIP_REQUIRED_COLUMNS,
    RUNTIME_LEADERSHIP_REQUIRED_TABLES,
)
from migrations.versions.passkey_authority_20260731_0016 import (
    PASSKEY_REQUIRED_COLUMNS,
    PASSKEY_REQUIRED_TABLES,
)
from src.database_runtime import (
    SCHEMA_AUTHORITY,
    SHARED_SCHEMA_AUTHORITY_READY,
    DatabaseConfigurationError,
    shared_runtime_blockers,
    validate_database_mode,
)


LEGACY_BASELINE_REVISION = "20260716_0001"
EXPLICIT_BASELINE_REVISION = "20260717_0002"
SCHEMA_HEAD_REVISION = "20260731_0016"
KNOWN_BEHIND_REVISIONS = frozenset({
    LEGACY_BASELINE_REVISION,
    EXPLICIT_BASELINE_REVISION,
    "20260718_0003",
    "20260719_0004",
    "20260720_0005",
    "20260721_0006",
    "20260722_0007",
    "20260723_0008",
    "20260724_0009",
    "20260725_0010",
    "20260726_0011",
    "20260727_0012",
    "20260728_0013",
    "20260729_0014",
    "20260730_0015",
})
# Compatibility export retained for existing tooling.  This is now the full
# frozen baseline manifest, not a small sentinel subset.
LEGACY_BASELINE_REQUIRED_TABLES = BASELINE_REQUIRED_TABLES

# A pre-Alembic Restia database must look substantially like the v2.1 schema
# before compatibility code may mutate it. Requiring only ``sessions`` and
# ``chat_messages`` was weak enough to adopt an unrelated or badly truncated
# SQLite file. These tables and columns were all present in the shipped v2.1
# release and remain stable in the pushed stamp-only 0001 foundation.
LEGACY_ADOPTION_REQUIRED_COLUMNS = {
    "api_tokens": frozenset({
        "id", "owner", "name", "token_hash", "token_prefix", "scopes",
        "is_active", "last_used_at", "created_at", "updated_at",
    }),
    "calendar_events": frozenset({"uid", "calendar_id", "dtstart"}),
    "calendars": frozenset({"id", "name", "created_at", "updated_at"}),
    "chat_messages": frozenset({
        "id", "session_id", "role", "content", "timestamp",
    }),
    "documents": frozenset({
        "id", "session_id", "title", "current_content", "created_at",
        "updated_at",
    }),
    "email_accounts": frozenset({
        "id", "name", "imap_host", "smtp_host", "created_at", "updated_at",
    }),
    "model_endpoints": frozenset({
        "id", "name", "base_url", "created_at", "updated_at",
    }),
    "notes": frozenset({"id", "title", "content", "created_at", "updated_at"}),
    "project_work_items": frozenset({
        "id", "project_id", "item_number", "title", "created_at", "updated_at",
    }),
    "projects": frozenset({
        "id", "owner", "key", "name", "created_at", "updated_at",
    }),
    "scheduled_tasks": frozenset({
        "id", "name", "task_type", "trigger_type", "next_run", "status",
        "created_at", "updated_at",
    }),
    "sessions": frozenset({
        "id", "name", "endpoint_url", "model", "created_at", "updated_at",
    }),
}


class SchemaRevisionError(RuntimeError):
    """Raised when schema revision state is absent, ambiguous, or stale."""


@dataclass(frozen=True)
class SchemaRevisionStatus:
    expected_revision: str
    current_revisions: tuple[str, ...]
    state: str
    matches_expected: bool

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["current_revisions"] = list(self.current_revisions)
        return value


def schema_revision_status(engine: Engine) -> SchemaRevisionStatus:
    """Inspect Alembic state without creating tables or stamping revisions."""

    if engine.dialect.name == "sqlite":
        database = str(engine.url.database or "")
        if database not in ("", ":memory:") and not Path(database).exists():
            return SchemaRevisionStatus(
                expected_revision=SCHEMA_HEAD_REVISION,
                current_revisions=(),
                state="unstamped",
                matches_expected=False,
            )
    if not inspect(engine).has_table("alembic_version"):
        return SchemaRevisionStatus(
            expected_revision=SCHEMA_HEAD_REVISION,
            current_revisions=(),
            state="unstamped",
            matches_expected=False,
        )
    with engine.connect() as connection:
        revisions = tuple(
            sorted(
                str(row[0])
                for row in connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).all()
            )
        )
    matches = revisions == (SCHEMA_HEAD_REVISION,)
    if matches:
        state = "current"
    elif len(revisions) == 1 and revisions[0] in KNOWN_BEHIND_REVISIONS:
        state = "behind"
    elif not revisions:
        # Alembic intentionally leaves an empty version table after an online
        # downgrade to base. Runtime startup decides whether its accompanying
        # application schema is also empty before treating it as recoverable.
        state = "unstamped"
    else:
        state = "unknown"
    return SchemaRevisionStatus(
        expected_revision=SCHEMA_HEAD_REVISION,
        current_revisions=revisions,
        state=state,
        matches_expected=matches,
    )


def assert_schema_revision(engine: Engine) -> SchemaRevisionStatus:
    status = schema_revision_status(engine)
    if not status.matches_expected:
        current = ", ".join(status.current_revisions) or "unstamped"
        raise SchemaRevisionError(
            f"Database schema revision is {current}; expected "
            f"{status.expected_revision}"
        )
    return status


def database_status() -> dict[str, Any]:
    """Return deployment-mode and Alembic status without schema mutation."""

    mode_error = None
    try:
        config = validate_database_mode(require_schema_authority=False)
        mode = config.mode
        dialect = config.dialect
        driver = config.driver
        schema_authority = config.schema_authority
    except DatabaseConfigurationError as exc:
        mode_error = str(exc)
        mode = "invalid"
        dialect = "unknown"
        driver = "unknown"
        schema_authority = SCHEMA_AUTHORITY

    revision_error = None
    if mode_error is None:
        try:
            from core.database import engine

            revision = schema_revision_status(engine).as_dict()
        except Exception as exc:
            # Status must remain useful when a configured remote database is
            # down or the selected SQLAlchemy driver cannot initialize.
            revision_error = f"{type(exc).__name__}: database revision query failed"
            revision = {
                "expected_revision": SCHEMA_HEAD_REVISION,
                "current_revisions": [],
                "state": "unavailable",
                "matches_expected": False,
            }
    else:
        # Status must remain useful when a configured remote database is down,
        # without echoing a connection string or credentials into logs/JSON.
        revision_error = "database revision query skipped: invalid configuration"
        revision = {
            "expected_revision": SCHEMA_HEAD_REVISION,
            "current_revisions": [],
            "state": "unavailable",
            "matches_expected": False,
        }
    return {
        "database_mode": mode,
        "dialect": dialect,
        "driver": driver,
        "schema_authority": schema_authority,
        "shared_schema_ready": SHARED_SCHEMA_AUTHORITY_READY,
        "shared_runtime_blockers": shared_runtime_blockers(),
        "configuration_error": mode_error,
        "revision_error": revision_error,
        "revision": revision,
    }


def _alembic_config(database_url: str):
    try:
        from alembic.config import Config
    except ImportError as exc:
        raise SchemaRevisionError(
            "Alembic is not installed; install Restia's requirements first"
        ) from exc

    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def application_table_names(engine: Engine) -> set[str]:
    """Return real application tables, excluding Alembic/SQLite auxiliaries."""

    return {
        str(name)
        for name in inspect(engine).get_table_names()
        if name != "alembic_version" and not name.startswith("sqlite_")
    }


def validate_legacy_adoption_preflight(engine: Engine) -> None:
    """Refuse to mutate a SQLite file that is not a recognizable Restia DB."""

    if engine.dialect.name != "sqlite":
        raise SchemaRevisionError("Legacy adoption is only supported for SQLite")
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    missing_tables = sorted(set(LEGACY_ADOPTION_REQUIRED_COLUMNS) - existing)
    if missing_tables:
        raise SchemaRevisionError(
            "Refusing to adopt an unrecognized partial SQLite schema; missing "
            f"legacy tables: {', '.join(missing_tables)}"
        )
    missing_columns: list[str] = []
    for table_name, required in sorted(LEGACY_ADOPTION_REQUIRED_COLUMNS.items()):
        present = {
            str(column["name"]) for column in inspector.get_columns(table_name)
        }
        absent = sorted(required - present)
        if absent:
            missing_columns.append(f"{table_name}({', '.join(absent)})")
    if missing_columns:
        raise SchemaRevisionError(
            "Refusing to adopt an unrecognized partial SQLite schema; missing "
            f"legacy columns: {'; '.join(missing_columns)}"
        )

    has_accounts = "accounts" in existing
    has_identities = "auth_identities" in existing
    if has_accounts != has_identities:
        raise SchemaRevisionError(
            "Refusing to adopt a partial unified-identity schema"
        )
    if has_accounts:
        required_accounts = {
            "id", "username", "display_name", "created_at", "updated_at",
        }
        required_identities = {
            "id", "account_id", "provider", "subject", "created_at", "updated_at",
        }
        account_columns = {
            str(column["name"]) for column in inspector.get_columns("accounts")
        }
        identity_columns = {
            str(column["name"])
            for column in inspector.get_columns("auth_identities")
        }
        if not required_accounts.issubset(account_columns) or not required_identities.issubset(
            identity_columns
        ):
            raise SchemaRevisionError(
                "Refusing to adopt an unrecognized unified-identity schema"
            )


def backup_legacy_sqlite_database(engine: Engine) -> Path | None:
    """Create a consistent, private SQLite backup before compatibility repair."""

    if engine.dialect.name != "sqlite":
        raise SchemaRevisionError("Legacy backup is only supported for SQLite")
    database = str(engine.url.database or "")
    if database in ("", ":memory:"):
        # In-memory engines are test/ephemeral stores and have no durable file
        # that can be recovered after process exit.
        return None
    source = Path(database)
    if not source.exists() or not source.is_file() or source.is_symlink():
        raise SchemaRevisionError(
            "Refusing legacy adoption without a regular SQLite database file"
        )
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = source.with_name(
        f"{source.name}.pre-{SCHEMA_HEAD_REVISION}-{timestamp}-"
        f"{secrets.token_hex(4)}.bak"
    )
    temporary = target.with_suffix(target.suffix + ".tmp")
    source_connection = None
    backup_connection = None
    try:
        source_connection = sqlite3.connect(str(source))
        backup_connection = sqlite3.connect(str(temporary))
        source_connection.backup(backup_connection)
        result = backup_connection.execute("PRAGMA quick_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise SchemaRevisionError("Legacy SQLite backup failed integrity check")
        backup_connection.close()
        backup_connection = None
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        return target
    except SchemaRevisionError:
        raise
    except Exception as exc:
        raise SchemaRevisionError(
            "Could not create the required pre-migration SQLite backup"
        ) from exc
    finally:
        if backup_connection is not None:
            backup_connection.close()
        if source_connection is not None:
            source_connection.close()
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _identity_unique_sets(inspector) -> set[tuple[str, ...]]:
    values = {
        tuple(str(name) for name in constraint.get("column_names") or ())
        for constraint in inspector.get_unique_constraints("auth_identities")
    }
    values.update({
        tuple(str(name) for name in index.get("column_names") or ())
        for index in inspector.get_indexes("auth_identities")
        if bool(index.get("unique"))
    })
    return values


def _table_unique_sets(inspector, table_name: str) -> set[tuple[str, ...]]:
    values = {
        tuple(str(name) for name in constraint.get("column_names") or ())
        for constraint in inspector.get_unique_constraints(table_name)
    }
    values.update({
        tuple(str(name) for name in index.get("column_names") or ())
        for index in inspector.get_indexes(table_name)
        if bool(index.get("unique"))
    })
    if inspector.bind.dialect.name == "sqlite":
        # SQLite's SQLAlchemy parser can miss a multiline named UNIQUE
        # constraint on an adopted legacy table even though PRAGMA exposes its
        # authoritative auto-index. Include those auto-index column sets.
        with inspector.bind.connect() as connection:
            index_rows = connection.exec_driver_sql(
                f'PRAGMA index_list("{table_name}")'
            ).fetchall()
            for index_row in index_rows:
                if not bool(index_row[2]):
                    continue
                index_name = str(index_row[1]).replace('"', '""')
                columns = tuple(
                    str(column_row[2])
                    for column_row in connection.exec_driver_sql(
                        f'PRAGMA index_info("{index_name}")'
                    ).fetchall()
                    if column_row[2] is not None
                )
                if columns:
                    values.add(columns)
    return values


def _contact_kind_predicate_matches(
    predicate: object,
    *,
    expected_kind: str,
    dialect: str,
) -> bool:
    """Match one exact contact-source partial-index predicate.

    PostgreSQL reflects predicates through ``pg_get_expr`` rather than
    returning the DDL text verbatim.  A predicate emitted as
    ``kind = 'local'`` is therefore commonly reflected as
    ``((kind)::text = 'local'::text)``.  Normalize only those harmless casts
    and parentheses; logical operators or a different comparison still fail
    closed.
    """

    if expected_kind not in {"local", "carddav"}:
        return False
    predicate_text = "" if predicate is None else str(predicate)
    value = re.sub(r"\s+", " ", predicate_text.strip()).lower()
    value = value.replace('"kind"', "kind")
    if dialect == "postgresql":
        value = re.sub(
            r"::\s*(?:text|varchar(?:\s*\(\s*\d+\s*\))?|"
            r"character\s+varying(?:\s*\(\s*\d+\s*\))?)"
            r"(?=\s|$|\))",
            "",
            value,
        )

    def strip_outer_parentheses(text_value: str) -> str:
        while text_value.startswith("(") and text_value.endswith(")"):
            depth = 0
            wrapped = True
            for position, character in enumerate(text_value):
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                    if depth < 0:
                        return text_value
                    if depth == 0 and position != len(text_value) - 1:
                        wrapped = False
                        break
            if not wrapped or depth != 0:
                break
            text_value = text_value[1:-1].strip()
        return text_value

    for _ in range(4):
        previous = value
        value = strip_outer_parentheses(value)
        value = re.sub(r"\(\s*kind\s*\)", "kind", value)
        value = re.sub(r"\(\s*('[^']*')\s*\)", r"\1", value)
        if value == previous:
            break
    return bool(re.fullmatch(
        rf"kind\s*=\s*'{re.escape(expected_kind)}'",
        value,
    ))


def _has_foreign_key(
    inspector,
    table_name: str,
    *,
    constrained: tuple[str, ...],
    referred_table: str,
    referred: tuple[str, ...],
    ondelete: str,
) -> bool:
    return any(
        tuple(str(name) for name in foreign_key.get("constrained_columns") or ())
        == constrained
        and str(foreign_key.get("referred_table") or "") == referred_table
        and tuple(str(name) for name in foreign_key.get("referred_columns") or ())
        == referred
        and str(
            (foreign_key.get("options") or {}).get("ondelete") or ""
        ).upper() == ondelete.upper()
        for foreign_key in inspector.get_foreign_keys(table_name)
    )


def _stored_encrypted_json_envelope(value: object, *, dialect: str) -> str | None:
    """Return the JSON string stored for an ``EncryptedJSON`` column.

    SQLite exposes the raw JSON document, so an encrypted string arrives as a
    quoted JSON string. PostgreSQL drivers normally decode the JSON value first
    and return the envelope directly. Accept both supported representations,
    while refusing SQLite text that is not valid JSON because the ORM's JSON
    result processor could not read it either.
    """

    if isinstance(value, (bytes, bytearray, memoryview)):
        try:
            value = bytes(value).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    if dialect == "sqlite":
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, str) else None
    if value.startswith("enc:"):
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        decoded = value
    return decoded if isinstance(decoded, str) else None


def _validate_entity_link_private_json_encryption(engine: Engine) -> None:
    """Verify every head edge contains readable encrypted private JSON."""

    from src.secret_storage import (
        decrypt,
        is_content_encrypted,
        is_decryptable,
    )

    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT metadata, provenance FROM entity_links"
        )).all()
    for stored_metadata, stored_provenance in rows:
        for field, stored_value in (
            ("metadata", stored_metadata),
            ("provenance", stored_provenance),
        ):
            envelope = _stored_encrypted_json_envelope(
                stored_value, dialect=engine.dialect.name
            )
            if envelope is None or not is_content_encrypted(envelope):
                raise SchemaRevisionError(
                    f"entity_links contains plaintext or invalid {field} at the "
                    "current revision"
                )
            if not is_decryptable(envelope):
                raise SchemaRevisionError(
                    f"entity_links {field} could not be decrypted with the active key"
                )
            try:
                decoded = json.loads(decrypt(envelope))
            except (TypeError, json.JSONDecodeError) as exc:
                raise SchemaRevisionError(
                    f"entity_links {field} does not contain an encrypted JSON object"
                ) from exc
            if not isinstance(decoded, dict):
                raise SchemaRevisionError(
                    f"entity_links {field} does not contain an encrypted JSON object"
                )


def _validate_contact_private_encryption(engine: Engine) -> None:
    """Verify contact authority never legitimizes plaintext private data."""

    from src.secret_storage import (
        decrypt,
        is_content_encrypted,
        is_decryptable,
        is_encrypted,
        private_digest,
    )

    def require_text(field: str, value: object, *, credential: bool = False) -> None:
        if value in (None, ""):
            return
        if not isinstance(value, str):
            raise SchemaRevisionError(f"{field} is not encrypted text")
        valid = is_encrypted(value) if credential else is_content_encrypted(value)
        if not valid or not is_decryptable(value):
            raise SchemaRevisionError(f"{field} is plaintext or not decryptable")

    def require_json(field: str, value: object) -> None:
        envelope = _stored_encrypted_json_envelope(
            value, dialect=engine.dialect.name
        )
        if (
            envelope is None
            or not is_content_encrypted(envelope)
            or not is_decryptable(envelope)
        ):
            raise SchemaRevisionError(f"{field} is plaintext or not decryptable")
        try:
            decoded = json.loads(decrypt(envelope))
        except (TypeError, json.JSONDecodeError) as exc:
            raise SchemaRevisionError(f"{field} is not an encrypted JSON object") from exc
        if not isinstance(decoded, dict):
            raise SchemaRevisionError(f"{field} is not an encrypted JSON object")

    with engine.connect() as connection:
        sources = connection.execute(text(
            "SELECT label, base_url, username, password, last_error "
            "FROM contact_sources"
        )).all()
        records = connection.execute(text(
            "SELECT remote_uid, remote_uid_digest, remote_href, remote_etag, "
            "payload, raw_vcard "
            "FROM contact_records"
        )).all()
        deliveries = connection.execute(text(
            "SELECT payload FROM contact_deliveries"
        )).all()
        imports = connection.execute(text(
            "SELECT backup_settings_path, backup_contacts_path, details "
            "FROM contact_import_runs"
        )).all()
    for label, base_url, username, password, last_error in sources:
        require_text("contact_sources.label", label)
        require_text("contact_sources.base_url", base_url)
        require_text("contact_sources.username", username)
        require_text("contact_sources.password", password, credential=True)
        require_text("contact_sources.last_error", last_error)
    for (
        remote_uid, remote_uid_digest, remote_href, remote_etag, payload,
        raw_vcard,
    ) in records:
        require_text("contact_records.remote_uid", remote_uid)
        if (
            not isinstance(remote_uid_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", remote_uid_digest) is None
            or not hmac.compare_digest(
                private_digest("contact-remote-uid-v1", decrypt(remote_uid)),
                remote_uid_digest,
            )
        ):
            raise SchemaRevisionError(
                "contact_records.remote_uid_digest is missing or inconsistent"
            )
        require_text("contact_records.remote_href", remote_href)
        require_text("contact_records.remote_etag", remote_etag)
        require_json("contact_records.payload", payload)
        require_text("contact_records.raw_vcard", raw_vcard)
    for (payload,) in deliveries:
        require_json("contact_deliveries.payload", payload)
    for settings_path, contacts_path, details in imports:
        require_text("contact_import_runs.backup_settings_path", settings_path)
        require_text("contact_import_runs.backup_contacts_path", contacts_path)
        require_json("contact_import_runs.details", details)


def _validate_calendar_private_encryption(engine: Engine) -> None:
    """Verify calendar undo snapshots and delivery payloads stay encrypted."""

    from src.secret_storage import decrypt, is_content_encrypted, is_decryptable

    def require_json(field: str, value: object) -> None:
        envelope = _stored_encrypted_json_envelope(
            value, dialect=engine.dialect.name
        )
        if (
            envelope is None
            or not is_content_encrypted(envelope)
            or not is_decryptable(envelope)
        ):
            raise SchemaRevisionError(f"{field} is plaintext or not decryptable")
        try:
            decoded = json.loads(decrypt(envelope))
        except (TypeError, json.JSONDecodeError) as exc:
            raise SchemaRevisionError(
                f"{field} is not an encrypted JSON object"
            ) from exc
        if not isinstance(decoded, dict):
            raise SchemaRevisionError(
                f"{field} is not an encrypted JSON object"
            )

    with engine.connect() as connection:
        undo_rows = connection.execute(text(
            "SELECT before_state, created_link_ids FROM calendar_action_undos"
        )).all()
        delivery_rows = connection.execute(text(
            "SELECT payload FROM calendar_deliveries"
        )).all()
    for before_state, created_link_ids in undo_rows:
        require_json("calendar_action_undos.before_state", before_state)
        require_json(
            "calendar_action_undos.created_link_ids", created_link_ids
        )
    for (payload,) in delivery_rows:
        require_json("calendar_deliveries.payload", payload)


def _validate_email_outbound_private_encryption(engine: Engine) -> None:
    """Verify reviewed email content remains encrypted and digest-exact."""

    from src.secret_storage import decrypt, is_content_encrypted, is_decryptable

    def require_json(field: str, value: object) -> dict[str, Any]:
        envelope = _stored_encrypted_json_envelope(
            value, dialect=engine.dialect.name
        )
        if (
            envelope is None
            or not is_content_encrypted(envelope)
            or not is_decryptable(envelope)
        ):
            raise SchemaRevisionError(f"{field} is plaintext or not decryptable")
        try:
            decoded = json.loads(decrypt(envelope))
        except (TypeError, json.JSONDecodeError) as exc:
            raise SchemaRevisionError(
                f"{field} is not an encrypted JSON object"
            ) from exc
        if not isinstance(decoded, dict):
            raise SchemaRevisionError(
                f"{field} is not an encrypted JSON object"
            )
        return decoded

    def digest(value: dict[str, Any]) -> str:
        canonical = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    with engine.connect() as connection:
        drafts = connection.execute(text(
            "SELECT d.content, d.source, d.content_sha256, p.payload "
            "FROM email_outbound_drafts AS d "
            "JOIN action_proposals AS p "
            "ON p.id = d.proposal_id AND p.owner_id = d.owner_id"
        )).all()
        deliveries = connection.execute(text(
            "SELECT payload, content_sha256, provider_message_id "
            "FROM email_outbound_deliveries"
        )).all()
    for stored_content, stored_source, content_sha256, proposal_payload in drafts:
        content = require_json("email_outbound_drafts.content", stored_content)
        require_json("email_outbound_drafts.source", stored_source)
        proposal_content = require_json(
            "action_proposals.payload", proposal_payload
        )
        if (
            not isinstance(content_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
            or not hmac.compare_digest(digest(content), content_sha256)
            or content != proposal_content
        ):
            raise SchemaRevisionError(
                "email_outbound_drafts content digest or proposal snapshot is inconsistent"
            )
    for stored_payload, content_sha256, provider_message_id in deliveries:
        payload = require_json(
            "email_outbound_deliveries.payload", stored_payload
        )
        if (
            not isinstance(content_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
            or not hmac.compare_digest(digest(payload), content_sha256)
        ):
            raise SchemaRevisionError(
                "email_outbound_deliveries content digest is inconsistent"
            )
        if provider_message_id not in (None, "") and (
            not isinstance(provider_message_id, str)
            or not is_content_encrypted(provider_message_id)
            or not is_decryptable(provider_message_id)
        ):
            raise SchemaRevisionError(
                "email_outbound_deliveries.provider_message_id is plaintext"
            )


def _validate_telegram_private_encryption(engine: Engine) -> None:
    """Verify Telegram lookup indexes reveal no chat/session plaintext."""

    from src.secret_storage import (
        decrypt,
        is_content_encrypted,
        is_decryptable,
        private_digest,
    )

    def require_text(field: str, value: object, *, allow_empty: bool = False) -> str:
        if allow_empty and value in (None, ""):
            return ""
        if (
            not isinstance(value, str)
            or not is_content_encrypted(value)
            or not is_decryptable(value)
        ):
            raise SchemaRevisionError(f"{field} is plaintext or not decryptable")
        plaintext = decrypt(value)
        if not plaintext and not allow_empty:
            raise SchemaRevisionError(f"{field} decrypted to an empty value")
        return plaintext

    def require_json(field: str, value: object) -> None:
        envelope = _stored_encrypted_json_envelope(
            value, dialect=engine.dialect.name
        )
        if (
            envelope is None
            or not is_content_encrypted(envelope)
            or not is_decryptable(envelope)
        ):
            raise SchemaRevisionError(f"{field} is plaintext or not decryptable")
        try:
            decoded = json.loads(decrypt(envelope))
        except (TypeError, json.JSONDecodeError) as exc:
            raise SchemaRevisionError(f"{field} is not an encrypted JSON object") from exc
        if not isinstance(decoded, dict):
            raise SchemaRevisionError(f"{field} is not an encrypted JSON object")

    with engine.connect() as connection:
        principals = connection.execute(text(
            "SELECT bot_fingerprint, chat_id, chat_id_digest "
            "FROM telegram_principals"
        )).all()
        bindings = connection.execute(text(
            "SELECT session_id FROM telegram_conversation_bindings"
        )).all()
        link_codes = connection.execute(text(
            "SELECT bot_fingerprint, code_digest, digest_scheme "
            "FROM telegram_link_codes"
        )).all()
        imports = connection.execute(text(
            "SELECT source_sha256, source_path, details "
            "FROM telegram_identity_import_runs"
        )).all()
        runtime_inbound = connection.execute(text(
            "SELECT chat_id, reply_text, status, processing_claim_digest, "
            "reply_claim_digest FROM telegram_inbound_updates"
        )).all()
        runtime_imports = connection.execute(text(
            "SELECT bot_fingerprint, source_sha256, details "
            "FROM telegram_runtime_import_runs"
        )).all()
    for bot_fingerprint, stored_chat_id, chat_digest in principals:
        if (
            not isinstance(bot_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", bot_fingerprint) is None
            or not isinstance(chat_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", chat_digest) is None
        ):
            raise SchemaRevisionError("telegram_principals has an invalid digest")
        chat_id = require_text("telegram_principals.chat_id", stored_chat_id)
        expected = private_digest(
            f"telegram-chat-principal-v1:{bot_fingerprint}", chat_id
        )
        if not hmac.compare_digest(expected, chat_digest):
            raise SchemaRevisionError(
                "telegram_principals chat digest is inconsistent"
            )
    for (stored_session_id,) in bindings:
        require_text(
            "telegram_conversation_bindings.session_id", stored_session_id
        )
    for bot_fingerprint, code_digest, digest_scheme in link_codes:
        if (
            not isinstance(bot_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", bot_fingerprint) is None
            or not isinstance(code_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", code_digest) is None
            or digest_scheme not in {"hmac_sha256_v1", "legacy_sha256_v1"}
        ):
            raise SchemaRevisionError("telegram_link_codes has an invalid digest")
    for source_sha256, source_path, details in imports:
        if (
            not isinstance(source_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        ):
            raise SchemaRevisionError(
                "telegram_identity_import_runs has an invalid source digest"
            )
        require_text(
            "telegram_identity_import_runs.source_path",
            source_path,
            allow_empty=True,
        )
        require_json("telegram_identity_import_runs.details", details)
    for (
        stored_chat_id, stored_reply_text, status,
        processing_claim_digest, reply_claim_digest,
    ) in runtime_inbound:
        require_text("telegram_inbound_updates.chat_id", stored_chat_id)
        if stored_reply_text not in (None, ""):
            require_text(
                "telegram_inbound_updates.reply_text", stored_reply_text
            )
        elif status == "reply_pending":
            raise SchemaRevisionError(
                "telegram_inbound_updates has an empty pending reply"
            )
        for field, digest in (
            ("processing_claim_digest", processing_claim_digest),
            ("reply_claim_digest", reply_claim_digest),
        ):
            if digest is not None and (
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise SchemaRevisionError(
                    f"telegram_inbound_updates.{field} is invalid"
                )
    for bot_fingerprint, source_sha256, details in runtime_imports:
        if any(
            not isinstance(value, str)
            or re.fullmatch(r"[0-9a-f]{64}", value) is None
            for value in (bot_fingerprint, source_sha256)
        ):
            raise SchemaRevisionError(
                "telegram_runtime_import_runs has an invalid digest"
            )
        require_json("telegram_runtime_import_runs.details", details)


def _validate_notification_private_encryption(engine: Engine) -> None:
    """Reject plaintext browser payloads/tokens and malformed private digests."""

    from src.secret_storage import decrypt, is_content_encrypted, is_decryptable

    def require_json(field: str, value: object) -> None:
        envelope = _stored_encrypted_json_envelope(
            value, dialect=engine.dialect.name
        )
        if (
            envelope is None
            or not is_content_encrypted(envelope)
            or not is_decryptable(envelope)
        ):
            raise SchemaRevisionError(f"{field} is plaintext or not decryptable")
        try:
            decoded = json.loads(decrypt(envelope))
        except (TypeError, json.JSONDecodeError) as exc:
            raise SchemaRevisionError(
                f"{field} is not an encrypted JSON object"
            ) from exc
        if not isinstance(decoded, dict):
            raise SchemaRevisionError(
                f"{field} is not an encrypted JSON object"
            )

    with engine.connect() as connection:
        browser_rows = connection.execute(text(
            "SELECT payload, dedupe_key_digest, claim_token "
            "FROM browser_notifications"
        )).all()
        reminder_rows = connection.execute(text(
            "SELECT claim_token_digest FROM reminder_delivery_claims"
        )).all()
        imports = connection.execute(text(
            "SELECT source_sha256, details FROM notification_runtime_import_runs"
        )).all()
    for payload, dedupe_digest, claim_token in browser_rows:
        require_json("browser_notifications.payload", payload)
        if dedupe_digest is not None and (
            not isinstance(dedupe_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", dedupe_digest) is None
        ):
            raise SchemaRevisionError(
                "browser_notifications.dedupe_key_digest is invalid"
            )
        if claim_token not in (None, "") and (
            not isinstance(claim_token, str)
            or not is_content_encrypted(claim_token)
            or not is_decryptable(claim_token)
        ):
            raise SchemaRevisionError(
                "browser_notifications.claim_token is plaintext"
            )
    for (claim_digest,) in reminder_rows:
        if claim_digest is not None and (
            not isinstance(claim_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", claim_digest) is None
        ):
            raise SchemaRevisionError(
                "reminder_delivery_claims.claim_token_digest is invalid"
            )
    for source_sha256, details in imports:
        if (
            not isinstance(source_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        ):
            raise SchemaRevisionError(
                "notification_runtime_import_runs has an invalid digest"
            )
        require_json("notification_runtime_import_runs.details", details)


def _validate_email_life_projection_private_encryption(engine: Engine) -> None:
    """Verify projection headers/thread evidence never become plaintext SQL."""

    from src.secret_storage import decrypt, is_content_encrypted, is_decryptable

    def require_json(field: str, value: object) -> dict[str, Any]:
        envelope = _stored_encrypted_json_envelope(
            value, dialect=engine.dialect.name
        )
        if (
            envelope is None
            or not is_content_encrypted(envelope)
            or not is_decryptable(envelope)
        ):
            raise SchemaRevisionError(f"{field} is plaintext or not decryptable")
        try:
            decoded = json.loads(decrypt(envelope))
        except (TypeError, json.JSONDecodeError) as exc:
            raise SchemaRevisionError(
                f"{field} is not an encrypted JSON object"
            ) from exc
        if not isinstance(decoded, dict):
            raise SchemaRevisionError(
                f"{field} is not an encrypted JSON object"
            )
        return decoded

    def payload_digest(value: dict[str, Any]) -> str:
        canonical = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT payload, header_sha256, state, claim_token_digest, "
            "last_error_code FROM email_life_projection_ledger"
        )).all()
        imports = connection.execute(text(
            "SELECT source_sha256, details "
            "FROM email_life_projection_import_runs"
        )).all()
    for payload, header_sha256, state, claim_digest, error_code in rows:
        if (
            not isinstance(header_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", header_sha256) is None
        ):
            raise SchemaRevisionError(
                "email_life_projection_ledger has an invalid header digest"
            )
        if state == "completed":
            if payload is not None:
                raise SchemaRevisionError(
                    "completed email Life projection retains private payload"
                )
        else:
            decoded = require_json(
                "email_life_projection_ledger.payload", payload
            )
            if not hmac.compare_digest(payload_digest(decoded), header_sha256):
                raise SchemaRevisionError(
                    "email_life_projection_ledger payload digest is inconsistent"
                )
        if claim_digest is not None and (
            not isinstance(claim_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", claim_digest) is None
        ):
            raise SchemaRevisionError(
                "email_life_projection_ledger has an invalid claim digest"
            )
        if error_code not in (None, "payload_invalid", "projection_failed"):
            raise SchemaRevisionError(
                "email_life_projection_ledger has an unsafe error code"
            )
    for source_sha256, details in imports:
        if (
            not isinstance(source_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        ):
            raise SchemaRevisionError(
                "email_life_projection_import_runs has an invalid source digest"
            )
        require_json("email_life_projection_import_runs.details", details)


def _validate_email_runtime_private_encryption(engine: Engine) -> None:
    """Verify mutable email authority stores only encrypted private values."""

    from src.secret_storage import decrypt, is_content_encrypted, is_decryptable

    def require_json(field: str, value: object) -> dict[str, Any]:
        envelope = _stored_encrypted_json_envelope(
            value, dialect=engine.dialect.name
        )
        if (
            envelope is None
            or not is_content_encrypted(envelope)
            or not is_decryptable(envelope)
        ):
            raise SchemaRevisionError(f"{field} is plaintext or not decryptable")
        try:
            decoded = json.loads(decrypt(envelope))
        except (TypeError, json.JSONDecodeError) as exc:
            raise SchemaRevisionError(
                f"{field} is not an encrypted JSON object"
            ) from exc
        if not isinstance(decoded, dict):
            raise SchemaRevisionError(
                f"{field} is not an encrypted JSON object"
            )
        return decoded

    def require_digest(field: str, value: object, *, nullable: bool = False) -> None:
        if nullable and value is None:
            return
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise SchemaRevisionError(f"{field} is not a valid private digest")

    with engine.connect() as connection:
        tags = connection.execute(text(
            "SELECT message_digest, location_digest, payload "
            "FROM email_tag_states"
        )).all()
        rules = connection.execute(text(
            "SELECT rules FROM email_automation_rules"
        )).all()
        schedules = connection.execute(text(
            "SELECT payload, payload_sha256, claim_token_digest, "
            "provider_message_id, last_error_code "
            "FROM email_scheduled_deliveries"
        )).all()
        automation = connection.execute(text(
            "SELECT message_digest, payload, claim_token_digest, "
            "last_error_code FROM email_automation_runs"
        )).all()
        imports = connection.execute(text(
            "SELECT source_sha256, details FROM email_runtime_import_runs"
        )).all()
    for message_digest, location_digest, payload in tags:
        require_digest("email_tag_states.message_digest", message_digest)
        require_digest("email_tag_states.location_digest", location_digest)
        require_json("email_tag_states.payload", payload)
    for (value,) in rules:
        decoded = require_json("email_automation_rules.rules", value)
        if set(decoded) - {
            "email_auto_summarize", "email_auto_reply", "email_auto_tag",
            "email_auto_spam", "email_auto_calendar",
        } or any(type(flag) is not bool for flag in decoded.values()):
            raise SchemaRevisionError(
                "email_automation_rules contains unsupported rule data"
            )
    for payload, payload_digest, claim_digest, provider_id, error_code in schedules:
        require_json("email_scheduled_deliveries.payload", payload)
        require_digest("email_scheduled_deliveries.payload_sha256", payload_digest)
        require_digest(
            "email_scheduled_deliveries.claim_token_digest", claim_digest,
            nullable=True,
        )
        if provider_id not in (None, "") and (
            not isinstance(provider_id, str)
            or not is_content_encrypted(provider_id)
            or not is_decryptable(provider_id)
        ):
            raise SchemaRevisionError(
                "email_scheduled_deliveries.provider_message_id is plaintext"
            )
        if error_code not in (
            None, "smtp_failed", "payload_invalid", "email_account_unavailable",
        ):
            raise SchemaRevisionError(
                "email_scheduled_deliveries has an unsafe error code"
            )
    for message_digest, payload, claim_digest, error_code in automation:
        require_digest("email_automation_runs.message_digest", message_digest)
        require_json("email_automation_runs.payload", payload)
        require_digest(
            "email_automation_runs.claim_token_digest", claim_digest,
            nullable=True,
        )
        if error_code not in (None, "operation_failed", "payload_invalid"):
            raise SchemaRevisionError(
                "email_automation_runs has an unsafe error code"
            )
    for source_digest, details in imports:
        require_digest("email_runtime_import_runs.source_sha256", source_digest)
        require_json("email_runtime_import_runs.details", details)


def _validate_profile_configuration_private_encryption(engine: Engine) -> None:
    """Verify profile authority privacy, digest, and split-authority rules."""

    from src.profile_configuration_service import (
        DEPLOYMENT_ONLY_KEYS,
        EMAIL_AUTOMATION_SETTING_KEYS,
    )
    from src.secret_storage import decrypt, is_content_encrypted, is_decryptable

    def private_object(field: str, value: object) -> dict[str, Any]:
        envelope = _stored_encrypted_json_envelope(
            value, dialect=engine.dialect.name,
        )
        if (
            envelope is None
            or not is_content_encrypted(envelope)
            or not is_decryptable(envelope)
        ):
            raise SchemaRevisionError(f"{field} is plaintext or not decryptable")
        try:
            decoded = json.loads(decrypt(envelope))
        except (TypeError, json.JSONDecodeError) as exc:
            raise SchemaRevisionError(f"{field} is not encrypted JSON") from exc
        if not isinstance(decoded, dict) or set(decoded) != {"value"}:
            raise SchemaRevisionError(f"{field} has an invalid value envelope")
        return decoded

    def public_object(field: str, value: object) -> dict[str, Any]:
        if isinstance(value, dict):
            decoded = value
        elif isinstance(value, str):
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as exc:
                raise SchemaRevisionError(f"{field} is not JSON") from exc
        else:
            raise SchemaRevisionError(f"{field} is not a JSON object")
        if not isinstance(decoded, dict) or set(decoded) != {"value"}:
            raise SchemaRevisionError(f"{field} has an invalid value envelope")
        return decoded

    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT namespace, key, visibility, public_value, private_value "
            "FROM profile_configurations"
        )).all()
        mutations = connection.execute(text(
            "SELECT idempotency_digest, request_digest "
            "FROM profile_configuration_mutations"
        )).all()
        imports = connection.execute(text(
            "SELECT source_sha256, details "
            "FROM profile_configuration_import_runs"
        )).all()
    for namespace, key, visibility, public_value, private_value in rows:
        if namespace == "feature":
            if visibility != "public" or private_value is not None:
                raise SchemaRevisionError(
                    "feature preferences must use the public payload column"
                )
            public_object("profile_configurations.public_value", public_value)
        else:
            if visibility != "private" or public_value is not None:
                raise SchemaRevisionError(
                    "private profile configuration uses an unsafe payload column"
                )
            private_object("profile_configurations.private_value", private_value)
        normalized_key = str(key or "").strip().lower().replace("-", "_")
        if normalized_key in DEPLOYMENT_ONLY_KEYS or normalized_key.startswith((
            "database_", "restia_encryption_", "odysseus_encryption_",
        )):
            raise SchemaRevisionError(
                "profile configuration contains deployment-only authority"
            )
        if (
            namespace == "setting"
            and normalized_key in EMAIL_AUTOMATION_SETTING_KEYS
        ):
            raise SchemaRevisionError(
                "profile configuration duplicates email automation authority"
            )
    for idempotency_digest, request_digest in mutations:
        for field, value in (
            ("idempotency_digest", idempotency_digest),
            ("request_digest", request_digest),
        ):
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise SchemaRevisionError(
                    f"profile_configuration_mutations.{field} is invalid"
                )
    for source_digest, details in imports:
        if (
            not isinstance(source_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None
        ):
            raise SchemaRevisionError(
                "profile_configuration_import_runs source digest is invalid"
            )
        private_object("profile_configuration_import_runs.details", details)


def _validate_upload_metadata_private_encryption(engine: Engine) -> None:
    """Verify upload payload encryption, keyed digests, and safe blob keys."""

    from src.blob_store import BlobKeyError, FileSystemBlobStore
    from src.secret_storage import decrypt, is_content_encrypted, is_decryptable
    from src.secret_storage import private_digest

    def private_object(field: str, value: object) -> dict[str, Any]:
        envelope = _stored_encrypted_json_envelope(
            value, dialect=engine.dialect.name,
        )
        if (
            envelope is None
            or not is_content_encrypted(envelope)
            or not is_decryptable(envelope)
        ):
            raise SchemaRevisionError(f"{field} is plaintext or not decryptable")
        try:
            decoded = json.loads(decrypt(envelope))
        except (TypeError, json.JSONDecodeError) as exc:
            raise SchemaRevisionError(f"{field} is not encrypted JSON") from exc
        if not isinstance(decoded, dict):
            raise SchemaRevisionError(f"{field} is not an encrypted JSON object")
        return decoded

    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT owner_id, content_digest, blob_key, payload, state "
            "FROM chat_upload_metadata"
        )).all()
        imports = connection.execute(text(
            "SELECT source_sha256, details "
            "FROM chat_upload_metadata_import_runs"
        )).all()
    required_payload = {
        "content_sha256", "name", "original_name", "mime", "size",
        "uploaded_at", "client_ip", "width", "height",
    }
    for owner_id, digest, blob_key, payload, state in rows:
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise SchemaRevisionError(
                "chat_upload_metadata.content_digest is invalid"
            )
        try:
            FileSystemBlobStore.validate_key(blob_key)
        except BlobKeyError as exc:
            raise SchemaRevisionError(
                "chat_upload_metadata.blob_key is unsafe"
            ) from exc
        decoded = private_object("chat_upload_metadata.payload", payload)
        if state == "tombstoned":
            if decoded:
                raise SchemaRevisionError(
                    "tombstoned upload metadata retains private payload"
                )
            continue
        if set(decoded) != required_payload:
            raise SchemaRevisionError(
                "chat_upload_metadata.payload has an invalid contract"
            )
        content_sha = decoded.get("content_sha256")
        if (
            not isinstance(content_sha, str)
            or re.fullmatch(r"[0-9a-f]{64}", content_sha) is None
            or digest != private_digest(
                f"chat-upload-content-v1:{owner_id}", content_sha,
            )
        ):
            raise SchemaRevisionError(
                "chat_upload_metadata content identity is inconsistent"
            )
    for source_digest, details in imports:
        if (
            not isinstance(source_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None
        ):
            raise SchemaRevisionError(
                "chat_upload_metadata_import_runs source digest is invalid"
            )
        private_object("chat_upload_metadata_import_runs.details", details)


def validate_head_schema(engine: Engine) -> None:
    """Verify frozen baseline plus every reviewed post-baseline contract."""

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    required_tables = (
        BASELINE_REQUIRED_TABLES | V3_REQUIRED_TABLES
        | CONTACT_REQUIRED_TABLES | CALENDAR_REQUIRED_TABLES
        | TELEGRAM_REQUIRED_TABLES | EMAIL_OUTBOUND_REQUIRED_TABLES
        | TELEGRAM_RUNTIME_REQUIRED_TABLES
        | NOTIFICATION_RUNTIME_REQUIRED_TABLES
        | EMAIL_LIFE_PROJECTION_REQUIRED_TABLES
        | EMAIL_RUNTIME_REQUIRED_TABLES
        | PROFILE_CONFIGURATION_REQUIRED_TABLES
        | UPLOAD_METADATA_REQUIRED_TABLES
        | RUNTIME_LEADERSHIP_REQUIRED_TABLES
        | PASSKEY_REQUIRED_TABLES
    )
    required_columns = {
        **BASELINE_REQUIRED_COLUMNS,
        **V3_REQUIRED_COLUMNS,
        **CONTACT_REQUIRED_COLUMNS,
        **CALENDAR_REQUIRED_COLUMNS,
        **TELEGRAM_REQUIRED_COLUMNS,
        **EMAIL_OUTBOUND_REQUIRED_COLUMNS,
        **TELEGRAM_RUNTIME_REQUIRED_COLUMNS,
        **NOTIFICATION_RUNTIME_REQUIRED_COLUMNS,
        **EMAIL_LIFE_PROJECTION_REQUIRED_COLUMNS,
        **EMAIL_RUNTIME_REQUIRED_COLUMNS,
        **PROFILE_CONFIGURATION_REQUIRED_COLUMNS,
        **UPLOAD_METADATA_REQUIRED_COLUMNS,
        **RUNTIME_LEADERSHIP_REQUIRED_COLUMNS,
        **PASSKEY_REQUIRED_COLUMNS,
    }
    missing = sorted(required_tables - existing)
    if missing:
        raise SchemaRevisionError(
            "Database claims the current revision but is missing baseline "
            f"tables: {', '.join(missing)}"
        )
    missing_columns: list[str] = []
    for table_name, required in sorted(required_columns.items()):
        present = {
            str(column["name"]) for column in inspector.get_columns(table_name)
        }
        absent = sorted(required - present)
        if absent:
            missing_columns.append(f"{table_name}({', '.join(absent)})")
    if missing_columns:
        raise SchemaRevisionError(
            "Database is missing frozen baseline columns: "
            + "; ".join(missing_columns)
        )

    # The head revision is also a data-bound privacy contract. A version row
    # must not be able to legitimize plaintext PlanningItem content after an
    # offline/manual stamp or an incomplete legacy repair. Empty details carry
    # no private value; every non-empty content value uses the distinct V3
    # content envelope so credential-looking literal text remains exact.
    with engine.connect() as connection:
        plaintext_planning_count = int(connection.execute(text(
            "SELECT COUNT(*) FROM planning_items "
            "WHERE title NOT LIKE 'enc:c1:%' "
            "OR (details <> '' AND details NOT LIKE 'enc:c1:%')"
        )).scalar_one())
    if plaintext_planning_count:
        raise SchemaRevisionError(
            "planning_items contains plaintext content at the current revision"
        )
    _validate_entity_link_private_json_encryption(engine)
    _validate_contact_private_encryption(engine)
    _validate_calendar_private_encryption(engine)
    _validate_email_outbound_private_encryption(engine)
    _validate_telegram_private_encryption(engine)
    _validate_notification_private_encryption(engine)
    _validate_email_life_projection_private_encryption(engine)
    _validate_email_runtime_private_encryption(engine)
    _validate_profile_configuration_private_encryption(engine)
    _validate_upload_metadata_private_encryption(engine)

    identity_unique_sets = _identity_unique_sets(inspector)
    expected_identity_key = ("provider", "issuer", "subject")
    if expected_identity_key not in identity_unique_sets:
        raise SchemaRevisionError(
            "auth_identities lacks issuer-qualified provider uniqueness"
        )
    if ("provider", "subject") in identity_unique_sets:
        raise SchemaRevisionError(
            "auth_identities retains obsolete provider/subject uniqueness"
        )

    account_checks = {
        str(constraint.get("name") or "")
        for constraint in inspector.get_check_constraints("accounts")
    }
    required_account_checks = {"ck_accounts_status", "ck_accounts_auth_epoch"}
    if not required_account_checks.issubset(account_checks):
        raise SchemaRevisionError(
            "accounts lacks required status/auth-epoch database checks"
        )

    leadership_checks = {
        str(constraint.get("name") or "")
        for constraint in inspector.get_check_constraints(
            "runtime_worker_leases"
        )
    }
    required_leadership_checks = {
        "ck_runtime_worker_leases_fencing",
        "ck_runtime_worker_leases_holder",
    }
    if not required_leadership_checks.issubset(leadership_checks):
        raise SchemaRevisionError(
            "runtime_worker_leases lacks fencing/holder database checks"
        )
    leadership_pk = tuple(
        inspector.get_pk_constraint("runtime_worker_leases").get(
            "constrained_columns"
        ) or ()
    )
    if leadership_pk != ("lease_name",):
        raise SchemaRevisionError(
            "runtime_worker_leases lacks lease-name primary identity"
        )
    leadership_indexes = {
        tuple(str(name) for name in index.get("column_names") or ())
        for index in inspector.get_indexes("runtime_worker_leases")
    }
    if not {
        ("holder_id",), ("lease_expires_at",),
    }.issubset(leadership_indexes):
        raise SchemaRevisionError(
            "runtime_worker_leases lacks takeover/expiry indexes"
        )

    if engine.dialect.name == "sqlite":
        # SQLAlchemy's SQLite inspector does not reliably recover ON DELETE
        # options from multiline CREATE TABLE statements. PRAGMA is SQLite's
        # authoritative parsed representation of the constraint.
        with engine.connect() as connection:
            api_account_fk = any(
                str(row[2]) == "accounts"
                and str(row[3]) == "account_id"
                and str(row[4]) == "id"
                and str(row[6]).upper() == "CASCADE"
                for row in connection.exec_driver_sql(
                    'PRAGMA foreign_key_list("api_tokens")'
                ).fetchall()
            )
    else:
        api_account_fk = any(
            tuple(foreign_key.get("constrained_columns") or ())
            == ("account_id",)
            and str(foreign_key.get("referred_table") or "") == "accounts"
            and tuple(foreign_key.get("referred_columns") or ()) == ("id",)
            and str(
                (foreign_key.get("options") or {}).get("ondelete") or ""
            ).upper() == "CASCADE"
            for foreign_key in inspector.get_foreign_keys("api_tokens")
        )
    if not api_account_fk:
        raise SchemaRevisionError(
            "api_tokens lacks the account ownership cascade constraint"
        )

    for table_name in (
        "profile_configurations",
        "profile_configuration_mutations",
        "profile_configuration_import_runs",
        "chat_upload_metadata",
        "chat_upload_metadata_import_runs",
    ):
        if not _has_foreign_key(
            inspector,
            table_name,
            constrained=("owner_id",),
            referred_table="accounts",
            referred=("id",),
            ondelete="CASCADE",
        ):
            raise SchemaRevisionError(
                f"{table_name} lacks Account.id ownership cascade"
            )
    if not _has_foreign_key(
        inspector,
        "profile_configuration_mutations",
        constrained=("configuration_id",),
        referred_table="profile_configurations",
        referred=("id",),
        ondelete="CASCADE",
    ):
        raise SchemaRevisionError(
            "profile_configuration_mutations lacks configuration cascade"
        )

    life_entity_unique_sets = {
        tuple(str(name) for name in constraint.get("column_names") or ())
        for constraint in inspector.get_unique_constraints("life_entities")
    }
    if ("id", "owner_id") not in life_entity_unique_sets:
        raise SchemaRevisionError(
            "life_entities lacks composite id/owner uniqueness"
        )

    for child_table in ("life_entity_versions", "focus_sessions"):
        composite_owner_fk = any(
            tuple(foreign_key.get("constrained_columns") or ())
            == ("entity_id", "owner_id")
            and str(foreign_key.get("referred_table") or "") == "life_entities"
            and tuple(foreign_key.get("referred_columns") or ())
            == ("id", "owner_id")
            and str(
                (foreign_key.get("options") or {}).get("ondelete") or ""
            ).upper() == "CASCADE"
            for foreign_key in inspector.get_foreign_keys(child_table)
        )
        if not composite_owner_fk:
            raise SchemaRevisionError(
                f"{child_table} lacks owner-matching life-entity ownership"
            )

    contact_source_owner_fk = any(
        tuple(foreign_key.get("constrained_columns") or ())
        == ("source_id", "owner_id")
        and str(foreign_key.get("referred_table") or "") == "contact_sources"
        and tuple(foreign_key.get("referred_columns") or ())
        == ("id", "owner_id")
        and str(
            (foreign_key.get("options") or {}).get("ondelete") or ""
        ).upper() == "CASCADE"
        for foreign_key in inspector.get_foreign_keys("contact_records")
    )
    if not contact_source_owner_fk:
        raise SchemaRevisionError(
            "contact_records lacks owner-matching contact-source ownership"
        )

    for constrained, referred, label in (
        (("source_id", "owner_id"), "contact_sources", "contact source"),
        (("record_id", "owner_id"), "contact_records", "contact record"),
    ):
        expected_referred = ("id", "owner_id")
        matching = any(
            tuple(foreign_key.get("constrained_columns") or ()) == constrained
            and str(foreign_key.get("referred_table") or "") == referred
            and tuple(foreign_key.get("referred_columns") or ())
            == expected_referred
            and str(
                (foreign_key.get("options") or {}).get("ondelete") or ""
            ).upper() == "CASCADE"
            for foreign_key in inspector.get_foreign_keys("contact_deliveries")
        )
        if not matching:
            raise SchemaRevisionError(
                f"contact_deliveries lacks owner-matching {label} ownership"
            )

    calendar_event_owner_fk = any(
        tuple(foreign_key.get("constrained_columns") or ())
        == ("calendar_id", "owner_id")
        and str(foreign_key.get("referred_table") or "") == "calendars"
        and tuple(foreign_key.get("referred_columns") or ())
        == ("id", "owner_id")
        and str(
            (foreign_key.get("options") or {}).get("ondelete") or ""
        ).upper() == "CASCADE"
        for foreign_key in inspector.get_foreign_keys("calendar_events")
    )
    if not calendar_event_owner_fk:
        raise SchemaRevisionError(
            "calendar_events lacks owner-matching calendar ownership"
        )

    calendar_event_pk = tuple(
        inspector.get_pk_constraint("calendar_events").get(
            "constrained_columns"
        ) or ()
    )
    if calendar_event_pk != ("uid", "owner_id"):
        raise SchemaRevisionError(
            "calendar_events lacks owner-scoped primary identity"
        )

    planning_event_fk = any(
        tuple(foreign_key.get("constrained_columns") or ())
        == ("calendar_event_uid", "calendar_id")
        and str(foreign_key.get("referred_table") or "") == "calendar_events"
        and tuple(foreign_key.get("referred_columns") or ())
        == ("uid", "calendar_id")
        and str(
            (foreign_key.get("options") or {}).get("ondelete") or ""
        ).upper() == "SET NULL"
        for foreign_key in inspector.get_foreign_keys("planning_items")
    )
    if not planning_event_fk:
        raise SchemaRevisionError(
            "planning_items lacks calendar-scoped event ownership"
        )

    for table_name in (
        "calendar_action_undos",
        "calendar_deliveries",
        "email_outbound_drafts",
        "email_outbound_deliveries",
    ):
        proposal_owner_fk = any(
            tuple(foreign_key.get("constrained_columns") or ())
            == ("proposal_id", "owner_id")
            and str(foreign_key.get("referred_table") or "")
            == "action_proposals"
            and tuple(foreign_key.get("referred_columns") or ())
            == ("id", "owner_id")
            for foreign_key in inspector.get_foreign_keys(table_name)
        )
        if not proposal_owner_fk:
            raise SchemaRevisionError(
                f"{table_name} lacks owner-matching action-proposal ownership"
            )

    email_delivery_draft_owner_fk = any(
        tuple(foreign_key.get("constrained_columns") or ())
        == ("draft_id", "owner_id")
        and str(foreign_key.get("referred_table") or "")
        == "email_outbound_drafts"
        and tuple(foreign_key.get("referred_columns") or ())
        == ("id", "owner_id")
        and str(
            (foreign_key.get("options") or {}).get("ondelete") or ""
        ).upper() == "CASCADE"
        for foreign_key in inspector.get_foreign_keys(
            "email_outbound_deliveries"
        )
    )
    if not email_delivery_draft_owner_fk:
        raise SchemaRevisionError(
            "email_outbound_deliveries lacks owner-matching draft ownership"
        )

    for table_name in (
        "email_outbound_drafts", "email_outbound_deliveries",
        "email_scheduled_deliveries",
    ):
        if not _has_foreign_key(
            inspector,
            table_name,
            constrained=("email_account_id",),
            referred_table="email_accounts",
            referred=("id",),
            ondelete="RESTRICT",
        ):
            raise SchemaRevisionError(
                f"{table_name} lacks the configured email-account constraint"
            )

    calendar_delivery_owner_fk = any(
        tuple(foreign_key.get("constrained_columns") or ())
        == ("calendar_id", "owner_id")
        and str(foreign_key.get("referred_table") or "") == "calendars"
        and tuple(foreign_key.get("referred_columns") or ())
        == ("id", "owner_id")
        for foreign_key in inspector.get_foreign_keys("calendar_deliveries")
    )
    if not calendar_delivery_owner_fk:
        raise SchemaRevisionError(
            "calendar_deliveries lacks owner-matching calendar ownership"
        )

    focus_checks = {
        str(constraint.get("name") or "")
        for constraint in inspector.get_check_constraints("focus_sessions")
    }
    required_focus_checks = {
        "ck_focus_sessions_state",
        "ck_focus_sessions_elapsed",
        "ck_focus_sessions_version",
    }
    if not required_focus_checks.issubset(focus_checks):
        raise SchemaRevisionError(
            "focus_sessions lacks required state/elapsed/version checks"
        )

    principal_tables = (
        "life_sources",
        "life_entities",
        "life_entity_versions",
        "entity_links",
        "action_policies",
        "action_proposals",
        "focus_sessions",
        "contact_sources",
        "contact_records",
        "contact_deliveries",
        "contact_import_runs",
        "calendars",
        "calendar_events",
        "calendar_action_undos",
        "calendar_deliveries",
        "email_outbound_drafts",
        "email_outbound_deliveries",
        "email_life_projection_ledger",
        "email_life_projection_import_runs",
        "email_tag_states",
        "email_automation_rules",
        "email_scheduled_deliveries",
        "email_automation_runs",
        "email_runtime_import_runs",
    )
    for table_name in principal_tables:
        if not _has_foreign_key(
            inspector,
            table_name,
            constrained=("owner_id",),
            referred_table="accounts",
            referred=("id",),
            ondelete="CASCADE",
        ):
            raise SchemaRevisionError(
                f"{table_name} lacks the account ownership cascade constraint"
            )

    for table_name in (
        "telegram_principals",
        "telegram_conversation_bindings",
        "telegram_link_codes",
    ):
        if not _has_foreign_key(
            inspector,
            table_name,
            constrained=("account_id",),
            referred_table="accounts",
            referred=("id",),
            ondelete="CASCADE",
        ):
            raise SchemaRevisionError(
                f"{table_name} lacks immutable account ownership"
            )

    telegram_binding_owner_fk = any(
        tuple(foreign_key.get("constrained_columns") or ())
        == ("principal_id", "account_id")
        and str(foreign_key.get("referred_table") or "")
        == "telegram_principals"
        and tuple(foreign_key.get("referred_columns") or ())
        == ("id", "account_id")
        and str(
            (foreign_key.get("options") or {}).get("ondelete") or ""
        ).upper() == "CASCADE"
        for foreign_key in inspector.get_foreign_keys(
            "telegram_conversation_bindings"
        )
    )
    if not telegram_binding_owner_fk:
        raise SchemaRevisionError(
            "telegram_conversation_bindings lacks owner-matching principal ownership"
        )

    if not _has_foreign_key(
        inspector,
        "action_proposals",
        constrained=("approved_by_account_id",),
        referred_table="accounts",
        referred=("id",),
        ondelete="SET NULL",
    ):
        raise SchemaRevisionError(
            "action_proposals lacks the approver account constraint"
        )

    required_uniques: dict[str, set[tuple[str, ...]]] = {
        "life_sources": {("owner_id", "idempotency_key")},
        "life_entities": {
            ("id", "owner_id"),
            ("owner_id", "idempotency_key"),
            (
                "owner_id",
                "entity_type",
                "domain_ref_type",
                "domain_ref_id",
            ),
        },
        "life_entity_versions": {("entity_id", "version")},
        "entity_links": {
            (
                "owner_id",
                "source_type",
                "source_id",
                "relation",
                "target_type",
                "target_id",
            )
        },
        "action_policies": {("owner_id", "domain")},
        "action_proposals": {
            ("id", "owner_id"),
            ("owner_id", "idempotency_key"),
            ("confirmation_digest",),
        },
        "contact_sources": {("id", "owner_id")},
        "contact_records": {
            ("id", "owner_id"),
            ("source_id", "remote_uid_digest"),
        },
        "contact_deliveries": {("owner_id", "idempotency_key")},
        "contact_import_runs": {("source_kind",)},
        "calendars": {("id", "owner_id")},
        "calendar_events": {
            ("uid", "owner_id"),
            ("uid", "calendar_id"),
        },
        "planning_items": {("calendar_event_uid", "calendar_id")},
        "calendar_action_undos": {
            ("id", "owner_id"),
            ("owner_id", "proposal_id"),
        },
        "calendar_deliveries": {("owner_id", "idempotency_key")},
        "email_outbound_drafts": {
            ("id", "owner_id"),
            ("owner_id", "proposal_id"),
        },
        "email_outbound_deliveries": {
            ("id", "owner_id"),
            ("owner_id", "proposal_id"),
            ("owner_id", "idempotency_key"),
            ("claim_token_digest",),
        },
        "telegram_principals": {
            ("id", "account_id"),
            ("bot_fingerprint", "chat_id_digest"),
        },
        "telegram_conversation_bindings": {("principal_id",)},
        "telegram_link_codes": {("bot_fingerprint", "code_digest")},
        "telegram_identity_import_runs": {("source_kind",)},
        "telegram_dead_letters": {("bot_fingerprint", "update_id")},
        "telegram_inbound_updates": {("bot_fingerprint", "update_id")},
        "telegram_runtime_import_runs": {
            ("bot_fingerprint", "source_kind")
        },
        "email_life_projection_ledger": {
            (
                "owner_id", "account_key", "folder", "message_uid",
                "header_sha256",
            ),
            ("claim_token_digest",),
        },
        "email_life_projection_import_runs": {
            ("owner_id", "source_kind", "source_sha256"),
        },
        "email_tag_states": {
            ("owner_id", "account_key", "message_digest"),
            ("owner_id", "account_key", "location_digest"),
        },
        "email_automation_rules": {("owner_id", "account_key")},
        "email_scheduled_deliveries": {
            ("owner_id", "idempotency_key"),
            ("claim_token_digest",),
        },
        "email_automation_runs": {
            ("owner_id", "account_key", "operation", "message_digest"),
            ("claim_token_digest",),
        },
        "email_runtime_import_runs": {
            ("owner_id", "source_kind", "source_sha256"),
        },
        "profile_configurations": {
            ("owner_id", "namespace", "key"),
        },
        "profile_configuration_mutations": {
            ("owner_id", "idempotency_digest"),
        },
        "profile_configuration_import_runs": {
            ("owner_id", "source_kind", "source_sha256"),
        },
        "chat_upload_metadata": {
            ("owner_id", "content_digest"),
        },
        "chat_upload_metadata_import_runs": {
            ("owner_id", "source_kind", "source_sha256"),
        },
    }
    for table_name, expected in required_uniques.items():
        present = _table_unique_sets(inspector, table_name)
        missing_unique = expected - present
        if missing_unique:
            rendered = ", ".join("/".join(value) for value in sorted(missing_unique))
            raise SchemaRevisionError(
                f"{table_name} lacks required uniqueness: {rendered}"
            )

    required_v3_checks: dict[str, set[str]] = {
        "life_sources": {"ck_life_sources_version"},
        "life_entities": {
            "ck_life_entities_type",
            "ck_life_entities_confidence",
            "ck_life_entities_version",
        },
        "life_entity_versions": {"ck_life_entity_versions_version"},
        "action_policies": {
            "ck_action_policies_autonomy",
            "ck_action_policies_version",
        },
        "action_proposals": {
            "ck_action_proposals_autonomy",
            "ck_action_proposals_state",
            "ck_action_proposals_approver_owner",
            "ck_action_proposals_version",
        },
        "contact_sources": {
            "ck_contact_sources_kind",
            "ck_contact_sources_sync_state",
            "ck_contact_sources_config_version",
            "ck_contact_sources_version",
        },
        "contact_records": {"ck_contact_records_version"},
        "contact_deliveries": {
            "ck_contact_deliveries_operation",
            "ck_contact_deliveries_state",
            "ck_contact_deliveries_attempts",
            "ck_contact_deliveries_version",
        },
        "contact_import_runs": {"ck_contact_import_runs_state"},
        "calendars": {"ck_calendars_config_version"},
        "calendar_events": {"ck_calendar_events_version"},
        "calendar_action_undos": {
            "ck_calendar_action_undos_operation",
            "ck_calendar_action_undos_state",
            "ck_calendar_action_undos_event_version",
            "ck_calendar_action_undos_graph_version",
            "ck_calendar_action_undos_version",
        },
        "calendar_deliveries": {
            "ck_calendar_deliveries_operation",
            "ck_calendar_deliveries_state",
            "ck_calendar_deliveries_attempts",
            "ck_calendar_deliveries_event_version",
            "ck_calendar_deliveries_config_version",
            "ck_calendar_deliveries_version",
        },
        "email_outbound_drafts": {
            "ck_email_outbound_drafts_kind",
            "ck_email_outbound_drafts_state",
            "ck_email_outbound_drafts_version",
        },
        "email_outbound_deliveries": {
            "ck_email_outbound_deliveries_state",
            "ck_email_outbound_deliveries_attempts",
            "ck_email_outbound_deliveries_version",
        },
        "telegram_principals": {
            "ck_telegram_principals_state",
            "ck_telegram_principals_digests",
            "ck_telegram_principals_version",
        },
        "telegram_conversation_bindings": {
            "ck_telegram_conversation_bindings_version",
        },
        "telegram_link_codes": {
            "ck_telegram_link_codes_digest_scheme",
            "ck_telegram_link_codes_digests",
        },
        "telegram_identity_import_runs": {
            "ck_telegram_identity_import_runs_state",
            "ck_telegram_identity_import_runs_digest",
        },
        "telegram_polling_states": {
            "ck_telegram_polling_states_fingerprint",
            "ck_telegram_polling_states_offset",
            "ck_telegram_polling_states_attempts",
            "ck_telegram_polling_states_fencing",
        },
        "telegram_dead_letters": {
            "ck_telegram_dead_letters_resolution",
        },
        "telegram_inbound_updates": {
            "ck_telegram_inbound_updates_status",
            "ck_telegram_inbound_updates_version",
        },
        "telegram_runtime_import_runs": {
            "ck_telegram_runtime_import_runs_state",
            "ck_telegram_runtime_import_runs_digests",
        },
        "email_life_projection_ledger": {
            "ck_email_life_projection_state",
            "ck_email_life_projection_attempts",
            "ck_email_life_projection_header_digest",
            "ck_email_life_projection_routing_identity",
            "ck_email_life_projection_claim_digest",
            "ck_email_life_projection_payload_lifecycle",
            "ck_email_life_projection_lease_lifecycle",
            "ck_email_life_projection_version",
        },
        "email_life_projection_import_runs": {
            "ck_email_life_projection_import_state",
            "ck_email_life_projection_import_digest",
        },
        "email_tag_states": {
            "ck_email_tag_states_digests",
            "ck_email_tag_states_version",
        },
        "email_automation_rules": {
            "ck_email_automation_rules_scope",
            "ck_email_automation_rules_version",
        },
        "email_scheduled_deliveries": {
            "ck_email_scheduled_deliveries_state",
            "ck_email_scheduled_deliveries_attempts",
            "ck_email_scheduled_deliveries_payload_digest",
            "ck_email_scheduled_deliveries_claim_digest",
            "ck_email_scheduled_deliveries_lease_lifecycle",
            "ck_email_scheduled_deliveries_version",
        },
        "email_automation_runs": {
            "ck_email_automation_runs_operation",
            "ck_email_automation_runs_state",
            "ck_email_automation_runs_attempts",
            "ck_email_automation_runs_message_digest",
            "ck_email_automation_runs_claim_digest",
            "ck_email_automation_runs_lease_lifecycle",
            "ck_email_automation_runs_version",
        },
        "email_runtime_import_runs": {
            "ck_email_runtime_import_runs_state",
            "ck_email_runtime_import_runs_digest",
        },
        "profile_configurations": {
            "ck_profile_configuration_namespace",
            "ck_profile_configuration_visibility",
            "ck_profile_configuration_state",
            "ck_profile_configuration_source",
            "ck_profile_configuration_payload_visibility",
            "ck_profile_configuration_version",
            "ck_profile_configuration_delete_state",
        },
        "profile_configuration_mutations": {
            "ck_profile_configuration_mutation_operation",
            "ck_profile_configuration_mutation_digests",
            "ck_profile_configuration_mutation_version",
        },
        "profile_configuration_import_runs": {
            "ck_profile_configuration_import_source_kind",
            "ck_profile_configuration_import_state",
            "ck_profile_configuration_import_digest",
            "ck_profile_configuration_import_counts",
            "ck_profile_configuration_import_version",
        },
        "chat_upload_metadata": {
            "ck_chat_upload_metadata_content_digest",
            "ck_chat_upload_metadata_state",
            "ck_chat_upload_metadata_delete_state",
            "ck_chat_upload_metadata_version",
        },
        "chat_upload_metadata_import_runs": {
            "ck_chat_upload_metadata_import_source_kind",
            "ck_chat_upload_metadata_import_state",
            "ck_chat_upload_metadata_import_digest",
            "ck_chat_upload_metadata_import_counts",
            "ck_chat_upload_metadata_import_version",
        },
    }
    for table_name, expected in required_v3_checks.items():
        present = {
            str(constraint.get("name") or "")
            for constraint in inspector.get_check_constraints(table_name)
        }
        if not expected.issubset(present):
            raise SchemaRevisionError(
                f"{table_name} lacks required database checks"
            )

    telegram_code_indexes = {
        str(index.get("name") or ""): index
        for index in inspector.get_indexes("telegram_link_codes")
    }
    live_code_index = telegram_code_indexes.get(
        "uq_telegram_link_codes_account_live"
    )
    live_code_predicate = ""
    if live_code_index is not None:
        predicate_value = (
            live_code_index.get("dialect_options") or {}
        ).get(f"{engine.dialect.name}_where")
        live_code_predicate = (
            "" if predicate_value is None else str(predicate_value).upper()
        )
    if (
        live_code_index is None
        or not bool(live_code_index.get("unique"))
        or tuple(live_code_index.get("column_names") or ())
        != ("account_id", "bot_fingerprint")
        or "CONSUMED_AT IS NULL" not in live_code_predicate
        or "INVALIDATED_AT IS NULL" not in live_code_predicate
    ):
        raise SchemaRevisionError(
            "telegram_link_codes lacks one-live-code-per-account uniqueness"
        )

    contact_source_indexes = {
        str(index.get("name") or ""): index
        for index in inspector.get_indexes("contact_sources")
    }
    def exact_kind_index(name: str, kind: str) -> bool:
        index = contact_source_indexes.get(name)
        if index is None or not bool(index.get("unique")):
            return False
        if tuple(index.get("column_names") or ()) != ("owner_id",):
            return False
        dialect_options = index.get("dialect_options") or {}
        predicate = dialect_options.get(f"{engine.dialect.name}_where")
        return _contact_kind_predicate_matches(
            predicate,
            expected_kind=kind,
            dialect=engine.dialect.name,
        )

    if not exact_kind_index("uq_contact_sources_owner_local", "local"):
        raise SchemaRevisionError(
            "contact_sources lacks one-local-source-per-owner uniqueness"
        )
    if not exact_kind_index("uq_contact_sources_owner_carddav", "carddav"):
        raise SchemaRevisionError(
            "contact_sources lacks one-CardDAV-source-per-owner uniqueness"
        )

    calendar_delivery_indexes = {
        str(index.get("name") or ""): index
        for index in inspector.get_indexes("calendar_deliveries")
    }
    event_order = calendar_delivery_indexes.get(
        "ix_calendar_deliveries_event_order"
    )
    if (
        event_order is None
        or bool(event_order.get("unique"))
        or tuple(event_order.get("column_names") or ())
        != ("owner_id", "event_uid", "created_at", "id")
    ):
        raise SchemaRevisionError(
            "calendar_deliveries lacks the owner-scoped event FIFO index"
        )

    focus_indexes = {
        str(index.get("name") or ""): index
        for index in inspector.get_indexes("focus_sessions")
    }
    live_index = focus_indexes.get("uq_focus_sessions_owner_live")
    live_predicate = ""
    if live_index is not None:
        dialect_options = live_index.get("dialect_options") or {}
        predicate_value = dialect_options.get(
            f"{engine.dialect.name}_where"
        )
        live_predicate = (
            "" if predicate_value is None else str(predicate_value).upper()
        )
    if (
        live_index is None
        or not bool(live_index.get("unique"))
        or tuple(live_index.get("column_names") or ()) != ("owner_id",)
        or "STATE" not in live_predicate
        or "ACTIVE" not in live_predicate
        or "PAUSED" not in live_predicate
    ):
        raise SchemaRevisionError(
            "focus_sessions lacks the one-live-session partial unique index"
        )

    if engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            trigger_rows = connection.execute(text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name='action_audit'"
            )).all()
        trigger_sql = {
            str(row[0]): " ".join(str(row[1] or "").upper().split())
            for row in trigger_rows
        }
        update_guard = trigger_sql.get("action_audit_no_update", "")
        delete_guard = trigger_sql.get("action_audit_no_delete", "")
        if "BEFORE UPDATE ON ACTION_AUDIT" not in update_guard or "RAISE(ABORT" not in update_guard:
            raise SchemaRevisionError("action_audit update guard is missing or invalid")
        if "BEFORE DELETE ON ACTION_AUDIT" not in delete_guard or "RAISE(ABORT" not in delete_guard:
            raise SchemaRevisionError("action_audit delete guard is missing or invalid")
    elif engine.dialect.name == "postgresql":
        with engine.connect() as connection:
            definitions = {
                str(row[0]): str(row[1]).upper()
                for row in connection.execute(text("""
                    SELECT trigger_name, action_statement
                    FROM information_schema.triggers
                    WHERE event_object_table = 'action_audit'
                """)).all()
            }
        definition = definitions.get("action_audit_no_update_or_delete", "")
        if "RESTIA_REJECT_ACTION_AUDIT_MUTATION" not in definition:
            raise SchemaRevisionError("action_audit PostgreSQL guard is missing")

    if engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            version_trigger_rows = connection.execute(text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name='life_entity_versions'"
            )).all()
        version_trigger_sql = {
            str(row[0]): " ".join(str(row[1] or "").upper().split())
            for row in version_trigger_rows
        }
        update_guard = version_trigger_sql.get("life_entity_versions_no_update", "")
        delete_guard = version_trigger_sql.get("life_entity_versions_no_delete", "")
        if (
            "BEFORE UPDATE ON LIFE_ENTITY_VERSIONS" not in update_guard
            or "RAISE(ABORT" not in update_guard
        ):
            raise SchemaRevisionError(
                "life_entity_versions update guard is missing or invalid"
            )
        if (
            "BEFORE DELETE ON LIFE_ENTITY_VERSIONS" not in delete_guard
            or "RAISE(ABORT" not in delete_guard
        ):
            raise SchemaRevisionError(
                "life_entity_versions delete guard is missing or invalid"
            )
    elif engine.dialect.name == "postgresql":
        with engine.connect() as connection:
            version_definitions = {
                str(row[0]): str(row[1]).upper()
                for row in connection.execute(text("""
                    SELECT trigger_name, action_statement
                    FROM information_schema.triggers
                    WHERE event_object_table = 'life_entity_versions'
                """)).all()
            }
        version_definition = version_definitions.get(
            "life_entity_versions_no_update_or_delete", ""
        )
        if "RESTIA_REJECT_LIFE_ENTITY_VERSION_MUTATION" not in version_definition:
            raise SchemaRevisionError(
                "life_entity_versions PostgreSQL guard is missing"
            )

    if engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            edge_trigger_rows = connection.execute(text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name='entity_links'"
            )).all()
        edge_trigger_sql = {
            str(row[0]): " ".join(str(row[1] or "").upper().split())
            for row in edge_trigger_rows
        }
        edge_checks = {
            str(constraint.get("name") or "")
            for constraint in inspector.get_check_constraints("entity_links")
        }
        required_edge_checks = {
            "ck_entity_links_confidence",
            "ck_entity_links_version",
        }
        validation_fragments = {
            "entity_links_validate_insert": (
                "BEFORE INSERT ON ENTITY_LINKS",
                "NEW.CONFIDENCE < 0",
                "NEW.VERSION < 1",
                "RAISE(ABORT",
            ),
            "entity_links_validate_update": (
                "BEFORE UPDATE ON ENTITY_LINKS",
                "NEW.CONFIDENCE < 0",
                "NEW.VERSION < 1",
                "RAISE(ABORT",
            ),
        }
        # A fresh ORM-created SQLite table has native CHECK constraints. The
        # additive 0003/legacy path uses equivalent triggers because SQLite
        # cannot add those constraints without rebuilding the whole table.
        if not required_edge_checks.issubset(edge_checks):
            for trigger_name, fragments in validation_fragments.items():
                definition = edge_trigger_sql.get(trigger_name, "")
                if any(fragment not in definition for fragment in fragments):
                    raise SchemaRevisionError(
                        "entity_links confidence/version guard is missing or "
                        f"invalid: {trigger_name}"
                    )
        updated_at = next(
            column
            for column in inspector.get_columns("entity_links")
            if str(column.get("name")) == "updated_at"
        )
        default = str(updated_at.get("default") or "").upper()
        if "1970-01-01 00:00:00" in default:
            definition = edge_trigger_sql.get(
                "entity_links_fill_updated_at", ""
            )
            required = (
                "AFTER INSERT ON ENTITY_LINKS",
                "NEW.UPDATED_AT = '1970-01-01 00:00:00'",
                "SET UPDATED_AT = CURRENT_TIMESTAMP",
            )
            if any(fragment not in definition for fragment in required):
                raise SchemaRevisionError(
                    "entity_links updated_at sentinel guard is missing or invalid"
                )
    elif engine.dialect.name == "postgresql":
        edge_checks = {
            str(constraint.get("name") or "")
            for constraint in inspector.get_check_constraints("entity_links")
        }
        required_edge_checks = {
            "ck_entity_links_confidence",
            "ck_entity_links_version",
        }
        if not required_edge_checks.issubset(edge_checks):
            raise SchemaRevisionError(
                "entity_links lacks confidence/version database checks"
            )
        updated_at = next(
            column
            for column in inspector.get_columns("entity_links")
            if str(column.get("name")) == "updated_at"
        )
        default = str(updated_at.get("default") or "").upper()
        if "CURRENT_TIMESTAMP" not in default and "NOW()" not in default:
            raise SchemaRevisionError(
                "entity_links.updated_at lacks a current-time server default"
            )


def _stamp_revision(engine: Engine, revision: str) -> None:
    try:
        from alembic import command
    except ImportError as exc:
        raise SchemaRevisionError(
            "Alembic is not installed; install Restia's requirements first"
        ) from exc
    config = _alembic_config(str(engine.url))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.stamp(config, revision)


def upgrade_schema(engine: Engine) -> SchemaRevisionStatus:
    """Upgrade an empty or executable known-behind engine to schema head."""

    status = schema_revision_status(engine)
    application_tables = application_table_names(engine)
    if status.state == "unknown":
        current = ", ".join(status.current_revisions) or "unknown"
        raise SchemaRevisionError(f"Unknown Alembic revision state: {current}")
    if status.matches_expected:
        validate_head_schema(engine)
        return status
    executable_upgrade = status.current_revisions in {
        (EXPLICIT_BASELINE_REVISION,),
        ("20260718_0003",),
        ("20260719_0004",),
        ("20260720_0005",),
        ("20260721_0006",),
        ("20260722_0007",),
        ("20260723_0008",),
        ("20260724_0009",),
        ("20260725_0010",),
        ("20260726_0011",),
        ("20260727_0012",),
        ("20260728_0013",),
        ("20260729_0014",),
    }
    if application_tables and not executable_upgrade:
        raise SchemaRevisionError(
            "A non-empty legacy database must use the verified adoption path"
        )
    if status.state == "unstamped":
        _stamp_revision(engine, LEGACY_BASELINE_REVISION)

    try:
        from alembic import command
    except ImportError as exc:
        raise SchemaRevisionError(
            "Alembic is not installed; install Restia's requirements first"
        ) from exc
    config = _alembic_config(str(engine.url))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, SCHEMA_HEAD_REVISION)
    result = assert_schema_revision(engine)
    validate_head_schema(engine)
    return result


def stamp_legacy_baseline() -> SchemaRevisionStatus:
    """Stamp a repaired, verified local SQLite database directly at head."""

    config = validate_database_mode(require_schema_authority=False)
    if config.mode != "local-single" or config.dialect != "sqlite":
        raise SchemaRevisionError(
            "Legacy schema adoption is only supported for local-single SQLite"
        )
    from core.database import engine

    before = schema_revision_status(engine)
    if before.state == "unknown":
        current = ", ".join(before.current_revisions) or "unknown"
        raise SchemaRevisionError(
            f"Refusing to replace unknown Alembic revision state: {current}"
        )
    validate_head_schema(engine)
    if before.matches_expected:
        return before
    _stamp_revision(engine, SCHEMA_HEAD_REVISION)
    result = assert_schema_revision(engine)
    validate_head_schema(engine)
    return result
