#!/usr/bin/env python3
"""Configure Telegram runtime settings without echoing the bot token.

Reads the bot token from stdin, stores it in local runtime settings, registers
the webhook, and optionally sends one silent test message to the configured
chat. Output is deliberately sanitized.
"""

from __future__ import annotations

import json
import argparse
import getpass
import os
import re
import secrets
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.settings import load_settings, save_settings


TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{20,}$")
def main() -> int:
    parser = argparse.ArgumentParser(description="Configure a Restia Telegram bot webhook")
    parser.add_argument("--public-url", default=os.getenv("RESTIA_PUBLIC_URL", ""), help="Public HTTPS URL for this Restia instance")
    parser.add_argument("--owner", default="", help="Optional legacy single-user owner")
    parser.add_argument("--chat-id", default="", help="Optional chat ID for a silent delivery test")
    args = parser.parse_args()
    token = _read_token()
    if not TOKEN_RE.fullmatch(token):
        print({"ok": False, "error": "invalid_token_shape"})
        return 2

    settings = load_settings()
    public_url = str(args.public_url or settings.get("app_public_url") or "").strip().rstrip("/")
    if not public_url.startswith("https://"):
        print({"ok": False, "error": "public_https_url_required"})
        return 2
    settings["telegram_enabled"] = True
    settings["telegram_bot_token"] = token
    settings["telegram_allow_all_chats"] = False
    if args.owner:
        settings["telegram_owner"] = args.owner.strip().lower()
    settings["app_public_url"] = public_url
    if not str(settings.get("telegram_webhook_secret") or "").strip():
        settings["telegram_webhook_secret"] = secrets.token_urlsafe(32)
    save_settings(settings)

    webhook_url = f"{public_url}/api/telegram/webhook"
    base = f"https://api.telegram.org/bot{token}"
    summary: dict[str, object] = {
        "token_saved_to_runtime_settings": True,
        "webhook_url_host": public_url.replace("https://", "").replace("http://", ""),
        "allowed_chat_ids_count": len(settings.get("telegram_allowed_chat_ids") or []),
        "legacy_owner_configured": bool(args.owner),
    }

    with httpx.Client(timeout=30) as client:
        me = client.get(f"{base}/getMe")
        me_data = _json_or_empty(me)
        summary["get_me_ok"] = bool(me.status_code == 200 and me_data.get("ok"))
        if summary["get_me_ok"]:
            summary["bot_username_present"] = bool((me_data.get("result") or {}).get("username"))
        else:
            summary["get_me_error_code"] = me_data.get("error_code") or me.status_code

        set_resp = client.post(
            f"{base}/setWebhook",
            data={
                "url": webhook_url,
                "secret_token": str(settings["telegram_webhook_secret"]),
                "allowed_updates": json.dumps(["message", "edited_message"]),
            },
        )
        set_data = _json_or_empty(set_resp)
        summary["set_webhook_ok"] = bool(set_resp.status_code == 200 and set_data.get("ok"))
        if not summary["set_webhook_ok"]:
            summary["set_webhook_error_code"] = set_data.get("error_code") or set_resp.status_code

        info_resp = client.get(f"{base}/getWebhookInfo")
        info_data = _json_or_empty(info_resp)
        info = info_data.get("result") if isinstance(info_data.get("result"), dict) else {}
        summary["get_webhook_info_ok"] = bool(info_resp.status_code == 200 and info_data.get("ok"))
        summary["webhook_url_matches"] = bool(info.get("url") == webhook_url)
        summary["pending_update_count"] = int(info.get("pending_update_count") or 0)
        last_error = str(info.get("last_error_message") or "")
        summary["last_error_present"] = bool(info.get("last_error_date"))
        if last_error:
            summary["last_error_message_prefix"] = last_error[:80]

        if args.chat_id:
            send_resp = client.post(
                f"{base}/sendMessage",
                json={
                    "chat_id": args.chat_id.strip(),
                    "text": "Restia Telegram bridge is online.",
                    "disable_notification": True,
                },
            )
            send_data = _json_or_empty(send_resp)
            summary["send_test_ok"] = bool(send_resp.status_code == 200 and send_data.get("ok"))
            if not summary["send_test_ok"]:
                summary["send_test_error_code"] = send_data.get("error_code") or send_resp.status_code
                summary["send_test_error_prefix"] = str(send_data.get("description") or "")[:80]

    print(summary)
    return 0 if summary.get("get_me_ok") and summary.get("set_webhook_ok") else 1


def _json_or_empty(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _read_token() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("").strip()
    return sys.stdin.readline().strip()


if __name__ == "__main__":
    raise SystemExit(main())
