from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.database import Account, Base, InboxItem, LifeEntity
from routes.communications_routes import setup_communications_routes
from src.communications_hub import (
    COMMUNICATION_CONNECTORS,
    CommunicationItemNotFound,
    communications_view,
    convert_communication_item,
)
from src.identity import ensure_account
from src.life_ingestion import (
    GENERIC_READ_ONLY_COMMUNICATION_CHANNELS,
    LifeIngestionError,
    ingest_readonly_communication_message,
    normalize_capture_source_category,
)


@pytest.fixture()
def connector_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'connector-projections.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        ensure_account(db, "alice")
        ensure_account(db, "bob")
        db.commit()

    for index, channel in enumerate(
        ("slack", "sms", "call", "other"), start=1
    ):
        ingest_readonly_communication_message(
            owner="alice",
            channel=channel,
            connector_id=f"{channel}-alice-import",
            conversation_ref=f"{channel}-alice-conversation",
            text=f"ALICE_{channel.upper()}_MARKER: confirm by Friday?",
            message_id=f"alice-{channel}-1",
            sender_name=f"{channel.title()} contact",
            observed_at=datetime(2026, 7, 18, 8, index, tzinfo=timezone.utc),
            unread=True,
            important=channel == "call",
            session_factory=factory,
        )
        ingest_readonly_communication_message(
            owner="bob",
            channel=channel,
            connector_id=f"{channel}-bob-import",
            conversation_ref=f"{channel}-bob-conversation",
            text=f"BOB_PRIVATE_{channel.upper()}_MARKER",
            message_id=f"bob-{channel}-1",
            sender_name="Bob private contact",
            observed_at=datetime(2026, 7, 18, 9, index, tzinfo=timezone.utc),
            session_factory=factory,
        )

    yield SimpleNamespace(Session=factory, engine=engine)
    engine.dispose()


def _tool_connector_schema() -> dict:
    from src.agent_tools import FUNCTION_TOOL_SCHEMAS

    query_life = next(
        schema["function"]
        for schema in FUNCTION_TOOL_SCHEMAS
        if schema.get("function", {}).get("name") == "query_life"
    )
    return query_life["parameters"]["properties"]["connectors"]


def test_missing_connectors_share_one_owner_scoped_read_only_hub(connector_env):
    with connector_env.Session() as db:
        alice = db.query(Account).filter_by(username="alice").one()
        bob = db.query(Account).filter_by(username="bob").one()
        alice_view = communications_view(db, account=alice)
        bob_view = communications_view(db, account=bob)

    assert {thread["connector"] for thread in alice_view["threads"]} == {
        "slack", "sms", "call", "other",
    }
    assert {thread["connector"] for thread in bob_view["threads"]} == {
        "slack", "sms", "call", "other",
    }
    assert "BOB_PRIVATE_" not in str(alice_view)
    assert "ALICE_" not in str(bob_view)

    policies = {item["id"]: item for item in alice_view["connectors"]}
    for channel in GENERIC_READ_ONLY_COMMUNICATION_CHANNELS:
        policy = policies[channel]
        assert policy["enabled"] is True
        assert policy["read"] == {"allowed": True, "side_effect_free": True}
        assert policy["draft"] == {
            "allowed": True,
            "suggestion_only": True,
            "requires_human_review": True,
        }
        assert policy["send"] == {
            "available_in_existing_connector": False,
            "available_in_hub": False,
            "requires_human_confirmation": False,
            "generic_capability": False,
        }
        assert policy["read_only_connector"] is True
        assert "send" not in policy["capabilities"]
    assert alice_view["send_endpoints"] == []


def test_new_connector_filter_conversion_and_cross_owner_guard(connector_env):
    with connector_env.Session() as db:
        alice = db.query(Account).filter_by(username="alice").one()
        bob = db.query(Account).filter_by(username="bob").one()
        alice_slack = communications_view(
            db, account=alice, connectors=("slack",), query="confirm"
        )
        bob_slack = communications_view(
            db, account=bob, connectors=("slack",)
        )
        assert alice_slack["count"] == bob_slack["count"] == 1
        assert alice_slack["threads"][0]["connector"] == "slack"

        alice_item_id = alice_slack["threads"][0]["messages"][0]["id"]
        bob_item_id = bob_slack["threads"][0]["messages"][0]["id"]
        converted = convert_communication_item(
            db,
            account=alice,
            item_id=alice_item_id,
            kind="note",
            process=False,
        )
        db.commit()
        assert converted["conversion_path"] == "universal_inbox"
        assert converted["external_action_executed"] is False
        assert converted["send_attempted"] is False
        assert converted["item"]["source_type"] == "slack"
        with pytest.raises(CommunicationItemNotFound):
            convert_communication_item(
                db,
                account=alice,
                item_id=bob_item_id,
                kind="note",
                process=False,
            )

        converted_rows = [
            row for row in db.query(InboxItem).filter_by(owner_id=alice.id).all()
            if "communication" in dict(row.meta_data or {})
        ]
        assert len(converted_rows) == 1
        assert not [
            row for row in db.query(InboxItem).filter_by(owner_id=bob.id).all()
            if "communication" in dict(row.meta_data or {})
        ]


def test_generic_readonly_projection_is_idempotent_private_and_fail_closed(
    connector_env,
):
    retry = ingest_readonly_communication_message(
        owner="alice",
        channel="slack",
        connector_id="slack-alice-import",
        conversation_ref="slack-alice-conversation",
        text="ALICE_SLACK_MARKER: confirm by Friday?",
        message_id="alice-slack-1",
        sender_name="Slack contact",
        observed_at=datetime(2026, 7, 18, 8, 1, tzinfo=timezone.utc),
        unread=True,
        session_factory=connector_env.Session,
    )
    assert retry.source_created is False
    assert retry.message_created is False
    assert retry.thread_created is False

    with connector_env.Session() as db:
        message = db.query(LifeEntity).filter_by(id=retry.message_id).one()
        assert message.properties["channel"] == "slack"
        assert message.properties["read_only"] is True
        assert message.properties["conversation_ref"] == (
            "slack-alice-conversation"
        )

    connection = sqlite3.connect(connector_env.engine.url.database)
    try:
        raw_database = "\n".join(connection.iterdump())
    finally:
        connection.close()
    assert "slack-alice-conversation" not in raw_database
    assert "alice-slack-1" not in raw_database

    with pytest.raises(LifeIngestionError, match="must be slack, sms, call, or other"):
        ingest_readonly_communication_message(
            owner="alice",
            channel="telegram",
            connector_id="telegram-bypass",
            conversation_ref="private-chat",
            text="Must use the canonical Telegram adapter",
            session_factory=connector_env.Session,
        )


@pytest.mark.parametrize(
    ("label", "canonical"),
    [
        ("forwarded_email", "email"),
        ("forwarded_emails", "email"),
        ("forwarded_message", "restia_message"),
        ("forwarded_messages", "restia_message"),
        ("quick_capture", "text"),
        ("Slack messages", "slack"),
        ("text messages", "sms"),
        ("call transcripts", "call"),
        ("other messaging systems", "other"),
    ],
)
def test_capture_aliases_normalize_without_expanding_destinations(label, canonical):
    assert normalize_capture_source_category(label) == canonical


def test_hub_and_tool_filters_expose_connectors_without_a_send_surface(
    connector_env,
):
    schema = _tool_connector_schema()
    assert schema["maxItems"] == len(COMMUNICATION_CONNECTORS) == 9
    assert set(schema["items"]["enum"]) == set(COMMUNICATION_CONNECTORS)

    import src.communications_hub as hub

    assert not [
        name for name, value in inspect.getmembers(hub, callable)
        if "send" in name.casefold()
    ]
    router = setup_communications_routes(session_factory=connector_env.Session)
    assert not [route.path for route in router.routes if "send" in route.path.casefold()]
