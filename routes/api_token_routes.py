"""API Token management routes — /api/tokens/*."""

import os
from fastapi import APIRouter, HTTPException, Request, Form

from core.database import get_db_session, ApiToken, utcnow_naive
from core.middleware import require_admin
from src.auth_helpers import get_current_user

MAX_NAME_LEN = 100
DEFAULT_SCOPES = "chat"
ALLOWED_SCOPES = {
    "chat",
    "todos:read",
    "todos:write",
    "documents:read",
    "documents:write",
    "email:read",
    "email:draft",
    "email:send",
    "calendar:read",
    "calendar:write",
    "memory:read",
    "memory:write",
    "life:read",
    "life:write",
    "cookbook:read",
    "cookbook:launch",
}
TOKEN_PROFILES = {
    "chat": ["chat"],
    "codex_todos": ["todos:read", "todos:write"],
    "codex_documents": ["documents:read", "documents:write"],
    "codex_email_drafts": ["email:read", "email:draft", "documents:read", "documents:write"],
    "life_os": ["life:read", "life:write"],
}


def _normalize_scopes(scopes: str | list[str] | None = None, profile: str | None = None) -> list[str]:
    profile = profile if isinstance(profile, str) else None
    profile_key = (profile or "").strip()
    if profile_key:
        if profile_key not in TOKEN_PROFILES:
            raise HTTPException(400, "Unknown token profile")
        requested = list(TOKEN_PROFILES[profile_key])
    elif isinstance(scopes, list):
        requested = [str(s).strip() for s in scopes if str(s).strip()]
    elif isinstance(scopes, str) and scopes:
        requested = [s.strip() for s in scopes.replace(" ", ",").split(",") if s.strip()]
    else:
        requested = [DEFAULT_SCOPES]

    normalized = []
    for scope in requested:
        if scope not in ALLOWED_SCOPES:
            raise HTTPException(400, f"Unknown token scope: {scope}")
        if scope not in normalized:
            normalized.append(scope)

    def ensure_before(write_scope: str, read_scope: str):
        if write_scope not in normalized or read_scope in normalized:
            return
        idx = normalized.index(write_scope)
        normalized.insert(idx, read_scope)

    ensure_before("todos:write", "todos:read")
    ensure_before("documents:write", "documents:read")
    ensure_before("calendar:write", "calendar:read")
    ensure_before("memory:write", "memory:read")
    ensure_before("life:write", "life:read")
    ensure_before("email:draft", "email:read")
    ensure_before("cookbook:launch", "cookbook:read")

    return normalized or [DEFAULT_SCOPES]


def setup_api_token_routes() -> APIRouter:
    router = APIRouter(prefix="/api", tags=["api_tokens"])

    @router.get("/tokens")
    def list_tokens(request: Request):
        require_admin(request)
        with get_db_session() as db:
            tokens = db.query(ApiToken).all()
            return [
                {
                    "id": t.id,
                    "name": t.name,
                    "owner": getattr(t, "owner", None),
                    "token_prefix": t.token_prefix,
                    "scopes": [s.strip() for s in (getattr(t, "scopes", "") or DEFAULT_SCOPES).split(",") if s.strip()],
                    "is_active": t.is_active,
                    "last_used_at": t.last_used_at.isoformat() if t.last_used_at else None,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
                for t in tokens
            ]

    def _auth_manager(request: Request):
        manager = getattr(request.app.state, "auth_manager", None)
        if manager is None:
            raise HTTPException(503, "Database authentication is unavailable")
        return manager

    def _current_account_id(request: Request) -> str | None:
        value = getattr(request.state, "current_account_id", None)
        if value:
            return str(value)
        manager = _auth_manager(request)
        resolver = getattr(manager, "account_id_for_username", None)
        if not callable(resolver):
            raise HTTPException(503, "Database authentication is unavailable")
        return resolver(get_current_user(request))

    def _require_token_owner(request: Request, token: ApiToken) -> None:
        if os.getenv("AUTH_ENABLED", "true").lower() == "false":
            return
        account_id = _current_account_id(request)
        if not account_id or str(getattr(token, "account_id", "") or "") != account_id:
            raise HTTPException(403, "Not your token")

    @router.get("/tokens/profiles")
    def token_profiles(request: Request):
        require_admin(request)
        return {
            "profiles": TOKEN_PROFILES,
            "allowed_scopes": sorted(ALLOWED_SCOPES),
        }

    @router.post("/tokens")
    def create_token(
        request: Request,
        name: str = Form(""),
        scopes: str = Form(None),
        profile: str = Form(None),
    ):
        require_admin(request)
        name = name.strip()[:MAX_NAME_LEN]
        if not name:
            raise HTTPException(400, "Token name is required")
        owner = get_current_user(request)
        scope_list = _normalize_scopes(scopes, profile)
        issue = getattr(_auth_manager(request), "issue_api_token", None)
        if not callable(issue):
            raise HTTPException(503, "Database authentication is unavailable")
        issued = issue(
            owner,
            name=name,
            scopes=scope_list,
        )
        if issued is None:
            raise HTTPException(503, "Could not issue an account-bound API token")

        return {
            "id": issued["id"],
            "name": name,
            "owner": issued["owner"],
            "token": issued["token"],
            "token_prefix": issued["token_prefix"],
            "scopes": issued["scopes"],
        }

    @router.patch("/tokens/{token_id}")
    async def update_token(request: Request, token_id: str):
        require_admin(request)
        current_user = get_current_user(request)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        with get_db_session() as db:
            token = db.query(ApiToken).filter(ApiToken.id == token_id).first()
            if not token:
                raise HTTPException(404, "Token not found")
            if current_user:
                _require_token_owner(request, token)
            if isinstance(payload.get("name"), str) and payload["name"].strip():
                token.name = payload["name"].strip()[:MAX_NAME_LEN]
            # Only touch scopes when the caller actually sent them. A partial
            # update such as a rename ({"name": ...} with no "scopes" key) must
            # not silently reset the token to the default scope — that dropped
            # every previously granted scope.
            if "scopes" in payload:
                token.scopes = ",".join(_normalize_scopes(payload.get("scopes")))
            db.add(token)
            current_scopes = [
                s.strip()
                for s in (getattr(token, "scopes", "") or DEFAULT_SCOPES).split(",")
                if s.strip()
            ]
            response = {
                "id": token_id,
                "name": getattr(token, "name", ""),
                "owner": getattr(token, "owner", None),
                "token_prefix": getattr(token, "token_prefix", ""),
                "scopes": current_scopes,
            }
        return response

    @router.delete("/tokens/{token_id}")
    def delete_token(request: Request, token_id: str):
        require_admin(request)
        current_user = get_current_user(request)
        with get_db_session() as db:
            token = db.query(ApiToken).filter(ApiToken.id == token_id).first()
            if not token:
                raise HTTPException(404, "Token not found")
            if current_user:
                _require_token_owner(request, token)
            token.is_active = False
            token.revoked_at = utcnow_naive()
            db.add(token)
        return {"status": "revoked"}

    return router
