"""lore-web plugin: MIDI files -> piano-roll SVG + track/note metadata.

Pure stdlib Standard MIDI File parser (format 0/1, running status, note
on/off pairing). Preview is an SVG piano roll colored per track.
"""
import struct

MATCH = [".mid", ".midi"]

COLORS = ["#4a9eda", "#e0823d", "#5cb85c", "#c9556f", "#9067c6",
          "#b8a637", "#50b8b0", "#8a8a8a"]


def _varlen(data, i):
    val = 0
    while True:
        b = data[i]
        i += 1
        val = (val << 7) | (b & 0x7F)
        if not b & 0x80:
            return val, i


def _parse(path):
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"MThd":
        raise ValueError("not a MIDI file")
    _, fmt, ntrk, division = struct.unpack(">IHHH", data[4:14])
    notes = []          # (track, tick, pitch, dur_ticks)
    tempo = None        # first tempo (us/quarter)
    max_tick = 0
    i = 14
    for trk in range(ntrk):
        if data[i:i + 4] != b"MTrk":
            break
        (length,) = struct.unpack(">I", data[i + 4:i + 8])
        j, end = i + 8, i + 8 + length
        tick, status = 0, 0
        active = {}     # (ch, pitch) -> start tick
        while j < end:
            delta, j = _varlen(data, j)
            tick += delta
            b = data[j]
            if b & 0x80:
                status = b
                j += 1
            ev = status & 0xF0
            if ev == 0x90 and data[j + 1] > 0:          # note on
                active[(status & 0x0F, data[j])] = tick
                j += 2
            elif ev == 0x80 or (ev == 0x90 and data[j + 1] == 0):
                key = (status & 0x0F, data[j])
                start = active.pop(key, None)
                if start is not None:
                    notes.append((trk, start, data[j], max(tick - start, 1)))
                j += 2
            elif ev in (0xA0, 0xB0, 0xE0):
                j += 2
            elif ev in (0xC0, 0xD0):
                j += 1
            elif status == 0xFF:                         # meta
                mtype = data[j]
                mlen, j2 = _varlen(data, j + 1)
                if mtype == 0x51 and tempo is None:
                    tempo = int.from_bytes(data[j2:j2 + 3], "big")
                j = j2 + mlen
            elif status in (0xF0, 0xF7):                 # sysex
                mlen, j2 = _varlen(data, j)
                j = j2 + mlen
            else:
                j += 1
        max_tick = max(max_tick, tick)
        i = end
    return ntrk, division, tempo, notes, max_tick


def _svg(notes, max_tick, w=900, h=140):
    if not notes or not max_tick:
        return None
    lo = min(n[2] for n in notes)
    hi = max(n[2] for n in notes)
    span = max(hi - lo + 1, 12)
    rows = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" '
            f'height="{h}" viewBox="0 0 {w} {h}">'
            f'<rect width="{w}" height="{h}" fill="#00000010"/>']
    nh = max(min(h / span, 6), 1.5)
    for trk, start, pitch, dur in notes[:4000]:
        x = start / max_tick * w
        nw = max(dur / max_tick * w, 1.5)
        y = h - ((pitch - lo + 1) / span * (h - nh))
        c = COLORS[trk % len(COLORS)]
        rows.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{nw:.1f}" '
                    f'height="{nh:.1f}" rx="1" fill="{c}"/>')
    rows.append("</svg>")
    return "".join(rows).encode()


def inspect(path, ctx):
    ntrk, division, tempo, notes, max_tick = _parse(path)
    bpm = round(60_000_000 / tempo) if tempo else None
    meta = {"tracks": ntrk, "notes": len(notes)}
    if bpm:
        meta["bpm"] = bpm
    if division and max_tick and tempo:
        meta["length"] = f"{max_tick / division * tempo / 1e6:.0f}s"
    out = {"meta": meta, "kind": "midi"}
    svg = _svg(notes, max_tick)
    if svg:
        out["preview"] = (svg, "svg")
    return out
