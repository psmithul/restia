from __future__ import annotations

from types import SimpleNamespace

from src import service_health as sh


def test_telegram_health_is_disabled_without_configuration(monkeypatch):
    monkeypatch.setattr(
        "src.telegram_bot.load_telegram_config",
        lambda: SimpleNamespace(enabled=False, bot_token="", chat_owners={}),
    )
    result = sh.telegram_health(http_get=lambda *_a, **_k: None)
    assert result["status"] == sh.DISABLED


def test_telegram_health_checks_getme_and_link_targets(monkeypatch):
    monkeypatch.setattr(
        "src.telegram_bot.load_telegram_config",
        lambda: SimpleNamespace(
            enabled=True,
            bot_token="secret-token",
            chat_owners={"100": "alice"},
        ),
    )
    monkeypatch.setattr(
        "src.notification_preferences.load_notification_preferences",
        lambda _owner: {
            "reminder_channel": "browser",
            "reminder_telegram_mirror": True,
            "digest_cadence": "off",
        },
    )
    seen = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    def get(url, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        return Response()

    result = sh.telegram_health(http_get=get)
    assert result["status"] == sh.OK
    assert result["meta"]["linked_profiles"] == 1
    assert seen["url"].endswith("/getMe")
    assert "secret-token" not in repr(result)


def test_telegram_health_flags_linked_profiles_with_no_delivery_route(monkeypatch):
    monkeypatch.setattr(
        "src.telegram_bot.load_telegram_config",
        lambda: SimpleNamespace(
            enabled=True, bot_token="secret-token", chat_owners={"100": "alice"},
        ),
    )
    monkeypatch.setattr(
        "src.notification_preferences.load_notification_preferences",
        lambda _owner: {
            "reminder_channel": "browser",
            "reminder_telegram_mirror": False,
            "digest_cadence": "off",
        },
    )

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True}

    result = sh.telegram_health(http_get=lambda *_a, **_k: Response())
    assert result["status"] == sh.DEGRADED
    assert result["meta"]["routed_profiles"] == 0


def test_due_notification_health_reports_runtime_progress(monkeypatch):
    monkeypatch.setattr(
        "src.due_notification_worker.due_notification_worker_status",
        lambda: {
            "enabled": True,
            "running": True,
            "last_scan_at": "2026-07-20T12:00:00+00:00",
            "last_success_at": "2026-07-20T12:00:01+00:00",
            "last_error_at": None,
            "last_error": "",
            "last_result": {"scanned": 2, "delivered": 2},
        },
    )
    result = sh.due_notifications_health()
    assert result["status"] == sh.OK
    assert result["meta"]["last_result"]["delivered"] == 2
