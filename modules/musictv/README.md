# Music TV module for 240-MP

A music-video channel for [240-MP](https://github.com/anthonycaccese/240-MP),
in the shape a 1980s music-video channel broadcast in: a bumper, then two to
four music videos, then another bumper, on and on until you leave. Each music
video gets a title card in the corner and an optional channel bug.

Videos come from YouTube playlists you choose. Nothing is compiled — the module
is QML, Python and one mpv Lua script.

## What it does

- **Continuous rotation.** Bumper → 2-4 music videos → bumper → … The whole run
  is handed to a single mpv playlist, so there is no gap between videos.
- **Title cards.** Artist, song, and optionally album, label and director,
  fading in two seconds after the video starts, again near the end, and whenever
  you bring up the on-screen controls.
- **Screen bugs.** A transparent logo in the corner, drawn by mpv itself. You
  supply the images; none ship with the module. Bumpers get neither card nor bug.

## Install

Copy `manifest.json`, `views/`, `scripts/` and `assets/` into
`<appRoot>/modules/musictv/` — appRoot is the directory holding 240-MP's
`modules/` (the checkout for a source build, `/opt/240mp/share/240mp` for a
release install). Then, on Linux:

```bash
# QML bytecode is cached by mtime+size; stale bytecode silently wins.
rm -rf ~/.cache/240-MP/qmlcache

# The title card is drawn by mpv, not Qt, so the font has to reach fontconfig.
cp assets/fonts/*.ttf ~/.local/share/fonts/ && fc-cache -f

# Playlist fetching and bug conversion. The units carry @USER@/@MODULE_DIR@/
# @HOME@ placeholders — substitute them for your install first.
sudo cp scripts/musictv-*.{service,timer,path} /etc/systemd/system/
sudo systemctl enable --now musictv-fetch.path musictv-bugs.path musictv-fetch.timer
sudo systemctl start musictv-fetch.service
```

The module's source repo ships a `deploy-musictv.sh` that does all of this over
SSH, including the placeholder substitution.

Requires `yt-dlp` on the box (the YouTube module needs it too) and
`QML_XHR_ALLOW_FILE_READ=1` in the app's environment — without it the module
cannot read its own cache and sits on "TUNING IN…" with nothing in the log. The
deploy script writes a systemd drop-in for it.

## Configure

Everything you own lives in 240-MP's data directory (`~/.local/share/240-MP` on
Linux, `~/Library/Application Support/240-MP` on macOS):

| File | What it is |
|---|---|
| `musictv_playlists.txt` | Music videos. One YouTube playlist URL per line; `Name \| <url>` optional. |
| `musictv_bumpers.txt` | Bumpers/idents. Playlist URLs **or** single video URLs. |
| `musictv_metadata.json` | Optional. Album/label/director for the title card, keyed by video id. |
| `musictv_bugs/` | Your transparent PNG screen bugs. Drop files in; they join the rotation. |
| `musictv_cache.json` | Generated. Don't edit. |

Saving either playlist file rebuilds the cache automatically; adding a PNG
converts it automatically. To force a rebuild:

```bash
sudo systemctl start musictv-fetch.service
journalctl -u musictv-fetch.service -f
```

Artist and song are parsed from the video title. `musictv_metadata.json` fills
in what YouTube cannot tell us, and anything set there wins:

```json
{"UG3VcCAlUgE": {"album": "Purple Rain",
                 "label": "Original Motion Picture Soundtrack",
                 "director": "Prince"}}
```

Screen bugs must be **PNG with real transparency** — an opaque frame-grab
converts happily and then paints a solid rectangle over the corner of the
screen. The converter warns when it sees one. Animated GIFs are not supported:
mpv composites a single still frame.

## Settings (Settings → Music TV)

| Setting | Default | What it does |
|---|---|---|
| Enabled | OFF | Show the module in the menu. |
| Playback Resolution | 480p | Cap handed to yt-dlp. 480p is what a CRT wants. |
| Show Video Titles | ON | The title card. |
| Show Screen Bug | ON | The corner logo. |
| Videos Between Bumpers | 2-4 | `2-4` re-rolls per segment; or pick a fixed 1-5. |

## How it works

`views/Player.qml` reads `musictv_cache.json`, shuffles each pool, interleaves
them and hands the whole run to **one** mpv launch — mpv resolves each URL
through yt-dlp as it reaches it.

The card and bug are drawn by `scripts/musictv-overlay.lua` **inside mpv**,
because mpv owns the screen during playback and nothing Qt draws is visible
then. The bug is composited with mpv's `overlay-add`, which takes raw
premultiplied BGRA, so `scripts/musictv_bugs.py` converts PNGs using only the
Python standard library. `scripts/musictv_fetch.py` runs
`yt-dlp --flat-playlist` to build the cache, driven by a systemd `.path` unit on
the playlist files plus a daily timer.

The title font is named, not hardcoded — pass
`--script-opts-append=musictv-font=<family>` to use something else.

## Credits and licensing

- **Code:** GPL-3.0, matching
  [240-MP](https://github.com/anthonycaccese/240-MP/blob/main/LICENSE).
- **Font:** [Josefin Sans](https://fonts.google.com/specimen/Josefin+Sans) by
  Santiago Orozco / Typemade, SIL Open Font License 1.1 — full text in
  [assets/fonts/LICENSE-josefin-sans.txt](assets/fonts/LICENSE-josefin-sans.txt).
  A geometric sans based on Rudolf Koch's Kabel (1927), which is the lettering
  these title cards imitate. SemiBold is the default because Regular is too
  light to hold up over moving video.
- **Screen bugs:** none are included, deliberately. A channel ident is somebody's
  trademark; supply your own images and the module will use them.
- Playback is [mpv](https://mpv.io/); listing is
  [yt-dlp](https://github.com/yt-dlp/yt-dlp). Neither is bundled.
- No tracking, no analytics, no server of ours. The only network traffic is
  yt-dlp talking to YouTube, and mpv fetching the streams.

## Development notes

The code in this module was written with
[Claude Code](https://www.anthropic.com/claude-code) — QML views, the mpv Lua
overlay, the Python scripts and this documentation. All behaviour was tested by
a human on real hardware: a Raspberry Pi 4 running 240-MP on a CRT, driven with
a USB remote, which is also where the layout and timing were tuned.
