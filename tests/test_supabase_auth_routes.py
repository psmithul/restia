from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.auth_routes import SESSION_COOKIE, setup_auth_routes
from src.database_auth_manager import DatabaseLoginResult
from src.supabase_auth import (
    SupabaseJWTIdentity,
    SupabaseJWTVerificationError,
)


class _Verifier:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.tokens = []

    def verify(self, token):
        self.tokens.append(token)
        if self.fail:
            raise SupabaseJWTVerificationError("invalid_signature")
        return SupabaseJWTIdentity(
            issuer="https://project.supabase.co/auth/v1",
            subject="Opaque-Subject",
            audience="authenticated",
            expires_at=2_000_000_000,
            auth_provider="supabase",
            credential_type="asymmetric_jwt",
            algorithm="RS256",
            key_id="key-1",
        )


class _Manager:
    def __init__(self):
        self.external_calls = []
        self.link_calls = []
        self.link_ok = True
        self.requires_totp = False

    def authenticate_external_identity(self, **kwargs):
        self.external_calls.append(kwargs)
        if self.requires_totp and not kwargs.get("totp_code"):
            return DatabaseLoginResult(requires_totp=True)
        return DatabaseLoginResult(
            token="rst_s_secret-session",
            username="alice",
            account_id="account-1",
        )

    def resolve_session(self, token):
        if token == "rst_s_existing-session":
            return SimpleNamespace(account_id="account-1", username="alice")
        return None

    def link_external_identity(self, session_token, **kwargs):
        self.link_calls.append((session_token, kwargs))
        return self.link_ok


def _client(manager, verifier):
    app = FastAPI()
    app.include_router(
        setup_auth_routes(
            manager,
            identity_renamer=lambda *_args, **_kwargs: None,
            supabase_verifier=verifier,
        )
    )
    return TestClient(app)


def test_supabase_login_exchanges_verified_identity_without_exposing_token():
    manager = _Manager()
    verifier = _Verifier()
    response = _client(manager, verifier).post(
        "/api/auth/external/supabase/login",
        json={"access_token": "verified-jwt", "remember": True},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "username": "alice",
        "account_id": "account-1",
        "provider": "supabase",
    }
    assert "verified-jwt" not in response.text
    assert verifier.tokens == ["verified-jwt"]
    assert manager.external_calls == [{
        "provider": "supabase",
        "issuer": "https://project.supabase.co/auth/v1",
        "subject": "Opaque-Subject",
        "totp_code": None,
        "interface": "web",
    }]
    cookie = response.headers["set-cookie"]
    assert f"{SESSION_COOKIE}=rst_s_secret-session" in cookie
    assert "HttpOnly" in cookie


def test_supabase_login_sanitizes_verification_failures():
    response = _client(_Manager(), _Verifier(fail=True)).post(
        "/api/auth/external/supabase/login",
        json={"access_token": "bad-jwt"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid external credentials"}


def test_supabase_link_rechecks_the_existing_database_session():
    manager = _Manager()
    verifier = _Verifier()
    client = _client(manager, verifier)
    client.cookies.set(SESSION_COOKIE, "rst_s_existing-session")
    response = client.post(
        "/api/auth/external/supabase/link",
        json={
            "access_token": "verified-jwt",
            "current_password": "correct horse battery staple",
        },
    )

    assert response.status_code == 200
    assert manager.link_calls == [(
        "rst_s_existing-session",
        {
            "current_password": "correct horse battery staple",
            "totp_code": None,
            "provider": "supabase",
            "issuer": "https://project.supabase.co/auth/v1",
            "subject": "Opaque-Subject",
        },
    )]


def test_supabase_link_requires_session_and_refuses_cross_account_collision():
    manager = _Manager()
    verifier = _Verifier()
    client = _client(manager, verifier)
    assert client.post(
        "/api/auth/external/supabase/link",
        json={
            "access_token": "verified-jwt",
            "current_password": "correct horse battery staple",
        },
    ).status_code == 401

    client.cookies.set(SESSION_COOKIE, "rst_s_existing-session")
    manager.link_ok = False
    response = client.post(
        "/api/auth/external/supabase/link",
        json={
            "access_token": "verified-jwt",
            "current_password": "correct horse battery staple",
        },
    )
    assert response.status_code == 403
    assert response.json() == {
        "detail": "External identity link authorization failed"
    }


def test_supabase_login_requires_restia_mfa_when_account_policy_requires_it():
    manager = _Manager()
    manager.requires_totp = True
    client = _client(manager, _Verifier())

    challenge = client.post(
        "/api/auth/external/supabase/login",
        json={"access_token": "verified-jwt"},
    )
    assert challenge.status_code == 200
    assert challenge.json() == {"ok": False, "requires_totp": True}
    assert SESSION_COOKIE not in challenge.cookies

    completed = client.post(
        "/api/auth/external/supabase/login",
        json={"access_token": "verified-jwt", "totp_code": "123456"},
    )
    assert completed.status_code == 200
    assert completed.json()["ok"] is True
    assert manager.external_calls[-1]["totp_code"] == "123456"


def test_supabase_link_requires_explicit_local_step_up_fields():
    client = _client(_Manager(), _Verifier())
    client.cookies.set(SESSION_COOKIE, "rst_s_existing-session")
    response = client.post(
        "/api/auth/external/supabase/link",
        json={"access_token": "verified-jwt"},
    )
    assert response.status_code == 422


def test_supabase_routes_fail_closed_when_adapter_is_not_configured():
    response = _client(_Manager(), None).post(
        "/api/auth/external/supabase/login",
        json={"access_token": "anything"},
    )
    assert response.status_code == 503
