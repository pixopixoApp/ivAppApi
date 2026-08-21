#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$ROOT_DIR/.deploy.env" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT_DIR/.deploy.env"
fi

DEPLOY_HOST="${DEPLOY_HOST:-123.56.218.5}"
DEPLOY_USER="${DEPLOY_USER:-root}"
DEPLOY_PORT="${DEPLOY_PORT:-22}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/play_video/ivapp}"
DEPLOY_PROJECT="${DEPLOY_PROJECT:-ivapp}"
DEPLOY_SERVICE="${DEPLOY_SERVICE:-api}"
DEPLOY_WORKER_SERVICE="${DEPLOY_WORKER_SERVICE:-worker}"
DEPLOY_CDN_WORKER_SERVICE="${DEPLOY_CDN_WORKER_SERVICE:-cdn-worker}"
DEPLOY_BACKUP_ROOT="${DEPLOY_BACKUP_ROOT:-/opt/play_video/backups/ivapp}"
DEPLOY_HEALTH_URL="${DEPLOY_HEALTH_URL:-http://127.0.0.1:8100/health}"

usage() {
  cat <<'USAGE'
Usage:
  scripts/rollback.sh --list
  scripts/rollback.sh <backup-id> [--yes]

Rollback restores source/configuration only. Remote .env and volumes are never
replaced. The API image is rebuilt from the selected snapshot before restart.
USAGE
}

if [[ "${1:-}" == "--list" ]]; then
  ssh -p "$DEPLOY_PORT" -o BatchMode=yes "$DEPLOY_USER@$DEPLOY_HOST" \
    find "$DEPLOY_BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' 2>/dev/null | sort
  exit 0
fi

BACKUP_ID="${1:-}"
[[ -n "$BACKUP_ID" ]] || { usage >&2; exit 2; }
[[ "$BACKUP_ID" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Invalid backup id" >&2; exit 2; }

ASSUME_YES=0
if [[ "${2:-}" == "--yes" ]]; then
  ASSUME_YES=1
elif [[ -n "${2:-}" ]]; then
  usage >&2
  exit 2
fi

if [[ "$ASSUME_YES" -eq 0 ]]; then
  read -r -p "Rollback $DEPLOY_HOST to $BACKUP_ID? [y/N] " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]] || { echo "Cancelled"; exit 1; }
fi

BACKUP_PATH="$DEPLOY_BACKUP_ROOT/$BACKUP_ID"
ssh -p "$DEPLOY_PORT" -o BatchMode=yes "$DEPLOY_USER@$DEPLOY_HOST" bash -s -- \
  "$DEPLOY_PATH" "$BACKUP_PATH" "$DEPLOY_PROJECT" "$DEPLOY_SERVICE" \
  "$DEPLOY_WORKER_SERVICE" "$DEPLOY_CDN_WORKER_SERVICE" "$DEPLOY_HEALTH_URL" <<'REMOTE'
set -Eeuo pipefail
deploy_path="$1"
backup_path="$2"
project="$3"
service="$4"
worker_service="$5"
cdn_worker_service="$6"
health_url="$7"

case "$deploy_path" in
  /opt/*) ;;
  *) echo "Unsafe deployment path: $deploy_path" >&2; exit 2 ;;
esac
test -d "$backup_path/source"
test -f "$backup_path/source/docker-compose.yml"
test -f "$deploy_path/.env"

if docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" config --services \
  | grep -qx "$worker_service"; then
  docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" stop "$worker_service"
fi
if docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" config --services \
  | grep -qx "$cdn_worker_service"; then
  docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" stop "$cdn_worker_service"
fi

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

for ((attempt = 1; attempt <= 20; attempt++)); do
  if curl -fsS --max-time 5 "$health_url" >/dev/null; then
    if docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" config --services \
      | grep -qx "$worker_service"; then
      docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" build "$worker_service"
      docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" up -d \
        --no-deps --force-recreate "$worker_service"
    fi
    if docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" config --services \
      | grep -qx "$cdn_worker_service"; then
      docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" build "$cdn_worker_service"
      docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" up -d \
        --no-deps --force-recreate "$cdn_worker_service"
    fi
    curl -fsS --max-time 5 "$health_url"
    echo
    echo "Rollback succeeded: $backup_path"
    exit 0
  fi
  sleep 2
done

echo "Rollback source was applied, but health check failed" >&2
exit 1
REMOTE
