from __future__ import annotations

import base64
import json
import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from src.supabase_auth import (
    MAX_JWKS_CACHE_SECONDS,
    MAX_JWKS_RESPONSE_BYTES,
    MAX_UNKNOWN_KEY_IDS,
    UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS,
    SupabaseAuthConfigurationError,
    SupabaseJWTVerificationError,
    SupabaseJWTVerifier,
)


PROJECT_URL = "https://project-ref.supabase.co"
ISSUER = PROJECT_URL + "/auth/v1"
JWKS_URL = ISSUER + "/.well-known/jwks.json"
AUDIENCE = "authenticated"


RSA_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_PRIVATE = ec.generate_private_key(ec.SECP256R1())


def _public_jwk(private_key, *, kid: str, algorithm: str) -> dict:
    if algorithm == "RS256":
        value = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    else:
        value = ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    value.update({"kid": kid, "alg": algorithm, "use": "sig", "key_ops": ["verify"]})
    return value


RSA_JWK = _public_jwk(RSA_PRIVATE, kid="rsa-key", algorithm="RS256")
EC_JWK = _public_jwk(EC_PRIVATE, kid="ec-key", algorithm="ES256")


def _claims(**overrides) -> dict:
    value = {
        "iss": ISSUER,
        "sub": "opaque-subject-uuid",
        "aud": AUDIENCE,
        "exp": int(time.time()) + 3600,
        # These authorization-looking values must never be returned.
        "email": "private@example.com",
        "role": "service_role",
        "app_metadata": {"role": "admin"},
        "user_metadata": {"name": "Private"},
    }
    value.update(overrides)
    return value


def _token(
    private_key=RSA_PRIVATE,
    *,
    algorithm: str = "RS256",
    kid: str = "rsa-key",
    claims: dict | None = None,
    headers: dict | None = None,
) -> str:
    jose_headers = {"kid": kid, **(headers or {})}
    return jwt.encode(claims or _claims(), private_key, algorithm=algorithm, headers=jose_headers)


def _raw_token_with_header(header: object) -> str:
    def encode_json(value: object) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{encode_json(header)}.{encode_json(_claims())}.AA"


class _JWKSService:
    def __init__(self, responses: list[httpx.Response | dict | bytes]):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        value = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(value, httpx.Response):
            return value
        if isinstance(value, bytes):
            return httpx.Response(200, content=value, request=request)
        return httpx.Response(200, json=value, request=request)


def _verifier(
    service: _JWKSService,
    *,
    cache_ttl_seconds: float = 600,
    clock=lambda: 1000.0,
) -> SupabaseJWTVerifier:
    client = httpx.Client(transport=httpx.MockTransport(service), follow_redirects=True)
    return SupabaseJWTVerifier(
        project_url=PROJECT_URL,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS_URL,
        cache_ttl_seconds=cache_ttl_seconds,
        http_client=client,
        clock=clock,
    )


@pytest.mark.parametrize(
    ("private_key", "algorithm", "kid", "jwk"),
    [
        (RSA_PRIVATE, "RS256", "rsa-key", RSA_JWK),
        (EC_PRIVATE, "ES256", "ec-key", EC_JWK),
    ],
)
def test_verifies_only_opaque_identity_and_auth_metadata(
    private_key, algorithm, kid, jwk
):
    service = _JWKSService([{"keys": [jwk]}])
    verifier = _verifier(service)

    result = verifier.verify(
        _token(private_key, algorithm=algorithm, kid=kid)
    )

    assert result.subject == "opaque-subject-uuid"
    assert result.issuer == ISSUER
    assert result.audience == AUDIENCE
    assert result.auth_provider == "supabase"
    assert result.credential_type == "asymmetric_jwt"
    assert result.algorithm == algorithm
    assert result.key_id == kid
    assert set(result.as_dict()) == {
        "issuer",
        "subject",
        "audience",
        "expires_at",
        "auth_provider",
        "credential_type",
        "algorithm",
        "key_id",
    }
    rendered = json.dumps(result.as_dict())
    assert "private@example.com" not in rendered
    assert "service_role" not in rendered
    assert "admin" not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_url", "http://project-ref.supabase.co"),
        ("project_url", "https://user@project-ref.supabase.co"),
        ("issuer", "https://other.supabase.co/auth/v1"),
        ("jwks_url", "https://other.supabase.co/jwks.json"),
        ("jwks_url", JWKS_URL + "?from=token"),
        ("audience", ""),
    ],
)
def test_configuration_is_exact_and_https(field: str, value: str):
    kwargs = {
        "project_url": PROJECT_URL,
        "issuer": ISSUER,
        "audience": AUDIENCE,
        "jwks_url": JWKS_URL,
    }
    kwargs[field] = value
    with pytest.raises(SupabaseAuthConfigurationError):
        SupabaseJWTVerifier(**kwargs)


def test_cache_ttl_cannot_exceed_ten_minutes():
    service = _JWKSService([{"keys": [RSA_JWK]}])
    with pytest.raises(SupabaseAuthConfigurationError):
        _verifier(service, cache_ttl_seconds=MAX_JWKS_CACHE_SECONDS + 0.01)


@pytest.mark.parametrize("algorithm", ["HS256", "HS384", "HS512", "none"])
def test_rejects_symmetric_and_none_algorithms_without_fetching_jwks(algorithm: str):
    service = _JWKSService([{"keys": [RSA_JWK]}])
    verifier = _verifier(service)
    if algorithm == "none":
        token = jwt.encode(_claims(), key="", algorithm="none", headers={"kid": "rsa-key"})
    else:
        token = jwt.encode(
            _claims(),
            key="shared-secret-material-" * 4,
            algorithm=algorithm,
            headers={"kid": "rsa-key"},
        )

    with pytest.raises(SupabaseJWTVerificationError) as exc:
        verifier.verify(token)

    assert exc.value.code == "invalid_algorithm"
    assert service.requests == []


@pytest.mark.parametrize("algorithm", [[], {}])
def test_rejects_non_string_token_algorithms_with_sanitized_error(algorithm: object):
    service = _JWKSService([{"keys": [RSA_JWK]}])
    verifier = _verifier(service)
    token = _raw_token_with_header({"alg": algorithm, "kid": "rsa-key"})

    with pytest.raises(SupabaseJWTVerificationError) as exc:
        verifier.verify(token)

    assert exc.value.code == "invalid_algorithm"
    assert str(exc.value) == "Supabase token algorithm is not allowed"
    assert token not in str(exc.value)
    assert service.requests == []


@pytest.mark.parametrize("header", [{"jku": "https://evil.test/jwks"}, {"x5u": "https://evil.test/cert"}, {"jwk": RSA_JWK}, {"x5c": ["fake"]}])
def test_rejects_token_provided_key_sources_without_network_access(header: dict):
    service = _JWKSService([{"keys": [RSA_JWK]}])
    verifier = _verifier(service)
    token = _token(headers=header)

    with pytest.raises(SupabaseJWTVerificationError) as exc:
        verifier.verify(token)

    assert exc.value.code == "invalid_header"
    assert service.requests == []


def test_jwks_request_uses_only_fixed_endpoint_and_never_follows_redirects():
    seen: list[str] = []

    def redirecting(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://evil.test/stolen"},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(redirecting), follow_redirects=True)
    verifier = SupabaseJWTVerifier(
        project_url=PROJECT_URL,
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url=JWKS_URL,
        http_client=client,
    )

    with pytest.raises(SupabaseJWTVerificationError) as exc:
        verifier.verify(_token())

    assert exc.value.code == "jwks_unavailable"
    assert seen == [JWKS_URL]


def test_jwks_response_body_is_bounded_even_without_content_length():
    oversized = b"{" + (b" " * MAX_JWKS_RESPONSE_BYTES) + b"}"
    service = _JWKSService([oversized])
    verifier = _verifier(service)

    with pytest.raises(SupabaseJWTVerificationError) as exc:
        verifier.verify(_token())

    assert exc.value.code == "jwks_invalid"


@pytest.mark.parametrize("algorithm", [[], {}])
def test_rejects_non_string_jwks_algorithms_with_sanitized_error(algorithm: object):
    malformed = dict(RSA_JWK, alg=algorithm)
    service = _JWKSService([{"keys": [malformed]}])
    verifier = _verifier(service)

    with pytest.raises(SupabaseJWTVerificationError) as exc:
        verifier.verify(_token())

    assert exc.value.code == "jwks_invalid"
    assert str(exc.value) == "Supabase signing keys are invalid"
    assert len(service.requests) == 1


def test_cache_is_reused_until_expiry_and_can_be_purged():
    now = [1000.0]
    service = _JWKSService([
        {"keys": [RSA_JWK]},
        {"keys": [RSA_JWK]},
        {"keys": [RSA_JWK]},
    ])
    verifier = _verifier(service, clock=lambda: now[0])
    token = _token()

    verifier.verify(token)
    verifier.verify(token)
    assert len(service.requests) == 1

    now[0] += 599.9
    verifier.verify(token)
    assert len(service.requests) == 1

    now[0] += 0.1
    verifier.verify(token)
    assert len(service.requests) == 2

    verifier.purge_jwks_cache()
    verifier.verify(token)
    assert len(service.requests) == 3


def test_unknown_kid_forces_exactly_one_refresh_and_accepts_rotated_key():
    rotated_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rotated_jwk = _public_jwk(rotated_private, kid="rotated", algorithm="RS256")
    service = _JWKSService([
        {"keys": [RSA_JWK]},
        {"keys": [RSA_JWK, rotated_jwk]},
    ])
    verifier = _verifier(service)
    verifier.verify(_token())

    result = verifier.verify(_token(rotated_private, kid="rotated"))

    assert result.key_id == "rotated"
    assert len(service.requests) == 2


def test_cold_unknown_kid_fetches_only_once_and_is_negative_cached():
    other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    service = _JWKSService([{"keys": [RSA_JWK]}])
    verifier = _verifier(service)
    token = _token(other_private, kid="never-published")

    for _ in range(2):
        with pytest.raises(SupabaseJWTVerificationError) as exc:
            verifier.verify(token)
        assert exc.value.code == "unknown_key"

    assert len(service.requests) == 1


def test_unknown_kid_refreshes_once_then_fails_safely():
    other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    service = _JWKSService([
        {"keys": [RSA_JWK]},
        {"keys": [RSA_JWK]},
    ])
    verifier = _verifier(service)
    verifier.verify(_token())

    with pytest.raises(SupabaseJWTVerificationError) as exc:
        verifier.verify(_token(other_private, kid="never-published"))

    assert exc.value.code == "unknown_key"
    assert len(service.requests) == 2


def test_unknown_kid_flood_is_bounded_and_cannot_amplify_refreshes():
    now = [1000.0]
    other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    service = _JWKSService([
        {"keys": [RSA_JWK]},
        {"keys": [RSA_JWK]},
        {"keys": [RSA_JWK]},
    ])
    verifier = _verifier(service, clock=lambda: now[0])
    verifier.verify(_token())

    for index in range(MAX_UNKNOWN_KEY_IDS + 40):
        with pytest.raises(SupabaseJWTVerificationError) as exc:
            verifier.verify(_token(other_private, kid=f"missing-{index}"))
        assert exc.value.code == "unknown_key"

    assert len(service.requests) == 2
    assert len(verifier._unknown_kids) == MAX_UNKNOWN_KEY_IDS

    # A new unknown may probe for a real rotation only after the global
    # cooldown, even though the bounded negative cache contains other kids.
    now[0] += UNKNOWN_KID_REFRESH_COOLDOWN_SECONDS
    with pytest.raises(SupabaseJWTVerificationError) as exc:
        verifier.verify(_token(other_private, kid="missing-after-cooldown"))
    assert exc.value.code == "unknown_key"
    assert len(service.requests) == 3


def test_failed_warm_rotation_refresh_is_also_cooled_down():
    other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    service = _JWKSService([
        {"keys": [RSA_JWK]},
        httpx.Response(503),
        {"keys": [RSA_JWK]},
    ])
    verifier = _verifier(service)
    verifier.verify(_token())

    with pytest.raises(SupabaseJWTVerificationError) as exc:
        verifier.verify(_token(other_private, kid="missing-during-outage"))
    assert exc.value.code == "jwks_unavailable"
    assert len(service.requests) == 2

    with pytest.raises(SupabaseJWTVerificationError) as exc:
        verifier.verify(_token(other_private, kid="another-missing-during-outage"))
    assert exc.value.code == "unknown_key"
    assert len(service.requests) == 2


def test_purge_clears_unknown_kid_cooldown_and_discovers_rotation_immediately():
    rotated_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    rotated_jwk = _public_jwk(rotated_private, kid="rotated", algorithm="RS256")
    service = _JWKSService([
        {"keys": [RSA_JWK]},
        {"keys": [RSA_JWK]},
        {"keys": [RSA_JWK, rotated_jwk]},
    ])
    verifier = _verifier(service)
    verifier.verify(_token())

    with pytest.raises(SupabaseJWTVerificationError):
        verifier.verify(_token(rotated_private, kid="rotated"))
    assert len(service.requests) == 2

    verifier.purge_jwks_cache()
    result = verifier.verify(_token(rotated_private, kid="rotated"))

    assert result.key_id == "rotated"
    assert len(service.requests) == 3


@pytest.mark.parametrize(
    ("claims", "code"),
    [
        ({key: value for key, value in _claims().items() if key != "sub"}, "missing_claim"),
        ({key: value for key, value in _claims().items() if key != "exp"}, "missing_claim"),
        ({key: value for key, value in _claims().items() if key != "iss"}, "missing_claim"),
        ({key: value for key, value in _claims().items() if key != "aud"}, "missing_claim"),
        ({**_claims(), "iss": "https://other.supabase.co/auth/v1"}, "invalid_issuer"),
        ({**_claims(), "aud": "another-service"}, "invalid_audience"),
        ({**_claims(), "aud": [AUDIENCE]}, "invalid_audience"),
        ({**_claims(), "exp": int(time.time()) - 1}, "expired"),
    ],
)
def test_requires_exact_standard_claims(claims: dict, code: str):
    service = _JWKSService([{"keys": [RSA_JWK]}])
    verifier = _verifier(service)

    with pytest.raises(SupabaseJWTVerificationError) as exc:
        verifier.verify(_token(claims=claims))

    assert exc.value.code == code


def test_preserves_exact_opaque_subject_without_trimming_or_casefolding():
    subject = " MiXeD/opaque Subject "
    service = _JWKSService([{"keys": [RSA_JWK]}])
    verifier = _verifier(service)

    result = verifier.verify(_token(claims=_claims(sub=subject)))

    assert result.subject == subject


def test_accepts_opaque_subject_at_255_character_storage_limit():
    subject = "S" * 255
    service = _JWKSService([{"keys": [RSA_JWK]}])
    verifier = _verifier(service)

    result = verifier.verify(_token(claims=_claims(sub=subject)))

    assert result.subject == subject


@pytest.mark.parametrize("subject", ["S" * 256, "opaque\x00subject", "opaque\u0085subject"])
def test_rejects_subjects_outside_exact_storage_contract(subject: str):
    service = _JWKSService([{"keys": [RSA_JWK]}])
    verifier = _verifier(service)

    with pytest.raises(SupabaseJWTVerificationError) as exc:
        verifier.verify(_token(claims=_claims(sub=subject)))

    assert exc.value.code == "invalid_token"


def test_requires_kid():
    service = _JWKSService([{"keys": [RSA_JWK]}])
    verifier = _verifier(service)
    token = jwt.encode(_claims(), RSA_PRIVATE, algorithm="RS256")

    with pytest.raises(SupabaseJWTVerificationError) as exc:
        verifier.verify(token)

    assert exc.value.code == "invalid_header"
    assert service.requests == []


def test_signature_errors_are_sanitized_and_never_echo_the_jwt():
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    service = _JWKSService([{"keys": [RSA_JWK]}])
    verifier = _verifier(service)
    token = _token(attacker_key)

    with pytest.raises(SupabaseJWTVerificationError) as exc:
        verifier.verify(token)

    assert exc.value.code == "invalid_signature"
    assert token not in str(exc.value)
    assert token not in repr(exc.value)


def test_rejects_duplicate_or_private_jwks_keys():
    duplicate = dict(RSA_JWK)
    private = dict(RSA_JWK, d="private-material")
    for keys in ([RSA_JWK, duplicate], [private]):
        service = _JWKSService([{"keys": keys}])
        verifier = _verifier(service)
        with pytest.raises(SupabaseJWTVerificationError) as exc:
            verifier.verify(_token())
        assert exc.value.code == "jwks_invalid"
