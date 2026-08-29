#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -lt 3 ]]; then
  echo 'usage: compose_target.sh ROOT PROJECT COMPOSE_ARGS...' >&2
  exit 2
fi

root_dir="$1"
project="$2"
shift 2

test -f "$root_dir/.env"
test -f "$root_dir/docker-compose.yml"

compose=(
  docker compose
  --env-file "$root_dir/.env"
  -p "$project"
  -f "$root_dir/docker-compose.yml"
)
if [[ -f "$root_dir/.env.target" ]]; then
  compose=(
    docker compose
    --env-file "$root_dir/.env"
    --env-file "$root_dir/.env.target"
    -p "$project"
    -f "$root_dir/docker-compose.yml"
  )
fi
if [[ -f "$root_dir/docker-compose.rds.yml" ]]; then
  compose+=(-f "$root_dir/docker-compose.rds.yml")
  compose+=(--profile background-workers)
fi

exec "${compose[@]}" "$@"
