#!/usr/bin/env bash
#
# One-time server setup for push-to-deploy. Run as root on 169.58.183.151:
#
#   scp -r deploy root@169.58.183.151:/tmp/synora-deploy
#   ssh root@169.58.183.151 bash /tmp/synora-deploy/bootstrap.sh
#
# Turns /opt/synora-backend into a checkout of the repo, hands the code to the
# `deploy` user, and installs the release script and the one privileged command
# it needs. Idempotent — re-run it after changing deploy.sh or the unit file.
#
set -euo pipefail

BASE=/opt/synora-backend
REPO_URL=https://github.com/asadbekayubovwork/synora-backend.git
BRANCH=main
SERVICE=synora-api
APP_USER=synora
DEPLOY_USER=deploy
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# git refuses to work in a directory owned by someone else, so every git command
# here runs as the user that will own the checkout.
as_deploy() { sudo -u "$DEPLOY_USER" -H "$@"; }

[ "$(id -u)" -eq 0 ] || die "run this as root"
[ -d "$BASE" ] || die "$BASE does not exist — deploy the app by hand once first"
id "$DEPLOY_USER" >/dev/null 2>&1 || die "user $DEPLOY_USER does not exist"
id "$APP_USER"    >/dev/null 2>&1 || die "user $APP_USER does not exist"

# ------------------------------------------------- back up the irreplaceable
# Everything else here can be re-fetched from git; these two cannot. .env holds
# the production JWT_SECRET, and data/ is every account.
BACKUP=/root/synora-backend-backup-$(date -u +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP"
if [ -f "$BASE/.env" ]; then cp -a "$BASE/.env"  "$BACKUP/"; fi
if [ -d "$BASE/data" ]; then cp -a "$BASE/data" "$BACKUP/"; fi
log "Backed up .env and data/ to $BACKUP"

# ------------------------------------------------------------- ownership
# `deploy` owns the code and the venv so a release needs no root. `synora` runs
# the service and owns only what it must write: the database, plus the secret it
# reads. The unit is ProtectSystem=strict, so the code stays read-only to it
# either way.
chown -R "$DEPLOY_USER:$DEPLOY_USER" "$BASE"
mkdir -p "$BASE/data"
chown -R "$APP_USER:$APP_USER" "$BASE/data"
chmod 750 "$BASE/data"
if [ -f "$BASE/.env" ]; then
  chown "$APP_USER:$APP_USER" "$BASE/.env"
  chmod 600 "$BASE/.env"
fi

# --------------------------------------------------- make it a git checkout
if [ ! -d "$BASE/.git" ]; then
  log "Turning $BASE into a checkout of $REPO_URL"
  as_deploy git init -q -b "$BRANCH" "$BASE"
fi
# Set separately from init: a half-finished earlier run can leave a .git with no
# remote, and re-running this script has to be able to finish the job.
if as_deploy git -C "$BASE" remote get-url origin >/dev/null 2>&1; then
  as_deploy git -C "$BASE" remote set-url origin "$REPO_URL"
else
  as_deploy git -C "$BASE" remote add origin "$REPO_URL"
fi
as_deploy git -C "$BASE" fetch --prune origin "$BRANCH"
# The files already there were deployed from this repo, so this overwrites like
# with like. .env, data/ and .venv are gitignored and are left alone.
as_deploy git -C "$BASE" reset --hard "origin/$BRANCH"
log "Checked out $(as_deploy git -C "$BASE" rev-parse --short HEAD)"

# -------------------------------------------- release script and its one sudo
install -m 755 "$HERE/deploy.sh" /usr/local/sbin/synora-api-deploy
log "Installed /usr/local/sbin/synora-api-deploy"

# The second rule is what lets a release migrate. Alembic has to run as
# `synora` because DATABASE_URL is in .env, which is 0600 synora:synora — the
# whole point of splitting the two accounts is that `deploy` cannot read the
# production secret. Pinned to the exact command, so this grants the right to
# run one migration and nothing else.
cat > /etc/sudoers.d/deploy-synora-api <<EOF
$DEPLOY_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart $SERVICE, /usr/bin/systemctl status $SERVICE, /usr/bin/systemctl is-active $SERVICE, /usr/bin/journalctl -u $SERVICE *
$DEPLOY_USER ALL=($APP_USER) NOPASSWD: $BASE/.venv/bin/alembic upgrade head
EOF
chmod 440 /etc/sudoers.d/deploy-synora-api
visudo -cf /etc/sudoers.d/deploy-synora-api

# ------------------------------------------------ keep the unit file in sync
if ! cmp -s "$HERE/synora-api.service" "/etc/systemd/system/$SERVICE.service"; then
  log "Updating /etc/systemd/system/$SERVICE.service"
  install -m 644 "$HERE/synora-api.service" "/etc/systemd/system/$SERVICE.service"
  systemctl daemon-reload
fi

systemctl is-enabled "$SERVICE" >/dev/null 2>&1 || systemctl enable "$SERVICE"

log "Ready. Add the private half of /home/$DEPLOY_USER/.ssh/github-actions to the"
echo "    repository as the DEPLOY_SSH_KEY secret, then push to $BRANCH."
