from __future__ import annotations

import ast
import inspect
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import (
    Account,
    ActionAudit,
    Base,
    BrowserNotification,
    DirectMessage,
    EmailAccount,
    InboxItem,
    LifeEntity,
    PlanningItem,
)
from routes.communications_routes import setup_communications_routes
from src.communications_hub import (
    WHATSAPP_READ_ONLY_CAPABILITIES,
    communications_view,
    extract_communication_signals,
)
from src.identity import ensure_account
from src.life_ingestion import (
    ingest_telegram_message,
    ingest_whatsapp_readonly_message,
)


class _IdentityAuthority:
    def __init__(self, *usernames: str):
        self._config_lock = threading.Lock()
        self._identity_migrations: set[str] = set()
        self.retired_usernames: set[str] = set()
        self.users = {name: {} for name in usernames}

    @property
    def is_configured(self) -> bool:
        return True


def _create_email_cache(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE email_message_index (
                owner TEXT NOT NULL,
                account_key TEXT NOT NULL,
                folder TEXT NOT NULL,
                uid TEXT NOT NULL,
                message_id TEXT,
                subject TEXT,
                from_name TEXT,
                from_address TEXT,
                to_text TEXT,
                cc_text TEXT,
                date_iso TEXT,
                date_epoch REAL,
                size INTEGER DEFAULT 0,
                flags TEXT DEFAULT '',
                has_attachments INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (owner, account_key, folder, uid)
            );
            CREATE TABLE email_summaries (
                message_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                summary TEXT NOT NULL,
                PRIMARY KEY (message_id, owner)
            );
            CREATE TABLE email_ai_replies (
                message_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                reply TEXT NOT NULL,
                PRIMARY KEY (message_id, owner)
            );
            """
        )
        connection.execute(
            """
            INSERT INTO email_message_index(
                owner, account_key, folder, uid, message_id, subject,
                from_name, from_address, to_text, cc_text, date_iso,
                date_epoch, flags, updated_at
            ) VALUES (?, ?, 'INBOX', '11', '<alice-mail@test>', ?, ?, ?, '', '', ?, ?, ?, ?)
            """,
            (
                "alice",
                "mail-alice",
                "Project deadline by 2026-07-20",
                "Alex",
                "alex@example.test",
                "2026-07-17T09:00:00Z",
                1784278800,
                "\\Flagged",
                "2026-07-17T09:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO email_summaries(message_id, owner, summary) VALUES (?, ?, ?)",
            ("<alice-mail@test>", "alice", "Alex needs the project decision by Monday."),
        )
        connection.execute(
            "INSERT INTO email_ai_replies(message_id, owner, reply) VALUES (?, ?, ?)",
            ("<alice-mail@test>", "alice", "Thanks Alex — I will confirm by Monday."),
        )
        connection.execute(
            """
            INSERT INTO email_message_index(
                owner, account_key, folder, uid, message_id, subject,
                from_name, from_address, to_text, cc_text, date_iso,
                date_epoch, flags, updated_at
            ) VALUES (?, ?, 'INBOX', '11', '<bob-mail@test>', ?, ?, ?, '', '', ?, ?, '', ?)
            """,
            (
                "bob",
                "mail-bob",
                "BOB_PRIVATE_EMAIL_MARKER",
                "Mallory",
                "mallory@example.test",
                "2026-07-17T10:00:00Z",
                1784282400,
                "2026-07-17T10:00:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture()
def communications_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'communications.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    try:
        alice = ensure_account(db, "alice")
        bob = ensure_account(db, "bob")
        db.add_all([
            EmailAccount(
                id="mail-alice", owner="alice", name="Alice mail",
                enabled=True, is_default=True,
            ),
            EmailAccount(
                id="mail-bob", owner="bob", name="Bob mail",
                enabled=True, is_default=True,
            ),
            DirectMessage(
                sender="charlie",
                recipient="alice",
                body="I will send the report by 2026-07-20. Can you confirm?",
                created_at=datetime(2026, 7, 17, 11, 0, 0),
            ),
            DirectMessage(
                sender="mallory",
                recipient="bob",
                body="BOB_PRIVATE_DM_MARKER",
                created_at=datetime(2026, 7, 17, 12, 0, 0),
            ),
            BrowserNotification(
                id="notification-alice",
                owner_id=alice.id,
                payload={
                    "title": "Follow-up reminder",
                    "body": "Follow up with Dana tomorrow",
                    "importance": "high",
                    "timestamp": "2026-07-17T12:30:00Z",
                },
            ),
            BrowserNotification(
                id="notification-bob",
                owner_id=bob.id,
                payload={
                    "title": "BOB_PRIVATE_NOTIFICATION_MARKER",
                    "body": "Only Bob can see this",
                    "timestamp": "2026-07-17T12:31:00Z",
                },
            ),
        ])
        db.commit()
    finally:
        db.close()

    ingest_telegram_message(
        owner="alice",
        bot_fingerprint="bot-alice",
        chat_id="telegram-alice-chat",
        text="Could you review the proposal by Friday?",
        message_id=7,
        update_id=70,
        session_factory=factory,
    )
    ingest_telegram_message(
        owner="bob",
        bot_fingerprint="bot-bob",
        chat_id="telegram-bob-chat",
        text="BOB_PRIVATE_TELEGRAM_MARKER",
        message_id=8,
        update_id=80,
        session_factory=factory,
    )
    ingest_whatsapp_readonly_message(
        owner="alice",
        connector_id="approved-export",
        conversation_ref="+910000000001",
        text="I promise to share the files tomorrow. Please remind me?",
        message_id="wa-alice-1",
        sender_name="Ravi",
        observed_at=datetime(2026, 7, 17, 13, 0, tzinfo=timezone.utc),
        unread=True,
        important=True,
        session_factory=factory,
    )
    ingest_whatsapp_readonly_message(
        owner="bob",
        connector_id="approved-export",
        conversation_ref="+910000000002",
        text="BOB_PRIVATE_WHATSAPP_MARKER",
        message_id="wa-bob-1",
        sender_name="Mallory",
        session_factory=factory,
    )

    email_cache = tmp_path / "email-cache.db"
    _create_email_cache(email_cache)
    app = FastAPI()
    app.state.auth_manager = _IdentityAuthority("alice", "bob")

    @app.middleware("http")
    async def inject_identity(request, call_next):
        api_owner = request.headers.get("x-api-owner")
        request.state.api_token = bool(api_owner)
        if api_owner:
            request.state.api_token_owner = api_owner
            request.state.api_token_scopes = request.headers.get(
                "x-api-scopes", ""
            ).split(",")
            request.state.current_user = "api"
        else:
            request.state.current_user = request.headers.get("x-user")
        return await call_next(request)

    app.include_router(setup_communications_routes(
        session_factory=factory,
        email_cache_paths=(email_cache,),
    ))
    yield SimpleNamespace(
        app=app,
        Session=factory,
        engine=engine,
        email_cache=email_cache,
    )
    engine.dispose()


async def _call(env, method: str, path: str, *, user="alice", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if user:
        headers.setdefault("x-user", user)
    transport = httpx.ASGITransport(app=env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers=headers, **kwargs)


@pytest.mark.asyncio
async def test_unified_view_spans_enabled_sources_and_extracts_thread_evidence(
    communications_env,
):
    response = await _call(communications_env, "GET", "/api/communications")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["read_only"] is True
    assert payload["send_endpoints"] == []
    connectors = {item["id"]: item for item in payload["connectors"]}
    assert all(connectors[name]["enabled"] for name in (
        "email", "telegram", "restia_message", "notification", "whatsapp",
    ))
    assert connectors["whatsapp"]["send"] == {
        "available_in_existing_connector": False,
        "available_in_hub": False,
        "requires_human_confirmation": False,
        "generic_capability": False,
    }
    assert "send" not in connectors["whatsapp"]["capabilities"]
    assert payload["unread_items"] >= 4
    assert payload["important_threads"] >= 3

    by_connector = {thread["connector"]: thread for thread in payload["threads"]}
    assert set(by_connector) == {
        "email", "telegram", "restia_message", "notification", "whatsapp",
    }
    email_thread = by_connector["email"]
    assert email_thread["summary"] == "Alex needs the project decision by Monday."
    assert email_thread["response_suggestion"]["text"] == (
        "Thanks Alex — I will confirm by Monday."
    )
    dm_thread = by_connector["restia_message"]
    assert dm_thread["commitments"]
    assert dm_thread["deadlines"][0]["normalized_at"] == "2026-07-20"
    assert dm_thread["contacts"] == [{"kind": "sender", "value": "charlie"}]
    assert any(
        item["reason"] == "question awaiting an answer"
        for item in dm_thread["follow_ups"]
    )
    whatsapp = by_connector["whatsapp"]
    assert whatsapp["importance"]["label"] == "high"
    assert whatsapp["response_suggestion"]["send_attempted"] is False


@pytest.mark.asyncio
async def test_view_is_owner_isolated_and_read_does_not_mutate_authorities(
    communications_env,
):
    db = communications_env.Session()
    try:
        before = {
            "audits": db.query(ActionAudit).count(),
            "inbox": db.query(InboxItem).count(),
            "planning": db.query(PlanningItem).count(),
            "life": db.query(LifeEntity).count(),
        }
    finally:
        db.close()

    alice = await _call(communications_env, "GET", "/api/communications")
    bob = await _call(
        communications_env, "GET", "/api/communications", user="bob"
    )
    assert alice.status_code == bob.status_code == 200
    alice_text = alice.text
    bob_text = bob.text
    assert "BOB_PRIVATE_" not in alice_text
    assert "BOB_PRIVATE_EMAIL_MARKER" in bob_text
    assert "BOB_PRIVATE_DM_MARKER" in bob_text
    assert "BOB_PRIVATE_NOTIFICATION_MARKER" in bob_text
    assert "BOB_PRIVATE_TELEGRAM_MARKER" in bob_text
    assert "BOB_PRIVATE_WHATSAPP_MARKER" in bob_text

    db = communications_env.Session()
    try:
        after = {
            "audits": db.query(ActionAudit).count(),
            "inbox": db.query(InboxItem).count(),
            "planning": db.query(PlanningItem).count(),
            "life": db.query(LifeEntity).count(),
        }
    finally:
        db.close()
    assert after == before


@pytest.mark.asyncio
async def test_explicit_conversion_uses_canonical_universal_inbox_idempotently(
    communications_env,
):
    view = (await _call(
        communications_env,
        "GET",
        "/api/communications?connectors=restia_message",
    )).json()
    message_id = view["threads"][0]["messages"][0]["id"]

    first = await _call(
        communications_env,
        "POST",
        f"/api/communications/items/{message_id}/convert",
        json={"kind": "task", "process": True},
    )
    assert first.status_code == 200, first.text
    assert first.json()["conversion_path"] == "universal_inbox"
    assert first.json()["external_action_executed"] is False
    assert first.json()["send_attempted"] is False
    assert first.json()["item"]["status"] == "processed"

    retry = await _call(
        communications_env,
        "POST",
        f"/api/communications/items/{message_id}/convert",
        json={"kind": "task", "process": True},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["created"] is False
    assert retry.json()["item"]["id"] == first.json()["item"]["id"]

    db = communications_env.Session()
    try:
        alice = db.query(Account).filter_by(username="alice").one()
        assert len([
            row for row in db.query(InboxItem).filter_by(owner_id=alice.id).all()
            if "communication" in dict(row.meta_data or {})
        ]) == 1
        assert db.query(PlanningItem).filter_by(owner="alice").count() == 1
        tasks = db.query(LifeEntity).filter_by(
            owner_id=alice.id, entity_type="task"
        ).all()
        assert len(tasks) == 1
        assert tasks[0].provenance.get("source_id")
        assert db.query(ActionAudit).filter_by(
            owner_id=alice.id, action="inbox.processed"
        ).count() == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_cross_owner_item_id_cannot_be_converted(communications_env):
    bob_view = (await _call(
        communications_env,
        "GET",
        "/api/communications?connectors=restia_message",
        user="bob",
    )).json()
    bob_item_id = bob_view["threads"][0]["messages"][0]["id"]
    response = await _call(
        communications_env,
        "POST",
        f"/api/communications/items/{bob_item_id}/convert",
        json={"kind": "note"},
        user="alice",
    )
    assert response.status_code == 404
    db = communications_env.Session()
    try:
        assert not [
            row for row in db.query(InboxItem).all()
            if "communication" in dict(row.meta_data or {})
        ]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_api_token_requires_life_scope(communications_env):
    denied = await _call(
        communications_env,
        "GET",
        "/api/communications",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "todos:read"},
    )
    allowed = await _call(
        communications_env,
        "GET",
        "/api/communications",
        user=None,
        headers={"x-api-owner": "alice", "x-api-scopes": "life:read"},
    )
    assert denied.status_code == 403
    assert allowed.status_code == 200


def test_whatsapp_surface_has_no_generic_send_callable_or_route(
    communications_env,
):
    import src.communications_hub as hub
    import src.life_ingestion as ingestion

    assert "send" not in WHATSAPP_READ_ONLY_CAPABILITIES
    for module in (hub, ingestion):
        assert not [
            name for name, value in inspect.getmembers(module, callable)
            if "whatsapp" in name.lower() and "send" in name.lower()
        ]
    router = setup_communications_routes(
        session_factory=communications_env.Session,
        email_cache_paths=(communications_env.email_cache,),
    )
    assert not [
        route.path for route in router.routes
        if "send" in route.path.lower()
    ]

    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for base in (root / "src", root / "routes"):
        for path in base.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lowered = node.name.lower()
                    if "whatsapp" in lowered and "send" in lowered:
                        offenders.append(f"{path.relative_to(root)}:{node.lineno}:{node.name}")
                if isinstance(node, ast.ClassDef) and "whatsapp" in node.name.lower():
                    for member in node.body:
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            if member.name.lower() in {"send", "reply", "send_message"}:
                                offenders.append(
                                    f"{path.relative_to(root)}:{member.lineno}:{node.name}.{member.name}"
                                )
    assert offenders == []


def test_whatsapp_inbound_projection_is_idempotent_private_and_read_only(
    communications_env,
):
    retry = ingest_whatsapp_readonly_message(
        owner="alice",
        connector_id="approved-export",
        conversation_ref="+910000000001",
        text="I promise to share the files tomorrow. Please remind me?",
        message_id="wa-alice-1",
        sender_name="Ravi",
        observed_at=datetime(2026, 7, 17, 13, 0, tzinfo=timezone.utc),
        unread=True,
        important=True,
        session_factory=communications_env.Session,
    )
    assert retry.source_created is False
    assert retry.thread_created is False
    assert retry.message_created is False
    assert retry.link_created is False

    db = communications_env.Session()
    try:
        alice = db.query(Account).filter_by(username="alice").one()
        message = db.query(LifeEntity).filter_by(
            owner_id=alice.id,
            id=retry.message_id,
        ).one()
        assert message.properties["channel"] == "whatsapp"
        assert message.properties["read_only"] is True
        assert message.properties["conversation_ref"] == "+910000000001"
        interfaces = {
            row.details["audit"]["interface"]
            for row in db.query(ActionAudit).filter_by(owner_id=alice.id).all()
            if row.details.get("audit")
        }
        assert "domain_service" in interfaces
    finally:
        db.close()

    connection = sqlite3.connect(communications_env.engine.url.database)
    try:
        raw_database = "\n".join(connection.iterdump())
    finally:
        connection.close()
    assert "+910000000001" not in raw_database
    assert "wa-alice-1" not in raw_database


def test_service_connector_filter_and_search_are_bounded(communications_env):
    db = communications_env.Session()
    try:
        alice = db.query(Account).filter_by(username="alice").one()
        result = communications_view(
            db,
            account=alice,
            connectors=("email",),
            query="project decision",
            important_only=True,
            limit=1,
            email_cache_paths=(communications_env.email_cache,),
        )
        assert result["count"] == 1
        assert result["threads"][0]["connector"] == "email"
        assert result["threads"][0]["importance"]["label"] == "high"
    finally:
        db.close()


def test_email_fallback_never_merges_unrelated_messages_by_subject(
    communications_env,
):
    connection = sqlite3.connect(communications_env.email_cache)
    try:
        connection.execute(
            """
            INSERT INTO email_message_index(
                owner, account_key, folder, uid, message_id, subject,
                from_name, from_address, to_text, cc_text, date_iso,
                date_epoch, flags, updated_at
            ) VALUES (?, ?, 'INBOX', '12', '<alice-mail-2@test>', ?, ?, ?, '', '', ?, ?, '', ?)
            """,
            (
                "alice",
                "mail-alice",
                "Project deadline by 2026-07-20",
                "Different sender",
                "different@example.test",
                "2026-07-17T09:05:00Z",
                1784279100,
                "2026-07-17T09:05:00Z",
            ),
        )
        connection.commit()
    finally:
        connection.close()

    db = communications_env.Session()
    try:
        alice = db.query(Account).filter_by(username="alice").one()
        result = communications_view(
            db,
            account=alice,
            connectors=("email",),
            email_cache_paths=(communications_env.email_cache,),
        )
        assert result["count"] == 2
        assert {thread["message_count"] for thread in result["threads"]} == {1}
    finally:
        db.close()


def test_deadline_extraction_does_not_promote_an_unqualified_date():
    historical = extract_communication_signals(
        "We met on 2026-07-20 and discussed the proposal."
    )
    committed = extract_communication_signals(
        "The reviewed proposal is due by 2026-07-20."
    )

    assert historical["deadlines"] == []
    assert committed["deadlines"] == [{
        "text": "2026-07-20",
        "normalized_at": "2026-07-20",
        "confidence": 98,
    }]


@pytest.mark.asyncio
async def test_query_life_exposes_same_read_only_communications_view(
    communications_env, monkeypatch,
):
    import core.database as database
    from src.tools.life import do_query_life

    monkeypatch.setattr(database, "SessionLocal", communications_env.Session)
    result = await do_query_life(
        json.dumps({
            "action": "communications",
            "connectors": ["restia_message"],
            "query": "report",
            "limit": 10,
        }),
        owner="alice",
    )

    assert result["exit_code"] == 0
    assert result["read_only"] is True
    assert result["send_endpoints"] == []
    assert result["count"] == 1
    assert result["threads"][0]["connector"] == "restia_message"
    assert "no message was sent" in result["response"]


def test_production_app_registers_communications_router():
    root = Path(__file__).resolve().parents[1]
    source = (root / "app.py").read_text(encoding="utf-8")
    assert "from routes.communications_routes import setup_communications_routes" in source
    assert "app.include_router(setup_communications_routes())" in source
