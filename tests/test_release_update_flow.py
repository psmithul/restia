from pathlib import Path

from src.update_checker import build_update_result, release_is_newer, version_tuple


ROOT = Path(__file__).resolve().parents[1]


def test_semantic_release_comparison():
    assert version_tuple("v1.2.3") == (1, 2, 3)
    assert version_tuple("v3") == (3, 0, 0)
    assert version_tuple("v2.1") == (2, 1, 0)
    assert release_is_newer("2.0.0", "v2.1") is True
    assert release_is_newer("2.1.0", "v3") is True
    assert release_is_newer("2.1.0", "v2.1") is False
    assert release_is_newer("1.0.1", "v1.0.2") is True
    assert release_is_newer("1.0.2", "v1.0.2") is False


def test_release_comparison_rejects_non_policy_versions():
    for invalid in ("V3", "v03", "v3.01", "v3.1.0-rc.1", "v3+build", "3.1.0.0"):
        assert version_tuple(invalid) is None
        assert release_is_newer("2.1.0", invalid) is False


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


def test_draft_and_prerelease_releases_never_trigger_stable_updates():
    base = {
        "tag_name": "v3",
        "name": "Restia v3",
        "html_url": "https://github.com/psmithul/restia/releases/tag/v3",
        "published_at": "2026-07-16T00:00:00Z",
    }

    for flag in ("draft", "prerelease"):
        release = {**base, flag: True}
        result = build_update_result(
            repo="psmithul/restia",
            current_version="2.1.0",
            current_commit="unknown",
            release=release,
        )
        assert result["update_available"] is False
        assert result["channel"] == "current"


def test_stable_release_images_never_advertise_rolling_dev_commits():
    branch_commit = {
        "sha": "b" * 40,
        "commit": {"message": "new dev work"},
    }

    for build_channel in ("stable", "release"):
        result = build_update_result(
            repo="psmithul/restia",
            current_version="3.0.0",
            current_commit="a" * 12,
            release={"tag_name": "v3", "draft": False, "prerelease": False},
            branch_commit=branch_commit,
            build_channel=build_channel,
        )
        assert result["update_available"] is False
        assert result["channel"] == "current"
        assert result["build_channel"] == build_channel


def test_dev_images_still_receive_dev_commit_updates_between_releases():
    result = build_update_result(
        repo="psmithul/restia",
        current_version="3.0.0",
        current_commit="a" * 12,
        release={"tag_name": "v3", "draft": False, "prerelease": False},
        branch_commit={"sha": "b" * 40, "commit": {"message": "new dev work"}},
        build_channel="dev",
    )

    assert result["update_available"] is True
    assert result["channel"] == "dev"


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
    assert "BUILD_CHANNEL=${{ needs.preflight.outputs.build_channel }}" in workflow
    assert "github.event_name == 'release'" in workflow
    assert "needs: [preflight, merge, smoke, shared_smoke]" in workflow


def test_update_endpoint_never_fetches_dev_for_stable_images():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    block = source[source.index("async def update_check()") : source.index("@app.get(\"/api/health\")")]

    assert 'if BUILD_CHANNEL in {"source", "dev"}:' in block
    assert "build_channel=BUILD_CHANNEL" in block


def test_macos_bundle_reads_the_shared_release_version():
    script = (ROOT / "build-macos-app.sh").read_text(encoding="utf-8")

    assert 'APP_VERSION="$(sed' in script
    assert script.count("<string>$APP_VERSION</string>") == 2
    assert "<string>2.0.0</string>" not in script
