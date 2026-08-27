#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PERFORMANCE_AUDIT=0
PERFORMANCE_ARGS=()
PERFORMANCE_ACTION_FILE="$ROOT_DIR/../load-testing/.server-status-action"
if [[ $# -eq 0 && -f "$PERFORMANCE_ACTION_FILE" ]]; then
  IFS=' ' read -r -a PERFORMANCE_ARGS < "$PERFORMANCE_ACTION_FILE"
  PERFORMANCE_AUDIT=1
fi
if [[ "${1:-}" == "--performance-audit" ]]; then
  PERFORMANCE_AUDIT=1
  shift
  PERFORMANCE_ARGS=("$@")
  set --
fi

if [[ -f "$ROOT_DIR/.deploy.env" ]]; then
  # shellcheck source=/dev/null
  source "$ROOT_DIR/.deploy.env"
fi

DEPLOY_HOST="${DEPLOY_HOST:-123.56.218.5}"
DEPLOY_USER="${DEPLOY_USER:-root}"
DEPLOY_PORT="${DEPLOY_PORT:-22}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/play_video/ivapp}"
DEPLOY_PROJECT="${DEPLOY_PROJECT:-ivapp}"
DEPLOY_HEALTH_URL="${DEPLOY_HEALTH_URL:-http://127.0.0.1:8100/health}"
DEPLOY_BACKUP_ROOT="${DEPLOY_BACKUP_ROOT:-/opt/play_video/backups/ivapp}"
DEPLOY_RELEASE_ROOT="${DEPLOY_RELEASE_ROOT:-/opt/play_video/releases/ivapp}"
PERF_ROOT="$ROOT_DIR/../load-testing"
PERF_REMOTE_ROOT="${PIXO_PERF_REMOTE_ROOT:-/opt/play_video/perf-audit/20260827}"

if [[ "$PERFORMANCE_AUDIT" -eq 1 ]]; then
  command_name="${PERFORMANCE_ARGS[0]:-}"
  command_args=("${PERFORMANCE_ARGS[@]:1}")
  SSH=(ssh -p "$DEPLOY_PORT" -o BatchMode=yes "$DEPLOY_USER@$DEPLOY_HOST")
  RSYNC_SSH="ssh -p $DEPLOY_PORT -o BatchMode=yes"

  validate_perf_name() {
    [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$ ]] || {
      echo "Invalid performance artifact name: $1" >&2
      exit 2
    }
  }

  case "$PERF_REMOTE_ROOT" in
    /opt/play_video/perf-audit/*) ;;
    *) echo "Unsafe remote performance root: $PERF_REMOTE_ROOT" >&2; exit 2 ;;
  esac

  case "$command_name" in
    sync)
      [[ ${#command_args[@]} -eq 0 ]] || exit 2
      "${SSH[@]}" install -d -m 700 \
        "$PERF_REMOTE_ROOT" "$PERF_REMOTE_ROOT/scenarios" \
        "$PERF_REMOTE_ROOT/results"
      rsync -az --no-owner --no-group -e "$RSYNC_SSH" \
        "$PERF_ROOT/api_load.py" "$PERF_ROOT/http_load.py" \
        "$PERF_ROOT/server_probe.py" "$PERF_ROOT/perf_fixture.py" \
        "$DEPLOY_USER@$DEPLOY_HOST:$PERF_REMOTE_ROOT/"
      rsync -az --no-owner --no-group -e "$RSYNC_SSH" \
        "$PERF_ROOT/scenarios/audit-20260827/" \
        "$DEPLOY_USER@$DEPLOY_HOST:$PERF_REMOTE_ROOT/scenarios/"
      "${SSH[@]}" chmod 700 \
        "$PERF_REMOTE_ROOT/api_load.py" \
        "$PERF_REMOTE_ROOT/http_load.py" \
        "$PERF_REMOTE_ROOT/server_probe.py" \
        "$PERF_REMOTE_ROOT/perf_fixture.py"
      ;;
    fixture)
      [[ ${#command_args[@]} -eq 1 ]] || exit 2
      fixture_action="${command_args[0]}"
      case "$fixture_action" in
        setup|cleanup|verify) ;;
        *) echo "Invalid fixture action: $fixture_action" >&2; exit 2 ;;
      esac
      "${SSH[@]}" bash -s -- "$PERF_REMOTE_ROOT" "$fixture_action" <<'REMOTE'
set -Eeuo pipefail
remote_root="$1"
fixture_action="$2"
test -f "$remote_root/perf_fixture.py"
if [[ "$fixture_action" == "setup" ]]; then
  token_file="$remote_root/.fixture.env"
  temp_file="$token_file.tmp"
  trap 'rm -f "$temp_file"' EXIT
  docker exec -i ivapp-api-1 python - setup \
    < "$remote_root/perf_fixture.py" > "$temp_file"
  test "$(wc -l < "$temp_file")" -eq 1
  grep -Eq '^PIXO_LOAD_BEARER_TOKEN=[A-Za-z0-9_./+=-]+$' "$temp_file"
  chmod 600 "$temp_file"
  mv "$temp_file" "$token_file"
  trap - EXIT
  echo "fixture=ready token_file_mode=$(stat -c '%a' "$token_file")"
elif [[ "$fixture_action" == "cleanup" ]]; then
  docker exec -i ivapp-api-1 python - cleanup \
    < "$remote_root/perf_fixture.py" > /dev/null
  rm -f "$remote_root/.fixture.env"
  echo "fixture=removed"
else
  docker exec -i ivapp-api-1 python - verify < "$remote_root/perf_fixture.py"
fi
REMOTE
      ;;
    run)
      [[ ${#command_args[@]} -ge 2 ]] || exit 2
      config_name="${command_args[0]}"
      output_prefix="${command_args[1]}"
      validate_perf_name "$config_name"
      validate_perf_name "$output_prefix"
      effect_args=()
      for effect in "${command_args[@]:2}"; do
        case "$effect" in
          idempotent_write|mutation|destructive|external)
            effect_args+=(--allow-effect "$effect")
            ;;
          *) echo "Invalid effect: $effect" >&2; exit 2 ;;
        esac
      done
      remote_argv=("$PERF_REMOTE_ROOT" "$config_name" "$output_prefix")
      if (( ${#effect_args[@]} > 0 )); then
        remote_argv+=("${effect_args[@]}")
      fi
      set +e
      "${SSH[@]}" bash -s -- "${remote_argv[@]}" <<'REMOTE'
set -Eeuo pipefail
remote_root="$1"
config_name="$2"
output_prefix="$3"
shift 3
test -f "$remote_root/scenarios/$config_name.json"
test ! -e "$remote_root/results/$output_prefix.json"
scenario_duration="$(python3 - "$remote_root/scenarios/$config_name.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(float(payload["duration_seconds"]) + 3.0)
PY
)"
python3 "$remote_root/server_probe.py" \
  --duration "$scenario_duration" --interval 1 \
  --output-json "$remote_root/results/$output_prefix-server.json" &
probe_pid="$!"
sleep 1
credential_flags=()
if [[ -f "$remote_root/.fixture.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$remote_root/.fixture.env"
  set +a
  credential_flags+=(--env PIXO_LOAD_BEARER_TOKEN)
fi
export PIXO_LOAD_PUBLISH_KEY="$(docker exec ivapp-api-1 python -c \
  'from app.config import get_settings; print(get_settings().publish_key)')"
credential_flags+=(--env PIXO_LOAD_PUBLISH_KEY)
set +e
docker run --rm --name "pixo-perf-$output_prefix" \
  --network container:ivapp-api-1 --cpus 0.75 --memory 384m \
  --pids-limit 128 --volume "$remote_root:/load" --workdir /load \
  "${credential_flags[@]}" \
  ivapp-api python api_load.py \
  --config "scenarios/$config_name.json" --confirm-host 127.0.0.1 \
  --allow-http --output-dir results --output-prefix "$output_prefix" "$@"
load_exit="$?"
set -e
wait "$probe_pid"
exit "$load_exit"
REMOTE
      run_exit="$?"
      set -e
      mkdir -p "$PERF_ROOT/results/audit-20260827"
      rsync -az --no-owner --no-group -e "$RSYNC_SSH" \
        "$DEPLOY_USER@$DEPLOY_HOST:$PERF_REMOTE_ROOT/results/$output_prefix.json" \
        "$DEPLOY_USER@$DEPLOY_HOST:$PERF_REMOTE_ROOT/results/$output_prefix.md" \
        "$DEPLOY_USER@$DEPLOY_HOST:$PERF_REMOTE_ROOT/results/$output_prefix-server.json" \
        "$PERF_ROOT/results/audit-20260827/"
      exit "$run_exit"
      ;;
    cleanup)
      [[ ${#command_args[@]} -eq 0 ]] || exit 2
      "${SSH[@]}" bash -s <<'REMOTE'
set -Eeuo pipefail
query="SELECT COUNT(*) FROM recommend_cursors WHERE token LIKE 'feed:ssid:perf-public-%' OR token LIKE 'feed:ssid:perf-audit-%'; DELETE FROM recommend_cursors WHERE token LIKE 'feed:ssid:perf-public-%' OR token LIKE 'feed:ssid:perf-audit-%'; SELECT ROW_COUNT();"
docker exec --env "PIXO_PERF_CLEANUP_QUERY=$query" ivapp-mysql-1 \
  sh -c 'exec mysql -N -uroot -p"$MYSQL_ROOT_PASSWORD" "$MYSQL_DATABASE" -e "$PIXO_PERF_CLEANUP_QUERY"'
REMOTE
      ;;
    workers)
      [[ ${#command_args[@]} -eq 0 ]] || exit 2
      "${SSH[@]}" bash -s <<'REMOTE'
set -Eeuo pipefail
workers="$(docker top ivapp-api-1 -eo pid,args | grep -c '[s]pawn_main' || true)"
restarts="$(docker inspect -f '{{.RestartCount}}' ivapp-api-1)"
health="$(docker inspect -f '{{.State.Health.Status}}' ivapp-api-1)"
printf 'workers=%s restarts=%s health=%s\n' "$workers" "$restarts" "$health"
REMOTE
      ;;
    *)
      echo "Usage: $0 --performance-audit {sync|fixture|run|cleanup|workers}" >&2
      exit 2
      ;;
  esac
  exit
fi

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

echo "== API worker summary =="
worker_count="$(docker top "${project}-api-1" -eo pid,args \
  | grep -c '[s]pawn_main' || true)"
restart_count="$(docker inspect -f '{{.RestartCount}}' "${project}-api-1")"
printf 'workers=%s restarts=%s\n' "$worker_count" "$restart_count"

echo "== Sensitive-file permissions =="
stat -c '%a %U:%G %n' "$deploy_path/.env"

echo "== Capacity =="
printf 'vcpus=%s\n' "$(nproc)"
df -h "$deploy_path"
free -h

echo "== Latest retained snapshots =="
find "$backup_root" -mindepth 1 -maxdepth 1 -type d -printf 'backup  %f\n' 2>/dev/null | sort | tail -n 5 || true
find "$release_root" -mindepth 1 -maxdepth 1 -type d -printf 'release %f\n' 2>/dev/null | sort | tail -n 5 || true

if [[ "$log_lines" -gt 0 ]]; then
  echo "== API/Worker logs (last $log_lines lines) =="
  services=(api)
  if docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" config --services \
    | grep -qx worker; then
    services+=(worker)
  fi
  if docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" config --services \
    | grep -qx cdn-worker; then
    services+=(cdn-worker)
  fi
  docker-compose -p "$project" -f "$deploy_path/docker-compose.yml" logs \
    --tail "$log_lines" "${services[@]}"
fi
REMOTE
