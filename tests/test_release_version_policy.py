import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.release_version import (
    ReleaseVersionError,
    parse_canonical_version,
    public_release_tag,
    public_release_version,
    release_identity,
    validate_public_release_tag,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("canonical", "public_version", "public_tag", "scope"),
    [
        ("3.0.0", "3", "v3", "major"),
        ("3.2.0", "3.2", "v3.2", "slightly-major"),
        ("3.2.1", "3.2.1", "v3.2.1", "minor"),
        ("3.0.4", "3.0.4", "v3.0.4", "minor"),
    ],
)
def test_public_release_precision_expresses_scope(
    canonical, public_version, public_tag, scope
):
    identity = release_identity(canonical)

    assert identity.canonical_version == canonical
    assert identity.public_version == public_version
    assert identity.public_tag == public_tag
    assert identity.image_tag == public_version
    assert identity.scope == scope
    assert public_release_version(canonical) == public_version
    assert public_release_tag(canonical) == public_tag


@pytest.mark.parametrize(
    "invalid",
    [
        "3",
        "3.2",
        "v3.2.0",
        "03.2.0",
        "3.02.0",
        "3.2.00",
        "3.2.0-rc.1",
        "3.2.0+build",
        "3.2.٠",
        " 3.2.0",
        "3.2.0 ",
        "",
    ],
)
def test_canonical_version_is_strict_three_component_semver(invalid):
    with pytest.raises(ReleaseVersionError, match="expected exact X.Y.Z"):
        parse_canonical_version(invalid)


@pytest.mark.parametrize(
    ("canonical", "tag"),
    [
        ("3.0.0", "v3"),
        ("3.2.0", "v3.2"),
        ("3.2.1", "v3.2.1"),
    ],
)
def test_public_release_tag_validation_accepts_exact_policy_tag(canonical, tag):
    assert validate_public_release_tag(canonical, tag).public_tag == tag


@pytest.mark.parametrize(
    ("canonical", "tag", "expected"),
    [
        ("3.0.0", "v3.0.0", "v3"),
        ("3.2.0", "v3.2.0", "v3.2"),
        ("3.2.1", "v3.2", "v3.2.1"),
        ("3.2.0", "3.2", "v3.2"),
        ("3.2.0", "V3.2", "v3.2"),
        ("3.2.0", "v3.2-rc.1", "v3.2"),
    ],
)
def test_public_release_tag_validation_rejects_wrong_precision_or_prefix(
    canonical, tag, expected
):
    with pytest.raises(ReleaseVersionError, match=expected):
        validate_public_release_tag(canonical, tag)


def test_preflight_cli_emits_identity_and_github_outputs(tmp_path):
    github_output = tmp_path / "github-output"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.release_version",
            "preflight",
            "4.3.0",
            "--release-tag",
            "v4.3",
            "--github-output",
            str(github_output),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "canonical_version": "4.3.0",
        "image_tag": "4.3",
        "public_tag": "v4.3",
        "public_version": "4.3",
        "scope": "slightly-major",
    }
    assert github_output.read_text(encoding="utf-8").splitlines() == [
        "canonical_version=4.3.0",
        "public_version=4.3",
        "public_tag=v4.3",
        "image_tag=4.3",
        "release_scope=slightly-major",
    ]


def test_preflight_cli_fails_closed_for_historical_full_precision_style():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.release_version",
            "preflight",
            "2.1.0",
            "--release-tag",
            "v2.1.0",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "expected 'v2.1' for a slightly-major release" in result.stderr


def test_docker_workflow_runs_version_preflight_before_build_matrix():
    workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.index("\n  preflight:") < workflow.index("\n  build:")
    assert "python3 -m src.release_version" in workflow
    assert "needs: preflight" in workflow
    assert "needs: [preflight, build]" in workflow
    assert 'args+=(--release-tag "$RELEASE_TAG")' in workflow


def test_docker_workflow_emits_stable_version_tag_only_for_release_event():
    workflow = (ROOT / ".github/workflows/docker-publish.yml").read_text(
        encoding="utf-8"
    )

    assert (
        "type=raw,value=${{ needs.preflight.outputs.image_tag }},"
        "enable=${{ github.event_name == 'release' }}"
    ) in workflow
    assert (
        "type=raw,value=${{ needs.preflight.outputs.canonical_version }}-dev."
        "${{ needs.preflight.outputs.short_sha }},"
        "enable=${{ github.ref == 'refs/heads/dev' }}"
    ) in workflow
    assert "steps.ver.outputs.version" not in workflow


def test_release_policy_is_documented_for_users_and_contributors():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs/releasing.md").read_text(encoding="utf-8")

    for text in (readme, contributing, guide):
        assert "`vX`" in text
        assert "`vX.Y`" in text
        assert "`vX.Y.Z`" in text
    assert "historical `v2.0.0` and `v2.1.0`" in guide
    assert "python3 -m src.release_version preflight" in guide
