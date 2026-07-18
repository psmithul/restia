from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_unix_updater_snapshots_checks_readiness_and_retains_exact_rollback():
    updater = _read("update.sh")

    assert "restia-pre-update-" in updater
    assert "odysseus-backup" in updater
    assert "verify \"$container_backup\"" in updater
    assert "/api/ready" in updater
    assert "previous_image" in updater
    assert "target_image" in updater
    assert 'docker image tag "$previous_image" "$target_image"' in updater
    assert "--restore-data" in updater
    assert "shared-backup" in updater
    assert "--entrypoint python" in updater
    assert "./scripts/odysseus-backup verify" not in updater
    assert "docker image prune" not in updater
    assert "rm -rf" not in updater

    syntax = subprocess.run(
        ["sh", "-n", str(ROOT / "update.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr


def test_compose_passes_every_independent_runtime_worker_and_shared_setting():
    required = (
        "RESTIA_DATABASE_MODE",
        "RESTIA_ENCRYPTION_KEY_FILE",
        "RESTIA_BLOB_STORE",
        "RESTIA_BLOB_ROOT",
        "RESTIA_SUPABASE_PROJECT_URL",
        "RESTIA_INPROCESS_POLLERS",
        "RESTIA_INPROCESS_TASKS",
        "RESTIA_INPROCESS_TELEGRAM",
        "RESTIA_INPROCESS_CONTACT_DELIVERY",
        "RESTIA_INPROCESS_CALENDAR_DELIVERY",
        "CARDDAV_BLOCK_PRIVATE_IPS",
        "CARDDAV_ALLOW_PUBLIC_HTTP",
    )
    for relative in (
        "docker-compose.yml",
        "docker-compose.gpu-amd.yml",
        "docker-compose.gpu-nvidia.yml",
    ):
        compose = _read(relative)
        for variable in required:
            assert f"- {variable}=" in compose, f"{relative} omits {variable}"
        assert ":/app/blobs" in compose
        assert (
            "RESTIA_INPROCESS_POLLERS="
            "${RESTIA_INPROCESS_POLLERS:-${ODYSSEUS_INPROCESS_POLLERS:-1}}"
        ) in compose
        assert (
            "RESTIA_INPROCESS_TASKS="
            "${RESTIA_INPROCESS_TASKS:-${ODYSSEUS_INPROCESS_TASKS:-1}}"
        ) in compose


def test_blob_mount_is_created_and_repaired_before_nonroot_start():
    dockerfile = _read("Dockerfile")
    entrypoint = _read("docker/entrypoint.sh")

    assert "mkdir -p data logs blobs" in dockerfile
    assert "/app/blobs" in entrypoint
    assert "for dir in /app/data /app/logs /app/blobs" in entrypoint


def test_shared_postgres_overlay_is_fail_closed_and_has_no_client_bypass():
    overlay = _read("docker/shared-postgres.yml")
    dockerignore = _read(".dockerignore")

    assert "postgresql+psycopg://" in overlay
    assert "RESTIA_DATABASE_MODE: shared" in overlay
    assert 'AUTH_ENABLED: "true"' in overlay
    assert 'LOCALHOST_BYPASS: "false"' in overlay
    assert "RESTIA_ENCRYPTION_KEY_FILE: /run/secrets/restia_fernet_key" in overlay
    assert "RESTIA_BLOB_STORE: shared-filesystem" in overlay
    assert "RESTIA_BLOB_ROOT: /app/blobs" in overlay
    assert "condition: service_healthy" in overlay
    assert "service_role" not in overlay.lower()
    assert "/secrets/" in dockerignore
    assert "/.restia-update-state" in dockerignore
    assert "/.restia-update-state.json" in dockerignore


def test_publish_workflow_gates_postgres_exact_platforms_and_native_smokes():
    workflow = _read(".github/workflows/docker-publish.yml")

    assert "postgres-gate:" in workflow
    # Alembic imports core.database through migrations/env.py. The core package
    # currently registers auth/middleware compatibility exports at import time,
    # so the deliberately-small migration environment must still install these
    # direct runtime imports instead of passing locally and failing on CI.
    for dependency in ("fastapi", "httpx", "bcrypt", "pyotp"):
        assert dependency in workflow
    assert "shared-gate --migration-smoke --pretty" in workflow
    assert "needs: [preflight, postgres-gate]" in workflow
    assert "platform: linux/amd64" in workflow
    assert "platform: linux/arm64" in workflow
    assert '["amd64", "arm64"]' in workflow
    assert "smoke:" in workflow
    assert "ubuntu-24.04-arm" in workflow
    assert "scripts/smoke-release-container.sh" in workflow
    assert "scripts/smoke-shared-postgres-compose.sh" in workflow
    assert "BUILD_CHANNEL=${{ needs.preflight.outputs.build_channel }}" in workflow
    assert "candidate-${GITHUB_SHA}-${BUILD_CHANNEL}" in workflow
    assert "needs: [preflight, merge, smoke, shared_smoke]" in workflow
    assert 'SOURCE_IMAGE: ${{ needs.merge.outputs.image }}' in workflow


def test_release_container_smoke_checks_identity_schema_shell_and_permissions():
    smoke = _read("scripts/smoke-release-container.sh")

    assert "/api/health" in smoke
    assert "/api/ready" in smoke
    assert "/api/version" in smoke
    assert 'os.environ.get("BUILD_CHANNEL")' in smoke
    assert "--user 1000:1000" in smoke
    assert '--platform "linux/$expected_arch"' in smoke
    assert "docker image inspect" in smoke
    assert "uname -m" in smoke
    assert "/app/data/.release-smoke" in smoke
    assert "/app/blobs/.release-smoke" in smoke

    syntax = subprocess.run(
        ["sh", "-n", str(ROOT / "scripts/smoke-release-container.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr


def test_shared_compose_smoke_uses_ephemeral_state_and_validates_authority():
    smoke = _read("scripts/smoke-shared-postgres-compose.sh")

    assert "docker/shared-postgres.yml" in smoke
    assert "mktemp -d" in smoke
    assert "RESTIA_POSTGRES_PASSWORD" in smoke
    assert "RESTIA_ENCRYPTION_KEY_FILE_HOST" in smoke
    assert "RESTIA_INPROCESS_POLLERS=0" in smoke
    assert "RESTIA_INPROCESS_TASKS=0" in smoke
    assert 'database_mode.get("mode") != "shared"' in smoke
    assert 'schema.get("matches_expected") is not True' in smoke
    assert "shared-gate --pretty" in smoke
    assert "--force-recreate odysseus" in smoke

    syntax = subprocess.run(
        ["sh", "-n", str(ROOT / "scripts/smoke-shared-postgres-compose.sh")],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr


def test_stable_build_channel_is_baked_for_update_policy():
    dockerfile = _read("Dockerfile")
    assert 'ARG BUILD_CHANNEL="source"' in dockerfile
    assert "ENV BUILD_CHANNEL=$BUILD_CHANNEL" in dockerfile
