**Title:** feat(musictv): a music-video channel built from YouTube playlists

**Branch:** `factorsparsity:musictv-module` → `anthonycaccese/240-MP:main`

---

## Summary

A music-video channel: a bumper, then two to four music videos, then another
bumper, continuing until the viewer leaves. The videos come from the user's own
YouTube playlists.

**Files**

- New `modules/musictv/`:
  - manifest, 2 QML views and a logo
  - an mpv Lua overlay
  - a Python screen-bug converter and its 2 systemd units
  - 2 OFL fonts and a README
- New `src/modules/musictv/MusicTvBackend`.
- One `registerModule` line in `src/main.cpp` and one source line in
  `CMakeLists.txt`.

**What it does**

- Two playlist files in the data directory, one for music videos and one for
  bumpers. They use the same line format as the YouTube module's
  `youtube_playlists.txt`. Bumper lines can also be single video URLs.
- Each music video gets a title card in the lower left: artist and song, plus
  album, label and director when supplied in an optional `musictv_metadata.json`.
  The card fades in two seconds after the video starts, again near the end, and
  whenever the on-screen controls come up.
- An optional channel bug in the corner. **No bug images ship with the module.**
  A channel ident is somebody's trademark, so the user drops their own
  transparent PNGs into a directory.
- Bumpers deliberately get neither card nor bug.
- Settings: playback resolution, title cards on/off, bug on/off, videos between
  bumpers, and a **Title Font** choice.

**Implementation notes worth your attention**

- **A small backend, following `YouTubeBackend`.** `MusicTvBackend` lists each
  playlist with `yt-dlp --flat-playlist`, because a playlist's RSS feed stops at
  15 entries. It parses artist and song from each title and writes
  `musictv_cache.json`.
  - It rebuilds only when a playlist or metadata file is newer than the cache, or
    the cache is a day old, so YouTube take-downs drop out. Otherwise it serves
    the cache immediately.
  - A failed rebuild falls back to the previous cache, so a network blip doesn't
    take the channel off the air.
  - Editing a playlist file just works the next time the module opens. The
    module doesn't need `QML_XHR_ALLOW_FILE_READ`.
- **One mpv launch, not one per video.** The interleaved rotation is handed to
  mpv as a single playlist (bare URLs after the first), so yt-dlp resolves each
  entry lazily as playback reaches it. Launching mpv once per video switches VT
  and re-takes DRM master on a Pi, which puts a black gap between every video.
- **The overlays are drawn inside mpv**, by `scripts/musictv-overlay.lua`,
  because mpv owns the screen during playback and nothing Qt draws is visible
  then.
  - The card is its own `osd-overlay`, never `set_osd_ass()`, so it can't collide
    with `scripts/mpv-osc.lua`.
  - The overlay is configured with `--script-opts-append`, never `--script-opts`,
    since `MpvController` builds its own list and a second flag would replace it.
  - It reads the same `musictv_cache.json` for the card text.
- **The bug uses mpv's `overlay-add`**, which composites without touching the
  decode path: no `--vf` or `--lavfi-complex`, so hardware decoding is
  unaffected.
  - `overlay-add` takes raw premultiplied BGRA, so `scripts/musictv_bugs.py`
    converts the user's PNGs using only the Python standard library. There's no
    ffmpeg CLI on a Pi install.
  - It crops transparent margins, so a source image of any size lands the same
    way.
  - A systemd `.path` unit converts new PNGs automatically, or the user runs the
    script by hand.
- **Fonts.** The default is Josefin Sans (SIL OFL 1.1, license text included), a
  geometric sans based on the 1927 Kabel these cards imitate. The Title Font
  setting can switch to Kabel for users who own a licensed copy; it isn't
  redistributable, so it isn't shipped.
  - The fonts go in the user font directory (`~/.local/share/fonts` or
    `~/Library/Fonts`). Something I ran into that you might want to know:
    `MpvController` appends its own `--osd-fonts-dir` after `extraArgs` on macOS,
    and mpv keeps only the last one. So a module can't point mpv at its own font
    directory without breaking the OSC font.

Everything the module writes stays in the data directory. The only network
traffic is yt-dlp listing playlists and mpv fetching the streams.

## Setup

The data directory is `~/.local/share/240-MP` on Linux and `~/Library/Application Support/240-MP` on macOS.

1. **Enable the module** under **Settings → Music TV**.
2. **Keep yt-dlp current.** It's used both to list playlists and to play videos.
   An outdated yt-dlp gets HTTP 403s on many official label uploads, and mpv
   silently skips those entries, so the channel ends up playing only bumpers.
   - The simplest setup is the release binary in `<data directory>/bin/yt-dlp`,
     which the app prefers over `PATH`. Update it with `yt-dlp -U`.
3. **Add music playlists** to `musictv_playlists.txt` in the data directory. This
   file is required. Use one YouTube playlist (or single video) per line. A
   `Name |` prefix is optional, and `#` starts a comment:
   ```
   # music videos
   80s Hits | https://www.youtube.com/playlist?list=PLxxxxxxxxxxxx
   90s Hits | https://www.youtube.com/playlist?list=PLyyyyyyyyyyyy
   ```
4. **Add bumpers (optional)** to `musictv_bumpers.txt`. It uses the same format,
   and single video URLs are handy here. Keep bumpers short: anything there plays
   between music videos.
5. **Add title-card extras (optional)** in `musictv_metadata.json`. It's keyed by
   video ID, and any field set there wins over what's parsed from the title:
   ```json
   {"UG3VcCAlUgE": {"album": "Purple Rain", "label": "Warner Bros. Records", "director": "Prince"}}
   ```
6. **Install the title font** into your user font folder. mpv draws the card, so
   the font has to be visible to mpv:
   ```bash
   # Linux
   mkdir -p ~/.local/share/fonts
   cp /opt/240mp/share/240mp/modules/musictv/assets/fonts/*.ttf ~/.local/share/fonts/
   fc-cache -f
   # macOS
   cp modules/musictv/assets/fonts/*.ttf ~/Library/Fonts/
   ```
   - To use Kabel, install your own licensed copy the same way, then set
     **Settings → Music TV → Title Font** to `Kabel`.
7. **Add screen bugs (optional).**
   - Create a `musictv_bugs/` folder in the data directory and drop in PNGs with
     **real transparency**. An opaque image draws as a solid box, and the
     converter warns about it.
   - Each PNG has to be converted once, into the raw image format mpv's overlay
     needs:
     ```bash
     python3 /opt/240mp/share/240mp/modules/musictv/scripts/musictv_bugs.py
     ```
   - To convert new PNGs automatically on Linux, install the bug units with the
     same placeholder substitution as the HDHomeRun units:
     ```bash
     MOD=/opt/240mp/share/240mp/modules/musictv
     for u in musictv-bugs.path musictv-bugs.service; do
       sed -e "s|@USER@|$USER|g" -e "s|@MODULE_DIR@|$MOD|g" -e "s|@HOME@|$HOME|g" \
         "$MOD/scripts/$u" | sudo tee "/etc/systemd/system/$u" >/dev/null
     done
     sudo systemctl daemon-reload && sudo systemctl enable --now musictv-bugs.path
     ```
   - Removing a PNG takes it out of the rotation.
8. **Open Music TV.**
   - The first open builds the playlist cache, showing "UPDATING PLAYLISTS..."
     for a few seconds.
   - After editing any of the files above, the next open picks up the change.
     The cache also refreshes daily.
   - To force a rebuild, delete `musictv_cache.json`.

## AI involvement

The code was written with Claude Code: the QML views, the C++ backend, the mpv
Lua overlay, the Python bug converter and the README. The backend's title parsing
is a port of an earlier Python version. I checked that its output matched field
for field on a 631-video run. I reviewed the code and did all of the hardware
testing below myself. This description was drafted with Claude Code, and I
edited it.

## Testing

- Platform(s) tested: Raspberry Pi 4B (1GB, arm64, Raspberry Pi OS Trixie),
  release build of this branch on current `main`. Also a macOS source build.
- Display / output tested: CRT over composite, navigated with a USB remote.
  macOS on the built-in display.

What I tested:

- A 631-video rotation across two music playlists and a bumper playlist.
- The cache rebuilding after a playlist edit, and served from the cache otherwise.
- Title-card timing and placement at both showings and with the OSC open, plus
  legibility tuned against video on the CRT.
- Screen-bug placement and rotation.
- Both overlay settings on and off, and both title fonts.

Not tested on Windows, Steam Deck, or Pi 3/5.

## Checklist

- [x] Changes work with remote-only navigation (up/down/left/right, enter, esc/backspace)
- [x] Handled sizing and positioning using the `root.sh` & `root.sw` properties (did not hardcode pixels) and kept CRT overscan in mind
- [x] Did not add tracking or analytics; only wrote settings to the local data directory if the change required storage
- [x] Follows the patterns in [ARCHITECTURE.md](https://github.com/anthonycaccese/240-MP/blob/main/ARCHITECTURE.md) and the principles in [CONTRIBUTING.md](https://github.com/anthonycaccese/240-MP/blob/main/CONTRIBUTING.md)
