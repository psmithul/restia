from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import bcrypt
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

import src.legacy_auth_import as importer
import src.secret_storage as secret_storage
from core.auth import ADMIN_PRIVILEGES, DEFAULT_PRIVILEGES
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
)
from src.auth_runtime import build_auth_manager
from src.legacy_auth_import import LegacyAuthImportError, import_legacy_auth


NOW = datetime(2026, 7, 17, 12, 0, 0)
NOW_EPOCH = NOW.replace(tzinfo=timezone.utc).timestamp()

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


def _bcrypt(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture
def auth_db(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine, tables=AUTH_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    yield engine, factory
    engine.dispose()


def _write_json(path: Path, value: object) -> bytes:
    raw = json.dumps(value, indent=2, sort_keys=False).encode("utf-8")
    path.write_bytes(raw)
    return raw


def _basic_auth(*, username: str = "alice", password_hash: str | None = None) -> dict:
    return {
        "users": {
            username: {
                "password_hash": password_hash or _bcrypt("alice-password"),
                "created": NOW_EPOCH - 100,
                "is_admin": True,
                "privileges": {"can_use_bash": True},
            }
        },
        "signup_enabled": False,
        "retired_usernames": [],
    }


def _run(factory, auth_path: Path, sessions_path: Path, backup_dir: Path):
    return import_legacy_auth(
        factory,
        auth_path=auth_path,
        sessions_path=sessions_path,
        backup_dir=backup_dir,
        now=lambda: NOW,
    )


def _race_factories(tmp_path):
    """Return independent SQLite engines forced across the empty-row gap."""

    database_path = tmp_path / "auth-import-race.db"
    connect_args = {"check_same_thread": False, "timeout": 0.05}
    engines = (
        create_engine(f"sqlite:///{database_path}", connect_args=connect_args),
        create_engine(f"sqlite:///{database_path}", connect_args=connect_args),
    )
    Base.metadata.create_all(engines[0], tables=AUTH_TABLES)
    with engines[0].connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar()

    class ImportRaceSession(Session):
        pass

    factories = tuple(
        sessionmaker(
            bind=engine,
            class_=ImportRaceSession,
            expire_on_commit=False,
        )
        for engine in engines
    )
    barrier = threading.Barrier(2)
    seen_threads: set[int] = set()
    seen_lock = threading.Lock()

    def synchronize_first_claim(session, _flush_context, _instances):
        if not any(
            isinstance(row, AuthImportRun)
            and row.source_kind == importer.IMPORT_SOURCE_KIND
            for row in session.new
        ):
            return
        thread_id = threading.get_ident()
        with seen_lock:
            if thread_id in seen_threads:
                return
            seen_threads.add(thread_id)
        barrier.wait(timeout=10)

    event.listen(ImportRaceSession, "before_flush", synchronize_first_claim)
    return engines, factories, ImportRaceSession, synchronize_first_claim


def _dispose_race_factories(engines, session_class, listener):
    event.remove(session_class, "before_flush", listener)
    for engine in engines:
        engine.dispose()


def test_success_imports_complete_auth_snapshot_and_durable_backups(auth_db, tmp_path):
    engine, factory = auth_db
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    backup_dir = tmp_path / "backups"
    raw_admin_session = "a" * 64
    raw_alice_session = "b" * 64
    alice_digest = "sha256:" + hashlib.sha256(raw_alice_session.encode()).hexdigest()
    active_secret = secret_storage.encrypt("JBSWY3DPEHPK3PXP")
    pending_secret = secret_storage.encrypt("KRSXG5DSNFXGOIDB")
    protected_recovery = "sha256:" + hashlib.sha256(b"already-protected").hexdigest()
    auth = {
        "users": {
            "Admin": {
                "password_hash": _bcrypt("admin-password"),
                "created": NOW_EPOCH - 300,
                "role": "admin",
            },
            "Alice": {
                "password_hash": _bcrypt("alice-password"),
                "created": NOW_EPOCH - 200,
                "is_admin": False,
                "privileges": {
                    "can_use_bash": True,
                    "allowed_models": ["model-a", "model-a", "model-b"],
                },
                "totp_enabled": True,
                "totp_secret": active_secret,
                "totp_secret_pending": pending_secret,
                "totp_backup_codes": ["plaintext-recovery", protected_recovery],
                "email": "must-not-be-linked@example.com",
                "role": "member",
            },
        },
        "signup_enabled": True,
        "retired_usernames": ["FormerUser"],
    }
    sessions = {
        raw_admin_session: {"username": "admin", "expiry": NOW_EPOCH + 3600},
        alice_digest: {"username": "alice", "expiry": NOW_EPOCH + 7200},
        "c" * 64: {"username": "deleted-user", "expiry": NOW_EPOCH - 1},
    }
    auth_raw = _write_json(auth_path, auth)
    sessions_raw = _write_json(sessions_path, sessions)

    db = factory()
    db.add(ApiToken(
        id="token-1",
        owner="Alice",
        account_id=None,
        name="legacy",
        token_hash=_bcrypt("ody_token"),
        token_prefix="ody_test",
        scopes="chat",
        is_active=True,
    ))
    db.commit()
    db.close()

    result = _run(factory, auth_path, sessions_path, backup_dir)

    assert result.state == "completed"
    assert result.accounts == 2
    assert result.sessions == 2
    assert result.retired_subjects == 1
    assert result.api_tokens_backfilled == 1
    assert result.idempotent is False
    assert result.auth_sha256 == hashlib.sha256(auth_raw).hexdigest()
    assert result.sessions_sha256 == hashlib.sha256(sessions_raw).hexdigest()
    assert Path(result.backup_auth_path).read_bytes() == auth_raw
    assert Path(result.backup_sessions_path).read_bytes() == sessions_raw
    assert auth_path.read_bytes() == auth_raw
    assert sessions_path.read_bytes() == sessions_raw
    if os.name != "nt":
        assert os.stat(result.backup_auth_path).st_mode & 0o777 == 0o600
        assert os.stat(result.backup_sessions_path).st_mode & 0o777 == 0o600

    db = factory()
    accounts = {row.username: row for row in db.query(Account).all()}
    assert set(accounts) == {"admin", "alice"}
    identities = {row.subject: row for row in db.query(AuthIdentity).all()}
    assert set(identities) == {"admin", "alice"}
    assert all(row.provider == "local" and row.issuer == "restia-local" for row in identities.values())
    credentials = db.query(LocalCredential).all()
    assert len(credentials) == 2
    assert all(row.algorithm == "bcrypt" for row in credentials)
    roles = {
        username: {
            row.role
            for row in db.query(AccountRole).filter(AccountRole.account_id == account.id)
        }
        for username, account in accounts.items()
    }
    assert roles == {"admin": {"member", "admin"}, "alice": {"member"}}
    caps = {
        account.username: db.query(AccountCapability).filter(
            AccountCapability.account_id == account.id
        ).one().capabilities
        for account in accounts.values()
    }
    assert caps["admin"] == ADMIN_PRIVILEGES
    assert caps["alice"]["can_use_bash"] is True
    assert caps["alice"]["allowed_models"] == ["model-a", "model-b"]
    for key, value in DEFAULT_PRIVILEGES.items():
        assert key in caps["alice"]

    factor = db.query(MfaFactor).one()
    assert factor.account_id == accounts["alice"].id
    assert factor.state == "active"
    assert factor.secret == "JBSWY3DPEHPK3PXP"
    assert factor.pending_secret == "KRSXG5DSNFXGOIDB"
    recovery = db.query(MfaRecoveryCode).all()
    assert len(recovery) == 2
    assert all(row.code_hash != "plaintext-recovery" for row in recovery)
    plaintext_row = next(row for row in recovery if row.digest_scheme == "bcrypt_legacy")
    assert bcrypt.checkpw(b"plaintext-recovery", plaintext_row.code_hash.encode())
    assert any(row.code_hash == protected_recovery for row in recovery)

    imported_sessions = db.query(AuthSession).all()
    assert len(imported_sessions) == 2
    expected_admin_digest = "sha256:" + hashlib.sha256(raw_admin_session.encode()).hexdigest()
    assert {row.token_digest for row in imported_sessions} == {
        expected_admin_digest,
        alice_digest,
    }
    assert all(row.digest_scheme == "sha256_legacy" for row in imported_sessions)
    assert all(raw_admin_session not in row.token_digest for row in imported_sessions)
    assert db.query(RetiredAuthSubject).one().subject == "formeruser"
    policy = db.query(AuthPolicy).one()
    assert policy.signup_enabled is True
    api_token = db.query(ApiToken).one()
    assert api_token.account_id == accounts["alice"].id

    run = db.query(AuthImportRun).one()
    assert run.state == "completed"
    assert run.auth_sha256 == hashlib.sha256(auth_raw).hexdigest()
    assert run.sessions_sha256 == hashlib.sha256(sessions_raw).hexdigest()
    redacted = json.dumps(run.details, sort_keys=True)
    for forbidden in (
        "alice",
        "admin",
        "plaintext-recovery",
        "must-not-be-linked@example.com",
        raw_admin_session,
        active_secret,
    ):
        assert forbidden not in redacted
    db.close()

    with engine.connect() as connection:
        stored_secret = connection.execute(text(
            "SELECT secret FROM mfa_factors"
        )).scalar_one()
    assert stored_secret.startswith("enc:")
    assert "JBSWY3DPEHPK3PXP" not in stored_secret


def test_exact_digest_rerun_is_idempotent_without_duplicate_secrets(auth_db, tmp_path):
    _engine, factory = auth_db
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    backup_dir = tmp_path / "backups"
    auth = _basic_auth()
    auth["users"]["alice"].update({
        "totp_enabled": True,
        "totp_secret": "JBSWY3DPEHPK3PXP",
        "totp_backup_codes": ["one-time-code"],
    })
    _write_json(auth_path, auth)
    _write_json(sessions_path, {
        "d" * 64: {"username": "alice", "expiry": NOW_EPOCH + 300},
    })

    first = _run(factory, auth_path, sessions_path, backup_dir)
    second = _run(factory, auth_path, sessions_path, backup_dir)

    assert first.run_id == second.run_id
    assert second.idempotent is True
    assert second.accounts == first.accounts
    assert second.sessions == first.sessions
    db = factory()
    assert db.query(Account).count() == 1
    assert db.query(AuthIdentity).count() == 1
    assert db.query(LocalCredential).count() == 1
    assert db.query(AccountRole).count() == 2
    assert db.query(AccountCapability).count() == 1
    assert db.query(MfaFactor).count() == 1
    assert db.query(MfaRecoveryCode).count() == 1
    assert db.query(AuthSession).count() == 1
    assert db.query(AuthImportRun).count() == 1
    db.close()


def test_changed_sources_after_completion_fail_loudly_and_preserve_completed_run(auth_db, tmp_path):
    _engine, factory = auth_db
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    backup_dir = tmp_path / "backups"
    original = _write_json(auth_path, _basic_auth())
    _write_json(sessions_path, {})
    first = _run(factory, auth_path, sessions_path, backup_dir)

    changed = original + b"\n"
    auth_path.write_bytes(changed)
    with pytest.raises(LegacyAuthImportError) as exc:
        _run(factory, auth_path, sessions_path, backup_dir)

    assert exc.value.code == "source_changed"
    changed_digest = hashlib.sha256(changed).hexdigest()
    assert (backup_dir / f"auth.{changed_digest}.json.bak").read_bytes() == changed
    db = factory()
    run = db.query(AuthImportRun).one()
    assert run.id == first.run_id
    assert run.state == "completed"
    assert run.auth_sha256 == first.auth_sha256
    assert db.query(Account).count() == 1
    db.close()


@pytest.mark.parametrize(
    ("users", "code"),
    [
        (
            {
                "Alice": {"password_hash": _bcrypt("one")},
                " alice ": {"password_hash": _bcrypt("two")},
            },
            "canonical_collision",
        ),
        (
            {"internal-tool": {"password_hash": _bcrypt("one")}},
            "reserved_subject",
        ),
    ],
)
def test_subject_collisions_fail_transactionally_with_redacted_run(
    auth_db, tmp_path, users, code
):
    _engine, factory = auth_db
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    backup_dir = tmp_path / "backups"
    raw = _write_json(auth_path, {"users": users})
    _write_json(sessions_path, {})

    with pytest.raises(LegacyAuthImportError) as exc:
        _run(factory, auth_path, sessions_path, backup_dir)

    assert exc.value.code == code
    digest = hashlib.sha256(raw).hexdigest()
    assert (backup_dir / f"auth.{digest}.json.bak").read_bytes() == raw
    db = factory()
    assert db.query(Account).count() == 0
    run = db.query(AuthImportRun).one()
    assert run.state == "failed"
    assert run.details == {
        "schema_version": 1,
        "accounts": 0,
        "sessions": 0,
        "retired_subjects": 0,
        "api_tokens_backfilled": 0,
        "error_code": code,
    }
    assert not any(name.lower() in json.dumps(run.details) for name in users)
    db.close()


def test_orphan_api_token_fails_closed_and_rolls_back_auth_rows(auth_db, tmp_path):
    _engine, factory = auth_db
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    backup_dir = tmp_path / "backups"
    _write_json(auth_path, _basic_auth())
    _write_json(sessions_path, {})
    db = factory()
    db.add(ApiToken(
        id="orphan",
        owner="not-a-profile",
        name="orphan",
        token_hash=_bcrypt("ody_orphan"),
        token_prefix="ody_orph",
        scopes="chat",
        is_active=True,
    ))
    db.commit()
    db.close()

    with pytest.raises(LegacyAuthImportError) as exc:
        _run(factory, auth_path, sessions_path, backup_dir)

    assert exc.value.code == "orphan_api_token"
    db = factory()
    assert db.query(Account).count() == 0
    assert db.query(LocalCredential).count() == 0
    assert db.query(AuthSession).count() == 0
    assert db.query(ApiToken).one().account_id is None
    run = db.query(AuthImportRun).one()
    assert run.state == "failed"
    assert run.details["error_code"] == "orphan_api_token"
    db.close()


def test_existing_local_identity_conflict_never_links_by_email(auth_db, tmp_path):
    _engine, factory = auth_db
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    backup_dir = tmp_path / "backups"
    source = _basic_auth()
    source["users"]["alice"]["email"] = "same@example.com"
    _write_json(auth_path, source)
    _write_json(sessions_path, {})
    db = factory()
    db.add_all([
        Account(id="account-alice", username="alice"),
        Account(id="account-bob", username="bob"),
    ])
    db.flush()
    db.add(AuthIdentity(
        id="identity-conflict",
        account_id="account-bob",
        provider="local",
        issuer="restia-local",
        subject="alice",
        state="active",
    ))
    db.commit()
    db.close()

    with pytest.raises(LegacyAuthImportError) as exc:
        _run(factory, auth_path, sessions_path, backup_dir)

    assert exc.value.code == "identity_conflict"
    db = factory()
    assert db.query(LocalCredential).count() == 0
    assert db.query(AuthIdentity).filter(
        AuthIdentity.subject == "alice"
    ).one().account_id == "account-bob"
    assert "same@example.com" not in json.dumps(db.query(AuthImportRun).one().details)
    db.close()


def test_legacy_single_user_format_becomes_canonical_admin(auth_db, tmp_path):
    _engine, factory = auth_db
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    backup_dir = tmp_path / "backups"
    password_hash = _bcrypt("legacy-password")
    _write_json(auth_path, {
        "username": "LegacyAdmin",
        "password_hash": password_hash,
        "created": NOW_EPOCH - 500,
        "signup_enabled": False,
    })
    result = _run(factory, auth_path, sessions_path, backup_dir)

    db = factory()
    account = db.query(Account).one()
    assert account.username == "legacyadmin"
    identity = db.query(AuthIdentity).one()
    assert identity.subject == "legacyadmin"
    credential = db.query(LocalCredential).one()
    assert credential.password_hash == password_hash
    assert {row.role for row in db.query(AccountRole).all()} == {"member", "admin"}
    assert result.sessions_sha256 is None
    assert result.backup_sessions_path is None
    db.close()


def test_duplicate_json_keys_are_rejected_after_backup_without_domain_mutation(auth_db, tmp_path):
    _engine, factory = auth_db
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    backup_dir = tmp_path / "backups"
    password_hash = _bcrypt("password")
    raw = (
        '{"users":{"alice":{"password_hash":"%s"},'
        '"alice":{"password_hash":"%s"}}}' % (password_hash, password_hash)
    ).encode()
    auth_path.write_bytes(raw)
    _write_json(sessions_path, {})

    with pytest.raises(LegacyAuthImportError) as exc:
        _run(factory, auth_path, sessions_path, backup_dir)

    assert exc.value.code == "invalid_json"
    digest = hashlib.sha256(raw).hexdigest()
    assert (backup_dir / f"auth.{digest}.json.bak").read_bytes() == raw
    db = factory()
    assert db.query(Account).count() == 0
    assert db.query(AuthImportRun).one().state == "failed"
    db.close()


def test_size_limit_fails_before_any_database_access(auth_db, tmp_path, monkeypatch):
    _engine, factory = auth_db
    auth_path = tmp_path / "auth.json"
    auth_path.write_bytes(b"{" + b" " * 64 + b"}")
    sessions_path = tmp_path / "sessions.json"
    _write_json(sessions_path, {})
    monkeypatch.setattr(importer, "MAX_AUTH_SOURCE_BYTES", 16)
    calls = []

    def forbidden_factory():
        calls.append(True)
        return factory()

    with pytest.raises(LegacyAuthImportError) as exc:
        _run(forbidden_factory, auth_path, sessions_path, tmp_path / "backups")

    assert exc.value.code == "source_too_large"
    assert calls == []
    db = factory()
    assert db.query(AuthImportRun).count() == 0
    db.close()


def test_backup_exists_before_first_database_session_is_opened(auth_db, tmp_path):
    _engine, factory = auth_db
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    backup_dir = tmp_path / "backups"
    auth_raw = _write_json(auth_path, _basic_auth())
    sessions_raw = _write_json(sessions_path, {})
    auth_digest = hashlib.sha256(auth_raw).hexdigest()
    sessions_digest = hashlib.sha256(sessions_raw).hexdigest()
    calls = []

    def checking_factory():
        assert (backup_dir / f"auth.{auth_digest}.json.bak").read_bytes() == auth_raw
        assert (backup_dir / f"sessions.{sessions_digest}.json.bak").read_bytes() == sessions_raw
        calls.append(True)
        return factory()

    _run(checking_factory, auth_path, sessions_path, backup_dir)

    assert calls


def test_completed_import_never_reconciles_stale_json_as_second_authority(
    auth_db,
    tmp_path,
):
    _engine, factory = auth_db
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    backup_dir = tmp_path / "backups"
    _write_json(auth_path, _basic_auth())
    _write_json(sessions_path, {})
    first = _run(factory, auth_path, sessions_path, backup_dir)

    db = factory()
    credential = db.query(LocalCredential).one()
    credential.password_hash = bcrypt.hashpw(
        b"database-only-new-password", bcrypt.gensalt(rounds=4)
    ).decode("ascii")
    policy = db.query(AuthPolicy).one()
    policy.signup_enabled = not policy.signup_enabled
    db.commit()
    changed_hash = credential.password_hash
    db.close()

    rerun = _run(factory, auth_path, sessions_path, backup_dir)

    assert rerun.run_id == first.run_id
    assert rerun.idempotent is True
    check = factory()
    assert check.query(LocalCredential).one().password_hash == changed_hash
    assert check.query(AuthPolicy).one().signup_enabled is True
    check.close()


def test_import_rejects_store_without_any_administrator(auth_db, tmp_path):
    _engine, factory = auth_db
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    backup_dir = tmp_path / "backups"
    payload = _basic_auth()
    payload["users"]["alice"]["is_admin"] = False
    _write_json(auth_path, payload)
    _write_json(sessions_path, {})

    with pytest.raises(LegacyAuthImportError) as exc:
        _run(factory, auth_path, sessions_path, backup_dir)

    assert exc.value.code == "authorization_conflict"


def test_concurrent_first_start_imports_converge_on_one_completed_run(tmp_path):
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    backup_dir = tmp_path / "backups"
    _write_json(auth_path, _basic_auth())
    _write_json(sessions_path, {})
    engines, factories, session_class, listener = _race_factories(tmp_path)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    _run,
                    factory,
                    auth_path,
                    sessions_path,
                    backup_dir,
                )
                for factory in factories
            ]
            results = [future.result(timeout=20) for future in futures]

        assert {result.state for result in results} == {"completed"}
        assert {result.idempotent for result in results} == {False, True}
        assert len({result.run_id for result in results}) == 1
        db = factories[0]()
        assert db.query(AuthImportRun).count() == 1
        assert db.query(AuthImportRun).one().state == "completed"
        assert db.query(Account).count() == 1
        assert db.query(AuthIdentity).count() == 1
        assert db.query(LocalCredential).count() == 1
        db.close()
    finally:
        _dispose_race_factories(engines, session_class, listener)


def test_concurrent_auth_runtime_builders_do_not_lock_the_losing_process(tmp_path):
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    backup_dir = tmp_path / "backups"
    _write_json(auth_path, _basic_auth())
    _write_json(sessions_path, {})
    engines, factories, session_class, listener = _race_factories(tmp_path)

    def build(factory):
        return build_auth_manager(
            factory,
            auth_path=auth_path,
            sessions_path=sessions_path,
            backup_dir=backup_dir,
            token_hmac_key=b"legacy-import-race-token-key-32b",
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(build, factory) for factory in factories]
            managers = [future.result(timeout=20) for future in futures]

        assert all(manager.auth_store_error is False for manager in managers)
        assert all(
            manager.verify_password("alice", "alice-password")
            for manager in managers
        )
        db = factories[0]()
        assert db.query(AuthImportRun).count() == 1
        assert db.query(AuthImportRun).one().state == "completed"
        db.close()
    finally:
        _dispose_race_factories(engines, session_class, listener)
