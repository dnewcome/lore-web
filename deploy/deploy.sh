#!/usr/bin/env bash
# Deploy lore-web to the NAS over ssh, from a checkout on the workstation.
#
#   ./deploy/deploy.sh              # sync code + restart the service
#   ./deploy/deploy.sh --units      # also (re)install the systemd user units
#   ./deploy/deploy.sh --status     # show what's running, then exit
#   HOST=nas ./deploy/deploy.sh     # override the ssh host
#
# Runs as the ssh user's *systemd user services* (lingering enabled), so no
# root is needed anywhere. Code lives at $REMOTE_DIR on the NAS; state
# (clones, previews, env) lives beside it and is never touched by deploys.
set -euo pipefail

HOST="${HOST:-nas}"
REMOTE_DIR="${REMOTE_DIR:-/mnt/nas2tb/lore/web}"
UNIT_DIR="\$HOME/.config/systemd/user"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }

if [[ "${1:-}" == "--status" ]]; then
    ssh "$HOST" "systemctl --user status loreserver lore-web --no-pager -n 5" || true
    exit 0
fi

if [[ "${1:-}" == "--units" ]]; then
    say "installing systemd user units on $HOST"
    ssh "$HOST" "mkdir -p $UNIT_DIR"
    scp -q "$SRC/deploy/loreserver.service" "$SRC/deploy/lore-web.service" \
        "$HOST:.config/systemd/user/"
    ssh "$HOST" "loginctl enable-linger \$USER >/dev/null 2>&1 || true
                 systemctl --user daemon-reload
                 systemctl --user enable loreserver.service lore-web.service"
    say "units installed and enabled (start at boot, restart on failure)"
    say "if cron still starts these, remove its @reboot lines: crontab -e"
fi

say "syncing code to $HOST:$REMOTE_DIR"
ssh "$HOST" "mkdir -p $REMOTE_DIR/plugins"
# code only: never the env file, clones, previews, or logs
rsync -a --delete \
    "$SRC/server.py" "$HOST:$REMOTE_DIR/server.py"
rsync -a --delete --include='*.py' --exclude='*' \
    "$SRC/plugins/" "$HOST:$REMOTE_DIR/plugins/"

say "restarting lore-web"
if ssh "$HOST" "systemctl --user cat lore-web.service >/dev/null 2>&1"; then
    ssh "$HOST" "systemctl --user restart lore-web.service"
else
    echo "lore-web.service not installed; run: $0 --units" >&2
    exit 1
fi

say "waiting for it to answer"
port="$(ssh "$HOST" "grep -oP '(?<=^export PORT=)\d+' $REMOTE_DIR/env || echo 41340")"
for _ in $(seq 20); do
    code="$(ssh "$HOST" "source $REMOTE_DIR/env
        curl -s -o /dev/null -w '%{http_code}' -u \"\$LORE_WEB_AUTH\" \
             http://localhost:$port/api/repos" || true)"
    [[ "$code" == "200" ]] && { say "healthy: HTTP 200 on :$port"; exit 0; }
    sleep 1
done

echo "did not become healthy; recent logs:" >&2
ssh "$HOST" "journalctl --user -u lore-web -n 20 --no-pager" >&2
exit 1
