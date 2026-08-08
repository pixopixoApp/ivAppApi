#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CACHE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ivapp-check.XXXXXX")"

cleanup() {
  rm -rf -- "$CACHE_DIR"
}
trap cleanup EXIT

echo "[check] Python syntax"
PYTHONPYCACHEPREFIX="$CACHE_DIR/pycache" python3 -m compileall -q "$ROOT_DIR/app"

echo "[check] Shell syntax"
for script in "$ROOT_DIR"/scripts/*.sh; do
  bash -n "$script"
done

if command -v docker-compose >/dev/null 2>&1; then
  echo "[check] Docker Compose configuration"
  cp "$ROOT_DIR/docker-compose.yml" "$CACHE_DIR/docker-compose.yml"
  cp "$ROOT_DIR/.env.example" "$CACHE_DIR/.env"
  docker-compose -f "$CACHE_DIR/docker-compose.yml" config --quiet
else
  echo "[check] docker-compose not installed locally; Compose validation skipped"
fi

if [[ -d "$ROOT_DIR/tests" ]] && python3 -c 'import pytest' >/dev/null 2>&1; then
  echo "[check] Pytest"
  python3 -m pytest -q "$ROOT_DIR/tests"
else
  echo "[check] no runnable pytest suite found"
fi

echo "[check] OK"
