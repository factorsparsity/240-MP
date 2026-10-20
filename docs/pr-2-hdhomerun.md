**Title:** feat(hdhomerun): live TV and an ATSC PSIP guide

**Branch:** `factorsparsity:hdhomerun-module` → `anthonycaccese/240-MP:main`

---

## Summary

A new module for HDHomeRun network tuners: live over-the-air TV, plus a
programme guide built from the broadcast's own PSIP data.

Everything is in `modules/hdhomerun/`:

- manifest, 6 QML views and a logo
- 3 Python scripts and 4 systemd units
- 1 mpv Lua script
- README

There are no changes to the shell, `src/`, `CMakeLists.txt` or `main.cpp`.

**What it does**

- Channel list from the tuner's `lineup.json`, with playback handed to mpv.
- Guide grid:
  - channels down the left and a scrolling time window across the top
  - programme blocks sized by duration
  - a detail pane
  - `Start In` chooses which view opens first
- Backing out of a channel returns to the guide with that channel still selected.
- Tuner picker (**D** from the channel list), fed by HDHomeRun's discovery service.
- Optional hardware transcoding on EXTEND models. The default is `internet480`
  (704x480): that's what a 480i CRT wants, at about a ninth of the 1080p bitrate.
- **Per-channel crop memory.** Press the OSC's CROP button on a channel and it
  stays cropped the next time you tune it.
  - Done by `scripts/hdhr-crop.lua` inside mpv, because QML can neither read
    mpv's `panscan` property nor write a file.
  - Channels never cropped by hand still follow the app's `auto_crop` setting.
  - The script stands down where crop is unavailable, the same condition that
    hides the OSC button.
- The live player forwards all eight OSC keys to mpv.

**Pure QML: no backend, no shell changes.** `views/HdhrService.qml` handles the
lineup fetch, tuning, discovery and guide read. The tuner is a plain HTTP server
on the LAN: no SDK, account, key, or third-party listings service.

**Three consequences of having no backend**

These are the trade-offs, and I'd rather you decide than have me hide them. Each
one goes away with a small C++ backend instead, and I'm happy to write it. The
Music TV PR, sent alongside this one, shows that shape.

1. **The guide cache is read with `XMLHttpRequest`, so the app needs
   `QML_XHR_ALLOW_FILE_READ=1`.** Qt blocks local-file GET by default. When it's
   blocked, *neither* XHR callback fires: no error, no log line, just a silently
   empty guide. A backend using `QFile` would remove the requirement.
2. **Guide scanning is a Python script driven by systemd.** QML can't spawn a
   process, so "Rescan Guide Data" writes a flag that a `.path` unit picks up.
   - A backend with `QProcess` would make it a normal `action_slot`.
   - The scanner is standard-library-only Python (no pip install).
   - It takes a lock, so the nightly timer and an in-app rescan can't collide on
     the one tuner.
   - A scan that captures nothing leaves the existing cache alone.
3. **Tuner choice lives in the module, not in Settings.** A dynamically populated
   `list_single` needs an `options_slot` on a backend, so the picker is a view
   reached with **D**. A backend could also do real UDP discovery instead of
   HDHomeRun's HTTP service, which needs internet.

Also worth mentioning: backend-less modules have to build the data-directory
path in QML by hand, because `AppCore` exposes `homePath()` but no `dataPath()`.
A three-line accessor would fix that for good. Say the word and I'll send it
separately.

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

A cold scan takes about 90 minutes, mostly the one-time sweep. After that it's a
nightly job.

Data and network:

- Output is a JSON cache in the data directory, and the crop memory is one small
  text file beside it. Nothing is written anywhere else.
- The only network traffic is the tuner on the LAN, plus HDHomeRun's discovery
  endpoint when the picker opens.

## Setup

1. **Enable the module** under **Settings → HDHomeRun**.
2. **Allow local file reads for the guide.** The app needs
   `QML_XHR_ALLOW_FILE_READ=1` in its environment. Without it, the guide shows
   "NO GUIDE DATA" even with a full cache.
   - With the `install.sh` autostart service, add a drop-in:
     ```bash
     sudo mkdir -p /etc/systemd/system/240mp.service.d
     printf '[Service]\nEnvironment=QML_XHR_ALLOW_FILE_READ=1\n' \
       | sudo tee /etc/systemd/system/240mp.service.d/hdhomerun.conf
     sudo systemctl daemon-reload
     ```
   - When running by hand: `QML_XHR_ALLOW_FILE_READ=1 240mp`.
3. **Pick a tuner.** On first open the tuner picker appears by itself, filled
   from HDHomeRun's discovery service. Press **D** in the channel list to change
   it later.
   - Discovery needs internet. Offline, set `device_id` under
     `modules → com.240mp.hdhomerun` in `config.json`.
4. **Install the guide scanner units (Linux).** The units in `scripts/` carry
   placeholders that need filling in before install:
   - `@USER@`: the user that runs 240-MP
   - `@MODULE_DIR@`: the module's path
   - `@HOME@`: that user's home

   For a release install:
   ```bash
   MOD=/opt/240mp/share/240mp/modules/hdhomerun
   for u in epg-rescan.path epg-rescan.service epg-scan.service epg-scan.timer; do
     sed -e "s|@USER@|$USER|g" -e "s|@MODULE_DIR@|$MOD|g" -e "s|@HOME@|$HOME|g" \
       "$MOD/scripts/$u" | sudo tee "/etc/systemd/system/$u" >/dev/null
   done
   sudo systemctl daemon-reload
   sudo systemctl enable --now epg-rescan.path epg-scan.timer
   ```
   - `epg-rescan.path` makes the **Rescan Guide Data** setting work.
   - `epg-scan.timer` refreshes the guide nightly.
5. **Run the first scan.** Turn on **Settings → HDHomeRun → Rescan Guide Data**.
   The first scan takes about 90 minutes: one sweep of the RF band, then a
   capture per multiplex. Later scans skip the sweep.
   - On macOS there's no systemd, so run the scanner by hand:
     ```bash
     HDHOMERUN_IP=<tuner-ip> \
     EPG_CACHE_PATH="$HOME/Library/Application Support/240-MP/epg_cache.json" \
     python3 scripts/epg_scanner.py
     ```
6. **Optional:**
   - **Video Quality** chooses the transcode profile. EXTEND models only; the
     default is `internet480`.
   - **Start In** chooses whether the guide or the channel list opens first.
7. **Crop memory needs no setup.** Toggle CROP in the on-screen controls and the
   channel remembers it. The values are stored in `hdhr_crop.txt` in the data
   directory.

The data directory is `~/.local/share/240-MP` on Linux and `~/Library/Application Support/240-MP` on macOS.

## AI involvement

The code was written with Claude Code: the QML views, the PSIP parser, the
scanner, the crop script and the README. I reviewed it and did all of the
hardware testing below myself, including the market-wide guide comparison. This
description was drafted with Claude Code, and I edited it.

## Testing

- Platform(s) tested: Raspberry Pi 4B (1GB, arm64, Raspberry Pi OS Trixie),
  release build of this branch on current `main`. Also a macOS source build.
- Display / output tested: CRT over composite, navigated with a USB remote.
- Tuner: HDHomeRun EXTEND (HDTC-2US) with a live antenna.

What I tested:

- Channel list, playback, the guide grid, the tuner picker, and both
  `Start In` values.
- Returning to the guide on the channel just watched.
- Crop memory: crop a channel, retune it, and an uncropped channel is left alone.
- Guide scanning against a full market: 2696 events across 48 channels,
  cross-checked event by event against an independent TSDuck capture. There were
  zero title, end-time or description mismatches.

Not tested on Windows, Steam Deck, or Pi 3/5.

Known limits are in the module README: discovery needs internet, weak channels
yield no listings (their PSIP fails CRC), and a Pi Zero W is too slow.

## Checklist

- [x] Changes work with remote-only navigation (up/down/left/right, enter, esc/backspace)
- [x] Handled sizing and positioning using the `root.sh` & `root.sw` properties (did not hardcode pixels) and kept CRT overscan in mind
- [x] Did not add tracking or analytics; only wrote settings to the local data directory if the change required storage
- [x] Follows the patterns in [ARCHITECTURE.md](https://github.com/anthonycaccese/240-MP/blob/main/ARCHITECTURE.md) and the principles in [CONTRIBUTING.md](https://github.com/anthonycaccese/240-MP/blob/main/CONTRIBUTING.md)
