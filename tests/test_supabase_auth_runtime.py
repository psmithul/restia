from __future__ import annotations

import pytest

from src.supabase_auth import SupabaseAuthConfigurationError
from src.supabase_auth_runtime import build_supabase_verifier_from_env


def test_supabase_runtime_is_optional_without_partial_configuration():
    assert build_supabase_verifier_from_env({}) is None
    with pytest.raises(SupabaseAuthConfigurationError, match="requires"):
        build_supabase_verifier_from_env({
            "RESTIA_SUPABASE_AUDIENCE": "authenticated",
        })


def test_supabase_runtime_derives_only_project_owned_verification_endpoints():
    verifier = build_supabase_verifier_from_env({
        "RESTIA_SUPABASE_PROJECT_URL": "https://example.supabase.co",
    })
    try:
        assert verifier.project_url == "https://example.supabase.co"
        assert verifier.issuer == "https://example.supabase.co/auth/v1"
        assert verifier.jwks_url == (
            "https://example.supabase.co/auth/v1/.well-known/jwks.json"
        )
        assert verifier.audience == "authenticated"
    finally:
        verifier.close()


@pytest.mark.parametrize(
    "project_url",
    [
        "http://example.supabase.co",
        "https://user:pass@example.supabase.co",
        "https://example.supabase.co/auth/v1",
        "https://example.supabase.co?redirect=evil",
        " https://example.supabase.co",
    ],
)
def test_supabase_runtime_rejects_ambiguous_project_urls(project_url):
    with pytest.raises(SupabaseAuthConfigurationError):
        build_supabase_verifier_from_env({
            "RESTIA_SUPABASE_PROJECT_URL": project_url,
        })
