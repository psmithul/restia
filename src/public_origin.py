"""Strict canonical origins used in links shared with other installations."""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit


def canonical_shared_origin(value: Any, *, allow_loopback_http: bool = True) -> str:
    """Return a safe HTTPS origin (or loopback HTTP origin), else ``""``.

    The value is only advertised to another Restia; this helper does not make
    a network request. Paths, credentials, queries, fragments, control
    characters, and non-loopback plain HTTP are rejected so copied setup
    instructions match Home Link's origin-pinning requirements.
    """

    raw = str(value or "").strip()
    if not raw or len(raw) > 2048 or any(ord(ch) < 33 for ch in raw):
        return ""
    try:
        parsed = urlsplit(raw)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            return ""
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return ""
    if not host or any(ch in host for ch in ("/", "\\", "@")):
        return ""
    loopback = host == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(host).is_loopback
    except ValueError:
        pass
    if parsed.scheme.lower() != "https" and not (allow_loopback_http and loopback):
        return ""
    rendered_host = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if port is not None and port != default_port:
        rendered_host = f"{rendered_host}:{port}"
    return f"{parsed.scheme.lower()}://{rendered_host}"


def is_loopback_origin(value: Any) -> bool:
    origin = canonical_shared_origin(value, allow_loopback_http=True)
    if not origin:
        return False
    host = urlsplit(origin).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
