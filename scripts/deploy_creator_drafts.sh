#!/usr/bin/env bash
# Narrow additive release: do not ship unrelated dirty feed/web work.
set -Eeuo pipefail
draft_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$draft_root"
draft_release="creator-drafts-$(date -u +%Y%m%dT%H%M%SZ)"
draft_files=(app/creator_drafts.py app/creator_story.py app/models.py
  app/schemas_platform.py app/routers/platform.py
  migrations/versions/20260913_0021_creator_drafts.py)
draft_ssh=(ssh -o BatchMode=yes -o ConnectTimeout=15 root@8.221.106.221)

echo '[drafts deploy] validating production and backing up affected source/database'
"${draft_ssh[@]}" bash -s -- "$draft_release" "${draft_files[@]}" <<'REMOTE'
set -Eeuo pipefail
release="$1"; shift
cd /opt/play_video/ivapp
grep -qx 'PIXO_ENVIRONMENT=production' .env.target
test -r /root/.config/pixo/ivapp.cnf
test -x scripts/compose_target.sh
backup="/opt/play_video/backups/ivapp/$release"
install -d -m 700 "$backup/source"
for file in "$@"; do
  if [[ -f "$file" ]]; then cp --parents "$file" "$backup/source/"; fi
done
mysqldump --defaults-extra-file=/root/.config/pixo/ivapp.cnf \
  --single-transaction --skip-lock-tables --skip-add-locks \
  --set-gtid-purged=OFF --no-tablespaces --quick ivapp \
  | gzip -9 > "$backup/database.sql.gz"
test -s "$backup/database.sql.gz"
chmod 600 "$backup/database.sql.gz"
echo "[drafts deploy] backup=$backup"
scripts/compose_target.sh /opt/play_video/ivapp ivapp production stop worker
REMOTE

rollback_drafts() {
  echo '[drafts deploy] restoring previous affected source' >&2
  "${draft_ssh[@]}" bash -s -- "$draft_release" <<'REMOTE'
set -Eeuo pipefail
backup="/opt/play_video/backups/ivapp/$1/source"
test -d "$backup"
rsync -a "$backup/" /opt/play_video/ivapp/
cd /opt/play_video/ivapp
scripts/compose_target.sh /opt/play_video/ivapp ivapp production restart api
scripts/compose_target.sh /opt/play_video/ivapp ivapp production start worker
# The additive index and new migration/module may safely remain for compatibility.
REMOTE
}
trap rollback_drafts ERR
rsync -azR --no-owner --no-group -e 'ssh -o BatchMode=yes' \
  "${draft_files[@]}" root@8.221.106.221:/opt/play_video/ivapp/

"${draft_ssh[@]}" bash -s <<'REMOTE'
set -Eeuo pipefail
cd /opt/play_video/ivapp
# The existing image bind-mounts app, but not migrations. Mount the host migration
# directory explicitly; no image rebuild or unrelated source replacement needed.
scripts/compose_target.sh /opt/play_video/ivapp ivapp production run \
  --rm --no-deps -v /opt/play_video/ivapp/migrations:/app/migrations:ro \
  api alembic upgrade head </dev/null
scripts/compose_target.sh /opt/play_video/ivapp ivapp production restart api
scripts/compose_target.sh /opt/play_video/ivapp ivapp production start worker
for attempt in {1..20}; do
  if curl -fsS http://127.0.0.1:8100/health >/dev/null; then
    break
  fi
  sleep 2
done
curl -fsS http://127.0.0.1:8100/health
docker exec ivapp-api-1 python -c \
  'from app.main import app; assert "/api/v1/creator/drafts" in app.openapi()["paths"]; print("Draft route registered")'
REMOTE
trap - ERR
echo '[drafts deploy] production ready; old active-creation API retained'
