#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT_DIR/.deploy.env" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT_DIR/.deploy.env"
fi

DEPLOY_HOST="${DEPLOY_HOST:-182.92.102.61}"
DEPLOY_USER="${DEPLOY_USER:-root}"
DEPLOY_PORT="${DEPLOY_PORT:-22}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/play_video/ivapp}"
DEPLOY_PROJECT="${DEPLOY_PROJECT:-ivapp}"
DEPLOY_SERVICE="${DEPLOY_SERVICE:-api}"
DEPLOY_DATABASE_SERVICE="${DEPLOY_DATABASE_SERVICE:-mysql}"
DEPLOY_RELEASE_ROOT="${DEPLOY_RELEASE_ROOT:-/opt/play_video/releases/ivapp}"
DEPLOY_BACKUP_ROOT="${DEPLOY_BACKUP_ROOT:-/opt/play_video/backups/ivapp}"
DEPLOY_HEALTH_URL="${DEPLOY_HEALTH_URL:-http://127.0.0.1:8100/health}"
DEPLOY_HEALTH_RETRIES="${DEPLOY_HEALTH_RETRIES:-20}"
DEPLOY_HEALTH_INTERVAL="${DEPLOY_HEALTH_INTERVAL:-2}"

DRY_RUN=0
ALLOW_DIRTY=0
SKIP_CHECKS=0
BUILD_IMAGE=1

usage() {
  cat <<'USAGE'
Usage: scripts/deploy.sh [options]

Options:
  --dry-run       Show files that would change; do not write or restart remotely.
  --allow-dirty   Allow deployment from a dirty local Git worktree.
  --skip-checks   Skip scripts/check.sh.
  --no-build      Reuse the current API image; only replace source and recreate API.
  -h, --help      Show this help.

Configuration is loaded from .deploy.env when present. Production .env is never
uploaded, downloaded, printed, or included in source backups.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --skip-checks) SKIP_CHECKS=1 ;;
    --no-build) BUILD_IMAGE=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

[[ "$DEPLOY_PORT" =~ ^[0-9]+$ ]] || { echo "DEPLOY_PORT must be numeric" >&2; exit 2; }
[[ "$DEPLOY_HEALTH_RETRIES" =~ ^[1-9][0-9]*$ ]] || { echo "DEPLOY_HEALTH_RETRIES must be positive" >&2; exit 2; }
[[ "$DEPLOY_HEALTH_INTERVAL" =~ ^[1-9][0-9]*$ ]] || { echo "DEPLOY_HEALTH_INTERVAL must be positive" >&2; exit 2; }

for command_name in git ssh rsync curl python3; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing local command: $command_name" >&2
    exit 1
  }
done

if [[ "$ALLOW_DIRTY" -eq 0 ]] && [[ -n "$(git -C "$ROOT_DIR" status --porcelain)" ]]; then
  echo "Refusing to deploy a dirty worktree. Commit changes or pass --allow-dirty." >&2
  exit 1
fi

if [[ "$SKIP_CHECKS" -eq 0 ]]; then
  "$ROOT_DIR/scripts/check.sh"
fi

SSH=(ssh -p "$DEPLOY_PORT" -o BatchMode=yes "$DEPLOY_USER@$DEPLOY_HOST")
RSYNC_SSH="ssh -p $DEPLOY_PORT -o BatchMode=yes"
RSYNC_FILTERS=(
  --exclude='.git/'
  --exclude='.env'
  --exclude='.deploy.env'
  --exclude='volumes/'
  --exclude='__pycache__/'
  --exclude='*.pyc'
  --exclude='.pytest_cache/'
  --exclude='.mypy_cache/'
  --exclude='.ruff_cache/'
  --exclude='.DS_Store'
)

echo "[deploy] target=$DEPLOY_USER@$DEPLOY_HOST:$DEPLOY_PATH service=$DEPLOY_SERVICE"

"${SSH[@]}" bash -s -- \
  "$DEPLOY_PATH" "$DEPLOY_RELEASE_ROOT" "$DEPLOY_BACKUP_ROOT" <<'REMOTE'
set -Eeuo pipefail
deploy_path="$1"
release_root="$2"
backup_root="$3"

validate_path() {
  case "$1" in
    /opt/*) ;;
    *) echo "Unsafe remote path: $1" >&2; exit 2 ;;
  esac
  case "$1" in
    /|/opt|/opt/play_video) echo "Remote path is too broad: $1" >&2; exit 2 ;;
  esac
}

validate_path "$deploy_path"
validate_path "$release_root"
validate_path "$backup_root"
test -d "$deploy_path"
test -f "$deploy_path/.env"
test -f "$deploy_path/docker-compose.yml"
command -v docker-compose >/dev/null
command -v rsync >/dev/null
command -v curl >/dev/null
command -v gzip >/dev/null
REMOTE

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "[deploy] dry run; proposed live-tree changes:"
  rsync -azn --no-owner --no-group --delete-delay --itemize-changes \
    "${RSYNC_FILTERS[@]}" \
    -e "$RSYNC_SSH" \
    "$ROOT_DIR/" "$DEPLOY_USER@$DEPLOY_HOST:$DEPLOY_PATH/"
  echo "[deploy] dry run complete; production was not changed"
  exit 0
fi

REVISION="$(git -C "$ROOT_DIR" rev-parse --short=12 HEAD)"
RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$REVISION"
RELEASE_PATH="$DEPLOY_RELEASE_ROOT/$RELEASE_ID"
BACKUP_PATH="$DEPLOY_BACKUP_ROOT/$RELEASE_ID"

echo "[deploy] preparing immutable release $RELEASE_ID"
"${SSH[@]}" bash -s -- "$DEPLOY_RELEASE_ROOT" "$DEPLOY_BACKUP_ROOT" "$RELEASE_PATH" <<'REMOTE'
set -Eeuo pipefail
release_root="$1"
backup_root="$2"
release_path="$3"
install -d -m 700 "$release_root" "$backup_root" "$release_path"
REMOTE

rsync -az --no-owner --no-group --delete-delay \
  "${RSYNC_FILTERS[@]}" \
  -e "$RSYNC_SSH" \
  "$ROOT_DIR/" "$DEPLOY_USER@$DEPLOY_HOST:$RELEASE_PATH/"

echo "[deploy] validating release configuration"
if ! "${SSH[@]}" bash -s -- \
  "$DEPLOY_PATH" "$RELEASE_PATH" "$DEPLOY_PROJECT" "$DEPLOY_SERVICE" "$BUILD_IMAGE" <<'REMOTE'
set -Eeuo pipefail
deploy_path="$1"
release_path="$2"
project="$3"
service="$4"
build_image="$5"
chmod 700 "$release_path"
ln -sfn "$deploy_path/.env" "$release_path/.env"
docker-compose -p "$project" -f "$release_path/docker-compose.yml" config --quiet
if [[ "$build_image" -eq 1 ]]; then
  docker-compose -p "$project" -f "$release_path/docker-compose.yml" build "$service"
fi
REMOTE
then
  echo "[deploy] release validation/build failed; live service was not changed" >&2
  exit 1
fi

echo "[deploy] snapshotting current live source to $BACKUP_PATH"
"${SSH[@]}" bash -s -- \
  "$DEPLOY_PATH" "$BACKUP_PATH" "$DEPLOY_PROJECT" "$DEPLOY_DATABASE_SERVICE" <<'REMOTE'
set -Eeuo pipefail
deploy_path="$1"
backup_path="$2"
project="$3"
database_service="$4"
source_backup="$backup_path/source"
install -d -m 700 "$backup_path" "$source_backup"
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.env' \
  --exclude='.deploy.env' \
  --exclude='volumes/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache/' \
  "$deploy_path/" "$source_backup/"
docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" exec -T \
  "$database_service" sh -c \
  'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --quick --routines --events --triggers "$MYSQL_DATABASE"' \
  | gzip -9 > "$backup_path/database.sql.gz"
test -s "$backup_path/database.sql.gz"
sha256sum "$backup_path/database.sql.gz" > "$backup_path/database.sql.gz.sha256"
chmod 700 "$backup_path"
REMOTE

rollback() {
  echo "[deploy] rolling back to $BACKUP_PATH" >&2
  "${SSH[@]}" bash -s -- \
    "$DEPLOY_PATH" "$BACKUP_PATH" "$DEPLOY_PROJECT" "$DEPLOY_SERVICE" <<'REMOTE'
set -Eeuo pipefail
deploy_path="$1"
backup_path="$2"
project="$3"
service="$4"
test -d "$backup_path/source"
rsync -a --delete \
  --exclude='.git/' \
  --exclude='.env' \
  --exclude='.deploy.env' \
  --exclude='volumes/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='.pytest_cache/' \
  "$backup_path/source/" "$deploy_path/"
chmod 600 "$deploy_path/.env"
docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" config --quiet
docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" build "$service"
docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" up -d --no-deps --force-recreate "$service"
REMOTE
}

echo "[deploy] syncing release into the live source tree"
if ! rsync -az --no-owner --no-group --delete-delay \
  "${RSYNC_FILTERS[@]}" \
  -e "$RSYNC_SSH" \
  "$ROOT_DIR/" "$DEPLOY_USER@$DEPLOY_HOST:$DEPLOY_PATH/"; then
  rollback
  exit 1
fi

echo "[deploy] recreating API container"
if ! "${SSH[@]}" bash -s -- \
  "$DEPLOY_PATH" "$DEPLOY_PROJECT" "$DEPLOY_SERVICE" <<'REMOTE'
set -Eeuo pipefail
deploy_path="$1"
project="$2"
service="$3"
chmod 600 "$deploy_path/.env"
docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" config --quiet
docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" up -d --no-deps --force-recreate "$service"
REMOTE
then
  rollback
  exit 1
fi

health_check() {
  "${SSH[@]}" bash -s -- \
    "$DEPLOY_HEALTH_URL" "$DEPLOY_HEALTH_RETRIES" "$DEPLOY_HEALTH_INTERVAL" <<'REMOTE'
set -Eeuo pipefail
health_url="$1"
retries="$2"
interval="$3"
for ((attempt = 1; attempt <= retries; attempt++)); do
  if curl -fsS --max-time 5 "$health_url" >/dev/null; then
    curl -fsS --max-time 5 "$health_url"
    echo
    exit 0
  fi
  sleep "$interval"
done
exit 1
REMOTE
}

echo "[deploy] waiting for health check"
if ! health_check; then
  echo "[deploy] new release failed health check" >&2
  rollback
  if health_check; then
    echo "[deploy] rollback is healthy" >&2
  else
    echo "[deploy] rollback completed but health check still fails; manual intervention required" >&2
  fi
  exit 1
fi

echo "[deploy] success release=$RELEASE_ID backup=$BACKUP_PATH"
