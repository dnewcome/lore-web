#!/usr/bin/env python3
"""flscan — read-only scan of a FruityLoops-era project archive.

Walks a directory tree, content-hashes audio and .flp files — including
members of .zip archives, recorded as "archive.zip!member" — parses every
.flp (minimal FLP event-stream parser, FruityLoops 1.x through FL Studio),
resolves each project's sample references against the scanned tree, and
emits a duplication/sharing report: unique vs duplicate samples, wasted
bytes, and which projects reference which samples.

Never writes inside the scanned tree. Stdlib only.

    python3 flscan.py /path/to/archive -o report.json
"""
import argparse
import hashlib
import json
import os
import re
import struct
import sys
import time
import zipfile

AUDIO_EXTS = {".wav", ".aif", ".aiff", ".mp3", ".ogg", ".flac", ".wv",
              ".m4a", ".wma", ".mod", ".xm", ".it", ".s3m"}

# ---------------------------------------------------------------- FLP parser
# Container: "FLhd" + 6-byte header (format, nChannels, PPQ; LE words),
# then "FLdt" + dword length + event stream. Event ids: <64 byte value,
# <128 word, <192 dword, >=192 varint length + payload. Stable since 1998.

EV_TEMPO = 66           # word: BPM (legacy)
EV_FINE_TEMPO = 156     # dword: BPM * 1000 (newer FL)
EV_CHAN_NAME = 192
EV_TITLE = 194
EV_SAMPLE_PATH = 196
EV_VERSION = 199


def _text(data):
    if len(data) >= 2 and data.count(0) >= len(data) // 2 - 1:
        try:
            return data.decode("utf-16-le").rstrip("\x00")
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1").rstrip("\x00")


def parse_flp(data):
    if data[:4] != b"FLhd":
        raise ValueError("no FLhd magic")
    hlen = struct.unpack("<I", data[4:8])[0]
    _fmt, nchan, ppq = struct.unpack("<HHH", data[8:14])
    i = 8 + hlen
    if data[i:i + 4] != b"FLdt":
        raise ValueError("no FLdt chunk")
    end = i + 8 + struct.unpack("<I", data[i + 4:i + 8])[0]
    i += 8
    out = {"version": None, "tempo": None, "title": None,
           "channels": nchan, "ppq": ppq, "channel_names": [], "samples": []}
    while i < min(end, len(data)):
        ev = data[i]
        i += 1
        if ev < 64:
            i += 1
        elif ev < 128:
            (val,) = struct.unpack("<H", data[i:i + 2])
            i += 2
            if ev == EV_TEMPO and out["tempo"] is None:
                out["tempo"] = val
        elif ev < 192:
            (val,) = struct.unpack("<I", data[i:i + 4])
            i += 4
            if ev == EV_FINE_TEMPO:
                out["tempo"] = val / 1000
        else:
            ln, shift = 0, 0
            while True:
                b = data[i]
                i += 1
                ln |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            payload = data[i:i + ln]
            i += ln
            if ev == EV_VERSION:
                out["version"] = _text(payload)
            elif ev == EV_TITLE and not out["title"]:
                out["title"] = _text(payload).strip() or None
            elif ev == EV_CHAN_NAME:
                out["channel_names"].append(_text(payload))
            elif ev == EV_SAMPLE_PATH:
                out["samples"].append(_text(payload))
    return out


# ------------------------------------------------------------ reference join

DRIVE_RE = re.compile(r"^[a-zA-Z]:[/\\]")


def classify_ref(raw, root_name):
    """-> (kind, rel_guess) where kind is factory|archive|external and
    rel_guess is a root-relative path when derivable from the raw ref."""
    p = raw.replace("\\", "/")
    if p.startswith("%") or not DRIVE_RE.match(p) and p.startswith("/"):
        return "factory", None
    if not DRIVE_RE.match(p):
        return "factory", None
    body = p[3:]
    low = body.lower()
    marker = root_name.lower() + "/"
    if marker in low:
        return "archive", body[low.index(marker) + len(marker):]
    return "external", None


def resolve_ref(raw, root_name, by_relpath, by_basename):
    kind, rel = classify_ref(raw, root_name)
    ref = {"ref": raw, "kind": kind, "status": "unresolved", "path": None}
    if kind == "factory":
        ref["status"] = "factory"
        return ref
    if rel:
        hit = by_relpath.get(rel.lower())
        if hit:
            ref.update(status="resolved", path=hit["path"])
            return ref
    base = raw.replace("\\", "/").rsplit("/", 1)[-1].lower()
    hits = by_basename.get(base, [])
    if len(hits) == 1:
        ref.update(status="resolved", path=hits[0]["path"])
    elif hits:
        hashes = {h["sha256"] for h in hits}
        if len(hashes) == 1 and None not in hashes:
            ref.update(status="resolved", path=hits[0]["path"])
        else:
            ref.update(status="ambiguous",
                       path=[h["path"] for h in hits[:8]])
    else:
        ref["status"] = "missing"
    return ref


# ------------------------------------------------------------------- scanner

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(root, out_path):
    root = os.path.abspath(root)
    root_name = os.path.basename(root.rstrip("/"))
    files, errors = [], []
    other_exts, zip_bytes = {}, 0
    t0 = time.time()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, root)
            ext = os.path.splitext(name)[1].lower()
            try:
                st = os.stat(full)
            except OSError as e:
                errors.append({"path": rel, "error": str(e)})
                continue
            entry = {"path": rel, "size": st.st_size,
                     "mtime": time.strftime("%Y-%m-%d",
                                            time.localtime(st.st_mtime)),
                     "ext": ext, "sha256": None}
            if ext in AUDIO_EXTS or ext == ".flp":
                files.append(entry)
            else:
                other_exts[ext or "(none)"] = \
                    [other_exts.get(ext or "(none)", [0, 0])[0] + 1,
                     other_exts.get(ext or "(none)", [0, 0])[1] + st.st_size]
                if ext == ".zip":
                    zip_bytes += st.st_size
                    try:
                        with zipfile.ZipFile(full) as z:
                            infos = z.infolist()
                    except Exception as e:
                        errors.append({"path": rel, "error": f"zip: {e}"})
                        continue
                    for zi in infos:
                        if zi.is_dir():
                            continue
                        mext = os.path.splitext(zi.filename)[1].lower()
                        if mext not in AUDIO_EXTS and mext != ".flp":
                            continue
                        files.append({
                            "path": rel + "!" + zi.filename.replace("\\", "/"),
                            "zip": rel, "member": zi.filename,
                            "size": zi.file_size,
                            "mtime": "%04d-%02d-%02d" % zi.date_time[:3],
                            "ext": mext, "sha256": None})

    total_bytes = sum(f["size"] for f in files)
    done_bytes = 0
    flps = []
    cur_zip_rel, cur_zip = None, None
    for n, f in enumerate(files):
        try:
            if "zip" in f:
                if f["zip"] != cur_zip_rel:
                    if cur_zip:
                        cur_zip.close()
                    cur_zip_rel = f["zip"]
                    cur_zip = zipfile.ZipFile(os.path.join(root, f["zip"]))
                with cur_zip.open(f["member"]) as fh:
                    h = hashlib.sha256()
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                f["sha256"] = h.hexdigest()
                read_flp = (lambda m=f["member"]: cur_zip.read(m))
            else:
                full = os.path.join(root, f["path"])
                f["sha256"] = sha256_file(full)
                read_flp = (lambda p=full: open(p, "rb").read())
        except Exception as e:
            errors.append({"path": f["path"], "error": str(e)})
            continue
        done_bytes += f["size"]
        if f["ext"] == ".flp":
            try:
                flps.append({**f, **parse_flp(read_flp())})
            except Exception as e:
                flps.append({**f, "parse_error":
                             f"{type(e).__name__}: {e}"})
        if n == len(files) - 1 and cur_zip:
            cur_zip.close()
        if n % 500 == 0:
            rate = done_bytes / max(time.time() - t0, 1) / (1 << 20)
            print(f"  hashed {n}/{len(files)} files "
                  f"({done_bytes / (1 << 30):.1f}/"
                  f"{total_bytes / (1 << 30):.1f} GiB, "
                  f"{rate:.0f} MiB/s)", file=sys.stderr, flush=True)

    # indexes for reference resolution
    by_relpath = {f["path"].lower(): f for f in files}
    by_basename = {}
    for f in files:
        by_basename.setdefault(
            f["path"].rsplit("/", 1)[-1].lower(), []).append(f)

    hash_to_flps = {}
    for flp in flps:
        refs = []
        for raw in flp.pop("samples", []):
            r = resolve_ref(raw, root_name, by_relpath, by_basename)
            if r["status"] == "resolved":
                r["sha256"] = by_relpath[r["path"].lower()]["sha256"]
                hash_to_flps.setdefault(r["sha256"], set()).add(flp["path"])
            refs.append(r)
        flp["refs"] = refs

    # duplicate groups among audio files
    by_hash = {}
    for f in files:
        if f["ext"] != ".flp" and f["sha256"]:
            by_hash.setdefault(f["sha256"], []).append(f)
    dup_groups = sorted(
        ({"sha256": h, "size": g[0]["size"], "count": len(g),
          "wasted": (len(g) - 1) * g[0]["size"],
          "paths": [x["path"] for x in g]}
         for h, g in by_hash.items() if len(g) > 1),
        key=lambda d: -d["wasted"])

    flp_by_hash = {}
    for f in files:
        if f["ext"] == ".flp":
            flp_by_hash.setdefault(f["sha256"], []).append(f["path"])
    flp_dups = sorted(([len(v)] + v for v in flp_by_hash.values()
                       if len(v) > 1), key=lambda d: -d[0])

    shared = sorted(
        ({"sha256": h, "projects": sorted(ps),
          "paths": [x["path"] for x in by_hash.get(h, [])][:4]}
         for h, ps in hash_to_flps.items() if len(ps) > 1),
        key=lambda s: -len(s["projects"]))

    audio = [f for f in files if f["ext"] != ".flp"]
    report = {
        "root": root,
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": round(time.time() - t0, 1),
        "totals": {
            "audio_files": len(audio),
            "audio_bytes": sum(f["size"] for f in audio),
            "audio_unique_hashes": len(by_hash),
            "audio_dup_groups": len(dup_groups),
            "audio_wasted_bytes": sum(d["wasted"] for d in dup_groups),
            "flp_files": len(flps),
            "flp_parsed": sum(1 for f in flps if "parse_error" not in f),
            "flp_dup_sets": len(flp_dups),
            "other_files": {k: v for k, v in sorted(
                other_exts.items(), key=lambda kv: -kv[1][1])},
            "zip_bytes": zip_bytes,
            "zip_members": sum(1 for f in files if "zip" in f),
            "errors": len(errors),
        },
        "dup_groups": dup_groups,
        "flp_dups": flp_dups,
        "shared_samples": shared,
        "flps": flps,
        "audio_index": audio,
        "errors": errors,
    }
    with open(out_path, "w") as f:
        json.dump(report, f, indent=1)
    return report


# ------------------------------------------------------------------- summary

def fmt_bytes(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def summarize(r):
    t = r["totals"]
    lines = [f"# flscan: {r['root']}  ({r['scanned_at']}, "
             f"{r['duration_s']}s)", ""]
    lines += [
        f"audio files : {t['audio_files']} "
        f"({fmt_bytes(t['audio_bytes'])}), "
        f"{t['audio_unique_hashes']} unique by content",
        f"duplicates  : {t['audio_dup_groups']} groups, "
        f"{fmt_bytes(t['audio_wasted_bytes'])} reclaimable",
        f".flp files  : {t['flp_files']} "
        f"({t['flp_parsed']} parsed ok, "
        f"{t['flp_files'] - t['flp_parsed']} failed), "
        f"{t['flp_dup_sets']} exact-duplicate sets",
        f"zips        : {t['zip_members']} audio/flp members scanned "
        f"inside ({fmt_bytes(t['zip_bytes'])} of zips total; members "
        f"counted at uncompressed size)",
        f"errors      : {t['errors']}", ""]

    versions = {}
    for f in r["flps"]:
        v = (f.get("version") or "?").split(".")[0]
        versions[v] = versions.get(v, 0) + 1
    lines.append("FL major versions: " + ", ".join(
        f"{k} ({v})" for k, v in sorted(versions.items())))

    statuses = {}
    for f in r["flps"]:
        for ref in f.get("refs", []):
            statuses[ref["status"]] = statuses.get(ref["status"], 0) + 1
    lines.append("sample refs: " + ", ".join(
        f"{k} {v}" for k, v in sorted(statuses.items(), key=lambda kv: -kv[1])))
    lines.append("")

    lines.append("top duplicate groups by wasted bytes:")
    for d in r["dup_groups"][:15]:
        lines.append(f"  {d['count']}x {fmt_bytes(d['size'])} "
                     f"(waste {fmt_bytes(d['wasted'])})  "
                     f"{d['paths'][0].rsplit('/', 1)[-1]}")
    lines.append("")
    lines.append("samples shared across most projects:")
    for s in r["shared_samples"][:10]:
        name = s["paths"][0].rsplit("/", 1)[-1] if s["paths"] else s["sha256"][:12]
        lines.append(f"  {len(s['projects'])} projects  {name}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("root")
    ap.add_argument("-o", "--out", default="flscan-report.json")
    args = ap.parse_args()
    report = scan(args.root, args.out)
    print(summarize(report))
    print(f"\nfull report: {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
