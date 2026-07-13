# tests/test_security_headers_middleware.py
"""
Focused regression coverage for `SecurityHeadersMiddleware`
(core/middleware.py), added alongside the HSTS + Permissions-Policy
hardening:

  1. HSTS is emitted only for HTTPS requests, including those reaching
     the app over a reverse proxy (`X-Forwarded-Proto: https`).
  2. HSTS is absent on plain HTTP so local/dev deployments are unaffected.
  3. `Permissions-Policy` grants camera/microphone only to the authenticated
     SPA document and keeps them disabled on every other response.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.middleware import SecurityHeadersMiddleware


def _build_app():
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/")
    def root():
        return {"ok": True}

    return app


def _client(base_url="http://testserver"):
    return TestClient(_build_app(), base_url=base_url)


def test_hsts_absent_on_plain_http():
    response = _client().get("/")

    assert "strict-transport-security" not in response.headers


def test_hsts_present_for_direct_https_requests():
    response = _client(base_url="https://testserver").get("/")

    assert response.headers["strict-transport-security"] == (
        "max-age=31536000; includeSubDomains"
    )


def test_hsts_present_via_x_forwarded_proto_https():
    response = _client().get("/", headers={"X-Forwarded-Proto": "https"})

    assert response.headers["strict-transport-security"] == (
        "max-age=31536000; includeSubDomains"
    )


@pytest.mark.parametrize("path", ["/", "/notes", "/gallery"])
def test_permissions_policy_allows_same_origin_media_on_spa_documents(path):
    response = _client().get(path)

    policy = response.headers["permissions-policy"]
    assert policy == "camera=(self), microphone=(self), geolocation=()"
    assert "camera=()" not in policy
    assert "microphone=()" not in policy


@pytest.mark.parametrize("path", ["/login", "/api/health", "/static/index.html"])
def test_permissions_policy_denies_media_outside_spa_documents(path):
    response = _client().get(path)

    assert response.headers["permissions-policy"] == (
        "camera=(), microphone=(), geolocation=()"
    )


def test_permissions_policy_denies_media_on_non_get_spa_response():
    response = _client().post("/")

    assert response.headers["permissions-policy"] == (
        "camera=(), microphone=(), geolocation=()"
    )
