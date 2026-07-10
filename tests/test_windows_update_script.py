from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_update_script_uses_safe_docker_update_flow():
    script = (ROOT / "update_windows.bat").read_text(encoding="utf-8")
    lowered = script.lower()

    assert 'pushd "%~dp0"' in lowered
    assert "where docker" in lowered
    assert "docker compose version" in lowered
    assert "ghcr.io/psmithul/restia:latest" in lowered
    assert "docker compose pull odysseus" in lowered
    assert "docker compose up -d --no-build odysseus" in lowered
    assert "git pull" not in lowered
    assert "docker image prune -f" in lowered
    assert "pause" in lowered
