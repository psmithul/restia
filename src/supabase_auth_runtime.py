"""Optional process-wide Supabase verifier configuration.

Supabase is an identity provider and optional managed PostgreSQL host here; it
is not a second Restia data authority.  The browser never receives database
credentials and never reads Restia tables directly.  A verified Supabase
subject must be explicitly linked to an existing immutable ``Account.id``.
"""

from __future__ import annotations

import os
import threading
from urllib.parse import urlsplit

from src.supabase_auth import (
    SupabaseAuthConfigurationError,
    SupabaseJWTVerifier,
)


PROJECT_URL_ENV = "RESTIA_SUPABASE_PROJECT_URL"
AUDIENCE_ENV = "RESTIA_SUPABASE_AUDIENCE"
DEFAULT_AUDIENCE = "authenticated"

_lock = threading.Lock()
_verifier: SupabaseJWTVerifier | None = None
_configured = False


def _project_origin(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SupabaseAuthConfigurationError(
            f"{PROJECT_URL_ENV} must be a non-empty exact HTTPS project origin"
        )
    try:
        parsed = urlsplit(value)
        parsed.port
    except (TypeError, ValueError) as exc:
        raise SupabaseAuthConfigurationError(
            f"{PROJECT_URL_ENV} must be a valid HTTPS project origin"
        ) from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise SupabaseAuthConfigurationError(
            f"{PROJECT_URL_ENV} must be a credential-free HTTPS project origin"
        )
    return value.rstrip("/")


def build_supabase_verifier_from_env(
    environ: dict[str, str] | os._Environ[str] | None = None,
) -> SupabaseJWTVerifier | None:
    """Build the verifier from one public project URL, or disable it cleanly."""

    env = os.environ if environ is None else environ
    raw_project_url = env.get(PROJECT_URL_ENV)
    raw_audience = env.get(AUDIENCE_ENV)
    if not raw_project_url:
        if raw_audience:
            raise SupabaseAuthConfigurationError(
                f"{AUDIENCE_ENV} requires {PROJECT_URL_ENV}"
            )
        return None
    project_url = _project_origin(raw_project_url)
    audience = raw_audience or DEFAULT_AUDIENCE
    issuer = f"{project_url}/auth/v1"
    return SupabaseJWTVerifier(
        project_url=project_url,
        issuer=issuer,
        audience=audience,
        jwks_url=f"{issuer}/.well-known/jwks.json",
    )


def get_supabase_verifier() -> SupabaseJWTVerifier | None:
    global _configured, _verifier
    if _configured:
        return _verifier
    with _lock:
        if not _configured:
            _verifier = build_supabase_verifier_from_env()
            _configured = True
    return _verifier


def _reset_supabase_verifier_for_tests() -> None:
    global _configured, _verifier
    with _lock:
        if _verifier is not None:
            _verifier.close()
        _verifier = None
        _configured = False


__all__ = [
    "AUDIENCE_ENV",
    "DEFAULT_AUDIENCE",
    "PROJECT_URL_ENV",
    "build_supabase_verifier_from_env",
    "get_supabase_verifier",
]
