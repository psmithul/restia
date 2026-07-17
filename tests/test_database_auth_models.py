from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    AccountCapability,
    AccountRole,
    ApiToken,
    AuthIdentity,
    AuthImportRun,
    AuthPolicy,
    AuthSession,
    Base,
    LocalCredential,
    MfaFactor,
    MfaRecoveryCode,
    RetiredAuthSubject,
    _migrate_add_unified_auth_columns,
)


AUTH_TABLES = (
    Account.__table__,
    AuthIdentity.__table__,
    AuthPolicy.__table__,
    LocalCredential.__table__,
    MfaFactor.__table__,
    MfaRecoveryCode.__table__,
    AccountRole.__table__,
    AccountCapability.__table__,
    AuthSession.__table__,
    RetiredAuthSubject.__table__,
    AuthImportRun.__table__,
    ApiToken.__table__,
)


def test_unified_auth_tables_create_with_expected_columns_and_constraints():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=AUTH_TABLES)
    schema = inspect(engine)

    assert {
        "accounts",
        "auth_identities",
        "auth_policy",
        "local_credentials",
        "mfa_factors",
        "mfa_recovery_codes",
        "account_roles",
        "account_capabilities",
        "auth_sessions",
        "retired_auth_subjects",
        "auth_import_runs",
        "api_tokens",
    } <= set(schema.get_table_names())
    assert {"status", "auth_epoch", "last_login_at"} <= {
        column["name"] for column in schema.get_columns("accounts")
    }
    assert {"issuer", "state", "linked_at", "last_verified_at"} <= {
        column["name"] for column in schema.get_columns("auth_identities")
    }
    assert {"account_id", "digest_scheme", "revoked_at", "expires_at"} <= {
        column["name"] for column in schema.get_columns("api_tokens")
    }

    session = sessionmaker(bind=engine)()
    session.add(Account(id="bad", username="bad", status="unexpected", auth_epoch=1))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()
    session.add(Account(id="bad-epoch", username="bad-epoch", auth_epoch=0))
    with pytest.raises(IntegrityError):
        session.commit()
    session.close()


def test_auth_identity_uniqueness_is_scoped_by_issuer():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=(Account.__table__, AuthIdentity.__table__),
    )
    factory = sessionmaker(bind=engine)
    db = factory()
    db.add_all([
        Account(id="account-a", username="alice"),
        Account(id="account-b", username="bob"),
    ])
    db.flush()
    db.add_all([
        AuthIdentity(
            id="identity-a",
            account_id="account-a",
            provider="oidc",
            issuer="https://issuer-a.example",
            subject="same-subject",
        ),
        AuthIdentity(
            id="identity-b",
            account_id="account-b",
            provider="oidc",
            issuer="https://issuer-b.example",
            subject="same-subject",
        ),
    ])
    db.commit()

    db.add(AuthIdentity(
        id="identity-duplicate",
        account_id="account-b",
        provider="oidc",
        issuer="https://issuer-a.example",
        subject="same-subject",
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.close()


def test_unified_auth_model_ddl_compiles_for_postgresql():
    dialect = postgresql.dialect()
    for table in AUTH_TABLES:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert "CREATE TABLE" in ddl
        assert "PRAGMA" not in ddl
        assert "sqlite" not in ddl.lower()


def test_legacy_sqlite_auth_columns_are_added_backfilled_and_idempotent(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-auth.db'}")
    now = datetime(2026, 7, 17, 1, 2, 3)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE accounts (
                id VARCHAR(36) PRIMARY KEY,
                username VARCHAR(160) NOT NULL UNIQUE,
                display_name VARCHAR(160),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE auth_identities (
                id VARCHAR(36) PRIMARY KEY,
                account_id VARCHAR(36) NOT NULL,
                provider VARCHAR(32) NOT NULL,
                subject VARCHAR(255) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE(provider, subject)
            )
        """))
        conn.execute(text("""
            CREATE TABLE api_tokens (
                id VARCHAR PRIMARY KEY,
                owner VARCHAR,
                name VARCHAR NOT NULL,
                token_hash VARCHAR NOT NULL,
                token_prefix VARCHAR NOT NULL,
                scopes VARCHAR NOT NULL DEFAULT 'chat',
                is_active BOOLEAN,
                last_used_at DATETIME,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE auth_identity_links (
                id VARCHAR(36) PRIMARY KEY,
                identity_id VARCHAR(36),
                FOREIGN KEY(identity_id) REFERENCES auth_identities(id)
                    ON DELETE SET NULL
            )
        """))
        conn.execute(text(
            "CREATE INDEX ix_auth_identity_subject_custom "
            "ON auth_identities(subject)"
        ))
        conn.execute(text("""
            INSERT INTO accounts
                (id, username, created_at, updated_at)
            VALUES ('account-1', 'alice', :now, :now)
        """), {"now": now})
        conn.execute(text("""
            INSERT INTO auth_identities
                (id, account_id, provider, subject, created_at, updated_at)
            VALUES
                ('local-1', 'account-1', 'local', 'alice', :now, :now),
                ('oidc-1', 'account-1', 'oidc', 'external-subject', :now, :now)
        """), {"now": now})
        conn.execute(text("""
            INSERT INTO auth_identity_links (id, identity_id)
            VALUES ('link-1', 'local-1')
        """))

    _migrate_add_unified_auth_columns(engine)
    _migrate_add_unified_auth_columns(engine)

    schema = inspect(engine)
    assert {"status", "auth_epoch", "last_login_at"} <= {
        column["name"] for column in schema.get_columns("accounts")
    }
    assert {"issuer", "state", "linked_at", "last_verified_at"} <= {
        column["name"] for column in schema.get_columns("auth_identities")
    }
    assert {"account_id", "digest_scheme", "revoked_at", "expires_at"} <= {
        column["name"] for column in schema.get_columns("api_tokens")
    }
    unique_column_sets = {
        tuple(constraint["column_names"])
        for constraint in schema.get_unique_constraints("auth_identities")
    }
    assert ("provider", "issuer", "subject") in unique_column_sets
    assert ("provider", "subject") not in unique_column_sets
    assert {
        "ix_auth_identities_account_id",
        "ix_auth_identity_account_provider",
        "ix_auth_identity_issuer_subject",
        "ix_auth_identity_subject_custom",
    } <= {
        index["name"] for index in schema.get_indexes("auth_identities")
    }
    identity_foreign_keys = schema.get_foreign_keys("auth_identities")
    assert len(identity_foreign_keys) == 1
    assert identity_foreign_keys[0]["constrained_columns"] == ["account_id"]
    assert identity_foreign_keys[0]["referred_table"] == "accounts"
    with engine.connect() as conn:
        identity_foreign_key_rows = conn.execute(text(
            'PRAGMA foreign_key_list("auth_identities")'
        )).all()
        account = conn.execute(text(
            "SELECT status, auth_epoch FROM accounts WHERE id='account-1'"
        )).one()
        identity_rows = {
            row.id: row
            for row in conn.execute(text("""
                SELECT id, account_id, provider, issuer, subject,
                       created_at, updated_at, linked_at
                FROM auth_identities ORDER BY id
            """)).all()
        }
        missing_linked = conn.execute(text(
            "SELECT COUNT(*) FROM auth_identities WHERE linked_at IS NULL"
        )).scalar_one()
        linked_identity_id = conn.execute(text(
            "SELECT identity_id FROM auth_identity_links WHERE id='link-1'"
        )).scalar_one()
        foreign_keys_enabled = conn.execute(text(
            "PRAGMA foreign_keys"
        )).scalar_one()
    assert len(identity_foreign_key_rows) == 1
    assert identity_foreign_key_rows[0][2] == "accounts"
    assert identity_foreign_key_rows[0][3] == "account_id"
    assert identity_foreign_key_rows[0][6] == "CASCADE"
    assert tuple(account) == ("active", 1)
    assert set(identity_rows) == {"local-1", "oidc-1"}
    assert identity_rows["local-1"].account_id == "account-1"
    assert identity_rows["local-1"].issuer == "restia-local"
    assert identity_rows["oidc-1"].account_id == "account-1"
    assert identity_rows["oidc-1"].provider == "oidc"
    assert identity_rows["oidc-1"].issuer == "legacy:oidc"
    assert identity_rows["oidc-1"].subject == "external-subject"
    assert datetime.fromisoformat(identity_rows["oidc-1"].created_at) == now
    assert datetime.fromisoformat(identity_rows["oidc-1"].updated_at) == now
    assert datetime.fromisoformat(identity_rows["oidc-1"].linked_at) == now
    assert missing_linked == 0
    assert linked_identity_id == "local-1"
    assert foreign_keys_enabled == 1

    # The legacy constraint is truly gone: the exact provider/subject may be
    # linked under a different issuer, while the issuer-qualified tuple remains
    # unique.
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO auth_identities (
                id, account_id, provider, issuer, subject, state, linked_at,
                last_verified_at, created_at, updated_at
            ) VALUES (
                'oidc-2', 'account-1', 'oidc', 'https://issuer.example',
                'external-subject', 'active', :now, NULL, :now, :now
            )
        """), {"now": now})
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO auth_identities (
                    id, account_id, provider, issuer, subject, state,
                    linked_at, last_verified_at, created_at, updated_at
                ) VALUES (
                    'oidc-3', 'account-1', 'oidc', 'https://issuer.example',
                    'external-subject', 'active', :now, NULL, :now, :now
                )
            """), {"now": now})

    # Rebuilding the parent table must not retarget or fire a child table's
    # foreign key. Its original ON DELETE behavior still applies afterwards.
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM auth_identities WHERE id='local-1'"
        ))
        assert conn.execute(text(
            "SELECT identity_id FROM auth_identity_links WHERE id='link-1'"
        )).scalar_one() is None


def test_legacy_auth_identity_rebuild_rolls_back_on_new_fk_violation(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'orphan-identity.db'}")
    now = datetime(2026, 7, 17, 1, 2, 3)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE accounts (
                id VARCHAR(36) PRIMARY KEY,
                username VARCHAR(160) NOT NULL UNIQUE,
                display_name VARCHAR(160),
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE auth_identities (
                id VARCHAR(36) PRIMARY KEY,
                account_id VARCHAR(36) NOT NULL,
                provider VARCHAR(32) NOT NULL,
                subject VARCHAR(255) NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                UNIQUE(provider, subject)
            )
        """))
        conn.execute(text("""
            INSERT INTO auth_identities
                (id, account_id, provider, subject, created_at, updated_at)
            VALUES
                ('orphan-1', 'missing-account', 'local', 'alice', :now, :now)
        """), {"now": now})

    with pytest.raises(RuntimeError, match="foreign-key violations"):
        _migrate_add_unified_auth_columns(engine)

    schema = inspect(engine)
    assert "issuer" not in {
        column["name"] for column in schema.get_columns("auth_identities")
    }
    assert "auth_identities__restia_migration" not in schema.get_table_names()
    assert (
        "provider", "subject"
    ) in {
        tuple(constraint["column_names"])
        for constraint in schema.get_unique_constraints("auth_identities")
    }
    with engine.connect() as conn:
        orphan = conn.execute(text("""
            SELECT id, account_id, provider, subject, created_at, updated_at
            FROM auth_identities
        """)).one()
        assert tuple(orphan[:4]) == (
            "orphan-1", "missing-account", "local", "alice",
        )
        assert datetime.fromisoformat(orphan.created_at) == now
        assert datetime.fromisoformat(orphan.updated_at) == now
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
