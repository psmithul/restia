from pathlib import Path

from src.update_checker import build_update_result, release_is_newer, version_tuple


ROOT = Path(__file__).resolve().parents[1]


def test_semantic_release_comparison():
    assert version_tuple("v1.2.3") == (1, 2, 3)
    assert release_is_newer("1.0.1", "v1.0.2") is True
    assert release_is_newer("1.0.2", "v1.0.2") is False


def test_release_update_works_even_when_build_commit_is_unknown():
    result = build_update_result(
        repo="psmithul/restia",
        current_version="1.0.1",
        current_commit="unknown",
        release={
            "tag_name": "v1.0.2",
            "name": "Restia 1.0.2",
            "html_url": "https://github.com/psmithul/restia/releases/tag/v1.0.2",
            "published_at": "2026-07-10T00:00:00Z",
            "draft": False,
        },
    )

    assert result["update_available"] is True
    assert result["channel"] == "release"
    assert result["latest_version"] == "1.0.2"
    assert result["image"] == "ghcr.io/psmithul/restia:latest"
    assert result["update_command"] == "./update.sh"


def test_downloaded_compose_uses_public_release_image_and_preserves_host_data():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    updater = (ROOT / "update.sh").read_text(encoding="utf-8")

    assert "ghcr.io/psmithul/restia:latest" in compose
    assert "${APP_DATA_DIR:-./data}:/app/data" in compose
    assert "pull odysseus" in updater
    assert "up -d --no-build odysseus" in updater
    assert "rm -rf" not in updater


def test_release_event_publishes_latest_and_bakes_commit():
    workflow = (ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")

    assert "types: [published]" in workflow
    assert "BUILD_COMMIT=${{ github.sha }}" in workflow
    assert "github.event_name == 'release'" in workflow


def test_macos_bundle_reads_the_shared_release_version():
    script = (ROOT / "build-macos-app.sh").read_text(encoding="utf-8")

    assert 'APP_VERSION="$(sed' in script
    assert script.count("<string>$APP_VERSION</string>") == 2
    assert "<string>2.0.0</string>" not in script
