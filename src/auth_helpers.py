"""Shared auth helpers used by all route files."""

import hashlib
import os
import re
from typing import Optional
from fastapi import Request, HTTPException


def _local_owner_from_env() -> str:
    for name in ("RESTIA_FALLBACK_OWNER", "ODYSSEUS_FALLBACK_OWNER"):
        value = str(os.getenv(name) or "").strip().lower()
        if value:
            return value
    return "owner@localhost"


# One concrete owner for auth-disabled and first-run stores. Keep the legacy
# environment alias for existing deployments, but make every new owner-scoped
# feature import this value instead of inventing its own "local" sentinel.
DEFAULT_LOCAL_OWNER = _local_owner_from_env()


def _owner_storage_identity(owner: str | None, fallback: str) -> str:
    """Return the canonical identity used to derive owner-scoped paths."""

    value = str(owner or fallback).strip().lower()
    return value or fallback


def legacy_owner_storage_key(owner: str | None, *, fallback: str = "default") -> str:
    """Reproduce the pre-V2 lossy owner filename component.

    This is only for privacy-checked migration reads. New state must always use
    :func:`owner_storage_key`; otherwise identities such as ``a/b`` and
    ``a_b`` share a file.
    """

    value = _owner_storage_identity(owner, fallback)
    return "".join(
        char if (char.isalnum() or char in "-_.@") else "_"
        for char in value
    )


def owner_storage_key(owner: str | None, *, fallback: str = "default") -> str:
    """Return a stable, collision-resistant filename component for an owner.

    Existing simple ASCII usernames keep their historical component so normal
    upgrades retain state without a migration. Any identity that needs
    escaping or truncation enters a reserved ``~`` namespace and includes a
    SHA-256 suffix. Since ``~`` itself is not in the unescaped alphabet, an
    escaped identity cannot collide with a literal legacy-safe username.
    """

    value = _owner_storage_identity(owner, fallback)
    if (
        len(value) <= 80
        and value not in {".", ".."}
        and re.fullmatch(r"[a-z0-9_.@-]+", value)
    ):
        return value

    readable = re.sub(r"[^a-z0-9_.@-]+", "_", value)
    readable = readable.strip("._-")[:48] or "owner"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"~{readable}-{digest}"


_UNSET_OWNER = object()


def get_current_user(request: Request) -> Optional[str]:
    """Get current username from request state (set by auth middleware)."""
    return getattr(getattr(request, "state", None), "current_user", None)


def effective_user(request: Request) -> Optional[str]:
    """The real human behind the request, for ownership/attribution.

    Cookie sessions resolve to the logged-in username. Bearer ``ody_`` callers
    come through as the sandboxed pseudo-user "api" so they can't wander into
    cookie/user routes by default, but their token was minted by, and belongs
    to, a real owner stamped on ``request.state.api_token_owner``. Routes that
    should attribute a token's actions to that owner (sessions, chat history)
    call this instead of :func:`get_current_user`, so a paired client sees and
    creates the SAME data as the owner's desktop UI rather than a separate
    "api"-owned silo.

    For cookie sessions this is identical to :func:`get_current_user`, so
    swapping a route over is a no-op for browser users. A bearer token with no
    owner falls back to :func:`get_current_user` (the "api" pseudo-user), so it
    never escalates.
    """
    state = getattr(request, "state", None)
    if getattr(state, "api_token", False):
        owner = getattr(state, "api_token_owner", None)
        if owner:
            return owner
    return get_current_user(request)


def _loopback_request(request: Request) -> bool:
    client = getattr(request, "client", None)
    host = (client.host if client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


def configured_single_user_owner(request: Request | None = None) -> Optional[str]:
    """Return the only configured real user/admin when the install is single-user.

    This does not authenticate a request by itself. It is for code paths that
    have already admitted a single-user/local operator request, but still need a
    concrete owner for owner-scoped stores such as email MCP accounts.
    """
    auth_mgr = getattr(getattr(getattr(request, "app", None), "state", None), "auth_manager", None)
    managers = [auth_mgr] if auth_mgr is not None else []
    try:
        from core.auth import AuthManager

        managers.append(AuthManager())
    except Exception:
        pass

    for mgr in managers:
        users = getattr(mgr, "users", None)
        if not isinstance(users, dict) or not users:
            continue
        cleaned = {
            str(name or "").strip().lower(): data
            for name, data in users.items()
            if str(name or "").strip()
        }
        if len(cleaned) == 1:
            return next(iter(cleaned))
        admins = [
            name
            for name, data in cleaned.items()
            if isinstance(data, dict) and data.get("is_admin")
        ]
        if len(admins) == 1:
            return admins[0]
    return None


def effective_owner(request: Request) -> Optional[str]:
    """Resolve the owner to use for owner-scoped runtime data.

    Authenticated requests keep their real user. Auth-disabled installs and
    loopback localhost-bypass requests may not have a cookie user, but on a
    single-user install they still need a concrete owner so owner-scoped tools
    (notably email MCP) can see the operator's data.
    """
    user = effective_user(request)
    if user:
        return user
    if _auth_disabled():
        return configured_single_user_owner(request)
    if _loopback_request(request) and os.getenv("LOCALHOST_BYPASS", "false").lower() == "true":
        return configured_single_user_owner(request)
    return None


def resolved_runtime_owner(owner: str | None = None) -> str:
    """Resolve a concrete owner for admitted non-request runtimes/tools."""

    value = str(owner or configured_single_user_owner() or DEFAULT_LOCAL_OWNER).strip()
    return value.lower() or DEFAULT_LOCAL_OWNER


def resolved_request_owner(
    request: Request,
    *,
    admitted_user: str | None | object = _UNSET_OWNER,
) -> str:
    """Return one concrete owner after applying the route admission gate.

    Owner-scoped features historically made their own choice between ``None``,
    ``"local"``, and ``owner@localhost`` when authentication was disabled.
    That split completion evidence from the profile returned by Progression.
    This helper is the single write/read identity for those routes:

    * an authenticated user remains the owner;
    * an auth-disabled single-profile install uses its configured profile;
    * a first-run/legacy local install uses :data:`DEFAULT_LOCAL_OWNER`.

    Passing ``admitted_user`` avoids running :func:`require_user` twice when a
    route already called it.  The empty string is meaningful and represents an
    explicitly admitted local/single-user request.
    """

    admitted = (
        require_user(request)
        if admitted_user is _UNSET_OWNER
        else str(admitted_user or "").strip()
    )
    value = effective_owner(request) or admitted
    return resolved_runtime_owner(value)


def allows_legacy_null_owner(
    request: Request,
    *,
    admitted_user: str | None | object = _UNSET_OWNER,
) -> bool:
    """Whether this admitted request may access pre-profile ``owner IS NULL`` rows.

    Legacy shared rows are kept only for explicit single-user/local modes.  A
    cookie-authenticated profile never gains access to another scope merely
    because an old row has no owner.
    """

    admitted = (
        require_user(request)
        if admitted_user is _UNSET_OWNER
        else str(admitted_user or "").strip()
    )
    return not bool(admitted)


def _is_api_token_request(request: Request) -> bool:
    """Return True when middleware authenticated a bearer API token."""
    return bool(getattr(getattr(request, "state", None), "api_token", False))


def require_authenticated_request(request: Request) -> str:
    """Allow either a browser session or a valid bearer API token.

    This is intentionally narrower than :func:`require_user`: use it only for
    routes that need authentication but do not read or mutate owner-scoped
    user data. Owner-scoped routes should use ``require_user`` for browser
    sessions or their own API-token scope/owner gate.
    """
    if _is_api_token_request(request):
        return effective_user(request) or ""
    return require_user(request)


def _auth_disabled() -> bool:
    """True when the operator has explicitly turned off auth via .env.
    Mirrors the AUTH_ENABLED parse in app.py / core/middleware.py so the
    three call sites agree on what "off" means."""
    return os.getenv("AUTH_ENABLED", "true").lower() == "false"


def require_user(request: Request) -> str:
    """FastAPI dependency: reject unauthenticated callers when the upstream
    auth middleware was bypassed unexpectedly (e.g. SSRF from a sibling
    service). Returns the resolved username, or "" in single-user / anonymous
    modes where no username is available.

    The three "" cases are:
      1. AUTH_ENABLED=false — the operator explicitly turned auth off.
         The full /login flow is skipped (issue #622), so route-level
         require_user must let the request through too instead of 401-ing
         and forcing the browser to /login.
      2. Unconfigured first-run + loopback caller — pre-setup access from
         localhost so the operator can hit the SPA before creating the
         first admin.
      3. LOCALHOST_BYPASS=true + loopback caller — documented dev bypass.

    Use this on routes that touch user data so middleware misconfig can't
    open them up.
    """
    if _is_api_token_request(request):
        raise HTTPException(403, "API tokens must use a scope-aware API route")

    u = get_current_user(request)
    if u:
        return u
    # Operator-disabled auth: honor it at the route layer too. Without this,
    # routes that depend on require_user 401, the front-end fetch wrapper
    # redirects to /login, and the user sees a login page despite
    # AUTH_ENABLED=false (issue #622). Docker / reverse-proxy deployments
    # hit this because requests arrive from a non-loopback client.host, so
    # the loopback fall-through below never fires.
    if _auth_disabled():
        return ""
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    # LOCALHOST_BYPASS=true is the dev-only "I'm on loopback, skip auth"
    # switch. Mirror the middleware so routes don't 401 the same caller
    # the middleware just let through.
    if _loopback_request(request) and os.getenv("LOCALHOST_BYPASS", "false").lower() == "true":
        return ""
    if auth_mgr is not None and getattr(auth_mgr, "is_configured", False):
        raise HTTPException(401, "Not authenticated")
    # Unconfigured / first-run mode: only allow loopback callers.
    if _loopback_request(request):
        return ""
    raise HTTPException(401, "Not authenticated")


def require_privilege(request: Request, key: str) -> str:
    """Reject callers whose `auth.json` privilege flag for `key` is False.
    Returns the username so the route handler can keep using it.

    Admins always have every privilege via `auth_manager.get_privileges`
    (which returns ADMIN_PRIVILEGES wholesale), so this is a no-op for
    them. In unauthenticated single-user mode (`require_user` returns ""),
    privileges aren't enforced.
    """
    user = require_user(request)
    if not user:
        return user
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    if auth_mgr is None:
        return user
    try:
        privs = auth_mgr.get_privileges(user) or {}
    except Exception:
        return user
    if not isinstance(privs, dict):
        privs = {}
    # True = permitted; missing key defaults to permitted (unknown privileges
    # fail open — the UI gates display-side).
    if not privs.get(key, True):
        raise HTTPException(403, f"Your account is not allowed to {key.replace('_', ' ')}.")
    return user


def owner_filter(query, model_cls, user: str, *, include_shared: bool = True):
    """Filter `query` so only rows owned by `user` (and optionally null-owner
    'shared' rows) come through. No-op when `user` is empty (single-user
    mode). Returns the modified query."""
    if not user:
        return query
    if include_shared:
        return query.filter((model_cls.owner == user) | (model_cls.owner == None))  # noqa: E711
    return query.filter(model_cls.owner == user)
