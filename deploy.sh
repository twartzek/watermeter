#!/bin/bash
# Deploys the current working tree to the Raspberry Pi via rsync and restarts
# the backend/frontend services.
#
# Usage:
#   ./deploy.sh [user@host] [remote_path]
#
# Reads SSH_HOST / SSH_USER / SSH_PASSWORD / REMOTE_PATH from deploy.env
# (copy deploy.env.example to deploy.env and fill in your Pi's credentials).
# Positional args, if given, override SSH_HOST/SSH_USER and REMOTE_PATH.
#
# Falls back to your regular SSH key (no password) if deploy.env / SSH_PASSWORD
# isn't set -- e.g. "user@host" as arg 1 with an ssh-agent already set up.
#
# Env overrides (optional):
#   SKIP_BUILD=1       skip "npm run build" and sync the existing frontend/dist as-is
#   SKIP_BACKEND=1     don't sync backend/
#   SKIP_FRONTEND=1    don't sync frontend/
#   SKIP_MEASUREMENT=1 don't trigger a measurement after restarting services

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# shellcheck disable=SC1091
[ -f deploy.env ] && source deploy.env

SSH_HOST="${SSH_HOST:-watermeter.local}"
SSH_USER="${SSH_USER:-admin}"
REMOTE_PATH="${REMOTE_PATH:-/home/admin/watermeter}"

# Positional args override deploy.env
if [ -n "${1:-}" ]; then
    SSH_USER="${1%@*}"
    SSH_HOST="${1#*@}"
fi
REMOTE_PATH="${2:-$REMOTE_PATH}"
REMOTE="${SSH_USER}@${SSH_HOST}"

SSH_CMD=(ssh)
RSYNC_SSH="ssh"
if [ -n "${SSH_PASSWORD:-}" ]; then
    if ! command -v sshpass >/dev/null 2>&1; then
        echo "sshpass not found but SSH_PASSWORD is set in deploy.env. Install sshpass or switch to key-based auth." >&2
        exit 1
    fi
    # PubkeyAuthentication=no: without this, ssh offers every key loaded in
    # the local ssh-agent before ever trying the password -- with several
    # keys loaded (e.g. after adding a new one for an unrelated project)
    # this exhausts the server's MaxAuthTries and gets the connection
    # dropped ("Too many authentication failures") before sshpass's
    # password is ever tried. Safe here since SSH_PASSWORD means we
    # explicitly want password auth for this host.
    SSH_OPTS=(-o PubkeyAuthentication=no)
    SSH_CMD=(sshpass -p "$SSH_PASSWORD" ssh "${SSH_OPTS[@]}")
    RSYNC_SSH="sshpass -p ${SSH_PASSWORD@Q} ssh -o PubkeyAuthentication=no"
fi

echo "==> Deploying to ${REMOTE}:${REMOTE_PATH}"

if [ "${SKIP_FRONTEND:-0}" != "1" ] && [ "${SKIP_BUILD:-0}" != "1" ]; then
    echo "==> Building frontend (npm run build)..."
    (cd frontend && npm run build)
fi

RSYNC_OPTS=(-az --delete --info=progress2 --no-i-r -e "$RSYNC_SSH")

if [ "${SKIP_BACKEND:-0}" != "1" ]; then
    echo "==> Syncing backend/..."
    rsync "${RSYNC_OPTS[@]}" \
        --exclude '__pycache__/' \
        --exclude '*.pyc' \
        --exclude '.pytest_cache/' \
        --exclude 'tests/' \
        --exclude '*.db' \
        --exclude '.env' \
        --exclude '.venv/' \
        backend/ "${REMOTE}:${REMOTE_PATH}/backend/"
fi

if [ "${SKIP_FRONTEND:-0}" != "1" ]; then
    echo "==> Syncing frontend/dist..."
    rsync "${RSYNC_OPTS[@]}" \
        frontend/dist/ "${REMOTE}:${REMOTE_PATH}/frontend/dist/"
fi

echo "==> Restarting services on ${REMOTE}..."
"${SSH_CMD[@]}" "${REMOTE}" "sudo systemctl restart watermeter_restapi watermeter_frontend"

if [ "${SKIP_MEASUREMENT:-0}" != "1" ]; then
    echo "==> Triggering a measurement (photo capture + reading) on ${REMOTE}..."
    "${SSH_CMD[@]}" "${REMOTE}" "nohup ${REMOTE_PATH}/backend/startMeasurement.sh > /tmp/watermeter_deploy_measurement.log 2>&1 < /dev/null &" || \
        echo "    (measurement trigger failed -- it will still run via its regular cron schedule)" >&2
fi

echo "==> Done."
