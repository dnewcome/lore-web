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
| `PD_MAX_WIDTH` | `1200` | cap on rendered Pd patch width; `0` disables |
| `PD_WIDTH` | *(unset)* | exact Pd patch render width, overriding the cap |
| `PD_SCALE` | *(unset)* | multiply a Pd patch's natural size |

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

Ships with five, all pure stdlib apart from the ffmpeg the server already uses:

- **`midi.py`** — SMF parser; piano-roll SVG, track/note/BPM/length chips
- **`ableton.py`** — reads `.als` (gzipped XML); track-map SVG, track counts,
  tempo, and the devices/plugins used in the set
- **`flp.py`** — FL Studio projects back to the 1998 format; pattern-grid SVG,
  channel rack, tempo, plugins, and missing-sample detection
- **`pd.py`** — Pure Data patches; draws the patch itself as SVG (boxes, cords,
  GUI objects, array traces) without Pd or a display, and reports which object
  classes are not vanilla, so you can see what a patch needs before opening it.
  Render size is configurable (see below)
- **`tif.py`** — TIFF/BigTIFF; thumbnail plus compression, bit depth, DPI,
  authoring tool and capture date. Works around several shapes ffmpeg decodes
  to a silent black frame (JPEG-in-TIFF, CMYK) or rejects outright (BigTIFF),
  and refuses by name the two it cannot fix rather than showing a wrong image

Each is also runnable as a CLI — `python3 plugins/flp.py --help`,
`python3 plugins/tif.py --help` — for inspecting files outside the viewer.

A plugin whose output depends on configuration should expose a `CACHE_SALT`
string that changes with it. Preview art is cached by content hash and
handler name, so without one the viewer would keep serving art rendered
under the old settings. An empty salt leaves the cache key untouched.

### Rendering Pd patches at any size

Patch drawings are vector, so size is purely the width and height the SVG
declares — the viewBox and every coordinate stay in Pd's own pixel units,
and the picture is identical at every resolution.

```sh
# a 4x SVG, and a 4000px-wide PNG
python3 plugins/pd.py patch.pd --svg out/ --scale 4
python3 plugins/pd.py *.pd    --png out/ --width 4000
```

`--width` sets the output width exactly, `--scale` multiplies the patch's
natural size, and with neither the output is capped at `--max-width`
(default 1200, `0` disables). `--png` needs an SVG rasterizer —
`rsvg-convert`, `inkscape` or ImageMagick's `convert`, whichever is found
first, or name one with `--renderer`.

The same knobs configure the viewer through `PD_WIDTH`, `PD_SCALE` and
`PD_MAX_WIDTH`; setting any of them changes the plugin's cache salt, so
previews are re-rendered rather than served stale from the cache.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The TIFF plugin: `tests/fixtures_tif.py` generates a corpus
spanning every compression, both byte orders, 1/8/16/32-bit and float samples,
tiled/stripped/multi-page layouts and RGB/gray/inverted/CMYK colour, and
`tests/test_tif.py` compares every page against an ImageMagick reference.

Assertions are on pixel values, never exit status — ffmpeg returns 0 on
several TIFF variants it decodes to black, so a test that only checks "a
thumbnail appeared" passes while the thumbnail is wrong.

Needs ImageMagick and ffmpeg on PATH; those tests skip themselves if either
is missing. Fixtures are generated into `tests/fixtures/` and not checked in.

`tests/test_server.py` covers the plugin dispatch and cache-key logic with
no Lore remote needed.

The Pd plugin (`tests/test_pd.py`) has no external renderer to check against,
so it asserts on what came out of the file and where the drawing puts it —
box geometry against Pd's own font table, port positions, cord routing, and
that the emitted SVG is well-formed XML. Pure stdlib, no external tools.

Both suites were mutation-checked: breaking a behaviour on purpose must fail
a test. Two assertions were rewritten because they did not — one compared the
code against its own constant, the other could not see a spec violation both
ffmpeg and ImageMagick tolerate.

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
enough), with `Restart=always`, ordering via `Requires=`/`After=`, an
optional mountpoint gate so neither starts before the data volume mounts,
and logs in the journal rather than an unbounded file.

Paths and hosts come from a git-ignored `.env`:

```bash
cp .env.example .env && $EDITOR .env   # host, remote dir, binary paths
./deploy/deploy.sh --units             # install + enable both units (once)
./deploy/deploy.sh                     # sync code, restart, health-check
./deploy/deploy.sh --status            # what's running
DEPLOY_HOST=other ./deploy/deploy.sh   # override anything ad hoc
```

The unit files are templates (`deploy/*.service.in`) rendered with those
values at install time, so nothing about your machine is baked into the
repo. Runtime settings (ports, credentials, cache locations) live in an
`env` file **on the host** — see the bottom of `.env.example`.

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
