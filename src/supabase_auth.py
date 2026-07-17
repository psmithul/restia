"""Strict, optional Supabase access-token verification.

This module is intentionally an adapter, not an authorization system.  It
verifies one explicitly configured Supabase project's asymmetric JWTs and
returns only the opaque identity and cryptographic metadata needed by a later
account-linking boundary.  Email addresses, roles, app metadata, and user
metadata never leave this verifier.

The JWKS URL is configuration, never token input.  Requests are bounded,
redirects are disabled, and cached keys live for at most ten minutes.
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import httpx
import jwt


ALLOWED_JWT_ALGORITHMS = frozenset({"ES256", "RS256"})
MAX_JWKS_CACHE_SECONDS = 10 * 60
MAX_JWKS_RESPONSE_BYTES = 256 * 1024
MAX_JWKS_KEYS = 64
MAX_JWT_BYTES = 32 * 1024
MAX_UNKNOWN_KEY_IDS = 128
UNKNOWN_KID_NEGATIVE_CACHE_SECONDS = 30.0
UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS = 5.0

_FORBIDDEN_TOKEN_KEY_HEADERS = frozenset({"jku", "x5u", "jwk", "x5c"})
_PRIVATE_JWK_FIELDS = frozenset({"d", "p", "q", "dp", "dq", "qi", "oth"})
_ERROR_MESSAGES = {
    "invalid_token": "Supabase token is invalid",
    "invalid_algorithm": "Supabase token algorithm is not allowed",
    "invalid_header": "Supabase token header is invalid",
    "missing_claim": "Supabase token is missing a required claim",
    "expired": "Supabase token has expired",
    "invalid_issuer": "Supabase token issuer is invalid",
    "invalid_audience": "Supabase token audience is invalid",
    "invalid_signature": "Supabase token signature is invalid",
    "unknown_key": "Supabase token signing key is unknown",
    "jwks_unavailable": "Supabase signing keys are unavailable",
    "jwks_invalid": "Supabase signing keys are invalid",
}


class SupabaseAuthConfigurationError(ValueError):
    """Raised when the trusted verifier configuration is unsafe or ambiguous."""


class SupabaseJWTVerificationError(ValueError):
    """A sanitized verification failure that never contains JWT material."""

    def __init__(self, code: str):
        self.code = code if code in _ERROR_MESSAGES else "invalid_token"
        super().__init__(_ERROR_MESSAGES[self.code])


@dataclass(frozen=True, slots=True)
class SupabaseJWTIdentity:
    """Non-authorizing identity result from a verified Supabase JWT."""

    issuer: str
    subject: str
    audience: str
    expires_at: int
    auth_provider: str
    credential_type: str
    algorithm: str
    key_id: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _CachedVerificationKey:
    algorithm: str
    key: Any


def _configuration_error(field: str, requirement: str) -> SupabaseAuthConfigurationError:
    return SupabaseAuthConfigurationError(f"{field} {requirement}")


def _validate_exact_text(field: str, value: object, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _configuration_error(field, "must be a non-empty exact string")
    if len(value) > max_length or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _configuration_error(field, "contains an invalid value")
    return value


def _validate_https_url(field: str, value: object) -> tuple[str, tuple[str, str, int | None]]:
    exact = _validate_exact_text(field, value, max_length=2048)
    try:
        parsed = urlsplit(exact)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise _configuration_error(field, "must be a valid HTTPS URL") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise _configuration_error(field, "must be a credential-free HTTPS URL")
    return exact, (parsed.scheme, parsed.hostname.lower(), port)


def _response_cache_ttl(headers: Mapping[str, str], configured_ttl: float) -> float:
    """Honor provider no-cache/max-age without ever exceeding configured TTL."""

    cache_control = str(headers.get("cache-control") or "").lower()
    if "no-store" in cache_control or "no-cache" in cache_control:
        return 0.0
    match = re.search(r"(?:^|,)\s*max-age\s*=\s*(\d+)\s*(?:,|$)", cache_control)
    if not match:
        return configured_ttl
    try:
        return min(configured_ttl, float(match.group(1)))
    except (TypeError, ValueError, OverflowError):
        return 0.0


class SupabaseJWTVerifier:
    """Verify access tokens for one fixed Supabase project configuration."""

    def __init__(
        self,
        *,
        project_url: str,
        issuer: str,
        audience: str,
        jwks_url: str,
        cache_ttl_seconds: float = MAX_JWKS_CACHE_SECONDS,
        request_timeout_seconds: float = 5.0,
        http_client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        project_url, project_origin = _validate_https_url("project_url", project_url)
        issuer, issuer_origin = _validate_https_url("issuer", issuer)
        jwks_url, jwks_origin = _validate_https_url("jwks_url", jwks_url)
        if urlsplit(project_url).path not in ("", "/"):
            raise _configuration_error("project_url", "must identify the project origin")
        if issuer_origin != project_origin:
            raise _configuration_error("issuer", "must use the configured project origin")
        if jwks_origin != project_origin:
            raise _configuration_error("jwks_url", "must use the configured project origin")
        if not issuer.startswith(project_url.rstrip("/") + "/"):
            raise _configuration_error("issuer", "must belong to the configured project URL")
        if not jwks_url.startswith(issuer.rstrip("/") + "/"):
            raise _configuration_error("jwks_url", "must belong to the configured issuer")

        audience = _validate_exact_text("audience", audience, max_length=512)
        if isinstance(cache_ttl_seconds, bool):
            raise _configuration_error("cache_ttl_seconds", "must be between 0 and 600")
        try:
            cache_ttl = float(cache_ttl_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _configuration_error("cache_ttl_seconds", "must be between 0 and 600") from exc
        if not 0 <= cache_ttl <= MAX_JWKS_CACHE_SECONDS:
            raise _configuration_error("cache_ttl_seconds", "must be between 0 and 600")
        try:
            timeout = float(request_timeout_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _configuration_error("request_timeout_seconds", "must be positive") from exc
        if not 0 < timeout <= 30:
            raise _configuration_error("request_timeout_seconds", "must be between 0 and 30")
        if not callable(clock):
            raise _configuration_error("clock", "must be callable")

        self.project_url = project_url.rstrip("/")
        self.issuer = issuer
        self.audience = audience
        self.jwks_url = jwks_url
        self._cache_ttl_seconds = cache_ttl
        self._request_timeout_seconds = timeout
        self._clock = clock
        self._http_client = http_client or httpx.Client(follow_redirects=False)
        self._owns_http_client = http_client is None
        self._cache_lock = threading.Lock()
        self._cached_keys: dict[str, _CachedVerificationKey] = {}
        self._cache_expires_at = 0.0
        self._cache_generation = 0
        self._unknown_kids: dict[str, tuple[int, float]] = {}
        self._last_unknown_refresh_generation: int | None = None
        self._last_unknown_refresh_at: float | None = None

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def __enter__(self) -> "SupabaseJWTVerifier":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def purge_jwks_cache(self) -> None:
        """Explicitly forget all cached provider key and miss state."""

        with self._cache_lock:
            self._cached_keys = {}
            self._cache_expires_at = 0.0
            self._cache_generation += 1
            self._unknown_kids.clear()
            self._last_unknown_refresh_generation = None
            self._last_unknown_refresh_at = None

    def _fetch_jwks(self) -> tuple[dict[str, _CachedVerificationKey], float]:
        try:
            with self._http_client.stream(
                "GET",
                self.jwks_url,
                headers={"Accept": "application/json"},
                timeout=self._request_timeout_seconds,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise SupabaseJWTVerificationError("jwks_unavailable")
                raw_length = response.headers.get("content-length")
                if raw_length:
                    try:
                        if int(raw_length) > MAX_JWKS_RESPONSE_BYTES:
                            raise SupabaseJWTVerificationError("jwks_invalid")
                    except ValueError:
                        raise SupabaseJWTVerificationError("jwks_invalid") from None
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=16 * 1024):
                    if len(body) + len(chunk) > MAX_JWKS_RESPONSE_BYTES:
                        raise SupabaseJWTVerificationError("jwks_invalid")
                    body.extend(chunk)
                ttl = _response_cache_ttl(response.headers, self._cache_ttl_seconds)
        except SupabaseJWTVerificationError:
            raise
        except (httpx.HTTPError, OSError, RuntimeError):
            raise SupabaseJWTVerificationError("jwks_unavailable") from None

        try:
            payload = json.loads(bytes(body).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            raise SupabaseJWTVerificationError("jwks_invalid") from None
        if not isinstance(payload, dict):
            raise SupabaseJWTVerificationError("jwks_invalid")
        raw_keys = payload.get("keys")
        if not isinstance(raw_keys, list) or not raw_keys or len(raw_keys) > MAX_JWKS_KEYS:
            raise SupabaseJWTVerificationError("jwks_invalid")

        parsed_keys: dict[str, _CachedVerificationKey] = {}
        seen_key_ids: set[str] = set()
        for raw_key in raw_keys:
            if not isinstance(raw_key, dict):
                raise SupabaseJWTVerificationError("jwks_invalid")
            kid = raw_key.get("kid")
            if not self._valid_key_id(kid) or kid in seen_key_ids:
                raise SupabaseJWTVerificationError("jwks_invalid")
            seen_key_ids.add(kid)
            if _PRIVATE_JWK_FIELDS.intersection(raw_key) or "jku" in raw_key or "x5u" in raw_key:
                raise SupabaseJWTVerificationError("jwks_invalid")

            algorithm = raw_key.get("alg")
            if not isinstance(algorithm, str):
                raise SupabaseJWTVerificationError("jwks_invalid")
            if algorithm not in ALLOWED_JWT_ALGORITHMS:
                continue
            if algorithm == "RS256" and raw_key.get("kty") != "RSA":
                raise SupabaseJWTVerificationError("jwks_invalid")
            if algorithm == "ES256" and (
                raw_key.get("kty") != "EC" or raw_key.get("crv") != "P-256"
            ):
                raise SupabaseJWTVerificationError("jwks_invalid")
            if raw_key.get("use") not in (None, "sig"):
                raise SupabaseJWTVerificationError("jwks_invalid")
            key_ops = raw_key.get("key_ops")
            if key_ops is not None and (
                not isinstance(key_ops, list) or "verify" not in key_ops
            ):
                raise SupabaseJWTVerificationError("jwks_invalid")
            try:
                parsed = jwt.PyJWK.from_dict(raw_key, algorithm=algorithm)
            except (jwt.PyJWKError, ValueError, TypeError):
                raise SupabaseJWTVerificationError("jwks_invalid") from None
            parsed_keys[kid] = _CachedVerificationKey(algorithm=algorithm, key=parsed.key)

        if not parsed_keys:
            raise SupabaseJWTVerificationError("jwks_invalid")
        return parsed_keys, ttl

    @staticmethod
    def _valid_key_id(value: object) -> bool:
        return (
            isinstance(value, str)
            and 0 < len(value) <= 256
            and value == value.strip()
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
        )

    def _refresh_keys_locked(self) -> dict[str, _CachedVerificationKey]:
        keys, response_ttl = self._fetch_jwks()
        now = self._clock()
        self._cached_keys = keys
        self._cache_expires_at = now + min(
            response_ttl,
            self._cache_ttl_seconds,
            MAX_JWKS_CACHE_SECONDS,
        )
        self._cache_generation += 1
        # Misses are meaningful only for the exact key-set generation in which
        # they were observed.  A successful refresh invalidates them all.
        self._unknown_kids.clear()
        return self._cached_keys

    def _unknown_is_cached_locked(self, kid: str, now: float) -> bool:
        cached_miss = self._unknown_kids.get(kid)
        if cached_miss is None:
            return False
        generation, expires_at = cached_miss
        if generation != self._cache_generation or now >= expires_at:
            self._unknown_kids.pop(kid, None)
            return False
        # Reinsert to make the bounded mapping approximate LRU behavior.
        self._unknown_kids.pop(kid, None)
        self._unknown_kids[kid] = cached_miss
        return True

    def _remember_unknown_locked(self, kid: str, now: float) -> None:
        self._unknown_kids.pop(kid, None)
        while len(self._unknown_kids) >= MAX_UNKNOWN_KEY_IDS:
            self._unknown_kids.pop(next(iter(self._unknown_kids)))
        self._unknown_kids[kid] = (
            self._cache_generation,
            now + UNKNOWN_KID_NEGATIVE_CACHE_SECONDS,
        )

    def _mark_unknown_refresh_locked(self, now: float) -> None:
        self._last_unknown_refresh_generation = self._cache_generation
        self._last_unknown_refresh_at = now

    def _unknown_refresh_is_cooled_down_locked(self, now: float) -> bool:
        return (
            self._last_unknown_refresh_generation == self._cache_generation
            and self._last_unknown_refresh_at is not None
            and now
            < self._last_unknown_refresh_at + UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS
        )

    def _verification_key(self, kid: str, algorithm: str) -> Any:
        with self._cache_lock:
            refreshed_current_attempt = False
            if not self._cached_keys or self._clock() >= self._cache_expires_at:
                keys = self._refresh_keys_locked()
                refreshed_current_attempt = True
            else:
                keys = self._cached_keys
            cached = keys.get(kid)
            if cached is None:
                now = self._clock()
                if self._unknown_is_cached_locked(kid, now):
                    raise SupabaseJWTVerificationError("unknown_key")

                # A cold/expired lookup already fetched the current provider
                # key set.  Fetching it again cannot safely discover more keys
                # and lets arbitrary kids amplify outbound requests.
                if refreshed_current_attempt:
                    self._mark_unknown_refresh_locked(now)
                    self._remember_unknown_locked(kid, now)
                    raise SupabaseJWTVerificationError("unknown_key")

                # A warm cache may be stale during provider key rotation.  One
                # bounded refresh is permitted, globally, before a short
                # cooldown and per-generation negative cache take over.
                if self._unknown_refresh_is_cooled_down_locked(now):
                    self._remember_unknown_locked(kid, now)
                    raise SupabaseJWTVerificationError("unknown_key")
                self._mark_unknown_refresh_locked(now)
                cached = self._refresh_keys_locked().get(kid)
                now = self._clock()
                self._mark_unknown_refresh_locked(now)
            if cached is None:
                self._remember_unknown_locked(kid, self._clock())
                raise SupabaseJWTVerificationError("unknown_key")
            if cached.algorithm != algorithm:
                raise SupabaseJWTVerificationError("invalid_algorithm")
            return cached.key

    def verify(self, token: str) -> SupabaseJWTIdentity:
        """Verify a token and return only non-authorizing opaque metadata."""

        if (
            not isinstance(token, str)
            or not token
            or not token.isascii()
            or len(token) > MAX_JWT_BYTES
            or token.count(".") != 2
        ):
            raise SupabaseJWTVerificationError("invalid_token")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError:
            raise SupabaseJWTVerificationError("invalid_header") from None
        if not isinstance(header, dict):
            raise SupabaseJWTVerificationError("invalid_header")
        if _FORBIDDEN_TOKEN_KEY_HEADERS.intersection(header):
            raise SupabaseJWTVerificationError("invalid_header")
        if "crit" in header or "b64" in header:
            raise SupabaseJWTVerificationError("invalid_header")
        algorithm = header.get("alg")
        if not isinstance(algorithm, str) or algorithm not in ALLOWED_JWT_ALGORITHMS:
            raise SupabaseJWTVerificationError("invalid_algorithm")
        kid = header.get("kid")
        if not self._valid_key_id(kid):
            raise SupabaseJWTVerificationError("invalid_header")

        key = self._verification_key(kid, algorithm)
        try:
            claims = jwt.decode(
                token,
                key=key,
                algorithms=[algorithm],
                issuer=self.issuer,
                audience=self.audience,
                options={
                    "require": ["sub", "exp", "iss", "aud"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iss": True,
                    "verify_aud": True,
                },
            )
        except jwt.ExpiredSignatureError:
            raise SupabaseJWTVerificationError("expired") from None
        except jwt.InvalidIssuerError:
            raise SupabaseJWTVerificationError("invalid_issuer") from None
        except jwt.InvalidAudienceError:
            raise SupabaseJWTVerificationError("invalid_audience") from None
        except jwt.MissingRequiredClaimError:
            raise SupabaseJWTVerificationError("missing_claim") from None
        except jwt.InvalidSignatureError:
            raise SupabaseJWTVerificationError("invalid_signature") from None
        except jwt.InvalidTokenError:
            raise SupabaseJWTVerificationError("invalid_token") from None
        except (TypeError, ValueError):
            raise SupabaseJWTVerificationError("invalid_token") from None

        subject = claims.get("sub")
        expires_at = claims.get("exp")
        # Supabase access tokens use one string audience. Requiring the exact
        # configured scalar avoids silently widening trust to multi-audience
        # tokens minted for another service as well.
        if claims.get("iss") != self.issuer:
            raise SupabaseJWTVerificationError("invalid_issuer")
        if claims.get("aud") != self.audience:
            raise SupabaseJWTVerificationError("invalid_audience")
        if (
            not isinstance(subject, str)
            or not subject
            or len(subject) > 255
            or any(unicodedata.category(char) == "Cc" for char in subject)
        ):
            raise SupabaseJWTVerificationError("invalid_token")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int):
            raise SupabaseJWTVerificationError("invalid_token")

        return SupabaseJWTIdentity(
            issuer=self.issuer,
            subject=subject,
            audience=self.audience,
            expires_at=expires_at,
            auth_provider="supabase",
            credential_type="asymmetric_jwt",
            algorithm=algorithm,
            key_id=kid,
        )


__all__ = [
    "ALLOWED_JWT_ALGORITHMS",
    "MAX_JWKS_CACHE_SECONDS",
    "MAX_JWKS_RESPONSE_BYTES",
    "MAX_UNKNOWN_KEY_IDS",
    "UNKNOWN_KID_NEGATIVE_CACHE_SECONDS",
    "UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS",
    "SupabaseAuthConfigurationError",
    "SupabaseJWTIdentity",
    "SupabaseJWTVerificationError",
    "SupabaseJWTVerifier",
]
