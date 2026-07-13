"""
Authentication module — multi-user password hashing, session tokens, config persistence.
Config stored in data/auth.json. Uses bcrypt directly.
"""

import enum
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List

import bcrypt
import pyotp

logger = logging.getLogger(__name__)


from core.atomic_io import atomic_write_json as _atomic_write_json  # noqa: E402
from core.middleware import INTERNAL_TOOL_USER  # noqa: E402

DEFAULT_PRIVILEGES = {
    "can_use_agent": True,
    "can_use_browser": True,
    "can_use_bash": False,
    "can_use_documents": True,
    "can_use_research": True,
    "can_generate_images": True,
    "can_manage_memory": True,
    "max_messages_per_day": 0,
    "allowed_models": [],
    "allowed_models_restricted": False,
    # Explicit "block every model" sentinel. An empty `allowed_models` list is
    # ambiguous — it's also what gets sent when the admin clicks "[All]" — so
    # we need a dedicated flag to express "this user may use no models at all"
    # distinctly from "this user has no restriction".
    "block_all_models": False,
}

# Admins get everything
ADMIN_PRIVILEGES = {k: (True if isinstance(v, bool) else (0 if isinstance(v, int) else [])) for k, v in DEFAULT_PRIVILEGES.items()}
ADMIN_PRIVILEGES["allowed_models_restricted"] = False
# Admins must never be blocked from using models — the generic dict
# comprehension above flips every boolean default to True, which would be
# backwards for this sentinel.
ADMIN_PRIVILEGES["block_all_models"] = False

# An unavailable credential store must never grant the permissive defaults
# used for ordinary profiles.  This map is returned only as defense in depth:
# locked stores cannot authenticate a profile in the first place, but callers
# that inspect privileges directly still fail closed.
LOCKED_PRIVILEGES = {
    **{
        key: (False if isinstance(value, bool) else (0 if isinstance(value, int) else []))
        for key, value in DEFAULT_PRIVILEGES.items()
    },
    "allowed_models_restricted": True,
    "block_all_models": True,
}

from src.constants import AUTH_FILE, PASSWORD_MIN_LENGTH
DEFAULT_AUTH_PATH = AUTH_FILE
TOKEN_TTL = 60 * 60 * 24 * 7  # 7 days

# Usernames the auth + middleware layer reserve as internal "synthetic owner"
# sentinels; they must never belong to a real account. The most dangerous is
# "internal-tool": `core.middleware.require_admin` treats any request whose
# `current_user == "internal-tool"` as the in-process tool loopback and grants
# admin, and because the cookie auth path sets `current_user` to the raw
# username, an account literally named "internal-tool" would be silently
# treated as an admin by every `require_admin`-gated route. "api" collides with
# the bearer-token owner-attribution sentinel. "demo"/"system" round out the
# synthetic-owner set the rest of the codebase already special-cases (see
# `_SYNTHETIC_OWNERS` in routes/assistant_routes.py and the matching guards in
# src/task_scheduler.py / routes/research_routes.py) — a real account with one
# of those names would be denied an assistant and inconsistently owner-scoped.
# Refuse to create or rename into any of them so the sentinels can't be
# impersonated. (Keep this in sync with that synthetic-owner set.)
RESERVED_USERNAMES = frozenset({INTERNAL_TOOL_USER, "api", "demo", "system"})
_SESSION_KEY_PREFIX = "sha256:"
_BACKUP_KEY_PREFIX = "sha256:"


def normalize_known_username(users: Dict[str, Any], username: str | None) -> Optional[str]:
    """Return a normalized username only when it exists in the auth user map."""
    key = str(username or "").strip().lower()
    if not key or key not in users:
        return None
    return key


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (TypeError, ValueError):
        return False


def _session_key(token: str) -> str:
    """One-way lookup key for a browser session token.

    Cookies keep the high-entropy plaintext token; sessions.json stores only
    this digest, so reading that file is not enough to impersonate a profile.
    """
    return _SESSION_KEY_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hash_backup_code(code: str) -> str:
    return _BACKUP_KEY_PREFIX + hashlib.sha256(code.encode("utf-8")).hexdigest()


def _verify_backup_code(code: str, protected: str) -> bool:
    if protected.startswith(_BACKUP_KEY_PREFIX):
        return hmac.compare_digest(_hash_backup_code(code), protected)
    # Legacy Restia codes were exactly eight hex characters and were migrated
    # to bcrypt. Avoid eight expensive bcrypt checks for every ordinary
    # six-digit TOTP attempt.
    if protected.startswith(("$2a$", "$2b$", "$2y$")) and re.fullmatch(r"[0-9a-fA-F]{8}", code or ""):
        return _verify_password(code, protected)
    return False


class SetAdminResult(enum.Enum):
    """Outcome of AuthManager.set_admin, so callers can map each case to a
    precise response instead of guessing from a bare bool."""
    OK = "ok"
    USER_NOT_FOUND = "user_not_found"
    NOT_AUTHORIZED = "not_authorized"   # requester is not an admin
    LAST_ADMIN = "last_admin"           # would remove the last remaining admin


class AuthManager:
    """Manages multi-user password + session-token auth system."""

    def __init__(self, auth_path: str = DEFAULT_AUTH_PATH):
        self.auth_path = auth_path
        self._sessions_path = os.path.join(os.path.dirname(auth_path), "sessions.json")
        self._config: Dict[str, Any] = {}
        self._auth_load_failed = False
        self._sessions: Dict[str, Dict[str, Any]] = {}  # token -> {username, expiry}
        # Guards mutations of self._sessions and the on-disk sessions.json.
        # Validate/create/revoke run concurrently from the FastAPI threadpool.
        self._sessions_lock = threading.RLock()
        # Guards all mutations of self._config and the on-disk auth.json so
        # concurrent create/delete/rename/privilege operations don't interleave
        # and corrupt the user database.
        self._config_lock = threading.Lock()
        # Guards the first-run setup check-and-write so concurrent requests
        # cannot both observe is_configured==False and both create admin accounts.
        self._setup_lock = threading.Lock()
        self._load()
        self._migrate_single_user()
        self._drop_reserved_loaded_users()
        # A legacy profile whose name now collides with a synthetic identity
        # needs an ownership-aware migration across every store, not a startup
        # deletion or a partial auth-only rename.  Leave the credential file
        # byte-for-byte intact and fail closed until an operator repairs it.
        if not self._auth_load_failed:
            self._load_sessions()
            self._migrate_legacy_admin_role()
            self._migrate_auth_secrets()

    def _load(self):
        try:
            if os.path.exists(self.auth_path):
                with open(self.auth_path, "r", encoding="utf-8") as f:
                    self._config = json.load(f)
                # Normalize all stored usernames to lowercase so they match
                # the .strip().lower() applied at login/verify time. Fixes
                # "Invalid credentials" when auth.json was written with
                # mixed-case keys (e.g. via manual edit or a future migration).
                if "users" in self._config:
                    self._config["users"] = {
                        k.strip().lower(): v
                        for k, v in self._config["users"].items()
                    }
                logger.info("Auth config loaded")
                try:
                    os.chmod(self.auth_path, 0o600)
                except OSError:
                    pass
            else:
                self._config = {}
                logger.info("No auth config found — first-run setup required")
        except Exception as e:
            logger.error(f"Failed to load auth config: {e}")
            self._config = {}
            # An existing-but-unreadable/corrupt auth store must not look like
            # first run. Otherwise /api/auth/setup becomes an account-takeover
            # path precisely when the credential file is damaged.
            self._auth_load_failed = os.path.exists(self.auth_path)

    def _load_sessions(self):
        """Load persisted session tokens from disk, pruning expired ones."""
        if self._auth_load_failed:
            # Do not parse, prune, or migrate a sibling session file while the
            # credential store is quarantined. Every validation path below is
            # disabled, so retaining no in-memory sessions is the safest state.
            self._sessions = {}
            return
        try:
            if os.path.exists(self._sessions_path):
                with open(self._sessions_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                now = time.time()
                self._sessions = {
                    (k if str(k).startswith(_SESSION_KEY_PREFIX) else _session_key(str(k))): v
                    for k, v in data.items()
                    if isinstance(v, dict) and v.get("expiry", 0) > now
                }
                pruned = len(data) - len(self._sessions)
                migrated = any(not str(k).startswith(_SESSION_KEY_PREFIX) for k in data)
                if pruned > 0 or migrated:
                    self._save_sessions()
                else:
                    try:
                        os.chmod(self._sessions_path, 0o600)
                    except OSError:
                        pass
                logger.info(f"Loaded {len(self._sessions)} session(s) from disk")
        except Exception as e:
            logger.error(f"Failed to load sessions: {e}")
            self._sessions = {}

    def _save_sessions(self):
        """Persist session tokens to disk (atomic, lock-guarded)."""
        if self._auth_load_failed:
            logger.error("Refused to modify sessions while auth store is unavailable")
            return
        try:
            with self._sessions_lock:
                # Hold the RLock through the replace. Taking a snapshot and
                # releasing first allowed an older concurrent writer to land
                # last and resurrect a session another thread had revoked.
                _atomic_write_json(self._sessions_path, dict(self._sessions))
        except Exception as e:
            logger.error(f"Failed to save sessions: {e}")

    def _migrate_single_user(self):
        """Migrate old single-user format to multi-user format."""
        if "password_hash" in self._config and "users" not in self._config:
            old_user = str(self._config.get("username", "admin") or "admin").strip().lower()
            if old_user in RESERVED_USERNAMES:
                # Renaming only the credential row would orphan every external
                # store still owned by the old username. Preserve the legacy
                # source verbatim and keep first-run setup closed instead.
                self._auth_load_failed = True
                logger.error(
                    "Legacy single-user auth uses reserved username '%s'; "
                    "auth store was left unchanged and profile migration is required",
                    old_user,
                )
                return
            old_hash = self._config["password_hash"]
            with self._config_lock:
                self._config = {
                    "users": {
                        old_user: {
                            "password_hash": old_hash,
                            "created": time.time(),
                            "is_admin": True,
                        }
                    }
                }
                self._save()
            logger.info(f"Migrated single-user auth to multi-user (admin: {old_user})")

    def _drop_reserved_loaded_users(self):
        """Quarantine legacy/manual auth rows that collide with sentinels.

        Never delete these rows here.  Their username is also the ownership
        key for sessions, uploads, messages, and other profile data, so an
        auth-only deletion both destroys credentials and can make an existing
        installation look like first run.  Mark the auth store unavailable
        instead; this keeps setup closed and preserves the source data for an
        explicit ownership-aware repair.
        """
        users = self._config.get("users")
        if not isinstance(users, dict):
            return
        collisions = sorted({
            str(username or "").strip().lower()
            for username in users
            if str(username or "").strip().lower() in RESERVED_USERNAMES
        })
        if collisions:
            self._auth_load_failed = True
            logger.error(
                "Auth store contains legacy reserved username(s) and was left unchanged; "
                "profile migration is required: %s",
                ", ".join(collisions),
            )

    def _migrate_legacy_admin_role(self):
        """Normalize setup.py's old role='admin' marker to is_admin=True."""
        if self._auth_load_failed:
            return
        changed = False
        for username, user in self.users.items():
            if user.get("role") == "admin" and "is_admin" not in user:
                user["is_admin"] = True
                changed = True
                logger.info(f"Migrated legacy admin role for '{username}'")
        if changed:
            self._save()

    def _migrate_auth_secrets(self) -> None:
        """Encrypt TOTP seeds and one-way hash legacy backup codes in place."""
        if self._auth_load_failed:
            return
        from src.secret_storage import encrypt, is_encrypted

        changed = False
        with self._config_lock:
            for user in self.users.values():
                for field in ("totp_secret", "totp_secret_pending"):
                    value = user.get(field)
                    if isinstance(value, str) and value and not is_encrypted(value):
                        user[field] = encrypt(value)
                        changed = True
                    # Keep encrypted values even when the current app key
                    # cannot decrypt them. Verification still fails closed,
                    # but a temporarily missing/restored key must not destroy
                    # the only copy of a user's TOTP seed and lock them out
                    # permanently.
                codes = user.get("totp_backup_codes")
                if isinstance(codes, list):
                    protected = []
                    for code in codes:
                        value = str(code or "")
                        if not value:
                            continue
                        if value.startswith((_BACKUP_KEY_PREFIX, "$2a$", "$2b$", "$2y$")):
                            protected.append(value)
                        else:
                            protected.append(_hash_password(value))
                            changed = True
                    if protected != codes:
                        user["totp_backup_codes"] = protected
                        changed = True
            if changed:
                self._save()

    def _save(self):
        if self._auth_load_failed:
            # Every public mutation checks before changing _config. Keep this
            # final guard so a future mutation cannot accidentally overwrite
            # the quarantined source file.
            raise RuntimeError("Authentication store is unavailable and read-only")
        _atomic_write_json(self.auth_path, self._config, indent=2)

    @property
    def auth_store_error(self) -> bool:
        """Whether the credential store is quarantined and profile auth is off."""
        return self._auth_load_failed

    @property
    def users(self) -> Dict[str, Any]:
        return self._config.get("users", {})

    @property
    def retired_usernames(self) -> set[str]:
        raw = self._config.get("retired_usernames", [])
        return {str(v).strip().lower() for v in raw if str(v).strip()} if isinstance(raw, list) else set()

    def retire_username(self, username: str) -> None:
        """Permanently reserve a former profile name against data re-binding."""
        if self._auth_load_failed:
            logger.error("Refused to retire username while auth store is unavailable")
            return
        key = str(username or "").strip().lower()
        if not key:
            return
        with self._config_lock:
            # Re-check under the mutation lock: create_user may have activated
            # this name after an earlier lock-free check.
            if key in self.users:
                return
            retired = self.retired_usernames
            if key in retired:
                return
            retired.add(key)
            self._config["retired_usernames"] = sorted(retired)
            self._save()

    @property
    def signup_enabled(self) -> bool:
        if self._auth_load_failed:
            return False
        return self._config.get("signup_enabled", False)

    @signup_enabled.setter
    def signup_enabled(self, value: bool):
        if self._auth_load_failed:
            logger.error("Refused to change signup policy while auth store is unavailable")
            return
        with self._config_lock:
            self._config["signup_enabled"] = value
            self._save()

    @property
    def is_configured(self) -> bool:
        return self._auth_load_failed or len(self.users) > 0

    def policy(self) -> dict:
        """Return public auth policy constants for the frontend."""
        return {
            "password_min_length": PASSWORD_MIN_LENGTH,
            "reserved_usernames": sorted(RESERVED_USERNAMES),
            "signup_enabled": self.signup_enabled,
            "session_days": TOKEN_TTL // 86400,
        }

    # ------------------------------------------------------------------
    # Account management
    # ------------------------------------------------------------------

    def setup(self, username: str, password: str) -> bool:
        """First-run admin setup. Only works if no users exist."""
        with self._setup_lock:
            if self.is_configured:
                return False
            return self.create_user(username, password, is_admin=True)

    def create_user(self, username: str, password: str, is_admin: bool = False) -> bool:
        """Create a new user account."""
        if self._auth_load_failed:
            logger.error("Refused to create profile while auth store is unavailable")
            return False
        username = username.strip().lower()
        if not username:
            return False
        if username in RESERVED_USERNAMES or username in self.retired_usernames:
            logger.warning("Refused to create reserved username '%s'", username)
            return False
        with self._config_lock:
            # The outer check is only a fast path. Both active and retired
            # names must be re-checked while holding the same lock used by
            # delete/rename so a concurrent request cannot recycle identity.
            if (
                username in RESERVED_USERNAMES
                or username in self.retired_usernames
                or username in self.users
            ):
                return False
            if "users" not in self._config:
                self._config["users"] = {}
            self._config["users"][username] = {
                "password_hash": _hash_password(password),
                "created": time.time(),
                "is_admin": is_admin,
                "privileges": dict(ADMIN_PRIVILEGES if is_admin else DEFAULT_PRIVILEGES),
            }
            self._save()
        logger.info(f"Created user '{username}' (admin={is_admin})")
        return True

    def delete_user(self, username: str, requesting_user: str) -> bool:
        """Delete a user. Only admins can delete, and can't delete themselves.

        SECURITY: also revoke every active session token belonging to this
        user so any open browser tab they have gets kicked back to /login
        on the next request. Without this the user kept full access until
        their cookie expired naturally (default ~30 days).
        """
        if self._auth_load_failed:
            logger.error("Refused to delete profile while auth store is unavailable")
            return False
        username = username.strip().lower()
        with self._config_lock:
            if username not in self.users:
                return False
            if username == requesting_user:
                return False
            if not self.users.get(requesting_user, {}).get("is_admin"):
                return False
            # Revoke API bearer tokens before removing the auth row. The bearer
            # path authenticates from ApiToken rows and does not require the
            # owner to still exist, so a successful delete must not leave active
            # rows behind. If the token store is unavailable, fail closed and
            # keep the user/session state intact so the admin can retry.
            try:
                from core.database import get_db_session, ApiToken
                with get_db_session() as db:
                    removed_tokens = db.query(ApiToken).filter(ApiToken.owner == username).delete()
                if removed_tokens:
                    logger.info(
                        f"Revoked {removed_tokens} API token(s) owned by deleted user '{username}'"
                    )
            except Exception:
                logger.warning(f"Failed to revoke API tokens for deleted user '{username}'")
                return False
            del self._config["users"][username]
            retired = self.retired_usernames
            retired.add(username)
            self._config["retired_usernames"] = sorted(retired)
            self._save()
        # Purge all sessions belonging to this user. validate_token doesn't
        # cross-check `self.users`, so without this step a deleted user's
        # cookie keeps authenticating.
        revoked = 0
        with self._sessions_lock:
            to_drop = [tok for tok, sess in self._sessions.items()
                       if (sess or {}).get("username") == username]
            for tok in to_drop:
                self._sessions.pop(tok, None)
                revoked += 1
        if revoked:
            self._save_sessions()
        logger.info(f"Deleted user '{username}' (by {requesting_user}); revoked {revoked} active session(s)")
        return True

    def rename_user(self, old_username: str, new_username: str, requesting_user: str) -> bool:
        """Rename a user in auth config and active sessions. Admin only."""
        if self._auth_load_failed:
            logger.error("Refused to rename profile while auth store is unavailable")
            return False
        old_username = old_username.strip().lower()
        new_username = new_username.strip().lower()
        requesting_user = (requesting_user or "").strip().lower()
        if not old_username or not new_username:
            return False
        if new_username in RESERVED_USERNAMES or new_username in self.retired_usernames:
            logger.warning("Refused to rename '%s' into reserved username '%s'", old_username, new_username)
            return False
        with self._config_lock:
            if old_username not in self.users:
                return False
            if (
                new_username in RESERVED_USERNAMES
                or new_username in self.retired_usernames
                or new_username in self.users
            ):
                return False
            if not self.users.get(requesting_user, {}).get("is_admin"):
                return False
            self._config.setdefault("users", {})[new_username] = self._config["users"].pop(old_username)
            # Reserve the former ownership key in the same durable write as
            # the auth rename. Otherwise signup can recreate it while the
            # route is still migrating SQL/file-backed owner references.
            retired = self.retired_usernames
            retired.add(old_username)
            self._config["retired_usernames"] = sorted(retired)
            self._save()

        renamed_sessions = 0
        with self._sessions_lock:
            for sess in self._sessions.values():
                sess_user = str((sess or {}).get("username") or "").strip().lower()
                if sess_user == old_username:
                    sess["username"] = new_username
                    renamed_sessions += 1
        if renamed_sessions:
            self._save_sessions()
        logger.info(
            "Renamed user '%s' -> '%s' (by %s); updated %d active session(s)",
            old_username, new_username, requesting_user, renamed_sessions,
        )
        return True

    def rollback_user_rename(
        self,
        current_username: str,
        former_username: str,
        requesting_user: str,
    ) -> bool:
        """Atomically reverse a rename whose owner-data migration failed.

        This is deliberately separate from ``rename_user``: a normal rename
        must never target a retired ownership key, while rollback may restore
        exactly the key reserved by the in-flight rename.
        """
        if self._auth_load_failed:
            logger.error("Refused to roll back profile rename while auth store is unavailable")
            return False
        current_username = (current_username or "").strip().lower()
        former_username = (former_username or "").strip().lower()
        requesting_user = (requesting_user or "").strip().lower()
        if not current_username or not former_username:
            return False
        with self._config_lock:
            users = self._config.get("users", {})
            if current_username not in users or former_username in users:
                return False
            retired = self.retired_usernames
            if former_username not in retired:
                return False
            if not users.get(requesting_user, {}).get("is_admin"):
                return False
            users[former_username] = users.pop(current_username)
            retired.discard(former_username)
            self._config["retired_usernames"] = sorted(retired)
            self._save()

        renamed_sessions = 0
        with self._sessions_lock:
            for sess in self._sessions.values():
                sess_user = str((sess or {}).get("username") or "").strip().lower()
                if sess_user == current_username:
                    sess["username"] = former_username
                    renamed_sessions += 1
        if renamed_sessions:
            self._save_sessions()
        logger.info(
            "Rolled back user rename '%s' -> '%s' (by %s); updated %d active session(s)",
            current_username,
            former_username,
            requesting_user,
            renamed_sessions,
        )
        return True

    def is_admin(self, username: str) -> bool:
        if self._auth_load_failed:
            return False
        return self.users.get(username, {}).get("is_admin", False)

    def list_users(self) -> List[Dict[str, Any]]:
        if self._auth_load_failed:
            return []
        return [
            {"username": u, "is_admin": d.get("is_admin", False), "privileges": self.get_privileges(u)}
            for u, d in self.users.items()
        ]

    def get_privileges(self, username: str) -> Dict[str, Any]:
        """Get privileges for a user. Admins get all privileges."""
        if self._auth_load_failed:
            return {
                key: (list(value) if isinstance(value, list) else value)
                for key, value in LOCKED_PRIVILEGES.items()
            }
        user = self.users.get(username, {})
        if user.get("is_admin"):
            return dict(ADMIN_PRIVILEGES)
        # Merge stored privileges with defaults (in case new privileges were added)
        stored = user.get("privileges", {})
        return {**DEFAULT_PRIVILEGES, **stored}

    def set_privileges(self, username: str, privileges: Dict[str, Any]) -> bool:
        """Update privileges for a user. Can't modify admin privileges."""
        if self._auth_load_failed:
            logger.error("Refused to change privileges while auth store is unavailable")
            return False
        username = username.strip().lower()
        with self._config_lock:
            if username not in self.users:
                return False
            if self.users[username].get("is_admin"):
                return False  # admins always have full access
            # Only allow known privilege keys
            current = self.get_privileges(username)
            for k, v in privileges.items():
                if k in DEFAULT_PRIVILEGES:
                    current[k] = v
            self._config["users"][username]["privileges"] = current
            self._save()
        logger.info(f"Updated privileges for '{username}': {current}")
        return True

    def set_admin(self, username: str, is_admin: bool,
                  requesting_user: str) -> SetAdminResult:
        """Promote/demote an existing user to/from admin. Admin only.

        Refuses to remove the last remaining admin so the instance can never
        be locked out of admin access; self-demotion is allowed as long as
        another admin remains. Admin status is re-checked live on every
        request, so unlike delete/rename no session or token revocation is
        needed — a demoted admin simply fails the next is_admin() gate.

        Promotion stashes the user's current privilege map and demotion
        restores it, so a temporary admin stint can't silently broaden a
        user's non-admin access; users without a stash (created as admin,
        or promoted before stashing existed) demote to DEFAULT_PRIVILEGES.

        Counting admins and flipping the flag happen in one critical section
        so two concurrent demotions can't race the admin count to zero.
        """
        if self._auth_load_failed:
            logger.error("Refused to change admin role while auth store is unavailable")
            return SetAdminResult.NOT_AUTHORIZED
        username = (username or "").strip().lower()
        requesting_user = (requesting_user or "").strip().lower()
        is_admin = bool(is_admin)
        with self._config_lock:
            target = self._config.get("users", {}).get(username)
            if target is None:
                return SetAdminResult.USER_NOT_FOUND
            if not self.users.get(requesting_user, {}).get("is_admin"):
                return SetAdminResult.NOT_AUTHORIZED
            currently_admin = bool(target.get("is_admin"))
            if currently_admin == is_admin:
                return SetAdminResult.OK  # no-op; leave privileges untouched
            if currently_admin and not is_admin:
                admin_count = sum(1 for d in self.users.values() if d.get("is_admin"))
                if admin_count <= 1:
                    return SetAdminResult.LAST_ADMIN
            # Write order matters for lock-free readers: get_privileges()
            # reads without _config_lock and trusts is_admin, so the admin
            # flag must be flipped while the stored map is safe to expose —
            # before writing admin privileges on promote, after restoring
            # the pre-admin map on demote.
            if is_admin:
                target["is_admin"] = True
                # Stash the pre-admin map so a later demotion can restore it.
                # While is_admin is set the stored map is inert: get_privileges
                # short-circuits to ADMIN_PRIVILEGES and set_privileges refuses
                # admins, so only set_admin ever touches the stash.
                target["privileges_before_admin"] = dict(
                    target.get("privileges") or DEFAULT_PRIVILEGES
                )
                target["privileges"] = dict(ADMIN_PRIVILEGES)
            else:
                # Restore the stashed pre-admin map. Fall back to defaults for
                # users created as admins (their stored map is ADMIN_PRIVILEGES,
                # which must not leak past demotion — e.g. can_use_bash) and
                # for admins promoted before the stash existed.
                target["privileges"] = dict(
                    target.pop("privileges_before_admin", None)
                    or DEFAULT_PRIVILEGES
                )
                target["is_admin"] = False
            self._save()
        logger.info("Set is_admin=%s for '%s' (by '%s')", is_admin, username, requesting_user)
        return SetAdminResult.OK

    def change_password(self, username: str, current_password: str, new_password: str) -> bool:
        if self._auth_load_failed:
            logger.error("Refused to change password while auth store is unavailable")
            return False
        username = username.strip().lower()
        if username not in self.users:
            return False
        if not _verify_password(current_password, self.users[username]["password_hash"]):
            return False
        with self._config_lock:
            self._config["users"][username]["password_hash"] = _hash_password(new_password)
            self._save()
        return True

    # ------------------------------------------------------------------
    # TOTP two-factor authentication
    # ------------------------------------------------------------------

    def totp_enabled(self, username: str) -> bool:
        """Check if 2FA is enabled for a user."""
        if self._auth_load_failed:
            return False
        user = self.users.get(username.strip().lower(), {})
        return bool(user.get("totp_enabled"))

    def totp_generate_secret(self, username: str) -> Optional[str]:
        """Generate a new TOTP secret for a user. Returns the secret (not yet enabled)."""
        if self._auth_load_failed:
            logger.error("Refused to set up 2FA while auth store is unavailable")
            return None
        username = username.strip().lower()
        if username not in self.users:
            return None
        secret = pyotp.random_base32()
        from src.secret_storage import encrypt
        with self._config_lock:
            self._config["users"][username]["totp_secret_pending"] = encrypt(secret)
            self._save()
        return secret

    def totp_get_provisioning_uri(self, username: str, secret: str) -> str:
        """Get the otpauth:// URI for QR code generation."""
        totp = pyotp.TOTP(secret)
        return totp.provisioning_uri(name=username, issuer_name="Restia")

    def totp_confirm_enable(self, username: str, code: str) -> Optional[List[str]]:
        """Enable 2FA and return one-time plaintext backup codes."""
        if self._auth_load_failed:
            logger.error("Refused to enable 2FA while auth store is unavailable")
            return None
        username = username.strip().lower()
        user = self.users.get(username, {})
        from src.secret_storage import decrypt, encrypt
        secret = decrypt(user.get("totp_secret_pending") or "")
        if not secret:
            return None
        totp = pyotp.TOTP(secret)
        if not totp.verify(code, valid_window=1):
            return None
        # Enable 2FA
        with self._config_lock:
            self._config["users"][username]["totp_secret"] = encrypt(secret)
            self._config["users"][username]["totp_enabled"] = True
            self._config["users"][username].pop("totp_secret_pending", None)
            # Generate backup codes
            backup = [secrets.token_urlsafe(9) for _ in range(8)]
            self._config["users"][username]["totp_backup_codes"] = [
                _hash_backup_code(value) for value in backup
            ]
            self._save()
        logger.info(f"2FA enabled for '{username}'")
        return backup

    def totp_verify(self, username: str, code: str) -> bool:
        """Verify a TOTP code for login."""
        if self._auth_load_failed:
            return False
        username = username.strip().lower()
        user = self.users.get(username, {})
        if not user.get("totp_enabled"):
            return True  # 2FA not enabled, always pass
        from src.secret_storage import decrypt
        secret = decrypt(user.get("totp_secret") or "")
        if not secret:
            # 2FA is enabled but no secret is stored (corrupt/partially-written
            # auth.json). Fail closed — returning True here bypassed the second
            # factor entirely.
            return False
        totp = pyotp.TOTP(secret)
        if totp.verify(code, valid_window=1):
            return True

        # Verification and removal share one critical section. Reading a list
        # before acquiring the lock let two concurrent logins both accept the
        # same code and then pop different entries by stale index.
        with self._config_lock:
            current_user = self._config.get("users", {}).get(username, {})
            backup = list(current_user.get("totp_backup_codes") or [])
            used_index = next(
                (idx for idx, value in enumerate(backup) if _verify_backup_code(code, value)),
                None,
            )
            if used_index is None:
                return False
            backup.pop(used_index)
            current_user["totp_backup_codes"] = backup
            self._save()
            remaining = len(backup)
        logger.info(f"Backup code used for '{username}' ({remaining} remaining)")
        return True

    def totp_disable(self, username: str, password: str) -> bool:
        """Disable 2FA for a user. Requires password confirmation."""
        if self._auth_load_failed:
            logger.error("Refused to disable 2FA while auth store is unavailable")
            return False
        username = username.strip().lower()
        if not self.verify_password(username, password):
            return False
        with self._config_lock:
            self._config["users"][username].pop("totp_secret", None)
            self._config["users"][username].pop("totp_secret_pending", None)
            self._config["users"][username].pop("totp_backup_codes", None)
            self._config["users"][username]["totp_enabled"] = False
            self._save()
        logger.info(f"2FA disabled for '{username}'")
        return True

    # ------------------------------------------------------------------
    # Login / logout / session tokens
    # ------------------------------------------------------------------

    def verify_password(self, username: str, password: str) -> bool:
        if self._auth_load_failed:
            return False
        username = username.strip().lower()
        if username not in self.users:
            return False
        return _verify_password(password, self.users[username]["password_hash"])

    def create_session(self, username: str, password: str) -> Optional[str]:
        """Verify credentials and return a session token, or None."""
        username = username.strip().lower()
        if not self.verify_password(username, password):
            return None
        return self.create_session_trusted(username)

    def create_session_trusted(self, username: str) -> Optional[str]:
        """Issue a session token for an already-verified user.
        Call only after verify_password (and TOTP if enabled) have passed."""
        if self._auth_load_failed:
            logger.warning("Refused to issue session while auth store is unavailable")
            return None
        username = username.strip().lower()
        token = secrets.token_hex(32)
        with self._config_lock:
            if username not in self.users:
                logger.warning("Refused to issue session for missing user '%s'", username)
                return None
            with self._sessions_lock:
                self._sessions[_session_key(token)] = {
                    "username": username,
                    "expiry": time.time() + TOKEN_TTL,
                }
        self._save_sessions()
        return token

    def validate_token(self, token: Optional[str]) -> bool:
        if self._auth_load_failed or not token:
            return False
        expired = False
        deleted_user = False
        with self._sessions_lock:
            key = _session_key(token)
            session = self._sessions.get(key)
            if session is None and token in self._sessions:  # in-memory legacy/test fixture
                key = token
                session = self._sessions.get(key)
            if session is None:
                return False
            if time.time() > session["expiry"]:
                self._sessions.pop(key, None)
                expired = True
            else:
                # SECURITY: if the user record has since been removed (admin
                # deleted them while their cookie was still valid), drop the
                # session so the next request kicks them out instead of
                # silently authenticating against a non-existent account.
                if session.get("username") not in self.users:
                    self._sessions.pop(key, None)
                    deleted_user = True
        if expired or deleted_user:
            self._save_sessions()
            return False
        return True

    def get_username_for_token(self, token: Optional[str]) -> Optional[str]:
        """Return the username associated with a valid token."""
        if self._auth_load_failed or not token:
            return None
        expired = False
        deleted_user = False
        with self._sessions_lock:
            key = _session_key(token)
            session = self._sessions.get(key)
            if session is None and token in self._sessions:
                key = token
                session = self._sessions.get(key)
            if session is None:
                return None
            if time.time() > session["expiry"]:
                self._sessions.pop(key, None)
                expired = True
            else:
                _u = session["username"]
                # SECURITY: orphan check — same rationale as validate_token.
                if _u not in self.users:
                    self._sessions.pop(key, None)
                    deleted_user = True
                else:
                    return _u
        if expired or deleted_user:
            self._save_sessions()
        return None

    def revoke_token(self, token: str):
        if self._auth_load_failed:
            return
        with self._sessions_lock:
            self._sessions.pop(_session_key(token), None)
            self._sessions.pop(token, None)  # in-memory legacy/test fixture
        self._save_sessions()

    def revoke_user_sessions(self, username: str, except_token: Optional[str] = None) -> int:
        """Revoke active browser sessions for a user, optionally preserving one."""
        if self._auth_load_failed:
            return 0
        username = username.strip().lower()
        keep_key = _session_key(except_token) if except_token else None
        revoked = 0
        with self._sessions_lock:
            to_drop = [
                token for token, session in self._sessions.items()
                if token not in {except_token, keep_key} and (session or {}).get("username") == username
            ]
            for token in to_drop:
                self._sessions.pop(token, None)
                revoked += 1
            if revoked:
                self._save_sessions()
        return revoked

    def status(self, token: Optional[str]) -> Dict[str, Any]:
        username = self.get_username_for_token(token)
        authenticated = username is not None
        result = {
            "configured": self.is_configured,
            "authenticated": authenticated,
            "username": username,
            "is_admin": self.is_admin(username) if username else False,
            "auth_store_error": self._auth_load_failed,
        }
        if authenticated:
            result["privileges"] = self.get_privileges(username)
        return result
