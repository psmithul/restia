#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

if docker compose version >/dev/null 2>&1; then
  compose="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  compose="docker-compose"
else
  echo "Docker Compose is required. Start or update Docker Desktop, then retry." >&2
  exit 1
fi

image="${RESTIA_IMAGE:-ghcr.io/psmithul/restia:latest}"
export RESTIA_IMAGE="$image"

echo "Pulling $image..."
$compose pull odysseus
echo "Restarting Restia while preserving data and logs..."
$compose up -d --no-build odysseus
echo "Restia update complete. Your data volume was not replaced."
