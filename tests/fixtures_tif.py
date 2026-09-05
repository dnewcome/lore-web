"""Generate the TIFF fixture corpus used by test_tif.py.

Most fixtures come from ImageMagick, which can write nearly every TIFF
variant on request.  BigTIFF is the exception: neither ImageMagick nor
Pillow will emit one below 4 GB, so a minimal valid BigTIFF is assembled
here by hand.

Run directly to populate a directory:  python3 tests/fixtures_tif.py DIR
"""
import os
import shutil
import struct
import subprocess

# name -> ImageMagick arguments producing <name>.tif
CASES = {
    # compression coverage
    "lzw":            ["-size", "640x400", "gradient:red-blue", "-compress", "LZW"],
    "deflate":        ["-size", "640x400", "gradient:red-blue", "-compress", "Zip"],
    "raw":            ["-size", "640x400", "gradient:red-blue", "-compress", "None"],
    "packbits":       ["-size", "640x400", "gradient:red-blue", "-compress", "RLE"],
    "g4fax":          ["-size", "640x400", "gradient:gray", "-monochrome",
                       "-compress", "Group4"],
    # bit depth / sample format
    "depth16":        ["-size", "640x400", "gradient:red-blue", "-depth", "16",
                       "-compress", "LZW"],
    "float32":        ["-size", "320x200", "gradient:red-blue", "-depth", "32",
                       "-define", "quantum:format=floating-point"],
    # colour models
    "cmyk":           ["-size", "640x400", "gradient:red-blue", "-colorspace",
                       "CMYK", "-compress", "LZW"],
    "alpha":          ["-size", "640x400", "xc:none", "-fill", "red",
                       "-draw", "circle 320,200 320,80"],
    "miniswhite":     ["-size", "200x100", "gradient:black-white", "-compress",
                       "LZW", "-define", "quantum:polarity=min-is-white"],
    # byte order
    "bigendian":      ["-size", "640x400", "gradient:red-blue", "-define",
                       "tiff:endian=msb", "-compress", "LZW"],
    # JPEG in TIFF: ffmpeg decodes these near-black
    "jpeg_gradient":  ["-size", "640x400", "gradient:red-blue", "-compress", "JPEG"],
    "jpeg_solid":     ["-size", "200x200", "xc:rgb(255,0,0)", "-compress", "JPEG",
                       "-quality", "95"],
    # page layout
    "multipage":      ["-size", "320x200", "xc:red", "-size", "320x200", "xc:green",
                       "-size", "320x200", "xc:blue", "-compress", "LZW"],
    "multipage_strips": ["-size", "320x200", "xc:red", "-size", "320x200", "xc:green",
                         "-size", "320x200", "xc:blue", "-define",
                         "tiff:rows-per-strip=8", "-compress", "LZW"],
    "multipage_jpeg": ["-size", "320x200", "xc:red", "-size", "320x200", "xc:green",
                       "-size", "320x200", "xc:blue", "-compress", "JPEG"],
    # multi-strip JPEG: each strip is a separate scan, so the splice below
    # cannot rebuild one image and the plugin refuses the file
    "jpeg_multistrip": ["-size", "320x400", "gradient:red-blue", "-compress",
                        "JPEG", "-define", "tiff:rows-per-strip=32"],
    # tiled: ffmpeg decodes these to solid black
    "tiled":          ["-size", "320x200", "xc:red", "-size", "320x200", "xc:green",
                       "-size", "320x200", "xc:blue", "-define",
                       "tiff:tile-geometry=64x64", "-compress", "LZW"],
}


def have_magick():
    return shutil.which("convert") is not None


def _bigtiff(path, w=120, h=80):
    """A minimal, valid, uncompressed RGB BigTIFF (magic 43, 64-bit offsets).

    Written by hand because no available tool emits BigTIFF under 4 GB, and
    ffmpeg rejects the whole variant -- which is the point of the fixture.
    """
    px = bytearray()
    for _ in range(h):
        for x in range(w):
            t = x / (w - 1)
            px += bytes((int(255 * (1 - t)), 0, int(255 * t)))
    software = b"bigtiff fixture\0"
    ifd_off = 16
    entries = [
        (256, 4, 1, None), (257, 4, 1, None), (258, 3, 3, None), (259, 3, 1, None),
        (262, 3, 1, None), (273, 16, 1, None), (277, 3, 1, None), (278, 4, 1, None),
        (279, 16, 1, None), (305, 2, len(software), None),
    ]
    ifd_size = 8 + len(entries) * 20 + 8
    soft_off = ifd_off + ifd_size
    px_off = soft_off + len(software)
    values = {
        256: struct.pack("<Q", w), 257: struct.pack("<Q", h),
        258: struct.pack("<3H", 8, 8, 8).ljust(8, b"\0"),
        259: struct.pack("<Q", 1), 262: struct.pack("<Q", 2),
        273: struct.pack("<Q", px_off), 277: struct.pack("<Q", 3),
        278: struct.pack("<Q", h), 279: struct.pack("<Q", len(px)),
        305: struct.pack("<Q", soft_off),
    }
    out = bytearray(struct.pack("<2sHHHQ", b"II", 43, 8, 0, ifd_off))
    out += struct.pack("<Q", len(entries))
    for tag, typ, count, _ in sorted(entries):
        out += struct.pack("<HHQ", tag, typ, count) + values[tag]
    out += struct.pack("<Q", 0)
    assert len(out) == soft_off, (len(out), soft_off)
    out += software + px
    with open(path, "wb") as f:
        f.write(bytes(out))


def build(dest):
    """Write every fixture into dest; returns the list of paths."""
    os.makedirs(dest, exist_ok=True)
    made = []
    for name, args in CASES.items():
        path = os.path.join(dest, f"{name}.tif")
        subprocess.run(["convert", *args, path], check=True, capture_output=True)
        made.append(path)
    big = os.path.join(dest, "bigtiff.tif")
    _bigtiff(big)
    made.append(big)
    return made


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures"
    for p in build(target):
        print(p)
