"""Shared pairing helpers for the companion bridge.

Token minting + LAN discovery + QR rendering, kept here as small, importable
units so the route layer stays thin and the logic is directly testable.
"""

from __future__ import annotations

import json
import os
import socket

PAIRING_VERSION = 1
COMPANION_SCOPE = "chat"


def default_port() -> int:
    """Best guess at the port the server is reachable on. Callers that know the
    real request port should pass it explicitly."""
    try:
        return int(os.environ.get("APP_PORT", "7000"))
    except ValueError:
        return 7000


def lan_ip_candidates() -> list[str]:
    """Likely LAN IPv4 addresses for this host, best candidate first.

    The UDP-connect trick reveals the egress interface the OS would use to reach
    the default gateway -- i.e. the address a phone on the same Wi-Fi should
    target. No packets are actually sent. Loopback is dropped.
    """
    candidates: list[str] = []

    def _add(ip):
        if ip and ip not in candidates and not ip.startswith("127."):
            candidates.append(ip)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        _add(s.getsockname()[0])
    except OSError:
        pass
    finally:
        s.close()

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _add(info[4][0])
    except OSError:
        pass

    return candidates


def _database_auth_manager(auth_manager=None):
    if auth_manager is not None:
        return auth_manager
    from src.auth_runtime import get_auth_manager

    return get_auth_manager()


def find_admin_user(auth_manager=None) -> str | None:
    """Resolve an admin from the canonical account/role authority."""

    manager = _database_auth_manager(auth_manager)
    users = getattr(manager, "users", {})
    if not isinstance(users, dict):
        return None
    for uname, udata in users.items():
        if isinstance(udata, dict) and udata.get("is_admin") is True:
            return uname
    return next(iter(users), None)


def mint_token(
    owner: str,
    name: str = "companion",
    *,
    auth_manager=None,
) -> tuple[str, str]:
    """Create a chat-scoped API token row and return (token_id, raw_token).

    The raw token is returned ONCE; the database stores only a keyed HMAC
    digest attached to the immutable account UUID.
    """
    issued = _database_auth_manager(auth_manager).issue_api_token(
        owner,
        name=name,
        scopes=[COMPANION_SCOPE],
    )
    if issued is None:
        raise RuntimeError("Could not issue an account-bound companion token")
    return issued["id"], issued["token"]


def pairing_payload(host: str, port: int, token: str) -> dict:
    """The exact JSON a client scans / accepts. Keep keys stable."""
    return {"v": PAIRING_VERSION, "host": host, "port": port, "token": token}


def pairing_qr_png_data_uri(payload: dict) -> str | None:
    """Render the pairing payload as a QR `data:` URI for an <img>. Returns None
    if the optional qrcode dep is unavailable."""
    try:
        import base64
        import io

        import qrcode

        img = qrcode.make(json.dumps(payload, separators=(",", ":")))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None
