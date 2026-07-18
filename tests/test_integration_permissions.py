from __future__ import annotations

import pytest

from src import integrations
from src.integration_permissions import (
    IntegrationPermissionDenied,
    integration_request_allowed,
    normalize_integration_permissions,
)


def _integration(**overrides):
    value = {
        "id": "home",
        "name": "Home",
        "base_url": "https://home.example.test",
        "enabled": True,
        "auth_type": "none",
        "permissions": {
            "allowed_methods": ["GET"],
            "allowed_path_prefixes": ["/api/states"],
            "require_action_approval_for_writes": True,
        },
    }
    value.update(overrides)
    return value


def test_connector_permissions_default_read_only_and_validate_path_boundaries():
    assert normalize_integration_permissions(None) == {
        "allowed_methods": ["GET"],
        "allowed_path_prefixes": ["/"],
        "require_action_approval_for_writes": True,
    }
    integration = _integration()
    integration_request_allowed(
        integration, method="GET", path="/api/states/light.desk",
    )
    with pytest.raises(IntegrationPermissionDenied, match="outside"):
        integration_request_allowed(
            integration, method="GET", path="/api/state-secrets",
        )
    with pytest.raises(IntegrationPermissionDenied, match="POST"):
        integration_request_allowed(
            integration, method="POST", path="/api/states/light.desk",
        )


@pytest.mark.parametrize(
    "path",
    [
        "/api/states/../config",
        "/api/states/%2e%2e/config",
        "/api/states/%252e%252e/config",
        "/api/states%2f..%2fconfig",
        "/api/states\\..\\config",
        "https://other.example/api/states",
    ],
)
def test_connector_permission_rejects_host_escape_and_encoded_traversal(path):
    with pytest.raises(IntegrationPermissionDenied):
        integration_request_allowed(_integration(), method="GET", path=path)


def test_connector_write_grant_cannot_disable_action_approval():
    with pytest.raises(IntegrationPermissionDenied, match="approved external action"):
        normalize_integration_permissions({
            "allowed_methods": ["GET", "POST"],
            "allowed_path_prefixes": ["/api/services/light"],
            "require_action_approval_for_writes": False,
        })
    permissions = normalize_integration_permissions({
        "allowed_methods": ["GET", "POST"],
        "allowed_path_prefixes": ["/api/services/light"],
        "require_action_approval_for_writes": True,
    })
    integration = _integration(permissions=permissions)
    with pytest.raises(IntegrationPermissionDenied, match="freshly approved"):
        integration_request_allowed(
            integration, method="POST", path="/api/services/light/turn_on",
        )
    integration_request_allowed(
        integration,
        method="POST",
        path="/api/services/light/turn_on",
        approved_external_action=True,
    )


@pytest.mark.asyncio
async def test_execute_api_call_checks_connector_grant_before_network(monkeypatch):
    calls = []

    class _Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = "{}"

        def json(self):
            return {"ok": True}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return _Response()

    monkeypatch.setattr(integrations, "_find_integration", lambda *_args, **_kwargs: _integration())
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda **_kwargs: _Client())
    import src.url_safety as url_safety

    monkeypatch.setattr(url_safety, "check_outbound_url", lambda *_args, **_kwargs: (True, ""))

    denied_method = await integrations.execute_api_call(
        "home", "POST", "/api/states/light.desk",
    )
    assert denied_method["exit_code"] == 1
    assert "does not grant POST" in denied_method["error"]
    denied_path = await integrations.execute_api_call(
        "home", "GET", "/api/config",
    )
    assert denied_path["exit_code"] == 1
    assert "outside" in denied_path["error"]
    denied_traversal = await integrations.execute_api_call(
        "home", "GET", "/api/states/%2e%2e/config",
    )
    assert denied_traversal["exit_code"] == 1
    assert "traversal" in denied_traversal["error"]
    assert calls == []

    allowed = await integrations.execute_api_call(
        "home", "GET", "/api/states/light.desk",
    )
    assert allowed["exit_code"] == 0
    assert calls[0][0] == "GET"


@pytest.mark.asyncio
async def test_execute_api_call_accepts_write_only_from_reviewed_executor(monkeypatch):
    calls = []
    integration = _integration(permissions={
        "allowed_methods": ["GET", "POST"],
        "allowed_path_prefixes": ["/api/services/light"],
        "require_action_approval_for_writes": True,
    })

    class _Response:
        status_code = 200
        headers = {"content-type": "application/json"}
        text = "{}"

        def json(self):
            return {"ok": True}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return _Response()

    monkeypatch.setattr(integrations, "_find_integration", lambda *_args, **_kwargs: integration)
    monkeypatch.setattr(integrations.httpx, "AsyncClient", lambda **_kwargs: _Client())
    import src.url_safety as url_safety

    monkeypatch.setattr(url_safety, "check_outbound_url", lambda *_args, **_kwargs: (True, ""))

    denied = await integrations.execute_api_call(
        "home", "POST", "/api/services/light/turn_on",
    )
    assert denied["exit_code"] == 1
    assert calls == []
    allowed = await integrations.execute_api_call(
        "home",
        "POST",
        "/api/services/light/turn_on",
        body={"entity_id": "light.desk"},
        approved_external_action=True,
    )
    assert allowed["exit_code"] == 0
    assert calls[0][0] == "POST"
