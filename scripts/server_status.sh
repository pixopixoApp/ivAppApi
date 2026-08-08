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
DEPLOY_HEALTH_URL="${DEPLOY_HEALTH_URL:-http://127.0.0.1:8100/health}"
DEPLOY_BACKUP_ROOT="${DEPLOY_BACKUP_ROOT:-/opt/play_video/backups/ivapp}"
DEPLOY_RELEASE_ROOT="${DEPLOY_RELEASE_ROOT:-/opt/play_video/releases/ivapp}"

LOG_LINES=0
if [[ "${1:-}" == "--logs" ]]; then
  LOG_LINES="${2:-100}"
  [[ "$LOG_LINES" =~ ^[0-9]+$ ]] || { echo "--logs requires a numeric line count" >&2; exit 2; }
fi

ssh -p "$DEPLOY_PORT" -o BatchMode=yes "$DEPLOY_USER@$DEPLOY_HOST" bash -s -- \
  "$DEPLOY_PATH" "$DEPLOY_PROJECT" "$DEPLOY_HEALTH_URL" "$DEPLOY_BACKUP_ROOT" \
  "$DEPLOY_RELEASE_ROOT" "$LOG_LINES" <<'REMOTE'
set -Eeuo pipefail
deploy_path="$1"
project="$2"
health_url="$3"
backup_root="$4"
release_root="$5"
log_lines="$6"

echo "== Compose services =="
docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" ps

echo "== Health =="
curl -fsS --max-time 5 "$health_url"
echo

echo "== Sensitive-file permissions =="
stat -c '%a %U:%G %n' "$deploy_path/.env"

echo "== Capacity =="
df -h "$deploy_path"
free -h

echo "== Latest retained snapshots =="
find "$backup_root" -mindepth 1 -maxdepth 1 -type d -printf 'backup  %f\n' 2>/dev/null | sort | tail -n 5 || true
find "$release_root" -mindepth 1 -maxdepth 1 -type d -printf 'release %f\n' 2>/dev/null | sort | tail -n 5 || true

if [[ "$log_lines" -gt 0 ]]; then
  echo "== API logs (last $log_lines lines) =="
  docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" logs --tail "$log_lines" api
fi
REMOTE
