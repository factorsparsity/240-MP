# HDHomeRun module for 240-MP

Live over-the-air TV and a retro programme guide for
[240-MP](https://github.com/anthonycaccese/240-MP), driven by an HDHomeRun
network tuner.


## What it does

- **Live TV.** Reads the tuner's `lineup.json` and hands the stream URL to
  mpv. No SDK, no account, no cloud service — the tuner is a plain HTTP
  server on your LAN.
- **A real programme guide.** Channels down the left, a scrolling time window
  across the top, programme blocks sized by duration, and a detail pane.
  Guide data is parsed from the broadcast itself (ATSC PSIP), not fetched
  from a paid listings service.
- **Automatic tuner discovery** by UDP broadcast on the LAN (no internet
  needed), in Settings and in the module's own picker.
- **Optional hardware transcoding** on HDHomeRun EXTEND models, defaulting
  to 704x480 — which is what a 480i CRT actually wants, and about a ninth of
  the bitrate of the 1080p stream.

## Requirements

- An HDHomeRun tuner. Developed against an **EXTEND (HDTC-2US)**; a
  non-transcoding model (e.g. **DUAL / HDHR-US**) works fine, it just hides
  the video-quality setting.
- Python 3 for the guide scanner — **standard library only**, no packages to
  install.
- An antenna with decent signal. Guide data comes off the air, so a marginal
  channel yields no listings (see [Troubleshooting](#troubleshooting)).

## Setup

The module ships with 240-MP. Enable **HDHomeRun** in Settings, then choose a
tuner under **Settings → HDHomeRun → Tuner** (or press **D** in the channel
list; the picker also opens by itself when no tuner is set).

Guide data needs one scan: press **R** in the guide, or use **Settings →
HDHomeRun → Rescan Guide Data**. It runs in the background (logging to
`epg_scan.log` in the data directory), and the guide's top-left cell shows its
progress — `SCANNING 3/12` — then `GUIDE UPDATED` and reloads when it finishes. To
refresh the guide nightly as well, install the optional timer (Linux) — the
units carry `@USER@`/`@MODULE_DIR@`/`@HOME@` placeholders to fill in first:

```bash
MOD=/opt/240mp/share/240mp/modules/hdhomerun   # the module's install path
for u in epg-scan.service epg-scan.timer; do
  sed -e "s|@USER@|$USER|g" -e "s|@MODULE_DIR@|$MOD|g" -e "s|@HOME@|$HOME|g" \
    "$MOD/scripts/$u" | sudo tee "/etc/systemd/system/$u" >/dev/null
done
sudo systemctl daemon-reload && sudo systemctl enable --now epg-scan.timer
```

Note that video playback needs the Pi's own console — mpv draws to the
framebuffer and cannot take the screen over SSH.

## Settings

| Setting | What it does |
|---|---|
| Enabled | Show the module |
| Start In | Which view opens first: **Guide** (default) or Channels. `>` opens the guide from the channel list, `C` opens the channel list from the guide, `R` in the guide starts a rescan |
| Tuner | HDHomeRun tuners found on the LAN by UDP broadcast |
| Video Quality | Transcode profile. Shown only when the selected tuner has an encoder (EXTEND) |
| Rescan Guide Data | Starts a guide scan in the background (~90 min the first time) |

A tuner that broadcast discovery cannot reach (a different subnet, say) can be
set by IP: put it in `device_id` under `modules → com.240mp.hdhomerun` in
`config.json`.

Scans share one lock, so a rescan started while the nightly scan is running
(or vice versa) is skipped rather than fighting it for the tuner.

Video Quality options, measured on a real EXTEND:

| profile | codec | resolution | bitrate |
|---|---|---|---|
| `none` | MPEG-2 | 1920x1080 | 6.4 Mbps |
| `heavy` | H.264 | 1920x1080 | 8.4 Mbps |
| `mobile` | H.264 | 1280x540 | 2.5 Mbps |
| `internet720` | H.264 | 960x540 | 1.5 Mbps |
| **`internet480`** (default) | H.264 | 704x480 | 0.9 Mbps |
| `internet360` | H.264 | 528x360 | 0.8 Mbps |
| `internet240` | H.264 | 352x240 | 0.7 Mbps |

Note that `none` means *MPEG-2*, not "no preference" — leaving the profile
unset does not give you the raw broadcast, because the device applies its own
configured default.

## Guide data

`scripts/epg_scanner.py` tunes each physical RF channel in turn and parses the
ATSC PSIP tables carried in the broadcast, writing a JSON cache the app reads.

```bash
HDHOMERUN_IP=<tuner-ip> python3 scripts/epg_scanner.py
```

By default it writes to `~/.local/share/240-MP/epg_cache.json`, which is where
the app looks on Linux. **On macOS you must set `EPG_CACHE_PATH`** to
`~/Library/Application Support/240-MP/epg_cache.json`, or the app won't see it.

A first scan takes roughly 90 minutes: it sweeps the whole RF band once to
learn which multiplex carries which channels, then captures each one for 150
seconds. The RF map is cached, so later scans skip the sweep. One capture
picks up the station's entire published schedule — often several days — so
this is a nightly job, not a polling loop.

In the app, **Rescan Guide Data** runs this for you; see [Setup](#setup) for
the optional nightly timer.

## Troubleshooting

**A channel plays but has no listings.** Almost always weak signal. Guide data
rides on the broadcast, and PSIP sections that fail their checksum are
discarded — so a channel can be perfectly watchable while its listings never
arrive intact. Compare `SignalQualityPercent` in `http://<tuner-ip>/status.json`
against a channel that does work; in testing, everything at 98-100% produced
listings and everything in the 40s produced none.

**Guide data is thin a day or two out.** The later portions of the schedule
cycle more slowly on the broadcast, especially on multiplexes carrying many
subchannels. Raise `CAPTURE_SECONDS` in `scripts/epg_scanner.py`.

**A whole multiplex is missing.** If both tuners were busy when the RF sweep
ran, that RF channel is absent from the cached map and won't be scanned again.
Delete `epg_cache.json` to force a fresh sweep.

**A setting does nothing.** Manifest slot names are resolved by name at
runtime, so a typo fails silently in the UI — but 240-MP logs a warning.
Check the app's console output.

## Module layout

| Path | |
|---|---|
| `src/modules/hdhomerun/HDHomeRunBackend` | C++: guide read, scan launch, UDP tuner discovery |
| `views/HdhrService.qml` | Lineup, tuning and stream URLs over the tuner's HTTP API |
| `views/` | QML: channel list, guide grid, player, tuner picker |
| `scripts/psip.py` | Dependency-free ATSC PSIP parser |
| `scripts/epg_scanner.py` | Guide scanner built on it |
| `scripts/epg-rescan-trigger.py` | Runs one scan (locked) against the configured tuner |
| `scripts/hdhr-crop.lua` | mpv script: per-channel crop memory |
| `scripts/epg-scan.{service,timer}` | Optional nightly scan |

## Known limits

- Discovery is a LAN broadcast, so a tuner on another subnet must be set by IP
  (see [Settings](#settings)).
- **A Pi Zero W is not sufficient.** It builds and runs, but video can't keep
  up and navigation is slow — a CPU limit, with GPU acceleration confirmed
  working, so it isn't a fixable misconfiguration. Use a Pi 3B+/4 or Zero 2 W.

## Credits and licensing

- **Code:** GPL-3.0, matching
  [240-MP](https://github.com/anthonycaccese/240-MP/blob/main/LICENSE), which
  this module is built for (thanks to Anthony Caccese for a shell that makes a
  module this small possible).
- **HDHomeRun** is a product of [SiliconDust](https://www.silicondust.com/).
  This module talks only to the tuner's documented local HTTP API and is not
  affiliated with or endorsed by them. No account, no cloud service, no key.
- **Guide data** is ATSC PSIP, parsed off the broadcast by
  `scripts/psip.py` — no third-party listings provider, and no dependency
  beyond the Python standard library. The parser was validated against
  [TSDuck](https://tsduck.io/), which the implementation originally used and no
  longer needs.
- Playback is handed to [mpv](https://mpv.io/), which 240-MP already ships.
- `assets/images/logo.svg` was drawn for this module; it is GPL-3.0 with the
  rest.
- No tracking, no analytics, no server of ours. The only network traffic is to
  your tuner on the LAN, plus HDHomeRun's discovery endpoint when you use the
  device picker.

## Development notes

The code in this module was written with
[Claude Code](https://www.anthropic.com/claude-code) — the QML views and
`HdhrService.qml`, the PSIP parser and guide scanner, the deploy tooling and
this documentation. All behaviour was tested by a human on real hardware: an
HDHomeRun EXTEND tuner and a live antenna feed, with a Raspberry Pi 4 running
240-MP on a CRT and driven by a USB remote.
