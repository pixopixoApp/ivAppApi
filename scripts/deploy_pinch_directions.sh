#!/usr/bin/env bash
# Narrow deployment: update only Pinch-related code in both APIs/admin.
set -Eeuo pipefail
pinch_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pinch_admin="$pinch_root/../ivadmin-api"
pinch_web="$pinch_root/../ivadmin-web"
pinch_release="pinch-directions-$(date -u +%Y%m%dT%H%M%SZ)"
pinch_ssh=(ssh -o BatchMode=yes -o ConnectTimeout=15 root@8.221.106.221)
pinch_app_files=(app/protocol_video.py app/schemas.py app/schemas_platform.py
  app/creator_manual_edits.py app/creator_story.py app/routers/platform.py
  app/pinch_backfill.py)
pinch_admin_files=(backend/app/services/protocol_video.py
  backend/app/services/manual_annotate.py backend/app/services/story.py
  backend/app/services/player_timeline.py ivcore/ivcore/models.py
  ivcore/ivcore/domain/gameplay.py ivcore/ivcore/local_workflow.py)
pinch_web_files=(admin/src/types/interaction.ts admin/src/types/run.ts
  admin/src/pages/AnnotatePage.tsx admin/src/pages/StoryEditPage.tsx
  admin/src/components/story-edit/ClipEditor.tsx
  admin/src/components/PreviewPlayer.tsx admin/src/components/PinchDirectionFields.tsx
  player/player.js player/player-core.js)

"${pinch_ssh[@]}" bash -s -- "$pinch_release" <<'REMOTE'
set -Eeuo pipefail
release="$1"
for role in ivapp ivadmin; do
  root="/opt/play_video/$role"
  grep -qx 'PIXO_ENVIRONMENT=production' "$root/.env.target"
  test -r "/root/.config/pixo/$role.cnf"
  backup="/opt/play_video/backups/$role/$release"
  install -d -m 700 "$backup"
  # Full source backup is read-only; deployment below remains file-scoped.
  tar -czf "$backup/source.tar.gz" -C "$root" app 2>/dev/null || {
    test "$role" = ivadmin
    tar -czf "$backup/source.tar.gz" -C "$root" backend/app ivcore admin/src player
  }
  mysqldump --defaults-extra-file="/root/.config/pixo/$role.cnf" \
    --single-transaction --skip-lock-tables --skip-add-locks \
    --set-gtid-purged=OFF --no-tablespaces --quick "$role" \
    | gzip -9 > "$backup/database.sql.gz"
  gzip -t "$backup/database.sql.gz"
  test -s "$backup/database.sql.gz"
  chmod 600 "$backup/"*.gz
  echo "backup=$backup"
done
REMOTE

cd "$pinch_root"
rsync -azR --no-owner --no-group -e 'ssh -o BatchMode=yes' \
  "${pinch_app_files[@]}" root@8.221.106.221:/opt/play_video/ivapp/
cd "$pinch_admin"
rsync -azR --no-owner --no-group -e 'ssh -o BatchMode=yes' \
  "${pinch_admin_files[@]}" root@8.221.106.221:/opt/play_video/ivadmin/
cd "$pinch_web"
rsync -azR --no-owner --no-group -e 'ssh -o BatchMode=yes' \
  "${pinch_web_files[@]}" root@8.221.106.221:/opt/play_video/ivadmin/

"${pinch_ssh[@]}" bash -s -- "$pinch_release" <<'REMOTE'
set -Eeuo pipefail
release="$1"
docker cp /opt/play_video/ivapp/app/pinch_backfill.py ivadmin-api-1:/tmp/pixo_pinch_backfill.py
for role in ivapp ivadmin; do
  module=app.pinch_backfill
  if [[ "$role" = ivadmin ]]; then module=pixo_pinch_backfill; fi
  docker exec "$role-api-1" python -c \
    "import sys,json;sys.path.insert(0,'/tmp');from $module import backfill;from app.db import engine;print(json.dumps(backfill(engine,apply=True,expected_database='$role')))" \
    | tee "/opt/play_video/backups/$role/$release/pinch-apply.json"
  docker exec "$role-api-1" python -c \
    "import sys,json;sys.path.insert(0,'/tmp');from $module import backfill;from app.db import engine;r=backfill(engine,expected_database='$role');print(json.dumps(r));assert r['added']==0 and not r['invalid']" \
    | tee "/opt/play_video/backups/$role/$release/pinch-verify.json"
done
cd /opt/play_video/ivapp
scripts/compose_target.sh /opt/play_video/ivapp ivapp production restart api worker
cd /opt/play_video/ivadmin
scripts/compose_target.sh /opt/play_video/ivadmin ivadmin production restart api
scripts/compose_target.sh /opt/play_video/ivadmin ivadmin production build web
scripts/compose_target.sh /opt/play_video/ivadmin ivadmin production up -d --no-deps web
for attempt in {1..30}; do
  if curl -fsS http://127.0.0.1:8100/health >/dev/null \
    && curl -fsS http://127.0.0.1:8000/api/health >/dev/null \
    && curl -fsS http://127.0.0.1:8090/ >/dev/null; then break; fi
  sleep 2
done
curl -fsS http://127.0.0.1:8100/health
curl -fsS http://127.0.0.1:8000/api/health
curl -fsS http://127.0.0.1:8090/ >/dev/null
echo "Pinch APIs/admin healthy; audit=$release"
REMOTE
