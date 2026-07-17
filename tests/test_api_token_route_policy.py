from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.api_token_policy import api_token_route_error


ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    ("method", "path", "scopes"),
    [
        ("GET", "/api/companion/ping", ["chat"]),
        ("GET", "/api/sessions", ["chat"]),
        ("GET", "/api/history/session-1", ["chat"]),
        ("POST", "/api/chat", ["chat"]),
        ("POST", "/api/v1/chat", ["chat"]),
        ("GET", "/api/inbox", ["todos:read"]),
        ("POST", "/api/inbox", ["todos:write"]),
        ("GET", "/api/life/entities", ["life:read"]),
        ("POST", "/api/life/entities", ["life:write"]),
        ("GET", "/api/life/focus/current", ["life:write"]),
        ("GET", "/api/codex/todos", ["todos:write"]),
        ("GET", "/api/claude/plugin.zip", ["todos:read"]),
        ("GET", "/api/codex/emails/42", ["email:draft"]),
        ("POST", "/api/codex/cookbook/serve", ["cookbook:launch"]),
        (
            "POST",
            "/api/codex/emails/draft-document",
            ["email:draft", "documents:write"],
        ),
    ],
)
def test_explicit_api_token_routes_accept_their_scopes(method, path, scopes):
    assert api_token_route_error(method, path, scopes) is None


@pytest.mark.parametrize(
    ("method", "path", "scopes"),
    [
        ("GET", "/api/sessions", ["todos:read"]),
        ("GET", "/api/history/session-1", ["todos:read"]),
        ("POST", "/api/chat", ["todos:write"]),
        ("GET", "/api/email/list", ["email:read"]),
        ("GET", "/api/cookbook/state", ["cookbook:read"]),
        ("GET", "/api/tokens", ["chat"]),
        ("GET", "/api/auth/status", ["chat"]),
        ("GET", "/api/codex/emails", ["todos:read", "todos:write"]),
        ("GET", "/api/codex/cookbook/tasks", ["todos:read"]),
        ("DELETE", "/api/codex/todos", ["todos:write"]),
        ("GET", "/api/codex/todos/extra", ["todos:read"]),
        ("GET", "/api/inbox//", ["todos:read"]),
        ("GET", "/api/life/entities", ["todos:read"]),
        ("POST", "/api/life/entities", ["life:read"]),
    ],
)
def test_unknown_or_wrong_scope_api_token_routes_fail_closed(method, path, scopes):
    assert api_token_route_error(method, path, scopes) is not None


def test_multi_scope_route_requires_every_scope_group():
    error = api_token_route_error(
        "POST",
        "/api/codex/emails/draft-document",
        ["email:draft"],
    )

    assert error == "API token missing required scope: documents:write"


def test_database_bearer_boundary_is_enforced_by_the_real_app(tmp_path):
    data_dir = tmp_path / "data"
    db_path = data_dir / "app.db"
    env = os.environ.copy()
    env.update({
        "RESTIA_DATA_DIR": str(data_dir),
        "DATABASE_URL": f"sqlite:///{db_path}",
        "RESTIA_DATABASE_MODE": "local-single",
        "AUTH_ENABLED": "true",
        "LOCALHOST_BYPASS": "false",
        "SECURE_COOKIES": "false",
        "RESTIA_STARTUP_WARMUPS": "0",
        "RESTIA_MODEL_KEEPALIVE": "0",
        "RESTIA_INPROCESS_TASKS": "0",
        "RESTIA_INPROCESS_POLLERS": "0",
        "RESTIA_INPROCESS_TELEGRAM": "0",
        "FASTEMBED_CACHE_PATH": str(tmp_path / "fastembed"),
        "MNEMOSYNE_DATA_DIR": str(tmp_path / "mnemosyne"),
    })
    code = r'''
import os
from fastapi.testclient import TestClient

import app as app_module

admin = TestClient(app_module.app)
setup = admin.post(
    "/api/auth/setup",
    json={"username": "alice", "password": "correct horse battery staple"},
)
assert setup.status_code == 200, setup.text
login = admin.post(
    "/api/auth/login",
    json={
        "username": "alice",
        "password": "correct horse battery staple",
        "remember": True,
    },
)
assert login.status_code == 200, login.text

def issue(name, scopes):
    response = admin.post("/api/tokens", data={"name": name, "scopes": scopes})
    assert response.status_code == 200, response.text
    return response.json()["token"]

todos_token = issue("todos-only", "todos:read,todos:write")
todos = TestClient(
    app_module.app,
    headers={"Authorization": f"Bearer {todos_token}"},
)

# Both scope-aware todo surfaces remain usable.
listed = todos.get("/api/inbox")
assert listed.status_code == 200, listed.text
casefolded_scheme = TestClient(
    app_module.app,
    headers={"Authorization": f"bearer {todos_token}"},
).get("/api/inbox")
assert casefolded_scheme.status_code == 200, casefolded_scheme.text
created = todos.post(
    "/api/inbox",
    json={"title": "API-boundary regression", "content": "test"},
)
assert created.status_code == 201, created.text
codex_todos = todos.get("/api/codex/todos")
assert codex_todos.status_code == 200, codex_todos.text

# The same valid token cannot become a general authenticated browser session.
denied = [
    todos.get("/api/sessions"),
    todos.get("/api/history/not-a-session"),
    todos.post("/api/chat", json={"session": "not-a-session", "message": "hello"}),
    todos.get("/api/email/list"),
    todos.get("/api/cookbook/state"),
    todos.get("/api/codex/emails"),
    todos.get("/api/codex/cookbook/tasks"),
    todos.get("/api/life/entities"),
    todos.get("/api/tokens"),
    # Auth-exempt paths must not bypass policy when an ody_ credential is sent.
    todos.get("/api/auth/status"),
]
for response in denied:
    assert response.status_code == 403, response.text

# A Life write credential resolves to the same principal as the browser and
# carries the domain-local read implication needed for optimistic workflows.
life_token = issue("life-os", "life:write")
life = TestClient(
    app_module.app,
    headers={"Authorization": f"Bearer {life_token}"},
)
life_created = life.post(
    "/api/life/entities",
    json={
        "entity_type": "task",
        "title": "Cross-interface task",
        "properties": {"next_action": "Verify the shared principal"},
        "idempotency_key": "real-app-life-task",
    },
)
assert life_created.status_code == 201, life_created.text
life_entity_id = life_created.json()["entity"]["id"]
life_list = life.get("/api/life/entities")
assert life_list.status_code == 200, life_list.text
assert [row["id"] for row in life_list.json()["items"]] == [life_entity_id]
browser_life = admin.get("/api/life/entities")
assert browser_life.status_code == 200, browser_life.text
assert [row["id"] for row in browser_life.json()["items"]] == [life_entity_id]

chat_token = issue("paired-chat", "chat")
chat = TestClient(
    app_module.app,
    headers={"Authorization": f"Bearer {chat_token}"},
)
ping = chat.get("/api/companion/ping")
assert ping.status_code == 200, ping.text
assert ping.json()["auth"] == "token"
companion_models = chat.get("/api/companion/models")
assert companion_models.status_code == 200, companion_models.text
sessions = chat.get("/api/sessions")
assert sessions.status_code == 200, sessions.text
history = chat.get("/api/history/not-a-session")
assert history.status_code == 404, history.text

# The request reaches the chat handler and its owner-scoped session check,
# rather than being rejected by the bearer boundary.
chat_request = chat.post(
    "/api/chat",
    json={"session": "not-a-session", "message": "hello"},
)
assert chat_request.status_code == 404, chat_request.text
sync_chat = chat.post("/api/v1/chat", json={"message": " "})
assert sync_chat.status_code == 400, sync_chat.text
print("api-token-boundary-ready", flush=True)
# Some chat-adjacent lazy singletons own process-lifetime worker threads.  The
# assertions above have completed; avoid making the outer test wait for those
# production-lifetime workers in this short-lived integration subprocess.
os._exit(0)
'''
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stdout.strip().endswith("api-token-boundary-ready")
