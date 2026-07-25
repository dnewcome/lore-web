#!/usr/bin/env python3
"""lore-web: a read-only web viewer for a Lore VCS server.

Single file, Python stdlib only (ffmpeg/ffprobe optional, for previews).
Wraps the `lore` CLI. Instead of mirroring binaries, the server keeps a
*preview cache*: each repo is cloned once, every media file gets a waveform
(audio) or keyframe strip (video) or thumbnail (image) rendered via ffmpeg,
then the binary is purged from the working copy (PREVIEW_ONLY=1, default).
The tree is served from a manifest; downloads re-hydrate single files on
demand with `lore reset <path>`. Verified against loreserver 0.8.5: sync
does not restore locally-deleted files, and metadata survives purging.

Env config:
  LORE_REMOTE   lore://127.0.0.1:41337   remote loreserver
  LORE_BIN      ~/.local/bin/lore        lore CLI binary
  CLONES_DIR    ~/lore-web-clones        viewer clones + manifests
  PREVIEWS_DIR  <CLONES_DIR>/../lore-web-previews   preview cache (by sha256)
  PREVIEW_ONLY  1                        purge binaries after preview
  FFMPEG/FFPROBE                          override tool paths
  PORT          41340
"""
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REMOTE = os.environ.get("LORE_REMOTE", "lore://127.0.0.1:41337")
LORE = os.path.expanduser(os.environ.get("LORE_BIN", "~/.local/bin/lore"))
CLONES = os.path.expanduser(os.environ.get("CLONES_DIR", "~/lore-web-clones"))
PREVIEWS = os.path.expanduser(os.environ.get(
    "PREVIEWS_DIR", os.path.join(CLONES, os.pardir, "lore-web-previews")))
PREVIEW_ONLY = os.environ.get("PREVIEW_ONLY", "1") == "1"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
PORT = int(os.environ.get("PORT", "41340"))
SYNC_TTL = 60

AUDIO_EXT = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg", ".m4a", ".opus"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

_lock = threading.Lock()          # one lore/ffmpeg pipeline at a time
_last_sync = {}


def run(cmd, cwd=None, timeout=600):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"{os.path.basename(cmd[0])} failed: "
                           f"{p.stderr.strip()[:400]}")
    return p.stdout


def run_lore(args, cwd=None, timeout=600):
    return run([LORE, *args], cwd=cwd, timeout=timeout)


def have_ffmpeg():
    return shutil.which(FFMPEG) is not None


def list_repos():
    out = run_lore(["repository", "list", REMOTE], timeout=30)
    return [{"name": m.group(1), "id": m.group(2)}
            for line in out.splitlines()
            if (m := re.match(r"^(\S+) \(([0-9a-f]+)\)\s*$", line.strip()))]


def repo_path(name):
    if not any(r["name"] == name for r in list_repos()):
        raise KeyError(f"unknown repository {name!r}")
    return os.path.join(CLONES, name)


def manifest_path(path):
    return os.path.join(path, ".lore-web-manifest.json")


def load_manifest(path):
    try:
        with open(manifest_path(path)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_manifest(path, manifest):
    with open(manifest_path(path), "w") as f:
        json.dump(manifest, f)


def file_kind(relpath):
    ext = os.path.splitext(relpath)[1].lower()
    if ext in AUDIO_EXT:
        return "audio"
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    return "other"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def probe_duration(path):
    try:
        out = run([FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
                   "-of", "csv=p=0", path], timeout=60)
        return round(float(out.strip()), 1)
    except Exception:
        return None


def render_preview(src, kind, digest, duration=None):
    """Render a preview PNG for src into the cache; return relative name."""
    out = os.path.join(PREVIEWS, f"{digest}.png")
    if os.path.exists(out) or not have_ffmpeg():
        return os.path.basename(out) if os.path.exists(out) else None
    tmp = out + ".tmp.png"
    try:
        if kind == "audio":
            run([FFMPEG, "-v", "quiet", "-y", "-i", src, "-filter_complex",
                 "aformat=channel_layouts=mono,compand,"
                 "showwavespic=s=900x140:colors=#4a9eda", "-frames:v", "1",
                 tmp], timeout=300)
        elif kind == "video":
            # strip of 5 evenly spaced frames across the clip
            fps = 5.0 / max(duration or 1.0, 0.2)
            run([FFMPEG, "-v", "quiet", "-y", "-i", src, "-vf",
                 f"fps={fps},scale=320:-2,tile=5x1", "-frames:v", "1",
                 tmp], timeout=300)
        elif kind == "image":
            run([FFMPEG, "-v", "quiet", "-y", "-i", src,
                 "-vf", "scale='min(480,iw)':-2", "-frames:v", "1", tmp],
                timeout=120)
        else:
            return None
        os.replace(tmp, out)
        return os.path.basename(out)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        return None


def lore_file_hash(path, rel):
    out = run_lore(["file", "info", rel], cwd=path, timeout=60)
    m = re.search(r"^Hash:\s*([0-9a-f]+)", out, re.M)
    status = re.search(r"^Status:\s*(\S+)", out, re.M)
    size = re.search(r"^Size:\s*(\d+)", out, re.M)
    return (m.group(1) if m else None,
            status.group(1) if status else "-",
            int(size.group(1)) if size else 0)


def absorb_file(path, rel, manifest):
    """File is present on disk: hash it, preview it, purge it, record it."""
    full = os.path.join(path, rel)
    digest = sha256_file(full)
    kind = file_kind(rel)
    entry = manifest.get(rel, {})
    if entry.get("sha256") != digest:
        entry = {
            "sha256": digest,
            "size": os.path.getsize(full),
            "kind": kind,
            "mtime": int(os.path.getmtime(full)),
        }
        if kind in ("audio", "video"):
            entry["duration"] = probe_duration(full)
        if render_preview(full, kind, digest, entry.get("duration")):
            entry["preview"] = f"{digest}.png"
        lore_hash, _, _ = lore_file_hash(path, rel)
        entry["lore_hash"] = lore_hash
    manifest[rel] = entry
    if PREVIEW_ONLY:
        os.unlink(full)


def scan_disk_files(path):
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".lore"]
        for f in files:
            if f == os.path.basename(manifest_path(path)):
                continue
            yield os.path.normpath(
                os.path.relpath(os.path.join(root, f), path))


def refresh(name, force=False):
    """Clone/sync a repo, absorb any hydrated files, reconcile manifest."""
    path = repo_path(name)
    with _lock:
        fresh_clone = not os.path.isdir(os.path.join(path, ".lore"))
        if fresh_clone:
            os.makedirs(CLONES, exist_ok=True)
            run_lore(["clone", f"{REMOTE}/{name}", path])
        elif force or time.time() - _last_sync.get(name, 0) > SYNC_TTL:
            run_lore(["sync"], cwd=path)
        else:
            return path
        _last_sync[name] = time.time()

        manifest = load_manifest(path)
        # absorb everything sync/clone hydrated
        for rel in list(scan_disk_files(path)):
            absorb_file(path, rel, manifest)
        # reconcile tracked-but-purged files against repo metadata
        for rel in list(manifest):
            lore_hash, status, size = (None, "-", 0)
            try:
                lore_hash, status, size = lore_file_hash(path, rel)
            except RuntimeError:
                status = "Gone"
            if status == "Gone" or lore_hash is None:
                del manifest[rel]
            elif manifest[rel].get("lore_hash") not in (None, lore_hash):
                # changed remotely while purged locally: re-hydrate + redo
                run_lore(["reset", rel], cwd=path)
                if os.path.exists(os.path.join(path, rel)):
                    absorb_file(path, rel, manifest)
        save_manifest(path, manifest)
    return path


def hydrate_for_download(name, rel):
    path = refresh(name)
    manifest = load_manifest(path)
    if rel not in manifest:
        raise KeyError("not a tracked file")
    full = os.path.realpath(os.path.join(path, rel))
    base = os.path.realpath(path)
    if not full.startswith(base + os.sep):
        raise KeyError("bad path")
    with _lock:
        if not os.path.exists(full):
            run_lore(["reset", rel], cwd=path)
    if not os.path.isfile(full):
        raise KeyError("could not hydrate")
    return path, full


def history(name):
    path = refresh(name)
    out = run_lore(["history", "50", "--no-pager"], cwd=path)
    revs, cur = [], None
    for line in out.splitlines():
        m = re.match(r"^(Revision|Signature|Branch|Date)\s*:\s*(.*)$", line)
        if m:
            key = m.group(1).lower()
            if key == "revision":
                cur = {"revision": m.group(2).strip(), "message": ""}
                revs.append(cur)
            elif cur is not None:
                cur[key] = m.group(2).strip()
        elif cur is not None and line.startswith("    "):
            cur["message"] = (cur["message"] + "\n" + line.strip()).strip()
    return revs


def tree(name):
    path = refresh(name)
    manifest = load_manifest(path)
    return sorted(
        ({"path": rel, **{k: v for k, v in e.items() if k != "lore_hash"}}
         for rel, e in manifest.items()),
        key=lambda e: e["path"])


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>lore-web</title><style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{margin:0;display:grid;grid-template-columns:220px 1fr;min-height:100vh}
nav{border-right:1px solid #8884;padding:1rem}
nav h1{font-size:1rem;margin:0 0 .75rem}
nav a{display:block;padding:.35rem .5rem;border-radius:6px;text-decoration:none;color:inherit}
nav a.sel,nav a:hover{background:#8882}
main{padding:1rem 1.5rem;max-width:64rem}
table{border-collapse:collapse;width:100%;font-size:.9rem}
td,th{text-align:left;padding:.35rem .6rem;border-bottom:1px solid #8883;vertical-align:middle}
.sig{font-family:monospace;font-size:.8rem;opacity:.7}
.msg{white-space:pre-wrap}
.muted{opacity:.6;font-size:.85rem}
h2{display:flex;align-items:center;font-size:1.1rem}
button{margin-left:.5rem}
img.pv{display:block;max-width:460px;max-height:90px;border-radius:4px;background:#8881}
</style></head><body>
<nav><h1>lore-web</h1><div id="repos"></div>
<p class="muted" id="remote"></p></nav>
<main id="main"><p class="muted">Pick a repository.</p></main>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtSize=n=>n>1e9?(n/1e9).toFixed(1)+' GB':n>1e6?(n/1e6).toFixed(1)+' MB':n>1e3?(n/1e3).toFixed(1)+' KB':n+' B';
const fmtDur=s=>s==null?'':(s>=60?Math.floor(s/60)+'m'+String(Math.round(s%60)).padStart(2,'0')+'s':s+'s');
let current=null;
async function j(u){const r=await fetch(u);if(!r.ok)throw new Error(await r.text());return r.json()}
async function loadRepos(){
  const repos=await j('/api/repos');
  $('#repos').innerHTML=repos.map(r=>`<a href="#" data-n="${esc(r.name)}">${esc(r.name)}</a>`).join('');
  document.querySelectorAll('#repos a').forEach(a=>a.onclick=e=>{e.preventDefault();show(a.dataset.n)});
}
async function show(name,refresh){
  current=name;
  document.querySelectorAll('#repos a').forEach(a=>a.classList.toggle('sel',a.dataset.n===name));
  $('#main').innerHTML=`<h2>${esc(name)} <button onclick="show(current,1)">sync</button></h2><p class="muted">loading${refresh?' (syncing + rendering previews)':''}…</p>`;
  const q=refresh?'?refresh=1':'';
  try{
    const [hist,files]=await Promise.all([j(`/api/repo/${name}/history${q}`),j(`/api/repo/${name}/tree`)]);
    $('#main').innerHTML=`<h2>${esc(name)} <button onclick="show(current,1)">sync</button></h2>
    <h3>History</h3><table>${hist.map(r=>`<tr><td>#${esc(r.revision)}</td><td class="msg">${esc(r.message)}</td><td>${esc(r.date||'')}</td><td class="sig">${esc((r.signature||'').slice(0,10))}</td></tr>`).join('')||'<tr><td class=muted>no revisions</td></tr>'}</table>
    <h3>Files</h3><table>${files.map(f=>`<tr>
      <td><a href="/api/repo/${name}/file?path=${encodeURIComponent(f.path)}">${esc(f.path)}</a>
        ${f.preview?`<img class="pv" loading="lazy" src="/previews/${esc(f.preview)}" alt="">`:''}</td>
      <td>${fmtSize(f.size)}</td><td>${fmtDur(f.duration)}</td></tr>`).join('')||'<tr><td class=muted>empty</td></tr>'}</table>`;
  }catch(e){$('#main').innerHTML=`<h2>${esc(name)}</h2><p>error: ${esc(e.message)}</p>`}
}
loadRepos();
fetch('/api/info').then(r=>r.json()).then(i=>$('#remote').textContent=i.remote+(i.ffmpeg?'':' (no ffmpeg: previews off)'));
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            url = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(url.query)
            force = qs.get("refresh", ["0"])[0] == "1"
            parts = [p for p in url.path.split("/") if p]

            if url.path == "/":
                self._bytes(PAGE.encode(), "text/html; charset=utf-8")
            elif url.path == "/api/info":
                self._json({"remote": REMOTE, "ffmpeg": have_ffmpeg(),
                            "preview_only": PREVIEW_ONLY})
            elif url.path == "/api/repos":
                self._json(list_repos())
            elif len(parts) == 2 and parts[0] == "previews":
                name = os.path.basename(parts[1])
                full = os.path.join(PREVIEWS, name)
                if not re.fullmatch(r"[0-9a-f]{64}\.png", name) \
                        or not os.path.isfile(full):
                    raise KeyError("no such preview")
                with open(full, "rb") as f:
                    self._bytes(f.read(), "image/png")
            elif len(parts) == 4 and parts[:2] == ["api", "repo"]:
                name, action = parts[2], parts[3]
                if action == "history":
                    if force:
                        refresh(name, force=True)
                    self._json(history(name))
                elif action == "tree":
                    self._json(tree(name))
                elif action == "file":
                    rel = qs.get("path", [""])[0]
                    path, full = hydrate_for_download(name, rel)
                    ctype = mimetypes.guess_type(full)[0] or \
                        "application/octet-stream"
                    self.send_response(200)
                    self.send_header("Content-Type", ctype)
                    self.send_header(
                        "Content-Disposition",
                        f'attachment; filename="{os.path.basename(full)}"')
                    self.send_header("Content-Length",
                                     str(os.path.getsize(full)))
                    self.end_headers()
                    with open(full, "rb") as f:
                        while chunk := f.read(1 << 16):
                            self.wfile.write(chunk)
                    if PREVIEW_ONLY:
                        os.unlink(full)
                else:
                    self._json({"error": "unknown action"}, 404)
            else:
                self._json({"error": "not found"}, 404)
        except KeyError as e:
            self._json({"error": str(e)}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001 - surface everything to the LAN client
            self._json({"error": str(e)}, 500)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")


if __name__ == "__main__":
    os.makedirs(CLONES, exist_ok=True)
    os.makedirs(PREVIEWS, exist_ok=True)
    print(f"lore-web on :{PORT} -> {REMOTE}")
    print(f"  clones: {CLONES}  previews: {PREVIEWS}  "
          f"preview_only={PREVIEW_ONLY} ffmpeg={have_ffmpeg()}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
