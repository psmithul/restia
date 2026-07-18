#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

STATE_FILE=".restia-update-state"
READINESS_ATTEMPTS="${RESTIA_UPDATE_READINESS_ATTEMPTS:-90}"
SHARED_BACKUP=""
ROLLBACK=0
RESTORE_DATA=0

usage() {
  cat <<'EOF'
Usage:
  ./update.sh [--shared-backup PATH]
  ./update.sh --rollback [--restore-data]

Normal updates create and verify a private pre-update snapshot, pull the
configured image, and require /api/ready before succeeding. Shared PostgreSQL
deployments must provide --shared-backup PATH naming an operator-created,
non-empty database + blob-store backup artifact.

--rollback restarts the exact prior image recorded by the last update.
--restore-data additionally restores the local SQLite/data snapshot and is
destructive; it is unavailable for external/shared backup artifacts.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --shared-backup)
      [ "$#" -ge 2 ] || { echo "--shared-backup requires a path" >&2; exit 2; }
      SHARED_BACKUP=$2
      shift 2
      ;;
    --rollback)
      ROLLBACK=1
      shift
      ;;
    --restore-data)
      RESTORE_DATA=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if docker compose version >/dev/null 2>&1; then
  compose() { docker compose "$@"; }
elif command -v docker-compose >/dev/null 2>&1; then
  compose() { docker-compose "$@"; }
else
  echo "Docker Compose is required. Start or update Docker Desktop, then retry." >&2
  exit 1
fi

wait_ready() {
  attempt=0
  while [ "$attempt" -lt "$READINESS_ATTEMPTS" ]; do
    if compose exec -T odysseus python -c '
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:7000/api/ready", timeout=3) as response:
    payload = json.load(response)
raise SystemExit(0 if response.status == 200 and payload.get("ready") is True else 1)
' >/dev/null 2>&1; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 2
  done
  return 1
}

container_image_id() {
  container_id=$(compose ps -q odysseus)
  [ -n "$container_id" ] || return 1
  docker inspect --format '{{.Image}}' "$container_id"
}

validate_image_id() {
  printf '%s\n' "$1" | grep -Eq '^sha256:[0-9a-f]{64}$'
}

validate_target_image() {
  # Rollback deliberately retags this mutable local reference to the prior
  # image. Digest-only references cannot be retagged and would make the next
  # ordinary `docker compose up` silently jump back to the failed image.
  case "$1" in
    sha256:*|*@*) return 1 ;;
  esac
  printf '%s\n' "$1" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9._/:+-]*$'
}

state_value() {
  key=$1
  sed -n "s/^${key}=//p" "$STATE_FILE" | head -n 1
}

write_state() {
  previous_image=$1
  backup_mode=$2
  backup_file=$3
  target_image=$4
  umask 077
  pending="${STATE_FILE}.tmp.$$"
  {
    echo "previous_image=$previous_image"
    echo "backup_mode=$backup_mode"
    echo "backup_file=$backup_file"
    echo "target_image=$target_image"
    date -u '+updated_at=%Y-%m-%dT%H:%M:%SZ'
  } > "$pending"
  mv "$pending" "$STATE_FILE"
}

rollback_image() {
  previous_image=$1
  target_image=$2
  # Move the configured local tag back to the exact prior image. This keeps a
  # later `docker compose up` on the rollback instead of resolving the newly
  # pulled (failed) image again.
  if ! docker image tag "$previous_image" "$target_image"; then
    return 1
  fi
  export RESTIA_IMAGE="$target_image"
  if ! compose up -d --no-build odysseus; then
    return 1
  fi
  if ! wait_ready; then
    return 1
  fi
  return 0
}

if [ "$ROLLBACK" -eq 1 ]; then
  [ -f "$STATE_FILE" ] || {
    echo "No rollback state found at $STATE_FILE" >&2
    exit 1
  }
  previous_image=$(state_value previous_image)
  backup_mode=$(state_value backup_mode)
  backup_file=$(state_value backup_file)
  target_image=$(state_value target_image)
  validate_image_id "$previous_image" || {
    echo "Rollback state contains an invalid image ID." >&2
    exit 1
  }
  validate_target_image "$target_image" || {
    echo "Rollback state contains an invalid target image reference." >&2
    exit 1
  }

  if [ "$RESTORE_DATA" -eq 1 ]; then
    [ "$backup_mode" = "local" ] || {
      echo "Automatic data restore is only available for local snapshots." >&2
      exit 1
    }
    case "$backup_file" in
      backups/*) ;;
      *) echo "Rollback state contains an unsafe backup path." >&2; exit 1 ;;
    esac
    [ -f "$backup_file" ] || {
      echo "Rollback snapshot is missing: $backup_file" >&2
      exit 1
    }
    backup_abs=$(cd "$(dirname "$backup_file")" && pwd)/$(basename "$backup_file")
    echo "Stopping Restia and restoring the verified pre-update snapshot..."
    compose stop odysseus
    export RESTIA_IMAGE="$previous_image"
    compose run --rm --no-deps -T \
      -v "$backup_abs:/restore/restia-backup.tar.gz:ro" \
      odysseus python /app/scripts/odysseus-backup \
      restore /restore/restia-backup.tar.gz --yes
  fi

  echo "Restarting the previous image $previous_image..."
  if rollback_image "$previous_image" "$target_image"; then
    echo "Rollback complete and /api/ready is healthy."
    exit 0
  fi
  echo "Rollback image started but did not become ready." >&2
  exit 1
fi

[ "$RESTORE_DATA" -eq 0 ] || {
  echo "--restore-data requires --rollback" >&2
  exit 2
}

previous_image=$(container_image_id) || {
  echo "Restia must be running so the updater can create a consistent snapshot." >&2
  exit 1
}
validate_image_id "$previous_image" || {
  echo "Could not determine the exact current image for rollback." >&2
  exit 1
}

image=${RESTIA_IMAGE:-ghcr.io/psmithul/restia:latest}
validate_target_image "$image" || {
  echo "RESTIA_IMAGE must be a mutable Docker tag so rollback remains durable." >&2
  exit 1
}
export RESTIA_IMAGE="$image"

database_mode=$(compose exec -T odysseus python -c \
  'import os; print(os.getenv("RESTIA_DATABASE_MODE") or os.getenv("ODYSSEUS_DATABASE_MODE") or "local-single")')

if [ "$database_mode" = "shared" ]; then
  [ -n "$SHARED_BACKUP" ] || {
    echo "Shared mode requires --shared-backup PATH before updating." >&2
    echo "The artifact must cover PostgreSQL and RESTIA_BLOB_ROOT; Restia will not pretend a local data/ snapshot is sufficient." >&2
    exit 1
  }
  [ -f "$SHARED_BACKUP" ] && [ -s "$SHARED_BACKUP" ] || {
    echo "Shared backup proof must be an existing non-empty file." >&2
    exit 1
  }
  backup_mode="shared-external"
  backup_file=$(cd "$(dirname "$SHARED_BACKUP")" && pwd)/$(basename "$SHARED_BACKUP")
else
  [ "$database_mode" = "local-single" ] || {
    echo "Unsupported database mode reported by the running container: $database_mode" >&2
    exit 1
  }
  mkdir -p backups
  backup_name="restia-pre-update-$(date -u '+%Y%m%d-%H%M%S')-$$.tar.gz"
  container_backup="/tmp/$backup_name"
  backup_file="backups/$backup_name"
  echo "Creating and verifying the pre-update snapshot..."
  compose exec -T odysseus python /app/scripts/odysseus-backup \
    snapshot --out "$container_backup"
  compose exec -T odysseus python /app/scripts/odysseus-backup \
    verify "$container_backup"
  compose cp "odysseus:$container_backup" "$backup_file"
  compose exec -T odysseus rm -f "$container_backup"
  [ -s "$backup_file" ] || {
    echo "The pre-update snapshot copy is missing or empty." >&2
    exit 1
  }
  chmod 600 "$backup_file" 2>/dev/null || true
  backup_abs=$(cd "$(dirname "$backup_file")" && pwd)/$(basename "$backup_file")
  # Verify the bytes copied onto the host with the exact previous image. Do
  # not require Python/project dependencies on the Docker host.
  export RESTIA_IMAGE="$previous_image"
  compose run --rm --no-deps -T --entrypoint python \
    -v "$backup_abs:/restore/restia-backup.tar.gz:ro" \
    odysseus /app/scripts/odysseus-backup \
    verify /restore/restia-backup.tar.gz
  export RESTIA_IMAGE="$image"
  backup_mode="local"
fi

write_state "$previous_image" "$backup_mode" "$backup_file" "$image"

echo "Pulling $image..."
compose pull odysseus
echo "Restarting Restia while preserving data, logs, and blobs..."
if ! compose up -d --no-build odysseus || ! wait_ready; then
  echo "The updated container did not become ready; restoring the previous image..." >&2
  if rollback_image "$previous_image" "$image"; then
    echo "Previous image is healthy again. Data was not automatically overwritten." >&2
    echo "Use './update.sh --rollback --restore-data' only if a local data restore is required." >&2
  else
    echo "Automatic image rollback also failed. Snapshot: $backup_file" >&2
  fi
  exit 1
fi

version=$(compose exec -T odysseus python -c '
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:7000/api/version", timeout=3) as response:
    payload = json.load(response)
print(str(payload.get("version") or "unknown"))
')
echo "Restia $version is ready. Snapshot: $backup_file"
echo "Rollback remains available with: ./update.sh --rollback"
