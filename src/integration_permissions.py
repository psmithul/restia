"""Least-privilege method/path grants for user-created API connectors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlsplit


CONNECTOR_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
DEFAULT_CONNECTOR_PERMISSIONS: dict[str, Any] = {
    "allowed_methods": ["GET"],
    "allowed_path_prefixes": ["/"],
    "require_action_approval_for_writes": True,
}


class IntegrationPermissionError(ValueError):
    pass


class IntegrationPermissionDenied(IntegrationPermissionError):
    pass


def _path_prefix(value: object) -> str:
    if not isinstance(value, str):
        raise IntegrationPermissionError("Connector path prefixes must be strings")
    prefix = value.strip()
    if (
        not prefix
        or len(prefix) > 1_000
        or not prefix.startswith("/")
        or prefix.startswith("//")
        or "://" in prefix
        or "?" in prefix
        or "#" in prefix
        or any(part in {".", ".."} for part in prefix.split("/"))
    ):
        raise IntegrationPermissionError(
            "Connector path prefixes must be absolute paths without query, fragment, or traversal"
        )
    return prefix.rstrip("/") or "/"


def normalize_integration_permissions(value: object | None) -> dict[str, Any]:
    if value is None:
        return {
            "allowed_methods": list(DEFAULT_CONNECTOR_PERMISSIONS["allowed_methods"]),
            "allowed_path_prefixes": list(DEFAULT_CONNECTOR_PERMISSIONS["allowed_path_prefixes"]),
            "require_action_approval_for_writes": True,
        }
    if not isinstance(value, Mapping):
        raise IntegrationPermissionError("Connector permissions must be an object")
    unknown = set(value) - {
        "allowed_methods", "allowed_path_prefixes",
        "require_action_approval_for_writes",
    }
    if unknown:
        raise IntegrationPermissionError("Connector permissions contain unsupported fields")
    raw_methods = value.get("allowed_methods", ["GET"])
    if (
        not isinstance(raw_methods, list)
        or not raw_methods
        or len(raw_methods) > len(CONNECTOR_METHODS)
        or any(not isinstance(item, str) for item in raw_methods)
    ):
        raise IntegrationPermissionError("Connector allowed_methods must be a non-empty list")
    methods = {item.strip().upper() for item in raw_methods}
    if not methods <= CONNECTOR_METHODS:
        raise IntegrationPermissionError("Connector permissions contain an unsupported method")
    raw_prefixes = value.get("allowed_path_prefixes", ["/"])
    if (
        not isinstance(raw_prefixes, list)
        or not raw_prefixes
        or len(raw_prefixes) > 50
    ):
        raise IntegrationPermissionError(
            "Connector allowed_path_prefixes must contain 1 to 50 paths"
        )
    prefixes = sorted({_path_prefix(item) for item in raw_prefixes})
    require_approval = value.get("require_action_approval_for_writes", True)
    if not isinstance(require_approval, bool):
        raise IntegrationPermissionError(
            "require_action_approval_for_writes must be true or false"
        )
    if methods - {"GET"} and not require_approval:
        # An enabled connector is agent-visible.  A profile setting cannot
        # turn model output into an unapproved external side effect.
        raise IntegrationPermissionDenied(
            "Connector write methods must require an approved external action"
        )
    return {
        "allowed_methods": sorted(methods),
        "allowed_path_prefixes": prefixes,
        "require_action_approval_for_writes": True,
    }


def integration_request_allowed(
    integration: Mapping[str, Any],
    *,
    method: object,
    path: object,
    approved_external_action: bool = False,
) -> dict[str, Any]:
    permissions = normalize_integration_permissions(integration.get("permissions"))
    normalized_method = str(method or "GET").strip().upper()
    if normalized_method not in permissions["allowed_methods"]:
        raise IntegrationPermissionDenied(
            f"Connector does not grant {normalized_method} requests"
        )
    raw_path = str(path or "")
    parsed = urlsplit(raw_path)
    if parsed.scheme or parsed.netloc or not raw_path.startswith("/"):
        raise IntegrationPermissionDenied(
            "Connector request path must be an absolute path on the configured host"
        )

    # Compare grants against the path the remote HTTP stack will actually see.
    # A raw prefix check alone lets `/api/states/../config` (or an encoded
    # variant) pass a `/api/states` grant before URL joining normalizes it to
    # `/api/config`.  Decode repeatedly to cover one layer added by a proxy,
    # then reject traversal and backslash aliases instead of trying to repair
    # an ambiguous request.
    request_path = parsed.path or "/"
    for _ in range(3):
        decoded = unquote(request_path)
        if decoded == request_path:
            break
        request_path = decoded
    if (
        "\\" in request_path
        or any(part in {".", ".."} for part in request_path.split("/"))
    ):
        raise IntegrationPermissionDenied(
            "Connector request path contains traversal"
        )
    permitted = any(
        prefix == "/"
        or request_path == prefix
        or request_path.startswith(prefix + "/")
        for prefix in permissions["allowed_path_prefixes"]
    )
    if not permitted:
        raise IntegrationPermissionDenied("Connector path is outside its granted prefixes")
    if normalized_method != "GET" and not approved_external_action:
        raise IntegrationPermissionDenied(
            "Connector write requires a freshly approved external action"
        )
    return permissions


__all__ = [
    "CONNECTOR_METHODS",
    "DEFAULT_CONNECTOR_PERMISSIONS",
    "IntegrationPermissionDenied",
    "IntegrationPermissionError",
    "integration_request_allowed",
    "normalize_integration_permissions",
]
