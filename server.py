#!/usr/bin/env python3
"""lore-web: a read-only web viewer for a Lore VCS server.

Single file, Python stdlib only. Wraps the `lore` CLI: the server keeps one
clone per repository (hydrated at head) and serves repo list, commit history,
file tree, and file downloads over HTTP. No auth -- intended for LAN use next
to an unauthenticated pilot loreserver.

Env config:
  LORE_REMOTE  lore://127.0.0.1:41337   remote loreserver
  LORE_BIN     ~/.local/bin/lore        lore CLI binary
  CLONES_DIR   ~/lore-web-clones        where viewer clones live
  PORT         41340                    HTTP port
"""
import html
import json
import os
import re
import subprocess
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REMOTE = os.environ.get("LORE_REMOTE", "lore://127.0.0.1:41337")
LORE = os.path.expanduser(os.environ.get("LORE_BIN", "~/.local/bin/lore"))
CLONES = os.path.expanduser(os.environ.get("CLONES_DIR", "~/lore-web-clones"))
PORT = int(os.environ.get("PORT", "41340"))
SYNC_TTL = 60  # seconds between automatic syncs per repo

_last_sync = {}


def run_lore(args, cwd=None, timeout=120):
    p = subprocess.run([LORE, *args], cwd=cwd, capture_output=True,
                       text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError(f"lore {' '.join(args)}: {p.stderr.strip()[:500]}")
    return p.stdout


def list_repos():
    out = run_lore(["repository", "list", REMOTE])
    repos = []
    for line in out.splitlines():
        m = re.match(r"^(\S+) \(([0-9a-f]+)\)\s*$", line.strip())
        if m:
            repos.append({"name": m.group(1), "id": m.group(2)})
    return repos


def repo_path(name):
    # whitelist against the server's own repo list; no path characters sneak in
    if not any(r["name"] == name for r in list_repos()):
        raise KeyError(f"unknown repository {name!r}")
    return os.path.join(CLONES, name)


def ensure_clone(name, refresh=False):
    path = repo_path(name)
    if not os.path.isdir(os.path.join(path, ".lore")):
        os.makedirs(CLONES, exist_ok=True)
        run_lore(["clone", f"{REMOTE}/{name}", path], timeout=600)
        _last_sync[name] = time.time()
    elif refresh or time.time() - _last_sync.get(name, 0) > SYNC_TTL:
        run_lore(["sync"], cwd=path, timeout=600)
        _last_sync[name] = time.time()
    return path


def history(name, refresh=False):
    path = ensure_clone(name, refresh)
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


def tree(name, refresh=False):
    path = ensure_clone(name, refresh)
    entries = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d != ".lore"]
        rel = os.path.relpath(root, path)
        for f in sorted(files):
            fp = os.path.join(root, f)
            st = os.stat(fp)
            entries.append({
                "path": os.path.normpath(os.path.join(rel, f)).lstrip("./"),
                "size": st.st_size,
                "mtime": int(st.st_mtime),
            })
    return sorted(entries, key=lambda e: e["path"])


def safe_file(name, relpath):
    base = os.path.realpath(ensure_clone(name))
    full = os.path.realpath(os.path.join(base, relpath))
    if not full.startswith(base + os.sep) or ".lore" in full.split(os.sep):
        raise KeyError("bad path")
    if not os.path.isfile(full):
        raise KeyError("not a file")
    return full


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>lore-web</title><style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{margin:0;display:grid;grid-template-columns:220px 1fr;min-height:100vh}
nav{border-right:1px solid #8884;padding:1rem}
nav h1{font-size:1rem;margin:0 0 .75rem}
nav a{display:block;padding:.35rem .5rem;border-radius:6px;text-decoration:none;color:inherit}
nav a.sel,nav a:hover{background:#8882}
main{padding:1rem 1.5rem;max-width:60rem}
table{border-collapse:collapse;width:100%;font-size:.9rem}
td,th{text-align:left;padding:.3rem .6rem;border-bottom:1px solid #8883}
.sig{font-family:monospace;font-size:.8rem;opacity:.7}
.msg{white-space:pre-wrap}
button{margin-left:.5rem}
h2{display:flex;align-items:center;font-size:1.1rem}
.muted{opacity:.6;font-size:.85rem}
</style></head><body>
<nav><h1>lore-web</h1><div id="repos"></div>
<p class="muted" id="remote"></p></nav>
<main id="main"><p class="muted">Pick a repository.</p></main>
<script>
const $=s=>document.querySelector(s);
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtSize=n=>n>1e9?(n/1e9).toFixed(1)+' GB':n>1e6?(n/1e6).toFixed(1)+' MB':n>1e3?(n/1e3).toFixed(1)+' KB':n+' B';
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
  $('#main').innerHTML=`<h2>${esc(name)} <button onclick="show(current,1)">sync</button></h2><p class="muted">loading${refresh?' (syncing)':''}…</p>`;
  const q=refresh?'?refresh=1':'';
  try{
    const [hist,files]=await Promise.all([j(`/api/repo/${name}/history${q}`),j(`/api/repo/${name}/tree`)]);
    $('#main').innerHTML=`<h2>${esc(name)} <button onclick="show(current,1)">sync</button></h2>
    <h3>History</h3><table>${hist.map(r=>`<tr><td>#${r.revision}</td><td class="msg">${esc(r.message)}</td><td>${esc(r.date||'')}</td><td class="sig">${(r.signature||'').slice(0,10)}</td></tr>`).join('')||'<tr><td class=muted>no revisions</td></tr>'}</table>
    <h3>Files</h3><table>${files.map(f=>`<tr><td><a href="/api/repo/${name}/file?path=${encodeURIComponent(f.path)}">${esc(f.path)}</a></td><td>${fmtSize(f.size)}</td></tr>`).join('')||'<tr><td class=muted>empty</td></tr>'}</table>`;
  }catch(e){$('#main').innerHTML=`<h2>${esc(name)}</h2><p>error: ${esc(String(e.message))}</p>`}
}
loadRepos();
fetch('/api/info').then(r=>r.json()).then(i=>$('#remote').textContent=i.remote);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            url = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(url.query)
            refresh = qs.get("refresh", ["0"])[0] == "1"
            parts = [p for p in url.path.split("/") if p]

            if url.path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/info":
                self._json({"remote": REMOTE})
            elif url.path == "/api/repos":
                self._json(list_repos())
            elif len(parts) == 4 and parts[:2] == ["api", "repo"]:
                name, action = parts[2], parts[3]
                if action == "history":
                    self._json(history(name, refresh))
                elif action == "tree":
                    self._json(tree(name, refresh))
                elif action == "file":
                    full = safe_file(name, qs.get("path", [""])[0])
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{os.path.basename(full)}"')
                    self.send_header("Content-Length", str(os.path.getsize(full)))
                    self.end_headers()
                    with open(full, "rb") as f:
                        while chunk := f.read(1 << 16):
                            self.wfile.write(chunk)
                else:
                    self._json({"error": "unknown action"}, 404)
            else:
                self._json({"error": "not found"}, 404)
        except KeyError as e:
            self._json({"error": str(e)}, 404)
        except Exception as e:  # noqa: BLE001 - surface everything to the LAN client
            self._json({"error": str(e)}, 500)

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}")


if __name__ == "__main__":
    os.makedirs(CLONES, exist_ok=True)
    print(f"lore-web on :{PORT} -> {REMOTE} (clones in {CLONES})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
