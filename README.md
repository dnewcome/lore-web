# lore-web

A web viewer for [Lore](https://github.com/EpicGames/lore), Epic Games' open
source version control system for large binary assets. Lore ships a CLI and
SDKs but no browser UI — this fills the gap for the "what's in my repos?"
case, with a twist built for media work:

**it never mirrors your binaries.** Audio files render to waveforms, video to
keyframe strips, images to thumbnails (all via ffmpeg), and then the bytes are
purged from the viewer's working copy. What remains is a manifest and a
preview cache keyed by content hash. A 775MB Ableton project set browses from
~500KB on disk.

![example waveform](docs/waveform-example.png)

## Features

- Repository list, commit history, file tree, file downloads, copyable
  `lore clone` commands
- Waveform / keyframe-strip / thumbnail previews with durations
- In-browser audio/video playback — seekable (HTTP Range) streaming that
  hydrates transiently and purges after a few idle minutes
- **Preview plugins**: drop a `.py` in `plugins/` to add thumbnails and
  metadata chips for new file types; ships with MIDI (piano-roll) and
  Ableton Live (track map, tempo, devices used) inspectors
- Preview cache keyed by content SHA-256 — identical files across repos
  render once, unchanged files are never reprocessed
- Downloads re-hydrate single files on demand (`lore reset`), then purge again
- Optional HTTP Basic auth
- Single file, Python 3.8+ stdlib only; ffmpeg optional (previews degrade
  gracefully without it)

## Quickstart

Requires the [`lore` CLI](https://github.com/EpicGames/lore) on PATH and,
for previews, `ffmpeg`/`ffprobe`.

```bash
LORE_REMOTE=lore://your-server:41337 python3 server.py
# open http://localhost:41340
```

## Configuration (env vars)

| var | default | |
|---|---|---|
| `LORE_REMOTE` | `lore://127.0.0.1:41337` | the loreserver to browse |
| `LORE_PUBLIC_REMOTE` | `LORE_REMOTE` | reachable URL shown in clone commands (set when the server connects via localhost) |
| `PORT` | `41340` | HTTP port |
| `LORE_WEB_AUTH` | *(empty)* | `user:password` — enables HTTP Basic auth on all routes |
| `PREVIEW_ONLY` | `1` | purge binaries after preview; `0` keeps full working copies |
| `CLONES_DIR` | `~/lore-web-clones` | viewer clones + manifests |
| `PREVIEWS_DIR` | sibling `lore-web-previews` | preview cache (PNG/SVG) |
| `PLUGINS_DIR` | `./plugins` | preview plugin directory |
| `LORE_BIN`, `FFMPEG`, `FFPROBE` | | tool path overrides |

## Preview plugins

Anything in `plugins/*.py` exposing `MATCH` (a list of extensions) and
`inspect(path, ctx)` becomes a preview handler, taking precedence over the
built-in ffmpeg ones. Return any of:

```python
{
  "preview": (svg_or_png_bytes, "svg" | "png"),   # thumbnail for the file list
  "meta":    {"tracks": 7, "bpm": 128, ...},       # rendered as chips in the UI
  "kind":    "ableton",                            # overrides extension-based kind
}
```

Plugin failures fall back to the built-in preview; results are cached by
content hash, and when a new plugin appears, already-purged files are
re-hydrated once and re-inspected automatically.

Ships with two:

- **`midi.py`** — pure-stdlib SMF parser; piano-roll SVG, track/note/BPM/length chips
- **`ableton.py`** — reads `.als` (gzipped XML); track-map SVG, track counts,
  tempo, and the devices/plugins used in the set

## How it works

The server keeps one clone per repository. On first contact (and after each
sync) any hydrated file is hashed, previewed, recorded in a manifest, and
deleted from the working copy. The file tree serves from the manifest; a
download re-hydrates just that file and purges it after streaming. Files
changed remotely are detected by comparing `lore file info` content hashes
against the manifest and re-previewed individually.

This leans on two behaviors verified against loreserver 0.8.5:

1. `lore sync` does **not** restore locally-deleted files (the gutted working
   copy stays in sync at the metadata level), and
2. `lore file info` returns size/hash/status for files absent from disk.

If a future Lore release changes either, set `PREVIEW_ONLY=0`.

## Namespaces

Lore accepts slashes in repository names, which works as an org/section
mechanism without any extra concept:

```bash
lore repository create lore://your-server:41337/bl1t/drums
lore clone lore://your-server:41337/bl1t/drums drums
```

The viewer groups them: `bl1t/drums` and `bl1t/synths` appear as `drums`
and `synths` under a collapsible **bl1t** heading in the sidebar.

Prefer one repo per project over a single large one — collaborators clone
only what they need, each project gets its own history, and identical files
are still stored once across repos by the content-addressed store. For
composing repos (a shared sample library mounted into each project, say),
see `lore link add`.

## Running it as a service

Two processes are involved, and they're deliberately separate:

1. **`loreserver`** — Epic's Lore server (third party, installed on its own,
   holds the repositories). `deploy/loreserver.service` just supervises it.
2. **`lore-web`** — this viewer, which talks to it over `LORE_REMOTE`.

`deploy/deploy.sh` pushes *only this repo's code* (`server.py`, `plugins/`)
to the host over ssh and restarts the viewer; it never touches the
loreserver binary, the env file, clones, or the preview cache. Both run as
**systemd user services** (no root needed — `loginctl enable-linger` is
enough), with `Restart=always`, ordering via `Requires=`/`After=`, a
`RequiresMountsFor=` guard so neither starts before the data volume mounts,
and logs in the journal rather than an unbounded file.

```bash
./deploy/deploy.sh --units     # install + enable both units (once)
./deploy/deploy.sh             # sync code, restart, health-check
./deploy/deploy.sh --status    # what's running
HOST=nas ./deploy/deploy.sh    # pick the ssh host (default: nas)
```

```bash
journalctl --user -u lore-web -f            # follow logs
systemctl --user restart lore-web           # restart by hand
```

## Security

This is a read-only viewer, but downloads expose full repository contents:
run it on a trusted network, set `LORE_WEB_AUTH`, or better, serve it over
[Tailscale](https://tailscale.com/) for remote access. It does no TLS itself.

## Status

Early, like Lore itself (pre-1.0, interfaces may change). Built and tested
against loreserver/CLI 0.8.5 on Linux. Not affiliated with Epic Games.

## License

MIT
