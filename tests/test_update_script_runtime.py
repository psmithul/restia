from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PREVIOUS_IMAGE = "sha256:" + ("a" * 64)


def _fake_install(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    install = tmp_path / "install"
    fake_bin = tmp_path / "bin"
    install.mkdir()
    fake_bin.mkdir()
    shutil.copy2(ROOT / "update.sh", install / "update.sh")
    (install / "update.sh").chmod(0o755)

    docker = fake_bin / "docker"
    docker.write_text(
        """#!/bin/sh
set -eu
printf 'docker %s\\n' "$*" >> "$TRACE"
if [ "${1:-}" = "compose" ]; then
  exit 1
fi
if [ "${1:-}" = "inspect" ]; then
  printf '%s\\n' "$PREVIOUS_IMAGE"
  exit 0
fi
if [ "${1:-}" = "image" ] && [ "${2:-}" = "tag" ]; then
  : > "$ROLLBACK_MARK"
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    compose = fake_bin / "docker-compose"
    compose.write_text(
        """#!/bin/sh
set -eu
printf 'compose %s\\n' "$*" >> "$TRACE"
case "${1:-}" in
  ps)
    printf 'restia-container\\n'
    ;;
  cp)
    printf 'verified archive\\n' > "$3"
    ;;
  exec)
    case "$*" in
      *RESTIA_DATABASE_MODE*) printf '%s\\n' "${DB_MODE:-local-single}" ;;
      */api/ready*)
        if [ "${FAIL_UPDATED_READY:-0}" = "1" ] && [ ! -f "$ROLLBACK_MARK" ]; then
          exit 1
        fi
        ;;
      */api/version*) printf '3.0.0\\n' ;;
    esac
    ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    compose.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "TRACE": str(tmp_path / "trace.log"),
            "ROLLBACK_MARK": str(tmp_path / "rollback.marker"),
            "PREVIOUS_IMAGE": PREVIOUS_IMAGE,
            "RESTIA_UPDATE_READINESS_ATTEMPTS": "1",
        }
    )
    return install, env


def _run(install: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", "update.sh", *args],
        cwd=install,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_unix_update_verifies_host_snapshot_and_records_exact_rollback(tmp_path):
    install, env = _fake_install(tmp_path)

    result = _run(install, env)

    assert result.returncode == 0, result.stderr
    state = (install / ".restia-update-state").read_text(encoding="utf-8")
    trace = Path(env["TRACE"]).read_text(encoding="utf-8")
    assert f"previous_image={PREVIOUS_IMAGE}" in state
    assert "target_image=ghcr.io/psmithul/restia:latest" in state
    assert "compose run --rm --no-deps -T --entrypoint python" in trace
    assert "compose pull odysseus" in trace
    assert "compose up -d --no-build odysseus" in trace
    snapshots = list((install / "backups").glob("restia-pre-update-*.tar.gz"))
    assert len(snapshots) == 1
    assert snapshots[0].stat().st_size > 0


def test_unix_failed_readiness_retags_and_recovers_previous_image(tmp_path):
    install, env = _fake_install(tmp_path)
    env["FAIL_UPDATED_READY"] = "1"

    result = _run(install, env)

    assert result.returncode == 1
    trace = Path(env["TRACE"]).read_text(encoding="utf-8")
    assert (
        f"docker image tag {PREVIOUS_IMAGE} ghcr.io/psmithul/restia:latest"
        in trace
    )
    assert Path(env["ROLLBACK_MARK"]).is_file()
    assert "Previous image is healthy again" in result.stderr


def test_unix_shared_update_refuses_unproven_external_backup(tmp_path):
    install, env = _fake_install(tmp_path)
    env["DB_MODE"] = "shared"

    result = _run(install, env)

    assert result.returncode == 1
    assert "Shared mode requires --shared-backup PATH" in result.stderr
    trace = Path(env["TRACE"]).read_text(encoding="utf-8")
    assert "compose pull odysseus" not in trace
