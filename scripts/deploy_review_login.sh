#!/usr/bin/env bash
# Account-scoped release; never upload unrelated local feed/web/config changes.
set -Eeuo pipefail
review_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$review_root"
review_identity="$review_root/../output/pixo-store-review/login.json"
test -r "$review_identity"
review_release="store-review-login-$(date -u +%Y%m%dT%H%M%SZ)"
review_remote="/opt/play_video/releases/ivapp/$review_release"
review_ssh=(ssh -o BatchMode=yes -o ConnectTimeout=15 root@8.221.106.221)
review_files=(app/review_login.py app/verification_codes.py
  scripts/provision_store_review.py)

"${review_ssh[@]}" bash -s -- "$review_release" <<'REMOTE'
set -Eeuo pipefail
cd /opt/play_video/ivapp
grep -qx 'PIXO_ENVIRONMENT=production' .env.target
test -x scripts/compose_target.sh
expected=d8f1fa849aaa1df7e92cf3d7c19bb8e4355875f615f000c580bc50f0ac5fb914
test "$(sha256sum app/verification_codes.py | cut -d ' ' -f 1)" = "$expected"
test ! -e app/review_login.py
test ! -e scripts/provision_store_review.py
if grep -q '^APP_REVIEW_LOGIN_' .env; then
  echo 'An existing review login configuration requires inspection.' >&2
  exit 1
fi
backup="/opt/play_video/backups/ivapp/$1"
install -d -m 700 "$backup" "/opt/play_video/releases/ivapp/$1"
cp .env "$backup/env.before"
cp app/verification_codes.py "$backup/verification_codes.py"
chmod 600 "$backup/env.before"
echo "review_backup=$backup"
REMOTE

rsync -azR --no-owner --no-group -e 'ssh -o BatchMode=yes' \
  "${review_files[@]}" "root@8.221.106.221:$review_remote/"
scp -q "$review_identity" "root@8.221.106.221:$review_remote/login.json"

"${review_ssh[@]}" bash -s -- "$review_release" <<'REMOTE'
set -Eeuo pipefail
release="$1"
cd /opt/play_video/ivapp
stage="/opt/play_video/releases/ivapp/$release"
backup="/opt/play_video/backups/ivapp/$release"
chmod 600 "$stage/login.json"
rollback_review() {
  cp "$backup/env.before" .env
  cp "$backup/verification_codes.py" app/verification_codes.py
  rm -f app/review_login.py scripts/provision_store_review.py
  scripts/compose_target.sh /opt/play_video/ivapp ivapp production \
    up -d --no-deps --force-recreate api
  echo 'Review login source/configuration rolled back.' >&2
}
trap rollback_review ERR
cp "$stage/app/review_login.py" app/review_login.py
cp "$stage/scripts/provision_store_review.py" scripts/provision_store_review.py
# Existing code is still live during provisioning. No code or tokens are printed.
docker exec -i ivapp-api-1 python -m scripts.provision_store_review \
  < "$stage/login.json"
cp "$stage/app/verification_codes.py" app/verification_codes.py
python3 - "$stage/login.json" <<'PY'
import json
import os
import re
import sys
from pathlib import Path
identity = json.loads(Path(sys.argv[1]).read_text())
assert identity['email'] == 'app-review@pixopixo.com'
encoded = identity['code_hash']
assert re.fullmatch(r'pbkdf2_sha256\$200000\$[0-9a-f]{32}\$[0-9a-f]{64}', encoded)
path = Path('.env')
updated = path.read_text().rstrip() + '\n\n# Dedicated application-store review login\n'
updated += "APP_REVIEW_LOGIN_ENABLED=true\nAPP_REVIEW_LOGIN_EMAIL=app-review@pixopixo.com\n"
updated += "APP_REVIEW_LOGIN_CODE_HASH='" + encoded + "'\n"
temp = path.with_name('.env.review-next')
temp.write_text(updated)
os.chmod(temp, path.stat().st_mode & 0o777)
temp.replace(path)
PY
scripts/compose_target.sh /opt/play_video/ivapp ivapp production \
  up -d --no-deps --force-recreate api
docker exec ivapp-api-1 python -c \
  'from app.review_login import get_review_login_settings; s=get_review_login_settings(); assert s.enabled and s.email=="app-review@pixopixo.com"; assert len(s.code_hash.split("$"))==4; print("review_configuration=ready")'
for attempt in {1..20}; do
  if curl -fsS --max-time 3 http://127.0.0.1:8100/health >/dev/null; then
    break
  fi
  sleep 1
done
curl -fsS --max-time 5 http://127.0.0.1:8100/health
trap - ERR
echo 'Production review login configured.'
REMOTE
