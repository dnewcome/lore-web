"""lore-web plugin: TIFF images -> thumbnail + directory metadata.

Pure-stdlib TIFF/BigTIFF *directory* reader; pixels are decoded by ffmpeg,
which the server already depends on.  TIFF is less a format than a family,
and ffmpeg handles only part of it, so parsing the IFDs ourselves earns its
keep several ways:

  * **metadata worth showing** - compression, bit depth, DPI, the program
    that wrote the file, the capture date.  That is the archaeology this
    viewer exists for; a bare thumbnail does not tell you a scan is 600 dpi
    G4 fax from 1998 rather than a 16-bit render from last week.
  * **multi-page TIFFs** (scans, faxes, layered exports) render as a contact
    strip.  ffmpeg only ever decodes the first directory, so each page is
    re-emitted as a standalone one-page TIFF first.  Pixel data is copied
    byte-for-byte, never re-encoded, so this works for any compression -
    including ones we could not decode ourselves.
  * **BigTIFF**, the 64-bit variant used by anything over 4 GB, which ffmpeg
    rejects outright ("Invalid TIFF header").  A page lifted out of one is
    ordinary TIFF, so the same machinery makes it readable.
  * **CMYK**, which ffmpeg's tiff decoder hands back as raw RGBA: the four
    channels really are C, M, Y, K but are labelled R, G, B, A, so a plain
    scale gives confident nonsense (a red gradient comes out green).
  * **JPEG-compressed TIFF**, which ffmpeg decodes to a near-black frame and
    still exits 0.  The strip is a real JPEG scan missing its tables; tag
    347 holds them, and splicing the two back together decodes exactly.

Where ffmpeg fails *loudly* we let it fail.  The dangerous cases are the
ones above where it fails silently, with a plausible-looking black image and
a zero exit status -- so tiled TIFF and multi-strip JPEG, which we cannot
repair, are refused by name rather than previewed wrongly.

Verified against ImageMagick on 24 fixtures covering every combination
above; see _unsupported() for the two that are deliberately skipped.

Orientation (tag 274) is applied too - ffmpeg ignores it, and scanners set
it constantly.
"""
import os
import struct
import subprocess
import tempfile

MATCH = [".tif", ".tiff"]

MAX_PAGES = 64          # directories to walk; guards against IFD loops
STRIP_PAGES = 5         # pages shown in a multi-page contact strip
LARGE_BYTES = 512 << 20  # above this, prefer a pyramid level
THUMB_W = 480

# --- tags we care about ------------------------------------------------
T_SUBFILE, T_WIDTH, T_HEIGHT, T_BITS = 254, 256, 257, 258
T_COMPRESSION, T_PHOTOMETRIC = 259, 262
T_DESCRIPTION, T_MAKE, T_MODEL = 270, 271, 272
T_STRIP_OFFSETS, T_ORIENTATION, T_SAMPLES = 273, 274, 277
T_STRIP_COUNTS, T_XRES, T_YRES = 279, 282, 283
T_RES_UNIT, T_SOFTWARE, T_DATETIME, T_ARTIST = 296, 305, 306, 315
T_TILE_OFFSETS, T_TILE_COUNTS = 324, 325
T_TILE_WIDTH, T_TILE_LENGTH = 322, 323
T_JPEG_TABLES = 347
T_SAMPLE_FORMAT = 339

# tags whose value is a pointer into the pixel data, rewritten when a page
# is lifted out into a file of its own
DATA_TAGS = {T_STRIP_OFFSETS: T_STRIP_COUNTS, T_TILE_OFFSETS: T_TILE_COUNTS}

TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8,
             11: 4, 12: 8, 16: 8, 17: 8, 18: 8}
TYPE_FMT = {1: "B", 3: "H", 4: "I", 6: "b", 8: "h", 9: "i", 11: "f",
            12: "d", 16: "Q", 17: "q", 18: "Q"}

COMPRESSION = {1: "none", 2: "CCITT RLE", 3: "CCITT G3", 4: "CCITT G4",
               5: "LZW", 6: "JPEG (old)", 7: "JPEG", 8: "Deflate",
               9: "JBIG BW", 10: "JBIG color", 32766: "NeXT RLE",
               32773: "PackBits", 32809: "ThunderScan", 32946: "Deflate",
               34712: "JPEG 2000", 34925: "LZMA", 50000: "Zstd",
               50001: "WebP", 50002: "JPEG XL"}

PHOTOMETRIC = {0: "grayscale (inverted)", 1: "grayscale", 2: "RGB",
               3: "palette", 4: "mask", 5: "CMYK", 6: "YCbCr", 8: "CIELab",
               9: "ICCLab", 10: "ITULab", 32844: "LogL", 32845: "LogLuv"}

SAMPLE_FORMAT = {1: "uint", 2: "int", 3: "float", 4: "undefined"}

CMYK = 5
JPEG_COMPRESSIONS = (6, 7)


class Tiff:
    """A TIFF opened for structure, not for pixels."""

    def __init__(self, f):
        self.f = f
        head = self._at(0, 8)
        if head[:2] == b"II":
            self.bo = "<"
        elif head[:2] == b"MM":
            self.bo = ">"
        else:
            raise ValueError("not a TIFF")
        magic = self._u("H", head[2:4])
        if magic == 42:
            self.big = False
            self.osize, self.ofmt = 4, "I"
            nxt = self._u("I", head[4:8])
        elif magic == 43:                       # BigTIFF
            self.big = True
            self.osize, self.ofmt = 8, "Q"
            if self._u("H", head[4:6]) != 8:
                raise ValueError("unsupported BigTIFF offset size")
            nxt = self._u("Q", self._at(8, 8))
        else:
            raise ValueError(f"bad TIFF magic {magic}")
        self.pages, seen = [], set()
        while nxt and nxt not in seen and len(self.pages) < MAX_PAGES:
            seen.add(nxt)
            entries, nxt = self._ifd(nxt)
            self.pages.append(entries)
        if not self.pages:
            raise ValueError("no image directories")

    # --- raw access ---
    def _at(self, off, n):
        self.f.seek(off)
        b = self.f.read(n)
        if len(b) < n:
            raise ValueError("truncated TIFF")
        return b

    def _u(self, fmt, b):
        return struct.unpack(self.bo + fmt, b)[0]

    def _ifd(self, off):
        """Entries of the directory at off, plus the next directory offset."""
        n = (self._u("Q", self._at(off, 8)) if self.big
             else self._u("H", self._at(off, 2)))
        head = 8 if self.big else 2
        esize = 20 if self.big else 12
        raw = self._at(off + head, n * esize)
        nxt = self._u(self.ofmt, self._at(off + head + n * esize, self.osize))
        entries = {}
        for i in range(n):
            e = raw[i * esize:(i + 1) * esize]
            tag, typ = struct.unpack(self.bo + "HH", e[:4])
            count = self._u(self.ofmt, e[4:4 + self.osize])
            room = 8 if self.big else 4
            size = TYPE_SIZE.get(typ, 0) * count
            if size and size <= room:
                entries[tag] = (typ, count, e[4 + self.osize:4 + self.osize + size], None)
            else:
                entries[tag] = (typ, count, None,
                                self._u(self.ofmt, e[4 + self.osize:]))
        return entries, nxt

    def payload(self, entry):
        """The tag's bytes, whether they were inline or off in the file."""
        typ, count, inline, off = entry
        if inline is not None:
            return inline
        size = TYPE_SIZE.get(typ, 0) * count
        return self._at(off, size) if size else b""

    def value(self, page, tag, default=None):
        """Decoded tag value: scalar for count 1, list otherwise."""
        entry = self.pages[page].get(tag)
        if entry is None:
            return default
        typ, count, _, _ = entry
        data = self.payload(entry)
        if typ == 2:                                     # ASCII
            return data.split(b"\0")[0].decode("latin-1", "replace").strip()
        if typ in (5, 10):                               # RATIONAL
            f = "Ii"[typ == 10]
            vals = []
            for i in range(count):
                num, den = struct.unpack(self.bo + f * 2, data[i * 8:i * 8 + 8])
                vals.append(num / den if den else 0.0)
            return vals[0] if count == 1 else vals
        fmt = TYPE_FMT.get(typ)
        if fmt is None:                                  # UNDEFINED / unknown
            return data
        vals = list(struct.unpack(f"{self.bo}{count}{fmt}", data[:count * struct.calcsize(fmt)]))
        return vals[0] if count == 1 else vals

    def scalar(self, page, tag, default=None):
        """Like value(), but collapses an array to its first element."""
        v = self.value(page, tag, default)
        return v[0] if isinstance(v, list) and v else v

    # --- lifting one page into a standalone file ---
    MAX_CLASSIC = 0xFFFFFFFF        # classic TIFF offsets are 32-bit

    def _narrow(self, typ, count, data):
        """Re-type a value for a classic TIFF.

        BigTIFF adds LONG8/SLONG8/IFD8, which classic readers do not know;
        their values have to come back down to 32 bits.  Anything that will
        not fit is a file we have no business rewriting.
        """
        if typ not in (16, 17, 18):
            return typ, data
        fmt = "q" if typ == 17 else "Q"
        vals = struct.unpack(f"{self.bo}{count}{fmt}", data[:count * 8])
        if any(not -0x80000000 <= v <= self.MAX_CLASSIC for v in vals):
            raise ValueError("64-bit tag value exceeds classic TIFF range")
        out = 9 if typ == 17 else 4
        return out, struct.pack(f"{self.bo}{count}{'i' if typ == 17 else 'I'}",
                                *vals)

    def page_bytes(self, page):
        """Total compressed pixel bytes in a page."""
        total = 0
        for otag, ctag in DATA_TAGS.items():
            if otag not in self.pages[page]:
                continue
            lens = self.value(page, ctag, 0)
            total += sum(lens) if isinstance(lens, list) else lens
        return total

    def extract(self, page, out):
        """Write page as a standalone one-page classic TIFF at path out.

        Pixel data is copied verbatim, so any compression survives -- even
        ones we could not decode.  Only the offsets pointing at it are
        rewritten.  This doubles as the BigTIFF escape hatch: ffmpeg rejects
        BigTIFF outright, but a page lifted out of one is ordinary TIFF.
        """
        entries = self.pages[page]
        bo = self.bo
        tags = sorted(t for t in entries if t)

        # 1. settle every non-pixel tag's final type and bytes
        fields = {}
        for tag in tags:
            if tag in DATA_TAGS:
                continue                    # rebuilt once pixels are placed
            typ, count, _, _ = entries[tag]
            typ, data = self._narrow(typ, count, self.payload(entries[tag]))
            fields[tag] = [typ, count, data, None]      # ..., offset

        # 2. lay the file out: header, IFD, tag values, pixels, pixel offsets
        cur = 8 + 2 + len(tags) * 12 + 4
        for tag in tags:
            if tag in fields and len(fields[tag][2]) > 4:
                fields[tag][3] = cur
                cur += len(fields[tag][2]) + (len(fields[tag][2]) & 1)

        moves = []                          # (dst, src, length)
        for otag, ctag in DATA_TAGS.items():
            if otag not in entries:
                continue
            offs = self.value(page, otag)
            lens = self.value(page, ctag)
            offs = offs if isinstance(offs, list) else [offs]
            lens = lens if isinstance(lens, list) else [lens]
            if len(lens) < len(offs):
                raise ValueError(
                    f"tag {otag}: {len(offs)} offsets but {len(lens)} lengths")
            placed = []
            for off, ln in zip(offs, lens):
                ln = int(ln)
                placed.append(cur)
                moves.append((cur, int(off), ln))
                cur += ln + (ln & 1)
            data = struct.pack(f"{bo}{len(placed)}I", *placed)
            fields[otag] = [4, len(placed), data, None]

        for otag in DATA_TAGS:
            if otag in fields and len(fields[otag][2]) > 4:
                fields[otag][3] = cur
                cur += len(fields[otag][2]) + (len(fields[otag][2]) & 1)

        if cur > self.MAX_CLASSIC:
            raise ValueError("page too large for a classic TIFF")

        # 3. write it
        with open(out, "wb") as w:
            w.write(struct.pack(f"{bo}2sHI",
                                b"II" if bo == "<" else b"MM", 42, 8))
            w.write(struct.pack(f"{bo}H", len(tags)))
            for tag in tags:
                typ, count, data, off = fields[tag]
                field = (struct.pack(f"{bo}I", off) if off is not None
                         else data.ljust(4, b"\0")[:4])
                w.write(struct.pack(f"{bo}HHI", tag, typ, count) + field)
            w.write(struct.pack(f"{bo}I", 0))           # no next directory
            for tag in tags:
                typ, count, data, off = fields[tag]
                if off is not None:
                    w.seek(off)
                    w.write(data + (b"\0" if len(data) & 1 else b""))
            for dst, src, ln in moves:
                w.seek(dst)
                w.write(self._at(src, ln) + (b"\0" if ln & 1 else b""))


def _orient_filter(orientation):
    """ffmpeg filter chain applying a TIFF Orientation tag."""
    return {2: "hflip", 3: "hflip,vflip", 4: "vflip",
            5: "transpose=0", 6: "transpose=1",
            7: "transpose=3", 8: "transpose=2"}.get(orientation)


def _vf(photometric, orientation, width):
    chain = []
    if photometric == CMYK:
        # ffmpeg labels the C,M,Y,K channels R,G,B,A.  RGB = (1-C)(1-K).
        chain.append(
            "format=rgba,geq="
            "r='(255-r(X,Y))*(255-alpha(X,Y))/255':"
            "g='(255-g(X,Y))*(255-alpha(X,Y))/255':"
            "b='(255-b(X,Y))*(255-alpha(X,Y))/255':a=255")
    if (turn := _orient_filter(orientation)):
        chain.append(turn)
    chain.append(f"scale='min({width},iw)':-2:flags=lanczos")
    return ",".join(chain)


def _render(run, ffmpeg, src, dst, photometric, orientation, width=THUMB_W):
    run([ffmpeg, "-v", "error", "-y", "-i", src,
         "-vf", _vf(photometric, orientation, width),
         "-frames:v", "1", dst], timeout=180)


def _reduced_pages(tif):
    """Directories flagged as reduced-resolution copies (pyramid levels)."""
    return [n for n in range(len(tif.pages))
            if (tif.scalar(n, T_SUBFILE, 0) or 0) & 1]


def _image_pages(tif):
    """Page indices that are real images, not reduced-resolution copies.

    Bit 0 of NewSubfileType marks a thumbnail/pyramid directory; Photoshop
    and scanners both stash one there, and showing it as page 2 of a scan
    would be a lie.
    """
    reduced = set(_reduced_pages(tif))
    full = [n for n in range(len(tif.pages)) if n not in reduced]
    return full or list(range(len(tif.pages)))


def _pages_to_show(tif):
    """The pages worth rendering.

    Huge scans are usually pyramidal: the full image is gigabytes and a
    reduced-resolution level sits in the same file, which is exactly the
    thumbnail we want.  Copying the full level out to feed ffmpeg would
    mean moving those gigabytes to make a 480px strip.
    """
    pages = _image_pages(tif)
    if pages and tif.page_bytes(pages[0]) > LARGE_BYTES:
        small = [n for n in _reduced_pages(tif)
                 if tif.page_bytes(n) <= LARGE_BYTES]
        if small:
            return [max(small, key=tif.page_bytes)]
    return pages


def _jpeg_stream(tif, page):
    """A JPEG-in-TIFF page rebuilt as an ordinary .jpg, or None.

    ffmpeg's tiff decoder gets JPEG-compressed TIFF badly wrong -- it
    returns a near-black frame and exits 0, so the damage is silent.  The
    strip is a real JPEG scan, though; it is only missing the quantisation
    and Huffman tables, which the TIFF keeps once in tag 347.  Splicing the
    tables back on (dropping their EOI and the scan's SOI) yields a file
    any JPEG decoder reads correctly.

    Only single-strip pages are rebuilt: with several strips each one is a
    separate scan that would have to be stitched, and a partial image is
    worse than none.
    """
    entries = tif.pages[page]
    if tif.scalar(page, T_COMPRESSION) not in JPEG_COMPRESSIONS:
        return None
    if T_JPEG_TABLES not in entries or T_STRIP_OFFSETS not in entries:
        return None
    offs = tif.value(page, T_STRIP_OFFSETS)
    lens = tif.value(page, T_STRIP_COUNTS)
    if isinstance(offs, list) and len(offs) != 1:
        return None
    off = offs[0] if isinstance(offs, list) else offs
    ln = lens[0] if isinstance(lens, list) else lens
    tables = tif.payload(entries[T_JPEG_TABLES])
    if not tables.startswith(b"\xff\xd8"):
        return None
    scan = tif._at(int(off), int(ln))
    head = tables[:-2] if tables.endswith(b"\xff\xd9") else tables
    body = scan[2:] if scan.startswith(b"\xff\xd8") else scan
    return head + body


def _unsupported(tif, page):
    """Why ffmpeg cannot be trusted with this page, or None if it can.

    Both cases below decode to a solid black frame with a zero exit status,
    so they have to be caught from the directory rather than the output.
    """
    if T_TILE_OFFSETS in tif.pages[page]:
        return "tiled TIFF"
    if tif.scalar(page, T_COMPRESSION) in JPEG_COMPRESSIONS \
            and _jpeg_stream(tif, page) is None:
        return "multi-strip JPEG"
    return None


def _render_page(tif, path, page, dst, ctx, width):
    """Thumbnail one page, copying it out first only when we have to.

    ffmpeg reads the *first* directory of a *classic* file and nothing
    else, so that one case can be handed the original path; everything
    else -- later pages, and every BigTIFF -- goes through extract().
    """
    run = ctx.get("run") or (lambda a, **k: subprocess.run(a, check=True))
    ffmpeg = ctx.get("ffmpeg", "ffmpeg")
    if (why := _unsupported(tif, page)):
        raise ValueError(f"{why}: ffmpeg decodes it to a blank frame")
    photometric = tif.scalar(page, T_PHOTOMETRIC)
    src, tmp = path, None
    if (jpeg := _jpeg_stream(tif, page)) is not None:
        tmp = dst + ".src.jpg"
        with open(tmp, "wb") as w:
            w.write(jpeg)
        src = tmp
        photometric = None          # the JPEG carries its own colour model
    elif tif.big or page != 0:
        # ffmpeg needs its own copy of this page; refuse if that means
        # moving gigabytes for a 480px thumbnail
        if tif.page_bytes(page) > LARGE_BYTES:
            raise ValueError(
                f"page is {tif.page_bytes(page) >> 20} MiB with no reduced-"
                "resolution level to use instead")
        tmp = dst + ".src.tif"
        tif.extract(page, tmp)
        src = tmp
    try:
        run([ffmpeg, "-v", "error", "-y", "-i", src, "-vf",
             _vf(photometric, tif.scalar(page, T_ORIENTATION, 1), width),
             "-frames:v", "1", dst], timeout=180)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def _thumb(tif, path, ctx):
    """PNG bytes for the file: one image, or a strip of the first pages."""
    run = ctx.get("run") or (lambda a, **k: subprocess.run(a, check=True))
    ffmpeg = ctx.get("ffmpeg", "ffmpeg")
    pages = _pages_to_show(tif)
    with tempfile.TemporaryDirectory(prefix="loreweb-tif-") as tmp:
        out = os.path.join(tmp, "out.png")
        if len(pages) < 2:
            _render_page(tif, path, pages[0], out, ctx, THUMB_W)
        else:
            shown = pages[:STRIP_PAGES]
            cell = max(THUMB_W // len(shown), 120)
            for n, page in enumerate(shown):
                _render_page(tif, path, page,
                             os.path.join(tmp, f"p{n:03d}.png"), ctx, cell)
            run([ffmpeg, "-v", "error", "-y", "-i",
                 os.path.join(tmp, "p%03d.png"), "-vf",
                 f"scale={cell}:-2,tile={len(shown)}x1:padding=2"
                 ":color=#00000000", "-frames:v", "1", out], timeout=180)
        with open(out, "rb") as f:
            return f.read()


def _dpi(tif):
    x = tif.scalar(0, T_XRES)
    unit = tif.scalar(0, T_RES_UNIT, 2)
    if not x or unit not in (2, 3):
        return None
    dpi = x * 2.54 if unit == 3 else x        # 3 = per centimetre
    return round(dpi) if dpi >= 1 else None


def _meta(tif):
    w = tif.scalar(0, T_WIDTH)
    h = tif.scalar(0, T_HEIGHT)
    bits = tif.value(0, T_BITS, 1)
    bits = bits if isinstance(bits, list) else [bits]
    spp = tif.scalar(0, T_SAMPLES, len(bits))
    comp = tif.scalar(0, T_COMPRESSION, 1)
    photo = tif.scalar(0, T_PHOTOMETRIC)
    fmt = tif.scalar(0, T_SAMPLE_FORMAT, 1)
    meta = {}
    if w and h:
        meta["size"] = f"{w}x{h}"
    if photo in PHOTOMETRIC:
        meta["color"] = PHOTOMETRIC[photo]
    if bits and bits[0]:
        depth = f"{bits[0]}-bit"
        if fmt == 3:
            depth += " float"
        if spp and spp > 1:
            depth += f" x{spp}"
        meta["depth"] = depth
    meta["compression"] = COMPRESSION.get(comp, f"unknown ({comp})")
    if tif.big:
        meta["format"] = "BigTIFF"
    if (dpi := _dpi(tif)):
        meta["dpi"] = dpi
    pages = _image_pages(tif)
    if len(pages) > 1:
        meta["pages"] = len(pages)
    if len(pages) < len(tif.pages):
        meta["embedded"] = f"{len(tif.pages) - len(pages)} preview"
    orientation = tif.scalar(0, T_ORIENTATION, 1)
    if orientation and orientation != 1:
        meta["orientation"] = orientation
    for tag, key in ((T_SOFTWARE, "software"), (T_DATETIME, "captured"),
                     (T_ARTIST, "artist"), (T_DESCRIPTION, "description")):
        if (v := tif.value(0, tag)) and isinstance(v, str):
            meta[key] = v[:80]
    make, model = tif.value(0, T_MAKE), tif.value(0, T_MODEL)
    if isinstance(make, str) and isinstance(model, str) and make and model:
        meta["camera"] = f"{make} {model}"[:80]
    return meta


def inspect(path, ctx):
    with open(path, "rb") as f:
        tif = Tiff(f)
        out = {"meta": _meta(tif), "kind": "image"}
        try:
            png = _thumb(tif, path, ctx)
        except ValueError as e:
            # a shape we deliberately refuse; say so in plain words
            out["meta"]["preview"] = f"none - {e}"
            return out
        except Exception as e:                  # noqa: BLE001
            # metadata is still worth having when the pixels will not decode
            out["meta"]["preview"] = f"none - {type(e).__name__}"
            return out
    if png:
        out["preview"] = (png, "png")
    return out


def _cli(argv=None):
    """Standalone: dump metadata, or write thumbnails to a directory."""
    import argparse
    import json

    ap = argparse.ArgumentParser(description="inspect/thumbnail TIFF files")
    ap.add_argument("files", nargs="+")
    ap.add_argument("--json", action="store_true", help="metadata as JSON")
    ap.add_argument("--thumb", metavar="DIR", help="write <name>.png here")
    args = ap.parse_args(argv)

    report, rc = {}, 0
    for path in args.files:
        try:
            with open(path, "rb") as f:
                tif = Tiff(f)
                meta = _meta(tif)
                if args.thumb:
                    os.makedirs(args.thumb, exist_ok=True)
                    name = os.path.splitext(os.path.basename(path))[0] + ".png"
                    dst = os.path.join(args.thumb, name)
                    try:
                        png = _thumb(tif, path, {})
                    except Exception as e:      # noqa: BLE001
                        # a file we can describe but not decode still counts
                        meta["thumb"] = (str(e) if isinstance(e, ValueError)
                                         else f"failed: {type(e).__name__}")
                        rc = 1
                    else:
                        with open(dst, "wb") as w:
                            w.write(png)
                        meta["thumb"] = dst
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
