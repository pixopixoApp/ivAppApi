#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -lt 4 ]]; then
  echo 'usage: compose_target.sh ROOT PROJECT ENVIRONMENT COMPOSE_ARGS...' >&2
  exit 2
fi

root_dir="$1"
project="$2"
environment="$3"
shift 3

test -f "$root_dir/.env"
test -f "$root_dir/docker-compose.yml"

compose=(
  docker compose
  --env-file "$root_dir/.env"
  -p "$project"
  -f "$root_dir/docker-compose.yml"
)
case "$environment" in
  development)
    if [[ -e "$root_dir/.env.target" ]]; then
      echo 'development compose refuses .env.target' >&2
      exit 1
    fi
    ;;
  production)
    test -f "$root_dir/.env.target"
    test -f "$root_dir/docker-compose.rds.yml"
    compose=(
      docker compose
      --env-file "$root_dir/.env"
      --env-file "$root_dir/.env.target"
      -p "$project"
      -f "$root_dir/docker-compose.yml"
      -f "$root_dir/docker-compose.rds.yml"
      --profile background-workers
    )
    ;;
  *)
    echo "invalid compose environment: $environment" >&2
    exit 2
    ;;
esac

exec "${compose[@]}" "$@"
