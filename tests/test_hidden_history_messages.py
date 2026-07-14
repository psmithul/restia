"""Internal continuity records never leak through history or export APIs."""

import json

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from core.models import ChatMessage, Session
import routes.session_routes as session_routes


class _SessionManager:
    def __init__(self, session):
        self.session = session

    def get_session(self, session_id):
        if session_id != self.session.id:
            raise KeyError(session_id)
        return self.session


def _client(monkeypatch):
    history = [
        ChatMessage(role="user", content="VISIBLE-QUESTION"),
        ChatMessage(
            role="user",
            content="INTERNAL-STUDY-CONTINUITY",
            metadata={
                "compacted": True,
                "hidden_from_user_view": True,
                "study_summary": True,
            },
        ),
        ChatMessage(
            role="system",
            content="LEGACY-HIDDEN-CONTEXT",
            metadata={"hidden": True},
        ),
        ChatMessage(role="assistant", content="VISIBLE-ANSWER"),
    ]
    session = Session(
        id="study-session",
        name="Study",
        endpoint_url="http://local/v1/chat/completions",
        model="local-model",
        history=history,
        message_count=len(history),
    )
    manager = _SessionManager(session)
    monkeypatch.setattr(
        session_routes,
        "router",
        APIRouter(prefix="/api", tags=["sessions"]),
    )
    monkeypatch.setattr(
        session_routes,
        "_verify_session_owner",
        lambda request, session_id: None,
    )
    app = FastAPI()
    app.include_router(session_routes.setup_session_routes(manager, {}))
    return TestClient(app)


def test_history_api_excludes_all_internal_context_messages(monkeypatch):
    response = _client(monkeypatch).get("/api/history/study-session")

    assert response.status_code == 200
    assert response.json()["history"] == [
        {"role": "user", "content": "VISIBLE-QUESTION"},
        {"role": "assistant", "content": "VISIBLE-ANSWER"},
    ]


def test_every_export_format_excludes_internal_context_messages(monkeypatch):
    client = _client(monkeypatch)

    for export_format in ("json", "txt", "html", "md"):
        response = client.get(
            "/api/session/study-session/export",
            params={"fmt": export_format},
        )
        assert response.status_code == 200
        assert "VISIBLE-QUESTION" in response.text
        assert "VISIBLE-ANSWER" in response.text
        assert "INTERNAL-STUDY-CONTINUITY" not in response.text
        assert "LEGACY-HIDDEN-CONTEXT" not in response.text

    json_payload = json.loads(
        client.get(
            "/api/session/study-session/export",
            params={"fmt": "json"},
        ).text
    )
    assert [message["role"] for message in json_payload["messages"]] == [
        "user",
        "assistant",
    ]
