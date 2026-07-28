#!/usr/bin/env bash
# Deploy lore-web to a host over ssh.
#
#   cp .env.example .env && $EDITOR .env
#   ./deploy/deploy.sh --units      # first time: install the systemd units
#   ./deploy/deploy.sh              # sync code + restart + health check
#   ./deploy/deploy.sh --status     # what's running
#
# Settings come from .env (see .env.example); every one can be overridden
# from the environment: DEPLOY_HOST=other ./deploy/deploy.sh
#
# Runs both processes as *systemd user services* (lingering enabled), so no
# root is needed anywhere. Only code is pushed - the runtime env file,
# clones and preview cache on the host are never touched.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
[[ -f "$SRC/.env" ]] && . "$SRC/.env"

HOST="${DEPLOY_HOST:-${HOST:-nas}}"
REMOTE_DIR="${REMOTE_DIR:-/srv/lore/web}"
DATA_MOUNT="${DATA_MOUNT:-}"
LORESERVER_BIN="${LORESERVER_BIN:-loreserver}"
LORESERVER_CONFIG="${LORESERVER_CONFIG:-/srv/lore/config}"
PYTHON_BIN="${PYTHON_BIN:-/usr/bin/python3}"
UNIT_DIR="\$HOME/.config/systemd/user"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

if [[ "${1:-}" == "--status" ]]; then
    ssh "$HOST" "systemctl --user status loreserver lore-web --no-pager -n 5" || true
    exit 0
fi

render() {  # $1 = template, stdout = unit file
    local gate=""
    # RequiresMountsFor= is inert in a *user* unit (mount units live in the
    # system manager), so gate on the mount ourselves and let Restart retry.
    [[ -n "$DATA_MOUNT" ]] &&
        gate="ExecStartPre=/bin/sh -c 'mountpoint -q $DATA_MOUNT'"
    sed -e "s|@REMOTE_DIR@|$REMOTE_DIR|g" \
        -e "s|@PYTHON_BIN@|$PYTHON_BIN|g" \
        -e "s|@LORESERVER_BIN@|$LORESERVER_BIN|g" \
        -e "s|@LORESERVER_CONFIG@|$LORESERVER_CONFIG|g" \
        -e "s|@MOUNT_GATE@|$gate|g" \
        "$1" | grep -v '^$#'
}

if [[ "${1:-}" == "--units" ]]; then
    say "installing systemd user units on $HOST"
    tmp="$(mktemp -d)"
    trap 'rm -rf "$tmp"' EXIT
    render "$SRC/deploy/loreserver.service.in" > "$tmp/loreserver.service"
    render "$SRC/deploy/lore-web.service.in"  > "$tmp/lore-web.service"
    ssh "$HOST" "mkdir -p $UNIT_DIR"
    scp -q "$tmp/loreserver.service" "$tmp/lore-web.service" \
        "$HOST:.config/systemd/user/"
    ssh "$HOST" "loginctl enable-linger \$USER >/dev/null 2>&1 || true
                 systemctl --user daemon-reload
                 systemctl --user enable loreserver.service lore-web.service"
    say "units installed and enabled (start at boot, restart on failure)"
fi

say "syncing code to $HOST:$REMOTE_DIR"
ssh "$HOST" "mkdir -p $REMOTE_DIR/plugins"
rsync -a "$SRC/server.py" "$HOST:$REMOTE_DIR/server.py"
rsync -a --delete --include='*.py' --exclude='*' \
    "$SRC/plugins/" "$HOST:$REMOTE_DIR/plugins/"

say "restarting lore-web"
if ! ssh "$HOST" "systemctl --user cat lore-web.service >/dev/null 2>&1"; then
    echo "lore-web.service not installed; run: $0 --units" >&2
    exit 1
fi
ssh "$HOST" "systemctl --user restart lore-web.service"

say "waiting for it to answer"
port="$(ssh "$HOST" "grep -oE 'PORT=[0-9]+' $REMOTE_DIR/env | tail -1 | cut -d= -f2" || true)"
port="${port:-41340}"
for _ in $(seq 20); do
    code="$(ssh "$HOST" "set -a; . $REMOTE_DIR/env; set +a
        curl -s -o /dev/null -w '%{http_code}' \
             \${LORE_WEB_AUTH:+-u \"\$LORE_WEB_AUTH\"} \
             http://localhost:$port/api/repos" || true)"
    [[ "$code" == "200" ]] && { say "healthy: HTTP 200 on :$port"; exit 0; }
    sleep 1
done

echo "did not become healthy; recent logs:" >&2
ssh "$HOST" "journalctl --user -u lore-web -n 20 --no-pager" >&2
exit 1
