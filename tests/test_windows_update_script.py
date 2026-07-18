from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_update_script_uses_safe_docker_update_flow():
    batch = (ROOT / "update_windows.bat").read_text(encoding="utf-8")
    powershell = (ROOT / "update_windows.ps1").read_text(encoding="utf-8")
    lowered = (batch + "\n" + powershell).lower()

    assert 'pushd "%~dp0"' in lowered
    assert "where docker" in lowered
    assert "docker compose version" in lowered
    assert "ghcr.io/psmithul/restia:latest" in lowered
    assert "invoke-compose pull odysseus" in lowered
    assert "invoke-compose up -d --no-build odysseus" in lowered
    assert "/api/ready" in lowered
    assert "previous_image" in lowered
    assert "target_image" in lowered
    assert "docker image tag" in lowered
    assert "restia-pre-update-" in lowered
    assert "--restore-data" in lowered
    assert "--entrypoint python" in lowered
    assert "move-item" in lowered
    assert "git pull" not in lowered
    assert "docker image prune" not in lowered
    assert "pause" in lowered
    assert "exit /b %exit_code%" in lowered
