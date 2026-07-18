"""Authentication routes — login, logout, signup, status, user management."""

from fastapi import APIRouter, Request, Response, HTTPException
from pydantic import BaseModel, Field
from typing import Any, Callable, Optional
import asyncio
import logging
import os
import uuid
from functools import wraps

import json
import re
from pathlib import Path

from core.atomic_io import atomic_write_json, atomic_write_text
from core.auth import AuthManager, SetAdminResult, TOKEN_TTL, username_is_reserved
from routes.link_routes import GUEST_SUFFIX
from src.constants import DEEP_RESEARCH_DIR, MEMORY_FILE, PASSWORD_MIN_LENGTH, SKILLS_DIR
from src.rate_limiter import RateLimiter
from src.settings_scrub import scrub_settings
from src.settings import (
    load_settings as _load_settings,
    save_settings as _save_settings,
    load_features as _load_features,
    save_features as _save_features,
    DEFAULT_SETTINGS,
)
from src.integrations import (
    load_integrations,
    add_integration,
    update_integration,
    delete_integration,
    get_integration,
    mask_integration_secret,
    execute_api_call,
    INTEGRATION_PRESETS,
    migrate_from_settings,
)

logger = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    username: str
    password: str
    remember: bool = True
    totp_code: Optional[str] = None


class SetupRequest(BaseModel):
    username: str
    password: str


class SignupRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class DeleteUserRequest(BaseModel):
    username: str


class RenameUserRequest(BaseModel):
    username: str


class SetAdminRequest(BaseModel):
    is_admin: bool


class SetOpenRegistrationRequest(BaseModel):
    enabled: bool


class SupabaseSessionRequest(BaseModel):
    access_token: str = Field(min_length=1, max_length=32 * 1024)
    remember: bool = True
    totp_code: Optional[str] = Field(default=None, max_length=128)


class SupabaseLinkRequest(BaseModel):
    access_token: str = Field(min_length=1, max_length=32 * 1024)
    current_password: str = Field(min_length=1, max_length=4096)
    totp_code: Optional[str] = Field(default=None, max_length=128)


class WebAuthnRegistrationBeginRequest(BaseModel):
    label: str = Field(default="Passkey", min_length=1, max_length=160)
    current_password: str = Field(min_length=1, max_length=4096)
    totp_code: Optional[str] = Field(default=None, max_length=128)


class WebAuthnRegistrationCompleteRequest(BaseModel):
    ceremony_id: str = Field(min_length=1, max_length=64)
    label: str = Field(default="Passkey", min_length=1, max_length=160)
    credential: dict[str, Any]


class WebAuthnUnlockCompleteRequest(BaseModel):
    ceremony_id: str = Field(min_length=1, max_length=64)
    credential: dict[str, Any]


class WebAuthnRevokeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=4096)
    totp_code: Optional[str] = Field(default=None, max_length=128)

SESSION_COOKIE = "odysseus_session"


def username_reserved(name: str) -> bool:
    """Names no local account may take: the static reserved set, plus the
    '@remote' namespace that Home Link guests appear under in DMs — a local
    account named 'alice@remote' could impersonate a remote guest
    (see routes/link_routes.py)."""
    key = (name or "").strip().lower()
    return username_is_reserved(key) or key.endswith(GUEST_SUFFIX)


def setup_auth_routes(
    auth_manager: AuthManager,
    *,
    identity_renamer=None,
    supabase_verifier=None,
) -> APIRouter:
    # Keep V3 identity migration independently testable from the route's many
    # legacy owner stores. Production always resolves the real fail-closed
    # migrator; only focused tests that replace SQLAlchemy inject a test double.
    if identity_renamer is None:
        from src.identity import rename_local_identity

        identity_renamer = rename_local_identity

    router = APIRouter(prefix="/api/auth", tags=["auth"])

    passkey_session_factory = getattr(auth_manager, "session_factory", None)

    def _passkey_principal(request: Request):
        """Resolve an exact cookie session; bearer API tokens cannot use WebAuthn."""

        if bool(getattr(request.state, "api_token", False)):
            raise HTTPException(403, "Passkey operations require a browser session")
        token = request.cookies.get(SESSION_COOKIE)
        principal = auth_manager.resolve_session(token)
        if principal is None or not token:
            raise HTTPException(401, "Not authenticated")
        if not callable(passkey_session_factory):
            raise HTTPException(503, "Passkey storage is unavailable")
        return token, principal

    def _run_passkey_operation(
        request: Request,
        operation: Callable[[Any, Any, Any], Any],
        *,
        write: bool,
    ) -> Any:
        """Run one owner-bound passkey operation with immutable audit attribution."""

        _token, principal = _passkey_principal(request)
        from core.database import Account
        from src.audit_context import bind_service_audit_context

        db = passkey_session_factory()
        try:
            account = db.query(Account).filter(Account.id == principal.account_id).one_or_none()
            if account is None:
                raise HTTPException(409, "Authenticated account is unavailable")
            bind_service_audit_context(
                db,
                account_id=account.id,
                interface="web",
                actor_type="account",
                credential_type="session",
                credential_id=principal.credential_id,
            )
            result = operation(db, account, principal)
            if write:
                db.commit()
            else:
                db.rollback()
            return result
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    async def _passkey_call(
        request: Request,
        operation: Callable[[Any, Any, Any], Any],
        *,
        write: bool,
    ) -> Any:
        from src.webauthn_service import (
            PasskeyConflict,
            PasskeyError,
            PasskeyNotFound,
            PasskeyVerificationError,
        )

        try:
            return await asyncio.to_thread(
                _run_passkey_operation,
                request,
                operation,
                write=write,
            )
        except HTTPException:
            raise
        except PasskeyNotFound as exc:
            raise HTTPException(404, str(exc)) from None
        except PasskeyConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except PasskeyVerificationError as exc:
            raise HTTPException(400, str(exc)) from None
        except PasskeyError as exc:
            raise HTTPException(400, str(exc)) from None

    def _reserve_identity_migration(*usernames: str) -> bool:
        """Block owner-scoped writers across an auth rename transaction.

        AuthManager.rename_user intentionally releases its config lock before
        this route migrates SQL/file ownership. Reserving both identities under
        that same lock closes the window where a newly renamed session could
        create owner data that collides with the migration or survives a later
        auth rollback.
        """

        names = {str(value or "").strip().lower() for value in usernames}
        names.discard("")
        lock = getattr(auth_manager, "_config_lock", None)
        if lock is not None:
            lock.acquire()
        try:
            active = set(getattr(auth_manager, "_identity_migrations", set()) or set())
            if active.intersection(names):
                return False
            setattr(auth_manager, "_identity_migrations", active | names)
            return True
        finally:
            if lock is not None:
                lock.release()

    def _release_identity_migration(*usernames: str) -> None:
        names = {str(value or "").strip().lower() for value in usernames}
        names.discard("")
        lock = getattr(auth_manager, "_config_lock", None)
        if lock is not None:
            lock.acquire()
        try:
            active = set(getattr(auth_manager, "_identity_migrations", set()) or set())
            active.difference_update(names)
            setattr(auth_manager, "_identity_migrations", active)
        finally:
            if lock is not None:
                lock.release()

    def _guard_profile_identity_mutation(handler):
        """Reserve a deletion target through preflight, auth, and cleanup."""

        @wraps(handler)
        async def guarded(body: DeleteUserRequest, request: Request):
            target = str(body.username or "").strip().lower()
            requesting_user = str(_get_current_user(request) or "").strip().lower()
            identities = {value for value in (target, requesting_user) if value}
            if identities and not _reserve_identity_migration(*identities):
                raise HTTPException(409, "A profile identity change is already in progress")
            try:
                return await handler(body, request)
            finally:
                if identities:
                    _release_identity_migration(*identities)

        return guarded

    def _guard_profile_rename(handler):
        """Reserve rename source, destination, and requesting principal."""

        @wraps(handler)
        async def guarded(
            username: str,
            body: RenameUserRequest,
            request: Request,
        ):
            old_username = str(username or "").strip().lower()
            new_username = str(body.username or "").strip().lower()
            requesting_user = str(_get_current_user(request) or "").strip().lower()
            identities = {
                value for value in (old_username, new_username, requesting_user) if value
            }
            if identities and not _reserve_identity_migration(*identities):
                raise HTTPException(409, "A profile identity change is already in progress")
            try:
                return await handler(username, body, request)
            finally:
                if identities:
                    _release_identity_migration(*identities)

        return guarded

    _login_limiter = RateLimiter(max_requests=15, window_seconds=60)
    _signup_limiter = RateLimiter(max_requests=3, window_seconds=300)
    _setup_limiter = RateLimiter(max_requests=3, window_seconds=300)
    _external_login_limiter = RateLimiter(max_requests=15, window_seconds=60)
    _external_link_limiter = RateLimiter(max_requests=5, window_seconds=300)

    def _get_current_user(request: Request) -> Optional[str]:
        token = request.cookies.get(SESSION_COOKIE)
        return auth_manager.get_username_for_token(token)

    def _set_session_cookie(
        response: Response,
        token: str,
        *,
        remember: bool,
    ) -> None:
        cookie_kwargs = dict(
            key=SESSION_COOKIE,
            value=token,
            httponly=True,
            samesite="lax",
            secure=os.getenv("SECURE_COOKIES", "false").lower() == "true",
            path="/",
        )
        if remember:
            cookie_kwargs["max_age"] = TOKEN_TTL
        response.set_cookie(**cookie_kwargs)

    @router.post("/setup")
    async def first_run_setup(body: SetupRequest, request: Request):
        """Create initial admin account. Only works if no accounts exist."""
        if not _setup_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        if auth_manager.is_configured:
            raise HTTPException(400, "Already configured")
        if len(body.password) < PASSWORD_MIN_LENGTH:
            raise HTTPException(400, f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
        if len(body.username.strip()) < 1:
            raise HTTPException(400, "Username is required")
        if username_reserved(body.username):
            raise HTTPException(403, "Username is reserved")
        ok = await asyncio.to_thread(auth_manager.setup, body.username, body.password)
        if not ok:
            raise HTTPException(500, "Setup failed")
        # Projects created during localhost first-run use an immutable
        # sentinel owner. Claim them as part of first-admin setup so adding
        # more profiles before opening Projects cannot strand that data.
        try:
            from core.database import Project, SessionLocal, utcnow_naive
            from routes.project_routes import _activity

            setup_username = body.username.strip().lower()
            db = SessionLocal()
            try:
                rows = db.query(Project).filter(Project.owner == "owner@localhost").order_by(
                    Project.created_at.asc(), Project.id.asc()
                ).all()
                for project in rows:
                    project.owner = setup_username
                    project.updated_at = utcnow_naive()
                    _activity(
                        db,
                        project.id,
                        setup_username,
                        "first_run_project_claimed",
                        "Claimed first-run project during profile setup",
                    )
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        except Exception:
            # Setup itself succeeded. Keep the account usable and leave the
            # sentinel rows eligible for the Projects route's singleton claim.
            logger.exception("Failed to claim first-run projects during admin setup")
        return {"ok": True, "message": "Admin account created"}

    @router.post("/signup")
    async def signup(body: SignupRequest, request: Request):
        """Create a new user account. Only works if signup is enabled by admin."""
        if not _signup_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        if not auth_manager.is_configured:
            raise HTTPException(400, "Run setup first")
        if not auth_manager.signup_enabled:
            raise HTTPException(403, "Registration is disabled. Ask an admin for an account.")
        if len(body.password) < PASSWORD_MIN_LENGTH:
            raise HTTPException(400, f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
        if len(body.username.strip()) < 1:
            raise HTTPException(400, "Username is required")
        if username_reserved(body.username):
            raise HTTPException(403, "Username is reserved")
        ok = await asyncio.to_thread(auth_manager.create_user, body.username, body.password, is_admin=False)
        if not ok:
            raise HTTPException(409, "Username already taken")
        return {"ok": True, "message": "Account created"}

    @router.post("/login")
    async def login(body: LoginRequest, request: Request, response: Response):
        if not _login_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        username = body.username.strip().lower()
        authenticate = getattr(auth_manager, "authenticate_session", None)
        if not callable(authenticate):
            raise HTTPException(503, "Database authentication is unavailable")
        result = await asyncio.to_thread(
            authenticate,
            username,
            body.password,
            totp_code=body.totp_code,
            interface="web",
        )
        if result.requires_totp:
            return {
                "ok": False,
                "requires_totp": True,
                "username": result.username or username,
            }
        token = result.token
        if not token:
            raise HTTPException(401, "Invalid credentials")
        _set_session_cookie(response, token, remember=body.remember)
        return {"ok": True, "username": result.username or username}

    @router.post("/external/supabase/login")
    async def supabase_login(
        body: SupabaseSessionRequest,
        request: Request,
        response: Response,
    ):
        """Exchange a verified, already-linked Supabase subject for a session."""

        if supabase_verifier is None:
            raise HTTPException(503, "Supabase login is not configured")
        if not _external_login_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        try:
            from src.supabase_auth import SupabaseJWTVerificationError

            identity = await asyncio.to_thread(
                supabase_verifier.verify, body.access_token
            )
        except SupabaseJWTVerificationError:
            # Do not expose signature/key/claim distinctions as an identity
            # oracle to unauthenticated clients.
            raise HTTPException(401, "Invalid external credentials") from None
        authenticate = getattr(
            auth_manager, "authenticate_external_identity", None
        )
        if not callable(authenticate):
            raise HTTPException(503, "Database authentication is unavailable")
        result = await asyncio.to_thread(
            authenticate,
            provider=identity.auth_provider,
            issuer=identity.issuer,
            subject=identity.subject,
            totp_code=body.totp_code,
            interface="web",
        )
        if result.requires_totp:
            return {"ok": False, "requires_totp": True}
        if not result.token:
            raise HTTPException(401, "Invalid external credentials")
        _set_session_cookie(response, result.token, remember=body.remember)
        return {
            "ok": True,
            "username": result.username,
            "account_id": result.account_id,
            "provider": "supabase",
        }

    @router.post("/external/supabase/link")
    async def supabase_link(body: SupabaseLinkRequest, request: Request):
        """Explicitly link a verified Supabase subject to this signed-in account."""

        if supabase_verifier is None:
            raise HTTPException(503, "Supabase login is not configured")
        if not _external_link_limiter.check(request.client.host):
            raise HTTPException(429, "Too many requests — try again later")
        session_token = request.cookies.get(SESSION_COOKIE)
        principal = auth_manager.resolve_session(session_token)
        if principal is None:
            raise HTTPException(401, "Not authenticated")
        try:
            from src.supabase_auth import SupabaseJWTVerificationError

            identity = await asyncio.to_thread(
                supabase_verifier.verify, body.access_token
            )
        except SupabaseJWTVerificationError:
            raise HTTPException(401, "Invalid external credentials") from None
        linker = getattr(auth_manager, "link_external_identity", None)
        if not callable(linker):
            raise HTTPException(503, "Database authentication is unavailable")
        linked = await asyncio.to_thread(
            linker,
            session_token,
            current_password=body.current_password,
            totp_code=body.totp_code,
            provider=identity.auth_provider,
            issuer=identity.issuer,
            subject=identity.subject,
        )
        if not linked:
            raise HTTPException(403, "External identity link authorization failed")
        return {"ok": True, "provider": "supabase"}

    @router.post("/logout")
    async def logout(request: Request, response: Response):
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            auth_manager.revoke_token(token)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True}

    @router.get("/sessions")
    async def list_auth_sessions(request: Request):
        """List this account's active browser/native sessions, never tokens."""

        user = _get_current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        list_sessions = getattr(auth_manager, "list_user_sessions", None)
        if not callable(list_sessions):
            raise HTTPException(503, "Session management is unavailable")
        items = await asyncio.to_thread(
            list_sessions,
            user,
            request.cookies.get(SESSION_COOKIE),
        )
        return {"sessions": items, "count": len(items)}

    @router.delete("/sessions/{session_id}")
    async def revoke_auth_session(
        session_id: str,
        request: Request,
        response: Response,
    ):
        """Revoke one active session owned by the signed-in account."""

        user = _get_current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        if not session_id or len(session_id) > 128:
            raise HTTPException(404, "Session not found")
        list_sessions = getattr(auth_manager, "list_user_sessions", None)
        revoke_session = getattr(auth_manager, "revoke_user_session", None)
        if not callable(list_sessions) or not callable(revoke_session):
            raise HTTPException(503, "Session management is unavailable")
        current_token = request.cookies.get(SESSION_COOKIE)
        items = await asyncio.to_thread(list_sessions, user, current_token)
        target = next((item for item in items if item.get("id") == session_id), None)
        if target is None:
            raise HTTPException(404, "Session not found")
        revoked = await asyncio.to_thread(revoke_session, user, session_id)
        if not revoked:
            raise HTTPException(409, "Session changed in another client")
        current = bool(target.get("current"))
        if current:
            response.delete_cookie(SESSION_COOKIE, path="/")
        return {"ok": True, "revoked_session_id": session_id, "current": current}

    @router.get("/webauthn/status")
    async def webauthn_status(request: Request):
        """List public passkey metadata and this session's verification grant."""

        from src.webauthn_service import list_credentials, session_verification_state

        result = await _passkey_call(
            request,
            lambda db, account, principal: {
                "supported": True,
                "credentials": list_credentials(db, account_id=account.id),
                "verification": session_verification_state(
                    db,
                    account_id=account.id,
                    auth_session_id=principal.credential_id,
                ),
            },
            write=False,
        )
        result["count"] = len(result["credentials"])
        return result

    @router.post("/webauthn/register/options")
    async def webauthn_registration_options(
        body: WebAuthnRegistrationBeginRequest,
        request: Request,
    ):
        """Begin passkey enrollment after password and active-MFA step-up."""

        token, _principal = _passkey_principal(request)
        step_up = getattr(auth_manager, "verify_session_step_up", None)
        if not callable(step_up):
            raise HTTPException(503, "Passkey enrollment authorization is unavailable")
        authorized = await asyncio.to_thread(
            step_up,
            token,
            current_password=body.current_password,
            totp_code=body.totp_code,
        )
        if authorized is None:
            raise HTTPException(403, "Password or MFA verification failed")
        from src.webauthn_service import begin_registration, relying_party_context

        request_base_url = str(request.base_url)
        return await _passkey_call(
            request,
            lambda db, account, principal: begin_registration(
                db,
                account=account,
                auth_session_id=principal.credential_id,
                context=relying_party_context(request_base_url),
                label=body.label,
            ),
            write=True,
        )

    @router.post("/webauthn/register/complete")
    async def webauthn_registration_complete(
        body: WebAuthnRegistrationCompleteRequest,
        request: Request,
    ):
        from src.webauthn_service import complete_registration

        credential = await _passkey_call(
            request,
            lambda db, account, principal: complete_registration(
                db,
                account=account,
                auth_session_id=principal.credential_id,
                ceremony_id=body.ceremony_id,
                label=body.label,
                credential=body.credential,
            ),
            write=True,
        )
        return {"ok": True, "credential": credential}

    @router.post("/webauthn/unlock/options")
    async def webauthn_unlock_options(request: Request):
        from src.webauthn_service import begin_unlock, relying_party_context

        request_base_url = str(request.base_url)
        return await _passkey_call(
            request,
            lambda db, account, principal: begin_unlock(
                db,
                account=account,
                auth_session_id=principal.credential_id,
                context=relying_party_context(request_base_url),
            ),
            write=True,
        )

    @router.post("/webauthn/unlock/complete")
    async def webauthn_unlock_complete(
        body: WebAuthnUnlockCompleteRequest,
        request: Request,
    ):
        from src.webauthn_service import complete_unlock

        verification = await _passkey_call(
            request,
            lambda db, account, principal: complete_unlock(
                db,
                account=account,
                auth_session_id=principal.credential_id,
                ceremony_id=body.ceremony_id,
                credential=body.credential,
            ),
            write=True,
        )
        return {"ok": True, "verification": verification}

    @router.post("/webauthn/credentials/{credential_id}/revoke")
    async def webauthn_revoke(
        credential_id: str,
        body: WebAuthnRevokeRequest,
        request: Request,
    ):
        token, _principal = _passkey_principal(request)
        step_up = getattr(auth_manager, "verify_session_step_up", None)
        if not callable(step_up):
            raise HTTPException(503, "Passkey revocation authorization is unavailable")
        authorized = await asyncio.to_thread(
            step_up,
            token,
            current_password=body.current_password,
            totp_code=body.totp_code,
        )
        if authorized is None:
            raise HTTPException(403, "Password or MFA verification failed")
        from src.webauthn_service import revoke_credential

        result = await _passkey_call(
            request,
            lambda db, account, _principal: revoke_credential(
                db,
                account=account,
                credential_id=credential_id,
            ),
            write=True,
        )
        return {"ok": True, **result}

    @router.get("/status")
    async def auth_status(request: Request):
        token = request.cookies.get(SESSION_COOKIE)
        result = auth_manager.status(token)
        result["signup_enabled"] = auth_manager.signup_enabled
        # Include the caller's effective privileges so the frontend can
        # hide / dim UI controls the user isn't allowed to use. Admins get
        # ADMIN_PRIVILEGES (everything on), regular users get their stored
        # set merged with DEFAULT_PRIVILEGES.
        try:
            u = result.get("username")
            if u:
                result["privileges"] = auth_manager.get_privileges(u)
        except Exception:
            pass
        return result

    @router.get("/policy")
    async def auth_policy():
        """Return public auth policy constants for the frontend."""
        return auth_manager.policy()

    @router.post("/change-password")
    async def change_password(body: ChangePasswordRequest, request: Request):
        user = _get_current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        if len(body.new_password) < PASSWORD_MIN_LENGTH:
            raise HTTPException(400, f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
        current_token = request.cookies.get(SESSION_COOKIE)
        ok = await asyncio.to_thread(
            auth_manager.change_password,
            user,
            body.current_password,
            body.new_password,
            current_token,
        )
        if not ok:
            raise HTTPException(400, "Current password is incorrect")
        return {"ok": True}

    # ------------------------------------------------------------------
    # Two-factor authentication
    # ------------------------------------------------------------------

    @router.post("/2fa/setup")
    async def totp_setup(request: Request):
        """Generate a TOTP secret and return the QR code URI."""
        user = _get_current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        if auth_manager.totp_enabled(user):
            raise HTTPException(400, "2FA is already enabled")
        secret = auth_manager.totp_generate_secret(user)
        if not secret:
            raise HTTPException(500, "Failed to generate secret")
        uri = auth_manager.totp_get_provisioning_uri(user, secret)
        # Generate QR code as base64 PNG
        import qrcode, io, base64
        qr = qrcode.make(uri, box_size=6, border=2)
        buf = io.BytesIO()
        qr.save(buf, format="PNG")
        qr_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return {"secret": secret, "uri": uri, "qr_code": f"data:image/png;base64,{qr_b64}"}

    class TotpVerifyRequest(BaseModel):
        code: str
        password: str = Field(min_length=1, max_length=4096)

    @router.post("/2fa/confirm")
    async def totp_confirm(body: TotpVerifyRequest, request: Request):
        """Verify a TOTP code to confirm 2FA setup. Returns backup codes."""
        user = _get_current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        backup = auth_manager.totp_confirm_enable(
            user,
            body.code,
            body.password,
            request.cookies.get(SESSION_COOKIE),
        )
        if not backup:
            raise HTTPException(400, "Invalid code — try again")
        return {"ok": True, "backup_codes": backup}

    class TotpDisableRequest(BaseModel):
        password: str

    @router.post("/2fa/disable")
    async def totp_disable(body: TotpDisableRequest, request: Request):
        """Disable 2FA. Requires password confirmation."""
        user = _get_current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        if not auth_manager.totp_disable(
            user,
            body.password,
            request.cookies.get(SESSION_COOKIE),
        ):
            raise HTTPException(400, "Invalid password")
        return {"ok": True}

    @router.get("/2fa/status")
    async def totp_status(request: Request):
        """Check if 2FA is enabled for the current user."""
        user = _get_current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        return {"enabled": auth_manager.totp_enabled(user)}

    # Admin-only profile management.  ``/users`` remains a compatibility
    # surface for older clients; new UI and API consumers use ``/profiles``.
    @router.get("/profiles")
    @router.get("/users", deprecated=True)
    async def list_users(request: Request):
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        profiles = auth_manager.list_users()
        return {"profiles": profiles, "users": profiles}

    @router.post("/profiles")
    @router.post("/users", deprecated=True)
    async def admin_create_user(body: CreateUserRequest, request: Request):
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        if len(body.password) < PASSWORD_MIN_LENGTH:
            raise HTTPException(400, f"Password must be at least {PASSWORD_MIN_LENGTH} characters")
        if len(body.username.strip()) < 1:
            raise HTTPException(400, "Username is required")
        if username_reserved(body.username):
            raise HTTPException(403, "Username is reserved")
        ok = auth_manager.create_user(
            body.username,
            body.password,
            body.is_admin,
            requesting_user=user,
        )
        if not ok:
            raise HTTPException(409, "Username already taken")
        return {"ok": True}

    @router.put("/profiles/{username}/privileges")
    @router.put("/users/{username}/privileges", deprecated=True)
    async def update_user_privileges(username: str, request: Request):
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        body = await request.json()
        ok = auth_manager.set_privileges(
            username,
            body,
            requesting_user=user,
        )
        if not ok:
            raise HTTPException(404, "User not found or is admin")
        return {"ok": True, "privileges": auth_manager.get_privileges(username)}

    @router.put("/profiles/{username}/rename")
    @router.put("/users/{username}/rename", deprecated=True)
    @_guard_profile_rename
    async def rename_user(username: str, body: RenameUserRequest, request: Request):
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        old_username = (username or "").strip().lower()
        new_username = (body.username or "").strip().lower()
        if not new_username:
            raise HTTPException(400, "Username required")
        if old_username == new_username:
            return {"ok": True, "username": new_username, "renamed_self": old_username == user}
        if old_username not in auth_manager.users:
            raise HTTPException(404, "User not found")
        if new_username in auth_manager.users:
            raise HTTPException(409, "Username already taken")
        if username_reserved(new_username):
            raise HTTPException(403, "Username is reserved")

        # TODO(project-identity-journal): persist an idempotent pending rename
        # before database identity changes and resume it during startup. The in-process
        # coordinator below closes request races, but process death between the
        # auth save and SQL owner migration still requires durable recovery.
        # Gate on auth first. Every mutation below is contingent on this
        # succeeding — doing it last meant a rejected rename (e.g. reserved
        # username) left file-backed owner fields already rewritten with no
        # way to roll them back.
        ok = auth_manager.rename_user(old_username, new_username, user)
        if not ok:
            raise HTTPException(400, "Cannot rename user")

        def _rollback_auth_rename() -> bool:
            # On self-rename the admin session has already moved to the new
            # username, so the rollback must authenticate as the new user.
            rollback_user = new_username if user == old_username else user
            try:
                return bool(
                    auth_manager.rollback_user_rename(
                        new_username,
                        old_username,
                        rollback_user,
                    )
                )
            except Exception as rollback_err:
                logger.error(
                    "Failed to roll back auth rename %s -> %s after owner migration failure: %s",
                    new_username, old_username, rollback_err,
                )
                return False

        # Usernames are ownership keys for user data. Rename the common
        # owner-scoped DB rows so the account keeps access to its sessions,
        # docs, email accounts, tasks, etc.
        try:
            from sqlalchemy import func
            from core.database import (
                Base, DirectMessage, HomeLink, LinkInvite, RemoteBlock,
                RemoteContactPref, SessionLocal, StatusPost, StatusView,
                UserKey, UserProfile, Project, ProjectMember, ProjectRemoteGrant,
                ProjectWorkItem, ProjectChecklistItem, ProjectComment,
                ProjectAttachment, ProjectActivity, ProjectQuotaLock,
                project_owner_quota_lock_key,
            )
            db = SessionLocal()
            try:
                # V3 domains use Account.id as their durable owner key.  Keep
                # that UUID stable while this same SQL transaction migrates
                # the legacy username-owned rows.
                identity_renamer(db, old_username, new_username)
                for mapper in Base.registry.mappers:
                    model = mapper.class_
                    if not hasattr(model, "owner"):
                        continue
                    (
                        db.query(model)
                        .filter(func.lower(model.owner) == old_username)
                        .update({"owner": new_username}, synchronize_session=False)
                    )
                # Identity-bearing tables predate the generic ``owner``
                # convention. Leaving any of these behind either orphaned the
                # renamed profile's private state or let a later profile with
                # the old name inherit it.
                identity_columns = (
                    (Project, Project.owner),
                    (ProjectMember, ProjectMember.username),
                    (ProjectMember, ProjectMember.added_by),
                    (ProjectRemoteGrant, ProjectRemoteGrant.invited_by),
                    (ProjectWorkItem, ProjectWorkItem.reporter),
                    (ProjectWorkItem, ProjectWorkItem.assignee),
                    (ProjectChecklistItem, ProjectChecklistItem.created_by),
                    (ProjectComment, ProjectComment.author),
                    (ProjectAttachment, ProjectAttachment.uploader),
                    (ProjectActivity, ProjectActivity.actor),
                    (DirectMessage, DirectMessage.sender),
                    (DirectMessage, DirectMessage.recipient),
                    (UserKey, UserKey.username),
                    (UserProfile, UserProfile.username),
                    (StatusPost, StatusPost.author),
                    (StatusView, StatusView.viewer),
                    (HomeLink, HomeLink.local_user),
                    (RemoteContactPref, RemoteContactPref.local_user),
                    (RemoteBlock, RemoteBlock.local_user),
                    (LinkInvite, LinkInvite.created_by),
                )
                for model, column in identity_columns:
                    (
                        db.query(model)
                        .filter(func.lower(column) == old_username)
                        .update({column.key: new_username}, synchronize_session=False)
                    )
                # Owner quota mutexes are created lazily. Remove the obsolete
                # hash so repeated profile renames cannot grow the lock table.
                db.query(ProjectQuotaLock).filter(
                    ProjectQuotaLock.key == project_owner_quota_lock_key(old_username),
                    ProjectQuotaLock.project_id.is_(None),
                ).delete(synchronize_session=False)
                # Reaction ownership is encoded as JSON object keys rather
                # than a column, so it needs an explicit rewrite too.
                reacted = db.query(DirectMessage).filter(DirectMessage.reactions.isnot(None)).all()
                for message in reacted:
                    try:
                        reactions = json.loads(message.reactions or "{}")
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(reactions, dict):
                        continue
                    key = next(
                        (k for k in reactions if str(k).strip().lower() == old_username),
                        None,
                    )
                    if key is not None:
                        reactions[new_username] = reactions.pop(key)
                        message.reactions = json.dumps(reactions)
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        except Exception as e:
            logger.error("Failed to rename owner references %s -> %s: %s", old_username, new_username, e)
            if not _rollback_auth_rename():
                logger.error(
                    "Auth rename %s -> %s could not be rolled back after owner migration failure",
                    old_username, new_username,
                )
            raise HTTPException(500, "Failed to rename user data")

        # Per-user prefs are JSON-backed, not SQL-backed.
        try:
            from routes.prefs_routes import _load as _load_prefs, _save as _save_prefs
            prefs = _load_prefs()
            users = prefs.get("_users") if isinstance(prefs, dict) else None
            if isinstance(users, dict):
                prefs_key = next(
                    (k for k in users if str(k).strip().lower() == old_username),
                    None,
                )
                new_taken = any(str(k).strip().lower() == new_username for k in users)
                if prefs_key is not None and not new_taken:
                    users[new_username] = users.pop(prefs_key)
                    _save_prefs(prefs)
        except Exception as e:
            logger.warning("Failed to rename user prefs %s -> %s: %s", old_username, new_username, e)

        # In-flight deep-research tasks live in the process-local
        # ResearchHandler registry. They are not covered by the persisted JSON
        # migration above, but the research routes filter and cancel by this
        # owner field while the job is running. Do this before sweeping
        # completed JSON files so a job that finishes during the rename saves
        # with the new owner or is caught by the disk sweep below.
        try:
            rh = getattr(request.app.state, "research_handler", None)
            rename_owner = getattr(rh, "rename_owner", None)
            if callable(rename_owner):
                rename_owner(old_username, new_username)
        except Exception as e:
            logger.warning("Failed to rename active research tasks %s -> %s: %s", old_username, new_username, e)

        # deep_research: each completed report is a standalone JSON file with
        # an `owner` field. research_routes filters by d.get("owner") == user,
        # so a stale owner makes every report invisible to the renamed user.
        try:
            dr_dir = Path(DEEP_RESEARCH_DIR)
            if dr_dir.is_dir():
                for p in dr_dir.glob("*.json"):
                    try:
                        d = json.loads(p.read_text(encoding="utf-8"))
                        if str(d.get("owner", "")).strip().lower() == old_username:
                            d["owner"] = new_username
                            atomic_write_json(str(p), d)
                    except Exception as err:
                        logger.warning("Failed to update research owner in %s: %s", p.name, err)
        except Exception as e:
            logger.warning("Failed to rename research owner references %s -> %s: %s", old_username, new_username, e)

        # memory.json: a flat JSON array where each entry carries an `owner`
        # field. memory_manager.load(owner=user) filters on it, so stale
        # entries disappear from the memory panel.
        try:
            if os.path.isfile(MEMORY_FILE):
                with open(MEMORY_FILE, encoding="utf-8") as fh:
                    entries = json.loads(fh.read())
                if isinstance(entries, list):
                    changed = False
                    for entry in entries:
                        if isinstance(entry, dict) and str(entry.get("owner", "")).strip().lower() == old_username:
                            entry["owner"] = new_username
                            changed = True
                    if changed:
                        atomic_write_json(MEMORY_FILE, entries)
        except Exception as e:
            logger.warning("Failed to rename memory.json owner references %s -> %s: %s", old_username, new_username, e)

        # uploads.json: upload rows use owner metadata for access checks and
        # owner-prefixed index keys for dedupe. Rename both so attachments keep
        # resolving after the account username changes.
        try:
            upload_handler = getattr(request.app.state, "upload_handler", None)
            rename_owner = getattr(upload_handler, "rename_owner", None)
            if callable(rename_owner):
                rename_owner(old_username, new_username)
        except Exception as e:
            logger.warning("Failed to rename upload owner references %s -> %s: %s", old_username, new_username, e)

        # direct personal RAG uploads live in per-owner directories and the
        # vector metadata also carries the username used for owner-filtered
        # search. Keep both in sync with the auth rename.
        try:
            from routes.personal_routes import rename_personal_upload_owner
            personal_docs_manager = getattr(request.app.state, "personal_docs_manager", None)
            if personal_docs_manager is not None:
                rag_manager = getattr(personal_docs_manager, "rag_manager", None)
                rename_personal_upload_owner(
                    old_username,
                    new_username,
                    personal_docs_manager=personal_docs_manager,
                    rag_manager=rag_manager,
                )
        except Exception as e:
            logger.warning("Failed to rename personal RAG upload owner references %s -> %s: %s", old_username, new_username, e)

        # skills: SKILL.md frontmatter carries owner: <username>; the usage
        # sidecar (_usage.json) keys entries as owner::skill-name. Both must
        # be updated or the renamed user's Skills panel goes empty.
        try:
            skills_root = Path(SKILLS_DIR)
            if skills_root.is_dir():
                _owner_re = re.compile(
                    r'(?m)^(owner:\s*)' + re.escape(old_username) + r'\s*$',
                    re.IGNORECASE,
                )
                for p in skills_root.rglob("SKILL.md"):
                    try:
                        text = p.read_text(encoding="utf-8")
                        new_text = _owner_re.sub(r'\g<1>' + new_username, text)
                        if new_text != text:
                            atomic_write_text(str(p), new_text)
                    except Exception as err:
                        logger.warning("Failed to update skill owner in %s: %s", p, err)
                usage_path = skills_root / "_usage.json"
                if usage_path.is_file():
                    try:
                        usage = json.loads(usage_path.read_text(encoding="utf-8"))
                        if isinstance(usage, dict):
                            new_usage = {}
                            changed = False
                            for k, v in usage.items():
                                owner_part, sep, skill_part = k.partition("::")
                                if sep and owner_part.lower() == old_username:
                                    new_usage[new_username + "::" + skill_part] = v
                                    changed = True
                                else:
                                    new_usage[k] = v
                            if changed:
                                atomic_write_json(str(usage_path), new_usage)
                    except Exception as err:
                        logger.warning("Failed to update skills usage keys %s -> %s: %s", old_username, new_username, err)
        except Exception as e:
            logger.warning("Failed to rename skills owner references %s -> %s: %s", old_username, new_username, e)

        # The in-memory session cache (session_manager.sessions) stores each
        # session's owner at load time. Without this patch the renamed user's
        # sessions are invisible on the next /api/sessions call because
        # get_sessions_for_user does an exact `s.owner == username` comparison
        # against stale in-memory values.
        sm = getattr(request.app.state, "session_manager", None)
        if sm is not None:
            for sess in list(getattr(sm, "sessions", {}).values()):
                if str(getattr(sess, "owner", None) or "").strip().lower() == old_username:
                    sess.owner = new_username

        # The owner-rename loop above updated ApiToken.owner in the DB, but the
        # bearer-token cache still maps each token to the OLD owner. Without
        # refreshing it, the renamed user's API tokens resolve to the old (now
        # non-existent) owner and stop reaching their data until the cache next
        # goes dirty. Invalidate it now, like the token CRUD routes do.
        invalidator = getattr(request.app.state, "invalidate_token_cache", None)
        if callable(invalidator):
            invalidator()
        # AuthManager reserved the former ownership key atomically with the
        # initial rename, before any of these external stores were migrated.
        return {"ok": True, "username": new_username, "renamed_self": old_username == user}

    @router.put("/profiles/{username}/admin")
    @router.put("/users/{username}/admin", deprecated=True)
    async def set_user_admin(username: str, body: SetAdminRequest, request: Request):
        """Promote/demote a user to/from admin. Admin only.

        The last remaining admin can't be demoted (no lockout). Self-demotion
        is allowed while another admin exists; the `self` flag tells the UI to
        reload the acting user into the normal-user view.
        """
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        result = auth_manager.set_admin(username, body.is_admin, user)
        if result is SetAdminResult.USER_NOT_FOUND:
            raise HTTPException(404, "User not found")
        if result is SetAdminResult.NOT_AUTHORIZED:
            raise HTTPException(403, "Admin only")
        if result is SetAdminResult.LAST_ADMIN:
            raise HTTPException(400, "Cannot demote the last admin")
        target = (username or "").strip().lower()
        return {
            "ok": True,
            "is_admin": body.is_admin,
            "self": target == (user or "").strip().lower(),
        }

    @router.post("/signup-toggle", deprecated=True)
    async def toggle_signup(request: Request):
        """
        Toggle open registration on/off. Admin only.

        DEPRECATED: This endpoint uses toggle semantics which can lead to unsafe state changes.
        Use PUT /open-signup instead.

        This endpoint is kept for backward compatibility and may be removed in future versions.
        """
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        enabled = not auth_manager.signup_enabled
        setter = getattr(auth_manager, "set_signup_enabled", None)
        if callable(setter):
            if not setter(enabled, user):
                raise HTTPException(403, "Admin only")
        else:
            auth_manager.signup_enabled = enabled
        return {"ok": True, "signup_enabled": auth_manager.signup_enabled}

    @router.put("/open-signup")
    async def set_signup_enabled(body: SetOpenRegistrationRequest, request: Request):
        """Set open signup enabled state. Admin only."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        setter = getattr(auth_manager, "set_signup_enabled", None)
        if callable(setter):
            if not setter(body.enabled, user):
                raise HTTPException(403, "Admin only")
        else:
            auth_manager.signup_enabled = body.enabled
        return {"ok": True,"signup_enabled": auth_manager.signup_enabled}

    @router.delete("/profiles")
    @router.delete("/users", deprecated=True)
    @_guard_profile_identity_mutation
    async def admin_delete_user(body: DeleteUserRequest, request: Request):
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")

        target_username = (body.username or "").strip().lower()
        # A deleted owner cannot be recreated (usernames are retired), so
        # silently leaving their projects behind would permanently remove the
        # only principal allowed to manage stages/members/deletion. Require an
        # explicit project transfer/delete first; the Projects API exposes an
        # owner-only transfer endpoint for that workflow.
        try:
            from sqlalchemy import func
            from core.database import OutboundChatLink, Project, SessionLocal

            db = SessionLocal()
            try:
                owned_projects = db.query(Project.id).filter(
                    func.lower(Project.owner) == target_username
                ).count()
                owned_chat_links = db.query(OutboundChatLink.id).filter(
                    func.lower(OutboundChatLink.owner) == target_username
                ).count()
            finally:
                db.close()
        except Exception as exc:
            logger.error("Failed project-ownership preflight for profile deletion: %s", exc)
            raise HTTPException(503, "Could not verify project ownership; profile was not deleted")
        if owned_projects:
            raise HTTPException(
                409,
                f"Transfer or delete {owned_projects} owned project(s) before deleting this profile",
            )
        if owned_chat_links:
            raise HTTPException(
                409,
                f"Disconnect {owned_chat_links} connected Restia chat(s) before deleting this profile",
            )

        def _invalidate_api_token_cache():
            try:
                invalidator = getattr(request.app.state, "invalidate_token_cache", None)
                if invalidator:
                    invalidator()
            except Exception:
                pass

        try:
            ok = auth_manager.delete_user(body.username, user)
        except Exception:
            # delete_user can touch ApiToken rows before a later auth-store write
            # fails. Dirty the bearer cache anyway so a partial token purge does
            # not leave already-cached tokens authenticating until restart.
            _invalidate_api_token_cache()
            raise
        if not ok:
            raise HTTPException(400, "Cannot delete user")
        # Token revocation is already committed. Invalidate the process cache
        # before any later project cleanup can fail and exit this request.
        _invalidate_api_token_cache()
        # Membership is access state rather than historical attribution. Once
        # the auth principal is gone, remove it and unassign open work; keep
        # reporter/comment/activity names as immutable audit history.
        try:
            from sqlalchemy import func
            from core.database import (
                Project,
                ProjectMember,
                ProjectQuotaLock,
                ProjectWorkItem,
                SessionLocal,
                project_owner_quota_lock_key,
                utcnow_naive,
            )
            from routes.project_routes import _activity

            db = SessionLocal()
            try:
                # A target request may have committed a project after the
                # preflight but before delete_user acquired AuthManager's
                # identity lock. Project writes now share that lock and reject
                # retired actors, so this post-delete sweep deterministically
                # catches the only remaining window and hands ownership to the
                # deleting admin instead of stranding data.
                admin_username = str(user).strip().lower()
                raced_projects = db.query(Project).filter(
                    func.lower(Project.owner) == target_username
                ).order_by(Project.created_at.asc(), Project.id.asc()).all()
                for project in raced_projects:
                    candidate = project.key
                    suffix = 2
                    while db.query(Project.id).filter(
                        Project.owner == admin_username,
                        Project.key == candidate,
                        Project.id != project.id,
                    ).first():
                        suffix_text = str(suffix)
                        candidate = project.key[: 12 - len(suffix_text)] + suffix_text
                        suffix += 1
                    project.key = candidate
                    project.owner = admin_username
                    project.version = int(project.version or 1) + 1
                    project.updated_at = utcnow_naive()
                    db.query(ProjectMember).filter(
                        ProjectMember.project_id == project.id,
                        func.lower(ProjectMember.username) == admin_username,
                    ).delete(synchronize_session=False)
                    _activity(
                        db,
                        project.id,
                        admin_username,
                        "project_owner_recovered",
                        "Recovered ownership during profile deletion",
                        payload={"deleted_owner": target_username},
                    )
                    # SessionLocal disables autoflush. Materialize the newly
                    # claimed owner/key before selecting the next collision so
                    # two raced projects cannot both be assigned the same key.
                    db.flush()
                db.query(ProjectMember).filter(
                    func.lower(ProjectMember.username) == target_username
                ).delete(synchronize_session=False)
                db.query(ProjectWorkItem).filter(
                    func.lower(ProjectWorkItem.assignee) == target_username
                ).update(
                    {
                        ProjectWorkItem.assignee: None,
                        ProjectWorkItem.version: ProjectWorkItem.version + 1,
                        ProjectWorkItem.updated_at: utcnow_naive(),
                    },
                    synchronize_session=False,
                )
                db.query(ProjectQuotaLock).filter(
                    ProjectQuotaLock.key == project_owner_quota_lock_key(target_username),
                    ProjectQuotaLock.project_id.is_(None),
                ).delete(synchronize_session=False)
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
        except Exception:
            # Authentication deletion already committed and cannot be undone,
            # but returning success would conceal stranded project ownership.
            logger.exception("Failed to clean project membership for deleted profile %s", target_username)
            raise HTTPException(
                500,
                "Profile was deleted, but project cleanup failed; administrator action is required",
            )
        return {"ok": True}

    # ---- Feature visibility (admin-managed) ----

    @router.get("/features")
    async def get_features():
        """Public: returns which UI features are enabled."""
        return _load_features()

    @router.post("/features")
    async def set_features(request: Request):
        """Admin only: update feature toggles."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        body = await request.json()
        current = _load_features(owner=user)
        for key in current:
            if key in body and isinstance(body[key], bool):
                current[key] = body[key]
        _save_features(current, owner=user)
        return current

    # ---- App settings (admin-managed) ----

    @router.get("/settings")
    async def get_settings(request: Request):
        """Returns app settings. Admins get the full set; non-admins get
        a scrubbed copy with secret keys blanked. The frontend uses this
        for keybinds + TTS prefs, so it stays callable without admin."""
        user = _get_current_user(request)
        settings = _load_settings(owner=user)
        if user and auth_manager.is_admin(user):
            return settings
        return scrub_settings(settings)

    @router.post("/settings")
    async def set_settings(request: Request):
        """Admin only: update app settings."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        body = await request.json()
        current = _load_settings(owner=user)
        # Per-key validation for numeric settings: coerce to int and clamp to a
        # sane range so a bad value can't disable the agent or let it run away.
        _INT_RANGES = {
            "agent_max_rounds": (1, 200),
            "agent_max_tool_calls": (0, 1000),  # 0 = unlimited
        }
        for key in DEFAULT_SETTINGS:
            if key not in body:
                continue
            val = body[key]
            if key in _INT_RANGES:
                lo, hi = _INT_RANGES[key]
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    raise HTTPException(400, f"{key} must be an integer")
                val = max(lo, min(val, hi))
            current[key] = val
        _save_settings(current, owner=user)
        return current

    # ---- Integrations CRUD ----

    # Run migration on startup
    migrate_from_settings()

    @router.get("/integrations")
    async def list_integrations_route(request: Request):
        """List all integrations (admin only, keys masked)."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        items = load_integrations(owner=user)
        # Mask API keys for frontend display
        safe = [mask_integration_secret(item) for item in items]
        return {"integrations": safe}

    @router.get("/integrations/presets")
    async def list_presets():
        """List available integration presets."""
        return {"presets": {k: {kk: vv for kk, vv in v.items() if kk != "api_key"} for k, v in INTEGRATION_PRESETS.items()}}

    @router.post("/integrations")
    async def create_integration(request: Request):
        """Create a new integration (admin only)."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        body = await request.json()
        item = add_integration(body, owner=user)
        return {"ok": True, "integration": mask_integration_secret(item)}

    @router.put("/integrations/{integration_id}")
    async def update_integration_route(integration_id: str, request: Request):
        """Update an existing integration (admin only)."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        body = await request.json()
        item = update_integration(integration_id, body, owner=user)
        if not item:
            raise HTTPException(404, "Integration not found")
        return {"ok": True, "integration": mask_integration_secret(item)}

    @router.delete("/integrations/{integration_id}")
    async def delete_integration_route(integration_id: str, request: Request):
        """Delete an integration (admin only)."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        ok = delete_integration(integration_id, owner=user)
        if not ok:
            raise HTTPException(404, "Integration not found")
        return {"ok": True}

    @router.post("/integrations/{integration_id}/test")
    async def test_integration_route(integration_id: str, request: Request):
        """Test connectivity to an integration (admin only)."""
        user = _get_current_user(request)
        if not user or not auth_manager.is_admin(user):
            raise HTTPException(403, "Admin only")
        integ = get_integration(integration_id, owner=user)
        if not integ:
            raise HTTPException(404, "Integration not found")
        preset = (integ.get("preset") or integ.get("name", "")).lower()

        # ntfy is special: a GET / proves the server is reachable but
        # publishes nothing, so the user has no way to know whether
        # subscribers will actually receive notifications. Instead, do
        # the real thing — POST a one-line "connectivity test" message
        # to the topic the Reminders panel is configured to use. If the
        # subscriber app is wired up correctly, this is what the green
        # checkmark + a phone ping confirms together.
        if preset == "ntfy":
            import httpx
            from urllib.parse import urlparse
            # Strip any path/query the user accidentally pasted in the
            # base URL (e.g. `http://host:8091/odysseus`) — otherwise
            # the topic gets appended after the path and we publish to
            # `/odysseus/odysseus` (which ntfy 404s on). ntfy itself
            # only ever serves from the root.
            raw_base = (integ.get("base_url") or "").strip()
            parsed = urlparse(raw_base)
            base = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else raw_base.rstrip("/")
            settings = _load_settings(owner=user)
            topic = (settings.get("reminder_ntfy_topic") or "reminders").strip() or "reminders"
            full_url = f"{base}/{topic}"
            api_key = integ.get("api_key", "")
            auth_type = (integ.get("auth_type") or "none").lower()
            headers = {
                "Title": "Restia connectivity test",
                "Tags": "white_check_mark",
                "Priority": "default",
            }
            if api_key:
                if auth_type == "bearer":
                    headers["Authorization"] = f"Bearer {api_key}"
                elif auth_type == "header":
                    headers[integ.get("auth_header") or "Authorization"] = api_key
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    r = await client.post(
                        full_url,
                        content="Connectivity test from Restia. If you see this on your phone, ntfy is wired up correctly.",
                        headers=headers,
                    )
                if r.is_success:
                    # Tell the user EXACTLY where it went and what to
                    # subscribe to on their phone, so they can match
                    # without guesswork. The doubled-topic / wrong-host
                    # mistakes are easier to spot when the actual URL
                    # is right there in the success line.
                    return {
                        "ok": True,
                        "message": (
                            f"Sent to {full_url} — on your ntfy app, "
                            f"subscribe to topic \"{topic}\" with server "
                            f"\"{base}\" (or paste the full URL: {full_url})."
                        ),
                    }
                return {"ok": False, "message": f"ntfy returned HTTP {r.status_code} from {full_url}: {r.text[:200]}"}
            except Exception as e:
                hint = ""
                if parsed.hostname not in ("127.0.0.1", "localhost"):
                    hint = " If this is Docker Compose ntfy, set NTFY_BIND to that host/Tailscale IP and NTFY_BASE_URL to the same server URL in .env, then recreate ntfy."
                return {"ok": False, "message": f"ntfy publish to {full_url} failed: {e}.{hint}"[:500]}

        if preset == "discord_webhook":
            import httpx
            webhook_url = (integ.get("base_url") or "").strip()
            if not webhook_url:
                return {"ok": False, "message": "No webhook URL set — paste the full Discord webhook URL into the Base URL field."}
            payload = {
                "embeds": [{
                    "title": "Restia connectivity test",
                    "description": "If you see this, your Discord Webhook integration is wired up correctly.",
                    "color": 5793266,
                }]
            }
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    r = await client.post(webhook_url, json=payload)
                if r.is_success:
                    return {"ok": True, "message": "Test embed sent — check your Discord channel to confirm it arrived."}
                return {"ok": False, "message": f"Discord returned HTTP {r.status_code}: {r.text[:200]}"}
            except Exception as e:
                return {"ok": False, "message": f"Request failed: {e}"[:400]}

        # All other presets: GET against a known health endpoint.
        # Fall back to detecting from name if preset is missing.
        health_paths = {
            "miniflux": "/v1/me",
            "gitea": "/api/v1/version",
            "linkding": "/api/tags/",
            "homeassistant": "/api/",
            "home assistant": "/api/",
        }
        path = health_paths.get(preset, "/")
        result = await execute_api_call(integration_id, "GET", path, owner=user)
        if result.get("exit_code", 1) == 0:
            return {"ok": True, "message": "Connection successful"}
        return {"ok": False, "message": (result.get("error") or "Connection failed")[:300]}

    return router
