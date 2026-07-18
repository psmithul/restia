"""Pure helpers for GitHub release and commit update checks."""

from __future__ import annotations

import os
import re
from typing import Any


_VERSION_RE = re.compile(
    r"^v?(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?(?:\.(0|[1-9]\d*))?$"
)


def version_tuple(value: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(str(value or "").strip())
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def release_is_newer(current_version: str, release_tag: str) -> bool:
    current = version_tuple(current_version)
    latest = version_tuple(release_tag)
    return bool(current and latest and latest > current)


def build_update_result(
    *,
    repo: str,
    current_version: str,
    current_commit: str,
    release: dict[str, Any] | None = None,
    branch_commit: dict[str, Any] | None = None,
    build_channel: str | None = None,
) -> dict[str, Any]:
    """Build the stable JSON contract consumed by ``updateChecker.js``."""
    release = release or {}
    tag = str(release.get("tag_name") or "").strip()
    release_update = bool(
        tag
        and not release.get("draft")
        and not release.get("prerelease")
        and release_is_newer(current_version, tag)
    )
    current_build_channel = str(
        build_channel if build_channel is not None else os.getenv("BUILD_CHANNEL") or "source"
    ).strip().lower()
    if current_build_channel not in {"source", "dev", "stable", "release"}:
        current_build_channel = "source"
    allow_dev_updates = current_build_channel in {"source", "dev"}
    remote_sha = str((branch_commit or {}).get("sha") or "")[:12]
    commit_update = bool(
        not release_update
        and allow_dev_updates
        and current_commit
        and current_commit != "unknown"
        and remote_sha
        and remote_sha != current_commit[:12]
    )

    result: dict[str, Any] = {
        "update_available": release_update or commit_update,
        "channel": "release" if release_update else "dev" if commit_update else "current",
        "current_version": current_version,
        "current_commit": current_commit,
        "build_channel": current_build_channel,
        "latest_version": tag.lstrip("vV") if tag else "",
        "latest_commit": remote_sha,
        "repo": repo,
        "release_url": str(release.get("html_url") or ""),
        "release_name": str(release.get("name") or tag or "")[:120],
        "release_date": str(release.get("published_at") or ""),
        "image": f"ghcr.io/{repo}:latest",
        "update_command": "./update.sh",
    }
    if branch_commit:
        commit = branch_commit.get("commit") if isinstance(branch_commit.get("commit"), dict) else {}
        result["latest_message"] = str(commit.get("message") or "").split("\n", 1)[0][:120]
        committer = commit.get("committer") if isinstance(commit.get("committer"), dict) else {}
        result["latest_date"] = str(committer.get("date") or "")
    if release_update:
        result["latest_message"] = result["release_name"] or f"Restia {tag}"
        result["latest_date"] = result["release_date"]
    return result
