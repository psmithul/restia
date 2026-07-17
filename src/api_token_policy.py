"""Central allowlist for database-backed ``ody_`` API tokens.

An API token is not a browser session.  Validating its signature establishes
the principal, but it does not grant access to every authenticated Restia
route.  This module is the single request-boundary policy used by
``AuthMiddleware``: a bearer request must match one explicit method/path rule
and satisfy every scope group on that rule before a route handler can run.

Route handlers keep their existing, more detailed authorization checks (for
example the action-dependent read/write check on ``POST /api/codex/todos``).
Those checks can narrow access further; this boundary only establishes the
maximum surface an API token may ever reach.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Pattern


ScopeGroup = frozenset[str]


@dataclass(frozen=True)
class _RouteRule:
    methods: frozenset[str]
    path: Pattern[str]
    # Every group is required; one scope from each group is sufficient.
    scope_groups: tuple[ScopeGroup, ...] = ()


def _rule(
    methods: str | Iterable[str],
    path: str,
    *scope_groups: Iterable[str],
) -> _RouteRule:
    normalized_methods = (
        {methods.upper()} if isinstance(methods, str)
        else {str(method).upper() for method in methods}
    )
    return _RouteRule(
        methods=frozenset(normalized_methods),
        path=re.compile(rf"^(?:{path})$"),
        scope_groups=tuple(frozenset(group) for group in scope_groups),
    )


CHAT = frozenset({"chat"})
TODO_READ = frozenset({"todos:read", "todos:write"})
TODO_WRITE = frozenset({"todos:write"})
INBOX_READ = frozenset({"todos:read"})
EMAIL_READ = frozenset({"email:read", "email:draft", "email:send"})
EMAIL_DRAFT = frozenset({"email:draft", "email:send"})
EMAIL_SEND = frozenset({"email:send"})
MEMORY_READ = frozenset({"memory:read", "memory:write"})
MEMORY_WRITE = frozenset({"memory:write"})
LIFE_READ = frozenset({"life:read", "life:write"})
LIFE_WRITE = frozenset({"life:write"})
CALENDAR_READ = frozenset({"calendar:read", "calendar:write"})
CALENDAR_WRITE = frozenset({"calendar:write"})
DOCUMENT_READ = frozenset({"documents:read", "documents:write"})
DOCUMENT_WRITE = frozenset({"documents:write"})
COOKBOOK_READ = frozenset({"cookbook:read", "cookbook:launch"})
COOKBOOK_LAUNCH = frozenset({"cookbook:launch"})


_API_TOKEN_ROUTE_RULES: tuple[_RouteRule, ...] = (
    # Paired chat companion.  Pairing itself remains admin-cookie-only.
    _rule("GET", r"/api/companion/(?:ping|info|models)", CHAT),
    _rule("GET", r"/api/models", CHAT),

    # Owner-scoped chat sessions and history used by paired clients.
    _rule("GET", r"/api/sessions", CHAT),
    _rule("GET", r"/api/sessions/archived", CHAT),
    _rule("GET", r"/api/history/[^/]+", CHAT),
    _rule("GET", r"/api/session/[^/]+/(?:export|context_info)", CHAT),
    _rule("POST", r"/api/session", CHAT),
    _rule("POST", r"/api/session/openai", CHAT),
    _rule(
        "POST",
        r"/api/session/[^/]+/(?:inject_messages|delete|archive|unarchive|important|compact)",
        CHAT,
    ),
    _rule("POST", r"/api/sessions/(?:bulk-delete|save|auto-sort)", CHAT),
    _rule("PATCH", r"/api/session/[^/]+", CHAT),
    _rule("DELETE", r"/api/session/[^/]+", CHAT),
    _rule("DELETE", r"/api/sessions/all", CHAT),

    # Chat execution and the owner-scoped chat helpers.
    _rule("POST", r"/api/(?:chat|chat_stream|rewrite)", CHAT),
    _rule("POST", r"/api/v1/chat", CHAT),
    _rule("GET", r"/api/chat/(?:resume|stream_status)/[^/]+", CHAT),
    _rule("POST", r"/api/chat/stop/[^/]+", CHAT),
    _rule("POST", r"/api/inject_context/[^/]+", CHAT),
    _rule("GET", r"/api/search", CHAT),

    # V3 universal inbox.  Its transaction barrier also checks these scopes.
    _rule("GET", r"/api/inbox", INBOX_READ),
    _rule("POST", r"/api/inbox", TODO_WRITE),
    _rule("GET", r"/api/inbox/[^/]+", INBOX_READ),
    _rule("PATCH", r"/api/inbox/[^/]+", TODO_WRITE),
    _rule("POST", r"/api/inbox/[^/]+/(?:classify|process|archive)", TODO_WRITE),

    # Principal-scoped Life OS graph, focus, and action-policy APIs.  The
    # request transaction inside each handler narrows ownership and versions;
    # this boundary prevents a chat/todo token from reaching the life graph.
    _rule("GET", r"/api/life(?:/.*)?", LIFE_READ),
    _rule(("POST", "PUT", "PATCH", "DELETE"), r"/api/life(?:/.*)?", LIFE_WRITE),

    # Codex discovery/bundle routes intentionally expose no owner data.  Every
    # owner-data route below has an explicit method/path scope rule.
    _rule("GET", r"/api/codex/(?:capabilities|plugin\.zip)"),
    _rule("GET", r"/api/claude/plugin\.zip"),
    _rule("GET", r"/api/codex/todos", TODO_READ),
    # The handler narrows read versus write based on the requested action.
    _rule("POST", r"/api/codex/todos", TODO_READ),
    _rule("GET", r"/api/codex/emails(?:/[^/]+)?", EMAIL_READ),
    _rule("POST", r"/api/codex/emails/draft", EMAIL_DRAFT),
    _rule("POST", r"/api/codex/emails/send", EMAIL_SEND),
    _rule(
        "POST",
        r"/api/codex/emails/draft-document",
        EMAIL_DRAFT,
        DOCUMENT_WRITE,
    ),
    _rule("GET", r"/api/codex/memory", MEMORY_READ),
    _rule("POST", r"/api/codex/memory", MEMORY_WRITE),
    _rule("DELETE", r"/api/codex/memory/[^/]+", MEMORY_WRITE),
    _rule("GET", r"/api/codex/calendar/events", CALENDAR_READ),
    _rule("POST", r"/api/codex/calendar/events", CALENDAR_WRITE),
    _rule("DELETE", r"/api/codex/calendar/events/[^/]+", CALENDAR_WRITE),
    _rule("GET", r"/api/codex/documents(?:/[^/]+)?", DOCUMENT_READ),
    _rule("POST", r"/api/codex/documents", DOCUMENT_WRITE),
    _rule("DELETE", r"/api/codex/documents/[^/]+", DOCUMENT_WRITE),
    _rule(
        "GET",
        r"/api/codex/cookbook/(?:tasks|servers|cached|presets|output/[^/]+)",
        COOKBOOK_READ,
    ),
    _rule(
        "POST",
        r"/api/codex/cookbook/(?:serve|adopt|stop/[^/]+|preset/[^/]+)",
        COOKBOOK_LAUNCH,
    ),
)


def api_token_route_error(
    method: str,
    path: str,
    scopes: Iterable[str] | str | None,
) -> str | None:
    """Return a safe denial reason, or ``None`` when the route is allowed.

    A single trailing slash is treated as the same route because FastAPI may
    redirect between those spellings.  Repeated trailing slashes are not
    normalized and therefore remain denied.
    """

    normalized_method = str(method or "").upper()
    normalized_path = str(path or "")
    if normalized_path != "/" and normalized_path.endswith("/"):
        candidate = normalized_path[:-1]
        if not candidate.endswith("/"):
            normalized_path = candidate

    if isinstance(scopes, str):
        raw_scopes: Iterable[str] = scopes.split(",")
    else:
        raw_scopes = scopes or ()
    granted = {
        str(scope).strip()
        for scope in raw_scopes
        if str(scope).strip()
    }

    for rule in _API_TOKEN_ROUTE_RULES:
        if normalized_method not in rule.methods:
            continue
        if not rule.path.fullmatch(normalized_path):
            continue
        for group in rule.scope_groups:
            if granted.isdisjoint(group):
                choices = " or ".join(sorted(group))
                return f"API token missing required scope: {choices}"
        return None

    return "API token is not permitted for this route"
