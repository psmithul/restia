"""Regression coverage for collision-resistant owner-scoped state files."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.auth_helpers import (
    legacy_owner_storage_key,
    owner_storage_key,
)


def test_owner_storage_key_preserves_safe_names_and_separates_legacy_collisions():
    assert owner_storage_key("alice") == "alice"
    assert owner_storage_key("Alice") == "alice"

    collision_pairs = [
        ("a/b", "a_b"),
        ("alice+work@example.com", "alice_work@example.com"),
        ("team name", "team_name"),
    ]
    for escaped_owner, literal_owner in collision_pairs:
        assert legacy_owner_storage_key(escaped_owner) == legacy_owner_storage_key(literal_owner)
        assert owner_storage_key(escaped_owner) != owner_storage_key(literal_owner)
        assert owner_storage_key(escaped_owner).startswith("~")
        assert not owner_storage_key(literal_owner).startswith("~")


def test_personal_upload_directories_do_not_share_a_lossy_owner_slug(
    monkeypatch, tmp_path
):
    import routes.personal_routes as personal

    monkeypatch.setattr(personal, "UPLOADS_DIR", str(tmp_path))
    escaped = personal._personal_upload_dir_for_owner("alice+work@example.com")
    literal = personal._personal_upload_dir_for_owner("alice_work@example.com")

    assert escaped != literal
    assert escaped.startswith(str(tmp_path))
    assert literal.startswith(str(tmp_path))


def test_notification_state_is_isolated_for_historically_colliding_owners(
    monkeypatch, tmp_path
):
    import routes.notification_center_routes as notifications

    escaped_owner = "alice+work@example.com"
    literal_owner = "alice_work@example.com"
    monkeypatch.setattr(notifications, "DATA_DIR", str(tmp_path))

    for owner, subject in (
        (escaped_owner, "ESCAPED_OWNER_PRIVATE_SUBJECT"),
        (literal_owner, "LITERAL_OWNER_PRIVATE_SUBJECT"),
    ):
        path = tmp_path / f"email_urgency_state_{owner_storage_key(owner)}.json"
        path.write_text(
            json.dumps({
                "owner": owner,
                "per_uid": {
                    "account:1": {
                        "score": 3,
                        "unread": True,
                        "subject": subject,
                    }
                },
            }),
            encoding="utf-8",
        )

    escaped = notifications._emails_needing_reply(escaped_owner)
    literal = notifications._emails_needing_reply(literal_owner)

    assert [item["subject"] for item in escaped] == ["ESCAPED_OWNER_PRIVATE_SUBJECT"]
    assert [item["subject"] for item in literal] == ["LITERAL_OWNER_PRIVATE_SUBJECT"]


def test_lossy_legacy_state_requires_exact_embedded_owner(monkeypatch, tmp_path):
    import routes.notification_center_routes as notifications

    escaped_owner = "a/b"
    colliding_owner = "a_b"
    monkeypatch.setattr(notifications, "DATA_DIR", str(tmp_path))
    legacy_path = tmp_path / (
        f"email_urgency_state_{legacy_owner_storage_key(escaped_owner)}.json"
    )
    legacy_path.write_text(
        json.dumps({
            "owner": escaped_owner,
            "per_uid": {
                "account:9": {
                    "score": 3,
                    "unread": True,
                    "subject": "LEGACY_PRIVATE_SUBJECT",
                }
            },
        }),
        encoding="utf-8",
    )

    assert notifications._emails_needing_reply(escaped_owner)[0]["subject"] == (
        "LEGACY_PRIVATE_SUBJECT"
    )
    assert notifications._emails_needing_reply(colliding_owner) == []


@pytest.mark.asyncio
async def test_private_telegram_digest_state_uses_distinct_owner_files(
    monkeypatch, tmp_path
):
    import src.builtin_actions as actions
    import src.notification_preferences as preferences
    import src.telegram_bot as telegram

    monkeypatch.setattr(actions, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        telegram,
        "load_telegram_config",
        lambda: SimpleNamespace(enabled=True, bot_token="token"),
    )
    monkeypatch.setattr(
        telegram, "telegram_chat_ids_for_owner", lambda config, owner: [owner]
    )
    monkeypatch.setattr(preferences, "load_notification_preferences", lambda owner: {
        "digest_cadence": "daily",
        "digest_time": "08:00",
        "notification_topics": ["todos"],
        "timezone": "UTC",
        "quiet_hours_enabled": True,
    })
    monkeypatch.setattr(preferences, "quiet_hours_active", lambda prefs, **kwargs: True)
    monkeypatch.setattr(
        preferences, "seconds_until_quiet_hours_end", lambda prefs, **kwargs: 600
    )

    owners = ("alice+work@example.com", "alice_work@example.com")
    for owner in owners:
        with pytest.raises(actions.TaskDeferred):
            await actions.action_telegram_hourly_digest(owner)

    paths = [
        tmp_path / f"telegram_digest_{owner_storage_key(owner, fallback='local')}.json"
        for owner in owners
    ]
    assert paths[0] != paths[1]
    assert all(path.exists() for path in paths)
