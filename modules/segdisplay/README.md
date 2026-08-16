# Front Panel — SSD1306 OLED, drawn as a 7-segment display

Drives a **0.91" SSD1306 128x32 I2C OLED** (address `0x3C`) from a Raspberry Pi
running 240-MP, painting four 7-segment digits into the frame buffer so the
cable box's front panel shows something real:

| Where you are | What the panel shows |
|---|---|
| HDHomeRun (browsing or watching) | the channel, decimal point and all — `9.1`, `12.10` |
| Any other playback module | elapsed time, `MM:SS` under an hour, `H:MM` over it |
| The setup screen | `StP` |
| Anywhere else | the time of day, colon blinking |

Every module can be switched off individually in
**Settings → Modules → Front Panel**.

**This replaced a VK16K33/HT16K33 4-digit LED module**, which never worked on
the bench. The behaviour above is unchanged from that design; only the hardware
layer is new. Drawing the glyphs instead of lighting bars also buys three things
the LED board could not do:

- **Decimal points that definitely exist.** Half the 4-digit "clock" boards on
  the market carry a colon and no per-digit points, and `12.10` needs one.
- **Ghost segments** (`Ghost Unlit Segments`, off by default) — the unlit
  segments drawn at quarter density, which is how a real LED panel shows a faint
  `8888` behind the digits. There is no per-pixel brightness on a mono OLED, so
  the dimming is a dither.
- **Burn-in protection.** The whole layout shifts by up to two pixels every five
  minutes. An idle clock is exactly the static image that burns an OLED, and
  this box is meant to sit powered on.

## Wiring

| Module | Pi pin | Signal |
|---|---|---|
| `GND` | 6 | GND |
| `VCC` | 1 (3V3) | 3.3 V |
| `SDA` | 3 | GPIO2 / SDA1 |
| `SCL` | 5 | GPIO3 / SCL1 |

Power from 3V3. SSD1306 breakouts have an onboard regulator and charge pump and
run fine at 3.3 V, and the Pi's I2C lines never swing higher — so unlike the LED
board this replaced, there is no level-shifting question to get wrong.

`deploy-segdisplay.sh` handles the two Pi-side prerequisites (`dtparam=i2c_arm=on`
and `i2c` group membership), both of which need a reboot.

Two environment variables in `segdisplay.service` cover the board-to-board
differences: `SEGDISPLAY_I2C_ADDR=0x3D` for a panel strapped to the alternate
address, and `SEGDISPLAY_FLIP=1` for one mounted upside down in the bezel.

## Install

```bash
./deploy-segdisplay.sh [user@host]      # default vcr@240mp.local
```

Copies the module to `<appRoot>/modules/segdisplay/`, installs and starts
`segdisplay.service`, and patches upstream's `Main.qml`.

**Re-run it after every 240-MP upgrade.** The module directory survives an
upgrade, but `Main.qml` does not — and without the patch the panel falls back to
showing only the clock, silently.

## How it works

240-MP has no GPIO/I2C code, and its QML can neither spawn a process nor write a
file. So:

- `scripts/segdisplay.py` is a small stdlib-only daemon — no `smbus2`, no
  `luma.oled`, no PIL. It owns the I2C bus and listens on `127.0.0.1:8377`. It
  reads its own settings straight out of 240-MP's `config.json`, so the
  per-module toggles need no plumbing.
- `views/SegHook.qml` is loaded once from `Main.qml` and POSTs the current view
  path plus mpv's playback position. It uses `mpvController`'s existing
  `positionChanged` / `playbackEnded` signals, which is why every playback
  module — including ones this project did not write — gets a timer for free.
- `views/HdhrService.qml` (the HDHomeRun module) POSTs the channel number as the
  cursor moves and when a channel is tuned.
- `views/Root.qml` is a status screen: is the daemon up, did the device answer,
  what is on the panel right now, and `T` for a display test.

Priority when several could apply: **setup > channel > elapsed > clock > blank**.
HDHomeRun stays on the channel even during playback — live TV has no elapsed
time worth showing.

The glyphs are rectangles, not a bitmap font: a digit is 20x30 with a 4-pixel
stroke, and the seven segment rectangles are indexed by the same `a..g` bit
order the HT16K33 font used, so `encode()` and the font survived the hardware
change untouched. Four digits, two 5-pixel gaps and one 9-pixel colon gap come
to 99 of the 128 columns; the slack is what the burn-in shift moves into.

## Bring-up and debugging

```bash
python3 segdisplay.py --probe            # does 0x3C answer?
python3 segdisplay.py --test             # all pixels, each segment, 8.8.8.8 + colon
python3 segdisplay.py --show 12.10       # or "StP", "03:47"; add --ghost
python3 segdisplay.py --clear
journalctl -u segdisplay.service -f
```

`--test` starts with an all-pixels frame on purpose: it answers "is the whole
128x32 area alive and the right way up" before the glyph geometry is in play.

Without the display attached the daemon still runs, serving `/status` with
`"connected": false`, so the app side can be tested on a machine with no
hardware.

Two log lines mark the app side, once each per run:
`[SegDisplay] hook active` (Main.qml was patched) and
`[SegDisplay] panel daemon reachable` / `unreachable`.

## Files

| Path | Goes to |
|---|---|
| `manifest.json`, `views/*.qml`, `assets/images/logo.svg` | `<appRoot>/modules/segdisplay/` |
| `scripts/segdisplay.py`, `scripts/patch_main_qml.py` | `<appRoot>/modules/segdisplay/scripts/` |
| `scripts/segdisplay.service` | `/etc/systemd/system/` (placeholders substituted) |
