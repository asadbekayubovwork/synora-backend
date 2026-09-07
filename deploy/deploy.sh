#!/usr/bin/env bash
#
# Release back.synora-ai.uz.
#
# Runs as the `deploy` user, both by hand and from GitHub Actions, so the two
# paths cannot drift. /opt/synora-backend is itself the git checkout systemd
# runs, so a release is: fetch, reset, install requirements, restart, health
# check — and if the health check fails, the previous commit is put back
# before the script gives up.
#
#   synora-api-deploy              # deploy origin/main
#   DEPLOY_REF=<sha> synora-api-deploy
#
set -euo pipefail

BASE=/opt/synora-backend
REPO_URL=https://github.com/asadbekayubovwork/synora-backend.git
BRANCH=${DEPLOY_BRANCH:-main}
SERVICE=synora-api
# Loopback only; nginx is the sole way in from outside.
HEALTH_URL=http://127.0.0.1:8010/health
PIP=$BASE/.venv/bin/pip
PYTHON=$BASE/.venv/bin/python

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# Poll the health endpoint until it answers, so we never race a slow boot.
wait_for_http() {
  local url=$1 tries=${2:-40}
  for _ in $(seq 1 "$tries"); do
    if curl -fs -o /dev/null --max-time 3 "$url" 2>/dev/null; then return 0; fi
    sleep 0.5
  done
  return 1
}

# Bring the service onto whatever is checked out right now. Used for the
# release and, unchanged, for the rollback — so a rollback cannot be the path
# that was never tested.
activate() {
  "$PIP" install -q -r "$BASE/requirements.txt"
  sudo /usr/bin/systemctl restart "$SERVICE"
}

[ -d "$BASE/.git" ] || die "$BASE is not a git checkout — run deploy/bootstrap.sh on the server first"

cd "$BASE"

# ---------------------------------------------------------------- fetch source
log "Fetching origin/$BRANCH"
git remote set-url origin "$REPO_URL"
git fetch --prune origin "$BRANCH"

PREVIOUS=$(git rev-parse HEAD)
TARGET_REF=${DEPLOY_REF:-origin/$BRANCH}
git reset --hard "$TARGET_REF"
# -fd, deliberately never -fdx: .env, data/ and .venv are all gitignored, and
# -x would delete the production secret and the database along with the junk.
git clean -fd -e .env -e data -e .venv

SHA=$(git rev-parse --short HEAD)
log "Deploying $SHA — $(git log -1 --pretty=%s)"

# Cheap gate before the running service is touched. It catches a syntax error
# but not an import-time one: settings come from .env, which only `synora` reads.
"$PYTHON" -m compileall -q app || die "app/ does not compile; live service left untouched"

# --------------------------------------------------------- install and restart
log "Installing requirements and restarting $SERVICE"
activate

if wait_for_http "$HEALTH_URL"; then
  log "Live: $SHA"
else
  log "Health check failed — rolling back to $(git rev-parse --short "$PREVIOUS")"
  git reset --hard "$PREVIOUS"
  activate
  wait_for_http "$HEALTH_URL" && log "Rolled back" || log "Rollback also unhealthy"
  sudo /usr/bin/journalctl -u "$SERVICE" -n 40 --no-pager || true
  die "$SERVICE did not answer $HEALTH_URL"
fi

# This script is installed outside the checkout, so it can be replaced while it
# runs. Say so rather than silently deploying from a stale copy.
if [ -f "$BASE/deploy/deploy.sh" ] && ! cmp -s "$BASE/deploy/deploy.sh" "$0"; then
  log "NOTE: $0 is out of date — re-run deploy/bootstrap.sh as root to update it"
fi

log "Done — https://back.synora-ai.uz ($SHA)"
