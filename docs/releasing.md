# Releasing Restia

Restia separates its canonical internal version from its public release
precision. The internal version is always an exact three-component `X.Y.Z`
value in `src/constants.py`. The public tag communicates release scope.

| Scope | Canonical `APP_VERSION` | GitHub release tag | GHCR version tag |
| --- | --- | --- | --- |
| Major | `X.0.0` | `vX` | `X` |
| Slightly major | `X.Y.0`, with `Y > 0` | `vX.Y` | `X.Y` |
| Minor | `X.Y.Z`, with `Z > 0` | `vX.Y.Z` | `X.Y.Z` |

Examples:

- A major release with `APP_VERSION = "3.0.0"` publishes as `v3` and `:3`.
- A slightly-major release with `APP_VERSION = "3.2.0"` publishes as `v3.2`
  and `:3.2`.
- A minor release with `APP_VERSION = "3.2.1"` publishes as `v3.2.1` and
  `:3.2.1`.

The historical `v2.0.0` and `v2.1.0` releases predate this policy. They remain
unchanged as compatibility history; do not retag them or change the current
`APP_VERSION = "2.1.0"` merely to rewrite that history.

## Preflight

The version helper is strict, stdlib-only, and available as a module CLI:

```bash
python3 -m src.release_version preflight 3.0.0 --release-tag v3
python3 -m src.release_version preflight 3.2.0 --release-tag v3.2
python3 -m src.release_version preflight 3.2.1 --release-tag v3.2.1
```

It rejects omitted canonical components, leading zeroes, prefixes or suffixes
on `APP_VERSION`, uppercase or missing `v` prefixes, and public tags with the
wrong precision. Run the focused release checks as well:

```bash
.venv/bin/pytest -q tests/test_release_version_policy.py tests/test_release_update_flow.py
```

## Publication flow

1. Classify the change as major, slightly-major, or minor.
2. Update `APP_VERSION` in `src/constants.py` using canonical `X.Y.Z` form.
3. Run the preflight with the exact public tag and run the relevant tests.
4. Publish a GitHub release with that tag. Do not publish a stable version tag
   from a branch push.
5. Wait for `ci / docker publish`. Its preflight runs before the build matrix,
   then the release publishes both `ghcr.io/psmithul/restia:latest` and the
   scope-precision immutable tag.
6. Verify that the amd64 and arm64 manifests exist and that `latest` and the
   immutable version tag resolve to the intended release index.

A push to `main` may refresh `:latest`, but it never creates an immutable
stable version tag. A push to `dev` publishes `:dev` plus the canonical
`X.Y.Z-dev.<sha>` traceability tag.

The application update checker accepts one-, two-, and three-component public
release tags so new scope-precision releases and historical releases remain
comparable to canonical internal versions.
