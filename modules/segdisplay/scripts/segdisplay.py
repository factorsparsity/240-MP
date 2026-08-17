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

def digit_rects(w, h, t):
    """Seven rectangles for one digit, (dx, dy, w, h) relative to its top-left,
    in the font's bit order: a b c d e f g. Parameterised by size because the
    weather screen draws the same glyphs smaller alongside text."""
    half = h // 2
    return (
        (t, 0, w - 2 * t, t),                    # a  top
        (w - t, t, t, half - t),                 # b  top right
        (w - t, half, t, h - half - t),          # c  bottom right
        (t, h - t, w - 2 * t, t),                # d  bottom
        (0, half, t, h - half - t),              # e  bottom left
        (0, t, t, half - t),                     # f  top left
        (t, half - t // 2, w - 2 * t, t),        # g  middle
    )


SEGMENT_RECTS = digit_rects(DIGIT_W, DIGIT_H, STROKE)

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
# A 5x7 font, for the two screens seven segments cannot spell
# ---------------------------------------------------------------------------
#
# Seven segments can draw a channel, a clock and "StP" and nothing else, which
# was fine on the LED board because nothing else was possible. On a 128x32 OLED
# it is a self-imposed limit, so menu labels and weather conditions get real
# glyphs. The 7-segment look still owns every screen that is fundamentally a
# number — that is the panel's character, not a limitation to route around.
#
# Rows top to bottom, "1" is a lit pixel. Authored as strings rather than packed
# hex so a wrong glyph is visible in the diff instead of needing a decoder.
_GLYPHS = {
    "0": "01110/10001/10011/10101/11001/10001/01110",
    "1": "00100/01100/00100/00100/00100/00100/01110",
    "2": "01110/10001/00001/00010/00100/01000/11111",
    "3": "11111/00010/00100/00010/00001/10001/01110",
    "4": "00010/00110/01010/10010/11111/00010/00010",
    "5": "11111/10000/11110/00001/00001/10001/01110",
    "6": "00110/01000/10000/11110/10001/10001/01110",
    "7": "11111/00001/00010/00100/01000/01000/01000",
    "8": "01110/10001/10001/01110/10001/10001/01110",
    "9": "01110/10001/10001/01111/00001/00010/01100",
    "A": "01110/10001/10001/11111/10001/10001/10001",
    "B": "11110/10001/10001/11110/10001/10001/11110",
    "C": "01110/10001/10000/10000/10000/10001/01110",
    "D": "11100/10010/10001/10001/10001/10010/11100",
    "E": "11111/10000/10000/11110/10000/10000/11111",
    "F": "11111/10000/10000/11110/10000/10000/10000",
    "G": "01110/10001/10000/10111/10001/10001/01111",
    "H": "10001/10001/10001/11111/10001/10001/10001",
    "I": "01110/00100/00100/00100/00100/00100/01110",
    "J": "00111/00010/00010/00010/00010/10010/01100",
    "K": "10001/10010/10100/11000/10100/10010/10001",
    "L": "10000/10000/10000/10000/10000/10000/11111",
    "M": "10001/11011/10101/10101/10001/10001/10001",
    "N": "10001/11001/11001/10101/10011/10011/10001",
    "O": "01110/10001/10001/10001/10001/10001/01110",
    "P": "11110/10001/10001/11110/10000/10000/10000",
    "Q": "01110/10001/10001/10001/10101/10010/01101",
    "R": "11110/10001/10001/11110/10100/10010/10001",
    "S": "01111/10000/10000/01110/00001/00001/11110",
    "T": "11111/00100/00100/00100/00100/00100/00100",
    "U": "10001/10001/10001/10001/10001/10001/01110",
    "V": "10001/10001/10001/10001/10001/01010/00100",
    "W": "10001/10001/10001/10101/10101/11011/01010",
    "X": "10001/10001/01010/00100/01010/10001/10001",
    "Y": "10001/10001/01010/00100/00100/00100/00100",
    "Z": "11111/00001/00010/00100/01000/10000/11111",
    " ": "00000/00000/00000/00000/00000/00000/00000",
    "-": "00000/00000/00000/11111/00000/00000/00000",
    ".": "00000/00000/00000/00000/00000/01100/01100",
    ",": "00000/00000/00000/00000/01100/01100/01000",
    "'": "01100/01100/01000/00000/00000/00000/00000",
    "/": "00001/00010/00010/00100/01000/01000/10000",
    "&": "01100/10010/10100/01000/10101/10010/01101",
    "+": "00000/00100/00100/11111/00100/00100/00000",
    ":": "00000/01100/01100/00000/01100/01100/00000",
    "(": "00010/00100/01000/01000/01000/00100/00010",
    ")": "01000/00100/00010/00010/00010/00100/01000",
    "%": "11001/11010/00010/00100/01000/01011/10011",
    "?": "01110/10001/00001/00010/00100/00000/00100",
    "!": "00100/00100/00100/00100/00100/00000/00100",
}

GLYPH_W = 5
GLYPH_H = 7
FONT5X7 = {ch: tuple(rows.split("/")) for ch, rows in _GLYPHS.items()}


def text_width(text, scale=1):
    """Rendered width including the trailing inter-character gap's absence."""
    n = len(text)
    return 0 if n == 0 else (n * (GLYPH_W + 1) - 1) * scale


def draw_text(buf, x, y, text, scale=1, dither=False):
    """Draw uppercase 5x7 text. Unknown characters render as a space rather
    than raising: this draws live weather descriptions and menu labels from
    upstream, and a missing glyph must never take the panel down."""
    cx = x
    for ch in str(text).upper():
        rows = FONT5X7.get(ch)
        if rows is not None:
            for ry, row in enumerate(rows):
                for rx, cell in enumerate(row):
                    if cell == "1":
                        _rect(buf, cx + rx * scale, y + ry * scale, scale, scale,
                              dither=dither)
        cx += (GLYPH_W + 1) * scale
    return cx


# Labels are always this size — never scaled to fit. A label that shrank to suit
# its own length made the panel jump between two type sizes as the cursor moved,
# which reads as a glitch rather than as a design. At 4x a glyph is 20x28, close
# enough to the 30px 7-segment digits that the two screens look like one device;
# what does not fit scrolls instead. This is the knob: 3 shows ~7 characters at
# 21px, 4 shows ~5 at 28px.
LABEL_SCALE = 4
LABEL_MARGIN = 2

# Marquee timing. Parked at each end long enough to actually read the ends,
# which is the point of holding at all.
MARQUEE_HOLD = 1.2         # seconds stationary at start and at end
MARQUEE_SPEED = 44.0       # pixels per second
MARQUEE_TICK = 0.05        # render period while something is scrolling


# ---------------------------------------------------------------------------
# The two text screens
# ---------------------------------------------------------------------------

# Temperature block: the same 7-segment glyphs, smaller, so the weather screen
# still reads as this panel and not as a different device.
TEMP_W, TEMP_H, TEMP_STROKE = 14, 24, 3
TEMP_RECTS = digit_rects(TEMP_W, TEMP_H, TEMP_STROKE)
TEMP_X, TEMP_Y = 2, 4
TEMP_PITCH = TEMP_W + 3
DEGREE_X = TEMP_X + TEMP_PITCH * 3 - 3
DEGREE_SIZE, DEGREE_STROKE = 6, 2

TEXT_X = DEGREE_X + DEGREE_SIZE + 3
TEXT_MAX = WIDTH - TEXT_X - 1
TEXT_ROW1, TEXT_ROW2 = 6, 18

# Max characters in the right-hand column at 1x. Conditions are shortened to fit
# rather than scrolled: a marquee on a front panel is a distraction, and every
# WMO code has a sensible short form.
COND_CHARS = (TEXT_MAX + 1) // (GLYPH_W + 1)

# WMO weather codes -> a label that fits. open-meteo returns the code; the app's
# own NWS text is not reachable from here, and would not fit anyway.
WMO = {
    0: "CLEAR", 1: "MAINLY CLR", 2: "PARTLY CLDY", 3: "OVERCAST",
    45: "FOG", 48: "RIME FOG",
    51: "LT DRIZZLE", 53: "DRIZZLE", 55: "HVY DRIZZLE",
    56: "FRZ DRIZZLE", 57: "FRZ DRIZZLE",
    61: "LIGHT RAIN", 63: "RAIN", 65: "HEAVY RAIN",
    66: "FRZ RAIN", 67: "FRZ RAIN",
    71: "LIGHT SNOW", 73: "SNOW", 75: "HEAVY SNOW", 77: "SNOW GRAINS",
    80: "RAIN SHWRS", 81: "RAIN SHWRS", 82: "HVY SHWRS",
    85: "SNOW SHWRS", 86: "SNOW SHWRS",
    95: "T-STORM", 96: "TSTM HAIL", 99: "TSTM HAIL",
}


def render_weather_frame(temp, code, high, low, ghost=False, shift=(0, 0)):
    """Big 7-segment temperature on the left, condition and hi/lo on the right.

    Temperature is right-aligned in three cells so -12 and 100 both land without
    the layout moving; two-digit temperatures leave the first cell blank.
    """
    buf = bytearray(FRAME_BYTES)
    ox, oy = shift[0], shift[1]

    text = "%d" % int(round(temp))
    cells = list(text[-3:].rjust(3))
    for i, ch in enumerate(cells):
        cell = FONT.get(ch, 0x00)
        x = TEMP_X + ox + i * TEMP_PITCH
        for bit, (sx, sy, sw, sh) in enumerate(TEMP_RECTS):
            if cell & (1 << bit):
                _rect(buf, x + sx, TEMP_Y + oy + sy, sw, sh)
            elif ghost:
                _rect(buf, x + sx, TEMP_Y + oy + sy, sw, sh, dither=True)

    # Degree mark: a hollow square, which is what the a+b+f+g segment trick
    # approximates on a real 7-segment panel — here it can just be drawn.
    dx, dy = DEGREE_X + ox, TEMP_Y + oy
    _rect(buf, dx, dy, DEGREE_SIZE, DEGREE_STROKE)
    _rect(buf, dx, dy + DEGREE_SIZE - DEGREE_STROKE, DEGREE_SIZE, DEGREE_STROKE)
    _rect(buf, dx, dy, DEGREE_STROKE, DEGREE_SIZE)
    _rect(buf, dx + DEGREE_SIZE - DEGREE_STROKE, dy, DEGREE_STROKE, DEGREE_SIZE)

    label = (WMO.get(code) or "")[:COND_CHARS]
    draw_text(buf, TEXT_X + ox, TEXT_ROW1 + oy, label)

    if high is not None and low is not None:
        draw_text(buf, TEXT_X + ox, TEXT_ROW2 + oy,
                  ("H%d L%d" % (int(round(high)), int(round(low))))[:COND_CHARS])
    return buf


def label_travel(label):
    """Pixels a label has to scroll to reveal its tail. 0 means it fits."""
    return max(0, text_width(label, LABEL_SCALE) - (WIDTH - 2 * LABEL_MARGIN))


def render_label_frame(label, offset=0, shift=(0, 0)):
    """One text label — a menu selection, or SETUP — at full height.

    Short labels are centred; anything wider than the panel is left-aligned and
    drawn shifted by `offset`, which the caller ramps to scroll it. Clipping is
    free: _rect already discards pixels outside the panel.
    """
    buf = bytearray(FRAME_BYTES)
    label = str(label).upper().strip()
    ox, oy = shift[0], shift[1]
    y = (HEIGHT - GLYPH_H * LABEL_SCALE) // 2

    width = text_width(label, LABEL_SCALE)
    if label_travel(label) <= 0:
        x = (WIDTH - width) // 2
    else:
        x = LABEL_MARGIN - int(offset)
    draw_text(buf, ox + x, oy + y, label, LABEL_SCALE)
    return buf


class Marquee:
    """Where a label sits this instant: parked, scrolling, parked, then back.

    Deliberately a function of elapsed time rather than an accumulated position,
    so a slow tick or a missed frame cannot make the text drift out of step with
    itself — it just draws the phase it should be at.
    """

    def __init__(self):
        self.text = None
        self.started = 0.0

    def offset(self, label, now):
        if label != self.text:
            self.text = label
            self.started = now
        travel = label_travel(label)
        if travel <= 0:
            return 0
        crossing = travel / MARQUEE_SPEED
        period = MARQUEE_HOLD + crossing + MARQUEE_HOLD
        phase = (now - self.started) % period
        if phase < MARQUEE_HOLD:
            return 0
        if phase < MARQUEE_HOLD + crossing:
            return (phase - MARQUEE_HOLD) * MARQUEE_SPEED
        return travel


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
        self._weather = {}
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
        modules = cfg.get("modules") or {}
        self._values = modules.get(MODULE_ID) or {}
        self._weather = modules.get("com.240mp.weather") or {}
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

    def weather_units(self):
        """Follow the weather module's own Metric/US setting rather than adding
        a second, contradictable one here."""
        units = str(self._weather.get("units", "Metric")).strip().upper()
        return "fahrenheit" if units == "US" else "celsius"

    def module_shown(self, folder):
        """Per-module opt-out. A module with no key defaults to shown, so a
        module added later works without touching the manifest."""
        if not folder:
            return True
        return is_on(self._values.get("show_" + folder), True)


# ---------------------------------------------------------------------------
# Weather — fetched here, not handed over by the app
# ---------------------------------------------------------------------------

LOCATION_FILE = CONFIG.parent / "weather_location.txt"


class Weather:
    """Current conditions for the weather screen, straight from open-meteo.

    The app cannot give us this. 240-MP's weather data lives in C++
    (WeatherBackend) with no signal or property reachable from module QML, its
    background music runs on a private mpv so not even the elapsed-time rung
    fires, and patching upstream's weather views would add another file a 240-MP
    upgrade reverts. Reading weather_location.txt directly is the same move this
    daemon already makes for config.json, and it works whether or not the
    weather module is even open.

    open-meteo needs no API key, which is why it is the forecast source here
    even though the app itself prefers NWS observations for the on-screen text.
    """

    REFRESH = 600            # seconds between successful polls
    RETRY_FIRST = 10         # first retry after a failure
    RETRY_MAX = 60           # ceiling for the backoff
    TIMEOUT = 10
    # How long a reading from the cache file is worth showing. Long enough to
    # cover a reboot or an outage, short enough that nobody reads yesterday's
    # temperature off the front panel and believes it.
    STALE_AFTER = 3 * 3600
    FORECAST = "https://api.open-meteo.com/v1/forecast"
    GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"

    def __init__(self, settings, path=LOCATION_FILE):
        self.settings = settings
        self.path = path
        self.cache = CONFIG.parent / "segdisplay_weather.json"
        self.lock = threading.Lock()
        self.data = None     # (temp, code, high, low)
        self.error = None
        self.coords = None
        self._next = 0.0
        self._fails = 0
        self._load_cache()

    # -- cache ------------------------------------------------------------
    def _load_cache(self):
        """Last good reading, so a reboot has something to show immediately.

        Without this, a cold boot plus one transient 503 leaves the weather
        screen showing the clock for a full retry interval — which is exactly
        the window someone walks into the module in.
        """
        try:
            body = json.loads(self.cache.read_text())
            if time.time() - float(body["time"]) < self.STALE_AFTER:
                self.data = tuple(body["data"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save_cache(self, data):
        try:
            self.cache.write_text(json.dumps({"time": time.time(),
                                              "data": list(data)}))
        except OSError:
            pass     # a read-only data dir is not worth failing a poll over

    # -- location ---------------------------------------------------------
    def _first_line(self):
        try:
            for raw in self.path.read_text().splitlines():
                line = raw.strip()
                if line and not line.startswith("#"):
                    return line
        except OSError:
            return None
        return None

    def _resolve(self):
        """"45.07,-93.45" -> coordinates; a place name -> one geocode lookup.

        Deliberately the same two forms the app accepts, so one file drives both
        and the panel can never disagree with the screen about where you are.
        """
        line = self._first_line()
        if not line:
            self.error = "no weather_location.txt"
            return None
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            try:
                return (float(parts[0]), float(parts[1]))
            except ValueError:
                pass
        # A name. Send the leading component only: "Maple Grove, MN, USA"
        # geocodes reliably as "Maple Grove", and the qualifier matching the app
        # does for the remainder is not worth reimplementing for a fallback.
        try:
            body = self._get(self.GEOCODE, name=parts[0], count=1, format="json")
            hit = (body.get("results") or [None])[0]
            if not hit:
                self.error = "geocode found nothing for %r" % parts[0]
                return None
            return (float(hit["latitude"]), float(hit["longitude"]))
        except Exception as exc:                      # noqa: BLE001 - see below
            self.error = "geocode failed: %s" % exc
            return None

    # -- fetch ------------------------------------------------------------
    def _get(self, url, **params):
        import urllib.parse
        import urllib.request
        full = url + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(full, timeout=self.TIMEOUT) as resp:
            return json.loads(resp.read().decode())

    def poll(self, now):
        """One attempt, if due. Called from a background thread — never from the
        render loop, which must keep its 0.25s tick even with the network down."""
        if now < self._next:
            return
        # Back off from 10s rather than sitting out a flat minute: the failure
        # that actually happens is a boot-time one (no DNS yet, or a transient
        # 503), and it clears in seconds.
        self._next = now + min(self.RETRY_MAX,
                               self.RETRY_FIRST * (2 ** self._fails))
        try:
            if self.coords is None:
                self.coords = self._resolve()
                if self.coords is None:
                    return
            lat, lon = self.coords
            body = self._get(self.FORECAST, latitude=lat, longitude=lon,
                             current="temperature_2m,weather_code",
                             daily="temperature_2m_max,temperature_2m_min",
                             temperature_unit=self.settings.weather_units(),
                             timezone="auto", forecast_days=1)
            current = body["current"]
            daily = body.get("daily") or {}
            fresh = (
                float(current["temperature_2m"]),
                int(current.get("weather_code") or 0),
                (daily.get("temperature_2m_max") or [None])[0],
                (daily.get("temperature_2m_min") or [None])[0],
            )
            with self.lock:
                self.data = fresh
                self.error = None
            self._save_cache(fresh)
            self._fails = 0
            self._next = now + self.REFRESH
        # Every failure mode here is the same failure — no data this time — and
        # they are all transient: no DNS at boot, no route, a 503, malformed
        # JSON. The panel's job is to fall back to the clock, not to crash a
        # thread the render loop depends on.
        except Exception as exc:                      # noqa: BLE001
            self._fails = min(self._fails + 1, 8)
            with self.lock:
                self.error = str(exc)

    def snapshot(self):
        with self.lock:
            return self.data


def weather_thread(weather):
    while True:
        weather.poll(time.monotonic())
        time.sleep(5)


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
        self.label = ""
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
                # Same reasoning for the highlighted row: it belongs to the list
                # that was on screen. The hook sends view and label in ONE
                # message so the two can never be applied out of order — this
                # server is threaded, and a label arriving just after the view
                # that invalidated it would stick until the next keypress.
                if "label" not in msg:
                    self.label = ""
            if "label" in msg:
                self.label = str(msg["label"] or "")
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
            return dict(view=self.view, channel=self.channel, label=self.label,
                        position_ms=self.position_ms, playing=self.playing)


def render(state, settings, weather, now):
    """Decide what the panel shows. Returns a scene: ("digits", (text, colon)),
    ("label", text) or ("weather", (temp, code, high, low)).

    Priority order, and each rung is a deliberate choice:
      menu label > setup > channel > weather > elapsed > clock > blank

    The menu label leads because it is the only rung that is *feedback* — it
    changes as the user presses a key, and a panel that lags the keypress is
    worse than one that says nothing. HDHomeRun sits on the channel rung even
    while playing: live TV has no elapsed time worth showing, and the channel is
    the thing you glance down for.
    """
    if not settings.enabled():
        return ("digits", ("", False))

    snap = state.snapshot()
    view = snap["view"]
    folder = module_folder(view)
    shown = settings.module_shown(folder)

    # Only for the app's own screens. A module's internal lists (the channel
    # list, a playlist) have something better to say, and their highlighted row
    # is usually far too long for a front panel anyway.
    if snap["label"] and not folder:
        return ("label", snap["label"])

    if view in SETTINGS_VIEWS:
        return ("label", "SETUP")

    if shown and snap["channel"]:
        return ("digits", (snap["channel"], False))

    if shown and folder == "weather":
        current = weather.snapshot() if weather else None
        if current:
            return ("weather", current)

    if shown and snap["playing"]:
        # No pause state to show: MpvController keeps mpv's pause flag private
        # (MpvController.h:132) and publishes no signal for it. Pausing simply
        # freezes the digits, which is what a VCR does anyway.
        return ("digits", (format_elapsed(snap["position_ms"]), int(now) % 2 == 0))

    if settings.idle_clock():
        stamp = datetime.now()
        hour = stamp.hour if settings.clock_24h() else (stamp.hour % 12 or 12)
        # 12-hour blanks the leading zero the way a clock radio does; 24-hour
        # keeps it, so 09:05 stays four digits.
        text = ("%02d:%02d" if settings.clock_24h() else "%2d:%02d") % (hour, stamp.minute)
        return ("digits", (text, int(now) % 2 == 0))

    return ("digits", ("", False))


def render_scene(scene, ghost=False, shift=(0, 0), offset=0):
    kind, payload = scene
    if kind == "weather":
        return render_weather_frame(*payload, ghost=ghost, shift=shift)
    if kind == "label":
        return render_label_frame(payload, offset, shift)
    text, colon = payload
    return render_frame(encode(text), colon, ghost, shift)


def scene_text(scene):
    """What /status reports, and therefore what the module's status screen
    shows. One readable string per scene, whatever the scene draws."""
    kind, payload = scene
    if kind == "weather":
        temp, code, high, low = payload
        return "%d' %s" % (round(temp), WMO.get(code, ""))
    if kind == "label":
        return payload
    return payload[0]


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    # Injected by serve().
    state = None
    settings = None
    display = None
    weather = None
    current = ("digits", ("", False))

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
        kind, _payload = Handler.current
        body = dict(Handler.state.snapshot(),
                    text=scene_text(Handler.current), kind=kind,
                    connected=bool(Handler.display and Handler.display.connected),
                    error=(Handler.display.error if Handler.display else "i2c not opened"),
                    weather_error=(Handler.weather.error if Handler.weather else None))
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
    weather = Weather(settings)
    marquee = Marquee()
    display.set_brightness(settings.brightness())

    Handler.state = state
    Handler.settings = settings
    Handler.display = display
    Handler.weather = weather

    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Thread(target=weather_thread, args=(weather,), daemon=True).start()
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
            scene = render(state, settings, weather, now)
            Handler.current = scene
            ghost = settings.ghost_segments()
            shift = SHIFT_STEPS[int(now // SHIFT_SECONDS) % len(SHIFT_STEPS)]

            scrolling = False
            offset = 0
            if scene[0] == "label":
                offset = marquee.offset(str(scene[1]).upper().strip(), now)
                scrolling = label_travel(str(scene[1]).upper().strip()) > 0
                # Quantised so a parked marquee produces an unchanged key and
                # therefore no I2C traffic at all while it sits at either end.
                offset = int(offset)

            key = (scene, ghost, shift, offset)
            if key != display.last:
                try:
                    buf = render_scene(scene, ghost, shift, offset)
                except ValueError as exc:
                    # A channel number that will not fit is a bug, not a crash.
                    print("[segdisplay] %s" % exc, file=sys.stderr)
                    buf = render_frame(encode("----"), False, ghost, shift)
                display.blit(buf, key)

            # Fast only while text is actually moving. A 512-byte frame is ~13ms
            # on a 400kHz bus, so 20Hz costs a quarter of the bus during a scroll
            # and nothing at all the rest of the time.
            time.sleep(MARQUEE_TICK if scrolling else 0.25)
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
        print("usage: segdisplay.py [--probe | --test | --show TEXT [--ghost]"
              " | --label TEXT | --weather | --clear]")
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
        elif args[0] == "--label":
            if len(args) < 2:
                print("--label needs a value", file=sys.stderr)
                return 2
            display.blit(render_label_frame(args[1]), ("label", args[1]))
            print("showing %r (%d px of scroll needed)"
                  % (args[1], label_travel(str(args[1]).upper().strip())))
            return 0
        elif args[0] == "--weather":
            # Live, not canned: this is the mode that answers "is the location
            # file readable and does this box have a route to open-meteo",
            # which is most of what can go wrong with the weather screen.
            weather = Weather(Settings())
            weather.poll(time.monotonic())
            data = weather.snapshot()
            if not data:
                print("no weather: %s" % weather.error, file=sys.stderr)
                return 1
            display.blit(render_weather_frame(*data), ("weather", data))
            print("showing %d' %s (H%s L%s)"
                  % (round(data[0]), WMO.get(data[1], "?"), data[2], data[3]))
            return 0
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
        # --show/--label/--weather return early and deliberately leave the frame
        # up; only the modes that fall through here should blank the panel.
        if args[0] not in ("--show", "--label", "--weather"):
            display.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
