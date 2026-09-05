"""lore-web plugin: Pure Data patches -> patch diagram SVG + object census.

A .pd file is a plain-text list of records: object positions, their class
and arguments, and the cords between them.  Everything needed to draw the
patch is right there, so the viewer can show what a patch *looks like*
without Pd, without a display, and without the externals it depends on --
which is the point, since a 2012 GEM patch will not open on a machine that
no longer has GEM installed.

What the format makes easy, and what it does not:

  * **Positions are exact**; box *sizes* are not stored.  Pd recomputes them
    from the text and the canvas font at load time, so we redo that with
    Pd's own font metrics (sys_fontlist) and get boxes within a pixel or two.
  * **Inlet and outlet counts are not stored either** -- they belong to the
    object class, which lives in Pd or in an external we do not have.  They
    are inferred from the cords actually present, which is exact for every
    inlet/outlet a patch really uses and invisible for the rest.
  * **Records wrap across lines.**  A record ends at an unescaped ';', not at
    a newline, and long object boxes routinely span several lines, so this
    parses by record rather than by line.

The object census is the other half: it reports which classes a patch uses
and flags the ones that are not Pd vanilla, which is how you find out that
a patch needs GEM or zexy before trying to open it.
"""
import os
import re

MATCH = [".pd"]

# Pd's own font table (sys_fontlist in s_main.c): size -> (width, height).
# Box geometry is derived from these, so they have to match Pd's, not look
# plausible.
FONTS = {8: (5, 11), 10: (6, 13), 12: (7, 16),
         16: (10, 19), 24: (14, 29), 36: (22, 44)}

BOX_PAD_X = 5           # Pd's LMARGIN + RMARGIN
# message boxes and atoms give up part of their right edge to the flag or
# the clipped corner, so they need that width back or the text runs into it
FLAG_W = {"msg": 6, "floatatom": 5, "symbolatom": 5, "listbox": 5}
BOX_PAD_Y = 4           # TMARGIN + BMARGIN
IO_W, IO_H = 7, 2       # inlet/outlet nub
MAX_W = 1200            # cap on emitted SVG width
COMMENT_WRAP = 60       # chars before a comment wraps, when not set explicitly

# GUI classes and where their pixel size lives in the argument list.
# (width_arg, height_arg); None means "square, from width".
GUI_SIZE = {
    "bng": (0, None), "tgl": (0, None),
    "cnv": (1, 2),
    "hsl": (0, 1), "vsl": (0, 1),
    "hradio": (0, None), "vradio": (0, None),
    "vu": (0, 1),
    "nbx": (0, 1),
}

# Pd vanilla, near enough to tell "needs an external" from "does not".
VANILLA = set("""
bang b float f symbol int i send s receive r select sel route pack unpack
trigger t spigot moses until print makefilename change swap value v list
delay del metro line timer cputime realtime pipe text array struct element
get set getsize setsize append scalar drawcurve drawpolygon drawnumber plot
mtof ftom powtodb rmstodb dbtopow dbtorms mod div sin cos tan atan atan2
sqrt log exp abs random max min clip wrap expr expr~ fexpr~ pow
tabread tabread4 tabwrite tabsend tabreceive soundfiler table openpanel
savepanel key keyup keyname declare inlet outlet inlet~ outlet~ namecanvas
notein ctlin pgmin bendin touchin polytouchin midiin sysexin midirealtimein
noteout ctlout pgmout bendout touchout polytouchout midiout makenote
stripnote poly bag loadbang netsend netreceive qlist textfile msgfile
block~ switch~ adc~ dac~ sig~ line~ vline~ threshold~ snapshot~ vsnapshot~
bang~ samplerate~ send~ receive~ throw~ catch~ readsf~ writesf~ osc~ phasor~
cos~ noise~ tabwrite~ tabplay~ tabread~ tabread4~ tabosc4~ tabsend~
tabreceive~ vcf~ env~ hip~ lop~ bp~ biquad~ samphold~ print~ rfft~ rifft~
fft~ ifft~ framp~ mtof~ ftom~ rmstodb~ dbtorms~ powtodb~ dbtopow~
delwrite~ delread~ delread4~ vd~ clip~ wrap~ log~ exp~ abs~ pow~ sqrt~
q8_rsqrt~ q8_sqrt~ max~ min~ rsqrt~ pd graph
bng tgl nbx vsl hsl vradio hradio vu cnv
""".split()) | {"+", "-", "*", "/", "==", "!=", ">", "<", ">=", "<=",
                "&", "&&", "|", "||", "%", "<<", ">>",
                "+~", "-~", "*~", "/~"}

# objects whose *first* outlet carries control data despite the ~ name,
# so a cord from them is drawn thin.  Not exhaustive; only affects line width.
TILDE_CONTROL_OUT = {"snapshot~", "vsnapshot~", "env~", "threshold~",
                     "bang~", "samplerate~", "print~"}

# --- colours -----------------------------------------------------------
# The patch paints its own light canvas rather than inheriting the page's,
# so the thumbnail reads the same in a light or dark viewer -- and looks
# like Pd, which is the point of recognising it at a glance.
C_CANVAS = "#fbfbfb"
C_BOX = "#ffffff"
C_LINE = "#2a2a2a"
C_TEXT = "#1a1a1a"
C_COMMENT = "#555555"
C_CORD = "#3a3a3a"
C_SIGNAL = "#2a2a2a"


def _records(text):
    """Yield records: everything up to an unescaped ';'.

    Newlines are not record separators -- Pd wraps long boxes across lines.
    """
    buf, esc = [], False
    for ch in text:
        if esc:
            buf.append(ch)
            esc = False
        elif ch == "\\":
            buf.append(ch)
            esc = True
        elif ch == ";":
            yield "".join(buf)
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        yield "".join(buf)


def _atoms(record):
    """Split a record on unescaped whitespace."""
    out, buf, esc = [], [], False
    for ch in record:
        if esc:
            buf.append(ch)
            esc = False
        elif ch == "\\":
            buf.append(ch)
            esc = True
        elif ch.isspace():
            if buf:
                out.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _unescape(s):
    return re.sub(r"\\(.)", r"\1", s)


def _join(atoms):
    """Join atoms for display the way Pd renders them.

    Escaped commas and semicolons are their own atoms in the file but hug
    the preceding word on screen.
    """
    out = ""
    for atom in atoms:
        text = _unescape(atom)
        out += text if (text in (",", ";") or not out) else " " + text
    return out


def _num(s, default=0):
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return default


def _color(atom):
    """An iemgui colour atom -> #rrggbb.

    Two encodings exist: modern '#rrggbb', and the classic negative integer
    packing six bits per channel, which is what older patches carry.
    """
    if isinstance(atom, str) and atom.startswith("#"):
        return atom if len(atom) == 7 else None
    n = _num(atom, 0)
    if n >= 0:
        return None
    packed = -1 - n
    # six bits per channel, scaled by 4 the way Pd does it -- which is why
    # its default background is #fcfcfc (63 << 2) and not #ffffff
    r, g, b = (packed >> 12) & 0x3F, (packed >> 6) & 0x3F, packed & 0x3F
    return f"#{r << 2:02x}{g << 2:02x}{b << 2:02x}"


class Canvas:
    def __init__(self, font=10, name=None):
        self.font = font
        self.name = name
        self.objects = []
        self.cords = []
        self.coords = None
        self.arrays = []


def parse(text):
    """Parse a patch into its canvas tree; returns the root Canvas."""
    root, stack = None, []
    for record in _records(text):
        atoms = _atoms(record)
        if len(atoms) < 2:
            continue
        tag, sel, rest = atoms[0], atoms[1], atoms[2:]
        if tag == "#N" and sel == "canvas":
            # 5 trailing atoms = subpatch (x y w h name open); 4 = top level
            font = _num(rest[4], 10) if len(rest) >= 5 else 10
            name = _unescape(rest[4]) if len(rest) >= 6 else None
            canvas = Canvas(font if name is None else
                            (stack[-1].font if stack else 10), name)
            if root is None:
                root = canvas
            stack.append(canvas)
        elif tag == "#A" and stack:
            if stack[-1].arrays:
                stack[-1].arrays[-1]["data"].extend(
                    float(a) for a in rest if _isnum(a))
        elif tag == "#X" and stack:
            _record(stack, sel, rest)
    return root


def _isnum(a):
    try:
        float(a)
        return True
    except ValueError:
        return False


def _record(stack, sel, rest):
    canvas = stack[-1]
    if sel == "restore":
        done = stack.pop()
        if stack and len(rest) >= 2:
            kind = rest[2] if len(rest) > 2 else "pd"
            label = _join(rest[2:]) or "pd"
            stack[-1].objects.append({
                "type": "graph" if kind == "graph" else "subpatch",
                "x": _num(rest[0]), "y": _num(rest[1]),
                "text": label, "cls": "pd", "sub": done,
                "args": rest[2:], "width": None})
    elif sel in ("obj", "msg", "text", "floatatom", "symbolatom", "listbox"):
        if len(rest) < 2:
            return
        node = {"type": sel, "x": _num(rest[0]), "y": _num(rest[1]),
                "args": rest[2:], "width": None}
        if sel == "obj":
            node["cls"] = rest[2] if len(rest) > 2 else ""
            node["text"] = _join(rest[2:])
        elif sel in ("floatatom", "symbolatom", "listbox"):
            node["cls"] = sel
            # rest[2] is the box width in characters
            node["width"] = max(_num(rest[2], 5), 1)
            node["text"] = "0" if sel == "floatatom" else "symbol"
        else:
            node["cls"] = sel
            node["text"] = _join(rest[2:])
        canvas.objects.append(node)
    elif sel == "connect" and len(rest) >= 4:
        canvas.cords.append(tuple(_num(a) for a in rest[:4]))
    elif sel == "f" and canvas.objects:
        canvas.objects[-1]["width"] = _num(rest[0], 0) or None
    elif sel == "coords":
        canvas.coords = [float(a) if _isnum(a) else 0.0 for a in rest]
    elif sel == "array" and len(rest) >= 2:
        canvas.arrays.append({"name": _unescape(rest[0]),
                              "size": _num(rest[1]), "data": []})


# --- geometry ----------------------------------------------------------
def _wrap(text, cols):
    """Pd wraps box text on whitespace at the box width."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > cols:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur or not lines:
        lines.append(cur)
    return lines


def _size(node, font):
    """Box size in pixels, the way Pd recomputes it at load."""
    fw, fh = FONTS.get(font, FONTS[10])
    cls = node.get("cls", "")
    if node["type"] == "obj" and cls in GUI_SIZE:
        wi, hi = GUI_SIZE[cls]
        args = node["args"][1:]                 # drop the class name
        w = _num(args[wi], 15) if len(args) > wi else 15
        h = w if hi is None else (_num(args[hi], 15) if len(args) > hi else 15)
        if cls == "hradio":
            w *= max(_num(args[3], 8) if len(args) > 3 else 8, 1)
        elif cls == "vradio":
            h *= max(_num(args[3], 8) if len(args) > 3 else 8, 1)
        elif cls == "nbx":
            w = w * fw + 10                     # width is in characters
        node["lines"] = []
        return max(w, 8), max(h, 8)
    if node["type"] == "graph":
        coords = (node.get("sub").coords if node.get("sub") else None) or []
        if len(coords) >= 6:
            return max(coords[4], 20), max(coords[5], 20)
        return 200, 140
    cols = node["width"] or (COMMENT_WRAP if node["type"] == "text" else None)
    lines = _wrap(node["text"], cols) if cols else [node["text"]]
    node["lines"] = lines
    chars = max(max((len(ln) for ln in lines), default=1), 1)
    if node["width"]:
        chars = max(chars, node["width"])
    return (chars * fw + BOX_PAD_X + FLAG_W.get(node["type"], 0),
            len(lines) * fh + BOX_PAD_Y)


def _io_counts(canvas):
    """Inlet/outlet counts inferred from the cords present.

    The real counts live in the object class, which is exactly what we do
    not have.  Every port a patch actually connects is placed correctly;
    unused ports simply are not drawn.
    """
    nin = [1] * len(canvas.objects)
    nout = [1] * len(canvas.objects)
    for src, outlet, dst, inlet in canvas.cords:
        if 0 <= src < len(nout):
            nout[src] = max(nout[src], outlet + 1)
        if 0 <= dst < len(nin):
            nin[dst] = max(nin[dst], inlet + 1)
    return nin, nout


def _port_x(node, index, count):
    """x of port `index` of `count`, matching Pd's spacing."""
    if count < 2:
        return node["x"] + IO_W / 2
    return node["x"] + index * (node["w"] - IO_W) / (count - 1) + IO_W / 2


# --- drawing -----------------------------------------------------------
def _esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _text(x, y, line, fw, fh, fill):
    """A run of text occupying exactly len(line)*fw pixels.

    Boxes are sized with Pd's font metrics, but an SVG renderer picks its
    own monospace face, whose advance will not match.  textLength pins the
    run to the width the box was built for, so text never overflows its
    box whatever font the viewer has.
    """
    if not line:
        return ""
    return (f'<text x="{x}" y="{y}" font-family="monospace" '
            f'font-size="{fw * 1.6:.1f}" textLength="{len(line) * fw}" '
            f'lengthAdjust="spacingAndGlyphs" fill="{fill}">'
            f'{_esc(line)}</text>')


def _bg_color(args):
    """An iemgui's background colour.

    Colour atoms sit in a run at the end (bcolor fcolor lcolor, or bcolor
    lcolor), but where that run starts differs per class and per Pd version,
    and a negative slider minimum looks exactly like a colour.  Taking the
    longest run and its first element picks bcolor in every layout.
    """
    best, run = [], []
    for atom in args:
        if _color(atom):
            run.append(atom)
        else:
            if len(run) > len(best):
                best = run
            run = []
    if len(run) > len(best):
        best = run
    return _color(best[0]) if best else None


def _shape(node):
    """SVG path for a box, in the shape Pd draws for its type."""
    x, y, w, h = node["x"], node["y"], node["w"], node["h"]
    if node["type"] == "msg":
        # message boxes have the pinched right edge
        f = min(6, w / 3)
        return (f"M{x} {y}H{x + w}L{x + w - f} {y + f}V{y + h - f}"
                f"L{x + w} {y + h}H{x}Z")
    if node["type"] in ("floatatom", "symbolatom", "listbox"):
        f = min(6, w / 3)
        return f"M{x} {y}H{x + w - f}L{x + w} {y + f}V{y + h}H{x}Z"
    return f"M{x} {y}H{x + w}V{y + h}H{x}Z"


def _draw(canvas, width_cap=MAX_W):
    """Render the top canvas as an SVG string."""
    font = canvas.font if canvas.font in FONTS else 10
    fw, fh = FONTS[font]
    for node in canvas.objects:
        node["w"], node["h"] = _size(node, font)
    if not canvas.objects:
        return None

    pad = 12
    x0 = min(n["x"] for n in canvas.objects) - pad
    y0 = min(n["y"] for n in canvas.objects) - pad
    x1 = max(n["x"] + n["w"] for n in canvas.objects) + pad
    y1 = max(n["y"] + n["h"] for n in canvas.objects) + pad
    vw, vh = max(x1 - x0, 1), max(y1 - y0, 1)
    scale = min(1.0, width_cap / vw)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" '
           f'width="{vw * scale:.0f}" height="{vh * scale:.0f}" '
           f'viewBox="{x0} {y0} {vw} {vh}">',
           f'<rect x="{x0}" y="{y0}" width="{vw}" height="{vh}" '
           f'fill="{C_CANVAS}"/>']

    nin, nout = _io_counts(canvas)
    # cords first, so boxes sit on top of them as they do in Pd
    for src, outlet, dst, inlet in canvas.cords:
        if not (0 <= src < len(canvas.objects)
                and 0 <= dst < len(canvas.objects)):
            continue
        a, b = canvas.objects[src], canvas.objects[dst]
        signal = (a.get("cls", "").endswith("~") and outlet == 0
                  and a.get("cls") not in TILDE_CONTROL_OUT)
        out.append(
            f'<line x1="{_port_x(a, outlet, nout[src]):.1f}" '
            f'y1="{a["y"] + a["h"]:.1f}" '
            f'x2="{_port_x(b, inlet, nin[dst]):.1f}" y2="{b["y"]:.1f}" '
            f'stroke="{C_SIGNAL if signal else C_CORD}" '
            f'stroke-width="{2.0 if signal else 1.0}"/>')

    for i, node in enumerate(canvas.objects):
        x, y, w, h = node["x"], node["y"], node["w"], node["h"]
        cls = node.get("cls", "")
        if node["type"] == "text":
            for n, line in enumerate(node.get("lines") or [node["text"]]):
                out.append(_text(x, y + (n + 1) * fh - 3, line, fw, fh,
                                 C_COMMENT))
            continue
        fill = C_BOX
        if node["type"] == "obj" and cls in GUI_SIZE:
            fill = _bg_color(node["args"]) or "#f0f0f0"
        out.append(f'<path d="{_shape(node)}" fill="{fill}" '
                   f'stroke="{C_LINE}" stroke-width="1"/>')
        if node["type"] == "graph":
            out.append(_trace(node))
            continue
        if node["type"] == "obj" and cls in GUI_SIZE:
            if cls in ("bng",):
                out.append(f'<circle cx="{x + w / 2:.1f}" cy="{y + h / 2:.1f}" '
                           f'r="{max(w, h) / 2 - 1.5:.1f}" fill="none" '
                           f'stroke="{C_LINE}"/>')
            elif cls in ("tgl",):
                out.append(f'<path d="M{x + 2} {y + 2}L{x + w - 2} {y + h - 2}'
                           f'M{x + w - 2} {y + 2}L{x + 2} {y + h - 2}" '
                           f'fill="none" stroke="{C_LINE}" opacity=".55"/>')
            continue
        for n, line in enumerate(node.get("lines") or [node["text"]]):
            out.append(_text(x + 2, y + (n + 1) * fh - 3, line, fw, fh, C_TEXT))
        # inlet / outlet nubs
        for k in range(nin[i]):
            px = _port_x(node, k, nin[i]) - IO_W / 2
            out.append(f'<rect x="{px:.1f}" y="{y}" width="{IO_W}" '
                       f'height="{IO_H}" fill="{C_LINE}"/>')
        for k in range(nout[i]):
            px = _port_x(node, k, nout[i]) - IO_W / 2
            out.append(f'<rect x="{px:.1f}" y="{y + h - IO_H}" '
                       f'width="{IO_W}" height="{IO_H}" fill="{C_LINE}"/>')
    out.append("</svg>")
    return "".join(out)


def _trace(node):
    """Polyline of an array's stored data, mapped through its coords.

    A graph's y range runs top-to-bottom (y1 is the *top* value), which is
    already the direction SVG y grows, so the mapping needs no flip.
    """
    sub = node.get("sub")
    if not sub or not sub.arrays or len(sub.coords or []) < 6:
        return ""
    data = sub.arrays[0]["data"]
    if len(data) < 2:
        return ""
    _, y1, _, y2, _, _ = sub.coords[:6]
    span = (y1 - y2) or 1.0
    x, y, w, h = node["x"], node["y"], node["w"], node["h"]
    step = max(len(data) // 400, 1)             # keep the path small
    pts = []
    for i in range(0, len(data), step):
        px = x + i / max(len(data) - 1, 1) * w
        py = y + (y1 - data[i]) / span * h
        pts.append(f"{px:.1f},{max(y, min(y + h, py)):.1f}")
    return (f'<polyline points="{" ".join(pts)}" fill="none" '
            f'stroke="{C_LINE}" stroke-width="1"/>')


# --- census ------------------------------------------------------------
def _walk(canvas):
    yield canvas
    for node in canvas.objects:
        if node.get("sub"):
            yield from _walk(node["sub"])


def _census(root):
    classes, objs, cords, subs, arrays = {}, 0, 0, 0, 0
    for canvas in _walk(root):
        cords += len(canvas.cords)
        arrays += len(canvas.arrays)
        for node in canvas.objects:
            if node["type"] == "text":
                continue
            objs += 1
            if node.get("sub"):
                subs += 1
            cls = node.get("cls", "")
            if node["type"] == "obj" and cls:
                classes[cls] = classes.get(cls, 0) + 1
    return classes, objs, cords, subs, arrays


def _meta(root):
    classes, objs, cords, subs, arrays = _census(root)
    meta = {"objects": objs, "cords": cords}
    if subs:
        meta["subpatches"] = subs
    if arrays:
        meta["arrays"] = arrays
    if any(c.endswith("~") for c in classes):
        meta["audio"] = "yes"
    external = sorted(c for c in classes if c not in VANILLA
                      and not c.startswith("$") and not _isnum(c))
    if external:
        meta["externals"] = ", ".join(external[:8]) + \
            (f" +{len(external) - 8}" if len(external) > 8 else "")
    return meta


def inspect(path, ctx):
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    root = parse(text.replace("\r\n", "\n"))
    if root is None:
        raise ValueError("no canvas record: not a Pd patch")
    out = {"meta": _meta(root), "kind": "puredata"}
    svg = _draw(root)
    if svg:
        out["preview"] = (svg.encode(), "svg")
    return out


def _cli(argv=None):
    """Standalone: dump the census, or write patch SVGs to a directory."""
    import argparse
    import json

    ap = argparse.ArgumentParser(description="inspect/draw Pd patches")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--json", action="store_true", help="census as JSON")
    ap.add_argument("--svg", metavar="DIR", help="write <name>.svg here")
    args = ap.parse_args(argv)

    report, rc = {}, 0
    for path in args.files:
        try:
            res = inspect(path, {})
            meta = dict(res["meta"])
            if args.svg:
                os.makedirs(args.svg, exist_ok=True)
                name = os.path.splitext(os.path.basename(path))[0] + ".svg"
                dest = os.path.join(args.svg, name)
                if res.get("preview"):
                    with open(dest, "wb") as w:
                        w.write(res["preview"][0])
                    meta["svg"] = dest
                else:
                    meta["svg"] = "empty patch"
        except Exception as e:                  # noqa: BLE001
            meta, rc = {"error": f"{type(e).__name__}: {e}"}, 1
        report[path] = meta
        if not args.json:
            print(f"{path}:")
            for k, v in meta.items():
                print(f"  {k}: {v}")
    if args.json:
        print(json.dumps(report, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(_cli())
