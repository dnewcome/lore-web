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
import base64
import hashlib
import hmac
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
# what clone commands in the UI should point at (a reachable hostname,
# e.g. lore://nas.local:41337) -- defaults to LORE_REMOTE
PUBLIC_REMOTE = os.environ.get("LORE_PUBLIC_REMOTE", REMOTE)
LORE = os.path.expanduser(os.environ.get("LORE_BIN", "~/.local/bin/lore"))
CLONES = os.path.expanduser(os.environ.get("CLONES_DIR", "~/lore-web-clones"))
PREVIEWS = os.path.expanduser(os.environ.get(
    "PREVIEWS_DIR", os.path.join(CLONES, os.pardir, "lore-web-previews")))
PREVIEW_ONLY = os.environ.get("PREVIEW_ONLY", "1") == "1"
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE", "ffprobe")
PORT = int(os.environ.get("PORT", "41340"))
# "user:password" enables HTTP Basic auth on every route; empty disables it
AUTH = os.environ.get("LORE_WEB_AUTH", "")
PLUGINS_DIR = os.path.expanduser(os.environ.get(
    "PLUGINS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "plugins")))
SYNC_TTL = 60

AUDIO_EXT = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg", ".m4a", ".opus"}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}


def load_plugins():
    """Load preview plugins: any plugins/*.py exposing MATCH + inspect()."""
    import importlib.util
    mods = []
    if os.path.isdir(PLUGINS_DIR):
        for fn in sorted(os.listdir(PLUGINS_DIR)):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    f"loreweb_plugin_{fn[:-3]}", os.path.join(PLUGINS_DIR, fn))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if hasattr(mod, "MATCH") and hasattr(mod, "inspect"):
                    mod._name = fn[:-3]
                    mods.append(mod)
            except Exception as e:  # noqa: BLE001 - a bad plugin must not kill the server
                print(f"plugin {fn}: failed to load: {e}")
    return mods


PLUGINS = load_plugins()


def plugin_for(relpath):
    ext = os.path.splitext(relpath)[1].lower()
    for p in PLUGINS:
        if ext in p.MATCH:
            return p
    return None

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
    return lore_file_info(path, [rel]).get(rel, (None, "Gone", 0))


def lore_file_info(path, rels, chunk=400):
    """{rel: (hash, status, size)} for many paths in one CLI call each 400.

    `lore file info` takes any number of paths; one process per file is
    the difference between a second and a minute on a large repo.
    """
    out = {}
    for i in range(0, len(rels), chunk):
        batch = rels[i:i + chunk]
        try:
            text = run_lore(["file", "info", *batch], cwd=path,
                            timeout=300)
        except RuntimeError:
            # one bad path fails the whole batch: fall back to singles
            for rel in batch:
                try:
                    text = run_lore(["file", "info", rel], cwd=path,
                                    timeout=60)
                except RuntimeError:
                    out[rel] = (None, "Gone", 0)
                    continue
                out.update(_parse_file_info(text))
            continue
        out.update(_parse_file_info(text))
    for rel in rels:
        out.setdefault(rel, (None, "Gone", 0))
    return out


def _parse_file_info(text):
    """Parse one or more `Path:/Hash:/Status:/Size:` records."""
    out, cur = {}, None
    for line in text.splitlines():
        m = re.match(r"^(Path|Hash|Status|Size):\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key == "Path":
            cur = os.path.normpath(val)
            out[cur] = (None, "-", 0)
        elif cur is not None:
            h, st, sz = out[cur]
            if key == "Hash":
                out[cur] = (val, st, sz)
            elif key == "Status":
                out[cur] = (h, val, sz)
            elif key == "Size" and val.isdigit():
                out[cur] = (h, st, int(val))
    return out


CACHED_KEYS = ("kind", "duration", "meta", "preview", "pv_by")


def cache_path(digest, pv_by):
    return os.path.join(PREVIEWS, f"{digest}.{pv_by}.json")


def cache_get(digest, pv_by):
    """Previously computed preview/metadata for this exact content, or None.

    Keyed by content hash *and* the handler that produced it, so identical
    files anywhere (same repo, other repos) are inspected once, and a new
    plugin version still invalidates.
    """
    try:
        with open(cache_path(digest, pv_by)) as f:
            hit = json.load(f)
    except (OSError, ValueError):
        return None
    pv = hit.get("preview")
    if pv and not os.path.isfile(os.path.join(PREVIEWS, pv)):
        return None                     # cache entry outlived its artifact
    return hit


def cache_put(digest, entry):
    payload = {k: entry[k] for k in CACHED_KEYS if k in entry}
    tmp = cache_path(digest, entry.get("pv_by", "builtin")) + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, cache_path(digest, entry.get("pv_by", "builtin")))
    except OSError:
        pass


def absorb_file(path, rel, manifest, known=()):
    """File is present on disk: hash it, preview it, purge it, record it."""
    full = os.path.join(path, rel)
    digest = sha256_file(full)
    kind = file_kind(rel)
    plugin = plugin_for(rel)
    handler = plugin._name if plugin else "builtin"
    entry = manifest.get(rel, {})
    if entry.get("sha256") != digest or entry.get("pv_by") != handler:
        entry = {
            "sha256": digest,
            "size": os.path.getsize(full),
            "kind": kind,
            "mtime": int(os.path.getmtime(full)),
            "pv_by": "builtin",
        }
        hit = cache_get(digest, handler)
        if hit is not None:
            entry.update(hit)
            entry["lore_hash"] = lore_file_hash(path, rel)[0]
            manifest[rel] = entry
            if PREVIEW_ONLY:
                os.unlink(full)
            return
        if kind in ("audio", "video"):
            entry["duration"] = probe_duration(full)
        if plugin:
            try:
                res = plugin.inspect(full, {"run": run, "ffmpeg": FFMPEG,
                                            "ffprobe": FFPROBE,
                                            "repo_files": known}) or {}
                if res.get("meta"):
                    entry["meta"] = res["meta"]
                if res.get("kind"):
                    entry["kind"] = res["kind"]
                art = res.get("preview")
                if art:
                    data, fmt = art
                    pv = f"{digest}.{fmt}"
                    with open(os.path.join(PREVIEWS, pv), "wb") as f:
                        f.write(data)
                    entry["preview"] = pv
                entry["pv_by"] = plugin._name
            except Exception as e:  # noqa: BLE001 - plugin failure falls back to builtin
                print(f"plugin {plugin._name} failed on {rel}: {e}")
        if "preview" not in entry and \
                render_preview(full, kind, digest, entry.get("duration")):
            entry["preview"] = f"{digest}.png"
        cache_put(digest, entry)
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
        disk = list(scan_disk_files(path))
        known = set(manifest) | set(disk)
        for rel in disk:
            absorb_file(path, rel, manifest, known)
        # reconcile tracked-but-purged files against repo metadata
        info = lore_file_info(path, list(manifest))
        for rel in list(manifest):
            lore_hash, status, size = info.get(rel, (None, "Gone", 0))
            plugin = plugin_for(rel)
            needs_replay = plugin and \
                manifest[rel].get("pv_by") != plugin._name
            if status == "Gone" or lore_hash is None:
                del manifest[rel]
            elif manifest[rel].get("lore_hash") not in (None, lore_hash) \
                    or needs_replay:
                # a new plugin wants a look at content we've already seen
                # somewhere: take it from cache, no download needed
                handler = plugin._name if plugin else "builtin"
                hit = manifest[rel].get("sha256") and \
                    manifest[rel].get("lore_hash") == lore_hash and \
                    cache_get(manifest[rel]["sha256"], handler)
                if hit:
                    manifest[rel].update(hit)
                    continue
                # changed remotely, or uncached: re-hydrate + re-absorb
                run_lore(["reset", rel], cwd=path)
                if os.path.exists(os.path.join(path, rel)):
                    absorb_file(path, rel, manifest, known)
        save_manifest(path, manifest)
    return path


_hydrated = {}  # full path -> last access time (janitor purges idle ones)


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
    _hydrated[full] = time.time()
    return path, full


def janitor():
    """Purge hydrated files once idle (players issue many Range requests)."""
    while True:
        time.sleep(60)
        if not PREVIEW_ONLY:
            continue
        now = time.time()
        for full, last in list(_hydrated.items()):
            if now - last > 300:
                try:
                    os.unlink(full)
                except OSError:
                    pass
                _hydrated.pop(full, None)


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
        ({"path": rel,
          **{k: v for k, v in e.items() if k not in ("lore_hash", "pv_by")}}
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
.chip{display:inline-block;background:#8882;border-radius:10px;padding:0 .5rem;font-size:.75rem;margin:.15rem .15rem 0 0}
.clone{display:flex;gap:.5rem;align-items:center;margin:.25rem 0 .75rem}
.clone code{background:#8882;padding:.3rem .6rem;border-radius:6px;font-size:.8rem;overflow-x:auto;white-space:nowrap}
details.dir{margin:.15rem 0;padding-left:.75rem;border-left:1px solid #8883}
details.dir>summary{cursor:pointer;padding:.25rem .4rem;border-radius:6px;user-select:none;list-style-position:outside}
details.dir>summary:hover{background:#8882}
</style></head><body>
<nav><h1>lore-web</h1><div id="repos"></div>
<p class="muted" id="remote"></p></nav>
<main id="main"><p class="muted">Pick a repository.</p></main>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtSize=n=>n>1e9?(n/1e9).toFixed(1)+' GB':n>1e6?(n/1e6).toFixed(1)+' MB':n>1e3?(n/1e3).toFixed(1)+' KB':n+' B';
const fmtDur=s=>s==null?'':(s>=60?Math.floor(s/60)+'m'+String(Math.round(s%60)).padStart(2,'0')+'s':s+'s');
let current=null,info={};
async function j(u){const r=await fetch(u);if(!r.ok)throw new Error(await r.text());return r.json()}
function cloneCmd(name){return `lore clone ${info.public_remote||''}/${name} ${name}`}
function copyClone(name,btn){navigator.clipboard.writeText(cloneCmd(name)).then(()=>{btn.textContent='copied';setTimeout(()=>btn.textContent='copy',1200)})}
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
    <div class="clone"><code>${esc(cloneCmd(name))}</code><button onclick="copyClone('${esc(name)}',this)">copy</button></div>
    <h3>History</h3><table>${hist.map(r=>`<tr><td>#${esc(r.revision)}</td><td class="msg">${esc(r.message)}</td><td>${esc(r.date||'')}</td><td class="sig">${esc((r.signature||'').slice(0,10))}</td></tr>`).join('')||'<tr><td class=muted>no revisions</td></tr>'}</table>
    <h3>Files</h3>${renderTree(files)||'<p class="muted">empty</p>'}`;
  }catch(e){$('#main').innerHTML=`<h2>${esc(name)}</h2><p>error: ${esc(e.message)}</p>`}
}
function fileRow(f,i){
  const fu=`/api/repo/${current}/file?path=${encodeURIComponent(f.path)}`;
  const media=f.kind==='audio'?'audio':f.kind==='video'?'video':null;
  const chips=f.meta?Object.entries(f.meta).flatMap(([k,v])=>
    Array.isArray(v)?v.map(x=>`<span class="chip">${esc(x)}</span>`)
    :[`<span class="chip">${esc(k)}: ${esc(v)}</span>`]).join(''):'';
  return `<tr>
  <td><a href="${fu}" title="download">${esc(f.path.split('/').pop())}</a>
    ${media?` <button onclick="play(${i},'${media}','${fu}&inline=1')">&#9654;</button>`:''}
    ${f.preview?`<img class="pv" loading="lazy" src="/previews/${esc(f.preview)}" alt="">`:''}
    ${chips?`<div>${chips}</div>`:''}
    <div id="player-${i}"></div></td>
  <td>${fmtSize(f.size)}</td><td>${fmtDur(f.duration)}</td></tr>`;
}
function renderTree(files){
  const root={dirs:{},files:[]};
  files.forEach((f,i)=>{
    let node=root;
    for(const p of f.path.split('/').slice(0,-1))
      node=node.dirs[p]??=(node.dirs[p]={dirs:{},files:[]});
    node.files.push([f,i]);
  });
  const stats=node=>{
    let n=node.files.length,b=node.files.reduce((s,[f])=>s+(f.size||0),0);
    for(const d of Object.values(node.dirs)){const [n2,b2]=stats(d);n+=n2;b+=b2}
    return [n,b];
  };
  const render=(node,depth)=>{
    const dirs=Object.keys(node.dirs).sort((a,b)=>a.localeCompare(b)).map(d=>{
      const [n,b]=stats(node.dirs[d]);
      return `<details class="dir"${depth===0?' open':''}><summary>${esc(d)}/
        <span class="muted">${n} file${n===1?'':'s'}, ${fmtSize(b)}</span></summary>
        ${render(node.dirs[d],depth+1)}</details>`;
    }).join('');
    return dirs+(node.files.length
      ?`<table>${node.files.map(([f,i])=>fileRow(f,i)).join('')}</table>`:'');
  };
  return render(root,0);
}
function play(i,kind,url){
  const slot=$('#player-'+i);
  if(slot.firstChild){slot.innerHTML='';return}
  document.querySelectorAll('[id^=player-]').forEach(p=>p.innerHTML='');
  slot.innerHTML=kind==='audio'
    ?`<audio controls autoplay preload="none" style="width:460px" src="${url}"></audio>`
    :`<video controls autoplay preload="none" style="max-width:460px" src="${url}"></video>`;
}
fetch('/api/info').then(r=>r.json()).then(i=>{info=i;$('#remote').textContent=(i.public_remote||i.remote)+(i.ffmpeg?'':' (no ffmpeg: previews off)')}).then(loadRepos);
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

    def _send_file(self, full, inline):
        """Stream a file with single-range support (audio/video seeking)."""
        size = os.path.getsize(full)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        start, end = 0, size - 1
        status = 200
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:  # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        disp = "inline" if inline else \
            f'attachment; filename="{os.path.basename(full)}"'
        self.send_header("Content-Disposition", disp)
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(full, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 16, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _authorized(self):
        if not AUTH:
            return True
        want = "Basic " + base64.b64encode(AUTH.encode()).decode()
        if hmac.compare_digest(self.headers.get("Authorization", ""), want):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="lore-web"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self._authorized():
            return
        try:
            url = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(url.query)
            force = qs.get("refresh", ["0"])[0] == "1"
            parts = [p for p in url.path.split("/") if p]

            if url.path == "/":
                self._bytes(PAGE.encode(), "text/html; charset=utf-8")
            elif url.path == "/api/info":
                self._json({"remote": REMOTE, "public_remote": PUBLIC_REMOTE,
                            "ffmpeg": have_ffmpeg(),
                            "preview_only": PREVIEW_ONLY})
            elif url.path == "/api/repos":
                self._json(list_repos())
            elif len(parts) == 2 and parts[0] == "previews":
                name = os.path.basename(parts[1])
                full = os.path.join(PREVIEWS, name)
                m = re.fullmatch(r"[0-9a-f]{64}\.(png|svg)", name)
                if not m or not os.path.isfile(full):
                    raise KeyError("no such preview")
                ctype = "image/svg+xml" if m.group(1) == "svg" else "image/png"
                with open(full, "rb") as f:
                    self._bytes(f.read(), ctype)
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
                    inline = qs.get("inline", ["0"])[0] == "1"
                    path, full = hydrate_for_download(name, rel)
                    self._send_file(full, inline)
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
    threading.Thread(target=janitor, daemon=True).start()
    print(f"lore-web on :{PORT} -> {REMOTE}")
    print(f"  clones: {CLONES}  previews: {PREVIEWS}  "
          f"preview_only={PREVIEW_ONLY} ffmpeg={have_ffmpeg()} "
          f"auth={'on' if AUTH else 'off'}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
