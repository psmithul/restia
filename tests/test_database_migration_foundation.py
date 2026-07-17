from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from src import database_runtime
from src.database_migrations import (
    EXPLICIT_BASELINE_REVISION,
    LEGACY_BASELINE_REVISION,
    SCHEMA_HEAD_REVISION,
    SchemaRevisionError,
    _stored_encrypted_json_envelope,
    assert_schema_revision,
    schema_revision_status,
    stamp_legacy_baseline,
    upgrade_schema,
)
from src.database_runtime import DatabaseConfigurationError, validate_database_mode


ROOT = Path(__file__).resolve().parent.parent


def _create_pushed_0001_legacy_database(engine) -> None:
    """Create the schema shape that the pushed stamp-only 0001 recorded."""

    import sqlite3

    from core.database import Base

    Base.metadata.create_all(engine)
    database_path = str(engine.url.database)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.executescript("""
            DROP TABLE focus_sessions;
            DROP TABLE action_proposals;
            DROP TABLE action_policies;
            DROP TABLE life_entity_versions;
            DROP TABLE life_entities;
            DROP TABLE life_sources;
            DROP TABLE entity_links;
            DROP TABLE auth_sessions;
            DROP TABLE mfa_recovery_codes;
            DROP TABLE mfa_factors;
            DROP TABLE local_credentials;
            DROP TABLE account_roles;
            DROP TABLE account_capabilities;
            DROP TABLE retired_auth_subjects;
            DROP TABLE auth_import_runs;
            DROP TABLE auth_policy;
            DROP TABLE auth_identities;
            DROP TABLE api_tokens;
            DROP TABLE accounts;

            CREATE TABLE accounts (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                username VARCHAR(160) NOT NULL,
                display_name VARCHAR(160),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE UNIQUE INDEX ix_accounts_username ON accounts(username);

            CREATE TABLE auth_identities (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                account_id VARCHAR(36) NOT NULL,
                provider VARCHAR(32) NOT NULL,
                subject VARCHAR(255) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_auth_identity_subject UNIQUE(provider, subject),
                FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
            );
            CREATE INDEX ix_auth_identities_account_id
                ON auth_identities(account_id);
            CREATE INDEX ix_auth_identity_account_provider
                ON auth_identities(account_id, provider);

            CREATE TABLE entity_links (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                owner_id VARCHAR(36) NOT NULL,
                source_type VARCHAR(48) NOT NULL,
                source_id VARCHAR(255) NOT NULL,
                relation VARCHAR(64) NOT NULL,
                target_type VARCHAR(48) NOT NULL,
                target_id VARCHAR(255) NOT NULL,
                metadata JSON NOT NULL,
                created_at DATETIME NOT NULL,
                CONSTRAINT uq_entity_link_edge UNIQUE (
                    owner_id, source_type, source_id, relation,
                    target_type, target_id
                ),
                FOREIGN KEY(owner_id) REFERENCES accounts(id) ON DELETE CASCADE
            );
            CREATE INDEX ix_entity_links_owner_id ON entity_links(owner_id);
            CREATE INDEX ix_entity_links_source
                ON entity_links(owner_id, source_type, source_id);
            CREATE INDEX ix_entity_links_target
                ON entity_links(owner_id, target_type, target_id);

            CREATE TABLE api_tokens (
                id VARCHAR NOT NULL PRIMARY KEY,
                owner VARCHAR,
                name VARCHAR NOT NULL,
                token_hash VARCHAR NOT NULL,
                token_prefix VARCHAR NOT NULL,
                scopes VARCHAR NOT NULL DEFAULT 'chat',
                is_active BOOLEAN,
                last_used_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            CREATE INDEX ix_api_tokens_id ON api_tokens(id);
            CREATE INDEX ix_api_tokens_owner ON api_tokens(owner);

            CREATE TABLE alembic_version (
                version_num VARCHAR(32) NOT NULL PRIMARY KEY
            );
            INSERT INTO alembic_version(version_num)
                VALUES ('20260716_0001');
        """)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        connection.commit()
    finally:
        connection.close()


def _shared_env(**overrides: str) -> dict[str, str]:
    env = {
        "RESTIA_DATABASE_MODE": "shared",
        "DATABASE_URL": "postgresql+psycopg://restia@db/restia",
        "AUTH_ENABLED": "true",
        "LOCALHOST_BYPASS": "false",
        "RESTIA_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
    }
    env.update(overrides)
    return env


def test_importing_core_database_does_not_create_or_migrate_schema(tmp_path):
    db_path = tmp_path / "import-only.db"
    data_dir = tmp_path / "data"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_path}",
            "RESTIA_DATA_DIR": str(data_dir),
            "RESTIA_DATABASE_MODE": "local-single",
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import core.database; print('imported')",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("imported")
    assert not db_path.exists()


def test_initialize_database_runs_alembic_once_per_engine(monkeypatch):
    import core.database as database
    import src.database_migrations as database_migrations

    calls = []
    test_engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(
        database_migrations,
        "upgrade_schema",
        lambda _engine: calls.append("upgrade"),
    )
    monkeypatch.setattr(database, "harden_database_permissions", lambda: None)
    monkeypatch.setattr(database_runtime, "_initialized_binding", None)
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "local-single")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")

    database_runtime.initialize_database()
    database_runtime.initialize_database()

    assert calls == ["upgrade"]
    test_engine.dispose()


def test_app_bootstrap_precedes_component_initialization():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert source.index("initialize_database()") < source.index("initialize_managers(")


def test_fresh_data_import_app_creates_schema_before_consumers(tmp_path):
    db_path = tmp_path / "fresh-app.db"
    data_dir = tmp_path / "data"
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{db_path}",
            "RESTIA_DATA_DIR": str(data_dir),
            "RESTIA_DATABASE_MODE": "local-single",
            "AUTH_ENABLED": "false",
            "LOCALHOST_BYPASS": "false",
            "RESTIA_STARTUP_WARMUPS": "0",
            "RESTIA_MODEL_KEEPALIVE": "0",
            "RESTIA_INPROCESS_TASKS": "0",
            "RESTIA_INPROCESS_POLLERS": "0",
            "RESTIA_INPROCESS_TELEGRAM": "0",
            # This test exercises database-first app import, not optional
            # vector services.  Pin ChromaDB to the reserved local port 0 so
            # a developer's live service cannot trigger model downloads into
            # the deliberately empty per-test cache and make startup hang.
            "CHROMADB_HOST": "127.0.0.1",
            "CHROMADB_PORT": "0",
            "CHROMADB_CONNECT_TIMEOUT": "0.05",
            "ODYSSEUS_BRAIN_MEMORY_ENABLED": "0",
            "FASTEMBED_CACHE_PATH": str(tmp_path / "fastembed"),
            "MNEMOSYNE_DATA_DIR": str(tmp_path / "mnemosyne"),
        }
    )
    code = (
        "import sqlite3; import app; "
        f"c=sqlite3.connect({str(db_path)!r}); "
        "names={r[0] for r in c.execute(\"SELECT name FROM sqlite_master "
        "WHERE type='table'\")}; c.close(); "
        "assert {'sessions','accounts','inbox_items'} <= names; print('ready')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ready")


@pytest.mark.parametrize(
    ("env", "message"),
    [
        (
            _shared_env(DATABASE_URL="sqlite:///shared.db"),
            "requires a PostgreSQL DATABASE_URL",
        ),
        (
            _shared_env(AUTH_ENABLED="false"),
            "requires AUTH_ENABLED=true",
        ),
        (
            _shared_env(LOCALHOST_BYPASS="true"),
            "requires LOCALHOST_BYPASS=false",
        ),
        (
            {
                key: value
                for key, value in _shared_env().items()
                if key != "RESTIA_ENCRYPTION_KEY"
            },
            "requires RESTIA_ENCRYPTION_KEY",
        ),
    ],
)
def test_shared_mode_security_prerequisites_fail_closed(env, message):
    with pytest.raises(DatabaseConfigurationError, match=message):
        validate_database_mode(environ=env)


def test_shared_mode_refuses_startup_until_alembic_is_authoritative():
    with pytest.raises(DatabaseConfigurationError, match="not available yet"):
        validate_database_mode(environ=_shared_env())


def test_shared_mode_diagnostic_validation_can_report_prerequisites():
    config = validate_database_mode(
        environ=_shared_env(),
        require_schema_authority=False,
    )
    assert config.mode == "shared"
    assert config.dialect == "postgresql"
    assert config.encryption_key_source == "RESTIA_ENCRYPTION_KEY"


def test_shared_mode_wraps_non_ascii_inline_key_as_configuration_error():
    with pytest.raises(DatabaseConfigurationError, match="valid Fernet"):
        validate_database_mode(
            environ=_shared_env(RESTIA_ENCRYPTION_KEY="not-ascii-é"),
            require_schema_authority=False,
        )


def test_local_single_mode_rejects_non_sqlite_database():
    with pytest.raises(DatabaseConfigurationError, match="requires SQLite"):
        validate_database_mode(
            environ={
                "RESTIA_DATABASE_MODE": "local-single",
                "DATABASE_URL": "postgresql+psycopg://restia@db/restia",
            }
        )


def test_revision_mismatch_is_detected_without_mutating_schema():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('old_revision')")
        )

    status = schema_revision_status(engine)
    assert status.state == "unknown"
    assert status.current_revisions == ("old_revision",)
    with pytest.raises(SchemaRevisionError, match=SCHEMA_HEAD_REVISION):
        assert_schema_revision(engine)


def test_unstamped_revision_status_does_not_create_version_table():
    engine = create_engine("sqlite:///:memory:")
    status = schema_revision_status(engine)
    assert status.state == "unstamped"
    assert status.current_revisions == ()
    assert not inspect(engine).has_table("alembic_version")


def test_known_stamp_only_baseline_reports_behind():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO alembic_version (version_num) VALUES (:revision)"
        ), {"revision": LEGACY_BASELINE_REVISION})
    status = schema_revision_status(engine)
    assert status.state == "behind"
    assert status.expected_revision == SCHEMA_HEAD_REVISION


def test_empty_version_table_and_empty_database_recover_to_head(tmp_path, monkeypatch):
    import core.database as database

    db_path = tmp_path / "downgraded-empty.db"
    test_engine = create_engine(f"sqlite:///{db_path}")
    with test_engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
        ))
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(database_runtime, "_initialized_binding", None)
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "local-single")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    database_runtime.initialize_database()

    assert schema_revision_status(test_engine).matches_expected is True
    assert "accounts" in inspect(test_engine).get_table_names()
    test_engine.dispose()


def test_fake_current_version_table_is_rejected(tmp_path, monkeypatch):
    import core.database as database

    db_path = tmp_path / "fake-current.db"
    test_engine = create_engine(f"sqlite:///{db_path}")
    with test_engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
        ))
        connection.execute(text(
            "INSERT INTO alembic_version (version_num) VALUES (:revision)"
        ), {"revision": SCHEMA_HEAD_REVISION})
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(database_runtime, "_initialized_binding", None)
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "local-single")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")

    with pytest.raises(DatabaseConfigurationError, match="missing baseline tables"):
        database_runtime.initialize_database()
    test_engine.dispose()


def test_legacy_stamp_refuses_partial_schema_before_loading_alembic(monkeypatch):
    import core.database as database

    partial = create_engine("sqlite:///:memory:")
    with partial.begin() as connection:
        connection.execute(text("CREATE TABLE sessions (id VARCHAR PRIMARY KEY)"))
    monkeypatch.setattr(database, "engine", partial)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "local-single")

    with pytest.raises(SchemaRevisionError, match="missing baseline tables"):
        stamp_legacy_baseline()


def test_legacy_stamp_succeeds_and_is_idempotent(tmp_path, monkeypatch):
    import core.database as database
    from core.database import Base

    db_path = tmp_path / "legacy-complete.db"
    legacy = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(legacy)

    monkeypatch.setattr(database, "engine", legacy)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "local-single")
    application_logger = logging.getLogger("restia.tests.alembic-preserves-loggers")
    application_logger.disabled = False

    first = stamp_legacy_baseline()
    second = stamp_legacy_baseline()

    assert first.matches_expected is True
    assert first.current_revisions == (SCHEMA_HEAD_REVISION,)
    assert second == first
    assert application_logger.disabled is False


def test_pushed_0001_database_is_backed_up_repaired_and_adopted(
    tmp_path,
    monkeypatch,
):
    import core.database as database
    from src import secret_storage

    db_path = tmp_path / "pushed-0001.db"
    legacy = create_engine(f"sqlite:///{db_path}")
    _create_pushed_0001_legacy_database(legacy)
    with legacy.begin() as connection:
        connection.execute(text(
            "INSERT INTO accounts "
            "(id, username, display_name, created_at, updated_at) VALUES "
            "('legacy-a', 'legacy-alice', 'Legacy Alice', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO entity_links "
            "(id, owner_id, source_type, source_id, relation, target_type, "
            "target_id, metadata, created_at) VALUES "
            "('legacy-edge', 'legacy-a', 'inbox_item', 'capture', 'supports', "
            "'project', 'project', '{\"private\":\"legacy context\"}', "
            "CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO planning_items "
            "(id, owner, title, details, status, priority, source, version, "
            "created_at, updated_at) VALUES "
            "('legacy-plan', 'legacy-alice', 'Legacy private plan', "
            "'Legacy private details', 'open', 'normal', 'user', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))

    monkeypatch.setattr(database, "engine", legacy)
    monkeypatch.setattr(database, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "SETTINGS_FILE", str(tmp_path / "missing-settings.json"))
    monkeypatch.setattr(database_runtime, "_initialized_binding", None)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("RESTIA_DATABASE_MODE", "local-single")
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)

    database_runtime.initialize_database()

    assert schema_revision_status(legacy).current_revisions == (SCHEMA_HEAD_REVISION,)
    schema = inspect(legacy)
    assert {constraint["name"] for constraint in schema.get_check_constraints(
        "accounts"
    )} >= {"ck_accounts_status", "ck_accounts_auth_epoch"}
    with legacy.connect() as connection:
        assert any(
            row[2] == "accounts"
            and row[3] == "account_id"
            and row[4] == "id"
            and row[6] == "CASCADE"
            for row in connection.exec_driver_sql(
                'PRAGMA foreign_key_list("api_tokens")'
            ).fetchall()
        )
        encrypted_edge_json = connection.execute(text(
            "SELECT metadata, provenance FROM entity_links "
            "WHERE id='legacy-edge'"
        )).one()
        encrypted_planning = connection.execute(text(
            "SELECT title, details FROM planning_items WHERE id='legacy-plan'"
        )).one()
    assert all(str(value).startswith('"enc:c1:') for value in encrypted_edge_json)
    assert "legacy context" not in " ".join(map(str, encrypted_edge_json))
    assert all(str(value).startswith("enc:c1:") for value in encrypted_planning)
    assert "Legacy private" not in " ".join(map(str, encrypted_planning))
    session = sessionmaker(bind=legacy)()
    try:
        adopted_edge = session.get(database.EntityLink, "legacy-edge")
        assert adopted_edge.meta_data == {
            "private": "legacy context"
        }
        assert adopted_edge.provenance == {}
        planning = session.get(database.PlanningItem, "legacy-plan")
        assert planning.title == "Legacy private plan"
        assert planning.details == "Legacy private details"
    finally:
        session.close()
    backups = list(tmp_path.glob(
        f"pushed-0001.db.pre-{SCHEMA_HEAD_REVISION}-*.bak"
    ))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o077 == 0
    legacy.dispose()


def test_baseline_revision_is_explicit_and_not_dynamic_metadata():
    revision = (
        ROOT / "migrations" / "versions" /
        "restia_schema_baseline_20260717_0002.py"
    ).read_text(encoding="utf-8")
    assert "op.create_table('accounts'" in revision
    assert "op.create_table('auth_sessions'" in revision
    assert "restia_reject_action_audit_mutation" in revision
    assert "Base.metadata" not in revision
    assert "create_all(" not in revision


def test_pushed_stamp_only_revision_is_immutable():
    pushed_revision = (
        ROOT / "migrations" / "versions" /
        "20260716_0001_legacy_baseline.py"
    )
    assert hashlib.sha256(pushed_revision.read_bytes()).hexdigest() == (
        "50864b1ba44dc709df2a5eed8abc14aee03bf77b32d185d01b7af3032b643e0b"
    )


def test_baseline_upgrade_creates_complete_schema_and_append_only_guard(
    tmp_path,
    monkeypatch,
):
    from migrations.versions.restia_schema_baseline_20260717_0002 import (
        BASELINE_REQUIRED_TABLES,
    )

    db_path = tmp_path / "fresh-alembic.db"
    database_url = f"sqlite:///{db_path}"
    migrated = create_engine(database_url)
    upgrade_schema(migrated)
    assert BASELINE_REQUIRED_TABLES <= set(inspect(migrated).get_table_names())
    assert {
        "life_sources", "life_entities", "life_entity_versions",
        "action_policies", "action_proposals", "focus_sessions",
    } <= set(inspect(migrated).get_table_names())
    schema = inspect(migrated)
    assert "active_since" in {
        column["name"] for column in schema.get_columns("focus_sessions")
    }
    assert ("id", "owner_id") in {
        tuple(constraint.get("column_names") or ())
        for constraint in schema.get_unique_constraints("life_entities")
    }
    for child_table in ("life_entity_versions", "focus_sessions"):
        assert any(
            tuple(foreign_key.get("constrained_columns") or ())
            == ("entity_id", "owner_id")
            and foreign_key.get("referred_table") == "life_entities"
            and tuple(foreign_key.get("referred_columns") or ())
            == ("id", "owner_id")
            for foreign_key in schema.get_foreign_keys(child_table)
        )
    focus_live_index = next(
        index for index in schema.get_indexes("focus_sessions")
        if index.get("name") == "uq_focus_sessions_owner_live"
    )
    assert focus_live_index["unique"]
    assert focus_live_index["column_names"] == ["owner_id"]
    focus_predicate = str(
        focus_live_index["dialect_options"]["sqlite_where"]
    ).lower()
    assert "state" in focus_predicate
    assert "active" in focus_predicate
    assert "paused" in focus_predicate
    with migrated.begin() as connection:
        connection.execute(text(
            "INSERT INTO accounts "
            "(id, username, status, auth_epoch, created_at, updated_at) "
            "VALUES ('a', 'alice', 'active', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO action_audit "
            "(id, owner_id, action, entity_type, entity_id, before_state, "
            "after_state, details, created_at) VALUES "
            "('e', 'a', 'created', 'test', '1', '{}', '{}', '{}', CURRENT_TIMESTAMP)"
        ))
    with pytest.raises(Exception, match="append-only"):
        with migrated.begin() as connection:
            connection.execute(text(
                "UPDATE action_audit SET action='changed' WHERE id='e'"
            ))
    with migrated.begin() as connection:
        connection.execute(text(
            "INSERT INTO life_entities "
            "(id, owner_id, entity_type, title, summary, status, properties, "
            "provenance, confidence, sensitivity, version, created_at, updated_at) "
            "VALUES ('life-1', 'a', 'task', '', '', 'active', '{}', '{}', "
            "100, 'private', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO life_entity_versions "
            "(id, owner_id, entity_id, version, snapshot, reason, created_at) "
            "VALUES ('life-version-1', 'a', 'life-1', 1, '{}', '', CURRENT_TIMESTAMP)"
        ))
    with pytest.raises(Exception, match="append-only"):
        with migrated.begin() as connection:
            connection.execute(text(
                "DELETE FROM life_entity_versions WHERE id='life-version-1'"
            ))
    assert schema_revision_status(migrated).matches_expected is True
    migrated.dispose()


def test_head_validation_rejects_missing_focus_live_index(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'missing-focus-index.db'}")
    upgrade_schema(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "DROP INDEX uq_focus_sessions_owner_live"
        ))
    with pytest.raises(
        SchemaRevisionError, match="one-live-session partial unique index"
    ):
        upgrade_schema(engine)
    engine.dispose()


def test_head_validation_rejects_missing_v3_policy_invariants(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'invalid-policy-shape.db'}")
    upgrade_schema(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE action_policies"))
        connection.execute(text("""
            CREATE TABLE action_policies (
                id VARCHAR(36) NOT NULL PRIMARY KEY,
                owner_id VARCHAR(36) NOT NULL,
                domain VARCHAR(48) NOT NULL,
                max_autonomy INTEGER NOT NULL,
                external_requires_confirmation BOOLEAN NOT NULL,
                enabled BOOLEAN NOT NULL,
                rules JSON NOT NULL,
                version INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                FOREIGN KEY(owner_id) REFERENCES accounts(id) ON DELETE CASCADE
            )
        """))
    with pytest.raises(
        SchemaRevisionError, match="action_policies lacks required uniqueness"
    ):
        upgrade_schema(engine)

    with engine.begin() as connection:
        connection.execute(text(
            "CREATE UNIQUE INDEX uq_test_action_policy_owner_domain "
            "ON action_policies(owner_id, domain)"
        ))
    with pytest.raises(
        SchemaRevisionError, match="action_policies lacks required database checks"
    ):
        upgrade_schema(engine)
    engine.dispose()


def test_head_validation_rejects_plaintext_planning_content(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'plaintext-at-head.db'}")
    upgrade_schema(engine)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO planning_items "
            "(id, owner, title, details, status, priority, source, version, "
            "created_at, updated_at) VALUES "
            "('plaintext-plan', 'alice', 'private title', 'private details', "
            "'open', 'normal', 'user', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))

    with pytest.raises(
        SchemaRevisionError, match="planning_items contains plaintext content"
    ):
        upgrade_schema(engine)
    engine.dispose()


def _insert_raw_head_entity_link(
    engine,
    metadata: str,
    *,
    provenance: str | None = None,
) -> None:
    from src.secret_storage import encrypt_plaintext

    if provenance is None:
        provenance = json.dumps(encrypt_plaintext(json.dumps({})))
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO accounts "
            "(id, username, status, auth_epoch, created_at, updated_at) "
            "VALUES ('edge-owner', 'edge-owner', 'active', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO entity_links "
            "(id, owner_id, source_type, source_id, relation, target_type, "
            "target_id, metadata, provenance, confidence, sensitivity, version, "
            "created_at, updated_at) VALUES "
            "('head-edge', 'edge-owner', 'inbox_item', 'capture', 'supports', "
            "'project', 'project', :metadata, :provenance, 100, 'private', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), {"metadata": metadata, "provenance": provenance})


def test_head_validation_rejects_plaintext_entity_link_metadata(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'plaintext-edge-at-head.db'}")
    upgrade_schema(engine)
    _insert_raw_head_entity_link(
        engine, json.dumps({"private": "plaintext edge context"})
    )

    with pytest.raises(
        SchemaRevisionError, match="plaintext or invalid metadata"
    ):
        upgrade_schema(engine)
    engine.dispose()


def test_head_validation_rejects_corrupt_entity_link_envelope(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'corrupt-edge-at-head.db'}")
    upgrade_schema(engine)
    _insert_raw_head_entity_link(engine, json.dumps("enc:c1:not-a-token"))

    with pytest.raises(
        SchemaRevisionError, match="plaintext or invalid metadata"
    ):
        upgrade_schema(engine)
    engine.dispose()


def test_head_validation_rejects_plaintext_entity_link_provenance(tmp_path):
    from src.secret_storage import encrypt_plaintext

    engine = create_engine(f"sqlite:///{tmp_path / 'plaintext-edge-source.db'}")
    upgrade_schema(engine)
    _insert_raw_head_entity_link(
        engine,
        json.dumps(encrypt_plaintext(json.dumps({"kind": "edge"}))),
        provenance=json.dumps({"source": "private connector"}),
    )

    with pytest.raises(
        SchemaRevisionError, match="plaintext or invalid provenance"
    ):
        upgrade_schema(engine)
    engine.dispose()


def test_head_validation_rejects_wrong_entity_link_key(tmp_path, monkeypatch):
    from src import secret_storage

    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(f"sqlite:///{tmp_path / 'wrong-edge-key.db'}")
    upgrade_schema(engine)
    envelope = secret_storage.encrypt_plaintext(
        json.dumps({"private": "encrypted edge context"})
    )
    _insert_raw_head_entity_link(engine, json.dumps(envelope))

    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)
    with pytest.raises(SchemaRevisionError, match="active key"):
        upgrade_schema(engine)
    engine.dispose()


def test_head_envelope_decoder_supports_sqlite_and_postgresql_json_strings():
    envelope = "enc:c1:gAAAAABtest-envelope"
    assert _stored_encrypted_json_envelope(
        json.dumps(envelope), dialect="sqlite"
    ) == envelope
    assert _stored_encrypted_json_envelope(
        envelope, dialect="postgresql"
    ) == envelope
    assert _stored_encrypted_json_envelope(
        json.dumps({"plaintext": True}), dialect="sqlite"
    ) is None
    assert _stored_encrypted_json_envelope(
        {"plaintext": True}, dialect="postgresql"
    ) is None


def test_baseline_compiles_for_postgresql_until_online_encryption_revision():
    env = os.environ.copy()
    env["DATABASE_URL"] = (
        "postgresql+psycopg://restia:unused@localhost/restia"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            f"{LEGACY_BASELINE_REVISION}:{EXPLICIT_BASELINE_REVISION}",
            "--sql",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "CREATE TABLE auth_sessions" in result.stdout
    assert "CREATE FUNCTION restia_reject_action_audit_mutation" in result.stdout

    blocked = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            f"{EXPLICIT_BASELINE_REVISION}:head",
            "--sql",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert blocked.returncode != 0
    assert "requires an online Alembic migration" in (blocked.stdout + blocked.stderr)

    revision = (
        ROOT / "migrations" / "versions" /
        "life_planning_spine_20260718_0003.py"
    ).read_text(encoding="utf-8")
    assert "restia_reject_life_entity_version_mutation" in revision
    assert "ck_entity_links_confidence" in revision
    assert "ck_entity_links_version" in revision
    assert "type_=sa.Text()" in revision


def test_executable_0002_upgrades_to_life_spine_without_losing_edges(
    tmp_path, monkeypatch
):
    from alembic import command
    from core.database import EntityLink, PlanningItem
    from src.database_migrations import _alembic_config
    from src import secret_storage

    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)

    db_path = tmp_path / "from-0002.db"
    database_url = f"sqlite:///{db_path}"
    engine = create_engine(database_url)
    config = _alembic_config(database_url)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.stamp(config, LEGACY_BASELINE_REVISION)
        command.upgrade(config, EXPLICIT_BASELINE_REVISION)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO accounts "
            "(id, username, status, auth_epoch, created_at, updated_at) "
            "VALUES ('a', 'alice', 'active', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO entity_links "
            "(id, owner_id, source_type, source_id, relation, target_type, "
            "target_id, metadata, created_at) VALUES "
            "('edge-1', 'a', 'inbox_item', 'capture-1', 'supports', "
            "'project', 'project-1', '{\"private\":\"edge context\"}', "
            "CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO planning_items "
            "(id, owner, title, details, status, priority, source, version, "
            "created_at, updated_at) VALUES "
            "('plan-1', 'alice', 'enc:Private planning title', "
            "'Private planning details', 'open', 'normal', 'user', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))

    upgrade_schema(engine)

    assert schema_revision_status(engine).current_revisions == (SCHEMA_HEAD_REVISION,)
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT metadata, provenance, confidence, sensitivity, version, updated_at "
            "FROM entity_links WHERE id='edge-1'"
        )).one()
        raw_planning = connection.execute(text(
            "SELECT title, details FROM planning_items WHERE id='plan-1'"
        )).one()
    assert "enc:" in str(row[0])
    assert "edge context" not in str(row[0])
    assert "enc:" in str(row[1])
    assert row[2:5] == (100, "private", 1)
    assert row[5] is not None
    assert all(str(value).startswith("enc:") for value in raw_planning)
    assert "Private planning" not in " ".join(map(str, raw_planning))

    session = sessionmaker(bind=engine)()
    try:
        assert session.get(EntityLink, "edge-1").meta_data == {
            "private": "edge context"
        }
        assert session.get(EntityLink, "edge-1").provenance == {}
        planning = session.get(PlanningItem, "plan-1")
        assert planning.title == "enc:Private planning title"
        assert planning.details == "Private planning details"
    finally:
        session.close()

    # Future inserts that omit the additive fields use safe values and a real
    # timestamp rather than the migration sentinel.
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO entity_links "
            "(id, owner_id, source_type, source_id, relation, target_type, "
            "target_id, metadata, created_at) VALUES "
            "('edge-2', 'a', 'life_entity', 'one', 'supports', "
            "'life_entity', 'two', '{}', CURRENT_TIMESTAMP)"
        ))
    with engine.connect() as connection:
        inserted = connection.execute(text(
            "SELECT confidence, version, updated_at FROM entity_links "
            "WHERE id='edge-2'"
        )).one()
    assert inserted[0:2] == (100, 1)
    assert str(inserted[2]) != "1970-01-01 00:00:00"

    with pytest.raises(Exception, match="confidence/version is invalid"):
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE entity_links SET confidence=101 WHERE id='edge-2'"
            ))
    with pytest.raises(Exception, match="confidence/version is invalid"):
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE entity_links SET version=0 WHERE id='edge-2'"
            ))

    # Downgrade restores retained metadata to the V2 plaintext JSON contract
    # and removes graph-only edges before their LifeEntity authority vanishes.
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, EXPLICIT_BASELINE_REVISION)
    with engine.connect() as connection:
        retained = connection.execute(text(
            "SELECT metadata FROM entity_links WHERE id='edge-1'"
        )).scalar_one()
        graph_only = connection.execute(text(
            "SELECT COUNT(*) FROM entity_links WHERE id='edge-2'"
        )).scalar_one()
        restored_planning = connection.execute(text(
            "SELECT title, details FROM planning_items WHERE id='plan-1'"
        )).one()
    assert json.loads(str(retained)) == {"private": "edge context"}
    assert graph_only == 0
    assert restored_planning == (
        "enc:Private planning title", "Private planning details"
    )
    engine.dispose()


def test_0003_downgrade_wrong_key_fails_before_destructive_ddl(
    tmp_path, monkeypatch
):
    from alembic import command
    from src import secret_storage
    from src.database_migrations import _alembic_config

    db_path = tmp_path / "wrong-key-downgrade.db"
    database_url = f"sqlite:///{db_path}"
    engine = create_engine(database_url)
    config = _alembic_config(database_url)
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.stamp(config, LEGACY_BASELINE_REVISION)
        command.upgrade(config, EXPLICIT_BASELINE_REVISION)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO accounts "
            "(id, username, status, auth_epoch, created_at, updated_at) "
            "VALUES ('a', 'alice', 'active', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        connection.execute(text(
            "INSERT INTO entity_links "
            "(id, owner_id, source_type, source_id, relation, target_type, "
            "target_id, metadata, created_at) VALUES "
            "('edge', 'a', 'inbox_item', 'capture', 'supports', 'project', "
            "'project', '{\"private\":\"must survive\"}', CURRENT_TIMESTAMP)"
        ))
    upgrade_schema(engine)

    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)
    with pytest.raises(RuntimeError, match="could not be decrypted"):
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, EXPLICIT_BASELINE_REVISION)

    assert schema_revision_status(engine).current_revisions == (
        SCHEMA_HEAD_REVISION,
    )
    tables = set(inspect(engine).get_table_names())
    assert {"focus_sessions", "action_proposals", "life_entities"} <= tables
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT COUNT(*) FROM entity_links WHERE id='edge'"
        )).scalar_one() == 1
    engine.dispose()


def test_0003_upgrade_preserves_token_looking_v2_planning_content(
    tmp_path, monkeypatch
):
    from alembic import command
    from src import secret_storage
    from src.database_migrations import _alembic_config

    db_path = tmp_path / "token-looking-v2-planning.db"
    database_url = f"sqlite:///{db_path}"
    engine = create_engine(database_url)
    config = _alembic_config(database_url)
    encryption_key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", encryption_key)
    monkeypatch.setattr(secret_storage, "_fernet", None)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.stamp(config, LEGACY_BASELINE_REVISION)
        command.upgrade(config, EXPLICIT_BASELINE_REVISION)

    literal_title = secret_storage.encrypt("literal inner payload")
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO planning_items "
            "(id, owner, title, details, status, priority, source, version, "
            "created_at, updated_at) VALUES "
            "('plan', 'alice', :title, '', 'open', 'normal', 'user', 1, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ), {"title": literal_title})

    upgrade_schema(engine)
    assert schema_revision_status(engine).current_revisions == (SCHEMA_HEAD_REVISION,)
    with engine.connect() as connection:
        outer_envelope = connection.execute(text(
            "SELECT title FROM planning_items WHERE id='plan'"
        )).scalar_one()
    assert outer_envelope != literal_title
    assert secret_storage.decrypt(outer_envelope) == literal_title

    from core.database import PlanningItem
    session = sessionmaker(bind=engine)()
    try:
        assert session.get(PlanningItem, "plan").title == literal_title
    finally:
        session.close()

    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, EXPLICIT_BASELINE_REVISION)
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT title FROM planning_items WHERE id='plan'"
        )).scalar_one() == literal_title
    engine.dispose()


def test_0003_planning_downgrade_wrong_key_preserves_encrypted_rows(
    tmp_path, monkeypatch
):
    from alembic import command
    from src import secret_storage
    from src.database_migrations import _alembic_config

    db_path = tmp_path / "wrong-key-planning-downgrade.db"
    database_url = f"sqlite:///{db_path}"
    engine = create_engine(database_url)
    config = _alembic_config(database_url)
    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.stamp(config, LEGACY_BASELINE_REVISION)
        command.upgrade(config, EXPLICIT_BASELINE_REVISION)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO planning_items "
            "(id, owner, title, details, status, priority, source, version, "
            "created_at, updated_at) VALUES "
            "('plan', 'alice', 'Private title', 'Private details', 'open', "
            "'normal', 'user', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
    upgrade_schema(engine)
    with engine.connect() as connection:
        encrypted_before = connection.execute(text(
            "SELECT title, details FROM planning_items WHERE id='plan'"
        )).one()

    monkeypatch.setenv(
        "RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii")
    )
    monkeypatch.setattr(secret_storage, "_fernet", None)
    with pytest.raises(RuntimeError, match="title could not be decrypted"):
        with engine.connect() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, EXPLICIT_BASELINE_REVISION)

    assert schema_revision_status(engine).current_revisions == (
        SCHEMA_HEAD_REVISION,
    )
    assert {"focus_sessions", "action_proposals", "life_entities"} <= set(
        inspect(engine).get_table_names()
    )
    with engine.connect() as connection:
        encrypted_after = connection.execute(text(
            "SELECT title, details FROM planning_items WHERE id='plan'"
        )).one()
    assert encrypted_after == encrypted_before
    engine.dispose()


def test_database_backed_cli_entrypoints_opt_into_explicit_initialization():
    names = (
        "calendar",
        "docs",
        "gallery",
        "mail",
        "mcp",
        "notes",
        "sessions",
        "signature",
        "tasks",
        "webhook",
    )
    for name in names:
        source = (ROOT / "scripts" / f"odysseus-{name}").read_text(encoding="utf-8")
        assert "initialize_db=True" in source, name


def test_secret_storage_uses_explicit_shared_key_without_writing_local_file(
    tmp_path,
    monkeypatch,
):
    from src import secret_storage

    key_path = tmp_path / ".app_key"
    monkeypatch.setattr(secret_storage, "_KEY_PATH", key_path)
    monkeypatch.setattr(secret_storage, "_fernet", None)
    monkeypatch.setenv("RESTIA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("RESTIA_ENCRYPTION_KEY_FILE", raising=False)

    encrypted = secret_storage.encrypt("shared secret")
    assert secret_storage.decrypt(encrypted) == "shared secret"
    assert not key_path.exists()
