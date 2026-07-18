#!/usr/bin/env sh
set -eu

if [ "$#" -ne 4 ]; then
  echo "usage: $0 IMAGE EXPECTED_VERSION EXPECTED_COMMIT EXPECTED_CHANNEL" >&2
  exit 2
fi

image=$1
expected_version=$2
expected_commit=$3
expected_channel=$4
expected_short_commit=$(printf '%s' "$expected_commit" | cut -c1-12)
root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
cd "$root"

printf '%s\n' "$expected_version" \
  | grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' || {
  echo "invalid expected version" >&2
  exit 2
}
printf '%s\n' "$expected_commit" | grep -Eq '^[0-9a-f]{40}$' || {
  echo "expected commit must be a full lowercase Git SHA" >&2
  exit 2
}
case "$expected_channel" in
  dev|stable|release) ;;
  *) echo "invalid expected build channel" >&2; exit 2 ;;
esac

if docker compose version >/dev/null 2>&1; then
  compose_command="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  compose_command="docker-compose"
else
  echo "Docker Compose is required" >&2
  exit 1
fi

project="restia-shared-smoke-${GITHUB_RUN_ID:-local}-$$"
project=$(printf '%s' "$project" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')
workdir=$(mktemp -d "${TMPDIR:-/tmp}/restia-shared-compose.XXXXXX")

compose() {
  # Intentional word splitting: compose_command is either two fixed words or
  # one fixed executable selected above, never user input.
  # shellcheck disable=SC2086
  $compose_command --project-name "$project" \
    -f docker-compose.yml -f docker/shared-postgres.yml "$@"
}

cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [ "$status" -ne 0 ]; then
    compose logs --no-color odysseus postgres 2>&1 || true
  fi
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  rm -rf -- "$workdir"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p "$workdir/data" "$workdir/logs" "$workdir/blobs"
python3 -c 'import base64, os, sys; sys.stdout.buffer.write(base64.urlsafe_b64encode(os.urandom(32)))' \
  > "$workdir/restia_fernet_key"
chmod 600 "$workdir/restia_fernet_key"

export RESTIA_IMAGE="$image"
export RESTIA_POSTGRES_PASSWORD
RESTIA_POSTGRES_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
export ODYSSEUS_ADMIN_PASSWORD
ODYSSEUS_ADMIN_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
export RESTIA_ENCRYPTION_KEY_FILE_HOST="$workdir/restia_fernet_key"
export APP_DATA_DIR="$workdir/data"
export APP_LOGS_DIR="$workdir/logs"
export APP_BLOB_DIR="$workdir/blobs"
# Match the container's non-root identity to the owner of the ephemeral bind
# mounts and mode-0600 secret. This is the documented PUID/PGID deployment
# contract and keeps the smoke representative on runners whose uid is not 1000.
export PUID PGID
PUID=$(id -u)
PGID=$(id -g)
export APP_BIND=127.0.0.1
export APP_PORT=17902
export TZ=UTC
export RESTIA_INPROCESS_POLLERS=0
export RESTIA_INPROCESS_TASKS=0
export RESTIA_INPROCESS_TELEGRAM=0
export RESTIA_INPROCESS_CONTACT_DELIVERY=0
export RESTIA_INPROCESS_CALENDAR_DELIVERY=0

compose config >/dev/null
compose pull odysseus postgres
compose up -d --no-build postgres

attempt=0
until compose exec -T postgres pg_isready -U restia -d restia >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "PostgreSQL did not become healthy" >&2
    exit 1
  fi
  sleep 2
done

# Base Compose also offers optional search/vector services. They are not part
# of the database authority smoke, so start only the app and its PostgreSQL
# authority while retaining the exact production overlay configuration.
compose up -d --no-build --no-deps odysseus

wait_ready() {
  attempt=0
  while [ "$attempt" -lt 90 ]; do
    if compose exec -T odysseus python -c '
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:7000/api/health", timeout=3) as response:
    payload = json.load(response)
raise SystemExit(0 if response.status == 200 and payload.get("status") == "healthy" else 1)
' >/dev/null 2>&1; then
      break
    fi
    attempt=$((attempt + 1))
    sleep 2
  done
  if [ "$attempt" -ge 90 ]; then
    return 1
  fi

  # Readiness is intentionally authenticated. Verify it with the explicit
  # ephemeral admin instead of weakening the production auth middleware or
  # relying on LOCALHOST_BYPASS, which shared mode forbids.
  compose exec -T odysseus python - <<'PY'
import http.cookiejar
import json
import os
import urllib.request

base = "http://127.0.0.1:7000"
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)
login = urllib.request.Request(
    base + "/api/auth/login",
    data=json.dumps({
        "username": "admin",
        "password": os.environ["ODYSSEUS_ADMIN_PASSWORD"],
        "remember": False,
    }).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with opener.open(login, timeout=5) as response:
    if response.status != 200 or json.load(response).get("ok") is not True:
        raise SystemExit("ephemeral admin login failed")
with opener.open(base + "/api/ready", timeout=5) as response:
    payload = json.load(response)
    if response.status != 200 or payload.get("ready") is not True:
        raise SystemExit("authenticated readiness failed")
PY
}

wait_ready || {
  echo "shared Restia container did not become ready" >&2
  exit 1
}

compose exec -T odysseus python - \
  "$expected_version" "$expected_short_commit" "$expected_channel" <<'PY'
import json
import http.cookiejar
import os
import sys
import urllib.request

expected_version, expected_commit, expected_channel = sys.argv[1:]
base = "http://127.0.0.1:7000"
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)
login = urllib.request.Request(
    base + "/api/auth/login",
    data=json.dumps({
        "username": "admin",
        "password": os.environ["ODYSSEUS_ADMIN_PASSWORD"],
        "remember": False,
    }).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with opener.open(login, timeout=5) as response:
    if response.status != 200 or json.load(response).get("ok") is not True:
        raise SystemExit("ephemeral admin login failed")

def get_json(path):
    with opener.open(base + path, timeout=5) as response:
        if response.status != 200:
            raise SystemExit(f"{path} returned {response.status}")
        return json.load(response)

ready = get_json("/api/ready")
version = get_json("/api/version")
checks = ready.get("checks") or {}
database_mode = checks.get("database_mode") or {}
schema = checks.get("schema") or {}
shared = checks.get("shared_runtime") or {}
if version.get("version") != expected_version:
    raise SystemExit(f"version mismatch: {version}")
if version.get("commit") != expected_commit:
    raise SystemExit(f"commit mismatch: {version}")
if os.environ.get("BUILD_CHANNEL") != expected_channel:
    raise SystemExit("build channel mismatch")
if database_mode.get("mode") != "shared" or database_mode.get("dialect") != "postgresql":
    raise SystemExit(f"database mode mismatch: {database_mode}")
if schema.get("matches_expected") is not True or schema.get("state") != "current":
    raise SystemExit(f"schema mismatch: {schema}")
if shared.get("schema_authority_ready") is not True or shared.get("blocker_codes") != []:
    raise SystemExit(f"shared runtime is blocked: {shared}")
PY

compose exec -T odysseus python /app/scripts/odysseus-db shared-gate --pretty >/dev/null
compose exec -T --user 1000:1000 odysseus sh -c '
  touch /app/blobs/.shared-compose-smoke
  rm -f /app/blobs/.shared-compose-smoke
'

# A recreate must adopt the same PostgreSQL schema and shared blob authority.
compose up -d --no-build --no-deps --force-recreate odysseus
wait_ready || {
  echo "shared Restia container did not recover after recreate" >&2
  exit 1
}

echo "Shared PostgreSQL Compose smoke passed for $image"
