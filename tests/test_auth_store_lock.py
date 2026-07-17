"""Fail-closed contract for quarantined legacy/corrupt auth stores."""

import json
import time
from pathlib import Path

from core.auth import AuthManager, SetAdminResult, _hash_password


def _locked_manager(tmp_path):
    auth_path = tmp_path / "auth.json"
    sessions_path = tmp_path / "sessions.json"
    config = {
        "users": {
            "admin": {
                "password_hash": _hash_password("admin-password"),
                "is_admin": True,
            },
            "alice": {
                "password_hash": _hash_password("alice-password"),
                "is_admin": False,
                "totp_enabled": False,
                "totp_secret_pending": "legacy-plaintext-secret",
            },
            # A manually-created legacy sentinel locks the entire store. The
            # row must remain intact for an ownership-aware repair.
            "internal-tool": {
                "password_hash": _hash_password("tool-password"),
                "is_admin": True,
            },
        },
        "signup_enabled": True,
    }
    auth_raw = json.dumps(config, indent=2)
    sessions_raw = json.dumps(
        {
            "admin-session": {
                "username": "admin",
                "expiry": time.time() + 3600,
            },
            "tool-session": {
                "username": "internal-tool",
                "expiry": time.time() + 3600,
            },
        },
        indent=2,
    )
    auth_path.write_text(auth_raw, encoding="utf-8")
    sessions_path.write_text(sessions_raw, encoding="utf-8")
    return AuthManager(str(auth_path)), auth_path, sessions_path, auth_raw, sessions_raw


def test_locked_store_rejects_every_profile_auth_and_mutation_path(tmp_path):
    manager, auth_path, sessions_path, auth_raw, sessions_raw = _locked_manager(tmp_path)

    # It remains configured so first-run setup cannot replace the preserved
    # credential source, but no row is a usable real profile while locked.
    assert manager.auth_store_error is True
    assert manager.is_configured is True
    assert manager.status("admin-session") == {
        "configured": True,
        "authenticated": False,
        "username": None,
        "is_admin": False,
        "auth_store_error": True,
    }
    assert manager.verify_password("admin", "admin-password") is False
    assert manager.create_session("admin", "admin-password") is None
    assert manager.create_session_trusted("admin") is None
    assert manager.validate_token("admin-session") is False
    assert manager.get_username_for_token("admin-session") is None
    assert manager.is_admin("admin") is False
    assert manager.list_users() == []
    assert manager.signup_enabled is False
    assert manager.policy()["signup_enabled"] is False
    privileges = manager.get_privileges("admin")
    assert privileges["block_all_models"] is True
    assert privileges["allowed_models_restricted"] is True
    assert all(not privileges[key] for key in privileges if key.startswith("can_"))

    # Account, policy, role, password, 2FA, and session mutations all refuse
    # to act. In particular, backup/TOTP verification may not return the
    # ordinary "2FA disabled" success value for a quarantined profile.
    assert manager.setup("attacker", "attacker-password") is False
    assert manager.create_user("mallory", "mallory-password") is False
    assert manager.delete_user("alice", "admin") is False
    assert manager.rename_user("alice", "alice2", "admin") is False
    assert manager.rollback_user_rename("alice", "alice2", "admin") is False
    assert manager.set_privileges("alice", {"can_use_bash": True}) is False
    assert manager.set_admin("alice", True, "admin") is SetAdminResult.NOT_AUTHORIZED
    assert manager.change_password("alice", "alice-password", "new-password") is False
    assert manager.totp_enabled("alice") is False
    assert manager.totp_generate_secret("alice") is None
    assert manager.totp_confirm_enable(
        "alice", "123456", "alice-password"
    ) is None
    assert manager.totp_verify("alice", "123456") is False
    assert manager.totp_disable("alice", "alice-password") is False
    assert manager.revoke_user_sessions("admin") == 0
    manager.revoke_token("admin-session")
    manager.retire_username("new-retired-name")
    manager.signup_enabled = False

    # Loading and exercising the manager must preserve both files byte for
    # byte. Session migration/pruning is intentionally skipped while locked.
    assert auth_path.read_text(encoding="utf-8") == auth_raw
    assert sessions_path.read_text(encoding="utf-8") == sessions_raw
    assert set(manager.users) == {"admin", "alice", "internal-tool"}
    assert "new-retired-name" not in manager.retired_usernames


def test_app_lock_gate_precedes_localhost_and_bearer_bypasses():
    """Pin production middleware ordering without importing the full app."""
    source = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
    middleware_start = source.index("class AuthMiddleware")
    internal_start = source.index("# In-process internal-tool token bypass", middleware_start)
    lock_start = source.index("if auth_manager.auth_store_error:", internal_start)
    localhost_start = source.index("if LOCALHOST_BYPASS and _is_trusted_loopback", lock_start)
    bearer_start = source.index("# --- Bearer token auth", localhost_start)

    assert internal_start < lock_start < localhost_start < bearer_start
    assert 'not getattr(_auth_mgr, "auth_store_error", False)' in source[
        internal_start:lock_start
    ]
    assert 'request.state.current_user = INTERNAL_TOOL_USER' in source[
        internal_start:lock_start
    ]
    assert '"auth_store_error": True' in source[lock_start:localhost_start]

    assert "def _refresh_token_cache" not in source
    assert "_token_cache.get(" not in source
    bearer_end = source.index("# --- Cookie-based session auth", bearer_start)
    assert "auth_manager.resolve_api_token" in source[bearer_start:bearer_end]
