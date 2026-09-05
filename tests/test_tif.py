"""TIFF plugin tests: every page of every fixture, checked against ImageMagick.

The point of comparing pixels rather than exit status: ffmpeg returns 0 for
several TIFF shapes it decodes to a black or near-black frame, so a test
that only asserts "a thumbnail was produced" passes while the thumbnail is
wrong.  Every assertion here is about pixel values.

    python3 -m unittest discover -s tests -v

Needs ImageMagick (`convert`) for the reference renders and ffmpeg for the
plugin itself; the suite skips if either is missing.
"""
import importlib.util
import os
import shutil
import struct
import subprocess
import tempfile
import unittest

import fixtures_tif

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, os.pardir, "plugins", "tif.py")

_spec = importlib.util.spec_from_file_location("loreweb_plugin_tif", PLUGIN)
tif = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tif)

# mean absolute channel difference we accept against ImageMagick, out of 255.
# Lanczos scaling and naive CMYK conversion account for all of it; a decode
# that is actually wrong lands two orders of magnitude above this.
TOLERANCE = 12.0

# fixtures ffmpeg cannot decode correctly and the plugin therefore refuses,
# mapped to the phrase its explanation must contain
REFUSED = {"tiled": "tiled TIFF", "jpeg_multistrip": "multi-strip JPEG"}


def _pixels(path, size=64):
    """Flattened RGB pixel list of path, normalised to size x size."""
    out = subprocess.run(
        ["convert", path, "-alpha", "remove", "-colorspace", "sRGB",
         "-resize", f"{size}x{size}!", "-depth", "8", "txt:"],
        check=True, capture_output=True, text=True).stdout
    px = []
    for line in out.splitlines()[1:]:
        if "(" in line:
            vals = line.split("(")[1].split(")")[0].split(",")
            px.append(tuple(int(float(v)) for v in vals[:3]))
    return px


def _tag_types(path):
    """(tag, type) pairs in the first directory of a classic TIFF."""
    with open(path, "rb") as f:
        data = f.read()
    bo = "<" if data[:2] == b"II" else ">"
    off = struct.unpack(bo + "I", data[4:8])[0]
    count = struct.unpack(bo + "H", data[off:off + 2])[0]
    return [struct.unpack(bo + "HH", data[off + 2 + i * 12:off + 6 + i * 12])
            for i in range(count)]


def _diff(a, b):
    n = min(len(a), len(b))
    if not n:
        raise AssertionError("no pixels to compare")
    return sum(abs(a[i][c] - b[i][c])
               for i in range(n) for c in range(3)) / (n * 3)


@unittest.skipUnless(shutil.which("convert"), "needs ImageMagick")
@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class TiffPluginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="loreweb-tif-tests-")
        cls.dir = cls._tmp.name
        fixtures_tif.build(cls.dir)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def fixture(self, name):
        return os.path.join(self.dir, f"{name}.tif")

    def reference(self, path, page, dest):
        subprocess.run(["convert", f"{path}[{page}]", "-alpha", "remove",
                        "-colorspace", "sRGB", "-resize", "64x64!", dest],
                       check=True, capture_output=True)

    # --- pixels ---
    def test_every_page_matches_imagemagick(self):
        """Each page of each fixture renders to the right pixels."""
        for name in sorted(fixtures_tif.CASES):
            if name in REFUSED:
                continue
            path = self.fixture(name)
            with open(path, "rb") as f:
                doc = tif.Tiff(f)
                for page in range(len(doc.pages)):
                    with self.subTest(fixture=name, page=page):
                        got = os.path.join(self.dir, "got.png")
                        ref = os.path.join(self.dir, "ref.png")
                        tif._render_page(doc, path, page, got, {}, 64)
                        self.reference(path, page, ref)
                        self.assertLess(_diff(_pixels(got), _pixels(ref)),
                                        TOLERANCE)

    def test_bigtiff_is_decoded(self):
        """ffmpeg rejects BigTIFF outright; the plugin down-converts it."""
        path = self.fixture("bigtiff")
        self.assertEqual(subprocess.run(
            ["ffmpeg", "-v", "quiet", "-y", "-i", path, "-frames:v", "1",
             os.path.join(self.dir, "direct.png")],
            capture_output=True).returncode != 0, True,
            "fixture is no longer a BigTIFF ffmpeg rejects")
        got = os.path.join(self.dir, "big.png")
        with open(path, "rb") as f:
            tif._render_page(tif.Tiff(f), path, 0, got, {}, 64)
        ref = os.path.join(self.dir, "bigref.png")
        self.reference(path, 0, ref)
        self.assertLess(_diff(_pixels(got), _pixels(ref)), TOLERANCE)

    def test_jpeg_in_tiff_is_not_black(self):
        """The case that motivated pixel comparison: ffmpeg exits 0 and lies."""
        path = self.fixture("jpeg_solid")
        direct = os.path.join(self.dir, "direct.png")
        subprocess.run(["ffmpeg", "-v", "quiet", "-y", "-i", path,
                        "-frames:v", "1", direct], check=True)
        self.assertLess(max(max(p) for p in _pixels(direct)), 32,
                        "ffmpeg no longer mis-decodes JPEG-in-TIFF; the "
                        "splice workaround may be removable")
        got = os.path.join(self.dir, "jpeg.png")
        with open(path, "rb") as f:
            tif._render_page(tif.Tiff(f), path, 0, got, {}, 64)
        px = _pixels(got)
        self.assertGreater(sum(p[0] for p in px) / len(px), 200)   # red
        self.assertLess(sum(p[1] for p in px) / len(px), 40)

    # --- refusals ---
    def test_undecodable_shapes_are_refused_by_name(self):
        """A wrong image is worse than none: these must raise, not render."""
        for name, phrase in REFUSED.items():
            with self.subTest(fixture=name):
                path = self.fixture(name)
                with open(path, "rb") as f:
                    doc = tif.Tiff(f)
                    with self.assertRaises(ValueError) as caught:
                        tif._thumb(doc, path, {})
                self.assertIn(phrase, str(caught.exception))

    def test_refused_files_still_report_metadata(self):
        path = self.fixture("tiled")
        res = tif.inspect(path, {})
        self.assertNotIn("preview", res)
        self.assertEqual(res["meta"]["size"], "320x200")
        self.assertIn("tiled TIFF", res["meta"]["preview"])

    # --- metadata ---
    def test_metadata_reports_the_format_family(self):
        expected = {
            "lzw": ("compression", "LZW"),
            "deflate": ("compression", "Deflate"),
            "raw": ("compression", "none"),
            "g4fax": ("compression", "CCITT G4"),
            "cmyk": ("color", "CMYK"),
            "miniswhite": ("color", "grayscale (inverted)"),
            "bigtiff": ("format", "BigTIFF"),
        }
        for name, (key, value) in expected.items():
            with self.subTest(fixture=name):
                with open(self.fixture(name), "rb") as f:
                    self.assertEqual(tif._meta(tif.Tiff(f))[key], value)

    def test_multipage_is_counted_and_striped(self):
        for name in ("multipage", "multipage_strips"):
            with self.subTest(fixture=name):
                path = self.fixture(name)
                with open(path, "rb") as f:
                    doc = tif.Tiff(f)
                    self.assertEqual(tif._meta(doc)["pages"], 3)
                    png = tif._thumb(doc, path, {})
                strip = os.path.join(self.dir, "strip.png")
                with open(strip, "wb") as w:
                    w.write(png)
                # three pages side by side: red, then green, then blue
                thirds = _pixels(strip, 3)[:3]
                self.assertEqual([max(range(3), key=lambda c: p[c])
                                  for p in thirds], [0, 1, 2])

    def test_big_endian_parses(self):
        with open(self.fixture("bigendian"), "rb") as f:
            doc = tif.Tiff(f)
            self.assertEqual(doc.bo, ">")
            self.assertEqual(tif._meta(doc)["size"], "640x400")

    # --- page extraction ---
    def test_extracted_page_is_a_standalone_classic_tiff(self):
        for name in ("multipage", "multipage_strips", "tiled", "bigtiff"):
            with self.subTest(fixture=name):
                path = self.fixture(name)
                dest = os.path.join(self.dir, "page.tif")
                with open(path, "rb") as f:
                    doc = tif.Tiff(f)
                    doc.extract(len(doc.pages) - 1, dest)
                with open(dest, "rb") as f:
                    head = f.read(4)
                self.assertIn(head[:2], (b"II", b"MM"))
                self.assertEqual(head[2:], b"*\x00" if head[:2] == b"II"
                                 else b"\x00*")          # classic, never 43
                # BigTIFF's LONG8/SLONG8/IFD8 have no meaning in a classic
                # file.  ffmpeg and ImageMagick happen to tolerate them, so
                # only a structural check catches the omission.
                self.assertEqual(
                    [p for p in _tag_types(dest) if p[1] in (16, 17, 18)], [],
                    "64-bit tag types must be narrowed for classic TIFF")
                ref = os.path.join(self.dir, "pageref.png")
                self.reference(path, len(doc.pages) - 1, ref)
                self.assertLess(_diff(_pixels(dest), _pixels(ref)), TOLERANCE)

    def test_rejects_non_tiff(self):
        junk = os.path.join(self.dir, "junk.tif")
        with open(junk, "wb") as f:
            f.write(b"not a tiff at all")
        with self.assertRaises(ValueError):
            with open(junk, "rb") as f:
                tif.Tiff(f)


if __name__ == "__main__":
    unittest.main()
