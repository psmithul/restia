from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta

import bcrypt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    AccountCapability,
    AccountRole,
    ApiToken,
    AuthIdentity,
    AuthSession,
    Base,
    LocalCredential,
)
from src.database_auth import (
    DatabaseAuthRepository,
    HMAC_SHA256_V1,
    LEGACY_API_BCRYPT,
    LEGACY_SESSION_SHA256,
)


TABLES = (
    Account.__table__,
    AuthIdentity.__table__,
    LocalCredential.__table__,
    AccountRole.__table__,
    AccountCapability.__table__,
    AuthSession.__table__,
    ApiToken.__table__,
)


@pytest.fixture()
def auth_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'database-auth.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine, tables=TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    fixed_now = datetime(2026, 7, 17, 4, 30, 0)
    repo = DatabaseAuthRepository(
        factory,
        token_hmac_key=b"database-auth-test-key-material-32b",
        default_capabilities={"can_use_agent": True, "can_use_bash": False},
        admin_capabilities={"can_use_agent": True, "can_use_bash": True},
        now=lambda: fixed_now,
    )

    account_id = str(uuid.uuid4())
    credential_id = str(uuid.uuid4())
    db = factory()
    db.add(Account(
        id=account_id,
        username="alice",
        status="active",
        auth_epoch=1,
    ))
    db.flush()
    db.add_all([
        AuthIdentity(
            id=str(uuid.uuid4()),
            account_id=account_id,
            provider="local",
            issuer="restia-local",
            subject="alice",
            state="active",
        ),
        LocalCredential(
            id=credential_id,
            account_id=account_id,
            password_hash=bcrypt.hashpw(
                b"correct horse battery staple",
                bcrypt.gensalt(rounds=4),
            ).decode("ascii"),
            algorithm="bcrypt",
        ),
        AccountRole(
            id=str(uuid.uuid4()),
            account_id=account_id,
            role="member",
        ),
        AccountCapability(
            account_id=account_id,
            capabilities={"can_use_agent": False},
        ),
    ])
    db.commit()
    db.close()
    yield repo, factory, account_id, fixed_now
    engine.dispose()


def test_local_credential_resolves_immutable_account_and_capabilities(auth_env):
    repo, _factory, account_id, _now = auth_env

    principal = repo.verify_local_credential(
        "  ALICE ",
        "correct horse battery staple",
    )

    assert principal is not None
    assert principal.account_id == account_id
    assert principal.username == "alice"
    assert principal.roles == ("member",)
    assert principal.capabilities == {
        "can_use_agent": False,
        "can_use_bash": False,
    }
    assert principal.credential_type == "local_password"
    assert repo.verify_local_credential("alice", "wrong") is None
    assert repo.verify_local_credential("missing", "anything") is None


def test_admin_role_overlays_without_destroying_stored_capabilities(auth_env):
    repo, factory, account_id, _now = auth_env
    db = factory()
    db.add(AccountRole(
        id=str(uuid.uuid4()),
        account_id=account_id,
        role="admin",
    ))
    db.commit()
    db.close()

    roles, capabilities = repo.effective_authorization(account_id)

    assert roles == ("admin", "member")
    assert capabilities["can_use_agent"] is True
    assert capabilities["can_use_bash"] is True
    check = factory()
    assert check.query(AccountCapability).one().capabilities == {
        "can_use_agent": False
    }
    check.close()


def test_hmac_session_is_resolved_revoked_and_never_stored_raw(auth_env):
    repo, factory, account_id, now = auth_env
    issued = repo.create_session(account_id, ttl_seconds=3600, interface="desktop")

    db = factory()
    row = db.query(AuthSession).filter(AuthSession.id == issued.session_id).one()
    assert row.token_digest != issued.token
    assert issued.token not in row.token_digest
    assert row.digest_scheme == HMAC_SHA256_V1
    assert row.expires_at == now + timedelta(hours=1)
    db.close()

    principal = repo.resolve_session(issued.token)
    assert principal is not None
    assert principal.account_id == account_id
    assert principal.credential_id == issued.session_id
    assert repo.revoke_session(issued.token) is True
    assert repo.resolve_session(issued.token) is None
    assert repo.revoke_session(issued.token) is False


def test_auth_epoch_revokes_other_sessions_and_can_preserve_current(auth_env):
    repo, _factory, account_id, _now = auth_env
    current = repo.create_session(account_id, ttl_seconds=3600)
    other = repo.create_session(account_id, ttl_seconds=3600)

    assert repo.increment_auth_epoch(
        account_id,
        preserve_session_id=current.session_id,
    ) == 2

    assert repo.resolve_session(current.token) is not None
    assert repo.resolve_session(other.token) is None


def test_legacy_sha256_session_survives_database_import(auth_env):
    repo, factory, account_id, now = auth_env
    raw = "a" * 64
    digest = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    db = factory()
    db.add(AuthSession(
        id=str(uuid.uuid4()),
        account_id=account_id,
        token_digest=digest,
        digest_scheme=LEGACY_SESSION_SHA256,
        auth_epoch=1,
        expires_at=now + timedelta(hours=1),
    ))
    db.commit()
    db.close()

    assert repo.resolve_session(raw).account_id == account_id


def test_disabled_account_invalidates_session_without_deleting_it(auth_env):
    repo, factory, account_id, _now = auth_env
    issued = repo.create_session(account_id, ttl_seconds=3600)
    db = factory()
    db.query(Account).filter(Account.id == account_id).update({"status": "disabled"})
    db.commit()
    db.close()

    assert repo.resolve_session(issued.token) is None


def test_api_token_resolution_supports_legacy_bcrypt_and_hmac(auth_env):
    repo, factory, account_id, _now = auth_env
    legacy_raw = "ody_legacy-token-secret-material"
    hmac_raw = "ody_hmac-token-secret-material"
    db = factory()
    db.add_all([
        ApiToken(
            id="legacy-token",
            owner="alice",
            account_id=account_id,
            name="Legacy",
            token_hash=bcrypt.hashpw(
                legacy_raw.encode("utf-8"), bcrypt.gensalt(rounds=4)
            ).decode("ascii"),
            token_prefix=legacy_raw[:8],
            digest_scheme=LEGACY_API_BCRYPT,
            scopes="chat,todos:read,chat",
            is_active=True,
        ),
        ApiToken(
            id="hmac-token",
            owner="alice",
            account_id=account_id,
            name="HMAC",
            token_hash=repo.api_token_digest(hmac_raw),
            token_prefix=hmac_raw[:8],
            digest_scheme=HMAC_SHA256_V1,
            scopes="todos:read,todos:write",
            is_active=True,
        ),
    ])
    db.commit()
    db.close()

    legacy = repo.resolve_api_token(legacy_raw)
    protected = repo.resolve_api_token(hmac_raw)
    assert legacy.account_id == account_id
    assert legacy.scopes == ("chat", "todos:read")
    assert protected.account_id == account_id
    assert protected.scopes == ("todos:read", "todos:write")
    assert repo.resolve_api_token("ody_wrong-token-secret-material") is None


def test_api_token_without_account_mapping_never_trusts_legacy_owner(auth_env):
    repo, factory, _account_id, _now = auth_env
    raw = "ody_owner-only-token-secret"
    db = factory()
    db.add(ApiToken(
        id="owner-only",
        owner="alice",
        account_id=None,
        name="Owner-only",
        token_hash=bcrypt.hashpw(
            raw.encode("utf-8"), bcrypt.gensalt(rounds=4)
        ).decode("ascii"),
        token_prefix=raw[:8],
        digest_scheme=LEGACY_API_BCRYPT,
        scopes="chat",
        is_active=True,
    ))
    db.commit()
    db.close()

    assert repo.resolve_api_token(raw) is None


def test_short_hmac_keys_are_rejected(auth_env):
    _repo, factory, _account_id, _now = auth_env
    with pytest.raises(ValueError, match="at least 32 bytes"):
        DatabaseAuthRepository(factory, token_hmac_key=b"too-short")


def test_unbounded_or_malformed_tokens_are_rejected_before_crypto(auth_env):
    repo, _factory, _account_id, _now = auth_env

    for raw in (
        "legacy-browser-session-secret",
        "rst_s_short",
        "rst_s_" + ("é" * 40),
        "x" * 10_000,
    ):
        assert repo.resolve_session(raw) is None
        assert repo.revoke_session(raw) is False

    for raw in (
        "not-an-api-token",
        "ody_short",
        "ody_" + ("é" * 40),
        "ody_" + ("a" * 10_000),
    ):
        assert repo.resolve_api_token(raw) is None


def test_missing_user_still_performs_dummy_bcrypt_verification(
    auth_env,
    monkeypatch,
):
    repo, _factory, _account_id, _now = auth_env
    calls = []

    def record_check(password, protected):
        calls.append((password, protected))
        return False

    monkeypatch.setattr("src.database_auth.bcrypt.checkpw", record_check)

    assert repo.verify_local_credential("missing", "guess") is None
    assert len(calls) == 1
    assert calls[0][0] == b"guess"
    assert calls[0][1].startswith(b"$2b$12$")
