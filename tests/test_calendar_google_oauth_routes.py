import types

import pytest


class _Request:
    headers = {"host": "localhost:7000"}

    def __init__(self, payload=None):
        self._payload = payload or {}

    async def json(self):
        return self._payload


def _calendar_endpoint(path: str, method: str):
    from routes.calendar_routes import setup_calendar_routes

    router = setup_calendar_routes()
    method = method.upper()
    for route in router.routes:
        if route.path == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"calendar route not found: {method} {path}")


def _prefs_with_google_account():
    from src.secret_storage import encrypt

    return {
        "caldav_accounts": [{
            "id": "cal-google",
            "label": "Google Primary",
            "url": "https://apidata.googleusercontent.com/caldav/v2/alice@example.com/events",
            "username": "alice@example.com",
            "password": "",
            "oauth_provider": "google",
            "oauth_access_token": encrypt("ya29.old"),
            "oauth_refresh_token": encrypt("refresh"),
            "oauth_token_expiry": "1",
        }]
    }


@pytest.mark.asyncio
async def test_calendar_accounts_expose_oauth_status_without_tokens(monkeypatch):
    import routes.calendar_routes as calendar_routes
    import routes.prefs_routes as prefs_routes

    monkeypatch.setattr(calendar_routes, "require_user", lambda request: "alice")
    monkeypatch.setattr(prefs_routes, "_load_for_user", lambda owner: _prefs_with_google_account())

    endpoint = _calendar_endpoint("/api/calendar/config/accounts", "GET")
    response = await endpoint(_Request())

    account = response["accounts"][0]
    assert account["oauth_provider"] == "google"
    assert account["oauth_connected"] is True
    assert account["has_password"] is False
    assert "oauth_access_token" not in account
    assert "oauth_refresh_token" not in account


@pytest.mark.asyncio
async def test_calendar_test_uses_bearer_token_for_saved_google_account(monkeypatch):
    import httpx
    import routes.calendar_routes as calendar_routes
    import routes.prefs_routes as prefs_routes
    from src import caldav_sync

    captured = {}

    class _AsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def request(self, method, url, auth=None, headers=None, content=None):
            captured.update({
                "method": method,
                "url": url,
                "auth": auth,
                "headers": headers or {},
                "content": content,
            })
            return types.SimpleNamespace(status_code=207, headers={})

    monkeypatch.setattr(calendar_routes, "require_user", lambda request: "alice")
    monkeypatch.setattr(prefs_routes, "_load_for_user", lambda owner: _prefs_with_google_account())
    monkeypatch.setattr(caldav_sync, "_ensure_google_calendar_token", lambda acc, owner: "ya29.live")
    monkeypatch.setattr(caldav_sync, "validate_caldav_url", lambda url: url.rstrip("/"))
    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClient)

    endpoint = _calendar_endpoint("/api/calendar/test", "POST")
    response = await endpoint(_Request({"account_id": "cal-google"}))

    assert response == {"ok": True}
    assert captured["auth"] is None
    assert captured["headers"]["Authorization"] == "Bearer ya29.live"
    assert captured["headers"]["Depth"] == "0"
