# lore-web

Read-only web viewer for a [Lore](https://github.com/EpicGames/lore) VCS server —
because none exists yet. Single-file Python (stdlib only) wrapping the `lore` CLI.

Repo list, commit history, file tree at head, file downloads. No auth: built for
LAN use next to an unauthenticated pilot loreserver.

## Run

```bash
LORE_REMOTE=lore://127.0.0.1:41337 \
CLONES_DIR=/mnt/nas2tb/lore/web-clones \
PORT=41340 python3 server.py
```

Requires the `lore` CLI on PATH (`~/.local/bin/lore`). The service keeps one
clone per repository (hydrated at head — Lore has no sparse clone yet) and
syncs lazily (60s TTL, or the sync button).

Deployed on nas: `/mnt/nas2tb/lore/web/server.py`, @reboot crontab, port 41340.
