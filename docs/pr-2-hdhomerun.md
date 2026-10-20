**Title:** feat(hdhomerun): live TV and an ATSC PSIP guide

**Branch:** `factorsparsity:hdhomerun-module` → `anthonycaccese/240-MP:main`

---

## Summary

A new module for HDHomeRun network tuners: live over-the-air TV, plus a
programme guide built from the broadcast's own PSIP data.

**Files**

- New `modules/hdhomerun/`:
  - manifest, 6 QML views and a logo
  - a Python guide scanner (3 scripts) and an optional nightly systemd timer
  - an mpv Lua script
  - README
- New `src/modules/hdhomerun/HDHomeRunBackend`.
- One `registerModule` line in `src/main.cpp`, one source line in
  `CMakeLists.txt`, and `__pycache__/`/`*.pyc` added to `.gitignore`. Running
  the scanner in place writes bytecode next to it.

**What it does**

- Channel list from the tuner's `lineup.json`, with playback handed to mpv.
- Guide grid:
  - channels down the left and a scrolling time window across the top
  - programme blocks sized by duration
  - a detail pane
  - `Start In` chooses which view opens first
- Backing out of a channel returns to the guide with that channel still selected.
- **Tuner** setting, filled by UDP broadcast discovery on the LAN (no internet
  needed). The same list is in an in-module picker (**D** from the channel list),
  which also opens on its own when no tuner is set.
- **Tuner fallback.** An HDHomeRun refuses a stream (HTTP 503) when all its
  tuners are in use, by other clients on the LAN or by a guide scan. Before
  tuning, the module checks each device's `status.json` for a free tuner, in
  this order:
  1. **Tuner**
  2. **Backup Tuner** (default Automatic)
  3. every other discovered tuner that carries the channel

  Tuners without a hardware encoder always come last, whatever the settings say:
  they send full-rate MPEG-2, which a Pi decodes in software. If no tuner is
  free, the guide says **TUNERS BUSY** (or **NO TUNER** when none answers)
  instead of the player silently dropping back. A stream that still fails gets
  the same explanation.
- Optional hardware transcoding on EXTEND models. The **Video Quality** setting
  only appears when the selected tuner has the encoder. The default is
  `internet480` (704x480): what a 480i CRT wants, at about a ninth of the 1080p
  bitrate.
- **Guide scanning from the app.** Pressing **R** in the guide, or **Settings →
  Rescan Guide Data**, starts a scan in the background. The guide's top-left cell
  shows its progress (`FINDING 14/35` during the one-time sweep, then
  `SCANNING 3/12` with a progress bar), then `GUIDE UPDATED`, and the guide
  reloads.
- **Per-channel crop memory.** Press the OSC's CROP button on a channel and it
  stays cropped the next time you tune it.
  - Done by `scripts/hdhr-crop.lua` inside mpv, because QML can neither read
    mpv's `panscan` property nor write a file.
  - Channels never cropped by hand still follow the app's `auto_crop` setting.
- The live player forwards all eight OSC keys to mpv.

**Design notes worth your attention**

- **A deliberately small backend.** The tuner is a plain HTTP server on the LAN
  (no SDK, account, key, or listings service), so lineup, tuning and stream URLs
  stay in `views/HdhrService.qml`. `HDHomeRunBackend` covers only what QML can't
  do:
  - Read the guide cache with `QFile`, so the module doesn't need
    `QML_XHR_ALLOW_FILE_READ`.
  - Start a scan (`QProcess::startDetached`, so a long scan outlives the app) and
    report its status.
  - UDP discovery for the Tuner and Backup Tuner `options_slot`, plus a
    `transcode` capability (via `apply_slot`) that gates Video Quality.
- **Priority as two settings.** There's no ordered-list setting type, so the
  tuner order is Tuner plus Backup Tuner, with the rest in discovery order. That
  covers a full order for up to three tuners.
- **Legacy tuners are left out.** Models with `"Legacy":1` in `discover.json`
  (the original DUAL, for one) have no HTTP streaming or local `lineup.json`, so
  discovery skips them rather than offering a tuner that can never play.
- **The guide scanner stays in Python**, standard library only (no pip install),
  run as a child process the way yt-dlp is. Every scan goes through one entry
  point, `epg-rescan-trigger.py`, which scans the configured tuner and holds a
  lock, so an in-app scan and the nightly timer can't fight over the tuner. A
  scan that captures nothing leaves the existing cache alone.
- **Scan feedback lives in the guide**, because settings action rows show no
  feedback. Running state comes from the scan lock, so scans started by the
  nightly timer show too. Progress comes from the scan log, which in-app scans
  write. A generic way for action slots to report a result in Settings would
  help modules like this one, and YouTube's delete actions too. Happy to propose
  that separately if you're interested.
- **Letter keys.** `R`, `C`, `D` and `>` are keyboard shortcuts. Everything they
  do is also reachable with remote-only navigation: Rescan and Tuner are in
  Settings, and Start In picks the first view.

**About the guide data**

EIT tables aren't a "now playing" ticker. Each EIT-N covers a three-hour window
and they cycle continuously, so one capture per multiplex yields a station's
whole published schedule, often several days of it.

Two findings that shaped the scanner, both learned the hard way:

- **You can't scan PSIP from a `lineup.json` URL.** That URL makes the tuner
  remux down to the single requested programme. The result contains only PAT and
  PMT, with no MGT/VCT/EIT/ETT at all. Scanning requires tuning the physical RF
  channel (`/auto/ch<RF>`).
- **Virtual channel majors don't map one-to-one onto RF channels.** In my market,
  `9.1/9.2/9.3/9.7` sit on one multiplex and `9.4/9.5/9.6/9.8/9.10` on another.
  Grouping by major number would silently lose half a station's listings, so the
  scanner sweeps RF once and caches the real map.

A cold scan takes about 90 minutes, mostly the one-time sweep. After that, scans
are shorter.

Data and network:

- Output is a JSON cache, a scan log and a small crop-memory file, all in the
  data directory. Nothing is written anywhere else.
- The only network traffic is to tuners on the LAN.

## Setup

The data directory is `~/.local/share/240-MP` on Linux and
`~/Library/Application Support/240-MP` on macOS.

1. **Enable the module** under **Settings → HDHomeRun**.
2. **Choose a tuner** under **Settings → HDHomeRun → Tuner**. Tuners on the same
   network are found automatically. Alternatively, open the module: the picker
   appears by itself the first time, and **D** in the channel list reopens it.
   - A tuner discovery can't reach (another subnet, say) can be set by IP: put it
     in `device_id` under `modules → com.240mp.hdhomerun` in `config.json`.
   - **With more than one tuner**, set **Backup Tuner** to the next choice, or
     leave it on Automatic. Fallback needs no other setup.
3. **Build the guide.** Press **R** in the guide, or use **Settings → HDHomeRun →
   Rescan Guide Data**. Progress shows in the guide's top-left cell. The first
   scan takes about 90 minutes. Requires `python3`, standard library only.
4. **Optional: refresh nightly (Linux).** Install the timer. Its units carry
   placeholders to fill in: `@USER@` (the user that runs 240-MP), `@MODULE_DIR@`
   (the module's path) and `@HOME@` (that user's home).
   ```bash
   MOD=/opt/240mp/share/240mp/modules/hdhomerun
   for u in epg-scan.service epg-scan.timer; do
     sed -e "s|@USER@|$USER|g" -e "s|@MODULE_DIR@|$MOD|g" -e "s|@HOME@|$HOME|g" \
       "$MOD/scripts/$u" | sudo tee "/etc/systemd/system/$u" >/dev/null
   done
   sudo systemctl daemon-reload && sudo systemctl enable --now epg-scan.timer
   ```
5. **Optional settings:**
   - **Video Quality** (EXTEND tuners only) sets the transcode profile.
   - **Backup Tuner** picks which tuner to try next when Tuner is busy.
   - **Start In** chooses whether the guide or the channel list opens first.
6. **Crop memory needs no setup.** Toggle CROP in the on-screen controls and the
   channel remembers it.

## AI involvement

The code was written with Claude Code: the C++ backend, the QML views, the PSIP
parser, the scanner, the crop script and the README. I reviewed it and did all
of the hardware testing below myself, including the market-wide guide
comparison. This description was drafted with Claude Code, and I edited it.

## Testing

- Platform(s) tested: Raspberry Pi 4B (1GB, arm64, Raspberry Pi OS Trixie),
  release build of this branch on current `main`. Also a macOS source build.
- Display / output tested: CRT over composite, navigated with a USB remote.
- Tuners: two HDHomeRun EXTEND (HDTC-2US) and a DUAL (HDHR-US) on one LAN, with
  a live antenna.

What I tested:

- Channel list, playback, the guide grid, and both `Start In` values.
- The Tuner setting and the in-module picker listing both EXTENDs by UDP
  discovery, with the legacy DUAL left out. Video Quality appearing for the
  EXTENDs as the Tuner changes.
- The guide reading its cache with no `QML_XHR_ALLOW_FILE_READ` in the
  environment.
- Starting a scan from Settings and from R; a second request while one runs is
  refused; the scan survives the app quitting; the guide shows progress and
  reloads when done.
- Returning to the guide on the channel just watched.
- Tuner fallback: with both tuners on the chosen EXTEND in use (another client
  and a guide scan), channels played from the other EXTEND. Hopping channels
  fast enough that the tuners hadn't been released yet showed TUNERS BUSY in
  the guide.
- Crop memory: crop a channel, retune it, and an uncropped channel is left alone.
- Guide scanning against a full market: 2696 events across 48 channels,
  cross-checked event by event against an independent TSDuck capture. There were
  zero title, end-time or description mismatches.

Not tested on Windows, Steam Deck, or Pi 3/5.

Known limits are in the module README: weak channels yield no listings (their
PSIP fails CRC), and a Pi Zero W is too slow.

## Checklist

- [x] Changes work with remote-only navigation (up/down/left/right, enter, esc/backspace)
- [x] Handled sizing and positioning using the `root.sh` & `root.sw` properties (did not hardcode pixels) and kept CRT overscan in mind
- [x] Did not add tracking or analytics; only wrote settings to the local data directory if the change required storage
- [x] Follows the patterns in [ARCHITECTURE.md](https://github.com/anthonycaccese/240-MP/blob/main/ARCHITECTURE.md) and the principles in [CONTRIBUTING.md](https://github.com/anthonycaccese/240-MP/blob/main/CONTRIBUTING.md)
