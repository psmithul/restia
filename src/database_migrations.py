"""Immutable Alembic revision handling and guarded legacy schema adoption."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
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
from src.database_runtime import validate_database_mode


LEGACY_BASELINE_REVISION = "20260716_0001"
EXPLICIT_BASELINE_REVISION = "20260717_0002"
SCHEMA_HEAD_REVISION = "20260718_0003"
KNOWN_BEHIND_REVISIONS = frozenset({
    LEGACY_BASELINE_REVISION,
    EXPLICIT_BASELINE_REVISION,
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

    from core.database import engine

    mode_error = None
    try:
        config = validate_database_mode(require_schema_authority=False)
        mode = config.mode
        dialect = config.dialect
        schema_authority = config.schema_authority
    except Exception as exc:
        mode_error = str(exc)
        mode = "invalid"
        dialect = engine.dialect.name
        schema_authority = "alembic-20260718"

    revision_error = None
    try:
        revision = schema_revision_status(engine).as_dict()
    except Exception as exc:
        # Status must remain useful when a configured remote database is down,
        # without echoing a connection string or credentials into logs/JSON.
        revision_error = f"{type(exc).__name__}: database revision query failed"
        revision = {
            "expected_revision": SCHEMA_HEAD_REVISION,
            "current_revisions": [],
            "state": "unavailable",
            "matches_expected": False,
        }
    return {
        "database_mode": mode,
        "dialect": dialect,
        "schema_authority": schema_authority,
        "shared_schema_ready": False,
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


def validate_head_schema(engine: Engine) -> None:
    """Verify frozen baseline plus every reviewed post-baseline contract."""

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    required_tables = BASELINE_REQUIRED_TABLES | V3_REQUIRED_TABLES
    required_columns = {**BASELINE_REQUIRED_COLUMNS, **V3_REQUIRED_COLUMNS}
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
            ("owner_id", "idempotency_key"),
            ("confirmation_digest",),
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
    executable_upgrade = status.current_revisions == (EXPLICIT_BASELINE_REVISION,)
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
