#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
CACHE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ivapp-check.XXXXXX")"
PYTHON_BIN="python3"
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/.venv/bin/python"
fi

cleanup() {
  rm -rf -- "$CACHE_DIR"
}
trap cleanup EXIT

echo "[check] Python syntax"
PYTHONPYCACHEPREFIX="$CACHE_DIR/pycache" "$PYTHON_BIN" -m compileall -q \
  "$ROOT_DIR/app" "$ROOT_DIR/migrations" "$ROOT_DIR/scripts"

if command -v node >/dev/null 2>&1; then
  echo "[check] HTML Host SDK contract"
  node "$ROOT_DIR/tests/js/pixo_html_host_sdk_test.js"
  node "$ROOT_DIR/tests/js/pixo_html_browser_compat_test.js"
fi

echo "[check] Shell syntax"
for script in "$ROOT_DIR"/scripts/*.sh; do
  bash -n "$script"
done

echo "[check] Alembic fresh-schema migration"
DATABASE_URL="sqlite:///$CACHE_DIR/alembic.db" \
PUBLISH_KEY="check-only" \
CURSOR_SECRET="check-only" \
  "$PYTHON_BIN" -m alembic -c "$ROOT_DIR/alembic.ini" upgrade head

if command -v docker-compose >/dev/null 2>&1; then
  echo "[check] Docker Compose configuration"
  cp "$ROOT_DIR/docker-compose.yml" "$CACHE_DIR/docker-compose.yml"
  cp "$ROOT_DIR/docker-compose.media-transition.yml" "$CACHE_DIR/docker-compose.media-transition.yml"
  cp "$ROOT_DIR/docker-compose.media-fallback.yml" "$CACHE_DIR/docker-compose.media-fallback.yml"
  cp "$ROOT_DIR/docker-compose.media-migration.yml" "$CACHE_DIR/docker-compose.media-migration.yml"
  cp "$ROOT_DIR/.env.example" "$CACHE_DIR/.env"
  docker-compose -f "$CACHE_DIR/docker-compose.yml" config --quiet
  docker-compose -f "$CACHE_DIR/docker-compose.yml" \
    -f "$CACHE_DIR/docker-compose.media-transition.yml" config --quiet
  docker-compose -f "$CACHE_DIR/docker-compose.yml" \
    -f "$CACHE_DIR/docker-compose.media-fallback.yml" config --quiet
  docker-compose -f "$CACHE_DIR/docker-compose.yml" \
    -f "$CACHE_DIR/docker-compose.media-migration.yml" config --quiet
else
  echo "[check] docker-compose not installed locally; Compose validation skipped"
fi

if [[ -d "$ROOT_DIR/tests" ]] && "$PYTHON_BIN" -c 'import pytest' >/dev/null 2>&1; then
  echo "[check] Pytest"
  "$PYTHON_BIN" -m pytest -q "$ROOT_DIR/tests"
else
  echo "[check] no runnable pytest suite found"
fi

if [[ -x "$ROOT_DIR/.venv/bin/ruff" ]]; then
  echo "[check] Ruff"
  "$ROOT_DIR/.venv/bin/ruff" check \
    "$ROOT_DIR/app" "$ROOT_DIR/tests" "$ROOT_DIR/migrations" "$ROOT_DIR/scripts"
fi

echo "[check] OK"
