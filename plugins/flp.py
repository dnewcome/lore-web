"""lore-web plugin: FruityLoops/FL Studio projects (.flp) -> pattern-grid SVG
+ session metadata.

Pure-stdlib FLP event-stream parser. The container (FLhd header + FLdt event
stream) is unchanged since FruityLoops 1.x (1998). Pattern content comes in
two encodings, both handled here:

- FruityLoops 1.x: event 68 is a 16-bit step-sequencer bitmask for
  (current channel, current pattern) - e.g. 0x1111 is a four-on-the-floor
- FL ~2-5: event 91, one word per lit step - low byte step, high byte channel
- FL 3.5+: event 224 carries packed note structs (20 bytes before FL 6,
  24 bytes after; pos u32, flags u16, rack u16, dur u32 @8, key u8 @12)

Preview: channel rows x pattern lanes; lit steps as cells, notes as blocks
with a pitch-contour wiggle. Falls back to a channel-rack list when a
project has no pattern data. (pyflp 2.2.1 crashes on Python >= 3.11, hence
hand-rolled.)
"""
import struct
from xml.sax.saxutils import escape

MATCH = [".flp"]

EV_NEW_CHAN = 64
EV_NEW_PAT = 65
EV_TEMPO = 66
EV_STEP_MASK = 68       # word: 16-step bitmask (FruityLoops 1.x)
EV_STEP_ONE = 91        # word: one lit step, chan << 8 | step (FL ~2-5)
EV_FINE_TEMPO = 156     # dword: BPM * 1000 (newer FL)
EV_CHAN_NAME = 192
EV_TITLE = 194
EV_SAMPLE_PATH = 196
EV_VERSION = 199
EV_PAT_NOTES = 224      # packed note structs (FL 3.5+)

SAMPLER = "#5cb85c"
GENERATOR = "#4a9eda"
COLORS = ["#4a9eda", "#e0823d", "#5cb85c", "#c9556f", "#9067c6",
          "#b8a637", "#50b8b0", "#8a8a8a"]


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
    ppq = struct.unpack("<H", data[12:14])[0] or 96
    i = 8 + hlen
    if data[i:i + 4] != b"FLdt":
        raise ValueError("no FLdt chunk")
    end = i + 8 + struct.unpack("<I", data[i + 4:i + 8])[0]
    i += 8
    out = {"version": None, "tempo": None, "title": None, "ppq": ppq}
    channels = []           # {"name", "sample"}
    steps = {}              # (chan_idx, pat) -> set of lit step positions
    notes = []              # (pat, rack, pos, dur, key)
    cur_pat = None

    def chan(need):
        if not channels or (need and channels[-1].get(need) is not None):
            channels.append({"name": None, "sample": None})
        return channels[-1]

    while i < min(end, len(data)):
        ev = data[i]
        i += 1
        if ev < 64:
            i += 1
        elif ev < 128:
            (val,) = struct.unpack("<H", data[i:i + 2])
            i += 2
            if ev == EV_NEW_CHAN:
                channels.append({"name": None, "sample": None})
            elif ev == EV_TEMPO and out["tempo"] is None:
                out["tempo"] = val
            elif ev == EV_STEP_MASK and val and cur_pat is not None \
                    and channels:
                steps.setdefault((len(channels) - 1, cur_pat), set()) \
                    .update(b for b in range(16) if val >> b & 1)
            elif ev == EV_STEP_ONE and cur_pat is not None:
                steps.setdefault((val >> 8, cur_pat), set()).add(val & 0xFF)
            elif ev == EV_NEW_PAT:
                cur_pat = val
        elif ev < 192:
            (val,) = struct.unpack("<I", data[i:i + 4])
            i += 4
            if ev == EV_FINE_TEMPO:
                out["tempo"] = round(val / 1000)
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
                chan("name")["name"] = _text(payload)
            elif ev == EV_SAMPLE_PATH:
                chan("sample")["sample"] = _text(payload)
            elif ev == EV_PAT_NOTES and payload and cur_pat is not None:
                try:
                    major = int((out["version"] or "0").split(".")[0])
                except ValueError:
                    major = 0
                size = 24 if major >= 6 else 20
                if len(payload) % size:
                    size = 44 - size          # try the other layout
                    if len(payload) % size:
                        continue
                for k in range(0, len(payload), size):
                    pos, _flags, rack = struct.unpack_from("<IHH", payload, k)
                    (dur,) = struct.unpack_from("<I", payload, k + 8)
                    notes.append((cur_pat, rack, pos, dur, payload[k + 12]))

    out["channels"] = channels
    out["steps"] = steps
    out["notes"] = notes
    return out


def _pattern_svg(info, rows):
    """rows: [(orig_idx, channel)] - channels with content, capped."""
    steps, notes, ppq = info["steps"], info["notes"], info["ppq"]
    pats = sorted({p for _, p in steps} | {n[0] for n in notes})
    if not pats or not rows:
        return None
    shown_pats = pats[:12]
    row_of = {orig: r for r, (orig, _) in enumerate(rows)}
    keys = [n[4] for n in notes]
    klo, kspan = (min(keys), max(max(keys) - min(keys), 1)) if keys else (0, 1)

    w, name_w, rh = 460, 122, 18
    lane_x, lane_w = name_w + 4, w - name_w - 4
    patw = lane_w / len(shown_pats)
    h = len(rows) * rh
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
         f'viewBox="0 0 {w} {h}" font-family="system-ui,sans-serif" '
         f'font-size="9">']
    for r, (orig, c) in enumerate(rows):
        y = r * rh
        tint = SAMPLER if c["sample"] else GENERATOR
        name = c["name"] or (c["sample"] or "?").replace("\\", "/") \
            .rsplit("/", 1)[-1]
        s.append(f'<rect x="0" y="{y + 2}" width="{name_w}" '
                 f'height="{rh - 4}" rx="3" fill="{tint}" '
                 f'fill-opacity="0.25"/>')
        s.append(f'<rect x="0" y="{y + 2}" width="3" height="{rh - 4}" '
                 f'fill="{tint}"/>')
        s.append(f'<text x="8" y="{y + rh - 6}" fill="currentColor">'
                 f'{escape(name[:20])}</text>')
    # pattern lane backgrounds (alternating)
    for pi in range(len(shown_pats)):
        if pi % 2:
            s.append(f'<rect x="{lane_x + pi * patw:.1f}" y="0" '
                     f'width="{patw:.1f}" height="{h}" fill="#8888" '
                     f'fill-opacity="0.08"/>')
    # step cells; a pattern's grid is 16 steps unless a step sits beyond it
    nsteps = {}
    for (_ch, pat), lit in steps.items():
        mx = max(lit, default=0)
        need = -(-(mx + 1) // 4) * 4 if mx >= 16 else 16
        nsteps[pat] = max(nsteps.get(pat, 16), need)
    for (ch, pat), lit in steps.items():
        if pat not in shown_pats or ch not in row_of:
            continue
        x0 = lane_x + shown_pats.index(pat) * patw
        cw = patw / nsteps[pat]
        y = row_of[ch] * rh
        color = COLORS[row_of[ch] % len(COLORS)]
        for b in sorted(lit):
            s.append(f'<rect x="{x0 + b * cw:.1f}" y="{y + 4}" '
                     f'width="{max(cw - 0.6, 0.8):.1f}" '
                     f'height="{rh - 8}" fill="{color}"/>')
    # notes
    patlen = {}
    for pat, _rack, pos, dur, _k in notes:
        patlen[pat] = max(patlen.get(pat, 4 * ppq), pos + dur)
    for pat, rack, pos, dur, key in notes:
        if pat not in shown_pats or rack not in row_of:
            continue
        pl = patlen[pat]
        x0 = lane_x + shown_pats.index(pat) * patw
        x = x0 + pos / pl * patw
        nw = max(dur / pl * patw, 1)
        y = row_of[rack] * rh + 3 + (1 - (key - klo) / kspan) * (rh - 10)
        color = COLORS[row_of[rack] % len(COLORS)]
        s.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{nw:.1f}" '
                 f'height="3.4" rx="1" fill="{color}"/>')
    if len(pats) > len(shown_pats):
        s.append(f'<text x="{w - 4}" y="{h - 4}" text-anchor="end" '
                 f'fill="currentColor" opacity="0.6">'
                 f'+{len(pats) - len(shown_pats)} pats</text>')
    s.append("</svg>")
    return "".join(s).encode()


def _rack_svg(rows):
    s, rh, w = [], 18, 460
    shown = rows[:14]
    h = max(len(shown) * rh, rh)
    s.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" '
             f'height="{h}" viewBox="0 0 {w} {h}" '
             f'font-family="system-ui,sans-serif" font-size="11">')
    for i, (_orig, c) in enumerate(shown):
        color = SAMPLER if c["sample"] else GENERATOR
        name = c["name"] or (c["sample"] or "?").replace("\\", "/") \
            .rsplit("/", 1)[-1]
        y = i * rh
        s.append(f'<rect x="0" y="{y + 2}" width="{w}" height="{rh - 4}" '
                 f'rx="3" fill="{color}" fill-opacity="0.25"/>')
        s.append(f'<rect x="0" y="{y + 2}" width="4" height="{rh - 4}" '
                 f'fill="{color}"/>')
        s.append(f'<text x="10" y="{y + rh - 6}" fill="currentColor">'
                 f'{escape(name[:60])}</text>')
    s.append("</svg>")
    return "".join(s).encode()


def inspect(path, ctx):
    with open(path, "rb") as f:
        info = parse_flp(f.read())
    rows = [(i, c) for i, c in enumerate(info["channels"])
            if c["name"] or c["sample"]]
    n_samples = sum(1 for _, c in rows if c["sample"])
    n_steps = sum(len(s) for s in info["steps"].values())
    pats = {p for _, p in info["steps"]} | {n[0] for n in info["notes"]}

    meta = {"channels": len(rows), "samples": n_samples}
    if pats:
        meta["patterns"] = len(pats)
    if info["notes"] or n_steps:
        meta["notes"] = len(info["notes"]) + n_steps
    if info["tempo"]:
        meta["bpm"] = info["tempo"]
    if info["version"]:
        meta["fl"] = info["version"]
    if info["title"]:
        meta["title"] = info["title"][:60]

    svg = _pattern_svg(info, rows[:14]) or _rack_svg(rows)
    return {"meta": meta, "kind": "flp", "preview": (svg, "svg")}
