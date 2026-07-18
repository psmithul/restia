#!/usr/bin/env sh
set -eu

if [ "$#" -ne 5 ]; then
  echo "usage: $0 IMAGE EXPECTED_VERSION EXPECTED_COMMIT EXPECTED_CHANNEL EXPECTED_ARCH" >&2
  exit 2
fi

image=$1
expected_version=$2
expected_commit=$3
expected_channel=$4
expected_arch=$5
expected_short_commit=$(printf '%s' "$expected_commit" | cut -c1-12)
name="restia-release-smoke-${GITHUB_RUN_ID:-local}-$$"
name=$(printf '%s' "$name" | tr -cd 'a-zA-Z0-9_.-')

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
case "$expected_arch" in
  amd64)
    case "$(uname -m)" in x86_64|amd64) ;; *) echo "runner is not native amd64" >&2; exit 1 ;; esac
    ;;
  arm64)
    case "$(uname -m)" in aarch64|arm64) ;; *) echo "runner is not native arm64" >&2; exit 1 ;; esac
    ;;
  *) echo "invalid expected architecture" >&2; exit 2 ;;
esac

cleanup() {
  status=$?
  trap - EXIT INT TERM
  if [ "$status" -ne 0 ]; then
    docker logs "$name" 2>&1 || true
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker pull --platform "linux/$expected_arch" "$image" >/dev/null
docker run -d --name "$name" --platform "linux/$expected_arch" \
  -e AUTH_ENABLED=false \
  -e LOCALHOST_BYPASS=true \
  -e RESTIA_DATABASE_MODE=local-single \
  -e RESTIA_INPROCESS_POLLERS=0 \
  -e RESTIA_INPROCESS_TASKS=0 \
  -e RESTIA_INPROCESS_TELEGRAM=0 \
  -e RESTIA_INPROCESS_CONTACT_DELIVERY=0 \
  -e RESTIA_INPROCESS_CALENDAR_DELIVERY=0 \
  -e PUID=1000 \
  -e PGID=1000 \
  "$image" >/dev/null

container_image=$(docker inspect --format '{{.Image}}' "$name")
actual_image_arch=$(docker image inspect --format '{{.Architecture}}' "$container_image")
[ "$actual_image_arch" = "$expected_arch" ] || {
  echo "resolved image architecture $actual_image_arch does not match $expected_arch" >&2
  exit 1
}

attempt=0
until docker exec "$name" python -c '
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:7000/api/ready", timeout=3) as response:
    payload = json.load(response)
raise SystemExit(0 if response.status == 200 and payload.get("ready") is True else 1)
' >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 90 ]; then
    echo "container did not become ready" >&2
    exit 1
  fi
  sleep 2
done

docker exec -i --user 1000:1000 "$name" python - \
  "$expected_version" "$expected_short_commit" "$expected_channel" <<'PY'
import json
import os
import sys
import urllib.request

expected_version, expected_commit, expected_channel = sys.argv[1:]

def get_json(path):
    with urllib.request.urlopen(f"http://127.0.0.1:7000{path}", timeout=5) as response:
        if response.status != 200:
            raise SystemExit(f"{path} returned {response.status}")
        return json.load(response)

health = get_json("/api/health")
ready = get_json("/api/ready")
version = get_json("/api/version")
if health.get("status") != "healthy":
    raise SystemExit(f"unhealthy liveness payload: {health}")
if ready.get("ready") is not True:
    raise SystemExit(f"unready payload: {ready}")
if version.get("version") != expected_version:
    raise SystemExit(f"version mismatch: {version}")
if version.get("commit") != expected_commit:
    raise SystemExit(f"commit mismatch: {version}")
if os.environ.get("BUILD_CHANNEL") != expected_channel:
    raise SystemExit("build channel mismatch")

with urllib.request.urlopen("http://127.0.0.1:7000/", timeout=5) as response:
    if response.status != 200 or b"<!DOCTYPE html>" not in response.read(4096):
        raise SystemExit("application shell smoke failed")
PY

docker exec --user 1000:1000 "$name" sh -c '
  test "$(id -u)" != 0
  touch /app/data/.release-smoke /app/blobs/.release-smoke
  rm -f /app/data/.release-smoke /app/blobs/.release-smoke
'

echo "Release container smoke passed for $image"
