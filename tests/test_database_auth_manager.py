from __future__ import annotations

import uuid
from datetime import datetime

import pyotp
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.auth import SetAdminResult
from core.database import (
    Account,
    AccountCapability,
    AccountRole,
    ApiToken,
    AuthIdentity,
    AuthPolicy,
    AuthSession,
    Base,
    LocalCredential,
    MfaFactor,
    MfaRecoveryCode,
    RetiredAuthSubject,
)
from src.database_auth_manager import DatabaseAuthManager


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
    ApiToken.__table__,
)


@pytest.fixture()
def manager_env(tmp_path, monkeypatch):
    from src import secret_storage

    monkeypatch.delenv("RESTIA_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("RESTIA_ENCRYPTION_KEY_FILE", raising=False)
    monkeypatch.setattr(secret_storage, "_KEY_PATH", tmp_path / ".app_key")
    monkeypatch.setattr(secret_storage, "_fernet", None)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'auth-manager.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine, tables=AUTH_TABLES)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    wall_clock = [1_789_000_000.0]

    def make_manager():
        return DatabaseAuthManager(
            factory,
            token_hmac_key=b"database-manager-test-key-material-32b",
            now=lambda: datetime.utcfromtimestamp(wall_clock[0]),
            wall_clock=lambda: wall_clock[0],
        )

    yield make_manager, factory, engine, wall_clock
    engine.dispose()
    secret_storage._fernet = None


def test_setup_login_and_sessions_are_shared_by_immutable_account(manager_env):
    make_manager, factory, _engine, _clock = manager_env
    first = make_manager()
    second = make_manager()

    assert first.is_configured is False
    assert first.setup(" Alice ", "correct horse battery staple") is True
    assert second.is_configured is True
    assert second.setup("other", "correct horse battery staple") is False

    token = first.create_session("ALICE", "correct horse battery staple")
    assert token.startswith("rst_s_")
    principal = second.resolve_session(token)
    assert principal is not None
    assert principal.username == "alice"
    assert principal.is_admin is True

    db = factory()
    account = db.query(Account).filter_by(username="alice").one()
    row = db.query(AuthSession).one()
    assert principal.account_id == account.id
    assert row.account_id == account.id
    assert token not in row.token_digest
    assert account.last_login_at is not None
    db.close()


def test_first_setup_can_adopt_exact_uncredentialed_foundation_account(manager_env):
    make_manager, factory, _engine, _clock = manager_env
    account_id = str(uuid.uuid4())
    db = factory()
    db.add(Account(
        id=account_id,
        username="alice",
        status="active",
        auth_epoch=1,
    ))
    db.add(AuthIdentity(
        id=str(uuid.uuid4()),
        account_id=account_id,
        provider="local",
        issuer="restia-local",
        subject="alice",
        state="active",
    ))
    db.commit()
    db.close()

    manager = make_manager()
    assert manager.is_configured is False
    assert manager.setup("alice", "correct horse battery staple") is True
    assert manager.create_session("alice", "correct horse battery staple")

    check = factory()
    assert check.query(Account).one().id == account_id
    assert check.query(LocalCredential).one().account_id == account_id
    check.close()


def test_completed_bootstrap_never_reopens_when_local_credentials_disappear(manager_env):
    make_manager, factory, _engine, _clock = manager_env
    manager = make_manager()
    assert manager.setup("alice", "correct horse battery staple") is True

    db = factory()
    db.query(LocalCredential).delete()
    db.commit()
    db.close()

    restarted = make_manager()
    assert restarted.is_configured is True
    assert restarted.setup("attacker", "another correct horse battery") is False
    check = factory()
    assert check.query(Account).filter_by(username="attacker").count() == 0
    check.close()


def test_signup_policy_roles_capabilities_and_last_admin_guard(manager_env):
    make_manager, _factory, _engine, _clock = manager_env
    manager = make_manager()
    assert manager.setup("admin", "correct horse battery staple")
    assert manager.signup_enabled is False
    manager.signup_enabled = True
    assert make_manager().signup_enabled is True
    assert manager.create_user(
        "alice",
        "correct horse battery staple",
        requesting_user="admin",
    )

    assert manager.set_privileges(
        "alice",
        {
            "can_use_bash": True,
            "max_messages_per_day": -1,
            "unknown": True,
        },
        requesting_user="admin",
    )
    privileges = manager.get_privileges("alice")
    assert privileges["can_use_bash"] is True
    assert privileges["max_messages_per_day"] == 0
    assert "unknown" not in privileges

    assert manager.set_admin("alice", True, "admin") is SetAdminResult.OK
    assert manager.is_admin("alice") is True
    assert manager.set_admin("admin", False, "admin") is SetAdminResult.OK
    assert manager.set_admin("alice", False, "alice") is SetAdminResult.LAST_ADMIN


def test_admin_demotion_atomically_revokes_privileged_api_tokens(manager_env):
    make_manager, factory, _engine, _clock = manager_env
    manager = make_manager()
    resolver = make_manager()
    assert manager.setup("admin", "correct horse battery staple")
    assert manager.create_user(
        "alice",
        "correct horse battery staple",
        is_admin=True,
        requesting_user="admin",
    )
    issued = manager.issue_api_token(
        "alice",
        name="host control",
        scopes=["cookbook:launch", "email:send"],
    )
    assert resolver.resolve_api_token(issued["token"]) is not None

    assert manager.set_admin("alice", False, "admin") is SetAdminResult.OK
    assert resolver.resolve_api_token(issued["token"]) is None
    db = factory()
    try:
        row = db.query(ApiToken).filter_by(id=issued["id"]).one()
        assert row.is_active is False
        assert row.revoked_at is not None
    finally:
        db.close()


def test_open_signup_cannot_request_an_admin_role(manager_env):
    make_manager, _factory, _engine, _clock = manager_env
    manager = make_manager()
    assert manager.setup("admin", "correct horse battery staple")
    assert manager.set_signup_enabled(True, "admin") is True

    assert manager.create_user(
        "alice",
        "correct horse battery staple",
        is_admin=True,
    ) is True
    assert manager.is_admin("alice") is False

    assert manager.create_user(
        "bob",
        "correct horse battery staple",
        is_admin=True,
        requesting_user="admin",
    ) is True
    assert manager.is_admin("bob") is True


def test_missing_or_malformed_capability_row_is_locked_not_defaulted(manager_env):
    make_manager, factory, _engine, _clock = manager_env
    manager = make_manager()
    assert manager.setup("admin", "correct horse battery staple")
    assert manager.create_user(
        "alice",
        "correct horse battery staple",
        requesting_user="admin",
    )
    db = factory()
    account_id = db.query(Account).filter_by(username="alice").one().id
    db.query(AccountCapability).filter_by(account_id=account_id).delete()
    db.commit()
    db.close()

    assert manager.get_privileges("alice")["block_all_models"] is True
    db = factory()
    db.add(AccountCapability(
        account_id=account_id,
        capabilities={"can_use_bash": "yes"},
    ))
    db.commit()
    db.close()
    assert manager.get_privileges("alice")["block_all_models"] is True


def test_rename_and_rollback_keep_sessions_bound_to_same_account(manager_env):
    make_manager, _factory, _engine, _clock = manager_env
    manager = make_manager()
    assert manager.setup("admin", "correct horse battery staple")
    assert manager.create_user(
        "alice",
        "correct horse battery staple",
        requesting_user="admin",
    )
    token = manager.create_session_trusted("alice")
    before = manager.resolve_session(token)

    assert manager.rename_user("alice", "alice2", "admin") is True
    after = make_manager().resolve_session(token)
    assert after.account_id == before.account_id
    assert after.username == "alice2"
    assert "alice" in manager.retired_usernames
    assert manager.create_user(
        "alice",
        "another correct password",
        requesting_user="admin",
    ) is False

    assert manager.rollback_user_rename("alice2", "alice", "admin") is True
    restored = manager.resolve_session(token)
    assert restored.account_id == before.account_id
    assert restored.username == "alice"
    assert "alice" not in manager.retired_usernames


def test_delete_disables_principal_and_every_credential(manager_env):
    make_manager, factory, _engine, clock = manager_env
    manager = make_manager()
    assert manager.setup("admin", "correct horse battery staple")
    assert manager.create_user(
        "alice",
        "correct horse battery staple",
        requesting_user="admin",
    )
    token = manager.create_session_trusted("alice")
    account_id = manager.resolve_session(token).account_id
    db = factory()
    db.add(ApiToken(
        id="alice-token",
        owner="alice",
        account_id=account_id,
        name="Alice API",
        token_hash="unused",
        token_prefix="ody_test",
        scopes="chat",
        is_active=True,
    ))
    db.commit()
    db.close()

    assert manager.delete_user("alice", "admin") is True
    assert manager.resolve_session(token) is None
    assert manager.verify_password("alice", "correct horse battery staple") is False
    assert manager.create_user(
        "alice",
        "another correct password",
        requesting_user="admin",
    ) is False

    check = factory()
    assert check.query(Account).filter_by(id=account_id).one().status == "deleted"
    api = check.query(ApiToken).one()
    assert api.is_active is False
    assert api.revoked_at == datetime.utcfromtimestamp(clock[0])
    assert check.query(LocalCredential).filter_by(account_id=account_id).count() == 0
    check.close()


def test_password_change_revokes_other_sessions_but_preserves_current(manager_env):
    make_manager, _factory, _engine, _clock = manager_env
    manager = make_manager()
    assert manager.setup("alice", "correct horse battery staple")
    current = manager.create_session_trusted("alice")
    other = manager.create_session_trusted("alice")

    assert manager.change_password(
        "alice",
        "correct horse battery staple",
        "new correct horse battery staple",
        current,
    )
    assert manager.resolve_session(current) is not None
    assert manager.resolve_session(other) is None
    assert not manager.verify_password("alice", "correct horse battery staple")
    assert manager.verify_password("alice", "new correct horse battery staple")


def test_api_tokens_are_hmac_protected_account_bound_and_cross_process(manager_env):
    make_manager, factory, _engine, _clock = manager_env
    issuer = make_manager()
    resolver = make_manager()
    assert issuer.setup("alice", "correct horse battery staple")

    issued = issuer.issue_api_token(
        "alice",
        name="Companion",
        scopes=["chat", "chat", "todos:read"],
    )
    principal = resolver.resolve_api_token(issued["token"])
    assert principal is not None
    assert principal.username == "alice"
    assert principal.scopes == ("chat", "todos:read")

    db = factory()
    row = db.query(ApiToken).one()
    assert row.account_id == principal.account_id
    assert row.digest_scheme == "hmac_sha256_v1"
    assert issued["token"] not in row.token_hash
    db.close()


def test_linked_supabase_identity_issues_same_account_session_across_interfaces(manager_env):
    make_manager, factory, _engine, _clock = manager_env
    linker = make_manager()
    login = make_manager()
    assert linker.setup("alice", "correct horse battery staple")
    local = linker.authenticate_session(
        "alice", "correct horse battery staple", interface="web"
    )
    assert local.account_id

    assert linker.link_external_identity(
        local.token,
        current_password="correct horse battery staple",
        provider="supabase",
        issuer="https://project.supabase.co/auth/v1",
        subject="Opaque-Subject-ABC",
    ) is True
    external = login.authenticate_external_identity(
        provider="supabase",
        issuer="https://project.supabase.co/auth/v1",
        subject="Opaque-Subject-ABC",
        interface="mobile",
    )
    assert external.token
    assert external.account_id == local.account_id
    assert login.resolve_session(external.token).username == "alice"

    db = factory()
    row = db.query(AuthSession).filter_by(
        token_digest=login._repository.session_token_digest(external.token)
    ).one()
    identity = db.query(AuthIdentity).filter_by(
        provider="supabase",
        issuer="https://project.supabase.co/auth/v1",
        subject="Opaque-Subject-ABC",
    ).one()
    assert row.auth_method == "supabase"
    assert row.interface == "mobile"
    assert row.source_identity_id == identity.id
    assert identity.last_verified_at is not None
    db.close()


def test_external_link_requires_password_and_active_restia_mfa(manager_env):
    make_manager, _factory, _engine, clock = manager_env
    manager = make_manager()
    assert manager.setup("alice", "correct horse battery staple")
    local = manager.authenticate_session(
        "alice", "correct horse battery staple"
    )
    secret = manager.totp_generate_secret("alice")
    assert manager.totp_confirm_enable(
        "alice",
        pyotp.TOTP(secret).at(clock[0]),
        "correct horse battery staple",
        local.token,
    )
    clock[0] += 30

    link_kwargs = {
        "provider": "supabase",
        "issuer": "https://project.supabase.co/auth/v1",
        "subject": "step-up-subject",
    }
    assert manager.link_external_identity(
        local.token,
        current_password="wrong password",
        totp_code=pyotp.TOTP(secret).at(clock[0]),
        **link_kwargs,
    ) is False
    assert manager.link_external_identity(
        local.token,
        current_password="correct horse battery staple",
        **link_kwargs,
    ) is False
    assert manager.link_external_identity(
        local.token,
        current_password="correct horse battery staple",
        totp_code=pyotp.TOTP(secret).at(clock[0]),
        **link_kwargs,
    ) is True

    challenge = manager.authenticate_external_identity(**link_kwargs)
    assert challenge.requires_totp is True
    assert challenge.token is None
    assert manager.authenticate_external_identity(
        **link_kwargs, totp_code="000000"
    ).token is None
    clock[0] += 30
    external = manager.authenticate_external_identity(
        **link_kwargs,
        totp_code=pyotp.TOTP(secret).at(clock[0]),
        interface="mobile",
    )
    assert external.token
    assert manager.resolve_session(external.token).username == "alice"


def test_password_rotation_revokes_external_sessions_and_requires_relink(manager_env):
    make_manager, factory, _engine, _clock = manager_env
    manager = make_manager()
    assert manager.setup("alice", "correct horse battery staple")
    local = manager.authenticate_session(
        "alice", "correct horse battery staple"
    )
    identity = {
        "provider": "supabase",
        "issuer": "https://project.supabase.co/auth/v1",
        "subject": "password-rotation-subject",
    }
    assert manager.link_external_identity(
        local.token,
        current_password="correct horse battery staple",
        **identity,
    )
    external = manager.authenticate_external_identity(**identity)
    assert external.token

    assert manager.change_password(
        "alice",
        "correct horse battery staple",
        "new correct horse battery staple",
        local.token,
    )
    assert manager.resolve_session(local.token) is not None
    assert manager.resolve_session(external.token) is None
    assert manager.authenticate_external_identity(**identity).token is None
    db = factory()
    try:
        assert db.query(AuthIdentity).filter_by(
            provider="supabase",
            subject="password-rotation-subject",
        ).one().state == "unlinked"
    finally:
        db.close()

    assert manager.link_external_identity(
        local.token,
        current_password="new correct horse battery staple",
        **identity,
    )
    assert manager.authenticate_external_identity(**identity).token


def test_external_identity_never_links_by_matching_username_or_cross_account(manager_env):
    make_manager, _factory, _engine, _clock = manager_env
    manager = make_manager()
    assert manager.setup("admin", "correct horse battery staple")
    assert manager.create_user(
        "alice",
        "correct horse battery staple",
        requesting_user="admin",
    )
    admin_session = manager.create_session_trusted("admin")
    alice_session = manager.create_session_trusted("alice")
    admin_id = manager.resolve_session(admin_session).account_id

    # The external subject may look exactly like a local username; it has no
    # authority until explicitly linked to an immutable account UUID.
    unlinked = manager.authenticate_external_identity(
        provider="supabase",
        issuer="https://project.supabase.co/auth/v1",
        subject="alice",
    )
    assert unlinked.token is None
    assert manager.link_external_identity(
        admin_session,
        current_password="correct horse battery staple",
        provider="supabase",
        issuer="https://project.supabase.co/auth/v1",
        subject="alice",
    ) is True
    assert manager.link_external_identity(
        alice_session,
        current_password="correct horse battery staple",
        provider="supabase",
        issuer="https://project.supabase.co/auth/v1",
        subject="alice",
    ) is False
    linked = manager.authenticate_external_identity(
        provider="supabase",
        issuer="https://project.supabase.co/auth/v1",
        subject="alice",
    )
    assert linked.account_id == admin_id


def test_external_identity_link_rechecks_session_revocation_in_transaction(manager_env):
    make_manager, _factory, _engine, _clock = manager_env
    manager = make_manager()
    assert manager.setup("alice", "correct horse battery staple")
    session_token = manager.create_session_trusted("alice")
    manager.revoke_token(session_token)

    assert manager.link_external_identity(
        session_token,
        current_password="correct horse battery staple",
        provider="supabase",
        issuer="https://project.supabase.co/auth/v1",
        subject="subject",
    ) is False
    assert manager.authenticate_external_identity(
        provider="supabase",
        issuer="https://project.supabase.co/auth/v1",
        subject="subject",
    ).token is None


@pytest.mark.parametrize(
    ("provider", "issuer", "subject"),
    [
        ("unknown", "https://project.supabase.co/auth/v1", "subject"),
        ("supabase", "http://project.supabase.co/auth/v1", "subject"),
        ("supabase", "https://project.supabase.co/auth/v1?wrong=1", "subject"),
        ("supabase", "https://project.supabase.co/auth/v1", "bad\nsubject"),
    ],
)
def test_external_identity_manager_boundary_rejects_untrusted_values(
    manager_env, provider, issuer, subject
):
    make_manager, _factory, _engine, _clock = manager_env
    manager = make_manager()
    assert manager.setup("alice", "correct horse battery staple")
    session_token = manager.create_session_trusted("alice")
    assert manager.link_external_identity(
        session_token,
        current_password="correct horse battery staple",
        provider=provider,
        issuer=issuer,
        subject=subject,
    ) is False
    assert manager.authenticate_external_identity(
        provider=provider,
        issuer=issuer,
        subject=subject,
    ).token is None


def test_totp_secret_is_encrypted_and_recovery_code_is_single_use(manager_env):
    make_manager, factory, engine, clock = manager_env
    manager = make_manager()
    assert manager.setup("alice", "correct horse battery staple")
    secret = manager.totp_generate_secret("alice")
    confirm_code = pyotp.TOTP(secret).at(clock[0])
    assert manager.totp_confirm_enable(
        "alice", confirm_code, "wrong password"
    ) is None
    assert manager.totp_enabled("alice") is False
    recovery = manager.totp_confirm_enable(
        "alice", confirm_code, "correct horse battery staple"
    )
    assert recovery and len(recovery) == 8
    assert manager.create_session("alice", "correct horse battery staple") is None
    assert manager.create_session_trusted("alice") is None

    with engine.connect() as connection:
        raw_secret = connection.execute(
            text("SELECT secret FROM mfa_factors")
        ).scalar_one()
        raw_codes = [
            row[0] for row in connection.execute(
                text("SELECT code_hash FROM mfa_recovery_codes")
            ).all()
        ]
    assert secret not in raw_secret
    assert raw_secret.startswith("enc:")
    assert all(code not in raw_codes for code in recovery)

    assert manager.totp_verify("alice", recovery[0]) is True
    assert manager.totp_verify("alice", recovery[0]) is False
    clock[0] += 30
    next_code = pyotp.TOTP(secret).at(clock[0])
    assert manager.totp_verify("alice", next_code) is True
    assert manager.totp_verify("alice", next_code) is False

    check = factory()
    assert check.query(MfaRecoveryCode).filter(
        MfaRecoveryCode.used_at.isnot(None)
    ).count() == 1
    check.close()


def test_atomic_login_requires_mfa_and_issues_only_after_factor(manager_env):
    make_manager, _factory, _engine, clock = manager_env
    manager = make_manager()
    assert manager.setup("alice", "correct horse battery staple")
    secret = manager.totp_generate_secret("alice")
    assert manager.totp_confirm_enable(
        "alice",
        pyotp.TOTP(secret).at(clock[0]),
        "correct horse battery staple",
    )
    clock[0] += 30

    needs = manager.authenticate_session(
        "alice", "correct horse battery staple"
    )
    assert needs.requires_totp is True
    assert needs.token is None

    invalid = manager.authenticate_session(
        "alice",
        "correct horse battery staple",
        totp_code="000000",
    )
    assert invalid.token is None
    valid = manager.authenticate_session(
        "alice",
        "correct horse battery staple",
        totp_code=pyotp.TOTP(secret).at(clock[0]),
    )
    assert valid.token
    assert manager.resolve_session(valid.token).account_id == valid.account_id


def test_future_window_enrollment_code_cannot_be_reused_for_login(manager_env):
    make_manager, _factory, _engine, clock = manager_env
    manager = make_manager()
    assert manager.setup("alice", "correct horse battery staple")
    secret = manager.totp_generate_secret("alice")
    future_code = pyotp.TOTP(secret).at(clock[0] + 30)

    assert manager.totp_confirm_enable(
        "alice", future_code, "correct horse battery staple"
    )
    clock[0] += 30
    replay = manager.authenticate_session(
        "alice",
        "correct horse battery staple",
        totp_code=future_code,
    )
    assert replay.token is None


def test_totp_disable_requires_current_password_and_removes_factor_atomically(manager_env):
    make_manager, _factory, _engine, clock = manager_env
    manager = make_manager()
    assert manager.setup("alice", "correct horse battery staple")
    secret = manager.totp_generate_secret("alice")
    assert manager.totp_confirm_enable(
        "alice",
        pyotp.TOTP(secret).at(clock[0]),
        "correct horse battery staple",
    )

    assert manager.totp_disable("alice", "wrong password") is False
    assert manager.totp_enabled("alice") is True
    assert manager.totp_disable(
        "alice", "correct horse battery staple"
    ) is True
    assert manager.totp_enabled("alice") is False


def test_mfa_policy_changes_rotate_sessions_but_preserve_current(manager_env):
    make_manager, _factory, _engine, clock = manager_env
    manager = make_manager()
    assert manager.setup("alice", "correct horse battery staple")
    current = manager.create_session_trusted("alice")
    pre_mfa_stolen = manager.create_session_trusted("alice")
    secret = manager.totp_generate_secret("alice")
    assert manager.totp_confirm_enable(
        "alice",
        pyotp.TOTP(secret).at(clock[0]),
        "correct horse battery staple",
        current,
    )
    assert manager.resolve_session(current) is not None
    assert manager.resolve_session(pre_mfa_stolen) is None

    clock[0] += 30
    second = manager.authenticate_session(
        "alice",
        "correct horse battery staple",
        totp_code=pyotp.TOTP(secret).at(clock[0]),
    ).token
    assert second
    assert manager.totp_disable(
        "alice",
        "correct horse battery staple",
        current,
    )
    assert manager.resolve_session(current) is not None
    assert manager.resolve_session(second) is None


def test_store_error_is_configured_but_fails_every_auth_path(manager_env):
    _make_manager, factory, _engine, _clock = manager_env
    manager = DatabaseAuthManager(
        factory,
        token_hmac_key=b"database-manager-test-key-material-32b",
        store_error=True,
    )

    assert manager.is_configured is True
    assert manager.auth_store_error is True
    assert manager.setup("admin", "correct horse battery staple") is False
    assert manager.create_user("alice", "correct horse battery staple") is False
    assert manager.verify_password("alice", "correct horse battery staple") is False
    assert manager.status(None)["auth_store_error"] is True
    assert manager.get_privileges("alice")["block_all_models"] is True
