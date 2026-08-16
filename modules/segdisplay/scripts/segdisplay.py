#!/usr/bin/env python3
"""Drive the front-panel display: an SSD1306 128x32 OLED drawn as 7-segment.

Why a daemon: 240-MP contains no GPIO/I2C/serial code at all, and QML can
neither spawn a process nor write a file (same wall epg-rescan-trigger.py works
around). What QML *can* do is an HTTP request, so the app POSTs its state to
this process on loopback and this process owns the I2C bus.

Hardware: a 0.91" SSD1306 128x32 I2C OLED, default address 0x3C. It replaces the
VK16K33/HT16K33 4-digit LED module this file used to drive — that board never
worked on the bench. The *look* is unchanged: four 7-segment digits with real
per-digit decimal points and a blinking colon, drawn as rectangles into the
frame buffer. Being pixels rather than LED bars buys three things the LED module
could not do: decimal points that are guaranteed to exist, an optional ghost of
the unlit segments (the grey "8888" a real panel shows), and a slow pixel shift
so a clock left up for days cannot burn in.

Dependency-free on purpose (no smbus2, no luma.oled, no PIL), matching psip.py
and musictv_bugs.py: /dev/i2c-<n> plus one ioctl is all an SSD1306 needs, and
the Pi image ships none of those libraries.

Run with no arguments to serve. See --help for the bring-up modes.
"""

import fcntl
import json
import os
import pathlib
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODULE_ID = "com.240mp.segdisplay"

I2C_BUS = os.environ.get("SEGDISPLAY_I2C_BUS", "/dev/i2c-1")
I2C_ADDR = int(os.environ.get("SEGDISPLAY_I2C_ADDR", "0x3C"), 0)
PORT = int(os.environ.get("SEGDISPLAY_PORT", "8377"))

# Linux ioctl number for "set the slave address for subsequent transfers".
I2C_SLAVE = 0x0703

# SSD1306 I2C framing: every transfer starts with a control byte saying whether
# what follows is commands (0x00) or display data (0x40).
CTRL_CMD = 0x00
CTRL_DATA = 0x40

WIDTH = 128
HEIGHT = 32
PAGES = HEIGHT // 8          # SSD1306 RAM is column-major in 8-pixel pages
FRAME_BYTES = WIDTH * PAGES

# Chunk size for the data write. The i2c-dev write path has no 32-byte SMBus
# limit, but a whole 512-byte frame in one transfer is needlessly close to
# driver buffer limits for no gain — one page per write is both safe and tidy.
CHUNK = WIDTH

# Init sequence for the 128x32 variant. The two values that differ from the
# 128x64 sequence every example on the internet uses are the ones that matter:
# multiplex ratio 0x1F (32 rows, not 0x3F) and COM pin config 0x02 (sequential,
# not 0x12). Get either wrong and the panel shows a doubled or interlaced image
# that looks like a wiring fault.
INIT = (
    0xAE,                    # display off
    0xD5, 0x80,              # clock divide / oscillator frequency
    0xA8, 0x1F,              # multiplex ratio = 31 (32 rows)
    0xD3, 0x00,              # display offset
    0x40,                    # start line 0
    0x8D, 0x14,              # charge pump on (these modules have no external Vcc)
    0x20, 0x00,              # horizontal addressing mode
    # Orientation. 0xA1 + 0xC8 is the usual way up for these breakouts (ribbon
    # at the bottom); SEGDISPLAY_FLIP=1 sends 0xA0 + 0xC0 for a board mounted
    # upside down in the bezel, which is a mounting decision, not a code change.
    0xA0 if os.environ.get("SEGDISPLAY_FLIP") else 0xA1,
    0xC0 if os.environ.get("SEGDISPLAY_FLIP") else 0xC8,
    0xDA, 0x02,              # COM pins: sequential, no left/right remap
    0x81, 0x7F,              # contrast (overwritten by set_brightness)
    0xD9, 0xF1,              # pre-charge period
    0xDB, 0x40,              # VCOMH deselect level
    0xA4,                    # resume from RAM (not all-on)
    0xA6,                    # normal, not inverted
    0xAF,                    # display on
)

# Segment bits: a=0 b=1 c=2 d=3 e=4 f=5 g=6, decimal point = 7. Unchanged from
# the LED version — the bit layout is the natural one for a 7-segment glyph, and
# keeping it means encode() and the font did not have to be rewritten.
FONT = {
    "0": 0x3F, "1": 0x06, "2": 0x5B, "3": 0x4F, "4": 0x66,
    "5": 0x6D, "6": 0x7D, "7": 0x07, "8": 0x7F, "9": 0x6F,
    " ": 0x00, "-": 0x40,
    # Only the letters "StP" needs. S is the same glyph as 5, t and P are the
    # conventional 7-segment approximations.
    "S": 0x6D, "t": 0x78, "P": 0x73,
}

# SSD1306 contrast is 0..255 and is decidedly non-linear — most of the visible
# range is in the bottom third, so these are not evenly spaced.
BRIGHTNESS_LEVELS = {"LOW": 0x08, "MEDIUM": 0x40, "HIGH": 0xA0, "MAX": 0xFF}

# App views that mean "the user is in setup". ModuleList.qml is deliberately not
# here — that is the home screen, which counts as idle.
SETTINGS_VIEWS = (
    "views/Settings.qml",
    "views/ModuleSettings.qml",
    "views/MultiSelectSettings.qml",
    "views/RemapControls.qml",
    "views/Update.qml",
    "views/DirectoryBrowser.qml",
)


# ---------------------------------------------------------------------------
# Text -> segments
# ---------------------------------------------------------------------------

def encode(text):
    """Render up to 4 digit positions, right-aligned. Returns [b0,b1,b2,b3].

    A "." attaches to the digit before it rather than consuming a position,
    which is the whole reason every ATSC channel fits: "12.10" is four digits
    with the decimal point set on the second, not five characters.
    """
    cells = []
    for ch in str(text):
        if ch == "." and cells:
            cells[-1] |= 0x80
            continue
        if ch == ":":
            # The colon is drawn between the middle digits, not in one. Skipping
            # it here lets the rendered text stay readable ("03:47") in --show
            # and in /status without a second, colon-less spelling of every
            # string.
            continue
        if ch not in FONT:
            raise ValueError("no glyph for %r" % ch)
        cells.append(FONT[ch])

    if len(cells) > 4:
        raise ValueError("%r needs %d digits, display has 4" % (text, len(cells)))
    return [0x00] * (4 - len(cells)) + cells


# ---------------------------------------------------------------------------
# Segments -> pixels
# ---------------------------------------------------------------------------
#
# Geometry, all in pixels, chosen so four digits plus a colon plus decimal
# points fill 128x32 with a couple of pixels of slack for the burn-in shift:
#
#   4*20 digits + 2*5 plain gaps + 1*9 colon gap = 99 wide, 30 tall.

DIGIT_W = 20
DIGIT_H = 30
STROKE = 4                   # segment thickness
GAP = 5                      # between digits; a decimal point lives in here
COLON_GAP = 9                # the wider gap between digit 2 and digit 3
DOT = 3                      # decimal point / colon dot, square

LAYOUT_W = DIGIT_W * 4 + GAP * 2 + COLON_GAP
ORIGIN_X = (WIDTH - LAYOUT_W) // 2
ORIGIN_Y = 1

# Left edge of each digit, relative to ORIGIN_X.
DIGIT_X = (
    0,
    DIGIT_W + GAP,
    (DIGIT_W + GAP) * 2 + (COLON_GAP - GAP),
    (DIGIT_W + GAP) * 3 + (COLON_GAP - GAP),
)

# Seven rectangles per digit, (dx, dy, w, h) relative to the digit's top-left,
# indexed by the font's bit order: a b c d e f g.
_H = DIGIT_H // 2
SEGMENT_RECTS = (
    (STROKE, 0, DIGIT_W - 2 * STROKE, STROKE),                       # a  top
    (DIGIT_W - STROKE, STROKE, STROKE, _H - STROKE),                 # b  top right
    (DIGIT_W - STROKE, _H, STROKE, DIGIT_H - _H - STROKE),           # c  bottom right
    (STROKE, DIGIT_H - STROKE, DIGIT_W - 2 * STROKE, STROKE),        # d  bottom
    (0, _H, STROKE, DIGIT_H - _H - STROKE),                          # e  bottom left
    (0, STROKE, STROKE, _H - STROKE),                                # f  top left
    (STROKE, _H - STROKE // 2, DIGIT_W - 2 * STROKE, STROKE),        # g  middle
)

# Pixel-shift positions, stepped every SHIFT_SECONDS. An OLED showing a clock
# 24/7 is exactly the burn-in case, and the idle clock is the most static thing
# this panel ever shows. Two pixels is invisible at arm's length behind a bezel
# and is enough to keep any one pixel from carrying a segment edge for days.
SHIFT_STEPS = ((0, 0), (1, 0), (2, 0), (2, 1), (1, 1), (0, 1), (-1, 1), (-1, 0))
SHIFT_SECONDS = 300


def _rect(buf, x, y, w, h, dither=False):
    """Fill a rectangle in the page-packed frame buffer.

    dither draws every other pixel on both axes — a quarter-density fill, which
    on a monochrome panel is how you get a dim segment out of a display that has
    no per-pixel brightness.
    """
    for py in range(max(0, y), min(HEIGHT, y + h)):
        row = (py >> 3) * WIDTH
        bit = 1 << (py & 7)
        for px in range(max(0, x), min(WIDTH, x + w)):
            if dither and ((px & 1) or (py & 1)):
                continue
            buf[row + px] |= bit


def render_frame(cells, colon, ghost=False, shift=(0, 0)):
    """Four encoded cells -> a 512-byte SSD1306 frame."""
    buf = bytearray(FRAME_BYTES)
    ox = ORIGIN_X + shift[0]
    oy = ORIGIN_Y + shift[1]

    for cell, dx in zip(cells, DIGIT_X):
        x = ox + dx
        for bit, (sx, sy, sw, sh) in enumerate(SEGMENT_RECTS):
            on = cell & (1 << bit)
            if on:
                _rect(buf, x + sx, oy + sy, sw, sh)
            elif ghost:
                _rect(buf, x + sx, oy + sy, sw, sh, dither=True)
        if cell & 0x80:
            _rect(buf, x + DIGIT_W + 1, oy + DIGIT_H - DOT, DOT, DOT)
        elif ghost:
            _rect(buf, x + DIGIT_W + 1, oy + DIGIT_H - DOT, DOT, DOT, dither=True)

    # The colon sits in the wide gap after digit 2, at thirds of the digit
    # height — the position a clock radio puts it.
    cx = ox + DIGIT_X[1] + DIGIT_W + (COLON_GAP - DOT) // 2
    for cy in (oy + DIGIT_H // 3 - DOT // 2, oy + 2 * DIGIT_H // 3 - DOT // 2):
        if colon:
            _rect(buf, cx, cy, DOT, DOT)
        elif ghost:
            _rect(buf, cx, cy, DOT, DOT, dither=True)

    return buf


# ---------------------------------------------------------------------------
# I2C
# ---------------------------------------------------------------------------

class Display:
    """The panel. Falls back to a no-op, so the HTTP side and the render logic
    can be exercised on a machine with no display attached."""

    # Seconds between reopen attempts once the panel is gone.
    RETRY_SECONDS = 3

    def __init__(self, bus=I2C_BUS, addr=I2C_ADDR, quiet=False):
        self.bus = bus
        self.addr = addr
        self.contrast = BRIGHTNESS_LEVELS["MEDIUM"]
        self.fd = None
        self.error = None
        self.last = None
        self.quiet = quiet
        self._next_try = 0.0
        self.connect()
        if self.fd is None and not quiet:
            print("[segdisplay] no display (%s) — running dry" % self.error,
                  file=sys.stderr)

    def connect(self):
        """(Re)open the bus and re-init the chip. Safe to call every tick.

        Reconnecting matters more than it sounds: unplugging the panel for ten
        seconds of rewiring otherwise leaves the daemon dry until someone
        remembers to restart the unit, and a dry daemon looks exactly like a
        dead panel.
        """
        if self.fd is not None:
            return True
        now = time.monotonic()
        if now < self._next_try:
            return False
        self._next_try = now + self.RETRY_SECONDS
        try:
            fd = os.open(self.bus, os.O_RDWR)
        except OSError as exc:
            self.error = "%s: %s" % (self.bus, exc.strerror or exc)
            return False
        try:
            fcntl.ioctl(fd, I2C_SLAVE, self.addr)
            os.write(fd, bytes([CTRL_CMD]) + bytes(INIT))
            os.write(fd, bytes([CTRL_CMD, 0x81, self.contrast]))
        except OSError as exc:
            os.close(fd)
            self.error = "%s: %s" % (self.bus, exc.strerror or exc)
            return False
        self.fd = fd
        self.error = None
        # Whatever was on the panel before is gone; force the next frame out.
        self.last = None
        if not self.quiet:
            print("[segdisplay] display connected at 0x%02X" % self.addr)
        return True

    def _drop(self, exc):
        """A failed transfer means the panel went away — unplugged mid-rewire,
        or a loose jumper. Close so connect() can pick it up again."""
        self.error = "%s: %s" % (self.bus, exc.strerror or exc)
        try:
            os.close(self.fd)
        except OSError:
            pass
        self.fd = None
        self._next_try = time.monotonic() + self.RETRY_SECONDS
        if not self.quiet:
            print("[segdisplay] display lost (%s)" % self.error, file=sys.stderr)

    @property
    def connected(self):
        return self.fd is not None

    def _cmd(self, *bytes_):
        if self.fd is None:
            return
        try:
            os.write(self.fd, bytes([CTRL_CMD]) + bytes(bytes_))
        except OSError as exc:
            self._drop(exc)

    def set_brightness(self, contrast):
        # Remembered so a reconnect restores it rather than reverting.
        self.contrast = max(0, min(255, int(contrast)))
        self._cmd(0x81, self.contrast)

    def blit(self, buf, key=None):
        """Push one frame. `key` is whatever cheap value identifies it; an equal
        key skips the transfer, and the render loop ticks 4x a second with
        almost every tick a no-op."""
        if key is not None and key == self.last:
            return
        self.last = key

        if self.fd is None:
            return
        try:
            # Window the whole panel every frame. Partial updates would be
            # faster and are not worth the state: 512 bytes at 400kHz is ~13ms.
            os.write(self.fd, bytes([CTRL_CMD, 0x21, 0, WIDTH - 1,
                                     0x22, 0, PAGES - 1]))
            for start in range(0, FRAME_BYTES, CHUNK):
                os.write(self.fd, bytes([CTRL_DATA]) + bytes(buf[start:start + CHUNK]))
        except OSError as exc:
            self._drop(exc)

    def show(self, cells, colon, ghost=False, shift=(0, 0)):
        key = (tuple(cells), bool(colon), bool(ghost), tuple(shift))
        if key == self.last:
            return
        self.blit(render_frame(cells, colon, ghost, shift), key)

    def clear(self):
        self.blit(bytearray(FRAME_BYTES), "clear")

    def close(self):
        if self.fd is None:
            return
        self.clear()
        self._cmd(0xAE)          # display off
        os.close(self.fd)
        self.fd = None


# ---------------------------------------------------------------------------
# Settings — read straight from 240-MP's config.json
# ---------------------------------------------------------------------------

def data_root():
    home = pathlib.Path.home()
    if sys.platform == "darwin":
        return home / "Library/Application Support/240-MP"
    return home / ".local/share/240-MP"


CONFIG = pathlib.Path(os.environ.get("MP240_CONFIG", data_root() / "config.json"))


def is_on(value, default=True):
    # Toggles are stored as the manifest's "ON"/"OFF" strings, but ModuleSettings
    # also writes real JSON bools for some rows, so accept both.
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() in ("ON", "TRUE", "1")


class Settings:
    """config.json, re-read when it changes. No IPC: the settings screen already
    persists there, so the app never has to tell us anything about them."""

    def __init__(self, path=CONFIG):
        self.path = path
        self._mtime = None
        self._values = {}
        self.reload()

    def reload(self):
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._values = {}
            return False
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        try:
            cfg = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return False
        self._values = (cfg.get("modules") or {}).get(MODULE_ID) or {}
        return True

    def enabled(self):
        return is_on(self._values.get("enabled"), True)

    def brightness(self):
        name = str(self._values.get("brightness", "Medium")).strip().upper()
        return BRIGHTNESS_LEVELS.get(name, BRIGHTNESS_LEVELS["MEDIUM"])

    def idle_clock(self):
        return is_on(self._values.get("idle_clock"), True)

    def clock_24h(self):
        return is_on(self._values.get("clock_24h"), False)

    def ghost_segments(self):
        return is_on(self._values.get("ghost_segments"), False)

    def module_shown(self, folder):
        """Per-module opt-out. A module with no key defaults to shown, so a
        module added later works without touching the manifest."""
        if not folder:
            return True
        return is_on(self._values.get("show_" + folder), True)


# ---------------------------------------------------------------------------
# State + render
# ---------------------------------------------------------------------------

def module_folder(view):
    """"modules/hdhomerun/views/Channels.qml" -> "hdhomerun"; app views -> ""."""
    parts = str(view).split("/")
    if len(parts) >= 2 and parts[0] == "modules":
        return parts[1]
    return ""


def format_elapsed(position_ms):
    """MM:SS under an hour, H:MM at or over it — the same switch a VCR makes.

    Counting seconds past an hour would need five digits, and the seconds stop
    being the interesting number on a feature-length title anyway.
    """
    seconds = max(0, int(position_ms) // 1000)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours == 0:
        return "%02d:%02d" % (minutes, secs)
    return "%d:%02d" % (min(hours, 99), minutes)


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.view = ""
        self.channel = ""
        self.position_ms = 0
        self.playing = False

    def update(self, msg):
        with self.lock:
            if "view" in msg:
                self.view = str(msg["view"] or "")
                # The channel belongs to the HDHomeRun module. Leaving it must
                # drop the number, or the display keeps showing a channel while
                # the user is three screens away.
                if module_folder(self.view) != "hdhomerun":
                    self.channel = ""
            if "channel" in msg:
                self.channel = str(msg["channel"] or "")
            if "position_ms" in msg:
                self.position_ms = int(msg["position_ms"] or 0)
            if "playing" in msg:
                self.playing = bool(msg["playing"])
                if not self.playing:
                    self.position_ms = 0

    def snapshot(self):
        with self.lock:
            return dict(view=self.view, channel=self.channel,
                        position_ms=self.position_ms, playing=self.playing)


def render(state, settings, now):
    """Decide what the panel shows. Returns (text, colon) — text is what the
    digits spell, colon is drawn separately because it is not a digit.

    Priority order, and each rung is a deliberate choice:
      setup > channel > elapsed > clock > blank
    HDHomeRun sits on the channel rung even while playing: live TV has no
    elapsed time worth showing, and the channel is the thing you glance down for.
    """
    if not settings.enabled():
        return "", False

    snap = state.snapshot()
    view = snap["view"]

    if view in SETTINGS_VIEWS:
        return "StP", False

    folder = module_folder(view)
    shown = settings.module_shown(folder)

    if shown and snap["channel"]:
        return snap["channel"], False

    if shown and snap["playing"]:
        # No pause state to show: MpvController keeps mpv's pause flag private
        # (MpvController.h:132) and publishes no signal for it. Pausing simply
        # freezes the digits, which is what a VCR does anyway.
        return format_elapsed(snap["position_ms"]), int(now) % 2 == 0

    if settings.idle_clock():
        stamp = datetime.now()
        hour = stamp.hour if settings.clock_24h() else (stamp.hour % 12 or 12)
        # 12-hour blanks the leading zero the way a clock radio does; 24-hour
        # keeps it, so 09:05 stays four digits.
        text = ("%02d:%02d" if settings.clock_24h() else "%2d:%02d") % (hour, stamp.minute)
        return text, int(now) % 2 == 0

    return "", False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    # Injected by serve().
    state = None
    settings = None
    display = None
    current = ("", False)

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # One line per position update would drown the journal.

    def _reply(self, code, payload=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(payload)))
        if payload:
            self.send_header("Content-Type", "application/json")
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if self.path == "/test":
            threading.Thread(target=display_test, args=(Handler.display,),
                             daemon=True).start()
            self._reply(200, b'{"ok":true}')
            return
        if self.path != "/state":
            self._reply(404)
            return
        try:
            msg = json.loads(raw or b"{}")
        except ValueError:
            self._reply(400)
            return
        if isinstance(msg, dict):
            Handler.state.update(msg)
        self._reply(200, b'{"ok":true}')

    def do_GET(self):
        if self.path != "/status":
            self._reply(404)
            return
        text, colon = Handler.current
        body = dict(Handler.state.snapshot(),
                    text=text, colon=colon,
                    connected=bool(Handler.display and Handler.display.connected),
                    error=(Handler.display.error if Handler.display else "i2c not opened"))
        self._reply(200, json.dumps(body).encode())


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

# Held for the duration of a display test so the render loop stops writing.
# Without it the loop repaints the current text every 0.25s and the test is
# invisible — which is exactly when you are trying to see it.
TESTING = threading.Event()


def display_test(display):
    """Every pixel, then the segments one at a time, then 8.8.8.8 with colon.

    The all-pixels frame answers "is the whole 128x32 area alive and is it the
    right way up"; the segment sweep and the 8.8.8.8 answer "does the glyph
    geometry land where it should", which is the part that is easy to get wrong
    by two pixels and never notice on a clock face alone.
    """
    TESTING.set()
    try:
        display.blit(bytearray([0xFF] * FRAME_BYTES), "test-all")
        time.sleep(1.0)
        for bit in range(8):
            display.show([1 << bit] * 4, False)
            time.sleep(0.15)
        display.show(encode("8.8.8.8."), True)
        time.sleep(1.5)
        display.clear()
    finally:
        TESTING.clear()


def serve():
    display = Display()
    settings = Settings()
    state = State()
    display.set_brightness(settings.brightness())

    Handler.state = state
    Handler.settings = settings
    Handler.display = display

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    print("[segdisplay] listening on 127.0.0.1:%d, display %s"
          % (PORT, "ready" if display.connected else "ABSENT"))

    try:
        while True:
            if TESTING.is_set():
                time.sleep(0.25)
                continue
            display.connect()
            if settings.reload():
                display.set_brightness(settings.brightness())
            now = time.time()
            text, colon = render(state, settings, now)
            Handler.current = (text, colon)
            shift = SHIFT_STEPS[int(now // SHIFT_SECONDS) % len(SHIFT_STEPS)]
            try:
                display.show(encode(text), colon, settings.ghost_segments(), shift)
            except ValueError as exc:
                # A channel number that will not fit is a bug, not a crash.
                print("[segdisplay] %s" % exc, file=sys.stderr)
                display.show(encode("----"), False, settings.ghost_segments(), shift)
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        display.close()


def main():
    args = sys.argv[1:]
    if not args:
        serve()
        return 0

    if args[0] in ("-h", "--help"):
        print(__doc__)
        print("usage: segdisplay.py [--probe | --test | --show TEXT [--ghost] | --clear]")
        return 0

    if args[0] == "--probe":
        display = Display(quiet=True)
        if not display.connected:
            print("NOT FOUND at 0x%02X on %s (%s)" % (I2C_ADDR, I2C_BUS, display.error))
            return 1
        print("OK — device responded at 0x%02X on %s" % (I2C_ADDR, I2C_BUS))
        display.close()
        return 0

    display = Display()
    try:
        if args[0] == "--test":
            display_test(display)
        elif args[0] == "--clear":
            display.clear()
        elif args[0] == "--show":
            if len(args) < 2:
                print("--show needs a value", file=sys.stderr)
                return 2
            text = args[1]
            display.show(encode(text), ":" in text, "--ghost" in args[2:])
            # Leave it up: os.close() alone does not blank an SSD1306, and for a
            # bring-up check that is the point.
            print("showing %r" % text)
            return 0
        else:
            print("unknown option %r" % args[0], file=sys.stderr)
            return 2
    finally:
        if args[0] != "--show":
            display.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
