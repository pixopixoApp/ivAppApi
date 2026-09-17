#!/usr/bin/env bash
# Narrow release for the creator interaction catalog. No migrations are required.
set -Eeuo pipefail

catalog_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$catalog_root"
catalog_release="interaction-catalog-$(date -u +%Y%m%dT%H%M%SZ)"
catalog_files=(
  app/vision_targets.py
  app/creator_interaction_presets.py
  app/creator_manual_edits.py
  app/creator_story.py
  app/routers/platform.py
  app/schemas_platform.py
)
catalog_ssh=(ssh -o BatchMode=yes -o ConnectTimeout=15 root@8.221.106.221)

for file in "${catalog_files[@]}"; do
  test -f "$file"
done

echo '[interaction catalog] backing up production source and database'
"${catalog_ssh[@]}" bash -s -- "$catalog_release" "${catalog_files[@]}" <<'REMOTE'
set -Eeuo pipefail
release="$1"
shift
cd /opt/play_video/ivapp
grep -qx 'PIXO_ENVIRONMENT=production' .env.target
test -r /root/.config/pixo/ivapp.cnf
test -x scripts/compose_target.sh
backup="/opt/play_video/backups/ivapp/$release"
install -d -m 700 "$backup/source"
: > "$backup/missing-files.txt"
for file in "$@"; do
  if [[ -f "$file" ]]; then
    cp --parents "$file" "$backup/source/"
  else
    printf '%s\n' "$file" >> "$backup/missing-files.txt"
  fi
done
mysqldump --defaults-extra-file=/root/.config/pixo/ivapp.cnf \
  --single-transaction --skip-lock-tables --skip-add-locks \
  --set-gtid-purged=OFF --no-tablespaces --quick ivapp \
  | gzip -9 > "$backup/database.sql.gz"
gzip -t "$backup/database.sql.gz"
test -s "$backup/database.sql.gz"
chmod 600 "$backup/database.sql.gz" "$backup/missing-files.txt"
echo "[interaction catalog] backup=$backup"
REMOTE

rollback_catalog() {
  echo '[interaction catalog] restoring previous source' >&2
  "${catalog_ssh[@]}" bash -s -- "$catalog_release" <<'REMOTE'
set -Eeuo pipefail
backup="/opt/play_video/backups/ivapp/$1"
test -d "$backup/source"
rsync -a "$backup/source/" /opt/play_video/ivapp/
while IFS= read -r file; do
  [[ -z "$file" ]] || rm -f "/opt/play_video/ivapp/$file"
done < "$backup/missing-files.txt"
cd /opt/play_video/ivapp
scripts/compose_target.sh /opt/play_video/ivapp ivapp production restart api worker
REMOTE
}

trap rollback_catalog ERR
rsync -azR --no-owner --no-group -e 'ssh -o BatchMode=yes' \
  "${catalog_files[@]}" root@8.221.106.221:/opt/play_video/ivapp/

"${catalog_ssh[@]}" bash -s -- "$catalog_release" <<'REMOTE'
set -Eeuo pipefail
release="$1"
cd /opt/play_video/ivapp
scripts/compose_target.sh /opt/play_video/ivapp ivapp production restart api worker
for attempt in {1..30}; do
  if curl -fsS http://127.0.0.1:8100/health >/dev/null; then
    break
  fi
  sleep 2
done
curl -fsS http://127.0.0.1:8100/health
docker exec -i ivapp-api-1 python - <<'PY'
from app.creator_interaction_presets import creator_interaction_presets
from app.vision_targets import creator_vision_config

presets = creator_interaction_presets()
by_type = {}
for preset in presets:
    by_type.setdefault(preset.type, []).append(preset)
sustained = {preset.id for preset in presets if preset.lifecycle == "sustained"}
assert len(presets) == 53
assert len({preset.id for preset in presets}) == 53
assert len(by_type) == 35
assert len(by_type["camera_motion"]) == 16
assert sum(preset.variant_group == "hand" for preset in by_type["camera_motion"]) == 7
assert sum(preset.variant_group == "face" for preset in by_type["camera_motion"]) == 9
assert {preset.id for preset in by_type["pinch"]} == {"pinch_in", "pinch_out"}
assert {preset.id for preset in by_type["rotate"]} == {
    "rotate_clockwise", "rotate_counterclockwise",
}
assert sustained == {
    "continuous_tap", "continuous_swipe",
    "camera_continuous.hand_finger_snap",
    "camera_continuous.hand_finger_gun_recoil",
    "mic_level_continuous", "mic_blow_continuous",
}
assert creator_vision_config("hand_thumb_up") == {
    "registry_version": "v1",
    "target": "hand_thumb_up",
    "camera_facing": "front",
    "show_preview": True,
    "min_confidence": 0.60,
    "stable_for_ms": 250,
}
print("interaction catalog v2 verified: 53 presets, 35 types, 6 continuous")
PY
docker exec ivapp-worker-1 python -c \
  'from app.creator_interaction_presets import creator_interaction_presets; assert len(creator_interaction_presets()) == 53'
printf '%s\n' "$release" > "/opt/play_video/backups/ivapp/$release/deployed.txt"
REMOTE
trap - ERR
echo '[interaction catalog] production ready'
