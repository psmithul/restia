"""Password-recovery security and UI regression tests."""

import asyncio
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from routes.auth_routes import RecoverPasswordRequest, setup_auth_routes
from src.password_recovery import ensure_recovery_key, use_recovery_key


def _endpoint(auth_manager):
    router = setup_auth_routes(
        auth_manager,
        identity_renamer=lambda *_args, **_kwargs: None,
    )
    for route in router.routes:
        if getattr(route, "path", None) == "/api/auth/recover-password":
            return route.endpoint
    raise AssertionError("recover-password route not found")


def _request():
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))


def test_recovery_key_is_owner_only_and_stable(tmp_path):
    path = tmp_path / ".password_recovery_key"

    ensure_recovery_key(path)
    first = path.read_text(encoding="utf-8").strip()
    ensure_recovery_key(path)

    assert path.read_text(encoding="utf-8").strip() == first
    assert len(first) >= 20
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_recovery_key_is_one_time_after_success(tmp_path):
    path = tmp_path / ".password_recovery_key"
    ensure_recovery_key(path)
    first = path.read_text(encoding="utf-8").strip()
    operation = MagicMock(return_value=True)

    assert use_recovery_key(first, operation, path=path) is True
    second = path.read_text(encoding="utf-8").strip()

    assert second != first
    assert use_recovery_key(first, operation, path=path) is False
    assert operation.call_count == 1


def test_wrong_recovery_key_never_runs_password_reset(tmp_path):
    path = tmp_path / ".password_recovery_key"
    ensure_recovery_key(path)
    operation = MagicMock(return_value=True)

    assert use_recovery_key("not-the-recovery-key", operation, path=path) is False
    operation.assert_not_called()


def test_recovery_route_resets_password_and_clears_cookie(monkeypatch, tmp_path):
    auth = MagicMock()
    auth.auth_store_error = False
    auth.recover_password.return_value = True
    endpoint = _endpoint(auth)
    response = MagicMock()
    recovery_path = tmp_path / ".password_recovery_key"
    ensure_recovery_key(recovery_path)
    key = recovery_path.read_text(encoding="utf-8").strip()
    monkeypatch.setattr("src.password_recovery.RECOVERY_KEY_PATH", recovery_path)

    result = asyncio.run(endpoint(
        body=RecoverPasswordRequest(
            username="Alice",
            recovery_key=key,
            new_password="new-secure-password",
        ),
        request=_request(),
        response=response,
    ))

    assert result["ok"] is True
    auth.recover_password.assert_called_once_with("Alice", "new-secure-password")
    response.delete_cookie.assert_called_once_with("odysseus_session", path="/")
    assert recovery_path.read_text(encoding="utf-8").strip() != key


def test_recovery_route_uses_generic_error_for_bad_proof(monkeypatch, tmp_path):
    auth = MagicMock()
    endpoint = _endpoint(auth)
    recovery_path = tmp_path / ".password_recovery_key"
    ensure_recovery_key(recovery_path)
    monkeypatch.setattr("src.password_recovery.RECOVERY_KEY_PATH", recovery_path)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(endpoint(
            body=RecoverPasswordRequest(
                username="missing-user",
                recovery_key="x" * 24,
                new_password="new-secure-password",
            ),
            request=_request(),
            response=MagicMock(),
        ))

    assert exc.value.status_code == 400
    assert exc.value.detail == "Profile or recovery key is incorrect"
    auth.recover_password.assert_not_called()


def test_login_page_exposes_complete_recovery_flow():
    login_html = (Path(__file__).parents[1] / "static" / "login.html").read_text(
        encoding="utf-8"
    )

    assert "Forgot password?" in login_html
    assert 'id="recoveryKey"' in login_html
    assert "/api/auth/recover-password" in login_html
    assert "python3 -m src.password_recovery show" in login_html


def test_recovery_route_is_available_while_auth_store_is_locked():
    app_source = (Path(__file__).parents[1] / "app.py").read_text(encoding="utf-8")
    exempt_block = app_source.split("AUTH_EXEMPT_EXACT = {", 1)[1].split("}", 1)[0]

    assert '"/api/auth/recover-password"' in exempt_block
    assert '"/api/auth/policy"' in exempt_block
