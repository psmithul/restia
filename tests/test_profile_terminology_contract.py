"""Human-identity terminology compatibility contracts.

The public vocabulary is ``installation/user`` for the external Restia
identity and ``profile`` for a local login principal.  Legacy ``/users``
routes and response keys remain available to existing clients.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import routes.auth_routes as auth_routes
import routes.messaging_routes as messaging_routes


ROOT = Path(__file__).resolve().parent.parent


def _routes(router):
    out = {}
    for route in router.routes:
        for method in getattr(route, "methods", set()):
            out[(method, route.path)] = route.endpoint
    return out


class _Auth:
    def __init__(self):
        self.users = {
            "admin": {"is_admin": True},
            "alice": {"is_admin": False},
        }

    def get_username_for_token(self, token):
        return "admin" if token == "session" else None

    def is_admin(self, username):
        return bool(self.users.get(username, {}).get("is_admin"))

    def list_users(self):
        return [
            {"username": name, "is_admin": data["is_admin"], "privileges": {}}
            for name, data in self.users.items()
        ]


def _request(auth, username="admin"):
    return SimpleNamespace(
        cookies={auth_routes.SESSION_COOKIE: "session"},
        state=SimpleNamespace(current_user=username, api_token=False),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=auth)),
        client=SimpleNamespace(host="127.0.0.1"),
        headers={},
    )


def test_auth_profile_aliases_preserve_legacy_routes_and_payload():
    auth = _Auth()
    router = auth_routes.setup_auth_routes(auth)
    routes = _routes(router)
    expected = {
        ("GET", "/api/auth/profiles"),
        ("POST", "/api/auth/profiles"),
        ("PUT", "/api/auth/profiles/{username}/privileges"),
        ("PUT", "/api/auth/profiles/{username}/rename"),
        ("PUT", "/api/auth/profiles/{username}/admin"),
        ("DELETE", "/api/auth/profiles"),
    }
    legacy = {(method, path.replace("/profiles", "/users", 1)) for method, path in expected}

    assert expected <= routes.keys()
    assert legacy <= routes.keys()
    legacy_specs = [route for route in router.routes if route.path.startswith("/api/auth/users")]
    assert legacy_specs and all(route.deprecated for route in legacy_specs)

    canonical = asyncio.run(routes[("GET", "/api/auth/profiles")](_request(auth)))
    old = asyncio.run(routes[("GET", "/api/auth/users")](_request(auth)))
    assert canonical == old
    assert canonical["profiles"] == canonical["users"]


def test_message_profile_alias_preserves_users_key(monkeypatch):
    auth = _Auth()
    monkeypatch.setattr(messaging_routes.link_routes, "hub_enabled", lambda: False)
    monkeypatch.setattr(messaging_routes.link_routes, "home_enabled", lambda: False)
    router = messaging_routes.setup_messaging_routes()
    routes = _routes(router)

    assert ("GET", "/api/messages/profiles") in routes
    assert ("GET", "/api/messages/users") in routes
    legacy_spec = next(route for route in router.routes if route.path == "/api/messages/users")
    assert legacy_spec.deprecated is True

    canonical = asyncio.run(
        routes[("GET", "/api/messages/profiles")](_request(auth, username="admin"))
    )
    old = asyncio.run(
        routes[("GET", "/api/messages/users")](_request(auth, username="admin"))
    )
    assert canonical == old
    assert canonical["profiles"] == canonical["users"]
    assert [profile["username"] for profile in canonical["profiles"]] == ["alice"]


def test_ui_and_docs_use_scoped_profile_vocabulary():
    login = (ROOT / "static/login.html").read_text(encoding="utf-8")
    index = (ROOT / "static/index.html").read_text(encoding="utf-8")
    admin = (ROOT / "static/js/admin.js").read_text(encoding="utf-8")
    messaging = (ROOT / "static/js/messaging.js").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    setup = (ROOT / "docs/setup.md").read_text(encoding="utf-8")

    assert 'for="username">Profile name<' in login
    assert "Create Owner Profile" in login
    assert ">Profiles</span>" in index
    assert "Add Profile</h2>" in index
    assert "/api/auth/profiles" in admin
    assert "/api/messages/profiles" in messaging
    assert "Settings → Profiles" in messaging
    assert "One Restia installation is the external Restia" in readme
    assert "Login principals\ninside it are called **profiles**" in setup

    # Existing client and storage identifiers stay intact; this is a vocabulary
    # compatibility layer, not a global symbol replacement.
    assert 'data-settings-tab="users"' in index
    assert "reserved_usernames" in login
    assert (ROOT / "services/hwfit/profiles.py").exists()
