"""Canonical-to-public Restia release version policy.

Restia keeps a strict three-component version internally (``X.Y.Z``), while
the precision of the public release identifier communicates the release scope:

* ``X.0.0`` -> ``vX`` (major)
* ``X.Y.0`` -> ``vX.Y`` (slightly-major)
* ``X.Y.Z`` -> ``vX.Y.Z`` (minor), when ``Z > 0``

The module is deliberately stdlib-only so GitHub Actions can run its preflight
before starting the multi-architecture Docker build matrix.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re
from typing import Sequence


_CANONICAL_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$"
)


class ReleaseVersionError(ValueError):
    """Raised when a canonical version or public release tag is invalid."""


@dataclass(frozen=True)
class ReleaseIdentity:
    """The canonical and public identities for one Restia release."""

    canonical_version: str
    public_version: str
    public_tag: str
    image_tag: str
    scope: str


def parse_canonical_version(value: str) -> tuple[int, int, int]:
    """Parse an exact internal ``X.Y.Z`` version.

    Prefixes, suffixes, whitespace, omitted components, and leading zeroes are
    rejected. Public tags are validated separately by
    :func:`validate_public_release_tag`.
    """

    if not isinstance(value, str):
        raise ReleaseVersionError("canonical version must be a string")
    match = _CANONICAL_VERSION_RE.fullmatch(value)
    if match is None:
        raise ReleaseVersionError(
            f"invalid canonical version {value!r}; expected exact X.Y.Z"
        )
    return tuple(int(part) for part in match.groups())


def release_identity(canonical_version: str) -> ReleaseIdentity:
    """Return the public release identity for a canonical internal version."""

    major, minor, patch = parse_canonical_version(canonical_version)
    if patch > 0:
        public_version = f"{major}.{minor}.{patch}"
        scope = "minor"
    elif minor > 0:
        public_version = f"{major}.{minor}"
        scope = "slightly-major"
    else:
        public_version = str(major)
        scope = "major"

    return ReleaseIdentity(
        canonical_version=canonical_version,
        public_version=public_version,
        public_tag=f"v{public_version}",
        image_tag=public_version,
        scope=scope,
    )


def public_release_version(canonical_version: str) -> str:
    """Return ``X``, ``X.Y``, or ``X.Y.Z`` according to release scope."""

    return release_identity(canonical_version).public_version


def public_release_tag(canonical_version: str) -> str:
    """Return the exact lowercase-``v`` public tag for a canonical version."""

    return release_identity(canonical_version).public_tag


def validate_public_release_tag(
    canonical_version: str, release_tag: str
) -> ReleaseIdentity:
    """Validate a release tag against the canonical version and return identity."""

    identity = release_identity(canonical_version)
    if release_tag != identity.public_tag:
        raise ReleaseVersionError(
            f"release tag {release_tag!r} does not match canonical version "
            f"{canonical_version!r}; expected {identity.public_tag!r} for a "
            f"{identity.scope} release"
        )
    return identity


def _write_github_output(path: Path, identity: ReleaseIdentity) -> None:
    values = {
        "canonical_version": identity.canonical_version,
        "public_version": identity.public_version,
        "public_tag": identity.public_tag,
        "image_tag": identity.image_tag,
        "release_scope": identity.scope,
    }
    with path.open("a", encoding="utf-8") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.release_version",
        description="Apply Restia's canonical-to-public release version policy.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser(
        "preflight",
        help="validate a canonical version and optional public release tag",
    )
    preflight.add_argument("app_version", help="strict canonical X.Y.Z version")
    preflight.add_argument(
        "--release-tag",
        help="public tag to validate (required by the release workflow)",
    )
    preflight.add_argument(
        "--github-output",
        type=Path,
        help="append canonical/public identity fields to this GitHub output file",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.release_tag is None:
            identity = release_identity(args.app_version)
        else:
            identity = validate_public_release_tag(
                args.app_version, args.release_tag
            )
    except ReleaseVersionError as exc:
        parser.error(str(exc))

    if args.github_output is not None:
        _write_github_output(args.github_output, identity)
    print(json.dumps(asdict(identity), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
