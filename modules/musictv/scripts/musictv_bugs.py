#!/usr/bin/env python3
"""Convert screen-bug PNGs into the raw format mpv's overlay-add wants.

mpv can composite an image over video without touching the decode path, but only
from raw premultiplied BGRA — it will not read a PNG, and there is no ffmpeg CLI
on the Pi to do it for us. So this does it in pure stdlib Python (zlib does the
only heavy lifting), the same way the EPG scanner avoids TSDuck.

For every <name>.png in the bug directory it writes <name>.<w>x<h>.bgra, with
the pixel size in the filename because the Lua overlay script has no way to
learn it otherwise. Fully-transparent margins are cropped away first, so a bug
that ships centred in a 1920x1080 frame and one that ships as a tight logo both
end up as the same kind of object; musictv-overlay.lua then scales whatever it finds
to a fraction of the screen height via overlay-add's dw/dh.

Idempotent: an output newer than its PNG is left alone. Outputs whose PNG is
gone are deleted, so removing a bug from the directory removes it from rotation.

Usage:  musictv_bugs.py [bug-dir]        (default: <240-MP data dir>/musictv_bugs)

Supports 8- and 16-bit non-interlaced PNGs in all five colour types. Animated
GIFs are not supported — overlay-add takes one still frame, so a GIF would show
as a single frame at best; convert it to a PNG first.
"""

import os
import struct
import sys
import zlib

MAX_DIM = 512  # a bug is drawn at ~10% of screen height; more is wasted RAM


# --- PNG decoding -----------------------------------------------------------

def _unfilter(raw, width, height, bpp, stride):
    """Reverse the per-scanline PNG filters. Returns the raw sample bytes."""
    out = bytearray(height * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(height):
        ftype = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if ftype == 1:      # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ftype == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 3:    # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:    # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif ftype != 0:
            raise ValueError("unknown PNG filter %d" % ftype)
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return out


def read_png(path):
    """-> (width, height, RGBA bytearray). Raises ValueError on anything exotic."""
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")

    pos = 8
    idat = bytearray()
    palette = b""
    trns = b""
    width = height = depth = ctype = interlace = 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        ctag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        pos += 12 + length          # 4 len + 4 tag + body + 4 crc
        if ctag == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif ctag == b"PLTE":
            palette = body
        elif ctag == b"tRNS":
            trns = body
        elif ctag == b"IDAT":
            idat += body
        elif ctag == b"IEND":
            break

    if interlace:
        raise ValueError("interlaced PNGs are not supported")
    if depth not in (8, 16):
        raise ValueError("bit depth %d is not supported (need 8 or 16)" % depth)

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None:
        raise ValueError("colour type %d is not supported" % ctype)
    if ctype == 3 and depth != 8:
        raise ValueError("sub-8-bit palettes are not supported")

    sample = depth // 8
    bpp = channels * sample
    stride = width * bpp
    samples = _unfilter(zlib.decompress(bytes(idat)), width, height, bpp, stride)

    # Normalise to 8-bit RGBA
    rgba = bytearray(width * height * 4)
    step = bpp
    for y in range(height):
        row = y * stride
        dst = y * width * 4
        for x in range(width):
            s = row + x * step
            if ctype == 3:
                idx = samples[s]
                p = idx * 3
                r, g, b = palette[p], palette[p + 1], palette[p + 2]
                a = trns[idx] if idx < len(trns) else 255
            elif ctype == 0:
                r = g = b = samples[s]
                a = 255
            elif ctype == 4:
                r = g = b = samples[s]
                a = samples[s + sample]
            elif ctype == 2:
                r, g, b = samples[s], samples[s + sample], samples[s + 2 * sample]
                a = 255
            else:  # 6
                r, g, b = samples[s], samples[s + sample], samples[s + 2 * sample]
                a = samples[s + 3 * sample]
            d = dst + x * 4
            rgba[d] = r
            rgba[d + 1] = g
            rgba[d + 2] = b
            rgba[d + 3] = a
    return width, height, rgba


# --- transforms -------------------------------------------------------------

def transparency_fraction(rgba):
    """Share of pixels that are not fully opaque. A "bug" saved as a video-frame
    grab (opaque black or white behind the logo) converts perfectly happily and
    then covers a corner of the screen with a solid rectangle — worth saying out
    loud, because the failure only shows up on the CRT."""
    n = len(rgba) // 4
    if n == 0:
        return 0.0
    clear = sum(1 for i in range(3, len(rgba), 4) if rgba[i] < 255)
    return clear / float(n)


def crop_transparent(w, h, rgba):
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            if rgba[row + x * 4 + 3]:
                if x < x0: x0 = x
                if x > x1: x1 = x
                if y < y0: y0 = y
                if y > y1: y1 = y
    if x1 < 0:
        raise ValueError("image is fully transparent")
    nw, nh = x1 - x0 + 1, y1 - y0 + 1
    if (nw, nh) == (w, h):
        return w, h, rgba
    out = bytearray(nw * nh * 4)
    for y in range(nh):
        src = ((y + y0) * w + x0) * 4
        out[y * nw * 4:(y + 1) * nw * 4] = rgba[src:src + nw * 4]
    return nw, nh, out


def resize(w, h, rgba, max_dim):
    """Bilinear downscale. Logos have hard edges; nearest looks ragged on a CRT."""
    if max(w, h) <= max_dim:
        return w, h, rgba
    scale = max_dim / float(max(w, h))
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    out = bytearray(nw * nh * 4)
    for y in range(nh):
        sy = (y + 0.5) / scale - 0.5
        y0 = max(0, min(h - 1, int(sy)))
        y1 = min(h - 1, y0 + 1)
        fy = max(0.0, min(1.0, sy - y0))
        for x in range(nw):
            sx = (x + 0.5) / scale - 0.5
            x0 = max(0, min(w - 1, int(sx)))
            x1 = min(w - 1, x0 + 1)
            fx = max(0.0, min(1.0, sx - x0))
            p00 = (y0 * w + x0) * 4
            p01 = (y0 * w + x1) * 4
            p10 = (y1 * w + x0) * 4
            p11 = (y1 * w + x1) * 4
            d = (y * nw + x) * 4
            for c in range(4):
                top = rgba[p00 + c] + (rgba[p01 + c] - rgba[p00 + c]) * fx
                bot = rgba[p10 + c] + (rgba[p11 + c] - rgba[p10 + c]) * fx
                out[d + c] = int(top + (bot - top) * fy + 0.5)
    return nw, nh, out


def to_premultiplied_bgra(w, h, rgba):
    out = bytearray(len(rgba))
    for i in range(0, len(rgba), 4):
        a = rgba[i + 3]
        out[i] = rgba[i + 2] * a // 255
        out[i + 1] = rgba[i + 1] * a // 255
        out[i + 2] = rgba[i] * a // 255
        out[i + 3] = a
    return out


# --- driver -----------------------------------------------------------------

def data_dir():
    env = os.environ.get("MUSICTV_DATA_DIR")
    if env:
        return env
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "240-MP")
    return os.path.join(home, ".local", "share", "240-MP")


def main():
    bug_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(data_dir(), "musictv_bugs")
    if not os.path.isdir(bug_dir):
        sys.exit("no bug directory at %s" % bug_dir)

    names = sorted(os.listdir(bug_dir))
    pngs = [n for n in names if n.lower().endswith(".png")]
    stems = {os.path.splitext(n)[0] for n in pngs}

    # Drop outputs whose source PNG was removed or renamed.
    for name in names:
        if not name.endswith(".bgra"):
            continue
        stem = name.split(".")[0]
        if stem not in stems:
            os.remove(os.path.join(bug_dir, name))
            print("removed stale %s" % name)

    for name in pngs:
        stem = os.path.splitext(name)[0]
        src = os.path.join(bug_dir, name)
        existing = [n for n in os.listdir(bug_dir)
                    if n.startswith(stem + ".") and n.endswith(".bgra")]
        if existing and all(os.path.getmtime(os.path.join(bug_dir, n)) >= os.path.getmtime(src)
                            for n in existing):
            print("%s: up to date" % name)
            continue
        try:
            w, h, rgba = read_png(src)
            clear = transparency_fraction(rgba)
            w, h, rgba = crop_transparent(w, h, rgba)
            w, h, rgba = resize(w, h, rgba, MAX_DIM)
            bgra = to_premultiplied_bgra(w, h, rgba)
        except (ValueError, zlib.error) as exc:
            print("%s: SKIPPED (%s)" % (name, exc))
            continue
        for old in existing:
            os.remove(os.path.join(bug_dir, old))
        out = os.path.join(bug_dir, "%s.%dx%d.bgra" % (stem, w, h))
        with open(out, "wb") as fh:
            fh.write(bgra)
        note = "" if clear >= 0.02 else "   WARNING: no transparent pixels — " \
                                        "this will draw as a solid rectangle"
        print("%s -> %s%s" % (name, os.path.basename(out), note))

    skipped = [n for n in names
               if os.path.splitext(n)[1].lower() in (".gif", ".jpg", ".jpeg", ".webp")]
    if skipped:
        print("not converted (PNG only): %s" % ", ".join(skipped))


if __name__ == "__main__":
    main()
