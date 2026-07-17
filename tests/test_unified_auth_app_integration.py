from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_fresh_app_uses_one_database_authority_across_cookie_api_and_restart(
    tmp_path,
):
    data_dir = tmp_path / "data"
    db_path = data_dir / "app.db"
    env = os.environ.copy()
    env.update({
        "RESTIA_DATA_DIR": str(data_dir),
        "DATABASE_URL": f"sqlite:///{db_path}",
        "RESTIA_DATABASE_MODE": "local-single",
        "AUTH_ENABLED": "true",
        "LOCALHOST_BYPASS": "false",
        "SECURE_COOKIES": "false",
        "RESTIA_STARTUP_WARMUPS": "0",
        "RESTIA_MODEL_KEEPALIVE": "0",
        "RESTIA_INPROCESS_TASKS": "0",
        "RESTIA_INPROCESS_POLLERS": "0",
        "RESTIA_INPROCESS_TELEGRAM": "0",
        "FASTEMBED_CACHE_PATH": str(tmp_path / "fastembed"),
        "MNEMOSYNE_DATA_DIR": str(tmp_path / "mnemosyne"),
    })
    code = r'''
from pathlib import Path
from fastapi.testclient import TestClient

import app as app_module
from core.database import SessionLocal
from src.database_auth_manager import DatabaseAuthManager

client = TestClient(app_module.app)
setup = client.post(
    "/api/auth/setup",
    json={"username": "alice", "password": "correct horse battery staple"},
)
assert setup.status_code == 200, setup.text
login = client.post(
    "/api/auth/login",
    json={
        "username": "alice",
        "password": "correct horse battery staple",
        "remember": True,
    },
)
assert login.status_code == 200, login.text
cookie = client.cookies.get("odysseus_session")
assert cookie and cookie.startswith("rst_s_")

issued = client.post(
    "/api/tokens",
    data={"name": "integration", "scopes": "chat,todos:read"},
)
assert issued.status_code == 200, issued.text
api_token = issued.json()["token"]
assert api_token.startswith("ody_")

bearer = TestClient(app_module.app).get(
    "/api/inbox",
    headers={"Authorization": f"Bearer {api_token}"},
)
assert bearer.status_code == 200, bearer.text
assert bearer.json()["items"] == []

restarted = DatabaseAuthManager(SessionLocal)
cookie_principal = restarted.resolve_session(cookie)
api_principal = restarted.resolve_api_token(api_token)
assert cookie_principal.account_id == api_principal.account_id
assert cookie_principal.username == api_principal.username == "alice"
assert not (Path(__import__("os").environ["RESTIA_DATA_DIR"]) / "auth.json").exists()
print("unified-auth-ready")
'''
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("unified-auth-ready")
