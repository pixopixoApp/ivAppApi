#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:---dry-run}"

case "$MODE" in
  --dry-run)
    docker-compose -f "$ROOT_DIR/docker-compose.yml" run --rm --no-deps api \
      python -m app.runtime_backfill
    ;;
  --apply)
    docker-compose -f "$ROOT_DIR/docker-compose.yml" run --rm --no-deps api \
      python -m app.runtime_backfill --apply
    ;;
  *)
    echo "Usage: $0 [--dry-run|--apply]" >&2
    exit 2
    ;;
esac
